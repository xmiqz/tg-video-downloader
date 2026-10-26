"""V1 TR8/TR9: auth boundaries and the non-ASCII credential matrix."""
from __future__ import annotations

import json

from fastapi.testclient import TestClient
import pytest
from starlette.websockets import WebSocketDisconnect

import server_app
import server_auth
from _v1_support import (
    make_desktop_app,
    make_remote_app,
)
from server_auth import AuthManager


NON_ASCII_TOKEN = "初始化令牌-é中文"
NON_ASCII_PASSWORD = "管理员密码-é中文"
GOOD_PASSWORD = "a-strong-password"


def _configured(tmp_path, monkeypatch, **kwargs):
    app, auth = make_remote_app(tmp_path, monkeypatch, **kwargs)
    setup = auth.setup(auth.bootstrap_token(), GOOD_PASSWORD)
    assert setup.ok
    return app, auth


def test_tr8_bootstrap_token_is_single_use(tmp_path, monkeypatch):
    app, auth = make_remote_app(tmp_path, monkeypatch)
    token = auth.bootstrap_token()
    with TestClient(app) as client:
        first = client.post("/auth/setup", json={
            "bootstrap_token": token,
            "password": GOOD_PASSWORD,
        })
        second = client.post("/auth/setup", json={
            "bootstrap_token": token,
            "password": "another-strong-password",
        })
    assert first.status_code == 200
    assert second.status_code == 400
    assert second.json()["error"] == "管理员密码已经设置"
    assert auth.token_file.exists() is False
    assert auth.bootstrap_token() == ""
    assert auth.login(GOOD_PASSWORD, "k").ok
    assert not auth.login("another-strong-password", "k2").ok


def test_tr8_setup_is_rejected_after_configuration(tmp_path, monkeypatch):
    app, auth = _configured(tmp_path, monkeypatch)
    with TestClient(app) as client:
        status = client.get("/auth/status")
        setup_page = client.get("/setup", follow_redirects=False)
        response = client.post("/auth/setup", json={
            "bootstrap_token": "anything",
            "password": GOOD_PASSWORD,
        })
    assert status.json()["configured"] is True
    assert status.json()["setup_required"] is False
    assert setup_page.status_code == 303
    assert setup_page.headers["location"] == "/login"
    assert response.status_code == 400
    assert response.json()["error"] == "管理员密码已经设置"


def test_tr8_session_cookie_expires(tmp_path, monkeypatch):
    now = [10_000.0]
    app, auth = _configured(
        tmp_path, monkeypatch, clock=lambda: now[0], session_hours=1
    )
    session = auth.login(GOOD_PASSWORD, "k")
    assert session.ok
    assert auth.session_valid(session.session_token) is True

    now[0] += 3600
    assert auth.session_valid(session.session_token) is False
    assert auth.session_valid(session.session_token) is False


def test_tr8_expired_cookie_is_rejected_end_to_end(tmp_path, monkeypatch):
    now = [20_000.0]
    app, auth = _configured(
        tmp_path, monkeypatch, clock=lambda: now[0], session_hours=1
    )
    with TestClient(app) as client:
        login = client.post("/auth/login", json={"password": GOOD_PASSWORD})
        assert login.status_code == 200
        assert client.post(
            "/rpc", json={"method": "login_state", "params": []}
        ).status_code == 200
        now[0] += 3601
        expired = client.post(
            "/rpc", json={"method": "login_state", "params": []}
        )
        status = client.get("/auth/status")
    assert expired.status_code == 401
    assert status.json()["authenticated"] is False


def test_tr8_rate_limit_blocks_then_unlocks(tmp_path, monkeypatch):
    now = [30_000.0]
    app, auth = _configured(
        tmp_path, monkeypatch, clock=lambda: now[0]
    )
    with TestClient(app) as client:
        for _ in range(server_auth.MAX_FAILURES):
            bad = client.post("/auth/login", json={"password": "wrong password"})
            assert bad.status_code == 401
        blocked = client.post("/auth/login", json={"password": GOOD_PASSWORD})
        now[0] += 60
        unlocked = client.post("/auth/login", json={"password": GOOD_PASSWORD})
    assert blocked.status_code == 429
    assert int(blocked.headers["retry-after"]) == 60
    assert blocked.json()["ok"] is False
    assert unlocked.status_code == 200


def test_tr8_rate_limit_is_per_client(tmp_path, monkeypatch):
    now = [40_000.0]
    app, auth = _configured(
        tmp_path,
        monkeypatch,
        clock=lambda: now[0],
        trust_proxy=True,
    )
    with TestClient(app) as client:
        for _ in range(server_auth.MAX_FAILURES):
            bad = client.post(
                "/auth/login",
                json={"password": "wrong password"},
                headers={"x-forwarded-for": "203.0.113.10"},
            )
            assert bad.status_code == 401
        blocked = client.post(
            "/auth/login",
            json={"password": GOOD_PASSWORD},
            headers={"x-forwarded-for": "203.0.113.10"},
        )
        other_client = client.post(
            "/auth/login",
            json={"password": GOOD_PASSWORD},
            headers={"x-forwarded-for": "198.51.100.20"},
        )
    assert blocked.status_code == 429
    assert other_client.status_code == 200


def test_tr8_logout_invalidates_session(tmp_path, monkeypatch):
    app, auth = _configured(tmp_path, monkeypatch)
    with TestClient(app) as client:
        assert client.post(
            "/auth/login", json={"password": GOOD_PASSWORD}
        ).status_code == 200
        assert client.post(
            "/rpc", json={"method": "login_state", "params": []}
        ).status_code == 200
        assert client.post("/auth/logout", json={}).status_code == 200
        assert client.post(
            "/rpc", json={"method": "login_state", "params": []}
        ).status_code == 401
        # The cookie is deleted client-side as well.
        assert server_auth.COOKIE_NAME not in client.cookies


@pytest.mark.parametrize(
    "payload",
    [
        "{broken",
        "[]",
        json.dumps({"version": 1, "salt": "00", "iterations": 1000}),
        json.dumps({"version": 1, "salt": "zz", "iterations": 1000,
                    "digest": "00"}),
        json.dumps({"version": 1, "salt": "00", "iterations": "many",
                    "digest": "00"}),
    ],
)
def test_tr8_corrupt_auth_file_fails_closed(tmp_path, payload):
    (tmp_path / "auth.json").write_text(payload, encoding="utf-8")
    with pytest.raises(RuntimeError):
        AuthManager(tmp_path, iterations=1000)


def test_tr8_empty_digest_locks_out_instead_of_failing_open(tmp_path):
    # Empty hex parses, so the file loads; no password can ever match it.
    (tmp_path / "auth.json").write_text(
        json.dumps({"version": 1, "salt": "00", "iterations": 1000,
                    "digest": ""}),
        encoding="utf-8",
    )
    manager = AuthManager(tmp_path, iterations=1000)
    assert manager.configured is True
    assert not manager.login(GOOD_PASSWORD, "k").ok


def test_tr8_corrupt_auth_file_blocks_service_startup(tmp_path, monkeypatch):
    (tmp_path / "auth.json").write_text("{broken", encoding="utf-8")
    with pytest.raises(RuntimeError):
        server_app._prepare_runtime(server_app.RuntimeConfig(
            data_dir=tmp_path, host="0.0.0.0"
        ))


def test_tr8_login_before_setup_is_rejected(tmp_path, monkeypatch):
    app, auth = make_remote_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        response = client.post("/auth/login", json={"password": GOOD_PASSWORD})
    assert response.status_code == 401
    assert response.json()["error"] == "尚未设置管理员密码"


def test_tr8_password_length_policy_is_enforced(tmp_path, monkeypatch):
    app, auth = make_remote_app(tmp_path, monkeypatch)
    token = auth.bootstrap_token()
    with TestClient(app) as client:
        short = client.post("/auth/setup", json={
            "bootstrap_token": token, "password": "short",
        })
        long = client.post("/auth/setup", json={
            "bootstrap_token": token,
            "password": "x" * (server_auth.MAX_PASSWORD_LENGTH + 1),
        })
        exact = client.post("/auth/setup", json={
            "bootstrap_token": token,
            "password": "x" * server_auth.MAX_PASSWORD_LENGTH,
        })
    assert short.status_code == 400
    assert short.json()["error"] == "密码至少需要 8 位"
    assert long.status_code == 400
    assert long.json()["error"] == "密码过长"
    assert exact.status_code == 200


def test_tr9_non_ascii_bearer_matrix(tmp_path, monkeypatch):
    app = make_desktop_app(tmp_path, monkeypatch, token="desk-token-ascii")
    with TestClient(app) as client:
        cases = [
            (None, 401),
            ("", 401),
            ("Bearer", 401),
            ("Bearer ", 401),
            ("Bearer wrong-ascii", 401),
            ("Basic desk-token-ascii", 401),
            ("Bearer desk-token-ascii", 200),
        ]
        for value, expected in cases:
            headers = {} if value is None else {"authorization": value}
            response = client.post(
                "/rpc",
                json={"method": "login_state", "params": []},
                headers=headers,
            )
            assert response.status_code == expected, value
        non_ascii = client.post(
            "/rpc",
            json={"method": "login_state", "params": []},
            headers=[("authorization",
                      ("Bearer " + NON_ASCII_TOKEN).encode("utf-8"))],
        )
        status = client.get(
            "/auth/status",
            headers=[("authorization",
                      ("Bearer " + NON_ASCII_TOKEN).encode("utf-8"))],
        )
    assert non_ascii.status_code == 401
    assert non_ascii.json()["error"] == "未授权或会话已过期"
    assert status.status_code == 200
    assert status.json()["authenticated"] is False


@pytest.mark.parametrize(
    "token,expected_error",
    [
        (NON_ASCII_TOKEN, "初始化令牌无效"),
        ("", "初始化令牌无效"),
        (None, "初始化令牌无效"),
        (123456, "初始化令牌无效"),
        ({"token": "x"}, "初始化令牌无效"),
    ],
)
def test_tr9_non_ascii_and_malformed_bootstrap_tokens(
    tmp_path, monkeypatch, token, expected_error
):
    app, auth = make_remote_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        response = client.post("/auth/setup", json={
            "bootstrap_token": token,
            "password": GOOD_PASSWORD,
        })
    assert response.status_code == 400
    assert response.json()["error"] == expected_error
    assert auth.configured is False
    assert auth.token_file.exists()


def test_tr9_ws_first_frame_matrix(tmp_path, monkeypatch):
    app = make_desktop_app(tmp_path, monkeypatch, token="desk-token-ascii")
    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect) as non_ascii:
            with client.websocket_connect(
                "/events", headers={"origin": "http://testserver"}
            ) as ws:
                ws.send_json({"token": NON_ASCII_TOKEN})
                ws.receive_json()
        with pytest.raises(WebSocketDisconnect) as missing:
            with client.websocket_connect(
                "/events", headers={"origin": "http://testserver"}
            ) as ws:
                ws.send_json({"not_token": "x"})
                ws.receive_json()
        with pytest.raises(WebSocketDisconnect) as not_object:
            with client.websocket_connect(
                "/events", headers={"origin": "http://testserver"}
            ) as ws:
                ws.send_json(["desk-token-ascii"])
                ws.receive_json()
        with client.websocket_connect(
            "/events", headers={"origin": "http://testserver"}
        ) as ws:
            ws.send_json({"token": "desk-token-ascii"})
            assert ws.receive_json() == {"type": "auth_ok"}
    assert non_ascii.value.code == 4401
    assert missing.value.code == 4401
    assert not_object.value.code == 4401


def test_tr9_ws_first_frame_timeout_closes_4401(tmp_path, monkeypatch):
    import time

    app = make_desktop_app(tmp_path, monkeypatch, token="desk-token-ascii")
    monkeypatch.setattr(server_app, "AUTH_TIMEOUT_SECS", 0.05)
    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect) as excinfo:
            with client.websocket_connect(
                "/events", headers={"origin": "http://testserver"}
            ) as ws:
                time.sleep(0.2)
                ws.receive_json()
    assert excinfo.value.code == 4401


def test_tr9_non_ascii_password_matrix(tmp_path, monkeypatch):
    manager = AuthManager(tmp_path, iterations=1000)
    setup = manager.setup(manager.bootstrap_token(), NON_ASCII_PASSWORD)
    assert setup.ok

    good = manager.login(NON_ASCII_PASSWORD, "k")
    bad = manager.login("错的密码-é中文", "k2")
    assert good.ok
    assert not bad.ok
    assert bad.error == "密码错误"


@pytest.mark.parametrize(
    "password,expected_error",
    [
        ("short", "密码错误"),
        ("x" * (server_auth.MAX_PASSWORD_LENGTH + 1), "密码错误"),
        ({"password": "a-strong-password"}, "密码错误"),
        (12345678, "密码错误"),
        (None, "密码错误"),
    ],
)
def test_tr9_malformed_password_is_cleanly_rejected(
    tmp_path, password, expected_error
):
    manager = AuthManager(tmp_path, iterations=1000)
    assert manager.setup(manager.bootstrap_token(), GOOD_PASSWORD).ok
    result = manager.login(password, "k")
    assert not result.ok
    assert result.error == expected_error


def test_tr9_tokens_equal_helper_never_raises():
    for left, right in [
        (NON_ASCII_TOKEN, NON_ASCII_TOKEN),
        (NON_ASCII_TOKEN, "ascii"),
        ("", ""),
        (None, None),
        (b"\xff\xfe", b"\xff\xfe"),
        (b"\xff", "ascii"),
        (123, 123),
        (["x"], ["x"]),
    ]:
        assert isinstance(server_auth.tokens_equal(left, right), bool)
    assert server_auth.tokens_equal(NON_ASCII_TOKEN, NON_ASCII_TOKEN) is True
    assert server_auth.tokens_equal(NON_ASCII_TOKEN, "other") is False
    assert server_auth.tokens_equal("", "") is False


def test_tr9_remote_setup_accepts_non_ascii_password_end_to_end(
    tmp_path, monkeypatch
):
    app, auth = make_remote_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        setup = client.post("/auth/setup", json={
            "bootstrap_token": auth.bootstrap_token(),
            "password": NON_ASCII_PASSWORD,
        })
        assert setup.status_code == 200
        assert client.post("/auth/logout", json={}).status_code == 200
        assert client.post(
            "/auth/login", json={"password": NON_ASCII_PASSWORD}
        ).status_code == 200
        assert client.post("/auth/logout", json={}).status_code == 200
        assert client.post(
            "/auth/login", json={"password": "错的密码-é中文"}
        ).status_code == 401
