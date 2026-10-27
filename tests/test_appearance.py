"""set_appearance must accept every mode the UI offers.

Caught a real regression: the settings UI gained a "每日壁纸" segment whose
value is "bing", but set_appearance still whitelisted only dark/light/liquid,
so picking it surfaced "未知外观模式" and the choice was never persisted.
"""
from __future__ import annotations

import re
import pathlib

import gui

ROOT = pathlib.Path(__file__).resolve().parent.parent
SETTINGS_JS = ROOT / "web" / "js" / "views" / "settings.js"


def _ui_values():
    """Every `value: "<mode>"` listed in the appearance segment array."""
    text = SETTINGS_JS.read_text(encoding="utf-8")
    start = text.index("const APPEARANCE_SEGS")
    end = text.index("];", start)
    return set(re.findall(r'value:\s*"([a-z]+)"', text[start:end]))


def test_ui_values_are_all_accepted_by_backend():
    values = _ui_values()
    assert values, "解析不到 APPEARANCE_SEGS"
    api = gui.Api.__new__(gui.Api)
    api.cfg = {}
    api.host = None
    api._drv = None
    rejected = []
    for mode in sorted(values):
        r = api.set_appearance(mode)
        if not r.get("ok"):
            rejected.append((mode, r.get("error")))
    assert rejected == [], f"后端拒绝了 UI 提供的模式：{rejected}"


def test_bing_is_persisted():
    api = gui.Api.__new__(gui.Api)
    api.cfg = {}
    api.host = None
    api._drv = None
    assert api.set_appearance("bing")["ok"] is True
    assert api.cfg["appearance"] == "bing"


def test_unknown_mode_still_rejected():
    api = gui.Api.__new__(gui.Api)
    api.cfg = {}
    api.host = None
    api._drv = None
    r = api.set_appearance("nope")
    assert r["ok"] is False
    assert r["error"] == "未知外观模式"
    assert "appearance" not in api.cfg