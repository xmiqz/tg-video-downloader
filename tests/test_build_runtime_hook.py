"""Packaged windowed builds must tolerate invalid console handles."""
from __future__ import annotations

import sys

import build
import wv2_app


def test_runtime_hook_replaces_a_handle_that_cannot_flush(tmp_path, monkeypatch):
    monkeypatch.setattr(build, "WORK", str(tmp_path))
    hook = build._write_stdio_runtime_hook()
    text = (tmp_path / "rthook_stdio.py").read_text(encoding="utf-8")

    assert hook == str(tmp_path / "rthook_stdio.py")
    assert "_stream.flush()" in text
    assert "except (OSError, ValueError, AttributeError)" in text
    assert "open(os.devnull" in text


def test_wv2_emit_does_not_raise_when_stdout_is_broken(monkeypatch):
    class BrokenStdout:
        def write(self, _text):
            raise OSError(22, "Invalid argument")

        def flush(self):
            raise OSError(22, "Invalid argument")

    host = object.__new__(wv2_app.NativeHost)
    monkeypatch.setattr(sys, "stdout", BrokenStdout())
    host.emit({"event": "test"})
