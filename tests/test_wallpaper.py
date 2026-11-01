"""Wallpaper cache: Bing sync cadence, saved history, select, delete, upload."""
from __future__ import annotations

import json
import pathlib

import pytest

import wallpaper

FEED = {
    "images": [
        {"startdate": "20261026", "fullstartdate": "202610261600",
         "url": "/th?id=A_1920x1080.jpg", "copyright": "今天",
         "copyrightlink": "https://x/a", "title": "今日图"},
        {"startdate": "20261025", "fullstartdate": "202610251600",
         "url": "/th?id=B_1920x1080.jpg", "copyright": "昨天",
         "copyrightlink": "https://x/b", "title": "昨日图"},
    ]
}


class FakeClock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, secs):
        self.t += secs


def make(tmp_path, *, clock=None, feed=None):
    clock = clock or FakeClock()
    fetched = []
    feed = feed if feed is not None else FEED

    def fetch(url):
        fetched.append(url)
        return b"IMG:" + url.encode()

    def http(url, timeout=None, limit=None):
        return json.dumps(feed).encode()

    wp = wallpaper.WallpaperCache(
        tmp_path, now=clock, fetcher=fetch,
    )
    wp._http = http               # feed poll
    return wp, fetched, clock


def test_parse_feed_absolutizes_and_sorts():
    entries = wallpaper.parse_feed(FEED)
    assert [e["date"] for e in entries] == ["20261026", "20261025"]
    assert entries[0]["url"].startswith("https://www.bing.com/th?id=")


def test_refresh_adopts_newest_and_caches_file(tmp_path):
    wp, fetched, _ = make(tmp_path)
    st = wp.refresh()
    assert st["ok"] is True
    assert st["current"]["date"] == "20261026"
    assert st["current"]["local"] is True
    assert fetched == ["https://www.bing.com/th?id=A_1920x1080.jpg"]
    # 文件真的落到 data_dir/cache
    assert (tmp_path / "cache" / st["current"]["file"]).is_file()


def test_refresh_is_idempotent_within_same_day(tmp_path):
    wp, fetched, clock = make(tmp_path)
    wp.refresh()
    clock.advance(60)                      # 同一天，几分钟后
    st = wp.refresh()
    assert st["current"]["date"] == "20261026"
    assert len(fetched) == 1, "同一天不应重复下载"


def test_refresh_picks_up_next_day(tmp_path):
    wp, fetched, clock = make(tmp_path)
    wp.refresh()
    clock.advance(24 * 3600)
    wp._http = lambda url: json.dumps({
        "images": [{"startdate": "20261027", "url": "/th?id=C_1920x1080.jpg",
                    "copyright": "新图", "title": "明日图"}]
    }).encode()
    st = wp.refresh()
    assert st["current"]["date"] == "20261027"
    assert len(fetched) == 2


def test_outdated_flag_when_feed_is_newer(tmp_path):
    wp, _, _ = make(tmp_path)
    wp.refresh()
    # 模拟 Bing 已换图但本地仍停在前一天
    wp._index["current"]["date"] = "20261025"
    wp._index["feed"][0]["date"] = "20261026"
    st = wp.status()
    assert st["outdated"] is True


def test_history_only_records_adopted_entries(tmp_path):
    wp, _, clock = make(tmp_path)
    assert wp.set_save_history(True)["ok"] is True
    wp.refresh()
    hist = wp.history()["items"]
    assert [h["date"] for h in hist] == ["20261026"]
    # 远端 feed 里的另一张不应出现在历史里
    assert all(h["date"] != "20261025" for h in hist)


def test_history_disabled_records_nothing(tmp_path):
    wp, _, _ = make(tmp_path)
    wp.refresh()
    assert wp.history()["items"] == []
    assert wp.status()["save_history"] is False


def test_select_only_from_saved_history(tmp_path):
    wp, _, clock = make(tmp_path)
    wp.set_save_history(True)
    wp.refresh()                       # 存下 20261026
    clock.advance(24 * 3600)
    wp._http = lambda url: json.dumps({
        "images": [{"startdate": "20261027", "url": "/th?id=C_1920x1080.jpg",
                    "copyright": "新", "title": "新图"}]
    }).encode()
    wp.refresh()                       # 存下 20261027，current=27
    assert wp.status()["current"]["date"] == "20261027"

    st = wp.select("20261026")
    assert st["ok"] is True
    assert st["current"]["date"] == "20261026"


def test_select_rejects_unsaved_remote_date(tmp_path):
    wp, _, _ = make(tmp_path)
    wp.refresh()                       # feed 含 25，但未保存历史
    st = wp.select("20261025")
    assert st["ok"] is False
    assert "没有这张已保存的壁纸" in st["error"]


def test_delete_removes_one_entry_only(tmp_path):
    wp, _, clock = make(tmp_path)
    wp.set_save_history(True)
    wp.refresh()
    clock.advance(24 * 3600)
    wp._http = lambda url: json.dumps({
        "images": [{"startdate": "20261027", "url": "/th?id=C_1920x1080.jpg",
                    "copyright": "新", "title": "新图"}]
    }).encode()
    wp.refresh()

    victim = wp.status()["current"]["file"]
    assert wp.delete("20261027")["ok"] is True
    assert [h["date"] for h in wp.history()["items"]] == ["20261026"]
    assert not (tmp_path / "cache" / victim).exists()
    # 删掉在用的那张后 current 被清空
    assert wp.status()["current"] is None


def test_delete_unknown_date_fails_cleanly(tmp_path):
    wp, _, _ = make(tmp_path)
    r = wp.delete("19700101")
    assert r["ok"] is False


def test_import_image_becomes_current_and_history(tmp_path):
    wp, _, _ = make(tmp_path)
    r = wp.import_image(b"PNGDATA", filename="mine.png")
    assert r["ok"] is True
    cur = r["current"]
    assert cur["date"].startswith("upload-")
    assert cur["local"] is True
    assert cur["title"] == "mine.png"
    assert wp.status()["save_history"] is True
    assert [h["date"] for h in wp.history()["items"]] == [cur["date"]]
    assert (tmp_path / "cache" / cur["file"]).read_bytes() == b"PNGDATA"


def test_import_rejects_empty_and_oversize(tmp_path):
    wp, _, _ = make(tmp_path)
    assert wp.import_image(b"")["ok"] is False
    assert wp.import_image(b"x" * (wallpaper.MAX_BYTES + 1))["ok"] is False


def test_image_path_blocks_traversal(tmp_path):
    wp, _, _ = make(tmp_path)
    (tmp_path / "secret.txt").write_text("nope")
    for bad in ("../secret.txt", "..\\secret.txt", ".hidden", "a/b.jpg", ""):
        assert wp.image_path(bad) is None


def test_network_failure_degrades_without_raising(tmp_path):
    wp, _, _ = make(tmp_path)

    def boom(url):
        raise OSError("offline")

    wp._http = boom
    st = wp.refresh()
    assert st["ok"] is False
    assert "壁纸同步失败" in st["error"]