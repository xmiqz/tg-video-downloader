"""V1-D3: a restart must restore the saved Telegram session in the background.

Regression for the "new task spins forever after a container restart" bug:
_prepare_runtime built gui.Api but never called connect(), so api.client
stayed None even though the session file was valid. The restore now runs off
the startup critical path, but must still happen for headless deployments.
"""
from __future__ import annotations

import threading
import time

import gui
import server_app


class _FakeApi:
    def __init__(self, result):
        self.client = None
        self.cfg = {}
        self.result = result
        self.connect_calls = 0
        self.push_event = None

    def connect(self):
        self.connect_calls += 1
        return self.result


def _prepare(monkeypatch, tmp_path, result, *, desktop_mode=False):
    """Run _prepare_runtime with gui.Api and EngineLoop stubbed."""
    api = _FakeApi(result)

    class _Engine:
        loop = None

        def start(self):
            pass

        def stop(self):
            pass

    monkeypatch.setattr(gui, "Api", lambda engine: api)
    monkeypatch.setattr(gui, "EngineLoop", _Engine)
    monkeypatch.setattr(server_app.core, "configure_data_dir", lambda p: None)
    monkeypatch.setattr(server_app.core, "ensure_dirs_and_migrate", lambda: None)
    monkeypatch.setattr(server_app.core, "load_config", lambda: {})

    class _Cfg:
        data_dir = tmp_path
        auth_required = False
        desktop_mode = False
        session_hours = 1
        cookie_secure = False
        bootstrap_token_file = None

        def normalized(self):
            return self

    _Cfg.desktop_mode = desktop_mode
    server_app._prepare_runtime(_Cfg())
    return api


def _wait_for_connect(api, count=1):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and api.connect_calls < count:
        time.sleep(0.01)
    assert api.connect_calls == count


def test_boot_restores_saved_session(monkeypatch, tmp_path):
    api = _prepare(monkeypatch, tmp_path, {"ok": True, "stage": "ready"})
    _wait_for_connect(api)


def test_boot_tolerates_unauthorized_session(monkeypatch, tmp_path):
    api = _prepare(monkeypatch, tmp_path, {"ok": True, "stage": "need_phone"})
    _wait_for_connect(api)


def test_boot_survives_connect_failure(monkeypatch, tmp_path):
    api = _FakeApi(None)
    attempted = threading.Event()

    def _boom():
        attempted.set()
        raise RuntimeError("network down")

    api.connect = _boom

    class _Engine:
        loop = None

        def start(self):
            pass

        def stop(self):
            pass

    monkeypatch.setattr(gui, "Api", lambda engine: api)
    monkeypatch.setattr(gui, "EngineLoop", _Engine)
    monkeypatch.setattr(server_app.core, "configure_data_dir", lambda p: None)
    monkeypatch.setattr(server_app.core, "ensure_dirs_and_migrate", lambda: None)
    monkeypatch.setattr(server_app.core, "load_config", lambda: {})

    class _Cfg:
        data_dir = tmp_path
        auth_required = False
        desktop_mode = False
        session_hours = 1
        cookie_secure = False
        bootstrap_token_file = None

        def normalized(self):
            return self

    # A dead network must not abort startup: the operator still needs the
    # wizard to reach the instance.
    server_app._prepare_runtime(_Cfg())
    assert attempted.wait(2), "后台恢复必须实际尝试连接"


def test_desktop_does_not_restore_on_startup_critical_path(monkeypatch, tmp_path):
    api = _prepare(monkeypatch, tmp_path, {"ok": True, "stage": "ready"},
                   desktop_mode=True)
    time.sleep(0.15)
    assert api.connect_calls == 0, "桌面窗口启动不得等待 Telegram 连接"
