"""V1-D3: a restart must restore the saved Telegram session at boot.

Regression for the "new task spins forever after a container restart" bug:
_prepare_runtime built gui.Api but never called connect(), so api.client
stayed None even though the session file was valid. Every RPC then answered
need_login and the UI bounced to the login view instead of searching.
"""
from __future__ import annotations

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


def _prepare(monkeypatch, tmp_path, result):
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
        session_hours = 1
        cookie_secure = False
        bootstrap_token_file = None

        def normalized(self):
            return self

    server_app._prepare_runtime(_Cfg())
    return api


def test_boot_restores_saved_session(monkeypatch, tmp_path):
    api = _prepare(monkeypatch, tmp_path, {"ok": True, "stage": "ready"})
    assert api.connect_calls == 1, "启动必须尝试恢复本地会话"


def test_boot_tolerates_unauthorized_session(monkeypatch, tmp_path):
    api = _prepare(monkeypatch, tmp_path, {"ok": True, "stage": "need_phone"})
    assert api.connect_calls == 1


def test_boot_survives_connect_failure(monkeypatch, tmp_path):
    api = _FakeApi(None)

    def _boom():
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
        session_hours = 1
        cookie_secure = False
        bootstrap_token_file = None

        def normalized(self):
            return self

    # A dead network must not abort startup: the operator still needs the
    # wizard to reach the instance.
    server_app._prepare_runtime(_Cfg())