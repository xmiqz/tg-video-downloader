"""v2.6 二维码登录：会话隔离、RPC 白名单与 SVG 输出。"""
from __future__ import annotations

import asyncio
import threading
import types

import gui
import telegram_login


class FakeEngine:
    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()

    def submit(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout=5)

    def close(self):
        self.loop.call_soon_threadsafe(self.loop.stop)


class FakeQR:
    url = "tg://login?token=example"
    expires = None

    def __init__(self, result=None):
        self._result = result

    async def wait(self):
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


class FakeClient:
    def __init__(self, qr):
        self._qr = qr
        self.disconnected = False

    async def connect(self):
        return None

    async def disconnect(self):
        self.disconnected = True

    async def qr_login(self):
        return self._qr

    async def sign_in(self, password=None):
        return None


def make_api(monkeypatch, qr_result=None):
    monkeypatch.setattr(gui.core, "ensure_dirs_and_migrate", lambda: None)
    monkeypatch.setattr(gui.core, "ensure_builtin_credentials",
                        lambda: {"api_id": 1, "api_hash": "a" * 32})
    api = gui.Api.__new__(gui.Api)
    api.engine = FakeEngine()
    api.client = None
    api.cfg = {}
    api.guest = False
    api._code_sent = False
    api._code_delivery = {}
    api._qr_client = None
    api._qr_pending_password = False
    api._qr_login = telegram_login.QRLoginManager()
    api._adopt_and_cache_me = lambda: None
    api.push_event = None
    fake = FakeClient(FakeQR(qr_result))
    original_start = telegram_login.QRLoginManager.start

    def start_with_fake(self, *, lease_client, client_factory, engine,
                        password_error=None):
        return original_start(
            self,
            lease_client=lease_client,
            client_factory=lambda: fake,
            engine=engine,
            password_error=password_error,
        )

    monkeypatch.setattr(
        gui.telegram_login.QRLoginManager, "start", start_with_fake,
    )
    return api, fake


def test_start_returns_svg_and_poll_waits(monkeypatch):
    api, fake = make_api(monkeypatch, types.SimpleNamespace(id=1))
    result = api.qr_login_start()
    assert result["ok"] is True
    assert "<svg" in result["svg"]
    assert "viewBox=" in result["svg"]
    assert "tg://login" not in result["svg"]
    status = api.qr_login_poll()
    assert status["ok"] is True
    assert status["stage"] == "qr_waiting"
    assert api.login_state()["qr"]["active"] is True
    api.qr_login_cancel()
    assert api.login_state()["qr"]["active"] is False
    assert fake.disconnected is True


def test_qr_start_replaces_pending_phone_code_client(monkeypatch):
    api, fake = make_api(monkeypatch, types.SimpleNamespace(id=1))
    api.client = fake
    api._code_sent = True
    api._code_delivery = {"delivery": "app"}
    result = api.qr_login_start()
    assert result["ok"] is True
    assert api.client is None
    assert api._code_sent is False


def test_qr_success_finalizes_login(monkeypatch):
    api, fake = make_api(monkeypatch, types.SimpleNamespace(id=1))
    assert api.qr_login_start()["ok"] is True
    api._qr_login._session.result = {
        "ok": True, "stage": "qr_ready", "user": types.SimpleNamespace(id=1),
    }
    status = api.qr_login_poll()
    assert status["stage"] == "ready"
    assert api.client is fake
    assert api.login_state()["logged_in"] is True


def test_qr_two_step_password(monkeypatch):
    class SessionPasswordNeededError(Exception):
        pass

    monkeypatch.setattr(
        telegram_login, "SessionPasswordNeededError",
        SessionPasswordNeededError, raising=False,
    )
    api, fake = make_api(monkeypatch, SessionPasswordNeededError())
    assert api.qr_login_start()["ok"] is True

    def sign_in(password=None):
        async def _do():
            return None
        return _do()

    api._qr_login._session.result = {
        "ok": True, "stage": "need_password", "user": None,
    }
    status = api.qr_login_poll()
    assert status["stage"] == "need_password"
    done = api.qr_login_password("123456")
    assert done["stage"] == "ready"
    assert api.client is fake


def test_qr_login_uses_main_session_path(monkeypatch):
    """扫码得到的授权必须落到主 session，否则重启后登录态丢失。"""
    captured = {}

    class RecordingClient(FakeClient):
        def __init__(self, qr):
            super().__init__(qr)

    def fake_client_factory(path, api_id, api_hash, **kwargs):
        captured["path"] = path
        return RecordingClient(FakeQR(types.SimpleNamespace(id=1)))

    monkeypatch.setattr(gui, "TelegramClient", fake_client_factory)
    monkeypatch.setattr(gui, "SessionPasswordNeededError",
                        type("SessionPasswordNeededError", (Exception,), {}),
                        raising=False)
    monkeypatch.setattr(gui.core, "ensure_dirs_and_migrate", lambda: None)
    monkeypatch.setattr(gui.core, "ensure_builtin_credentials",
                        lambda: {"api_id": 1, "api_hash": "a" * 32})
    api = gui.Api.__new__(gui.Api)
    api.engine = FakeEngine()
    api.client = None
    api.cfg = {"api_id": 1, "api_hash": "a" * 32}
    api.guest = False
    api._code_sent = False
    api._code_delivery = {}
    api._qr_client = None
    api._qr_pending_password = False
    api._qr_login = telegram_login.QRLoginManager()
    api._adopt_and_cache_me = lambda: None
    api.push_event = None

    result = api.qr_login_start()
    assert result["ok"] is True
    assert captured["path"] == gui.core.SESSION_PATH


def test_alt_qr_login_uses_alt_session_and_finalizes_pool(monkeypatch):
    """小号扫码必须写小号 session，成功后按池收口落盘 accounts。"""
    captured = {}
    saved = {"called": False}

    def fake_client_factory(path, api_id, api_hash, **kwargs):
        captured["path"] = path
        return FakeClient(FakeQR(types.SimpleNamespace(id=2)))

    monkeypatch.setattr(gui, "TelegramClient", fake_client_factory)
    monkeypatch.setattr(gui, "SessionPasswordNeededError",
                        type("SessionPasswordNeededError", (Exception,), {}),
                        raising=False)
    monkeypatch.setattr(gui.core, "ensure_dirs_and_migrate", lambda: None)
    monkeypatch.setattr(gui.core, "ensure_builtin_credentials",
                        lambda: {"api_id": 1, "api_hash": "a" * 32})
    monkeypatch.setattr(gui.core, "save_accounts_add_alt",
                        lambda *a, **k: saved.update(called=True))
    api = gui.Api.__new__(gui.Api)
    api.engine = FakeEngine()
    api.client = types.SimpleNamespace(id=9)
    api.cfg = {"api_id": 1, "api_hash": "a" * 32}
    api.guest = False
    api._code_sent = False
    api._code_delivery = {}
    api._qr_client = None
    api._qr_pending_password = False
    api._qr_login = telegram_login.QRLoginManager()
    api._adopt_and_cache_me = lambda: None
    api.push_event = None

    class FakePool:
        def __init__(self):
            self.clients = {gui.core.ACCT_MAIN: api.client, gui.core.ACCT_ALT: None}
            self.state = {}
            self.me_cache = {}
            self._adding = {}
            self._alt_phone = ""
            self._alt_cfg = False

        def alt_adding(self):
            return self._adding.get(gui.core.ACCT_ALT) is not None

    api._pool = FakePool()

    result = api.account_add_qr_start(gui.core.ACCT_ALT)
    assert result["ok"] is True
    assert result["stage"] == "qr"
    assert captured["path"] == gui.core.session_path_for(gui.core.ACCT_ALT)
    assert api._pool.state[gui.core.ACCT_ALT] == "adding"

    session = api._qr_login_alt._session
    session.result = {"ok": True, "stage": "qr_ready", "user": None}
    poll = api.account_add_qr_poll(gui.core.ACCT_ALT)
    assert poll["stage"] == "ready"
    assert api._pool.state[gui.core.ACCT_ALT] == "ready"
    assert api._pool._alt_cfg is True
    assert saved["called"] is True
