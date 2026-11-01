"""Dockerfile must ship every local module the app imports.

This caught a real break: wallpaper.py and taskport.py were added to gui.py
but not to the COPY line, so the container died at import time with
ModuleNotFoundError while local tests stayed green (they run from the source
tree, where the modules exist).
"""
from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
DOCKERFILE = ROOT / "Dockerfile"

# Modules the app imports that are NOT part of the standard library and are
# not installed from requirements-server.txt.
LOCAL_MODULES = ["server_app", "server_auth", "server_config", "gui", "host",
                 "tg_video_dl", "wallpaper", "taskport"]


def test_dockerfile_copies_every_local_module():
    text = DOCKERFILE.read_text(encoding="utf-8")
    # The COPY for app sources spans two lines (backslash continuation), so
    # collect every COPY block by walking until the continuation ends.
    lines = text.splitlines()
    copy_block = []
    collecting = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("COPY "):
            collecting = True
        if collecting:
            copy_block.append(line)
            if not stripped.endswith("\\"):
                collecting = False
    copy_block = "\n".join(copy_block)
    missing = [m for m in LOCAL_MODULES
               if not re.search(rf"\b{re.escape(m)}\.py\b", copy_block)]
    assert missing == [], f"Dockerfile 缺少 {missing}"


def test_every_local_module_exists():
    missing = [m for m in LOCAL_MODULES if not (ROOT / f"{m}.py").is_file()]
    assert missing == []


def test_folderpicker_shim_ships_beside_gui():
    """gui._choose_folder_win reads folderpicker.cs next to __file__."""
    assert (ROOT / "folderpicker.cs").is_file()
    text = DOCKERFILE.read_text(encoding="utf-8")
    # Windows-only path; the container does not need it, but the desktop
    # bundle does, so guard that we did not silently drop it from the tree.
    assert "folderpicker" not in text or "folderpicker.cs" in text