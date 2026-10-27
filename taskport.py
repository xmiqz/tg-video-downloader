"""Task export/import without any media payload.

A task file stores metadata only (msg id, name, size, status...), never the
video itself, so "exporting a task" means writing a portable JSON bundle and
"importing" means recreating those task records on another instance.

The hard part is ``folder``: it is an absolute path baked for the machine
that created the task. Export therefore records the *shape* of the path
relative to the download root, and import re-anchors it under the target's
own root. That keeps a task created at ``/data/downloads/频道`` importable on
a Windows desktop whose root is ``D:/TG/downloads``.
"""
from __future__ import annotations

import json
import os
import pathlib
import time

BUNDLE_VERSION = 1
EXPORT_LIMIT_ITEMS = 5000


def _norm(p):
    return str(pathlib.Path(str(p or "")).as_posix())


def relative_folder(folder, root):
    """Return ``folder`` as a posix path relative to ``root``.

    Falls back to the last path component when the folder is not under the
    root (e.g. the user picked an unrelated directory), so the import side
    still gets something usable instead of an absolute path from another
    machine.
    """
    f = pathlib.Path(str(folder or "")).resolve()
    r = pathlib.Path(str(root or "")).resolve()
    try:
        rel = f.relative_to(r)
        return rel.as_posix()
    except ValueError:
        return f.name


def resolve_folder(rel, root):
    """Re-anchor an exported relative folder under this instance's root."""
    root_p = pathlib.Path(str(root or "")).resolve()
    rel = _norm(rel).lstrip("/")
    parts = [p for p in rel.split("/") if p and p not in (".", "..")]
    return root_p.joinpath(*parts) if parts else root_p


def export_task(task, root, *, keep_status=True):
    """Convert one stored task into its portable form.

    Media is never included: items carry no file bytes and no absolute path.
    """
    items = []
    for ti in task.get("items") or []:
        if not isinstance(ti, dict):
            continue
        items.append({
            "msg": ti.get("msg"),
            "src": ti.get("src", ""),
            "name": str(ti.get("name") or ""),
            "size": ti.get("size", 0),
            "date": str(ti.get("date") or ""),
            "duration": ti.get("duration", 0),
            "text": str(ti.get("text") or "")[:400],
            # "done" means "the file was on the old machine" and is
            # meaningless after import, so it is reset to pending.
            "status": ("pending"
                       if str(ti.get("status")) == "done"
                       else str(ti.get("status") or "pending"))
            if keep_status else "pending",
        })
    return {
        "id": str(task.get("id") or ""),
        "created": str(task.get("created") or ""),
        "channel": str(task.get("channel") or ""),
        "channel_title": str(task.get("channel_title") or ""),
        "params": dict(task.get("params") or {}),
        "folder_rel": relative_folder(task.get("folder"), root),
        "items": items[:EXPORT_LIMIT_ITEMS],
    }


def build_bundle(tasks, root, *, keep_status=True):
    return {
        "format": "tgvd-task-bundle",
        "version": BUNDLE_VERSION,
        "exported_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "count": len(tasks),
        "tasks": [export_task(t, root, keep_status=keep_status)
                  for t in tasks],
    }


def parse_bundle(payload):
    """Validate an uploaded bundle; raise ValueError with a readable reason."""
    if not isinstance(payload, dict):
        raise ValueError("不是有效的 JSON 对象")
    if payload.get("format") != "tgvd-task-bundle":
        raise ValueError("不是本应用导出的任务包")
    version = payload.get("version")
    if not isinstance(version, int) or version < 1:
        raise ValueError("任务包版本无效")
    if version > BUNDLE_VERSION:
        raise ValueError(f"任务包版本 {version} 高于本程序支持的 {BUNDLE_VERSION}")
    tasks = payload.get("tasks")
    if not isinstance(tasks, list):
        raise ValueError("任务包缺少 tasks 列表")
    if not tasks:
        raise ValueError("任务包里没有任务")
    out = []
    for raw in tasks:
        if not isinstance(raw, dict):
            continue
        items = []
        for ti in raw.get("items") or []:
            if not isinstance(ti, dict):
                continue
            msg = ti.get("msg")
            if not isinstance(msg, int):
                continue
            items.append({
                "msg": msg,
                "size": ti.get("size", 0),
                "name": str(ti.get("name") or ""),
                "date": str(ti.get("date") or ""),
                "status": "pending",     # 导入后一律重新下载
                "src": str(ti.get("src") or ""),
                "text": str(ti.get("text") or ""),
                "duration": ti.get("duration", 0),
            })
        if not items:
            continue
        out.append({
            "channel": str(raw.get("channel") or ""),
            "channel_title": str(raw.get("channel_title") or ""),
            "params": dict(raw.get("params") or {}),
            "folder_rel": str(raw.get("folder_rel") or ""),
            "items": items,
        })
    if not out:
        raise ValueError("任务包里没有可用任务")
    return out


def _unique_id(base, taken):
    cand = base
    n = 1
    while cand in taken:
        n += 1
        cand = f"{base}_{n}"
    return cand


def materialize(parsed, root, *, new_task_id, now=None):
    """Build saveable task dicts from a parsed bundle.

    ``new_task_id`` is injected so ids stay collision-free per instance.
    ``root`` is this instance's download root; folders are re-anchored under
    it. Nothing from the source machine's absolute paths survives.
    """
    stamp = new_task_id()
    taken = set()
    out = []
    created = (now or time.strftime)("%Y-%m-%d %H:%M:%S")
    for n, task in enumerate(parsed, 1):
        tid = stamp if len(parsed) == 1 else f"{stamp}_{n}"
        tid = _unique_id(tid, taken)
        taken.add(tid)
        folder = resolve_folder(task.get("folder_rel"), root)
        out.append({
            "id": tid,
            "created": created,
            "channel": task.get("channel", ""),
            "channel_title": task.get("channel_title", ""),
            "params": dict(task.get("params") or {}),
            "folder": str(folder),
            "items": task["items"],
        })
    return out


def safe_bundle_filename(stamp=None):
    s = stamp or time.strftime("%Y%m%d-%H%M%S")
    return f"tgvd-tasks-{s}.json"


def write_bundle(path, bundle):
    pathlib.Path(path).write_text(
        json.dumps(bundle, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    return path