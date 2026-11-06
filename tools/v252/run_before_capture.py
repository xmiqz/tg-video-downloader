"""Capture the four required v2.5.1 before images from a clean baseline tree."""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import shutil
import socket
import subprocess
import sys
import time
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[2]
TOOLS = pathlib.Path(__file__).resolve().parent


def _free_subst(mock_root: pathlib.Path) -> str:
    mock_root.mkdir(parents=True, exist_ok=True)
    for letter in ("T", "U", "V", "W"):
        drive = f"{letter}:"
        if pathlib.Path(drive + "\\").exists():
            continue
        p = subprocess.run(["subst", drive, str(mock_root)],
                           capture_output=True, text=True)
        if p.returncode == 0:
            return drive
    raise RuntimeError("没有可用的空闲盘符")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--work", default=str(ROOT / "work" / "v252-before"))
    args = parser.parse_args()

    baseline = pathlib.Path(args.baseline).resolve()
    if not (baseline / "gui.py").is_file():
        raise SystemExit(f"baseline 不是 v2.5.1 源码树：{baseline}")
    out = pathlib.Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    work = pathlib.Path(args.work).resolve()
    work.mkdir(parents=True, exist_ok=True)
    data_dir = work / "data"
    mock_root = work / "mock-drive"
    for path in (data_dir, mock_root):
        if path.exists():
            if not str(path).startswith(str(work)):
                raise RuntimeError(f"拒绝清理工作目录外路径：{path}")
            shutil.rmtree(path)
    data_dir.mkdir(parents=True, exist_ok=True)
    mock_root.mkdir(parents=True, exist_ok=True)
    drive = _free_subst(mock_root)
    neutral_root = f"{drive}\\TVD\\深夜频道"
    port = _free_port()
    seed_log = work / "seed.log"
    server_log = work / "server.log"
    capture_log = work / "capture.log"
    proc = None
    try:
        with seed_log.open("w", encoding="utf-8") as f:
            subprocess.run([
                sys.executable, str(TOOLS / "seed_fixture.py"),
                "--data-dir", str(data_dir),
                "--neutral-root", neutral_root,
            ], cwd=str(ROOT), stdout=f, stderr=subprocess.STDOUT, check=True,
                env={**os.environ, "PYTHONUTF8": "1"})

        env = {**os.environ, "PYTHONUTF8": "1"}
        server_out = server_log.open("w", encoding="utf-8")
        proc = subprocess.Popen([
            sys.executable, str(TOOLS / "before_server.py"),
            "--baseline", str(baseline),
            "--port", str(port),
            "--data-dir", str(data_dir),
        ], cwd=str(baseline), env=env, stdout=server_out,
           stderr=subprocess.STDOUT,
           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        base_url = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise RuntimeError(f"baseline server 提前退出：{proc.returncode}")
            try:
                with urllib.request.urlopen(base_url + "/healthz", timeout=1) as r:
                    if r.status == 200:
                        break
            except Exception:
                pass
            time.sleep(0.2)
        else:
            raise RuntimeError("baseline server 启动超时")

        node = pathlib.Path(
            r"C:\Users\xqz\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe"
        )
        with capture_log.open("w", encoding="utf-8") as f:
            subprocess.run([
                str(node), str(TOOLS / "capture_before.mjs"),
                f"--out={out}",
                f"--base-url={base_url}",
            ], cwd=str(ROOT), stdout=f, stderr=subprocess.STDOUT, check=True)
        print(json.dumps({
            "ok": True,
            "out": str(out),
            "work": str(work),
            "base_url": base_url,
        }, ensure_ascii=False, indent=2))
        return 0
    finally:
        if proc is not None and proc.poll() is None:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           capture_output=True, text=True)
        subprocess.run(["subst", drive, "/D"], capture_output=True, text=True)


if __name__ == "__main__":
    raise SystemExit(main())
