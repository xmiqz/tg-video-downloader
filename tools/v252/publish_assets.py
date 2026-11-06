"""Copy the packaged-EXE screenshot matrix and build before/after evidence."""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import shutil

from PIL import Image, ImageDraw, ImageFont

PUBLIC = [
    "theme-dark.png",
    "theme-light.png",
    "theme-bing.png",
    "theme-bing-dark.png",
    "theme-liquid.png",
    "hero-dashboard.png",
    "login.png",
    "settings.png",
    "new-download.png",
    "new-download-results.png",
    "task-library.png",
    "task-edit.png",
    "mobile/login.png",
    "mobile/settings.png",
    "mobile/new-download.png",
    "mobile/new-download-results.png",
    "mobile/task-library.png",
    "mobile/task-edit.png",
    "tablet/settings.png",
    "tablet/tasks.png",
    "widths/1280-active.png",
    "widths/834-active.png",
    "widths/390-active.png",
]

PAIRS = [
    ("login", "before-login-1920.png", "login.png", None),
    ("tablet-left-edge", "before-tablet-settings-834.png",
     "tablet/settings.png", (72, 80, 360, 880)),
    ("mobile-appearance", "before-appearance-390.png",
     "compare/after-appearance-390.png", None),
    ("bottom-selected", "before-bottom-selected-390.png",
     "compare/after-bottom-selected-390.png", None),
]


def _font(size: int):
    for name in ("segoeui.ttf", "msyh.ttc", "arial.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _pair(before: pathlib.Path, after: pathlib.Path, out: pathlib.Path,
          crop: tuple[int, int, int, int] | None) -> None:
    a = Image.open(before).convert("RGB")
    b = Image.open(after).convert("RGB")
    if crop:
        a = a.crop(crop)
        b = b.crop(crop)
    if a.height != b.height:
        h = max(a.height, b.height)
        a = a.resize((round(a.width * h / a.height), h))
        b = b.resize((round(b.width * h / b.height), h))
    label_h = 42
    gap = 18
    canvas = Image.new("RGB", (a.width + b.width + gap, a.height + label_h), "#0b0e17")
    canvas.paste(a, (0, label_h))
    canvas.paste(b, (a.width + gap, label_h))
    draw = ImageDraw.Draw(canvas)
    font = _font(18)
    draw.text((12, 10), "v2.5.1 BEFORE", fill="#ffb84d", font=font)
    draw.text((a.width + gap + 12, 10), "v2.5.2 AFTER",
              fill="#34d9a0", font=font)
    out.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out)


def _sha(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--screenshots", required=True)
    parser.add_argument("--before", required=True)
    parser.add_argument("--docs-images", required=True)
    parser.add_argument("--evidence", required=True)
    args = parser.parse_args()

    shots = pathlib.Path(args.screenshots).resolve()
    before = pathlib.Path(args.before).resolve()
    docs = pathlib.Path(args.docs_images).resolve()
    evidence = pathlib.Path(args.evidence).resolve()
    docs.mkdir(parents=True, exist_ok=True)
    evidence.mkdir(parents=True, exist_ok=True)
    (evidence / "before").mkdir(exist_ok=True)
    (evidence / "after").mkdir(exist_ok=True)
    (evidence / "compare").mkdir(exist_ok=True)

    copied = []
    for rel in PUBLIC:
        src = shots / rel
        if not src.is_file():
            raise SystemExit(f"缺少截图：{src}")
        dst = docs / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        copied.append(dst)

    for rel in ("smoke-v252.log",):
        src = shots / rel
        if src.is_file():
            shutil.copy2(src, evidence / rel)

    before_public = []
    for _name, before_rel, after_rel, crop in PAIRS:
        src_before = before / before_rel
        src_after = shots / after_rel
        if not src_before.is_file() or not src_after.is_file():
            raise SystemExit(f"缺少对比图：{src_before} / {src_after}")
        bdst = evidence / "before" / before_rel
        adst = evidence / "after" / after_rel.replace("/", "__")
        adst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_before, bdst)
        shutil.copy2(src_after, adst)
        out = evidence / "compare" / (src_before.stem.replace("before-", "")
                                      + "-before-after.png")
        _pair(src_before, src_after, out, crop)
        public_out = docs / "compare" / out.name
        public_out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(out, public_out)
        before_public.append(public_out)

    manifest = {
        "screenshots_source": "packaged Windows x64 EXE + isolated mock data",
        "public_images": [
            {"path": str(p.relative_to(docs)), "sha256": _sha(p)}
            for p in copied + before_public
        ],
        "compare": [str(p.relative_to(evidence)) for p in
                    sorted((evidence / "compare").glob("*.png"))],
    }
    (evidence / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps({"docs": str(docs), "evidence": str(evidence),
                      "images": len(copied), "compare": len(before_public)},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
