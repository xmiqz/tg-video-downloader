"""Top-level windows must use the same caption palette as the app theme."""
from __future__ import annotations

import ctypes
import sys

import pytest

if sys.platform != "win32":
    pytest.skip(
        "Windows caption integration runs only on Windows",
        allow_module_level=True,
    )

import wv2_native


class _FakeDwm:
    def __init__(self):
        self.calls = []

    def DwmSetWindowAttribute(self, hwnd, attr, value, size):
        self.calls.append((int(hwnd), int(attr), ctypes.cast(
            value, ctypes.POINTER(ctypes.c_int))[0], int(size)))
        return 0


def _capture(monkeypatch, dark):
    fake = _FakeDwm()
    monkeypatch.setattr(wv2_native, "dwmapi", fake)
    wv2_native.set_immersive_caption(123, dark)
    return {attr: value for _hwnd, attr, value, _size in fake.calls}


def test_dark_caption_is_pinned_to_app_palette(monkeypatch):
    attrs = _capture(monkeypatch, True)
    assert attrs[wv2_native.DWMWA_USE_IMMERSIVE_DARK_MODE] == 1
    assert attrs[wv2_native.DWMWA_CAPTION_COLOR] == 0x000F0807
    assert attrs[wv2_native.DWMWA_BORDER_COLOR] == 0x000F0807
    assert attrs[wv2_native.DWMWA_TEXT_COLOR] == 0x00FFFFFF


def test_light_caption_is_pinned_to_app_palette(monkeypatch):
    attrs = _capture(monkeypatch, False)
    assert attrs[wv2_native.DWMWA_USE_IMMERSIVE_DARK_MODE] == 0
    assert attrs[wv2_native.DWMWA_CAPTION_COLOR] == 0x00F5ECE9
    assert attrs[wv2_native.DWMWA_BORDER_COLOR] == 0x00F5ECE9
    assert attrs[wv2_native.DWMWA_TEXT_COLOR] == 0x002E1A10
