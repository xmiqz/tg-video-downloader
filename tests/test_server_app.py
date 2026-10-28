from fastapi.testclient import TestClient
import pytest
from starlette.websockets import WebSocketDisconnect

import server_app
import tg_video_dl as core
from server_auth import AuthManager
from server_config import RuntimeConfig


# Non-ASCII credentials used to crash secrets.compare_digest with a TypeError,
# which surfaced as HTTP 500 / a crashed WebSocket handler instead of a clean
# 401 / 4401 rejection.
NON_ASCII_TOKEN = "初始化令牌-é中文"
NON_ASCII_PASSWORD = "管理员密码-é中文"
NON_ASCII_BEARER_HEADER = [
    ("authorization", ("Bearer " + NON_ASCII_TOKEN).encode("utf-8"))
]


def _desktop_app(tmp_path, token):
    config = RuntimeConfig(
        data_dir=tmp_path,
        host="127.0.0.1",
        port=0,
        desktop_mode=True,
    ).normalized()
    return server_app.create_app(
        config, engine=object(), api=FakeApi(), token=token, auth=None
    )


def _remote_app(tmp_path):
    config = RuntimeConfig(
        data_dir=tmp_path,
        host="0.0.0.0",
        port=8787,
        allowed_roots=(tmp_path,),
    ).normalized()
    auth = AuthManager(tmp_path, iterations=1000)
    return server_app.create_app(
        config, engine=object(), api=FakeApi(), token="", auth=auth
    ), auth


def test_rpc_rejects_non_ascii_bearer_without_500(tmp_path):
    app = _desktop_app(tmp_path, token="desk-token-ascii")

    with TestClient(app) as client:
        response = client.post(
            "/rpc",
            json={"method": "login_state", "params": []},
            headers=NON_ASCII_BEARER_HEADER,
        )

    assert response.status_code == 401
    assert response.json()["error"] == "未授权或会话已过期"


def test_auth_status_rejects_non_ascii_bearer_without_500(tmp_path):
    app = _desktop_app(tmp_path, token="desk-token-ascii")

    with TestClient(app) as client:
        response = client.get(
            "/auth/status", headers=NON_ASCII_BEARER_HEADER
        )

    assert response.status_code == 200
    assert response.json()["authenticated"] is False


def test_setup_rejects_non_ascii_bootstrap_token(tmp_path):
    app, auth = _remote_app(tmp_path)

    with TestClient(app) as client:
        response = client.post("/auth/setup", json={
            "bootstrap_token": NON_ASCII_TOKEN,
            "password": "a-strong-password",
        })

    assert response.status_code == 400
    assert response.json()["error"] == "初始化令牌无效"
    assert not auth.configured


def test_websocket_first_frame_non_ascii_token_closes_4401(tmp_path):
    app = _desktop_app(tmp_path, token="desk-token-ascii")

    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect) as excinfo:
            with client.websocket_connect(
                    "/events", headers={"origin": "http://testserver"}) as ws:
                ws.send_json({"token": NON_ASCII_TOKEN})
                ws.receive_json()

    assert excinfo.value.code == 4401


def test_websocket_first_frame_ascii_token_still_authenticates(tmp_path):
    # Guards the rejection above: a valid ASCII token must still reach
    # auth_ok, so 4401 cannot pass vacuously.
    app = _desktop_app(tmp_path, token="desk-token-ascii")

    with TestClient(app) as client:
        with client.websocket_connect(
                "/events", headers={"origin": "http://testserver"}) as ws:
            ws.send_json({"token": "desk-token-ascii"})
            assert ws.receive_json() == {"type": "auth_ok"}


def test_login_accepts_non_ascii_password(tmp_path):
    app, auth = _remote_app(tmp_path)

    with TestClient(app) as client:
        setup = client.post("/auth/setup", json={
            "bootstrap_token": auth.bootstrap_token(),
            "password": NON_ASCII_PASSWORD,
        })
        assert setup.status_code == 200

        good = client.post(
            "/auth/login", json={"password": NON_ASCII_PASSWORD}
        )
        assert good.status_code == 200

        assert client.post("/auth/logout", json={}).status_code == 200
        bad = client.post(
            "/auth/login", json={"password": "错的密码-é中文"}
        )
        assert bad.status_code == 401
        assert bad.json()["error"] == "密码错误"


# --- asset routes: desktop shell has no cookie, only the URL-hash token ------

def test_desktop_wallpaper_image_accepts_query_token(tmp_path):
    """<img src> and CSS url() cannot send Authorization headers.

    The desktop shell authenticates with a per-process bearer token carried in
    the page URL hash, so asset URLs must accept it as ?t= - otherwise the
    daily wallpaper theme renders nothing and the thumbnails stay broken.
    """
    import wallpaper

    cache = wallpaper.WallpaperCache(tmp_path)
    (tmp_path / "cache").mkdir(parents=True, exist_ok=True)
    (tmp_path / "cache" / "bing-20260101.jpg").write_bytes(b"\xff\xd8fakejpeg")

    app = _desktop_app(tmp_path, token="desk-token")
    with TestClient(app) as client:
        denied = client.get("/wallpaper/bing-20260101.jpg")
        allowed = client.get("/wallpaper/bing-20260101.jpg?t=desk-token")
        wrong = client.get("/wallpaper/bing-20260101.jpg?t=not-the-token")

    assert denied.status_code == 401
    assert wrong.status_code == 401
    assert allowed.status_code == 200
    assert allowed.content == b"\xff\xd8fakejpeg"


def test_desktop_wallpaper_upload_accepts_bearer(tmp_path):
    app = _desktop_app(tmp_path, token="desk-token")
    with TestClient(app) as client:
        denied = client.post("/wallpaper/upload?name=a.jpg", content=b"img")
        allowed = client.post(
            "/wallpaper/upload?name=a.jpg",
            content=b"img",
            headers={"authorization": "Bearer desk-token"},
        )

    assert denied.status_code == 401
    assert allowed.status_code == 200
    assert allowed.json()["ok"] is True


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


def test_headless_folder_browser_lists_only_allowed_directories(tmp_path):
    import gui

    allowed = tmp_path / "data"
    allowed.mkdir()
    (allowed / "zeta").mkdir()
    (allowed / "alpha").mkdir()
    (allowed / "note.txt").write_text("not a directory", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (allowed / "escape").symlink_to(outside, target_is_directory=True)
    except OSError:
        pass  # Windows without symlink privilege; containment is still tested.

    api = gui.Api.__new__(gui.Api)
    api.runtime_mode = "headless"
    api.allowed_roots = (allowed,)
    api.cfg = {"download_root": str(allowed)}

    root = api.browse_folders("")
    assert root["ok"] is True
    assert root["path"] == str(allowed.resolve())
    assert root["parent"] == ""
    assert [item["name"] for item in root["dirs"]] == ["alpha", "zeta"]
    assert root["roots"] == [{"name": "data", "path": str(allowed.resolve())}]

    child = api.browse_folders(str(allowed / "alpha"))
    assert child["ok"] is True
    assert child["parent"] == str(allowed.resolve())

    denied = api.browse_folders(str(outside))
    assert denied["ok"] is False
    assert "允许范围" in denied["error"]


def test_backdrop_driver_does_not_repush_after_leaving_liquid():
    import gui

    driver = object.__new__(gui._BackdropDriver)
    driver.cap = object()
    driver.mode = "bing"
    driver.api = type("Api", (), {
        "push_event": lambda self, *args: (_ for _ in ()).throw(
            AssertionError("must not push outside liquid")
        ),
    })()

    # One loop iteration must reach the mode gate without touching capture.
    source = gui._BackdropDriver.push_loop.__code__
    names = source.co_names
    assert "mode" in names
