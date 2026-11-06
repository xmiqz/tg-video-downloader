"""Release governance contract: one mainline, one version, one publish SHA."""
from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
CI = ROOT / ".github" / "workflows" / "ci.yml"
BUILD = ROOT / ".github" / "workflows" / "build.yml"
CONTAINER = ROOT / ".github" / "workflows" / "container.yml"
COMPOSE = ROOT / "compose.yaml"
AGENTS = ROOT / "AGENTS.md"


def _read(path):
    return path.read_text(encoding="utf-8")


def test_ci_no_longer_listens_on_retired_branch():
    text = _read(CI)
    assert "v2.5-dev" not in text, "ci.yml 不得继续监听已废弃的 v2.5-dev"
    assert re.search(r"push:\s*\n\s*branches:\s*\n\s*-\s*main", text), (
        "ci.yml push 触发必须只保留 main")
    assert re.search(r"pull_request:\s*\n\s*branches:\s*\n\s*-\s*main", text), (
        "ci.yml PR 触发必须只保留 main")


def test_container_publish_depends_on_both_validations():
    text = _read(CONTAINER)
    assert re.search(r"validate-amd64:", text), "缺少 amd64 验证 job"
    assert re.search(r"validate-arm64:", text), "缺少 arm64 验证 job"
    assert re.search(r"needs:\s*\[\s*validate-amd64\s*,\s*validate-arm64\s*\]", text), (
        "publish 必须在 amd64 与 arm64 验证都通过后才执行")
    assert re.search(r"verify:\s*\n\s*needs:\s*publish", text), (
        "发布后必须有 verify job 校验 manifest 与版本号")


def test_container_does_not_tag_dev_sha_on_release_tags():
    text = _read(CONTAINER)
    assert re.search(
        r"type=sha,prefix=dev-,enable=\$\{\{\s*github\.ref_type\s*!=\s*'tag'\s*\}\}",
        text), "正式 tag 构建不得额外生成 dev-<sha> 标签"
    assert "TGDL_BUILD=${{ steps.buildmeta.outputs.short_sha }}" in text, (
        "镜像构建号必须使用与桌面端一致的 7 位短 SHA")


def test_release_job_repairs_prerelease_state_on_existing_release():
    text = _read(BUILD)
    assert "gh release view" in text and "gh release edit" in text, (
        "Release 已存在时必须显式 edit 修正 prerelease/latest，而不是只 upload")
    assert "--prerelease=false" in text, "正式 tag 必须清除历史 prerelease 状态"


def test_compose_default_image_matches_current_stable():
    text = _read(COMPOSE)
    assert "ghcr.io/xmiqz/tg-video-downloader:v2.5.2" in text, (
        "compose 默认镜像必须指向当前稳定版 v2.5.2")
    assert ":latest" not in text


def test_agents_file_has_release_guardrails():
    assert AGENTS.is_file(), "仓库根目录必须有 AGENTS.md"
    text = _read(AGENTS)
    for needle in ("main", "Tag", "SHA", "测试", "数据卷"):
        assert needle in text, f"AGENTS.md 缺少「{needle}」相关约束"
