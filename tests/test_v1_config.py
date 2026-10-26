"""V1 TR12: runtime config precedence, validation, and derived URLs."""
from __future__ import annotations

import json

import pytest

from server_config import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    RuntimeConfig,
    _as_roots,
    is_loopback_host,
    load_runtime_config,
)


@pytest.fixture(autouse=True)
def clean_env(tmp_path, monkeypatch):
    for name in [
        "TGDL_HOST", "TGDL_PORT", "TGDL_ALLOWED_ROOTS", "TGDL_TRUST_PROXY",
        "TGDL_COOKIE_SECURE", "TGDL_SESSION_HOURS", "TGDL_SETUP_TOKEN_FILE",
        "TGDL_CONFIG",
    ]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TGDL_DATA_DIR", str(tmp_path / "data"))
    (tmp_path / "data").mkdir(exist_ok=True)
    return tmp_path


def test_tr12_defaults(clean_env):
    config = load_runtime_config([])
    assert config.host == DEFAULT_HOST
    assert config.port == DEFAULT_PORT
    assert config.loopback is True
    assert config.auth_required is False
    assert config.data_dir in config.allowed_roots
    assert config.bootstrap_token_file == config.data_dir / ".setup-token"
    assert config.base_url == f"http://127.0.0.1:{DEFAULT_PORT}"


def test_tr12_precedence_cli_over_env_over_file(clean_env, monkeypatch):
    data = clean_env / "data"
    (data / "server.json").write_text(json.dumps({
        "host": "0.0.0.0",
        "port": 8000,
        "session_hours": 24,
        "trust_proxy": False,
        "cookie_secure": False,
    }), encoding="utf-8")
    monkeypatch.setenv("TGDL_HOST", "127.0.0.1")
    monkeypatch.setenv("TGDL_PORT", "8100")
    monkeypatch.setenv("TGDL_SESSION_HOURS", "48")
    monkeypatch.setenv("TGDL_TRUST_PROXY", "1")

    config = load_runtime_config(["--port", "8200"])
    assert config.host == "127.0.0.1"
    assert config.port == 8200
    assert config.session_hours == 48
    assert config.trust_proxy is True

    config = load_runtime_config(["--host", "0.0.0.0", "--port", "8300"])
    assert config.host == "0.0.0.0"
    assert config.port == 8300
    assert config.auth_required is True


def test_tr12_cli_roots_override_env_and_file(clean_env, monkeypatch):
    first = clean_env / "first"
    second = clean_env / "second"
    first.mkdir()
    second.mkdir()
    monkeypatch.setenv("TGDL_ALLOWED_ROOTS", str(second))
    config = load_runtime_config([
        "--allowed-root", str(first),
        "--allowed-root", str(second),
    ])
    assert first.resolve() in config.allowed_roots
    assert second.resolve() in config.allowed_roots
    assert config.data_dir in config.allowed_roots


def test_tr12_relative_roots_resolve_against_data_dir(clean_env):
    config = load_runtime_config(["--allowed-root", "media"])
    assert (config.data_dir / "media").resolve() in config.allowed_roots


def test_tr12_roots_are_deduplicated(clean_env):
    target = clean_env / "same"
    target.mkdir()
    config = load_runtime_config([
        "--allowed-root", str(target),
        "--allowed-root", str(target),
        "--allowed-root", str(target) + "/.",
    ])
    assert len(config.allowed_roots) == len(set(config.allowed_roots))


@pytest.mark.parametrize("port", ["0", "70000", "-1", "65536"])
def test_tr12_invalid_ports_are_rejected(clean_env, port):
    with pytest.raises(ValueError):
        load_runtime_config(["--port", port])


@pytest.mark.parametrize("hours", ["0", "8761", "-5"])
def test_tr12_invalid_session_hours_are_rejected(clean_env, monkeypatch, hours):
    monkeypatch.setenv("TGDL_SESSION_HOURS", hours)
    with pytest.raises(ValueError):
        load_runtime_config([])


@pytest.mark.parametrize("hours", ["1", "8760"])
def test_tr12_session_hour_bounds_are_inclusive(clean_env, monkeypatch, hours):
    monkeypatch.setenv("TGDL_SESSION_HOURS", hours)
    assert load_runtime_config([]).session_hours == int(hours)


def test_tr12_bad_port_string_raises_value_error(clean_env, monkeypatch):
    monkeypatch.setenv("TGDL_PORT", "not-a-port")
    with pytest.raises(ValueError):
        load_runtime_config([])


def test_tr12_config_file_must_be_json_object(clean_env):
    path = clean_env / "bad.json"
    path.write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError):
        load_runtime_config(["--config", str(path)])
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError):
        load_runtime_config(["--config", str(path)])


def test_tr12_missing_config_file_falls_back_to_defaults(clean_env):
    config = load_runtime_config(["--config", str(clean_env / "absent.json")])
    assert config.host == DEFAULT_HOST
    assert config.port == DEFAULT_PORT


def test_tr12_data_dir_from_file_is_relative_to_config_file(clean_env, monkeypatch):
    monkeypatch.delenv("TGDL_DATA_DIR", raising=False)
    config_dir = clean_env / "cfg"
    config_dir.mkdir()
    path = config_dir / "server.json"
    path.write_text(json.dumps({"data_dir": "volume"}), encoding="utf-8")
    config = load_runtime_config(
        ["--config", str(path)],
    )
    assert config.data_dir == (config_dir / "volume").resolve()


def test_tr12_explicit_data_dir_wins_over_config_file(clean_env):
    config_dir = clean_env / "cfg2"
    config_dir.mkdir()
    path = config_dir / "server.json"
    path.write_text(json.dumps({"data_dir": "volume"}), encoding="utf-8")
    explicit = clean_env / "explicit"
    config = load_runtime_config([
        "--config", str(path), "--data-dir", str(explicit),
    ])
    assert config.data_dir == explicit.resolve()


def test_tr12_setup_token_file_is_relative_to_data_dir(clean_env):
    config = load_runtime_config(["--config", str(clean_env / "none.json")])
    path = clean_env / "secrets" / "bootstrap.txt"
    path.parent.mkdir()
    path.write_text("tok", encoding="utf-8")
    config = load_runtime_config(
        ["--config", str(clean_env / "none.json")],
    )
    config = RuntimeConfig(
        data_dir=config.data_dir,
        bootstrap_token_file=path,
    ).normalized()
    assert config.bootstrap_token_file == path.resolve()


@pytest.mark.parametrize("value,expected", [
    ("1", True), ("true", True), ("YES", True), ("on", True),
    ("0", False), ("false", False), ("no", False), ("", False),
    ("anything-else", False),
])
def test_tr12_bool_env_parsing(clean_env, monkeypatch, value, expected):
    monkeypatch.setenv("TGDL_COOKIE_SECURE", value)
    assert load_runtime_config([]).cookie_secure is expected


def test_tr12_trust_proxy_affects_client_key(clean_env, monkeypatch):
    from server_auth import client_key
    from _v1_support import request_like
    from types import SimpleNamespace

    request = SimpleNamespace(
        client=SimpleNamespace(host="10.0.0.5"),
        headers={"x-forwarded-for": "203.0.113.9, 10.0.0.5"},
        url=SimpleNamespace(path="/auth/login"),
    )
    assert client_key(request, trust_proxy=True).startswith("203.0.113.9|")
    assert client_key(request, trust_proxy=False).startswith("10.0.0.5|")


@pytest.mark.parametrize("host,expected", [
    ("127.0.0.1", True),
    ("127.255.255.254", True),
    ("::1", True),
    ("localhost", True),
    ("LOCALHOST", True),
    (" 127.0.0.1 ", True),
    ("0.0.0.0", False),
    ("::", False),
    ("10.0.0.1", False),
    ("example.com", False),
    ("", False),
    (None, False),
])
def test_tr12_loopback_detection_matrix(host, expected):
    assert is_loopback_host(host) is expected


@pytest.mark.parametrize("host,expected_url", [
    ("127.0.0.1", "http://127.0.0.1:8787"),
    ("0.0.0.0", "http://127.0.0.1:8787"),
    ("::", "http://127.0.0.1:8787"),
    ("::1", "http://[::1]:8787"),
    ("example.com", "http://example.com:8787"),
])
def test_tr12_base_url_formatting(tmp_path, host, expected_url):
    assert RuntimeConfig(data_dir=tmp_path, host=host).base_url == expected_url


def test_tr12_wildcard_host_requires_auth(tmp_path):
    assert RuntimeConfig(data_dir=tmp_path, host="0.0.0.0").auth_required is True
    assert RuntimeConfig(data_dir=tmp_path, host="127.0.0.1").auth_required is False


def test_tr12_normalized_is_idempotent(tmp_path):
    config = RuntimeConfig(
        data_dir=tmp_path / "x", allowed_roots=(tmp_path / "y",)
    ).normalized()
    again = config.normalized()
    assert again == config


def test_tr12_as_roots_rejects_bad_type(tmp_path):
    with pytest.raises(ValueError):
        _as_roots(123, tmp_path)


def test_tr12_as_roots_accepts_pathsep_string(tmp_path, monkeypatch):
    import os
    first = tmp_path / "a"
    second = tmp_path / "b"
    roots = _as_roots(os.pathsep.join([str(first), str(second)]), tmp_path)
    assert roots == (first.resolve(), second.resolve())


def test_tr12_empty_roots_fall_back_to_data_dir(tmp_path):
    config = RuntimeConfig(data_dir=tmp_path, allowed_roots=()).normalized()
    assert config.allowed_roots == (tmp_path.resolve(),)


def test_tr12_data_dir_is_prepended_to_roots(tmp_path):
    other = tmp_path / "other"
    config = RuntimeConfig(
        data_dir=tmp_path / "data", allowed_roots=(other,)
    ).normalized()
    assert config.allowed_roots[0] == (tmp_path / "data").resolve()
    assert other.resolve() in config.allowed_roots
