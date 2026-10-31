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
# T60: remember-me persistent sessions; T60-followup: 7-day TTL + UA binding
# ---------------------------------------------------------------------------

REMEMBER_SECONDS = 7 * 86400


def _configured_manager(tmp_path, now):
    manager = AuthManager(tmp_path, iterations=1000, clock=lambda: now[0])
    assert manager.setup(manager.bootstrap_token(), GOOD_PASSWORD).ok
    return manager


def test_remember_days_constant_and_manager_defaults(tmp_path):
    assert server_auth.REMEMBER_DAYS == 7
    manager = AuthManager(tmp_path, iterations=1000)
    assert manager.remember_seconds == 7 * 86400
    assert manager.session_seconds == 168 * 3600


def test_remember_login_survives_restart(tmp_path):
    now = [1_000.0]
    manager = _configured_manager(tmp_path, now)
    result = manager.login(GOOD_PASSWORD, "k", remember=True)

    assert result.ok
    token = result.session_token
    assert token.startswith("r2.")
    assert token.count(".") == 3
    assert result.expires_at == int(now[0] + REMEMBER_SECONDS)
    assert manager.session_valid(token) is True

    # Simulated restart: a fresh AuthManager over the same data_dir.
    restarted = AuthManager(tmp_path, iterations=1000, clock=lambda: now[0])
    assert restarted.session_valid(token) is True

    now[0] += 6 * 86400
    assert restarted.session_valid(token) is True

    now[0] += 1 * 86400 + 1  # past 7 days
    assert restarted.session_valid(token) is False


def test_plain_login_does_not_survive_restart(tmp_path):
    now = [1_000.0]
    manager = _configured_manager(tmp_path, now)
    legacy_call = manager.login(GOOD_PASSWORD, "k")
    explicit_call = manager.login(GOOD_PASSWORD, "k", remember=False)

    assert legacy_call.ok
    assert explicit_call.ok
    assert not legacy_call.session_token.startswith("r2.")
    assert not explicit_call.session_token.startswith("r2.")
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
    _, expires, ua8, signature = token.split(".")

    flipped = ("1" if signature[0] != "1" else "0") + signature[1:]
    assert manager.session_valid(f"r2.{expires}.{ua8}.{flipped}") is False
    assert manager.session_valid(
        f"r2.{int(expires) + 1}.{ua8}.{signature}") is False
    assert manager.session_valid(
        f"r2.{int(expires) - 1}.{ua8}.{signature}") is False

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
        other_key, f"r2.{forged_exp}.{ua8}".encode("utf-8"), hashlib.sha256
    ).hexdigest()
    assert manager.session_valid(
        f"r2.{forged_exp}.{ua8}.{forged_sig}") is False

    for malformed in [
        None,
        "",
        "r2",
        "r2.",
        "r2..",
        "r2...",
        "r2.abc.sig",
        "r2.123",
        "r2.123.",
        "r2.123.456.789",
        "r2.-1." + "a" * 8 + "." + "a" * 64,
        "r2.123." + "a" * 63,
        "r2." + "9" * 400 + "." + "f" * 8 + "." + "f" * 64,
        "r2.123." + "é" * 8 + "." + "é" * 64,
        "r2.123.abcd\u00e9" + "f" * 3 + "." + "a" * 64,
        "plain-opaque-token",
        123,
        ["r2.123.abcdef01." + "f" * 64],
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
        assert "max-age=604800" in set_cookie
        assert "httponly" in set_cookie
        assert "samesite=strict" in set_cookie
        assert "path=/" in set_cookie
        token = client.cookies.get(server_auth.COOKIE_NAME)
    assert token and token.startswith("r2.")

    # Simulated restart over the same data_dir keeps the session alive.
    app2, _auth2 = make_remote_app(tmp_path, monkeypatch, clock=lambda: now[0])
    with TestClient(app2) as client:
        rpc = client.post(
            "/rpc",
            json={"method": "login_state", "params": []},
            headers={"cookie": f"{server_auth.COOKIE_NAME}={token}"},
        )
        assert rpc.status_code == 200

    now[0] += 6 * 86400
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
        assert token and not token.startswith("r2.")
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


# ---------------------------------------------------------------------------
# T60-followup: remember tokens are bound to the requesting browser (UA)
# ---------------------------------------------------------------------------

UA_ALICE = "Mozilla/5.0 (X11; Linux x86_64) alice/1.0"
UA_BOB = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) bob/2.0"


def test_T60_followup_remember_token_binds_user_agent(tmp_path):
    now = [1_000.0]
    manager = _configured_manager(tmp_path, now)

    result = manager.login(
        GOOD_PASSWORD, "k", remember=True, user_agent=UA_ALICE)
    assert result.ok
    token = result.session_token
    _, expires, ua8, signature = token.split(".")
    assert token.startswith("r2.")
    assert ua8 == hashlib.sha256(
        UA_ALICE.encode("utf-8")).hexdigest()[:8]
    assert result.expires_at == int(now[0] + 7 * 86400)
    # r2 tokens are deterministic: same UA + same second => same token
    # (stateless design; they are indistinguishable sessions).
    twin = manager.login(
        GOOD_PASSWORD, "k", remember=True, user_agent=UA_ALICE)
    assert twin.ok
    assert twin.session_token == token

    assert manager.session_valid(token, UA_ALICE) is True
    assert manager.session_valid(token, UA_BOB) is False
    assert manager.session_valid(token, "") is False  # "" hashes "-"
    assert manager.session_valid(token) is False      # default UA is ""

    # Empty UA mints with the literal "-" fingerprint, valid from any
    # empty-UA request (mint and validation sides stay symmetric).
    empty = manager.login(
        GOOD_PASSWORD, "k", remember=True, user_agent="")
    assert empty.ok
    assert empty.session_token.split(".")[2] == (
        hashlib.sha256(b"-").hexdigest()[:8])
    assert manager.session_valid(empty.session_token, "") is True
    assert manager.session_valid(empty.session_token, UA_ALICE) is False

    # Non-str UA values are treated as empty; nothing ever raises.
    none_ua = manager.login(
        GOOD_PASSWORD, "k", remember=True, user_agent=None)
    assert none_ua.ok
    assert none_ua.session_token.split(".")[2] == (
        hashlib.sha256(b"-").hexdigest()[:8])
    assert manager.session_valid(none_ua.session_token, None) is True
    assert manager.session_valid(none_ua.session_token, ["ua"]) is False

    # Non-ASCII user agents hash deterministically and stay UA-bound.
    kanji_ua = "浏览器/1.0 (测试代理)"
    kanji = manager.login(
        GOOD_PASSWORD, "k", remember=True, user_agent=kanji_ua)
    assert kanji.ok
    assert manager.session_valid(kanji.session_token, kanji_ua) is True
    assert manager.session_valid(kanji.session_token, UA_ALICE) is False


def test_T60_followup_remember_ttl_is_exactly_seven_days(tmp_path):
    now = [1_000.0]
    manager = _configured_manager(tmp_path, now)
    result = manager.login(
        GOOD_PASSWORD, "k", remember=True, user_agent=UA_ALICE)
    token = result.session_token
    expires_at = result.expires_at
    assert expires_at - int(now[0]) == 7 * 86400

    now[0] = expires_at - 1
    assert manager.session_valid(token, UA_ALICE) is True

    now[0] = expires_at  # exactly at expiry
    assert manager.session_valid(token, UA_ALICE) is False

    now[0] = expires_at + 1
    assert manager.session_valid(token, UA_ALICE) is False


def test_T60_followup_forged_and_tampered_tokens_fail(tmp_path):
    now = [1_000.0]
    manager = _configured_manager(tmp_path, now)
    result = manager.login(
        GOOD_PASSWORD, "k", remember=True, user_agent=UA_ALICE)
    token = result.session_token
    _, expires, ua8, signature = token.split(".")
    key = bytes.fromhex(json.loads(
        manager.auth_path.read_text(encoding="utf-8"))["session_key"])

    # Forged expires_at (not re-signed) is invalid from every UA.
    assert manager.session_valid(
        f"r2.{int(expires) + 86400}.{ua8}.{signature}", UA_ALICE) is False
    # Tampered ua8 (not re-signed) is invalid too.
    assert manager.session_valid(
        f"r2.{expires}.{ua8[::-1]}.{signature}", UA_ALICE) is False

    # A perfectly self-consistent token signed with the real key but minted
    # for Bob's browser does not validate from Alice's browser.
    bob_ua8 = hashlib.sha256(UA_BOB.encode("utf-8")).hexdigest()[:8]
    bob_sig = hmac.new(
        key, f"r2.{expires}.{bob_ua8}".encode("utf-8"), hashlib.sha256
    ).hexdigest()
    bob_token = f"r2.{expires}.{bob_ua8}.{bob_sig}"
    assert manager.session_valid(bob_token, UA_BOB) is True  # sanity
    assert manager.session_valid(bob_token, UA_ALICE) is False

    # Legacy r1 tokens (pre-2.4.1 format) are always invalid, even when
    # correctly signed with this instance's own key.
    r1_sig = hmac.new(
        key, f"r1.{int(expires)}".encode("utf-8"), hashlib.sha256
    ).hexdigest()
    legacy = f"r1.{int(expires)}.{r1_sig}"
    assert manager.session_valid(legacy) is False
    assert manager.session_valid(legacy, UA_ALICE) is False


def test_T60_followup_malformed_inputs_never_raise(tmp_path):
    now = [1_000.0]
    manager = _configured_manager(tmp_path, now)
    result = manager.login(
        GOOD_PASSWORD, "k", remember=True, user_agent=UA_ALICE)
    token = result.session_token

    for bad in [
        None,
        "",
        123,
        4.2,
        ["r2.123.abcdef01." + "f" * 64],
        ("r2", "123", "abcd1234", "f" * 64),
        {"r2": 1},
        token + "!",
        token + "\x00",
        "r2." + "9" * 4000 + ".abcd1234." + "f" * 64,
        "r2.1234.abcd1234." + "é" * 64,
        "r2.12 34.abcd1234." + "f" * 64,
    ]:
        assert manager.session_valid(bad, UA_ALICE) is False

    assert manager._remember_parts(["r2.1.2.3"], UA_ALICE) is None
    assert manager._remember_parts(None, UA_ALICE) is None

    # Malformed UA values never raise either; they simply never match.
    for weird_ua in [None, 123, ["ua"], {"a": 1}, "", "é-" * 200]:
        assert manager.session_valid(token, weird_ua) is False


def test_T60_followup_logout_revocation_and_rebuild(tmp_path):
    now = [1_000.0]
    manager = _configured_manager(tmp_path, now)
    a1 = manager.login(
        GOOD_PASSWORD, "k", remember=True, user_agent=UA_ALICE)
    b1 = manager.login(
        GOOD_PASSWORD, "k", remember=True, user_agent=UA_BOB)
    assert a1.ok and b1.ok
    assert a1.session_token != b1.session_token

    # Same browser, a bit later: r2 tokens are deterministic per
    # (UA, expires_at), so a different second mints a distinct token.
    now[0] += 120
    a2 = manager.login(
        GOOD_PASSWORD, "k", remember=True, user_agent=UA_ALICE)
    assert a2.ok
    assert a2.session_token != a1.session_token

    manager.logout(a1.session_token, UA_ALICE)
    assert manager.session_valid(a1.session_token, UA_ALICE) is False
    assert manager.session_valid(b1.session_token, UA_BOB) is True
    assert manager.session_valid(a2.session_token, UA_ALICE) is True

    data = json.loads(manager.auth_path.read_text(encoding="utf-8"))
    ids = [entry["id"] for entry in data["revoked_sessions"]]
    assert hashlib.sha256(
        a1.session_token.encode("utf-8")).hexdigest() in ids
    assert hashlib.sha256(
        a2.session_token.encode("utf-8")).hexdigest() not in ids

    # T61b 裁决 1「宽进严出」：登出只验签名与过期，不校验 UA。
    # 因此从 Bob 的 UA 登出同样会吊销 Alice 的令牌。
    manager.logout(a2.session_token, UA_BOB)
    assert manager.session_valid(a2.session_token, UA_ALICE) is False
    ids_after = [entry["id"] for entry in json.loads(
        manager.auth_path.read_text(encoding="utf-8"))["revoked_sessions"]]
    assert hashlib.sha256(
        a2.session_token.encode("utf-8")).hexdigest() in ids_after
    assert len(ids_after) == len(ids) + 1

    # 重复登出是幂等的：不再新增吊销条目。
    manager.logout(a2.session_token, UA_ALICE)
    assert manager.session_valid(a2.session_token, UA_ALICE) is False
    ids_again = [entry["id"] for entry in json.loads(
        manager.auth_path.read_text(encoding="utf-8"))["revoked_sessions"]]
    assert ids_again == ids_after

    # Simulated restart over the same auth.json keeps the revocations.
    restarted = AuthManager(tmp_path, iterations=1000, clock=lambda: now[0])
    assert restarted.session_valid(a1.session_token, UA_ALICE) is False
    assert restarted.session_valid(a2.session_token, UA_ALICE) is False
    assert restarted.session_valid(b1.session_token, UA_BOB) is True


def test_T60_followup_plain_session_is_not_ua_bound(tmp_path):
    now = [1_000.0]
    manager = _configured_manager(tmp_path, now)
    result = manager.login(
        GOOD_PASSWORD, "k", remember=False, user_agent=UA_ALICE)
    assert result.ok
    token = result.session_token
    assert not token.startswith("r2.")

    # In-memory sessions are exact-match keys; the UA argument is irrelevant.
    assert manager.session_valid(token, UA_ALICE) is True
    assert manager.session_valid(token, UA_BOB) is True
    assert manager.session_valid(token, "") is True

    restarted = AuthManager(tmp_path, iterations=1000, clock=lambda: now[0])
    assert restarted.session_valid(token, UA_ALICE) is False


def test_T60_followup_end_to_end_ua_mismatch_returns_401(tmp_path, monkeypatch):
    now = [50_000.0]
    app, auth = make_remote_app(tmp_path, monkeypatch, clock=lambda: now[0])
    assert auth.setup(auth.bootstrap_token(), GOOD_PASSWORD).ok

    with TestClient(app) as client:
        login = client.post(
            "/auth/login",
            json={"password": GOOD_PASSWORD, "remember": True},
            headers={"user-agent": UA_ALICE},
        )
        assert login.status_code == 200
        set_cookie = login.headers["set-cookie"].lower()
        assert "max-age=604800" in set_cookie
        assert "max-age=2592000" not in set_cookie
        token = client.cookies.get(server_auth.COOKIE_NAME)
    assert token and token.startswith("r2.")

    # Same UA on a fresh app instance (simulated restart) stays authorized.
    app2, _auth2 = make_remote_app(tmp_path, monkeypatch, clock=lambda: now[0])
    with TestClient(app2) as client:
        same = client.post(
            "/rpc",
            json={"method": "login_state", "params": []},
            headers={"cookie": f"{server_auth.COOKIE_NAME}={token}",
                     "user-agent": UA_ALICE},
        )
        assert same.status_code == 200

        # The same cookie replayed from Bob's browser is rejected.
        other = client.post(
            "/rpc",
            json={"method": "login_state", "params": []},
            headers={"cookie": f"{server_auth.COOKIE_NAME}={token}",
                     "user-agent": UA_BOB},
        )
        assert other.status_code == 401

        # An empty UA header mismatches as well.
        anon = client.post(
            "/rpc",
            json={"method": "login_state", "params": []},
            headers={"cookie": f"{server_auth.COOKIE_NAME}={token}",
                     "user-agent": ""},
        )
        assert anon.status_code == 401

        # /auth/status reflects the same UA binding.
        status_other = client.get(
            "/auth/status",
            headers={"user-agent": UA_BOB},
            cookies={server_auth.COOKIE_NAME: token},
        )
        assert status_other.json()["authenticated"] is False
        status_same = client.get(
            "/auth/status",
            headers={"user-agent": UA_ALICE},
            cookies={server_auth.COOKIE_NAME: token},
        )
        assert status_same.json()["authenticated"] is True


def test_T60_followup_end_to_end_logout_binding(tmp_path, monkeypatch):
    """T61b 裁决 1：登录 → 改 UA → 登出 → 用原 UA 访问 = 401。"""
    now = [50_000.0]
    app, auth = make_remote_app(tmp_path, monkeypatch, clock=lambda: now[0])
    assert auth.setup(auth.bootstrap_token(), GOOD_PASSWORD).ok
    with TestClient(app) as client:
        login = client.post(
            "/auth/login",
            json={"password": GOOD_PASSWORD, "remember": True},
            headers={"user-agent": UA_ALICE},
        )
        assert login.status_code == 200
        token = client.cookies.get(server_auth.COOKIE_NAME)
    assert token and token.startswith("r2.")

    app2, _auth2 = make_remote_app(tmp_path, monkeypatch, clock=lambda: now[0])
    with TestClient(app2) as client:
        alice_before = client.post(
            "/rpc",
            json={"method": "login_state", "params": []},
            headers={"cookie": f"{server_auth.COOKIE_NAME}={token}",
                     "user-agent": UA_ALICE},
        )
        assert alice_before.status_code == 200

        # 从 Bob 的 UA 登出：只验签名与过期，签名合法即写入吊销表。
        bob_logout = client.post(
            "/auth/logout",
            headers={"cookie": f"{server_auth.COOKIE_NAME}={token}",
                     "user-agent": UA_BOB},
        )
        assert bob_logout.status_code == 200

        # 换回原 UA 访问：吊销已生效 → 401（严格校验仍然保留）。
        revoked = client.post(
            "/rpc",
            json={"method": "login_state", "params": []},
            headers={"cookie": f"{server_auth.COOKIE_NAME}={token}",
                     "user-agent": UA_ALICE},
        )
        assert revoked.status_code == 401

    # 吊销记录落在 auth.json，重启后仍然 401。
    app3, _auth3 = make_remote_app(tmp_path, monkeypatch, clock=lambda: now[0])
    with TestClient(app3) as client:
        after_restart = client.post(
            "/rpc",
            json={"method": "login_state", "params": []},
            headers={"cookie": f"{server_auth.COOKIE_NAME}={token}",
                     "user-agent": UA_ALICE},
        )
        assert after_restart.status_code == 401

        # 非法的令牌（签名损坏）登出不得写入任何吊销条目，也不得抛错。
        forged = token[:-1] + ("0" if token[-1] != "0" else "1")
        forged_logout = client.post(
            "/auth/logout",
            headers={"cookie": f"{server_auth.COOKIE_NAME}={forged}",
                     "user-agent": UA_ALICE},
        )
        assert forged_logout.status_code == 200
    data = json.loads(auth.auth_path.read_text(encoding="utf-8"))
    ids = [entry["id"] for entry in data["revoked_sessions"]]
    assert hashlib.sha256(token.encode("utf-8")).hexdigest() in ids
    assert hashlib.sha256(forged.encode("utf-8")).hexdigest() not in ids
