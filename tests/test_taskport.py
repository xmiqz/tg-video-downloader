"""Task export/import: no media, path re-anchoring, validation."""
from __future__ import annotations

import json
import pathlib

import pytest

import taskport


def task(tid="20261026_090449", folder=None, status="pending"):
    return {
        "id": tid,
        "created": "2026-10-26 09:04:49",
        "channel": "cctv1",
        "channel_title": "电影频道 【SOTV】",
        "params": {"parallel": 2, "query": "伊吕波"},
        "folder": folder or "/data/downloads/电影频道",
        "items": [
            {"msg": 101, "size": 11111111, "name": "a.mp4",
             "date": "2026-10-24", "status": status, "src": "频道",
             "text": "秋季档日剧", "duration": 33},
            {"msg": 102, "size": 22222222, "name": "b.mp4",
             "date": "2026-10-22", "status": "failed", "src": "频道",
             "text": "姐妹", "duration": 120},
        ],
    }


# ---------------------------------------------------------------- export
def test_export_contains_no_absolute_folder(tmp_path):
    b = taskport.build_bundle([task()], "/data/downloads")
    t0 = b["tasks"][0]
    assert t0["folder_rel"] == "电影频道"
    assert "/data" not in json.dumps(b, ensure_ascii=False)


def test_export_never_carries_media_bytes(tmp_path):
    b = taskport.build_bundle([task()], "/data/downloads")
    item = b["tasks"][0]["items"][0]
    assert set(item) == {"msg", "src", "name", "size", "date",
                         "duration", "text", "status"}
    assert "path" not in item and "file" not in item


def test_export_resets_done_status(tmp_path):
    b = taskport.build_bundle([task(status="done")], "/data/downloads")
    assert b["tasks"][0]["items"][0]["status"] == "pending"
    # 其它状态保留，便于用户看清哪些失败过
    assert b["tasks"][0]["items"][1]["status"] == "failed"


def test_export_handles_folder_outside_root():
    b = taskport.build_bundle([task(folder="/mnt/other/xyz")], "/data/downloads")
    assert b["tasks"][0]["folder_rel"] == "xyz"


def test_bundle_is_json_roundtrippable(tmp_path):
    b = taskport.build_bundle([task()], "/data/downloads")
    raw = json.dumps(b, ensure_ascii=False)
    assert taskport.parse_bundle(json.loads(raw))


# ---------------------------------------------------------------- import
def test_import_reanchors_folder_under_target_root(tmp_path):
    b = taskport.build_bundle([task()], "/data/downloads")
    parsed = taskport.parse_bundle(json.loads(json.dumps(b)))
    out = taskport.materialize(parsed, "D:/TG/downloads",
                               new_task_id=lambda: "NEW1")
    folder = out[0]["folder"].replace("\\", "/")
    assert folder.endswith("TG/downloads/电影频道")
    assert "/data" not in folder


def test_import_forces_pending_status():
    b = taskport.build_bundle([task(status="done")], "/data/downloads")
    parsed = taskport.parse_bundle(json.loads(json.dumps(b)))
    out = taskport.materialize(parsed, "/x", new_task_id=lambda: "N")
    assert all(i["status"] == "pending" for i in out[0]["items"])


def test_import_assigns_fresh_ids():
    b = taskport.build_bundle([task(), task(tid="other")], "/data/downloads")
    parsed = taskport.parse_bundle(json.loads(json.dumps(b)))
    out = taskport.materialize(parsed, "/x", new_task_id=lambda: "NEW1")
    ids = [t["id"] for t in out]
    assert len(set(ids)) == 2
    assert "20261026_090449" not in ids


def test_import_rejects_path_traversal_in_folder():
    payload = {
        "format": "tgvd-task-bundle", "version": 1,
        "tasks": [{"channel": "c", "channel_title": "t",
                   "folder_rel": "../../etc", "items": [
                       {"msg": 1, "name": "x.mp4", "size": 1}]}],
    }
    parsed = taskport.parse_bundle(payload)
    out = taskport.materialize(parsed, "/safe/root",
                               new_task_id=lambda: "N")
    folder = out[0]["folder"].replace("\\", "/")
    assert ".." not in folder
    # Windows resolves a leading /safe/root to C:/safe/root, so compare the
    # resolved form rather than the literal prefix.
    root = str(pathlib.Path("/safe/root").resolve()).replace("\\", "/")
    assert folder.startswith(root)


def test_import_skips_items_without_integer_msg():
    payload = {
        "format": "tgvd-task-bundle", "version": 1,
        "tasks": [{"channel": "c", "channel_title": "t",
                   "folder_rel": "x", "items": [
                       {"msg": "not-int", "name": "a.mp4"},
                       {"msg": 7, "name": "b.mp4", "size": 5}]}],
    }
    parsed = taskport.parse_bundle(payload)
    assert [i["msg"] for i in parsed[0]["items"]] == [7]


# ---------------------------------------------------------------- validate
@pytest.mark.parametrize("payload,frag", [
    ({}, "不是本应用导出的任务包"),
    ({"format": "tgvd-task-bundle"}, "版本无效"),
    ({"format": "tgvd-task-bundle", "version": 99, "tasks": []}, "高于本程序支持"),
    ({"format": "tgvd-task-bundle", "version": 1, "tasks": []}, "没有任务"),
    ({"format": "tgvd-task-bundle", "version": 1, "tasks": [{}]}, "没有可用任务"),
])
def test_parse_rejects_bad_bundles(payload, frag):
    with pytest.raises(ValueError) as exc:
        taskport.parse_bundle(payload)
    assert frag in str(exc.value)


def test_parse_rejects_non_object():
    with pytest.raises(ValueError):
        taskport.parse_bundle([1, 2, 3])


def test_filename_is_json():
    assert taskport.safe_bundle_filename("20261027-101010").endswith(".json")