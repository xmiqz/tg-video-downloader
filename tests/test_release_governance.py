"""Release governance contract: one mainline, one version, one publish SHA."""
from __future__ import annotations

import fnmatch
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
CI = ROOT / ".github" / "workflows" / "ci.yml"
BUILD = ROOT / ".github" / "workflows" / "build.yml"
CONTAINER = ROOT / ".github" / "workflows" / "container.yml"
COMPOSE = ROOT / "compose.yaml"
AGENTS = ROOT / "AGENTS.md"
PREFLIGHT = ROOT / "tools" / "release_preflight.py"


def _read(path):
    return path.read_text(encoding="utf-8")


def _push_tags(text: str) -> list[str]:
    match = re.search(
        r"^  push:\s*$\n    tags:\s*$\n((?:      - .*\n)+)", text, re.M)
    assert match, "工作流缺少 push.tags 触发"
    return [
        line.split("-", 1)[1].strip().strip("\"'")
        for line in match.group(1).splitlines()
    ]


def test_ci_no_longer_listens_on_retired_branch():
    text = _read(CI)
    assert "v2.5-dev" not in text, "ci.yml 不得继续监听已废弃的 v2.5-dev"
    assert re.search(r"push:\s*\n\s*branches:\s*\n\s*-\s*main", text), (
        "ci.yml push 触发必须只保留 main")
    assert re.search(r"pull_request:\s*\n\s*branches:\s*\n\s*-\s*main", text), (
        "ci.yml PR 触发必须只保留 main")


def test_workflow_yaml_is_parseable():
    """语法错误的 workflow 不会产生检查结果，必须在 CI 里直接失败。"""
    yaml = pytest.importorskip("yaml")
    workflows = sorted((ROOT / ".github" / "workflows").glob("*.yml"))
    assert workflows, "未找到 workflow 文件"
    for path in workflows:
        with path.open(encoding="utf-8") as handle:
            doc = yaml.safe_load(handle)
        assert isinstance(doc, dict), f"{path.name} 不是有效的 YAML 映射"
        # YAML 1.1 会把裸 on: 解析成布尔 True，两种键都接受。
        triggers = doc.get("on", doc.get(True))
        assert isinstance(triggers, dict), f"{path.name} 缺少 on 触发定义"
        assert isinstance(doc.get("jobs"), dict), f"{path.name} 缺少 jobs"


def test_container_release_tag_pattern_covers_every_major():
    tags = _push_tags(_read(CONTAINER))
    assert tags == ["v*"], f"container 正式 Tag 触发应为 v*，实际 {tags}"
    for candidate in ("v2.5.2", "v3.0.0"):
        assert any(fnmatch.fnmatchcase(candidate, p) for p in tags), (
            f"container 必须能由 {candidate} 触发")
    assert not any(fnmatch.fnmatchcase("main", p) for p in tags)
    assert _push_tags(_read(BUILD)) == tags, (
        "桌面端与 Docker 的 Tag 触发范围必须一致")


def test_container_publish_depends_on_both_validations():
    text = _read(CONTAINER)
    assert re.search(r"validate-amd64:", text), "缺少 amd64 验证 job"
    assert re.search(r"validate-arm64:", text), "缺少 arm64 验证 job"
    assert re.search(
        r"needs:\s*\[\s*preflight\s*,\s*validate-amd64\s*,\s*validate-arm64\s*\]",
        text), "publish 必须在 preflight 与双架构验证都通过后才执行"
    assert re.search(r"verify:\s*\n\s*needs:\s*publish", text), (
        "发布后必须有 verify job 校验 manifest 与版本号")
    assert re.search(r"publish:\s*\n\s*needs:[^\n]*\n\s*if:\s*needs\.preflight\.outputs\.publish == 'true'", text), (
        "publish 必须由 preflight 的 publish 输出显式放行")


def test_container_dispatch_defaults_to_validate_only():
    text = _read(CONTAINER)
    publish_input = re.search(
        r"      publish:\n(?:.*\n)*?        default: false\n", text)
    assert publish_input, "workflow_dispatch 的 publish 输入必须默认 false"
    assert "INPUT_PUBLISH" in text and "--publish" in text, (
        "手动触发的 publish 开关必须显式传给 preflight")
    assert "tools/release_preflight.py" in text, (
        "container 必须使用带单元测试的 preflight 规则")


def test_container_verify_uses_publish_output_not_ref_name():
    text = _read(CONTAINER)
    assert text.count("TAG: ${{ needs.publish.outputs.tag }}") == 2, (
        "verify 的两步都必须使用 publish 输出的真实 tag")
    assert "GITHUB_REF_NAME" not in text.split("verify:", 1)[1], (
        "verify 不得再用 github.ref_name 拼镜像标签")
    assert "${TAG:?publish 未输出 tag" in text, (
        "tag 为空时必须直接失败，而不是校验不存在的镜像")


def test_container_does_not_tag_dev_sha_on_release_tags():
    text = _read(CONTAINER)
    assert "type=sha" not in text, "正式发布不再生成 dev-<sha> 标签"
    assert "type=raw,value=${{ needs.preflight.outputs.tag }}" in text, (
        "镜像标签必须来自 preflight 解析出的正式 Tag")
    assert "TGDL_BUILD=${{ needs.preflight.outputs.short_sha }}" in text, (
        "镜像构建号必须使用与桌面端一致的 7 位短 SHA")


def test_build_locks_source_commit_from_preflight():
    text = _read(BUILD)
    assert re.search(r"preflight:\s*\n\s*runs-on:", text), "build.yml 缺少 preflight job"
    assert "--workflow build" in text and "--rebuild-existing" in text, (
        "build.yml 必须用 preflight 规则校验手动重建参数")
    assert "ref: ${{ needs.preflight.outputs.source_ref }}" in text, (
        "构建 checkout 必须使用 preflight 解析出的提交")
    assert 'test "$sha" = "${{ needs.preflight.outputs.source_sha }}"' in text, (
        "构建前必须断言 HEAD 与 preflight 锁定提交一致")
    assert "TGDL_BUILD: ${{ needs.preflight.outputs.source_sha }}" in text, (
        "构建号必须来自 preflight 的源码 SHA")


def test_build_refuses_to_overwrite_assets_from_another_commit():
    text = _read(BUILD)
    assert "Assert tag still points at the built commit" in text, (
        "上传前必须再次核对远端 Tag 指向")
    assert re.search(r'git ls-remote origin "refs/tags/\$\{TAG\}\^\{\}"', text), (
        "必须解析 annotated tag 指向的提交")
    assert "拒绝覆盖正式资产" in text
    assert text.count("if: needs.preflight.outputs.publish == 'true'") >= 3, (
        "写 Release、打标签与上传都必须由 publish 输出放行")


def test_build_dispatch_dev_release_uses_resolved_sha():
    text = _read(BUILD)
    assert 'TAG="dev-${SRC:0:7}"' in text, (
        "开发预发布必须用 preflight 解析出的 SHA，而不是分支 HEAD")
    assert 'TAG="dev-${GITHUB_SHA::7}"' not in text


def test_release_job_repairs_prerelease_state_on_existing_release():
    text = _read(BUILD)
    assert "gh release view" in text and "gh release edit" in text, (
        "Release 已存在时必须显式 edit 修正 prerelease/latest，而不是只 upload")
    assert "--prerelease=false" in text, "正式 tag 必须清除历史 prerelease 状态"


def test_preflight_rules_live_in_tested_module():
    assert PREFLIGHT.is_file(), "tools/release_preflight.py 必须存在"
    assert (ROOT / "tests" / "test_release_preflight.py").is_file(), (
        "preflight 规则必须有对应单元测试")


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
