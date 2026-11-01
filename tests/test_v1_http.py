"""V1 TR4/TR5/TR6/TR7: static serving, headers, and origin policy."""
from __future__ import annotations

from fastapi.testclient import TestClient
import pytest
from starlette.websockets import WebSocketDisconnect

import server_app
import server_auth
from _v1_support import (
    live_server,
    make_desktop_app,
    make_remote_app,
    make_web_tree,
    raw_http,
    request_like,
)


LEAK_MARKER = "V1-OUTSIDE-WEB-SECRET"


def test_tr4_encoded_traversal_cannot_reach_outside_web(tmp_path, monkeypatch):
    app = make_desktop_app(tmp_path, monkeypatch)
    attempts = [
        "/..%2F..%2Fsecret.txt",
        "/js/..%2F..%2F..%2Fsecret.txt",
        "/%2e%2e/%2e%2e/secret.txt",
        "/js/%2e%2e/%2e%2e/%2e%2e/secret.txt",
        "/%2e%2e%5c%2e%2e%5csecret.txt",
        "/js/..%5c..%5c..%5csecret.txt",
    ]
    with live_server(app) as port:
        for path in attempts:
            response = raw_http(port, path)
            assert response.status_code in (400, 404), (path, response.status_code)
            assert LEAK_MARKER not in response.text, path


def test_tr4_dotdot_paths_cannot_reach_outside_web(tmp_path, monkeypatch):
    app = make_desktop_app(tmp_path, monkeypatch)
    attempts = [
        "/../secret.txt",
        "/../../secret.txt",
        "/js/../../secret.txt",
        "/js/../../../secret.txt",
        "//../secret.txt",
    ]
    with live_server(app) as port:
        for path in attempts:
            response = raw_http(port, path)
            assert response.status_code in (400, 404), (path, response.status_code)
            assert LEAK_MARKER not in response.text, path


def test_tr4_absolute_path_injection_is_rejected(tmp_path, monkeypatch):
    app = make_desktop_app(tmp_path, monkeypatch)
    absolute = str((tmp_path / "secret.txt").resolve()).replace("\\", "/")
    attempts = [
        "/%2F" + absolute.lstrip("/"),
        "//" + absolute.lstrip("/"),
        "/js/%2F" + absolute.lstrip("/"),
        "/..%2F" + absolute.lstrip("/"),
        "/js/C:/Windows/win.ini",
        "/%2FC%3A/Windows/win.ini",
        "/js/%2FC%3A/Windows/win.ini",
    ]
    with live_server(app) as port:
        for path in attempts:
            response = raw_http(port, path)
            assert response.status_code in (400, 404), (path, response.status_code)
            assert LEAK_MARKER not in response.text, path


def test_tr4_normal_static_files_still_serve(tmp_path, monkeypatch):
    make_web_tree(tmp_path, monkeypatch)
    app = make_desktop_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        asset = client.get("/js/app.js")
        index = client.get("/")
        missing = client.get("/not-a-real-asset")
    assert asset.status_code == 200
    assert asset.text == "console.log('v1');"
    assert index.status_code == 200
    assert "v1-index" in index.text
    assert missing.status_code == 404


def test_tr4_staticfiles_does_not_follow_symlinks():
    from starlette.staticfiles import StaticFiles

    assert StaticFiles(directory=".").follow_symlink is False


HEADER_CASES = [
    ("get", "/healthz", {}),
    ("get", "/", {}),
    ("get", "/login", {}),
    ("post", "/rpc", {"json": {"method": "login_state", "params": []},
                      "headers": {"authorization": "Bearer desk-token"}}),
    ("post", "/auth/logout", {}),
    ("get", "/definitely-missing-static", {}),
]


@pytest.mark.parametrize("method,path,kwargs", HEADER_CASES)
def test_tr5_security_headers_on_every_response(
    tmp_path, monkeypatch, method, path, kwargs
):
    app = make_desktop_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        response = getattr(client, method)(path, **kwargs)
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "no-referrer"
    csp = response.headers["content-security-policy"]
    assert "default-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp
    assert "object-src 'none'" in csp


@pytest.mark.parametrize(
    "method,path,kwargs",
    [
        ("get", "/healthz", {}),
        ("get", "/auth/status", {}),
        ("post", "/auth/setup", {"json": {}}),
        ("post", "/auth/login", {"json": {}}),
        ("post", "/auth/logout", {}),
        ("post", "/rpc", {"json": {}}),
    ],
)
def test_tr5_sensitive_routes_are_no_store(
    tmp_path, monkeypatch, method, path, kwargs
):
    app = make_desktop_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        response = getattr(client, method)(path, **kwargs)
    assert response.headers.get("cache-control") == "no-store"


@pytest.mark.parametrize(
    "origin",
    [
        "http://evil.example",
        "https://evil.example",
        "http://testserver:9999",
        "ftp://testserver",
        "http://testserver.evil.com",
        "null",
    ],
)
def test_tr6_websocket_cross_origin_is_rejected(tmp_path, monkeypatch, origin):
    app = make_desktop_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect) as excinfo:
            with client.websocket_connect(
                "/events", headers={"origin": origin}
            ) as ws:
                ws.send_json({"token": "desk-token"})
                ws.receive_json()
    assert excinfo.value.code == 1008


def test_tr6_same_origin_websocket_still_authenticates(tmp_path, monkeypatch):
    app = make_desktop_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        with client.websocket_connect(
            "/events", headers={"origin": "http://testserver"}
        ) as ws:
            ws.send_json({"token": "desk-token"})
            assert ws.receive_json() == {"type": "auth_ok"}


@pytest.mark.parametrize(
    "origin,host",
    [
        ("http://other.example", "testserver"),
        ("http://testserver:9999", "testserver"),
        ("ftp://testserver", "testserver"),
        ("javascript://testserver", "testserver"),
        ("http://", "testserver"),
        ("http://testserver/path", "testserver:8080"),
    ],
)
def test_tr7_same_origin_negative_cases(origin, host):
    request = request_like(origin=origin, host=host)
    assert server_auth.same_origin(request) is False


@pytest.mark.parametrize(
    "origin,host",
    [
        ("http://testserver", "testserver"),
        ("http://TESTserver", "testserver"),
        ("https://testserver:8443", "testserver:8443"),
    ],
)
def test_tr7_same_origin_positive_controls(origin, host):
    request = request_like(origin=origin, host=host)
    assert server_auth.same_origin(request) is True


def test_tr7_same_origin_accepts_missing_origin_by_current_contract():
    # Known v2.4 risk: a missing Origin header is treated as same-origin.
    # Pinned here so a future change to this policy is a deliberate decision.
    assert server_auth.same_origin(request_like(origin=None)) is True


def test_tr7_empty_origin_header_is_treated_as_absent():
    # Same known risk as a missing header: ``if not origin`` short-circuits.
    assert server_auth.same_origin(request_like(origin="")) is True
    assert server_auth.websocket_same_origin(request_like(origin="")) is True


def test_tr7_websocket_same_origin_negative_cases():
    for origin, host in [
        ("http://other.example", "testserver"),
        ("http://testserver:9999", "testserver"),
        ("ftp://testserver", "testserver"),
        ("http://", "testserver"),
    ]:
        assert server_auth.websocket_same_origin(
            request_like(origin=origin, host=host)
        ) is False
    assert server_auth.websocket_same_origin(
        request_like(origin="http://testserver", host="testserver")
    ) is True


def test_tr7_auth_endpoints_reject_cross_origin_without_consuming_setup(
    tmp_path, monkeypatch
):
    app, auth = make_remote_app(tmp_path, monkeypatch)
    token = auth.bootstrap_token()
    with TestClient(app) as client:
        setup = client.post(
            "/auth/setup",
            json={"bootstrap_token": token, "password": "a-strong-password"},
            headers={"origin": "http://evil.example"},
        )
        login = client.post(
            "/auth/login",
            json={"password": "a-strong-password"},
            headers={"origin": "http://evil.example"},
        )
        logout = client.post(
            "/auth/logout", json={}, headers={"origin": "http://evil.example"}
        )
    for response in (setup, login, logout):
        assert response.status_code == 403
        assert response.json() == {"ok": False, "error": "请求来源无效"}
    assert auth.configured is False
    assert auth.token_file.exists()
    assert auth.bootstrap_token() == token


def test_tr7_websocket_origin_rejection_is_policy_not_auth(tmp_path, monkeypatch):
    # 1008 (policy violation) must not be confused with 4401 (auth failure).
    app = make_desktop_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect) as excinfo:
            with client.websocket_connect(
                "/events", headers={"origin": "http://evil.example"}
            ):
                pass
    assert excinfo.value.code == 1008
    assert excinfo.value.code != 4401


def test_tr4_static_mount_is_the_last_route(tmp_path, monkeypatch):
    # The "/" mount must not shadow API routes; guards accidental reordering.
    app = make_desktop_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        assert client.get("/healthz").json()["service"] == "tg-video-downloader"
        assert client.get("/auth/status").status_code == 200
        assert client.post("/rpc", json={}).status_code == 401
    assert server_app.RPC_METHODS
