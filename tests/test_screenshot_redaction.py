"""Public screenshots must never expose the capture machine's local paths.

The folder-picker shots render an absolute path inside the isolated mock tree,
which still contains the capture machine's working directory. Redaction is
applied after capture, so it must fail closed: a missing rect, a broken rect or
text that is still readable after blurring has to abort the run instead of
silently publishing a leaking screenshot.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
RUNNER = ROOT / "tools" / "v252" / "run_capture.py"

pytest.importorskip("PIL", reason="截图脱敏依赖 Pillow")


def _load_runner():
    spec = importlib.util.spec_from_file_location("v252_run_capture", RUNNER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


runner = _load_runner()


def _make_shot(tmp_path, name="task-edit-folder", text="C:\\Users\\someone\\TVD\\data"):
    from PIL import Image, ImageDraw

    image = tmp_path / f"{name}.png"
    img = Image.new("RGB", (400, 200), (20, 24, 34))
    draw = ImageDraw.Draw(img)
    draw.text((20, 90), text, fill=(240, 244, 255))
    img.save(image)
    return image


def _write_meta(tmp_path, name="task-edit-folder", box=None):
    meta = tmp_path / f"{name}.redact.json"
    meta.write_text(
        json.dumps({"box": box or {"x": 10, "y": 80, "width": 380, "height": 40}}),
        encoding="utf-8",
    )
    return meta


def _bright_count(path, box):
    from PIL import Image

    with Image.open(path) as img:
        region = img.crop(
            (box["x"], box["y"], box["x"] + box["width"], box["y"] + box["height"])
        ).convert("L")
    hist = region.histogram()
    return sum(hist[runner.REDACT_BRIGHT_LIMIT + 1:])


def test_redaction_removes_readable_text_and_drops_meta(tmp_path, monkeypatch):
    monkeypatch.setattr(
        runner, "FOLDER_PICKER_SHOTS", (("task-edit-folder", "task-edit-folder"),))
    image = _make_shot(tmp_path)
    meta = _write_meta(tmp_path)
    box = json.loads(meta.read_text(encoding="utf-8"))["box"]
    assert _bright_count(image, box) > 0, "夹具必须真的画出可读文字"

    runner._redact_folder_path(tmp_path)

    assert _bright_count(image, box) == 0
    assert not meta.exists(), "脱敏完成后应删除中间记录"


def test_missing_meta_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(
        runner, "FOLDER_PICKER_SHOTS", (("task-edit-folder", "task-edit-folder"),))
    _make_shot(tmp_path)
    with pytest.raises(runner.RedactionError, match="缺少脱敏记录"):
        runner._redact_folder_path(tmp_path)


def test_unparsable_meta_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(
        runner, "FOLDER_PICKER_SHOTS", (("task-edit-folder", "task-edit-folder"),))
    _make_shot(tmp_path)
    (tmp_path / "task-edit-folder.redact.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(runner.RedactionError, match="无法解析"):
        runner._redact_folder_path(tmp_path)


def test_empty_box_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(
        runner, "FOLDER_PICKER_SHOTS", (("task-edit-folder", "task-edit-folder"),))
    _make_shot(tmp_path)
    (tmp_path / "task-edit-folder.redact.json").write_text(
        json.dumps({"box": None}), encoding="utf-8")
    with pytest.raises(runner.RedactionError, match="缺少 box"):
        runner._redact_folder_path(tmp_path)


def test_degenerate_box_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(
        runner, "FOLDER_PICKER_SHOTS", (("task-edit-folder", "task-edit-folder"),))
    _make_shot(tmp_path)
    _write_meta(tmp_path, box={"x": 10, "y": 80, "width": 0, "height": 40})
    with pytest.raises(runner.RedactionError, match="尺寸非法"):
        runner._redact_folder_path(tmp_path)


def test_out_of_bounds_box_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(
        runner, "FOLDER_PICKER_SHOTS", (("task-edit-folder", "task-edit-folder"),))
    _make_shot(tmp_path)
    _write_meta(tmp_path, box={"x": 380, "y": 80, "width": 200, "height": 40})
    with pytest.raises(runner.RedactionError, match="超出图片范围"):
        runner._redact_folder_path(tmp_path)


def test_missing_pillow_fails_closed(tmp_path, monkeypatch):
    def _boom():
        raise runner.RedactionError("Pillow 不可用，无法脱敏目录选择截图：boom")

    monkeypatch.setattr(runner, "_require_pil", _boom)
    with pytest.raises(runner.RedactionError, match="Pillow 不可用"):
        runner._redact_folder_path(tmp_path)


def test_shot_not_captured_is_skipped(tmp_path, monkeypatch):
    """只跑其它采集步骤时，不应因缺少该图而失败。"""
    monkeypatch.setattr(
        runner, "FOLDER_PICKER_SHOTS", (("task-edit-folder", "task-edit-folder"),))
    runner._redact_folder_path(tmp_path)  # 不抛异常即通过


def test_mobile_shot_is_redacted_too(tmp_path, monkeypatch):
    monkeypatch.setattr(
        runner, "FOLDER_PICKER_SHOTS",
        (("mobile/task-edit-folder", "mobile/task-edit-folder"),))
    (tmp_path / "mobile").mkdir()
    image = _make_shot(tmp_path / "mobile")
    meta = _write_meta(tmp_path / "mobile")
    box = json.loads(meta.read_text(encoding="utf-8"))["box"]
    assert _bright_count(image, box) > 0

    runner._redact_folder_path(tmp_path)

    assert _bright_count(image, box) == 0


@pytest.mark.parametrize(
    ("rel", "box"),
    [
        ("docs/images/task-edit-folder.png", (725, 428, 1270, 462)),
        ("docs/images/mobile/task-edit-folder.png", (105, 335, 365, 372)),
    ],
)
def test_committed_screenshots_have_no_readable_path(rel, box):
    """公开素材里的路径框必须已被遮蔽：不能残留浅色可读文字。"""
    from PIL import Image

    path = ROOT / rel
    assert path.is_file(), f"缺少截图 {rel}"
    with Image.open(path) as img:
        gray = img.convert("L").crop(box)
    hist = gray.histogram()
    bright = sum(hist[141:])
    assert bright == 0, f"{rel} 路径框仍有 {bright} 个可读亮像素"
