"""Unit tests for tools/release_preflight.py.

The workflows call this script to decide whether a run may publish. The rules
are tested here instead of only being asserted as YAML text, because a wrong
decision here means a manual run can overwrite a published Release or push an
image built from a commit the tag does not point at.
"""
from __future__ import annotations

import pathlib
import shutil
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import release_preflight as rp  # noqa: E402


@pytest.mark.parametrize("tag", ["v2.5.2", "v3.0.0", "v2.5.2-beta.1", "v10.0.1"])
def test_accepts_release_tags(tag):
    assert rp.is_release_tag(tag)


@pytest.mark.parametrize(
    "tag", ["", "2.5.2", "main", "v2.5", "v2", "v2.5.2/extra", "release-v2.5.2"])
def test_rejects_non_release_tags(tag):
    assert not rp.is_release_tag(tag)


def test_build_tag_push_publishes_that_tag():
    plan = rp.plan_build(
        "push", ref_name="v2.5.2", ref="refs/tags/v2.5.2", sha="a" * 40)
    assert plan == rp.Plan(True, "v2.5.2", "a" * 40)


def test_build_tag_push_rejects_non_release_tag():
    with pytest.raises(rp.PreflightError):
        rp.plan_build(
            "push", ref_name="v2.5", ref="refs/tags/v2.5", sha="a" * 40)


def test_build_tag_push_rejects_branch_ref():
    with pytest.raises(rp.PreflightError):
        rp.plan_build(
            "push", ref_name="main", ref="refs/heads/main", sha="a" * 40)


def test_build_dispatch_without_inputs_is_dev_build():
    plan = rp.plan_build(
        "workflow_dispatch", ref_name="main", ref="refs/heads/main", sha="b" * 40)
    assert plan.publish is False
    assert plan.release_tag == ""
    assert plan.source_ref == "b" * 40


def test_build_dispatch_rebuild_requires_tag():
    with pytest.raises(rp.PreflightError, match="release_tag"):
        rp.plan_build(
            "workflow_dispatch",
            rebuild_existing="true",
            source_ref="v2.5.2",
            sha="c" * 40,
        )


def test_build_dispatch_rebuild_requires_source_ref():
    with pytest.raises(rp.PreflightError, match="source_ref"):
        rp.plan_build(
            "workflow_dispatch",
            rebuild_existing="true",
            release_tag="v2.5.2",
            sha="c" * 40,
        )


def test_build_dispatch_tag_without_rebuild_is_rejected():
    with pytest.raises(rp.PreflightError, match="rebuild_existing"):
        rp.plan_build(
            "workflow_dispatch",
            release_tag="v2.5.2",
            source_ref="v2.5.2",
            sha="c" * 40,
        )


def test_build_dispatch_rebuild_publishes_requested_tag():
    plan = rp.plan_build(
        "workflow_dispatch",
        rebuild_existing="true",
        release_tag="v2.5.2",
        source_ref="v2.5.2",
        sha="c" * 40,
    )
    assert plan == rp.Plan(True, "v2.5.2", "v2.5.2")


def test_build_dispatch_rebuild_rejects_bad_tag():
    with pytest.raises(rp.PreflightError, match="vX.Y.Z"):
        rp.plan_build(
            "workflow_dispatch",
            rebuild_existing="true",
            release_tag="main",
            source_ref="main",
            sha="c" * 40,
        )


def test_container_dispatch_default_is_validate_only():
    plan = rp.plan_container(
        "workflow_dispatch", publish="", ref_name="main",
        ref="refs/heads/main", sha="d" * 40)
    assert plan.publish is False
    assert plan.release_tag == ""


def test_container_dispatch_publish_requires_tag():
    with pytest.raises(rp.PreflightError, match="tag"):
        rp.plan_container(
            "workflow_dispatch", publish="true", ref_name="main",
            ref="refs/heads/main", sha="d" * 40)


def test_container_dispatch_publish_rejects_branch_like_tag():
    with pytest.raises(rp.PreflightError, match="vX.Y.Z"):
        rp.plan_container(
            "workflow_dispatch", publish="true", release_tag="main",
            ref_name="main", ref="refs/heads/main", sha="d" * 40)


def test_container_dispatch_publish_uses_requested_tag():
    plan = rp.plan_container(
        "workflow_dispatch", publish="true", release_tag="v2.5.2",
        ref_name="main", ref="refs/heads/main", sha="d" * 40)
    assert plan == rp.Plan(True, "v2.5.2", "")


def test_container_pull_request_never_publishes():
    plan = rp.plan_container(
        "pull_request", publish="true", release_tag="v2.5.2",
        ref="refs/pull/7/merge", sha="e" * 40)
    assert plan.publish is False
    assert plan.release_tag == ""


def test_verify_same_commit_rejects_mismatch():
    with pytest.raises(rp.PreflightError, match="禁止用不相关的提交"):
        rp.verify_same_commit("v2.5.2", "a" * 40, "b" * 40)


def test_verify_same_commit_accepts_equal_sha():
    rp.verify_same_commit("v2.5.2", "a" * 40, "a" * 40)


def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def test_annotated_tag_resolves_to_tagged_commit(tmp_path):
    if shutil.which("git") is None:
        pytest.skip("git 不可用")
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "a.txt").write_text("1", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-q", "-m", "one")
    tagged = _git(repo, "rev-parse", "HEAD")
    _git(repo, "tag", "-a", "v2.5.2", "-m", "release")
    (repo / "a.txt").write_text("2", encoding="utf-8")
    _git(repo, "commit", "-q", "-am", "two")
    head = _git(repo, "rev-parse", "HEAD")

    assert head != tagged
    assert rp.resolve_commit("refs/tags/v2.5.2", cwd=repo) == tagged
    with pytest.raises(rp.PreflightError):
        rp.verify_same_commit("v2.5.2", tagged, head)
