import json

import pytest

from server_config import is_loopback_host, load_runtime_config


def test_config_precedence_and_allowed_roots(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    extra = tmp_path / "media"
    (data / "server.json").write_text(
        json.dumps({
            "host": "0.0.0.0",
            "port": 8000,
            "allowed_roots": [str(tmp_path / "file-root")],
            "cookie_secure": False,
            "session_hours": 12,
        }),
        encoding="utf-8",
    )
    monkeypatch.setenv("TGDL_DATA_DIR", str(data))
    monkeypatch.setenv("TGDL_HOST", "127.0.0.1")
    monkeypatch.setenv("TGDL_PORT", "8100")
    monkeypatch.setenv("TGDL_ALLOWED_ROOTS", str(extra))

    config = load_runtime_config(["--port", "8200"])
    assert config.data_dir == data.resolve()
    assert config.host == "127.0.0.1"
    assert config.port == 8200
    assert config.data_dir in config.allowed_roots
    assert extra.resolve() in config.allowed_roots
    assert config.loopback
    assert not config.auth_required


def test_bad_port_is_rejected(tmp_path):
    with pytest.raises(ValueError):
        load_runtime_config([
            "--data-dir", str(tmp_path),
            "--port", "70000",
        ])


def test_data_dir_can_come_from_explicit_config(tmp_path, monkeypatch):
    config_file = tmp_path / "settings.json"
    target = tmp_path / "custom-data"
    config_file.write_text(
        json.dumps({"data_dir": str(target), "host": "127.0.0.1"}),
        encoding="utf-8",
    )
    monkeypatch.setenv("TGDL_CONFIG", str(config_file))
    config = load_runtime_config([])
    assert config.data_dir == target.resolve()


def test_loopback_detection():
    assert is_loopback_host("127.0.0.1")
    assert is_loopback_host("::1")
    assert is_loopback_host("localhost")
    assert not is_loopback_host("0.0.0.0")
