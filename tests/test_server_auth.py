import json

import pytest

from server_auth import AuthManager


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
