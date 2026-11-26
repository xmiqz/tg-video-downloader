"""Build numbers are commit short SHAs, including local builds."""
from __future__ import annotations

from pathlib import Path

import build


def test_injected_sha_is_normalized_to_seven_chars(monkeypatch):
    monkeypatch.setenv("TGDL_BUILD", "F7036F921CB653D37F28D50141C88BA2ADB78082")
    assert build._project_build() == "f7036f9"


def test_injected_build_number_is_trimmed(monkeypatch):
    monkeypatch.setenv("TGDL_BUILD", "  f7036f9  ")
    assert build._project_build() == "f7036f9"


def test_local_build_falls_back_to_git_short_sha(monkeypatch):
    monkeypatch.delenv("TGDL_BUILD", raising=False)
    monkeypatch.setattr(build, "_git_short_sha", lambda: "f7036f9")
    assert build._project_build() == "f7036f9"


def test_no_source_identity_stays_empty_instead_of_using_round_labels(monkeypatch):
    monkeypatch.delenv("TGDL_BUILD", raising=False)
    monkeypatch.setattr(build, "_git_short_sha", lambda: "")
    assert build._project_build() == ""


def test_manual_round_label_is_not_accepted(monkeypatch):
    monkeypatch.setenv("TGDL_BUILD", "r9")
    monkeypatch.setattr(build, "_git_short_sha", lambda: "f7036f9")
    assert build._project_build() == "f7036f9"


def test_release_workflow_uses_the_resolved_source_sha():
    root = Path(__file__).resolve().parents[1]
    workflow = (root / ".github" / "workflows" / "build.yml").read_text(
        encoding="utf-8")
    # 构建号仍必须是解析出的源码 SHA，只是解析改由 preflight 统一完成，
    # 不再直接信任可变的 github.ref / workflow 输入。
    assert "TGDL_BUILD: ${{ needs.preflight.outputs.source_sha }}" in workflow
    assert "ref: ${{ needs.preflight.outputs.source_ref }}" in workflow
    assert "rebuild_existing" in workflow
