# -*- coding: utf-8 -*-
"""Telegram 登录流程的公共实现。

v2.6 把登录状态、二维码会话和渠道提示集中在这里，GUI 与无头服务通过
``gui.Api`` 的薄封装调用。本模块不写业务数据，不输出验证码、二维码 token
或任何 Telegram 身份信息到日志。
"""
from __future__ import annotations

import dataclasses
import io
import threading
import time
import typing
import asyncio
import contextlib


@dataclasses.dataclass
class QRLoginSession:
    client: typing.Any
    qr: typing.Any
    url: str
    expires: float
    generation: int
    cancelled: threading.Event = dataclasses.field(default_factory=threading.Event)
    task: typing.Any = None
    result: typing.Optional[dict] = None


def qr_svg(url: str) -> str:
    """把 Telegram ``tg://login`` URL 渲染成 SVG，不返回原始 token。"""
    import segno

    qr = segno.make(url, error="m")
    out = io.BytesIO()
    qr.save(out, kind="svg", scale=6, border=2, light="#ffffff", dark="#111111")
    svg = out.getvalue().decode("utf-8")
    if "viewBox=" not in svg:
        matrix_size = len(qr.matrix)
        scale = 6
        border = 2
        total = (matrix_size + border * 2) * scale
        svg = svg.replace(
            "<svg ",
            f'<svg viewBox="0 0 {total} {total}" preserveAspectRatio="xMidYMid meet" ',
            1,
        )
    return svg


class QRLoginManager:
    """主账号二维码登录会话管理。

    会话只存在内存中；同一时刻只允许一个二维码。新会话会取消旧会话，
    成功或失败后自动清理，避免 token 被复用。
    """

    def __init__(self, logger=None):
        self._lock = threading.Lock()
        self._session: QRLoginSession | None = None
        self._generation = 0
        self._log = logger
        self._password_error = None

    @staticmethod
    def _expires_at(qr: typing.Any) -> float:
        expires = getattr(qr, "expires", None)
        if expires is None:
            return time.time() + 60.0
        try:
            return float(expires.timestamp())
        except Exception:
            return time.time() + 60.0

    def state(self) -> dict:
        with self._lock:
            session = self._session
            if session is None:
                return {"active": False}
            remaining = max(0.0, session.expires - time.time())
            if session.result is not None:
                return {"active": False}
            return {
                "active": True,
                "expires_in": int(remaining),
            }

    def cancel(self) -> None:
        with self._lock:
            session = self._session
            self._session = None
            self._generation += 1
        if session is not None:
            session.cancelled.set()

    def start(self, *, lease_client, client_factory, engine,
              password_error=None) -> dict:
        """创建并返回二维码登录材料，后台异步等待扫码结果。"""
        self.cancel()
        self._password_error = password_error
        client = client_factory()
        session_id = self._generation

        async def _create():
            await client.connect()
            return await client.qr_login()

        try:
            qr = engine.submit(_create())
            url = qr.url
            svg = qr_svg(url)
        except Exception as exc:
            if self._log:
                self._log.info("二维码登录创建失败：%s", type(exc).__name__)
            with contextlib.suppress(Exception):
                engine.submit(client.disconnect())
            return {"ok": False, "error": self._map_error(exc)}

        session = QRLoginSession(
            client=client,
            qr=qr,
            url=url,
            expires=self._expires_at(qr),
            generation=session_id,
        )
        with self._lock:
            self._session = session

        async def _wait():
            try:
                user = await qr.wait()
                result = {"ok": True, "stage": "qr_ready", "user": user}
            except Exception as exc:
                ename = type(exc).__name__
                password_name = (
                    self._password_error.__name__
                    if self._password_error else "SessionPasswordNeededError"
                )
                if ename == password_name:
                    result = {"ok": True, "stage": "need_password", "user": None}
                elif session.cancelled.is_set():
                    result = None
                else:
                    if self._log:
                        self._log.info("二维码登录等待失败：%s", ename)
                    result = {"ok": False, "error": self._map_error(exc), "user": None}
            with self._lock:
                if self._session is session:
                    session.result = result
            return result

        try:
            future = asyncio.run_coroutine_threadsafe(_wait(), engine.loop)
            session.task = future
        except Exception as exc:
            self.cancel()
            with contextlib.suppress(Exception):
                engine.submit(client.disconnect())
            return {"ok": False, "error": self._map_error(exc)}

        lease_client(client)
        return {
            "ok": True,
            "stage": "qr",
            "svg": svg,
            "expires_in": max(0, int(session.expires - time.time())),
        }

    @staticmethod
    def _map_error(exc: BaseException) -> str:
        name = type(exc).__name__
        if name in ("TimeoutError", "asyncio.TimeoutError"):
            return "二维码已过期，请重新获取"
        if name == "AuthKeyDuplicatedError":
            return "登录会话被其它程序占用，请先关闭另一个实例后重试"
        if "Cancelled" in name or "Invalid" in name:
            return "二维码已取消或失效，请重新获取"
        return f"二维码登录失败（{name}）"
