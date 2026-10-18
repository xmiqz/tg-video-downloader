# -*- coding: utf-8 -*-
"""TG 视频下载器 —— 现代图形界面（宿主适配层架构，协议见 host.py）。

架构说明：核心下载/扫描/任务引擎完全复用 tg_video_dl.py，本文件只做一层
HTTP/JSON-RPC 桥接，把引擎的异步能力暴露给 web/ 下的界面。引擎的 CLI 入口
与本 GUI 互不影响。窗口/定时器/UI 线程能力全部来自 host.HostApp 抽象，
具体宿主（迁移期旧窗口栈 / v2.0 原生 WebView2 / 后续 NAS、Android）
对本文件透明。
"""
import asyncio
import base64
import ctypes
import io
import json
import logging
import os
import pathlib
import queue
import subprocess
import sys
import threading
import time
import uuid

import host

import tg_video_dl as core
from telethon import TelegramClient
from telethon.errors import (
    SessionPasswordNeededError,
    PhoneCodeInvalidError,
    PhoneCodeExpiredError,
    PasswordHashInvalidError,
)


def resource_dir():
    """web 资源目录：打包后从解包目录取，开发时取脚本所在 web/"""
    if getattr(sys, "frozen", False):
        base = pathlib.Path(getattr(sys, "_MEIPASS", ""))
        p = base / "web"
        if p.exists():
            return str(p)
    return str(pathlib.Path(__file__).resolve().parent / "web")


class EngineLoop:
    """在独立线程跑一个 asyncio 事件循环，供桥接方法提交协程。"""

    def __init__(self):
        self.loop = None
        self.ready = threading.Event()

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()
        self.ready.wait()

    def _run(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.ready.set()
        self.loop.run_forever()

    def submit(self, coro):
        fut = asyncio.run_coroutine_threadsafe(coro, self.loop)
        return fut.result()


# ============================================================
# T45 作业模型（JobRegistry）：进程内内存作业表，不持久化。
#
# 契约（钉死，下游任务 T46+ 不得改）：
#   job 字段集 {job_id, kind, state, phase, phase_detail, done, total,
#     bytes_done, bytes_total, error, created_ts, updated_ts}；
#   state 枚举 queued|running|done|error|cancelled，终态不可变；
#   job_update 事件 payload 即完整 job 对象（根即 job，不再包 data）；
#   节流：state 突变必发；running 常态更新间隔 >=0.30s，尾帧 <=0.60s 必达。
# 线程安全：EngineLoop 线程、uvicorn to_thread 工作线程、作业 daemon
# 线程都会访问，内部一把锁保护全部状态。
# ============================================================
_job_log = logging.getLogger("gui.jobs")

# T48 search_items WS 节流阈值（gui 层钉死；JobRegistry 的 0.30s/0.60s
# job_update 节流契约零改动）：
#   首帧：累计 ≥40 条 或 作业启动后 ≥2.0s 且至少 1 条，先到即发；
#   追加帧：距上一帧 ≥0.5s 或 自上帧新增 ≥40 条。
SEARCH_WS_FIRST_ITEMS = 40
SEARCH_WS_FIRST_SECS = 2.0
SEARCH_WS_INTERVAL = 0.5
SEARCH_WS_DELTA_ITEMS = 40


class JobRegistry:
    """线程安全的作业注册表：创建/更新/取消/快照，并按节流规则外发 job_update。"""

    # T57 取消双根因 A：新增中间态 cancelling。取消请求只置
    # cancel_event + 流转 cancelling（非终态），真终态 cancelled/done/
    # error 一律由作业 worker 真实收尾 update 驱动；TERMINAL 集合
    # （done/error/cancelled，终态不可变）不动。
    STATES = frozenset({"queued", "running", "done", "error",
                        "cancelled", "cancelling"})
    TERMINAL = frozenset({"done", "error", "cancelled"})
    # 合法流转：queued->running->终态、queued/running->（cancelled|
    # cancelling）、cancelling->{done,error,cancelled}（取消与完成竞态时
    # 允许诚实显 done）；其余拒绝
    _TRANSITIONS = {
        "queued": frozenset({"running", "cancelled", "cancelling"}),
        "running": frozenset({"done", "error", "cancelled",
                               "cancelling"}),
        "cancelling": frozenset({"done", "error", "cancelled"}),
        "done": frozenset(),
        "error": frozenset(),
        "cancelled": frozenset(),
    }
    # update() 允许变更的字段（其余字段一律拒绝合并）
    _MUTABLE = frozenset({
        "state", "phase", "phase_detail", "done", "total",
        "bytes_done", "bytes_total", "error",
    })
    THROTTLE_SECS = 0.30      # running 常态更新最小发送间隔

    def __init__(self, sink=None):
        # sink(name:str, params:list) -> None；None 时推送为 no-op。
        # sink 允许延迟绑定（闭包运行时读取），适配“先建 Api、后注入 push_event”
        # 的启动顺序。
        self._sink = sink
        self._lock = threading.Lock()
        self._jobs = {}        # job_id -> job dict（12 字段齐全）
        self._events = {}      # job_id -> threading.Event（取消信号）
        self._cancelable = {}  # job_id -> bool
        self._last_emit = {}   # job_id -> float（上次 job_update 发送时刻）
        self._timers = {}      # job_id -> threading.Timer（节流尾帧）
        self._emit_errors = 0  # sink 异常计数（绝不向上抛）

    # ---------- 创建 / 更新 / 取消 ----------
    def create(self, kind, cancelable=True):
        """新建 queued 作业并入表，立即强制外发首帧 job_update。
        返回 (job_id, cancel_event)。"""
        job_id = "j_" + uuid.uuid4().hex[:16]
        now = time.time()
        job = {
            "job_id": job_id,
            "kind": str(kind),
            "state": "queued",
            "phase": "",
            "phase_detail": "",
            "done": 0,
            "total": 0,
            "bytes_done": 0,
            "bytes_total": 0,
            "error": "",
            "created_ts": now,
            "updated_ts": now,
        }
        ev = threading.Event()
        with self._lock:
            self._jobs[job_id] = job
            self._events[job_id] = ev
            self._cancelable[job_id] = bool(cancelable)
            self._last_emit[job_id] = now
            payload = dict(job)
        self._emit(payload)
        return job_id, ev

    def update(self, job_id, force=False, **fields):
        """合并允许变更的字段并按节流规则外发。
        未知 job / 终态 job / 非法流转：静默忽略并返回 False，不抛异常。"""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return False
            if job["state"] in self.TERMINAL:
                return False   # 终态不可变
            bad = [k for k in fields if k not in self._MUTABLE]
            if bad:
                _job_log.warning("job %s update 忽略非法字段：%s", job_id, bad)
            new_state = fields.get("state")
            if new_state is not None:
                if new_state not in self.STATES:
                    _job_log.warning("job %s 未知 state：%r", job_id, new_state)
                    return False
                if (new_state != job["state"]
                        and new_state not in self._TRANSITIONS[job["state"]]):
                    _job_log.warning(
                        "job %s 非法流转 %s -> %s，已忽略",
                        job_id, job["state"], new_state)
                    return False
            old_state = job["state"]
            for k, v in fields.items():
                if k in self._MUTABLE:
                    job[k] = v
            # updated_ts 强制严格递增：worker 末 tick 与终态 update 可能仅隔
            # 数微秒，float 墙钟在 epoch 量级精度 ~0.5μs，两帧可能同值——若
            # 相等，前端按契约“同等更新不重写”会把终态帧丢掉。单调递增
            # 保证每个突变帧都可被 updated_ts 区分（最多领先真实墙钟 1ms）。
            now = time.time()
            if now <= job["updated_ts"]:
                now = job["updated_ts"] + 0.001
            job["updated_ts"] = now
            state_changed = (new_state is not None and new_state != old_state)
            payload = None
            if state_changed or force:
                # 突变必发；终态/新帧直发时取消待发尾帧
                self._cancel_tail_locked(job_id)
                self._last_emit[job_id] = now
                payload = dict(job)
            else:
                elapsed = now - self._last_emit.get(job_id, 0.0)
                if elapsed >= self.THROTTLE_SECS:
                    self._cancel_tail_locked(job_id)
                    self._last_emit[job_id] = now
                    payload = dict(job)
                else:
                    # 节流：安排尾帧，距上次发送满节流窗口即发最新值
                    self._schedule_tail_locked(job_id,
                                               self.THROTTLE_SECS - elapsed)
        if payload is not None:
            self._emit(payload)
        return True

    def request_cancel(self, job_id):
        """返回 "ok" | "unsupported" | "not_found"。
        取消请求受理（T57）：置 cancel_event、改中间态 cancelling 并
        立即强制外发——不再提前钉终态 cancelled；真终态只由作业
        worker 真实收尾 update 驱动（download/aborted→cancelled、
        search/scan/t45_demo 收尾点收口）。cancelling 重复请求幂等
        （重发一帧，状态不变）。"""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return "not_found"
            if (not self._cancelable.get(job_id, False)
                    or job["state"] in self.TERMINAL):
                return "unsupported"
            ev = self._events.get(job_id)
            if ev is not None:
                ev.set()
            job["state"] = "cancelling"
            now = time.time()
            if now <= job["updated_ts"]:
                now = job["updated_ts"] + 0.001   # 同 update：严格单调递增
            job["updated_ts"] = now
            self._cancel_tail_locked(job_id)
            self._last_emit[job_id] = now
            payload = dict(job)
        self._emit(payload)
        return "ok"

    # ---------- 快照 ----------
    def snapshot(self, job_id):
        """单作业完整字段 dict 拷贝；未知 id 返回 None。"""
        with self._lock:
            job = self._jobs.get(job_id)
            return dict(job) if job is not None else None

    def snapshots(self):
        """全部作业的全量快照列表（重连/刷新用，顺序稳定）。"""
        with self._lock:
            return [dict(j) for j in self._jobs.values()]

    # ---------- 内部：节流尾帧 / 事件外发 ----------
    def _cancel_tail_locked(self, job_id):
        t = self._timers.pop(job_id, None)
        if t is not None:
            t.cancel()

    def _schedule_tail_locked(self, job_id, delay):
        self._cancel_tail_locked(job_id)
        t = threading.Timer(max(0.0, delay), self._tail_fire, args=(job_id,))
        t.daemon = True
        self._timers[job_id] = t
        t.start()

    def _tail_fire(self, job_id):
        # 尾帧：节流窗口期满后发送最新值；终态已直发则丢弃。
        # 竞态保护：距上次发送不足节流窗口时重排而不是早发。
        with self._lock:
            self._timers.pop(job_id, None)
            job = self._jobs.get(job_id)
            if job is None or job["state"] in self.TERMINAL:
                return
            elapsed = time.time() - self._last_emit.get(job_id, 0.0)
            if elapsed < self.THROTTLE_SECS:
                self._schedule_tail_locked(job_id,
                                           self.THROTTLE_SECS - elapsed)
                return
            self._last_emit[job_id] = time.time()
            payload = dict(job)
        self._emit(payload)

    def _emit(self, payload):
        """统一外发 job_update：params=[job dict 拷贝]；sink 异常全部吞掉。"""
        sink = self._sink
        if sink is None:
            return
        try:
            sink("job_update", [payload])
        except Exception:  # noqa: BLE001 - sink 异常绝不杀死作业线程
            self._emit_errors += 1
            _job_log.warning("job_update 外发失败（累计 %d 次）",
                             self._emit_errors, exc_info=True)


class Api:
    def __init__(self, engine):
        self.engine = engine
        self.client = None
        self.cfg = {}
        # 游客（离线受限）模式：纯内存态，严禁写盘；重启进程必然复位为 False
        self.guest = False
        self._preview = {}      # mid -> record（含活动消息对象）
        # T48 搜索作业存储：job_id -> {(src, mid): record}。worker 线程写、
        # start_download 读——并发由 _preview_lock 保护（帧边界小锁）。
        # 作业 cancelled 后已到达部分仍可用其建任务（任务书 3.2）。
        self._search_previews = {}
        self._preview_lock = threading.Lock()
        # 宿主适配（main 启动时注入；离线/假宿主测试时可为 None）
        self.host = None
        # 进程内 HTTP/WS 连接信息（main 启动时从 server_app 句柄注入）
        self.base_url = ""
        self.token = ""
        # 事件推送句柄（main 注入；签名 push_event(name:str, params:list)）
        self.push_event = None
        # 实时取光相关（仅 Windows 液态玻璃模式使用）
        self.capture = None
        self._drv = None
        # T45 作业注册表：sink 闭包延迟读 self.push_event（main 在 L1175 附近
        # 才注入；注入前推送为 no-op 不报错，注入后自动生效，零额外接线）
        def _job_sink(name, params):
            push = self.push_event
            if push is None:
                return
            push(name, params)
        self.jobs = JobRegistry(sink=_job_sink)
        # T49 download 作业双向映射（纯内存，_dl_lock 保护；绝不向
        # JobRegistry 12 字段加 task_id 等任何键）：
        #   _dl_jobs: job_id -> task_id；_dl_tasks: task_id -> job_id。
        # 生命周期与作业一致：open 成功登记，worker 收口 finally 清除。
        self._dl_jobs = {}
        self._dl_tasks = {}
        self._dl_lock = threading.Lock()
        # T50 move_folder 作业映射（纯内存，_move_lock 保护）：task_id ->
        # job_id。防同任务并发两个搬运作业（与内核 core.MOVING_TASKS 双
        # 保险）。生命周期与作业一致：worker 收口 finally 清除。
        self._move_tasks = {}
        self._move_lock = threading.Lock()
        # T53b-E scan_append 作业映射（纯内存，_scan_lock 保护）：
        # task_id -> job_id。防同任务并发两个追加扫描作业。生命周期与
        # 作业一致：worker finally 清除（含 error/cancelled 全路径）。
        self._scan_tasks = {}
        self._scan_lock = threading.Lock()
        # T51 账号池：唯一调度内核（选号/状态/登录参数化全在
        # core.AccountPool，本层只做参数传递与转发）。event_cb 延迟
        # 绑定 push_event（仿 jobs sink；注入前 no-op）。池侧 emit 传
        # 单 payload，这里包成 [payload]（T45 推送包络契约）。
        def _acct_sink(name, payload):
            push = self.push_event
            if push is None:
                return
            push(name, [payload])
        self._acct_sink = _acct_sink
        self._pool = core.AccountPool(event_cb=_acct_sink)
        # T58 2.1：本进程内是否已成功发出“待验证”验证码（phone_code_hash
        # 在 client 内存中）。纯内存态：不写盘、不进 JSON、不加配置键。
        # 唯一信号——connect 早退分支与 login_state 的 has_session 共用。
        self._code_sent = False

    # ---------- 外观模式 ----------
    def get_appearance(self):
        """外观：dark=静态暗（默认）/ light=静态亮 / liquid=实时透明液态玻璃。"""
        return {"ok": True, "mode": self.cfg.get("appearance", "dark")}

    def set_appearance(self, mode):
        if mode not in ("dark", "light", "liquid"):
            return {"ok": False, "error": "未知外观模式"}
        self.cfg["appearance"] = mode
        core.save_config(self.cfg)
        if sys.platform == "win32":
            try:
                self._apply_mode(mode)
            except Exception as e:
                print("[GUI] 切换外观失败：", e)
        return {"ok": True}

    def _apply_mode(self, mode):
        """把取光任务的启停 marshal 到 UI 线程（经宿主适配接口）。"""
        host_app, drv = self.host, self._drv
        if host_app is None or drv is None:
            return

        def _do():
            drv.set_mode(mode)

        host_app.ui_invoke(_do)

    # ---------- client 生命周期辅助 ----------
    def _new_client(self):
        # T56 Bug1：构造前补凭证（全新数据目录未先 connect 直接 send_code
        # 时 self.cfg 为空，此处同步补齐并落盘，消除 KeyError 风险）
        self.cfg = core.ensure_builtin_credentials()
        return TelegramClient(
            core.SESSION_PATH, self.cfg["api_id"], self.cfg["api_hash"],
            flood_sleep_threshold=120,
        )

    def _discard_client(self, client):
        """断开并丢弃一个（可能半连接的）client，任何失败都吞掉。
        T58 2.1：client 一经丢弃，其内存中的 phone_code_hash 随之消亡，
        _code_sent 复位（集中覆盖 connect/_ensure_client/send_code/
        logout_promote 全部丢弃调用点）。"""
        self._code_sent = False
        if client is None:
            return

        async def _do():
            try:
                await client.disconnect()
            except Exception:
                pass

        try:
            self.engine.submit(_do())
        except Exception:
            pass

    def _remove_session_files(self):
        for p in (core.SESSION_PATH + ".session",
                  core.SESSION_PATH + ".session-journal"):
            try:
                pathlib.Path(p).unlink()
            except Exception:
                pass

    def _ensure_client(self):
        """client 缺失时（connect() 曾失败）新建并连接。返回 None 或错误串。"""
        if self.client is not None:
            return None
        client = self._new_client()

        async def _do():
            # 连接整体限时 20s（对齐 connect() 的 wait_for 上限）：
            # 网络不通时快速失败，不让 send_code 无限挂起。
            await asyncio.wait_for(client.connect(), timeout=20.0)

        try:
            self.engine.submit(_do())
        except Exception as e:
            # 半成品 client 绝不保留（对齐 connect()/send_code 失败路径）
            self._discard_client(client)
            if type(e).__name__ in ("TimeoutError", "asyncio.TimeoutError"):
                return ("连接 Telegram 服务器超时（20 秒）。\n"
                        "请检查网络或代理后重试；若桌面版正在运行，"
                        "请先关闭它再启动。")
            return f"{type(e).__name__}: {e}"
        self.client = client
        return None

    # ---------- 退出登录 ----------
    def logout(self):
        """退出登录。T56 Bug5：有小号（cfg 已配置且 session 文件在——
        adding 半成品绝不提升）时第一次退出 = 主号 Telegram 注销 + 小号
        会话提升为主号 + 进程内重建连接（返回 stage=promoted）；无小号 =
        维持既有清光语义（stage=logged_out）。重连失败仍返回 promoted
        +warning（本地授权有效，下次启动自动恢复，绝不回滚文件）。
        busy 闸：下载 ACTIVE_SESSIONS / 目录搬运 MOVING_TASKS / 小号
        添加中 / 小号下载忙，任一非空拒绝（文案单一真源 LOGOUT_BUSY_TEXT）。"""
        if (core.any_active() or core.MOVING_TASKS
                or self._pool.alt_adding()
                or self._pool.busy_snapshot().get(core.ACCT_ALT, 0) > 0):
            return {"ok": False, "busy": True,
                    "error": core.LOGOUT_BUSY_TEXT}
        # alt 判定双条件：cfg 已配置 且 alt session 文件在（契约 Q3：
        # accounts 正常态只在 alt 登录成功后写入，"有 accounts 无文件"
        # 只能来自提升中断——由启动对账收敛，不当提升依据）
        has_alt = (core.alt_configured()
                   and pathlib.Path(
                       core.session_path_for(core.ACCT_ALT)
                       + ".session").exists())
        if has_alt:
            return self._logout_promote()
        return self._logout_plain()

    def _logout_plain(self):
        """无小号：既有清光语义（T55 登出），返回体补 stage=logged_out。"""
        client = self.client

        async def _do():
            if client is None:
                return
            try:
                await client.log_out()
            except Exception:
                # 网络异常等导致服务端注销失败，也要本地断开，
                # 否则旧连接仍占着事件循环
                try:
                    await client.disconnect()
                except Exception:
                    pass

        try:
            if client is not None:
                self.engine.submit(_do())
        except Exception:
            pass
        # T51：主号退出时断开小号运行态（不删 alt 文件/配置）
        try:
            self.engine.submit(self._pool.shutdown_alts())
        except Exception:
            pass
        self.client = None
        self._code_sent = False
        if self.guest:
            print("[GUI] guest=False（logout）")
        self.guest = False
        self._preview = {}
        # T48：同时清空搜索作业存储（内存态；作业表自身不持久化）
        self._search_previews = {}
        self._remove_session_files()
        # 清掉已保存手机号，重新登录时从输入手机号开始。
        # T56：以盘上 cfg 为准重读再写（内存 self.cfg 可能已被提升
        # 流程/其它写盘点更新，直接写回会把旧键复活）
        cfg = core.load_config()
        cfg.pop("phone", None)
        try:
            core.save_config(cfg)
        except Exception:
            pass
        self.cfg = cfg
        return {"ok": True, "stage": "logged_out"}

    def _logout_promote(self):
        """T56 Bug5：有 alt 的退出编排——主号注销（失败降级 disconnect
        → alt 最佳努力取 phone 后断开 → 主号残留文件扫尾 → alt 会话
        文件提升 → cfg 单次写 → 池清态 → 进程内重连（wait_for 20s）→
        adopt+me 缓存。返回 {ok, stage:"promoted"}（重连失败仍 promoted
        +warning，不回滚——服务端主号已注销，回滚只会得到死会话）。"""
        client = self.client
        # T58 2.1：退出/提升路径无待验证码
        self._code_sent = False

        async def _main_do():
            # 主号 Telegram 授权注销；限时 20s 防挂死，失败/超时降级
            # disconnect（沿用既有"本地照登出"语义）
            if client is None:
                return
            try:
                await asyncio.wait_for(client.log_out(), timeout=20.0)
                return
            except Exception:
                pass
            try:
                await client.disconnect()
            except Exception:
                pass

        async def _alt_do():
            # 最佳努力：惰性连上小号拿 phone（cfg 回填用），拿完优雅断开
            # （get_me 失败不阻断——授权会话不依赖 phone）
            phone = ""
            try:
                c = await asyncio.wait_for(
                    self._pool.aclient(core.ACCT_ALT), timeout=20.0)
                try:
                    me = await asyncio.wait_for(c.get_me(), timeout=10.0)
                    phone = getattr(me, "phone", "") or ""
                except Exception:
                    pass
            except Exception:
                pass
            c = self._pool.clients.get(core.ACCT_ALT)
            if c is not None:
                try:
                    await c.disconnect()
                except Exception:
                    pass
            return phone

        try:
            if client is not None:
                self.engine.submit(_main_do())
        except Exception:
            pass
        alt_phone = ""
        try:
            alt_phone = self.engine.submit(_alt_do()) or ""
        except Exception:
            alt_phone = ""
        # 主号残留会话文件扫尾（log_out 内 telethon 会自删 .session 本体；
        # -journal 等残留在此清）
        self._remove_session_files()
        # alt 会话文件提升为主号（RPC 线程同步段，毫秒级）
        # alt 会话文件提升为主号（RPC 线程同步段，毫秒级）；成功判据：
        # 主号 .session 出现在 moved（两侧同为 str(Path()) 规范化后
        # 比较，免受正反斜杠拼接差异影响）
        moved = core.promote_alt_session_files()
        if str(pathlib.Path(core.SESSION_PATH + ".session")) \
                not in moved["moved"]:
            # 提升失败（.session 改名未成，常见为被占用）：alt 文件原地
            # 保留、cfg 不动——重启时启动对账按残缺组合自愈；当前进程
            # 维持登出视图
            self.client = None
            self.guest = False
            self._preview = {}
            self._search_previews = {}
            return {"ok": False,
                    "error": "小号会话提升失败（会话文件被占用），"
                             "请重启应用后重试"}
        # cfg 单次写：pop accounts + phone 置小号（alt phone 是本轮
        # 最佳努力取的展示便利值，取不到为空——授权不依赖 phone）
        cfg = core.load_config()
        cfg.pop("accounts", None)
        cfg["phone"] = alt_phone
        try:
            core.save_config(cfg)
        except Exception:
            pass          # 写失败不回滚文件；重启由启动对账组合 B 自愈
        self.cfg = cfg
        # 池清态：旧主号 + 旧 alt 运行态全清（连接已断、文件已改）
        self._pool.shutdown_main_state()
        self._pool.reset_alt_runtime()
        # 内存态收尾（与无 alt 登出路径一致的部分——账号态留空，
        # 由下方重连成功后 re-adopt）
        self.client = None
        if self.guest:
            print("[GUI] guest=False（logout promote）")
        self.guest = False
        self._preview = {}
        self._search_previews = {}
        # 进程内重建连接（不重启进程）：SESSION_PATH 已是提升后会话
        new_client = self._new_client()
        ready = False
        try:
            async def _reconnect():
                async def _do():
                    await new_client.connect()
                    if await new_client.is_user_authorized():
                        return "ok"
                    raise RuntimeError("新主号会话未授权")
                return await asyncio.wait_for(_do(), timeout=20.0)
            ready = (self.engine.submit(_reconnect()) == "ok")
        except Exception:
            ready = False
        if ready:
            self.client = new_client
            self._adopt_and_cache_me()
            return {"ok": True, "stage": "promoted"}
        # 重连失败：client 丢弃（下次启动 connect() 自动恢复），
        # 仍返回 promoted+warning
        self._discard_client(new_client)
        return {"ok": True, "stage": "promoted",
                "warning": "小号已提升为主号；当前网络连接失败，"
                           "下次启动将自动恢复登录"}

    # ---------- 登录 ----------
    def set_guest(self, on=True):
        """进入/退出游客（离线受限）模式。

        - 纯内存态，严禁写盘（重启进程必然复位为 False）；
        - 已登录（client 非 None）时拒绝进入游客模式；
        - 立即返回（毫秒级），不触网、不碰 EngineLoop。
        """
        if not isinstance(on, bool):
            on = False
        if self.client is not None:
            print("[GUI] set_guest 拒绝：已登录账号无需进入游客模式")
            return {"ok": False, "error": "已登录账号无需进入游客模式"}
        # T51：池内有 alt 就绪/adding 时拒绝进入游客模式
        # （退出游客 on=False 不受限）
        if on and self._pool.alt_ready_or_adding():
            print("[GUI] set_guest 拒绝：已有小号登录信息")
            return {"ok": False, "error": "已有小号登录信息，请先移除小号再进入游客模式"}
        self.guest = on
        print(f"[GUI] guest={self.guest}")
        return {"ok": True, "guest": self.guest}

    def login_state(self):
        # T58 2.3：新三态口径（has_session 不再读 .session 文件）。
        # has_session 新语义=“本轮已发码待验证”，仅在 _code_sent 时为 true。
        if self.client is not None:
            if self._code_sent:
                return {"logged_in": False, "has_session": True,
                        "guest": bool(self.guest)}
            return {"logged_in": True, "guest": False}
        return {"logged_in": False, "has_session": False,
                "guest": bool(self.guest)}

    def _adopt_and_cache_me(self):
        """T51：主号就绪（connect ok / verify ready 后调用）——交池
        托管（幂等，池不断开主号）+ 缓存 get_me（失败只吞，不影响
        RPC 返回；account_list 自身绝不触网）。
        T56 Bug3：同一处 fire-and-forget 预热小号（run_coroutine_
        threadssafe 提交即返回，绝不阻塞本方法/engine.submit 等待）。"""
        self._pool.adopt_main(self.client)

        async def _do():
            u = await self.client.get_me()
            self._pool.me_cache[core.ACCT_MAIN] = {
                "name": u.first_name or "", "username": u.username or "",
                "phone": u.phone or ""}

        try:
            self.engine.submit(_do())
        except Exception:
            pass

        # T56 Bug3：小号预热（静默、失败吞掉；warm 内部自带
        # 前置判定/20s 限时/失败回落 has_session）
        async def _warm():
            try:
                await self._pool.warm_alt()
            except Exception as e:
                print(f"[acct] 小号预热异常已吞：{type(e).__name__}")

        try:
            asyncio.run_coroutine_threadsafe(_warm(), self.engine.loop)
        except Exception:
            pass

    def connect(self):
        # T58 2.2：早退分支按 _code_sent 区分——已发码待验证 → need_code；
        # 已授权就绪 → {"ok":True}（前端等同 ready）。
        if self.client is not None:
            if self._code_sent:
                return {"ok": True, "stage": "need_code"}
            return {"ok": True}
        core.ensure_dirs_and_migrate()
        # T56 Bug1：GUI 改用非交互补凭证（绝不 input；phone 缺失由
        # need_phone 步骤补），消除 windowed GUI 无控制台时的冻结风险
        self.cfg = core.ensure_builtin_credentials()
        client = self._new_client()
        # T58 2.1：新建探测 client → 本轮无待验证码
        self._code_sent = False

        async def _do():
            # 连接 + 鉴权探测整体限时：网络不通 / 同一 session 被另一实例
            # 占用导致服务端反复重置时，快速失败而不是在启动时长时间挂起。
            # T58 A1：未授权一律 need_phone——connect 全路径零发码（只有
            # 用户主动点“发送”才走 send_code）。
            async def _connect_and_check():
                await client.connect()
                if await client.is_user_authorized():
                    return "ok"
                return "need_phone"

            return await asyncio.wait_for(_connect_and_check(), timeout=20.0)

        try:
            status = self.engine.submit(_do())
        except Exception as e:
            msg = f"{type(e).__name__}: {e}"
            # 半成品 client 不能保留，否则后续 send_code 会拿到死连接/None
            self._discard_client(client)
            if type(e).__name__ == "AuthKeyDuplicatedError":
                # 会话密钥已被服务端作废（同一 session 被两处 IP 同时使用），
                # 删除会话文件，稍后由 send_code 走全新连接
                self._remove_session_files()
                msg += "\n本地登录信息已失效，请直接重新发送验证码登录。"
            if type(e).__name__ in ("TimeoutError", "asyncio.TimeoutError"):
                msg = ("连接 Telegram 服务器超时（20 秒）。\n"
                       "请检查网络或代理后重试；若桌面版正在运行，"
                       "请先关闭它再启动。")
            if "locked" in msg or "is locked" in msg:
                return {"ok": False, "busy": True,
                        "error": "会话正被其它程序占用（桌面版还在运行？），请先关闭后重试。"}
            return {"ok": False, "error": msg}
        if status == "ok":
            self.client = client
            if self.guest:
                print("[GUI] guest=False（connect 就绪）")
            self.guest = False
            self._adopt_and_cache_me()       # T51：主号就绪交池托管
            return {"ok": True, "stage": "ready"}
        # need_phone：探测 client 未授权，丢弃（不留半成品——否则
        # login_state 会误判 logged_in，且下次 connect 早退返 ready）。
        # cfg phone 仅作 phone_prefill 预填，绝不触发任何发送。
        self._discard_client(client)
        return {"ok": True, "stage": "need_phone",
                "phone_prefill": self.cfg.get("phone") or ""}

    @staticmethod
    def _is_net_exc(e):
        """网络族异常判定（Timeout/Connection/OSError 族）。按异常类名匹配
        （不新增 telethon import 依赖），_map_send_code_exc/_map_verify_exc
        与 _submit_net_retry 共用同一口径。"""
        ename = type(e).__name__
        return (ename in ("TimeoutError", "ConnectionError", "OSError")
                or "Timeout" in ename or "Connection" in ename
                or isinstance(e, (asyncio.TimeoutError, TimeoutError,
                                  ConnectionError, OSError)))

    def _submit_net_retry(self, coro_fn):
        """engine.submit 网络族异常恰重试一次（环境网络瞬时劫持自愈）；
        非网络族异常或重试仍失败原样上抛，由调用方映射。"""
        try:
            return self.engine.submit(coro_fn())
        except Exception as e:
            if not self._is_net_exc(e):
                raise
            return self.engine.submit(coro_fn())

    def _map_send_code_exc(self, e):
        """T58 2.4：send_code/account_add_start 共用的异常→返回形状映射。

        纯映射，不做任何状态清理；AuthKeyDuplicated 由调用方单独处理，
        不进本辅助。按异常类名匹配（不新增 telethon import 依赖）。"""
        ename = type(e).__name__
        if ename == "SendCodeUnavailableError":
            # 首次 SendCodeRequest 已成功（验证码已经 App 内消息送达）；
            # 失败的只是升级重发 → 由调用方按软成功解释。
            return {"__soft__": True}
        if ename == "FloodWaitError":
            secs = getattr(e, "seconds", None)
            if secs is None:
                err = "发送验证码过于频繁，请稍后再试"
            else:
                err = f"发送验证码过于频繁，请等待 {int(secs)}s 后再试"
            return {"ok": False, "code": "flood_wait",
                    "retry_after": (int(secs) if secs is not None else None),
                    "error": err}
        if ename == "PhoneNumberInvalidError":
            return {"ok": False, "code": "bad_phone",
                    "error": "手机号格式不正确，请检查国家区号与号码"}
        if ename == "PhoneNumberBannedError":
            return {"ok": False, "code": "banned",
                    "error": "该手机号已被 Telegram 封禁，无法登录"}
        if ename in ("ApiIdInvalidError", "ApiIdPublishedFloodError"):
            return {"ok": False, "code": "api_invalid",
                    "error": "内置 API 凭证失效，请更换凭证后重试"}
        if self._is_net_exc(e):
            return {"ok": False, "recoverable": True,
                    "error": "网络异常，验证码可能已发送到你的 Telegram；"
                             "如已收到可直接点下方入口输入"}
        # 其余未知异常：同样可恢复（附带原始 type:message；phone_code_hash
        # 不在时 verify 只会返回 invalid code，无害）
        return {"ok": False, "recoverable": True,
                "error": "网络异常，验证码可能已发送到你的 Telegram；"
                         "如已收到可直接点下方入口输入"
                         f"（{ename}: {e}）"}

    def send_code(self, phone):
        # connect() 失败后 self.client 可能为 None，先补建连接。
        # _ensure_client 失败（连接都没建成、无 hash）时不带 recoverable。
        err = self._ensure_client()
        if err:
            return {"ok": False, "error": err}

        async def _do():
            await self.client.send_code_request(phone)

        try:
            self._submit_net_retry(_do)   # 网络族异常恰重试一次（D1 加固）
        except Exception as e:
            ename = type(e).__name__
            if ename == "AuthKeyDuplicatedError":
                # T58 2.4：维持现行清理（discard+置空+删会话文件），
                # 且强制无 recoverable
                self._discard_client(self.client)
                self.client = None
                self._remove_session_files()
                self._code_sent = False
                return {"ok": False, "error": f"{ename}: {e}"}
            mapped = self._map_send_code_exc(e)
            if mapped.get("__soft__"):
                # 首次发送已成功，验证码已到 App；失败的只是短信升级重发
                self.cfg["phone"] = phone
                core.save_config(self.cfg)
                self._code_sent = True
                return {"ok": True, "stage": "need_code",
                        "warning": "验证码已通过 Telegram App 消息发送，请直接查看并输入最新验证码；无需重复点击发送。"}
            return mapped
        self.cfg["phone"] = phone
        core.save_config(self.cfg)
        self._code_sent = True
        return {"ok": True}

    def _map_verify_exc(self, e):
        """verify_code/verify_password 共用的异常→返回形状映射（D1 加固）。
        网络族→可恢复；PhoneCodeHash 缺失（从其他渠道直验无 hash）→引导
        重新发码且不可恢复；未知→可恢复附原始 type:message。"""
        ename = type(e).__name__
        if self._is_net_exc(e):
            return {"ok": False, "recoverable": True,
                    "error": "网络异常，请稍后重试"}
        if "PhoneCodeHash" in ename:
            return {"ok": False,
                    "error": "验证码会话已失效，请重新点击「发送验证码」获取新验证码后输入"}
        return {"ok": False, "recoverable": True,
                "error": f"登录异常，请稍后重试（{ename}: {e}）"}

    def verify_code(self, code):
        async def _do():
            try:
                await self.client.sign_in(self.cfg.get("phone"), code)
                return "ready"
            except SessionPasswordNeededError:
                return "need_password"
            except (PhoneCodeInvalidError, PhoneCodeExpiredError) as e:
                return f"__err__{type(e).__name__}: {e}"
        try:
            r = self.engine.submit(_do())
        except Exception as e:
            # D1 加固：网络异常不再裸抛（"验证码登录也报错"根因），
            # 统一映射为 UI 可消费的返回形状
            return self._map_verify_exc(e)
        if r.startswith("__err__"):
            # PhoneCodeInvalid/Expired：仍在 code 步，_code_sent 保持 True
            return {"ok": False, "error": r[len("__err__"):]}
        if r in ("ready", "need_password"):
            # T58 2.5：代码已核验（need_password 时代码也已通过）→ 清标志
            self._code_sent = False
        if r == "ready" and self.guest:
            print("[GUI] guest=False（verify_code 就绪）")
            self.guest = False
        if r == "ready":
            self._adopt_and_cache_me()       # T51：主号就绪交池托管
        return {"ok": True, "stage": r}

    def verify_password(self, password):
        async def _do():
            try:
                await self.client.sign_in(password=password)
                return "ready"
            except PasswordHashInvalidError as e:
                return f"__err__{e}"
        try:
            r = self.engine.submit(_do())
        except Exception as e:
            # D1 加固：同 verify_code，网络异常统一映射不裸抛
            return self._map_verify_exc(e)
        if r.startswith("__err__"):
            return {"ok": False, "error": r[len("__err__"):]}
        if r == "ready" and self.guest:
            print("[GUI] guest=False（verify_password 就绪）")
            self.guest = False
        if r == "ready":
            self._adopt_and_cache_me()       # T51：主号就绪交池托管
        return {"ok": True, "stage": r}

    def me(self):
        if self.client is None:
            print("[GUI] need_login: me")
            return {"ok": False, "need_login": True}
        async def _do():
            u = await self.client.get_me()
            return {"name": (u.first_name or ""), "username": u.username or "",
                    "phone": u.phone or ""}
        try:
            r = self.engine.submit(_do())
        except Exception as e:
            return {"ok": False, "error": str(e)}
        # T58 2.6：成功同步回填主号 me 缓存（account_list 只读该缓存）
        self._pool.me_cache[core.ACCT_MAIN] = {
            "name": r["name"], "username": r["username"],
            "phone": r["phone"]}
        return {"ok": True, **r}

    # ---------- T51 账号池 RPC（5 个；除 account_list 外主号就绪+非游客闸门） ----------

    def _acct_gate(self):
        """账号管理闸门：游客态（用户明确进入）优先返回游客拒绝；
        主号未就绪 -> need_login。返回拒绝 dict 或 None（通过）。"""
        if self.guest:
            return {"ok": False, "error": "游客模式下不可管理账号"}
        if self.client is None:
            print("[GUI] need_login: account rpc")
            return {"ok": False, "need_login": True}
        return None

    def account_list(self):
        """账号列表快照（不触网）：main 恒在列；alt1 仅在已配置或
        adding 中时在列。main phone 补 cfg；me 取登录成功缓存。"""
        rows = []
        for a in self._pool.snapshot():
            a = dict(a)
            if a["id"] == core.ACCT_MAIN:
                a["phone"] = (self.cfg.get("phone") or "")
            rows.append(a)
        return {"ok": True, "guest": bool(self.guest), "accounts": rows}

    def account_add_start(self, account_id, phone):
        gate = self._acct_gate()
        if gate is not None:
            return gate
        if account_id != core.ACCT_ALT:
            return {"ok": False, "error": "仅支持添加小号（alt1）"}
        phone = (phone or "").strip()
        if not phone:
            return {"ok": False, "error": "手机号不能为空"}
        # T58 2.7 重入回收：上次向导取消/返回留下的半成品（前端"取消"
        # 不调后端，池 _adding 残留会让下次添加永远报"正在添加中"）——
        # 先丢弃旧半成品（断旧 client），再走正常 add_start。"正在添加中"
        # 只留作下方防御性兜底，正常重入不透传给用户。
        if self._pool.alt_adding():
            self.engine.submit(self._pool._cleanup_add())
        try:
            stage = self.engine.submit(
                self._pool.add_start(account_id, phone))
        except Exception as e:
            # 池域逻辑错误（已存在/正在添加中）优先按原语义返回，不走
            # 网络异常映射（避免把池错误误判为可恢复）
            if "已存在" in str(e):
                return {"ok": False, "error": "小号已存在"}
            if "正在添加中" in str(e):
                return {"ok": False, "error": f"{type(e).__name__}: {e}"}
            # T58 2.7：与 send_code 同一辅助映射（软成功/网络族/未知…）
            mapped = self._map_send_code_exc(e)
            if mapped.get("__soft__"):
                return {"ok": True, "stage": "need_code",
                        "warning": "验证码已通过 Telegram App 消息发送，请直接查看并输入最新验证码；无需重复点击发送。"}
            return mapped
        return {"ok": True, "stage": stage}

    def account_verify_code(self, account_id, code):
        gate = self._acct_gate()
        if gate is not None:
            return gate
        if account_id != core.ACCT_ALT:
            return {"ok": False, "error": "仅支持小号（alt1）"}
        try:
            stage = self.engine.submit(
                self._pool.add_verify_code(account_id, code))
        except Exception as e:
            # 与 verify_code 同形：PhoneCodeInvalid/Expired 等原样透传
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
        return {"ok": True, "stage": stage}

    def account_verify_password(self, account_id, password):
        gate = self._acct_gate()
        if gate is not None:
            return gate
        if account_id != core.ACCT_ALT:
            return {"ok": False, "error": "仅支持小号（alt1）"}
        try:
            stage = self.engine.submit(
                self._pool.add_verify_password(account_id, password))
        except Exception as e:
            # 与 verify_password 同形：PasswordHashInvalid 等原样透传
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
        return {"ok": True, "stage": stage}

    def account_remove(self, account_id):
        if account_id == core.ACCT_MAIN:
            return {"ok": False, "error": "主号请使用退出登录"}
        gate = self._acct_gate()
        if gate is not None:
            return gate
        if account_id != core.ACCT_ALT:
            return {"ok": False, "error": "仅支持小号（alt1）"}
        # 同步段先判（RPC 线程毫秒级拒绝；池内 remove_account 再判一次）
        if (core.any_active()
                or self._pool.alt_adding()
                or self._pool.busy_snapshot().get(core.ACCT_ALT, 0) > 0):
            return {"ok": False, "busy": True,
                    "error": "小号仍有下载任务，请先中止后再移除。"}
        try:
            self.engine.submit(self._pool.remove_account(account_id))
        except core.AccountBusy:
            return {"ok": False, "busy": True,
                    "error": "小号仍有下载任务，请先中止后再移除。"}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
        return {"ok": True}

    # ---------- 频道预览（高级搜索；T48 改为 300ms ACK 作业式） ----------
    def preview(self, params):
        """params: 前端组装的高级搜索参数对象。

        T48 起立即返回 {"ok": true, "job_id": ...}（ACK 前不触网），
        后台 daemon 线程消费 core.search_videos_stream 并经 WS 推
        search_items 增量帧 + job_update；done 由终帧（final:true）
        权威收敛。"""
        if self.client is None:
            print("[GUI] need_login: preview")
            return {"ok": False, "need_login": True}
        try:
            channel_in = params.get("channel", "")
            channel_name, ref_mid = core.parse_channel_ref(channel_in)
        except Exception as e:
            return {"ok": False, "error": str(e)}
        job_id, cancel_event = self.jobs.create("search")
        threading.Thread(
            target=self._search_worker,
            args=(job_id, cancel_event, channel_name, ref_mid, dict(params)),
            daemon=True, name="search-" + job_id,
        ).start()
        return {"ok": True, "job_id": job_id}

    @staticmethod
    def _search_item_of(r):
        """record → 前端 item 序列化：T47 6 键纯增 match_in/ref_text 2 键
        （.get 取值，缺省 "self"/None 与 record 一致），无其它形变。"""
        return {
            "mid": r["msg"].id,
            "src": r.get("src", core.SRC_CHANNEL),
            "name": r["name"],
            "size": r["size"],
            "date": (f"{r['msg'].date:%Y-%m-%d}" if r["msg"].date else ""),
            "text": r["text"],
            "match_in": r.get("match_in", "self"),
            "ref_text": r.get("ref_text"),
        }

    def _search_push(self, job_id, data):
        """经既有线程安全 hub 扇出一帧 search_items（包络钉死：
        params[0] = {"job_id", "data": {...}}）。"""
        push = self.push_event
        if push is not None:
            push("search_items", [{"job_id": job_id, "data": data}])

    def _search_worker(self, job_id, cancel_event, channel_name, ref_mid,
                       params):
        """搜索作业 daemon 线程（仿 t45_demo 范式）：协程提交到
        engine.loop 消费 stream；任何异常兜底为作业 error，绝不带崩进程
        （cancelled 终态已由 request_cancel 原子置位并直发，registry 对
        终态 update 静默拒绝，兜底只对未终态生效）。"""
        try:
            fut = asyncio.run_coroutine_threadsafe(
                self._search_coro(job_id, cancel_event, channel_name,
                                  ref_mid, params),
                self.engine.loop)
            fut.result()
        except Exception as e:  # noqa: BLE001 - worker 线程兜底
            _job_log.warning("search worker 异常 job=%s", job_id,
                              exc_info=True)
            self.jobs.update(job_id, state="error",
                             error=f"{type(e).__name__}: {e}")

    async def _search_coro(self, job_id, cancel_event, channel_name,
                           ref_mid, params):
        """搜索作业协程（跑在 engine.loop）：消费 stream、按节流推
        search_items、终帧收敛，然后置 done。作业存储每帧并入
        _search_previews（cancelled 后已到达部分仍可建任务）。"""
        p = dict(params)
        p.pop("channel", None)
        job0 = self.jobs.snapshot(job_id) or {}
        t0 = float(job0.get("created_ts", time.time()))
        items_seq = []       # 已到达序列化 item（按到达序，键唯一）
        pushed_n = 0         # 已 WS 下发条数（items_seq 前缀长度）
        last_push = 0.0
        first_sent = False

        def _absorb(recs):
            """items 帧落地：并入作业存储与遗留全局映射（增量刷新，
            锁只包临界区，内存操作极短）。"""
            fresh = []
            with self._preview_lock:
                store = self._search_previews.setdefault(job_id, {})
                for r in recs:
                    key = (r.get("src", core.SRC_CHANNEL), r["msg"].id)
                    if key in store:
                        continue
                    store[key] = r
                    self._preview[key] = r
                    fresh.append(r)
            for r in fresh:
                items_seq.append(self._search_item_of(r))
            return len(fresh)

        def _maybe_push():
            """WS 节流：首帧（≥40 条或启动 ≥2.0s 且 ≥1 条）先到即发；
            其后追加帧满足距上一帧 ≥0.5s 或新增 ≥40 条。"""
            nonlocal pushed_n, last_push, first_sent
            n = len(items_seq)
            if n <= pushed_n:
                return
            now = time.time()
            if not first_sent:
                if ((n - pushed_n) >= SEARCH_WS_FIRST_ITEMS
                        or (now - t0 >= SEARCH_WS_FIRST_SECS)):
                    self._search_push(
                        job_id, {"items": items_seq[:n]})
                    pushed_n, last_push, first_sent = n, now, True
                return
            if ((now - last_push) >= SEARCH_WS_INTERVAL
                    or (n - pushed_n) >= SEARCH_WS_DELTA_ITEMS):
                self._search_push(job_id, {"items": items_seq[pushed_n:n]})
                pushed_n, last_push = n, now

        self.jobs.update(job_id, state="running", phase="server_search",
                         phase_detail="搜索作业启动", total=0)
        entity = await self.client.get_entity(channel_name)
        async for ev in core.search_videos_stream(self.client, entity, p,
                                                  cancel=cancel_event):
            kind = ev["kind"]
            if kind == "progress":
                detail = (f"已扫描 {ev['scanned']} 条"
                          f" · 已匹配 {ev['matched']} 条")
                if ev.get("earliest"):
                    detail += f" · 最早 {ev['earliest']}"
                self.jobs.update(job_id, phase=ev["phase"],
                                 phase_detail=detail,
                                 done=len(items_seq), total=0)
            elif kind == "items":
                _absorb(ev["items"])
                _maybe_push()
            elif kind == "done":
                if cancel_event.is_set():
                    # T57：取消竞态收口 cancelling→cancelled（真终态由
                    # 本收尾驱动，不再依赖 request_cancel 提前钉死）
                    self.jobs.update(job_id, state="cancelled")
                    return
                records, meta = ev["records"], ev["meta"]
                title = getattr(entity, "title", None) or channel_name
                # gui 层附加键（与 permalink 同性质，内核 meta 不产）：
                # 旧 preview 响应承载的 title 改经终帧 meta 下发
                meta["title"] = title
                # permalink（T47 原样保留）：链接里带具体消息 id 时精准
                # 补抓并插入首行，不受扫描上限与体积/日期筛选限制
                if ref_mid is not None:
                    have = {(r.get("src", core.SRC_CHANNEL), r["msg"].id)
                            for r in records}
                    if (core.SRC_CHANNEL, ref_mid) not in have:
                        try:
                            pm = await self.client.get_messages(
                                entity, ids=ref_mid)
                        except Exception:
                            pm = None
                        if pm is not None and core.is_video_message(pm):
                            records.insert(
                                0, core.make_record(pm, core.SRC_CHANNEL))
                            meta["permalink"] = True
                # 终态存储：final 全量（含 permalink 首行）替换两个映射
                final_store = {
                    (r.get("src", core.SRC_CHANNEL), r["msg"].id): r
                    for r in records}
                final_items = [self._search_item_of(r) for r in records]
                with self._preview_lock:
                    self._search_previews[job_id] = final_store
                    self._preview = dict(final_store)
                # 终帧必须先于 done 突变的 job_update（同一协程内顺序调用）
                self._search_push(
                    job_id, {"items": final_items, "meta": meta,
                             "final": True})
                self.jobs.update(job_id, state="done", phase="filter",
                                 phase_detail=f"共 {len(records)} 条",
                                 done=len(records), total=len(records))
                return
        # T57：搜索流被取消时内核生成器直接 return（不发 done），
        # async for 提前结束——作业此时停在 cancelling，必须由本侧
        # 收口真终态 cancelled，否则卡片永挂取消中
        if cancel_event.is_set():
            self.jobs.update(job_id, state="cancelled")

    def preview_cancel(self, job_id):
        """取消搜索作业（T48 新 RPC）。返回体与 job_cancel 同构：
        成功 {"ok":true}；未知 id job_not_found；不可取消/已终态 unsupported。

        游客态豁免论证（不设 need_login 闸门）：本方法只读写进程内
        JobRegistry 内存（request_cancel 置位取消令牌 + 改 state），
        不触网、不碰磁盘、不访问 client、幂等；作业表不区分登录态，
        游客态调用与登录态完全同构，且取消一个不存在的或已终态的
        作业只会得到 job_not_found/unsupported，无任何副作用。"""
        r = self.jobs.request_cancel(job_id)
        if r == "ok":
            return {"ok": True}
        if r == "unsupported":
            return {"ok": False, "unsupported": True}
        return {"ok": False, "error": "job_not_found"}

    # ---------- T49 download 作业（kind="download"，占位-实例 1:1） ----------

    def _make_dl_phase_cb(self, task_id):
        """构造 download 作业的 phase_cb（6 参契约 (task_id, phase,
        detail, index, total, snap)，跑在 engine.loop 线程；闭包状态只被
        该线程触达）。job_update 计数映射：fetching 用 fetched/fetch_total，
        终态与 downloading 用 task_finished/task_total，其余阶段不动
        done/total（flood 倒计时秒不进计数）；节流交给 JobRegistry 既有
        契约。dl_phase 另算节拍（Api 内每 task 记 last）：阶段突变立即推、
        flood_wait 每秒必达、其余 ≥0.4s；push_event 为 None 时 no-op；
        回调异常禁上抛（渲染失败不影响下载）。"""
        st = {"phase": None, "ts": 0.0}

        def cb(tid, phase, detail, index, total, snap):
            try:
                with self._dl_lock:
                    job_id = self._dl_tasks.get(tid)
                if job_id is None:
                    return
                fields = {"phase": phase, "phase_detail": detail}
                if phase == "fetching":
                    fields["done"] = snap.get("fetched", 0)
                    fields["total"] = snap.get("fetch_total", 0)
                elif phase in ("downloading", "done", "aborted", "error"):
                    fields["done"] = snap.get("task_finished", 0)
                    fields["total"] = snap.get("task_total", 0)
                self.jobs.update(job_id, **fields)
                now = time.monotonic()
                if (phase != st["phase"] or phase == "flood_wait"
                        or now - st["ts"] >= 0.4):
                    st["phase"] = phase
                    st["ts"] = now
                    push = self.push_event
                    if push is not None:
                        push("dl_phase", [{
                            "task_id": tid,
                            "phase": phase,
                            "phase_detail": detail,
                            "phase_index": int(index or 0),
                            "phase_total": int(total or 0),
                        }])
            except Exception:  # noqa: BLE001 - 回调异常绝不上抛
                pass

        return cb

    def _dl_worker(self, job_id, cancel_event, task_id, task, sess,
                   lazy_statuses, do_reconcile):
        """download 作业 daemon 线程（仿 _search_worker 范式）：run_
        download_session 提交到 engine.loop，按返回 status 收口
        done/cancelled/error（错误文案透传）；线程兜底 except 同样
        error（type+msg 透传，禁裸 print 吞错）；finally 清双向映射
        （取消 watcher 据此退出）。registry 对已 cancelled 终态的
        update 静默拒绝——取消竞态由 request_cancel 原子置位兜底。"""
        try:
            fut = asyncio.run_coroutine_threadsafe(
                core.run_download_session(
                    self.client, task, sess, gui={},
                    lazy_statuses=lazy_statuses,
                    do_reconcile=do_reconcile),
                self.engine.loop)
            result = fut.result()
        except Exception as e:  # noqa: BLE001 - worker 线程兜底
            _job_log.warning("download worker 异常 job=%s", job_id,
                             exc_info=True)
            result = {"status": "error",
                      "error": f"{type(e).__name__}: {e}"}
        result = result or {}
        status = result.get("status", "error")
        if status == "done":
            state, phase = "done", "done"
        elif status == "aborted":
            state, phase = "cancelled", "aborted"
        else:
            state, phase = "error", "error"
        # 终态细节优先取墓碑快照的 phase_detail（内核拼装的同源文本），
        # 过期/缺失时退化为 phase_text
        detail = ""
        ent = core.SESSION_TOMBSTONES.get(task_id)
        if isinstance(ent, tuple) and isinstance(ent[0], dict):
            detail = ent[0].get("phase_detail", "")
        if not detail:
            detail = core.phase_text(phase)
        err = result.get("error", "") if status == "error" else ""
        self.jobs.update(job_id, state=state, phase=phase,
                         phase_detail=detail, error=err)
        with self._dl_lock:
            self._dl_jobs.pop(job_id, None)
            self._dl_tasks.pop(task_id, None)

    def _start_dl_cancel_watcher(self, job_id, cancel_event, task_id):
        """download 作业取消 watcher：cancel_event 置位 → 对 live 会话
        request_abort()（job_cancel 与 abort_task 等效）；映射消失（worker
        收口）后退出。search/t45_demo 不挂 watcher（T45/T48 零回归）。"""
        def _watch():
            while True:
                cancel_event.wait(0.5)
                if job_id not in self._dl_jobs:
                    return
                if cancel_event.is_set():
                    sess = core.ACTIVE_SESSIONS.get(task_id)
                    if sess is not None:
                        sess.request_abort()

        threading.Thread(target=_watch, daemon=True,
                         name="dl-cancel-" + job_id).start()

    # ---------- 建任务并下载 ----------
    def start_download(self, channel, title, keys, parallel, folder="",
                       job_id=""):
        """T48：job_id 非空 → 从 _search_previews[job_id] 取记录（作业已
        cancelled 也允许用其已到达部分建任务）；job_id 空/缺失 → 走遗留
        全局 _preview（兼容窗口，至少保留一个版本）。其余逻辑逐行不变。"""
        if self.client is None:
            print("[GUI] need_login: start_download")
            return {"ok": False, "need_login": True}
        pool_map = {}
        if job_id:
            with self._preview_lock:
                pool_map = dict(self._search_previews.get(job_id) or {})
        if not pool_map:
            # 兼容窗口：job_id 空/缺失（或作业映射缺失）走遗留全局映射
            with self._preview_lock:
                pool_map = dict(self._preview)
        todo = []
        for k in keys:
            src = k.get("src", core.SRC_CHANNEL)
            rec = pool_map.get((src, int(k["mid"])))
            if rec is not None:
                todo.append(rec)
        if not todo:
            return {"ok": False, "error": "没有选择视频"}
        if not folder:
            root = core.get_download_root(self.cfg)
        else:
            root = pathlib.Path(folder)
            root.mkdir(parents=True, exist_ok=True)
        safe = "".join(c for c in (title or channel) if c not in '\\/:*?"<>|').strip()
        folder_dest = root / (safe or "channel")
        folder_dest.mkdir(parents=True, exist_ok=True)

        task_items = [core.make_task_item(r) for r in todo]
        tasks = core.build_task_dicts(
            channel, title,
            {"parallel": max(1, min(int(parallel), 8))},
            str(folder_dest), task_items,
        )
        for t in tasks:
            core.save_task(t)
        n_split = len(tasks)

        # 只自动开始第 1 个任务；拆分后的其余任务保存在任务库，
        # 由用户按需点"继续下载"，避免同时建立过多连接
        first = tasks[0]
        first_ids = {it["msg"] for it in first["items"]}
        first_todo = [r for r in todo if r["msg"].id in first_ids]

        # T49：同步段（选记录/makedirs/save_task 先落盘）全部完成后才
        # 建 download 作业（占位-实例 1:1）——open 纯内存动作即刻注册，
        # 点"开始"后轮询立刻可见 preparing。
        try:
            planned = core.build_planned(first, first_todo, None)
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
        # T50 反向防线：目录搬运中禁开下载（拆分任务通常新 id 不会命中，
        # 属防御性检查；与 resume_task/open_download_session 同源）
        if first["id"] in core.MOVING_TASKS:
            return {"ok": False, "error": "任务目录正在移动中，暂不能开始下载"}
        job_id, cancel_event = self.jobs.create("download")
        self.jobs.update(job_id, force=True, state="running",
                         phase="preparing",
                         phase_detail=core.phase_text("preparing"),
                         total=len(first["items"]))
        cb = self._make_dl_phase_cb(first["id"])
        try:
            fut = asyncio.run_coroutine_threadsafe(
                core.open_download_session(
                    self.client, first, planned,
                    first["params"]["parallel"], gui={},
                    lazy_statuses=None, phase_cb=cb,
                    pool=self._pool, event_cb=self._acct_sink),
                self.engine.loop)
            sess = fut.result()
        except Exception as e:
            self.jobs.update(job_id, state="error", phase="error",
                             phase_detail=core.phase_text("error"),
                             error=f"{type(e).__name__}: {e}")
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
        if sess is None:
            self.jobs.update(job_id, state="error", phase="error",
                             phase_detail=core.phase_text("error"),
                             error="该任务已在下载中，请勿重复开始")
            return {"ok": False, "error": "该任务已在下载中，请勿重复开始"}
        sess.queue_source = first_todo   # 非 lazy 模式 run 入队用
        with self._dl_lock:
            self._dl_jobs[job_id] = first["id"]
            self._dl_tasks[first["id"]] = job_id
        threading.Thread(
            target=self._dl_worker,
            args=(job_id, cancel_event, first["id"], first, sess,
                  None, False),
            daemon=True, name="dl-" + job_id,
        ).start()
        self._start_dl_cancel_watcher(job_id, cancel_event, first["id"])
        return {"ok": True, "task_id": first["id"], "split": n_split,
                "job_id": job_id}

    # ---------- 实时状态 ----------
    def active_states(self):
        return core.active_states()

    def is_active(self):
        return core.any_active()

    # ---------- T45 作业模型（job_state / jobs_state / job_cancel + 测试挂点） ----------
    def job_state(self, job_id):
        """单作业快照；未知 id -> {"ok":false,"error":"job_not_found"}。"""
        job = self.jobs.snapshot(job_id)
        if job is None:
            return {"ok": False, "error": "job_not_found"}
        return {"ok": True, "job": job}

    def jobs_state(self):
        """全部作业全量快照（重连/刷新补全用）。"""
        return {"ok": True, "jobs": self.jobs.snapshots()}

    def job_cancel(self, job_id):
        """取消作业：可取消且非终态 -> ok；否则 unsupported；未知 -> not_found。"""
        r = self.jobs.request_cancel(job_id)
        if r == "ok":
            return {"ok": True}
        if r == "unsupported":
            return {"ok": False, "unsupported": True}
        return {"ok": False, "error": "job_not_found"}

    # ---------- T45 测试挂点（env 门控：TG_T45_TEST_HOOK=1 才进有效白名单） ----------
    def t45_demo_job(self, duration_ms=3000, steps=10, cancelable=True,
                     fail=False):
        """演示作业：立即建 queued job 并起 daemon 线程按 tick 推进，
        RPC 立即返回 {"ok":true,"job_id":...}（ACK<=300ms）。"""
        if (isinstance(duration_ms, bool) or not isinstance(duration_ms, int)
                or not 200 <= duration_ms <= 30000):
            return {"ok": False, "error": "duration_ms 必须为 200..30000 的整数"}
        if (isinstance(steps, bool) or not isinstance(steps, int)
                or not 1 <= steps <= 100):
            return {"ok": False, "error": "steps 必须为 1..100 的整数"}
        if not isinstance(cancelable, bool) or not isinstance(fail, bool):
            return {"ok": False, "error": "cancelable/fail 必须为布尔值"}
        job_id, cancel_event = self.jobs.create("t45_demo",
                                                cancelable=cancelable)
        threading.Thread(
            target=self._t45_demo_worker,
            args=(job_id, cancel_event, duration_ms, steps, fail),
            daemon=True, name="t45-demo-" + job_id,
        ).start()
        return {"ok": True, "job_id": job_id}

    def _t45_demo_worker(self, job_id, cancel_event, duration_ms, steps, fail):
        try:
            interval = (duration_ms / 1000.0) / steps
            self.jobs.update(job_id, state="running", phase="t45_demo",
                             phase_detail="演示作业运行中",
                             total=steps, bytes_total=steps * 1048576)
            for i in range(steps):
                time.sleep(interval)
                if cancel_event.is_set():
                    # T57：取消收尾（cancelling→cancelled，真终态由
                    # 本收点驱动；request_cancel 不再提前钉终态；取消帧
                    # phase/phase_detail 保持原值，与旧直发帧内容一致）
                    self.jobs.update(job_id, state="cancelled")
                    return
                done_n = i + 1
                if fail and i == steps - 1:
                    self.jobs.update(
                        job_id, done=done_n, bytes_done=done_n * 1048576,
                        state="error", phase="t45_demo",
                        phase_detail="模拟失败",
                        error="t45_demo deliberate failure")
                    return
                self.jobs.update(job_id, done=done_n,
                                 bytes_done=done_n * 1048576)
            self.jobs.update(job_id, state="done", phase="t45_demo",
                             phase_detail="演示作业完成")
        except Exception:  # noqa: BLE001 - 测试挂点线程绝不允许带崩进程
            _job_log.warning("t45_demo worker 异常", exc_info=True)

    def get_item_text(self, task_id, src, mid):
        """按需取单条消息原文（轮询快照为省流量不再携带 text）。"""
        mid = int(mid)
        # 活动会话：直接从监控条目取，含最新文本
        sess = core.ACTIVE_SESSIONS.get(task_id)
        if sess is not None:
            e = sess.mon.order.get((src, mid))
            if e is not None:
                return {"ok": True, "text": e.get("text", "")}
        t = self._load_one(task_id)
        if t is None:
            return {"ok": False, "error": "任务不存在"}
        for ti in t["items"]:
            if ti["msg"] == mid and ti.get("src", core.SRC_CHANNEL) == src:
                return {"ok": True, "text": ti.get("text", "")}
        return {"ok": False, "error": "未找到该条目"}

    def abort_task(self, task_id):
        sess = core.ACTIVE_SESSIONS.get(task_id)
        if sess is None:
            return {"ok": False, "error": "该任务当前没有下载会话"}
        sess.request_abort()
        return {"ok": True}

    def item_action(self, task_id, action, keys):
        """单项/批量操作。action: pause/resume/skip/remove。
        任务下载中：走会话控制（暂停释放槽位、恢复优先调度）；
        非下载中：remove 直接删条目，skip/resume 直接改持久状态。"""
        ks = [(k.get("src", core.SRC_CHANNEL), int(k["mid"]))
              for k in keys]
        sess = core.ACTIVE_SESSIONS.get(task_id)
        if sess is not None:
            sess.request(action, ks)
            return {"ok": True}
        t = self._load_one(task_id)
        if t is None:
            return {"ok": False, "error": "任务不存在"}
        if action == "remove":
            return {"ok": True, "removed": core.remove_items(t, ks)}
        changed = 0
        target = "skipped" if action == "skip" else "pending"
        for k in ks:
            if core.set_item_status(t, k, target):
                changed += 1
        return {"ok": True, "changed": changed}

    # ---------- 任务历史 ----------
    def list_tasks(self):
        out = []
        for t in core.load_tasks():
            c = core.task_counts(t)
            out.append({
                "id": t["id"], "channel_title": t.get("channel_title", ""),
                "channel": t.get("channel", ""), "created": t.get("created", ""),
                "finished": c["finished"], "total": c["total"],
                "pending": c["pending"], "failed": c["failed"],
                "skipped": c["skipped"],
                # T53b（T52 回显）：账号偏好——落盘 params.acct_pref
                # 为 main/altN；auto = 键不存在（edit_task 写入语义）。
                # 只读装配，任务 JSON 零写盘；前端 noteTaskAcct 消费
                # 的键名是 acct（app.js t.acct），非 acct_pref。
                "acct": ((t.get("params") or {}).get("acct_pref")) or "auto",
            })
        return out

    def get_task(self, task_id):
        # T53b：按 id 直读单个任务（core.load_task 单文件读），不再
        # 全目录解析——滑层 2s 轮询的每次拍打都是这条热路径（与
        # resume_task 的 T49 单读语义一致；坏文件/不存在/穿越 id
        # 均返"任务不存在"，与旧全扫过滤语义等价）。
        t = core.load_task(task_id)
        if t is None or t.get("id") != task_id:
            return {"ok": False, "error": "任务不存在"}
        return {"ok": True, "task": t,
                "counts": core.task_counts(t)}

    def delete_task(self, task_id):
        for t in core.load_tasks():
            if t["id"] == task_id:
                core.delete_task(t)
                return {"ok": True}
        return {"ok": False, "error": "任务不存在"}

    def resume_task(self, task_id, only_failed=False, only_skipped=False):
        """T57 C8：新增 only_skipped（与 only_failed 同构、互斥、bool
        校验）——True 时仅恢复 skipped 条目（"继续下载"仍负责
        pending/failed）。默认 False 行为与旧版逐字节一致；任务
        JSON 零新增字段，白名单 45 零改（kwargs/位置传参同通路）。"""
        # 入参硬校验（仿 t45_demo_job：类型先行，互斥同真返回参数错误）
        if (not isinstance(only_failed, bool)
                or not isinstance(only_skipped, bool)):
            return {"ok": False,
                    "error": "only_failed/only_skipped 必须为布尔值"}
        if only_failed and only_skipped:
            return {"ok": False,
                    "error": "only_failed 与 only_skipped 不能同时开启"}
        if self.client is None:
            print("[GUI] need_login: resume_task")
            return {"ok": False, "need_login": True}
        # T49：按 id 直读单个任务（core.load_task），不再 load_tasks()
        # 全目录解析——多任务目录下那是 preparing 占位 ≤200ms 的主要
        # 耗时（Defender 扫描下实测 p50 117ms）；语义与逐条过滤一致。
        t = core.load_task(task_id)
        if t is None or t.get("id") != task_id:
            return {"ok": False, "error": "任务不存在"}
        if task_id in core.ACTIVE_SESSIONS:
            return {"ok": False, "error": "该任务已在下载中，请勿重复开始"}
        # T50 反向防线：目录搬运中禁开下载（补 worker 已建作业、会话
        # 未注册的窗口；open_download_session 内另有同源兜底）
        if task_id in core.MOVING_TASKS:
            return {"ok": False, "error": "任务目录正在移动中，暂不能开始下载"}
        # T57：only_skipped 只拉 skipped（C10：语义单一，不兼带
        # pending/failed）；pull_records 的 make 确认消息后自动重置
        # pending 清错，全链复用既有 statuses 通路，零分支新增
        if only_skipped:
            statuses = ("skipped",)
        elif only_failed:
            statuses = ("failed",)
        else:
            statuses = ("pending", "failed")
        n = sum(1 for ti in t["items"] if ti["status"] in statuses)
        if not n:
            return {"ok": False, "error": "没有需要下载的视频"}
        parallel = int(t.get("params", {}).get("parallel") or 1)

        # T49：全部同步校验之后才建 download 作业（占位-实例 1:1；
        # 游客/无条目/重复/任务不存在均不挂空作业）。open 纯内存动作
        # 即刻注册 ACTIVE_SESSIONS（preparing 立即可轮询可见）。
        try:
            planned = core.build_planned(t, [], statuses)
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
        job_id, cancel_event = self.jobs.create("download")
        self.jobs.update(job_id, force=True, state="running",
                         phase="preparing",
                         phase_detail=core.phase_text("preparing"),
                         total=n)
        cb = self._make_dl_phase_cb(task_id)
        try:
            fut = asyncio.run_coroutine_threadsafe(
                core.open_download_session(
                    self.client, t, planned, parallel, gui={},
                    lazy_statuses=statuses, phase_cb=cb,
                    pool=self._pool, event_cb=self._acct_sink),
                self.engine.loop)
            sess = fut.result()
        except Exception as e:
            self.jobs.update(job_id, state="error", phase="error",
                             phase_detail=core.phase_text("error"),
                             error=f"{type(e).__name__}: {e}")
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
        if sess is None:
            # open 重复守卫命中（同步检查与 open 之间的竞态窗口）
            self.jobs.update(job_id, state="error", phase="error",
                             phase_detail=core.phase_text("error"),
                             error="该任务已在下载中，请勿重复开始")
            return {"ok": False,
                    "error": "该任务已在下载中，请勿重复开始"}
        with self._dl_lock:
            self._dl_jobs[job_id] = task_id
            self._dl_tasks[task_id] = job_id
        # 边读边下：会话已建立，消息分批补取（大清单读取阶段有反馈）
        threading.Thread(
            target=self._dl_worker,
            args=(job_id, cancel_event, task_id, t, sess,
                  statuses, False),
            daemon=True, name="dl-" + job_id,
        ).start()
        self._start_dl_cancel_watcher(job_id, cancel_event, task_id)
        return {"ok": True, "job_id": job_id}

    # ---------- 任务编辑 ----------
    def _load_one(self, task_id):
        for t in core.load_tasks():
            if t["id"] == task_id:
                return t
        return None

    def edit_task(self, task_id, fields):
        """批量修改任务属性：title/parallel/folder（+move_files）。
        T50：目录有变化且勾选搬文件 → 建 move_folder 作业异步搬运，
        立即返回 {"ok": True, "job_id": ...}（请求线程只做 makedirs 与
        stat 级规划，不做任何文件搬运 IO）；纯路径修改仍瞬时同步。"""
        t = self._load_one(task_id)
        if t is None:
            return {"ok": False, "error": "任务不存在"}
        if fields.get("title"):
            core.rename_task(t, fields["title"])
        if fields.get("parallel"):
            core.set_parallel(t, int(fields["parallel"]))
        # T51：账号偏好（不加新 RPC；T52 经 edit_task 切换任务偏好）。
        # 仅接受 main/alt1/auto；非法值不落盘。auto 时 pop 键；live
        # 会话存在时同步就地更新 sess.task（排队条目立即生效，不动
        # 正在下载的条目）。
        pref = fields.get("acct_pref")
        if pref is not None:
            if pref not in ("main", "alt1", "auto"):
                return {"ok": False,
                        "error": "acct_pref 仅支持 main/alt1/auto"}
            params = t.setdefault("params", {})
            if pref == "auto":
                params.pop("acct_pref", None)
            else:
                params["acct_pref"] = pref
            core.save_task(t)
            sess = core.ACTIVE_SESSIONS.get(task_id)
            if sess is not None:
                sp = sess.task.setdefault("params", {})
                if pref == "auto":
                    sp.pop("acct_pref", None)
                else:
                    sp["acct_pref"] = pref
        folder = (fields.get("folder") or "").strip()
        if not folder:
            return {"ok": True, "warnings": []}
        new_norm = os.path.abspath(os.path.expanduser(folder))
        cur_norm = os.path.abspath(os.path.expanduser(
            str(t.get("folder") or "")))
        if new_norm == cur_norm:
            # 路径未变化：不写 folder、不建作业
            return {"ok": True, "warnings": []}
        # 移动目录是写操作：登录闸门（任何写盘/建作业之前）
        if self.client is None:
            print("[GUI] need_login: edit_task")
            return {"ok": False, "need_login": True}
        # 状态闸：下载中禁搬 / 同任务防重复提交
        if task_id in core.ACTIVE_SESSIONS:
            return {"ok": False, "error": "该任务正在下载中，无法移动目录"}
        with self._move_lock:
            if task_id in self._move_tasks:
                return {"ok": False,
                        "error": "目录正在移动中，请勿重复提交"}
        if bool(fields.get("move_files")):
            # 请求线程只做：建目标目录 + stat 级规划（首帧 total/
            # bytes_total 估算），不做任何阻塞搬运 IO
            try:
                os.makedirs(folder, exist_ok=True)
                plan, size_sum = core.plan_move_files(t, new_norm)
            except Exception as e:
                return {"ok": False, "error": f"{type(e).__name__}: {e}"}
            job_id, _ev = self.jobs.create("move_folder", cancelable=False)
            with self._move_lock:
                self._move_tasks[task_id] = job_id
            self.jobs.update(job_id, force=True, state="running",
                             phase="moving", phase_detail="准备移动…",
                             total=len(plan), bytes_total=size_sum,
                             done=0, bytes_done=0)
            threading.Thread(
                target=self._move_worker,
                args=(job_id, task_id, new_norm),
                daemon=True, name="move-" + job_id,
            ).start()
            # 立即返回（不带 warnings 键），搬运进度经 job_update 推送
            return {"ok": True, "job_id": job_id}
        # 纯路径（不搬文件）：瞬时同步路径（一次 save_task），行为不变
        try:
            r = core.change_folder(t, folder, move_files=False)
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
        if r.get("ok"):
            return {"ok": True, "warnings": []}
        return {"ok": False, "error": r.get("error") or "修改失败"}

    def _move_worker(self, job_id, task_id, new_path):
        """move_folder 作业 daemon 线程（T50）：重新加载最新任务对象
        （不用请求线程的旧副本），progress 闭包转 jobs.update；调
        core.change_folder（唯一搬运实现）——ok → done 终帧并记日志，
        fail → error 终帧 + 完整清单入日志；兜底 except 同样 error 终帧，
        线程绝不裸崩；finally 清 _move_tasks 映射并核对终帧已发。"""
        try:
            t = core.load_task(task_id)
            if t is None:
                self.jobs.update(job_id, force=True, state="error",
                                 phase="error", phase_detail="移动失败",
                                 error="任务不存在")
                return
            cell = {"total": 0, "bytes_total": 0}
            last = {"done": -1}

            def _cb(done, total, bytes_done, bytes_total, name):
                try:
                    cell["total"] = total
                    cell["bytes_total"] = bytes_total
                    # 钉死格式：文件进行中 "[i/n] 文件名"（i 从 1 起，
                    # 文件开始/分块帧 done==上一完成数 → i=done+1）；
                    # 完成帧 done 前进 → i=done。开工帧 name 为空。
                    if name:
                        i = done + 1 if done == last["done"] else done
                        detail = f"[{i}/{total}] {name[:80]}"
                    else:
                        detail = "准备移动…"
                    last["done"] = done
                    self.jobs.update(job_id, done=done, total=total,
                                     bytes_done=bytes_done,
                                     bytes_total=bytes_total,
                                     phase_detail=detail)
                except Exception:
                    pass

            r = core.change_folder(t, new_path, move_files=True,
                                  progress=_cb)
            if r.get("ok"):
                n = int(cell["total"] or 0)
                self.jobs.update(job_id, force=True, state="done",
                                 phase="done",
                                 phase_detail=f"已完成（共 {n} 个文件）",
                                 done=n, total=n,
                                 bytes_done=cell["bytes_total"],
                                 bytes_total=cell["bytes_total"])
                _job_log.info(
                    "move_folder 完成 job=%s task=%s moved=%d skipped=%d"
                    " moved清单=%s skipped清单=%s",
                    job_id, task_id, len(r.get("moved") or []),
                    len(r.get("skipped") or []),
                    r.get("moved"), r.get("skipped"))
            else:
                self.jobs.update(job_id, force=True, state="error",
                                 phase="error", phase_detail="移动失败",
                                 error=r.get("error", ""))
                _job_log.warning(
                    "move_folder 失败 job=%s task=%s error=%s moved=%s"
                    " pending=%s", job_id, task_id, r.get("error", ""),
                    r.get("moved"), r.get("pending"))
        except Exception as e:  # noqa: BLE001 - worker 线程兜底
            _job_log.warning("move_folder worker 异常 job=%s", job_id,
                             exc_info=True)
            self.jobs.update(job_id, force=True, state="error",
                             phase="error", phase_detail="移动失败",
                             error=f"{type(e).__name__}: {e}")
        finally:
            with self._move_lock:
                self._move_tasks.pop(task_id, None)
            snap = self.jobs.snapshot(job_id)
            if (snap is not None
                    and snap.get("state") not in ("done", "error",
                                                  "cancelled")):
                # 终帧未达兜底（正常路径不可达）：补发 error 终帧
                self.jobs.update(job_id, force=True, state="error",
                                 phase="error", phase_detail="移动失败",
                                 error="移动作业未正常收口")

    def set_item(self, task_id, src, mid, status):
        t = self._load_one(task_id)
        if t is None:
            return {"ok": False, "error": "任务不存在"}
        ok = core.set_item_status(t, (src, int(mid)), status)
        return {"ok": ok}

    def remove_items(self, task_id, keys):
        t = self._load_one(task_id)
        if t is None:
            return {"ok": False, "error": "任务不存在"}
        ks = [(k.get("src", core.SRC_CHANNEL), int(k["mid"])) for k in keys]
        sess = core.ACTIVE_SESSIONS.get(task_id)
        if sess is not None:
            # 下载中：取消正在下的条目并从清单删除，槽位随之释放
            sess.request("remove", ks)
            return {"ok": True, "removed": len(ks)}
        n = core.remove_items(t, ks)
        return {"ok": True, "removed": n}

    def scan_append(self, task_id, params):
        """T53b-E：追加扫描作业化（ACK 式，仿 preview/gui.py:758-779）。

        同步前置校验（全部在建 job 之前完成，ACK 前零触网）：
          1. 游客/未登录 -> need_login（建 job 前）
          2. core.load_task 单读（不再全目录扫描）
          3. 每任务并发占位（仿 _move_tasks）
        成功返回 {"ok":True,"job_id":...}；后台 daemon 线程消费
        search_videos_stream 并按批 append_records 落盘，经
        search_items 增量帧 + job_update 推送进度与终态。"""
        if self.client is None:
            print("[GUI] need_login: scan_append")
            return {"ok": False, "need_login": True}
        t = core.load_task(task_id)
        if t is None or t.get("id") != task_id:
            return {"ok": False, "error": "任务不存在"}
        with self._scan_lock:
            if task_id in self._scan_tasks:
                return {"ok": False,
                        "error": "该任务正在追加扫描中，请勿重复提交"}
        job_id, cancel_event = self.jobs.create("scan_append")
        with self._scan_lock:
            self._scan_tasks[task_id] = job_id
        threading.Thread(
            target=self._scan_worker,
            args=(job_id, cancel_event, task_id, dict(params)),
            daemon=True, name="scan-" + job_id,
        ).start()
        return {"ok": True, "job_id": job_id}

    def _scan_worker(self, job_id, cancel_event, task_id, params):
        """scan_append 作业 daemon 线程（仿 _search_worker/_move_worker）：
        协程提交到 engine.loop 消费 stream；异常兜底为 job error（文案含
        已追加数）；finally 仿 _move_worker 清 _scan_tasks 占位并做终态
        未达兜底。绝不裸崩进程。"""
        try:
            fut = asyncio.run_coroutine_threadsafe(
                self._scan_coro(job_id, cancel_event, task_id, params),
                self.engine.loop)
            fut.result()
        except Exception as e:  # noqa: BLE001 - worker 线程兜底
            _job_log.warning("scan worker 异常 job=%s", job_id, exc_info=True)
            snap = self.jobs.snapshot(job_id) or {}
            added = snap.get("done", 0)
            err_msg = f"已追加 {added} 条后失败：{type(e).__name__}: {e}"
            self.jobs.update(job_id, force=True, state="error",
                             phase="error", phase_detail="追加扫描失败",
                             error=err_msg)
        finally:
            with self._scan_lock:
                self._scan_tasks.pop(task_id, None)
            snap = self.jobs.snapshot(job_id)
            if (snap is not None
                    and snap.get("state") not in ("done", "error",
                                                  "cancelled")):
                added = snap.get("done", 0)
                self.jobs.update(job_id, force=True, state="error",
                                 phase="error", phase_detail="追加扫描失败",
                                 error=f"已追加 {added} 条后未正常收口")

    async def _scan_coro(self, job_id, cancel_event, task_id, params):
        """scan_append 作业协程（跑在 engine.loop）：消费 stream、
        每批重读任务→过滤真新增→append_records 落盘→节流推增量帧
        （task-item 8 键 msg 形）；终帧先于 done job_update；取消竞态
        按 preview 同构处理。"""
        p = dict(params)
        t0 = time.time()
        added_total = 0
        skipped_dup = 0
        items_seq = []       # 已追加条目的序列化（按到达序，键唯一）
        pushed_n = 0         # 已 WS 下发条数
        last_push = 0.0
        first_sent = False

        def _maybe_push():
            """WS 节流（沿用 SEARCH_WS_* 常量）。"""
            nonlocal pushed_n, last_push, first_sent
            n = len(items_seq)
            if n <= pushed_n:
                return
            now = time.time()
            if not first_sent:
                if ((n - pushed_n) >= SEARCH_WS_FIRST_ITEMS
                        or (now - t0 >= SEARCH_WS_FIRST_SECS)):
                    self._search_push(
                        job_id, {"items": list(items_seq[:n])})
                    pushed_n, last_push, first_sent = n, now, True
                return
            if ((now - last_push) >= SEARCH_WS_INTERVAL
                    or (n - pushed_n) >= SEARCH_WS_DELTA_ITEMS):
                self._search_push(
                    job_id, {"items": list(items_seq[pushed_n:n])})
                pushed_n, last_push = n, now

        self.jobs.update(job_id, state="running", phase="server_search",
                         phase_detail="追加扫描启动", total=0)
        # 任务对象每批重读（缩小并发丢更新窗口）
        t = core.load_task(task_id)
        if t is None or t.get("id") != task_id:
            self.jobs.update(job_id, force=True, state="error",
                             phase="error", phase_detail="追加扫描失败",
                             error="任务不存在")
            return
        channel = t["channel"]
        entity = await self.client.get_entity(channel)
        async for ev in core.search_videos_stream(self.client, entity, p,
                                                  cancel=cancel_event):
            kind = ev["kind"]
            if kind == "progress":
                detail = (f"已扫描 {ev['scanned']} 条"
                          f" · 已匹配 {ev['matched']} 条"
                          f" · 已追加 {added_total} 条")
                self.jobs.update(job_id, phase=ev["phase"],
                                 phase_detail=detail,
                                 done=added_total, total=0)
            elif kind == "items":
                # 每批重读任务对象（获取最新已落盘条目）
                t = core.load_task(task_id)
                if t is None or t.get("id") != task_id:
                    self.jobs.update(
                        job_id, force=True, state="error",
                        phase="error", phase_detail="追加扫描失败",
                        error=f"已追加 {added_total} 条后任务丢失")
                    return
                # 用 item_key + (name,size) 集合过滤本批真新增候选
                exist_keys = {core.item_key(ti) for ti in t["items"]}
                exist_content = {(ti["name"], ti["size"])
                                 for ti in t["items"]}
                fresh_recs = []
                for r in ev["items"]:
                    k = (r.get("src", core.SRC_CHANNEL), r["msg"].id)
                    if k in exist_keys or (r["name"], r["size"]) in exist_content:
                        skipped_dup += 1
                        continue
                    fresh_recs.append(r)
                    exist_keys.add(k)
                    exist_content.add((r["name"], r["size"]))
                if fresh_recs:
                    added = core.append_records(t, fresh_recs)
                    added_total += added
                    for r in fresh_recs[:added]:
                        ti = core.make_task_item(r)
                        items_seq.append(ti)
                    _maybe_push()
            elif kind == "done":
                if cancel_event.is_set():
                    # T57：取消竞态收口 cancelling→cancelled
                    self.jobs.update(job_id, state="cancelled")
                    return
                records, meta = ev["records"], ev["meta"]
                meta["added"] = added_total
                meta["skipped_dup"] = skipped_dup
                meta["n_matched"] = len(records)
                # 终帧必须先于 done 的 job_update（同一协程内顺序调用）
                self._search_push(
                    job_id, {"items": [], "meta": meta, "final": True})
                self.jobs.update(job_id, state="done", phase="filter",
                                 phase_detail=f"已追加 {added_total} 条",
                                 done=added_total, total=0)
                return
        # T57：扫描流被取消时生成器直接 return（不一致 done），
        # async for 提前结束——收口 cancelling→cancelled，防卡片永挂取消中
        if cancel_event.is_set():
            self.jobs.update(job_id, state="cancelled")

    def editor_data(self, task_id, enrich=True):
        """任务与全部条目（含 TG 消息原文、时长、日期）。
        enrich=True 时后台补全缺失 text/duration；回捞时传 False 仅取本地。"""
        t = self._load_one(task_id)
        if t is None:
            return {"ok": False, "error": "任务不存在"}
        # 从 Telegram 补全旧条目缺失的消息文本/时长：改为后台执行，绝不阻塞
        # 首屏返回——否则条目多时 get_entity + 分批 get_messages 会让编辑窗口
        # 长时间停在 loading。后台独立重新加载任务副本，避免与本次返回竞争；
        # 补全结果由 enrich 内部 save_task 持久化，前端再静默拉一次即可。
        if enrich and self.client is not None:
            def _bg_enrich():
                try:
                    tt = self._load_one(task_id)
                    if tt is None:
                        return

                    async def _en():
                        await core.enrich_task_items(self.client, tt)

                    self.engine.submit(_en())
                except Exception as e:
                    print("[GUI] 条目详情补全失败：", e)

            threading.Thread(target=_bg_enrich, daemon=True).start()
        return {
            "ok": True,
            "task": {
                "id": t["id"],
                "channel_title": t.get("channel_title", ""),
                "channel": t.get("channel", ""),
                "folder": t.get("folder", ""),
                "parallel": t.get("params", {}).get("parallel", 1),
                "created": t.get("created", ""),
            },
            "items": t["items"],
            "counts": core.task_counts(t),
        }

    def editor_save(self, task_id, fields):
        return self.edit_task(task_id, fields)

    def editor_set_status(self, task_id, keys, status):
        t = self._load_one(task_id)
        if t is None:
            return {"ok": False, "error": "任务不存在"}
        ks = [(k.get("src", core.SRC_CHANNEL), int(k["mid"])) for k in keys]
        n = core.set_items_status(t, ks, status)
        return {"ok": True, "n": n}

    def editor_remove(self, task_id, keys):
        return self.remove_items(task_id, keys)

    def item_file_info(self, task_id, src, mid):
        """条目对应本地文件信息：是否存在/路径/大小"""
        t = self._load_one(task_id)
        if t is None:
            return {"ok": False}
        ti = core.find_item(t, (src, int(mid)))
        if ti is None:
            return {"ok": False}
        p = pathlib.Path(t["folder"]) / ti["name"]
        partial = pathlib.Path(str(p) + ".dl")
        return {"ok": True, "exists": p.exists(),
                "path": str(p), "st_size": (p.stat().st_size if p.exists() else 0),
                "partial": partial.exists()}

    def items_file_info(self, task_id):
        """批量返回任务下所有条目的文件存在状态：{"src|mid": {exists,partial}}"""
        t = self._load_one(task_id)
        if t is None:
            return {"ok": False}
        folder = pathlib.Path(t["folder"])
        out = {}
        for ti in t["items"]:
            p = folder / ti["name"]
            key = f"{ti.get('src', core.SRC_CHANNEL)}|{ti['msg']}"
            out[key] = {"exists": p.exists(), "partial": (pathlib.Path(str(p)+'.dl').exists())}
        return {"ok": True, "items": out}

    def open_item_file(self, task_id, src, mid):
        r = self.item_file_info(task_id, src, mid)
        if r.get("exists"):
            return self._open_path(r["path"])
        return {"ok": False, "error": "文件不存在"}

    def _open_path(self, p):
        try:
            if sys.platform.startswith("win"):
                os.startfile(p)  # noqa
            elif sys.platform == "darwin":
                subprocess.Popen(["open", p])
            else:
                subprocess.Popen(["xdg-open", p])
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ---------- 其它 ----------
    def open_folder(self, path):
        if not path:
            path = str(core.get_download_root(self.cfg))
        try:
            if sys.platform.startswith("win"):
                os.startfile(path)  # noqa
            elif sys.platform == "darwin":
                subprocess.Popen(["open", path])
            else:
                subprocess.Popen(["xdg-open", path])
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def choose_folder(self):
        try:
            import tkinter as tk
            from tkinter import filedialog
            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            p = filedialog.askdirectory()
            root.destroy()
            return {"ok": True, "path": p}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def get_settings(self):
        return {
            "download_root": str(
                self.cfg.get("download_root") or core.DEFAULT_DOWNLOAD_ROOT
            ),
        }

    def set_download_root(self, path):
        self.cfg["download_root"] = path
        core.save_config(self.cfg)
        core.get_download_root(self.cfg)
        return {"ok": True}

    # ---------- 实时环境取光 ----------
    def backdrop_frame(self):
        cap = getattr(self, "capture", None)
        if cap is None:
            return {"ok": False}
        try:
            changed, url, ver = cap.render_jpeg()
            return {"ok": True, "changed": changed, "url": url, "v": ver}
        except Exception as e:
            print("[GUI] backdrop_frame 失败：", e)
            return {"ok": False}


if sys.platform == "win32":
    from ctypes import wintypes

    class _MagImageHeader(ctypes.Structure):
        # width/height/format/stride 的偏移（0/4/8/24）是回调唯一读取的
        # 字段，必须与运行时一致；尾部字段不使用。
        _fields_ = [
            ("width", wintypes.UINT),
            ("height", wintypes.UINT),
            ("format", ctypes.c_byte * 16),
            ("stride", wintypes.UINT),
            ("offset_x", wintypes.LONG),
            ("offset_y", wintypes.LONG),
        ]

    _MAGCB = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HWND, ctypes.c_void_p, _MagImageHeader,
        ctypes.c_void_p, _MagImageHeader, wintypes.RECT, wintypes.RECT,
        wintypes.HRGN,
    )

    class _MagTransform(ctypes.Structure):
        _fields_ = [("v", ctypes.c_float * 3 * 3)]

    # ---- 64 位安全的原型声明 ----
    # 不声明时 ctypes 默认把句柄当 32 位 c_int：CreateWindowExW 返回的 HWND
    # 一旦超过 0x7FFFFFFF 就被截断，后续 Mag* 调用拿到坏句柄而 access
    # violation（取光组件偶发初始化失败、液态玻璃“时有时无”的根因）。
    _u32 = ctypes.windll.user32
    _magf = ctypes.windll.magnification
    _EnumProcT = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    _u32.GetWindowRect.argtypes = [
        wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    _u32.GetWindowRect.restype = wintypes.BOOL
    _u32.EnumChildWindows.argtypes = [
        wintypes.HWND, _EnumProcT, wintypes.LPARAM]
    _u32.EnumChildWindows.restype = wintypes.BOOL
    _u32.GetWindowThreadProcessId.argtypes = [
        wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    _u32.GetWindowThreadProcessId.restype = wintypes.DWORD
    _u32.EnumThreadWindows.argtypes = [
        wintypes.DWORD, _EnumProcT, wintypes.LPARAM]
    _u32.EnumThreadWindows.restype = wintypes.BOOL
    _u32.CreateWindowExW.argtypes = [
        wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
    _u32.CreateWindowExW.restype = wintypes.HWND
    _u32.SetLayeredWindowAttributes.argtypes = [
        wintypes.HWND, wintypes.COLORREF, wintypes.BYTE, wintypes.DWORD]
    _u32.SetLayeredWindowAttributes.restype = wintypes.BOOL
    _u32.SetWindowPos.argtypes = [
        wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
        ctypes.c_int, ctypes.c_int, wintypes.DWORD]
    _u32.SetWindowPos.restype = wintypes.BOOL
    _u32.InvalidateRect.argtypes = [
        wintypes.HWND, wintypes.LPVOID, wintypes.BOOL]
    _u32.InvalidateRect.restype = wintypes.BOOL
    _u32.UpdateWindow.argtypes = [wintypes.HWND]
    _u32.UpdateWindow.restype = wintypes.BOOL
    _u32.IsIconic.argtypes = [wintypes.HWND]
    _u32.IsIconic.restype = wintypes.BOOL

    _magf.MagInitialize.restype = wintypes.BOOL
    _magf.MagSetWindowFilterList.argtypes = [
        wintypes.HWND, wintypes.DWORD, ctypes.c_int,
        ctypes.POINTER(wintypes.HWND)]
    _magf.MagSetWindowFilterList.restype = wintypes.BOOL
    _magf.MagSetImageScalingCallback.argtypes = [wintypes.HWND, _MAGCB]
    _magf.MagSetImageScalingCallback.restype = wintypes.BOOL
    _magf.MagSetWindowSource.argtypes = [wintypes.HWND, wintypes.RECT]
    _magf.MagSetWindowSource.restype = wintypes.BOOL
    _magf.MagSetWindowTransform.argtypes = [
        wintypes.HWND, ctypes.POINTER(_MagTransform)]
    _magf.MagSetWindowTransform.restype = wintypes.BOOL


class AmbientCapture:
    """实时环境取光。

    创建一个不可见、鼠标穿透的全屏放大镜窗口，借助 Magnification API
    的缩放回调持续拿到真实屏幕像素；主窗口被排除在采样之外，于是
    主窗口区域采到的正是它"背后"的内容（壁纸 / 其他窗口）。
    """

    def __init__(self):
        self._cb = None
        self.mag_hwnd = None
        self.main_hwnd = None
        self.sw = self.sh = 0
        self._lock = threading.Lock()
        self._frame = None           # (w, h, buffer BGRA)
        self._buf = None             # 复用的全屏缓冲，避免每帧分配大块
        self.version = 0
        self._cache_key = None
        self._prev_bytes = None       # 上一次 JPEG 字节，用于内容去重

    def _main_rect(self):
        r = wintypes.RECT()
        ctypes.windll.user32.GetWindowRect(
            self.main_hwnd, ctypes.byref(r))
        return (r.left, r.top, r.right - r.left, r.bottom - r.top)

    def start(self, main_hwnd):
        user32 = ctypes.windll.user32
        mag = ctypes.windll.magnification
        sw, sh = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
        self.sw, self.sh = sw, sh
        self.main_hwnd = int(main_hwnd)

        if not mag.MagInitialize():
            raise OSError("MagInitialize 失败")

        # 收集要排除的窗口：主窗 + 全部子窗
        excluded = [self.main_hwnd]
        enum_proc = ctypes.WINFUNCTYPE(
            wintypes.BOOL, wintypes.HWND, wintypes.LPARAM
        )(lambda h, _l: (excluded.append(int(h)), True)[1])
        user32.EnumChildWindows(self.main_hwnd, enum_proc, 0)

        # 同一 UI 线程的所有窗口（WebView2/Chromium 的隐藏窗口在该线程）
        tid = user32.GetWindowThreadProcessId(self.main_hwnd, None)
        user32.EnumThreadWindows(int(tid), enum_proc, 0)

        # 放大镜始终为全屏：几何完全不依赖主窗当前矩形（主窗在线程恢复
        # DPI 的瞬间可能处于虚拟化中间态），这是取光稳定不崩的关键。
        # 主窗背后的像素在 render_jpeg 里按主窗矩形实时裁剪。
        # 分层（alpha=0 不可见）+ 鼠标穿透 + 工具窗（不进任务栏）。
        # 归主窗所有（owned），确保不在任务栏/Alt+Tab 出现，并随主窗销毁。
        self.mag_hwnd = user32.CreateWindowExW(
            0x00080000 |            # WS_EX_LAYERED
            0x00000020 |            # WS_EX_TRANSPARENT（鼠标穿透）
            0x00000080 |            # WS_EX_TOOLWINDOW（不显示任务栏按钮）
            0x08000000,             # WS_EX_NOACTIVATE（不抢焦点）
            "Magnifier", None,
            0x80000000 | 0x10000000,      # WS_POPUP | WS_VISIBLE
            0, 0, sw, sh,
            wintypes.HWND(self.main_hwnd), None, None, None,
        )
        if not self.mag_hwnd:
            raise OSError("放大镜窗口创建失败")
        user32.SetLayeredWindowAttributes(self.mag_hwnd, 0, 0, 0x2)
        excluded.append(int(self.mag_hwnd))

        # 去重
        seen, uniq = set(), []
        for hh in excluded:
            if hh not in seen:
                seen.add(hh)
                uniq.append(hh)

        arr = (wintypes.HWND * len(uniq))(*[wintypes.HWND(hh) for hh in uniq])
        # MW_FILTERMODE_EXCLUDE = 0
        mag.MagSetWindowFilterList(self.mag_hwnd, 0, len(uniq), arr)

        self._cb = _MAGCB(self._on_frame)
        mag.MagSetImageScalingCallback(self.mag_hwnd, self._cb)
        mag.MagSetWindowSource(self.mag_hwnd, wintypes.RECT(0, 0, sw, sh))
        mt = _MagTransform()
        for i in range(3):
            mt.v[i][i] = 1.0
        mag.MagSetWindowTransform(self.mag_hwnd, ctypes.byref(mt))

        # 放大镜宿主窗口在创建/首次显示阶段会自行带上 WS_EX_TOPMOST，并把
        # 属主（主窗）一起抬进置顶层——表现为其它应用窗口无法盖到主窗上。
        # 该窗口 alpha=0 不可见、只用于采样，置顶毫无必要：统一显式取消。
        # 已实测 pump 的 MagSetWindowSource 不会再把置顶加回。
        _SWP = 0x0001 | 0x0002      # SWP_NOSIZE | SWP_NOMOVE
        _NOTOPMOST = wintypes.HWND(-2)
        user32.SetWindowPos(self.mag_hwnd, _NOTOPMOST, 0, 0, 0, 0, _SWP)
        user32.SetWindowPos(wintypes.HWND(self.main_hwnd),
                            _NOTOPMOST, 0, 0, 0, 0, _SWP)

    def _on_frame(self, hwnd, src, hdr, dst, dhdr, unc, clip, dirty):
        try:
            if src and hdr.stride and hdr.width and hdr.height:
                w, h, st = int(hdr.width), int(hdr.height), int(hdr.stride)
                row, need = w * 4, w * 4 * h
                with self._lock:
                    if self._buf is None or len(self._buf) < need:
                        self._buf = ctypes.create_string_buffer(need)
                    buf = self._buf
                    if st == row:
                        # 行紧凑：一次整块拷贝，不再逐行 Python 循环
                        ctypes.memmove(buf, src, need)
                    else:
                        addr = ctypes.addressof(buf)
                        for yy in range(h):
                            ctypes.memmove(addr + yy * row, src + yy * st, row)
                    self._frame = (w, h, buf)
                    self.version += 1
        except Exception:
            pass
        return True

    def pump(self):
        """在 UI 线程主动驱动全屏放大镜重采样（UpdateWindow 同步触发回调）。"""
        user32 = ctypes.windll.user32
        mag = ctypes.windll.magnification
        mag.MagSetWindowSource(self.mag_hwnd,
                               wintypes.RECT(0, 0, self.sw, self.sh))
        user32.InvalidateRect(self.mag_hwnd, None, False)
        user32.UpdateWindow(self.mag_hwnd)

    def render_jpeg(self, out_w=None, quality=82):
        """按主窗当前屏幕位置，从最新全屏帧裁剪背后区域，降采样并烘焙
        模糊，编码为 JPEG data URL。
        返回 (是否有变化, url或None, 版本号)。可从任意线程调用。"""
        rect = self._main_rect()
        with self._lock:
            if self._frame is None:
                return False, None, 0
            fw, fh, buf = self._frame
            ver = self.version
            if out_w is None:
                # 输出宽按主窗物理宽自适应：背景是预模糊环境光，按物理
                # 像素 2:1 降采样即可，彻底消除旧版固定 560 宽在 4K 屏被
                # 拉伸 6 倍造成的缩略图/马赛克感；上下限兼容低 DPI 窗口。
                rw = rect[2]
                out_w = max(640, min(1600, round(rw * 0.5)))
            key = (ver, rect, out_w, quality)
            if key == self._cache_key:
                return False, None, ver
            x, y, w, h = rect
            x0, y0 = max(0, x), max(0, y)
            x1, y1 = min(fw, x + w), min(fh, y + h)
            if x1 <= x0 or y1 <= y0:
                return False, None, ver
            from PIL import Image
            full = Image.frombuffer("RGBA", (fw, fh), buf, "raw", "BGRA", 0, 1)
            # crop 在锁内完成，得到独立图像，避免 buf 被 pump 复用
            crop = full.crop((x0, y0, x1, y1))
            cw = out_w
            ch = max(2, round(cw * crop.size[1] / crop.size[0]))
            small = crop.resize((cw, ch), Image.BILINEAR).convert("RGB")

        # 锁外后处理：一次干净的高斯霜化 + 提色 + gamma 提亮 + 白色雾气 +
        # 对角冷色液晶染色。旧版“盒式 resize 霜化”（先缩到约 74px 再放大
        # 回全尺寸）会让文本行变成横向条带，深色 IDE 背景整体 murky 灰暗，
        # 故彻底删除。注：仅 liquid 主题调用，静态主题不受影响。
        from PIL import Image, ImageFilter, ImageEnhance
        phys_w = rect[2] or cw
        # 输出宽约为主窗物理宽的一半，半径按比例反推，目标≈28 物理像素霜化
        r_eff = max(8, min(22, round(28 * cw / max(1, phys_w))))
        baked = small.filter(ImageFilter.GaussianBlur(r_eff))
        # 提亮前提色：环境彩光在玻璃边缘的折射更生动
        baked = ImageEnhance.Color(baked).enhance(1.40)
        # gamma 提亮暗部（指数 <1 把暗灰抬起），LUT 三通道复用
        lut = bytes(int(255 * ((i / 255.0) ** 0.82)) for i in range(256))
        baked = baked.point(lut * 3)
        # 整体亮度
        baked = ImageEnhance.Brightness(baked).enhance(1.16)
        # 白色雾气混合：通透感的关键
        baked = Image.blend(
            baked, Image.new("RGB", (cw, ch), (255, 255, 255)), 0.10)
        # 冷色液晶染色：左上纯蓝 (59,107,255) → 右下纯紫 (163,91,255) 的
        # 对角渐变，alpha 0.10，让灰色背景也有液晶冷调。256x256 对角掩码
        # 只生成一次并缓存，渐变层随后放大到当前帧尺寸。
        if getattr(self, "_diag_mask", None) is None:
            diag = Image.new("L", (256, 256))
            dpx = diag.load()
            for yy in range(256):
                for xx in range(256):
                    dpx[xx, yy] = (xx + yy) >> 1   # 左上 0 → 右下 255
            self._diag_mask = diag
        blue = Image.new("RGB", (256, 256), (59, 107, 255))
        purple = Image.new("RGB", (256, 256), (163, 91, 255))
        tint = Image.composite(purple, blue, self._diag_mask)
        tint = tint.resize((cw, ch), Image.BILINEAR)
        baked = Image.blend(baked, tint, 0.10)
        bio = io.BytesIO()
        baked.save(bio, format="JPEG", quality=quality)
        data = bio.getvalue()
        if data == self._prev_bytes:
            # 裁剪内容真正未变，不重复推送（静止画面零 JS 通信）
            self._cache_key = key
            return False, None, ver
        self._prev_bytes = data
        url = ("data:image/jpeg;base64,"
               + base64.b64encode(data).decode("ascii"))
        self._cache_key = key
        return True, url, ver


def _dpi_scale():
    """系统/主屏 DPI 缩放比例（不受调用线程 DPI context 影响的取法）。"""
    u = ctypes.windll.user32
    try:
        d = u.GetDpiForSystem()
        if d and d >= 96:
            return d / 96.0
    except Exception:
        pass
    try:
        shcore = ctypes.windll.shcore
        mon = u.MonitorFromWindow(wintypes.HWND(0), 1)  # MONITOR_DEFAULTTOPRIMARY
        dpx = wintypes.UINT()
        dpy = wintypes.UINT()
        shcore.GetDpiForMonitor(mon, 0, ctypes.byref(dpx), ctypes.byref(dpy))
        if dpx.value:
            return dpx.value / 96.0
    except Exception:
        pass
    return 1.0


class _BackdropDriver:
    """液态玻璃取光驱动：UI 线程周期采样 + 后台线程 JPEG 推送。

    窗口与定时器能力全部来自 host.HostApp/HostWindow 抽象，不含任何
    旧窗口栈专有符号（具体委托封装由宿主实现负责）。
    """

    PUMP_MS = 200       # 5fps 环境取光：背景帧每帧要解码+
                        # 重光栅全屏层，5fps 较 10fps GPU 减半

    def __init__(self, api, win, host_app, hwnd, user32):
        self.api = api
        self.cap = None        # 放大镜懒创建：仅液态玻璃模式才存在
        self.timer = None      # host.TimerHandle
        self.win = win
        self.host_app = host_app
        self.hwnd = hwnd
        self.user32 = user32
        self.last_ver = 0
        self.n = 0

    def set_mode(self, mode):
        """UI 线程调用：进入液态玻璃才创建放大镜并启动泵，其余模式停止。"""
        if mode == "liquid":
            if self.cap is None:
                cap = AmbientCapture()
                cap.start(self.hwnd)
                self.cap = cap
                self.api.capture = cap
            if self.timer is None:
                self.timer = self.host_app.set_interval(self.on_pump,
                                                        self.PUMP_MS)
            self.timer.start()
        elif self.timer is not None:
            self.timer.stop()

    def on_pump(self):
        # 必须在 UI 线程：同步驱动放大镜更新最新帧
        try:
            # 主窗最小化时不采样（保留最后一帧背景，零开销）
            if self.cap is not None and not self.user32.IsIconic(self.hwnd):
                self.cap.pump()
        except Exception as ex:
            print("[GUI] pump 失败：", ex)

    def push_loop(self):
        # 后台线程：编码并经 main 注入的推送句柄发出事件（不直接触碰页面）
        last = 0
        while True:
            time.sleep(0.2)              # 5fps 推送；环境光变化缓慢，足够流畅，
                                        # 静止画面字节去重零通信
            try:
                if self.cap is None or self.cap.version == last:
                    continue
                changed, url, ver = self.cap.render_jpeg()
                if changed and url:
                    self.api.push_event("_setBackdrop", [url])
                last = ver
            except Exception as ex:
                print("[GUI] 背景推送失败：", ex)
                time.sleep(0.5)


def main():
    if sys.platform != "win32":
        sys.stderr.write("v2.0 原生壳仅支持 Windows 桌面系统。\n")
        return 2

    # 1) PerMonitorV2 DPI 感知，保证物理坐标与放大镜裁剪准确
    try:
        if not ctypes.windll.user32.SetProcessDpiAwarenessContext(-4):
            raise OSError
    except Exception:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()

    # 2) 启动进程内 HTTP/WS 服务（api.cfg 由 server_app 负责加载）
    # 以脚本启动时本模块名是 __main__，而 server_app 顶层有 `import gui`：
    # 不登记会把 gui.py 二次加载为模块 gui，产生两份 WinFunctionType，第二份
    # 覆盖放大镜回调的 argtypes，导致 MagSetImageScalingCallback 注册失败。
    # 先把自身登记为 gui，保证全进程唯一模块身份（已存在则不覆盖）。
    sys.modules.setdefault("gui", sys.modules[__name__])
    import server_app
    handle = server_app.start(None)
    try:
        api = handle.api
        _mode = api.cfg.get("appearance", "dark")
        _bg = "#e9ecf5" if _mode == "light" else "#07080f"

        # 3) 原生壳初始化：UserDataFolder 入数据目录
        import wv2_app
        host_app = wv2_app.NativeHost(
            os.path.join(str(core.DATA_DIR), "wv2"))

        # 4) 注入连接信息与事件推送句柄
        api.host = host_app
        api.base_url = handle.base_url
        api.token = handle.token
        api.push_event = handle.push_event

        # on_shown：原生壳在 UI 线程直接触发 setup，不再 Invoke 包一层
        def on_shown(win):
            user32 = ctypes.windll.user32
            try:
                hwnd = win.hwnd

                # 恢复/确认 UI 线程 PerMonitorV2 context（保留 Win32 接线）
                user32.SetThreadDpiAwarenessContext.restype = wintypes.HANDLE
                user32.SetThreadDpiAwarenessContext.argtypes = [
                    wintypes.HANDLE]
                _newctx = user32.SetThreadDpiAwarenessContext(
                    wintypes.HANDLE(-4))
                print("[GUI] thread ctx ->", _newctx,
                      "DPI=", user32.GetDpiForSystem())

                drv = _BackdropDriver(api, win, host_app, hwnd, user32)
                threading.Thread(target=drv.push_loop,
                                 daemon=True).start()
                api._drv = drv
                # 按已保存外观决定是否创建放大镜并启动泵
                drv.set_mode(api.cfg.get("appearance", "dark"))

                # 物理尺寸补回（原生壳窗口已按物理尺寸创建，保留接线）
                s = user32.GetDpiForSystem() / 96.0
                if s > 1.0:
                    SWP_NOMOVE = 0x0002
                    SWP_NOZORDER = 0x0004
                    user32.SetWindowPos(
                        hwnd, 0, 0, 0,
                        int(1180 * s), int(780 * s),
                        SWP_NOMOVE | SWP_NOZORDER)
                user32.SetWindowTextW(hwnd, "TG 视频下载器")
            except Exception as e:
                print("[GUI] 取光组件初始化失败：", e)

        # 5) 创建主窗：token 只走 URL hash（不进访问日志）
        host_app.create_window(
            "TG 视频下载器",
            handle.base_url + "/#t=" + handle.token,
            api,
            1180, 780,
            (960, 640),
            _bg,
            on_shown,
        )

        # 6) 消息循环；退出路径收口服务
        host_app.run()
    finally:
        handle.shutdown()


if __name__ == "__main__":
    main()
