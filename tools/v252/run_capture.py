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


FOLDER_PICKER_SHOTS = (
    ("task-edit-folder", "task-edit-folder"),
    ("mobile/task-edit-folder", "mobile/task-edit-folder"),
)

# 路径字段里的文字是浅色（>140 灰度）。模糊后必须一个亮像素都不剩，
# 否则说明脱敏没有真正生效。
REDACT_BRIGHT_LIMIT = 140


class RedactionError(RuntimeError):
    """Raised when a public screenshot cannot be proven to be redacted."""


def _require_pil():
    try:
        from PIL import Image, ImageFilter
    except Exception as exc:  # pragma: no cover - exercised via monkeypatch
        raise RedactionError(
            f"Pillow 不可用，无法脱敏目录选择截图：{exc}"
        ) from exc
    return Image, ImageFilter


def _bright_pixels(region, Image) -> int:
    gray = region.convert("L")
    # getdata() 在 Pillow 14 会被移除；histogram 更稳且更快。
    hist = gray.histogram()
    return sum(hist[REDACT_BRIGHT_LIMIT + 1:])


def _redact_folder_path(out: pathlib.Path) -> None:
    """Blur the folder-picker path field; fail closed if it cannot be verified.

    A silently skipped redaction leaves a screenshot that exposes the capture
    machine's working directory, so every unexpected state raises instead of
    returning: missing Pillow, missing capture metadata, an unusable rect, or
    text still readable after blurring.
    """
    Image, ImageFilter = _require_pil()
    for meta_rel, image_rel in FOLDER_PICKER_SHOTS:
        image = out / f"{image_rel}.png"
        meta = out / f"{meta_rel}.redact.json"
        if not image.is_file():
            # 该次采集没有产出这张图（例如只跑了别的步骤），无需脱敏。
            meta.unlink(missing_ok=True)
            continue
        if not meta.is_file():
            raise RedactionError(
                f"{image} 已生成但缺少脱敏记录 {meta.name}，拒绝发布未脱敏截图"
            )
        try:
            box = json.loads(meta.read_text(encoding="utf-8")).get("box")
        except Exception as exc:
            raise RedactionError(f"{meta.name} 无法解析：{exc}") from exc
        if not box:
            raise RedactionError(f"{meta.name} 缺少 box，拒绝发布未脱敏截图")

        x, y = int(box["x"]), int(box["y"])
        w, h = int(box["width"]), int(box["height"])
        if w <= 0 or h <= 0:
            raise RedactionError(f"{meta.name} 的 box 尺寸非法：{w}x{h}")

        with Image.open(image) as img:
            if x < 0 or y < 0 or x + w > img.width or y + h > img.height:
                raise RedactionError(
                    f"{meta.name} 的 box 超出图片范围：{img.size} vs {box}"
                )
            region = img.crop((x, y, x + w, y + h))
            # 半径按区域高度自适应：移动端字段更窄，固定 7px 仍能看出笔画。
            radius = max(12, int(h * 0.9))
            blurred = region.filter(ImageFilter.GaussianBlur(radius=radius))
            if _bright_pixels(blurred, Image) > 0:
                raise RedactionError(
                    f"{image_rel}.png 模糊后仍有可读文字，脱敏失败"
                )
            img.paste(blurred, (x, y))
            img.save(image)
        meta.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--exe", default="")
    parser.add_argument("--out", required=True)
    parser.add_argument("--work", default=str(ROOT / "work" / "v252-capture"))
    parser.add_argument("--keep-data", action="store_true")
    parser.add_argument("--source-server", action="store_true")
    parser.add_argument("--quick", action="store_true")
    parser.add_argument("--only", default="",
                        help="只采集指定步骤（目前支持 folder）")
    parser.add_argument("--mock-root", default="",
                        help="直接使用该中性目录作为 mock 根（不创建 subst 盘）")
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

    drive = ""
    if args.mock_root:
        # 产品代码会对路径做 resolve()，subst 盘会被还原成真实工作目录。
        # 指定一个真正中性的绝对路径，公开截图才不会出现采集机的个人目录。
        mock_root = pathlib.Path(args.mock_root).expanduser()
        if mock_root.exists():
            shutil.rmtree(mock_root)
        mock_root.mkdir(parents=True, exist_ok=True)
        # 数据目录放在 mock 根内，允许根自然覆盖整棵树，弹层里看到的是
        # 真实存在的频道/季度目录，而不是工作目录里的 cache/tasks。
        data_dir = mock_root / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        neutral_root = str(data_dir / "downloads" / "深夜频道")
    else:
        drive = _free_subst(mock_root)
        neutral_root = f"{drive}\\TVD\\深夜频道"
        data_dir = pathlib.Path(f"{drive}\\TVD\\data")
        data_dir.mkdir(parents=True, exist_ok=True)
    # The editor's folder picker lists real subdirectories of the task folder,
    # so the mock tree has to exist on disk. These are neutral placeholder
    # names only; nothing is downloaded into them.
    for rel in (
        r"第12季\2026年11月\最终归档\VeryLongNeutralFolderName",
        r"城市边缘\特别篇",
        r"第12季\2026年10月",
        r"第12季\花絮",
    ):
        (pathlib.Path(neutral_root) / rel).mkdir(parents=True, exist_ok=True)
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
            # The task editor's folder picker only lists directories inside the
            # configured roots. The mock task lives on the substituted drive, so
            # declare that drive as an allowed root instead of relaxing the
            # containment check in the product code.
            "TGDL_ALLOWED_ROOTS": f"{drive}\\{os.pathsep}{data_dir}",
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
            if args.only:
                capture_cmd.append(f"--only={args.only}")
            subprocess.run(capture_cmd, cwd=str(ROOT), stdout=f,
                           stderr=subprocess.STDOUT, check=True)
        _redact_folder_path(out)
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
