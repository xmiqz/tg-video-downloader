"""Release preflight rules for the build and container workflows.

Both workflows used to derive their publish target from ``github.ref_name``.
On ``workflow_dispatch`` that value is the selected *branch*, so a manual run
could try to create a release named ``main`` or publish an image tag that does
not exist.  Keeping the rules here (instead of inline workflow shell) makes
them unit testable; the workflows only resolve git refs and pass the result in.

Usage (from a workflow step):

    python3 tools/release_preflight.py \
        --workflow build --event workflow_dispatch \
        --release-tag v2.5.2 --source-ref v2.5.2 --rebuild-existing true \
        --ref-name main --ref refs/heads/main --sha "$GITHUB_SHA" \
        --github-output "$GITHUB_OUTPUT"
"""
from __future__ import annotations

import argparse
import dataclasses
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional, Sequence

RELEASE_TAG_RE = re.compile(r"^v\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.\-]+)?$")


class PreflightError(Exception):
    """Raised when the requested release operation is not allowed."""


@dataclasses.dataclass(frozen=True)
class Plan:
    """What the workflow is allowed to do with this run."""

    publish: bool
    release_tag: str
    source_ref: str

    def outputs(self, source_sha: str) -> dict:
        return {
            "publish": "true" if self.publish else "false",
            "release_tag": self.release_tag,
            "source_ref": source_ref_or_sha(self, source_sha),
            "source_sha": source_sha,
            "short_sha": source_sha[:7],
        }


def source_ref_or_sha(plan: Plan, source_sha: str) -> str:
    """Checkout target: the resolved commit, never a moving branch name."""
    return source_sha or plan.source_ref


def is_release_tag(tag: str) -> bool:
    return bool(RELEASE_TAG_RE.match((tag or "").strip()))


def _truthy(value) -> bool:
    return str(value or "").strip().lower() == "true"


def plan_build(
    event: str,
    *,
    ref_name: str = "",
    ref: str = "",
    sha: str = "",
    release_tag: str = "",
    source_ref: str = "",
    rebuild_existing="false",
) -> Plan:
    """Rules for .github/workflows/build.yml (desktop assets + Release)."""
    tag = (release_tag or "").strip()
    src = (source_ref or "").strip()

    if event == "push":
        if not ref.startswith("refs/tags/"):
            raise PreflightError(
                f"build.yml 只允许由 Tag 推送发布，收到的是 {ref or '<empty>'}")
        if not is_release_tag(ref_name):
            raise PreflightError(
                f"Tag {ref_name!r} 不是 vX.Y.Z 形式，拒绝构建正式 Release")
        return Plan(True, ref_name, sha)

    if event != "workflow_dispatch":
        raise PreflightError(f"build.yml 不支持的事件：{event}")

    if _truthy(rebuild_existing):
        if not tag:
            raise PreflightError("重建已有 Release 必须显式指定 release_tag")
        if not is_release_tag(tag):
            raise PreflightError(f"release_tag 必须是 vX.Y.Z 形式：{tag}")
        if not src:
            raise PreflightError(
                "重建已有 Release 必须显式指定 source_ref（指向该 Tag 的提交），"
                "否则无法保证资产与 Tag 同源")
        return Plan(True, tag, src)

    if tag:
        raise PreflightError(
            "指定 release_tag 时必须同时设置 rebuild_existing=true；"
            "否则不会更新任何 Release，只会产生开发预发布")
    # 默认开发构建：优先用事件自带的提交 SHA（一定可解析），
    # 避免依赖本地是否存在同名分支引用。
    return Plan(False, "", src or sha or ref)


def plan_container(
    event: str,
    *,
    publish="false",
    release_tag: str = "",
    ref_name: str = "",
    ref: str = "",
    sha: str = "",
) -> Plan:
    """Rules for .github/workflows/container.yml (GHCR image)."""
    if event == "push":
        if not ref.startswith("refs/tags/"):
            raise PreflightError(
                f"container.yml 只允许由 Tag 推送发布，收到的是 {ref or '<empty>'}")
        if not is_release_tag(ref_name):
            raise PreflightError(
                f"Tag {ref_name!r} 不是 vX.Y.Z 形式，拒绝发布正式镜像")
        return Plan(True, ref_name, sha)

    if event == "pull_request":
        return Plan(False, "", sha)

    if event != "workflow_dispatch":
        raise PreflightError(f"container.yml 不支持的事件：{event}")

    if not _truthy(publish):
        return Plan(False, "", sha)

    tag = (release_tag or "").strip()
    if not tag:
        raise PreflightError("手动发布必须显式指定 tag（publish=true 但 tag 为空）")
    if not is_release_tag(tag):
        raise PreflightError(f"tag 必须是 vX.Y.Z 形式：{tag}")
    return Plan(True, tag, "")


def git(args: Sequence[str], cwd: Optional[Path] = None) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise PreflightError(f"git {' '.join(args)} 失败：{detail}")
    return proc.stdout.strip()


def resolve_commit(ref: str, cwd: Optional[Path] = None) -> str:
    if not ref:
        raise PreflightError("无法解析空的 git ref")
    return git(["rev-parse", "--verify", f"{ref}^{{commit}}"], cwd=cwd)


def fetch_tag(tag: str, cwd: Optional[Path] = None, remote: str = "origin") -> str:
    git(["fetch", "--force", "--tags", remote,
         f"refs/tags/{tag}:refs/tags/{tag}"], cwd=cwd)
    return resolve_commit(f"refs/tags/{tag}", cwd=cwd)


def verify_same_commit(tag: str, tag_sha: str, source_sha: str) -> None:
    if not tag_sha or not source_sha:
        raise PreflightError(
            f"无法校验 Tag {tag} 与构建源码的一致性（tag={tag_sha!r}, source={source_sha!r}）")
    if tag_sha != source_sha:
        raise PreflightError(
            f"Tag {tag} 指向 {tag_sha[:12]}，但构建源码是 {source_sha[:12]}；"
            "禁止用不相关的提交覆盖正式 Release 资产")


def _write_outputs(values: dict, path: Optional[str]) -> None:
    lines = [f"{key}={value}" for key, value in values.items()]
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
    for line in lines:
        print(line)


def _parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workflow", choices=("build", "container"), required=True)
    parser.add_argument("--event", required=True)
    parser.add_argument("--release-tag", default="")
    parser.add_argument("--source-ref", default="")
    parser.add_argument("--rebuild-existing", default="false")
    parser.add_argument("--publish", default="false")
    parser.add_argument("--ref-name", default="")
    parser.add_argument("--ref", default="")
    parser.add_argument("--sha", default="")
    parser.add_argument("--repo", default=".")
    parser.add_argument("--github-output", default="")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parse_args(argv)
    repo = Path(args.repo)
    try:
        if args.workflow == "build":
            plan = plan_build(
                args.event,
                ref_name=args.ref_name,
                ref=args.ref,
                sha=args.sha,
                release_tag=args.release_tag,
                source_ref=args.source_ref,
                rebuild_existing=args.rebuild_existing,
            )
        else:
            plan = plan_container(
                args.event,
                publish=args.publish,
                release_tag=args.release_tag,
                ref_name=args.ref_name,
                ref=args.ref,
                sha=args.sha,
            )

        if plan.publish and plan.release_tag:
            tag_sha = fetch_tag(plan.release_tag, cwd=repo)
            if plan.source_ref:
                source_sha = resolve_commit(plan.source_ref, cwd=repo)
                verify_same_commit(plan.release_tag, tag_sha, source_sha)
            else:
                # container 手动发布：构建对象就是该 Tag 指向的提交。
                source_sha = tag_sha
        else:
            source_sha = resolve_commit(plan.source_ref or "HEAD", cwd=repo)
    except PreflightError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1

    _write_outputs(plan.outputs(source_sha), args.github_output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
