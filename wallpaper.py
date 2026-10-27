"""Bing photo-of-the-day wallpaper, cached under the data directory.

Why this lives on the backend instead of localStorage: the desktop shell
starts the HTTP server on port 0 (server_app.start), so the page origin is
127.0.0.1:<random> and changes on every launch. localStorage is partitioned
by origin, so a browser-side cache is wiped each restart. The data directory
is stable for both the desktop shell and the container and is already a
mounted volume, so caching here survives restarts and also works offline.

Update cadence (measured 2026-10-26): Bing publishes one image per day and
the archive reports ``fullstartdate`` as ``YYYYMMDD1600`` UTC, i.e. exactly
00:00 Asia/Shanghai. So freshness is decided by comparing the archive's
date with the cached one, not by a fixed TTL - that keeps the wallpaper in
step with Bing's own switchover.

Nothing here talks to Telegram or touches user credentials.
"""
from __future__ import annotations

import json
import os
import pathlib
import threading
import time
import urllib.error
import urllib.request

# The official archive endpoint. Calling it from the backend sidesteps CORS
# entirely (CORS only constrains browsers), which is why the earlier
# bing.biturl.top detour is no longer needed: that mirror also returned
# unreliable history indexes (it skipped days).
FEED_URL = ("https://www.bing.com/HPImageArchive.aspx"
            "?format=js&idx={idx}&n={n}&mkt={mkt}")
FEED_N = 8              # archive returns up to 8 entries per request
FEED_TTL = 30 * 60      # re-poll the archive at most this often
FETCH_TIMEOUT = 8.0
MAX_BYTES = 8 * 1024 * 1024
MAX_HISTORY = 60        # "save history" cap: ~60 days

CACHE_DIRNAME = "cache"
INDEX_NAME = "wallpaper.json"


def _cache_dir(data_dir):
    return pathlib.Path(data_dir) / CACHE_DIRNAME


def _index_path(data_dir):
    return _cache_dir(data_dir) / INDEX_NAME


def _read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _http_get(url, timeout=FETCH_TIMEOUT, limit=MAX_BYTES):
    req = urllib.request.Request(url, headers={"User-Agent": "tg-video-downloader"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        if resp.status != 200:
            raise RuntimeError(f"HTTP {resp.status}")
        data = resp.read(limit + 1)
    if len(data) > limit:
        raise RuntimeError("响应过大")
    return data


def _ext_of(url):
    low = str(url or "").lower()
    for ext in (".jpg", ".jpeg", ".png", ".webp"):
        if ext in low:
            return ".jpg" if ext == ".jpeg" else ext
    return ".jpg"


def _abs_url(u):
    """The archive returns site-relative paths like /th?id=..."""
    u = str(u or "")
    if u.startswith("/"):
        return "https://www.bing.com" + u
    return u


def parse_feed(payload):
    """Normalize the archive payload into dated entries (newest first)."""
    images = payload.get("images") if isinstance(payload, dict) else None
    out = []
    for img in images or []:
        if not isinstance(img, dict):
            continue
        url = _abs_url(img.get("url"))
        if not url:
            continue
        out.append({
            "date": str(img.get("startdate") or ""),
            "fullstartdate": str(img.get("fullstartdate") or ""),
            "url": url,
            "copyright": str(img.get("copyright") or ""),
            "copyright_link": str(img.get("copyrightlink") or ""),
            "title": str(img.get("title") or ""),
        })
    out.sort(key=lambda e: e["date"], reverse=True)
    return out


class WallpaperCache:
    """Thread-safe wallpaper cache; one instance per Api."""

    def __init__(self, data_dir, *, now=time.time, fetcher=None, mkt="zh-CN"):
        self.data_dir = pathlib.Path(data_dir)
        self._now = now
        self._fetch = fetcher or (lambda url: _http_get(url))
        # Kept as an instance attribute so tests (and future callers) can
        # substitute the feed poll without touching the network.
        self._http = _http_get
        self._mkt = mkt
        # Reentrant: refresh()/select()/import_image() hold the lock and then
        # call status() to build their reply. A plain Lock deadlocks there.
        self._lock = threading.RLock()
        self._index = None

    # ---------- index ----------
    def _load(self):
        if self._index is None:
            data = _read_json(_index_path(self.data_dir)) or {}
            if not isinstance(data, dict):
                data = {}
            data.setdefault("current", None)
            data.setdefault("feed", [])
            data.setdefault("feed_ts", 0)
            data.setdefault("history", [])
            if not isinstance(data["feed"], list):
                data["feed"] = []
            if not isinstance(data["history"], list):
                data["history"] = []
            self._index = data
        return self._index

    def _save(self):
        _write_json(_index_path(self.data_dir), self._load())

    def _file_of(self, entry):
        name = str(entry.get("file") or "")
        if not name:
            return None
        path = _cache_dir(self.data_dir) / name
        return path if path.is_file() else None

    def _entry_view(self, entry):
        # Uploaded backgrounds carry no remote url, only a local file, so the
        # guard must accept either source.
        if not isinstance(entry, dict):
            return None
        if not entry.get("url") and not entry.get("file"):
            return None
        path = self._file_of(entry)
        return {
            "date": str(entry.get("date") or ""),
            "url": str(entry.get("url") or ""),
            "copyright": str(entry.get("copyright") or ""),
            "copyright_link": str(entry.get("copyright_link") or ""),
            "title": str(entry.get("title") or ""),
            "file": str(entry.get("file") or ""),
            "local": path is not None,
        }

    # ---------- feed ----------
    def _feed_fresh(self, index):
        return (self._now() - float(index.get("feed_ts") or 0)) < FEED_TTL

    def _fetch_feed(self, index):
        payload = json.loads(
            self._http(FEED_URL.format(idx=0, n=FEED_N, mkt=self._mkt))
        )
        entries = parse_feed(payload)
        if not entries:
            raise RuntimeError("壁纸接口返回空列表")
        index["feed"] = entries
        index["feed_ts"] = self._now()
        return entries

    # ---------- download ----------
    def _ensure_file(self, entry):
        """Download an entry's image if it is not on disk. Caller holds lock."""
        path = self._file_of(entry)
        if path is not None:
            return True
        blob = self._fetch(entry["url"])
        name = f"bing-{entry['date'] or int(self._now())}{_ext_of(entry['url'])}"
        cdir = _cache_dir(self.data_dir)
        cdir.mkdir(parents=True, exist_ok=True)
        (cdir / name).write_bytes(blob)
        entry["file"] = name
        return True

    def _adopt(self, index, entry):
        index["current"] = {
            "date": entry.get("date", ""),
            "url": entry.get("url", ""),
            "copyright": entry.get("copyright", ""),
            "copyright_link": entry.get("copyright_link", ""),
            "title": entry.get("title", ""),
            "file": entry.get("file", ""),
            "ts": self._now(),
        }
        if index.get("save_history"):
            hist = [h for h in index["history"]
                    if isinstance(h, dict) and h.get("date") != entry.get("date")]
            hist.append(dict(index["current"]))
            hist.sort(key=lambda h: str(h.get("date") or ""), reverse=True)
            index["history"] = hist[:MAX_HISTORY]

    # ---------- public ----------
    def status(self):
        """Current entry plus a hint that a newer Bing image exists.

        ``feed`` stays server-side: the picker is fed by history() only, so
        the UI never offers images the user has not actually adopted.
        """
        with self._lock:
            index = self._load()
            cur = self._entry_view(index.get("current"))
            feed = index.get("feed") or []
            latest = feed[0]["date"] if feed and isinstance(feed[0], dict) else ""
            return {
                "ok": True,
                "current": cur,
                "latest_date": latest,
                "outdated": bool(cur and latest and cur["date"] != latest),
                "save_history": bool(index.get("save_history")),
                "feed_age_secs": int(self._now() - float(index.get("feed_ts") or 0)),
            }

    def refresh(self, *, force=False):
        """Sync with Bing's archive and adopt the newest image."""
        try:
            with self._lock:
                index = self._load()
                if force or not self._feed_fresh(index) or not index.get("feed"):
                    entries = self._fetch_feed(index)
                else:
                    entries = index["feed"]
                target = entries[0] if entries else None
                if not target:
                    return {"ok": False, "error": "壁纸接口返回空列表"}
                cur = index.get("current")
                same = isinstance(cur, dict) and cur.get("date") == target["date"]
                if same and self._file_of(cur) is not None and not force:
                    self._save()
                    return self.status()
                merged = dict(target)
                if same and isinstance(cur, dict):
                    merged["file"] = cur.get("file", "")
                try:
                    self._ensure_file(merged)
                except Exception as exc:  # noqa: BLE001 - degrade, never raise
                    stale = self.status()
                    stale["ok"] = False
                    stale["error"] = f"壁纸下载失败：{type(exc).__name__}: {exc}"
                    return stale
                self._adopt(index, merged)
                self._save()
                return self.status()
        except Exception as exc:  # noqa: BLE001 - network/parse failures degrade
            stale = self.status()
            stale["ok"] = False
            # Include the message and the failing call site: "FileNotFoundError"
            # on its own gave no hint that the cache directory could not be
            # created under data_dir.
            import traceback as _tb
            where = ""
            frames = _tb.extract_tb(exc.__traceback__)
            if frames:
                last = frames[-1]
                where = f"（{last.filename.rsplit(chr(47), 1)[-1]}:{last.lineno}）"
            stale["error"] = (f"壁纸同步失败：{type(exc).__name__}: {exc}{where}"
                              f" [data_dir={self.data_dir}]")
            return stale

    def select(self, date):
        """Switch to a wallpaper the user already has on disk.

        Deliberately limited to saved history: the picker is a "my saved
        wallpapers" list, not a browser for Bing's remote archive. Entries
        only reach history by being adopted while save_history was on.
        """
        wanted = str(date or "")
        if not wanted:
            return {"ok": False, "error": "缺少日期"}
        with self._lock:
            index = self._load()
            target = None
            for entry in index.get("history") or []:
                if not isinstance(entry, dict):
                    continue
                if str(entry.get("date")) != wanted:
                    continue
                if self._file_of(entry) is None:
                    continue        # 已删除的文件不再可选
                target = entry
                break
            if target is None:
                return {"ok": False, "error": "没有这张已保存的壁纸"}
            merged = dict(target)
            try:
                self._ensure_file(merged)
            except Exception as exc:  # noqa: BLE001
                return {"ok": False,
                        "error": f"壁纸下载失败：{type(exc).__name__}"}
            self._adopt(index, merged)
            self._save()
            return self.status()

    def history(self):
        with self._lock:
            index = self._load()
            out = []
            for entry in index.get("history") or []:
                view = self._entry_view(entry)
                if view is not None:
                    out.append(view)
            return {"ok": True, "items": out}

    def set_save_history(self, enabled):
        with self._lock:
            index = self._load()
            index["save_history"] = bool(enabled)
            if enabled and isinstance(index.get("current"), dict):
                self._adopt(index, index["current"])
            if not enabled:
                index["history"] = []
            self._save()
        return {"ok": True, "save_history": bool(enabled)}

    def delete(self, date):
        """Remove one saved wallpaper (file + history entry)."""
        wanted = str(date or "")
        if not wanted:
            return {"ok": False, "error": "缺少日期"}
        with self._lock:
            index = self._load()
            keep = []
            removed = None
            for entry in index.get("history") or []:
                if isinstance(entry, dict) and str(entry.get("date")) == wanted:
                    removed = entry
                    continue
                keep.append(entry)
            if removed is None:
                return {"ok": False, "error": "没有这张已保存的壁纸"}
            index["history"] = keep
            cur = index.get("current")
            # Deleting the wallpaper in use falls back to the live Bing image.
            if isinstance(cur, dict) and str(cur.get("date")) == wanted:
                index["current"] = None
            self._save()
            path = self._file_of(removed)
            if path is not None:
                try:
                    path.unlink()
                except OSError:
                    pass
            return {"ok": True}

    def import_image(self, blob, filename="", title=""):
        """Store a user-supplied background image and make it current.

        Uploads are kept apart from Bing entries: their date key is
        ``upload-<ts>`` so they can never collide with a Bing date and are
        only ever offered through history.
        """
        data = blob if isinstance(blob, (bytes, bytearray)) else b""
        if not data:
            return {"ok": False, "error": "文件为空"}
        if len(data) > MAX_BYTES:
            return {"ok": False, "error": "图片超过 8 MB"}
        ext = pathlib.Path(str(filename or "")).suffix.lower()
        if ext not in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
            ext = ".jpg"
        if ext == ".jpeg":
            ext = ".jpg"
        stamp = int(self._now())
        date = f"upload-{stamp}"
        name = f"upload-{stamp}{ext}"
        with self._lock:
            index = self._load()
            cdir = _cache_dir(self.data_dir)
            cdir.mkdir(parents=True, exist_ok=True)
            (cdir / name).write_bytes(data)
            entry = {
                "date": date,
                "url": "",
                "copyright": "",
                "copyright_link": "",
                "title": str(title or pathlib.Path(str(filename or "")).name
                             or "自定义背景"),
                "file": name,
                "ts": self._now(),
            }
            index["save_history"] = True   # 上传即视为用户想保留
            hist = [h for h in index["history"]
                    if isinstance(h, dict) and h.get("date") != date]
            hist.append(dict(entry))
            hist.sort(key=lambda h: str(h.get("date") or ""), reverse=True)
            index["history"] = hist[:MAX_HISTORY]
            index["current"] = dict(entry)
            self._save()
            return self.status()

    def image_path(self, name):
        """Resolve a cached image name to a real path, or None.

        Only flat file names inside the cache dir are accepted; anything that
        escapes the directory is rejected.
        """
        if not name or "/" in name or "\\" in name or name.startswith("."):
            return None
        cdir = _cache_dir(self.data_dir).resolve()
        try:
            path = (cdir / name).resolve()
        except OSError:
            return None
        if cdir not in path.parents:
            return None
        return path if path.is_file() else None