import asyncio
import datetime
import threading
import time
from types import SimpleNamespace

import gui
import tg_video_dl as core


def _msg(mid, day=1, size=None):
    return SimpleNamespace(
        id=mid,
        date=datetime.datetime(2026, 11, day, tzinfo=datetime.timezone.utc),
        message=f"video {mid}",
        document=SimpleNamespace(size=size if size is not None else 100 + mid),
    )


def _record(mid, day=1, name=None, size=None, src=core.SRC_CHANNEL):
    msg = _msg(mid, day, size)
    return {
        "msg": msg,
        "doc": msg.document,
        "size": size if size is not None else msg.document.size,
        "name": name or f"v{mid}.mp4",
        "text": msg.message,
        "src": src,
    }


def _task():
    return {
        "id": "t1",
        "channel": "@demo",
        "channel_title": "合集（1/2）",
        "folder": "/tmp/demo",
        "params": {"query": "cat", "limit": 10},
        "last_msg_id": 0,
        "items": [],
    }


def _make_api(client=None):
    api = gui.Api.__new__(gui.Api)
    api.client = client or object()
    api.cfg = {}
    api.jobs = None
    api.engine = None
    api._scan_lock = threading.Lock()
    api._scan_tasks = {}
    api._preview_lock = threading.Lock()
    api._preview = {}
    api._search_previews = {}
    api._search_tasks = {}
    api.push_event = None
    api._scan_group_tokens = {}
    api._scan_group_locks = {}
    api._scan_group_lock = threading.Lock()
    return api


class FakeJobs:
    def __init__(self, delay=0.0):
        self.delay = delay
        self.created = []
        self.updates = []

    def create(self, kind):
        if self.delay:
            time.sleep(self.delay)
        job_id = f"job-{len(self.created) + 1}"
        self.created.append((kind, job_id))
        return job_id, threading.Event()

    def update(self, *args, **kwargs):
        self.updates.append((args, kwargs))

    def snapshot(self, job_id):
        for args, kwargs in reversed(self.updates):
            if args and args[0] == job_id and "state" in kwargs:
                return {"state": kwargs["state"], "done": kwargs.get("done", 0)}
        return {"state": "queued", "done": 0}


class FakeThread:
    def __init__(self, target=None, args=None, daemon=None, name=None):
        self.target = target
        self.args = args

    def start(self):
        pass


def _bind_thread(monkeypatch):
    original = gui.threading.Thread
    monkeypatch.setattr(gui.threading, "Thread", FakeThread)
    return original


def test_append_records_dedup_status_watermark_and_save(monkeypatch):
    task = _task()
    task["items"].append(core.make_task_item(_record(10, 1)))
    task["items"][0]["status"] = "done"
    original_title = task["channel_title"]
    saves = []
    monkeypatch.setattr(core, "save_task",
                        lambda t, force=False: saves.append((t, force)) or True)

    assert core.append_records(task, []) == 0
    assert saves == []

    recs = [
        _record(10, 1),
        _record(11, 2, name="same.mp4", size=111),
        _record(12, 2, name="same.mp4", size=111),
        _record(13, 3),
        _record(13, 3),
        _record(14, 3, src="评论区"),
    ]
    assert core.append_records(task, recs) == 3
    assert [ti["msg"] for ti in task["items"]] == [10, 11, 13, 14]
    assert task["items"][1]["status"] == "pending"
    assert task["items"][0]["status"] == "done"
    assert task["channel_title"] == original_title
    assert task["last_msg_id"] == 13
    assert len(saves) == 1 and saves[0][1] is True

    saves.clear()
    assert core.append_records(task, [_record(
        60, 4, name="v13.mp4", size=113)]) == 0
    assert task["last_msg_id"] == 60
    assert len(saves) == 1 and saves[0][1] is True


def test_scan_append_sync_validation_and_param_merge(monkeypatch):
    api = _make_api()
    _bind_thread(monkeypatch)
    by_id = {"t1": _task()}
    by_id["t1"]["last_msg_id"] = 50
    monkeypatch.setattr(core, "load_task", lambda tid: by_id.get(tid))

    api.client = None
    assert api.scan_append("t1", {}) == {"ok": False, "need_login": True}
    api.client = object()
    assert api.scan_append("missing", {}) == {"ok": False,
                                              "error": "任务不存在"}

    api.jobs = FakeJobs()
    core.ACTIVE_SESSIONS["t1"] = object()
    try:
        busy = api.scan_append("t1", {"incremental": True})
    finally:
        core.ACTIVE_SESSIONS.pop("t1", None)
    assert busy["ok"] is False and busy["busy"] is True
    assert busy["error"] == "任务正在下载中，请先停止后再追加扫描"

    for bad in ("abc", -1, "1.5"):
        result = api.scan_append("t1", {"incremental": True, "min_id": bad})
        assert result == {"ok": False, "error": "min_id 必须是非负整数"}

    captured = {}

    def fake_thread(target=None, args=None, **kwargs):
        captured["args"] = args
        return FakeThread()

    monkeypatch.setattr(gui.threading, "Thread", fake_thread)
    result = api.scan_append("t1", {"incremental": True})
    assert result["ok"] is True
    merged = captured["args"][3]
    assert merged["min_id"] == 50
    assert "date_to" not in merged
    assert merged["query"] == "cat"

    api._scan_tasks.clear()
    api.scan_append("t1", {"incremental": True, "min_id": 77})
    assert captured["args"][3]["min_id"] == 77
    assert "date_to" not in captured["args"][3]

    api._scan_tasks.clear()
    legacy = _task()
    legacy["items"] = []
    by_id["t1"] = legacy
    api.scan_append("t1", {"incremental": True})
    assert "min_id" not in captured["args"][3]
    by_id["t1"]["last_msg_id"] = 50

    api._scan_tasks.clear()
    plain = {"query": "dog", "limit": 3}
    api.scan_append("t1", plain)
    assert captured["args"][3] == plain


def test_scan_append_rejects_duplicate_while_placeholder_held(monkeypatch):
    api = _make_api()
    _bind_thread(monkeypatch)
    api.jobs = FakeJobs()
    monkeypatch.setattr(core, "load_task", lambda tid: _task())
    api._scan_tasks["t1"] = "job-existing"
    result = api.scan_append("t1", {})
    assert result == {"ok": False,
                      "error": "该任务正在追加扫描中，请勿重复提交"}
    assert api.jobs.created == []


def test_scan_append_concurrent_placeholder_is_atomic(monkeypatch):
    api = _make_api()
    original_thread = _bind_thread(monkeypatch)
    jobs = FakeJobs(delay=0.03)
    api.jobs = jobs
    monkeypatch.setattr(core, "load_task", lambda tid: _task())

    results = []
    barrier = threading.Barrier(2)

    def run():
        barrier.wait()
        results.append(api.scan_append("t1", {}))

    threads = [original_thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    ok = [item for item in results if item.get("ok")]
    dup = [item for item in results if not item.get("ok")]
    assert len(ok) == 1 and len(dup) == 1
    assert dup[0]["error"] == "该任务正在追加扫描中，请勿重复提交"
    assert len(jobs.created) == 1
    assert len(api._scan_tasks) == 1


def test_scan_append_thread_start_failure_clears_placeholder_and_marks_job(
        monkeypatch):
    api = _make_api()
    api.jobs = FakeJobs()
    monkeypatch.setattr(core, "load_task", lambda tid: _task())

    class FailingThread(FakeThread):
        def start(self):
            raise RuntimeError("thread start failed")

    monkeypatch.setattr(gui.threading, "Thread", FailingThread)
    result = api.scan_append("t1", {})

    assert result == {"ok": False, "error": "追加扫描启动失败"}
    assert api._scan_tasks == {}
    updates = api.jobs.updates
    assert any(
        args and args[0] == "job-1"
        and kwargs.get("force") is True
        and kwargs.get("state") == "error"
        and kwargs.get("phase") == "error"
        and kwargs.get("phase_detail") == "追加扫描失败"
        and str(kwargs.get("error", "")).startswith("追加扫描启动失败：")
        for args, kwargs in updates
    )


class FakeClient:
    async def get_entity(self, channel):
        return SimpleNamespace(title="频道标题")


def _fake_scan_stream(events, canceled=None):
    async def stream(client, entity, params, cancel=None):
        for event in events:
            if canceled is not None and event.get("kind") == "done":
                canceled.set()
            yield event
    return stream


def _scan_api(monkeypatch, task, events, canceled=None):
    api = _make_api(FakeClient())
    api.jobs = FakeJobs()
    saved = []
    monkeypatch.setattr(core, "save_task",
                        lambda t, force=False: saved.append((t, force)) or True)
    live = {"task": task}

    def load_task(task_id):
        if live["task"] is None or live["task"].get("id") != task_id:
            return None
        return live["task"]

    monkeypatch.setattr(core, "load_task", load_task)
    monkeypatch.setattr(core, "search_videos_stream",
                        _fake_scan_stream(events, canceled))
    frames = []
    api.push_event = lambda name, payload: frames.append(payload[0]["data"])
    return api, saved, live, frames


def test_scan_coro_appends_only_real_new_items(monkeypatch):
    task = _task()
    task["items"].append(core.make_task_item(_record(10, 1)))
    task["items"][0]["status"] = "done"
    original_title = task["channel_title"]
    events = [
        {"kind": "items", "items": [
            _record(10, 1), _record(20, 2), _record(21, 2)]},
        {"kind": "items", "items": [
            _record(21, 2), _record(22, 3),
            _record(23, 3, name="v20.mp4", size=120)]},
        {"kind": "done", "records": [
            _record(10, 1), _record(20, 2), _record(21, 2), _record(22, 3)],
         "meta": {}},
    ]
    monkeypatch.setattr(gui, "SEARCH_WS_FIRST_ITEMS", 1)
    monkeypatch.setattr(gui, "SEARCH_WS_INTERVAL", 0)
    monkeypatch.setattr(gui, "SEARCH_WS_DELTA_ITEMS", 1)
    api, saved, live, frames = _scan_api(monkeypatch, task, events)

    asyncio.run(api._scan_coro("job-1", threading.Event(), "t1", {}))

    assert [ti["msg"] for ti in task["items"]] == [10, 20, 21, 22]
    assert task["channel_title"] == original_title
    assert task["items"][0]["status"] == "done"
    assert task["last_msg_id"] == 22
    final = frames[-1]
    assert final["final"] is True
    assert final["meta"]["added"] == 3
    assert final["meta"]["skipped_dup"] == 3
    assert final["meta"]["n_matched"] == 4
    incremental_items = [ti["msg"] for frame in frames[:-1]
                         for ti in frame.get("items", [])]
    assert incremental_items == [20, 21, 22]
    assert final.get("items") == []
    assert api.jobs.snapshot("job-1")["state"] == "done"
    assert saved


def test_scan_coro_cancel_keeps_partial_and_worker_clears_placeholder(
        monkeypatch):
    task = _task()
    task["last_msg_id"] = 0
    canceled = threading.Event()
    events = [
        {"kind": "items", "items": [_record(30, 3)]},
        {"kind": "done", "records": [_record(30, 3)], "meta": {}},
    ]
    api, saved, live, frames = _scan_api(monkeypatch, task, events, canceled)
    api._scan_tasks["t1"] = "job-1"

    asyncio.run(api._scan_coro("job-1", canceled, "t1", {}))
    assert [ti["msg"] for ti in task["items"]] == [30]
    assert api.jobs.snapshot("job-1")["state"] == "cancelled"

    class Future:
        def result(self):
            return None

    def fake_threadsafe(coro, loop):
        coro.close()
        return Future()

    monkeypatch.setattr(gui.asyncio, "run_coroutine_threadsafe",
                        fake_threadsafe)
    api.engine = SimpleNamespace(loop=None)
    api._scan_worker("job-1", canceled, "t1", {})
    assert "t1" not in api._scan_tasks


def test_scan_coro_task_loss_marks_error_and_worker_clears_placeholder(
        monkeypatch):
    task = _task()
    task["last_msg_id"] = 0
    api, saved, live, frames = _scan_api(monkeypatch, task, [])
    api._scan_tasks["t1"] = "job-1"

    async def stream(client, entity, params, cancel=None):
        yield {"kind": "items", "items": [_record(30, 3)]}
        live["task"] = None
        yield {"kind": "items", "items": [_record(31, 4)]}

    monkeypatch.setattr(core, "search_videos_stream", stream)
    asyncio.run(api._scan_coro("job-1", threading.Event(), "t1", {}))
    assert api.jobs.snapshot("job-1")["state"] == "error"
    assert [ti["msg"] for ti in task["items"]] == [30]

    class Future:
        def result(self):
            return None

    def fake_threadsafe(coro, loop):
        coro.close()
        return Future()

    monkeypatch.setattr(gui.asyncio, "run_coroutine_threadsafe",
                        fake_threadsafe)
    api.engine = SimpleNamespace(loop=None)
    api._scan_worker("job-1", threading.Event(), "t1", {})
    assert "t1" not in api._scan_tasks


def test_tui_append_flow_dedups_and_advances_watermark(monkeypatch, capsys):
    task = _task()
    task["items"].append(core.make_task_item(_record(10, 1)))
    original_title = task["channel_title"]
    records = [
        _record(10, 1),
        _record(60, 2, name="v10.mp4", size=110),
        _record(61, 3),
    ]
    saved = []
    monkeypatch.setattr(core, "save_task",
                        lambda t, force=False: saved.append((t, force)) or True)

    class Client:
        async def get_entity(self, channel):
            return SimpleNamespace(title="频道标题")

    async def fake_search(client, entity, params):
        return records, {}

    monkeypatch.setattr(core, "gather_search_params", lambda: {"query": "cat"})
    monkeypatch.setattr(core, "search_videos", fake_search)
    monkeypatch.setattr(core, "pause", lambda: None)

    asyncio.run(core.append_flow(Client(), task))

    assert [ti["msg"] for ti in task["items"]] == [10, 61]
    assert task["last_msg_id"] == 61
    assert task["channel_title"] == original_title
    assert saved and saved[-1][1] is True
    output = capsys.readouterr().out
    assert "已追加 1 个新视频（2 个已在清单中）" in output
