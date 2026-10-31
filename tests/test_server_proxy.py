# -*- coding: utf-8 -*-
"""T61-followup: 反向代理转发头信任范围收敛（TGDL_TRUSTED_PROXIES）。

验收口径（任务书 §4）：

1. ``TGDL_TRUST_PROXY=1`` + 默认白名单（仅回环 127.0.0.1 / ::1）：
   回环直连的 X-Forwarded-For 参与限速键，其余直连来源一律忽略转发头；
2. 显式 ``TGDL_TRUSTED_PROXIES`` 与默认回环集合取并集（回环永远可信，T61b 裁决 2）；
3. ``TGDL_TRUST_PROXY=0`` 时转发头完全不看（无论白名单）；
4. Cookie Secure = TGDL_COOKIE_SECURE OR（可信来源的 X-Forwarded-Proto=https）。
"""
from __future__ import annotations

import json
import pathlib
import re
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import server_auth
from _v1_support import make_remote_app
from server_auth import client_key, is_trusted_proxy_host, request_is_https
from server_config import RuntimeConfig, load_runtime_config

ROOT = pathlib.Path(__file__).resolve().parent.parent
PROXY_DOC = ROOT / "docs" / "nas-reverse-proxy.md"
BACKUP_DOC = ROOT / "docs" / "nas-backup-restore.md"
README = ROOT / "README.md"

GOOD_PASSWORD = "a-strong-password"


@pytest.fixture(autouse=True)
def clean_env(tmp_path, monkeypatch):
    """与 test_v1_config.py 同口径的干净 TGDL_* 环境。"""
    for name in [
        "TGDL_HOST", "TGDL_PORT", "TGDL_ALLOWED_ROOTS", "TGDL_TRUST_PROXY",
        "TGDL_TRUSTED_PROXIES", "TGDL_COOKIE_SECURE", "TGDL_SESSION_HOURS",
        "TGDL_SETUP_TOKEN_FILE", "TGDL_CONFIG",
    ]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TGDL_DATA_DIR", str(tmp_path / "data"))
    (tmp_path / "data").mkdir(exist_ok=True)
    return tmp_path


def _configured(tmp_path, monkeypatch, **kwargs):
    app, auth = make_remote_app(tmp_path, monkeypatch, **kwargs)
    setup = auth.setup(auth.bootstrap_token(), GOOD_PASSWORD)
    assert setup.ok
    return app, auth


def _forwarded_request(client_host="127.0.0.1", scheme="http", xff=None,
                       xfp=None, path="/auth/login"):
    headers = {}
    if xff is not None:
        headers["x-forwarded-for"] = xff
    if xfp is not None:
        headers["x-forwarded-proto"] = xfp
    return SimpleNamespace(
        client=SimpleNamespace(host=client_host) if client_host else None,
        headers=headers,
        url=SimpleNamespace(path=path, scheme=scheme),
    )


# ---------- §4.6 配置解析 ----------

def test_t61fu_env_parsing_comma_ip_cidr_whitespace(clean_env, monkeypatch):
    monkeypatch.setenv("TGDL_TRUSTED_PROXIES",
                       "192.168.2.50, 10.0.0.0/8 ,, ::1")
    config = load_runtime_config([])
    assert config.trusted_proxies == (
        "192.168.2.50/32", "10.0.0.0/8", "::1/128",
    )


def test_t61fu_env_empty_and_default_values(clean_env, monkeypatch):
    assert load_runtime_config([]).trusted_proxies == ()
    monkeypatch.setenv("TGDL_TRUSTED_PROXIES", "  , ,")
    assert load_runtime_config([]).trusted_proxies == ()


def test_t61fu_invalid_value_raises_without_echoing_input(
        clean_env, monkeypatch):
    raw = "not-an-ip.example"
    monkeypatch.setenv("TGDL_TRUSTED_PROXIES", raw)
    with pytest.raises(ValueError) as excinfo:
        load_runtime_config([])
    assert raw not in str(excinfo.value)


def test_t61fu_file_key_and_env_precedence(clean_env, monkeypatch):
    data = clean_env / "data"
    (data / "server.json").write_text(
        json.dumps({"trusted_proxies": ["192.168.2.50", "10.0.0.0/8"]}),
        encoding="utf-8",
    )
    assert load_runtime_config([]).trusted_proxies == (
        "192.168.2.50/32", "10.0.0.0/8",
    )
    monkeypatch.setenv("TGDL_TRUSTED_PROXIES", "203.0.113.0/24")
    assert load_runtime_config([]).trusted_proxies == ("203.0.113.0/24",)


def test_t61fu_runtime_config_default_and_normalized_dedupe(clean_env):
    config = RuntimeConfig(data_dir=clean_env / "data")
    assert config.trusted_proxies == ()
    normalized = RuntimeConfig(
        data_dir=clean_env / "data",
        trusted_proxies=("192.168.2.50", " 192.168.2.50 ", "10.0.0.0/8"),
    ).normalized()
    assert normalized.trusted_proxies == (
        "192.168.2.50/32", "10.0.0.0/8",
    )
    # normalized() 幂等
    assert normalized.normalized().trusted_proxies == (
        "192.168.2.50/32", "10.0.0.0/8",
    )


def test_t61fu_normalized_rejects_bad_entries(clean_env):
    with pytest.raises(ValueError):
        RuntimeConfig(
            data_dir=clean_env / "data", trusted_proxies=("999.999.1.1",)
        ).normalized()
    with pytest.raises(ValueError):
        RuntimeConfig(data_dir=clean_env / "data", trusted_proxies=(123,)).normalized()
    with pytest.raises(ValueError):
        RuntimeConfig(
            data_dir=clean_env / "data", trusted_proxies=("10.0.0.5/99",)
        ).normalized()


# ---------- §4.5 is_trusted_proxy_host 矩阵 ----------

@pytest.mark.parametrize("host,expected", [
    ("127.0.0.1", True),
    ("::1", True),
    (" 127.0.0.1 ", True),
    # 回环段里的其他地址不在默认精确集合内
    ("127.255.255.254", False),
    ("10.0.0.1", False),
    ("192.168.2.50", False),
    ("203.0.113.5", False),
    ("localhost", False),
    ("example.com", False),
    ("", False),
    (None, False),
    ("0.0.0.0", False),
    ("::", False),
])
def test_t61fu_trusted_proxy_host_default_matrix(host, expected):
    assert is_trusted_proxy_host(host) is expected


def test_t61fu_trusted_proxy_host_whitelist_unions_loopback():
    proxies = ("192.168.2.50", "10.0.0.0/8")
    # 命中白名单（单 IP 与 CIDR 均可）
    assert is_trusted_proxy_host("192.168.2.50", proxies) is True
    assert is_trusted_proxy_host("10.0.0.7", proxies) is True
    # 未命中 / 公网 / 非 IP → False
    assert is_trusted_proxy_host("192.168.2.51", proxies) is False
    assert is_trusted_proxy_host("203.0.113.5", proxies) is False
    assert is_trusted_proxy_host("localhost", proxies) is False
    # T61b 裁决 2：显式白名单与回环取并集，回环永远可信
    assert is_trusted_proxy_host("127.0.0.1", proxies) is True
    assert is_trusted_proxy_host("::1", proxies) is True
    assert is_trusted_proxy_host("127.255.255.254", proxies) is False
    # 规范化后的 /32、/128 形式同样可用
    assert is_trusted_proxy_host("203.0.113.5", ("203.0.113.5/32",)) is True
    assert is_trusted_proxy_host("2001:db8::1", ("2001:db8::/64",)) is True
    # 非法白名单条目：不可信的白名单条目被忽略，但回环仍可信
    assert is_trusted_proxy_host("203.0.113.5", ("garbage",)) is False
    assert is_trusted_proxy_host("127.0.0.1", ("garbage",)) is True


# ---------- 向后兼容调用形状 ----------

def test_t61fu_client_and_https_backward_compatible_call_shapes():
    request = _forwarded_request(client_host="127.0.0.1", xff="9.9.9.9")
    # 单参数 / 两参数（既有调用）不得抛错，且默认直连键不变
    assert client_key(request) == "127.0.0.1|/auth/login"
    assert client_key(request, True).startswith("9.9.9.9|")
    assert client_key(request, True, ()).startswith("9.9.9.9|")
    assert request_is_https(request) is False
    assert request_is_https(request, True) is False
    # 缺失 client 的请求形状（unknown 直连端永远不可信）
    no_client = _forwarded_request(client_host=None, xff="9.9.9.9")
    assert client_key(no_client, True) == "unknown|/auth/login"


# ---------- §4.6 trust_proxy=0 时白名单不生效 ----------

def test_t61fu_trust_proxy_off_ignores_forwarded_headers():
    request = _forwarded_request(
        client_host="192.168.2.50", scheme="http",
        xff="1.2.3.4", xfp="https",
    )
    proxies = ("192.168.2.50",)
    # 开关关闭：即使直连端在白名单内也完全不看转发头
    assert client_key(request, trust_proxy=False,
                      trusted_proxies=proxies).startswith("192.168.2.50|")
    assert request_is_https(request, trust_proxy=False,
                             trusted_proxies=proxies) is False
    # 开关打开且直连端可信：转发头生效
    assert client_key(request, trust_proxy=True,
                      trusted_proxies=proxies).startswith("1.2.3.4|")
    assert request_is_https(request, trust_proxy=True,
                            trusted_proxies=proxies) is True


# ---------- §4.1–§4.3 /auth/login 限速键 ----------

def _fail_once(client):
    return client.post(
        "/auth/login",
        json={"password": "wrong password"},
        headers={"x-forwarded-for": "1.2.3.4"},
    ).status_code


def test_t61fu_rate_limit_key_uses_xff_from_default_loopback(
        tmp_path, monkeypatch):
    app, auth = _configured(tmp_path, monkeypatch, trust_proxy=True)
    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        assert _fail_once(client) == 401
    assert set(auth._failures) == {"1.2.3.4|/auth/login"}


def test_t61fu_rate_limit_key_untrusted_lan_source_uses_direct_host(
        tmp_path, monkeypatch):
    app, auth = _configured(tmp_path, monkeypatch, trust_proxy=True)
    with TestClient(app, client=("192.168.2.50", 50000)) as client:
        assert _fail_once(client) == 401
    assert set(auth._failures) == {"192.168.2.50|/auth/login"}


def test_t61fu_rate_limit_key_whitelisted_source_uses_xff(
        tmp_path, monkeypatch):
    app, auth = _configured(
        tmp_path, monkeypatch,
        trust_proxy=True, trusted_proxies=("192.168.2.50",),
    )
    with TestClient(app, client=("192.168.2.50", 50000)) as client:
        assert _fail_once(client) == 401
    assert set(auth._failures) == {"1.2.3.4|/auth/login"}


def test_t61fu_rate_limit_key_loopback_still_trusted_with_whitelist(
        tmp_path, monkeypatch):
    """T61b 裁决 2：配置 LAN 白名单后，同机回环反代仍可采信 XFF。"""
    app, auth = _configured(
        tmp_path, monkeypatch,
        trust_proxy=True, trusted_proxies=("192.168.2.50",),
    )
    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        assert _fail_once(client) == 401
    assert set(auth._failures) == {"1.2.3.4|/auth/login"}


def test_t61fu_rate_limit_isolation_end_to_end(tmp_path, monkeypatch):
    """被伪造 XFF 封禁的键不影响其他客户端（新信任语义下的 TR8 行为）。"""
    app, auth = _configured(tmp_path, monkeypatch, trust_proxy=True)
    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        for _ in range(server_auth.MAX_FAILURES):
            response = client.post(
                "/auth/login",
                json={"password": "wrong password"},
                headers={"x-forwarded-for": "203.0.113.10"},
            )
            assert response.status_code == 401
        blocked = client.post(
            "/auth/login",
            json={"password": GOOD_PASSWORD},
            headers={"x-forwarded-for": "203.0.113.10"},
        )
        other = client.post(
            "/auth/login",
            json={"password": GOOD_PASSWORD},
            headers={"x-forwarded-for": "198.51.100.20"},
        )
    assert blocked.status_code == 429
    assert other.status_code == 200


# ---------- §4.4 Cookie Secure ----------

def test_t61fu_cookie_secure_matrix(tmp_path, monkeypatch):
    # 1) trust_proxy=0 + XFP=https → 无 Secure
    app, _ = _configured(tmp_path, monkeypatch, trust_proxy=False)
    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        login = client.post(
            "/auth/login",
            json={"password": GOOD_PASSWORD},
            headers={"x-forwarded-proto": "https"},
        )
        assert login.status_code == 200
    cookie = login.headers["set-cookie"].lower()
    assert "secure" not in cookie

    # 2) trust_proxy=1 + 可信回环来源 + XFP=https → 有 Secure
    app, _ = _configured(tmp_path / "b", monkeypatch, trust_proxy=True)
    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        login = client.post(
            "/auth/login",
            json={"password": GOOD_PASSWORD},
            headers={"x-forwarded-proto": "https"},
        )
        assert login.status_code == 200
    cookie = login.headers["set-cookie"].lower()
    assert "secure" in cookie

    # 3) TGDL_COOKIE_SECURE=1 + XFP=http → 仍有 Secure（OR 语义）
    app, _ = _configured(
        tmp_path / "c", monkeypatch, cookie_secure=True, trust_proxy=True
    )
    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        login = client.post(
            "/auth/login",
            json={"password": GOOD_PASSWORD},
            headers={"x-forwarded-proto": "http"},
        )
        assert login.status_code == 200
    cookie = login.headers["set-cookie"].lower()
    assert "secure" in cookie

    # 4) trust_proxy=1 但来源不可信（默认白名单外）+ XFP=https → 无 Secure
    app, _ = _configured(tmp_path / "d", monkeypatch, trust_proxy=True)
    with TestClient(app, client=("192.168.2.50", 50000)) as client:
        login = client.post(
            "/auth/login",
            json={"password": GOOD_PASSWORD},
            headers={"x-forwarded-proto": "https"},
        )
        assert login.status_code == 200
    cookie = login.headers["set-cookie"].lower()
    assert "secure" not in cookie


def test_t61fu_setup_cookie_honours_secure_semantics(tmp_path, monkeypatch):
    """/auth/setup 的 Set-Cookie 走同一 Secure 判定。"""
    app, auth = make_remote_app(tmp_path, monkeypatch, trust_proxy=True)
    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        setup = client.post(
            "/auth/setup",
            json={"bootstrap_token": auth.bootstrap_token(),
                  "password": GOOD_PASSWORD},
            headers={"x-forwarded-proto": "https"},
        )
        assert setup.status_code == 200
    assert "secure" in setup.headers["set-cookie"].lower()


# ---------- request_is_https 矩阵 ----------

@pytest.mark.parametrize("scheme,xfp,trust,proxies,expected", [
    ("https", None, False, (), True),            # 直连 https 一票通过
    ("https", "http", False, (), True),          # 开关关也优先直连 scheme
    ("http", "https", False, (), False),         # 开关关完全不看 XFP
    ("http", "https", True, (), True),           # 回环 + XFP https
    ("http", " HTTPS ,http", True, (), True),     # 第一段、去空白、大小写
    ("http", "http,https", True, (), False),     # 第一段不是 https
    ("http", "  ", True, (), False),             # 空白头
    ("http", "https", True, ("192.168.2.50",), True),   # 白名单与回环取并集
    ("http", "https", True, ("192.168.2.50",) * 0 + ("10.0.0.0/8",), True),
    ("http", "https", False, ("127.0.0.1",), False),    # 开关关白名单无效
])
def test_t61fu_request_is_https_matrix(scheme, xfp, trust, proxies, expected):
    request = _forwarded_request(
        client_host="127.0.0.1", scheme=scheme, xfp=xfp,
    )
    assert request_is_https(
        request, trust_proxy=trust, trusted_proxies=proxies
    ) is expected


def test_t61fu_request_is_https_whitelisted_source():
    request = _forwarded_request(
        client_host="10.0.0.7", scheme="http", xfp="https",
    )
    assert request_is_https(
        request, trust_proxy=True, trusted_proxies=("10.0.0.0/8",)
    ) is True


# ---------- §4.7 文档契约 ----------

def test_t61fu_docs_exist_and_readme_links_them():
    assert PROXY_DOC.is_file(), f"{PROXY_DOC} 必须存在"
    assert BACKUP_DOC.is_file()
    readme = README.read_text(encoding="utf-8")
    assert re.search(r"\]\((\./)?docs/nas-reverse-proxy\.md\)", readme), (
        "README 需要指向 docs/nas-reverse-proxy.md 的有效链接"
    )


def test_t61fu_reverse_proxy_doc_contract_tokens():
    text = PROXY_DOC.read_text(encoding="utf-8")
    for token in (
        "Caddy", "nginx", "tg-video-downloader", "8787", "/healthz",
        "TGDL_TRUST_PROXY", "TGDL_TRUSTED_PROXIES", "TGDL_COOKIE_SECURE",
        "X-Forwarded-For", "X-Forwarded-Proto",
    ):
        assert token in text, f"反代文档缺少「{token}」"
    # 必须写明覆盖（而非追加）转发头，且不得出现全网段示例
    assert "覆盖" in text, "文档必须写明反代要覆盖转发头"
    assert "0.0.0.0/0" not in text, "文档不得出现全网段白名单示例"
    # 失败限速数字与源码常量一致
    assert str(server_auth.MAX_FAILURES) in text
    assert str(server_auth.FAILURE_WINDOW_SECS) in text
    assert str(server_auth.MAX_BLOCK_SECS) in text


def test_t61fu_backup_doc_marks_data_as_sensitive():
    text = BACKUP_DOC.read_text(encoding="utf-8")
    assert "敏感级" in text, "备份文档需说明 /data 的敏感级"
    assert "tg_config.json" in text and "tg_session" in text
    assert "0600" in text
