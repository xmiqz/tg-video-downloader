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
SPEED_WIN_SEC = 10.0       # 总速度滑动窗口：统计最近 10 秒下载量，每秒更新
# T50 目录搬运跨卷分块大小。依据：单搬运线程内存上限 ≤4 MiB；4 MiB 在
# 10 MB/s 慢速 U 盘上单块约 0.4s，满足作业帧 ≤500ms 节拍预算；NVMe/SSD
# 上单块数毫秒~数十毫秒，由 JobRegistry 0.30s 节流聚合；相对下载分片
# PART=8 MiB 取半，节拍更细。
MOVE_CHUNK = 4 * 1024 * 1024


# ---------------- 配置与登录 ----------------

def load_config():
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_config(cfg):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
    )


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
                               terms, mode, excludes, raw, cancel=None):
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
    ENRICH_BATCH_SIZE 个一批 get_messages；ENRICH_MAX_BATCHES 预算
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
            if batches >= ENRICH_MAX_BATCHES:
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

    terms, excludes = parse_keywords(query)
    raw = query.split(",")[0] if mode == "phrase" else query
    use_server = bool(terms) and mode in ("phrase", "all", "any")
    server_terms = [raw.strip()] if mode == "phrase" else terms

    # 指定起始日期时，日期本身就是天然的扫描停止点：允许突破
    # MAX_LOCAL_SCAN 继续向旧消息翻页，直到扫到早于 dfrom 的消息；
    # 否则频道历史超过上限时，日期符合的早期视频会被漏扫。
    scan_cap = HARD_SCAN_CAP if dfrom is not None else MAX_LOCAL_SCAN

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
        """取走待发帧：默认攒满 STREAM_FLUSH_ITEMS 出一批；force 时出清
        剩余并按 STREAM_FLUSH_ITEMS 切帧（尾帧可不满）。"""
        frames = []
        if force:
            while outbox:
                frames.append(outbox[:STREAM_FLUSH_ITEMS])
                del outbox[:STREAM_FLUSH_ITEMS]
        elif len(outbox) >= STREAM_FLUSH_ITEMS:
            frames.append(outbox[:STREAM_FLUSH_ITEMS])
            del outbox[:STREAM_FLUSH_ITEMS]
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
        agen = client.iter_messages(ent)
        async for m in agen:
            if _cancelled():
                return
            # T47 local_scan 同序：入池 → 判定 → dfrom → cap
            pool[m.id] = m
            judged_ids.add(m.id)
            _after_judge(_judge(m, src), m, src)
            yield _progress("local_scan")
            for frame in _flush():
                yield {"kind": "items", "items": frame}
            if dfrom is not None and m.date is not None and m.date < dfrom:
                break   # 已覆盖全部目标时间段，不算触顶
            if len(pool) >= scan_cap:
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
            terms, mode, excludes, raw, cancel=cancel)
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
        phase_detail 存 phase_text 拼装产物（全链同源文案）。"""
        if phase not in PHASE_LABELS:
            raise AssertionError(f"未知下载阶段：{phase!r}")
        async with self.lock:
            old = self.phase
            if old == phase and not detail:
                # 同阶段重申（open 的 preparing 首帧）：仍发一次通知
                self._fire_phase_cb(phase, self.phase_detail,
                                    self.phase_index, self.phase_total)
                return
            if old in PHASE_TERMINAL:
                return                      # 终态不可再变（幂等）
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
            lines.append(f"[{e['n']}] 消息 #{e['mid']}   {e['status']}")
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
        async with sem:
            tripped = ctl.tripped()
            if tripped:
                raise tripped
            it = client.iter_download(
                msg.media, offset=off,
                request_size=512 * 1024, file_size=size
            ).__aiter__()
            # 独立文件句柄，直写到本分片的字节区间
            with open(work, "r+b") as f:
                f.seek(off)
                while True:
                    tripped = ctl.tripped()
                    if tripped:
                        raise tripped
                    try:
                        chunk = await asyncio.wait_for(
                            it.__anext__(), timeout=STALL_SECS
                        )
                    except StopAsyncIteration:
                        break
                    take = min(len(chunk), ln - got)
                    if take > 0:
                        f.write(chunk[:take])
                        got += take
                        if mon:
                            await mon.add(key, take)
                    if got >= ln:
                        break
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
        while not task.done():
            tripped = ctl.tripped()
            if tripped:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                raise tripped
            await asyncio.sleep(0.2)
        task.result()

    await asyncio.wait_for(pump(), timeout=1800)


async def refresh_record(client, sess, item):
    """file_reference 过期刷新：按条目来源重取该条消息并**原地**更新
    item/rec 的 msg/doc/size（sess.records 与队列中的是同一对象引用），
    任务清单持久条目 size 同步（make_task_item 字段集不变，无 phase 键）。
    消息已删除/无文档按确定性错误抛 RuntimeError。返回新 msg。"""
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
                raise RuntimeError("评论组不可用，无法刷新引用")
        sess.entities[src] = ent
    m = await client.get_messages(ent, ids=item["msg"].id)
    if isinstance(m, list):
        m = m[0] if m else None
    if m is None or not getattr(m, "document", None):
        raise RuntimeError(f"#{item['msg'].id} 消息已删除或无文档")
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
    """返回 ok / skipped / skip-exists / paused / removed / ('fail', reason)"""
    size = item["size"]
    mon = state["monitor"] if state else None
    key = (item.get("src", SRC_CHANNEL), item["msg"].id)

    if os.path.exists(target):
        if size and os.path.getsize(target) == size:
            return "skip-exists"
        try:
            os.remove(target)
        except OSError:
            pass

    for attempt in range(RETRIES):
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
            # 文件引用过期（T49）：刷新后重试，不计入 3/6/12 退避次数；
            # 刷新失败回 downloading 并按普通异常分类退避（文案透传）。
            sess = (state or {}).get("session")
            if mon:
                await mon.set_phase("refreshing", f"#{item['msg'].id}")
            try:
                if sess is None:
                    raise RuntimeError("会话缺失，无法刷新文件引用")
                await refresh_record(client, sess, item)
            except Exception as rexc:
                if mon:
                    await mon.set_phase(
                        "downloading",
                        f"引用刷新失败 {type(rexc).__name__}")
                raise
            if mon:
                await mon.set_phase("downloading", "引用已刷新")
        except FloodWaitError as e:
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
                await asyncio.sleep(e.seconds + 1)
        except Exception as e:
            if attempt == RETRIES - 1:
                reason = f"{type(e).__name__}: {str(e)[:100]}"
                if mon:
                    await mon.error(key, reason)
                return "fail", reason
            wait = [3, 6, 12][attempt]
            if mon:
                await mon.error(
                    key, f"{type(e).__name__} → {wait}s 后重试（{attempt+1}/{RETRIES}）"
                )
            await asyncio.sleep(wait)
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


def save_task(task):
    """原子写入任务清单：先写 .tmp 再改名，断电也不会写坏"""
    TASK_ROOT.mkdir(parents=True, exist_ok=True)
    path = task_file_path(task)
    tmp = str(path) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(task, f, ensure_ascii=False, indent=2)
    # Windows：目标文件被并发读（server 轮询/任务库渲染正打开同一 json）
    # 时 os.replace 报拒绝访问；短退避重试即可恢复，用尽仍失败才上抛。
    for _try in range(12):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if _try == 11:
                raise
            time.sleep(0.025)


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


def reconcile_task(task):
    """对账本地文件：标记完成但成品缺失/大小不符的，改回 pending。
    返回重置条数（.dl 分片续传不在这里处理）。"""
    folder = pathlib.Path(task["folder"])
    changed = 0
    for ti in task["items"]:
        if ti["status"] in DONE_STATUSES:
            p = folder / ti["name"]
            ok = p.exists() and (
                not ti.get("size") or p.stat().st_size == ti["size"]
            )
            if not ok:
                ti["status"] = "pending"
                ti.pop("error", None)
                changed += 1
    return changed


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
        save_task(task)


def set_parallel(task, n):
    n = max(1, min(int(n), 8))
    task.setdefault("params", {})["parallel"] = n
    save_task(task)


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
        save_task(task)
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
        save_task(task)
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
    save_task(task)
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
        save_task(task)
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
        save_task(task)


def remove_items(task, keys):
    """从任务清单删除条目（不删本地文件），返回删除数"""
    ks = set(keys)
    before = len(task["items"])
    task["items"] = [
        ti for ti in task["items"] if item_key(ti) not in ks
    ]
    save_task(task)
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
        save_task(task)
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

    def request_abort(self):
        if not self.loop.is_closed():
            self.loop.call_soon_threadsafe(self.abort.set)

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

    def _begin_resume(self, key):
        rec = self.paused.pop(key, None) or self.records.get(key)
        if rec is None:
            return
        self._want.pop(key, None)

        async def _do():
            ti = find_item(self.task, key)
            if ti is not None and ti["status"] != "pending":
                ti["status"] = "pending"
                ti.pop("error", None)
                save_task(self.task)
            mode = rec.get("_mode") or (
                "parallel" if rec["size"] > 4 * 1024 * 1024
                else "simple")
            await self.mon.restart(key, keep_done=mode != "simple")
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
            save_task(self.task)
        await self.mon.remove(key)
        self.wake.set()

    def state(self):
        snap = self.mon.snapshot_json()
        c = task_counts(self.task)
        snap["task_id"] = self.task_id
        snap["task_total"] = c["total"]
        snap["task_finished"] = c["finished"]
        return snap


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
                                lazy_statuses=None, phase_cb=None):
    """纯内存打开下载会话（T49）：构造 Monitor/DownloadSession 并以
    preparing 阶段注册进 ACTIVE_SESSIONS——不做任何磁盘/网络/阻塞
    调用，GUI 点"继续"后轮询立刻可见 preparing（≤200ms 预算的关键）。

    - task_id 已有 live 会话：打印既有提示并返回 None（调用方收口）。
    - 同 id 墓碑无条件清除后重建（中止后可立即再继续，不等 TTL）。
    - phase_cb：6 参契约 (task_id, phase, detail, index, total, snap)，
      内部包装成 Monitor 的 4 参 phase_cb；回调异常只记不抛。
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
    if phase_cb is not None:
        def _notify(phase, text, index, total, _cb=phase_cb, _s=sess):
            try:
                _cb(_s.task_id, phase, text, index, total, _s.state())
            except Exception:
                pass
        mon.phase_cb = _notify
    await mon.set_phase("preparing")
    ACTIVE_SESSIONS[task["id"]] = sess
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


async def run_download_session(client, task, sess, *, gui=None,
                               lazy_statuses=None, do_reconcile=False):
    """执行已 open 的下载会话（T49）。返回
    {"status": "done"|"aborted"|"error", "error": str, "counters": {...}}。
    阶段时序：preparing(open 已置) -> disk_check -> (reconciling) ->
    fetching(lazy) -> downloading <=> flood_wait/refreshing -> 终态墓碑。"""
    import collections
    mon = sess.mon
    folder = pathlib.Path(task["folder"])
    planned_n = len(mon.entries)
    counters = {"ok": 0, "skipped": 0, "exists": 0, "fail": 0}

    def persist(key, status, error=None):
        ti = find_item(task, key)
        if ti is None:
            return
        ti["status"] = status
        if error:
            ti["error"] = error
        else:
            ti.pop("error", None)
        save_task(task)

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

    # ---- d. reconciling（仅显式 do_reconcile=True；GUI 永不传，
    #      TUI 生产入口也不传，对账时机仍是 task_menu 入口） ----
    if do_reconcile:
        changed = reconcile_task(task)
        if changed > 0:
            save_task(task)
            await mon.set_phase("reconciling", f"重置 {changed} 条缺失记录")

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
            try:
                r = await download_one(
                    client, it, str(folder / it["name"]), ctl,
                    state={"monitor": mon, "session": sess},
                )
            finally:
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
                await sem.acquire()
                if sess.abort.is_set():
                    sem.release()
                    continue
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
                sess.abort.set()
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

    try:
        await schedule()
    finally:
        heartbeat.cancel()
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
                            lazy_statuses=None):
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
    """
    planned = build_planned(task, todo, lazy_statuses)
    phase_cb = _tui_phase_cb() if gui is None else None
    sess = await open_download_session(
        client, task, planned, parallel, gui=gui,
        lazy_statuses=lazy_statuses, phase_cb=phase_cb,
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
                save_task(task)
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
    return "".join(styles) + str(text) + RESET


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


async def resume_flow(client, task, statuses):
    """statuses=('pending','failed') 继续全部；=('failed',) 只重试失败。"""
    n_sel = sum(1 for ti in task["items"] if ti["status"] in statuses)
    if not n_sel:
        print(paint(" 没有需要下载的视频", DIM))
        return
    stored_par = int(task.get("params", {}).get("parallel") or 1)
    parallel = ask_int("同时下载几路（1-8）", stored_par, 1, 8)

    print(f"\n开始下载：共 {n_sel} 个视频，将分批从服务器核对信息，"
          f"读到即下 …")
    result = await execute_downloads(client, task, [], parallel,
                                     lazy_statuses=statuses)
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


async def task_menu(client, task):
    changed = reconcile_task(task)
    if changed:
        save_task(task)
        print(paint(f"（{changed} 个已完成视频本地文件缺失，已重置为未下载）",
                    YELLOW))

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
                await resume_flow(client, task, ("pending", "failed"))
            elif key == "2":
                await resume_flow(client, task, ("failed",))
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
                if ask_yesno("确定删除任务记录？视频文件保留", False):
                    delete_task(task)
                    return
        except Exception as e:
            print(paint(f"\n 操作中断：{type(e).__name__}: {str(e)[:120]}", RED))
            pause()


async def logout_flow(client):
    """退出登录：销毁授权、删除会话，随后必须重启程序。"""
    if not ask_yesno("确定退出当前账号？下次启动需重新接收验证码", False):
        return
    try:
        await client.log_out()
    except Exception:
        try:
            await client.disconnect()
        except Exception:
            pass
    for p in (SESSION_PATH + ".session", SESSION_PATH + ".session-journal"):
        try:
            os.remove(p)
        except OSError:
            pass
    print(paint(" 已退出登录，请关闭窗口后重新启动程序", GREEN))
    pause()
    raise SystemExit(0)


async def settings_menu(client):
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
            await logout_flow(client)


async def main_menu(client):
    while True:
        tasks = load_tasks()
        clear_screen()
        title_bar("TG 视频下载器", f"{len(tasks)} 个任务")
        opts = [("1", "新建下载任务", True)]
        for i, t in enumerate(tasks, 2):
            if reconcile_task(t):
                save_task(t)
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
        opts.append(("s", "设置（下载目录、账号凭证）", True))
        opts.append(("0", "退出", True))
        raw = choose_menu("请选择", opts)

        if raw == "0":
            return
        if raw and raw.lower() == "s":
            await settings_menu(client)
            continue
        if raw == "1":
            try:
                await new_task_flow(client)
            except Exception as e:
                print(paint(f"\n 任务中断：{type(e).__name__}: {str(e)[:120]}", RED))
                pause()
            continue
        try:
            idx = int(raw) - 2
            if 0 <= idx < len(tasks):
                await task_menu(client, tasks[idx])
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
    if not items:
        if meta.get("capped"):
            print(paint(
                f" 没有找到符合条件的视频（已扫描最近 {meta.get('scan_cap')} 条消息仍触顶）",
                YELLOW))
            print(paint(" 可改用关键词走服务端搜索，或放宽大小/日期条件", DIM))
        else:
            print(paint(" 没有找到符合条件的视频", YELLOW))
        return []
    if meta.get("capped"):
        print(paint(
            f" 本地扫描已达 {meta.get('scan_cap')} 条上限，更早的结果未覆盖："
            "加关键词可走服务端搜索", DIM))
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
            " 外部文本补抓已达批次预算上限，更早消息的引用/相册文本可能未覆盖",
            YELLOW))
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


async def new_task_flow(client, args=None):
    args = args or []
    channel_in = args[0] if args else ask(
        "频道用户名（@xxx 或 t.me/xxx 链接）")

    channel_name = parse_channel(channel_in)
    try:
        entity = await client.get_entity(channel_name)
    except ValueError:
        print(paint(f" 找不到频道「{channel_name}」：用户名拼错或不是公开频道", RED))
        return
    except Exception as e:
        print(paint(f" 解析频道失败：{type(e).__name__}: {str(e)[:120]}", RED))
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
        save_task(t)
    if len(tasks) > 1:
        print(paint(f"\n 任务较大，已自动拆分为 {len(tasks)} 个任务"
                    f"（每个约 200GB、不超过 999 条），先下载第 1 个，"
                    f"其余在任务库中可随时继续。", YELLOW))
    first = tasks[0]
    first_ids = {it["msg"] for it in first["items"]}
    first_todo = [r for r in todo if r["msg"].id in first_ids]
    result = await execute_downloads(client, first, first_todo, parallel)
    if isinstance(result, dict) and result.get("status") == "error":
        print(paint(f" 下载失败：{result.get('error', '')}", RED))
    if not args:
        pause()


# ---------------- 入口 ----------------

async def run():
    args = sys.argv[1:]
    setup_console_utf8()
    ensure_dirs_and_migrate()
    cfg = setup_credentials()

    client = TelegramClient(
        SESSION_PATH, cfg["api_id"], cfg["api_hash"], flood_sleep_threshold=120
    )
    print("\n正在登录 Telegram …（首次需输入 Telegram App 收到的验证码）")
    try:
        await client.start(phone=lambda: cfg["phone"])
    except Exception as e:
        # 内置凭证失效时，换成用户自己的凭证重试一次
        if cfg.get("builtin_api") and type(e).__name__ in (
            "ApiIdInvalidError", "ApiIdPublishedFloodError", "PhoneNumberBannedError"
        ):
            await client.disconnect()
            prompt_manual_api(cfg)
            client = TelegramClient(
                SESSION_PATH, cfg["api_id"], cfg["api_hash"],
                flood_sleep_threshold=120
            )
            await client.start(phone=lambda: cfg["phone"])
        else:
            raise
    me = await client.get_me()
    print(f"已登录：{me.first_name or ''} @{me.username or ''}")

    try:
        if args:
            # 命令行参数模式：直接新建任务（兼容旧用法）
            await new_task_flow(client, args)
        else:
            await main_menu(client)
    finally:
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
