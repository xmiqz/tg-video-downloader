from fastapi.testclient import TestClient

import server_app
import tg_video_dl as core
from server_auth import AuthManager
from server_config import RuntimeConfig


class FakeApi:
    def __init__(self):
        self.runtime_mode = "desktop"
        self.allowed_roots = ()
        self.cfg = {}

    def login_state(self):
        return {"logged_in": False, "has_session": False, "guest": False}

    def get_settings(self):
        return {
            "download_root": str(self.allowed_roots[0]),
            "mode": self.runtime_mode,
        }

    def set_api_credentials(self, api_id, api_hash):
        return {"ok": True}


def test_remote_setup_cookie_and_protected_rpc(tmp_path):
    config = RuntimeConfig(
        data_dir=tmp_path,
        host="0.0.0.0",
        port=8787,
        allowed_roots=(tmp_path,),
    ).normalized()
    auth = AuthManager(tmp_path, iterations=1000)
    api = FakeApi()
    app = server_app.create_app(
        config, engine=object(), api=api, token="", auth=auth
    )

    with TestClient(app) as client:
        assert client.get("/healthz").json()["ok"] is True
        denied = client.post("/rpc", json={"method": "login_state", "params": []})
        assert denied.status_code == 401
        setup_page = client.get("/setup")
        assert setup_page.status_code == 200
        assert setup_page.headers["x-frame-options"] == "DENY"
        assert "default-src 'self'" in setup_page.headers["content-security-policy"]
        status = client.get("/auth/status").json()
        assert status["setup_required"] is True

        token = (tmp_path / ".setup-token").read_text(encoding="utf-8").strip()
        setup = client.post("/auth/setup", json={
            "bootstrap_token": token,
            "password": "a-strong-password",
        })
        assert setup.status_code == 200
        cookie = setup.headers["set-cookie"].lower()
        assert "httponly" in cookie
        assert "samesite=strict" in cookie
        assert not (tmp_path / ".setup-token").exists()

        allowed = client.post("/rpc", json={
            "method": "get_settings", "params": [],
        })
        assert allowed.status_code == 200
        assert allowed.json()["mode"] == "headless"

        assert client.post("/auth/logout", json={}).status_code == 200
        denied_again = client.post(
            "/rpc", json={"method": "login_state", "params": []}
        )
        assert denied_again.status_code == 401
        login_page = client.get("/", follow_redirects=False)
        assert login_page.status_code == 303
        assert login_page.headers["location"] == "/login"
        bad_login = client.post(
            "/auth/login", json={"password": "wrong password"}
        )
        assert bad_login.status_code == 401
        good_login = client.post(
            "/auth/login", json={"password": "a-strong-password"}
        )
        assert good_login.status_code == 200

        with client.websocket_connect(
                "/events", headers={"origin": "http://testserver"}) as ws:
            assert ws.receive_json() == {"type": "auth_ok"}


def test_desktop_bearer_is_still_required(tmp_path):
    config = RuntimeConfig(
        data_dir=tmp_path,
        host="127.0.0.1",
        port=0,
        desktop_mode=True,
    ).normalized()
    api = FakeApi()
    app = server_app.create_app(
        config, engine=object(), api=api, token="desk-token", auth=None
    )

    with TestClient(app) as client:
        denied = client.post("/rpc", json={"method": "login_state", "params": []})
        assert denied.status_code == 401
        allowed = client.post(
            "/rpc",
            json={"method": "login_state", "params": []},
            headers={"authorization": "Bearer desk-token"},
        )
        assert allowed.status_code == 200


def test_headless_folder_is_confined(tmp_path):
    import gui

    allowed = tmp_path / "data"
    allowed.mkdir()
    api = gui.Api.__new__(gui.Api)
    api.runtime_mode = "headless"
    api.allowed_roots = (allowed,)
    api.cfg = {}

    ok_path, error = api._resolve_runtime_folder(
        allowed / "downloads", create=True
    )
    assert not error
    assert ok_path.is_dir()

    outside, error = api._resolve_runtime_folder(
        tmp_path / "outside", create=True
    )
    assert outside is None
    assert "允许范围" in error
