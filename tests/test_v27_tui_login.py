import asyncio
from types import SimpleNamespace

import tg_video_dl as core


class FakeMainClient:
    def __init__(self, authorized=False, connect_error=None):
        self.authorized = authorized
        self.connect_error = connect_error
        self.calls = []

    async def connect(self):
        self.calls.append("connect")
        if self.connect_error is not None:
            raise self.connect_error

    async def is_user_authorized(self):
        self.calls.append("is_user_authorized")
        return self.authorized

    async def get_me(self):
        self.calls.append("get_me")
        return SimpleNamespace(
            first_name="TUI", username="tui_user", phone="+8613800000000")

    async def send_code_request(self, phone):
        self.calls.append("send_code_request")
        return SimpleNamespace(type="app")

    async def start(self, phone=None, code_callback=None):
        self.calls.append("start")

    async def disconnect(self):
        self.calls.append("disconnect")


def _make_pool(client):
    pool = core.AccountPool(client_factory=lambda *args, **kwargs: client)
    cfg = {"phone": "+8613800000000", "api_id": 1, "api_hash": "hash"}
    return pool, cfg


def test_login_main_tui_existing_session_skips_code_and_start():
    client = FakeMainClient(authorized=True)
    pool, cfg = _make_pool(client)

    result = asyncio.run(pool.login_main_tui(cfg))

    assert result is client
    assert client.calls == ["connect", "is_user_authorized", "get_me"]
    assert pool.clients[core.ACCT_MAIN] is client
    assert pool.state[core.ACCT_MAIN] == "ready"
    assert pool.me_cache[core.ACCT_MAIN] == {
        "name": "TUI", "username": "tui_user", "phone": "+8613800000000"}


def test_login_main_tui_unauthorized_runs_code_and_start():
    client = FakeMainClient(authorized=False)
    pool, cfg = _make_pool(client)

    result = asyncio.run(pool.login_main_tui(cfg))

    assert result is client
    assert client.calls == [
        "connect", "is_user_authorized", "send_code_request", "start",
        "get_me"]
    assert pool.clients[core.ACCT_MAIN] is client
    assert pool.state[core.ACCT_MAIN] == "ready"


def test_login_main_tui_connect_failure_propagates():
    error = ConnectionError("Cannot send requests while disconnected")
    client = FakeMainClient(connect_error=error)
    pool, cfg = _make_pool(client)

    try:
        asyncio.run(pool.login_main_tui(cfg))
    except ConnectionError as exc:
        assert exc is error
    else:
        raise AssertionError("connect failure must propagate")

    assert client.calls == ["connect"]
    assert pool.clients[core.ACCT_MAIN] is None
    assert core.ACCT_MAIN not in pool.state
