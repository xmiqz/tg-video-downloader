import asyncio
import json
import pathlib
import time
from types import SimpleNamespace

import tg_video_dl as core


def test_describe_telegram_unstable_hides_raw_text():
    exc = ValueError("Request was unsuccessful 6 time(s)")
    info = core.describe_exception(exc)
    assert info["code"] == "telegram_unstable"
    assert "Telegram 服务端暂时不稳定" in info["message"]
    assert "已自动重试 6 次" in info["message"]
    assert "已下载进度保留" in info["message"]
    assert "Request was unsuccessful" not in info["message"]
    assert info["retryable"] is True
    assert info["action"] == "retry"
    assert info["technical"] == "ValueError: Request was unsuccessful 6 time(s)"
    assert core.describe_exception(exc, max_attempts=8)["message"].find("8 次") > 0
    rpc = type("RpcCallFailError", (Exception,), {})("server hiccup")
    rpc_info = core.describe_exception(rpc)
    assert rpc_info["code"] == "telegram_unstable"
    assert "已自动重试 6 次" in rpc_info["message"]
    assert "server hiccup" not in rpc_info["message"]


def test_describe_shard_reset_and_other_codes():
    info = core.describe_exception(core.ShardResetExhausted(42, 0))
    assert info["code"] == "network_stalled"
    assert "网络停滞，已自动重置 3 次" in info["message"]
    assert info["retryable"] is True
    assert set(info) == {
        "code", "message", "retryable", "retry_after", "scope",
        "action", "technical"}
    assert core.describe_exception(core.DeterministicDownloadError("x"))[
        "code"] == "message_unavailable"
    assert core.describe_exception(TimeoutError("x"))["code"] == "network_timeout"
    assert core.describe_exception(OSError("x"))["code"] == "download_failed"
    assert core.describe_exception(
        OSError("x"), scope="scan")["message"].startswith("扫描暂时中断")
    assert core.describe_exception(
        EOFError(), scope="task")["code"] == "input_required"
    assert core.describe_exception(
        RuntimeError("x"), scope="task")["message"] == "操作暂时中断，请稍后重试"
    login_rpc = core.describe_exception(
        ValueError("Request was unsuccessful 6 time(s)"),
        scope="task")["message"]
    assert "已下载进度保留" not in login_rpc
    assert login_rpc.endswith("请稍后重试")
    long_info = core.describe_exception(ValueError("x" * 500))
    assert len(long_info["technical"]) == 240


def test_shard_stall_delay_bounds_and_jitter():
    assert core.shard_stall_delay(1, jitter=0) == 1.0
    assert core.shard_stall_delay(2, jitter=0) == 3.0
    assert core.shard_stall_delay(3, jitter=0) == 6.0
    assert core.shard_stall_delay(4, jitter=0) == 6.0
    assert core.shard_stall_delay(-1, jitter=0) == 6.0
    assert 0.85 <= core.shard_stall_delay(1, jitter=-0.15) <= 0.851
    assert 1.149 <= core.shard_stall_delay(1, jitter=0.15) <= 1.151


def test_parallel_download_resets_stall_counter_after_new_bytes(monkeypatch, tmp_path):
    class StallOnce:
        def __init__(self, size, chunks):
            self.size = size
            self.chunks = list(chunks)
            self.index = 0

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self.index < len(self.chunks):
                chunk = self.chunks[self.index]
                self.index += 1
                return b"x" * chunk
            raise asyncio.TimeoutError

    class FakeClient:
        def __init__(self):
            self.calls = []
            self.iterators = [
            StallOnce(16, [15]),
            StallOnce(16, [1, 15]),
            ]

        def iter_download(self, media, offset, request_size, file_size):
            self.calls.append(offset)
            return self.iterators.pop(0) if self.iterators else StallOnce(16, [])

        def is_connected(self):
            return True

    client = FakeClient()
    item = {"msg": SimpleNamespace(id=7, media=object()), "size": 16,
            "src": core.SRC_CHANNEL}
    ctl = SimpleNamespace(tripped=lambda: None)
    waits = []

    async def fake_sleep(seconds):
        waits.append(seconds)

    async def fake_wait_for(coro, timeout):
        try:
            return await coro
        except asyncio.TimeoutError:
            raise

    monkeypatch.setattr(core, "_SHARD_SLEEP", fake_sleep)
    monkeypatch.setattr(core, "_SHARD_WAIT_FOR", fake_wait_for)
    target = str(tmp_path / "video.mp4")
    asyncio.run(core.parallel_download(client, item, target, ctl))
    # 第 1 代迭代器收到字节后 reset 清零；第 2 代再停滞只记 1/3。
    assert len(client.calls) == 2
    assert len(waits) == 1
    assert 0.85 <= waits[0] <= 1.15
    assert pathlib.Path(target).exists()


def test_monitor_error_restart_and_snapshot_fields():
    planned = [{"mid": 1, "src": core.SRC_CHANNEL, "size": 10, "name": "a"}]
    mon = core.Monitor(planned, 1, "t", False)
    key = (core.SRC_CHANNEL, 1)

    async def run():
        await mon.error(
            key, "网络停滞，已自动重置 3 次；已下载进度保留",
            code="network_stalled", retryable=True, action="retry",
            retry_after=9, attempt=2, max_attempts=3,
            technical="ShardResetExhausted: 分片 0 停滞重置超限（3/3）")
        item = mon.snapshot_json()["items"][0]
        assert item["error_code"] == "network_stalled"
        assert item["error_retryable"] is True
        assert item["error_action"] == "retry"
        assert item["retry_at"] == 9
        assert item["retry_count"] == 2
        assert item["retry_max"] == 3
        assert "ShardResetExhausted" in item["error_technical"]
        await mon.restart(key, keep_done=True)
        cleared = mon.snapshot_json()["items"][0]
        assert cleared["error_code"] == ""
        assert cleared["error_retryable"] is False
        assert cleared["error_action"] == ""
        assert cleared["error_technical"] == ""
        assert cleared["retry_at"] is None
        assert cleared["retry_count"] == 0
        assert cleared["retry_max"] == 0

    asyncio.run(run())


def _session(monkeypatch, item_count=4):
    def find_item(task, key):
        if isinstance(key, dict):
            source = key.get("src", core.SRC_CHANNEL)
            mid = key.get("msg", key.get("mid"))
        else:
            source, mid = key[0], key[1]
        return next((x for x in task["items"]
                     if x.get("msg") == mid
                     and x.get("src", core.SRC_CHANNEL) == source), None)

    task = {
        "id": "t-monitor", "channel": "demo",
        "items": [{"msg": i, "src": core.SRC_CHANNEL, "status": "pending"}
                  for i in range(1, item_count + 1)],
    }
    task["items"][0]["msg"] = 1
    monkeypatch.setattr(core, "save_task", lambda *a, **kw: True)
    monkeypatch.setattr(core, "find_item", find_item)
    mon = core.Monitor([
        {"mid": i, "src": core.SRC_CHANNEL, "size": 10, "name": f"v{i}"}
        for i in range(1, item_count + 1)
    ], 1, "t", False)
    sess = core.DownloadSession(task, mon, asyncio.get_running_loop())
    mon.phase = "downloading"
    sess.resume_q = None
    sess.wake = None
    return sess


def _info(code="network_stalled"):
    return {
        "code": code,
        "message": "网络停滞，已自动重置 3 次；已下载进度保留",
        "retryable": True, "retry_after": None, "scope": "item",
        "action": "retry", "technical": "",
    }


def test_breaker_triggers_on_third_failure_and_recovers(monkeypatch):
    async def run():
        sess = _session(monkeypatch)
        sess.resume_q = __import__("collections").deque()
        for i in range(1, 4):
            key = (core.SRC_CHANNEL, i)
            sess.task["items"][i - 1]["status"] = "failed"
            rec = {"msg": i, "src": core.SRC_CHANNEL, "size": 10}
            result = sess.note_item_failure(key, rec, _info())
            assert result is (i == 3)
            if i < 3:
                assert sess.breaker_round == 0
        await asyncio.sleep(0)
        assert sess.breaker_round == 1
        assert list(sess.task["items"][j]["status"] for j in range(3)) == [
            "pending"] * 3
        snap = sess.state()["health"]
        assert snap["action"] == "auto_retry"
        assert snap["attempt"] == 1
        assert snap["max_attempts"] == 3
        assert 19 <= snap["retry_in"] <= 20
        assert "约 20 秒后自动重试" in snap["message"]

        async def fake_sleep(delay):
            sess.breaker_until = 0.0

        monkeypatch.setattr(core, "_BREAKER_SLEEP", fake_sleep)
        assert await sess.await_breaker() is True
        assert list(sess.resume_q) == [
            {"msg": i, "src": core.SRC_CHANNEL, "size": 10}
            for i in (1, 2, 3)]
        assert sess.state()["health"] is None
        assert sess.breaker_until == 0.0
        assert "连接恢复，继续下载" in sess.mon.phase_detail

    asyncio.run(run())


def test_breaker_restarts_message_object_records_with_numeric_keys(monkeypatch):
    async def run():
        sess = _session(monkeypatch)
        seen = []

        async def fake_restart(key, keep_done=True):
            seen.append(key)

        sess.mon.restart = fake_restart
        for i in range(1, 4):
            rec = {
                "msg": SimpleNamespace(id=i),
                "src": core.SRC_CHANNEL,
                "size": 10,
            }
            sess.note_item_failure((core.SRC_CHANNEL, i), rec, _info())
        await asyncio.sleep(0)
        assert seen == [(core.SRC_CHANNEL, 1), (core.SRC_CHANNEL, 2),
                        (core.SRC_CHANNEL, 3)]
        assert [rec["msg"].id for rec in sess.breaker_hold] == [1, 2, 3]

    asyncio.run(run())


def test_breaker_counts_mixed_retryable_codes_and_holds_during_last_round(
        monkeypatch):
    async def run():
        sess = _session(monkeypatch)

        async def fake_restart(key, keep_done=True):
            return None

        sess.mon.restart = fake_restart
        codes = ["network_stalled", "network_timeout", "download_failed"]
        for i, code in enumerate(codes, 1):
            rec = {"msg": i, "src": core.SRC_CHANNEL, "size": 10}
            assert sess.note_item_failure(
                (core.SRC_CHANNEL, i), rec, _info(code)) is (i == 3)
        await asyncio.sleep(0)
        assert sess.breaker_round == 1
        assert len(sess.breaker_hold) == 3

        # 最后一轮等待期间仍会有失败到达：必须进入 hold，不能卡在下载中。
        sess.breaker_round = core.RETRY_BREAKER_MAX_ROUNDS
        sess.breaker_until = time.monotonic() + 30
        rec4 = {"msg": 4, "src": core.SRC_CHANNEL, "size": 10}
        assert sess.note_item_failure(
            (core.SRC_CHANNEL, 4), rec4, _info("network_stalled")) is True
        assert len(sess.breaker_hold) == 4

    asyncio.run(run())


def test_breaker_exhausts_after_three_rounds_and_success_clears(monkeypatch):
    async def run():
        sess = _session(monkeypatch)
        info = _info()

        async def fast_sleep(delay):
            sess.breaker_until = 0.0

        monkeypatch.setattr(core, "_BREAKER_SLEEP", fast_sleep)
        sess.breaker_round = core.RETRY_BREAKER_MAX_ROUNDS
        for i in range(1, 4):
            assert sess.note_item_failure(
                (core.SRC_CHANNEL, i), {"msg": i, "size": 10}, info) is False
        assert sess.health["action"] == "manual_retry"
        assert sess.health["code"] == "breaker_exhausted"
        assert sess.state()["health"]["retry_in"] == 0

        sess.health = {"action": "auto_retry"}
        sess.breaker_round = 2
        sess.recent_failures.append((time.monotonic(), "network_stalled",
                                     (core.SRC_CHANNEL, 1),
                                     {"msg": 1}, info))
        sess.note_item_success()
        assert sess.health is not None
        sess.breaker_until = 0.0
        sess.note_item_success()
        assert sess.breaker_round == 0
        assert sess.health is None
        assert len(sess.recent_failures) == 0


def test_download_one_failure_info_is_three_tuple_and_friendly(monkeypatch, tmp_path):
    async def fail(*args, **kwargs):
        raise ValueError("Request was unsuccessful 6 time(s)")

    monkeypatch.setattr(core, "simple_download", fail)
    monkeypatch.setattr(core, "_RETRY_SLEEP", lambda *a: asyncio.sleep(0))
    planned = [{"mid": 55, "src": core.SRC_CHANNEL, "size": 1, "name": "v"}]
    mon = core.Monitor(planned, 1, "t", False)
    item = {"msg": SimpleNamespace(id=55), "size": 1,
            "src": core.SRC_CHANNEL, "name": "v"}
    task = {"id": "t", "items": [{"mid": 55, "status": "pending"}]}
    sess = SimpleNamespace(task=task)
    result = asyncio.run(core.download_one(
        object(), item, str(tmp_path / "v.mp4"),
        SimpleNamespace(tripped=lambda: None),
        state={"monitor": mon, "session": sess, "cfg": {"retries": 3}}))
    assert result[0] == "fail"
    assert len(result) == 3
    assert "Telegram 服务端暂时不稳定" in result[1]
    assert "已自动重试 3 次" in result[1]
    assert "Request was unsuccessful" not in result[1]
    assert result[2]["code"] == "telegram_unstable"
    assert result[2]["scope"] == "item"


def test_preview_records_keeps_partial_results_on_error(monkeypatch, capsys):
    import datetime
    rec = {"msg": SimpleNamespace(id=9, date=datetime.datetime(2026, 1, 1)),
           "size": 10,
           "name": "a.mp4", "src": core.SRC_CHANNEL}

    async def broken_stream(*args, **kwargs):
        yield {"kind": "items", "items": [rec, rec]}
        raise ValueError("Request was unsuccessful 6 time(s)")

    monkeypatch.setattr(core, "search_videos_stream", broken_stream)
    monkeypatch.setattr(core, "dedup_records", lambda rows: list({(
        r["src"], r["msg"].id): r for r in rows}.values()))
    monkeypatch.setattr(core, "paged_view", lambda *a, **kw: None)
    monkeypatch.setattr(core, "human", lambda n: str(n))
    monkeypatch.setattr(core, "ask", lambda *a, **kw: "")
    result = asyncio.run(core.preview_records(
        object(), object(), "demo",
        {"limit": 10, "query": ""}, True))
    assert len(result) == 1
    out = capsys.readouterr().out
    assert "已保留已找到的 1 条" in out
    assert "Telegram 服务端暂时不稳定" in out
    assert "Request was unsuccessful" not in out
