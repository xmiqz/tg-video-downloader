# -*- coding: utf-8 -*-
"""TG 视频下载器 v2.4 HTTP/WS service.

The same application factory serves the Windows WebView2 shell and headless
Linux/Docker deployments. Authentication has two distinct layers:

* the desktop shell keeps its per-process bearer token;
* remote browser installations use a single administrator password and an
  HttpOnly session cookie.

Telegram phone/code/2FA login remains in gui.Api.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import pathlib
import secrets
import socket
import sys
import threading
import time
from contextlib import asynccontextmanager

import fastapi
import uvicorn
from fastapi import FastAPI, Request, Response, WebSocket
from fastapi.responses import (FileResponse, JSONResponse,
                             RedirectResponse)
from fastapi.staticfiles import StaticFiles

import gui
import server_auth
import tg_video_dl as core
import wallpaper
from server_config import RuntimeConfig


log = logging.getLogger("server_app")

RPC_METHODS = frozenset({
    "abort_task", "account_add_start", "account_list",
    "account_remove", "account_verify_code", "account_verify_password",
    "active_states", "choose_folder", "connect", "delete_task",
    "edit_task", "editor_data", "editor_remove", "editor_save",
    "editor_set_status", "get_appearance", "get_item_text", "get_settings",
    "get_task", "is_active", "item_action", "items_file_info", "job_cancel",
    "job_state", "jobs_state", "list_tasks",
    "login_state", "logout", "me", "open_editor", "open_folder",
    "open_item_file", "preview", "preview_cancel", "remove_items",
    "resume_task", "scan_append", "send_code", "set_appearance",
    "set_api_credentials", "set_download_root", "set_guest", "set_item",
    "start_download",
    "verify_code", "verify_password",
    # Bing photo-of-the-day wallpaper (cached under data_dir/cache)
    "wallpaper_status", "wallpaper_refresh", "wallpaper_history",
    "wallpaper_select", "wallpaper_delete", "set_wallpaper_history",
    # Task export/import (metadata only, never media)
    "tasks_exportable", "export_tasks", "import_tasks",
})

_T45_TEST_METHODS = frozenset({"t45_demo_job"})

AUTH_TIMEOUT_SECS = 10.0
STARTUP_TIMEOUT_SECS = 15.0
SERVER_THREAD_JOIN_SECS = 15.0
ENGINE_STOP_SECS = 5.0
MAX_RPC_BODY_BYTES = 1024 * 1024
EVENT_QUEUE_SIZE = 512


class _ClientGone(Exception):
    pass


class _EventHub:
    """Thread-safe event fan-out with a bounded queue per browser."""

    def __init__(self, loop):
        self._loop = loop
        self._lock = threading.Lock()
        self._queues = set()

    def bind(self, queue):
        with self._lock:
            self._queues.add(queue)

    def unbind(self, queue):
        with self._lock:
            self._queues.discard(queue)

    def push(self, name, params):
        with self._lock:
            queues = list(self._queues)
        if not queues:
            return
        message = {"event": name, "params": params}
        with contextlib.suppress(RuntimeError):
            self._loop.call_soon_threadsafe(self._fanout, queues, message)

    def close_all(self):
        with self._lock:
            queues = list(self._queues)
            self._queues.clear()
        with contextlib.suppress(RuntimeError):
            self._loop.call_soon_threadsafe(self._put_sentinels, queues)

    @staticmethod
    def _fanout(queues, message):
        for queue in queues:
            if queue.full():
                with contextlib.suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(message)

    @staticmethod
    def _put_sentinels(queues):
        for queue in queues:
            with contextlib.suppress(Exception):
                queue.put_nowait(None)


class ServerHandle:
    def __init__(self, base_url, token, api, auth, config, server, thread,
                 engine, hub):
        self._base_url = base_url
        self._token = token
        self._api = api
        self._auth = auth
        self._config = config
        self._server = server
        self._thread = thread
        self._engine = engine
        self._hub = hub
        self._closed = threading.Event()

    @property
    def base_url(self):
        return self._base_url

    @property
    def token(self):
        return self._token

    @property
    def api(self):
        return self._api

    @property
    def config(self):
        return self._config

    def push_event(self, name, params):
        if not self._closed.is_set() and self._hub is not None:
            self._hub.push(name, params)

    def shutdown(self):
        if self._closed.is_set():
            return
        self._closed.set()
        if self._hub is not None:
            self._hub.close_all()
        self._server.should_exit = True
        self._thread.join(SERVER_THREAD_JOIN_SECS)
        if self._thread.is_alive():
            log.error("server thread did not exit within %ss",
                      SERVER_THREAD_JOIN_SECS)
        _stop_engine_loop(self._engine)


def _stop_engine_loop(engine):
    loop = engine.loop
    if loop is None:
        return
    if loop.is_running():
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(loop.stop)
        deadline = time.monotonic() + ENGINE_STOP_SECS
        while loop.is_running() and time.monotonic() < deadline:
            time.sleep(0.02)
    if not loop.is_closed():
        with contextlib.suppress(Exception):
            loop.close()


_MEDIA_TYPES = {
    ".mp4": "video/mp4",
    ".mkv": "video/x-matroska",
    ".webm": "video/webm",
    ".mov": "video/quicktime",
    ".m4v": "video/mp4",
    ".avi": "video/x-msvideo",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


def _guess_media_type(name):
    """Pick a Content-Type from the file suffix (octet-stream otherwise)."""
    suffix = pathlib.Path(str(name)).suffix.lower()
    return _MEDIA_TYPES.get(suffix, "application/octet-stream")


def _json_error(message, status_code, retry_after=0):
    headers = {"Cache-Control": "no-store"}
    if retry_after:
        headers["Retry-After"] = str(int(retry_after))
    return JSONResponse(
        {"ok": False, "error": message},
        status_code=status_code,
        headers=headers,
    )


def _request_bearer(request):
    value = request.headers.get("authorization", "")
    prefix = "Bearer "
    if value.startswith(prefix):
        return value[len(prefix):]
    return ""


def _is_rpc_authorized(request, config, token, auth):
    bearer = _request_bearer(request)
    if bearer and token and server_auth.tokens_equal(bearer, token):
        return True
    if auth is not None:
        session = request.cookies.get(server_auth.COOKIE_NAME, "")
        if auth.session_valid(session):
            return server_auth.same_origin(request)
    if config.loopback and not config.desktop_mode:
        return True
    return False


def _set_session_cookie(response, auth, session_token):
    response.set_cookie(
        value=session_token,
        **auth.cookie_kwargs(),
    )
    response.headers["Cache-Control"] = "no-store"
    return response


def create_app(config, *, engine, api, token="", auth=None):
    """Build the ASGI app without taking ownership of the process lifecycle."""
    config = config.normalized()
    core.configure_data_dir(config.data_dir)
    api.runtime_mode = "desktop" if config.desktop_mode else "headless"
    api.allowed_roots = tuple(config.allowed_roots)

    if os.environ.get("TG_T45_TEST_HOOK") == "1":
        effective_methods = RPC_METHODS | _T45_TEST_METHODS
    else:
        effective_methods = RPC_METHODS

    shared = {"hub": None}

    @asynccontextmanager
    async def lifespan(app):
        hub = _EventHub(asyncio.get_running_loop())
        shared["hub"] = hub
        app.state.hub = hub
        api.push_event = hub.push
        try:
            yield
        finally:
            hub.close_all()
            api.push_event = None
            shared["hub"] = None
            app.state.hub = None

    app = FastAPI(
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    web_dir = gui.resource_dir()

    @app.middleware("http")
    async def security_headers(request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault(
            "Content-Security-Policy",
            # The optional "daily wallpaper" theme fetches the Bing image list
            # from bing.biturl.top (it serves Access-Control-Allow-Origin: *)
            # and renders the photo from www.bing.com. Both are read-only,
            # image/JSON only, and the theme is opt-in.
            "default-src 'self'; "
            "img-src 'self' data: https://www.bing.com; "
            "style-src 'self' 'unsafe-inline'; "
            "script-src 'self' 'unsafe-inline'; "
            "connect-src 'self' ws: wss: https://bing.biturl.top; "
            "object-src 'none'; "
            "base-uri 'none'; frame-ancestors 'none'",
        )
        if request.url.path in ("/rpc", "/auth/status", "/auth/setup",
                                "/auth/login", "/auth/logout"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/healthz")
    async def healthz():
        return JSONResponse(
            {"ok": True, "service": "tg-video-downloader"},
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/auth/status")
    async def auth_status(request: Request):
        authenticated = False
        if auth is not None:
            authenticated = auth.session_valid(
                request.cookies.get(server_auth.COOKIE_NAME, "")
            )
        elif config.loopback and not config.desktop_mode:
            authenticated = True
        bearer = _request_bearer(request)
        if bearer and token and server_auth.tokens_equal(bearer, token):
            authenticated = True
        return JSONResponse(
            {
                "ok": True,
                "setup_required": bool(auth is not None and not auth.configured),
                "configured": bool(auth is None or auth.configured),
                "authenticated": authenticated,
                "auth_required": bool(config.auth_required),
                "desktop": bool(config.desktop_mode),
            },
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/auth/setup")
    async def auth_setup(request: Request):
        if auth is None:
            return _json_error("当前运行模式不需要管理员密码", 409)
        if not server_auth.same_origin(request):
            return _json_error("请求来源无效", 403)
        body = await _read_json_object(request)
        if isinstance(body, JSONResponse):
            return body
        result = auth.setup(
            body.get("bootstrap_token", ""),
            body.get("password", ""),
        )
        if not result.ok:
            return _json_error(result.error, 400, result.retry_after)
        return _set_session_cookie(
            JSONResponse({"ok": True}), auth, result.session_token
        )

    @app.post("/auth/login")
    async def auth_login(request: Request):
        if auth is None:
            return _json_error("当前运行模式不需要管理员密码", 409)
        if not server_auth.same_origin(request):
            return _json_error("请求来源无效", 403)
        body = await _read_json_object(request)
        if isinstance(body, JSONResponse):
            return body
        result = auth.login(
            body.get("password", ""),
            server_auth.client_key(request, config.trust_proxy),
        )
        if not result.ok:
            status = 429 if result.retry_after else 401
            return _json_error(result.error, status, result.retry_after)
        return _set_session_cookie(
            JSONResponse({"ok": True}), auth, result.session_token
        )

    @app.post("/auth/logout")
    async def auth_logout(request: Request):
        if not server_auth.same_origin(request):
            return _json_error("请求来源无效", 403)
        if auth is not None:
            auth.logout(request.cookies.get(server_auth.COOKIE_NAME, ""))
        response = JSONResponse({"ok": True})
        response.delete_cookie(server_auth.COOKIE_NAME, path="/")
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/")
    async def root(request: Request):
        if auth is not None:
            if not auth.configured:
                return RedirectResponse("/setup", status_code=303)
            if not auth.session_valid(
                    request.cookies.get(server_auth.COOKIE_NAME, "")):
                return RedirectResponse("/login", status_code=303)
        return FileResponse(os.path.join(web_dir, "index.html"))

    @app.get("/setup")
    async def setup_page():
        if auth is None:
            return RedirectResponse("/", status_code=303)
        if auth.configured:
            return RedirectResponse("/login", status_code=303)
        return FileResponse(os.path.join(web_dir, "setup.html"))

    @app.get("/login")
    async def login_page(request: Request):
        if auth is None:
            return RedirectResponse("/", status_code=303)
        if not auth.configured:
            return RedirectResponse("/setup", status_code=303)
        if auth.session_valid(request.cookies.get(server_auth.COOKIE_NAME, "")):
            return RedirectResponse("/", status_code=303)
        return FileResponse(os.path.join(web_dir, "setup.html"))

    @app.post("/rpc")
    async def rpc(request: Request):
        if not _is_rpc_authorized(request, config, token, auth):
            return _json_error("未授权或会话已过期", 401)
        body = await _read_json_object(request)
        if isinstance(body, JSONResponse):
            return body

        method = body.get("method")
        params = body.get("params", [])
        if not isinstance(method, str) or not method:
            return _json_error("method 缺失或不是字符串", 400)
        if not isinstance(params, list):
            return _json_error("params 必须是数组", 400)
        if method.startswith("_") or method not in effective_methods:
            return _json_error(f"未知方法：{method}", 400)

        handler = getattr(api, method)
        try:
            result = await asyncio.to_thread(handler, *params)
            return JSONResponse(result)
        except Exception as exc:
            # Do not log exception text: Telegram/config exceptions can contain
            # user-entered identifiers. The authenticated caller still gets it.
            log.error("RPC failed: %s (%s)", method, type(exc).__name__)
            return _json_error(f"{type(exc).__name__}: {exc}", 500)

    @app.websocket("/events")
    async def events(ws: WebSocket):
        if not server_auth.websocket_same_origin(ws):
            await ws.close(code=1008)
            return
        await ws.accept()

        authenticated = (
            (config.loopback and not config.desktop_mode)
            or (auth is not None and auth.session_valid(
                ws.cookies.get(server_auth.COOKIE_NAME, "")))
        )
        if not authenticated:
            try:
                first = await asyncio.wait_for(
                    ws.receive_json(), timeout=AUTH_TIMEOUT_SECS
                )
            except Exception:
                with contextlib.suppress(Exception):
                    await ws.close(code=4401)
                return
            supplied = str(first.get("token", "")) if isinstance(first, dict) else ""
            authenticated = bool(
                token and supplied and server_auth.tokens_equal(supplied, token)
            )
        if not authenticated:
            with contextlib.suppress(Exception):
                await ws.close(code=4401)
            return

        await ws.send_json({"type": "auth_ok"})
        queue = asyncio.Queue(maxsize=EVENT_QUEUE_SIZE)
        hub = shared["hub"]
        if hub is None:
            await ws.close(code=1011)
            return
        hub.bind(queue)

        async def read_loop():
            while True:
                message = await ws.receive()
                if message.get("type") == "websocket.disconnect":
                    raise _ClientGone

        async def write_loop():
            while True:
                payload = await queue.get()
                if payload is None:
                    raise _ClientGone
                await ws.send_json(payload)

        reader = asyncio.create_task(read_loop())
        writer = asyncio.create_task(write_loop())
        try:
            await asyncio.gather(reader, writer)
        except Exception:
            pass
        finally:
            hub.unbind(queue)
            reader.cancel()
            writer.cancel()
            with contextlib.suppress(Exception):
                await ws.close()

    @app.get("/media/file")
    async def media_file(request: Request, task: str = "", src: str = "",
                         mid: str = ""):
        """Stream a downloaded item to the browser (same-origin).

        Headless mode cannot open files on the server desktop, so the UI gets
        this URL instead and lets the browser play/save it. Range is honoured
        because video scrubbing needs it. Path resolution goes through the
        same helpers the rest of the API uses, so TGDL_ALLOWED_ROOTS still
        applies.
        """
        if not (config.loopback and not config.desktop_mode):
            if auth is None or not auth.session_valid(
                    request.cookies.get(server_auth.COOKIE_NAME, "")):
                bearer = _request_bearer(request)
                if not (bearer and token
                        and server_auth.tokens_equal(bearer, token)):
                    return _json_error("未授权或会话已过期", 401)
        info = api.item_file_info(str(task), str(src), str(mid))
        if not info.get("exists"):
            return _json_error("文件不存在", 404)
        path = pathlib.Path(str(info["path"]))
        size = path.stat().st_size
        ctype = _guess_media_type(path.name)
        range_header = request.headers.get("range", "")
        if range_header.startswith("bytes="):
            spec = range_header[6:].split(",", 1)[0].strip()
            try:
                start_s, _, end_s = spec.partition("-")
                if start_s:
                    start = max(0, int(start_s))
                    end = int(end_s) if end_s else size - 1
                else:
                    length = int(end_s or 0)
                    start = max(0, size - length)
                    end = size - 1
            except (TypeError, ValueError):
                return _json_error("Range 非法", 416)
            end = min(end, size - 1)
            if start > end:
                return _json_error("Range 非法", 416)
            body = path.read_bytes()[start:end + 1]
            return Response(
                content=body,
                status_code=206,
                media_type=ctype,
                headers={
                    "Content-Range": f"bytes {start}-{end}/{size}",
                    "Accept-Ranges": "bytes",
                    "Content-Length": str(len(body)),
                    "Cache-Control": "private, max-age=3600",
                },
            )
        return FileResponse(
            path,
            media_type=ctype,
            headers={"Accept-Ranges": "bytes",
                     "Cache-Control": "private, max-age=3600"},
        )

    @app.post("/wallpaper/upload")
    async def wallpaper_upload(request: Request):
        """Accept a user background image (multipart or raw body).

        Kept out of /rpc on purpose: the 1 MiB RPC body cap is far below a
        photo. This endpoint has its own, larger limit.
        """
        if not (config.loopback and not config.desktop_mode):
            if auth is None or not auth.session_valid(
                    request.cookies.get(server_auth.COOKIE_NAME, "")):
                bearer = _request_bearer(request)
                if not (bearer and token
                        and server_auth.tokens_equal(bearer, token)):
                    return _json_error("未授权或会话已过期", 401)
        # Raw body + ?name=... instead of multipart: python-multipart is not a
        # dependency of this image, and adding it just for one upload is not
        # worth the extra wheel. The browser sends the File via fetch(Blob).
        filename = str(request.query_params.get("name", "") or "")
        declared = request.headers.get("content-length")
        if declared is not None:
            try:
                if int(declared) > wallpaper.MAX_BYTES:
                    return _json_error("图片超过 8 MB", 413)
            except (TypeError, ValueError):
                return _json_error("Content-Length 非法", 400)
        blob = await request.body()
        if not blob:
            return _json_error("没有收到图片", 400)
        if len(blob) > wallpaper.MAX_BYTES:
            return _json_error("图片超过 8 MB", 413)
        wp = getattr(api, "_wallpaper_cache", None)
        if wp is None:
            wp = wallpaper.WallpaperCache(config.data_dir)
            api._wallpaper_cache = wp
        result = wp.import_image(blob, filename=filename)
        return JSONResponse(result, status_code=200 if result.get("ok") else 400)

    @app.get("/wallpaper/{name}")
    async def wallpaper_image(name: str, request: Request):
        """Serve a cached Bing image from the data dir (same-origin).

        Serving it ourselves means the page never has to reach www.bing.com,
        so the wallpaper still renders offline and needs no cross-origin
        image exception once the file is cached.
        """
        if not (config.loopback and not config.desktop_mode):
            if auth is None or not auth.session_valid(
                    request.cookies.get(server_auth.COOKIE_NAME, "")):
                bearer = _request_bearer(request)
                if not (bearer and token
                        and server_auth.tokens_equal(bearer, token)):
                    return _json_error("未授权或会话已过期", 401)
        wp = getattr(api, "_wallpaper_cache", None)
        if wp is None:
            wp = wallpaper.WallpaperCache(config.data_dir)
            api._wallpaper_cache = wp
        path = wp.image_path(name)
        if path is None:
            return _json_error("壁纸不存在", 404)
        return FileResponse(
            path,
            media_type="image/jpeg",
            headers={"Cache-Control": "public, max-age=86400"},
        )

    app.mount("/", StaticFiles(directory=web_dir, html=True), name="web")
    app.state.runtime_config = config
    app.state.auth = auth
    app.state.api = api
    app.state.engine = engine
    app.state.rpc_methods = effective_methods
    return app


async def _read_json_object(request):
    # Check the declared size before touching the body: awaiting request.body()
    # on an oversized Content-Length would buffer/block until the client stops
    # sending, which is a cheap DoS. Reject up front, then keep the post-read
    # length check for clients that lie about Content-Length.
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            declared_len = int(declared)
        except (TypeError, ValueError):
            return _json_error("Content-Length 非法", 400)
        if declared_len < 0:
            return _json_error("Content-Length 非法", 400)
        if declared_len > MAX_RPC_BODY_BYTES:
            return _json_error("请求体过大", 413)
    try:
        raw = await request.body()
    except Exception:
        return _json_error("请求体读取失败", 400)
    if len(raw) > MAX_RPC_BODY_BYTES:
        return _json_error("请求体过大", 413)
    try:
        import json
        body = json.loads(raw.decode("utf-8"))
    except Exception:
        return _json_error("请求体不是合法 JSON", 400)
    if not isinstance(body, dict):
        return _json_error("请求体必须是 JSON 对象", 400)
    return body


def _prepare_runtime(config):
    config = config.normalized()
    core.configure_data_dir(config.data_dir)
    core.ensure_dirs_and_migrate()
    engine = gui.EngineLoop()
    engine.start()
    try:
        api = gui.Api(engine)
        api.data_dir = config.data_dir
        api.cfg = core.load_config()
        # Restore an existing Telegram session at boot. Without this, a
        # container restart leaves api.client = None even though the session
        # file is present, so every RPC answers need_login and the UI spins
        # forever on the first search. Failure here must not block startup:
        # the operator can still reach the wizard and log in again.
        try:
            restored = api.connect()
        except Exception as exc:  # noqa: BLE001 - boot must not abort
            log.warning("启动时恢复 Telegram 会话失败：%s", exc)
        else:
            stage = restored.get("stage") if isinstance(restored, dict) else None
            if stage == "ready":
                log.info("已从本地会话恢复 Telegram 登录")
            elif stage == "need_phone":
                log.info("本地会话未授权，等待浏览器完成登录")
            else:
                log.warning("启动时恢复 Telegram 会话未就绪：%s", restored)
        auth = None
        if config.auth_required:
            auth = server_auth.AuthManager(
                config.data_dir,
                session_hours=config.session_hours,
                cookie_secure=config.cookie_secure,
                bootstrap_token_file=config.bootstrap_token_file,
            )
        return config, engine, api, auth
    except Exception:
        _stop_engine_loop(engine)
        raise


def _make_listener(host, port):
    infos = socket.getaddrinfo(
        host, port, socket.AF_UNSPEC, socket.SOCK_STREAM,
        socket.IPPROTO_TCP, socket.AI_PASSIVE,
    )
    if not infos:
        raise OSError(f"无法解析监听地址：{host}")
    family, socktype, proto, _, sockaddr = infos[0]
    listener = socket.socket(family, socktype, proto)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(sockaddr)
    return listener


def start_in_thread(config):
    """Start the service in a background thread for the Windows shell."""
    config = config.normalized()
    token = secrets.token_urlsafe(24)
    config, engine, api, auth = _prepare_runtime(config)
    try:
        app = create_app(config, engine=engine, api=api, token=token, auth=auth)
        listener = _make_listener(config.host, config.port)
    except Exception:
        _stop_engine_loop(engine)
        raise
    actual_port = listener.getsockname()[1]
    base_url = config.base_url.rsplit(":", 1)[0] + f":{actual_port}"

    uv_config = uvicorn.Config(
        app,
        host=config.host,
        port=actual_port,
        log_level="warning",
        access_log=False,
        timeout_graceful_shutdown=5,
    )
    server = uvicorn.Server(uv_config)
    server.install_signal_handlers = lambda: None

    def _serve():
        try:
            server.run(sockets=[listener])
        except Exception:
            log.exception("server thread failed")

    thread = threading.Thread(target=_serve, name="server_app-uvicorn")
    thread.start()
    deadline = time.monotonic() + STARTUP_TIMEOUT_SECS
    while not server.started:
        if not thread.is_alive():
            with contextlib.suppress(Exception):
                listener.close()
            _stop_engine_loop(engine)
            raise RuntimeError("服务线程在启动完成前退出")
        if time.monotonic() > deadline:
            server.should_exit = True
            thread.join(5)
            with contextlib.suppress(Exception):
                listener.close()
            _stop_engine_loop(engine)
            raise RuntimeError("服务启动超时")
        time.sleep(0.02)

    print(f"[server_app] listening on {base_url} "
          f"(fastapi {fastapi.__version__}, uvicorn {uvicorn.__version__})")
    return ServerHandle(
        base_url, token, api, auth, config, server, thread, engine,
        getattr(app.state, "hub", None),
    )


def start(data_dir=None):
    """Compatibility entry used by the Windows desktop shell."""
    path = data_dir if data_dir is not None else core.APP_DIR / core.DATA_DIR_NAME
    config = RuntimeConfig(
        data_dir=path,
        host="127.0.0.1",
        port=0,
        desktop_mode=True,
    ).normalized()
    return start_in_thread(config)


def run_blocking(config):
    """Run the headless service in the main thread with normal signal handling."""
    config = config.normalized()
    if os.name != "nt":
        os.umask(0o077)
    config, engine, api, auth = _prepare_runtime(config)
    try:
        app = create_app(config, engine=engine, api=api, token="", auth=auth)
        print(
            f"[server_app] data={config.data_dir} "
            f"listen={config.host}:{config.port} "
            f"auth={'required' if config.auth_required else 'loopback'}"
        )
        if auth is not None and not auth.configured:
            print(
                "[server_app] 首次启动：请从 "
                f"{config.bootstrap_token_file} 读取一次性初始化令牌"
            )
        if config.auth_required and not config.cookie_secure:
            print(
                "[server_app] 警告：远程访问当前使用 HTTP。跨不可信网络访问时"
                "请置于 HTTPS 反向代理之后并设置 TGDL_COOKIE_SECURE=1"
            )

        uv_config = uvicorn.Config(
            app,
            host=config.host,
            port=config.port,
            log_level="info",
            access_log=True,
            timeout_graceful_shutdown=10,
        )
        server = uvicorn.Server(uv_config)
        server.run()
    finally:
        _stop_engine_loop(engine)


def _configure_logging():
    """Route warnings/errors to stdout so container logs show them.

    Without this, log.warning() calls in gui.jobs and server_app are dropped
    by the root handler-less logger, which hid a boot-time connect failure
    behind an apparently silent service.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="[%(name)s] %(levelname)s %(message)s",
        stream=sys.stdout,
    )
    for name in ("server_app", "gui.jobs"):
        logging.getLogger(name).setLevel(logging.INFO)


def main(argv=None):
    from server_config import load_runtime_config
    _configure_logging()
    try:
        config = load_runtime_config(argv)
        run_blocking(config)
    except (ValueError, OSError, RuntimeError) as exc:
        print(f"[server_app] 启动失败：{exc}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
