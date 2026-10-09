# -*- coding: utf-8 -*-
"""TG 视频下载器 v2.0 —— 进程内 HTTP/WS 协议层。

本模块是页面与内核之间的唯一协议入口：系统 WebView2 壳与未来无头/容器形态
复用同一个 app。设计约束：

- RPC 方法名 / JSON 字段与 v1.2.6 完全一致（前端零改动）。方法原样返回其
  Python 返回值（dict 或 list），本层只做透明 JSON 编解码。
- 内核（gui.Api / gui.EngineLoop / tg_video_dl）行为与并发语义不变：
  所有 RPC 调用经 asyncio.to_thread 落到工作线程，绝不阻塞 ASGI 事件循环。

对外入口：server_app.start(data_dir=None) -> ServerHandle
"""
import asyncio
import contextlib
import logging
import secrets
import socket
import threading
import time
from contextlib import asynccontextmanager

import fastapi
import uvicorn
from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

import gui
import tg_video_dl as core

log = logging.getLogger("server_app")

# RPC 方法白名单（35 个，逐字钉死；与 v1.2.6 window.pywebview.api 暴露面一致）
RPC_METHODS = frozenset({
    "abort_task", "active_states", "choose_folder", "connect", "delete_task",
    "edit_task", "editor_data", "editor_remove", "editor_save",
    "editor_set_status", "get_appearance", "get_item_text", "get_settings",
    "get_task", "is_active", "item_action", "items_file_info", "list_tasks",
    "login_state", "logout", "me", "open_editor", "open_folder",
    "open_item_file", "preview", "remove_items", "resume_task",
    "scan_append", "send_code", "set_appearance", "set_download_root",
    "set_item", "start_download", "verify_code", "verify_password",
})

AUTH_TIMEOUT_SECS = 10.0       # WS 首条认证消息等待上限
STARTUP_TIMEOUT_SECS = 15.0
SERVER_THREAD_JOIN_SECS = 15.0
ENGINE_STOP_SECS = 5.0


class _ClientGone(Exception):
    """WS 对端断开或服务端要求连接退出的内部信号。"""


class _EventHub:
    """向全部已认证 WS 连接扇出事件；push() 可从任意线程调用。

    每个连接在 ASGI 循环内持有一个 asyncio.Queue；跨线程推送经
    loop.call_soon_threadsafe 入队，由连接自己的 writer 任务发出，
    避免跨线程操作 WebSocket。
    """

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
        self._loop.call_soon_threadsafe(self._fanout, queues, message)

    def close_all(self):
        """让全部连接的 writer 立即退出（shutdown 时用，避免优雅关停等待）。"""
        with self._lock:
            queues = list(self._queues)
            self._queues.clear()
        self._loop.call_soon_threadsafe(self._put_sentinels, queues)

    @staticmethod
    def _fanout(queues, message):
        for queue in queues:
            try:
                queue.put_nowait(message)
            except Exception:  # noqa: BLE001 - 扇出失败不影响其它连接
                pass

    @staticmethod
    def _put_sentinels(queues):
        for queue in queues:
            with contextlib.suppress(Exception):
                queue.put_nowait(None)


class ServerHandle:
    """服务句柄：只暴露协议所需只读属性与 shutdown。"""

    def __init__(self, base_url, token, api, hub, server, thread, engine):
        self._base_url = base_url
        self._token = token
        self._api = api
        self._hub = hub
        self._server = server
        self._thread = thread
        self._engine = engine
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

    def push_event(self, name, params):
        """线程安全地向全部已认证连接扇出 {"event": name, "params": params}。"""
        if self._closed.is_set():
            return
        self._hub.push(name, params)

    def shutdown(self):
        """停止接收、断开全部连接、join 服务线程并关闭内核事件循环。幂等。"""
        if not self._closed.wait(0):
            self._closed.set()
        else:
            return

        # 1) 先主动断开全部 WS 连接，否则 uvicorn 优雅关停会等待长连接
        with contextlib.suppress(RuntimeError):
            self._hub.close_all()
        # 2) 请求 uvicorn 退出（信号处理器在非主线程不会安装，这里直接置位，
        #    uvicorn main_loop 每 0.1 秒读取该标志）
        self._server.should_exit = True
        self._thread.join(SERVER_THREAD_JOIN_SECS)
        if self._thread.is_alive():
            log.error("server thread did not exit within %ss",
                      SERVER_THREAD_JOIN_SECS)
        # 3) 停止并关闭内核 EngineLoop 事件循环
        _stop_engine_loop(self._engine)


def _stop_engine_loop(engine):
    """停止 gui.EngineLoop 的 run_forever 并关闭其循环。"""
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


def start(data_dir=None):
    """启动进程内 HTTP/WS 服务。

    data_dir：v2.0 仅支持 None，即沿用 tg_video_dl 现有数据目录约定
    （程序平级目录「TG视频下载器数据」）。传非 None 一律 ValueError。
    """
    if data_dir is not None:
        raise ValueError(
            "server_app v2.0 仅支持 data_dir=None（沿用 tg_video_dl 现有数据"
            "目录约定：程序平级「TG视频下载器数据」）；显式数据目录尚未支持"
        )

    # 启动顺序与 gui.main 一致：先起 EngineLoop，再建 Api 并提前加载配置
    engine = gui.EngineLoop()
    engine.start()
    api = gui.Api(engine)
    api.cfg = core.load_config()

    token = secrets.token_urlsafe(24)

    # 预绑定 127.0.0.1:0 的监听套接字，实际端口由系统分配
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]

    shared = {"hub": None}

    @asynccontextmanager
    async def lifespan(_app):
        # 服务在本线程内运行，事件循环即 ASGI 循环
        shared["hub"] = _EventHub(asyncio.get_running_loop())
        yield

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None,
                  openapi_url=None)

    @app.post("/rpc")
    async def rpc(request: Request):
        # ---- 鉴权 ----
        authorization = request.headers.get("authorization", "")
        if not secrets.compare_digest(authorization, "Bearer " + token):
            return JSONResponse(
                {"ok": False, "error": "未授权或令牌无效"}, status_code=401)

        # ---- 请求体解析 ----
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return JSONResponse(
                {"ok": False, "error": "请求体不是合法 JSON"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse(
                {"ok": False, "error": "请求体必须是 JSON 对象"},
                status_code=400)

        method = body.get("method")
        params = body.get("params", [])
        if not isinstance(method, str) or not method:
            return JSONResponse(
                {"ok": False, "error": "method 缺失或不是字符串"},
                status_code=400)
        if not isinstance(params, list):
            return JSONResponse(
                {"ok": False, "error": "params 必须是数组"}, status_code=400)
        if method.startswith("_") or method not in RPC_METHODS:
            return JSONResponse(
                {"ok": False, "error": f"未知方法：{method}"}, status_code=400)

        # ---- 透明调用：在线程池执行，绝不阻塞事件循环 ----
        handler = getattr(api, method)
        try:
            result = await asyncio.to_thread(handler, *params)
            # JSONResponse 构造时即完成序列化；序列化失败同样走 500
            return JSONResponse(result)
        except Exception as exc:  # noqa: BLE001
            # 只回异常类型与消息，禁止泄漏堆栈
            return JSONResponse(
                {"ok": False, "error": f"{type(exc).__name__}: {exc}"},
                status_code=500)

    @app.websocket("/events")
    async def events(ws: WebSocket):
        await ws.accept()

        # 首条消息必须是 {"token": token}；无消息（超时）/错 token 均 4401
        try:
            first = await asyncio.wait_for(
                ws.receive_json(), timeout=AUTH_TIMEOUT_SECS)
        except Exception:  # noqa: BLE001
            with contextlib.suppress(Exception):
                await ws.close(code=4401)
            return
        if (not isinstance(first, dict)
                or not secrets.compare_digest(str(first.get("token", "")),
                                              token)):
            with contextlib.suppress(Exception):
                await ws.close(code=4401)
            return

        await ws.send_json({"type": "auth_ok"})

        queue = asyncio.Queue()
        hub = shared["hub"]
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
        except Exception:  # noqa: BLE001
            pass
        finally:
            hub.unbind(queue)
            reader.cancel()
            writer.cancel()
            with contextlib.suppress(Exception):
                await ws.close()

    # /rpc、/events 必须先于静态挂载注册
    web_dir = gui.resource_dir()
    app.mount("/", StaticFiles(directory=web_dir, html=True), name="web")

    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        access_log=False,
        timeout_graceful_shutdown=5,
    )
    server = uvicorn.Server(config)
    # 双保险：本就运行在非主线程（uvicorn 因此不安装信号处理器），
    # 再显式禁用，确保后台线程绝不触碰进程信号。
    server.install_signal_handlers = lambda: None

    def _serve():
        try:
            server.run(sockets=[listener])
        except Exception:  # noqa: BLE001
            log.exception("server thread failed")

    thread = threading.Thread(target=_serve, name="server_app-uvicorn")
    thread.start()

    # 等待启动完成事件；server.started 在 startup() 末尾置 True
    deadline = time.monotonic() + STARTUP_TIMEOUT_SECS
    while not server.started:
        if not thread.is_alive():
            _stop_engine_loop(engine)
            raise RuntimeError("服务线程在启动完成前退出")
        if time.monotonic() > deadline:
            server.should_exit = True
            thread.join(5)
            _stop_engine_loop(engine)
            raise RuntimeError("服务启动超时")
        time.sleep(0.02)

    base_url = f"http://127.0.0.1:{port}"
    # 允许打印端口/版本，严禁打印 token
    print(f"[server_app] listening on {base_url} "
          f"(fastapi {fastapi.__version__}, uvicorn {uvicorn.__version__})")

    return ServerHandle(base_url, token, api, shared["hub"], server, thread,
                        engine)
