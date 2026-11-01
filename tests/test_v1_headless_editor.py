"""V1-D4: headless open_editor must hand back the taskedit.html URL.

Regression for "edit task view is a mess in the browser": without a window
host, open_editor answered a bare error and the UI fell back to the in-shell
/task/:id/edit route, which is laid out for the native shell and renders
unusable at browser widths. taskedit.html itself works in any browser.
"""
from __future__ import annotations

import gui


class _Api(gui.Api):
    def __init__(self):
        self.host = None
        self.token = "desk-token"
        self.runtime_mode = "headless"
        self.allowed_roots = ()
        self.base_url = ""

    def _load_one(self, task_id):
        return {"id": task_id, "channel_title": "t"} if task_id == "ok" else None


def test_headless_open_editor_returns_url():
    r = _Api().open_editor("ok")
    assert r["ok"] is False
    assert r["headless"] is True
    assert r["url"] == "/taskedit.html?id=ok#t=desk-token"


def test_headless_open_editor_missing_task():
    r = _Api().open_editor("nope")
    assert r["ok"] is False
    assert r.get("headless") is None
    assert r["error"] == "任务不存在"


def test_headless_open_editor_quotes_task_id():
    r = _Api().open_editor("ok")
    assert " " not in r["url"] or "%20" in r["url"]