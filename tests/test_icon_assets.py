"""应用图标资产契约（T67）：ICO 尺寸齐备、条目是真实 PNG、SVG 与页面同源。

不再维护第二份几何：favicon 与 web/assets/xian-icon.svg 都由生产
web/js/motion/xian.js 的 can 控制点派生，data-geometry 必须与之一致。
"""
from __future__ import annotations

import struct
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ICO = ROOT / "build" / "tvg.ico"
FAVICON = ROOT / "web" / "favicon.svg"
APP_ICON = ROOT / "web" / "assets" / "xian-icon.svg"
BUILD_PY = ROOT / "build.py"

REQUIRED_SIZES = [16, 24, 32, 48, 64, 128, 256]
GEOMETRY = "nose:56,-18 wing:-44,-20 crown:-10,-48 keel:-24,44"
PNG_SIG = b"\x89PNG\r\n\x1a\n"


def _entries():
    data = ICO.read_bytes()
    reserved, kind, count = struct.unpack("<HHH", data[:6])
    assert reserved == 0 and kind == 1, "ICO 头必须是 (0, 1, n)"
    out = []
    for i in range(count):
        off = 6 + 16 * i
        w, h, _colors, _res, _planes, bpp, size, offset = struct.unpack(
            "<BBBBHHII", data[off:off + 16])
        out.append({"w": w or 256, "h": h or 256, "bpp": bpp,
                    "size": size, "offset": offset})
    return data, out


def test_ico_carries_every_required_size():
    _data, entries = _entries()
    assert len(entries) == len(REQUIRED_SIZES)
    assert [(e["w"], e["h"]) for e in entries] == [
        (s, s) for s in REQUIRED_SIZES]
    assert all(e["bpp"] == 32 for e in entries), "图标须为 32bpp RGBA"


def test_every_ico_entry_is_a_png_of_its_declared_size():
    data, entries = _entries()
    for e in entries:
        blob = data[e["offset"]:e["offset"] + e["size"]]
        assert len(blob) == e["size"], "目录声明的字节数必须与载荷相符"
        assert blob[:8] == PNG_SIG, "条目载荷必须是 PNG"
        width, height = struct.unpack(">II", blob[16:24])
        assert (width, height) == (e["w"], e["h"])


def test_ico_payloads_do_not_overrun_the_file():
    data, entries = _entries()
    for e in entries:
        assert e["offset"] + e["size"] <= len(data)


def test_page_svgs_share_one_geometry():
    favicon = FAVICON.read_text(encoding="utf-8")
    master = APP_ICON.read_text(encoding="utf-8")
    for text in (favicon, master):
        assert 'viewBox="-60 -60 120 120"' in text
        assert 'data-geometry="' + GEOMETRY + '"' in text
        assert "x-w1" in text and "x-w2" in text
    assert 'width="64"' in favicon
    assert 'width="256"' in master


def test_windows_builds_pass_the_icon_to_pyinstaller():
    src = BUILD_PY.read_text(encoding="utf-8")
    assert src.count('"--icon", os.path.join(HERE, "build", "tvg.ico")') == 2
    # 窗口类图标随包进 assets/，冻结态由 wv2_native 载入
    assert 'os.path.join(HERE, "build", "tvg.ico") + os.pathsep + "assets"' in src


@pytest.mark.skipif(sys.platform != "win32",
                    reason="窗口图标路径只在 Windows 有意义")
def test_window_icon_path_prefers_frozen_bundle(monkeypatch):
    import wv2_native

    assert wv2_native._app_icon_path().endswith("build\\tvg.ico")
    monkeypatch.setattr(wv2_native.sys, "_MEIPASS", "C:/bundle", raising=False)
    assert wv2_native._app_icon_path() == "C:/bundle\\assets\\tvg.ico"
