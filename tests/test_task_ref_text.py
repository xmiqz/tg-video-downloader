"""T61c: task items keep quoted text (ref_text) so smart rename survives.

reply / album / forwarded hits have no message body of their own; the text
comes from the surrounding message. Before this patch ``make_task_item``
only stored ``text``, so the editor showed "no message text" and export /
import lost the only naming input.
"""
from __future__ import annotations

import datetime
import json

import gui
import taskport
import tg_video_dl as core


class _Msg:
    id = 7001
    date = datetime.datetime(2026, 10, 30, 9, 0, 0)


def record(text="", ref_text=None, name="clip.mp4"):
    return {
        "msg": _Msg(), "size": 12345, "name": name, "doc": None,
        "src": "频道", "text": text, "ref_text": ref_text,
    }


def test_make_task_item_stores_ref_text():
    ti = core.make_task_item(record(text="", ref_text="伊吕波 第三季 第12话"))
    assert ti["ref_text"] == "伊吕波 第三季 第12话"
    assert set(ti) == {"msg", "size", "name", "date", "status", "src",
                       "text", "ref_text", "duration"}


def test_make_task_item_clips_ref_text_to_120():
    long = "甲" * (core.REF_TEXT_MAX_CHARS + 10)
    ti = core.make_task_item(record(ref_text=long))
    assert ti["ref_text"].startswith("甲" * core.REF_TEXT_MAX_CHARS)
    assert ti["ref_text"].endswith("…")


def test_make_task_item_without_ref_text_is_empty():
    ti = core.make_task_item(record(text="正文"))
    assert ti["ref_text"] == ""


def test_task_dict_top_level_keys_stay_seven():
    items = [core.make_task_item(record(ref_text="引用句"))]
    tasks = core.build_task_dicts("c", "t", {"query": "引用"}, "/d", items)
    assert set(tasks[0]) == {"id", "created", "channel", "channel_title",
                             "params", "folder", "items"}


def test_smart_name_uses_ref_text_when_body_empty():
    name = core.smart_name_for("", "伊吕波", "clip.mp4",
                               "伊吕波 第三季 的最终话标题在这里")
    assert name.endswith(".mp4")
    assert len(name) > len(".mp4")


class _Api(gui.Api):
    def __init__(self, task):
        self.client = None
        self._task = task

    def _load_one(self, task_id):
        return self._task


def test_editor_data_renames_from_ref_text():
    task = {
        "id": "T1", "channel_title": "t", "channel": "c", "folder": "/d",
        "created": "2026-10-30", "params": {"query": "伊吕波"},
        "items": [core.make_task_item(record(ref_text="伊吕波 第三季 最终话"))],
    }
    out = _Api(task).editor_data("T1", enrich=False)
    assert out["ok"] is True
    assert str(out["items"][0].get("rename") or "") != ""


def test_bundle_roundtrip_keeps_ref_text():
    task = {
        "id": "T1", "created": "2026-10-30 09:00:00", "channel": "c",
        "channel_title": "t", "params": {}, "folder": "/d/子目录",
        "items": [core.make_task_item(record(ref_text="引用片段"))],
    }
    bundle = taskport.build_bundle([task], "/d")
    assert taskport.BUNDLE_VERSION == 1
    assert bundle["version"] == 1
    raw = json.loads(json.dumps(bundle, ensure_ascii=False))
    parsed = taskport.parse_bundle(raw)
    out = taskport.materialize(parsed, "/d2", new_task_id=lambda: "N")
    assert out[0]["items"][0]["ref_text"] == "引用片段"


def test_export_clips_ref_text_to_120():
    task = {
        "id": "T1", "created": "", "channel": "c", "channel_title": "t",
        "params": {}, "folder": "/d",
        "items": [{"msg": 1, "name": "a.mp4", "size": 1, "status": "pending",
                   "ref_text": "乙" * 200}],
    }
    item = taskport.export_task(task, "/d")["items"][0]
    assert len(item["ref_text"]) == 120
