import hashlib
import hmac
import json
import secrets

import pytest
from fastapi.testclient import TestClient

import server_auth
from _v1_support import make_remote_app
from server_auth import AuthManager


NON_ASCII_TOKEN = "初始化令牌-é中文"
NON_ASCII_PASSWORD = "管理员密码-é中文"
GOOD_PASSWORD = "a-strong-password"


def test_setup_and_session_lifecycle(tmp_path):
    manager = AuthManager(tmp_path, iterations=1000)
    assert not manager.configured
    token = manager.bootstrap_token()
    assert token
    assert manager.token_file.exists()

    bad = manager.setup("wrong", "correct horse battery staple")
    assert not bad.ok

    result = manager.setup(token, "correct horse battery staple")
    assert result.ok
    assert manager.configured
    assert not manager.token_file.exists()
    assert manager.session_valid(result.session_token)
    assert json.loads(manager.auth_path.read_text(encoding="utf-8"))["version"] == 1

    manager.logout(result.session_token)
    assert not manager.session_valid(result.session_token)


def test_login_rate_limit_and_unlock(tmp_path):
    now = [1000.0]
    manager = AuthManager(tmp_path, iterations=1000, clock=lambda: now[0])
    token = manager.bootstrap_token()
    assert manager.setup(token, "a-strong-password").ok

    for _ in range(5):
        assert not manager.login("wrong password", "127.0.0.1|/auth/login").ok

    limited = manager.login("a-strong-password", "127.0.0.1|/auth/login")
    assert not limited.ok
    assert limited.retry_after == 60

    now[0] += 61
    accepted = manager.login("a-strong-password", "127.0.0.1|/auth/login")
    assert accepted.ok


def test_corrupt_auth_file_fails_closed(tmp_path):
    (tmp_path / "auth.json").write_text("{broken", encoding="utf-8")
    with pytest.raises(RuntimeError):
        AuthManager(tmp_path)


def test_setup_rejects_non_ascii_bootstrap_token(tmp_path):
    manager = AuthManager(tmp_path, iterations=1000)
    assert manager.bootstrap_token().isascii()

    result = manager.setup(NON_ASCII_TOKEN, "a-strong-password")

    assert not result.ok
    assert result.error == "初始化令牌无效"
    assert not manager.configured


def test_setup_and_login_accept_non_ascii_password(tmp_path):
    manager = AuthManager(tmp_path, iterations=1000)
    token = manager.bootstrap_token()

    setup = manager.setup(token, NON_ASCII_PASSWORD)
    assert setup.ok
    assert manager.configured

    accepted = manager.login(NON_ASCII_PASSWORD, "127.0.0.1|/auth/login")
    assert accepted.ok

    rejected = manager.login("错的密码-é中文", "127.0.0.1|/auth/login")
    assert not rejected.ok
    assert rejected.error == "密码错误"


# ---------------------------------------------------------------------------
# T60: remember-me (30-day) persistent sessions
# ---------------------------------------------------------------------------

REMEMBER_SECONDS = 30 * 86400


def _configured_manager(tmp_path, now):
    manager = AuthManager(tmp_path, iterations=1000, clock=lambda: now[0])
    assert manager.setup(manager.bootstrap_token(), GOOD_PASSWORD).ok
    return manager


def test_remember_days_constant_and_manager_defaults(tmp_path):
    assert server_auth.REMEMBER_DAYS == 30
    manager = AuthManager(tmp_path, iterations=1000)
    assert manager.remember_seconds == 30 * 86400
    assert manager.session_seconds == 168 * 3600


def test_remember_login_survives_restart(tmp_path):
    now = [1_000.0]
    manager = _configured_manager(tmp_path, now)
    result = manager.login(GOOD_PASSWORD, "k", remember=True)

    assert result.ok
    token = result.session_token
    assert token.startswith("r1.")
    assert token.count(".") == 2
    assert result.expires_at == int(now[0] + REMEMBER_SECONDS)
    assert manager.session_valid(token) is True

    # Simulated restart: a fresh AuthManager over the same data_dir.
    restarted = AuthManager(tmp_path, iterations=1000, clock=lambda: now[0])
    assert restarted.session_valid(token) is True

    now[0] += 29 * 86400
    assert restarted.session_valid(token) is True

    now[0] += 1 * 86400 + 1  # past 30 days
    assert restarted.session_valid(token) is False


def test_plain_login_does_not_survive_restart(tmp_path):
    now = [1_000.0]
    manager = _configured_manager(tmp_path, now)
    legacy_call = manager.login(GOOD_PASSWORD, "k")
    explicit_call = manager.login(GOOD_PASSWORD, "k", remember=False)

    assert legacy_call.ok
    assert explicit_call.ok
    assert not legacy_call.session_token.startswith("r1.")
    assert not explicit_call.session_token.startswith("r1.")
    assert manager.session_valid(legacy_call.session_token) is True
    assert manager.session_valid(explicit_call.session_token) is True

    restarted = AuthManager(tmp_path, iterations=1000, clock=lambda: now[0])
    assert restarted.session_valid(legacy_call.session_token) is False
    assert restarted.session_valid(explicit_call.session_token) is False


def test_remember_logout_revocation_survives_restart(tmp_path):
    now = [1_000.0]
    manager = _configured_manager(tmp_path, now)
    result = manager.login(GOOD_PASSWORD, "k", remember=True)
    token = result.session_token
    assert manager.session_valid(token) is True

    manager.logout(token)
    assert manager.session_valid(token) is False

    stored = (tmp_path / "auth.json").read_text(encoding="utf-8")
    data = json.loads(stored)
    token_id = hashlib.sha256(token.encode("utf-8")).hexdigest()
    assert any(
        entry["id"] == token_id and entry["expires_at"] == result.expires_at
        for entry in data["revoked_sessions"]
    )
    assert token not in stored  # only the sha256 id is persisted

    restarted = AuthManager(tmp_path, iterations=1000, clock=lambda: now[0])
    assert restarted.session_valid(token) is False


def test_tampered_and_malformed_remember_tokens_fail(tmp_path):
    now = [1_000.0]
    manager = _configured_manager(tmp_path, now)
    result = manager.login(GOOD_PASSWORD, "k", remember=True)
    token = result.session_token
    _, expires, signature = token.split(".")

    flipped = ("1" if signature[0] != "1" else "0") + signature[1:]
    assert manager.session_valid(f"r1.{expires}.{flipped}") is False
    assert manager.session_valid(f"r1.{int(expires) + 1}.{signature}") is False
    assert manager.session_valid(f"r1.{int(expires) - 1}.{signature}") is False

    other_dir = tmp_path / "other-instance"
    other = _configured_manager(other_dir, now)
    other_data = json.loads(
        other.auth_path.read_text(encoding="utf-8"))
    other_key = bytes.fromhex(other_data["session_key"])
    assert other_key != bytes.fromhex(
        json.loads(manager.auth_path.read_text(encoding="utf-8"))["session_key"]
    )
    forged_exp = int(now[0] + 86400)
    forged_sig = hmac.new(
        other_key, f"r1.{forged_exp}".encode("utf-8"), hashlib.sha256
    ).hexdigest()
    assert manager.session_valid(f"r1.{forged_exp}.{forged_sig}") is False

    for malformed in [
        None,
        "",
        "r1",
        "r1.",
        "r1..",
        "r1.abc.sig",
        "r1.123",
        "r1.123.",
        "r1.123.456.789",
        "r1.-1." + "a" * 64,
        "r1.123." + "a" * 63,
        "r1." + "9" * 400 + "." + "f" * 64,
        "r1.123." + "é" * 64,
        "plain-opaque-token",
        123,
    ]:
        assert manager.session_valid(malformed) is False

    now[0] = result.expires_at + 1
    assert manager.session_valid(token) is False


def test_legacy_auth_json_is_backfilled(tmp_path):
    salt = secrets.token_bytes(16).hex()
    digest = hashlib.pbkdf2_hmac(
        "sha256", GOOD_PASSWORD.encode("utf-8"),
        bytes.fromhex(salt), 1000,
    ).hex()
    legacy = {
        "version": 1,
        "salt": salt,
        "iterations": 1000,
        "digest": digest,
    }
    (tmp_path / "auth.json").write_text(
        json.dumps(legacy, indent=2), encoding="utf-8")

    manager = AuthManager(tmp_path, iterations=1000)
    assert manager.configured is True

    data = json.loads((tmp_path / "auth.json").read_text(encoding="utf-8"))
    assert data["version"] == legacy["version"]
    assert data["salt"] == legacy["salt"]
    assert data["iterations"] == legacy["iterations"]
    assert data["digest"] == legacy["digest"]
    assert len(data["session_key"]) == 64
    bytes.fromhex(data["session_key"])  # must parse
    assert data["revoked_sessions"] == []

    assert manager.login(GOOD_PASSWORD, "k").ok
    assert not manager.login("wrong-password", "k2").ok


def test_no_auth_file_is_created_when_unconfigured(tmp_path):
    manager = AuthManager(tmp_path, iterations=1000)
    assert not manager.configured
    assert not (tmp_path / "auth.json").exists()


def test_setup_writes_remember_fields(tmp_path):
    manager = AuthManager(tmp_path, iterations=1000)
    assert manager.setup(manager.bootstrap_token(), GOOD_PASSWORD).ok

    data = json.loads(manager.auth_path.read_text(encoding="utf-8"))
    for key in ("version", "salt", "iterations", "digest", "created_at"):
        assert key in data
    assert len(data["session_key"]) == 64
    bytes.fromhex(data["session_key"])
    assert data["revoked_sessions"] == []


def test_cookie_kwargs_signature_is_backward_compatible(tmp_path):
    manager = AuthManager(tmp_path, iterations=1000)
    assert manager.cookie_kwargs() == {
        "key": server_auth.COOKIE_NAME,
        "httponly": True,
        "samesite": "strict",
        "secure": False,
        "max_age": 168 * 3600,
        "path": "/",
    }
    tuned = manager.cookie_kwargs(max_age=1234, secure=True)
    assert tuned["max_age"] == 1234
    assert tuned["secure"] is True
    partial = manager.cookie_kwargs(max_age=REMEMBER_SECONDS)
    assert partial["secure"] is False
    assert partial["max_age"] == REMEMBER_SECONDS


def test_remember_cookie_end_to_end(tmp_path, monkeypatch):
    now = [50_000.0]
    app, auth = make_remote_app(tmp_path, monkeypatch, clock=lambda: now[0])
    assert auth.setup(auth.bootstrap_token(), GOOD_PASSWORD).ok

    with TestClient(app) as client:
        login = client.post("/auth/login", json={
            "password": GOOD_PASSWORD,
            "remember": True,
        })
        assert login.status_code == 200
        set_cookie = login.headers["set-cookie"].lower()
        assert "max-age=2592000" in set_cookie
        assert "httponly" in set_cookie
        assert "samesite=strict" in set_cookie
        assert "path=/" in set_cookie
        token = client.cookies.get(server_auth.COOKIE_NAME)
    assert token and token.startswith("r1.")

    # Simulated restart over the same data_dir keeps the session alive.
    app2, _auth2 = make_remote_app(tmp_path, monkeypatch, clock=lambda: now[0])
    with TestClient(app2) as client:
        rpc = client.post(
            "/rpc",
            json={"method": "login_state", "params": []},
            headers={"cookie": f"{server_auth.COOKIE_NAME}={token}"},
        )
        assert rpc.status_code == 200

    now[0] += 20 * 86400
    app3, _auth3 = make_remote_app(tmp_path, monkeypatch, clock=lambda: now[0])
    with TestClient(app3) as client:
        rpc = client.post(
            "/rpc",
            json={"method": "login_state", "params": []},
            headers={"cookie": f"{server_auth.COOKIE_NAME}={token}"},
        )
        assert rpc.status_code == 200

    now[0] = 50_000.0 + REMEMBER_SECONDS + 1
    app4, _auth4 = make_remote_app(tmp_path, monkeypatch, clock=lambda: now[0])
    with TestClient(app4) as client:
        rpc = client.post(
            "/rpc",
            json={"method": "login_state", "params": []},
            headers={"cookie": f"{server_auth.COOKIE_NAME}={token}"},
        )
        assert rpc.status_code == 401


@pytest.mark.parametrize("remember_value", ["true", 1, "yes", None])
def test_remember_rejects_non_boolean_values(
        tmp_path, monkeypatch, remember_value):
    app, auth = make_remote_app(tmp_path, monkeypatch)
    assert auth.setup(auth.bootstrap_token(), GOOD_PASSWORD).ok
    with TestClient(app) as client:
        login = client.post("/auth/login", json={
            "password": GOOD_PASSWORD,
            "remember": remember_value,
        })
        assert login.status_code == 200
        set_cookie = login.headers["set-cookie"].lower()
        assert "max-age=2592000" not in set_cookie
        assert "max-age=604800" in set_cookie
        assert "samesite=strict" in set_cookie


def test_plain_cookie_rejected_after_restart_end_to_end(tmp_path, monkeypatch):
    now = [50_000.0]
    app, auth = make_remote_app(tmp_path, monkeypatch, clock=lambda: now[0])
    assert auth.setup(auth.bootstrap_token(), GOOD_PASSWORD).ok

    with TestClient(app) as client:
        login = client.post("/auth/login", json={"password": GOOD_PASSWORD})
        assert login.status_code == 200
        token = client.cookies.get(server_auth.COOKIE_NAME)
        assert token and not token.startswith("r1.")
        set_cookie = login.headers["set-cookie"].lower()
        assert "max-age=604800" in set_cookie
        assert "max-age=2592000" not in set_cookie

    app2, _auth2 = make_remote_app(tmp_path, monkeypatch, clock=lambda: now[0])
    with TestClient(app2) as client:
        rpc = client.post(
            "/rpc",
            json={"method": "login_state", "params": []},
            headers={"cookie": f"{server_auth.COOKIE_NAME}={token}"},
        )
        assert rpc.status_code == 401
