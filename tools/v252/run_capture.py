"""Run the v2.5.2 screenshot/smoke pass against a packaged Windows EXE."""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import shutil
import subprocess
import sys
import time

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
    raise RuntimeError("没有可用的空闲盘符用于隔离 mock 路径")


def _unsubst(drive: str) -> None:
    if drive:
        subprocess.run(["subst", drive, "/D"], capture_output=True, text=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--exe", default="")
    parser.add_argument("--out", required=True)
    parser.add_argument("--work", default=str(ROOT / "work" / "v252-capture"))
    parser.add_argument("--keep-data", action="store_true")
    parser.add_argument("--source-server", action="store_true")
    parser.add_argument("--quick", action="store_true")
    args = parser.parse_args()

    exe = pathlib.Path(args.exe).expanduser().resolve() if args.exe else None
    if not args.source_server and (exe is None or not exe.is_file()):
        raise SystemExit(f"EXE 不存在：{exe}")
    out = pathlib.Path(args.out).expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)
    work = pathlib.Path(args.work).expanduser().resolve()
    work.mkdir(parents=True, exist_ok=True)
    data_dir = work / "data"
    mock_root = work / "mock-drive"
    if data_dir.exists() and not args.keep_data:
        if not str(data_dir).startswith(str(work)):
            raise RuntimeError(f"拒绝清理工作目录外路径：{data_dir}")
        shutil.rmtree(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    if mock_root.exists():
        if not str(mock_root).startswith(str(work)):
            raise RuntimeError(f"拒绝清理工作目录外路径：{mock_root}")
        shutil.rmtree(mock_root)
    mock_root.mkdir(parents=True, exist_ok=True)

    drive = _free_subst(mock_root)
    neutral_root = f"{drive}\\TVD\\深夜频道"
    fixture = work / "ui-fixture.json"
    seed_log = work / "seed.log"
    capture_log = work / "capture.log"
    exe_log = work / "exe.log"
    proc = None
    try:
        with seed_log.open("w", encoding="utf-8") as f:
            subprocess.run([
                sys.executable, str(TOOLS / "seed_fixture.py"),
                "--data-dir", str(data_dir),
                "--fixture-out", str(fixture),
                "--neutral-root", neutral_root,
            ], cwd=str(ROOT), stdout=f, stderr=subprocess.STDOUT, check=True,
                env={**os.environ, "PYTHONUTF8": "1"})

        env = {
            **os.environ,
            "TGDL_SCREENSHOT_MODE": "1",
            "TGDL_DATA_DIR": str(data_dir),
            "TGDL_UI_FIXTURE": str(fixture),
            "PYTHONUTF8": "1",
        }
        exe_out = exe_log.open("w", encoding="utf-8")
        command = ([sys.executable, str(ROOT / "gui.py")] if args.source_server
                   else [str(exe)])
        cwd = str(ROOT) if args.source_server else str(exe.parent)
        proc = subprocess.Popen(
            command, cwd=cwd, env=env,
            stdout=exe_out, stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        info_path = data_dir / ".screenshot-server.json"
        deadline = time.monotonic() + 40
        info = None
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise RuntimeError(f"EXE 提前退出，code={proc.returncode}")
            if info_path.is_file():
                try:
                    info = json.loads(info_path.read_text(encoding="utf-8"))
                    break
                except Exception:
                    pass
            time.sleep(0.2)
        if not info:
            raise RuntimeError("等待截图服务连接信息超时")

        node = pathlib.Path(
            r"C:\Users\xqz\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe"
        )
        with capture_log.open("w", encoding="utf-8") as f:
            capture_cmd = [
                str(node), str(TOOLS / "capture.mjs"),
                f"--out={out}",
                f"--base-url={info['base_url']}",
                f"--token={info['token']}",
                "--task-id=t252_active",
            ]
            if args.quick:
                capture_cmd.append("--quick")
            subprocess.run(capture_cmd, cwd=str(ROOT), stdout=f,
                           stderr=subprocess.STDOUT, check=True)
        print(json.dumps({
            "ok": True,
            "out": str(out),
            "work": str(work),
            "neutral_root": neutral_root,
            "base_url": info["base_url"],
        }, ensure_ascii=False, indent=2))
        return 0
    finally:
        if proc is not None and proc.poll() is None:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           capture_output=True, text=True)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        _unsubst(drive)


if __name__ == "__main__":
    raise SystemExit(main())
