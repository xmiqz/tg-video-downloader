"""下载中改名必须同步 live 会话快照，否则下一次 force 保存会回退。

同时锁定：已下载条目原文件缺失时明确报错，未下载条目仍只登记拟用名。
全部使用 tmp_path，不读写真实任务目录。
"""
from __future__ import annotations

import copy

import pytest

import gui
import tg_video_dl as core


def _item(mid=7001, status="done", name="clip.mp4"):
    return {"msg": mid, "size": 12345, "name": name, "doc": None,
            "src": core.SRC_CHANNEL, "text": "", "ref_text": "",
            "status": status, "duration": 0}


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    task_dir = tmp_path / "tasks"
    task_dir.mkdir()
    monkeypatch.setattr(core, "TASK_ROOT", task_dir)
    core.reset_persistence()
    yield task_dir
    core.reset_persistence()


def _task(task_id, folder, items):
    return {"id": task_id, "created": "2026-11-26 10:00:00",
            "channel": "c", "channel_title": "旧标题", "params": {},
            "folder": str(folder), "items": items}


def _api(task):
    api = gui.Api.__new__(gui.Api)
    api._load_one = lambda task_id: task
    return api


def _sess(task):
    return core.DownloadSession(task, None, None)


def test_edit_title_syncs_live_session(isolated, monkeypatch):
    task = _task("t-title", isolated, [_item()])
    live = _sess(copy.deepcopy(task))
    monkeypatch.setitem(core.ACTIVE_SESSIONS, task["id"], live)
    assert _api(task).edit_task(task["id"], {"title": "新标题"})["ok"] is True
    assert live.task["channel_title"] == "新标题"
    core.save_task(live.task, force=True)
    assert core.load_tasks()[0]["channel_title"] == "新标题"


def test_edit_rename_syncs_live_session(isolated, monkeypatch):
    (isolated.parent / "clip.mp4").write_bytes(b"x")
    task = _task("t-rename", isolated.parent, [_item()])
    live = _sess(copy.deepcopy(task))
    monkeypatch.setitem(core.ACTIVE_SESSIONS, task["id"], live)
    out = _api(task).edit_task(task["id"], {
        "renames": [{"src": core.SRC_CHANNEL, "mid": 7001,
                     "name": "新名字.mp4"}]})
    assert out["ok"] is True and out["renamed"] == 1
    assert live.task["items"][0]["name"] == task["items"][0]["name"]
    core.save_task(live.task, force=True)
    assert core.load_tasks()[0]["items"][0]["name"] == task["items"][0]["name"]


def test_restore_syncs_live_session_and_clears_removed_keys(
        isolated, monkeypatch):
    (isolated.parent / "new.mp4").write_bytes(b"x")
    task = _task("t-restore", isolated.parent, [_item(name="new.mp4")])
    task["items"][0]["orig_name"] = "old.mp4"
    live = _sess(copy.deepcopy(task))
    monkeypatch.setitem(core.ACTIVE_SESSIONS, task["id"], live)
    out = _api(task).edit_task(task["id"], {
        "restore": [{"src": core.SRC_CHANNEL, "mid": 7001}]})
    assert out["ok"] is True and out["restored"] == 1
    assert live.task["items"][0]["name"] == "old.mp4"
    assert "orig_name" not in live.task["items"][0]


def test_apply_item_rename_missing_file_done_fails(isolated):
    task = _task("t-done", isolated, [_item(status="done")])
    result = core.apply_item_rename(task, task["items"][0], "new.mp4")
    assert result == {"ok": False, "renamed": False, "name": "clip.mp4",
                      "error": "原文件不存在，无法改名"}


def test_apply_item_rename_missing_file_pending_records(isolated):
    task = _task("t-pending", isolated, [_item(status="pending")])
    result = core.apply_item_rename(task, task["items"][0], "new.mp4")
    assert result == {"ok": True, "renamed": False, "name": "clip.mp4",
                      "error": ""}
    assert task["items"][0]["rename"] == "new.mp4"


class _InlineLoop:
    """把会话循环的投递就地执行，模拟事件循环线程上的串行保存。"""

    def is_closed(self):
        return False

    def call_soon_threadsafe(self, fn):
        fn()


def test_rename_sync_persists_on_session_loop(isolated, monkeypatch):
    """同步投递到会话循环后应立即回写，最后一个落盘快照必须带新名称。"""
    (isolated.parent / "clip.mp4").write_bytes(b"x")
    task = _task("t-loop", isolated.parent, [_item()])
    live = _sess(copy.deepcopy(task))
    live.loop = _InlineLoop()
    monkeypatch.setitem(core.ACTIVE_SESSIONS, task["id"], live)
    out = _api(task).edit_task(task["id"], {
        "title": "循环标题",
        "renames": [{"src": core.SRC_CHANNEL, "mid": 7001,
                     "name": "循环名字.mp4"}],
    })
    assert out["ok"] is True
    assert live.task["channel_title"] == "循环标题"
    assert live.task["items"][0]["name"] == "循环名字.mp4"
    stored = {t["id"]: t for t in core.load_tasks()}[task["id"]]
    assert stored["channel_title"] == "循环标题"
    assert stored["items"][0]["name"] == "循环名字.mp4"
