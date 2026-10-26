"""Shared helpers for the V1 v2.4 backend test suite."""
from __future__ import annotations

import asyncio
import contextlib
import socket
import threading
import time
from types import SimpleNamespace

import gui
import server_app
import uvicorn
from server_auth import AuthManager
from server_config import RuntimeConfig


class FakeApi:
    def __init__(self):
        self.runtime_mode = "desktop"
        self.allowed_roots = ()
        self.cfg = {}
        self.push_event = None

    def login_state(self):
        return {"logged_in": False, "has_session": False, "guest": False}

    def list_tasks(self):
        return []

    def me(self):
        return {"id": "fake", "name": "V1 FakeApi"}

    def get_settings(self):
        root = str(self.allowed_roots[0]) if self.allowed_roots else ""
        return {"download_root": root, "mode": self.runtime_mode}

    def set_api_credentials(self, api_id, api_hash):
        return {"ok": True}

    def t45_demo_job(self):
        return {"ok": True, "demo": True}


def make_web_tree(tmp_path, monkeypatch):
    """Point the public resource lookup at a controlled web root."""
    web = tmp_path / "web"
    (web / "js").mkdir(parents=True, exist_ok=True)
    (web / "index.html").write_text("<html>v1-index</html>", encoding="utf-8")
    (web / "js" / "app.js").write_text("console.log('v1');", encoding="utf-8")
    (tmp_path / "secret.txt").write_text(
        "V1-OUTSIDE-WEB-SECRET", encoding="utf-8"
    )
    monkeypatch.setattr(gui, "resource_dir", lambda: web)
    return web


def desktop_config(tmp_path, *, host="127.0.0.1"):
    return RuntimeConfig(
        data_dir=tmp_path / "data",
        host=host,
        port=0,
        desktop_mode=True,
    ).normalized()


def make_desktop_app(tmp_path, monkeypatch, *, token="desk-token", api=None):
    make_web_tree(tmp_path, monkeypatch)
    config = desktop_config(tmp_path)
    return server_app.create_app(
        config,
        engine=object(),
        api=api or FakeApi(),
        token=token,
        auth=None,
    )


def make_remote_app(
    tmp_path,
    monkeypatch,
    *,
    trust_proxy=False,
    clock=None,
    session_hours=168,
    cookie_secure=False,
):
    make_web_tree(tmp_path, monkeypatch)
    data_dir = tmp_path / "data"
    config = RuntimeConfig(
        data_dir=data_dir,
        host="0.0.0.0",
        port=8787,
        allowed_roots=(data_dir,),
        trust_proxy=trust_proxy,
        cookie_secure=cookie_secure,
        session_hours=session_hours,
    ).normalized()
    auth = AuthManager(
        data_dir,
        session_hours=session_hours,
        cookie_secure=cookie_secure,
        iterations=1000,
        clock=clock,
    )
    app = server_app.create_app(
        config,
        engine=object(),
        api=FakeApi(),
        token="",
        auth=auth,
    )
    return app, auth


def request_like(origin=None, host="testserver"):
    headers = {"host": host}
    if origin is not None:
        headers["origin"] = origin
    return SimpleNamespace(headers=headers)


class LoopThread:
    """Run a private asyncio loop so queue callbacks can be observed."""

    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(
            target=self.loop.run_forever, name="v1-loop", daemon=True
        )
        self.thread.start()

    def run(self, coro, timeout=5):
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        return future.result(timeout)

    def close(self):
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(5)
        if not self.loop.is_closed():
            self.loop.close()


@contextlib.contextmanager
def live_server(app):
    """Serve the app on a real loopback socket.

    httpx normalizes dot segments before TestClient sees them, so traversal
    needs a raw HTTP client to exercise the server-side path handling.
    """
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    server = uvicorn.Server(uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="critical",
        access_log=False,
    ))
    server.install_signal_handlers = lambda: None
    thread = threading.Thread(target=server.run, name="v1-live", daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("live server did not start")
        time.sleep(0.02)
    try:
        yield port
    finally:
        server.should_exit = True
        thread.join(10)


def raw_http(port, target, *, method="GET", headers=None, body=b""):
    """Send a raw request so the client cannot rewrite ``..`` segments."""
    lines = [
        f"{method} {target} HTTP/1.1",
        f"Host: 127.0.0.1:{port}",
        "Connection: close",
    ]
    for key, value in (headers or {}).items():
        lines.append(f"{key}: {value}")
    if body:
        lines.append(f"Content-Length: {len(body)}")
    request = ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + body
    with socket.create_connection(("127.0.0.1", port), timeout=10) as conn:
        conn.sendall(request)
        chunks = []
        while True:
            chunk = conn.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
    data = b"".join(chunks)
    head, _, payload = data.partition(b"\r\n\r\n")
    head_lines = head.decode("latin-1").split("\r\n")
    status = int(head_lines[0].split(" ", 2)[1])
    response_headers = {}
    for line in head_lines[1:]:
        key, _, value = line.partition(":")
        response_headers[key.strip().lower()] = value.strip()
    return SimpleNamespace(
        status_code=status,
        headers=response_headers,
        content=payload,
        text=payload.decode("utf-8", "replace"),
    )


