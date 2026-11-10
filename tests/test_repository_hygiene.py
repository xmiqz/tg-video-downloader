"""Repository hygiene contracts for the public source tree."""
from __future__ import annotations

import pathlib
import re
import subprocess


ROOT = pathlib.Path(__file__).resolve().parent.parent
TEXT_SUFFIXES = {".md", ".txt", ".py", ".mjs", ".js", ".ts", ".yaml",
                 ".yml", ".json", ".html", ".css", ".sh", ".ps1", ".bat",
                 ".cfg", ".ini"}
PERSONAL_PATTERNS = (
    re.compile(r"C:\\Users\\[^\\]+|C:/Users/[^/\s]+|(?<![\w])/home/[a-z0-9._-]+",
               re.IGNORECASE),
    re.compile(r"codex-runtimes|Documents[\\/]Codex|trae_projects|\.trae[\\/]team",
               re.IGNORECASE),
)
EXCLUDED_PATHS = {
    "lib",
    "tests/test_repository_hygiene.py",
}


def _tracked_text_files():
    output = subprocess.check_output(
        ["git", "ls-files", "-z"], cwd=ROOT, text=False)
    for raw in output.split(b"\0"):
        if not raw:
            continue
        relative = pathlib.PurePosixPath(raw.decode("utf-8"))
        if (relative.suffix.lower() not in TEXT_SUFFIXES
                or any(part == "lib" for part in relative.parts)
                or relative.as_posix() in EXCLUDED_PATHS):
            continue
        yield ROOT / pathlib.Path(relative)


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
    for path in _tracked_text_files():
        if path.suffix.lower() != ".md":
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


def test_build_outputs_are_ignored_without_broad_asset_patterns():
    ignored = [
        "dist/TGVideoDownloader.exe",
        "dist/TGVideoDownloader_mac_arm64.command",
        "dist/TGVideoDownloader_mac_arm64",
        "dist/release.zip",
        "dist/release.tar.gz",
        "build_tmp/app.spec",
        "build_pyi/app.bin",
        "build/local.tmp",
        "TGVideoDownloader.exe",
    ]
    preserved = [
        "examples/task-bundle.zip",
        "tests/fixtures/sample.tar",
        "tests/fixtures/TGVideoDownloader_mac_x64.command",
        "docs/examples/installer.msi",
        "docs/examples/installer.dmg",
        "docs/examples/TGVideoDownloader.app/Contents/Info.plist",
        "tools/fixtures/TGVideoDownloader_linux_x64",
        "build/tvg.ico",
        "requirements-dev.txt",
    ]
    for candidate in ignored:
        result = subprocess.run(
            ["git", "check-ignore", "--no-index", "-q", candidate],
            cwd=ROOT, capture_output=True)
        assert result.returncode == 0, f"构建产物应被忽略：{candidate}"
    for candidate in preserved:
        result = subprocess.run(
            ["git", "check-ignore", "--no-index", "-q", candidate],
            cwd=ROOT, capture_output=True)
        assert result.returncode != 0, f"合法资产不应被忽略：{candidate}"
