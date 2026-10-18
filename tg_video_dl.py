# -*- coding: utf-8 -*-
"""
Telegram 频道视频下载器（交互选择版）

功能：
- 扫描公开频道最近 N 个视频（可设最小大小过滤小视频）
- 下载前列出清单，输入编号排除不需要的（如 1,3,5-8）
- 任务级断点续传：任务清单实时落盘，中途关机/退出后重启可继续，
  已完成视频和已下分片不重新下载；失败视频可单独重试
- 下载中随时按 S 跳过当前视频
- 大文件(>4MB) 8MB 分片 x4 连接并行下载，分片级断点续传
- 下完自动把 moov 无损移到文件开头（快速起播），任何播放器都能直接放
- 下载停滞 90 秒自动掐断，最多重试 3 次
- 内置官方公开登录凭证，开箱即用；登录状态保存在本地数据目录

程序平级目录「TG视频下载器数据」保存全部数据：配置、登录会话、
任务清单、下载的视频，整个文件夹随程序一起拷贝即可换电脑使用。

命令行（可选）：tg_video_dl.py @频道名 数量 [最小MB] [关键词] [并发数]
     例：  tg_video_dl.py @somechan 20 50 "4K,预告片" 2
"""
import asyncio
import bisect
import collections
import ctypes
import difflib
import glob
import json
import os
import pathlib
import re
import shutil
import struct
import subprocess
import sys
import threading
import time
import unicodedata

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from telethon import TelegramClient
from telethon.tl.types import DocumentAttributeVideo, DocumentAttributeFilename
from telethon.errors import FloodWaitError, FileReferenceExpiredError

# 程序所在目录：打包后是 exe 目录，开发态是脚本目录
if getattr(sys, "frozen", False):
    APP_DIR = pathlib.Path(sys.executable).resolve().parent
else:
    APP_DIR = pathlib.Path(__file__).resolve().parent

# 所有用户数据集中在程序平级的数据目录里（便携化）
DATA_DIR_NAME = "TG视频下载器数据"
DATA_DIR = APP_DIR / DATA_DIR_NAME
CONFIG_PATH = DATA_DIR / "tg_config.json"
SESSION_PATH = str(DATA_DIR / "tg_session")
DEFAULT_DOWNLOAD_ROOT = DATA_DIR / "downloads"
TASK_ROOT = DATA_DIR / "tasks"          # 任务清单（断点续传）

PART = 8 * 1024 * 1024     # 分片大小
CONCURRENT = 4             # 并行连接数
RETRIES = 3                # 单视频重试次数
STALL_SECS = 90            # 停滞判定秒数
# ---- T24 断网感知与停滞分片重置（FR-11 / AC-9）----
SHARD_RESET_MAX = 3        # 单分片停滞重置上限（首取 +3 次重置 = 至多 4 个迭代器）
NET_POLL_SECS = 2.0        # 断网轮询间隔（生产 2s；测试 monkeypatch 即退）
SIMPLE_TIMEOUT_SECS = 1800 # simple_download 总超时（断网期间暂停计时）
SPEED_WIN_SEC = 10.0       # 总速度滑动窗口：统计最近 10 秒下载量，每秒更新
# T50 目录搬运跨卷分块大小。依据：单搬运线程内存上限 ≤4 MiB；4 MiB 在
# 10 MB/s 慢速 U 盘上单块约 0.4s，满足作业帧 ≤500ms 节拍预算；NVMe/SSD
# 上单块数毫秒~数十毫秒，由 JobRegistry 0.30s 节流聚合；相对下载分片
# PART=8 MiB 取半，节拍更细。
MOVE_CHUNK = 4 * 1024 * 1024

# T51 多账号（小号）池：账号 id 白名单与标记（v2.1 仅两个，硬校验）。
ACCT_MAIN = "main"
ACCT_ALT = "alt1"
ACCT_IDS = ("main", "alt1")
ACCT_MARK = {"main": "[主]", "alt1": "[小]"}

# 小号 session 同组附属文件后缀钉死 4 个（Telethon SQLite 可能产物，
# 存在才删）；清理按完整 basename 逐后缀精确删除，严禁 tg_session*
# 之类可能误伤主号的通配。
ALT_SESSION_SUFFIXES = (".session", ".session-journal", ".session-wal",
                        ".session-shm")

# 慢速判定阈值（acct_policy 键缺省时的默认；高级用户可在 tg_config.json
# 手改 acct_policy 覆盖，应用永远只读、永不主动写入）。依据：
#   ACCT_SLOW_BPS=200KB/s：TG 服务端账号级限速典型体感档为几十~一百多
#     KB/s；200KB/s 下 1GB 视频约 87 分钟，属明显被限速、值得分流；正常
#     媒体下载常见 ≥1MB/s 不会误伤；与 STALL_SECS=90（停滞 90s 零字节）
#     互补——后者抓死链，本阈值抓持续低速。
#   ACCT_SLOW_WIN_SEC=20：2 倍于总速度窗 SPEED_WIN_SEC，跨新条目启动
#     爬坡，避免误判。
#   ACCT_COOLDOWN_SECS=60 初判冷却；重复判慢翻倍、封顶 ACCT_COOLDOWN_MAX。
ACCT_SLOW_BPS = 200 * 1024        # 204800 B/s ≈ 200 KB/s
ACCT_SLOW_WIN_SEC = 20.0
ACCT_COOLDOWN_SECS = 60.0
ACCT_COOLDOWN_MAX = 300.0

# acct_hint 事件去抖：同 task+reason 最小间隔（channel_private 不受限）。
ACCT_HINT_MIN_INTERVAL = 30.0

# ---- T59 账号健康巡检阈值（FR-41 收口 / tasks.md Task 59）----
# warm_alt 由一次性 20s fire-and-forget 预热升级为有界后台巡检
# （health loop）。以下阈值全部可被 cfg.acct_policy 可选键覆盖
# （load_acct_policy——与 T51 慢速阈值同源，无第二配置源）：
#   warm_poll_sec=60：巡检轮询间隔（10..600）；
#   warm_hard_fail_max=3：连续确定性硬错上限→标 unhealthy（1..10）；
#   warm_unhealthy_backoff_sec=600：unhealthy 退避秒（30..3600）；
#   warm_transient_backoff_sec=30：瞬时错指数退避基值（1..300）。
WARM_POLL_SEC = 60.0
WARM_HARD_FAIL_MAX = 3
WARM_UNHEALTHY_BACKOFF = 600.0
# 瞬时退避序列钉死 30/60/120/300/300…（任务书 TR-59.1①）：前 3 次
# 指数 2 倍，第 4 次起一律封顶本值。
WARM_TRANSIENT_BACKOFF = 30.0
WARM_TRANSIENT_BACKOFF_MAX = 300.0

# ---- T25 慢任务让路（FR-12 / tasks.md Task 25，默认关）----
# 判定阈值四件（独立于 ACCT_* 账号级常量——那是"账号慢→改派分流"，
# 本组是"条目慢→暂停让路"；缺省值与 ACCT_* 同源取值但不共享变量）：
#   SLOW_YIELD_BPS=200KB/s：条目档典型限速体感（同 ACCT_SLOW_BPS 档位）；
#   SLOW_YIELD_WIN_SEC=20：持续低速满 20s 才触发（跨短抖动，同
#     ACCT_SLOW_WIN_SEC 两倍于速度采样窗以避开启动爬坡）；
#   SLOW_YIELD_COOLDOWN_SEC=60：让路后最短暂停时长（到期才谈恢复）；
#   SLOW_YIELD_MAX=3：单条目单会话让路上限（防抖/防饿死）。模块常量、
#     不提供任务级键——task["params"] 四可选键被红线钉死（§2.1），
#     上限唯一来源即本常量（测试经 monkeypatch 独立验证生效）。
SLOW_YIELD_BPS = 200 * 1024        # 204800 B/s ≈ 200 KB/s
SLOW_YIELD_WIN_SEC = 20.0
SLOW_YIELD_COOLDOWN_SEC = 60.0
SLOW_YIELD_MAX = 3

# 无权限频道错误类名钉死集合（用 type(e).__name__ 与真 telethon 类比较）。
PRIVATE_ERROR_NAMES = {"ChannelPrivateError"}

# ---- T21 持久化防抖与 .bak 轮转（FR-8 / AC-8）----
PERSIST_DEBOUNCE_SECS = 1.0      # 合并窗口（FR-8 要求 1–2s，取 1.0）
PERSIST_MAX_WAIT_SECS = 2.0      # 自首次脏起的保底落盘上限
PERSIST_BAK_COUNT = 3            # .bak 轮转保留份数
# 终态状态词集合（判定 force 落盘；沿用既有状态词，不新造）
PERSIST_FORCE_STATUSES = ("done", "exists", "failed", "skipped", "cancelled")

# ---- T22 错误分类重试与 file_reference 刷新（FR-9 / AC-6）----
# 瞬时退避序列钉死 3s/6s/12s，超过 3 次间隔后一律封顶 12s（§2 契约）。
RETRY_BACKOFF = (3.0, 6.0, 12.0)
RETRY_BACKOFF_CAP = 12.0       # 越界取此封顶值
FILE_REFRESH_MAX = 3           # file_reference 单条刷新上限（第 4 次过期=确定性失败）
RETRIES_MIN = 0                # 任务级/全局 retries 合法区间（0..8；
                               # 0=不重试即仅 1 次尝试，由 download_one 兑现）
RETRIES_MAX = 8
RETRY_FLOOD_SPARE = 32         # flood 不计次的有界安全阀（§2“任何路径
                               # 不得无限重试”）；一次性会话远达不到，
                               # 仅防极端 flood 背靠背导致循环无界续转。
# 确定性错误名集合（按 type(e).__name__ 判定，不硬 import；与
# PRIVATE_ERROR_NAMES 同风格）。三件均已在当前 telethon 1.45.0
# 验证存在（t22_result.md 形状备忘逐项证明）：
#   ChannelPrivateError——频道私有/无权限（该账号永不可访问此频道）
#   MediaEmptyError——引用消息无文档/无媒体
#   MessageIdInvalidError——消息 ID 无效/已删除
# 候选剔除记录（不存在或非永久无效，终版不入集）：DiscussionGroup
# UnavailableError / EntitySizeKnownError（当前 telethon 无此类）；
# ChatWriteForbiddenError / SlowModeWaitError（存在但下载场景非永久
# 无效/属等待语义）；MsgIdInvalidError（存在但为旧别名，避免双名歧义）。
DETERMINISTIC_ERROR_NAMES = frozenset({
    "ChannelPrivateError", "MediaEmptyError", "MessageIdInvalidError",
})
# 控制类名集合（分类器归档 control；download_one 侧维持现状
# 穿透（AccountDenied/CancelledError 走 BaseException）或经专属
# except 原样返回 paused/removed/skipped——分类器不得截留）。
CONTROL_ERROR_NAMES = frozenset({
    "PauseFile", "RemoveFile", "SkipFile",
    "AccountDenied", "CancelledError",
})


# ---------------- 配置与登录 ----------------

def load_config():
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_config(cfg):
    # T56 Bug5：原子写（同目录 tmp + os.replace）——崩溃至多留 .tmp
    # 残文件（由 ensure_dirs_and_migrate 启动清理），cfg 永远是旧值或
    # 新值二选一，杜绝写一半的劈叉文件。同目录 rename 不跨卷。
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_name(CONFIG_PATH.name + ".tmp")
    tmp.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    os.replace(tmp, CONFIG_PATH)


def get_download_root(cfg):
    """下载根目录：配置中可自定义，默认数据目录下 downloads"""
    raw = (cfg or {}).get("download_root")
    root = pathlib.Path(raw) if raw else DEFAULT_DOWNLOAD_ROOT
    root.mkdir(parents=True, exist_ok=True)
    return root


def setup_console_utf8():
    """Windows 控制台切到 UTF-8 代码页，避免中文乱码（真实窗口下生效）"""
    if os.name != "nt":
        return
    try:
        k = ctypes.windll.kernel32
        k.SetConsoleOutputCP(65001)
        k.SetConsoleCP(65001)
    except Exception:
        pass


def enable_vt():
    """开启 Windows 控制台 ANSI 转义（光标定位/清屏），成功返回 True"""
    if os.name != "nt":
        return True
    try:
        k = ctypes.windll.kernel32
        h = k.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_ulong()
        if not k.GetConsoleMode(h, ctypes.byref(mode)):
            return False
        ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
        if not k.SetConsoleMode(h, mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING):
            return False
        return True
    except Exception:
        return False


def ensure_dirs_and_migrate():
    """生成数据目录及子目录；旧版本（数据散在程序目录）配置自动迁入。"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    DEFAULT_DOWNLOAD_ROOT.mkdir(parents=True, exist_ok=True)
    TASK_ROOT.mkdir(parents=True, exist_ok=True)

    # 旧版布局：tg_config.json / tg_session.session 在程序目录
    if not CONFIG_PATH.exists():
        old_cfg = APP_DIR / "tg_config.json"
        if old_cfg.exists():
            try:
                CONFIG_PATH.write_bytes(old_cfg.read_bytes())
                old_sess = APP_DIR / "tg_session.session"
                new_sess = DATA_DIR / "tg_session.session"
                if old_sess.exists() and not new_sess.exists():
                    new_sess.write_bytes(old_sess.read_bytes())
                print(f"（已把原有配置和登录信息迁移到 {DATA_DIR_NAME}\\））")
            except Exception:
                pass

    # T56 Bug5：原子写崩溃残留的 .tmp 清理（tmp 只可能来自 save_config
    # 中途崩溃，此时 cfg 本体仍是完整旧值，残文件无保留意义）
    tmp_cfg = CONFIG_PATH.with_name(CONFIG_PATH.name + ".tmp")
    if tmp_cfg.exists():
        try:
            tmp_cfg.unlink()
            print("[acct] 已清理上次崩溃残留的配置临时文件")
        except OSError:
            pass

    # T56 Bug5：提升流程中断对账（零网络；正常态零动作）
    reconcile_session_promotion()

    # T59：异常手机号启动对账（只标记不删除；零网络；正常态零写盘）
    reconcile_phones(load_config())

    # T21：tasks 目录原子写残留 .*.json.tmp 清理（只清 tmp，绝不碰 bak）
    try:
        for tmp_task in TASK_ROOT.glob("*.json.tmp"):
            try:
                tmp_task.unlink()
            except OSError:
                pass
    except OSError:
        pass


# 内置凭证：Telegram 官方桌面客户端公开使用的 api_id/api_hash，
# 普通用户无需自己去 my.telegram.org 申请，开箱即用。
BUILTIN_API_ID = 2040
BUILTIN_API_HASH = "b18441a1ff607e10a989891a5462e627"


def setup_credentials():
    cfg = load_config()
    if not cfg.get("api_id") or not cfg.get("api_hash"):
        # 默认使用内置凭证（用户也可以在设置中换成自己申请的）
        cfg["api_id"] = BUILTIN_API_ID
        cfg["api_hash"] = BUILTIN_API_HASH
        cfg["builtin_api"] = True
        print("（使用内置登录凭证，无需自行申请 API）")
    if not cfg.get("phone"):
        cfg["phone"] = input(
            "手机号（含国家区号，不要加 +，例如 8613800000000）："
        ).strip()
    save_config(cfg)
    return cfg


def ensure_builtin_credentials():
    """GUI 专用（T56 Bug1）：非交互补凭证——只保证 api_id/api_hash
    存在（缺则补内置值并落盘），绝不 input、绝不碰 phone。
    覆盖 send_code/account_add_start 等未先 connect 的构造路径
    （GUI 无控制台，setup_credentials 的 phone input 有冻结风险）。"""
    cfg = load_config()
    if not cfg.get("api_id") or not cfg.get("api_hash"):
        cfg["api_id"] = BUILTIN_API_ID
        cfg["api_hash"] = BUILTIN_API_HASH
        cfg["builtin_api"] = True
        save_config(cfg)
    return cfg


# ---------------- T51 账号池配置辅助（accounts/acct_policy 读写边界） ----------------

def session_path_for(account_id):
    """账号 session 路径：main 沿用 SESSION_PATH（零迁移零触碰）；
    alt1 为 DATA_DIR/tg_session_alt1；其它 id 一律 ValueError。"""
    if account_id == ACCT_MAIN:
        return SESSION_PATH
    if account_id == ACCT_ALT:
        return str(DATA_DIR / "tg_session_alt1")
    raise ValueError(f"未知账号 id：{account_id!r}")


def alt_session_files():
    """小号 session 同组附属文件完整路径列表（4 后缀逐个精确枚举）。"""
    base = session_path_for(ACCT_ALT)
    return tuple(base + suf for suf in ALT_SESSION_SUFFIXES)


def load_acct_policy():
    """读 cfg.acct_policy（只读、永不写）：缺省/非法值回退默认并 print
    技术日志；数值夹紧到合法区间，任何情况不抛异常。"""
    try:
        raw = load_config().get("acct_policy")
    except Exception:
        raw = None
    if not isinstance(raw, dict):
        raw = {}

    def _num(key, dflt, lo, hi):
        try:
            v = float(raw.get(key, dflt))
        except (TypeError, ValueError):
            print(f"[acct] acct_policy.{key} 非法，回退默认 {dflt}")
            return dflt
        if v < lo or v > hi:
            print(f"[acct] acct_policy.{key}={v} 越界 [{lo},{hi}]，夹紧")
            v = max(lo, min(v, hi))
        return v

    return {
        "slow_bps": int(_num("slow_bps", ACCT_SLOW_BPS,
                             1024, 100 * 1024 * 1024)),
        "slow_win_sec": _num("slow_win_sec", ACCT_SLOW_WIN_SEC, 5, 300),
        "cooldown_sec": _num("cooldown_sec", ACCT_COOLDOWN_SECS, 10, 1800),
        # T59 巡检阈值（§2.4：可选键；缺省=内置默认，非法值回落默认，
        # 与 T51 阈值同源——本函数是唯一配置覆盖入口）
        "warm_poll_sec": _num("warm_poll_sec", WARM_POLL_SEC, 10, 600),
        "warm_hard_fail_max": int(_num("warm_hard_fail_max",
                                       WARM_HARD_FAIL_MAX, 1, 10)),
        "warm_unhealthy_backoff_sec": _num(
            "warm_unhealthy_backoff_sec", WARM_UNHEALTHY_BACKOFF, 30, 3600),
        "warm_transient_backoff_sec": _num(
            "warm_transient_backoff_sec", WARM_TRANSIENT_BACKOFF, 1, 300),
    }


def alt_configured():
    """cfg 已配置 alt1（accounts 含 alt1）？只读，任何路径不补写该键。"""
    try:
        accts = load_config().get("accounts")
    except Exception:
        return False
    if not isinstance(accts, list):
        return False
    return any(isinstance(a, dict) and a.get("id") == ACCT_ALT
               for a in accts)


def save_accounts_add_alt(label=""):
    """accounts 键的首次且唯一常规落盘时机：alt1 登录流程最终成功
    （sign_in 完成且已授权）的那一刻。与 cfg 合并一次 save_config；
    旧文件无该键视单账号，绝不因启动/主号登录/下载补写。"""
    cfg = load_config()
    accts = cfg.get("accounts")
    if not isinstance(accts, list):
        accts = [{"id": ACCT_MAIN}]
    if not any(isinstance(a, dict) and a.get("id") == ACCT_MAIN
               for a in accts):
        accts.insert(0, {"id": ACCT_MAIN})
    if any(isinstance(a, dict) and a.get("id") == ACCT_ALT for a in accts):
        return                       # 已在表（幂等）：不重写
    accts.append({"id": ACCT_ALT, "label": label or ""})
    cfg["accounts"] = accts
    save_config(cfg)


def save_accounts_remove_alt():
    """移除小号成功后：删除 accounts 键（恢复无该键状态），
    pop 后一次 save_config；无键时零写盘。"""
    cfg = load_config()
    if "accounts" not in cfg:
        return
    cfg.pop("accounts", None)
    save_config(cfg)


# ---------------- T56 Bug5：小号提升为主号（纯 FS，可离线单测） ----------------

def promote_alt_session_files():
    """把 alt1 的 4 个 session 文件提升（改名）为 main 同名文件。
    前提：主号已完成登出/断开（目标 main 文件是死文件，存在先删）。
    - 逐后缀 os.replace（同目录同卷原子改名）；缺失跳过；
    - .session 本体放最后改名（中途崩溃时启动对账把"alt .session 在
      且 main .session 缺"识别为未完成态续办，授权文件不双存）；
    - 单个 OSError 吞掉继续（不中断编排；调用方以返回值判定成功）。
    返回 {"moved": [...], "removed": [...]}（main 侧最终路径）。"""
    moved, removed = [], []
    sufs = [s for s in ALT_SESSION_SUFFIXES if s != ".session"] + [".session"]
    for suf in sufs:
        alt_f = pathlib.Path(session_path_for(ACCT_ALT) + suf)
        if not alt_f.exists():
            continue
        main_f = pathlib.Path(SESSION_PATH + suf)
        if main_f.exists():
            try:
                os.remove(main_f)
                removed.append(str(main_f))
            except OSError:
                pass
        try:
            os.replace(alt_f, main_f)
            moved.append(str(main_f))
        except OSError:
            pass
    return {"moved": moved, "removed": removed}


def reconcile_session_promotion():
    """启动对账（ensure_dirs_and_migrate 末尾调用；零网络）：
    提升（logout 主号位）流程在改名单与 cfg 写入之间崩溃时，
    依据"正常态不可达"的 accounts×session 组合收敛。正常态零动作。

    组合与判定（前提：cfg accounts 含 alt1——正常只在 alt 登录成功后
    写入、只在 alt 文件删除后移除，故"有 accounts 无 alt 文件"的中间
    态只能来自提升流程中断）：
      A. alt .session 在 且 main .session 缺 → 崩在改名中途：
         续完改名（alt 授权成为新主号）+ pop accounts + 清 phone；
      B. alt .session 缺 且 main .session 在 → 崩在改名后 cfg 前：
         main 已是提升后有效授权，删 alt 残留附属文件 + pop accounts
         + 清 phone；
      C. 其余组合（含双双在=正常双号在线）零动作。
    对账只做安全收敛，绝不删除 main 侧有效授权。"""
    try:
        cfg = load_config()
    except Exception:
        return
    accts = cfg.get("accounts")
    if not (isinstance(accts, list)
            and any(isinstance(a, dict) and a.get("id") == ACCT_ALT
                    for a in accts)):
        return
    alt_sess = pathlib.Path(session_path_for(ACCT_ALT) + ".session")
    main_sess = pathlib.Path(SESSION_PATH + ".session")
    if alt_sess.exists() and not main_sess.exists():
        r = promote_alt_session_files()
        if str(main_sess) in r["moved"]:
            print("[acct] reconcile: 检测到未完成的小号提升（崩在改名"
                  f"中途），已续完（moved={len(r['moved'])}）")
            cfg = load_config()
            cfg.pop("accounts", None)
            cfg.pop("phone", None)
            save_config(cfg)
            print("[acct] reconcile: 小号已提升为主号（accounts/phone "
                  "已收敛）")
        else:
            # .session 改名未成（被占用等）：保留 accounts 原样，
            # 下次启动由此组合继续重试（幂等、零丢失）
            print("[acct] reconcile: 小号提升改名未完成（会话文件被"
                  "占用），下次启动重试")
    elif not alt_sess.exists() and main_sess.exists():
        for f in alt_session_files():
            try:
                os.remove(f)
            except OSError:
                pass
        cfg = load_config()
        cfg.pop("accounts", None)
        cfg.pop("phone", None)
        save_config(cfg)
        print("[acct] reconcile: 检测到提升后残留的小号配置/附属文件，"
              "已收敛（新主号授权完好）")


def validate_phone(p):
    """T59 §2.2 异常手机号校验（纯函数；规则写死如下，禁自由发挥）：
      1) 空值（None/空串/纯空白）→ "empty"；
      2) 允许恰好一个前导 '+'，其余必须全为 ASCII 数字 → 否则
         "non-digit"；
      3) 数字总位数 7..15（E.164）→ 否则 "length"；
      4) 总位数 >7 时国家码段首位不得为 0（"国家码 1-3 位非零开头"
         规则的可判定投影：仅 >7 位时校验首位）→ 否则 "country-code"。
    合法返回 ""。钉死用例：q→non-digit；""→empty；123→length；
    "+"+90 位数字→length；+0123456789→country-code；+8613800138000
    →合法。"""
    s = str(p or "").strip()
    if not s:
        return "empty"
    if s.startswith("+"):
        s = s[1:]
    if not s or not (s.isascii() and s.isdigit()):
        return "non-digit"
    if not (7 <= len(s) <= 15):
        return "length"
    if len(s) > 7 and s[0] == "0":
        return "country-code"
    return ""


def _mask_phone(p):
    """T59 §2.2 手机号脱敏（日志强制）：显示为 ****+末 2 位；长度 <2
    （含空值）一律 ****。任何 [acct]/[wacct]/巡检/校验日志输出手机号
    必须经本助手，禁明文。"""
    s = str(p or "")
    return "****" + s[-2:] if len(s) >= 2 else "****"


def reconcile_phones(cfg):
    """T59 §2.2 启动对账：异常手机号清洗——只标记不删除（零网络、
    幂等）。由 ensure_dirs_and_migrate 在 reconcile_session_promotion
    之后调用（入参为现读 cfg）。

    校验范围与落点（规则钉死）：
      - cfg["phone"]（主号顶层键；键缺席=未登录/已登出，正常态不算脏）
        与 cfg["accounts"][*]["phone"]（历史遗留可选键；键缺席=小号
        phone 本就不落盘的正常态，不算脏）；
      - 脏值 → 对应 accounts 条目加可选键 "phone_invalid": true；
        合法且该条目曾被标 → 清除标记自愈（不删除任何条目/文件/
        既有键；合法号零触碰）；
      - 主号标记落在 id=="main" 条目：accounts 键缺席但主号 phone 脏
        时补建仅含 main 的最小条目承载标记（T58 提升流程实测形态：
        脏 phone 写入顶层的同时 accounts 被 pop——不补建则该脏值
        永远不可见、不可清洗）；
      - 主号脏 phone 只影响可见性/登录预填，不进调度排除（主号是
        唯一回退号，_eligible 的 invalid 守卫仅对小号生效）。
    幂等：重复跑 changed=0、零写盘（mtime 不变）。返回 changed 计数。
    [wacct] invalid 行：id/reason/脱敏 phone，一行一条（仅首标时）。"""
    if not isinstance(cfg, dict):
        return 0
    accts = cfg.get("accounts")
    if not isinstance(accts, list):
        accts = None
    main_phone = cfg.get("phone")
    # 主号需要标记但无承载条目：补建仅含 main 的最小条目（红线 5：
    # cfg 仅 accounts 条目允许加可选标记键；不写任何 alt 配置）
    if (main_phone is not None and validate_phone(main_phone)
            and not any(isinstance(a, dict) and a.get("id") == ACCT_MAIN
                        for a in (accts or []))):
        if accts is None:
            accts = []
            cfg["accounts"] = accts
        accts.insert(0, {"id": ACCT_MAIN})
    if accts is None:
        return 0
    changed = 0
    for a in accts:
        if not isinstance(a, dict):
            continue
        aid = a.get("id")
        cand = []                       # (脏值, 原因)：main 含顶层键
        if aid == ACCT_MAIN and main_phone is not None:
            r = validate_phone(main_phone)
            if r:
                cand.append((main_phone, r))
        if "phone" in a:
            r = validate_phone(a.get("phone"))
            if r:
                cand.append((a.get("phone"), r))
        if cand:
            phone, reason = cand[0]
            if not a.get("phone_invalid"):
                a["phone_invalid"] = True
                changed += 1
                _wacct_log(f"id={aid} event=invalid reason={reason} "
                           f"phone={_mask_phone(phone)}")
        elif a.get("phone_invalid"):
            a.pop("phone_invalid", None)   # 自愈：清除过期标记
            changed += 1
    if changed:
        save_config(cfg)
    return changed


def prompt_manual_api(cfg):
    """内置凭证不可用时，引导用户填写自己的凭证"""
    print("\n内置凭证登录失败（可能被临时限制）。")
    print("可免费自行申请：浏览器打开 https://my.telegram.org/apps 登录后创建应用")
    print("（应用短名须用 5-32 位英文或数字，不能用中文）\n")
    cfg["api_id"] = int(input("请输入 api_id：").strip())
    cfg["api_hash"] = input("请输入 api_hash：").strip()
    cfg.pop("builtin_api", None)
    save_config(cfg)


# ---------------- 工具函数 ----------------

def parse_channel_ref(text):
    """解析频道输入，返回 (频道标识, 消息id|None)。
    支持 t.me/<频道>/<id> 链接、裸写的 用户名/id、@用户名。
    私有频道 t.me/c/<...> 链接不在此处理（需邀请权限）。"""
    text = (text or "").strip()
    m = re.match(r"(?:https?://)?t\.me/([A-Za-z0-9_]{4,})(?:/(\d+))?", text)
    if m:
        return m.group(1), (int(m.group(2)) if m.group(2) else None)
    # 裸写 用户名 或 用户名/消息id
    m = re.match(r"@?([A-Za-z0-9_]{4,})(?:/(\d+))?$", text)
    if m:
        return m.group(1), (int(m.group(2)) if m.group(2) else None)
    return text.lstrip("@"), None


def parse_channel(text):
    return parse_channel_ref(text)[0]


def safe_name(name, fallback):
    name = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", name or "").strip().strip(".")
    return name or fallback


def is_video_message(msg):
    doc = msg.document
    if not doc:
        return False
    if doc.mime_type and doc.mime_type.lower().startswith("video/"):
        return True
    return any(isinstance(a, DocumentAttributeVideo) for a in doc.attributes)


def pick_filename(doc, msg_id):
    for a in doc.attributes:
        if isinstance(a, DocumentAttributeFilename) and a.file_name:
            return safe_name(f"{msg_id}_{a.file_name}", f"{msg_id}.mp4")
    mt = (doc.mime_type or "").lower()
    ext = {"video/quicktime": "mov", "video/x-matroska": "mkv",
           "video/webm": "webm"}.get(mt, "mp4")
    return f"{msg_id}.{ext}"


class SkipFile(Exception):
    """用户请求跳过当前视频（清理临时文件）"""


class PauseFile(Exception):
    """用户请求暂停当前视频（保留 .dl/分片标记，释放槽位）"""


class RemoveFile(Exception):
    """用户请求从任务移除当前视频（取消下载，条目从清单删除）"""


class ItemCtl:
    """单个视频的运行期控制信号；下载循环在每个断点检查。"""

    def __init__(self, key):
        self.key = key
        self.skip = asyncio.Event()
        self.pause = asyncio.Event()
        self.cancel = asyncio.Event()

    def tripped(self):
        """返回应抛出的异常类型，无则 None（优先级 取消>跳过>暂停）。"""
        if self.cancel.is_set():
            return RemoveFile
        if self.skip.is_set():
            return SkipFile
        if self.pause.is_set():
            return PauseFile
        return None


class KeyWatch:
    """后台线程监听键盘：P=暂停当前，R=继续一个暂停，S=跳过当前，
    X=移除当前，Q=中止全部，I=切换简洁/详细。
    Windows 用 msvcrt，macOS/Linux 用 termios 原始模式，无需回车。"""

    def __init__(self):
        self.skip = threading.Event()
        self.abort = threading.Event()
        self.toggle = threading.Event()
        self.pause = threading.Event()
        self.resume = threading.Event()
        self.remove = threading.Event()
        self._quit = False

    def start(self):
        target = self._listen_win if os.name == "nt" else self._listen_posix
        threading.Thread(target=target, daemon=True).start()

    def stop(self):
        self._quit = True

    def clear(self):
        self.skip.clear()

    def _dispatch(self, ch):
        ch = ch.lower()
        if ch == "s":
            self.skip.set()
        elif ch == "q":
            self.abort.set()
        elif ch == "i":
            self.toggle.set()
        elif ch == "p":
            self.pause.set()
        elif ch == "r":
            self.resume.set()
        elif ch == "x":
            self.remove.set()

    def _listen_win(self):
        import msvcrt
        while not self._quit:
            try:
                if msvcrt.kbhit():
                    ch = msvcrt.getch().decode("latin-1", errors="ignore")
                    self._dispatch(ch)
            except Exception:
                pass
            time.sleep(0.1)

    def _listen_posix(self):
        import termios, tty
        try:
            fd = sys.stdin.fileno()
            old = termios.tcgetattr(fd)
        except (AttributeError, termios.error):
            return   # 无真实终端时按键功能不可用，不影响下载
        try:
            tty.setcbreak(fd)
            while not self._quit:
                ch = sys.stdin.read(1)
                if ch:
                    self._dispatch(ch)
                else:
                    time.sleep(0.1)
        except Exception:
            pass
        finally:
            try:
                termios.tcsetattr(fd, termios.TCSADRAIN, old)
            except termios.error:
                pass


# ---------------- 扫描 ----------------

def parse_keywords(raw):
    """关键词串 → (包含词, 排除词)。逗号分隔；词首 - 表示排除。

    例：'4K,预告片,-广告' → (['4k','预告片'], ['广告'])
    """
    includes, excludes = [], []
    for tok in re.split(r"[,，]", (raw or "").strip()):
        tok = tok.strip()
        if not tok:
            continue
        if tok.startswith("-"):
            w = tok[1:].strip()
            if w:
                excludes.append(w.lower())
        else:
            includes.append(tok.lower())
    return includes, excludes


def kw_match(text, includes, excludes):
    """大小写不敏感子串匹配：包含词命中任一即可，排除词命中任一即否决"""
    t = (text or "").lower()
    if includes and not any(w in t for w in includes):
        return False
    if excludes and any(w in t for w in excludes):
        return False
    return True


MAX_LOCAL_SCAN = 5000     # 本地兜底扫描条数上限
HARD_SCAN_CAP = 100000    # 指定起始日期时的绝对安全上限，防止超大频道失控

ENRICH_BATCH_SIZE = 100     # 引用/相册文本补抓：每批 get_messages 的 id 数，与 enrich_task_items/pull_records 既有 100/批约定一致
ENRICH_MAX_BATCHES = 60     # 单次 search_videos 全局补抓批次预算（channel+discuss 合计）；耗尽则 meta["truncated"]=True
ALBUM_ID_WINDOW = 10        # 相册组补抓的候选 id 近邻窗口半径（频道相册各条 id 连续，±10 覆盖 ≤21 条）
REF_TEXT_MAX_CHARS = 120    # record["ref_text"] 最大字符数（空白压缩后，超长追加省略号）

# ---- T48 流式搜索（search_videos_stream）----
STREAM_FLUSH_ITEMS = 40     # 脊线扫描中每攒满 40 条可下发命中即出一批 items 增量帧
STREAM_FLUSH_SECS = 0.8     # 2.3：出帧时间维——命中零散时也持续出帧（实测
                            # 44 条命中攒到第 40 条才首出帧，前端列表整程空转；
                            # 帧协议/每帧 ≤40 条/T48 上界断言均不变）
# progress.phase → 中文展示映射（TUI 与前端同源同文，逐字钉死）
SEARCH_PHASE_TEXT = {
    "server_search": "服务端搜索",
    "local_scan": "本地翻页扫描",
    "enrich": "引用内容补全",
    "filter": "结果整理",
}

SRC_CHANNEL = "频道"
SRC_COMMENT = "评论"

SEARCH_MODES = {
    "phrase": "精确短语",
    "all": "包含全部词",
    "any": "包含任一词",
    "fuzzy": "模糊匹配",
    "regex": "正则表达式",
}


def fuzzy_term_hit(t, term):
    """模糊判断 term 是否近似出现在文本 t（均已小写）。
    短词直接子串；长词用滑窗相似度，避免全量两两比较。"""
    if not term:
        return True
    if term in t:
        return True
    if len(term) <= 2 or len(t) < 2:
        return False
    sm = difflib.SequenceMatcher(None, term, autojunk=False)
    if len(term) >= len(t):
        sm.set_seq2(t)
        return sm.ratio() >= 0.72
    L = len(term)
    step = max(1, L // 3)
    best = 0.0
    for i in range(0, len(t) - L + 1, step):
        sm.set_seq2(t[i:i + L])
        r = sm.ratio()
        if r >= 0.72:
            return True
        if r > best:
            best = r
    return best >= 0.72


def advanced_match(text, terms, mode, excludes, raw=""):
    """高级文本匹配。terms 为包含词（小写），excludes 为排除词。"""
    t = (text or "").lower()
    if excludes and any(w in t for w in excludes):
        return False
    if not terms:
        return True
    if mode == "phrase":
        return (raw or "").strip().lower() in t
    if mode == "all":
        return all(w in t for w in terms)
    if mode == "any":
        return any(w in t for w in terms)
    if mode == "regex":
        try:
            return re.search(raw, text or "", re.IGNORECASE) is not None
        except re.error:
            return False
    if mode == "fuzzy":
        return any(fuzzy_term_hit(t, w) for w in terms)
    return any(w in t for w in terms)


def _parse_date(s):
    s = (s or "").strip()
    if not s:
        return None
    try:
        import datetime as _dt
        return _dt.datetime.combine(
            _dt.date.fromisoformat(s), _dt.time(), tzinfo=_dt.timezone.utc
        )
    except Exception:
        return None


def _date_in_range(dt, dfrom, dto):
    if dt is None:
        return True   # 无日期的不因其被滤掉
    if dfrom and dt < dfrom:
        return False
    if dto and dt > dto:
        return False
    return True


async def get_discuss_entity(client, entity):
    """取频道关联的讨论组（评论区），无则 None"""
    try:
        from telethon.tl.functions.channels import GetFullChannelRequest
        full = await client(GetFullChannelRequest(channel=entity))
        lid = getattr(full.full_chat, "linked_chat_id", None)
        if not lid:
            return None
        return await client.get_entity(lid)
    except Exception:
        return None


def make_record(msg, src=SRC_CHANNEL):
    """消息 → 统一视频记录。match_in 默认 self：引用/相册/转发来源
    命中的记录由 search_videos 补抓流程覆写（gui permalink 直抓等
    旧调用点拿到默认值即可，消费方一律 .get）。"""
    doc = msg.document
    return {
        "msg": msg, "doc": doc, "size": doc.size or 0,
        "name": pick_filename(doc, msg.id),
        "text": msg.message or "", "matched": True, "src": src,
        "match_in": "self",
    }


def _peer_key(peer):
    """reply_to_peer_id 等 peer 对象 → ('PeerChannel', id) 形式的可比较键。

    Telethon 1.45 里频道/超级群组是 PeerChannel、小群组是 PeerChat；
    一律 getattr 防护（story 回复头等取不到就返回 None），不假设类型。
    """
    if peer is None:
        return None
    name = type(peer).__name__
    for attr in ("channel_id", "chat_id", "user_id"):
        v = getattr(peer, attr, None)
        if isinstance(v, int):
            return (name, v)
    return None


def _clip_ref_text(text):
    """外部命中文本 → 展示片段：空白压成单空格，超 REF_TEXT_MAX_CHARS
    字符截断后追加省略号（省略号在长度上限之外）。"""
    s = re.sub(r"\s+", " ", text or "").strip()
    if len(s) > REF_TEXT_MAX_CHARS:
        return s[:REF_TEXT_MAX_CHARS] + "…"
    return s


async def _pull_ids_retry(client, ent, chunk):
    """T47：批量补抓一批消息 id（重试/退避照抄 pull_records.pull_batch）。

    FloodWaitError 按秒等待；其它异常 2/4/6s 退避；4 次仍失败视为该批
    未取到（返回 None 继续后续批次），不把异常抛出 search_videos。
    """
    for attempt in range(4):
        try:
            return await client.get_messages(ent, ids=chunk)
        except FloodWaitError as e:
            await asyncio.sleep(e.seconds + 1)
        except Exception:
            if attempt == 3:
                return None
            await asyncio.sleep(2 * (attempt + 1))
    return None


async def _search_ref_matches(client, pending, entity, discuss, pools,
                               terms, mode, excludes, raw, cancel=None,
                               max_batches=ENRICH_MAX_BATCHES):
    """T47：为"自身文本未命中"的候选补全外部文本并二次匹配。

    外部文本三桶（self 桶已在 search_videos 主循环判定）：
      reply  被引用消息文本。entity 选择：{channel, discuss} 的
              peer_id→entity 映射按 reply_to_peer_id 定位（评论区评论
              回复频道帖的情形落到 channel entity）；无法判别时用候选
              自身 scope 的 entity 兜底。reply_to.quote_text 零请求兜底。
      album  同 grouped_id 相册组消息文本。本地池零成本先取；不足时按
              候选 id 近邻窗口（ALBUM_ID_WINDOW）批量补抓，取回后按
              grouped_id 相等严格配对，近邻但不同组的绝不关联。
      fwd    弱权重补充：fwd_from.from_name / fwd_from.post_author /
              m.post_author 非空去重，零额外请求。

    匹配规则：对文本集中每一条文本分别调用一次未改动的
    advanced_match，任一命中即候选命中；排除词在同一条文本内否决；
    命中桶优先级 reply > album > fwd；album 桶取同组消息 id 最小者的
    命中文本。只补抓一跳：取回消息不再递归解析。

    批量与缓存：先把全部待补候选的目标 id 汇总（跨候选去重、剔除
    池内已有、剔除候选自身 id），按 entity 分组、id 升序，每
    ENRICH_BATCH_SIZE 个一批 get_messages；max_batches 批次预算
    （缺省 ENRICH_MAX_BATCHES；search_videos_stream 按扫描窗口联动放大）
    耗尽即停（truncated），取回消息全部入池供同批其它候选复用。

    T48 起改为异步生成器（消费方：search_videos_stream）：
      - 每批补抓入池后 yield {"kind":"batch","truncated":..,"batches":..,
        "fetched":..}，供流式消费方对当前池做增量三桶匹配并下发；
      - 末尾 yield {"kind":"records","records":..,"stats":..}，
        records/stats 与 T47 同步返回版逐值同口径（统计不受生成器化影响）；
      - cancel（任何带同步 is_set() 的对象）：每批发请求前轮询，置位后
        不再发新请求、跳过阶段 3 纯 CPU 匹配并直接收尾（stats 含
        "cancelled":True，仅供流式消费方丢弃结果用）。

    引数：
      pending  [(msg, src)]：已过体积/日期过滤、terms 非空且自身文本
                未命中的候选；
      pools    {entity_id: {mid: msg}}：本地池消息（本函数向其追加
                补抓取回的消息作为缓存）；
      cancel   可选取消令牌；None 表示不可取消（T47 薄包装路径恒 None）。
    """
    ent_of = {SRC_CHANNEL: entity, SRC_COMMENT: discuss or entity}

    def _fallback_ent(src):
        return ent_of.get(src) or entity

    # peer_id → entity 映射：频道与讨论组两个 entity（同 id 双登记
    # PeerChannel/PeerChat，覆盖讨论组被建模成任一形态的夹具）
    entity_map = {}
    for _ent in (entity, discuss):
        _eid = getattr(_ent, "id", None) if _ent is not None else None
        if _eid is None:
            continue
        entity_map[("PeerChannel", _eid)] = _ent
        entity_map[("PeerChat", _eid)] = _ent

    # 相册组索引：ent_id → grouped_id → [msg]（取回的消息同样登记）
    gidx = {}

    def _gidx_add(eid, m):
        g = getattr(m, "grouped_id", None)
        if not isinstance(g, int):
            return
        gidx.setdefault(eid, {}).setdefault(g, []).append(m)

    for _eid, _pool in pools.items():
        for _m in _pool.values():
            _gidx_add(_eid, _m)

    def _reply_target(m, src):
        """返回 (目标 entity, 目标 entity id, 目标消息 id)；无则 (None,)*3"""
        reply_to = getattr(m, "reply_to", None)
        rid = getattr(reply_to, "reply_to_msg_id", None)
        if not rid:
            return None, None, None
        pk = _peer_key(getattr(reply_to, "reply_to_peer_id", None))
        ent_t = entity_map.get(pk) if pk else None
        if ent_t is None:
            ent_t = _fallback_ent(src)
        return ent_t, getattr(ent_t, "id", None), rid

    # ---- 阶段 1：汇总待补抓 id（跨候选去重、剔除池内已有/候选自身）----
    needed = set()   # 需要取文本的外部消息 key（ent_id, mid）——缓存命中口径
    plan = {}        # ent_id → set(需补抓的 mid)
    plan_ent = {}    # ent_id → entity 对象
    for m, src in pending:
        eid = getattr(_fallback_ent(src), "id", None)
        pool = pools.setdefault(eid, {})
        _ent_t, _teid, _rid = _reply_target(m, src)
        if _rid is not None:
            needed.add((_teid, _rid))
            if _rid not in pools.setdefault(_teid, {}):
                plan.setdefault(_teid, set()).add(_rid)
                plan_ent.setdefault(_teid, _ent_t)
        if getattr(m, "grouped_id", None) is not None:
            for _sib in gidx.get(eid, {}).get(m.grouped_id, ()):
                if _sib.id != m.id:
                    needed.add((eid, _sib.id))
            base = max(1, m.id - ALBUM_ID_WINDOW)
            for wid in range(base, m.id + ALBUM_ID_WINDOW + 1):
                if wid == m.id or wid in pool:
                    continue
                plan.setdefault(eid, set()).add(wid)
                plan_ent.setdefault(eid, _fallback_ent(src))

    # ---- 阶段 2：按 entity 分组、id 升序、ENRICH_BATCH_SIZE 一批补抓 ----
    # 补抓顺序固定：先频道后讨论组（entity/discuss 传入序），行为确定。
    order = []
    for _ent in (entity, discuss):
        _eid = getattr(_ent, "id", None) if _ent is not None else None
        if _eid in plan and _eid not in order:
            order.append(_eid)
    order += [e for e in plan if e not in order]

    batches = 0
    fetched = 0
    truncated = False
    cancelled = False
    for eid in order:
        ids = sorted(plan[eid])
        ent_t = plan_ent[eid]
        pool = pools.setdefault(eid, {})
        for i in range(0, len(ids), ENRICH_BATCH_SIZE):
            if batches >= max_batches:
                truncated = True
                break
            # T48 取消检查点：每批发请求前轮询（协作式，不用 task.cancel()）
            if cancel is not None and cancel.is_set():
                cancelled = True
                break
            chunk = ids[i:i + ENRICH_BATCH_SIZE]
            msgs = await _pull_ids_retry(client, ent_t, chunk)
            batches += 1
            fetched += len(chunk)
            for mm in msgs or []:
                if mm is None:
                    continue
                mid = getattr(mm, "id", None)
                if mid is None:
                    continue
                pool[mid] = mm
                _gidx_add(eid, mm)
            # T48：本批已全部入池，交给流式消费方做增量三桶匹配/下发
            yield {"kind": "batch", "truncated": truncated,
                   "batches": batches, "fetched": fetched}
        if truncated or cancelled:
            # 截断/取消发生在"下一批发请求前"（无新批）：补发一帧状态
            # 事件让流式消费方能及时感知 truncated 粘滞位并停发新请求
            yield {"kind": "batch", "truncated": truncated,
                   "batches": batches, "fetched": fetched}
            break

    # ---- 阶段 3：文本集逐条调用 advanced_match（桶优先级 reply>album>fwd）----
    records = []
    if not cancelled:
        for m, src in pending:
            eid = getattr(_fallback_ent(src), "id", None)
            bucket = None
            ref_text = None

            # reply 桶：被引用消息文本在前，quote_text 零请求兜底在后
            _ent_t, _teid, _rid = _reply_target(m, src)
            reply_texts = []
            if _rid is not None:
                tm = pools.get(_teid, {}).get(_rid)
                if tm is not None:
                    reply_texts.append(getattr(tm, "message", "") or "")
            qt = getattr(getattr(m, "reply_to", None), "quote_text", None)
            if qt:
                reply_texts.append(qt)
            for t in reply_texts:
                if advanced_match(t, terms, mode, excludes, raw):
                    bucket, ref_text = "reply", t
                    break

            # album 桶：同组消息 id 升序，取 id 最小者的命中文本
            if bucket is None and getattr(m, "grouped_id", None) is not None:
                sibs = sorted(
                    (x for x in gidx.get(eid, {}).get(m.grouped_id, ())
                     if x.id != m.id),
                    key=lambda x: x.id)
                for x in sibs:
                    t = getattr(x, "message", "") or ""
                    if advanced_match(t, terms, mode, excludes, raw):
                        bucket, ref_text = "album", t
                        break

            # fwd 桶：转发来源/发帖署名（零额外请求，三值非空去重）
            if bucket is None:
                ff = getattr(m, "fwd_from", None)
                seen_fwd = set()
                for t in (getattr(ff, "from_name", None),
                          getattr(ff, "post_author", None),
                          getattr(m, "post_author", None)):
                    if isinstance(t, str) and t.strip() and t not in seen_fwd:
                        seen_fwd.add(t)
                        if advanced_match(t, terms, mode, excludes, raw):
                            bucket, ref_text = "fwd", t
                            break

            if bucket is None:
                continue
            rec = make_record(m, src)
            rec["match_in"] = bucket
            rec["ref_text"] = _clip_ref_text(ref_text)
            records.append(rec)

    planned_keys = {(eid, i) for eid, s in plan.items() for i in s}
    stats = {
        "enrich_batches": batches,
        "enrich_fetched": fetched,
        "enrich_cache_hits": len(needed - planned_keys),
        "truncated": truncated,
        # T48 新增：取消标记（T47 同步消费路径不读该键，行为不变）
        "cancelled": cancelled,
    }
    yield {"kind": "records", "records": records, "stats": stats}


async def search_videos(client, entity, p):
    """高级视频搜索（T48 起为流式内核的薄包装，签名/返回形态不变，
    TR-47.4 契约延续）。p 支持的键：

    query       原始查询串（逗号分词，词首 - 排除）
    mode        phrase/all/any/fuzzy/regex
    limit       结果上限
    min_mb/max_mb 体积区间
    date_from/date_to  日期区间 YYYY-MM-DD（含端点）
    scope       channel/comments/both
    sort        date/size
    deep_scan   可选（1/true）：无起始日期也放开到 HARD_SCAN_CAP 深度扫描
    返回 (records, meta)。
    """
    final, meta = [], None
    async for ev in search_videos_stream(client, entity, p, cancel=None):
        if ev["kind"] == "done":
            final, meta = ev["records"], ev["meta"]
    return final, meta


async def search_videos_stream(client, entity, p, cancel=None):
    """T48 大频道流式搜索内核（唯一搜索循环；薄包装/TUI/GUI 作业均消费它）。

    yield 三种事件（契约钉死）：
      {"kind": "progress", "phase": server_search|local_scan|enrich|filter,
       "scanned": int,        # 已过判定的候选消息条数（非视频/被滤也计），单调非降
       "matched": int,        # 截至此刻已确认命中（self+ref），单调非降
       "earliest": "YYYY-MM-DD" 或 "",   # 已判定消息中最早 m.date
       "truncated": bool,     # 补抓预算触顶后粘滞 True
       "capped": bool}        # 本地扫描触 scan_cap 为 True（dfrom 早停仍 False）
      {"kind": "items", "items": [record, ...]}   # 增量批（record 即 make_record 字典）
      {"kind": "done", "records": [...], "meta": {…13 键…}}

    任何事件都不携带百分比/total 字段（服务端 search= 总数未知，禁止假进度）。

    cancel：任何带同步 is_set() -> bool 的对象均可（threading.Event /
    asyncio.Event / 适配器），None 表示不可取消。取消只发生在协作检查点
    （server_search/local_scan 每条消息前、dfrom/cap 判定后；enrich 每批
    发请求前与返回后；filter 进入前），置位后在途单个请求可结束、之后不再
    发新请求，生成器直接 return（StopAsyncIteration），不抛异常、不发 done。

    items 与 limit 的不变量（防"行闪现后消失"）：
      - 脊线 newest→oldest 扫描；server_search 只入池不出 items；
        池内脊线未访问到的 id（server= 命中且早于停止位置）由 sweep 按
        同一判定补扫，候选集与 T47 完全相等；
      - sort=date 时即时下发需满足窗口条件：已确认命中中优先于本条的
        数量 + unknown pending 中优先于本条的数量 < limit（单实体脊线上
        与"emitted + newer_pending < limit"等价：此时无 hold 且全部
        已下发记录都更新；跨实体/带 ref 时该保守计数仍严格充分）。
        sort=size 一律 hold，filter 阶段一次下发；
      - 脊线上攒满 STREAM_FLUSH_ITEMS 条出一批；enrich 每批后增量出清；
        filter 阶段先出清残留、再把 final 中尚未下发的记录作为单个
        items 尾批补发，随后 yield done（Σitems 去重 == len(done.records)）。
    """
    # ---- 参数解析（与 T47 search_videos 逐行同口径）----
    query = p.get("query", "")
    mode = p.get("mode", "all")
    limit = max(1, int(p.get("limit", 30)))
    min_mb = float(p.get("min_mb") or 0)
    max_mb = float(p.get("max_mb") or 0)
    dfrom = _parse_date(p.get("date_from"))
    dto = _parse_date(p.get("date_to"))
    if dto is not None:
        import datetime as _dt
        dto = dto + _dt.timedelta(days=1)   # 含当天
    scope = p.get("scope", "channel")
    sort = p.get("sort", "date")
    # 深度扫描（可选开关，缺省关）：无起始日期也放开到 HARD_SCAN_CAP，
    # 让外部文本（引用/相册/转发）候选覆盖突破 MAX_LOCAL_SCAN 翻页窗口。
    deep_scan = str(p.get("deep_scan", "")).strip().lower() in (
        "1", "true", "yes", "y", "on")

    terms, excludes = parse_keywords(query)
    raw = query.split(",")[0] if mode == "phrase" else query
    use_server = bool(terms) and mode in ("phrase", "all", "any")
    server_terms = [raw.strip()] if mode == "phrase" else terms

    # 指定起始日期时，日期本身就是天然的扫描停止点：允许突破
    # MAX_LOCAL_SCAN 继续向旧消息翻页，直到扫到早于 dfrom 的消息；
    # 否则频道历史超过上限时，日期符合的早期视频会被漏扫。
    # deep_scan 显式开关同理放开（扫描停止点仍为 HARD_SCAN_CAP 安全阀）。
    scan_cap = (HARD_SCAN_CAP
                if (dfrom is not None or deep_scan) else MAX_LOCAL_SCAN)
    # 补抓批次预算随扫描窗口乘法联动（×窗口倍数 = scan_cap //
    # MAX_LOCAL_SCAN）：默认 5000 窗口 ×1 即既有 ENRICH_MAX_BATCHES，
    # 行为不变（含 monkeypatch 精确生效面）；deep_scan/起始日期把窗口
    # 放大到 HARD_SCAN_CAP 时 ×20，pending 候选同量增加，预算不联动
    # 则外部文本补抓被截断、深度扫描白改。
    enrich_max_batches = ENRICH_MAX_BATCHES * max(1, scan_cap // MAX_LOCAL_SCAN)

    def _cancelled():
        return cancel is not None and cancel.is_set()

    # ---- 流式状态（与 T47 合池/补抓语义同构）----
    pools = {}            # {ent_id: {mid: msg}}：本地池 + 服务端池 + 补抓缓存
    discuss = None
    capped = False
    truncated = False
    scanned = n_small = n_large = n_video = 0
    earliest_dt = None
    judge_seq = 0
    seq_by_key = {}       # (src, mid) -> 判定序号（排序稳定 tie-break 用）
    records_self = []     # self 命中（判定序；T47 records 前段同构）
    pending = []           # [(msg, src)]：已过体积/日期、terms 非空且自身未命中（判定序）
    emitted_keys = set()   # 已下发 (src, mid)（防"下发后被挤出 limit"）
    outbox = []            # items 待冲刷缓冲（帧内 ≤ STREAM_FLUSH_ITEMS）
    flush_ts = [time.time()]  # 上次出帧时刻（2.3 时间维出帧）
    ref_confirmed = {}     # (src, mid) -> record：零请求/增量预确认（R 权威兜底）
    conf_keys = []         # 已确认命中排序键（有序；窗口计数 bisect 用）
    uk_keys = []           # unknown pending 排序键（有序）

    def _state_key(m, seq):
        # date 降序优先键：ts 大者优先；同 ts 按判定序（与 T47 稳定排序 tie-break 一致）
        ts = m.date.timestamp() if getattr(m, "date", None) is not None else 0.0
        return (-ts, seq)

    def _progress(phase):
        return {
            "kind": "progress", "phase": phase,
            "scanned": scanned,
            "matched": len(records_self) + len(ref_confirmed),
            "earliest": (f"{earliest_dt:%Y-%m-%d}"
                         if earliest_dt is not None else ""),
            "truncated": truncated,
            "capped": capped,
        }

    def _flush(force=False):
        """取走待发帧：默认攒满 STREAM_FLUSH_ITEMS 出一批，或距上批
        ≥STREAM_FLUSH_SECS 且有余量即出（2.3 时间维：命中零散时也持续
        出帧，修整程空转）；force 时出清剩余并按 STREAM_FLUSH_ITEMS
        切帧（尾帧可不满）。每帧 ≤STREAM_FLUSH_ITEMS 不变。"""
        frames = []
        if force:
            while outbox:
                frames.append(outbox[:STREAM_FLUSH_ITEMS])
                del outbox[:STREAM_FLUSH_ITEMS]
        elif outbox and (len(outbox) >= STREAM_FLUSH_ITEMS
                         or time.time() - flush_ts[0] >= STREAM_FLUSH_SECS):
            frames.append(outbox[:STREAM_FLUSH_ITEMS])
            del outbox[:STREAM_FLUSH_ITEMS]
        if frames:
            flush_ts[0] = time.time()
        return frames

    def _confirm_hit(rec):
        """登记一条确认命中（self/ref 同用）并按窗口规则即时下发或 hold。
        hold 记录不丢：filter 阶段按 final 补发。"""
        key = (rec.get("src", SRC_CHANNEL), rec["msg"].id)
        if sort == "date":
            seq = seq_by_key.get(key, 0)
            k = _state_key(rec["msg"], seq)
            nk = bisect.bisect_left(conf_keys, k)
            ukn = bisect.bisect_left(uk_keys, k)
            if nk + ukn < limit:
                emitted_keys.add(key)
                outbox.append(rec)
        # sort=size 或窗口不满足 → hold（不进 outbox）
        bisect.insort(conf_keys, _state_key(rec["msg"], seq_by_key.get(key, 0)))

    def _confirm_ref(rec):
        """预确认一条 ref 命中（零请求/增量阶段）：摘除 unknown 计数并
        走窗口；R（_search_ref_matches 阶段 3）对其最终兜底。"""
        key = (rec.get("src", SRC_CHANNEL), rec["msg"].id)
        if key in ref_confirmed:
            return
        ref_confirmed[key] = rec
        k = _state_key(rec["msg"], seq_by_key.get(key, 0))
        i = bisect.bisect_left(uk_keys, k)
        if i < len(uk_keys) and uk_keys[i] == k:
            del uk_keys[i]
        _confirm_hit(rec)

    def _judge(m, src):
        """单条判定（与 T47 判定循环逐行同口径）：
        video → min_mb → max_mb → 日期区间 → self advanced_match。"""
        nonlocal scanned, n_small, n_large, n_video, earliest_dt, judge_seq
        scanned += 1
        judge_seq += 1
        seq_by_key[(src, m.id)] = judge_seq
        if (getattr(m, "date", None) is not None
                and (earliest_dt is None or m.date < earliest_dt)):
            earliest_dt = m.date
        if not is_video_message(m):
            return "filtered"
        n_video += 1
        size = m.document.size or 0
        if min_mb and size / 1048576 < min_mb:
            n_small += 1
            return "filtered"
        if max_mb and size / 1048576 > max_mb:
            n_large += 1
            return "filtered"
        if not _date_in_range(m.date, dfrom, dto):
            return "filtered"
        if advanced_match(m.message or "", terms, mode, excludes, raw):
            return "hit"
        if terms:
            # 体积/日期过滤在前：被滤掉的候选不进补抓（无 terms 时零补抓）
            return "pending"
        return "filtered"

    def _after_judge(st, m, src):
        """判定结果落地：hit → 确认/入 records_self；pending → 登记 unknown。"""
        if st == "hit":
            rec = make_record(m, src)
            _confirm_hit(rec)
            records_self.append(rec)
        elif st == "pending":
            pending.append((m, src))
            bisect.insort(uk_keys, _state_key(m, seq_by_key[(src, m.id)]))

    async def _spine(ent, src):
        """单实体 newest→oldest 脊线（server 入池不出 items + 本地整扫）+
        池内未判定 id 的 sweep 补扫；被取消时立即裸 return（调用方检查
        cancel 令牌）。"""
        nonlocal capped
        eid = getattr(ent, "id", None)
        pool = pools.setdefault(eid, {})
        if use_server:
            # server_search 阶段：服务端 search= 只入池（其顺序不保证
            # id 降序，判定统一由本地脊线/sweep 承接，候选集与 T47 相等）
            yield _progress("server_search")   # 阶段首帧（即使零命中也可见）
            for q in server_terms:
                agen = client.iter_messages(ent, search=q)
                async for m in agen:
                    if _cancelled():
                        return
                    pool[m.id] = m
                    yield _progress("server_search")
        judged_ids = set()
        local_n = 0        # 本地翻页条目计数（scan_cap 配额只计本地翻页，
                           # 服务端入池不挤占本地扫描配额）
        agen = client.iter_messages(ent)
        async for m in agen:
            if _cancelled():
                return
            # T47 local_scan 同序：入池 → 判定 → dfrom → cap
            pool[m.id] = m
            judged_ids.add(m.id)
            local_n += 1
            _after_judge(_judge(m, src), m, src)
            yield _progress("local_scan")
            for frame in _flush():
                yield {"kind": "items", "items": frame}
            if dfrom is not None and m.date is not None and m.date < dfrom:
                break   # 已覆盖全部目标时间段，不算触顶
            if local_n >= scan_cap:
                capped = True
                # 触顶帧：让 capped=True 的首个 progress 帧落在
                # local_scan 阶段（其后各帧粘滞携带）
                yield _progress("local_scan")
                break
        # 取消检查点：dfrom/cap 判定后（取下一条消息前）
        if _cancelled():
            return
        # sweep：池内脊线未访问到的 id（server= 命中且早于停止位置）按
        # 同一判定补扫一遍（id 降序，new→old），保证候选集与 T47 相等
        for mid in sorted(set(pool) - judged_ids, reverse=True):
            if _cancelled():
                return
            _after_judge(_judge(pool[mid], src), pool[mid], src)
            yield _progress("local_scan")
            for frame in _flush():
                yield {"kind": "items", "items": frame}

    def _incremental_ref_match():
        """用当前池对未确认 pending 跑一次三桶匹配（与 _search_ref_matches
        阶段 3 同口径同优先级：reply（池内目标 + quote_text）> album > fwd）。
        新确认且窗口安全的 ref 记录立即进 outbox；R 对最终结果权威兜底。"""
        if not pending:
            return
        # 相册组索引：从当前池重建（enrich 批次入池后池在增长）
        gidx = {}
        for _eid, _pool in pools.items():
            for _m in _pool.values():
                g = getattr(_m, "grouped_id", None)
                if isinstance(g, int):
                    gidx.setdefault(_eid, {}).setdefault(g, []).append(_m)
        ent_of = {SRC_CHANNEL: entity, SRC_COMMENT: discuss or entity}
        entity_map = {}
        for _ent in (entity, discuss):
            _eid = getattr(_ent, "id", None) if _ent is not None else None
            if _eid is None:
                continue
            entity_map[("PeerChannel", _eid)] = _ent
            entity_map[("PeerChat", _eid)] = _ent

        def _fb(src):
            return ent_of.get(src) or entity

        for m, src in pending:
            if (src, m.id) in ref_confirmed:
                continue
            eid = getattr(_fb(src), "id", None)
            bucket = None
            ref_text = None
            # reply 桶：池内被引用消息文本在前，quote_text 零请求兜底在后
            reply_to = getattr(m, "reply_to", None)
            rid = getattr(reply_to, "reply_to_msg_id", None)
            reply_texts = []
            if rid is not None:
                pk = _peer_key(getattr(reply_to, "reply_to_peer_id", None))
                ent_t = entity_map.get(pk) if pk else None
                if ent_t is None:
                    ent_t = _fb(src)
                tm = pools.get(getattr(ent_t, "id", None), {}).get(rid)
                if tm is not None:
                    reply_texts.append(getattr(tm, "message", "") or "")
            qt = getattr(reply_to, "quote_text", None)
            if qt:
                reply_texts.append(qt)
            for t in reply_texts:
                if advanced_match(t, terms, mode, excludes, raw):
                    bucket, ref_text = "reply", t
                    break
            # album 桶：同组消息 id 升序，取 id 最小者的命中文本
            if bucket is None and getattr(m, "grouped_id", None) is not None:
                sibs = sorted(
                    (x for x in gidx.get(eid, {}).get(m.grouped_id, ())
                     if x.id != m.id),
                    key=lambda x: x.id)
                for x in sibs:
                    t = getattr(x, "message", "") or ""
                    if advanced_match(t, terms, mode, excludes, raw):
                        bucket, ref_text = "album", t
                        break
            # fwd 桶：转发来源/发帖署名（零额外请求，三值非空去重）
            if bucket is None:
                ff = getattr(m, "fwd_from", None)
                seen_fwd = set()
                for t in (getattr(ff, "from_name", None),
                          getattr(ff, "post_author", None),
                          getattr(m, "post_author", None)):
                    if isinstance(t, str) and t.strip() and t not in seen_fwd:
                        seen_fwd.add(t)
                        if advanced_match(t, terms, mode, excludes, raw):
                            bucket, ref_text = "fwd", t
                            break
            if bucket is None:
                continue
            rec = make_record(m, src)
            rec["match_in"] = bucket
            rec["ref_text"] = _clip_ref_text(ref_text)
            _confirm_ref(rec)

    # ---- 候选拉取（与 T47 gather 同构：server 池 + 本地脊线合池去重）----
    if scope in ("channel", "both"):
        async for ev in _spine(entity, SRC_CHANNEL):
            yield ev
        if _cancelled():
            return
    if scope in ("comments", "both"):
        if _cancelled():
            return
        discuss = await get_discuss_entity(client, entity)
        if discuss is not None:
            async for ev in _spine(discuss, SRC_COMMENT):
                yield ev
            if _cancelled():
                return

    # ---- enrich 阶段（无 pending 也发一帧，phase 序完整）----
    yield _progress("enrich")
    extra_records = []
    st = {"enrich_batches": 0, "enrich_fetched": 0,
          "enrich_cache_hits": 0, "truncated": False, "cancelled": False}
    if pending:
        # 开工前零请求预解析：当前池即可确认的命中（quote_text/fwd 等）
        _incremental_ref_match()
        for frame in _flush(force=True):
            yield {"kind": "items", "items": frame}
        if _cancelled():
            return
        agen = _search_ref_matches(
            client, pending, entity, discuss, pools,
            terms, mode, excludes, raw, cancel=cancel,
            max_batches=enrich_max_batches)
        try:
            async for ev in agen:
                if ev["kind"] == "batch":
                    if ev.get("truncated"):
                        truncated = True
                    # 每批返回后：增量三桶匹配 + 下发安全窗口内的 ref 命中
                    _incremental_ref_match()
                    yield _progress("enrich")
                    for frame in _flush(force=True):
                        yield {"kind": "items", "items": frame}
                    if _cancelled():
                        return
                elif ev["kind"] == "records":
                    extra_records = ev["records"]
                    st = ev["stats"]
        finally:
            # 显式收尾：不留未关闭的异步生成器（asyncio 终结器任务）
            await agen.aclose()
    truncated = truncated or st.get("truncated", False)

    # ---- filter 阶段：排序/取前 limit/meta（与 T47 逐行同口径）----
    if _cancelled():
        return
    yield _progress("filter")
    records = records_self + extra_records
    if sort == "size":
        records.sort(key=lambda r: r["size"], reverse=True)
    else:
        records.sort(
            key=lambda r: (r["msg"].date.timestamp() if r["msg"].date else 0),
            reverse=True,
        )
    final = records[:limit]
    meta = {"n_small": n_small, "n_large": n_large,
            "n_video": n_video, "capped": capped,
            "scan_cap": scan_cap,
            "candidates": scanned, "mode": mode,
            "has_comments": scope in ("comments", "both"),
            # ---- T47 新增（任何路径都带齐，零值也给）----
            "ref_matched": sum(
                1 for r in final
                if r.get("match_in") in ("reply", "album", "fwd")),
            "truncated": truncated,
            "enrich_batches": st.get("enrich_batches", 0),
            "enrich_fetched": st.get("enrich_fetched", 0),
            "enrich_cache_hits": st.get("enrich_cache_hits", 0)}
    # 出清残留缓冲，再补发 final 中尚未下发的记录（单个 items 尾批），随后 done
    for frame in _flush(force=True):
        yield {"kind": "items", "items": frame}
    tail = [r for r in final
            if (r.get("src", SRC_CHANNEL), r["msg"].id) not in emitted_keys]
    if tail:
        yield {"kind": "items", "items": tail}
    yield {"kind": "done", "records": final, "meta": meta}


async def scan_videos(client, entity, limit, min_mb, includes, excludes):
    """兼容旧调用（CLI）：等效于 mode=all、仅频道、按日期排序。"""
    q = ",".join(list(includes) + ["-" + w for w in excludes])
    records, meta = await search_videos(client, entity, {
        "query": q, "mode": "all", "limit": limit,
        "min_mb": min_mb, "scope": "channel", "sort": "date",
    })
    return records, meta["n_small"]


def parse_excludes(text, count):
    """解析 '1,3,5-8' → 排除的序号集合（1 起）。非法输入抛 ValueError"""
    text = (text or "").strip()
    if not text:
        return set()
    out = set()
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            a, b = int(a), int(b)
            if a > b:
                a, b = b, a
            if a < 1 or b > count:
                raise ValueError(f"{part} 超出范围")
            out.update(range(a, b + 1))
        else:
            v = int(part)
            if v < 1 or v > count:
                raise ValueError(f"{v} 超出范围")
            out.add(v)
    return out


# ---------------- 下载 ----------------

def human(n):
    return f"{n / 1048576:.1f} MB" if n < 1073741824 else f"{n / 1073741824:.2f} GB"


def highlight(text, words, lmark, rmark):
    """把文字中实际出现的关键词用标记包裹（大小写不敏感）。

    返回 (标注后文字, 实际命中的词原值列表)。
    按词长降序构建正则，避免短词先匹配截断长词。
    """
    present = list(dict.fromkeys(w for w in words if w and w in text.lower()))
    if not present:
        return text, []
    pattern = "|".join(
        sorted((re.escape(w) for w in present), key=len, reverse=True)
    )

    hits = []

    def repl(m):
        hits.append(m.group(0))
        return f"{lmark}{m.group(0)}{rmark}"

    out = re.sub(pattern, repl, text, flags=re.IGNORECASE)
    return out, list(dict.fromkeys(hits))


def snippet_with_hits(text, includes, excludes, width=180):
    """压缩空白 → 高亮 → 智能截断，返回 (显示文字, 命中包含词, 命中排除词)。

    消息过长且命中位置靠后时，截取命中词附近的上下文，
    避免出现'标了✓却看不到命中位置'。
    """
    s = re.sub(r"\s+", " ", text or "").strip()
    shown, hit_inc = highlight(s, includes, "【", "】")
    shown, hit_exc = highlight(shown, excludes, "〔", "〕")

    if len(shown) <= width:
        return shown, hit_inc, hit_exc

    head = shown[:width]
    if "【" in head or "〔" in head:
        return head + "…", hit_inc, hit_exc

    # 命中位置在文字后部：回退到原文取命中词上下文再高亮
    low = s.lower()
    pos = [low.find(w) for w in (includes + excludes) if low.find(w) >= 0]
    if not pos:
        return s[:width] + "…", hit_inc, hit_exc
    p = min(pos)
    st, en = max(0, p - 40), min(len(s), p + 80)
    ctx = ("…" if st else "") + s[st:en] + ("…" if en < len(s) else "")
    ctx, _ = highlight(ctx, includes, "【", "】")
    ctx, _ = highlight(ctx, excludes, "〔", "〕")
    return ctx, hit_inc, hit_exc


# ---------------- MP4 快速起播（moov 前置，无损） ----------------

def _scan_boxes_file(f, start, end):
    """流式枚举文件 [start,end) 内顶层 box：(type, start, header_len, end)。
    只读取 box 头，不读 box 内容。结构非法返回 None。"""
    out = []
    off = start
    while off + 8 <= end:
        f.seek(off)
        hdr = f.read(16)
        if len(hdr) < 8:
            return None
        size, btype = struct.unpack(">I4s", hdr[:8])
        hlen = 8
        if size == 1:
            if len(hdr) < 16:
                return None
            size = struct.unpack(">Q", hdr[8:16])[0]
            hlen = 16
        elif size == 0:
            size = end - off
        box_end = off + size
        if size < hlen or box_end > end:
            return None
        out.append((btype, off, hlen, box_end))
        off = box_end
    if off != end:
        return None
    return out


def _copy_range(fsrc, fdst, start, end):
    """把文件 [start,end) 区间按 8MB 块复制到输出"""
    fsrc.seek(start)
    remaining = end - start
    while remaining:
        chunk = fsrc.read(min(remaining, 8 * 1024 * 1024))
        if not chunk:
            raise IOError("读取源文件时提前结束")
        fdst.write(chunk)
        remaining -= len(chunk)


def _patch_chunk_offsets(buf, start, end, delta):
    """递归容器 box，给 stco/co64 中的每个 chunk 偏移加 delta（就地改写）。"""
    off = start
    while off + 8 <= end:
        size, btype = struct.unpack_from(">I4s", buf, off)
        hlen = 8
        if size == 1:
            size = struct.unpack_from(">Q", buf, off + 8)[0]
            hlen = 16
        ds, box_end = off + hlen, off + size
        if size < hlen or box_end > end:
            break
        if btype in (b"trak", b"mdia", b"minf", b"stbl"):
            _patch_chunk_offsets(buf, ds, box_end, delta)
        elif btype == b"stco":
            n = struct.unpack_from(">I", buf, ds + 4)[0]
            for k in range(n):
                p = ds + 8 + 4 * k
                struct.pack_into(
                    ">I", buf, p,
                    struct.unpack_from(">I", buf, p)[0] + delta
                )
        elif btype == b"co64":
            n = struct.unpack_from(">I", buf, ds + 4)[0]
            for k in range(n):
                p = ds + 8 + 8 * k
                struct.pack_into(
                    ">Q", buf, p,
                    struct.unpack_from(">Q", buf, p)[0] + delta
                )
        off = box_end


def faststart_mp4(path):
    """把 MP4 的 moov 移到首个 mdat 之前并重写 chunk 偏移（不重编码）。

    moov 已在 mdat 前、非标准/复杂结构时保持原样、返回 False；
    完成重排返回 True。整个过程先写 .tmp 再原子替换，失败不破坏原文件。
    """
    file_size = os.path.getsize(path)
    tmp = path + ".tmp"
    try:
        with open(path, "rb") as f:
            head = f.read(min(file_size, 32))
            if len(head) < 8 or head[4:8] != b"ftyp":
                return False
            boxes = _scan_boxes_file(f, 0, file_size)
            if not boxes:
                return False
            moov_idx = [i for i, b in enumerate(boxes) if b[0] == b"moov"]
            mdat_idx = [i for i, b in enumerate(boxes) if b[0] == b"mdat"]
            if len(moov_idx) != 1 or len(mdat_idx) != 1:
                return False  # 缺 moov/mdat 或多 mdat 的复杂结构不处理
            mi, di = moov_idx[0], mdat_idx[0]
            _, mo_start, mo_hlen, mo_end = boxes[mi]
            _, mdat_start, _, mdat_end = boxes[di]
            if mo_end <= mdat_start:
                return False  # moov 本来就在前面，无需处理

            # 新排列：原顺序去掉 moov，插到第一个 mdat 之前
            new_order = [b for b in boxes if b[0] != b"moov"]
            insert_at = next(i for i, b in enumerate(new_order) if b[0] == b"mdat")
            new_order.insert(insert_at, boxes[mi])

            # 计算每个 box 的新起点与字节位移
            pos = 0
            layout = []
            delta_by_start = {}
            for btype, o, hlen, e in new_order:
                delta_by_start[o] = pos - o
                layout.append((btype, o, hlen, e, pos))
                pos += e - o
            if pos != file_size:
                return False
            mdat_delta = delta_by_start[mdat_start]

            # 只读 moov 本体（通常 1–2MB），改写其中 chunk 偏移
            f.seek(mo_start + mo_hlen)
            moov_buf = bytearray(f.read(mo_end - mo_start - mo_hlen))
            _patch_chunk_offsets(moov_buf, 0, len(moov_buf), mdat_delta)

            with open(tmp, "wb") as out:
                for btype, o, hlen, e, new_pos in layout:
                    if btype == b"moov":
                        # 原 box 头 + 改写后的 body
                        f.seek(o)
                        out.write(f.read(mo_hlen))
                        out.write(moov_buf)
                    else:
                        _copy_range(f, out, o, e)
        # 源文件句柄已关闭，Windows 上才能原子替换
        if os.path.getsize(tmp) != file_size:
            raise IOError("重排后大小不符")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
    return True


# ---------------- 下载监视面板 ----------------

def human_speed(bps):
    if bps >= 1048576:
        return f"{bps / 1048576:.2f} MB/s"
    if bps >= 1024:
        return f"{bps / 1024:.1f} KB/s"
    return f"{bps:.0f} B/s"


def human_secs(s):
    if s < 0 or s != s or s == float("inf"):
        return "--:--"
    s = int(s)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"


def _bar(pct, width=22):
    pct = max(0, min(100, pct))
    fill = int(width * pct / 100)
    return "[" + "#" * fill + "-" * (width - fill) + "]"


def disp_width(s):
    """字符串在等宽终端里的显示宽度（中文/全角算 2）"""
    w = 0
    for c in s:
        if unicodedata.east_asian_width(c) in ("W", "F"):
            w += 2
        else:
            w += 1
    return w


def trunc_width(s, width):
    """按显示宽度截断，超出部分用 … 代替"""
    if disp_width(s) <= width:
        return s
    out, w = "", 0
    for c in s:
        cw = 2 if unicodedata.east_asian_width(c) in ("W", "F") else 1
        if w + cw + 1 > width:   # 预留 … 的位置
            return out + "…"
        out += c
        w += cw
    return out


def wrap_text(s, width, prefix=""):
    """按显示宽度换行。首行加 prefix，续行用与 prefix 等宽的空格缩进。
    支持原文中的换行符。"""
    width = max(10, width)
    iw = disp_width(prefix)
    cont_indent = " " * iw
    out = []
    for raw in str(s).split("\n"):
        cur, curw = prefix, iw
        for c in raw:
            cw = 2 if unicodedata.east_asian_width(c) in ("W", "F") else 1
            if curw + cw > width:
                out.append(cur.rstrip())
                cur, curw = cont_indent, iw
            cur += c
            curw += cw
        out.append(cur)
    return out or [""]


# ---------------- 下载全阶段状态机（T49；内存态，绝不进任务 JSON） ----------------

# T49：下载全阶段（内存态，不进任务 JSON）。枚举字符串为跨语言契约，禁改。
PHASE_PREPARING = "preparing"
PHASE_DISK_CHECK = "disk_check"
PHASE_FETCHING = "fetching"
PHASE_REFRESHING = "refreshing"
PHASE_DOWNLOADING = "downloading"
PHASE_FLOOD_WAIT = "flood_wait"
PHASE_RECONCILING = "reconciling"
PHASE_DONE = "done"
PHASE_ABORTED = "aborted"
PHASE_ERROR = "error"

DOWNLOAD_PHASES = (
    PHASE_PREPARING, PHASE_DISK_CHECK, PHASE_FETCHING,
    PHASE_REFRESHING, PHASE_DOWNLOADING, PHASE_FLOOD_WAIT,
    PHASE_RECONCILING,
)
PHASE_TERMINAL = (PHASE_DONE, PHASE_ABORTED, PHASE_ERROR)
PHASE_LABELS = {
    "preparing": "准备", "disk_check": "磁盘检查", "fetching": "读取清单",
    "refreshing": "刷新引用", "downloading": "下载中",
    "flood_wait": "限速等待", "reconciling": "对账",
    "done": "完成", "aborted": "已中止", "error": "失败",
}


def phase_text(phase, detail=""):
    """唯一中文拼装函数：短名 + 可选细节。GUI/WS/TUI 全部消费其产物。"""
    return PHASE_LABELS[phase] + ((" · " + detail) if detail else "")


# 合法阶段转移表（§3.1）：任意非终态可进 aborted；worker/feeder 致命
# 错误进 error；flood_wait 自 fetching/downloading 进、leave 回原阶段；
# refreshing 仅自 downloading 进、leave 回 downloading。
_PHASE_NEXT = {
    "preparing": frozenset({"disk_check", "error", "aborted"}),
    # done 仅是"零记录/空计划的正常收尾"边：fetching 一批记录都没入队
    # （全部条目已删除）或空 planned 直接结束；不得再放宽其它边。
    "disk_check": frozenset(
        {"reconciling", "fetching", "downloading", "done", "error", "aborted"}),
    "reconciling": frozenset(
        {"fetching", "downloading", "error", "aborted"}),
    "fetching": frozenset(
        {"downloading", "flood_wait", "done", "error", "aborted"}),
    "downloading": frozenset(
        {"refreshing", "flood_wait", "done", "error", "aborted"}),
    "refreshing": frozenset({"downloading", "error", "aborted"}),
    "flood_wait": frozenset({"fetching", "downloading", "error", "aborted"}),
    "done": frozenset(),
    "aborted": frozenset(),
    "error": frozenset(),
}


def phase_transition_ok(old, new):
    """阶段转移是否合法（TR-49.1 状态机校验表同源）。"""
    if old in PHASE_TERMINAL:
        return False
    return new in _PHASE_NEXT.get(old, frozenset())


class Monitor:
    """统一收集各视频字节进度、速度、状态，并按简洁/详细两种模式渲染。

    下载协程只调用 start/add/finish/error；渲染由独立的 display_loop 定时做，
    与按键线程（I 切换）互不干扰。
    """

    def __init__(self, planned, parallel, title, vt, push_cb=None):
        """planned：占位条目列表，每项含 mid/src/size/name/date。
        无需真实 Message 对象——消息可在下载开始后分批补取（边读边下），
        条目取到后通过 fill() 补充日期/正文等展示信息。"""
        self.vt = vt
        self.title = title
        self.parallel = parallel
        self.push_cb = push_cb      # GUI 回调：每帧推送 JSON 状态
        self.detailed = False
        self.t0 = time.monotonic()
        self.lock = asyncio.Lock()
        self.entries = []
        self.order = {}
        # 全局测速滑动窗口：元素 (monotonic 时刻, 全部条目已下字节总量)，
        # 仅保留最近 SPEED_WIN_SEC 秒，总速度取窗口首尾的平均，每秒更新。
        self.win = []
        for n, p in enumerate(planned, 1):
            src = p.get("src", SRC_CHANNEL)
            e = {
                "n": n, "mid": p["mid"], "src": src,
                "key": (src, p["mid"]),
                "total": p["size"],
                "name": p["name"],
                "date": p.get("date", ""),
                "text": "",
                "done": 0, "status": "等待中", "speed": 0.0,
                "samples": [], "started": None, "ended": None,
                "error": "", "final": "",
                # T51：条目下载账号（main 默认；快照/行标记消费，
                # 绝不进任务 JSON 以外的持久面）
                "acct": ACCT_MAIN,
            }
            self.entries.append(e)
            self.order[(src, p["mid"])] = e
        self.active_count = 0
        # 边读边下的读取进度（fetched/总数/说明）；非 lazy 模式直接置完成
        self.fetched = 0
        self.fetch_total = len(planned)
        self.fetching = False
        self.fetch_note = ""
        # T49 阶段状态机（内存态；详见模块级 PHASE_* 契约）
        self.phase = "preparing"
        self.phase_detail = phase_text("preparing")
        self.phase_index = 0
        self.phase_total = 0
        self.phase_entered = time.monotonic()
        self.phase_cb = None          # 4 参回调 (phase, text, index, total)
        self.flood_probe = None       # enter_flood 等待期的中止探测（run 侧注入）
        self._phase_before_flood = None
        self._last_fetch_ts = time.monotonic()   # 最后一批消息到达时刻
        self._refresh_note = ""       # refreshing 期间的条目标注（#mid）

    async def fill(self, rec):
        """记录从服务器取到后，补充占位条目的展示信息。"""
        async with self.lock:
            e = self.order.get(_rec_key(rec))
            if e is None:
                return
            m = rec["msg"]
            e["total"] = rec["size"]
            if m.date:
                e["date"] = f"{m.date:%Y-%m-%d %H:%M}"
            e["text"] = getattr(m, "message", "") or ""

    async def set_fetch(self, done, total, note="", fetching=True):
        async with self.lock:
            self.fetched, self.fetch_total = done, total
            self.fetching = fetching
            if fetching:
                self._last_fetch_ts = time.monotonic()
            if note:
                self.fetch_note = note

    # ---------- T49 阶段 API（均持锁改字段、锁外发回调；回调异常只记不抛） ----------

    def _fire_phase_cb(self, phase, text, index, total):
        cb = self.phase_cb
        if cb is None:
            return
        try:
            cb(phase, text, index, total)
        except Exception:
            pass    # 回调异常绝不上抛（GUI/TUI 渲染失败不影响下载）

    async def set_phase(self, phase, detail="", index=None, total=None):
        """切换阶段。未知阶段是编程错误（AssertionError）；非法转移拒绝并
        保持原阶段（打印技术日志）；终态幂等不可再变。detail 为裸细节，
        phase_detail 存 phase_text 拼装产物（全链同源文案）。
        T24：同相位 detail 刷新（old == phase 且 detail 非空）时就地
        更新 phase_detail 并照常发通知——不校验转移表、不改 self.phase、
        不重置 phase_entered、不动 index/total；供断网进入/恢复提示用
        （断网期不新增 phase，用既有 downloading + detail 表达）。"""
        if phase not in PHASE_LABELS:
            raise AssertionError(f"未知下载阶段：{phase!r}")
        async with self.lock:
            old = self.phase
            if old == phase and not detail:
                # 同阶段重申（open 的 preparing 首帧）：仍发一次通知；
                # T24 恢复清空：detail 为空时把 phase_detail 复位为
                # 该相位短名（断网提示语清除，同相位不改 self.phase）。
                if self.phase_detail != phase_text(phase, ""):
                    self.phase_detail = phase_text(phase, "")
                self._fire_phase_cb(phase, self.phase_detail,
                                    self.phase_index, self.phase_total)
                return
            if old in PHASE_TERMINAL:
                return                      # 终态不可再变（幂等）
            if old == phase and detail:
                # T24 同相位 detail 刷新：就地更新文案并通知（不转移）
                self.phase_detail = phase_text(phase, detail)
                text = self.phase_detail
                index_ = self.phase_index
                total_ = self.phase_total
                self._fire_phase_cb(phase, text, index_, total_)
                return
            if not phase_transition_ok(old, phase):
                print(f"[!] 忽略非法阶段转移 {old} -> {phase}")
                return
            self.phase = phase
            self.phase_detail = phase_text(phase, detail)
            if index is not None:
                self.phase_index = index
            if total is not None:
                self.phase_total = total
            self.phase_entered = time.monotonic()
            if phase == "refreshing":
                self._refresh_note = detail
            text = self.phase_detail
            index_ = self.phase_index
            total_ = self.phase_total
        self._fire_phase_cb(phase, text, index_, total_)

    async def tick_phase(self, detail=None, index=None, total=None):
        """停留期心跳：不换阶段、不重置 phase_entered；仅更新细节/计数。"""
        async with self.lock:
            if self.phase in PHASE_TERMINAL:
                return
            if detail is not None:
                self.phase_detail = phase_text(self.phase, detail)
            if index is not None:
                self.phase_index = index
            if total is not None:
                self.phase_total = total
            text = self.phase_detail
            index_ = self.phase_index
            total_ = self.phase_total
            phase = self.phase
        self._fire_phase_cb(phase, text, index_, total_)

    async def enter_flood(self, seconds, where):
        """服务器限速等待：切 flood_wait 并每秒倒计时；等待可被打断——
        feeder 路径依赖任务 cancel（CancelledError 上抛），download 路径
        每轮探测进入时捕获的本路 flood_probe（返回控制异常类即抛出，
        沿用 P/R/S 语义；捕获而非每轮读槽：并行期他路 worker 的
        装卸不再串槽）。正常醒后由调用方调 leave_flood() 回原阶段。"""
        async with self.lock:
            self._phase_before_flood = self.phase
            self.phase = "flood_wait"
            self.phase_detail = phase_text("flood_wait", f"等待 {seconds}s")
            self.phase_total = seconds
            self.phase_index = seconds
            self.phase_entered = time.monotonic()
            text = self.phase_detail
            index_ = self.phase_index
            total_ = self.phase_total
            probe = self.flood_probe      # 进入时捕获本路探测（T49 缺口 C）
        self._fire_phase_cb("flood_wait", text, index_, total_)
        remaining = int(seconds)
        try:
            while remaining > 0:
                await asyncio.sleep(min(1.0, remaining))
                remaining -= 1
                if probe is not None:
                    tripped = probe()
                    if tripped is not None:
                        raise tripped  # PauseFile/RemoveFile/SkipFile/CancelledError
                if remaining > 0:
                    await self.tick_phase(detail=f"等待 {remaining}s",
                                          index=remaining, total=seconds)
            # 归零：最后一帧倒计时为 0
            await self.tick_phase(detail="等待 0s", index=0, total=seconds)
        except BaseException:
            # 等待被打断（探测命中 P/R/S/abort，或任务被 cancel）：先把
            # 阶段还原到进入 flood 前的阶段再上抛——控制语义不变，且终态
            # 收口不会从 flood_wait 走出非法转移。
            back = self._phase_before_flood
            if back and back not in PHASE_TERMINAL:
                try:
                    await self.leave_flood()
                except Exception:
                    pass
            raise

    async def leave_flood(self):
        """FloodWait 正常醒后回原阶段（非法路径保持不动）。"""
        async with self.lock:
            back = self._phase_before_flood
            self._phase_before_flood = None
            if not back or not phase_transition_ok(self.phase, back):
                return
            self.phase = back
            self.phase_detail = PHASE_LABELS[back]
            self.phase_entered = time.monotonic()
            text = self.phase_detail
            index_ = self.phase_index
            total_ = self.phase_total
        self._fire_phase_cb(back, text, index_, total_)

    def snapshot_json(self):
        """当前状态的可序列化快照（GUI 轮询/推送用）"""
        snap = self._snapshot()
        return {
            "title": self.title,
            "parallel": self.parallel,
            "elapsed": snap["now"] - self.t0,
            "agg_speed": round(snap["agg_speed"], 1),
            "total_bytes": snap["total_bytes"],
            "total_tot": snap["total_tot"],
            "remaining": snap["remaining"],
            "eta": (None if snap["eta"] == float("inf") else round(snap["eta"], 1)),
            "fetching": self.fetching,
            "fetched": self.fetched,
            "fetch_total": self.fetch_total,
            "fetch_note": self.fetch_note,
            # T49 纯增 4 键（旧消费方零破坏；绝不进任务 JSON）
            "phase": self.phase,
            "phase_detail": self.phase_detail,
            "phase_index": self.phase_index,
            "phase_total": self.phase_total,
            "counts": {"done": snap["done"], "fail": snap["fail"],
                       "skip": snap["skip"], "paused": snap["paused"],
                       "active": len(snap["active"])},
            "items": [
                {
                    "n": e["n"], "mid": e["mid"], "src": e["src"],
                    "name": e["name"],
                    "date": e["date"], "size": e["total"],
                    "done": e["done"],
                    "pct": round(e["done"] * 100 / (e["total"] or 1), 1),
                    "speed": round(e["speed"], 1),
                    "status": e["status"], "error": e["error"],
                    # T51 纯增 1 键：本条下载账号（默认 main）
                    "acct": e.get("acct", ACCT_MAIN),
                    # text（消息原文）不随每秒轮询返回，前端展开时按需取
                }
                for e in self.entries
            ],
        }

    async def seed(self, key, bytes_already):
        """续传：把已存在分片的字节计入初始进度（不计速度）"""
        if bytes_already <= 0:
            return
        async with self.lock:
            e = self.order[key]
            e["done"] = min(e["total"], e["done"] + bytes_already)

    async def start(self, key):
        async with self.lock:
            e = self.order[key]
            if e["started"] is None:
                e["started"] = time.monotonic()
                e["status"] = "下载中"
                self.active_count += 1

    async def add(self, key, nb):
        if nb <= 0:
            return
        now = time.monotonic()
        async with self.lock:
            e = self.order[key]
            e["done"] = min(e["total"], e["done"] + nb)
            e["samples"].append((now, e["done"]))
            # 只保留最近 SPEED_WIN_SEC 秒的样本用于测速
            while len(e["samples"]) > 2 and now - e["samples"][0][0] > SPEED_WIN_SEC:
                e["samples"].pop(0)

    async def finish(self, key, final):
        async with self.lock:
            e = self.order.get(key)
            if e is None or e["final"]:
                return                    # 幂等，避免重复计数
            e["final"] = final
            e["status"] = final
            e["speed"] = 0.0
            # 完成/已存在：进度补满（部分条目可能未走 add 直接判定）
            if final in ("完成", "已存在"):
                e["done"] = e["total"]
            if e["started"] is not None and e["ended"] is None:
                e["ended"] = time.monotonic()
                self.active_count = max(0, self.active_count - 1)

    async def restart(self, key, keep_done=True):
        """暂停/失败项重新排队：重置运行态为等待中。
        keep_done=True 保留已下字节（parallel 分片续传）；
        False 清零（simple 路径重来）。"""
        async with self.lock:
            e = self.order.get(key)
            if e is None:
                return
            e["final"] = ""
            e["status"] = "等待中"
            e["speed"] = 0.0
            e["started"] = e["ended"] = None
            e["samples"] = []
            e["error"] = ""
            if not keep_done:
                e["done"] = 0

    async def remove(self, key):
        """条目被移除：从面板删除；进行中的先扣活动计数。"""
        async with self.lock:
            e = self.order.pop(key, None)
            if e is None:
                return
            self.entries = [x for x in self.entries if x is not e]
            if e["started"] is not None and e["ended"] is None:
                self.active_count = max(0, self.active_count - 1)

    async def error(self, key, msg):
        async with self.lock:
            e = self.order[key]
            e["error"] = msg

    def _snapshot(self):
        now = time.monotonic()
        active = []
        total_bytes = total_tot = 0
        done_cnt = fail_cnt = skip_cnt = paused_cnt = 0
        cutoff = now - SPEED_WIN_SEC
        for e in self.entries:
            st = e["status"]
            if st == "下载中":
                sm = e["samples"]
                # add() 只在收到分片时清旧样本；停滞期要靠这里补清窗口外数据
                while len(sm) > 1 and sm[0][0] < cutoff:
                    sm.pop(0)
                spd = 0.0
                if len(sm) >= 2:
                    # 窗口右端锚定“当前时刻”：网络停滞期间没有新样本，
                    # 分母随时间继续增大，速度平滑衰减到 0，不会卡在旧值。
                    dt = now - sm[0][0]
                    if dt > 0.1:
                        spd = max(0.0, (sm[-1][1] - sm[0][1]) / dt)
                e["speed"] = spd
                active.append(e)
            total_bytes += e["done"]
            total_tot += e["total"]
            if e["final"] in ("完成", "已存在"):
                done_cnt += 1
            elif e["final"] == "失败":
                fail_cnt += 1
            elif e["final"] == "已跳过":
                skip_cnt += 1
            elif e["final"] == "已暂停":
                paused_cnt += 1
        # 全局滑动窗口：记录每次快照时“全部条目已下字节总量”，
        # 总速度 = 最近 SPEED_WIN_SEC 秒的字节增量 / 实际经过时间，每秒更新。
        win = self.win
        win.append((now, total_bytes))
        while len(win) > 1 and win[0][0] < cutoff:
            win.pop(0)
        agg_speed = 0.0
        if len(win) >= 2:
            dt = now - win[0][0]
            if dt > 0.1:
                agg_speed = max(0.0, (win[-1][1] - win[0][1]) / dt)
        remaining = max(0, total_tot - total_bytes)
        eta = remaining / agg_speed if agg_speed > 0 else float("inf")
        return {
            "now": now, "active": active, "agg_speed": agg_speed,
            "total_bytes": total_bytes, "total_tot": total_tot,
            "remaining": remaining, "eta": eta,
            "done": done_cnt, "fail": fail_cnt, "skip": skip_cnt,
            "paused": paused_cnt,
        }

    def _entry_lines(self, e, now, w):
        """单个条目的逻辑行（已按宽度换行）。详细模式含消息原文。"""
        tot = e["total"] or 1
        pct = e["done"] * 100 / tot
        active = e["status"] == "下载中"
        lines = []

        if self.detailed:
            lines.append(
                f"{ACCT_MARK[e.get('acct', ACCT_MAIN)]} "
                f"[{e['n']}] 消息 #{e['mid']}   {e['status']}"
            )
            lines += wrap_text(e["name"], w, "     文件：")
            head = (
                f"日期：{e['date'] or '-'}    大小：{human(e['total'])}"
            )
            lines += wrap_text(head, w, "     ")
            barw = max(8, min(24, w - 40))
            lines.append(
                f"     {_bar(pct, barw)} {pct:5.1f}%  "
                f"{human(e['done'])}/{human(e['total'])}"
            )
            sp = human_speed(e["speed"]) if active else "--"
            dur = ((e["ended"] or now) - e["started"]) if e["started"] else 0
            rem = (
                (e["total"] - e["done"]) / e["speed"]
                if e["speed"] > 0 else float("inf")
            )
            lines += wrap_text(
                f"速度 {sp}   本视频已用 {human_secs(dur)}   剩余 {human_secs(rem)}",
                w, "     "
            )
            if e["error"]:
                lines += wrap_text(e["error"], w, "     备注：")
            # Telegram 对应消息原文
            if e["text"].strip():
                lines.append("     消息内容：")
                lines += wrap_text(e["text"], w, "       ")
            else:
                lines.append("     消息内容：（无文字）")
        else:
            barw = max(8, min(24, w - 30))
            tail = human_speed(e["speed"]) if active else e["status"]
            lines.append(
                f"{ACCT_MARK[e.get('acct', ACCT_MAIN)]}"
                f"[{e['n']}] #{e['mid']} {_bar(pct, barw)} {pct:5.1f}% {tail}"
            )

        # 统一再裁一次，保证任何一行都不超出窗口宽度
        fixed = []
        for ln in lines:
            if disp_width(ln) <= w:
                fixed.append(ln)
            else:
                fixed += wrap_text(ln, w)
        return fixed

    def render(self, snap):
        """按当前控制台实际宽高渲染；窗口过小也保证关键信息可见。"""
        size = shutil.get_terminal_size((80, 25))
        w, h = max(20, size.columns), max(6, size.lines)
        now = snap["now"]
        el = now - self.t0
        mode = "详细" if self.detailed else "简洁"

        header = [
            "=" * w,
            trunc_width(
                f" {self.title}   模式:{mode}  已用 {human_secs(el)}", w
            ),
            trunc_width(
                f" 阶段：{self.phase_detail}  （{human_secs(now - self.phase_entered)}）",
                w
            ),
            trunc_width(
                f" 并发 {self.parallel} 路 | 进行中 {len(snap['active'])} | "
                f"完成 {snap['done']} 失败 {snap['fail']} 跳过 {snap['skip']} "
                f"暂停 {snap['paused']}",
                w
            ),
            "-" * w,
        ]
        footer = [
            "-" * w,
            trunc_width(
                f" [{PHASE_LABELS.get(self.phase, self.phase)}] "
                f"总进度 {snap['total_bytes']/(snap['total_tot'] or 1)*100:5.1f}%  "
                f"{human(snap['total_bytes'])}/{human(snap['total_tot'])}", w
            ),
            trunc_width(
                f" 总速度 {human_speed(snap['agg_speed'])}   "
                f"剩余 {human(snap['remaining'])}   "
                f"预计剩余 {human_secs(snap['eta'])}", w
            ),
            "=" * w,
            trunc_width(" I=详细  P=暂停  R=继续  S=跳过  X=移除  Q=中止", w),
        ]

        if self.detailed:
            # 进行中的优先显示，其余按清单顺序
            groups = [
                self._entry_lines(e, now, w)
                for e in snap["active"]
            ] + [
                self._entry_lines(e, now, w)
                for e in self.entries if e["status"] != "下载中"
            ]
        else:
            groups = [
                self._entry_lines(e, now, w) for e in snap["active"]
            ]

        # 按高度贪心放置：保证页眉、页脚始终可见
        budget = h - len(header) - len(footer)
        body, hidden = [], 0
        for i, g in enumerate(groups):
            sep = [""] if body else []
            if budget - len(sep) <= 0:
                hidden += 1
                continue
            body += sep
            budget -= len(sep)
            take = min(len(g), budget)
            body += g[:take]
            budget -= take
            if take < len(g):
                note = "  …（本条目未显示完，放大窗口可见更多）"
                if budget > 0:
                    body.append(trunc_width(note, w))
                    budget -= 1
                hidden += 1

        if hidden:
            body.append(trunc_width(
                f"  …另有 {hidden} 项未显示：放大窗口或按 I 切换简洁模式", w
            ))

        lines = header + body + footer
        return lines[:h]

    async def display_loop(self, keys):
        # 隐藏光标，避免面板刷新时光标闪动
        if self.vt:
            sys.stdout.write("\033[?25l")
            sys.stdout.flush()
        try:
            while True:
                if keys.toggle.is_set():
                    keys.toggle.clear()
                    self.detailed = not self.detailed
                async with self.lock:
                    snap = self._snapshot()
                    lines = self.render(snap)
                if self.vt:
                    # 每帧回到左上角 + 清屏，窗口大小变化也不会留残影
                    sys.stdout.write("\033[H\033[J" + "\n".join(lines))
                    sys.stdout.flush()
                else:
                    os.system("cls" if os.name == "nt" else "clear")
                    print("\n".join(lines))
                await asyncio.sleep(0.4)
        except asyncio.CancelledError:
            pass
        finally:
            if self.vt:
                sys.stdout.write("\033[?25h")
                sys.stdout.flush()


def _ctl_or_abort(ctl, state):
    """T57 根因 B：在途断点统一信号——优先条目自身 ctl（取消>跳过>
    暂停，原有语义）；无条目信号时查任务级 abort（state["session"].
    abort），中止升格为 CancelledError 穿出（worker 的 except
    asyncio.CancelledError 直穿与 schedule 循环顶取消全套同源同一
    语义，.part/.done 标记保留可续传）。state 为 None/无 session 时
    退化为纯 ctl（兼容直接调用下载函数的既有夹具）。"""
    t = ctl.tripped()
    if t is not None:
        return t
    sess_abort = getattr((state or {}).get("session"), "abort", None)
    if sess_abort is not None and sess_abort.is_set():
        return asyncio.CancelledError
    return None


async def parallel_download(client, item, target, ctl, state=None):
    """断点续传：完成的分片写一个零字节 .partN.done 标记；重试时只下
    未完成分片（未完成分片从头覆盖重下）。
    ctl：ItemCtl（skip/pause/cancel），在每个断点检查。
    state：{'monitor': Monitor}，进度/速度全部上报到统一面板。
    """
    msg, size = item["msg"], item["size"]
    key = (item.get("src", SRC_CHANNEL), msg.id)
    mon = state["monitor"] if state else None
    ranges = [(off, min(PART, size - off)) for off in range(0, size, PART)]
    sem = asyncio.Semaphore(CONCURRENT)

    # 下载写入 .dl 临时文件，完成后才改名为成品，避免半成品被误判
    work = target + ".dl"
    if not (os.path.exists(work) and os.path.getsize(work) == size):
        with open(work, "wb") as f:
            f.truncate(size)

    # 续传：统计已完成分片的字节，作为面板初始进度（不计入下载速度）
    already = 0
    for i, (_, ln) in enumerate(ranges):
        if os.path.exists(f"{work}.part{i}.done"):
            already += ln
    if mon:
        await mon.seed(key, already)
        await mon.start(key)

    async def grab(off, ln, idx):
        mark = f"{work}.part{idx}.done"
        if os.path.exists(mark):
            return
        got = 0
        reset = 0               # T24 停滞重置计数（每代新迭代器超时 +1）
        net_down = False        # T24 断网等待态（局部标志去重，每事件一行）
        net_rounds = 0          # T24 断网轮询轮数（recover wait= 换算）
        async with sem:
            tripped = _ctl_or_abort(ctl, state)
            if tripped:
                raise tripped
            while True:
                if net_down:
                    # 断网等待：暂停一切计时；每轮先过 ctl（暂停/取消/
                    # 跳过同样一个轮询周期内生效，无不可取消阻塞），
                    # 再按 _NET_SLEEP 轮询；恢复后丢弃旧迭代器从 off
                    # 重下——不消耗分片重置次数、不消耗 T22 重试预算。
                    tripped = _ctl_or_abort(ctl, state)
                    if tripped:
                        raise tripped
                    await _NET_SLEEP(NET_POLL_SECS)
                    net_rounds += 1
                    if not _net_is_connected(client):
                        continue
                    net_down = False
                    _net_log(f"id={msg.id} part={idx} recover=1 "
                             f"wait={net_rounds * NET_POLL_SECS:g}")
                    if mon:
                        await mon.set_phase("downloading", "")
                # 每次（首取/断网恢复/停滞重置）重新取迭代器：从 off 起步
                it = client.iter_download(
                    msg.media, offset=off,
                    request_size=512 * 1024, file_size=size
                ).__aiter__()
                complete = False
                # 独立文件句柄，直写到本分片的字节区间
                with open(work, "r+b") as f:
                    f.seek(off)
                    got = 0
                    while True:
                        tripped = _ctl_or_abort(ctl, state)
                        if tripped:
                            raise tripped
                        try:
                            chunk = await asyncio.wait_for(
                                it.__anext__(), timeout=STALL_SECS
                            )
                        except StopAsyncIteration:
                            if got < ln:
                                raise IOError(f"分片 {idx} 不完整")
                            complete = True
                            break
                        except asyncio.TimeoutError:
                            # T24 超时分流：先探测连通性；FloodWait 路径
                            # 不在此（T22 原链，互不交叉）
                            if not _net_is_connected(client):
                                # 断网：进入有界轮询等待（事件去重，
                                # 不消耗重置次数）
                                if not net_down:
                                    net_down = True
                                    net_rounds = 0
                                    _net_log(
                                        f"id={msg.id} part={idx} down=1")
                                    if mon:
                                        await mon.set_phase(
                                            "downloading",
                                            "网络断开，等待自动恢复")
                                break
                            # 连通正常：分片停滞重置（取消/丢弃旧迭代器）
                            reset += 1
                            if reset > SHARD_RESET_MAX:
                                _shard_log(
                                    f"id={msg.id} part={idx} giveup=1 "
                                    f"reset={SHARD_RESET_MAX} reason=stall")
                                raise ShardResetExhausted(msg.id, idx)
                            _shard_log(
                                f"id={msg.id} part={idx} "
                                f"reset={reset}/{SHARD_RESET_MAX} "
                                f"reason=stall")
                            break
                        take = min(len(chunk), ln - got)
                        if take > 0:
                            f.write(chunk[:take])
                            got += take
                            if mon:
                                await mon.add(key, take)
                        if got >= ln:
                            complete = True
                            break
                if net_down:
                    continue            # 外层 while 头：断网轮询等待
                if complete:
                    break               # 分片取满，收尾
                # 停滞重置分支：got < ln 且非断网 → 外层循环重新取迭代器
        if got < ln:
            raise IOError(f"分片 {idx} 不完整")
        # 零字节完成标记
        open(mark, "wb").close()

    await asyncio.gather(
        *(grab(off, ln, i) for i, (off, ln) in enumerate(ranges))
    )
    if os.path.getsize(work) != size:
        raise IOError("最终文件大小不符")
    for i in range(len(ranges)):
        try:
            os.remove(f"{work}.part{i}.done")
        except OSError:
            pass
    os.replace(work, target)   # 原子改名，成品落位


async def simple_download(client, item, target, ctl, state=None):
    """小文件/未知大小：普通下载 + 停滞/控制轮询，进度上报统一面板"""
    msg = item["msg"]
    key = (item.get("src", SRC_CHANNEL), msg.id)
    mon = state["monitor"] if state else None
    loop = asyncio.get_running_loop()
    prev = [0]

    if mon:
        await mon.start(key)

    def prog(cur, total):
        delta = cur - prev[0]
        prev[0] = cur
        if mon and delta > 0:
            # 回调可能来自线程，用线程安全方式投递到事件循环
            asyncio.run_coroutine_threadsafe(mon.add(key, delta), loop)

    async def pump():
        task = asyncio.create_task(
            client.download_media(msg, file=target, progress_callback=prog)
        )
        # T24：硬 wait_for(1800) 替换为基于循环时间的 deadline；断网期
        # 每轮轮询给 deadline 顺延一个轮询周期时长（等价暂停计时，
        # 断网前已用额度保留）。pump 控制轮询仍是 0.2s。
        deadline = loop.time() + SIMPLE_TIMEOUT_SECS
        net_down = False        # T24 断网等待态（simple 路径 part 用 -）
        net_rounds = 0
        while not task.done():
            tripped = _ctl_or_abort(ctl, state)
            if tripped:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                raise tripped
            if not _net_is_connected(client):
                # T24 断网：暂停总超时计时 + down/recover 事件（去重）
                if not net_down:
                    net_down = True
                    net_rounds = 0
                    _net_log(f"id={msg.id} part=- down=1")
                    if mon:
                        await mon.set_phase("downloading",
                                            "网络断开，等待自动恢复")
                # 每轮顺延一个轮询周期（=本轮等待时长）：等价暂停
                # 计时——断网期不消耗总额度，断网前已用时间保留。
                deadline += NET_POLL_SECS
                await _NET_SLEEP(NET_POLL_SECS)
                net_rounds += 1
                continue
            if net_down:
                net_down = False
                _net_log(f"id={msg.id} part=- recover=1 "
                         f"wait={net_rounds * NET_POLL_SECS:g}")
                if mon:
                    await mon.set_phase("downloading", "")
            if loop.time() >= deadline:
                # 超过总额度：维持原 TimeoutError 语义
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                raise TimeoutError(f"simple 下载超时（{SIMPLE_TIMEOUT_SECS}s）")
            await asyncio.sleep(0.2)
        task.result()

    await pump()


# ============ T22 重试分类公共件（FR-9 / AC-6，refresh/download 邻域） ============

class DeterministicDownloadError(Exception):
    """永久无效下载错误（消息已删除/无文档/评论组不可用）：
    重试无意义，download_one 收到即零 sleep 快失败（§2 契约）。"""


# 测试缝（照 _CLIENT_FACTORY 注释风格）：模块级可覆盖 sleep 句柄。
# 生产恒为 asyncio.sleep；测试 monkeypatch 为即退收集器（TR 不得
# 真实等 3+6+12 秒）。download_one 的瞬时退避与 FloodWait 无 mon
# 等待统一走此句柄；其他路径（pump 轮询 0.2s 等）不经句柄。
_RETRY_SLEEP = asyncio.sleep


# ---- T24 断网感知公共件（FR-11 / AC-9；本任务唯一新增公共面）----
# 测试缝（照 _RETRY_SLEEP 风格）：断网轮询专用可覆盖 sleep 句柄。
# 生产恒为 asyncio.sleep；测试 monkeypatch 为即退收集器（TR 不得
# 真实等 NET_POLL_SECS×N）。parallel 断网等待与 simple 断网期共用。
_NET_SLEEP = asyncio.sleep


# ---- T59 账号健康巡检公共件（FR-41 收口 / tasks.md Task 59）----
# 测试缝（照 _NET_SLEEP 风格）：巡检排程等待专用可覆盖 sleep 句柄。
# 生产恒为 asyncio.sleep；测试 monkeypatch 为假时钟推进器（TR 不得
# 真实等 60s 轮询/30-300s 退避/600s unhealthy 窗）。health loop 的
# 轮询等待一律走此句柄；单次预热 20s 限时仍走 asyncio.wait_for
# （不在本缝——探测本身受 20s 上限约束）。
_WARM_SLEEP = asyncio.sleep


# ---- T25 慢任务让路公共件（FR-12 / tasks.md Task 25，默认关）----
# 测试缝（照 _WARM_SLEEP 风格）：让路巡检专用可覆盖 sleep 句柄。
# 生产恒为 asyncio.sleep（巡检节拍 1.0s，随 run_download_session
# 生命周期启停）；测试 monkeypatch 为短真实 sleep（时间读数照常
# 走 time.monotonic() 真时，压缩的是巡检等待成本，禁真实等 20s 窗
# /60s 冷却；窗口与冷却秒数由 task.params 四键缩至秒级）。
_YIELD_SLEEP = asyncio.sleep


def _net_is_connected(client) -> bool:
    """T24 连通探测：调 client.is_connected()；任何异常 fail-open 返回
    True（探测失败不误判断网，保持旧行为——只有明确返回 False 才算
    断网）。普通函数（可 monkeypatch；parallel/simple 两路共用）。"""
    try:
        return bool(client.is_connected())
    except Exception:
        return True


class ShardResetExhausted(Exception):
    """T24 分片停滞重置超限：同一分片 3 次重置（共 4 个迭代器）仍无
    字节进账。带 msg/part 信息；经 classify_download_error 未知兜底落
    transient（不得加入确定性集合），升级为条目级 3/6/12 重试。"""

    def __init__(self, mid, part):
        self.mid = mid
        self.part = part
        super().__init__(f"分片 {part} 停滞重置超限（3/3）")


def _net_log(line):
    """T24 机读日志：[net] 前缀，一行一条，字段稳定可 grep；
    只含 msg id/分片号/秒数，禁带文件名/手机号/密钥/会话串。"""
    print(f"[net] {line}", flush=True)


def _shard_log(line):
    """T24 机读日志：[shard] 前缀，一行一条，只含 msg id/分片号。"""
    print(f"[shard] {line}", flush=True)


def _retry_log(line):
    """T22 机读日志：固定前缀 [retry]，一行一条，字段稳定可 grep；
    只含 msg id/异常类名/秒数，禁带手机号/密钥/会话串。"""
    print(f"[retry] {line}", flush=True)


def _wacct_log(line):
    """T59 机读日志：[wacct] 前缀（账号巡检专用），一行一条，字段稳定
    可 grep；只含账号 id/秒数/计数/脱敏手机号，禁带明文手机号/密钥/
    会话串。探测结果行 event 词表钉死：probe|recover|flood|backoff|
    unhealthy|invalid；生命周期行（loop=exit，§2.1 循环退出一行）不带
    event（六词均为探测结果语义）。"""
    print(f"[wacct] {line}", flush=True)


def _yield_log(line):
    """T25 机读日志：[yield] 前缀（慢任务让路专用），一行一条，字段
    稳定可 grep。事件行键序钉死六键 event/item/speed_bps/win/queued/
    count（值均 int；win=与本事件相关的持续秒数：pause=低速已持续、
    resume=暂停已持续、skip_trigger=低速已累计、exhausted=低速已
    持续）；event 词表钉死 pause|resume|skip_trigger|exhausted；
    生命周期行 loop=exit（循环退出唯一行，关态零行=管理器零启动）。
    只含消息 id/秒数/计数，禁带文件名/路径/手机号/密钥/会话串。"""
    print(f"[yield] {line}", flush=True)


def classify_download_error(exc) -> str:
    """T22 五档分类：control | flood | reference | deterministic | transient。
    - control（PauseFile/RemoveFile/SkipFile/AccountDenied/CancelledError，
      名字+isinstance 双保险）；AccountDenied/CancelledError 为
      BaseException 本就穿出 except Exception——分类器只归档不截留。
    - flood（FloodWaitError）/ reference（FileReferenceExpiredError）：
      名字判定优先，兜底真类 isinstance。
    - deterministic：DeterministicDownloadError，或名字命中
      DETERMINISTIC_ERROR_NAMES（三件均已在 telethon 1.45.0 验证存在，
      见常量区注释；T22/1.45+ 若更名按报告铁则剔除并记录）。
    - 其余（TimeoutError/ConnectionError/OSError/asyncio.TimeoutError/
      5xx/未知）一律 transient（保守，维持现行重试行为）。"""
    name = type(exc).__name__
    if (name in CONTROL_ERROR_NAMES
            or isinstance(exc, (PauseFile, RemoveFile, SkipFile,
                                AccountDenied))
            or isinstance(exc, asyncio.CancelledError)):
        return "control"
    if name == "FloodWaitError" or isinstance(exc, FloodWaitError):
        return "flood"
    if (name == "FileReferenceExpiredError"
            or isinstance(exc, FileReferenceExpiredError)):
        return "reference"
    if isinstance(exc, DeterministicDownloadError):
        return "deterministic"
    if name in DETERMINISTIC_ERROR_NAMES:
        return "deterministic"
    return "transient"


def _valid_retries(v):
    """retries 合法值：0..8 整数（bool 排除；float/str/越界均非法）。"""
    return (isinstance(v, int) and not isinstance(v, bool)
            and RETRIES_MIN <= v <= RETRIES_MAX)


def _exec_cap(tries) -> int:
    """retries 配置值 → 总尝试次数上限（§2/TR-22.5 联合锁定）：
    0→1（不重试即仅 1 次尝试）；1→2（允许一次退避重试）；
    >=2→retries 自身（2→2，默认 3→3，8→8）。由任务书 TR-22.5
    （0→1 次尝试、1→2 次尝试）与 TR-22.2（默认 3 次执行、5 次执行）、
    TR-22.5（cfg=2→2 次尝试）四点共同钉死，唯一自洽公式。"""
    if tries <= 0:
        return 1
    return max(2, tries)


def resolve_retries(task, cfg=None) -> int:
    """T22 有效重试次数（总尝试次数上限；0=仅 1 次尝试由 download_one
    兑现）：params.retries 合法→其值；否则 cfg['retries']（tg_config
    可选键，仅读取无写入口）合法→其值；否则模块常量 RETRIES(=3)。
    非法值一律回落不抛（核心侧读到无效值忽略回默认——§2）。"""
    params = task.get("params") if isinstance(task, dict) else None
    if isinstance(params, dict):
        v = params.get("retries")
        if _valid_retries(v):
            return v
    if isinstance(cfg, dict):
        v2 = cfg.get("retries")
        if _valid_retries(v2):
            return v2
    return RETRIES


async def refresh_record(client, sess, item):
    """file_reference 过期刷新：按条目来源重取该条消息并**原地**更新
    item/rec 的 msg/doc/size（sess.records 与队列中的是同一对象引用），
    任务清单持久条目 size 同步（make_task_item 字段集不变，无 phase 键）。
    T22：消息已删除/无文档/评论组不可用按确定性错误抛
    DeterministicDownloadError（原 RuntimeError，消息文本保留）。返回新 msg。"""
    task = sess.task
    src = item.get("src", SRC_CHANNEL)
    key = (src, item["msg"].id)
    ent = sess.entities.get(src)
    if ent is None:
        chan = await client.get_entity(task["channel"])
        if src == SRC_CHANNEL:
            ent = chan
        else:
            ent = await get_discuss_entity(client, chan)
            if ent is None:
                # T22：评论组不可用=永久无效（重试无意义）
                raise DeterministicDownloadError("评论组不可用，无法刷新引用")
        sess.entities[src] = ent
    m = await client.get_messages(ent, ids=item["msg"].id)
    if isinstance(m, list):
        m = m[0] if m else None
    if m is None or not getattr(m, "document", None):
        # T22：消息已删除/无文档=永久无效（重试无意义）
        raise DeterministicDownloadError(f"#{item['msg'].id} 消息已删除或无文档")
    new_size = getattr(m.document, "size", 0) or item["size"]
    item["msg"] = m
    item["doc"] = m.document
    item["size"] = new_size
    rec = sess.records.get(key)
    if rec is not None and rec is not item:
        rec["msg"] = m
        rec["doc"] = m.document
        rec["size"] = new_size
    mon = getattr(sess, "mon", None)
    if mon is not None:
        await mon.fill(item)
    ti = find_item(task, key)
    if ti is not None and new_size and ti.get("size") != new_size:
        ti["size"] = new_size
        save_task(task)
    return m


async def download_one(client, item, target, ctl, state=None):
    """返回 ok / skipped / skip-exists / paused / removed / ('fail', reason)

    T22 重试分类（§2 行为契约，入口/返回词表与现状一致）：
      - flood：严格等待（UI 走 mon.enter_flood；无 mon 时句柄 sleep
        seconds+1），不消耗任何重试预算，等待后原 attempt 重新执行；
      - reference：独立刷新预算（单条上限 FILE_REFRESH_MAX=3），成功
        刷新后重下且不占瞬时次数；第 4 次过期=确定性失败；刷新动作
        异常按分类落入 deterministic（立即失败）/transient（占一次
        瞬时预算并按退避）口径；
      - deterministic：立即 fail（零 sleep）；
      - transient：RETRY_BACKOFF 3/6/12 封顶 12s，总尝试次数=有效
        retries（默认 RETRIES=3；params.retries 0..8 任务级可配，
        cfg['retries'] 全局可选），上限即 failed（不自动重入队）。
    机读日志前缀 [retry]（一行一条，仅 msg id/异常类名/秒数）。"""
    size = item["size"]
    mon = state["monitor"] if state else None
    key = (item.get("src", SRC_CHANNEL), item["msg"].id)
    mid = item["msg"].id
    sess = (state or {}).get("session")
    cfg = (state or {}).get("cfg") if state else None

    if os.path.exists(target):
        if size and os.path.getsize(target) == size:
            return "skip-exists"
        try:
            os.remove(target)
        except OSError:
            pass

    def _task_for_retries():
        return getattr(sess, "task", None) if sess is not None else None

    # T22：两类独立计数（flood/成功刷新不占任何预算）；有界 for——
    # 界 = 执行上限 + 刷新上限 + flood 安全阀（§2：任何路径不得无限重试）。
    transient_attempt = 0
    ref_refresh = 0
    base_tries = _exec_cap(resolve_retries(_task_for_retries(), cfg))
    for _round in range(base_tries + FILE_REFRESH_MAX + RETRY_FLOOD_SPARE):
        # 每 attempt 头重取有效 retries（正在下载的条目改
        # params.retries/cfg 后下次重试即生效——edit_task live 同步依
        # 赖此行为）。0=不重试即仅 1 次尝试在此兑现（见 _exec_cap）。
        tries = resolve_retries(_task_for_retries(), cfg)
        exec_cap = _exec_cap(tries)
        # 新一轮尝试开始即清掉上一轮的“限速等待/Ns 后重试”提示：
        # 否则本次成功后旧报错仍残留在卡片上。
        if mon:
            await mon.error(key, "")
        try:
            if size > 4 * 1024 * 1024:
                await parallel_download(client, item, target, ctl, state)
            else:
                await simple_download(client, item, target, ctl, state)
            # 无损把 moov 移到文件开头，兼容所有播放器（不重编码）
            try:
                faststart_mp4(target)
            except Exception:
                pass
            return "ok"
        except PauseFile:
            # simple_download 直写目标路径：清掉不完整成品，避免被误判完成，
            # parallel 路径只写 .dl，此处 target 不会存在
            if os.path.exists(target) and (
                    not size or os.path.getsize(target) != size):
                try:
                    os.remove(target)
                except OSError:
                    pass
            return "paused"
        except RemoveFile:
            # 移除：清理 .dl 临时文件与标记，条目随后从清单删除（成品保留）
            for p in glob.glob(target + ".dl*"):
                try:
                    os.remove(p)
                except OSError:
                    pass
            return "removed"
        except SkipFile:
            # 用户主动跳过：清理 .dl 临时文件与完成标记，不保留
            for p in glob.glob(target + ".dl*"):
                try:
                    os.remove(p)
                except OSError:
                    pass
            return "skipped"
        except FileReferenceExpiredError:
            # 文件引用过期（T49 刷新 + T22 独立预算）：成功刷新→重下
            # 不占任何预算；上限后→确定性失败（reason 含刷新上限）；
            # 刷新异常按分类落入 deterministic（立即失败）/transient
            # （占一次瞬时预算并退避）口径（消息文本透传）。
            if ref_refresh >= FILE_REFRESH_MAX:
                _retry_log(f"giveup id={mid} reason=refresh_cap")
                reason = (f"#{mid} 文件引用刷新已达上限"
                          f"（{FILE_REFRESH_MAX} 次）——刷新上限")
                if mon:
                    await mon.error(key, reason)
                return "fail", reason
            _retry_log(f"kind=reference id={mid} "
                       f"refresh={ref_refresh + 1}/{FILE_REFRESH_MAX}")
            if mon:
                await mon.set_phase("refreshing", f"#{mid}")
            try:
                if sess is None:
                    raise RuntimeError("会话缺失，无法刷新文件引用")
                await refresh_record(client, sess, item)
            except Exception as rexc:
                if mon:
                    await mon.set_phase(
                        "downloading",
                        f"引用刷新失败 {type(rexc).__name__}")
                rkind = classify_download_error(rexc)
                if rkind == "deterministic":
                    _retry_log(f"kind=deterministic id={mid} fail_fast=1 "
                               f"exc={type(rexc).__name__}")
                    _retry_log(f"giveup id={mid} reason=deterministic")
                    reason = f"{type(rexc).__name__}: {str(rexc)[:100]}"
                    if mon:
                        await mon.error(key, reason)
                    return "fail", reason
                # transient（及同名怪例→旧通用退化口径）：
                # 第 k=ta+1 个瞬时错误；k==exec_cap 即本尝试为第
                # exec_cap 次（总尝试上限）的失败——直接 giveup。
                if transient_attempt + 1 >= exec_cap:
                    _retry_log(f"giveup id={mid} reason=exhausted")
                    reason = f"{type(rexc).__name__}: {str(rexc)[:100]}"
                    if mon:
                        await mon.error(key, reason)
                    return "fail", reason
                wait = (RETRY_BACKOFF[transient_attempt]
                        if transient_attempt < len(RETRY_BACKOFF)
                        else RETRY_BACKOFF_CAP)
                transient_attempt += 1
                _retry_log(f"kind=transient id={mid} "
                           f"attempt={transient_attempt}/{tries} "
                           f"wait={wait:g} exc={type(rexc).__name__}")
                if mon:
                    await mon.error(
                        key, f"{type(rexc).__name__} → {wait:g}s 后重试"
                             f"（{transient_attempt}/{tries}）")
                await _RETRY_SLEEP(wait)
                continue
            ref_refresh += 1
            if mon:
                await mon.set_phase("downloading", "引用已刷新")
            # 刷新成功：重下不占任何预算
        except FloodWaitError as e:
            # T22：等待不消耗任何重试预算，等待后原 attempt 重新执行。
            _retry_log(f"kind=flood id={mid} "
                       f"seconds={getattr(e, 'seconds', '?')}")
            if mon:
                await mon.error(key, f"限速等待 {e.seconds}s")
            if mon:
                # T49 缺口 C 收口：等待期探测命中的控制异常（P/R/S）在
                # enter_flood 内上抛，不会落进本 try 的兄弟 except——
                # 必须在此按顶层同语义分类，否则会被 worker 误判为致命
                # 错误（限速等待中按键会把整个任务打成 error）。
                # CancelledError（会话 abort）不在此捕获，穿出由 schedule
                # 收场（现行取消语义）。
                try:
                    await mon.enter_flood(e.seconds, "downloading")
                except PauseFile:
                    if os.path.exists(target) and (
                            not size or os.path.getsize(target) != size):
                        try:
                            os.remove(target)
                        except OSError:
                            pass
                    return "paused"
                except (RemoveFile, SkipFile) as ce:
                    for p in glob.glob(target + ".dl*"):
                        try:
                            os.remove(p)
                        except OSError:
                            pass
                    return "removed" if isinstance(ce, RemoveFile) \
                        else "skipped"
                await mon.leave_flood()
            else:
                await _RETRY_SLEEP(e.seconds + 1)
        except Exception as e:
            # T22：先分类。deterministic→零 sleep 快失败；control 真类
            # （PauseFile 等）有专属 except 前置、BaseException 天然穿透——
            # 到不了此分支；同名怪例（名字判定为 control/flood 但类型
            # 不匹配者）按现状旧通用语义退避（分类器不截留，TR-22.8）。
            kind = classify_download_error(e)
            if kind == "deterministic":
                _retry_log(f"kind=deterministic id={mid} fail_fast=1 "
                           f"exc={type(e).__name__}")
                _retry_log(f"giveup id={mid} reason=deterministic")
                reason = f"{type(e).__name__}: {str(e)[:100]}"
                if mon:
                    await mon.error(key, reason)
                return "fail", reason
            # transient：第 k=ta+1 个瞬时错误；k==exec_cap 即本尝试为
            # 第 exec_cap 次（总尝试上限）的失败——到达上限即 fail。
            if transient_attempt + 1 >= exec_cap:
                _retry_log(f"giveup id={mid} reason=exhausted")
                reason = f"{type(e).__name__}: {str(e)[:100]}"
                if mon:
                    await mon.error(key, reason)
                return "fail", reason
            wait = (RETRY_BACKOFF[transient_attempt]
                    if transient_attempt < len(RETRY_BACKOFF)
                    else RETRY_BACKOFF_CAP)
            transient_attempt += 1
            _retry_log(f"kind=transient id={mid} "
                       f"attempt={transient_attempt}/{tries} "
                       f"wait={wait:g} exc={type(e).__name__}")
            if mon:
                await mon.error(
                    key, f"{type(e).__name__} → {wait:g}s 后重试"
                         f"（{transient_attempt}/{tries}）")
            await _RETRY_SLEEP(wait)
    return "fail", "重试次数用完"


# ---------------- 任务清单持久化 ----------------

# 视频状态：pending 待下载 / done 新下载完成 / exists 此前已存在
#           failed 失败 / skipped 用户手动跳过
DONE_STATUSES = ("done", "exists")
STATUS_MARK = {"done": "✓", "exists": "✓", "pending": "…",
               "failed": "✗", "skipped": "→"}


def new_task_id():
    return time.strftime("%Y%m%d_%H%M%S")


def task_file_path(task):
    return TASK_ROOT / f"{task['id']}.json"


def save_task(task, *, force=False):
    """保存任务清单（T21 防抖合卷 + .bak 轮转）。

    force=False：默认合卷。若该任务正处于合并窗口（冷启动后 1s 内），本次
    只把最新值入队合并、不阻塞调用方、返回 None；若窗口已关（冷启动），
    本次同步落盘最新值（durable）后返回 None，保证“冷启动单次写”调用方
    视角立即可见（兼容既有 保存→load 同进程读）。latest 值胜出；窗口由
    周期兜底，自最近一次冷启动起 ≤ PERSIST_DEBOUNCE_SECS 必有新写。
    force=True：走同一有序原子落盘路径并完成 .bak 轮转后同步返回；成功
    返回 True、失败返回 False（不抛异常）。

    并发一致性：入队即 deepcopy 一致快照（锁内），序列化期间调用方改原
    dict 无影响；force 与合卷走同一通道、按任务 id 只留最新值。
    """
    if force:
        return _persist_force(task)
    return _persist_coalesce(task)


# ===================== T21 合卷落盘器（模块级） =====================
# 单 daemon writer 线程 + 每任务锁；按 task id 维护"最新待写 dict"；
# 最新值胜出；线程在首次脏写时懒启动。全部写盘异常在持久化层捕获并
# 输出机读日志（前缀 [persist]），绝不向 worker 调用栈抛异常。

import copy as _copy
import atexit as _atexit

_PERSIST_LOCK = threading.Lock()        # 保护下方全部 _PERSIST_* 状态
_PERSIST_PENDING = {}                  # task_id -> (snapshot, seq, force)
_PERSIST_WINDOW = {}                   # task_id -> 合并窗口截止（monotonic）
_PERSIST_WRITTEN = {}                  # task_id -> 已写 seq（同步等待用）
_PERSIST_WRITTEN_OK = {}               # task_id -> 该次写是否成功
_PERSIST_SEQ = 0                       # 单调递增请求序号（newest-only）
_PERSIST_REPLACE_COUNT = 0             # 机读：实际 os.replace 成功次数（测试）
_PERSIST_THREAD = None                 # daemon writer 线程
_PERSIST_STOP = threading.Event()
_PERSIST_COND = threading.Condition(_PERSIST_LOCK)
_PERSIST_EXITING = False               # 解释器退出中：抑制 [persist] 打印


def _persist_log(msg):
    if _PERSIST_EXITING:
        return
    print(f"[persist] {msg}", flush=True)


def _persist_snapshot(task):
    """锁外 deepcopy 一致快照：worker 序列化期间调用方改 dict 无影响。"""
    return _copy.deepcopy(task)


def _persist_ensure_writer():
    global _PERSIST_THREAD
    with _PERSIST_LOCK:
        if _PERSIST_THREAD is None or not _PERSIST_THREAD.is_alive():
            _PERSIST_STOP.clear()
            _PERSIST_THREAD = threading.Thread(
                target=_persist_writer_main, name="t21-persist-writer",
                daemon=True)
            _PERSIST_THREAD.start()


def _persist_coalesce(task):
    """非 force 默认合卷。冷启动（窗口已关）本次同步 durable 落盘；
    窗口内只合并最新值不阻塞。返回 None。"""
    global _PERSIST_SEQ
    tid = task["id"]
    snap = _persist_snapshot(task)
    _persist_ensure_writer()
    with _PERSIST_LOCK:
        _PERSIST_SEQ += 1
        seq = _PERSIST_SEQ
        now = time.monotonic()
        if now < _PERSIST_WINDOW.get(tid, 0):
            # 窗口内：只更新最新值，不阻塞调用方
            _PERSIST_PENDING[tid] = (snap, seq, False)
            _PERSIST_COND.notify_all()
            return None
        # 冷启动：开本轮窗口，本次同步写（durable）
        _PERSIST_WINDOW[tid] = now + PERSIST_DEBOUNCE_SECS
        _PERSIST_PENDING[tid] = (snap, seq, True)
        _PERSIST_COND.notify_all()
    _persist_wait_seq(tid, seq)
    return None


def _persist_force(task):
    """force 同步落盘：走同一通道并等待该 seq 真正写出。"""
    global _PERSIST_SEQ
    tid = task["id"]
    snap = _persist_snapshot(task)
    _persist_ensure_writer()
    with _PERSIST_LOCK:
        _PERSIST_SEQ += 1
        seq = _PERSIST_SEQ
        _PERSIST_PENDING[tid] = (snap, seq, True)
        _PERSIST_WINDOW[tid] = time.monotonic()   # 重开窗口：下一次视为冷
        _PERSIST_COND.notify_all()
    return _persist_wait_seq(tid, seq)


def _persist_wait_seq(tid, seq):
    """有界等待该 seq 的写入。成功返回 True、失败/超时返回 False。"""
    with _PERSIST_LOCK:
        deadline = time.monotonic() + PERSIST_MAX_WAIT_SECS * 2 + 2.0
        while True:
            if _PERSIST_WRITTEN.get(tid, -1) >= seq:
                return bool(_PERSIST_WRITTEN_OK.get(tid, False))
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            _PERSIST_COND.wait(remaining)


def _persist_writer_main():
    while True:
        with _PERSIST_COND:
            while True:
                now = time.monotonic()
                ready = [tid for tid, (_snap, _sq, force)
                         in _PERSIST_PENDING.items()
                         if force or now >= _PERSIST_WINDOW.get(tid, 0)]
                if ready or _PERSIST_STOP.is_set():
                    break
                _PERSIST_COND.wait(0.05)
            if _PERSIST_STOP.is_set() and not _PERSIST_PENDING:
                break
            if not ready:
                continue
            batch = [(tid, _PERSIST_PENDING.pop(tid)) for tid in ready]
        # 锁外逐个写；失败保留最新快照（以非 force 重新入队、重开窗口）
        for tid, (snap, seq, force) in batch:
            ok = _persist_write_one(snap, force)
            with _PERSIST_LOCK:
                _PERSIST_WRITTEN[tid] = seq
                _PERSIST_WRITTEN_OK[tid] = ok
                if not ok:
                    cur = _PERSIST_PENDING.get(tid)
                    if cur is None or cur[1] <= seq:
                        _PERSIST_PENDING[tid] = (snap, seq, False)
                        _PERSIST_WINDOW[tid] = (time.monotonic()
                                                 + PERSIST_DEBOUNCE_SECS)
                _PERSIST_COND.notify_all()


def _bak_rotate(path, tid):
    """best-effort 3 份 .bak 轮转：replace 成功后把上一版正式内容滚入
    bak1（copy 而非 move，绝不删/截断当前正式文件），bak1→bak2→bak3，
    删 bak4。轮转失败只记机读日志、不影响正式写成功。"""
    p = str(path)
    b1, b2, b3, b4 = p + ".bak1", p + ".bak2", p + ".bak3", p + ".bak4"
    if os.path.exists(b3):
        os.replace(b3, b4)
    if os.path.exists(b2):
        os.replace(b2, b3)
    if os.path.exists(b1):
        os.replace(b1, b2)
    if os.path.exists(b4):
        os.remove(b4)
    if os.path.exists(path):
        shutil.copyfile(path, b1)


def _persist_write_one(task, force):
    """单任务原子落盘：json.dumps 预检 → 写 .tmp(flush+fsync) →
    os.replace 12×25ms → replace 成功后再轮转备份。返回是否成功。"""
    path = task_file_path(task)
    tid = task["id"]
    try:
        data = json.dumps(task, ensure_ascii=False, indent=2)
    except (TypeError, ValueError, RecursionError) as e:
        # 不可序列化：丢弃该次脏值，正式与 bak 一律不动，不阻塞后续好写
        _persist_log(f"serialize_error id={tid} "
                     f"force={1 if force else 0} "
                     f"exc={type(e).__name__} msg={e}")
        return False
    try:
        TASK_ROOT.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        _persist_log(f"write_error id={tid} force={1 if force else 0} "
                     f"attempt=0 exc={type(e).__name__} msg={e}")
        return False
    tmp = str(path) + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
    except OSError as e:
        _persist_log(f"write_error id={tid} force={1 if force else 0} "
                     f"attempt=0 exc={type(e).__name__} msg={e}")
        return False
    ok = False
    # 先轮转（把 replace 前正式内容滚入 bak1，bak1→bak2→bak3，绝不碰正式）
    try:
        _bak_rotate(path, tid)
    except OSError as e:
        _persist_log(f"rotate_error id={tid} exc={type(e).__name__} "
                     f"msg={e}")
    for attempt in range(12):
        try:
            os.replace(tmp, path)
            ok = True
            break
        except PermissionError:
            if attempt == 11:
                _persist_log(
                    f"write_error id={tid} force={1 if force else 0} "
                    f"attempt={attempt + 1} exc=PermissionError "
                    f"msg=replace 12 次仍被占用")
                return False
            time.sleep(0.025)
        except OSError as e:
            _persist_log(f"write_error id={tid} force={1 if force else 0} "
                         f"attempt={attempt + 1} exc={type(e).__name__} "
                         f"msg={e}")
            return False
    global _PERSIST_REPLACE_COUNT
    _PERSIST_REPLACE_COUNT += 1
    return ok


def reset_persistence():
    """测试复位钩子：停 writer、清待写、join、复位状态（生产代码不调用）。"""
    global _PERSIST_THREAD, _PERSIST_SEQ, _PERSIST_REPLACE_COUNT
    _PERSIST_STOP.set()
    with _PERSIST_LOCK:
        _PERSIST_COND.notify_all()
    t = _PERSIST_THREAD
    if t is not None:
        t.join(timeout=PERSIST_MAX_WAIT_SECS + 1.0)
    with _PERSIST_LOCK:
        _PERSIST_PENDING.clear()
        _PERSIST_WINDOW.clear()
        _PERSIST_WRITTEN.clear()
        _PERSIST_WRITTEN_OK.clear()
        _PERSIST_SEQ = 0
        _PERSIST_REPLACE_COUNT = 0
    _PERSIST_STOP.clear()


def _persist_flush_on_exit():
    """解释器退出有界 flush：先停 daemon writer（防 finalize 期其继续
    打印/写盘），再兜底把剩余待写同步写掉（退出期静默，出错即丢）。"""
    global _PERSIST_EXITING
    try:
        _PERSIST_EXITING = True
        _PERSIST_STOP.set()
        with _PERSIST_LOCK:
            _PERSIST_COND.notify_all()
        if _PERSIST_THREAD is not None:
            _PERSIST_THREAD.join(timeout=PERSIST_MAX_WAIT_SECS + 1.0)
        if _PERSIST_PENDING:
            with _PERSIST_LOCK:
                batch = list(_PERSIST_PENDING.items())
                _PERSIST_PENDING.clear()
            for tid, (snap, seq, force) in batch:
                try:
                    _persist_write_one(snap, force)
                except Exception:
                    pass
    except Exception:
        pass


_atexit.register(_persist_flush_on_exit)


def load_tasks():
    """读取全部任务清单，按创建时间新→旧排序。坏文件自动跳过。"""
    if not TASK_ROOT.exists():
        return []
    out = []
    for p in TASK_ROOT.glob("*.json"):
        try:
            t = json.loads(p.read_text(encoding="utf-8"))
            if t.get("id") and isinstance(t.get("items"), list):
                out.append(t)
        except Exception:
            pass
    out.sort(key=lambda t: t.get("id", ""), reverse=True)
    return out


def load_task(task_id):
    """按 id 直读单个任务清单（T49 热路径）：语义等同
    load_tasks() 后按 id 取那一条，但避开"每点一次继续就全目录解析
    所有任务 JSON"——300 条任务单文件约 66KB，Windows Defender
    实时扫描下全目录扫描实测 p50 117ms/p90 140ms，会把 preparing
    占位推出 ≤200ms 预算。坏文件/不存在返回 None（与 load_tasks
    跳过坏文件一致）；basename 约束防 id 穿越目录。"""
    if not task_id or pathlib.Path(task_id).name != str(task_id):
        return None
    path = TASK_ROOT / f"{task_id}.json"
    try:
        t = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if t.get("id") and isinstance(t.get("items"), list):
        return t
    return None


def delete_task(task):
    try:
        os.remove(task_file_path(task))
    except OSError:
        pass


# T23 启动对账（FR-10/AC-7）：对账允许自动变更的状态集合。
# downloading 仅是会话运行相位、正常路径从不持久化——出现在清单里即
# 崩溃残留（旧版/脏 JSON），无活动会话（由调用时机保证：会话启动前/
# 菜单入口均非运行期）一律回 pending。
RECONCILE_ACTIVE = frozenset(("downloading",))
# 反向补登：成品在且大小匹配时磁盘事实优先，未终态条目归 exists。
# skipped 是用户意图终态，两个状态变更集合都不含它（判定表 3/7 行）。
RECONCILE_PROMOTABLE = frozenset(("pending", "failed", "downloading"))


def _reconcile_artifacts(folder, ti, *, finished_ok):
    """T23 工件修复：只碰本条目严格命名的分片工件——work=<name>.dl 与
    <work>.part<N>.done 标记（任何删除前断言路径仍在 folder 内，防
    name 穿越；成品文件永不删，错误大小的 work 也保留——parallel_
    download 开工时自行 truncate）。

    - finished_ok（成品在且大小匹配）：work 与标记都属收尾 replace 后
      清理窗口的残留，全部删除；
    - 无匹配成品：size==0 无分片方案不动；work 在且大小==size 时仅删
      越界/畸形标记（idx 解析不出或 idx>=nparts；界内零字节标记信任
      ——Telegram 无 piece hash，FR-10 不做内容校验）；work 缺失或
      大小不符时删全部标记（防零字节洞）。
    返回 (purged, trimmed, kept, work_removed)：purged/trimmed 为删除
    的标记数，kept 为界内保留数（无成品分支），work_removed 0/1。"""
    folder = pathlib.Path(folder)
    name = ti["name"]
    size = ti.get("size") or 0
    work = folder / (name + ".dl")
    prefix = name + ".dl.part"
    found = sorted(folder.glob(name + ".dl.part*.done"))
    purged = trimmed = kept = work_removed = 0

    def _safe_remove(p):
        assert p.parent == folder, f"对账工件越界: {p}"
        try:
            os.remove(p)
            return True
        except OSError:
            return False

    if finished_ok:
        for p in found:
            if _safe_remove(p):
                purged += 1
        if work.exists() and _safe_remove(work):
            work_removed = 1
        return purged, trimmed, kept, work_removed
    if size == 0:
        return purged, trimmed, kept, work_removed
    nparts = -(-size // PART)
    work_ok = work.exists() and work.stat().st_size == size
    for p in found:
        token = p.name[len(prefix):-len(".done")]
        in_range = token.isdigit() and int(token) < nparts
        if work_ok:
            if in_range:
                kept += 1
            elif _safe_remove(p):
                trimmed += 1
        elif _safe_remove(p):
            purged += 1
    return purged, trimmed, kept, work_removed


def reconcile_task(task):
    """启动对账（T23 重写，FR-10/AC-7）：按磁盘事实全量重建——成品/
    .dl/.partN.done/大小四要素核对；崩溃活动态（downloading 残留）回
    pending；反向补登（成品在且大小匹配的未终态 →exists）；残留分片
    工件修复（孤儿 purge / 越界畸形 trim / 成品匹配的 stale 清理）。
    纯磁盘、零网络、幂等（第二遍 changed=0 零机读行）；skipped 是用户
    意图终态永不自动改。机读日志前缀 [reconcile]（仅任务 id/msg id/
    状态名/字节数，禁文件名与敏感信息）。返回计数 dict：
    {"changed": 变更条目数, "reset": 重置 pending 条数,
     "promote": 补登 exists 条数, "markers": purge+trim 删除标记数,
     "stale_work": 成品匹配清理的 work+markers 文件数}。"""
    folder = pathlib.Path(task["folder"])
    tid = task.get("id", "?")
    stats = {"changed": 0, "reset": 0, "promote": 0,
             "markers": 0, "stale_work": 0}
    touched = set()

    def _ev(kind, ti, from_st=None, to_st=None, detail=None):
        line = f"[reconcile] task={tid} item={ti.get('msg')} kind={kind}"
        if from_st is not None:
            line += f" from={from_st}"
        if to_st is not None:
            line += f" to={to_st}"
        if detail is not None:
            line += f" detail={detail}"
        print(line)
        touched.add(ti.get("msg"))

    for ti in task["items"]:
        target = folder / ti["name"]
        size = ti.get("size") or 0
        try:
            finished = target.exists() and (
                size == 0 or target.stat().st_size == size)
        except OSError:
            finished = False
        st = ti["status"]
        # ---- 状态判定（判定表；skipped 永不自动改） ----
        if finished:
            if st in RECONCILE_PROMOTABLE:       # done/exists/skipped 不动
                ti["status"] = "exists"
                ti.pop("error", None)
                _ev("promote_exists", ti, from_st=st, to_st="exists")
                stats["promote"] += 1
        elif target.exists():                    # 成品在但大小不符
            if st not in ("pending", "skipped"):
                try:
                    got = target.stat().st_size
                except OSError:
                    got = -1
                ti["status"] = "pending"
                ti.pop("error", None)
                _ev("reset_size", ti, from_st=st, to_st="pending",
                    detail=f"{got}/{size}")
                stats["reset"] += 1
        else:                                    # 成品缺失
            if st in DONE_STATUSES:
                ti["status"] = "pending"
                ti.pop("error", None)
                _ev("reset_missing", ti, from_st=st, to_st="pending")
                stats["reset"] += 1
            elif st in RECONCILE_ACTIVE:         # 崩溃活动态回 pending
                ti["status"] = "pending"
                ti.pop("error", None)
                _ev("active_reset", ti, from_st=st, to_st="pending")
                stats["reset"] += 1
        # ---- 工件修复（与 status 无关，凡无匹配成品时执行；
        #      成品匹配时清 replace 后清理窗口残留） ----
        try:
            purged, trimmed, kept, work_removed = _reconcile_artifacts(
                folder, ti, finished_ok=finished)
        except OSError:
            purged = trimmed = kept = work_removed = 0
        if finished:
            if purged or work_removed:
                _ev("stale_work", ti, detail=f"{purged}/{work_removed}")
                stats["stale_work"] += purged + work_removed
        else:
            if purged:
                _ev("markers_purge", ti, detail=f"{purged}/{-(-size // PART)}")
                stats["markers"] += purged
            if trimmed:
                _ev("markers_trim", ti, detail=f"{trimmed}/{kept}")
                stats["markers"] += trimmed

    stats["changed"] = len(touched)
    if touched:
        print(f"[reconcile] task={tid} changed={stats['changed']} "
              f"reset={stats['reset']} promote={stats['promote']} "
              f"markers={stats['markers']} "
              f"stale_work={stats['stale_work']}")
    return stats


def task_counts(task):
    c = {"total": len(task["items"]), "done": 0, "exists": 0,
         "pending": 0, "failed": 0, "skipped": 0}
    for ti in task["items"]:
        st = ti["status"]
        c[st] = c.get(st, 0) + 1
    c["finished"] = c["done"] + c["exists"]
    c["unfinished"] = c["pending"] + c["failed"]
    return c


def doc_duration(doc):
    """从文档属性提取视频时长（秒），无则 0。"""
    for a in getattr(doc, "attributes", None) or []:
        if type(a).__name__ == "DocumentAttributeVideo":
            return int(getattr(a, "duration", 0) or 0)
    return 0


def make_task_item(record):
    m = record["msg"]
    return {
        "msg": m.id, "size": record["size"], "name": record["name"],
        "date": f"{m.date:%Y-%m-%d}", "status": "pending",
        "src": record.get("src", SRC_CHANNEL),
        "text": record.get("text", "") or "",
        "duration": doc_duration(record.get("doc")),
    }


def item_key(ti):
    return (ti.get("src", SRC_CHANNEL), ti["msg"])


# ---------------- 大任务自动拆分 ----------------
SPLIT_TARGET_GB = 200      # 拆分后每片目标体积
SPLIT_TRIGGER_GB = 300     # 触发拆分的体积阈值
SPLIT_TRIGGER_COUNT = 300  # 与体积阈值同时满足的条数条件
SPLIT_MAX_ITEMS = 999      # 单任务条数硬上限（超过必拆）


def task_should_split(items):
    """是否需要自动拆分：条数>999 必拆；或条数>300 且总体积>300GB。
    只有一两个条目的任务永不拆（单个超大视频保持完整）。"""
    n = len(items)
    if n > SPLIT_MAX_ITEMS:
        return True
    if n > SPLIT_TRIGGER_COUNT and \
            sum(i.get("size", 0) for i in items) > SPLIT_TRIGGER_GB * 2**30:
        return True
    return False


def chunk_task_items(items):
    """按给定顺序贪心打包：每片不超过 200GB 且不超过 999 条。
    体积与条数两条上限同时生效——按 200GB 切完仍超 999 条的会继续切。
    单个视频本身超过 200GB 也不会被切开（该片即该视频）。"""
    target = SPLIT_TARGET_GB * 2**30
    chunks, cur, cur_sz = [], [], 0
    for it in items:
        size = it.get("size", 0)
        if cur and (cur_sz + size > target or len(cur) >= SPLIT_MAX_ITEMS):
            chunks.append(cur)
            cur, cur_sz = [], 0
        cur.append(it)
        cur_sz += size
    if cur:
        chunks.append(cur)
    return chunks


def build_task_dicts(channel, title, params, folder, items, stamp=None):
    """生成一个或按规则拆分后的多个任务字典；调用方负责 save_task。
    items 为已构造好的任务条目（make_task_item 的产物）。"""
    stamp = stamp or new_task_id()
    chunks = chunk_task_items(items) if task_should_split(items) else [items]
    total = len(chunks)
    out = []
    for n, chunk in enumerate(chunks, 1):
        out.append({
            "id": stamp if total == 1 else f"{stamp}_{n}",
            "created": time.strftime("%Y-%m-%d %H:%M:%S"),
            "channel": channel,
            "channel_title": title if total == 1 else f"{title}（{n}/{total}）",
            "params": dict(params),
            "folder": folder,
            "items": chunk,
        })
    return out


def find_item(task, key):
    src, mid = key
    for ti in task["items"]:
        if ti["msg"] == mid and ti.get("src", SRC_CHANNEL) == src:
            return ti
    return None


def rename_task(task, title):
    title = (title or "").strip()
    if title:
        task["channel_title"] = title[:80]
        save_task(task, force=True)


def set_parallel(task, n):
    n = max(1, min(int(n), 8))
    task.setdefault("params", {})["parallel"] = n
    save_task(task, force=True)


# ---------------- 智能重命名（2.3） ----------------
NAME_MIN_LEN = 6       # 拟用新名主体最短字符数（不足视为无有效句）
NAME_CTX_MIN = 30      # 命中句扩句目标：前后文上下文至少 30 字（2.3 加大）
NAME_MAX_LEN = 200     # 拟用新名主体最长字符数（2.3 加大；留余量给 (N) 与扩展名）
NAME_MAX_TOTAL = 240   # 落盘文件名总长上限（Windows 组件上限 255 内）
_NAME_SPLIT = re.compile(r"[。！？!?；;，,、\n\r\t ]+")
_NAME_EXT_RE = re.compile(r"\.[A-Za-z0-9]{1,8}")


def _split_name_stem(name):
    """文件名 → (stem, ext)；ext 含点且限 1..8 位字母数字，否则归入 stem。"""
    stem, ext = os.path.splitext(name or "")
    if ext and _NAME_EXT_RE.fullmatch(ext):
        return stem, ext
    return name or "", ""


def _name_segments(src_text):
    """句段偏移表（边界符之间，去空段）。"""
    parts, prev = [], 0
    for m in _NAME_SPLIT.finditer(src_text):
        parts.append((prev, m.start()))
        prev = m.end()
    parts.append((prev, len(src_text)))
    return [(s, e) for s, e in parts if e > s]


def smart_name_for(text, query, orig_name="", ref_text=None):
    """关键词命中句 → 拟用新名（原扩展名保留）；无命中回退正文头部
    上下文；均无有效句返回 ""。

    规则：取首个命中词所在句（空格/句读断句）；向前后文扩句至
    NAME_CTX_MIN 上下文（2.3 加大）、过长以命中词为中心向句界收缩；
    无关键词命中（含无关键词）回退正文头部句块命名；非法字符走
    safe_name 清洗；主体长度限 [NAME_MIN_LEN, NAME_MAX_LEN]。重名 (N)
    后缀在落盘改名时解析（预览/登记阶段不预占）。单一 Python 实现，
    TUI/GUI/web 同源。
    """
    includes, _ = parse_keywords(query)
    _stem, ext = _split_name_stem(orig_name)
    for src_text in (text or "", ref_text or ""):
        if not src_text:
            continue
        low = src_text.lower()
        hit = None
        for w in includes:
            i = low.find(w)
            if i >= 0 and (hit is None or i < hit[0]):
                hit = (i, i + len(w))
        if hit is None:
            continue
        hs, he = hit
        segs = _name_segments(src_text)
        if not segs:
            continue
        lo = hi = 0
        for i, (s, e) in enumerate(segs):
            if s <= hs and he <= e:
                lo = hi = i
                break
        else:
            lo = hi = min(range(len(segs)),
                          key=lambda i: abs(segs[i][0] - hs))
        # 扩句：上下文不足 NAME_CTX_MIN 时左右交替逐句延伸（保前后文）
        grow_right = True
        while (segs[hi][1] - segs[lo][0]) < NAME_CTX_MIN:
            if grow_right and hi + 1 < len(segs):
                hi += 1
            elif lo > 0:
                lo -= 1
            elif hi + 1 < len(segs):
                hi += 1
            else:
                break
            grow_right = not grow_right
        s, e = segs[lo][0], segs[hi][1]
        # 收缩：过长以命中词为中心截窗，两侧尽量贴句界/空格
        if e - s > NAME_MAX_LEN:
            pad = max(0, (NAME_MAX_LEN - (he - hs)) // 2)
            s2 = max(s, hs - pad)
            e2 = min(e, s2 + NAME_MAX_LEN)
            s2 = max(s2, e2 - NAME_MAX_LEN)
            for m in _NAME_SPLIT.finditer(src_text, s2, hs):
                s2 = m.end()
            for m in _NAME_SPLIT.finditer(src_text, he, e2):
                e2 = m.start()
                break
            if e2 <= he or s2 >= hs:
                s2 = max(s, hs - pad)
                e2 = min(e, s2 + NAME_MAX_LEN)
            s, e = s2, e2
        raw = src_text[s:e].strip()
        raw = raw.strip(" ,，、:：;；-_—.。!！?？\"'“”‘’()（）[]【】")
        name = safe_name(raw, "").strip("_ ")
        if len(name) < NAME_MIN_LEN:
            continue
        return name[:NAME_MAX_LEN] + ext
    # 回退（2.3）：无关键词命中（含无关键词）→ 正文头部上下文命名。
    # 语义：拟用名只是候选（前端「是否重命名」勾选显式控制），静默空串
    # 会让整版「留空不改」不可用；正文头部句块是最稳的语义名。
    for src_text in (text or "", ref_text or ""):
        if not src_text:
            continue
        segs = _name_segments(src_text)
        if not segs:
            continue
        lo = hi = 0
        while hi + 1 < len(segs) and (segs[hi][1] - segs[lo][0]) < NAME_CTX_MIN:
            hi += 1
        raw = src_text[segs[lo][0]:segs[hi][1]][:NAME_MAX_LEN].strip()
        raw = raw.strip(" ,，、:：;；-_—.。!！?？\"'“”‘’()（）[]【】")
        name = safe_name(raw, "").strip("_ ")
        if len(name) >= NAME_MIN_LEN:
            return name[:NAME_MAX_LEN] + ext
    return ""


def _rename_on_disk(folder, cur, want):
    """folder 内把 cur 改名为 want（重名自动 ' (N)' 后缀）。
    返回 (最终名, 错误串)；错误时最终名为 None。want 需已含扩展名。"""
    stem, ext = _split_name_stem(want)
    if not stem:
        return None, "新名为空"
    if len(stem) > NAME_MAX_LEN:
        stem = stem[:NAME_MAX_LEN]
    n = 1
    while True:
        suffix = f" ({n})" if n > 1 else ""
        cut = NAME_MAX_TOTAL - len(ext) - len(suffix)
        cand = stem[:cut] + suffix + ext
        if cand == cur:
            return cur, ""
        if not (folder / cand).exists():
            try:
                os.rename(folder / cur, folder / cand)
            except OSError as e:
                return None, f"{type(e).__name__}: {e}"
            return cand, ""
        n += 1


def apply_item_rename(task, ti, new_name=None):
    """执行单条目改名（新建任务下载完成命名 / 任务库确认改名共用）。

    - 文件已落盘：os.rename（重名 ' (N)' 后缀），orig_name 记首次
      原名（还原目标），name/rename 同步为最终名；
    - 文件未落盘（未下载）：只登记 ti["rename"]（拟用新名），待下载
      完成由 apply_pending_rename 收口。
    返回 {"ok","renamed","name","error"}（不 print，P5）。"""
    want = (new_name if new_name is not None else ti.get("rename", "")) or ""
    want = safe_name(str(want).strip(), "").strip()
    if not want:
        return {"ok": False, "renamed": False, "name": ti["name"],
                "error": "新名为空"}
    _ws, wext = _split_name_stem(want)
    if not wext:
        _os, oext = _split_name_stem(ti["name"])
        want = want + oext
    cur = ti["name"]
    folder = pathlib.Path(task["folder"])
    if not (folder / cur).exists():
        ti["rename"] = want
        return {"ok": True, "renamed": False, "name": cur, "error": ""}
    final, err = _rename_on_disk(folder, cur, want)
    if err:
        return {"ok": False, "renamed": False, "name": cur, "error": err}
    if final != cur:
        ti.setdefault("orig_name", cur)   # 只记首次原名（还原目标）
        ti["name"] = final
    ti["rename"] = final
    return {"ok": True, "renamed": final != cur, "name": final, "error": ""}


def apply_pending_rename(task, ti):
    """下载完成收口：登记过拟用新名且与现名不同 → 落盘改名。
    返回是否实际改名；失败静默保留现名（P5：不 print）。"""
    want = ti.get("rename") or ""
    if not want or want == ti["name"]:
        return False
    return apply_item_rename(task, ti)["renamed"]


def restore_item_names(task, keys):
    """还原命名（支持多选）：已改名条目回到 orig_name（重名自动
    ' (N)' 后缀；文件缺失时仅复位 name 记录）。返回
    {"ok","restored","skipped","errors"}（不 print，P5）。"""
    folder = pathlib.Path(task["folder"])
    out = {"ok": True, "restored": 0, "skipped": 0, "errors": []}
    for key in keys:
        ti = find_item(task, (key[0], key[1]))
        if ti is None or not ti.get("orig_name"):
            out["skipped"] += 1
            continue
        cur, orig = ti["name"], ti["orig_name"]
        if (folder / cur).exists():
            final, err = _rename_on_disk(folder, cur, orig)
            if err:
                out["errors"].append(f"#{ti['msg']} {err}")
                continue
            ti["name"] = final
        else:
            ti["name"] = orig
        ti.pop("orig_name", None)
        out["restored"] += 1
    return out


def _norm_dir(p):
    """目录路径规范化（T50）：expanduser + abspath（normpath 语义，
    折叠分隔符/相对段，不改动盘符大小写）。"""
    return os.path.abspath(os.path.expanduser(str(p).strip()))


def plan_move_files(task, new_path):
    """T50 搬运规划（单一来源，GUI 首帧估算与 change_folder 共用）：
    对每个 item 枚举成品 / .dl / .dl.partN.done 三类候选，按判定表定
    处置。只做 stat，不读文件内容、不做任何搬运动作——"动手前一次性
    规划，禁边搬边 glob"即本函数。

    返回 (plan, total_bytes)：
      plan 条目 {"name", "src", "dst", "size", "action"}，按处理顺序：
        move     src 在、dst 不在 → 执行搬运（同卷 rename/跨卷分块拷贝）
        skip     src 不在、dst 在且同大小（上次已完成）→ 自动跳过
        clean    src/dst 同在且同大小（上次跨卷拷贝完成但漏删 src）→
                 删 src，计入 moved
        conflict src/dst 同在但大小不同 → 确定性失败，立即停（两文件不动）
      两边都不存在的候选不进 plan、不计 total。
      size 一律取"目标尺寸"（dst 存在用 dst，否则 src）；
      total_bytes 为全部入 plan 候选的目标尺寸之和（skipped 按其大小
      计入，保证重跑时百分比连续）。

    分片标记 glob 用真实写出名 {name}.dl.part*.done（T50 修正：旧实现
    模式 {name}.part*.done 与下载管线实际写出的 {name}.dl.part{idx}.done
    前缀不符，永远匹配不到，跨目录移动从不搬续传标记）。"""
    old = _norm_dir(task["folder"])
    new = _norm_dir(new_path)
    plan, total_bytes = [], 0
    for ti in task["items"]:
        name = ti["name"]
        cands = [name, name + ".dl"]
        cands += [os.path.basename(p) for p in sorted(
            glob.glob(os.path.join(old, name + ".dl.part*.done")))]
        for fn in cands:
            src = os.path.join(old, fn)
            dst = os.path.join(new, fn)
            s_ok, d_ok = os.path.exists(src), os.path.exists(dst)
            if not s_ok and not d_ok:
                continue                       # 两边都不在：不属于本批
            if not s_ok:
                action, size = "skip", os.path.getsize(dst)
            else:
                ssize = os.path.getsize(src)
                size = os.path.getsize(dst) if d_ok else ssize
                if d_ok:
                    action = "clean" if size == ssize else "conflict"
                else:
                    action = "move"
            plan.append({"name": fn, "src": src, "dst": dst,
                         "size": size, "action": action})
            total_bytes += size
    return plan, total_bytes


def _copy_file_chunked(src, dst_tmp, on_bytes):
    """跨卷分块拷贝（T50）：以 MOVE_CHUNK 循环读 src 写到 dst_tmp，
    每块 flush（不要求 fsync），每块回调 on_bytes(本文件已拷字节)。
    读到 EOF 结束。测试缝：TR harness 可包本函数注入慢盘节拍/计数器。"""
    got = 0
    with open(src, "rb") as f, open(dst_tmp, "wb") as g:
        while True:
            block = f.read(MOVE_CHUNK)
            if not block:
                break
            g.write(block)
            g.flush()
            got += len(block)
            on_bytes(got)
    return got


def change_folder(task, new_path, move_files=False, progress=None):
    """修改任务保存目录（T50 重写，清除 P0-1 半截指向）。

    返回 dict（不再返回元组；两个调用点 gui.edit_task / TUI task_edit_flow
    均按此契约使用）：
      成功/无变化：{"ok": True, "unchanged": bool, "moved": [名...],
                    "skipped": [名...], "pending": [], "error": ""}
      失败（单文件失败即停）：{"ok": False, "unchanged": False,
                    "moved": [...], "skipped": [...], "pending": [...],
                    "error": "文件名: ErrorType: msg"}
    清单元素一律用 basename（ti 文件名本身，含 .dl/.partN.done 后缀）。

    P0-1 语义：task["folder"] 仅在全部文件确认到目标后一次性更新落盘；
    单文件失败即停、不再动后续文件，folder 仍指旧路径——可再次发起，
    已在目标且同大小的文件自动跳过（幂等判定只认 os.path.getsize 大小
    相等：下载管线自身对 .dl 完成度的既有判据即"存在且大小==目标尺寸"，
    成品经原子 rename 产生；同大小即内容完整，重哈希大文件代价不可接受，
    mtime 在 copy/rename 后不可靠）。

    progress：可调用 progress(done, total, bytes_done, bytes_total, name)。
    帧序：开工一次 (0, n, 0, total_bytes, "")；每文件开始
    (i-1, n, 累计, total_bytes, name) 与搬完 (i, n, 累计, total_bytes,
    name)（skipped/clean 同样两帧、无 IO）；跨卷分块期间每块追加
    (i-1, n, 此前累计+本文件已拷, total_bytes, name)。done/bytes_done
    全程非递减，末帧即 (n, n, total_bytes, total_bytes)（最后文件的
    完成帧）。name 为 basename、超长截断 80 字符；回调异常 try 吞掉。"""
    new_path = _norm_dir(new_path)
    os.makedirs(new_path, exist_ok=True)
    old = _norm_dir(task["folder"])
    if new_path == old:
        return {"ok": True, "unchanged": True, "moved": [], "skipped": [],
                "pending": [], "error": ""}

    def _emit(done, total, bd, bt, name):
        if progress is None:
            return
        try:
            progress(done, total, bd, bt, name)
        except Exception:
            pass

    if not move_files:
        # 纯路径修改：不搬任何文件，一次 save_task（行为与旧版一致）
        task["folder"] = new_path
        save_task(task, force=True)
        return {"ok": True, "unchanged": False, "moved": [], "skipped": [],
                "pending": [], "error": ""}

    # ---- 搬运：进入进程级移动表（防同任务并发两个搬运，TUI/GUI 共用）----
    tid = task["id"]
    with _move_lock:
        if tid in MOVING_TASKS:
            return {"ok": False, "unchanged": False, "moved": [],
                    "skipped": [], "pending": [],
                    "error": "目录正在移动中，请勿重复提交"}
        MOVING_TASKS.add(tid)
    try:
        plan, total_bytes = plan_move_files(task, new_path)
        conflicts = [c for c in plan if c["action"] == "conflict"]
        if conflicts:
            # 大小冲突：确定性失败，立即停（两文件都不动，不写盘）
            c = conflicts[0]
            ssize = os.path.getsize(c["src"])
            return {"ok": False, "unchanged": False, "moved": [],
                    "skipped": [p["name"] for p in plan
                                if p["action"] == "skip"],
                    "pending": [p["name"] for p in plan
                                if p["action"] != "skip"],
                    "error": (f"{c['name']}: 目标已存在但大小不一致"
                              f"（src {ssize} 字节/dst {c['size']} 字节），"
                              f"为避免覆盖已停止")}
        # 同卷判定（测试缝：TG_T50_FORCE_COPY=1 强制走分块拷贝分支，
        # 供单卷机器覆盖真实跨卷代码；生产禁设）
        force_copy = os.environ.get("TG_T50_FORCE_COPY") == "1"
        same_volume = (not force_copy
                       and os.stat(old).st_dev == os.stat(new_path).st_dev)
        # 测试缝：TG_T50_FAIL_AT=k 时第 k 个（1-based）实际搬运动作
        # （同卷 rename / 跨卷拷贝）开始处抛 OSError，供半截故障注入；
        # 生产禁设。skip/clean 不计动作序号。
        fail_at = None
        if (os.environ.get("TG_T50_FAIL_AT", "").strip().lstrip("-")
                .isdigit()):
            fail_at = int(os.environ["TG_T50_FAIL_AT"])
        n = len(plan)
        moved, skipped, action_no = [], [], 0
        done = 0
        bytes_done = 0
        _emit(0, n, 0, total_bytes, "")
        for idx, ent in enumerate(plan):
            fn, act, size = ent["name"], ent["action"], ent["size"]
            disp = fn[:80]
            _emit(done, n, bytes_done, total_bytes, disp)   # 文件开始帧
            try:
                if act == "skip":
                    skipped.append(fn)
                elif act == "clean":
                    # 上次拷贝已完成但漏删 src：删 src 即完成，计入 moved
                    os.remove(ent["src"])
                    moved.append(fn)
                else:                                      # move
                    action_no += 1
                    if fail_at is not None and action_no == fail_at:
                        raise OSError("[T50 fault injection]")
                    if same_volume:
                        os.rename(ent["src"], ent["dst"])
                    else:
                        tmp = ent["dst"] + ".t50tmp"
                        try:
                            os.remove(tmp)          # best-effort 清残留
                        except OSError:
                            pass
                        base = bytes_done
                        _copy_file_chunked(
                            ent["src"], tmp,
                            lambda got, _b=base, _d=done, _nm=disp:
                                _emit(_d, n, _b + got, total_bytes, _nm))
                        os.replace(tmp, ent["dst"])
                        os.remove(ent["src"])
                    moved.append(fn)
            except Exception as e:
                # 单文件失败即停：best-effort 清理该文件临时副本，不再
                # 动后续文件；不写 folder、不 save_task
                tmp = ent["dst"] + ".t50tmp"
                try:
                    if os.path.exists(tmp):
                        os.remove(tmp)
                except OSError:
                    pass
                msg = str(e)[:200]
                pending = [fn] + [p["name"] for p in plan[idx + 1:]
                                  if p["action"] != "skip"]
                return {"ok": False, "unchanged": False, "moved": moved,
                        "skipped": skipped, "pending": pending,
                        "error": f"{fn}: {type(e).__name__}: {msg}"}
            done += 1
            bytes_done += size
            _emit(done, n, bytes_done, total_bytes, disp)  # 文件完成帧
        # 全部确认到目标：folder 一次性更新落盘（仍在 MOVING_TASKS 内）
        task["folder"] = new_path
        save_task(task, force=True)
        return {"ok": True, "unchanged": False, "moved": moved,
                "skipped": skipped, "pending": [], "error": ""}
    finally:
        with _move_lock:
            MOVING_TASKS.discard(tid)


def set_item_status(task, key, status):
    """重置条目状态：pending=重新下载，skipped=永久跳过。"""
    ti = find_item(task, key)
    if ti is None or status not in ("pending", "skipped"):
        return False
    ti["status"] = status
    ti.pop("error", None)
    save_task(task, force=True)
    return True


def set_items_status(task, keys, status):
    """批量修改条目状态，只写盘一次，返回修改数。"""
    if status not in ("pending", "skipped"):
        return 0
    ks = set(keys)
    n = 0
    for ti in task["items"]:
        if item_key(ti) in ks:
            ti["status"] = status
            ti.pop("error", None)
            n += 1
    if n:
        save_task(task, force=True)
    return n


async def enrich_task_items(client, task):
    """从 Telegram 拉取全部条目消息，补全旧任务缺失的 text/duration。"""
    entity = await client.get_entity(task["channel"])
    groups = {SRC_CHANNEL: [], SRC_COMMENT: []}
    for ti in task["items"]:
        groups.setdefault(ti.get("src", SRC_CHANNEL), []).append(ti)
    remote = {}

    async def pull(ent, items, src):
        ids = [ti["msg"] for ti in items]
        for i in range(0, len(ids), 100):
            msgs = await client.get_messages(ent, ids=ids[i:i + 100])
            for m in msgs:
                if m is not None:
                    remote[(src, m.id)] = m

    if groups.get(SRC_CHANNEL):
        await pull(entity, groups[SRC_CHANNEL], SRC_CHANNEL)
    if groups.get(SRC_COMMENT):
        discuss = await get_discuss_entity(client, entity)
        if discuss is not None:
            await pull(discuss, groups[SRC_COMMENT], SRC_COMMENT)

    changed = False
    for ti in task["items"]:
        m = remote.get((ti.get("src", SRC_CHANNEL), ti["msg"]))
        if m is None:
            continue
        if not ti.get("text"):
            txt = getattr(m, "message", "") or ""
            if txt:
                ti["text"] = txt
                changed = True
        if not ti.get("duration") and getattr(m, "document", None):
            dur = doc_duration(m.document)
            if dur:
                ti["duration"] = dur
                changed = True
    if changed:
        save_task(task, force=True)


def remove_items(task, keys):
    """从任务清单删除条目（不删本地文件），返回删除数"""
    ks = set(keys)
    before = len(task["items"])
    task["items"] = [
        ti for ti in task["items"] if item_key(ti) not in ks
    ]
    save_task(task, force=True)
    return before - len(task["items"])


def _rec_rank(r):
    """消息新旧排序键：日期优先，同日期取更大的消息 ID。"""
    m = r["msg"]
    return (m.date or None, m.id)


def dedup_records(records):
    """内容去重：文件名与视频大小都相同的两条消息，只保留最新的。
    保留首次出现的排列顺序，仅替换为更新的那条。"""
    best = {}
    order = []
    for r in records:
        k = (r["name"], r["size"])
        if k not in best:
            best[k] = r
            order.append(k)
        elif _rec_rank(r) > _rec_rank(best[k]):
            best[k] = r
    return [best[k] for k in order]


def append_records(task, records, status="pending"):
    """把扫描记录追加进任务。两层去重：
    1) 来源+消息ID 相同（重复扫描）
    2) 文件名+大小 与清单中某条相同（同视频两条消息）
    返回新增数。"""
    exist = {item_key(ti) for ti in task["items"]}
    content = {(ti["name"], ti["size"]) for ti in task["items"]}
    added = 0
    for r in records:
        key = (r.get("src", SRC_CHANNEL), r["msg"].id)
        if key in exist or (r["name"], r["size"]) in content:
            continue
        ti = make_task_item(r)
        ti["status"] = status
        task["items"].append(ti)
        exist.add(key)
        content.add((r["name"], r["size"]))
        added += 1
    if added:
        save_task(task, force=True)
    return added


# ---------------- 下载执行（新建/续传共用） ----------------

class DownloadSession:
    """单个任务的下载会话：独立监控/调度/控制。不同任务各一个，互不干扰。"""

    def __init__(self, task, mon, loop):
        self.task = task
        self.task_id = task["id"]
        self.mon = mon
        self.loop = loop
        self.abort = asyncio.Event()
        self.ctls = {}            # key -> ItemCtl（正在下载）
        self.paused = {}          # key -> record（暂停中）
        self.records = {}         # key -> record（本会话全部，供失败重试）
        self.queue = None         # collections.deque（待调度）
        self.resume_q = None      # 优先队列（暂停/失败恢复）
        self.wake = None          # asyncio.Event（唤醒调度器）
        self.tasks = {}           # key -> asyncio.Task
        self._want = {}           # key -> action（排队中预置命令）

    # ----- 控制入口（可从其他线程调用） -----
    def request(self, action, keys):
        if not self.loop.is_closed():
            self.loop.call_soon_threadsafe(self._apply, action, list(keys))

    def _do_abort(self):
        # T57 取消双根因 B：abort 与 wake 双事件同置（跑在事件循环内，
        # 免去跨线程 set 的线程安全限制）——唤醒被 wake.wait() 挂住的
        # 调度器，使循环顶即有的取消全套（cancel feeder+全部 worker）
        # 能真正进入；wake 为 None（尚未 open/测试直接构造）防御性跳过。
        self.abort.set()
        if self.wake is not None:
            self.wake.set()

    def request_abort(self):
        if not self.loop.is_closed():
            self.loop.call_soon_threadsafe(self._do_abort)

    def _apply(self, action, keys):
        # 仅在下载事件循环线程内执行
        for key in keys:
            ctl = self.ctls.get(key)
            if ctl is not None:
                if action == "skip":
                    ctl.skip.set()
                elif action == "pause":
                    ctl.pause.set()
                elif action == "remove":
                    ctl.cancel.set()
            elif key in self.paused:
                if action == "resume":
                    self._begin_resume(key)
                elif action == "remove":
                    self.paused.pop(key, None)
                    self._begin_remove(key)
            else:
                # 排队中或已落定（完成/失败）
                if action in ("skip", "pause", "remove"):
                    self._want[key] = action
                if action == "remove":
                    self._begin_remove(key)
                elif action == "resume":
                    self._begin_resume(key)

    def _begin_resume(self, key, tail=False):
        rec = self.paused.pop(key, None) or self.records.get(key)
        if rec is None:
            return
        self._want.pop(key, None)

        async def _do():
            ti = find_item(self.task, key)
            if ti is not None and ti["status"] != "pending":
                ti["status"] = "pending"
                ti.pop("error", None)
                save_task(self.task, force=True)
            mode = rec.get("_mode") or (
                "parallel" if rec["size"] > 4 * 1024 * 1024
                else "simple")
            await self.mon.restart(key, keep_done=mode != "simple")
            # T25：tail=True（让路条目恢复）追加 sess.queue 尾——
            # "排队尾"语义（入 resume_q 会前插抢在排队条目之前，违背
            # 让路初衷）；缺省 False 手动暂停/失败恢复依旧前插
            # resume_q，手动路径逐字零变化。
            if tail:
                self.queue.append(rec)
            else:
                self.resume_q.append(rec)
            self.wake.set()

        asyncio.ensure_future(_do())

    def _begin_remove(self, key):
        async def _do():
            await self._do_remove(key)

        asyncio.ensure_future(_do())

    async def _do_remove(self, key):
        ti = find_item(self.task, key)
        if ti is not None:
            self.task["items"] = [
                x for x in self.task["items"] if item_key(x) != key
            ]
            save_task(self.task, force=True)
        await self.mon.remove(key)
        self.wake.set()

    def state(self):
        snap = self.mon.snapshot_json()
        c = task_counts(self.task)
        snap["task_id"] = self.task_id
        snap["task_total"] = c["total"]
        snap["task_finished"] = c["finished"]
        return snap


# ---------------- T25 慢任务让路（FR-12；默认关——开关关时本段零实例化） ----------------

def _slow_yield_cfg(task):
    """任务级让路配置解析（每会话开始快照一次，运行中改任务文件
    不热生效——与 parallel 键现状一致）。task["params"] 四可选键是
    唯一配置入口（slow_yield/slow_yield_bps/slow_yield_win_sec/
    slow_yield_resume_sec）；缺省 enabled=0（关）：直接短路返回，
    不解析不打印其余键（默认态零行为变化/零新日志的解析面保障）。
    开启后非法/缺失值回落缺省（数值夹紧参照 load_acct_policy 范式，
    越界打印一行技术日志）。上限 max 恒源自模块常量 SLOW_YIELD_MAX
    （不提供任务级键，§2.1 红线钉死 params 仅四键）。"""
    raw = (task or {}).get("params") or {}
    if not isinstance(raw, dict):
        raw = {}
    try:
        on = int(raw.get("slow_yield", 0)) == 1
    except (TypeError, ValueError):
        on = False
    if not on:
        return {"enabled": 0}

    def _num(key, dflt, lo, hi):
        try:
            v = float(raw.get(key, dflt))
        except (TypeError, ValueError):
            print(f"[yield] params.{key} 非法，回退默认 {dflt}")
            return dflt
        if v < lo or v > hi:
            print(f"[yield] params.{key}={v} 越界 [{lo},{hi}]，夹紧")
            v = max(lo, min(v, hi))
        return v

    return {
        "enabled": 1,
        "bps": int(_num("slow_yield_bps", SLOW_YIELD_BPS,
                        1024, 100 * 1024 * 1024)),
        "win": _num("slow_yield_win_sec", SLOW_YIELD_WIN_SEC, 5.0, 300.0),
        "resume": _num("slow_yield_resume_sec", SLOW_YIELD_COOLDOWN_SEC,
                       5.0, 1800.0),
        "max": int(SLOW_YIELD_MAX),
    }


class SlowYieldManager:
    """T25 慢任务让路状态机（仅开启态随 run_download_session 生命周期
    存在；全部状态在实例内——不进 task JSON、不进 snapshot 结构、
    不发任何 WS 事件/reason）。每轮巡检（_YIELD_SLEEP 节拍，默认 1s）
    直接消费 Monitor 既有速度数据（mon._snapshot() 现刷新全部
    e["speed"]——窗口右端锚定当前时刻的现成语义——不另建测速器）：

      触发（三者与，§2.2）：
        1) 条目"下载中"且持续低速：低速 streak 满 win 秒（高速即清
           累计——单帧低速/短暂停顿不触发；"下载中"覆盖不足 0.8*win
           不判慢——参照 T51 cover 语义，避开启动爬坡）；
        2) sess.queue 非空（严格只看 queue，feeder 未读完但 queue 空
           不算）；
        3) 本会话该条目让路次数 < max 且不在让路冷却中。
      触发动作：ctl.pause.set()（复用 ItemCtl.pause→PauseFile→worker
        收场置 sess.paused 的既有半途暂停路径，与用户按 P 完全同
        一条路）；[yield] event=pause 机读行；槽位由 worker 收场释放，
        排队条目经 schedule 自然插队。
      恢复（两者与，§2.3）：冷却满 resume 秒且 queue 为空（冷却到期
        但 queue 非空→保持暂停继续等，每轮重查——恢复与触发条件天然
        互斥，无乒乓）→ sess._begin_resume(key, tail=True)：回
        sess.queue 尾（"排队尾"语义；入 resume_q 会前插抢在排队条目
        之前，故让路条目不走）。mon.restart 的 keep_done 语义照旧
        （parallel 分片续传保留、simple 丢弃）。
      边界：手动 pause/resume/remove 优先——条目不再呈现"已暂停"
        （被用户恢复）即让位：waiting 除名、让路计数保留、低速窗口
        从零重累计；条目落终态/被移除即除名；max 用尽后 event=
        exhausted、照常下载到底。
    单轮 tick 异常吞掉不炸循环（CancelledError 照常穿透拆除）；
    退出前打印 loop=exit 生命周期行（关态不启动 → 零 [yield] 行）。
    """

    def __init__(self, sess, cfg, interval=1.0):
        self.sess = sess
        self.mon = sess.mon
        self.cfg = cfg
        self.interval = float(interval)   # 巡检节拍（生产 1.0s）
        self._task = None
        self.streak = {}          # key -> 低速起始时刻（高速即除名）
        self.cover = {}           # key -> "下载中"连续覆盖秒
        self.yields = {}          # key -> 本会话让路次数（跨让路保留）
        self.waiting = {}         # key -> {"due": 冷却到期, "at": 让路时刻}
        self.last_skip = {}       # key -> 上次 skip_trigger 行时刻（10s 节流）
        self.exhausted_seen = set()   # key -> 本轮满窗已报 exhausted
        self.ticks = 0
        self._last = time.monotonic()   # 上轮巡检时刻（真实 dt 累计覆盖秒）

    # ---------- 生命周期（start/stop 与 T51 慢速采样同范式） ----------
    def start(self):
        self._task = asyncio.ensure_future(self._run())
        return self._task

    def stop(self):
        t = self._task
        self._task = None
        if t is not None and not t.done():
            t.cancel()

    async def _run(self):
        try:
            while True:
                await _YIELD_SLEEP(self.interval)
                try:
                    self._tick()
                except Exception:
                    pass           # 单轮异常吞掉：循环必须有界不炸
                self.ticks += 1
        except asyncio.CancelledError:
            raise
        finally:
            _yield_log(f"loop=exit task={self.sess.task_id} "
                       f"ticks={self.ticks} "
                       f"yields={sum(self.yields.values())}")

    # ---------- 单轮巡检 ----------
    def _tick(self):
        now = time.monotonic()
        # 眞实 dt：巡检节拍被测试缝压缩时覆盖秒不虚增（与 streak
        # 的时刻语义同源，均走 time.monotonic 真时）
        dt = max(0.0, min(now - self._last, self.interval * 3))
        self._last = now
        # 现刷新全部条目 e["speed"]（含窗口外样本清理/右端锚定语义，
        # 与渲染/GUI 快照共用同一函数——速度数据唯一来源）
        snap = self.mon._snapshot()
        q = getattr(self.sess, "queue", None)
        qn = len(q) if q is not None else 0
        active = set()
        for e in snap["active"]:
            key = e["key"]
            active.add(key)
            if key in self.waiting:
                continue           # 让路中短暂残留下载态：不累计
            self.cover[key] = self.cover.get(key, 0.0) + dt
            speed = e["speed"]
            if self.cover[key] < self.cfg["win"] * 0.8:
                # 覆盖不足（启动爬坡）不判慢；高速即清累计（照 T51）
                if speed >= self.cfg["bps"]:
                    self.streak.pop(key, None)
                self._skip(key, speed, qn, now)
                continue
            if speed >= self.cfg["bps"]:
                self.streak.pop(key, None)     # 抖动免疫：回高速清零
                continue
            since = self.streak.setdefault(key, now)
            held = now - since
            if held < self.cfg["win"]:
                self._skip(key, speed, qn, now)
                continue
            # ---- 条件 1 成立：检查条件 2（queue）与条件 3（上限）----
            if self.yields.get(key, 0) >= self.cfg["max"]:
                self._exhausted(key, speed, qn, held)
                continue
            if qn == 0:
                self._skip(key, speed, qn, now)
                continue
            self._trigger(key, speed, qn, now, held)
        # 让路等待中的条目：恢复 / 除名 / 让位
        for key, st in list(self.waiting.items()):
            e = self.mon.order.get(key)
            # 注意：“已暂停”同样置 e["final"]（mon.finish 既有语义），
            # 只有四种真终态（完成/已存在/失败/已跳过）与移除才除名
            if e is None or e.get("final") in (
                    "完成", "已存在", "失败", "已跳过"):
                # 终态/被移除：除名（让路计数与收割可见性保留在日志）
                self.waiting.pop(key, None)
                continue
            # 让位判据：三者全不成立才让位于外部处置（手动 resume/
            # 异常重入等重跑路径）：mon 已暂停 / sess.paused 存在 /
            # 让路时的 ctl 还在跑（→只是触发后 worker 收场中的短暂
            # 残留"下载中"帧，并非被外部恢复）
            still_yielding = (
                e["status"] == "已暂停"
                or key in self.sess.paused
                or self.sess.ctls.get(key) is st.get("ctl"))
            if not still_yielding:
                # 外部恢复：让位，计数保留、低速窗口从零重累计
                # （不发事件零打扰）；仅下一轮正常巡检重新观察
                self.waiting.pop(key, None)
                continue
            if now >= st["due"] and qn == 0:
                self.waiting.pop(key, None)
                self.sess._begin_resume(key, tail=True)
                _yield_log(
                    f"event=resume item=#{key[1]} speed_bps=0 "
                    f"win={int(now - st['at'])} queued={qn} "
                    f"count={self.yields.get(key, 0)}")
                print(f"[yield] #{key[1]} 让路冷却已满并等到队列空闲，"
                      f"自动恢复（回队尾，重新排队下载）")
        # 非活跃条目：低速累计/覆盖清零（等待让路中的不重算）
        for key in list(self.streak):
            if key not in active and key not in self.waiting:
                self.streak.pop(key, None)
        for key in list(self.cover):
            if key not in active and key not in self.waiting:
                self.cover.pop(key, None)

    # ---------- 边沿动作 ----------
    def _trigger(self, key, speed, qn, now, held):
        ctl = self.sess.ctls.get(key)
        if ctl is None:
            return                 # 条目已收场（终态竞态）：不触发
        ctl.pause.set()
        self.yields[key] = self.yields.get(key, 0) + 1
        # 记下让路时的 ctl 引用：区分“收场中”（同一 ctl 仍在跑）与
        # “已被外部恢复重跑”（新 worker 新 ctl）——让位判定的关键
        self.waiting[key] = {"due": now + self.cfg["resume"],
                             "at": now, "ctl": ctl}
        self.streak.pop(key, None)
        self.cover.pop(key, None)
        self.exhausted_seen.discard(key)
        _yield_log(
            f"event=pause item=#{key[1]} speed_bps={int(speed)} "
            f"win={int(held)} queued={qn} count={self.yields[key]}")
        print(f"[yield] #{key[1]} 持续低速（已连续 {int(held)}s 低于 "
              f"{human_speed(self.cfg['bps'])}），自动暂停让路"
              f"（第 {self.yields[key]}/{self.cfg['max']} 次，冷却 "
              f"{int(self.cfg['resume'])}s 后待队列空闲恢复）")

    def _skip(self, key, speed, qn, now):
        """低速监控中但触发条件未满足的状态行（覆盖不足/窗口未满/
        queue 空），≤1 行/10s/条目；win=当前低速已累计秒。"""
        if now - self.last_skip.get(key, 0.0) < 10.0:
            return
        self.last_skip[key] = now
        since = self.streak.get(key)
        held = int(now - since) if since is not None else 0
        _yield_log(
            f"event=skip_trigger item=#{key[1]} speed_bps={int(speed)} "
            f"win={held} queued={qn} count={self.yields.get(key, 0)}")

    def _exhausted(self, key, speed, qn, held):
        """上限用尽：同一满窗段一行，不再让路、照常下载到底。"""
        if key in self.exhausted_seen:
            return
        self.exhausted_seen.add(key)
        _yield_log(
            f"event=exhausted item=#{key[1]} speed_bps={int(speed)} "
            f"win={int(held)} queued={qn} "
            f"count={self.yields.get(key, 0)}")
        print(f"[yield] #{key[1]} 本会话已让路 {self.cfg['max']} 次"
              f"（上限），不再让路，照常下载到底")


# ---------------- T51 多账号池（AccountPool；TUI/GUI 唯一调度内核） ----------------

class AccountUnavailable(Exception):
    """池内账号不可用（懒连接失败/未授权）：选号器据此跳过，
    worker 捕获后回退 main，不向上炸穿会话。"""


class AccountBusy(Exception):
    """池内忙拒绝（有活动下载/添加中）：调用方转 {"ok":False,"busy":True}。"""


class AccountDenied(BaseException):
    """池内信号：alt 对当前频道无权限（PRIVATE_ERROR_NAMES 命中），
    worker 捕获后仅一次回退 main。继承 BaseException 的依据：
    download_one 的通用分支是 `except Exception as e:`（3/6/12s 退避），
    Exception 子类会被吞进退避循环、到不了 worker 的回退分支；与
    CancelledError（同为 BaseException）"控制类信号穿透重试"的既有
    先例一致。抛出点仅 _ClientView（且仅 alt 启用 deny 转换），
    捕获点仅 worker，无裸 except 吞没风险。"""


# 测试缝：模块级 client 工厂覆盖位（夹具注入伪工厂用；
# 生产路径保持 None，一律走真 Telethon 工厂）。
_CLIENT_FACTORY = None


class _ClientView:
    """池提供的客户端视图包装器：__getattr__ 透传，仅包
    iter_download/download_media/get_messages 三个方法。
    - FloodWaitError：note_flood 后原样 re-raise（download_one 现有
      except 链逐字保留，当前条目仍原号严格等待——FR-9 不变）；
    - PRIVATE_ERROR_NAMES 命中：deny_convert=True（alt）时转抛
      AccountDenied；main（deny_convert=False）保持现状原样上抛。
    """

    def __init__(self, client, pool, acct, deny_convert=False):
        self._client = client
        self._pool = pool
        self._acct = acct
        self._deny = deny_convert

    def __getattr__(self, name):
        return getattr(self._client, name)

    def _raise_maybe_deny(self, e):
        if self._deny and type(e).__name__ in PRIVATE_ERROR_NAMES:
            raise AccountDenied(f"{self._acct} 无权限访问该频道") from e
        raise e

    def iter_download(self, media, *a, **kw):
        inner = self._client.iter_download(media, *a, **kw)

        async def _gen():
            it = inner.__aiter__()
            while True:
                try:
                    chunk = await it.__anext__()
                except StopAsyncIteration:
                    return
                except FloodWaitError as e:
                    self._pool.note_flood(self._acct, e.seconds)
                    raise
                except Exception as e:  # noqa: BLE001 - 分类后原样上抛
                    self._raise_maybe_deny(e)
                yield chunk

        return _gen()

    async def download_media(self, msg, *a, **kw):
        try:
            return await self._client.download_media(msg, *a, **kw)
        except FloodWaitError as e:
            self._pool.note_flood(self._acct, e.seconds)
            raise
        except Exception as e:  # noqa: BLE001 - 分类后原样上抛
            self._raise_maybe_deny(e)

    async def get_messages(self, ent, ids=None):
        try:
            return await self._client.get_messages(ent, ids=ids)
        except FloodWaitError as e:
            self._pool.note_flood(self._acct, e.seconds)
            raise
        except Exception as e:  # noqa: BLE001 - 分类后原样上抛
            self._raise_maybe_deny(e)


class AccountPool:
    """多 client 懒连接/健康状态/拆除 + 条目选号（T51）。
    单账号（无 accounts 键/无 alt session）是其退化态：所有条目恒
    main，行为与现状一致（feeder 主号、download_one 主号）。
    选号/采样只在 asyncio 循环内发生；snapshot() 供 RPC 线程轮询。"""

    def __init__(self, event_cb=None, client_factory=None, policy=None):
        # 事件出口（{"type":"acct_hint","task_id","data"} payload）：
        # None=no-op；异常只吞不炸
        self.event_cb = event_cb
        self.client_factory = client_factory     # None 用 _CLIENT_FACTORY/真工厂
        self.policy = policy or load_acct_policy()
        # 每号运行态（内存，不持久化）
        self.clients = {ACCT_MAIN: None, ACCT_ALT: None}
        self.state = {}                          # acct -> ready|has_session|logged_out|adding|error
        self.busy = {ACCT_MAIN: 0, ACCT_ALT: 0}  # 该号正在下载条目数
        self.flood_until = {}                    # acct -> monotonic 期限
        self.slow_since = {}                     # 连续低速起点
        self.slow_marked = {}                    # 是否被标记低速
        self.slow_strikes = {}                   # 连续判慢次数（冷却翻倍）
        self.cooldown_until = {}                 # 慢速冷却期限
        self.canary = {}                         # 探针恢复期中
        self.canary_ok_since = {}                # 探针连续达标起点
        self.denied_channels = {}                # 本进程内该号无权限频道集合
        self.me_cache = {}                       # acct -> {"name","username","phone"}|None
        self._adding = {}                        # acct -> {"client","phone"}（半成品）
        self._alt_cfg = alt_configured()         # cfg accounts 含 alt1（缓存）
        self._alt_phone = ""                     # 添加流程内存值
        self._lock = threading.Lock()
        self._seq = 0                            # 选号决策日志序号（从 1 连续）
        self._last_hint = {}                     # (task_id, reason) -> monotonic 去抖
        self._sampler = None                     # 慢速采样任务
        # T59 巡检状态（§2.1 有界后台巡检）：acct -> {probe_due, until,
        # hard_fail, tries, mode}；mode ∈ idle|flood_wait|backoff|
        # unhealthy。线程安全口径与 slow_*/flood_until 等既有状态字典
        # 一致：asyncio 循环线程单写（tick/_warm_fail/_warm_recover），
        # snapshot() 在 _lock 内读。
        self.warm_state = {}
        self._health_task = None                 # 巡检 asyncio task（自持）
        self._health_stop = False                # 停止旗（stop_health_loop）
        self._warm_inflight = False              # 单次探测进行中（防重入）

    # ---------- 事件出口 ----------
    def emit(self, name, payload):
        cb = self.event_cb
        if cb is None:
            return
        try:
            cb(name, payload)
        except Exception:
            pass

    def acct_hint(self, task, reason, from_acct, to_acct, item_msg=None,
                  has_alt=None):
        """acct_hint 事件（复用 T45 通道，唯一事件出口，不新增事件名）。
        payload=params[0]= {"task_id", "data":{reason,from_acct,to_acct,
        item_msg,has_alt,hint}}。去抖：同 task+reason 最小间隔 30s，
        channel_private 不受限。"""
        try:
            tid = (task or {}).get("id") or ""
        except Exception:
            tid = ""
        if reason in ("flood_wait", "slow"):
            now = time.monotonic()
            key = (tid, reason)
            if now - self._last_hint.get(key, 0.0) < ACCT_HINT_MIN_INTERVAL:
                return
            self._last_hint[key] = now
        if has_alt is None:
            has_alt = self._alt_ready()
        data = {
            "reason": reason, "from_acct": from_acct, "to_acct": to_acct,
            "item_msg": item_msg, "has_alt": bool(has_alt),
            "hint": "当前账号下载较慢（服务器限速），登录小号可自动分流提速",
            # seq 纯增 1 键：与 [acct] 决策日志同源序号，TUI 行展示与
            # 日志可对齐（六钉死键零改动；bridge 原样转发 params[0]）
            "seq": self._seq,
        }
        self.emit("acct_hint", {"task_id": tid, "data": data})

    # ---------- 工厂 ----------
    def _raw_client(self, acct, cfg=None):
        """构造（不连接）指定账号的 client：client_factory（池）/模块级
        _CLIENT_FACTORY（测试缝）优先，生产路径走真 Telethon 工厂。
        工厂签名 (session_path, api_id, api_hash) -> client。"""
        path = session_path_for(acct)
        _cfg = cfg or load_config()
        factory = self.client_factory or _CLIENT_FACTORY
        if factory is not None:
            return factory(path, _cfg.get("api_id"), _cfg.get("api_hash"))
        return TelegramClient(
            path, _cfg.get("api_id", BUILTIN_API_ID),
            _cfg.get("api_hash", BUILTIN_API_HASH),
            flood_sleep_threshold=120,
        )

    # ---------- 主号接入 ----------
    def adopt_main(self, client):
        """GUI 把现成 self.client 交池托管（幂等）；池绝不断开主号。"""
        if client is not None:
            self.clients[ACCT_MAIN] = client
            self.state[ACCT_MAIN] = "ready"

    async def login_main_tui(self, cfg):
        """TUI 主号登录：封装原 run() 内联流程（TelegramClient 构造、
        client.start(phone)、内置凭证失效换用户凭证重试一次、get_me），
        行为逐行不变；成功后托管进池并返回 client。"""
        client = self._raw_client(ACCT_MAIN, cfg)
        print("\n正在登录 Telegram …（首次需输入 Telegram App 收到的验证码）")
        try:
            await client.start(phone=lambda: cfg["phone"])
        except Exception as e:
            if cfg.get("builtin_api") and type(e).__name__ in (
                "ApiIdInvalidError", "ApiIdPublishedFloodError",
                "PhoneNumberBannedError"
            ):
                await client.disconnect()
                prompt_manual_api(cfg)
                client = self._raw_client(ACCT_MAIN, cfg)
                await client.start(phone=lambda: cfg["phone"])
            else:
                raise
        me = await client.get_me()
        print(f"已登录：{me.first_name or ''} @{me.username or ''}")
        self.clients[ACCT_MAIN] = client
        self.state[ACCT_MAIN] = "ready"
        self.me_cache[ACCT_MAIN] = {
            "name": me.first_name or "", "username": me.username or "",
            "phone": me.phone or ""}
        return client

    # ---------- 小号登录（参数化；gui RPC 与 TUI 菜单同一套协程） ----------
    async def add_start(self, account_id, phone):
        """alt1 添加第 1 步：构造+connect+(已授权即 ready)否则发码。
        返回 "need_code"|"ready"；异常原样上抛由调用方映射（gui
        connect/send_code 同款语义）。
        T58：connect/授权阶段异常维持 _cleanup_add 后 re-raise（码绝无
        发出）；send_code_request 阶段异常保留 _adding 与 client 原样
        上抛（码可能已在途，add_verify_code 仍可续走），由 gui 层
        _map_send_code_exc 统一分类。
        T59 §2.2：入参 phone 头部守卫——先过 validate_phone，非法立即
        ValueError（错误文案含原因词；gui 层按既有未知异常形状映射），
        不触网、不创建 client、不进 _adding/state。"""
        reason = validate_phone(phone)
        if reason:
            raise ValueError(f"小号手机号不合法：{reason}")
        if account_id != ACCT_ALT:
            raise ValueError(f"不支持添加账号：{account_id!r}（仅 alt1）")
        if self._adding.get(ACCT_ALT) is not None:
            raise RuntimeError("小号正在添加中")
        if (self.clients.get(ACCT_ALT) is not None
                or self._alt_cfg):
            raise RuntimeError("小号已存在")
        client = self._raw_client(ACCT_ALT)
        self._adding[ACCT_ALT] = {"client": client, "phone": phone or ""}
        self.state[ACCT_ALT] = "adding"
        try:
            await client.connect()
            if await client.is_user_authorized():
                await self._finish_add()
                return "ready"
        except Exception:
            await self._cleanup_add()   # 断开半成品；不删可能已授权的 session
            raise
        # T58 发码阶段分流：异常保留 _adding 与 client 原样上抛（不
        # disconnect、不 pop）；半成品回收只靠三处既有路径（verify 成功
        # _finish_add / gui 重入 _cleanup_add / shutdown_alts 等拆除）。
        await client.send_code_request(phone)
        return "need_code"

    async def add_verify_code(self, account_id, code):
        """alt1 添加第 2 步：sign_in(phone, code)；2FA 返回
        "need_password"，成功即 ready（此刻首次落盘 accounts 键）。"""
        from telethon.errors import SessionPasswordNeededError  # noqa: PLC0415
        if account_id != ACCT_ALT or self._adding.get(ACCT_ALT) is None:
            raise RuntimeError("小号不在添加流程中（请先发送验证码）")
        ent = self._adding[ACCT_ALT]
        try:
            await ent["client"].sign_in(phone=ent.get("phone") or None,
                                        code=code)
        except SessionPasswordNeededError:
            return "need_password"
        await self._finish_add()
        return "ready"

    async def add_verify_password(self, account_id, password):
        """alt1 添加第 3 步：sign_in(password=…)；成功即 ready
        （accounts 键唯一常规落盘时机在此刻，§2.2）。"""
        if account_id != ACCT_ALT or self._adding.get(ACCT_ALT) is None:
            raise RuntimeError("小号不在添加流程中（请先校验验证码）")
        ent = self._adding[ACCT_ALT]
        await ent["client"].sign_in(password=password)
        await self._finish_add()
        return "ready"

    async def _finish_add(self):
        """添加成功收口：get_me 缓存 -> 首次写 accounts 键 -> ready。"""
        ent = self._adding.pop(ACCT_ALT, None)
        if ent is None:
            return
        client = ent["client"]
        me = None
        try:
            me = await client.get_me()
            self.me_cache[ACCT_ALT] = {
                "name": me.first_name or "", "username": me.username or "",
                "phone": me.phone or ""}
        except Exception:
            self.me_cache[ACCT_ALT] = None
        self._alt_phone = ent.get("phone") or ""
        self.clients[ACCT_ALT] = client
        self.state[ACCT_ALT] = "ready"
        self._alt_cfg = True
        save_accounts_add_alt()

    async def _cleanup_add(self):
        """添加失败收口：断开半成品 client（失败吞掉），不删可能已授权
        的 session 文件（与主号现状同策略）。"""
        ent = self._adding.pop(ACCT_ALT, None)
        if ent is None:
            return
        client = ent.get("client")
        if client is not None:
            try:
                await client.disconnect()
            except Exception:
                pass
        self._alt_phone = ""
        self.state[ACCT_ALT] = (
            "has_session"
            if pathlib.Path(session_path_for(ACCT_ALT) + ".session").exists()
            else "logged_out")

    # ---------- 拆除 ----------
    async def remove_account(self, account_id):
        """移除小号：main 拒绝（主号退出走 logout 既有语义）；有任何
        活动下载（any_active / alt busy / adding 中）抛 AccountBusy。
        顺序：停选号资格 -> disconnect（吞）-> 逐后缀删 4 文件 ->
        pop accounts 键一次 save -> 清状态。"""
        if account_id != ACCT_ALT:
            raise ValueError("仅支持移除小号；主号请使用退出登录")
        if any_active() or self.busy.get(ACCT_ALT, 0) > 0 \
                or self._adding.get(ACCT_ALT) is not None:
            raise AccountBusy()
        client = self.clients.pop(ACCT_ALT, None)
        self.state.pop(ACCT_ALT, None)
        if client is not None:
            try:
                await client.disconnect()
            except Exception:
                pass
        for f in alt_session_files():
            try:
                os.remove(f)
            except OSError:
                pass
        save_accounts_remove_alt()
        self._alt_cfg = False
        self._alt_phone = ""
        self.me_cache.pop(ACCT_ALT, None)
        self.denied_channels.pop(ACCT_ALT, None)
        self.flood_until.pop(ACCT_ALT, None)
        self.busy[ACCT_ALT] = 0
        self.slow_since.pop(ACCT_ALT, None)
        self.slow_marked.pop(ACCT_ALT, None)
        self.slow_strikes.pop(ACCT_ALT, None)
        self.cooldown_until.pop(ACCT_ALT, None)
        self.canary.pop(ACCT_ALT, None)
        self.canary_ok_since.pop(ACCT_ALT, None)

    async def shutdown_alts(self):
        """主号 logout 时调用：断开 alt 连接、清运行态；
        不删 alt 文件/配置（下次连接可复用授权）。
        T59：先停有界巡检循环（拆除点，§2.1）。"""
        self.stop_health_loop()
        self._adding.pop(ACCT_ALT, None)
        client = self.clients.pop(ACCT_ALT, None)
        self.state.pop(ACCT_ALT, None)
        self.denied_channels.pop(ACCT_ALT, None)
        self.flood_until.pop(ACCT_ALT, None)
        self.busy[ACCT_ALT] = 0
        for k in (self.slow_since, self.slow_marked, self.slow_strikes,
                  self.cooldown_until, self.canary, self.canary_ok_since):
            k.pop(ACCT_ALT, None)
        self._alt_phone = ""
        if client is not None:
            try:
                await client.disconnect()
            except Exception:
                pass

    def shutdown_main_state(self):
        """T55 登出主号：清池内主号运行态（连接由调用方在登出语义内
        负责断开——log_out/disconnect 已含在 logout 流程）。对称
        shutdown_alts；小号侧（连接/文件/配置）不受影响。"""
        self.clients[ACCT_MAIN] = None
        self.state.pop(ACCT_MAIN, None)
        self.me_cache.pop(ACCT_MAIN, None)

    def reset_alt_runtime(self):
        """T56 Bug5：小号提升为主号后清 alt 全部运行态（对称
        remove_account 的状态清理段）。文件/cfg/连接不在本方法职责
        （编排方已断开连接并完成文件改名/配置改写）；提升后的新主号
        经既有 adopt_main()+me 缓存回收入池。"""
        self._adding.pop(ACCT_ALT, None)
        self.clients.pop(ACCT_ALT, None)
        self.state.pop(ACCT_ALT, None)
        self._alt_cfg = False
        self._alt_phone = ""
        self.me_cache.pop(ACCT_ALT, None)
        self.denied_channels.pop(ACCT_ALT, None)
        self.flood_until.pop(ACCT_ALT, None)
        self.busy[ACCT_ALT] = 0
        for k in (self.slow_since, self.slow_marked, self.slow_strikes,
                  self.cooldown_until, self.canary, self.canary_ok_since):
            k.pop(ACCT_ALT, None)

    # ---------- 懒连接 ----------
    async def aclient(self, account_id):
        """取已连接 client；alt 缺失则按 session 路径构造+connect+
        is_user_authorized；未授权/文件缺失置 error 并抛
        AccountUnavailable（worker 据此回退 main）。"""
        c = self.clients.get(account_id)
        if c is not None:
            return c
        if account_id == ACCT_MAIN:
            raise AccountUnavailable("主号未接入账号池")
        if self.state.get(ACCT_ALT) in ("adding",):
            raise AccountUnavailable("小号正在添加中")
        if not pathlib.Path(session_path_for(ACCT_ALT)
                            + ".session").exists():
            self.state[ACCT_ALT] = "logged_out"
            raise AccountUnavailable("小号无会话文件")
        client = self._raw_client(ACCT_ALT)
        try:
            await client.connect()
            if not await client.is_user_authorized():
                raise AccountUnavailable("小号未授权（请移除后重新登录）")
        except AccountUnavailable:
            self.state[ACCT_ALT] = "error"
            raise
        except Exception as e:
            try:
                await client.disconnect()
            except Exception:
                pass
            self.state[ACCT_ALT] = "error"
            raise AccountUnavailable(
                f"小号连接失败：{type(e).__name__}") from e
        self.clients[ACCT_ALT] = client
        self.state[ACCT_ALT] = "ready"
        return client

    async def _warm_once(self):
        """T59：单次预热尝试（自 warm_alt 连接块提炼；warm_alt 外部
        签名/调用点零改）。返回 (ok, exc)：
          ok=True → 成功（ready/缓存 me/回填 _alt_phone 在此完成）；
          ok=False, exc=None → 前置不满足（不可懒连接/phone_invalid/
            已连接/adding/探测重入），零动作零触网；
          ok=False, exc 非 None → 连接/授权失败（已 disconnect 吞掉，
            状态回落 has_session 绝不置 error；异常原样带回调用方
            分类——warm_alt 打印后吞掉保 T56 语义，巡检 tick 走 T22
            分类）。打印一律留在调用方（tick 巡检轮只出 [wacct] 行，
            每轮恰一行）。"""
        if self._warm_inflight:
            return False, None            # 单次探测重入守卫（有界）
        if not self._alt_lazy_ok():
            return False, None
        if self._phone_invalid_of(ACCT_ALT):
            return False, None            # T59 §2.2：脏 phone 不进巡检
        if self.clients.get(ACCT_ALT) is not None:
            return False, None
        if self._adding.get(ACCT_ALT) is not None:
            return False, None
        client = self._raw_client(ACCT_ALT)
        self._warm_inflight = True
        try:
            try:
                await asyncio.wait_for(client.connect(), timeout=20.0)
                if not await client.is_user_authorized():
                    raise RuntimeError("小号未授权")
            except Exception as e:
                try:
                    await client.disconnect()
                except Exception:
                    pass
                return False, e
            self.clients[ACCT_ALT] = client
            self.state[ACCT_ALT] = "ready"
            try:
                me = await client.get_me()
                self.me_cache[ACCT_ALT] = {
                    "name": me.first_name or "", "username": me.username or "",
                    "phone": me.phone or ""}
                if me.phone:
                    self._alt_phone = me.phone
            except Exception:
                self.me_cache[ACCT_ALT] = None
            return True, None
        finally:
            self._warm_inflight = False

    async def warm_alt(self):
        """T56 Bug3：主号就绪后静默预热小号（fire-and-forget 提交，
        绝不阻塞登录返回）。仅当可懒连接（已配置+session 文件在）且
        无 client、非 adding/error 时动作；construct→connect（限时
        20s）→is_user_authorized→成功置 ready+缓存 me（含回填
        _alt_phone）；任何失败 disconnect 吞掉、绝不置 error——状态
        回落 has_session，保住下次选号资格（离线不能永久打掉 alt）。
        T59：连接块提炼为 _warm_once()（语义等价：返回值/[acct] 行/
        状态效果逐一不变）；首次调用同时启动有界后台巡检
        （start_health_loop，§2.1）。"""
        self.start_health_loop()
        ok, exc = await self._warm_once()
        if ok:
            print("[acct] 小号预热成功（连接已就绪）")
            return True
        if exc is not None:
            print(f"[acct] 小号预热失败（{type(exc).__name__}），保持待连接")
        return False

    # ---------- T59 有界后台巡检（§2.1 health loop；零新事件/零新 RPC） ----------
    def _phone_invalid_of(self, acct):
        """T59 §2.2：读 cfg accounts 条目的 phone_invalid 标记（只读；
        标记由 reconcile_phones 启动对账幂等维护；运行期 add_start 只
        放行合法号、remove_account 弹除整键，标记与账号态自洽）。
        load_config 现读（小文件、低频调用点：选号决策/巡检 tick/
        snapshot）；异常/缺键一律 False 保守放行。"""
        try:
            accts = load_config().get("accounts")
        except Exception:
            return False
        if not isinstance(accts, list):
            return False
        for a in accts:
            if isinstance(a, dict) and a.get("id") == acct:
                return bool(a.get("phone_invalid"))
        return False

    @staticmethod
    def _warm_st0():
        """T59 巡检状态初值（mode=idle）。"""
        return {"probe_due": 0.0, "until": 0.0, "hard_fail": 0,
                "tries": 0, "mode": "idle"}

    def start_health_loop(self):
        """T59 §2.1：启动有界后台巡检（幂等；fire-and-forget——立即
        返回，绝不阻塞调用方）。warm_alt 首次调用自动触发；循环自持，
        随 shutdown_alts 停止；测试可显式 stop_health_loop。"""
        if self._health_task is not None and not self._health_task.done():
            return
        self._health_stop = False
        self._health_task = asyncio.ensure_future(self._health_loop())

    def stop_health_loop(self):
        """T59 测试缝/拆除：停巡检（幂等；cancel 即时打断排程等待，
        循环 finally 打印退出行）。"""
        self._health_stop = True
        t = self._health_task
        self._health_task = None
        if t is not None and not t.done():
            t.cancel()

    async def _health_loop(self):
        """T59 §2.1 巡检循环：轮询间隔 policy.warm_poll_sec（默认
        60s）；每轮至多一次预热尝试（_warm_tick）；任何异常吞掉不炸
        循环（CancelledError 照常穿透拆除）；退出前打印一行机读日志
        （[wacct] loop=exit——生命周期行，无探测结果语义故不带
        event=，字段另行稳定）。"""
        pol = float(self.policy.get("warm_poll_sec", WARM_POLL_SEC))
        ticks = 0
        try:
            while not self._health_stop:
                await _WARM_SLEEP(pol)
                if self._health_stop:
                    break
                try:
                    await self._warm_tick()
                except Exception:
                    pass           # 巡检轮异常吞掉：循环必须有界不炸
                ticks += 1
        finally:
            _wacct_log(f"loop=exit id={ACCT_ALT} ticks={ticks}")

    async def _warm_tick(self):
        """T59 §2.1 一轮巡检（有界）：仅小号、仅"待连接"态动作；
        phone_invalid 号零触碰（§2.2）；排程（flood/退避/unhealthy）
        未到期零动作零日志。单轮至多一次预热尝试。"""
        if self._adding.get(ACCT_ALT) is not None:
            return
        if self.clients.get(ACCT_ALT) is not None:
            return
        if not self._alt_lazy_ok():
            return
        if self._phone_invalid_of(ACCT_ALT):
            return
        st = self.warm_state.get(ACCT_ALT) or {}
        if time.monotonic() < float(st.get("probe_due", 0.0)):
            return
        ok, exc = await self._warm_once()
        if ok:
            self._warm_recover()
        elif exc is not None:
            self._warm_fail(exc)
        # exc 为 None：前置竞态（探测重入/adding 刚开始等）——零动作

    def _warm_classify(self, e):
        """T59 §2.1：复用 T22 classify_download_error 取分类（不复制
        实现）：flood→"flood"；deterministic→"deterministic"；其余
        （transient/reference/control/未知兜底）一律按 transient 保守
        再探（connect/get_me 链路不产 control；reference 不可达）。"""
        kind = classify_download_error(e)
        if kind == "flood":
            return "flood"
        if kind == "deterministic":
            return "deterministic"
        return "transient"

    def _warm_backoff_delay(self, tries):
        """T59 §2.1 瞬时退避序列（任务书钉死 30/60/120/300/300…）：
        前 3 次指数 2 倍（base/2base/4base，逐项封顶），第 4 次起一律
        WARM_TRANSIENT_BACKOFF_MAX；base 可经 policy
        warm_transient_backoff_sec 覆盖。"""
        base = float(self.policy.get("warm_transient_backoff_sec",
                                     WARM_TRANSIENT_BACKOFF))
        if tries <= 3:
            return min(base * (2 ** (tries - 1)),
                       WARM_TRANSIENT_BACKOFF_MAX)
        return WARM_TRANSIENT_BACKOFF_MAX

    def _warm_fail(self, e):
        """T59 §2.1 一次巡检探测失败（[wacct] 恰一行）：
        - flood：不计失败次、不进退避，按 wait 排程下轮（到期再探）；
        - deterministic：hard_fail+=1；达 policy.warm_hard_fail_max →
          标 unhealthy、退避 policy.warm_unhealthy_backoff_sec 后允许
          一轮再探（再探成功→_warm_recover 复位；再探硬错→hard_fail
          仍≥上限→重新标 unhealthy）；未达上限→下轮轮询再探；
        - transient：不计硬错，指数退避再探（探测成功即复位）。
        计数口径：flood/transient 不改 hard_fail、deterministic 不改
        tries；仅 _warm_recover 复位二者（任务书未定义跨类清零）。"""
        st = self.warm_state.setdefault(ACCT_ALT, self._warm_st0())
        now = time.monotonic()
        kind = self._warm_classify(e)
        if kind == "flood":
            wait = int(getattr(e, "seconds", 0) or 0)
            if wait < 1:
                wait = 1
            st["mode"] = "flood_wait"
            st["probe_due"] = now + wait
            st["until"] = st["probe_due"]
            _wacct_log(f"id={ACCT_ALT} event=flood wait={wait}")
            return
        if kind == "deterministic":
            st["hard_fail"] = int(st.get("hard_fail", 0)) + 1
            mx = int(self.policy.get("warm_hard_fail_max",
                                     WARM_HARD_FAIL_MAX))
            if st["hard_fail"] >= mx:
                bo = float(self.policy.get(
                    "warm_unhealthy_backoff_sec", WARM_UNHEALTHY_BACKOFF))
                st["mode"] = "unhealthy"
                st["probe_due"] = now + bo
                st["until"] = st["probe_due"]
                _wacct_log(f"id={ACCT_ALT} event=unhealthy "
                           f"hard_fail={st['hard_fail']} backoff={int(bo)}")
            else:
                pol = float(self.policy.get("warm_poll_sec",
                                            WARM_POLL_SEC))
                st["mode"] = "backoff"
                st["probe_due"] = now + pol
                st["until"] = st["probe_due"]
                _wacct_log(f"id={ACCT_ALT} event=probe outcome=fail "
                           f"kind=deterministic hard_fail={st['hard_fail']}")
            return
        st["tries"] = int(st.get("tries", 0)) + 1
        delay = self._warm_backoff_delay(st["tries"])
        st["mode"] = "backoff"
        st["probe_due"] = now + delay
        st["until"] = st["probe_due"]
        _wacct_log(f"id={ACCT_ALT} event=backoff delay={int(delay)} "
                   f"tries={st['tries']}")

    def _warm_recover(self):
        """T59 §2.1 探测成功：ready/me/_alt_phone 已由 _warm_once 置好
        → 自动回可调度池（_alt_lazy_ok 本就为真，无需额外动作）；复位
        计数/模式（[wacct] 恰一行）。恢复边沿零 acct_hint（reason 词表
        钉死），经 TUI 打印 + snapshot 可见。"""
        st = self.warm_state.setdefault(ACCT_ALT, self._warm_st0())
        prev = st.get("mode", "idle")
        st["mode"] = "idle"
        st["probe_due"] = 0.0
        st["until"] = 0.0
        st["hard_fail"] = 0
        st["tries"] = 0
        _wacct_log(f"id={ACCT_ALT} event=recover from={prev}")

    # ---------- 状态标注（下载侧经客户端视图回调） ----------
    def note_flood(self, acct, seconds, channel=None, item_msg=None):
        """该号条目捕获 FloodWaitError：置 flood_until=now+seconds。
        当前条目仍原号按 enter_flood 严格等待（FR-9 不变）；
        后续新条目改派（flood 到期自动恢复，无需 canary）。"""
        now = time.monotonic()
        until = now + float(seconds)
        if until > self.flood_until.get(acct, 0.0):
            self.flood_until[acct] = until
        print(f"[acct] {ACCT_MARK.get(acct, acct)} 触发服务器限速 "
              f"FloodWait {int(seconds)}s，新条目改派（本条仍原号等待）")

    def note_deny(self, acct, channel, item_msg=None):
        """alt 对该频道无权限（AccountDenied 已被 worker 捕获）：
        denied_channels 记频道（本进程），后续同频道条目不再派该号。"""
        if channel:
            self.denied_channels.setdefault(acct, set()).add(channel)
        print(f"[acct] {ACCT_MARK.get(acct, acct)} 无该频道权限"
              f"（{channel}，#{item_msg if item_msg is not None else '—'}），"
              f"后续同频道改派")

    # ---------- 慢速状态机（1s 采样喂入；随 run_download_session 启停） ----------
    def _strike(self, acct, now):
        """判慢一次：标记 + 冷却（重复翻倍、封顶 ACCT_COOLDOWN_MAX）。"""
        pol = self.policy
        self.slow_strikes[acct] = self.slow_strikes.get(acct, 0) + 1
        cd = min(pol["cooldown_sec"]
                 * (2 ** (self.slow_strikes[acct] - 1)), ACCT_COOLDOWN_MAX)
        self.cooldown_until[acct] = now + cd
        self.slow_marked[acct] = True
        self.slow_since.pop(acct, None)
        self.canary.pop(acct, None)
        self.canary_ok_since.pop(acct, None)
        print(f"[acct] {ACCT_MARK.get(acct, acct)} 持续低速"
              f"（阈值 {human_speed(pol['slow_bps'])} 已满 "
              f"{int(pol['slow_win_sec'])}s），标记低速并冷却 {int(cd)}s"
              f"（第 {self.slow_strikes[acct]} 次）")
        return cd

    def _recover(self, acct, now):
        """探针连续达标：清标记/冷却/次数，重新参与正常调度。"""
        self.slow_strikes.pop(acct, None)
        self.slow_marked.pop(acct, None)
        self.cooldown_until.pop(acct, None)
        self.canary.pop(acct, None)
        self.canary_ok_since.pop(acct, None)
        print(f"[acct] {ACCT_MARK.get(acct, acct)} 速度已恢复，"
              f"重新参与调度")
        # 说明：恢复边沿不产生新 acct_hint（reason 枚举钉死 3 值且
        # 禁新增事件名）；TUI 打印本行即可观测，GUI 经 account_list
        # 轮询 flag 感知（T52 已论证无新事件必要）。

    def _slow_feed(self, acct, speed, cover, now):
        """1s 采样输入：低速累计 + 边沿（标记/再冷却/恢复）。"""
        pol = self.policy
        win = pol["slow_win_sec"]
        if cover < win * 0.8:
            # 样本覆盖不足（条目刚启动爬坡）：不足以判慢；高速则清累计
            if speed >= pol["slow_bps"]:
                self.slow_since.pop(acct, None)
                self.canary_ok_since.pop(acct, None)
            return
        if speed >= pol["slow_bps"]:
            self.slow_since.pop(acct, None)
            if self.canary.get(acct):
                ok_since = self.canary_ok_since.get(acct)
                if ok_since is None:
                    self.canary_ok_since[acct] = now
                elif now - ok_since >= win:
                    self._recover(acct, now)
            return
        # 本秒低速
        self.canary_ok_since.pop(acct, None)
        if self.canary.get(acct):
            self._strike(acct, now)          # 探针失败：立即再冷却翻倍
            return
        if (self.slow_marked.get(acct)
                and now < self.cooldown_until.get(acct, 0.0)):
            return       # 已标记且冷却中：不再累计（canary 探针期除外）
        since = self.slow_since.setdefault(acct, now)
        if now - since >= win:
            self._strike(acct, now)

    def start_sampler(self, sess):
        """慢速采样任务（open 后启动、run 收尾 finally 取消，与
        phase_heartbeat 同生命周期范式）：每 1s 从 Monitor 条目取
        "分配给本号"条目的 done 增量之和喂 _slow_feed。

        逐条目增量求和（而非"下载中条目 done 之和"）：后者随条目
        完成离开窗口而下降，会造出假低速（负 delta）。完成条目
        的尾帧增量同样计入；seed 续传字节的单峰跳变只会高估速度，
        不会误判低速。该号本秒无任何增量且无新"下载中"条目时
        cover 断档重置（覆盖不足不判慢）。"""
        async def _sample():
            mon = sess.mon
            prev_done = {}     # entry key -> 上次 done
            cover = {}
            interval = 1.0
            while True:
                await asyncio.sleep(interval)
                now = time.monotonic()
                for a in ACCT_IDS:
                    delta = 0
                    active = False
                    for e in mon.entries:
                        if e.get("acct", ACCT_MAIN) != a:
                            continue
                        k = e["key"]
                        cur = e["done"]
                        prev = prev_done.get(k)
                        prev_done[k] = cur
                        if prev is None:
                            # 条目首帧：只记账不计速（起始爬坡）
                            if e.get("status") == "下载中":
                                active = True
                            continue
                        d = cur - prev
                        if d > 0:
                            delta += d
                            active = True
                    if not active:
                        cover.pop(a, None)
                        continue
                    cover[a] = cover.get(a, 0.0) + interval
                    self._slow_feed(a, delta / interval, cover[a], now)

        self._sampler = asyncio.ensure_future(_sample())
        return self._sampler

    def stop_sampler(self):
        t = self._sampler
        self._sampler = None
        if t is not None:
            t.cancel()

    # ---------- 条目选号（纯同步决策，确定性，决策日志逐行可 grep） ----------
    def _alt_lazy_ok(self):
        """alt 是否可懒连接（select 侧内存+文件 stat 判定，不触网）：
        有 ready client；或已配置+session 文件在且不在 error/adding。"""
        if self.clients.get(ACCT_ALT) is not None:
            return self.state.get(ACCT_ALT) != "error"
        if self.state.get(ACCT_ALT) in ("adding", "error"):
            return False
        return self._alt_cfg and pathlib.Path(
            session_path_for(ACCT_ALT) + ".session").exists()

    def _alt_ready(self):
        """alt 当下可用（供 acct_hint 的 has_alt）。"""
        return self._alt_lazy_ok()

    def _eligible(self, acct, channel):
        """该号当下是否可派新条目（返回 (ok, 过滤原因)）。
        过滤原因字符串：flood:<s> / slow:cooldown<s> / denied / invalid
        / ""（空=合格；main 永远不在 denied 集——note_deny 仅 alt 触发）。
        T59 §2.2 头部守卫：cfg 标 phone_invalid 的小号不进调度；过滤
        原因经 select 决策日志 reason=invalid 落盘（pref=alt1/auto/
        sticky 落到该号时可见）。主号不受本守卫影响（唯一回退号）。"""
        if acct == ACCT_ALT and self._phone_invalid_of(ACCT_ALT):
            return False, "invalid"
        now = time.monotonic()
        fu = self.flood_until.get(acct, 0.0)
        if now < fu:
            return False, f"flood:{int(fu - now) + 1}"
        if self.slow_marked.get(acct) and now < self.cooldown_until.get(
                acct, 0.0):
            return False, f"slow:cooldown{int(self.cooldown_until[acct] - now) + 1}"
        if acct == ACCT_ALT:
            if channel and channel in self.denied_channels.get(acct, ()):
                return False, "denied"
        return True, ""

    def select(self, task, item_key, sticky_acct=None):
        """条目选号（唯一调度决策实现，TUI/GUI 共用；禁止分叉）。
        输入：task（sess.task 同一 dict，偏好实时读）、item_key=(src,mid)、
        sticky_acct=条目持久 acct（缺失=main）。
        决策日志逐次打印：[acct] seq=n item=#mid pref=… select=… reason=…
        busy main=a alt1=b。"""
        pref = ((task.get("params") or {}).get("acct_pref")
                or "auto")
        if pref not in ("main", "alt1", "auto"):
            pref = "auto"
        mid = item_key[1] if isinstance(item_key, tuple) else item_key
        channel = (task or {}).get("channel")
        now = time.monotonic()
        self._seq += 1

        # main 侧过滤（flood/slow 冷却）
        m_ok, m_rsn = self._eligible(ACCT_MAIN, channel)
        a_ok = False
        a_rsn = ""
        has_alt = self._alt_lazy_ok()
        if has_alt:
            a_ok, a_rsn = self._eligible(ACCT_ALT, channel)

        sel, reason = ACCT_MAIN, "only"
        if pref == "main":
            sel, reason = ACCT_MAIN, "only"
        elif pref == "alt1":
            if a_ok:
                sel, reason = ACCT_ALT, "only"
            else:
                sel, reason = ACCT_MAIN, (a_rsn or "only")
        else:                                   # auto
            if not has_alt:
                sel, reason = ACCT_MAIN, "only"     # 退化态：恒 main
            elif sticky_acct == ACCT_ALT and a_ok:
                sel, reason = ACCT_ALT, "sticky"    # .dl 亲和
            elif not a_ok:
                sel, reason = ACCT_MAIN, (a_rsn or "busy")
            elif not m_ok:
                # main 被 flood/slow 过滤 -> 改派 alt（reason=过滤原因）
                sel, reason = ACCT_ALT, m_rsn
            else:
                # canary 优先：被标记号的冷却已到期且探针尚未在期 ->
                # 本条无条件作为探针（无论 busy 大小）
                cand = None
                if (self.slow_marked.get(ACCT_MAIN)
                        and not self.canary.get(ACCT_MAIN)
                        and now >= self.cooldown_until.get(ACCT_MAIN, 0.0)):
                    cand = ACCT_MAIN
                elif (self.slow_marked.get(ACCT_ALT)
                        and not self.canary.get(ACCT_ALT)
                        and now >= self.cooldown_until.get(ACCT_ALT, 0.0)
                        and a_ok):
                    cand = ACCT_ALT
                if cand is not None:
                    sel, reason = cand, "canary"
                    self.canary[cand] = True
                else:
                    mb, ab = self.busy[ACCT_MAIN], self.busy.get(ACCT_ALT, 0)
                    if ab < mb:
                        sel, reason = ACCT_ALT, "busy"
                    else:
                        sel, reason = ACCT_MAIN, "busy"  # tie -> main 优先

        # acct_hint（改派/提示；reason 词表映射 flood_wait/slow）。
        # 触发依据是 main 是否被 flood/slow 过滤（而非 reason 字面——
        # 无 alt 退化态 reason=only 也要发提示）：
        #  - 改派 alt：from=main to=alt1 has_alt=true
        #  - 无可用 alt（或偏好钉死 main）：from=main to=null
        #    has_alt=false（T52 无小号 CTA 场景）
        # 同 task+reason 去抖 30s（acct_hint 内部）。
        if not m_ok:
            ev = "flood_wait" if m_rsn.startswith("flood") else "slow"
            if sel == ACCT_ALT:
                self.acct_hint(task, ev, ACCT_MAIN, ACCT_ALT,
                               item_msg=mid, has_alt=True)
            else:
                self.acct_hint(task, ev, ACCT_MAIN, None,
                               item_msg=mid, has_alt=False)
        print(f"[acct] seq={self._seq} item=#{mid} pref={pref} "
              f"select={sel} reason={reason} "
              f"busy main={self.busy[ACCT_MAIN]} "
              f"alt1={self.busy.get(ACCT_ALT, 0)}")
        return sel

    # ---------- 快照（RPC 线程轮询；与 asyncio 侧一把锁） ----------
    def _flag_of(self, acct, now):
        if now < self.flood_until.get(acct, 0.0):
            return "flood"
        if (self.slow_marked.get(acct) or self.canary.get(acct)):
            return "slow"
        if self.denied_channels.get(acct):
            return "denied"
        return "ok"

    def _state_of(self, acct):
        if acct == ACCT_MAIN:
            if self.clients.get(ACCT_MAIN) is not None:
                return "ready"
            return ("has_session"
                    if pathlib.Path(SESSION_PATH + ".session").exists()
                    else "logged_out")
        if self._adding.get(ACCT_ALT) is not None:
            return "adding"
        if self.clients.get(ACCT_ALT) is not None:
            return self.state.get(ACCT_ALT, "ready")
        return self.state.get(ACCT_ALT) or (
            "has_session"
            if pathlib.Path(session_path_for(ACCT_ALT)
                            + ".session").exists()
            else "logged_out")

    def snapshot(self):
        """线程安全账号状态列表（account_list RPC 消费）：
        main 恒在列；alt1 仅在已配置（或半成品文件在）或 adding 中时
        在列。me 不触网（仅登录成功缓存的 get_me）。
        T59 §2.3：账号条目加可选键——phone_invalid:true（cfg 标记脏
        phone 时）/ health:"unhealthy"（巡检标 unhealthy 时），仅异常
        时出现、缺省省略（旧前端零感知）。"""
        now = time.monotonic()
        with self._lock:
            main_row = {
                "id": ACCT_MAIN, "label": "",
                "state": self._state_of(ACCT_MAIN),
                "phone": "", "me": self.me_cache.get(ACCT_MAIN),
                "busy": self.busy.get(ACCT_MAIN, 0),
                "flag": self._flag_of(ACCT_MAIN, now),
            }
            out = [self._t59_marks(ACCT_MAIN, main_row)]
            alt_on = (self._alt_lazy_ok()
                      or self._adding.get(ACCT_ALT) is not None)
            if alt_on:
                alt_row = {
                    "id": ACCT_ALT, "label": "",
                    "state": self._state_of(ACCT_ALT),
                    "phone": self._alt_phone,
                    "me": self.me_cache.get(ACCT_ALT),
                    "busy": self.busy.get(ACCT_ALT, 0),
                    "flag": self._flag_of(ACCT_ALT, now),
                }
                out.append(self._t59_marks(ACCT_ALT, alt_row))
            return out

    def _t59_marks(self, acct, row):
        """T59 §2.3 snapshot 可选键（就地加；仅异常时出现）。
        warm_state 读口与 snapshot 同锁（本方法仅在 _lock 内调用）；
        phone_invalid 现读 cfg 标记（reconcile_phones 维护）。"""
        if self._phone_invalid_of(acct):
            row["phone_invalid"] = True
        st = self.warm_state.get(acct)
        if isinstance(st, dict) and st.get("mode") == "unhealthy":
            row["health"] = "unhealthy"
        return row

    def busy_snapshot(self):
        """线程安全 busy 快照（remove 等闸门用）。"""
        with self._lock:
            return dict(self.busy)

    def alt_adding(self):
        return self._adding.get(ACCT_ALT) is not None

    def alt_ready_or_adding(self):
        """set_guest 拒绝条件：池内有 alt 就绪/adding。"""
        return (self.clients.get(ACCT_ALT) is not None
                or self._alt_lazy_ok()
                or self._adding.get(ACCT_ALT) is not None)

    # ---------- busy 记账（worker 调；asyncio 循环内） ----------
    def busy_add(self, acct):
        self.busy[acct] = self.busy.get(acct, 0) + 1

    def busy_sub(self, acct):
        self.busy[acct] = max(0, self.busy.get(acct, 0) - 1)


# 全部活动会话：task_id -> DownloadSession（不同任务独立，可并行）
ACTIVE_SESSIONS = {}

# T50 目录搬运中任务表：task_id 集合（TUI/GUI 共用，纯内存进程态，
# 不持久化）。change_folder(move_files=True) 进入即登记、finally 移出；
# open_download_session 在注册 ACTIVE_SESSIONS 之前的同步段检查拒绝
# （搬运中禁下载），GUI RPC/TUI 调用点反向检查 ACTIVE_SESSIONS
# （下载中禁搬）。
MOVING_TASKS = set()
_move_lock = threading.Lock()

# T49 终态墓碑：task_id -> (终态快照 dict, monotonic 时刻)。
# 会话结束后 3 秒内轮询（active_states）仍可见终态快照，前端能展示
# "完成/已中止/失败"；open 同 id 新会话时无条件清除（中止后立即可再继续）。
# 墓碑是只读快照，不参与控制（abort/item_action 仍只认 live 会话）。
SESSION_TOMBSTONES = {}
SESSION_TOMBSTONE_TTL = 3.0


def active_states():
    """live 会话快照 + 未过期墓碑快照；同 task_id 以 live 为准。"""
    now = time.monotonic()
    for tid in [k for k, (_st, ts) in SESSION_TOMBSTONES.items()
                if now - ts > SESSION_TOMBSTONE_TTL]:
        SESSION_TOMBSTONES.pop(tid, None)
    out = [s.state() for s in ACTIVE_SESSIONS.values()]
    for tid, (st, _ts) in SESSION_TOMBSTONES.items():
        if tid not in ACTIVE_SESSIONS:
            out.append(st)
    return out


def any_active():
    return bool(ACTIVE_SESSIONS)     # 只数 live（退出提示语义不变）


def _rec_key(it):
    return (it.get("src", SRC_CHANNEL), it["msg"].id)


def build_planned(task, todo, lazy_statuses):
    """占位计划（全部待处理条目）纯函数，无 IO：Monitor 与磁盘估算都
    基于它，不依赖真实 Message 对象。lazy_statuses 非空从任务清单算
    （零网络），否则从 todo（记录已在内存）算。"""
    if lazy_statuses is not None:
        return [{
            "mid": ti["msg"], "src": ti.get("src", SRC_CHANNEL),
            "size": ti["size"], "name": ti["name"],
            "date": ti.get("date", ""),
        } for ti in task["items"] if ti["status"] in lazy_statuses]
    return [{
        "mid": it["msg"].id, "src": it.get("src", SRC_CHANNEL),
        "size": it["size"], "name": it["name"],
        "date": (f"{it['msg'].date:%Y-%m-%d}" if it["msg"].date else ""),
    } for it in todo]


async def open_download_session(client, task, planned, parallel, *, gui=None,
                                lazy_statuses=None, phase_cb=None,
                                pool=None, event_cb=None):
    """纯内存打开下载会话（T49）：构造 Monitor/DownloadSession 并以
    preparing 阶段注册进 ACTIVE_SESSIONS——不做任何磁盘/网络/阻塞
    调用，GUI 点"继续"后轮询立刻可见 preparing（≤200ms 预算的关键）。

    - task_id 已有 live 会话：打印既有提示并返回 None（调用方收口）。
    - 同 id 墓碑无条件清除后重建（中止后可立即再继续，不等 TTL）。
    - phase_cb：6 参契约 (task_id, phase, detail, index, total, snap)，
      内部包装成 Monitor 的 4 参 phase_cb；回调异常只记不抛。
    - pool/event_cb（T51，仅 keyword-only 可选）：挂到 sess（sess.pool/
      sess.event_cb），不传时 worker 选号退化为主 client——全部既有
      夹具调用零改、行为逐字节一致。pool 非空时慢速采样任务随本 open
      启动（run 收尾 finally 取消）。
    返回 sess（已置 preparing 并注册）。"""
    if task["id"] in ACTIVE_SESSIONS:
        print("[!] 该任务已有下载会话，忽略重复启动")
        return None
    # T50：目录搬运中禁开新下载会话（GUI/TUI 共同防线，worker 已建作业、
    # 会话未注册的窗口由此兜底；异常经调用方既有 except 转 error 文案）
    if task["id"] in MOVING_TASKS:
        raise RuntimeError("任务目录正在移动中，暂不能开始下载")
    SESSION_TOMBSTONES.pop(task["id"], None)
    loop = asyncio.get_running_loop()
    mon = Monitor(
        planned, parallel,
        task.get("channel_title", task.get("channel", "")), False,
    )
    sess = DownloadSession(task, mon, loop)
    sess.resume_q = collections.deque()
    sess.wake = asyncio.Event()
    sess.entities = {}            # src -> entity（file_reference 刷新缓存）
    sess.fatal_error = ""         # worker/feeder 致命错误槽（首条不覆盖）
    sess.pool = pool              # T51：账号池（None=现状主 client 单号）
    sess.event_cb = event_cb      # T51：acct_hint 出口（None=no-op）
    if phase_cb is not None:
        def _notify(phase, text, index, total, _cb=phase_cb, _s=sess):
            try:
                _cb(_s.task_id, phase, text, index, total, _s.state())
            except Exception:
                pass
        mon.phase_cb = _notify
    await mon.set_phase("preparing")
    ACTIVE_SESSIONS[task["id"]] = sess
    if pool is not None:
        pool.start_sampler(sess)
    return sess


async def _close_session(task_id, phase, detail, error=""):
    """终态收口：终态 set_phase -> 快照入墓碑 -> 摘除 live 会话。"""
    sess = ACTIVE_SESSIONS.get(task_id)
    if sess is None:
        return
    await sess.mon.set_phase(phase, detail if detail else error)
    st = sess.state()
    ACTIVE_SESSIONS.pop(task_id, None)
    SESSION_TOMBSTONES[task_id] = (st, time.monotonic())
    # T21 终态收口：显式 flush 该任务（force），终态 durably 可见
    if getattr(sess, "task", None) is not None:
        save_task(sess.task, force=True)


async def run_download_session(client, task, sess, *, gui=None,
                               lazy_statuses=None):
    """执行已 open 的下载会话（T49）。返回
    {"status": "done"|"aborted"|"error", "error": str, "counters": {...}}。
    阶段时序：preparing(open 已置) -> disk_check -> (reconciling) ->
    fetching(lazy) -> downloading <=> flood_wait/refreshing -> 终态墓碑。"""
    import collections
    mon = sess.mon
    folder = pathlib.Path(task["folder"])
    planned_n = len(mon.entries)
    counters = {"ok": 0, "skipped": 0, "exists": 0, "fail": 0}
    # T22 S3：会话级 tg_config 快照（仅读可选 retries 键；无全局写入口），
    # 经 state["cfg"] 透传给 download_one，在循环内与 sess.task 同源解析
    # （选型：download_one 内经 sess.task 解析 + state 增 cfg 键，报告说明）。
    _cfg_snapshot = load_config()
    # T25：慢任务让路配置每会话快照一次（运行中改 params 不热生效，
    # 与 parallel 键现状一致）；缺省关——enabled=0 时管理器不构造、
    # 不启动，零新代码路径执行（TR-25.2 默认态与基线零差异）。
    _yield_cfg = _slow_yield_cfg(task)

    def persist(key, status, error=None):
        ti = find_item(task, key)
        if ti is None:
            return
        ti["status"] = status
        if error:
            ti["error"] = error
        else:
            ti.pop("error", None)
        # 2.3 智能重命名收口：条目登记了拟用新名且已落盘
        # （done/exists）→ 下载完成即按此命名（重名 (N) 后缀，
        # orig_name 记原名供还原；失败静默保留现名）。
        if status in ("done", "exists"):
            apply_pending_rename(task, ti)
        save_task(task, force=(status in PERSIST_FORCE_STATUSES))

    # ---- b. TUI 按键/显示任务（照现状创建：keys.start() 延后到磁盘
    #      y/N 询问之后——msvcrt 与 input 同抢控制台输入会把用户的
    #      应答字符吞掉导致误判取消，现状语义必须保留） ----
    vt = False if gui is not None else enable_vt()
    mon.vt = vt
    keys = KeyWatch()

    # ---- c. disk_check ----
    need = sum(e["total"] for e in mon.entries)
    free = shutil.disk_usage(str(folder)).free
    await mon.set_phase(
        "disk_check", f"需要约 {human(need)}，剩余 {human(free)}",
        index=0, total=planned_n)
    print(f"\n本次处理 {planned_n} 个；保存目录：{folder}")
    if need > free:
        if gui is not None:
            # GUI：不询问，直接终态失败（错误文案透传，不再静默 return）
            err = f"磁盘空间不足：需要约 {human(need)}，仅剩 {human(free)}"
            await _close_session(task["id"], "error", err)
            print(f"[GUI] 下载结束：error 磁盘空间不足 aborted=False")
            return {"status": "error", "error": err, "counters": counters}
        print(f"!! 磁盘空间不足：需要约 {human(need)}，仅剩 {human(free)}")
        if input("仍要开始下载吗？（y=继续，其他=取消）：").strip().lower() != "y":
            print("已取消")
            await _close_session(task["id"], "aborted", "已取消")
            return {"status": "aborted", "error": "", "counters": counters}

    # ---- d. reconciling（T23：会话启动前强制对账——GUI/TUI 同一
    #      chokepoint，磁盘事实优先重建后再进下载；changed=0 时静默
    #      （不打 phase 零机读行），逐条/汇总行由 reconcile_task 输出） ----
    r = reconcile_task(task)
    if r["changed"]:
        save_task(task, force=True)
        await mon.set_phase(
            "reconciling",
            f"重置 {r['reset']} 条，补登 {r['promote']} 条，"
            f"清理分片工件 {(r['markers'] + r['stale_work'])} 个")

    # 磁盘检查的交互输入结束后再启动按键监听（现状语义）
    if gui is None:
        keys.start()

    feeder_task = None
    feeder_done = lazy_statuses is None

    # ---- e. fetching（边读边下） / f. downloading（非 lazy 直接入队） ----
    if lazy_statuses is None:
        # 常规模式：记录已在内存（调用方经 sess.queue_source 传入），直接全部入队
        todo = getattr(sess, "queue_source", None) or []
        sess.records = {_rec_key(it): it for it in todo}
        sess.queue = collections.deque(todo)

        async def _prefill():
            for it in todo:
                await mon.fill(it)
        await _prefill()
        # T49 缺口 A：非 lazy 全程此前停在 disk_check，终态 done 会被转移表
        # 拒绝；_prefill 后立即切 downloading（disk_check→downloading 合法），
        # 空清单时也给出明确细节。
        await mon.set_phase(
            "downloading", "" if todo else "没有可下载条目")
    else:
        # 边读边下：后台分批拉取，取到一批补占位信息并立即入队
        mon.fetching = True
        await mon.set_phase("fetching", "", index=0, total=mon.fetch_total)

        async def _tick_fetch(done, total):
            # 执行时再查：FloodWait 分支先调 _progress 再 enter_flood，
            # 排队时 phase 仍是 fetching；flood 期间组头归 enter_flood
            # 倒计时独占，跳过本帧批进度 tick——否则首帧"等待 Ns"会在
            # 下一轮事件循环被"已取 N 条"覆盖（倒计时 2s→1s 首跳丢失）。
            if mon.phase != "flood_wait":
                await mon.tick_phase(detail=f"已取 {done}/{total} 条",
                                     index=done, total=total)

        def _progress(done, total, note=None):
            asyncio.ensure_future(
                mon.set_fetch(done, total, note or "", fetching=True))
            asyncio.ensure_future(_tick_fetch(done, total))
            if gui is None and note:
                print("  " + note)

        async def _feed():
            nonlocal feeder_done
            first_batch = True
            try:
                async for rec in pull_records(
                    client, task, lazy_statuses, progress=_progress,
                    mon=mon,
                ):
                    await mon.fill(rec)
                    sess.records[_rec_key(rec)] = rec
                    sess.queue.append(rec)
                    sess.wake.set()
                    if first_batch:
                        first_batch = False
                        await mon.set_phase(
                            "downloading",
                            f"已取 {mon.fetched} 条，开始下载")
            except asyncio.CancelledError:
                raise
            except Exception as ex:
                sess.fatal_error = (sess.fatal_error or
                                    f"读取消息信息失败："
                                    f"{type(ex).__name__}: {ex}")
                print(f"[!] {sess.fatal_error}")
            finally:
                feeder_done = True
                await mon.set_fetch(mon.fetched, mon.fetch_total,
                                    "", fetching=False)
                sess.wake.set()

        sess.records = {}
        sess.queue = collections.deque()
        feeder_task = asyncio.create_task(_feed())

    FINAL_LABEL = {"ok": "完成", "skipped": "已跳过",
                   "skip-exists": "已存在"}
    COUNT_KEY = {"ok": "ok", "skipped": "skipped",
                 "skip-exists": "exists"}
    # 下载结果 → 持久状态（注意 "ok" 的持久态是 "done"）
    PERSIST_STATUS = {"ok": "done", "skipped": "skipped",
                      "skip-exists": "exists"}

    async def worker(it):
        key = _rec_key(it)
        # T51 选号态（pool=None 即现状行为：单主号直下）
        pool = getattr(sess, "pool", None)
        acct = ACCT_MAIN
        busy_on = False
        try:
            ti = find_item(task, key)
            want = sess._want.pop(key, None)
            if ti is None or want == "remove":
                if ti is not None:
                    await sess._do_remove(key)
                return
            if want == "skip":
                counters["skipped"] += 1
                persist(key, "skipped")
                await mon.finish(key, "已跳过")
                return
            if want == "pause":
                # 还没开始就被暂停：进暂停区，不占槽位
                sess.paused[key] = it
                await mon.finish(key, "已暂停")
                return

            ctl = ItemCtl(key)
            sess.ctls[key] = ctl
            # flood_wait 等待期中止探测（T49 缺口 C）：本 worker 只安装
            # 探本条 ctl 的闭包（会话 abort 视作等效取消，CancelledError
            # 穿出 worker 由 schedule 收场）；download_one 结束后恢复进入
            # 前的槽值——后起的 worker 不再覆盖他路探测（enter_flood 在
            # 进入时捕获本路探测，捕获与还原顺序无关）。
            saved_probe = mon.flood_probe
            mon.flood_probe = (
                lambda ctl=ctl, sess=sess:
                ctl.tripped()
                or (asyncio.CancelledError if sess.abort.is_set() else None)
            )
            # 记录实际走的下载路径，暂停后恢复才能决定是否保留字节
            it["_mode"] = "parallel" if it["size"] > 4 * 1024 * 1024 \
                else "simple"

            # ---- T51 条目选号（唯一实现；每次实时读 sess.task 同一
            #      dict，运行中改偏好立即对排队条目生效） ----
            dl_client = client
            if pool is not None:
                acct = pool.select(task, key,
                                   (ti.get("acct") if ti is not None
                                    else None))
                ment = mon.order.get(key)
                if ment is not None:
                    ment["acct"] = acct
                # 持久条目归属变化才写盘一次（main→main 零写盘）
                if ti is not None and ti.get("acct") != acct:
                    if acct == ACCT_ALT:
                        ti["acct"] = ACCT_ALT
                    else:
                        ti.pop("acct", None)
                    save_task(task, force=True)
                try:
                    raw_client = await pool.aclient(acct)
                except AccountUnavailable as ex:
                    # 该号不可用（懒连接失败/未授权，已置 error）：
                    # 本条改派 main 一次，后续选号自动跳过该号
                    print(f"[acct] #{key[1]} 小号不可用（{ex}），"
                          f"本条改派主号")
                    acct = ACCT_MAIN
                    if ment is not None:
                        ment["acct"] = ACCT_MAIN
                    if ti is not None and "acct" in ti:
                        ti.pop("acct", None)
                        save_task(task, force=True)
                    raw_client = client
                # 客户端视图：FloodWait note_flood 后原样上抛（download_one
                # 严格等待——FR-9 不变）；alt 上 ChannelPrivate 转池内
                # AccountDenied（main 保持现状原样）
                dl_client = _ClientView(
                    raw_client, pool, acct,
                    deny_convert=(acct == ACCT_ALT))
                # 跨号/换号刷新引用（同号续传不刷新，保持 T49 现状）：
                # 用目标号自己的授权取新 file_reference/access_hash
                if raw_client is not client:
                    print(f"[acct] #{key[1]} 由 "
                          f"{ACCT_MARK.get(acct, acct)} 下载"
                          f"（切换账号，先刷新文件引用）")
                    try:
                        await refresh_record(raw_client, sess, it)
                    except Exception as rex:
                        print(f"[acct] #{key[1]} 引用刷新失败"
                              f"（{type(rex).__name__}），"
                              f"交由下载重试链兜底")
                pool.busy_add(acct)
                busy_on = True

            try:
                try:
                    r = await download_one(
                        dl_client, it, str(folder / it["name"]), ctl,
                            state={"monitor": mon, "session": sess,
                                   "cfg": _cfg_snapshot},
                    )
                except AccountDenied:
                    # ---- alt 无权限（ChannelPrivate）：仅一次回退 main。
                    # main 的 view 不做 deny 转换（自身无权限维持现状
                    # 重试→失败语义），故二次 AccountDenied 理论不可达；
                    # 防御性兜底按普通失败落终态。----
                    prev_acct = acct
                    pool.note_deny(prev_acct, task.get("channel"),
                                   it["msg"].id)
                    pool.acct_hint(task, "channel_private", prev_acct,
                                   ACCT_MAIN, item_msg=it["msg"].id,
                                   has_alt=True)
                    if busy_on:
                        pool.busy_sub(prev_acct)
                        busy_on = False
                    acct = ACCT_MAIN
                    ment2 = mon.order.get(key)
                    if ment2 is not None:
                        ment2["acct"] = ACCT_MAIN
                    ti2 = find_item(task, key)
                    if ti2 is not None and "acct" in ti2:
                        ti2.pop("acct", None)
                        save_task(task, force=True)
                    # main 接续 alt 的条目（跨号）：先刷新引用再重下
                    try:
                        await refresh_record(client, sess, it)
                    except Exception as rex:
                        print(f"[acct] #{key[1]} 回退主号后引用刷新失败"
                              f"（{type(rex).__name__}）")
                    print(f"[acct] #{key[1]} 小号无该频道权限，"
                          f"回退主号重下")
                    pool.busy_add(ACCT_MAIN)
                    busy_on = True
                    try:
                        r = await download_one(
                            client, it, str(folder / it["name"]), ctl,
                        state={"monitor": mon, "session": sess,
                               "cfg": _cfg_snapshot},
                        )
                    except AccountDenied:
                        r = ("fail", "AccountDenied: 主号也无权限")
            finally:
                if busy_on and pool is not None:
                    pool.busy_sub(acct)
                mon.flood_probe = saved_probe

            err = None
            if isinstance(r, tuple):
                r, err = r
            if r == "paused":
                sess.paused[key] = it
                await mon.finish(key, "已暂停")
                return
            if r == "removed":
                await sess._do_remove(key)
                return
            if r in FINAL_LABEL:
                counters[COUNT_KEY[r]] += 1
                persist(key, PERSIST_STATUS[r])
                await mon.finish(key, FINAL_LABEL[r])
            else:
                counters["fail"] += 1
                persist(key, "failed", err or "下载失败")
                await mon.finish(key, "失败")
        except asyncio.CancelledError:
            raise
        except Exception as ex:
            msg = f"worker 异常：{type(ex).__name__}: {ex}"
            print(f"[!] {msg}")
            if not sess.fatal_error:
                sess.fatal_error = msg      # 致命错误槽：首条不覆盖
        finally:
            sess.ctls.pop(key, None)
            sem.release()
            sess.wake.set()

    sem = asyncio.Semaphore(mon.parallel)   # 并发路数以 Monitor 为单一来源

    async def schedule():
        while True:
            if sess.abort.is_set():
                if feeder_task is not None:
                    feeder_task.cancel()
                for tsk in list(sess.tasks.values()):
                    tsk.cancel()
                pending = list(sess.tasks.values())
                if feeder_task is not None:
                    pending.append(feeder_task)
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
                return
            if sess.resume_q or sess.queue:
                # T57 根因 B：sem.acquire 改为与任务级 abort 竞争等待。
                # 槽位被在途条目占满时 abort 能立即抢占（原 await
                # sem.acquire() 只能等某 worker 结束释放槽位，取消要等
                # 空闲时最长整文件下完）；acquire 与 abort 同拍竞态到达
                # 时归还冗余 permit，信号量平衡不破坏。
                acq = asyncio.ensure_future(sem.acquire())
                abt = asyncio.ensure_future(sess.abort.wait())
                try:
                    await asyncio.wait({acq, abt},
                                       return_when=asyncio.FIRST_COMPLETED)
                finally:
                    if not acq.done():
                        acq.cancel()
                if sess.abort.is_set():
                    if acq.done():
                        # 竞态：槽位与 abort 同时到达，释放未用槽位
                        sem.release()
                    abt.cancel()
                    continue
                abt.cancel()
                rec = (sess.resume_q.popleft() if sess.resume_q
                       else sess.queue.popleft())
                sess.tasks[_rec_key(rec)] = asyncio.create_task(
                    worker(rec)
                )
            else:
                sess.tasks = {
                    k: t for k, t in sess.tasks.items() if not t.done()
                }
                # 队列暂时空了：工作任务全落定且消息也全部读取完才结束；
                # 否则等 feeder 取下一批或有其他唤醒
                if not sess.tasks and feeder_done:
                    return
                sess.wake.clear()
                await sess.wake.wait()
    # 控制台按键 → 会话控制。独立轮询，不依赖 schedule：
    # 所有槽位占满、调度器挂起等待时按键也能即时生效。
    async def translate_keys():
        while True:
            if keys.abort.is_set():
                keys.abort.clear()
                # T57 根因 B（TUI 同修）：中止同步置 wake，唤起被
                # wake.wait() 挂住的调度器进入循环顶取消全套
                sess.abort.set()
                if sess.wake is not None:
                    sess.wake.set()
            if keys.skip.is_set():
                keys.skip.clear()
                if sess.ctls:
                    next(iter(sess.ctls.values())).skip.set()
            if keys.pause.is_set():
                keys.pause.clear()
                if sess.ctls:
                    next(iter(sess.ctls.values())).pause.set()
            if keys.remove.is_set():
                keys.remove.clear()
                if sess.ctls:
                    next(iter(sess.ctls.values())).cancel.set()
            if keys.resume.is_set():
                keys.resume.clear()
                if sess.paused:
                    sess._begin_resume(next(iter(sess.paused)))
            await asyncio.sleep(0.15)

    translate = (
        None if gui is not None
        else asyncio.create_task(translate_keys())
    )
    display = (
        None if gui is not None
        else asyncio.create_task(mon.display_loop(keys))
    )

    # T49 阶段心跳：0.5s 节拍；fetching 每满 1s tick（距上一批 >2s 追加
    # 等待文案）、refreshing 停留 >1s 追加已用秒数；downloading/disk_check/
    # preparing 不强制（TR-49.1 分类断言）；flood_wait 由 enter_flood 倒计时。
    async def phase_heartbeat():
        last_tick = time.monotonic()
        while True:
            await asyncio.sleep(0.5)
            ph = mon.phase
            if ph in PHASE_TERMINAL:
                return
            now = time.monotonic()
            if ph == "fetching":
                if now - last_tick >= 1.0:
                    last_tick = now
                    idle = now - mon._last_fetch_ts
                    d = f"已取 {mon.fetched}/{mon.fetch_total} 条"
                    if idle > 2.0:
                        d += f" · 等待服务器 {int(idle)}s"
                    await mon.tick_phase(detail=d, index=mon.fetched,
                                         total=mon.fetch_total)
            elif ph == "refreshing":
                if now - mon.phase_entered > 1.0:
                    await mon.tick_phase(
                        detail=(f"{mon._refresh_note} · "
                                f"已用 {int(now - mon.phase_entered)}s"))
    heartbeat = asyncio.create_task(phase_heartbeat())

    # T25 慢任务让路（FR-12）：仅开启态启动巡检（随 run_download_
    # session 生命周期：收尾 finally 取消，与 T51 慢速采样/
    # phase_heartbeat 同生命周期范式）；默认关时零实例化、零
    # [yield] 输出、零计时器。
    yield_mgr = None
    if _yield_cfg["enabled"]:
        yield_mgr = SlowYieldManager(sess, _yield_cfg)
        yield_mgr.start()

    try:
        await schedule()
    finally:
        heartbeat.cancel()
        # T25：让路巡检随 run 收尾取消（退出前打 loop=exit 生命周期行）；
        # cancel 后补一拍 0 秒让出——_run 的 finally 打印落在本会话
        # 生命周期内（否则 asyncio.run 清理循环时才打，脱离调用方
        # 的 stdout 捕获窗口），行为语义零变化。
        if yield_mgr is not None:
            yield_mgr.stop()
            await asyncio.sleep(0)
        # T51：慢速采样任务随 run 收尾取消（与 open 的启动同生命周期）
        _pool = getattr(sess, "pool", None)
        if _pool is not None:
            _pool.stop_sampler()
        keys.stop()
        if translate is not None:
            translate.cancel()
        if display is not None:
            await asyncio.sleep(0.5)   # 让面板定格在最终状态
            display.cancel()

    # ---- i. 收尾判定 + 终态墓碑 ----
    if sess.abort.is_set():
        status, detail = "aborted", "未完成视频可随时继续"
    elif sess.fatal_error:
        status, detail = "error", sess.fatal_error
    else:
        status, detail = "done", ""
    await _close_session(task["id"], status, detail)

    if gui is not None:
        # GUI 模式：仅打印技术日志，界面自行刷新任务状态
        c = task_counts(task)
        print(f"[GUI] 下载结束：{c['finished']}/{c['total']}，"
              f"status={status} aborted={sess.abort.is_set()}")
        return {"status": status,
                "error": (sess.fatal_error if status == "error" else ""),
                "counters": counters}

    # 收尾：清掉面板，打印纯文本汇总
    if vt:
        sys.stdout.write("\033[H\033[J")
        sys.stdout.flush()
    else:
        os.system("cls" if os.name == "nt" else "clear")

    c = task_counts(task)
    print("=" * 50)
    print(
        f"本次：新下载 {counters['ok']} | 手动跳过 {counters['skipped']} "
        f"| 已存在 {counters['exists']} | 失败 {counters['fail']}"
    )
    bits = []
    if c["failed"]:
        bits.append(f"失败 {c['failed']}")
    if sess.abort.is_set():
        bits.append("已中止（未完成视频可随时继续）")
    if status == "error":
        # 错误文案醒目（RED；管道下 USE_COLOR=False 时退化为纯文本）
        bits.append(paint(f"发生错误：{sess.fatal_error}", RED))
    tail = "，" + "，".join(bits) if bits else ""
    print(f"任务总进度：{c['finished']}/{c['total']} 完成{tail}")
    print(f"保存位置：{folder}")
    print("=" * 50)
    return {"status": status,
            "error": (sess.fatal_error if status == "error" else ""),
            "counters": counters}


def _tui_phase_cb():
    """TUI 默认阶段回调（§3.8）：进入每阶段 print 一行（paint 着色，
    文案即 phase_text 产物，与 GUI/WS 全链同源）；flood_wait 倒计时
    每秒一行递减。GUI 模式不安装（无控制台输出面板）。
    签名为 open_download_session 的 6 参 phase_cb 契约（snap 不消费）。"""
    last = [None]

    def cb(task_id, phase, detail, index, total, snap=None):
        if phase != last[0] or phase == "flood_wait":
            last[0] = phase
            print(paint(f"〔{detail}〕", CYAN))

    return cb


async def execute_downloads(client, task, todo, parallel, gui=None,
                            lazy_statuses=None, pool=None, event_cb=None):
    """下载 todo（含消息对象的记录），每完成一个就把状态写回任务清单。

    T49 起为薄包装：build_planned -> open_download_session ->
    run_download_session（同一下载内核，GUI/TUI 无第二套循环）。
    返回 {"status": "done"|"aborted"|"error", "error": str,
    "counters": {...}}；重复启动返回 aborted 等价语义（TUI 行为不变）。

    - 已完成/已存在的成品：download_one 内部按大小跳过，不重下
    - 下到一半的：.dl + .partN.done 分片级续传，只下没下完的分片
    - 暂停：立即停在断点并释放槽位，队列里下一个视频马上开始，可随时恢复
    - 关机/强杀：清单里仍为 pending 的，重启继续即可
    gui 非空：GUI 模式（无控制台按键/无交互输入），控制走 DownloadSession。
    lazy_statuses 非空：边读边下模式（todo 可为空），按状态筛选清单，
    消息分批从服务器拉取，取到即排队开下，不再一次性等完全部消息。
    pool/event_cb（T51，可选）：透传 open_download_session（账号池与
    acct_hint 出口）；不传即现状单主号行为，既有夹具零改。
    execute_downloads 走 TUI 下载路径（GUI 的 start_download/resume_task
    直接 open+run 并自带 pool 参数）。
    """
    planned = build_planned(task, todo, lazy_statuses)
    phase_cb = _tui_phase_cb() if gui is None else None
    sess = await open_download_session(
        client, task, planned, parallel, gui=gui,
        lazy_statuses=lazy_statuses, phase_cb=phase_cb,
        pool=pool, event_cb=event_cb,
    )
    if sess is None:
        return {"status": "aborted",
                "error": "该任务已有下载会话，忽略重复启动",
                "counters": {}}
    sess.queue_source = todo        # 非 lazy 模式的内存记录（run 入队用）
    return await run_download_session(
        client, task, sess, gui=gui, lazy_statuses=lazy_statuses,
    )


async def pull_records(client, task, statuses, progress=None, mon=None):
    """续传/重试专用的分批拉取生成器：按清单消息ID逐批从服务器取消息，
    每取完一批就 yield 该批记录（保持清单顺序），供"边读边下"使用——
    不必等全部消息取完才开始下载（大清单在网络抖动/限流时可达十几分钟）。

    - 服务器上已删除/不可访问的条目标记 failed，不出现在结果里
    - 单批失败自动重试（连接错误退避；FloodWait 按服务器要求等待）
    progress(done, total, note=None)：每批结束回调（TUI/GUI 展示进度）
    mon：T49 传入时 FloodWait 走 mon.enter_flood（阶段可见+倒计时+
    取消跟随 feeder 任务）；缺省保持旧行为（纯 sleep）。
    """
    entity = await client.get_entity(task["channel"])
    wanted = [ti for ti in task["items"] if ti["status"] in statuses]

    groups = {SRC_CHANNEL: [], SRC_COMMENT: []}
    for ti in wanted:
        groups[ti.get("src", SRC_CHANNEL)].append(ti)
    discuss = None
    if groups[SRC_COMMENT]:
        discuss = await get_discuss_entity(client, entity)

    dirty = False

    async def pull_batch(ent, items):
        ids = [ti["msg"] for ti in items]
        for i in range(0, len(ids), 100):
            chunk = ids[i:i + 100]
            for attempt in range(4):
                try:
                    return await client.get_messages(ent, ids=chunk)
                except FloodWaitError as e:
                    if progress:
                        progress(0, len(wanted),
                                 f"服务器限流，等待 {e.seconds}s …")
                    if mon is not None:
                        await mon.enter_flood(e.seconds, "fetching")
                        await mon.leave_flood()
                    else:
                        await asyncio.sleep(e.seconds + 1)
                except Exception:
                    if attempt == 3:
                        raise
                    await asyncio.sleep(2 * (attempt + 1))

    def make(ti, m, src):
        nonlocal dirty
        actual_size = m.document.size or ti["size"]
        # 消息确认可取，立即把状态重置为 pending 并清错，否则下载全程
        # 任务库仍把它算作失败
        if ti["status"] != "pending" or ti.get("error"):
            ti["status"] = "pending"
            ti.pop("error", None)
            dirty = True
        if actual_size and actual_size != ti.get("size"):
            ti["size"] = actual_size
        return {
            "msg": m, "doc": m.document,
            "size": actual_size,
            "name": ti["name"], "text": "", "matched": True,
            "src": src,
        }

    done = 0
    for src, ent, items in (
        (SRC_CHANNEL, entity, groups[SRC_CHANNEL]),
        (SRC_COMMENT, discuss, groups[SRC_COMMENT]),
    ):
        if not items or ent is None:
            continue
        for i in range(0, len(items), 100):
            grp = items[i:i + 100]
            msgs = await pull_batch(ent, grp)
            remote = {m.id: m for m in msgs if m is not None}
            batch_recs = []
            for ti in grp:
                m = remote.get(ti["msg"])
                if m is None or not m.document:
                    ti["status"] = "failed"
                    ti["error"] = "消息已删除或无权访问"
                    dirty = True
                    if progress:
                        progress(0, len(wanted),
                                 f"#{ti['msg']} 已不存在，标记为失败")
                    continue
                batch_recs.append(make(ti, m, src))
            if dirty:
                save_task(task, force=True)
            done += len(grp)
            if progress:
                progress(done, len(wanted))
            for rec in batch_recs:
                yield rec


async def fetch_records(client, task, statuses):
    """一次性拉取（旧入口）：等效把 pull_records 全部收集为列表。"""
    return [r async for r in pull_records(client, task, statuses)]


# ---------------- 终端 UI 基础设施（无第三方依赖） ----------------

def _supports_color():
    try:
        if os.environ.get("NO_COLOR"):
            return False
        return bool(sys.stdout.isatty())
    except Exception:
        return False


USE_COLOR = _supports_color()


def _esc(code):
    return f"\033[{code}m" if USE_COLOR else ""


RESET = _esc(0)
BOLD = _esc(1)
DIM = _esc(2)
RED = _esc(31)
GREEN = _esc(32)
YELLOW = _esc(33)
BLUE = _esc(34)
MAGENTA = _esc(35)
CYAN = _esc(36)
GRAY = _esc(90)
BR_CYAN = _esc(96)


def paint(text, *styles):
    if not styles or not USE_COLOR:
        return str(text)
    # T56 Bug2 防御：过滤 None 样式（历史 bug：ready 行传 None 位置参数
    # 导致 join 抛 TypeError；现在即使再传 None 也不炸）
    return "".join(s for s in styles if s) + str(text) + RESET


def clear_screen():
    if USE_COLOR:
        sys.stdout.write("\033[2J\033[H")
        sys.stdout.flush()


def term_size():
    try:
        w, h = os.get_terminal_size()
        return max(20, w), max(8, h)
    except Exception:
        return 80, 24


def hr(ch="─", color=None):
    w = term_size()[0]
    line = (ch * w)[:w]
    print(paint(line, color) if color else line)


def title_bar(title, sub=""):
    w = term_size()[0]
    head = paint(" " + title + " ", BOLD, BR_CYAN)
    if sub:
        # 标题里 ANSI 不占显示宽度，显示宽度按去 ANSI 估算
        plain_len = len(title) + 2
        pad = max(1, w - plain_len - len(sub) - 1)
        print(head + " " * pad + paint(sub, DIM))
    else:
        print(head)


def ask(prompt, default=""):
    suffix = f"（回车={default}）" if default else ""
    try:
        raw = input(f"{prompt}{suffix}：").strip()
    except EOFError:
        return default
    return raw if raw else default


def ask_int(prompt, default=0, lo=None, hi=None):
    dflt = str(default) if default else ""
    for _ in range(3):
        raw = ask(prompt, dflt)
        try:
            v = int(raw)
            if (lo is not None and v < lo) or (hi is not None and v > hi):
                raise ValueError
            return v
        except ValueError:
            print(paint(" 请输入有效数字", YELLOW))
    return default


def ask_yesno(prompt, default_no=True):
    return ask(prompt, "n" if default_no else "y").lower().startswith("y")


def pause(msg="按回车键继续 …"):
    try:
        input(msg)
    except EOFError:
        pass


def choose_menu(title, options, prompt="请选择"):
    """options: [(key, 标签, 是否可用)]；返回所选 key，无效返回 None。"""
    print()
    if title:
        print(paint(title, BOLD))
    for key, label, enabled in options:
        line = f"  [{key}] {label}"
        print(line if enabled else paint(line, DIM))
    try:
        raw = input(f"{prompt}：").strip()
    except EOFError:
        return None
    for key, _label, enabled in options:
        if enabled and raw == str(key):
            return key
    print(paint(" 选择无效", YELLOW))
    return None


def paged_view(title, rows, page_mark=""):
    """rows: [(行文本)]，自动按终端高度分页；n/p 翻页，q/回车 返回。"""
    if not rows:
        print(paint("\n 没有条目", DIM))
        return
    per = max(3, term_size()[1] - 7)
    pages = (len(rows) + per - 1) // per
    page = 0
    while True:
        clear_screen()
        sub = f"{page_mark} 第{page + 1}/{pages}页".strip()
        title_bar(title, sub)
        start = page * per
        for i, line in enumerate(rows[start:start + per], start + 1):
            print(f"{i:>4} {line}")
        nav = []
        if page > 0:
            nav.append("n=上一页")
        if page < pages - 1:
            nav.append("p=下一页")
        nav.append("q=返回")
        print(paint("  " + "  ".join(nav), DIM))
        try:
            raw = input("选择：").strip().lower()
        except EOFError:
            return
        if raw in ("q", ""):
            return
        if raw == "n" and page > 0:
            page -= 1
        elif raw == "p" and page < pages - 1:
            page += 1


# ---------------- 菜单 ----------------

STATUS_COLOR = {"done": GREEN, "exists": GREEN, "pending": YELLOW,
                "failed": RED, "skipped": GRAY}
FILTER_GROUPS = [
    ("1", "全部", None), ("2", "已完成", DONE_STATUSES),
    ("3", "未下载", ("pending",)), ("4", "失败", ("failed",)),
    ("5", "跳过", ("skipped",)),
]


def _detail_row(ti, width):
    color = STATUS_COLOR.get(ti["status"], None)
    mark = paint(STATUS_MARK.get(ti["status"], "?"), color)
    src = paint(ti["src"], MAGENTA) if ti.get("src") == SRC_COMMENT else "  "
    dur = ti.get("duration") or 0
    dur_s = f"{dur // 60}:{dur % 60:02d}" if dur else "  -- "
    left = (f"{mark} {src} #{ti['msg']}  {ti.get('date', '')}  "
            f"{dur_s}  {human(ti['size']):>9}  ")
    name = trunc_width(ti["name"], max(10, width - disp_width(left) - 2))
    line = left + name
    if ti["status"] == "failed" and ti.get("error"):
        line += paint("  ✗" + trunc_width(ti["error"], 40), RED)
    return line


def filtered_items(task, statuses):
    if statuses is None:
        return list(task["items"])
    return [ti for ti in task["items"] if ti["status"] in statuses]


def show_task_detail(task):
    """带状态筛选 + 分页的视频明细，交互与 GUI 筛选标签对应。"""
    while True:
        c = task_counts(task)
        counts = {"全部": c["total"], "已完成": c["finished"],
                  "未下载": c["pending"], "失败": c["failed"],
                  "跳过": c["skipped"]}
        opts = [(k, f"{label}（{counts[label]}）", True)
                for k, label, _st in FILTER_GROUPS]
        opts.append(("0", "返回上级", True))
        key = choose_menu("视频明细 — 选择筛选", opts)
        if key in (None, "0"):
            return
        statuses = next(st for k, _lb, st in FILTER_GROUPS if k == key)
        label = next(lb for k, lb, _st in FILTER_GROUPS if k == key)
        items = filtered_items(task, statuses)
        rows = [_detail_row(ti, term_size()[0]) for ti in items]
        paged_view(f"{task['channel_title']} · {label}", rows)


def _ask_acct_pref(pool, task):
    """TUI 下载前账号偏好（§2.8）：仅当已配置 alt 时问一次；
    1=自动（默认，回车）/ 2=主号 / 3=小号。偏好写 params.acct_pref
    （仅 2/3 落盘；auto 时 pop；无小号时不问不写）。"""
    if pool is None or not pool._alt_lazy_ok():
        return
    raw = ask("本任务使用账号 1=自动（默认） 2=主号 3=小号", "1").strip()
    pref = {"1": "auto", "2": "main", "3": "alt1"}.get(raw, "auto")
    params = task.setdefault("params", {})
    if pref in ("main", "alt1"):
        params["acct_pref"] = pref
        save_task(task, force=True)
    elif "acct_pref" in params:
        params.pop("acct_pref", None)
        save_task(task, force=True)


async def resume_flow(client, task, statuses, pool=None):
    """statuses=('pending','failed') 继续全部；=('failed',) 只重试失败。"""
    n_sel = sum(1 for ti in task["items"] if ti["status"] in statuses)
    if not n_sel:
        print(paint(" 没有需要下载的视频", DIM))
        return
    stored_par = int(task.get("params", {}).get("parallel") or 1)
    parallel = ask_int("同时下载几路（1-8）", stored_par, 1, 8)
    _ask_acct_pref(pool, task)          # T51：已配置 alt 才问

    print(f"\n开始下载：共 {n_sel} 个视频，将分批从服务器核对信息，"
          f"读到即下 …")
    result = await execute_downloads(client, task, [], parallel,
                                     lazy_statuses=statuses, pool=pool,
                                     event_cb=(_tui_acct_cb()
                                               if pool is not None else None))
    if isinstance(result, dict) and result.get("status") == "error":
        print(paint(f" 下载失败：{result.get('error', '')}", RED))
    pause()


def _open_path(p):
    """跨平台“用系统默认方式打开”文件/目录；成功返回 True。"""
    try:
        if sys.platform == "win32":
            os.startfile(p)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", p])
        else:
            subprocess.Popen(["xdg-open", p])
        return True
    except Exception:
        return False


def _pick_items(task):
    """条目管理：先选范围 → 浏览分页明细 → 输入编号；返回选中的 key 列表。"""
    groups = {"1": (None, "全部条目"), "2": (("pending", "failed"), "未下载/失败"),
              "3": (("pending",), "未下载"), "4": (("failed",), "失败"),
              "5": (("skipped",), "跳过")}
    opts = [(k, label, True) for k, (_st, label) in groups.items()]
    opts.append(("0", "取消", True))
    key = choose_menu("条目管理 — 先选范围", opts)
    if key in (None, "0"):
        return []
    statuses, label = groups[key]
    items = filtered_items(task, statuses)
    if not items:
        print(paint(" 该范围没有条目", DIM))
        return []
    rows = [_detail_row(ti, term_size()[0]) for ti in items]
    paged_view(f"{task['channel_title']} · {label}", rows)
    raw = ask("输入要操作的编号（如 1,3,5-8）")
    if not raw:
        return []
    try:
        idxs = parse_excludes(raw, len(items))
    except ValueError as e:
        print(paint(f" {e}", RED))
        return []
    return [item_key(items[i - 1]) for i in idxs]


async def item_manage_flow(client, task):
    keys = _pick_items(task)
    if not keys:
        return
    opts = [
        ("1", "标记为待下载（重新下载）", True),
        ("2", "标记为跳过", True),
        ("3", "从任务清单移除（保留文件）", True),
        ("4", "从清单移除并删除本地文件", True),
        ("5", "打开所选文件", True),
        ("0", "取消", True),
    ]
    act = choose_menu(f"对 {len(keys)} 个条目执行", opts)
    if act in (None, "0"):
        return
    if act == "1":
        print(f"已修改 {set_items_status(task, keys, 'pending')} 个")
    elif act == "2":
        print(f"已修改 {set_items_status(task, keys, 'skipped')} 个")
    elif act == "3":
        print(f"已移除 {remove_items(task, keys)} 个条目")
    elif act == "4":
        if not ask_yesno("将同时删除本地文件，确定移除所选条目？", True):
            print(paint(" 已取消", DIM))
        else:
            for ti in task["items"]:
                if item_key(ti) in keys:
                    for p in glob.glob(os.path.join(task["folder"], ti["name"] + "*")):
                        try:
                            os.remove(p)
                        except OSError:
                            pass
            print(f"已移除 {remove_items(task, keys)} 个条目并删除文件")
    elif act == "5":
        n = 0
        for ti in task["items"]:
            if item_key(ti) in keys:
                p = os.path.join(task["folder"], ti["name"])
                if os.path.exists(p) and _open_path(p):
                    n += 1
        print(f"已尝试打开 {n} 个文件")
    pause()


def _tui_move_progress():
    """T50 目录搬运 TUI 进度打印器（闭包捕获节流状态）。
    文件开始帧打印 [i/n] 文件名（i 从 1 起）；跨卷分块期间按 ≥0.2s
    节流追加字节拍 [i/n] 名 已搬/总。回调异常全部吞掉。"""
    st = {"name": None, "i": 0, "t": 0.0}

    def _fmt(n):
        for unit in ("B", "KB", "MB", "GB"):
            if n < 1024 or unit == "GB":
                return f"{n:.1f}{unit}" if unit != "B" else f"{n}B"
            n /= 1024
        return f"{n}B"

    def _cb(done, total, bytes_done, bytes_total, name):
        try:
            if not name:
                return
            now = time.monotonic()
            if name != st["name"]:          # 新文件开始
                st["name"] = name
                st["i"] = done + 1
                st["t"] = now
                print(f"[{st['i']}/{total}] {name[:80]}")
                return
            if now - st["t"] >= 0.2:         # 同一文件内字节拍（跨卷分块）
                st["t"] = now
                print(f"[{st['i']}/{total}] {name[:80]}"
                      f"  {_fmt(bytes_done)}/{_fmt(bytes_total)}")
        except Exception:
            pass

    return _cb


async def task_edit_flow(client, task):
    while True:
        cur_par = int(task.get("params", {}).get("parallel") or 1)
        opts = [
            ("1", f"修改显示名称：{task['channel_title']}", True),
            ("2", f"修改默认并行：{cur_par} 路", True),
            ("3", f"修改保存目录：{task['folder']}", True),
            ("0", "返回", True),
        ]
        key = choose_menu("任务设置", opts)
        if key in (None, "0"):
            return
        if key == "1":
            rename_task(task, ask("新的显示名称", task["channel_title"]))
        elif key == "2":
            set_parallel(task, ask_int("默认并行路数（1-8）", cur_par, 1, 8))
        elif key == "3":
            newp = ask("新保存目录（绝对路径）", task["folder"])
            move = ask_yesno("把已下载文件一起搬过去", False)
            if task["id"] in ACTIVE_SESSIONS:
                print(paint(" 该任务正在下载中，无法移动目录", YELLOW))
            else:
                r = None
                try:
                    if move:
                        r = change_folder(task, newp, True,
                                          progress=_tui_move_progress())
                    else:
                        r = change_folder(task, newp, False)
                except Exception as e:
                    print(paint(f" 修改失败：{type(e).__name__}: {e}", YELLOW))
                    print(paint(" 未修改", DIM))
                if r is not None:
                    if r.get("ok"):
                        if r.get("unchanged"):
                            print("目录未变化")
                        else:
                            if move and r.get("skipped"):
                                print(paint(
                                    f"（{len(r['skipped'])} 个文件已在目标"
                                    f"目录，自动跳过）", DIM))
                            print("已修改")
                    else:
                        print(paint(" 移动失败：" + r.get("error", ""),
                                    YELLOW))
                        if r.get("moved"):
                            print(paint(
                                "（已搬 " + str(len(r["moved"])) + " 个："
                                + "、".join(r["moved"][:8])
                                + ("…" if len(r["moved"]) > 8 else "") + "）",
                                DIM))
                        for fn in r.get("pending") or []:
                            print(paint("  未搬：" + fn, YELLOW))
                        print("可再次执行，已存在文件自动跳过")
                        print(paint(" 未修改", DIM))
        pause()


async def task_menu(client, task, pool=None):
    r = reconcile_task(task)
    if r["changed"]:
        save_task(task, force=True)
        notes = []
        if r["reset"]:
            notes.append(f"{r['reset']} 条缺失/不符已重置为未下载")
        if r["promote"]:
            notes.append(f"{r['promote']} 条磁盘已有补记为已存在")
        if r["markers"] or r["stale_work"]:
            notes.append("残留分片工件已清理")
        print(paint(f"（对账：{'，'.join(notes)}）", YELLOW))

    while True:
        c = task_counts(task)
        clear_screen()
        title_bar(f"{task['channel_title']}  @{task['channel']}",
                  f"{c['finished']}/{c['total']} 完成")
        print(paint(f" 创建 {task['created']}    未下载 {c['pending']}，"
                    f"失败 {c['failed']}，跳过 {c['skipped']}", DIM))
        opts = [
            ("1", f"继续未完成（{c['unfinished']}）", bool(c["unfinished"])),
            ("2", f"仅重试失败（{c['failed']}）", bool(c["failed"])),
            ("3", "查看视频明细", True),
            ("4", "条目管理（状态/删除/打开）", True),
            ("5", "任务设置（改名/并行/目录）", True),
            ("6", "追加视频", True),
            ("7", "打开下载目录", True),
            ("8", "删除任务记录（保留视频）", True),
            ("0", "返回上级", True),
        ]
        key = choose_menu("请选择操作", opts)
        try:
            if key == "0":
                return
            elif key == "1":
                await resume_flow(client, task, ("pending", "failed"),
                                  pool=pool)
            elif key == "2":
                await resume_flow(client, task, ("failed",), pool=pool)
            elif key == "3":
                show_task_detail(task)
            elif key == "4":
                await item_manage_flow(client, task)
            elif key == "5":
                await task_edit_flow(client, task)
            elif key == "6":
                await append_flow(client, task)
            elif key == "7":
                if not _open_path(task["folder"]):
                    print(paint(" 目录不存在", YELLOW))
                pause()
            elif key == "8":
                if ask_yesno("确定删除任务记录？视频文件保留", True):
                    delete_task(task)
                    return
        except Exception as e:
            print(paint(f"\n 操作中断：{type(e).__name__}: {str(e)[:120]}", RED))
            pause()


LOGOUT_BUSY_TEXT = "有下载任务正在进行，请先中止下载后再退出登录。"


async def logout_flow(client, pool=None):
    """退出登录（T55：TUI 与 GUI logout() 语义统一）。

    - busy 拒绝：ACTIVE_SESSIONS / MOVING_TASKS 非空时提示先停止
      （沿用 GUI busy 文案）并返回 False；
    - 确认后：Telegram log_out（异常时 disconnect 兜底）→ 池清理
      （shutdown_alts 断小号连接/清运行态——复用 T51 函数，小号
      session 文件/配置保留；shutdown_main_state 清池内主号态）
      → 删主号 session 两文件（既有清理逻辑，与 GUI
      _remove_session_files 同一文件集）→ cfg 清 phone；
    - 返回 True 表示已登出，**不再 SystemExit**：调用方（settings_
      menu/main_menu/run）向上冒泡回到登录入口，可重新 send_code
      登录；退出程序仍只由菜单项 0 承担。
    """
    if ACTIVE_SESSIONS or MOVING_TASKS:
        print(paint(f" {LOGOUT_BUSY_TEXT}", YELLOW))
        return False
    if not ask_yesno("确定退出当前账号？退出后可重新接收验证码登录", True):
        return False
    try:
        await client.log_out()
    except Exception:
        try:
            await client.disconnect()
        except Exception:
            pass
    if pool is not None:
        # 复用 T51 池清理（不写第二套）：小号断连清态、文件配置保留；
        # 主号池态对称清理（连接已由上方 log_out/disconnect 语义处理）
        try:
            await pool.shutdown_alts()
        except Exception:
            pass
        try:
            pool.shutdown_main_state()
        except Exception:
            pass
    for p in (SESSION_PATH + ".session", SESSION_PATH + ".session-journal"):
        try:
            os.remove(p)
        except OSError:
            pass
    cfg = load_config()
    if "phone" in cfg:
        cfg.pop("phone", None)
        save_config(cfg)
    print(paint(" 已退出登录，返回登录入口（可重新登录）", GREEN))
    return True


async def settings_menu(client, pool=None):
    """设置菜单（T55：返回值语义）——False/None=正常返回上级；
    True=用户已退出登录（冒泡给 main_menu 回登录入口）。"""
    while True:
        cfg = load_config()
        cur_root = cfg.get("download_root") or str(DEFAULT_DOWNLOAD_ROOT)
        clear_screen()
        title_bar("设置")
        print(paint(f" 数据目录：{DATA_DIR}", DIM))
        print(paint(f" 下载目录：{cur_root}", DIM))
        print(paint(f" api_id ：{cfg.get('api_id', '(未设置)')}", DIM))
        masked = "(未设置)"
        if cfg.get("api_hash"):
            masked = cfg["api_hash"][:4] + "****" + cfg["api_hash"][-2:]
        print(paint(f" api_hash：{masked}", DIM))
        print(paint(f" 手机号 ：{cfg.get('phone', '(未设置)')}", DIM))
        opts = [
            ("1", "修改下载目录", True),
            ("2", "修改 API 凭证 / 手机号", True),
            ("3", "恢复下载目录为默认", True),
            ("4", "恢复使用内置登录凭证", True),
            ("5", "退出登录", True),
            ("0", "返回", True),
        ]
        choice = choose_menu("请选择", opts)

        if choice == "0":
            return
        elif choice == "1":
            raw = ask("新下载目录绝对路径（视频按频道名建子目录）")
            if raw:
                try:
                    p = pathlib.Path(raw).expanduser()
                    p.mkdir(parents=True, exist_ok=True)
                    cfg["download_root"] = str(p.resolve())
                    save_config(cfg)
                    print(paint(" 已保存", GREEN))
                except Exception as e:
                    print(paint(f" 目录不可用：{type(e).__name__}: {str(e)[:100]}", RED))
            pause()
        elif choice == "2":
            raw_id = ask(f"api_id", str(cfg.get("api_id", "")))
            raw_hash = ask("api_hash（4位+****+2位显示）", "")
            raw_phone = ask("手机号", cfg.get("phone", ""))
            if raw_id:
                cfg["api_id"] = int(raw_id)
            if raw_hash:
                cfg["api_hash"] = raw_hash
            if raw_phone:
                cfg["phone"] = raw_phone
            save_config(cfg)
            print(paint(" 已保存（更换凭证后若登录失效，重启会重新要求验证码）", GREEN))
            pause()
        elif choice == "3":
            cfg.pop("download_root", None)
            save_config(cfg)
            DEFAULT_DOWNLOAD_ROOT.mkdir(parents=True, exist_ok=True)
            print(paint(" 已恢复默认下载目录", GREEN))
            pause()
        elif choice == "4":
            cfg["api_id"] = BUILTIN_API_ID
            cfg["api_hash"] = BUILTIN_API_HASH
            cfg["builtin_api"] = True
            save_config(cfg)
            print(paint(" 已恢复内置凭证（重启程序后生效）", GREEN))
            pause()
        elif choice == "5":
            # T55：登出成功则冒泡 True（回登录入口）；被拒/取消时
            # 留在本菜单继续
            if await logout_flow(client, pool):
                return True


def _tui_acct_cb():
    """TUI 默认 acct_hint 回调（§2.6）：切号/回退/提示实时打印单行
    （YELLOW 着色），与 [acct] 决策日志同事件源；文本携带 seq，供
    验证与决策日志同一序号对齐。"""
    def cb(name, payload):
        try:
            if name != "acct_hint":
                return
            d = (payload or {}).get("data") or {}
            r = d.get("reason")
            seq = d.get("seq")
            mid = d.get("item_msg")
            if r == "channel_private":
                txt = f"〔回退〕seq={seq} #{mid} 小号无权限，回退主号"
            elif d.get("to_acct"):
                why = "主号限速" if r == "flood_wait" else "主号持续低速"
                txt = f"〔切号〕seq={seq} #{mid} 主号→小号（{why}）"
            else:
                txt = "〔提示〕主号持续低速，登录小号可自动分流"
            print(paint(txt, YELLOW))
        except Exception:
            pass
    return cb


ACCT_STATE_CN = {
    "ready": "就绪", "has_session": "有登录信息",
    "logged_out": "未登录", "adding": "添加中", "error": "异常",
}
ACCT_FLAG_CN = {
    "ok": "正常", "flood": "限速", "slow": "低速", "denied": "无权限频道",
}


async def account_menu(pool):
    """T51 账号管理子菜单：① 列出账号（[主]/[小]、状态/忙闲/标记/
    手机号）；② 添加小号（手机号→验证码→（需要时）2FA，错误中文可
    重试）；③ 移除小号（ask_yesno 二次确认，忙时中文拒绝）。
    全部走 pool 同一协程（TUI/GUI 无第二套逻辑）。"""
    while True:
        clear_screen()
        title_bar("账号管理")
        rows = pool.snapshot()
        for a in rows:
            st = ACCT_STATE_CN.get(a["state"], a["state"])
            flag = ACCT_FLAG_CN.get(a["flag"], a["flag"])
            me = a.get("me") or {}
            who = (a.get("phone") or me.get("phone") or "").strip()
            nm = me.get("name") or ""
            line = (f"  {ACCT_MARK[a['id']]} {nm}  {who}  {st}"
                    f"  忙 {a['busy']}  {flag}")
            # T59 §2.3：行尾追加 health/invalid 短词标记（既有字段与
            # 顺序零改动，只在行尾追加；展示同源 snapshot 可选键，
            # NFR-13 TUI 跟进）
            if a.get("phone_invalid"):
                line += "  手机号异常"
            if a.get("health") == "unhealthy":
                line += "  不健康"
            print(line if a["state"] == "ready" else paint(line, DIM))
        if not any(a["id"] == ACCT_ALT for a in rows):
            print(paint("  （无小号）", DIM))
        opts = [
            ("1", "添加小号（第二手机号登录）", True),
            ("2", "移除小号", True),
            ("0", "返回", True),
        ]
        key = choose_menu("请选择", opts)
        if key in (None, "0"):
            return
        if key == "1":
            try:
                if any(a["id"] == ACCT_ALT and a["state"] == "ready"
                       for a in rows):
                    print(paint(" 小号已存在", YELLOW))
                    pause()
                    continue
                phone = ask("小号手机号（含国家区号，不要加 +）")
                if not phone:
                    continue
                r = await pool.add_start(ACCT_ALT, phone)
                if r == "need_code":
                    code = ask("Telegram App 内收到的验证码")
                    r = await pool.add_verify_code(ACCT_ALT, code)
                if r == "need_password":
                    pw = ask("两步验证密码（该账号设置了 2FA）")
                    r = await pool.add_verify_password(ACCT_ALT, pw)
                if r == "ready":
                    print(paint(" 小号登录成功", GREEN))
            except Exception as e:
                print(paint(
                    f" 添加失败：{type(e).__name__}: {str(e)[:120]}", RED))
                print(paint(" 可重新进入本菜单再试", DIM))
            pause()
        elif key == "2":
            if not any(a["id"] == ACCT_ALT for a in pool.snapshot()):
                print(paint(" 没有小号可移除", YELLOW))
                pause()
                continue
            if ask_yesno("确定移除小号？会删除其本地登录信息", False):
                try:
                    await pool.remove_account(ACCT_ALT)
                    print(paint(" 小号已移除", GREEN))
                except AccountBusy:
                    print(paint(" 小号仍有下载任务，请先中止后再移除。",
                                YELLOW))
                except Exception as e:
                    print(paint(
                        f" 移除失败：{type(e).__name__}: {str(e)[:120]}",
                        RED))
            pause()


async def main_menu(client, pool=None):
    """主菜单（T55 返回值语义）：False=用户选 0 退出程序；
    True=已退出登录（冒泡给 run() 回登录入口，不退进程）。"""
    while True:
        tasks = load_tasks()
        clear_screen()
        title_bar("TG 视频下载器", f"{len(tasks)} 个任务")
        opts = [("1", "新建下载任务", True)]
        for i, t in enumerate(tasks, 2):
            r = reconcile_task(t)
            if r["changed"]:
                save_task(t, force=True)
            c = task_counts(t)
            if c["unfinished"]:
                state = paint(f"未完成 {c['unfinished']}（失败 {c['failed']}）", YELLOW)
            elif c["skipped"]:
                state = paint(f"已完成（另有 {c['skipped']} 个跳过）", GREEN)
            else:
                state = paint("已完成", GREEN)
            print(f"  [{i}] {t['channel_title']}  {t['created']}  "
                  f"{c['finished']}/{c['total']}  {state}")
            opts.append((str(i), t["channel_title"], True))
        opts.append(("a", "账号管理（小号分流提速）", True))
        opts.append(("s", "设置（下载目录、账号凭证）", True))
        opts.append(("0", "退出", True))
        raw = choose_menu("请选择", opts)

        if raw == "0":
            return False          # 退出程序（唯一退出路径）
        if raw and raw.lower() == "a":
            await account_menu(pool)          # None 时仅列主号（旧数据）
            continue
        if raw and raw.lower() == "s":
            if await settings_menu(client, pool):
                return True       # 已登出：冒泡，run() 回登录入口
            continue
        if raw == "1":
            try:
                await new_task_flow(client, pool=pool)
            except Exception as e:
                print(paint(f"\n 任务中断：{type(e).__name__}: {str(e)[:120]}", RED))
                pause()
            continue
        try:
            idx = int(raw) - 2
            if 0 <= idx < len(tasks):
                await task_menu(client, tasks[idx], pool=pool)
                continue
        except (ValueError, TypeError):
            pass
        print(paint(" 选择无效，请重新输入", YELLOW))
        pause()


# ---------------- 新建任务 ----------------

def _ask_float(prompt):
    raw = ask(prompt)
    try:
        return float(raw)
    except ValueError:
        return 0.0


def _ask_date(prompt):
    for _ in range(3):
        raw = ask(prompt)
        if not raw:
            return ""
        if _parse_date(raw) is not None:
            return raw
        print(paint(" 日期格式应为 YYYY-MM-DD", YELLOW))
    return ""


def gather_search_params():
    """交互式收集高级搜索参数（与 GUI 预览条件一一对应）。"""
    clear_screen()
    title_bar("搜索条件")
    params = {"query": ask("关键词（逗号分隔，词首 - 排除）")}
    mode = ask("匹配模式 all/phrase/any/fuzzy/regex", "all").lower()
    params["mode"] = mode if mode in ("phrase", "all", "any", "fuzzy", "regex") else "all"
    params["limit"] = ask_int("结果数量上限", 30, 1, 100000)
    params["min_mb"] = _ask_float("只保留大于多少 MB")
    params["max_mb"] = _ask_float("只保留小于多少 MB")
    params["date_from"] = _ask_date("起始日期 YYYY-MM-DD")
    params["date_to"] = _ask_date("截止日期 YYYY-MM-DD")
    scope = ask("搜索范围 channel/comments/both", "channel").lower()
    params["scope"] = scope if scope in ("channel", "comments", "both") else "channel"
    sort = ask("排序方式 date/size", "date").lower()
    params["sort"] = "size" if sort == "size" else "date"
    # 深度扫描开关：仅真实交互终端（TTY）提示。管道/脚本驱动（T47/T48
    # 冻结应答序、生产 stdin 重定向）不插入额外问题——该提示位于 14 行
    # 冻结序中段（sort 与 paged_view 之间），管道下提问会吃掉后续应答
    # 造成序列错位/挂死；非 TTY 缺省 deep_scan=0（GUI/web 走参数下发）。
    if sys.stdin.isatty():
        deep = ask("深度扫描（突破 5000 条扫描上限，大频道耗时显著变长）y/N",
                   "n").lower()
        params["deep_scan"] = 1 if deep in ("y", "yes") else 0
    else:
        params["deep_scan"] = 0
    return params


# T47：record.match_in → 结果行来源标记（self/缺省不插标记）
REF_SOURCE_MARKS = {"reply": "[引用]", "album": "[相册]", "fwd": "[转发]"}


class _SearchWatch:
    """T48 搜索中止监听：后台线程监听按键，q → 置位 stop（threading.Event）。

    - 真实交互终端（stdout/stdin 均 isatty）：msvcrt / termios 原始模式
      daemon 线程（KeyWatch 同款思路；不带下载那套 p/r/s/x 语义）；
    - 管道读行模式（TG_TUI_FORCE_INTERACTIVE=1 测试钩子用）：daemon
      线程阻塞读行、只认仅含 q 的行。竞态说明：读行线程可能抢先读到
      父进程随后写入的 input() 答案行——由测试父进程按输出节拍写入
      规避（先观察到搜索进行中的输出再写 q；q 行之后才写后续答案，
      此时线程已退出，不再抢读）。生产真管道场景不启用本监听
      （见 preview_records：无中途输入语义，避免吞掉后续 input() 答案）。
    """

    def __init__(self, stop):
        self.stop = stop          # threading.Event：q 置位
        self._quit = False
        self._thread = None

    def start_console(self):
        target = self._listen_win if os.name == "nt" else self._listen_posix
        self._thread = threading.Thread(target=target, daemon=True)
        self._thread.start()

    def start_pipe(self):
        self._thread = threading.Thread(target=self._listen_pipe, daemon=True)
        self._thread.start()

    def stop_listen(self):
        """置退出标志：轮询型线程随即退出；阻塞读线程读到下一行后退出。
        线程为 daemon，进程退出不受未竟读取影响。"""
        self._quit = True

    def _listen_pipe(self):
        try:
            for line in sys.stdin:
                if self._quit:
                    break
                if line.strip().lower() == "q":
                    self.stop.set()
                    break
        except Exception:
            pass

    def _listen_win(self):
        import msvcrt
        while not self._quit:
            try:
                if msvcrt.kbhit():
                    ch = msvcrt.getch().decode("latin-1", errors="ignore")
                    if ch.lower() == "q":
                        self.stop.set()
                        return
            except Exception:
                pass
            time.sleep(0.05)

    def _listen_posix(self):
        try:
            import termios, tty
            fd = sys.stdin.fileno()
            old = termios.tcgetattr(fd)
        except Exception:
            return   # 无真实终端时按键功能不可用，不影响搜索
        try:
            tty.setcbreak(fd)
            while not self._quit:
                ch = sys.stdin.read(1)
                if ch and ch.lower() == "q":
                    self.stop.set()
                    return
                if not ch:
                    time.sleep(0.1)
        except Exception:
            pass
        finally:
            try:
                termios.tcsetattr(fd, termios.TCSADRAIN, old)
            except Exception:
                pass


async def preview_records(client, entity, title, params, auto_all):
    """T48：流式预览（消费 search_videos_stream，与 GUI 作业同一内核）。

    渲染分两支：
    - 交互模式（stdout/stdin 均 isatty，或 TG_TUI_FORCE_INTERACTIVE=1
      测试钩子）：单行 \\r 覆盖式进度（阶段/已扫描/已匹配/最早日期），
      消费侧节流最少 0.2s 刷一次；首批 items 到达即逐条打印命中行；
    - 非交互模式（管道/重定向）：禁止 \\r，进度按固定节拍写独立新行
      （≥1.0s 一行 / phase 切换 / scanned 新增 ≥500）。
    中止（q）→ 打印已收条数并干净返回上一级。
    """
    interactive = ((sys.stdout.isatty() and sys.stdin.isatty())
                   or os.environ.get("TG_TUI_FORCE_INTERACTIVE") == "1")
    forced = os.environ.get("TG_TUI_FORCE_INTERACTIVE") == "1"
    print(paint(f"\n 频道：{title}，正在搜索最多 {params['limit']} 个视频 …", DIM))

    stop = threading.Event()
    watch = _SearchWatch(stop)
    if forced:
        watch.start_pipe()     # 测试钩子：交互渲染 + 管道读行监听
    elif interactive:
        watch.start_console()  # 真实交互终端：msvcrt/termios 按键
    # 真管道（未设钩子）：不启监听线程——无中途输入语义，
    # 避免吞掉后续 input() 答案行

    arrived = 0
    printed = set()
    items, meta = [], {}
    last_tick = 0.0
    last_phase = ""
    last_scanned = -1
    prev_w = 0

    def _progress_line(ev):
        line = ("搜索中 ["
                + SEARCH_PHASE_TEXT.get(ev["phase"], ev["phase"])
                + f"] 已扫描 {ev['scanned']} · 已匹配 {ev['matched']}")
        if ev["earliest"]:
            line += f" · 最早 {ev['earliest']}"
        return line + "（q=中止）"

    def _clear_inline():
        nonlocal prev_w
        if prev_w:
            sys.stdout.write("\r" + " " * prev_w + "\r")
            sys.stdout.flush()
            prev_w = 0

    try:
        async for ev in search_videos_stream(client, entity, params,
                                             cancel=stop):
            k = ev["kind"]
            if k == "progress":
                now = time.time()
                if interactive:
                    if now - last_tick >= 0.2 or ev["phase"] != last_phase:
                        line = trunc_width(_progress_line(ev),
                                           term_size()[0] - 1)
                        pad = " " * max(0, prev_w - disp_width(line))
                        sys.stdout.write("\r" + line + pad)
                        sys.stdout.flush()
                        prev_w = disp_width(line)
                        last_tick = now
                        last_phase = ev["phase"]
                elif (now - last_tick >= 1.0 or ev["phase"] != last_phase
                        or ev["scanned"] - last_scanned >= 500):
                    print(paint(_progress_line(ev), DIM))
                    last_tick = now
                    last_phase = ev["phase"]
                    last_scanned = ev["scanned"]
            elif k == "items":
                for it in ev["items"]:
                    key = (it.get("src", SRC_CHANNEL), it["msg"].id)
                    if key in printed:
                        continue
                    printed.add(key)
                    arrived += 1
                    _clear_inline()
                    m = it["msg"]
                    mark = REF_SOURCE_MARKS.get(it.get("match_in") or "", "")
                    print(paint("#" + str(m.id), CYAN)
                          + (paint(f" {mark}", CYAN) if mark else "")
                          + f"  {m.date:%Y-%m-%d}  {human(it['size']):>9}  "
                          + paint(it.get("src", ""), MAGENTA) + " "
                          + trunc_width(it["name"], 40))
            elif k == "done":
                items, meta = ev["records"], ev["meta"]
    finally:
        watch.stop_listen()
        _clear_inline()

    if stop.is_set():
        print(paint(f" 已中止搜索，已收到 {arrived} 条", YELLOW))
        return []
    # 文案分流判据由调用侧参数计算（内核 meta 键集合冻结不变）：
    # use_server 口径 = terms 非空且 mode 属服务端可搜（与内核同式）
    _terms, _ex = parse_keywords(str(params.get("query", "")))
    _use_server = (bool(_terms)
                   and str(params.get("mode", "all"))
                   in ("phrase", "all", "any"))
    _deep_scan = str(params.get("deep_scan", "")).strip().lower() in (
        "1", "true", "yes", "y", "on")
    if not items:
        if meta.get("capped"):
            print(paint(
                f" 没有找到符合条件的视频（已扫描最近 {meta.get('scan_cap')} 条消息仍触顶）",
                YELLOW))
            if _use_server:
                print(paint(
                    " 已走服务端搜索：加词已不能扩大覆盖，可填起始日期扩大扫描范围"
                    "，或放宽大小/日期条件", DIM))
            else:
                print(paint(" 可改用关键词走服务端搜索，或放宽大小/日期条件", DIM))
        else:
            print(paint(" 没有找到符合条件的视频", YELLOW))
        return []
    if meta.get("capped"):
        head = (f" 本地扫描已达 {meta.get('scan_cap')} 条上限，更早的结果未覆盖："
                if not _deep_scan else
                f" 深度扫描已触 {meta.get('scan_cap')} 条安全上限，更早的结果未覆盖：")
        if _use_server:
            print(paint(head + "已走服务端搜索，加词已不能扩大覆盖：可填起始日期扩大扫描范围", DIM))
        elif meta.get("ref_matched") and meta.get("ref_matched") >= len(items):
            # 纯外部文本命中：关键词在引用/相册/转发原文里，服务端搜索
            # 只索引消息自身文本，加词走服务端对这类命中无效
            print(paint(head + "命中均为外部文本（引用/相册/转发），加词走服务端搜索对这类命中无效：可填起始日期扩大扫描范围", DIM))
        else:
            print(paint(head + "加关键词可走服务端搜索", DIM))
    before_d = len(items)
    items = dedup_records(items)
    if len(items) < before_d:
        print(paint(f" 同名同大小去重：{before_d} → {len(items)}（保留最新）", DIM))
    # T47：外部文本（引用/相册/转发来源）命中汇总与补抓预算提示
    if meta.get("ref_matched"):
        print(paint(
            f" 外部文本命中 {meta['ref_matched']} 条（引用/相册/转发；"
            f"补抓 {meta.get('enrich_batches', 0)} 批取回 "
            f"{meta.get('enrich_fetched', 0)} 条，"
            f"缓存命中 {meta.get('enrich_cache_hits', 0)}）", DIM))
    if meta.get("truncated"):
        print(paint(
            " 外部文本补抓已达批次预算上限，更早消息的引用/相册文本可能未覆盖："
            "可填起始日期缩小扫描窗口后重试", YELLOW))
    rows = []
    for it in items:
        m = it["msg"]
        src = paint(it.get("src", ""), MAGENTA)
        mark = REF_SOURCE_MARKS.get(it.get("match_in") or "", "")
        rows.append(
            paint("#" + str(m.id), CYAN)
            + (paint(f" {mark}", CYAN) if mark else "")
            + f"  {m.date:%Y-%m-%d}  {human(it['size']):>9}  "
            + src + " " + trunc_width(it["name"], 40)
        )
    paged_view(f"找到 {len(items)} 个视频", rows)
    if auto_all:
        return items
    raw = ask("输入不要的编号（如 1,3,5-8，回车=全部下载）")
    excluded = set()
    if raw:
        try:
            excluded = parse_excludes(raw, len(items))
        except ValueError as e:
            print(paint(f" {e}", RED))
    return [it for i, it in enumerate(items, 1) if i not in excluded]


async def append_flow(client, task):
    """同频道按新条件搜索并追加条目（自动去重）。"""
    entity = await client.get_entity(task["channel"])
    params = gather_search_params()
    records, _meta = await search_videos(client, entity, params)
    added = append_records(task, records)
    dup = len(records) - added
    print(paint(f" 已追加 {added} 个新视频（{dup} 个已在清单中）", GREEN))
    pause()


async def new_task_flow(client, args=None, pool=None):
    args = args or []
    channel_in = args[0] if args else ask(
        "频道用户名（@xxx 或 t.me/xxx 链接）")

    channel_name = parse_channel(channel_in)
    try:
        entity = await client.get_entity(channel_name)
    except ValueError:
        print(paint(f" 找不到频道「{channel_name}」：用户名拼错或不是公开频道", RED))
        pause()
        return
    except Exception as e:
        print(paint(f" 解析频道失败：{type(e).__name__}: {str(e)[:120]}", RED))
        pause()
        return
    title = getattr(entity, "title", None) or channel_name

    if args:
        # 旧命令行参数兼容：channel 数量 最小MB 关键词 并行 [all]
        limit = int(args[1])
        min_mb = float(args[2]) if len(args) > 2 else 0
        kw_raw = args[3] if len(args) > 3 and args[3] != "all" else ""
        auto_all = args[-1] == "all"
        params = {"query": kw_raw, "mode": "all", "limit": limit,
                  "min_mb": min_mb, "max_mb": 0,
                  "date_from": "", "date_to": "",
                  "scope": "channel", "sort": "date"}
    else:
        auto_all = False
        params = gather_search_params()

    todo = await preview_records(client, entity, title, params, auto_all)
    if not todo:
        return

    parallel = ask_int("同时下载几路（1-8）", 1, 1, 8)
    folder = get_download_root(load_config()) / safe_name(title, channel_name)
    folder.mkdir(parents=True, exist_ok=True)

    # 建立任务清单并立即落盘：之后哪怕立刻关机，任务也已可继续。
    # 符合规则的大任务自动按 200GB/999 条拆分为多个任务。
    items = [make_task_item(it) for it in todo]
    tasks = build_task_dicts(
        channel_name, title, dict(params, parallel=parallel),
        str(folder), items,
    )
    for t in tasks:
        save_task(t, force=True)
    # T51：账号偏好问在任务落盘前（params 已并入首个任务 dict 时写键）；
    # 仅当已配置 alt 才问，无小号不问不写
    _ask_acct_pref(pool, tasks[0])
    if len(tasks) > 1:
        print(paint(f"\n 任务较大，已自动拆分为 {len(tasks)} 个任务"
                    f"（每个约 200GB、不超过 999 条），先下载第 1 个，"
                    f"其余在任务库中可随时继续。", YELLOW))
    first = tasks[0]
    first_ids = {it["msg"] for it in first["items"]}
    first_todo = [r for r in todo if r["msg"].id in first_ids]
    result = await execute_downloads(client, first, first_todo, parallel,
                                     pool=pool,
                                     event_cb=(_tui_acct_cb()
                                               if pool is not None else None))
    if isinstance(result, dict) and result.get("status") == "error":
        print(paint(f" 下载失败：{result.get('error', '')}", RED))
    if not args:
        pause()


# ---------------- 入口 ----------------

async def run():
    args = sys.argv[1:]
    setup_console_utf8()
    ensure_dirs_and_migrate()

    # T51：账号池（唯一调度内核）；T55：登录入口回路——主菜单
    # 退出登录后回到这里重新 setup_credentials + login_main_tui
    # （与 GUI 登出回登录卡对齐；退出程序只由菜单 0 承担）
    pool = AccountPool(event_cb=_tui_acct_cb())
    client = None
    try:
        while True:
            cfg = setup_credentials()
            client = await pool.login_main_tui(cfg)
            if args:
                # 命令行参数模式：直接新建任务（兼容旧用法，一次即退）
                await new_task_flow(client, args, pool=pool)
                break
            if not await main_menu(client, pool):
                break               # 菜单 0：退出程序
            # 已登出：清引用后回到循环头重新登录（login_main_tui 会
            # 重新构造 client 并 adopt；cfg.phone 已被登出清理）
            client = None
    finally:
        try:
            await pool.shutdown_alts()
        except Exception:
            pass
        if client is not None:
            await client.disconnect()


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("\n已取消，已下载内容保留，重启后可在菜单中选择该任务继续")
    except Exception as e:
        print(f"\n出错：{type(e).__name__}: {e}")
        try:
            input("按回车键退出 …")
        except EOFError:
            pass
