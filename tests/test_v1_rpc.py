"""V1 TR1/TR2/TR3/TR10: RPC contract, event fan-out, shutdown, body limit."""
from __future__ import annotations

import asyncio
import json
import socket
import time
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

import gui
import server_app
from _v1_support import (
    FakeApi,
    LoopThread,
    desktop_config,
    live_server,
    make_desktop_app,
)


AUTH = {"authorization": "Bearer desk-token"}


def test_tr1_rpc_method_set_is_locked(tmp_path, monkeypatch):
    expected = frozenset({
        "abort_task", "account_add_start", "account_list",
        "account_remove", "account_verify_code", "account_verify_password",
        "active_states", "choose_folder", "connect", "delete_task",
        "edit_task", "editor_data", "editor_remove", "editor_save",
        "editor_set_status", "get_appearance", "get_item_text", "get_settings",
        "get_task", "is_active", "item_action", "items_file_info", "job_cancel",
        "job_state", "jobs_state", "list_tasks",
        "login_state", "logout", "me", "open_editor", "open_folder",
        "open_item_file", "preview", "preview_cancel", "remove_items",
        "resume_task", "scan_append", "send_code", "set_appearance",
        "set_api_credentials", "set_download_root", "set_guest", "set_item",
        "start_download",
        "verify_code", "verify_password",
    })
    assert isinstance(server_app.RPC_METHODS, frozenset)
    assert len(server_app.RPC_METHODS) == 46
    assert server_app.RPC_METHODS == expected


def test_tr1_every_whitelisted_name_exists_on_gui_api():
    callable_names = {
        name
        for name in dir(gui.Api)
        if not name.startswith("_") and callable(getattr(gui.Api, name, None))
    }
    missing = sorted(server_app.RPC_METHODS - callable_names)
    assert missing == []


def test_tr1_whitelisted_methods_are_callable_through_rpc(tmp_path, monkeypatch):
    app = make_desktop_app(tmp_path, monkeypatch)
    expected = {
        "login_state": {"logged_in": False, "has_session": False, "guest": False},
        "list_tasks": [],
        "me": {"id": "fake", "name": "V1 FakeApi"},
    }
    with TestClient(app) as client:
        for method, result in expected.items():
            response = client.post(
                "/rpc",
                json={"method": method, "params": []},
                headers=AUTH,
            )
            assert response.status_code == 200, (method, response.text)
            assert response.json() == result
        settings = client.post(
            "/rpc",
            json={"method": "get_settings", "params": []},
            headers=AUTH,
        )
        assert settings.status_code == 200
        assert settings.json()["mode"] == "desktop"


@pytest.mark.parametrize(
    "method",
    ["t45_demo_job", "delete_task_everything", "_private", "  "],
)
def test_tr1_unknown_or_private_method_is_rejected(tmp_path, monkeypatch, method):
    monkeypatch.delenv("TG_T45_TEST_HOOK", raising=False)
    app = make_desktop_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        response = client.post(
            "/rpc",
            json={"method": method, "params": []},
            headers=AUTH,
        )
    assert response.status_code == 400
    assert response.json() == {"ok": False, "error": f"未知方法：{method}"}


def test_tr1_t45_hook_is_opt_in_only(tmp_path, monkeypatch):
    monkeypatch.setenv("TG_T45_TEST_HOOK", "1")
    app = make_desktop_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        response = client.post(
            "/rpc",
            json={"method": "t45_demo_job", "params": []},
            headers=AUTH,
        )
    assert response.status_code == 200
    assert response.json() == {"ok": True, "demo": True}


@pytest.mark.parametrize(
    "body",
    [
        {"params": []},
        {"method": "", "params": []},
        {"method": 42, "params": []},
        {"method": "login_state", "params": {}},
        {"method": "login_state", "params": "x"},
    ],
)
def test_tr1_malformed_rpc_request_is_rejected(tmp_path, monkeypatch, body):
    app = make_desktop_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        response = client.post("/rpc", json=body, headers=AUTH)
    assert response.status_code == 400
    assert response.json()["ok"] is False


def test_tr2_event_queue_drops_oldest_and_keeps_newest():
    loop_thread = LoopThread()
    queue = asyncio.Queue(maxsize=server_app.EVENT_QUEUE_SIZE)
    try:
        hub = server_app._EventHub(loop_thread.loop)
        hub.bind(queue)
        for index in range(server_app.EVENT_QUEUE_SIZE + 8):
            hub.push("evt", index)
        # All pushes are scheduled onto the loop; wait until every fan-out ran.
        loop_thread.run(asyncio.sleep(0.2))
        deadline = time.monotonic() + 5
        while queue.qsize() < server_app.EVENT_QUEUE_SIZE:
            assert time.monotonic() < deadline, "fan-out never filled the queue"
            time.sleep(0.01)
        values = [queue.get_nowait()["params"] for _ in range(queue.qsize())]
    finally:
        loop_thread.close()

    assert len(values) == server_app.EVENT_QUEUE_SIZE
    assert values[0] == 8
    assert values[-1] == server_app.EVENT_QUEUE_SIZE + 7
    assert 0 not in values


def test_tr2_websocket_survives_queue_overflow(tmp_path, monkeypatch):
    app = make_desktop_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        with client.websocket_connect(
            "/events", headers={"origin": "http://testserver"}
        ) as ws:
            ws.send_json({"token": "desk-token"})
            assert ws.receive_json() == {"type": "auth_ok"}
            hub = app.state.hub
            assert hub is not None
            for index in range(server_app.EVENT_QUEUE_SIZE + 8):
                hub.push("evt", index)
            received = [ws.receive_json() for _ in range(4)]

    assert all(item["event"] == "evt" for item in received)
    assert received == sorted(received, key=lambda item: item["params"])


def test_tr3_close_all_puts_sentinel_and_unbinds():
    loop_thread = LoopThread()
    queue = asyncio.Queue(maxsize=8)
    try:
        hub = server_app._EventHub(loop_thread.loop)
        hub.bind(queue)
        hub.close_all()
        deadline = time.monotonic() + 5
        while queue.empty():
            assert time.monotonic() < deadline, "close_all never sent sentinel"
            time.sleep(0.01)
        assert queue.get_nowait() is None
        hub.push("after-close", 1)
        time.sleep(0.05)
        assert queue.empty()
    finally:
        loop_thread.close()


class _FakeLoop:
    def __init__(self):
        self.stopped = False
        self.closed = False

    def is_running(self):
        return False

    def is_closed(self):
        return self.closed

    def stop(self):
        self.stopped = True

    def close(self):
        self.closed = True


class _FakeThread:
    def __init__(self):
        self.joined = []

    def join(self, timeout=None):
        self.joined.append(timeout)

    def is_alive(self):
        return False


class _FakeServer:
    should_exit = False


class _FakeHub:
    def __init__(self):
        self.close_calls = 0

    def close_all(self):
        self.close_calls += 1


def test_tr3_server_handle_shutdown_is_idempotent():
    engine = SimpleNamespace(loop=_FakeLoop())
    server = _FakeServer()
    thread = _FakeThread()
    hub = _FakeHub()
    handle = server_app.ServerHandle(
        "http://127.0.0.1:1", "tok", FakeApi(), None, object(),
        server, thread, engine, hub,
    )

    handle.shutdown()
    handle.shutdown()

    assert hub.close_calls == 1
    assert server.should_exit is True
    assert thread.joined == [server_app.SERVER_THREAD_JOIN_SECS]
    assert engine.loop.closed is True


def test_tr3_prepare_runtime_failure_stops_engine(monkeypatch):
    import pathlib

    loop = _FakeLoop()
    created = []

    class FakeEngineLoop:
        def __init__(self):
            self.loop = loop
            self.started = False
            created.append(self)

        def start(self):
            self.started = True

    monkeypatch.setattr(gui, "EngineLoop", FakeEngineLoop)

    def boom(*args, **kwargs):
        raise RuntimeError("api init failed")

    monkeypatch.setattr(gui, "Api", boom)
    monkeypatch.setattr(server_app.core, "configure_data_dir", lambda path: None)
    monkeypatch.setattr(
        server_app.core, "ensure_dirs_and_migrate", lambda: None
    )
    with pytest.raises(RuntimeError):
        server_app._prepare_runtime(server_app.RuntimeConfig(
            data_dir=pathlib.Path.cwd()
        ))
    assert len(created) == 1
    assert created[0].started is True
    assert loop.closed is True


def test_tr3_run_blocking_configures_graceful_shutdown(monkeypatch):
    captured = {}

    class FakeConfig:
        def __init__(self, app, **kwargs):
            captured.update(kwargs)

    class FakeServer:
        def __init__(self, config):
            captured["server_config"] = config

        def run(self):
            captured["ran"] = True

    monkeypatch.setattr(server_app, "_prepare_runtime",
                        lambda config: (config.normalized(),
                                        SimpleNamespace(loop=_FakeLoop()),
                                        FakeApi(), None))
    monkeypatch.setattr(server_app, "create_app",
                        lambda *args, **kwargs: object())
    monkeypatch.setattr(server_app.uvicorn, "Config", FakeConfig)
    monkeypatch.setattr(server_app.uvicorn, "Server", FakeServer)

    server_app.run_blocking(server_app.RuntimeConfig(
        data_dir=__import__("pathlib").Path.cwd(),
        host="127.0.0.1",
        port=1,
    ))

    assert captured["ran"] is True
    assert captured["timeout_graceful_shutdown"] == 10
    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 1


def test_tr10_oversize_body_gets_413(tmp_path, monkeypatch):
    app = make_desktop_app(tmp_path, monkeypatch)
    payload = {
        "method": "login_state",
        "params": [],
        "pad": "x" * (server_app.MAX_RPC_BODY_BYTES + 1),
    }
    with TestClient(app) as client:
        response = client.post("/rpc", json=payload, headers=AUTH)
    assert response.status_code == 413
    assert response.json() == {"ok": False, "error": "请求体过大"}


def test_tr10_exact_limit_body_is_accepted(tmp_path, monkeypatch):
    app = make_desktop_app(tmp_path, monkeypatch)
    skeleton = {"method": "login_state", "params": [], "pad": ""}
    base = len(json.dumps(skeleton, separators=(",", ":")).encode("utf-8"))
    payload = {
        "method": "login_state",
        "params": [],
        "pad": "x" * (server_app.MAX_RPC_BODY_BYTES - base),
    }
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    assert len(raw) == server_app.MAX_RPC_BODY_BYTES
    with TestClient(app) as client:
        response = client.post(
            "/rpc",
            content=raw,
            headers={**AUTH, "content-type": "application/json"},
        )
    assert response.status_code == 200


def test_tr10_content_length_is_checked_before_reading_body(
    tmp_path, monkeypatch
):
    app = make_desktop_app(tmp_path, monkeypatch)
    with live_server(app) as port:
        # Declare an oversized body and never send it. A Content-Length-first
        # check must answer 413 immediately; awaiting the body instead hangs.
        request = (
            "POST /rpc HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{port}\r\n"
            "Authorization: Bearer desk-token\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {server_app.MAX_RPC_BODY_BYTES + 1}\r\n"
            "Connection: close\r\n\r\n"
        ).encode("latin-1")
        with socket.create_connection(("127.0.0.1", port), timeout=2) as conn:
            conn.sendall(request)
            conn.settimeout(2)
            # A single recv() is racy: TCP may split the response, so read
            # until the peer closes (Connection: close) and only give up if
            # nothing arrives at all - which is what a hang looks like.
            chunks = []
            try:
                while True:
                    part = conn.recv(65536)
                    if not part:
                        break
                    chunks.append(part)
            except socket.timeout:
                if not chunks:
                    pytest.fail(
                        "server waited for an oversized body instead of rejecting "
                        "on Content-Length"
                    )
            data = b"".join(chunks)

    head, _, rest = data.partition(b"\r\n\r\n")
    status = int(head.split(b" ", 2)[1])
    assert status == 413
    # The raw socket sees the HTTP framing, not a bare JSON body: strip the
    # chunk-size line and any trailing chunk terminator before parsing.
    body = rest
    if b"\r\n" in body:
        body = body.split(b"\r\n", 1)[1].rsplit(b"\r\n0\r\n", 1)[0]
    assert json.loads(body) == {"ok": False, "error": "请求体过大"}
