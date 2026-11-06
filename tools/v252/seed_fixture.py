"""Prepare an isolated, portable mock data directory for v2.5.2 screenshots.

The data is consumed by the packaged EXE's own HTTP/WS service; no HTML
fixture is served. Public screenshots therefore use neutral D:\\TVD paths.
"""
from __future__ import annotations

import argparse
import datetime
import json
import math
import pathlib
import sys

from PIL import Image, ImageDraw, ImageFilter

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import tg_video_dl as core  # noqa: E402
import wallpaper  # noqa: E402


TASK_ID = "t252_active"
ARCHIVE_ID = "t252_archive"


def _task_items(prefix: str) -> list[dict]:
    names = [
        "第01话_海风与旧站台.mp4",
        "第02话_雨夜便利店.mp4",
        "第03话_凌晨四点的列车.mp4",
        "第04话_灯下的约定.mp4",
        "第05话_海边来信.mp4",
        "第06话_无人接听.mp4",
        "第07话_慢半拍的对白.mp4",
        "第08话_城市边缘.mp4",
        "第09话_最后一班车.mp4",
        "第10话_天亮之前.mp4",
        "第11话_回声.mp4",
        "第12话_深夜频道.mp4",
    ]
    items = []
    for i, name in enumerate(names, start=1):
        done = i <= 4
        items.append({
            "msg": 4100 + i,
            "name": name,
            "size": 180_000_000 + i * 17_000_000,
            "date": f"2026-10-{20 + (i % 9):02d}",
            "status": "done" if done else "pending",
            "src": "night",
            "text": f"{prefix} · {name}",
            "ref_text": "",
            "duration": 1420 + i * 13,
        })
    return items


def _task(task_id: str, title: str, offset: int, folder: str) -> dict:
    return {
        "id": task_id,
        "created": f"2026-11-0{offset} 2{offset}:18:00",
        "channel": "night_channel",
        "channel_title": title,
        "params": {"query": "深夜", "parallel": 2, "acct_pref": "auto"},
        "folder": folder,
        "items": _task_items(title),
    }


def _preview_items() -> list[dict]:
    items = []
    for i in range(1, 9):
        items.append({
            "mid": 5200 + i,
            "src": "night",
            "name": f"第{i:02d}话_搜索结果_{'最终版' if i == 8 else '修复版'}.mp4",
            "size": 210_000_000 + i * 9_500_000,
            "date": f"2026-10-{20 + i:02d}",
            "text": "深夜频道 · 第12季 · 官方片段",
            "match_in": "self",
            "ref_text": "",
            "rename": f"深夜频道 - 第{i:02d}话",
        })
    return items


def _active_state() -> dict:
    items = []
    names = [it["name"] for it in _task_items("下载中")]
    for i, name in enumerate(names, start=1):
        if i <= 4:
            status, pct, speed = "已完成", 100, 0
        elif i <= 7:
            status = "下载中"
            pct = 68 - i * 4
            speed = 4_500_000 + i * 260_000
        else:
            status, pct, speed = "等待中", 0, 0
        size = 180_000_000 + i * 17_000_000
        items.append({
            "src": "night",
            "mid": 4100 + i,
            "name": name,
            "status": status,
            "pct": pct,
            "done": int(size * pct / 100),
            "size": size,
            "speed": speed,
            "error": "",
        })
    return {
        "task_id": TASK_ID,
        "title": "深夜频道 · 第12季",
        "phase": "downloading",
        "phase_detail": "正在下载 3/12 · 海风与旧站台",
        "total_bytes": 3_260_000_000,
        "total_tot": 5_840_000_000,
        "agg_speed": 11_880_000,
        "counts": {"done": 4, "skip": 0, "fail": 0, "active": 3, "paused": 0},
        "task_total": 12,
        "task_finished": 4,
        "parallel": 3,
        "items": items,
    }


def _wallpaper_image(path: pathlib.Path, bright: bool) -> None:
    width, height = 1920, 1080
    im = Image.new("RGB", (width, height), "#d9e8f2" if bright else "#07111f")
    px = im.load()
    top = (212, 235, 246) if bright else (8, 18, 38)
    bottom = (247, 198, 154) if bright else (31, 13, 43)
    for y in range(height):
        t = y / (height - 1)
        r = round(top[0] * (1 - t) + bottom[0] * t)
        g = round(top[1] * (1 - t) + bottom[1] * t)
        b = round(top[2] * (1 - t) + bottom[2] * t)
        for x in range(width):
            wave = int(10 * math.sin((x / width) * math.pi * 2 + y / 150))
            px[x, y] = (max(0, min(255, r + wave)),
                        max(0, min(255, g + wave)),
                        max(0, min(255, b + wave)))
    draw = ImageDraw.Draw(im)
    sun = (251, 230, 180) if bright else (190, 215, 255)
    draw.ellipse((1280, 130, 1500, 350), fill=sun)
    mountain = (117, 150, 162) if bright else (16, 28, 52)
    draw.polygon([(0, 760), (380, 500), (700, 740), (1020, 470),
                  (1400, 760), (1920, 560), (1920, 1080), (0, 1080)],
                 fill=mountain)
    near = (63, 88, 100) if bright else (5, 12, 25)
    draw.polygon([(0, 900), (260, 710), (620, 880), (980, 680),
                  (1320, 900), (1680, 740), (1920, 850), (1920, 1080),
                  (0, 1080)], fill=near)
    im = im.filter(ImageFilter.GaussianBlur(2.2 if bright else 3.0))
    im.save(path, quality=91)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--fixture-out", default="")
    parser.add_argument("--neutral-root", default=r"D:\TVD\深夜频道")
    args = parser.parse_args()

    data_dir = pathlib.Path(args.data_dir).expanduser().resolve()
    neutral_root = str(args.neutral_root)
    core.configure_data_dir(data_dir)
    core.DATA_DIR.mkdir(parents=True, exist_ok=True)
    core.TASK_ROOT.mkdir(parents=True, exist_ok=True)
    (data_dir / "downloads").mkdir(parents=True, exist_ok=True)

    core.save_config({
        "appearance": "dark",
        "download_root": neutral_root,
        "parallel": 2,
        "retries": 3,
        "phone": "",
    })

    active = _task(
        TASK_ID,
        "深夜频道 · 第12季",
        6,
        neutral_root + r"\第12季\2026年11月\最终归档\VeryLongNeutralFolderName",
    )
    archive = _task(
        ARCHIVE_ID,
        "城市边缘 · 特别篇",
        4,
        neutral_root + r"\城市边缘\特别篇",
    )
    for task in (active, archive):
        (core.TASK_ROOT / f"{task['id']}.json").write_text(
            json.dumps(task, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    cache = wallpaper.WallpaperCache(data_dir)
    cache_dir = wallpaper._cache_dir(data_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    bright_name = "wallpaper-bright.jpg"
    dark_name = "wallpaper-dark.jpg"
    _wallpaper_image(cache_dir / bright_name, True)
    _wallpaper_image(cache_dir / dark_name, False)
    now = datetime.datetime.now().timestamp()
    index = {
        "current": {
            "date": "2026-11-06",
            "url": "",
            "copyright": "Mock wallpaper for UI verification",
            "copyright_link": "",
            "title": "晨光海岸",
            "file": bright_name,
            "ts": now,
        },
        "feed": [],
        "feed_ts": now,
        "history": [
            {
                "date": "2026-11-05",
                "url": "",
                "copyright": "Mock wallpaper for UI verification",
                "copyright_link": "",
                "title": "夜色城市",
                "file": dark_name,
                "ts": now - 86400,
            },
            {
                "date": "2026-11-06",
                "url": "",
                "copyright": "Mock wallpaper for UI verification",
                "copyright_link": "",
                "title": "晨光海岸",
                "file": bright_name,
                "ts": now,
            },
        ],
        "save_history": True,
    }
    wallpaper._write_json(wallpaper._index_path(data_dir), index)

    fixture = {
        "active_states": [_active_state()],
        "preview_items": _preview_items(),
    }
    fixture_path = pathlib.Path(args.fixture_out) if args.fixture_out else data_dir / "ui-fixture.json"
    fixture_path.parent.mkdir(parents=True, exist_ok=True)
    fixture_path.write_text(
        json.dumps(fixture, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps({
        "data_dir": str(data_dir),
        "fixture": str(fixture_path),
        "task_id": TASK_ID,
        "archive_id": ARCHIVE_ID,
        "wallpaper_bright": bright_name,
        "wallpaper_dark": dark_name,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
