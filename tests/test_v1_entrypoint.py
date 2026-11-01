"""V1 TR11: docker-entrypoint volume preparation and privilege drop."""
from __future__ import annotations

import importlib.util
import os
import pathlib
import sys

import pytest


ENTRYPOINT = pathlib.Path(__file__).resolve().parent.parent / "docker-entrypoint.py"


def _load():
    spec = importlib.util.spec_from_file_location("v1_entrypoint", ENTRYPOINT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def entry(monkeypatch):
    module = _load()
    monkeypatch.setattr(module.os, "umask", lambda mode: None)
    return module


@pytest.mark.parametrize("value,expected", [
    ("10001", 10001), ("1", 1), ("65534", 65534),
])
def test_tr11_id_accepts_positive_integers(entry, monkeypatch, value, expected):
    monkeypatch.setenv("PUID", value)
    assert entry._id("PUID", 10001) == expected


@pytest.mark.parametrize("value", ["0", "-1", "abc", "1.5", "", "  "])
def test_tr11_id_rejects_invalid_values(entry, monkeypatch, value):
    monkeypatch.setenv("PUID", value)
    with pytest.raises(SystemExit):
        entry._id("PUID", 10001)


def test_tr11_id_uses_default_when_unset(entry, monkeypatch):
    monkeypatch.delenv("PGID", raising=False)
    assert entry._id("PGID", 10001) == 10001


def test_tr11_main_creates_data_dir_and_execs_default(entry, tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    monkeypatch.setenv("TGDL_DATA_DIR", str(data_dir))
    monkeypatch.setattr(entry.os, "geteuid", lambda: 1000, raising=False)
    captured = []
    monkeypatch.setattr(entry.os, "execvp",
                        lambda program, args: captured.append((program, args)))
    monkeypatch.setattr(sys, "argv", ["docker-entrypoint.py"])

    entry.main()

    assert data_dir.is_dir()
    assert len(captured) == 1
    program, args = captured[0]
    assert program == sys.executable
    assert args == [
        sys.executable, "-m", "server_app",
        "--host", "0.0.0.0", "--port", "8787",
        "--data-dir", str(data_dir),
    ]


def test_tr11_main_drops_privileges_when_root(entry, tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    monkeypatch.setenv("TGDL_DATA_DIR", str(data_dir))
    monkeypatch.setenv("PUID", "10001")
    monkeypatch.setenv("PGID", "10002")
    calls = []
    monkeypatch.setattr(entry.os, "geteuid", lambda: 0, raising=False)
    # os.chown does not exist on Windows; inject the POSIX surface.
    monkeypatch.setattr(entry.os, "chown",
                        lambda path, uid, gid: calls.append(("chown", uid, gid)),
                        raising=False)
    monkeypatch.setattr(entry.os, "setgroups",
                        lambda groups: calls.append(("setgroups", list(groups))),
                        raising=False)
    monkeypatch.setattr(entry.os, "setgid",
                        lambda gid: calls.append(("setgid", gid)),
                        raising=False)
    monkeypatch.setattr(entry.os, "setuid",
                        lambda uid: calls.append(("setuid", uid)),
                        raising=False)
    monkeypatch.setattr(entry.os, "execvp", lambda program, args: None)
    monkeypatch.setattr(sys, "argv", ["docker-entrypoint.py"])

    entry.main()

    assert calls == [
        ("chown", 10001, 10002),
        ("setgroups", []),
        ("setgid", 10002),
        ("setuid", 10001),
    ]
    assert os.environ["HOME"] == str(data_dir)


def test_tr11_main_survives_chown_permission_error(entry, tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    monkeypatch.setenv("TGDL_DATA_DIR", str(data_dir))
    monkeypatch.setattr(entry.os, "geteuid", lambda: 0, raising=False)

    def deny(*args, **kwargs):
        raise PermissionError("read-only volume")

    monkeypatch.setattr(entry.os, "chown", deny, raising=False)
    monkeypatch.setattr(entry.os, "setgroups", lambda groups: None, raising=False)
    monkeypatch.setattr(entry.os, "setgid", lambda gid: None, raising=False)
    monkeypatch.setattr(entry.os, "setuid", lambda uid: None, raising=False)
    monkeypatch.setattr(entry.os, "execvp", lambda program, args: None)
    monkeypatch.setattr(sys, "argv", ["docker-entrypoint.py"])

    entry.main()

    assert data_dir.is_dir()


def test_tr11_main_honours_explicit_command(entry, tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    monkeypatch.setenv("TGDL_DATA_DIR", str(data_dir))
    monkeypatch.setattr(entry.os, "geteuid", lambda: 1000, raising=False)
    captured = []
    monkeypatch.setattr(entry.os, "execvp",
                        lambda program, args: captured.append((program, args)))
    monkeypatch.setattr(
        sys, "argv", ["docker-entrypoint.py", "python", "-m", "pytest"]
    )

    entry.main()

    assert captured == [("python", ["python", "-m", "pytest"])]


def test_tr11_main_rejects_bad_puid_before_exec(entry, tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    monkeypatch.setenv("TGDL_DATA_DIR", str(data_dir))
    monkeypatch.setenv("PUID", "root")
    monkeypatch.setattr(entry.os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setattr(entry.os, "execvp",
                        lambda *args: pytest.fail("must not exec"))
    monkeypatch.setattr(sys, "argv", ["docker-entrypoint.py"])

    with pytest.raises(SystemExit):
        entry.main()


def test_tr11_main_applies_restrictive_umask(entry, tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    monkeypatch.setenv("TGDL_DATA_DIR", str(data_dir))
    monkeypatch.setattr(entry.os, "geteuid", lambda: 1000, raising=False)
    monkeypatch.setattr(entry.os, "execvp", lambda program, args: None)
    monkeypatch.setattr(sys, "argv", ["docker-entrypoint.py"])
    seen = []
    monkeypatch.setattr(entry.os, "umask", lambda mode: seen.append(mode))

    entry.main()

    assert seen == [0o077]


def test_tr11_dockerfile_keeps_hardening_flags():
    dockerfile = (ENTRYPOINT.parent / "Dockerfile").read_text(encoding="utf-8")
    compose = (ENTRYPOINT.parent / "compose.yaml").read_text(encoding="utf-8")
    assert "HEALTHCHECK" in dockerfile
    assert "USER" not in dockerfile.split("FROM python:3.12-slim AS runtime")[1]
    assert "read_only: true" in compose
    assert "no-new-privileges:true" in compose
    assert "cap_drop:" in compose
    assert "PUID: \"${PUID:-10001}\"" in compose
    assert "PGID: \"${PGID:-10001}\"" in compose
