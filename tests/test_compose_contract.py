"""Compose contract for the NAS deployment shape (T63).

锁定 compose.yaml 的容器安全与数据契约：
  - restart: unless-stopped、healthcheck 打 http://127.0.0.1:8787/healthz
  - 默认命名卷 tgvd-data → /data，卷名固定 tg-video-downloader-data，
    服务 volumes 只挂 /data，只读根 + 唯一可写 tmpfs /tmp
  - cap_drop: ALL、no-new-privileges:true、user 默认 10001:10001
  - 镜像固定为 ghcr.io/xmiqz/tg-video-downloader 的版本 tag，禁止 :latest
  - TGDL_* 环境变量名与 server_config.py 实际读取的一字不差
同时锁定备份/升级/回滚文档（docs/nas-backup-restore.md）的存在、
必备小节与仓库名一致性。

实现约束：不引入 PyYAML 等新依赖，只用 pathlib + 正则 + 一个极简的
「顶层 key / services 缩进」解析辅助函数。本测试必须在无 Docker、
无网络的机器上通过：它锁的是文件内容契约；容器行为面由发布前 Docker / NAS
验收覆盖。
"""
from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
COMPOSE = ROOT / "compose.yaml"
README = ROOT / "README.md"
BACKUP_DOC = ROOT / "docs" / "nas-backup-restore.md"
SERVER_CONFIG = ROOT / "server_config.py"

SERVICE_NAME = "tg-video-downloader"
NAMED_VOLUME = "tgvd-data"
VOLUME_NAME = "tg-video-downloader-data"
HEALTHZ_URL = "http://127.0.0.1:8787/healthz"
IMAGE_REPO = "ghcr.io/xmiqz/tg-video-downloader"


# ---------- 极简缩进解析（仅本文件契约断言使用） ----------

def _load_lines(path):
    """[(indent, stripped_text)]：剥离空行与整行注释。"""
    out = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        out.append((len(raw) - len(raw.lstrip(" ")), stripped))
    return out


LINES = _load_lines(COMPOSE)


def _block(lines, key, indent):
    """key 行（如 'services:'）之后、缩进更深的连续子块。"""
    for idx, (i, t) in enumerate(lines):
        if i == indent and t == key:
            out = []
            for j, s in lines[idx + 1:]:
                if j <= indent:
                    return out
                out.append((j, s))
            return out
    raise AssertionError(f"compose.yaml 缺少「{key}」（缩进 {indent}）")


def _service_lines():
    return _block(_block(LINES, "services:", 0), SERVICE_NAME + ":", 2)


def _service_scalar(key):
    """服务直接子键 `key: value` 的 value 部分。"""
    for i, t in _service_lines():
        if i == 4 and t.startswith(key + ":"):
            return t.split(":", 1)[1].strip()
    raise AssertionError(f"服务 {SERVICE_NAME} 缺少「{key}:」")


def _list_items(key):
    """服务子键 key: 之下的列表项（'- ' 前缀已剥）。"""
    block = _block(_service_lines(), key + ":", 4)
    return [t[1:].strip() for _, t in block]


# ---------- 契约 1：compose.yaml ----------

def test_no_tab_indentation():
    for raw in COMPOSE.read_text(encoding="utf-8").splitlines():
        assert "\t" not in raw, f"compose.yaml 行含 tab：{raw.strip()!r}"


def test_top_level_keys_are_services_and_volumes():
    keys = [t for i, t in LINES if i == 0 and t.endswith(":")]
    assert keys == ["services:", "volumes:"], f"顶层 key 必须恰为 services+volumes，实际 {keys}"


def test_single_service_named_tg_video_downloader():
    services = _block(LINES, "services:", 0)
    names = [t for i, t in services if i == 2 and t.endswith(":")]
    assert names == [SERVICE_NAME + ":"], f"服务必须恰一个 {SERVICE_NAME}，实际 {names}"
    assert _service_scalar("container_name") == SERVICE_NAME


def test_restart_unless_stopped():
    assert _service_scalar("restart") == "unless-stopped"


def test_healthcheck_probes_loopback_healthz():
    block = _block(_service_lines(), "healthcheck:", 4)
    text = "\n".join(t for _, t in block)
    assert text, "healthcheck 块为空"
    assert HEALTHZ_URL in text, f"healthcheck 必须探活 {HEALTHZ_URL}"


def test_port_pinned_to_8787():
    assert _list_items("ports") == ['"${TGDL_PORT:-8787}:8787"']


def test_named_volume_declared_once_with_fixed_name():
    block = _block(LINES, "volumes:", 0)
    keys = [t for i, t in block if i == 2 and t.endswith(":")]
    assert keys == [NAMED_VOLUME + ":"], f"顶层 volumes 必须恰一个具名卷，实际 {keys}"
    names = [t for i, t in block if i == 4 and t.startswith("name:")]
    assert names == ["name: " + VOLUME_NAME], f"卷名必须固定为 {VOLUME_NAME}，实际 {names}"


def test_service_mounts_only_named_volume_at_data():
    items = _list_items("volumes")
    assert items == [f"{NAMED_VOLUME}:/data"], (
        f"服务 volumes 只允许命名卷挂 /data，实际 {items}")


def test_readonly_rootfs():
    assert _service_scalar("read_only") == "true"


def test_only_writable_tmpfs_is_tmp():
    """除 /data 外，唯一可写挂载必须仍是 /tmp tmpfs。"""
    tmpfs = _list_items("tmpfs")
    assert len(tmpfs) == 1, f"tmpfs 必须恰一条，实际 {tmpfs}"
    assert tmpfs[0].split(":")[0] == "/tmp", f"tmpfs 目标必须为 /tmp，实际 {tmpfs[0]}"


def test_cap_drop_all():
    assert _list_items("cap_drop") == ["ALL"]


def test_no_new_privileges():
    assert _list_items("security_opt") == ["no-new-privileges:true"]


def test_unprivileged_user_is_fixed_10001():
    value = _service_scalar("user").strip('"')
    assert value == "10001:10001", (
        "compose 必须固定 10001:10001：命名卷由镜像 /data 继承该属主，"
        f"可配置 PUID/PGID 会在 cap_drop ALL 下无法 chown，实际 {value}")


def test_image_pinned_to_versioned_tag():
    value = _service_scalar("image")
    m = re.match(r"\$\{TGVD_IMAGE:-(.+)\}$", value)
    assert m, f"镜像必须提供固定默认 tag（${{TGVD_IMAGE:-<固定tag>}}），实际 {value!r}"
    default = m.group(1)
    assert default.startswith(IMAGE_REPO + ":"), (
        f"镜像仓库必须固定为 {IMAGE_REPO}，实际 {default}")
    tag = default.rsplit(":", 1)[1]
    assert tag != "latest", "禁止 :latest"
    assert re.match(r"^v\d+(\.\d+)*", tag), f"默认 tag 必须版本化（vX.Y[.Z]），实际 {tag!r}"
    assert ":latest" not in COMPOSE.read_text(encoding="utf-8"), "compose 全文禁止 :latest"


def test_tgdl_env_names_match_server_config():
    """compose 传的每个 TGDL_* 变量必须被 server_config.py 逐字读取。"""
    env_block = _block(_service_lines(), "environment:", 4)
    compose_keys = {t.split(":", 1)[0] for _, t in env_block}
    tgdl_keys = {k for k in compose_keys if k.startswith("TGDL_")}
    cfg_text = SERVER_CONFIG.read_text(encoding="utf-8")
    cfg_keys = set(re.findall(r'os\.environ\.get\(\s*"(TGDL_[A-Z0-9_]+)"', cfg_text))
    unknown = sorted(tgdl_keys - cfg_keys)
    assert unknown == [], f"compose 环境变量 server_config.py 不读取：{unknown}"
    required = {"TGDL_HOST", "TGDL_PORT", "TGDL_DATA_DIR", "TGDL_ALLOWED_ROOTS",
                "TGDL_COOKIE_SECURE", "TGDL_TRUST_PROXY"}
    missing = sorted(required - tgdl_keys)
    assert missing == [], f"compose 缺少契约环境变量：{missing}"


def test_compose_does_not_override_baked_build_info():
    """镜像内已写入 TGDL_VERSION/TGDL_BUILD；compose 不得用空值覆盖。"""
    env_block = _block(_service_lines(), "environment:", 4)
    keys = {t.split(":", 1)[0] for _, t in env_block}
    assert "TGDL_VERSION" not in keys, "compose 不应覆盖镜像内 TGDL_VERSION"
    assert "TGDL_BUILD" not in keys, "compose 不应覆盖镜像内 TGDL_BUILD"


# ---------- 契约 2：备份/升级/回滚文档 ----------

def test_readme_links_backup_doc():
    text = README.read_text(encoding="utf-8")
    # 必须是有效 Markdown 链接目标（正文里单纯提到文件名不算数）
    assert re.search(r"\]\((\./)?docs/nas-backup-restore\.md\)", text), (
        "README 需要指向 docs/nas-backup-restore.md 的有效链接")
    assert BACKUP_DOC.is_file(), f"{BACKUP_DOC} 必须存在"


def test_backup_doc_required_sections():
    text = BACKUP_DOC.read_text(encoding="utf-8")
    # 只看 ## 级小节标题：h1 文档题目本身含全部关键词，不能作为小节存在性依据
    headings = [ln.strip() for ln in text.splitlines()
                if ln.strip().startswith("##")]
    for kw in ("备份", "升级", "回滚", "兼容"):
        assert any(kw in h for h in headings), (
            f"nas-backup-restore.md 缺少「{kw}」小节")


def test_backup_doc_repo_names_are_exact():
    """文档中的服务名/卷名/路径/端口必须与仓库实际逐字一致。"""
    text = BACKUP_DOC.read_text(encoding="utf-8")
    for token in (SERVICE_NAME, VOLUME_NAME, "/data", "8787", "/healthz"):
        assert token in text, f"文档缺少仓库实际名「{token}」"
    # 兼容边界必须点名 auth.json 追加键与回滚登录影响
    assert "session_key" in text and "revoked_sessions" in text
    assert "记住此设备" in text
