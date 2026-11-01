"""Task folders must remain editable offline and survive copied data dirs."""
from __future__ import annotations

import json
import threading

import gui
import tg_video_dl as core


def test_folder_change_does_not_require_telegram_login(tmp_path, monkeypatch):
    api = object.__new__(gui.Api)
    api.client = None
    api.cfg = {}
    api.runtime_mode = "desktop"
    api.allowed_roots = ()
    api._move_tasks = {}
    api._move_lock = threading.Lock()

    task = {"id": "t1", "folder": str(tmp_path / "old"), "items": []}
    monkeypatch.setattr(api, "_load_one", lambda _task_id: task)

    seen = {}

    def fake_change(task_obj, folder, move_files=False):
        seen["task"] = task_obj
        seen["folder"] = folder
        seen["move"] = move_files
        return {"ok": True}

    monkeypatch.setattr(core, "change_folder", fake_change)
    result = api.edit_task("t1", {"folder": str(tmp_path / "new")})

    assert result == {"ok": True, "warnings": []}
    assert seen == {"task": task, "folder": str(tmp_path / "new"),
                    "move": False}
    assert not any(k == "need_login" for k in result)


def test_startup_relocates_tasks_from_copied_data_dir(tmp_path, monkeypatch):
    data = tmp_path / "data"
    tasks = data / "tasks"
    tasks.mkdir(parents=True)
    core.configure_data_dir(data)
    (data / "tg_config.json").write_text(
        json.dumps({"download_root": str(data / "downloads")}),
        encoding="utf-8",
    )

    old_folder = str(tmp_path / "source-machine" / "电影频道")
    (tasks / "20261026_090449.json").write_text(
        json.dumps({
            "id": "20261026_090449",
            "folder": old_folder,
            "items": [],
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(core, "_TASK_RELOCATION_DONE", False)

    changed = core.relocate_imported_tasks()
    assert len(changed) == 1

    saved = json.loads(
        (tasks / "20261026_090449.json").read_text(encoding="utf-8")
    )
    assert saved["folder"] == str(data / "downloads" / "电影频道")
    assert (data / "downloads" / "电影频道").is_dir()


def test_relocation_leaves_existing_folder_untouched(tmp_path):
    data = tmp_path / "data"
    tasks = data / "tasks"
    tasks.mkdir(parents=True)
    existing = data / "downloads" / "existing"
    existing.mkdir(parents=True)
    core.configure_data_dir(data)
    (data / "tg_config.json").write_text(
        json.dumps({"download_root": str(data / "downloads")}),
        encoding="utf-8",
    )
    (tasks / "t2.json").write_text(
        json.dumps({"id": "t2", "folder": str(existing), "items": []}),
        encoding="utf-8",
    )

    import tg_video_dl
    tg_video_dl._TASK_RELOCATION_DONE = False
    assert core.relocate_imported_tasks() == []


def test_relocation_handles_foreign_path_separators(tmp_path, monkeypatch):
    data = tmp_path / "data"
    tasks = data / "tasks"
    tasks.mkdir(parents=True)
    core.configure_data_dir(data)
    (data / "tg_config.json").write_text(
        json.dumps({"download_root": str(data / "downloads")}),
        encoding="utf-8",
    )
    (tasks / "t3.json").write_text(
        json.dumps({
            "id": "t3",
            "folder": "C:\\Users\\old\\TG视频下载器数据\\downloads\\频道",
            "items": [],
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(core, "_TASK_RELOCATION_DONE", False)

    changed = core.relocate_imported_tasks()
    assert changed == [(
        "t3",
        "C:\\Users\\old\\TG视频下载器数据\\downloads\\频道",
        str(data / "downloads" / "频道"),
    )]


def test_relocation_does_not_clobber_existing_leaf(tmp_path, monkeypatch):
    data = tmp_path / "data"
    tasks = data / "tasks"
    tasks.mkdir(parents=True)
    root = data / "downloads"
    (root / "频道").mkdir(parents=True)
    core.configure_data_dir(data)
    (data / "tg_config.json").write_text(
        json.dumps({"download_root": str(root)}),
        encoding="utf-8",
    )
    (tasks / "t4.json").write_text(
        json.dumps({
            "id": "t4",
            "folder": str(tmp_path / "missing-source" / "频道"),
            "items": [],
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(core, "_TASK_RELOCATION_DONE", False)

    assert core.relocate_imported_tasks() == []
    saved = json.loads((tasks / "t4.json").read_text(encoding="utf-8"))
    assert saved["folder"] == str(tmp_path / "missing-source" / "频道")
