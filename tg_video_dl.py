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
    """用户按 S 请求跳过当前视频"""


class KeyWatch:
    """后台线程监听键盘：S=跳过当前视频，Q=中止全部，I=切换简洁/详细。
    Windows 用 msvcrt，macOS/Linux 用 termios 原始模式，无需回车。"""

    def __init__(self):
        self.skip = threading.Event()
        self.abort = threading.Event()
        self.toggle = threading.Event()
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
        if use_server:
            for q in server_terms:
                async for m in client.iter_messages(ent, search=q):
                    pool[m.id] = m
        else:
            async for m in client.iter_messages(ent):
                pool[m.id] = m
                if len(pool) >= MAX_LOCAL_SCAN:
                    break
        return pool

    # 候选：(消息, 来源)
    cand = {}
    if scope in ("channel", "both"):
        for mid, m in (await gather(entity)).items():
            cand[(SRC_CHANNEL, mid)] = (m, SRC_CHANNEL)
    if scope in ("comments", "both"):
        discuss = await get_discuss_entity(client, entity)
        if discuss is not None:
            for mid, m in (await gather(discuss)).items():
                cand[(SRC_COMMENT, mid)] = (m, SRC_COMMENT)

    records = []
    n_small = n_large = 0
    for m, src in cand.values():
        if not is_video_message(m):
            continue
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
        for n, it in enumerate(todo, 1):
            mid = it["msg"].id
            e = {
                "n": n, "mid": mid, "total": it["size"],
                "name": it["name"],
                "date": f"{it['msg'].date:%Y-%m-%d %H:%M}" if it["msg"].date else "",
                "text": getattr(it["msg"], "message", "") or "",
                "done": 0, "status": "等待中", "speed": 0.0,
                "samples": [], "started": None, "ended": None,
                "error": "", "final": "",
            }
            self.entries.append(e)
            self.order[mid] = e
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
                       "skip": snap["skip"], "active": len(snap["active"])},
            "items": [
                {
                    "n": e["n"], "mid": e["mid"], "name": e["name"],
                    "date": e["date"], "size": e["total"],
                    "done": e["done"],
                    "pct": round(e["done"] * 100 / (e["total"] or 1), 1),
                    "speed": round(e["speed"], 1),
                    "status": e["status"], "error": e["error"],
                    "text": e["text"],
                }
                for e in self.entries
            ],
        }

    async def seed(self, mid, bytes_already):
        """续传：把已存在分片的字节计入初始进度（不计速度）"""
        if bytes_already <= 0:
            return
        async with self.lock:
            e = self.order[mid]
            e["done"] = min(e["total"], e["done"] + bytes_already)

    async def start(self, mid):
        async with self.lock:
            e = self.order[mid]
            if e["started"] is None:
                e["started"] = time.monotonic()
                e["status"] = "下载中"
                self.active_count += 1

    async def add(self, mid, nb):
        if nb <= 0:
            return
        now = time.monotonic()
        async with self.lock:
            e = self.order[mid]
            e["done"] = min(e["total"], e["done"] + nb)
            e["samples"].append((now, e["done"]))
            # 只保留最近约 2.5 秒的样本用于测速
            while len(e["samples"]) > 2 and now - e["samples"][0][0] > 2.5:
                e["samples"].pop(0)

    async def finish(self, mid, final):
        async with self.lock:
            e = self.order[mid]
            e["final"] = final
            e["status"] = final
            e["speed"] = 0.0
            # 完成/已存在：进度补满（部分条目可能未走 add 直接判定）
            if final in ("完成", "已存在"):
                e["done"] = e["total"]
            if e["started"] is not None and e["ended"] is None:
                e["ended"] = time.monotonic()
                self.active_count = max(0, self.active_count - 1)

    async def error(self, mid, msg):
        async with self.lock:
            e = self.order[mid]
            e["error"] = msg

    def _snapshot(self):
        now = time.monotonic()
        active, agg_speed = [], 0.0
        total_bytes = sum_e = total_tot = 0
        done_cnt = fail_cnt = skip_cnt = 0
        for e in self.entries:
            st = e["status"]
            if st == "下载中":
                sm = e["samples"]
                spd = 0.0
                if len(sm) >= 2:
                    dt = sm[-1][0] - sm[0][0]
                    if dt > 0.05:
                        spd = (sm[-1][1] - sm[0][1]) / dt
                e["speed"] = spd
                agg_speed += spd
                active.append(e)
            total_bytes += e["done"]
            total_tot += e["total"]
            if e["final"] in ("完成", "已存在"):
                done_cnt += 1
            elif e["final"] == "失败":
                fail_cnt += 1
            elif e["final"] == "已跳过":
                skip_cnt += 1
        remaining = max(0, total_tot - total_bytes)
        eta = remaining / agg_speed if agg_speed > 0 else float("inf")
        return {
            "now": now, "active": active, "agg_speed": agg_speed,
            "total_bytes": total_bytes, "total_tot": total_tot,
            "remaining": remaining, "eta": eta,
            "done": done_cnt, "fail": fail_cnt, "skip": skip_cnt,
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
                f"完成 {snap['done']} 失败 {snap['fail']} 跳过 {snap['skip']}",
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
            trunc_width(" I=切换简洁/详细   S=跳过当前   Q=中止全部", w),
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


async def parallel_download(client, item, target, skip_ev, state=None):
    """断点续传：完成的分片写一个零字节 .partN.done 标记；重试时只下
    未完成分片（未完成分片从头覆盖重下）。
    state：{'monitor': Monitor}，进度/速度全部上报到统一面板。
    """
    msg, size = item["msg"], item["size"]
    mid = msg.id
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
        await mon.seed(mid, already)
        await mon.start(mid)

    async def grab(off, ln, idx):
        mark = f"{work}.part{idx}.done"
        if os.path.exists(mark):
            return
        got = 0
        async with sem:
            if skip_ev.is_set():
                raise SkipFile
            it = client.iter_download(
                msg.media, offset=off,
                request_size=512 * 1024, file_size=size
            ).__aiter__()
            # 独立文件句柄，直写到本分片的字节区间
            with open(work, "r+b") as f:
                f.seek(off)
                while True:
                    if skip_ev.is_set():
                        raise SkipFile
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
                            await mon.add(mid, take)
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


async def simple_download(client, item, target, skip_ev, state=None):
    """小文件/未知大小：普通下载 + 停滞/跳过轮询，进度上报统一面板"""
    msg = item["msg"]
    mid = msg.id
    mon = state["monitor"] if state else None
    loop = asyncio.get_running_loop()
    prev = [0]

    if mon:
        await mon.start(mid)

    def prog(cur, total):
        delta = cur - prev[0]
        prev[0] = cur
        if mon and delta > 0:
            # 回调可能来自线程，用线程安全方式投递到事件循环
            asyncio.run_coroutine_threadsafe(mon.add(mid, delta), loop)

    async def pump():
        task = asyncio.create_task(
            client.download_media(msg, file=target, progress_callback=prog)
        )
        while not task.done():
            if skip_ev.is_set():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                raise SkipFile
            await asyncio.sleep(0.2)
        task.result()

    await asyncio.wait_for(pump(), timeout=1800)


async def download_one(client, item, target, skip_ev, state=None):
    """返回 ok / skipped / skip-exists / ('fail', reason)"""
    size = item["size"]
    mon = state["monitor"] if state else None
    mid = item["msg"].id

    if os.path.exists(target):
        if size and os.path.getsize(target) == size:
            return "skip-exists"
        try:
            os.remove(target)
        except OSError:
            pass

    for attempt in range(RETRIES):
        try:
            if size > 4 * 1024 * 1024:
                await parallel_download(client, item, target, skip_ev, state)
            else:
                await simple_download(client, item, target, skip_ev, state)
            # 无损把 moov 移到文件开头，兼容所有播放器（不重编码）
            try:
                faststart_mp4(target)
            except Exception:
                pass
            return "ok"
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
                await mon.error(mid, f"限速等待 {e.seconds}s")
            await asyncio.sleep(e.seconds + 1)
        except Exception as e:
            if attempt == RETRIES - 1:
                reason = f"{type(e).__name__}: {str(e)[:100]}"
                if mon:
                    await mon.error(mid, reason)
                return "fail", reason
            wait = [3, 6, 12][attempt]
            if mon:
                await mon.error(
                    mid, f"{type(e).__name__} → {wait}s 后重试（{attempt+1}/{RETRIES}）"
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


def append_records(task, records, status="pending"):
    """把扫描记录追加进任务（按来源+ID 去重），返回新增数"""
    exist = {item_key(ti) for ti in task["items"]}
    added = 0
    for r in records:
        key = (r.get("src", SRC_CHANNEL), r["msg"].id)
        if key in exist:
            continue
        ti = make_task_item(r)
        ti["status"] = status
        task["items"].append(ti)
        exist.add(key)
        added += 1
    if added:
        save_task(task)
    return added


# ---------------- 下载执行（新建/续传共用） ----------------

class _ActiveSession:
    """当前活动下载会话（GUI 轮询/控制用）。同一时刻只跟踪一个会话。"""

    def __init__(self):
        self.clear()

    def clear(self):
        self.mon = None
        self.task_id = None
        self.abort = None
        self.skip = None

    def is_running(self):
        return self.mon is not None

    def state(self):
        if self.mon is None:
            return None
        return self.mon.snapshot_json()


ACTIVE_SESSION = _ActiveSession()


async def execute_downloads(client, task, todo, parallel, gui=None):
    """下载 todo（含消息对象的记录），每完成一个就把状态写回任务清单。

    - 已完成/已存在的成品：download_one 内部按大小跳过，不重下
    - 下到一半的：.dl + .partN.done 分片级续传，只下没下完的分片
    - 关机/强杀：清单里仍为 pending 的，重启继续即可
    gui={'mon_cb','abort','skip'}：GUI 模式，无控制台按键/无交互输入。
    """
    folder = pathlib.Path(task["folder"])
    by_id = {ti["msg"]: ti for ti in task["items"]}

    def persist_status(msg_id, status, error=None):
        ti = by_id[msg_id]
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
            gui["abort"].set()
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

    active = []          # [(seq, event)] 按开始顺序
    active_lock = asyncio.Lock()
    counters = {"ok": 0, "skipped": 0, "exists": 0, "fail": 0}
    aborted = False
    abort_ev = gui["abort"] if gui is not None else None
    skip_ev_g = gui["skip"] if gui is not None else None

    ACTIVE_SESSION.mon = mon
    ACTIVE_SESSION.task_id = task["id"]
    ACTIVE_SESSION.abort = abort_ev
    ACTIVE_SESSION.skip = skip_ev_g

    async def worker(n, it):
        dest = str(folder / it["name"])
        ev = asyncio.Event()
        seq = n
        mid = it["msg"].id
        async with active_lock:
            active.append((seq, ev))
        try:
            r = await download_one(
                client, it, dest, ev, state={"monitor": mon}
            )
        finally:
            async with active_lock:
                if (seq, ev) in active:
                    active.remove((seq, ev))
        err = None
        if isinstance(r, tuple):
            r, err = r
        if r == "ok":
            counters["ok"] += 1
            persist_status(mid, "done")
            await mon.finish(mid, "完成")
        elif r == "skipped":
            counters["skipped"] += 1
            persist_status(mid, "skipped")
            await mon.finish(mid, "已跳过")
        elif r == "skip-exists":
            counters["exists"] += 1
            persist_status(mid, "exists")
            await mon.finish(mid, "已存在")
        else:
            counters["fail"] += 1
            persist_status(mid, "failed", err or "下载失败")
            await mon.finish(mid, "失败")

    dl_tasks = []

    async def skip_watcher():
        nonlocal aborted
        while True:
            await asyncio.sleep(0.12)
            do_abort = keys.abort.is_set() or (
                abort_ev is not None and abort_ev.is_set()
            )
            if do_abort:
                keys.abort.clear()
                aborted = True
                # 取消所有下载协程；未完成视频状态保留为 pending
                for t in dl_tasks:
                    if not t.done():
                        t.cancel()
                return
            do_skip = keys.skip.is_set() or (
                skip_ev_g is not None and skip_ev_g.is_set()
            )
            if do_skip:
                keys.skip.clear()
                if skip_ev_g is not None:
                    skip_ev_g.clear()
                async with active_lock:
                    if active:
                        _, ev = active[0]
                        ev.set()

    sem = asyncio.Semaphore(parallel)

    async def bounded(n, it):
        async with sem:
            await worker(n, it)

    dl_tasks = [
        asyncio.create_task(bounded(n, it))
        for n, it in enumerate(todo, 1)
    ]
    display = (
        None if gui is not None
        else asyncio.create_task(mon.display_loop(keys))
    )
    watcher = asyncio.create_task(skip_watcher())
    try:
        await asyncio.gather(*dl_tasks, return_exceptions=True)
    finally:
        watcher.cancel()
        keys.stop()
        if display is not None:
            await asyncio.sleep(0.5)   # 让面板定格在最终状态
            display.cancel()

    ACTIVE_SESSION.clear()

    if gui is not None:
        # GUI 模式：仅打印技术日志，界面自行刷新任务状态
        c = task_counts(task)
        print(f"[GUI] 下载结束：{c['finished']}/{c['total']}，"
              f"aborted={aborted}")
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
    if aborted:
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
    for ti in wanted:
        src = ti.get("src", SRC_CHANNEL)
        m = remote.get((src, ti["msg"]))
        if m is None or not m.document:
            missing.append(ti)
            continue
        records.append({
            "msg": m, "doc": m.document,
            "size": m.document.size or ti["size"],
            "name": ti["name"], "text": "", "matched": True,
            "src": src,
        })
    for ti in missing:
        ti["status"] = "failed"
        ti["error"] = "消息已删除或无权访问"
        print(f"  #{ti['msg']} 已不存在，标记为失败")
    if missing:
        save_task(task)
    return records


# ---------------- 菜单 ----------------

def show_task_detail(task):
    for i, ti in enumerate(task["items"], 1):
        mark = STATUS_MARK.get(ti["status"], "?")
        print(
            f" [{i:>3}] {mark} #{ti['msg']:<10} {ti.get('date', '')}  "
            f"{human(ti['size']):>9}"
        )
        if ti["status"] == "failed" and ti.get("error"):
            print(f"         原因：{ti['error'][:100]}")


async def resume_flow(client, task, statuses):
    """statuses=('pending','failed') 继续全部；=('failed',) 只重试失败。"""
    n_sel = sum(1 for ti in task["items"] if ti["status"] in statuses)
    if not n_sel:
        print("没有需要下载的视频")
        return
    stored_par = int(task.get("params", {}).get("parallel") or 1)
    raw = input(f"同时下载几路（回车={stored_par}，1-8）：").strip()
    parallel = stored_par if not raw else max(1, min(int(raw), 8))

    print(f"\n正在从服务器读取 {n_sel} 个视频的最新信息 …")
    records = await fetch_records(client, task, statuses)
    if not records:
        print("没有可下载的视频")
        return
    await execute_downloads(client, task, records, parallel)
    input("\n按回车键返回菜单 …")


async def task_menu(client, task):
    changed = reconcile_task(task)
    if changed:
        save_task(task)
        print(f"（发现 {changed} 个已完成视频本地文件缺失，已重置为未下载）")

    while True:
        c = task_counts(task)
        print("\n" + "-" * 50)
        print(f"任务：{task['channel_title']}（@{task['channel']}）")
        print(
            f"创建：{task['created']}    进度 {c['finished']}/{c['total']}"
            f"（未下载 {c['pending']}，失败 {c['failed']}，手动跳过 {c['skipped']}）"
        )
        print("-" * 50)
        if c["unfinished"]:
            extra = "（含失败视频）" if c["failed"] else ""
            print(f" [1] 继续未完成任务{extra}")
        if c["failed"]:
            print(" [2] 仅重试失败视频")
        print(" [3] 查看视频明细")
        print(" [4] 删除任务记录（不删除已下载视频）")
        print(" [0] 返回上级")
        choice = input("请选择：").strip()

        try:
            if choice == "0":
                return
            elif choice == "1" and c["unfinished"]:
                await resume_flow(client, task, ("pending", "failed"))
            elif choice == "2" and c["failed"]:
                await resume_flow(client, task, ("failed",))
            elif choice == "3":
                show_task_detail(task)
            elif choice == "4":
                if input("确定删除任务记录？（视频文件保留）输入 y 确认："
                         ).strip().lower() == "y":
                    delete_task(task)
                    print("任务记录已删除")
                    return
            else:
                print("选择无效，请重新输入")
        except Exception as e:
            print(f"\n操作中断：{type(e).__name__}: {str(e)[:120]}")


async def settings_menu(client):
    while True:
        cfg = load_config()
        cur_root = cfg.get("download_root") or str(DEFAULT_DOWNLOAD_ROOT)
        print("\n" + "-" * 50)
        print(" 设置")
        print(f" 数据目录：{DATA_DIR}")
        print(f" 下载目录：{cur_root}")
        print(f" api_id ：{cfg.get('api_id', '(未设置)')}")
        masked = "(未设置)"
        if cfg.get("api_hash"):
            masked = cfg["api_hash"][:4] + "****" + cfg["api_hash"][-2:]
        print(f" api_hash：{masked}")
        print(f" 手机号 ：{cfg.get('phone', '(未设置)')}")
        print("-" * 50)
        print(" [1] 修改下载目录")
        print(" [2] 修改 API 凭证 / 手机号")
        print(" [3] 恢复下载目录为默认（数据目录内）")
        print(" [4] 恢复使用内置登录凭证")
        print(" [0] 返回")
        choice = input("请选择：").strip()

        if choice == "0":
            return
        elif choice == "1":
            print("输入新的下载目录绝对路径（视频会按频道名在其下建子目录）")
            raw = input("新目录：").strip()
            if raw:
                try:
                    p = pathlib.Path(raw).expanduser()
                    p.mkdir(parents=True, exist_ok=True)
                    cfg["download_root"] = str(p.resolve())
                    save_config(cfg)
                    print("已保存")
                except Exception as e:
                    print(f"目录不可用：{type(e).__name__}: {str(e)[:100]}")
        elif choice == "2":
            print("直接回车表示保留原值")
            raw_id = input(f"api_id [{cfg.get('api_id','')}]：").strip()
            raw_hash = input("api_hash（回车保留）：").strip()
            raw_phone = input(f"手机号 [{cfg.get('phone','')}]：").strip()
            if raw_id:
                cfg["api_id"] = int(raw_id)
            if raw_hash:
                cfg["api_hash"] = raw_hash
            if raw_phone:
                cfg["phone"] = raw_phone
            save_config(cfg)
            print("已保存（更换凭证后若登录失效，重启会重新要求验证码）")
        elif choice == "3":
            cfg.pop("download_root", None)
            save_config(cfg)
            DEFAULT_DOWNLOAD_ROOT.mkdir(parents=True,exist_ok=True)
            print("已恢复默认下载目录")
        elif choice == "4":
            cfg["api_id"] = BUILTIN_API_ID
            cfg["api_hash"] = BUILTIN_API_HASH
            cfg["builtin_api"] = True
            save_config(cfg)
            print("已恢复内置凭证（重启程序后生效）")
        else:
            print("选择无效，请重新输入")


async def main_menu(client):
    while True:
        tasks = load_tasks()
        print("\n" + "=" * 50)
        print(" Telegram 频道视频下载器")
        print(" [1] 新建下载任务")
        for i, t in enumerate(tasks, 2):
            if reconcile_task(t):
                save_task(t)
            c = task_counts(t)
            if c["unfinished"]:
                state = f"未完成 {c['unfinished']}（失败 {c['failed']}）"
            elif c["skipped"]:
                state = f"已完成（另有 {c['skipped']} 个手动跳过）"
            else:
                state = "已完成"
            print(
                f" [{i}] {t['channel_title']}  {t['created']}  "
                f"{c['finished']}/{c['total']}  {state}"
            )
        print(" [S] 设置（下载目录、账号凭证）")
        print(" [0] 退出")
        raw = input("请选择：").strip()

        if raw == "0":
            return
        if raw.lower() == "s":
            await settings_menu(client)
            continue
        if raw == "1":
            try:
                await new_task_flow(client)
            except Exception as e:
                print(f"\n任务中断：{type(e).__name__}: {str(e)[:120]}")
            continue
        try:
            idx = int(raw) - 2
            if 0 <= idx < len(tasks):
                await task_menu(client, tasks[idx])
                continue
        except ValueError:
            pass
        print("选择无效，请重新输入")


# ---------------- 新建任务 ----------------

async def new_task_flow(client, args=None):
    args = args or []

    channel_in = args[0] if args else input(
        "频道用户名（@xxx 或 t.me/xxx 链接）："
    )
    limit = int(args[1]) if len(args) > 1 else int(input("扫描几个视频："))
    if limit <= 0:
        print("数量必须大于 0")
        return
    min_mb = float(args[2]) if len(args) > 2 else float(
        input("只保留大于多少 MB 的（回车=不限制）：").strip() or 0
    )

    channel_name = parse_channel(channel_in)
    try:
        entity = await client.get_entity(channel_name)
    except ValueError:
        print(
            f"\n找不到频道「{channel_name}」：用户名可能拼错，或该频道不是公开频道"
            f"（你输入的是 {channel_name}，是不是想输 durov？）"
        )
        return
    except Exception as e:
        print(f"\n解析频道失败：{type(e).__name__}: {str(e)[:120]}")
        return
    title = getattr(entity, "title", None) or channel_name

    auto_all = bool(args) and args[-1] == "all"
    if len(args) > 3 and args[3] != "all":
        kw_raw = args[3]
    else:
        kw_raw = input(
            "关键词过滤（逗号分隔，词首 - 排除；如 4K,预告片,-广告；回车=不过滤）："
        )
    includes, excludes_kw = parse_keywords(kw_raw)

    # 同时下载几路视频：命令行第 5 个参数；交互默认 1
    if len(args) > 4:
        parallel = int(args[4])
    else:
        parallel = int(
            input("同时下载几路视频（回车=1，建议 1-4）：").strip() or 1
        )
    parallel = max(1, min(parallel, 8))

    tips = []
    if min_mb:
        tips.append(f"≥{min_mb:g} MB")
    if includes:
        tips.append("含:" + "/".join(includes))
    if excludes_kw:
        tips.append("不含:" + "/".join(excludes_kw))
    tip = f"（{'，'.join(tips)}）" if tips else ""
    action = "正在服务端搜索" if includes else "正在扫描"
    print(f"\n频道：{title}，{action}最多 {limit} 个视频{tip} …")

    items, n_small = await scan_videos(
        client, entity, limit, min_mb, includes, excludes_kw
    )
    if not items:
        print("没有找到符合条件的视频")
        return
    if n_small:
        print(f"（已自动忽略 {n_small} 个小于 {min_mb:g} MB 的视频）")

    # 下载前清单（1 = 最新）；关键词模式下列出的全部是命中视频
    print(f"\n找到 {len(items)} 个视频：")
    for i, it in enumerate(items, 1):
        m = it["msg"]
        print(
            f"  [{i:>2}] #{m.id:<10} {m.date:%Y-%m-%d}  "
            f"{human(it['size']):>9}  {it['name']}"
        )
        shown_txt, hit_inc, hit_exc = snippet_with_hits(
            it["text"], includes, excludes_kw, width=150
        )
        if shown_txt:
            print(f"        文字：{shown_txt}")
        if hit_inc:
            print(f"        命中词：{', '.join(hit_inc)}")
        if hit_exc:
            print(f"        （排除词同时出现：{', '.join(hit_exc)}）")

    excludes = set()
    if not auto_all:
        prompt = (
            "\n输入不要的编号（如 1,3,5-8，直接回车=全部下载）："
        )
        for _ in range(3):
            try:
                extra = parse_excludes(input(prompt), len(items))
                excludes |= extra
                break
            except ValueError as e:
                print(f"  {e}，请重新输入")

    todo = [it for i, it in enumerate(items, 1) if i not in excludes]
    if not todo:
        print("全部被排除，没有要下载的内容")
        return

    folder = get_download_root(load_config()) / safe_name(title, channel_name)
    folder.mkdir(parents=True, exist_ok=True)

    # 建立任务清单并立即落盘：之后哪怕立刻关机，任务也已可继续
    task = {
        "id": new_task_id(),
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        "channel": channel_name,
        "channel_title": title,
        "params": {"limit": limit, "min_mb": min_mb,
                   "keywords": kw_raw or "", "parallel": parallel},
        "folder": str(folder),
        "items": [make_task_item(it) for it in todo],
    }
    save_task(task)
    await execute_downloads(client, task, todo, parallel)
    if not args:
        input("\n按回车键返回菜单 …")


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
