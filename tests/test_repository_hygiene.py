"""Repository hygiene contracts for the public source tree."""
from __future__ import annotations

import pathlib
import re


ROOT = pathlib.Path(__file__).resolve().parent.parent
TEXT_SUFFIXES = {".md", ".py", ".mjs", ".js", ".yaml", ".yml", ".json",
                 ".html", ".css", ".sh"}
PERSONAL_PATTERNS = (
    re.compile(r"C:\\Users\\[^\\]+|C:/Users/[^/\s]+|(?<![\w])/home/[a-z0-9._-]+",
               re.IGNORECASE),
    re.compile(r"codex-runtimes|Documents[\\/]Codex|trae_projects|\.trae[\\/]team",
               re.IGNORECASE),
)
EXCLUDED_DIRS = {".git", "__pycache__", ".pytest_cache", "lib", "tests"}
EXCLUDED_FILES = {"tests/test_repository_hygiene.py"}


def _tracked_text_files():
    for path in ROOT.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        relative = path.relative_to(ROOT)
        if (set(relative.parts) & EXCLUDED_DIRS
                or relative.as_posix() in EXCLUDED_FILES):
            continue
        yield path


def test_no_personal_paths_or_private_hosts_in_tracked_text():
    hits = []
    for path in _tracked_text_files():
        text = path.read_text(encoding="utf-8", errors="ignore")
        for pattern in PERSONAL_PATTERNS:
            if pattern.search(text):
                hits.append(str(path.relative_to(ROOT)))
                break
    assert not hits, f"正式产品与文档不应包含个人环境信息：{hits}"


def test_temporary_deployment_and_evidence_packages_are_not_tracked():
    forbidden = (
        ROOT / "deploy" / "v2.5.2-test",
        ROOT / "evidence",
        ROOT / "docs" / "images" / "compare",
        ROOT / "tests" / "test_v252_nas_package.py",
    )
    present = [str(path.relative_to(ROOT)) for path in forbidden if path.exists()]
    assert not present, f"一次性测试包仍留在正式仓库：{present}"


def test_markdown_local_links_resolve():
    missing = []
    for path in ROOT.rglob("*.md"):
        if ".git" in path.relative_to(ROOT).parts:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for target in re.findall(r"!?\[[^\]]*\]\(([^)]+)\)", text):
            if re.match(r"^[a-z]+://|^#|^mailto:", target):
                continue
            clean = target.split("#", 1)[0].split("?", 1)[0]
            if not clean:
                continue
            resolved = (path.parent / clean).resolve()
            if not resolved.is_file():
                missing.append(f"{path.relative_to(ROOT)} -> {target}")
        for target in re.findall(r'<img[^>]+src="([^"]+)"', text):
            if re.match(r"^[a-z]+://|^/", target):
                continue
            resolved = (path.parent / target.split("#", 1)[0]).resolve()
            if not resolved.is_file():
                missing.append(f"{path.relative_to(ROOT)} -> {target}")
    assert not missing, f"存在失效的本地文档链接：{missing}"
