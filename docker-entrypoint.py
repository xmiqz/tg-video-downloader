#!/usr/bin/env python3
"""Prepare the data volume and drop root privileges before execing the app."""
from __future__ import annotations

import os
import pathlib
import sys


def _id(name, default):
    raw = os.environ.get(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise SystemExit(f"{name} 必须是整数") from exc
    if value < 1:
        raise SystemExit(f"{name} 必须大于 0")
    return value


def main():
    os.umask(0o077)
    data_dir = pathlib.Path(os.environ.get("TGDL_DATA_DIR", "/data")).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    if os.geteuid() == 0:
        uid = _id("PUID", 10001)
        gid = _id("PGID", 10001)
        try:
            os.chown(data_dir, uid, gid)
        except PermissionError:
            pass
        os.environ["HOME"] = str(data_dir)
        os.setgroups([])
        os.setgid(gid)
        os.setuid(uid)

    command = sys.argv[1:] or [
        sys.executable,
        "-m",
        "server_app",
        "--host",
        "0.0.0.0",
        "--port",
        "8787",
        "--data-dir",
        str(data_dir),
    ]
    os.execvp(command[0], command)


if __name__ == "__main__":
    main()
