import asyncio
from types import SimpleNamespace

import tg_video_dl as core


def test_task_counts_aggregates_total_and_done_bytes():
    task = {
        "items": [
            {"status": "done", "size": 1024},
            {"status": "exists", "size": 2048},
            {"status": "pending", "size": 4096},
            {"status": "failed", "size": 512},
            {"status": "skipped"},
        ]
    }
    counts = core.task_counts(task)
    assert counts["finished"] == 2
    assert counts["total"] == 5
    assert counts["total_bytes"] == 1024 + 2048 + 4096 + 512
    assert counts["done_bytes"] == 1024 + 2048


def test_refresh_record_isolates_entity_cache_per_client_and_retries_channel_invalid():
    class ChannelInvalidError(Exception):
        pass

    class FakeClient:
        def __init__(self, tag, fail_once_for_ent=None):
            self.tag = tag
            self.fail_once_for_ent = fail_once_for_ent
            self.get_entity_calls = 0
            self.get_messages_calls = []

        async def get_entity(self, channel):
            self.get_entity_calls += 1
            return f"ent-{self.tag}-{self.get_entity_calls}"

        async def get_messages(self, ent, ids):
            self.get_messages_calls.append((ent, ids))
            if self.fail_once_for_ent is not None and ent == self.fail_once_for_ent:
                self.fail_once_for_ent = None
                raise ChannelInvalidError("stale access hash")
            return SimpleNamespace(
                id=ids,
                document=SimpleNamespace(size=2048),
            )

    main_client = FakeClient("main")
    alt_client = FakeClient("alt", fail_once_for_ent="ent-alt-1")
    alt_view = core._ClientView(alt_client, pool=None, acct=core.ACCT_ALT, deny_convert=True)

    msg = SimpleNamespace(id=101)
    item = {"msg": msg, "size": 1024, "src": core.SRC_CHANNEL}
    sess = SimpleNamespace(
        task={"id": "t1", "channel": "demo", "items": []},
        entities={},
        records={(core.SRC_CHANNEL, 101): item},
        mon=None,
    )

    async def run():
        await core.refresh_record(main_client, sess, item)
        # First alt refresh uses alt's own entity (not main's), then hits
        # ChannelInvalidError once and invalidates + re-resolves alt's entity.
        await core.refresh_record(alt_view, sess, item)
        # Subsequent call through raw alt_client reuses the refreshed alt entity.
        await core.refresh_record(alt_client, sess, item)

    asyncio.run(run())

    assert main_client.get_entity_calls == 1
    assert alt_client.get_entity_calls == 2
    assert alt_client.get_messages_calls == [
        ("ent-alt-1", 101),
        ("ent-alt-2", 101),
        ("ent-alt-2", 101),
    ]
    assert item["size"] == 2048


def test_account_pool_snapshot_exposes_flood_remain():
    import time

    pool = core.AccountPool(client_factory=lambda *a, **kw: None)
    pool.flood_until[core.ACCT_MAIN] = time.monotonic() + 25.0
    rows = pool.snapshot()
    main_row = [r for r in rows if r["id"] == core.ACCT_MAIN][0]
    assert main_row["flag"] == "flood"
    assert 20 <= main_row["flood_remain"] <= 26


def test_scan_append_incremental_computes_watermark_min_id(monkeypatch):
    import gui

    captured = {}
    api = gui.Api.__new__(gui.Api)
    api.client = object()
    api._scan_lock = __import__("threading").Lock()
    api._scan_tasks = {}

    class FakeJobs:
        def create(self, kind):
            return ("job-1", __import__("threading").Event())

    api.jobs = FakeJobs()
    monkeypatch.setattr(
        core,
        "load_task",
        lambda tid: {
            "id": tid,
            "channel": "demo",
            "last_msg_id": 500,
            "params": {"query": "test", "limit": 30},
            "items": [
                {"msg": 420, "src": core.SRC_CHANNEL},
                {"msg": 610, "src": core.SRC_CHANNEL},
                {"msg": 9999, "src": "评论区"},
            ],
        },
    )

    class FakeThread:
        def __init__(self, target=None, args=None, daemon=None, name=None):
            captured["args"] = args

        def start(self):
            pass

    monkeypatch.setattr(gui.threading, "Thread", FakeThread)
    res = api.scan_append("t1", {"incremental": True})
    assert res == {"ok": True, "job_id": "job-1"}
    scan_params = captured["args"][3]
    assert scan_params["query"] == "test"
    assert scan_params["min_id"] == 610


def test_scan_append_rejects_task_with_live_download(monkeypatch):
    """下载中的旧快照会覆盖追加条目/水位线，必须先拒绝。"""
    import threading
    import gui

    api = gui.Api.__new__(gui.Api)
    api.client = object()
    api._scan_lock = threading.Lock()
    api._scan_tasks = {}
    monkeypatch.setattr(core, "load_task", lambda tid: {"id": tid, "items": []})
    monkeypatch.setitem(core.ACTIVE_SESSIONS, "t-live", object())
    try:
        res = api.scan_append("t-live", {"incremental": True})
        assert res["ok"] is False
        assert res["busy"] is True
        assert "正在下载" in res["error"]
    finally:
        core.ACTIVE_SESSIONS.pop("t-live", None)
