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
from telethon.errors import FloodWaitError

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

def parse_channel(text):
    text = text.strip()
    m = re.match(r"(?:https?://)?t\.me/([A-Za-z0-9_]{4,})", text)
    if m:
        return m.group(1)
    return text.lstrip("@")


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
    """消息 → 统一视频记录"""
    doc = msg.document
    return {
        "msg": msg, "doc": doc, "size": doc.size or 0,
        "name": pick_filename(doc, msg.id),
        "text": msg.message or "", "matched": True, "src": src,
    }


async def search_videos(client, entity, p):
    """高级视频搜索。p 支持的键：

    query       原始查询串（逗号分词，词首 - 排除）
    mode        phrase/all/any/fuzzy/regex
    limit       结果上限
    min_mb/max_mb 体积区间
    date_from/date_to  日期区间 YYYY-MM-DD（含端点）
    scope       channel/comments/both
    sort        date/size
    返回 (records, meta)。
    """
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

    async def gather(ent):
        pool = {}
        capped = False
        if use_server:
            for q in server_terms:
                async for m in client.iter_messages(ent, search=q):
                    pool[m.id] = m
        else:
            async for m in client.iter_messages(ent):
                pool[m.id] = m
                if len(pool) >= MAX_LOCAL_SCAN:
                    capped = True
                    break
        return pool, capped

    # 候选：(消息, 来源)
    cand = {}
    capped = False
    if scope in ("channel", "both"):
        pool, cap = await gather(entity)
        capped = capped or cap
        for mid, m in pool.items():
            cand[(SRC_CHANNEL, mid)] = (m, SRC_CHANNEL)
    if scope in ("comments", "both"):
        discuss = await get_discuss_entity(client, entity)
        if discuss is not None:
            pool, cap = await gather(discuss)
            capped = capped or cap
            for mid, m in pool.items():
                cand[(SRC_COMMENT, mid)] = (m, SRC_COMMENT)

    records = []
    n_small = n_large = n_video = 0
    for m, src in cand.values():
        if not is_video_message(m):
            continue
        n_video += 1
        size = m.document.size or 0
        if min_mb and size / 1048576 < min_mb:
            n_small += 1
            continue
        if max_mb and size / 1048576 > max_mb:
            n_large += 1
            continue
        if not _date_in_range(m.date, dfrom, dto):
            continue
        if not advanced_match(m.message or "", terms, mode, excludes, raw):
            continue
        records.append(make_record(m, src))

    if sort == "size":
        records.sort(key=lambda r: r["size"], reverse=True)
    else:
        records.sort(
            key=lambda r: (r["msg"].date.timestamp() if r["msg"].date else 0),
            reverse=True,
        )
    records = records[:limit]
    meta = {"n_small": n_small, "n_large": n_large,
            "n_video": n_video, "capped": capped,
            "scan_cap": MAX_LOCAL_SCAN,
            "candidates": len(cand), "mode": mode,
            "has_comments": scope in ("comments", "both")}
    return records, meta


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


class Monitor:
    """统一收集各视频字节进度、速度、状态，并按简洁/详细两种模式渲染。

    下载协程只调用 start/add/finish/error；渲染由独立的 display_loop 定时做，
    与按键线程（I 切换）互不干扰。
    """

    def __init__(self, todo, parallel, title, vt, push_cb=None):
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
        for n, it in enumerate(todo, 1):
            mid = it["msg"].id
            src = it.get("src", SRC_CHANNEL)
            e = {
                "n": n, "mid": mid, "src": src,
                "key": (src, mid),
                "total": it["size"],
                "name": it["name"],
                "date": f"{it['msg'].date:%Y-%m-%d %H:%M}" if it["msg"].date else "",
                "text": getattr(it["msg"], "message", "") or "",
                "done": 0, "status": "等待中", "speed": 0.0,
                "samples": [], "started": None, "ended": None,
                "error": "", "final": "",
            }
            self.entries.append(e)
            self.order[(src, mid)] = e
        self.active_count = 0

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
                f" 总进度 {snap['total_bytes']/(snap['total_tot'] or 1)*100:5.1f}%  "
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
        except FloodWaitError as e:
            if mon:
                await mon.error(key, f"限速等待 {e.seconds}s")
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
    os.replace(tmp, path)


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


def change_folder(task, new_path, move_files=False):
    """修改任务保存目录；move_files=True 时把已下载文件一起搬过去。
    返回 (成功, 消息列表)。"""
    new_path = os.path.abspath(new_path)
    os.makedirs(new_path, exist_ok=True)
    old = task["folder"]
    if new_path == old:
        return True, []
    msgs = []
    if move_files:
        for ti in task["items"]:
            for fn in (ti["name"], ti["name"] + ".dl"):
                srcp = os.path.join(old, fn)
                if os.path.exists(srcp):
                    try:
                        shutil.move(srcp, os.path.join(new_path, fn))
                    except Exception as e:
                        msgs.append(f"{fn}: {e}")
            # 分片标记
            for part in glob.glob(os.path.join(old, ti["name"] + ".part*.done")):
                try:
                    shutil.move(
                        part, os.path.join(new_path, os.path.basename(part))
                    )
                except Exception:
                    pass
    task["folder"] = new_path
    save_task(task)
    return True, msgs


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


def active_states():
    return [s.state() for s in ACTIVE_SESSIONS.values()]


def any_active():
    return bool(ACTIVE_SESSIONS)


def _rec_key(it):
    return (it.get("src", SRC_CHANNEL), it["msg"].id)


async def execute_downloads(client, task, todo, parallel, gui=None):
    """下载 todo（含消息对象的记录），每完成一个就把状态写回任务清单。

    - 已完成/已存在的成品：download_one 内部按大小跳过，不重下
    - 下到一半的：.dl + .partN.done 分片级续传，只下没下完的分片
    - 暂停：立即停在断点并释放槽位，队列里下一个视频马上开始，可随时恢复
    - 关机/强杀：清单里仍为 pending 的，重启继续即可
    gui 非空：GUI 模式（无控制台按键/无交互输入），控制走 DownloadSession。
    """
    if task["id"] in ACTIVE_SESSIONS:
        print("[!] 该任务已有下载会话，忽略重复启动")
        return

    folder = pathlib.Path(task["folder"])

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

    print(f"\n本次处理 {len(todo)} 个；保存目录：{folder}")

    # 磁盘空间检查（按待下视频总大小粗估，含已部分下载的，偏保守）
    need = sum(it["size"] for it in todo)
    free = shutil.disk_usage(str(folder)).free
    if need > free:
        if gui is not None:
            print("[GUI] 磁盘空间不足，未开始下载")
            return
        print(f"!! 磁盘空间不足：需要约 {human(need)}，仅剩 {human(free)}")
        if input("仍要开始下载吗？（y=继续，其他=取消）：").strip().lower() != "y":
            print("已取消")
            return

    vt = False if gui is not None else enable_vt()
    keys = KeyWatch()
    if gui is None:
        keys.start()
    mon = Monitor(
        todo, parallel,
        task.get("channel_title", task.get("channel", "")), vt
    )

    import collections
    counters = {"ok": 0, "skipped": 0, "exists": 0, "fail": 0}

    loop = asyncio.get_running_loop()
    sess = DownloadSession(task, mon, loop)
    sess.records = {_rec_key(it): it for it in todo}
    sess.queue = collections.deque(todo)
    sess.resume_q = collections.deque()
    sess.wake = asyncio.Event()
    ACTIVE_SESSIONS[task["id"]] = sess

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
            # 记录实际走的下载路径，暂停后恢复才能决定是否保留字节
            it["_mode"] = "parallel" if it["size"] > 4 * 1024 * 1024 \
                else "simple"
            r = await download_one(
                client, it, str(folder / it["name"]), ctl,
                state={"monitor": mon},
            )

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
            print(f"[!] worker 异常：{type(ex).__name__}: {ex}")
        finally:
            sess.ctls.pop(key, None)
            sem.release()
            sess.wake.set()

    sem = asyncio.Semaphore(parallel)

    async def schedule():
        while True:
            if sess.abort.is_set():
                for tsk in list(sess.tasks.values()):
                    tsk.cancel()
                if sess.tasks:
                    await asyncio.gather(
                        *sess.tasks.values(), return_exceptions=True
                    )
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
                if not sess.tasks:
                    return            # 全部落定
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
    try:
        await schedule()
    finally:
        keys.stop()
        if translate is not None:
            translate.cancel()
        if display is not None:
            await asyncio.sleep(0.5)   # 让面板定格在最终状态
            display.cancel()

    ACTIVE_SESSIONS.pop(task["id"], None)

    if gui is not None:
        # GUI 模式：仅打印技术日志，界面自行刷新任务状态
        c = task_counts(task)
        print(f"[GUI] 下载结束：{c['finished']}/{c['total']}，"
              f"aborted={sess.abort.is_set()}")
        return

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
    tail = "，" + "，".join(bits) if bits else ""
    print(f"任务总进度：{c['finished']}/{c['total']} 完成{tail}")
    print(f"保存位置：{folder}")
    print("=" * 50)


async def fetch_records(client, task, statuses):
    """续传/重试时：按清单中的消息ID从服务器重新取消息，构造下载记录。
    频道条目从频道取，评论条目（src=评论）从关联讨论组取。
    服务器上已删除/不可访问的条目标记 failed。按清单顺序返回。"""
    entity = await client.get_entity(task["channel"])
    wanted = [ti for ti in task["items"] if ti["status"] in statuses]

    groups = {SRC_CHANNEL: [], SRC_COMMENT: []}
    for ti in wanted:
        groups[ti.get("src", SRC_CHANNEL)].append(ti)

    remote = {}   # (src, id) -> msg

    async def pull(ent, items, src):
        ids = [ti["msg"] for ti in items]
        for i in range(0, len(ids), 100):
            msgs = await client.get_messages(ent, ids=ids[i:i + 100])
            for m in msgs:
                if m is not None:
                    remote[(src, m.id)] = m

    if groups[SRC_CHANNEL]:
        await pull(entity, groups[SRC_CHANNEL], SRC_CHANNEL)
    if groups[SRC_COMMENT]:
        discuss = await get_discuss_entity(client, entity)
        if discuss is not None:
            await pull(discuss, groups[SRC_COMMENT], SRC_COMMENT)

    records, missing = [], []
    reset = 0
    for ti in wanted:
        src = ti.get("src", SRC_CHANNEL)
        m = remote.get((src, ti["msg"]))
        if m is None or not m.document:
            missing.append(ti)
            continue
        actual_size = m.document.size or ti["size"]
        # 关键修复：消息确认可取，立即把状态重置为 pending 并清错，
        # 否则下载全程任务库仍把它算作失败
        if ti["status"] != "pending" or ti.get("error"):
            ti["status"] = "pending"
            ti.pop("error", None)
            reset += 1
        if actual_size and actual_size != ti.get("size"):
            ti["size"] = actual_size
        records.append({
            "msg": m, "doc": m.document,
            "size": actual_size,
            "name": ti["name"], "text": "", "matched": True,
            "src": src,
        })
    for ti in missing:
        ti["status"] = "failed"
        ti["error"] = "消息已删除或无权访问"
        print(f"  #{ti['msg']} 已不存在，标记为失败")
    if missing or reset:
        save_task(task)
    return records


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

    print(f"\n正在从服务器读取 {n_sel} 个视频的最新信息 …")
    records = await fetch_records(client, task, statuses)
    if not records:
        print(paint(" 没有可下载的视频", DIM))
        return
    await execute_downloads(client, task, records, parallel)
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
            ok, msgs = change_folder(task, newp, move)
            for m in msgs:
                print(paint(" " + m, YELLOW))
            print("已修改" if ok else paint(" 未修改", DIM))
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


async def preview_records(client, entity, title, params, auto_all):
    print(paint(f"\n 频道：{title}，正在搜索最多 {params['limit']} 个视频 …", DIM))
    items, meta = await search_videos(client, entity, params)
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
    rows = []
    for it in items:
        m = it["msg"]
        src = paint(it.get("src", ""), MAGENTA)
        rows.append(
            paint("#" + str(m.id), CYAN)
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

    # 建立任务清单并立即落盘：之后哪怕立刻关机，任务也已可继续
    task = {
        "id": new_task_id(),
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        "channel": channel_name,
        "channel_title": title,
        "params": dict(params, parallel=parallel),
        "folder": str(folder),
        "items": [make_task_item(it) for it in todo],
    }
    save_task(task)
    await execute_downloads(client, task, todo, parallel)
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
