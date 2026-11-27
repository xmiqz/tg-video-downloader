import asyncio
import datetime
import threading
from types import SimpleNamespace

import gui
import tg_video_dl as core


FOLDER = "/tmp/tgdl-logic"


def _msg(mid, day):
    return SimpleNamespace(
        id=mid,
        date=datetime.datetime(2026, 11, day, tzinfo=datetime.timezone.utc),
        message=f"video {mid}",
        document=SimpleNamespace(size=100 + mid),
    )


def _record(mid, day, name=None, size=None):
    msg = _msg(mid, day)
    return {
        "msg": msg,
        "doc": msg.document,
        "size": size if size is not None else msg.document.size,
        "name": name or f"v{mid}.mp4",
        "text": msg.message,
        "src": core.SRC_CHANNEL,
    }


def _item(mid, day, name=None, size=None):
    return core.make_task_item(_record(mid, day, name, size))


def _split_tasks():
    return [
        {
            "id": "a1",
            "channel": "@demo",
            "channel_title": "合集（1/3）",
            "folder": FOLDER,
            "params": {"query": "cat", "limit": 30},
            "last_msg_id": 100,
            "items": [_item(100, 1), _item(98, 1)],
        },
        {
            "id": "a2",
            "channel": "@demo",
            "channel_title": "合集（2/3）",
            "folder": FOLDER,
            "params": {"query": "cat", "limit": 30},
            "items": [_item(95, 2)],
        },
        {
            "id": "a3",
            "channel": "@demo",
            "channel_title": "合集（3/3）",
            "folder": FOLDER,
            "params": {"query": "cat", "limit": 30},
            "last_msg_id": 120,
            "items": [_item(120, 3)],
        },
        {
            "id": "other",
            "channel": "@demo",
            "channel_title": "合集（4/4）",
            "folder": "/tmp/other",
            "params": {},
            "items": [_item(999, 9)],
        },
    ]


def _bind_tasks(monkeypatch, tasks):
    by_id = {task["id"]: task for task in tasks}
    monkeypatch.setattr(core, "load_tasks", lambda: [dict(t) for t in tasks])
    monkeypatch.setattr(core, "load_task", lambda tid: by_id.get(tid))


def _make_api():
    api = gui.Api.__new__(gui.Api)
    api.client = object()
    api.cfg = {}
    api.runtime_mode = "desktop"
    api.allowed_roots = ()
    api.push_event = None
    api._preview_lock = threading.Lock()
    api._preview = {}
    api._search_previews = {}
    api._search_tasks = {}
    api._scan_group_tokens = {}
    api._scan_group_locks = {}
    api._scan_group_lock = threading.Lock()
    api._pool = object()
    api._acct_sink = lambda *args: None
    return api


def test_scan_group_info_uses_whole_split_group_and_date_from(monkeypatch):
    tasks = _split_tasks()
    info = core.scan_group_info(tasks[0], tasks)
    assert core.scan_base_title("合集（1/3）") == "合集"
    assert info["group_size"] == 3
    assert info["watermark_id"] == 120
    assert info["date_from"] == "2026-11-03"
    assert (core.SRC_CHANNEL, 95) in info["item_keys"]
    assert (core.SRC_CHANNEL, 999) not in info["item_keys"]


def test_scan_base_title_strips_split_suffix_before_scan_date():
    assert core.scan_base_title("合集（1/3）") == "合集"
    assert core.scan_base_title("合集(1/3)") == "合集"
    assert core.scan_base_title("合集 · 扫新 2026-11-27（1/2）") == "合集"


def test_scan_prefill_watermark_and_concurrent_lock(monkeypatch):
    tasks = _split_tasks()
    _bind_tasks(monkeypatch, tasks)
    api = _make_api()
    first = api.scan_prefill("a1")
    assert first["ok"] is True
    assert first["group_size"] == 3
    assert first["watermark_id"] == 120
    assert first["date_from"] == "2026-11-03"
    assert first["params"]["min_id"] == 120
    assert first["params"]["date_from"] == "2026-11-03"
    assert first["params"]["query"] == "cat"
    assert first["params"]["scan_task_id"] == "a1"
    assert first["params"]["scan_base_title"] == "合集"
    assert "date_to" not in first["params"]
    assert first["title"].startswith("合集 · 扫新 ")

    second = api.scan_prefill("a2")
    assert second["ok"] is False
    assert second["busy"] is True
    assert second["error"] == "该频道逻辑库正在扫新，请稍候"

    assert api.scan_release(first["token"]) == {"ok": True}
    assert api.scan_release(first["token"]) == {"ok": True}
    assert api.scan_release("unknown") == {"ok": True}
    assert api.scan_prefill("a2")["ok"] is True


def test_scan_params_reject_stale_or_wrong_group_token(monkeypatch):
    tasks = _split_tasks()
    _bind_tasks(monkeypatch, tasks)
    api = _make_api()
    prefill = api.scan_prefill("a1")
    params = dict(prefill["params"])
    params["scan_token"] = "stale-token"
    result = api.preview(params)
    assert result["ok"] is False
    assert result["busy"] is True
    assert api.scan_release(prefill["token"]) == {"ok": True}


def test_scan_prefill_rejects_live_task(monkeypatch):
    tasks = _split_tasks()
    _bind_tasks(monkeypatch, tasks)
    api = _make_api()
    core.ACTIVE_SESSIONS["a1"] = object()
    try:
        result = api.scan_prefill("a1")
    finally:
        core.ACTIVE_SESSIONS.pop("a1", None)
    assert result["busy"] is True
    assert "正在下载" in result["error"]


def test_scan_preview_marks_in_task_and_carries_min_id(monkeypatch):
    tasks = _split_tasks()
    _bind_tasks(monkeypatch, tasks)
    api = _make_api()
    prefill = api.scan_prefill("a1")

    captured = {}

    class FakeJobs:
        def create(self, kind):
            return "job-scan", threading.Event()

        def update(self, *args, **kwargs):
            pass

        def snapshot(self, job_id):
            return {"created_ts": 0.0}

    class FakeThread:
        def __init__(self, target=None, args=None, daemon=None, name=None):
            captured["args"] = args

        def start(self):
            pass

    api.jobs = FakeJobs()
    monkeypatch.setattr(gui.threading, "Thread", FakeThread)
    params = dict(prefill["params"])
    params["scan_token"] = prefill["token"]
    result = api.preview(params)
    assert result == {"ok": True, "job_id": "job-scan"}
    search_params = captured["args"][4]
    assert search_params["min_id"] == 120
    assert search_params["date_from"] == "2026-11-03"

    frames = []
    api.push_event = lambda name, payload: frames.append(payload[0]["data"])

    async def fake_stream(job_id, search_params, cancel_event):
        yield {"kind": "items", "items": [
            _record(100, 1),
            _record(200, 4),
            _record(300, 4, name="v100.mp4", size=200),
        ]}
        yield {"kind": "done", "records": [
            _record(100, 1),
            _record(200, 4),
            _record(300, 4, name="v100.mp4", size=200),
        ], "meta": {}}

    monkeypatch.setattr(api, "_scan_search_stream", fake_stream)
    asyncio.run(api._search_coro(
        "job-scan", threading.Event(), "@demo", None, search_params))
    assert frames[0]["items"][0]["in_task"] is True
    assert frames[0]["items"][1]["in_task"] is False
    assert frames[0]["items"][2]["in_task"] is True
    assert frames[-1]["meta"]["in_task_count"] == 2


def test_start_download_scan_creates_new_task_and_reports_skips(monkeypatch,
                                                                tmp_path):
    tasks = _split_tasks()
    _bind_tasks(monkeypatch, tasks)
    api = _make_api()
    prefill = api.scan_prefill("a1")
    saved = []
    monkeypatch.setattr(core, "save_task", lambda task: saved.append(task))
    monkeypatch.setattr(
        api, "_resolve_runtime_folder", lambda raw, **kw: (tmp_path, ""))
    monkeypatch.setattr(core, "build_planned", lambda task, recs, st: {})

    class FakeSession:
        def request(self, *args, **kwargs):
            pass

    async def fake_open(*args, **kwargs):
        return FakeSession()

    monkeypatch.setattr(core, "open_download_session", fake_open)

    class FakeJobs:
        def create(self, kind):
            return "job-dl", threading.Event()

        def update(self, *args, **kwargs):
            pass

        def snapshot(self, job_id):
            return {"created_ts": 0.0}

    api.jobs = FakeJobs()
    api.engine = SimpleNamespace(loop=None)
    monkeypatch.setattr(api, "_make_dl_phase_cb", lambda task_id: lambda *a: None)
    monkeypatch.setattr(api, "_start_dl_cancel_watcher", lambda *a: None)

    captured_session = {}

    def fake_run_coroutine(coro, loop):
        captured_session["coro"] = coro
        coro.close()
        return SimpleNamespace(result=lambda: FakeSession())

    monkeypatch.setattr(gui.asyncio, "run_coroutine_threadsafe",
                        fake_run_coroutine)

    class FakeThread:
        def __init__(self, target=None, args=None, daemon=None, name=None):
            pass

        def start(self):
            pass

    monkeypatch.setattr(gui.threading, "Thread", FakeThread)
    api._dl_jobs = {}
    api._dl_tasks = {}
    api._dl_lock = threading.Lock()
    channel = prefill["params"]["channel"]
    title = prefill["title"]
    keys = [
        {"src": core.SRC_CHANNEL, "mid": 100},
        {"src": core.SRC_CHANNEL, "mid": 200},
        {"src": core.SRC_CHANNEL, "mid": 300},
    ]
    api._preview = {
        (core.SRC_CHANNEL, 100): _record(100, 1),
        (core.SRC_CHANNEL, 200): _record(200, 4),
        (core.SRC_CHANNEL, 300): _record(300, 4),
    }
    scan = {
        "scan_token": prefill["token"],
        "scan_task_id": "a1",
        "params": dict(prefill["params"]),
    }
    result = api.start_download(
        channel, title, keys, 2, folder=prefill["folder"], scan=scan)
    assert "error" not in result, result
    assert result["ok"] is True
    assert result["skipped_in_task"] == 1
    assert len(saved) == 1
    new_task = saved[0]
    assert " · 扫新 " in new_task["channel_title"]
    assert new_task["channel_title"].startswith("合集 · 扫新 ")
    assert new_task["params"]["scan_base_title"] == "合集"
    assert new_task["params"]["min_id"] == 120
    assert new_task["params"]["parallel"] == 2
    assert "scan_token" not in new_task["params"]
    assert new_task["last_msg_id"] == 300
    assert new_task["folder"] == str(tmp_path)
    assert tasks[0]["items"] == _split_tasks()[0]["items"]
    assert api.scan_prefill("a2")["ok"] is True
    assert "coro" in captured_session


def test_start_download_accepts_frontend_scan_shape(monkeypatch, tmp_path):
    tasks = _split_tasks()
    _bind_tasks(monkeypatch, tasks)
    api = _make_api()
    prefill = api.scan_prefill("a1")
    saved = []
    monkeypatch.setattr(core, "save_task", lambda task: saved.append(task))
    monkeypatch.setattr(api, "_resolve_runtime_folder",
                        lambda raw, **kw: (tmp_path, ""))
    monkeypatch.setattr(core, "build_planned", lambda task, recs, st: {})

    class FakeSession:
        pass

    def fake_run_coroutine(coro, loop):
        coro.close()
        return SimpleNamespace(result=lambda: FakeSession())

    monkeypatch.setattr(gui.asyncio, "run_coroutine_threadsafe",
                        fake_run_coroutine)
    api.jobs = SimpleNamespace(
        create=lambda kind: ("job-dl", threading.Event()),
        update=lambda *args, **kwargs: None,
    )
    api.engine = SimpleNamespace(loop=None)
    monkeypatch.setattr(api, "_make_dl_phase_cb",
                        lambda task_id: lambda *a: None)
    monkeypatch.setattr(api, "_start_dl_cancel_watcher", lambda *a: None)

    class FakeThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            pass

    monkeypatch.setattr(gui.threading, "Thread", FakeThread)
    api._dl_jobs = {}
    api._dl_tasks = {}
    api._dl_lock = threading.Lock()
    api._preview = {
        (core.SRC_CHANNEL, 100): _record(100, 1),
        (core.SRC_CHANNEL, 200): _record(200, 4),
    }
    params = dict(prefill["params"])
    params.update({
        "mode": "all",
        "scope": "channel",
        "sort": "date",
        "scan_token": "stale-token-should-not-persist",
    })
    scan = {
        "token": prefill["token"],
        "task_id": "a1",
        "base_title": "合集",
        "scan_date": params["scan_date"],
        "folder": prefill["folder"],
        "params": params,
    }
    result = api.start_download(
        prefill["params"]["channel"], prefill["title"],
        [{"src": core.SRC_CHANNEL, "mid": 100},
         {"src": core.SRC_CHANNEL, "mid": 200}], 2, "",
        "job-preview", params["query"], params.get("name_wide", ""), scan)
    assert result["ok"] is True, result
    assert result["skipped_in_task"] == 1
    assert len(saved) == 1
    task_params = saved[0]["params"]
    for key in ("min_id", "date_from", "mode", "query", "scope", "sort",
                "scan_date", "scan_base_title"):
        assert task_params[key] == params[key], key
    assert "scan_token" not in task_params
    assert saved[0]["last_msg_id"] == 200
    assert api.scan_prefill("a2")["ok"] is True


def test_start_download_invalid_token_does_not_release_original_flow(
        monkeypatch):
    tasks = _split_tasks()
    _bind_tasks(monkeypatch, tasks)
    api = _make_api()
    prefill = api.scan_prefill("a1")
    api._preview = {(core.SRC_CHANNEL, 200): _record(200, 4)}
    result = api.start_download(
        prefill["params"]["channel"], prefill["title"],
        [{"src": core.SRC_CHANNEL, "mid": 200}], 1, "", "", "", "",
        {"token": "wrong-token", "task_id": "a1",
         "params": dict(prefill["params"])})
    assert result["ok"] is False
    assert result["busy"] is True
    assert api.scan_prefill("a2")["ok"] is False

    class FakeJobs:
        def create(self, kind):
            return "job-scan", threading.Event()

        def update(self, *args, **kwargs):
            pass

        def snapshot(self, job_id):
            return {"created_ts": 0.0}

    class FakeThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            pass

    api.jobs = FakeJobs()
    monkeypatch.setattr(gui.threading, "Thread", FakeThread)
    params = dict(prefill["params"])
    params["scan_token"] = prefill["token"]
    assert api.preview(params)["ok"] is True


def test_start_download_rejects_all_existing_and_keeps_legacy_contract(
        monkeypatch, tmp_path):
    tasks = _split_tasks()
    _bind_tasks(monkeypatch, tasks)
    api = _make_api()
    prefill = api.scan_prefill("a1")
    api._preview = {(core.SRC_CHANNEL, 100): _record(100, 1)}
    scan = {
        "scan_token": prefill["token"],
        "scan_task_id": "a1",
        "params": dict(prefill["params"]),
    }
    result = api.start_download(
        "x", "y", [{"src": core.SRC_CHANNEL, "mid": 100}], 1,
        folder=prefill["folder"], scan=scan)
    assert result["ok"] is False
    assert result["skipped_in_task"] == 1
    assert "已在任务" in result["error"]
    assert api.scan_prefill("a2")["ok"] is True

    api._preview = {(core.SRC_CHANNEL, 900): _record(900, 9)}
    api.jobs = SimpleNamespace(
        create=lambda kind: ("job-legacy", threading.Event()),
        update=lambda *args, **kwargs: None,
        snapshot=lambda job_id: {"created_ts": 0.0},
    )
    api.engine = SimpleNamespace(loop=None)
    monkeypatch.setattr(api, "_resolve_runtime_folder",
                        lambda raw, **kw: (tmp_path, ""))
    monkeypatch.setattr(core, "build_planned", lambda task, recs, st: {})
    def close_coroutine(coro, loop):
        coro.close()
        return SimpleNamespace(result=lambda: SimpleNamespace())

    monkeypatch.setattr(gui.asyncio, "run_coroutine_threadsafe",
                        close_coroutine)
    monkeypatch.setattr(api, "_make_dl_phase_cb",
                        lambda task_id: lambda *a: None)
    monkeypatch.setattr(api, "_start_dl_cancel_watcher", lambda *a: None)
    monkeypatch.setattr(core, "save_task", lambda task: None)

    class LegacyThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            pass

    monkeypatch.setattr(gui.threading, "Thread", LegacyThread)
    api._dl_jobs = {}
    api._dl_tasks = {}
    api._dl_lock = threading.Lock()
    error_result = api.start_download(
        "x", "y", [{"src": core.SRC_CHANNEL, "mid": 900}], 1,
        folder=str(tmp_path))
    assert error_result["ok"] is True, error_result
