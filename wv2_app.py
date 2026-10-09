# -*- coding: utf-8 -*-
"""原生壳：ctypes 直宿主系统 WebView2（v2.0 原生重构 Task 4 + Task 9，TR-4.x/TR-9.x）。

薄模块，只负责“窗口 + 浏览器内核”的原生接线；页面与内核之间的唯一通道
是进程内 HTTP/WS（server_app，并行任务），本模块不感知任何业务协议：

  1. NativeHost(host.HostApp)：create_window / create_aux_window / run /
     ui_invoke（PostMessage 自定义 WM + threading.Event 的阻塞 marshal）/
     set_interval（SetTimer/WM_TIMER，回调在 UI 线程）。
  2. 主窗/辅助窗均为 ShellWindow（复用 wv2_native.NativeWindow）：
     CreateCoreWebView2EnvironmentWithOptions（UserDataFolder 入数据目录）
     -> 环境完成回调内 CreateCoreWebView2Controller
     -> 控制器完成回调内：Settings 四项（每项 set 后 get 读回）、
     put_Bounds（父窗客户区物理矩形）、MoveFocus(PROGRAMMATIC)、
     put_IsVisible(1)、Navigate(传入 URL)。
  3. WM_SIZE/WM_DPICHANGED 后按新物理客户区 put_Bounds；主窗 WM_CLOSE：
     收口辅助窗 -> 关本窗控制器 -> DestroyWindow -> WM_DESTROY（仅主窗
     PostQuitMessage）；辅助窗 WM_CLOSE 只关自己的控制器与窗口。
  4. 同一 Environment、同一 UI 线程支持第二个控制器（辅助窗）。
  5. STA 强制；COM 纪律：c_void_p 入参 `or 0`、回调指针帧内 AddRef/
     用毕 Release、native 方法一律经 wv2_com.com_slot_addr 二次解引用。
  6. 浏览器内核缺失（版本探测失败或环境回调 hr != S_OK）：MessageBoxW
     弹明确中文引导（含官方地址），非零退出，禁止静默失败。
  7. 自测：python wv2_app.py --selftest --hold 12
     仅标准库桩服务器（127.0.0.1:0 托管 web/，逐请求记日志，固定 token）；
     建窗 -> 导航桩 URL -> 程序化三组尺寸变化（Bounds 逐字段 0 误差）
     -> 深色渲染截图 -> WM_CLOSE 干净退出。

官方依据（证据副本 .trae/team/evidence/T03/WebView2.h，1.0.4258.31）：
  - Environment.CreateCoreWebView2Controller：44189（vtable 槽 3）；
  - WebView2.get_Settings：3023（槽 3）/ Navigate：3029（槽 5）/
    get_BrowserProcessId：3141（槽 37）；
  - Controller：get/put_IsVisible 39215/39218（槽 3/4）、
    get/put_Bounds 39221/39224（槽 5/6）、MoveFocus 39244（槽 12）、
    Close 39283（槽 24）、get_CoreWebView2 39285（槽 25）；
  - Settings：get/put_IsScriptEnabled 63923/63926（槽 3/4）、
    get/put_AreDevToolsEnabled 63947/63950（槽 11/12）、
    get/put_AreDefaultContextMenusEnabled 63953/63956（槽 13/14）、
    get/put_IsZoomControlEnabled 63965/63968（槽 17/18）；
  - COREWEBVIEW2_MOVE_FOCUS_REASON_PROGRAMMATIC = 0（WebView2.h 2661）；
  - winuser.h：WM_ERASEBKGND(0x14)、WM_GETMINMAXINFO(0x24)、
    MINMAXINFO、WM_APP(0x8000)；
  - T03/T04 已实证：STA 下完成回调在创建线程触发；MTA 线程上
    CoInitializeEx(STA) 返 RPC_E_CHANGED_MODE。
"""
import argparse
import ctypes
from ctypes import wintypes
import http.server
import json
import math
import os
import sys
import threading
import time

import host
import wv2_native as wn
import wv2_com as wc

# ============================================================================
# HRESULT / 常量
# ============================================================================

S_OK = wc.S_OK
E_UNEXPECTED = wc.E_UNEXPECTED

# COREWEBVIEW2_MOVE_FOCUS_REASON（WebView2.h 2661）
MOVE_FOCUS_PROGRAMMATIC = 0

# 自定义窗口消息（winuser.h：WM_APP = 0x8000）
WM_APP = 0x8000
WM_UI_INVOKE = WM_APP + 1          # ui_invoke 阻塞 marshal

WM_SIZE = 0x0005
WM_ERASEBKGND = 0x0014
WM_GETMINMAXINFO = 0x0024

MB_ICONERROR = 0x10

# ---- vtable 槽位（IUnknown 3 槽在前；逐方法核对头文件，见模块 docstring） ---
SLOT_ENV_CREATE_CONTROLLER = 3
SLOT_WV_GET_SETTINGS = 3
SLOT_WV_NAVIGATE = 5
SLOT_WV_GET_BROWSERPROCESSID = 37
SLOT_CTRL_GET_ISVISIBLE = 3
SLOT_CTRL_PUT_ISVISIBLE = 4
SLOT_CTRL_GET_BOUNDS = 5
SLOT_CTRL_PUT_BOUNDS = 6
SLOT_CTRL_MOVEFOCUS = 12
SLOT_CTRL_CLOSE = 24
SLOT_CTRL_GET_COREWEBVIEW2 = 25
SLOT_SET_GET_ISSCRIPTENABLED = 3
SLOT_SET_PUT_ISSCRIPTENABLED = 4
SLOT_SET_GET_AREDEVTOOLSENABLED = 11
SLOT_SET_PUT_AREDEVTOOLSENABLED = 12
SLOT_SET_GET_AREDEFAULTCONTEXTMENUSENABLED = 13
SLOT_SET_PUT_AREDEFAULTCONTEXTMENUSENABLED = 14
SLOT_SET_GET_ISZOOMCONTROLENABLED = 17
SLOT_SET_PUT_ISZOOMCONTROLENABLED = 18

# 类型简称（COM ABI：this/指针一律指针宽度）
H = ctypes.c_long          # HRESULT
P = ctypes.c_void_p
W = wintypes.LPCWSTR
B = wintypes.BOOL

# ============================================================================
# 额外 Win32 绑定（不改 wv2_native 文件，只补函数原型）
# ============================================================================

wn.user32.MessageBoxW.argtypes = [wintypes.HWND, wintypes.LPCWSTR,
                                  wintypes.LPCWSTR, wintypes.UINT]
wn.user32.MessageBoxW.restype = ctypes.c_int

wn.gdi32.CreateSolidBrush.argtypes = [wintypes.COLORREF]
wn.gdi32.CreateSolidBrush.restype = wintypes.HBRUSH
wn.user32.FillRect.argtypes = [wintypes.HDC, ctypes.POINTER(wn.RECT),
                               wintypes.HBRUSH]
wn.user32.FillRect.restype = ctypes.c_int
wn.user32.ClientToScreen.argtypes = [wintypes.HWND,
                                    ctypes.POINTER(wn.POINT)]
wn.user32.ClientToScreen.restype = wintypes.BOOL
wn.user32.GetWindowThreadProcessId.argtypes = [
    wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
wn.user32.GetWindowThreadProcessId.restype = wintypes.DWORD
wn.user32.RegisterClassW.argtypes = [ctypes.c_void_p]
wn.user32.RegisterClassW.restype = wintypes.ATOM
wn.user32.SetClassLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int,
                                       ctypes.c_longlong]
wn.user32.SetClassLongPtrW.restype = ctypes.c_longlong
wn.user32.InvalidateRect.argtypes = [wintypes.HWND,
                                     ctypes.POINTER(wn.RECT), wintypes.BOOL]
wn.user32.InvalidateRect.restype = wintypes.BOOL


class MINMAXINFO(ctypes.Structure):
    # winuser.h tagMINMAXINFO：五个 POINT
    _fields_ = [("ptReserved", wn.POINT),
                ("ptMaxSize", wn.POINT),
                ("ptMaxPosition", wn.POINT),
                ("ptMinTrackSize", wn.POINT),
                ("ptMaxTrackSize", wn.POINT)]


# ============================================================================
# native 裸槽调用辅助（纪律：一律经 wv2_com.com_slot_addr 二次解引用）
# ============================================================================

def com_call(ptr, slot, argtypes, args, restype=H):
    """对接口指针 ptr 调 vtable[slot]；this 与全部指针参数按 `or 0` 封送。

    restype 默认 HRESULT，返回 unsigned 视角 HRESULT；其他 restype 原样 int。
    """
    fn = ctypes.cast(wc.com_slot_addr(ptr, slot),
                     ctypes.WINFUNCTYPE(restype, P, *argtypes))
    raw = fn(P(int(ptr) or 0), *args)
    if restype is H:
        return int(raw or 0) & 0xFFFFFFFF
    return int(raw or 0)


def pump_messages(seconds):
    """在当前 UI（STA）线程排空消息队列指定时长。返回期间是否派发过消息。"""
    end = time.monotonic() + float(seconds)
    msg = wn.MSG()
    worked = False
    while True:
        while wn.user32.PeekMessageW(ctypes.byref(msg), None, 0, 0,
                                     1):           # PM_REMOVE
            worked = True
            if msg.message == wc.WM_QUIT:
                # 嵌套泵期间收到退出：重新投回线程队列
                wn.user32.PostThreadMessageW(
                    wc.kernel32.GetCurrentThreadId(), wc.WM_QUIT,
                    msg.wParam, msg.lParam)
                return worked
            wn.user32.TranslateMessage(ctypes.byref(msg))
            wn.user32.DispatchMessageW(ctypes.byref(msg))
        if time.monotonic() >= end:
            return worked
        if not worked:
            time.sleep(0.01)


# ============================================================================
# 手写 COM“完成回调”（4 槽 vtable；QI/AddRef/Release + FTM，T03 实证做法）
# ============================================================================

_HANDLER_LIVE = {}


class _CompletedHandler(ctypes.Structure):
    """任意 ICoreWebView2*CompletedHandler 的 Python 实现。

    iid 决定 QI 认的自身接口；callback(hr:int, result:int)->HRESULT。
    """

    _fields_ = [("lpVtbl", P)]

    def __init__(self, iid, callback):
        super().__init__()
        self.iid_raw = wc.guid_raw(iid)
        self.callback = callback
        self.refcount = 0
        self.ftm_ptr = 0
        self.qi_iids = []
        self.lpVtbl = ctypes.cast(ctypes.byref(_COMPLETED_VTBL), P)
        _HANDLER_LIVE[ctypes.addressof(self)] = self

    def as_ptr(self):
        return P(ctypes.addressof(self))


def _h_query_interface(this, riid, ppv):
    try:
        if not riid or not ppv:
            return wc.E_POINTER
        asked = ctypes.string_at(riid, 16)
        out = ctypes.cast(ppv, ctypes.POINTER(P))
        obj = _HANDLER_LIVE.get(int(this or 0))
        if obj is None:
            out[0] = None
            return wc.E_NOINTERFACE
        if len(obj.qi_iids) < 32:
            obj.qi_iids.append(wc._iid_text(asked))
        if (asked == obj.iid_raw
                or asked == wc.guid_raw(wc.IID_IUnknown)
                or asked == wc.guid_raw(wc.IID_IAgileObject)):
            obj.refcount += 1
            out[0] = P(int(this))
            return S_OK
        if asked == wc.guid_raw(wc.IID_IMarshal):
            # 自由线程封送器（FTM）：T03 实证无 FTM 时 Invoke 不投递
            if not obj.ftm_ptr:
                ftm = P(0)
                mhr = int(wc.ole32.CoCreateFreeThreadedMarshaler(
                    P(int(this)), ctypes.byref(ftm))) & 0xFFFFFFFF
                if mhr != S_OK or not ftm.value:
                    out[0] = None
                    return wc.E_NOINTERFACE
                obj.ftm_ptr = int(ftm.value)
            else:
                wc.com_addref(obj.ftm_ptr)
            out[0] = P(obj.ftm_ptr)
            return S_OK
        out[0] = None
        return wc.E_NOINTERFACE
    except Exception:
        return E_UNEXPECTED


def _h_add_ref(this):
    try:
        obj = _HANDLER_LIVE.get(int(this or 0))
        if obj is not None:
            obj.refcount += 1
            return obj.refcount
        return 1
    except Exception:
        return 0


def _h_release(this):
    try:
        obj = _HANDLER_LIVE.get(int(this or 0))
        if obj is None:
            return 0
        obj.refcount -= 1
        if obj.refcount < 0:
            obj.refcount = 0
        # 实例由 _HANDLER_LIVE 持有到进程退出（T03 纪律）
        return obj.refcount
    except Exception:
        return 0


def _h_invoke(this, hr, result, _reserved):
    try:
        obj = _HANDLER_LIVE.get(int(this or 0))
        if obj is None:
            return E_UNEXPECTED
        return int(obj.callback(int(hr or 0) & 0xFFFFFFFF,
                                int(result or 0)) or S_OK) & 0xFFFFFFFF
    except Exception:
        return E_UNEXPECTED


# 模块级保活（WINFUNCTYPE + vtable 在进程生命期内永不悬垂）
_qi_fn = wc.FN_QueryInterface(_h_query_interface)
_ar_fn = wc.FN_AddRef(_h_add_ref)
_rel_fn = wc.FN_Release(_h_release)
_inv_fn = wc.FN_CompletedInvoke(_h_invoke)


class _CompletedVtbl(ctypes.Structure):
    _fields_ = [("QueryInterface", wc.FN_QueryInterface),
                ("AddRef", wc.FN_AddRef),
                ("Release", wc.FN_Release),
                ("Invoke", wc.FN_CompletedInvoke)]


_COMPLETED_VTBL = _CompletedVtbl(QueryInterface=_qi_fn, AddRef=_ar_fn,
                                 Release=_rel_fn, Invoke=_inv_fn)

# ============================================================================
# 单窗 WebView 状态（控制器/页面/设置）
# ============================================================================


class _WindowView:
    """ShellWindow 的浏览器内核部分；与窗口同生命周期。"""

    def __init__(self, win):
        self.win = win
        self.controller = 0
        self.webview = 0
        self.settings = 0
        self.browser_pid = None
        self.ctrl_handler = None
        self.closed = False

    # -- 控制器完成回调（UI 线程） ------------------------------------------

    def on_controller_completed(self, hr, controller):
        win = self.win
        if self.closed:
            return S_OK
        if hr != S_OK or not controller:
            win.host_app.fatal_runtime(
                "浏览器控制器创建失败（HRESULT=%s）。" % wc.hr_name(hr))
            return S_OK
        self.controller = controller
        wc.com_addref(controller)            # 帧内 AddRef，持有到关窗

        try:
            # get_CoreWebView2（槽25；out 指针已由运行时 AddRef）
            wv_out = P(0)
            ghr = com_call(controller, SLOT_CTRL_GET_COREWEBVIEW2,
                           [P], [ctypes.byref(wv_out)])
            wv = int(wv_out.value or 0)
            if ghr != S_OK or not wv:
                win.host_app.fatal_runtime("取浏览器对象失败。")
                return S_OK
            self.webview = wv

            # get_Settings（槽3）
            st_out = P(0)
            shr = com_call(wv, SLOT_WV_GET_SETTINGS, [P],
                           [ctypes.byref(st_out)])
            settings = int(st_out.value or 0)
            if shr != S_OK or not settings:
                win.host_app.fatal_runtime("取浏览器设置对象失败。")
                return S_OK
            self.settings = settings

            # Settings 四项：setter 后立即 getter 读回
            want_devtools = 1 if win.host_app.debug else 0
            plan = [
                ("IsScriptEnabled", SLOT_SET_PUT_ISSCRIPTENABLED,
                 SLOT_SET_GET_ISSCRIPTENABLED, 1),
                ("AreDefaultContextMenusEnabled",
                 SLOT_SET_PUT_AREDEFAULTCONTEXTMENUSENABLED,
                 SLOT_SET_GET_AREDEFAULTCONTEXTMENUSENABLED, 0),
                ("AreDevToolsEnabled", SLOT_SET_PUT_AREDEVTOOLSENABLED,
                 SLOT_SET_GET_AREDEVTOOLSENABLED, want_devtools),
                ("IsZoomControlEnabled", SLOT_SET_PUT_ISZOOMCONTROLENABLED,
                 SLOT_SET_GET_ISZOOMCONTROLENABLED, 0),
            ]
            readback = {}
            for name, put_slot, get_slot, want in plan:
                ph = com_call(settings, put_slot, [B], [B(want)])
                got_v = B(0)
                gh = com_call(settings, get_slot,
                              [ctypes.POINTER(B)], [ctypes.byref(got_v)])
                ok = (ph == S_OK and gh == S_OK
                      and int(got_v.value) == want)
                readback[name] = {"set": want, "readback": int(got_v.value),
                                  "ok": ok}
                if not ok:
                    win.host_app.record_failure(
                        "Settings %s 读回不符：%r" % (name, readback[name]))
            win.host_app.emit({"event": "settings_readback",
                               "readback": readback})

            # 初始 put_Bounds = 父窗客户区物理矩形
            cr = wn.RECT()
            wn.user32.GetClientRect(win.hwnd, ctypes.byref(cr))
            self.put_rect(cr)

            # MoveFocus(PROGRAMMATIC) -> put_IsVisible(1)
            com_call(controller, SLOT_CTRL_MOVEFOCUS, [ctypes.c_int],
                     [MOVE_FOCUS_PROGRAMMATIC])
            vhr = com_call(controller, SLOT_CTRL_PUT_ISVISIBLE, [B], [B(1)])

            # 窗口首次可见：主窗在 UI 线程回调 on_shown（此时 hwnd 有效）
            if not win.is_aux and win.on_shown is not None:
                try:
                    win.on_shown(win)
                except Exception as ex:
                    win.host_app.record_failure(
                        "on_shown 异常：%r" % ex)

            # Navigate（传入 URL；token 在 URL hash 内）
            nhr = com_call(wv, SLOT_WV_NAVIGATE, [W], [win.url])
            win.host_app.emit({"event": "navigate", "hresult": nhr,
                               "put_isvisible_hresult": vhr})
            if nhr != S_OK:
                win.host_app.fatal_runtime(
                    "导航失败（HRESULT=%s）。" % wc.hr_name(nhr))

            # 浏览器进程 PID（进程清理取证用）
            pid = wintypes.DWORD(0)
            phr = com_call(wv, SLOT_WV_GET_BROWSERPROCESSID,
                           [ctypes.POINTER(wintypes.DWORD)],
                           [ctypes.byref(pid)])
            if phr == S_OK:
                self.browser_pid = int(pid.value)
        except Exception as ex:
            win.host_app.record_failure("控制器回调异常：%r" % ex)
        return S_OK

    # -- Bounds -------------------------------------------------------------

    def put_rect(self, cr):
        return com_call(self.controller, SLOT_CTRL_PUT_BOUNDS, [wn.RECT],
                        [wn.RECT(int(cr.left), int(cr.top),
                                 int(cr.right), int(cr.bottom))])

    def put_client(self):
        cr = wn.RECT()
        wn.user32.GetClientRect(self.win.hwnd, ctypes.byref(cr))
        return self.put_rect(cr), cr

    def get_bounds(self):
        b = wn.RECT()
        hr = com_call(self.controller, SLOT_CTRL_GET_BOUNDS,
                      [ctypes.POINTER(wn.RECT)], [ctypes.byref(b)])
        return hr, b

    # -- 关闭/释放 -----------------------------------------------------------

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.win.host_app.stop_window_timers(self.win.hwnd)
        close_hr = None
        if self.controller:
            close_hr = com_call(self.controller, SLOT_CTRL_CLOSE, [], [])
        # 给运行时处理关闭内部消息的时间，再释放我方引用
        pump_messages(0.5)
        refs = {}
        if self.settings:
            refs["settings"] = wc.com_release(self.settings)
        if self.webview:
            refs["webview"] = wc.com_release(self.webview)
        if self.controller:
            refs["controller"] = wc.com_release(self.controller)
        self.settings = self.webview = self.controller = 0
        self.win.host_app.emit({"event": "webview_closed",
                                "controller_close_hresult": close_hr,
                                "refs": refs,
                                "browser_pid": self.browser_pid})


# ============================================================================
# 原生窗口（复用 wv2_native.NativeWindow，扩展浏览器接线）
# ============================================================================


def parse_color(text):
    """'#RRGGBB' -> COLORREF（0x00BBGGRR）。非法值返回 0（黑）。"""
    try:
        v = int(str(text).strip().lstrip("#"), 16)
        return (v & 0xFF) | ((v >> 8) & 0xFF) << 8 | ((v >> 16) & 0xFF) << 16
    except Exception:
        return 0


class ShellWindow(wn.NativeWindow):
    """主窗/辅助窗统一类。is_aux 区分生命周期语义。

    不直接多重继承 host.HostWindow：其 hwnd 为抽象 property，而本窗口的
    hwnd 是实例属性；用 register 做虚拟子类登记，isinstance 成立且不触发
    抽象方法实例化检查（duck typing 满足 HostWindow 契约）。
    """

    def __init__(self, host_app, title, width, height, min_size,
                 background_color, url, is_aux, on_shown=None):
        # 以下属性必须早于 super：建窗期 WM_SIZE/WM_ERASEBKGND 即可能到达
        self.host_app = host_app
        self.url = url
        self.is_aux = bool(is_aux)
        self.on_shown = on_shown
        self.min_size = (int(min_size[0]), int(min_size[1]))
        self.bg_brush = wn.gdi32.CreateSolidBrush(parse_color(background_color))
        self.view = _WindowView(self)
        super().__init__(title, int(width), int(height))
        host_app.register_window(self)

    # -- host.HostWindow -----------------------------------------------------

    def evaluate_js(self, script):
        # v2 原生壳不提供页面脚本注入通道；页面与内核仅经 HTTP/WS 通信
        raise RuntimeError("原生壳不支持 evaluate_js")

    # -- 窗口过程 ------------------------------------------------------------

    def handle_message(self, hwnd, msg, wparam, lparam):
        if msg == WM_ERASEBKGND:
            # 浏览器首帧前用背景色擦除，避免白屏闪烁
            cr = wn.RECT()
            wn.user32.GetClientRect(hwnd, ctypes.byref(cr))
            wn.user32.FillRect(wparam, ctypes.byref(cr), self.bg_brush)
            return 1

        if msg == WM_GETMINMAXINFO:
            mmi = ctypes.cast(lparam, ctypes.POINTER(MINMAXINFO))[0]
            dpi = int(wn.user32.GetDpiForWindow(hwnd)) or 96
            s = dpi / 96.0
            mmi.ptMinTrackSize.x = int(round(self.min_size[0] * s))
            mmi.ptMinTrackSize.y = int(round(self.min_size[1] * s))
            return user32_defproc(hwnd, msg, wparam, lparam)

        if msg == WM_SIZE and self.view.controller:
            cx = int(lparam & 0xFFFF)
            cy = int((lparam >> 16) & 0xFFFF)
            if not self.view.closed:
                try:
                    self.view.put_rect(wn.RECT(0, 0, cx, cy))
                except Exception as ex:
                    self.host_app.record_failure(
                        "WM_SIZE put_Bounds 异常：%r" % ex)
            return super().handle_message(hwnd, msg, wparam, lparam)

        if msg == wn.WM_DPICHANGED:
            ret = super().handle_message(hwnd, msg, wparam, lparam)
            # SetWindowPos（super 内）已触发 WM_SIZE；再按最终客户区显式同步
            if self.view.controller and not self.view.closed:
                try:
                    self.view.put_client()
                except Exception as ex:
                    self.host_app.record_failure(
                        "WM_DPICHANGED put_Bounds 异常：%r" % ex)
            return ret

        if msg == wn.WM_TIMER:
            cb = self.host_app.route_timer(int(wparam))
            if cb is not None:
                try:
                    cb()
                except Exception as ex:
                    self.host_app.record_failure(
                        "定时器回调异常：%r" % ex)
                return 0
            return super().handle_message(hwnd, msg, wparam, lparam)

        if msg == WM_UI_INVOKE:
            self.host_app.run_invoke(int(lparam))
            return 0

        if msg == wn.WM_CLOSE:
            self.counts["WM_CLOSE"] += 1
            self._log("WM_CLOSE", aux=self.is_aux)
            try:
                if self.is_aux:
                    self.view.close()
                else:
                    self.host_app.close_aux_windows()
                    self.view.close()
            except Exception as ex:
                self.host_app.record_failure("关窗异常：%r" % ex)
            wn.user32.DestroyWindow(hwnd)
            return 0

        if msg == wn.WM_DESTROY:
            self.counts["WM_DESTROY"] += 1
            self._log("WM_DESTROY", aux=self.is_aux)
            self.host_app.unregister_window(self)
            if not self.is_aux:
                # 仅主窗退出消息循环：主窗关闭全退
                wn.user32.PostQuitMessage(0)
            return 0

        return super().handle_message(hwnd, msg, wparam, lparam)


def user32_defproc(hwnd, msg, wparam, lparam):
    return wn.user32.DefWindowProcW(hwnd, msg, wparam, lparam)


# 虚拟子类登记：ShellWindow 是 host.HostWindow 的可用实现
host.HostWindow.register(ShellWindow)


# ============================================================================
# UI 线程 marshal 请求（ui_invoke）
# ============================================================================


class _InvokeRequest:
    def __init__(self, fn):
        self.fn = fn
        self.event = threading.Event()
        self.result = None
        self.error = None


# 定时器句柄（host.TimerHandle 语义：默认不启动，start/stop 幂等，间隔不变）
class _Interval(host.TimerHandle):

    def __init__(self, host_app, callback, ms):
        self._host = host_app
        self._cb = callback
        self._ms = int(ms)
        self._id = host_app.alloc_timer_id()
        self._running = False

    def start(self):
        if not self._running:
            tid = wn.user32.SetTimer(
                self._host.main_hwnd, self._id, self._ms, None)
            if not tid:
                raise ctypes.WinError(ctypes.get_last_error())
            self._host._timer_cbs[self._id] = self._cb
            self._running = True

    def stop(self):
        if self._running:
            wn.user32.KillTimer(self._host.main_hwnd, self._id)
            self._host._timer_cbs.pop(self._id, None)
            self._running = False


# ============================================================================
# NativeHost
# ============================================================================


class NativeHost(host.HostApp):
    """ctypes 直宿主系统 WebView2 的窗口宿主。"""

    def __init__(self, data_dir, loader_path=None, debug=False):
        self.data_dir = os.path.abspath(data_dir)
        self.debug = bool(debug)
        self.failures = []

        here = os.path.dirname(os.path.abspath(__file__))
        if loader_path is None:
            # 随包双架构 loader 放 lib/<arch>/；按本机架构选择，未知架构
            # 保留原路径（旧资产已删除，随后加载时给出明确失败）
            import platform
            _machine = platform.machine().lower()
            if _machine in ("arm64", "aarch64"):
                _arch = "arm64"
            elif _machine in ("amd64", "x86_64"):
                _arch = "x64"
            else:
                loader_path = os.path.join(
                    here, "webview2_arm64", "runtimes", "win-arm64", "native",
                    "WebView2Loader.dll")
                _arch = None
            if _arch is not None:
                loader_path = os.path.join(
                    here, "lib", _arch, "WebView2Loader.dll")
        self._loader, loader_abs = wc.load_webview2_loader(loader_path)
        self.emit({"event": "loader_loaded", "loader": loader_abs})

        # COM 初始化（STA 强制）。MTA 线程上切 STA 会得到 RPC_E_CHANGED_MODE
        co_hr = int(wc.ole32.CoInitializeEx(None,
                                            wc.COINIT_APARTMENTTHREADED)) \
            & 0xFFFFFFFF
        self._co_inited = True
        self.emit({"event": "coinitialize", "hresult": co_hr,
                   "hresult_text": wc.hr_name(co_hr)})
        if co_hr == wc.RPC_E_CHANGED_MODE:
            raise RuntimeError(
                "当前线程已按 MTA 初始化 COM，原生壳必须运行在 STA 线程"
                "（CoInitializeEx 返回 RPC_E_CHANGED_MODE）。")
        if co_hr not in (S_OK, wc.S_FALSE):
            raise RuntimeError("COM 初始化失败：%s" % wc.hr_name(co_hr))

        # 运行时版本探测：失败即内核缺失
        ver_hr, version = wc.get_browser_version(self._loader)
        self.emit({"event": "runtime_version", "hresult": ver_hr,
                   "version": version})
        if ver_hr != S_OK or not version:
            self.fatal_runtime(
                "系统未检测到 WebView2 运行时（版本探测返回 %s）。"
                % wc.hr_name(ver_hr))

        os.makedirs(self.data_dir, exist_ok=True)

        self._ui_tid = threading_get_ident()
        self._main_window = None
        self._windows = []
        self._env = 0
        self._env_handler = None

        # ui_invoke
        self._invoke_seq = 0
        self._invoke_pending = {}

        # timers
        self._timer_seq = 7000
        self._timer_cbs = {}

    # -- 建窗 ---------------------------------------------------------------

    def create_window(self, title, url, js_api, width, height,
                      min_size, background_color, on_shown):
        win = ShellWindow(self, title, width, height, min_size,
                          background_color, url, False, on_shown)
        self._main_window = win
        self._start_environment()
        return win

    def create_aux_window(self, title, url, js_api, width, height,
                          min_size, background_color):
        # 由 HTTP 服务线程调用：整段 marshal 到 UI 线程执行
        def _make():
            win = ShellWindow(self, title, width, height, min_size,
                              background_color, url, True)
            self._create_controller(win)
            return win
        return self.ui_invoke(_make)

    # -- 消息循环 ------------------------------------------------------------

    def run(self):
        try:
            return int(self._main_window.run())
        finally:
            self.shutdown()

    def shutdown(self):
        if self._env:
            wc.com_release(self._env)
            self._env = 0
        if self._co_inited:
            wc.ole32.CoUninitialize()
            self._co_inited = False

    # -- UI 线程 marshal -----------------------------------------------------

    def ui_invoke(self, fn):
        if threading_get_ident() == self._ui_tid:
            # 已在 UI 线程：直接执行，避免 PostMessage 自等死锁
            return fn()
        self._invoke_seq += 1
        token = self._invoke_seq
        req = _InvokeRequest(fn)
        self._invoke_pending[token] = req
        ok = wn.user32.PostMessageW(self.main_hwnd, WM_UI_INVOKE, 0, token)
        if not ok:
            self._invoke_pending.pop(token, None)
            raise ctypes.WinError(ctypes.get_last_error())
        req.event.wait()
        self._invoke_pending.pop(token, None)
        if req.error is not None:
            raise req.error
        return req.result

    def run_invoke(self, token):
        req = self._invoke_pending.get(token)
        if req is None:
            return
        try:
            req.result = req.fn()
        except BaseException as ex:       # 异常原样回传调用线程
            req.error = ex
        finally:
            req.event.set()

    # -- 定时器 --------------------------------------------------------------

    def set_interval(self, callback, ms):
        return _Interval(self, callback, ms)

    def alloc_timer_id(self):
        self._timer_seq += 1
        return self._timer_seq

    def route_timer(self, timer_id):
        return self._timer_cbs.get(int(timer_id))

    def stop_window_timers(self, hwnd):
        # set_interval 产生的定时器全部建在主窗；关窗路径兜底回收
        for timer_id in list(self._timer_cbs.keys()):
            wn.user32.KillTimer(self.main_hwnd, timer_id)
            self._timer_cbs.pop(timer_id, None)

    # -- 窗口登记 ------------------------------------------------------------

    @property
    def main_hwnd(self):
        return int(self._main_window.hwnd) if self._main_window else 0

    def register_window(self, win):
        self._windows.append(win)

    def unregister_window(self, win):
        try:
            self._windows.remove(win)
        except ValueError:
            pass

    def close_aux_windows(self):
        for w in list(self._windows):
            if w.is_aux and not w.view.closed and w.hwnd:
                w.view.close()
                wn.user32.DestroyWindow(w.hwnd)

    # -- 环境/控制器 ---------------------------------------------------------

    def _start_environment(self):
        h = _CompletedHandler(
            wc.IID_ICoreWebView2CreateCoreWebView2EnvironmentCompletedHandler,
            self._on_env_completed)
        self._env_handler = h
        hr = int(self._loader.CreateCoreWebView2EnvironmentWithOptions(
            None, self.data_dir, None, h.as_ptr())) & 0xFFFFFFFF
        self.emit({"event": "create_environment_call", "hresult": hr})
        if hr != S_OK:
            self.fatal_runtime(
                "创建浏览器环境失败（HRESULT=%s）。" % wc.hr_name(hr))

    def _on_env_completed(self, hr, env):
        if hr != S_OK or not env:
            self.fatal_runtime(
                "浏览器环境回调失败（HRESULT=%s）。" % wc.hr_name(hr))
            return S_OK
        wc.com_addref(env)                 # 帧内 AddRef，持有到进程退出
        self._env = env
        self._create_controller(self._main_window)
        return S_OK

    def _create_controller(self, win):
        h = _CompletedHandler(
            wc.IID_ICoreWebView2CreateCoreWebView2ControllerCompletedHandler,
            win.view.on_controller_completed)
        win.view.ctrl_handler = h
        hr = com_call(self._env, SLOT_ENV_CREATE_CONTROLLER, [P, P],
                      [P(int(win.hwnd) or 0), h.as_ptr()])
        self.emit({"event": "create_controller_call", "hresult": hr,
                   "parent_hwnd": int(win.hwnd)})
        if hr != S_OK:
            self.fatal_runtime(
                "创建浏览器控制器调用失败（HRESULT=%s）。" % wc.hr_name(hr))

    # -- 故障/取证 -----------------------------------------------------------

    def record_failure(self, msg):
        self.failures.append(msg)
        sys.stderr.write("[wv2_app] %s\n" % msg)

    def fatal_runtime(self, detail):
        """浏览器内核缺失/致命错误：弹中文引导框，非零退出。

        消息泵已在运行 -> PostQuitMessage(1)；尚未运行 -> 直接抛异常。
        """
        text = (
            "无法启动浏览器内核组件。\n\n"
            + detail + "\n\n"
            "请安装微软官方 WebView2 运行时后重新启动本程序；安装地址：\n"
            "https://developer.microsoft.com/microsoft-edge/webview2/")
        wn.user32.MessageBoxW(0, text, "TG 视频下载器", MB_ICONERROR)
        self.record_failure(detail)
        hwnd = self.main_hwnd
        if hwnd:
            wn.user32.PostQuitMessage(1)
        else:
            raise RuntimeError(detail)

    def emit(self, obj):
        sys.stdout.write("EVIDENCE: "
                         + json.dumps(obj, ensure_ascii=True,
                                      default=str) + "\n")
        sys.stdout.flush()


def threading_get_ident():
    return threading.get_ident()


# ============================================================================
# 自测桩服务器（仅标准库 http.server，绑 127.0.0.1:0，托管 web/）
# ============================================================================

STUB_TOKEN = "T4-STUB-TOKEN-0001"


class _StubServer(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, web_dir, log_path):
        self.web_dir = os.path.abspath(web_dir)
        self.log_path = os.path.abspath(log_path)
        self.lines = []
        self.paths = []
        self._lock = threading.Lock()
        handler = functools_partial(_StubHandler, directory=self.web_dir)
        super().__init__(("127.0.0.1", 0), handler)

    def base_url(self):
        return "http://127.0.0.1:%d" % self.server_port

    def record(self, line, path=None):
        with self._lock:
            self.lines.append(line)
            if path is not None:
                self.paths.append(path)
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")

    def track_path(self, path):
        with self._lock:
            self.paths.append(path)

    def seen_all(self):
        with self._lock:
            paths = set(self.paths)
        return ("/" in paths and "/style.css" in paths
                and any(p.startswith("/app.js") for p in paths))


def functools_partial(fn, directory):
    import functools
    return functools.partial(fn, directory=directory)


class _StubHandler(http.server.SimpleHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        self.server.track_path(self.path.split("?")[0])
        return super().do_GET()

    def log_message(self, fmt, *args):
        # 每请求一行：含请求行与响应状态/字节数
        line = "%s - %s" % (self.address_string(), fmt % args)
        self.server.record(line)


# ============================================================================
# TR-4.1 自测驱动（状态机跑在 UI 线程，100ms WM_TIMER）
# ============================================================================

IDT_ST_TICK = 6101


class _Selftest:
    def __init__(self, host_app, server, win, evidence_dir, hold_s):
        self.host = host_app
        self.server = server
        self.win = win
        self.dir = os.path.abspath(evidence_dir)
        self.hold_s = float(hold_s)
        self.t0 = time.monotonic()
        self.phase = "wait_nav"
        self.nav_at = None
        self.in_action = False
        self.bounds_log = os.path.join(self.dir, "tr41_bounds.log")
        self.shot_path = os.path.join(self.dir, "t4_shell_dark.png")
        self.shot = None
        self.closed = False
        with open(self.bounds_log, "w", encoding="utf-8") as f:
            f.write("# TR-4.1 Bounds 0 误差校验\n")
            f.write("# start=%s hold=%s\n"
                    % (time.strftime("%Y-%m-%d %H:%M:%S"), self.hold_s))

    def start(self):
        tid = wn.user32.SetTimer(self.win.hwnd, IDT_ST_TICK, 100, None)
        if not tid:
            raise ctypes.WinError(ctypes.get_last_error())

    def _log_bounds(self, label, logical, dpi, outer, client, bhr, bounds):
        delta = {"left": bounds["left"] - client["left"],
                 "top": bounds["top"] - client["top"],
                 "right": bounds["right"] - client["right"],
                 "bottom": bounds["bottom"] - client["bottom"]}
        match = (bhr == S_OK and all(v == 0 for v in delta.values()))
        line = ("[%s] logical=%s dpi=%s outer=%s\n"
                "  client(l=%d t=%d r=%d b=%d)\n"
                "  bounds(l=%d t=%d r=%d b=%d) get_hr=%s\n"
                "  delta(l=%d t=%d r=%d b=%d) match=%s"
                % (label, logical, dpi, outer,
                   client["left"], client["top"],
                   client["right"], client["bottom"],
                   bounds["left"], bounds["top"],
                   bounds["right"], bounds["bottom"],
                   wc.hr_name(bhr),
                   delta["left"], delta["top"],
                   delta["right"], delta["bottom"], match))
        with open(self.bounds_log, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        return match

    def _initial_bounds(self):
        win = self.win
        dpi = int(wn.user32.GetDpiForWindow(win.hwnd))
        cr = wn.RECT()
        wn.user32.GetClientRect(win.hwnd, ctypes.byref(cr))
        bhr, b = win.view.get_bounds()
        client = wn._rect_dict(cr)
        bounds = wn._rect_dict(b)
        ok = self._log_bounds("init", [1180, 780], dpi,
                              list(win.outer_size), client, bhr, bounds)
        if not ok:
            self.host.record_failure("初始 Bounds 不同步")

    def _resize(self, idx, logical_w, logical_h):
        win = self.win
        hwnd = win.hwnd
        dpi = int(wn.user32.GetDpiForWindow(hwnd))
        scale = dpi / 96.0
        cw = int(round(logical_w * scale))
        ch = int(round(logical_h * scale))
        r = wn.RECT(0, 0, cw, ch)
        wn.user32.AdjustWindowRectEx(ctypes.byref(r),
                                     wn.WS_OVERLAPPEDWINDOW, False, 0)
        outer_w = int(r.right - r.left)
        outer_h = int(r.bottom - r.top)
        wr0 = wn.RECT()
        wn.user32.GetWindowRect(hwnd, ctypes.byref(wr0))
        ok_pos = bool(wn.user32.SetWindowPos(
            hwnd, None, int(wr0.left), int(wr0.top),
            outer_w, outer_h, 0x4 | 0x10))
        # 嵌套泵 0.4s：WM_SIZE 已同步 put_Bounds；等渲染/内部布局跟上
        pump_messages(0.4)
        cr = wn.RECT()
        wn.user32.GetClientRect(hwnd, ctypes.byref(cr))
        bhr, b = win.view.get_bounds()
        match = self._log_bounds(
            "step%d" % idx, [logical_w, logical_h], dpi,
            [outer_w, outer_h], wn._rect_dict(cr), bhr,
            wn._rect_dict(b))
        if not ok_pos or not match:
            self.host.record_failure(
                "第 %d 组尺寸校验失败 pos=%s match=%s"
                % (idx, ok_pos, match))

    def tick(self):
        if self.in_action or self.closed:
            return
        now = time.monotonic()
        elapsed = now - self.t0
        self.in_action = True
        try:
            if self.phase == "wait_nav":
                if self.server.seen_all():
                    self.phase = "settle"
                    self.nav_at = now
                    self.host.emit({"event": "nav_seen",
                                    "elapsed_s": round(elapsed, 3)})
            elif self.phase == "settle":
                if now - self.nav_at >= 0.6:
                    self._initial_bounds()
                    self.phase = "step1"
            elif self.phase == "step1":
                self._resize(1, 900, 650)
                self.phase = "step2"
            elif self.phase == "step2":
                self._resize(2, 1400, 900)
                self.phase = "step3"
            elif self.phase == "step3":
                self._resize(3, 1180, 780)
                self.phase = "shot"
            elif self.phase == "shot":
                self.shot = wn.capture_window_png(self.win.hwnd,
                                                  self.shot_path)
                self.host.emit({"event": "screenshot", **self.shot})
                pid = self.win.view.browser_pid
                with open(self.bounds_log, "a", encoding="utf-8") as f:
                    f.write("browser_pid_before_close=%s\n"
                            "screenshot_ok=%s\n"
                            % (pid, bool(self.shot.get("ok"))))
                self.phase = "hold"
            # hold 阶段：等到 hold 截止（下方统一处理 WM_CLOSE）
            if elapsed >= self.hold_s and not self.closed:
                self.closed = True
                wn.user32.KillTimer(self.win.hwnd, IDT_ST_TICK)
                wn.user32.PostMessageW(self.win.hwnd, wn.WM_CLOSE, 0, 0)
        finally:
            self.in_action = False


# ============================================================================
# TR-4.1 自测入口
# ============================================================================


def run_selftest(args):
    os.makedirs(os.path.abspath(args.evidence_dir), exist_ok=True)
    os.makedirs(os.path.abspath(args.user_data), exist_ok=True)
    wn.set_process_dpi_awareness()

    stub_log = os.path.join(os.path.abspath(args.evidence_dir),
                            "stub_access.log")
    if os.path.exists(stub_log):
        os.remove(stub_log)
    server = _StubServer(args.web, stub_log)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    url = server.base_url() + "/#t=" + STUB_TOKEN
    host_app = NativeHost(args.user_data, args.loader, debug=args.debug)
    win = host_app.create_window(
        "TG 视频下载器 v2.0 · T4 原生壳自测",
        url, None, 1180, 780, (960, 640), "#07080f", None)

    st = _Selftest(host_app, server, win, args.evidence_dir, args.hold)

    # ShellWindow 经 host 定时器路由找到 st.tick（固定 ID，不经过分配器）
    host_app._timer_cbs[IDT_ST_TICK] = st.tick
    st.start()

    code = host_app.run()

    server.shutdown()
    server.server_close()

    ok = (int(code) == 0 and not host_app.failures
          and st.shot is not None and st.shot.get("ok")
          and server.seen_all())
    host_app.emit({"event": "selftest_summary", "ok": ok,
                   "exit_code": int(code),
                   "failures": host_app.failures})
    return 0 if ok else 1


# ============================================================================
# TR-9.1 液态管线边界自测（进程内 sink 替换事件推送）
# ============================================================================


class _WNDCLASS(ctypes.Structure):
    # winuser.h tagWNDCLASSW
    _fields_ = [("style", wintypes.UINT),
                ("lpfnWndProc", ctypes.c_void_p),
                ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE),
                ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HCURSOR),
                ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR)]


DIST_CLASS = "T91LiquidDisturberWindow"


TIMERPROC = ctypes.WINFUNCTYPE(None, wintypes.HWND, wintypes.UINT,
                               wintypes.WPARAM, wintypes.DWORD)
_GCL_HBRBACKGROUND = -10
_IDT_DIST_HUE = 9011


def _hsv_colorref(h, s, v):
    import colorsys
    r, g, b = colorsys.hsv_to_rgb(h % 1.0, s, v)
    return ((int(r * 255))
            | (int(g * 255) << 8)
            | (int(b * 255) << 16))


def _disturber_thread(owner_hwnd, ready):
    """独立线程的干扰窗：圆周运动 + 色相循环，制造真实且逐帧不同的像素变化。"""
    user32 = wn.user32
    hinst = wn.kernel32.GetModuleHandleW(None)
    state = {"hue": 0.0, "brush": wn.gdi32.CreateSolidBrush(0x00F2F4FA)}
    wc_obj = _WNDCLASS()
    wc_obj.lpfnWndProc = ctypes.cast(user32.DefWindowProcW, ctypes.c_void_p)
    wc_obj.hInstance = hinst
    wc_obj.hCursor = user32.LoadCursorW(None, wintypes.LPCWSTR(wn.IDC_ARROW))
    wc_obj.hbrBackground = state["brush"]
    wc_obj.lpszClassName = DIST_CLASS
    atom = user32.RegisterClassW(ctypes.byref(wc_obj))
    if not atom:
        ready.put(0)
        return

    # 色相循环（本线程定时器；比采样快，保证每次被采到的颜色都不同）
    def on_hue(_h, _m, _id, _t):
        state["hue"] += 0.08
        newb = wn.gdi32.CreateSolidBrush(
            _hsv_colorref(state["hue"], 0.28, 0.97))
        old = user32.SetClassLongPtrW(
            _h, _GCL_HBRBACKGROUND, ctypes.c_void_p(int(newb) or 0))
        if old:
            wn.gdi32.DeleteObject(wintypes.HBRUSH(int(old)))
        state["brush"] = int(newb)
        user32.InvalidateRect(_h, None, True)

    hue_proc = TIMERPROC(on_hue)
    # 初始位置：锚窗客户区左上角屏幕坐标
    pt = wn.POINT(0, 0)
    user32.ClientToScreen(owner_hwnd, ctypes.byref(pt))
    # 不设属主：跨线程创建带属主的窗口会向属主线程同步发消息（该线程
    # 此刻阻塞在 queue.get，无消息泵）会死锁；且属主窗强制置顶，与本窗
    # “位于锚窗背后”的目标相反。z 序由 on_pump 周期性压底维护。
    hwnd = user32.CreateWindowExW(
        0x80 | 0x08000000,            # WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE
        DIST_CLASS, "",
        0x80000000 | 0x10000000,      # WS_POPUP | WS_VISIBLE
        int(pt.x) + 120, int(pt.y) + 100, 130, 90,
        None, None, hinst, None)
    if not hwnd:
        ready.put(0)
        return
    if not user32.SetTimer(wintypes.HWND(hwnd), _IDT_DIST_HUE, 100,
                           hue_proc):
        ready.put(0)
        return
    ready.put(int(hwnd))
    msg = wn.MSG()
    while True:
        got = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
        if got == 0 or got == -1:
            break
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))
    try:
        user32.KillTimer(wintypes.HWND(hwnd), _IDT_DIST_HUE)
        user32.DestroyWindow(wintypes.HWND(hwnd))
    except Exception:
        pass


IDT_T91_PUMP = 6201


def run_liquid_sink(args):
    import queue as queue_mod
    import base64

    evdir = os.path.abspath(args.evidence_dir)
    os.makedirs(evdir, exist_ok=True)
    wn.set_process_dpi_awareness()

    # 锚窗（不挂浏览器；仅作为取光主窗等价物）
    anchor = wn.NativeWindow("T91 液态管线边界自测 · 锚窗", 900, 620)

    ready = queue_mod.Queue()
    dt = threading.Thread(target=_disturber_thread,
                          args=(int(anchor.hwnd), ready))
    dt.start()
    dist_hwnd = ready.get(timeout=10)
    if not dist_hwnd:
        return 1
    dist_tid = wn.user32.GetWindowThreadProcessId(
        wintypes.HWND(dist_hwnd), None)

    # 真实取光组件（与产品同一实现）
    import gui
    cap = gui.AmbientCapture()
    cap.start(int(anchor.hwnd))

    # -- _BackdropDriver 等价：UI 线程 200ms 采样（on_pump） ----------------
    angle = [0.0]

    def on_pump():
        # 移动干扰窗（锚窗客户区内圆周），再驱动放大镜重采样
        angle[0] += 0.55
        pt = wn.POINT(0, 0)
        wn.user32.ClientToScreen(anchor.hwnd, ctypes.byref(pt))
        cx = int(pt.x + anchor.client_rect["w"] / 2)
        cy = int(pt.y + anchor.client_rect["h"] / 2)
        radius = min(anchor.client_rect["w"], anchor.client_rect["h"]) * 0.28
        x = int(cx + math.cos(angle[0]) * radius) - 65
        y = int(cy + math.sin(angle[0]) * radius) - 45
        # 干扰窗压到 z 序底部，保证被锚窗遮挡；放大镜排除锚窗后即可采到它
        wn.user32.SetWindowPos(
            wintypes.HWND(dist_hwnd), wintypes.HWND(1), x, y, 0, 0,
            0x0001 | 0x10)   # NOSIZE | NOACTIVATE；HWND_BOTTOM(1)
        cap.pump()

    wn.user32.SetTimer(anchor.hwnd, IDT_T91_PUMP, 200, None)

    # -- push_loop 等价：后台线程 5fps render_jpeg -> 进程内 sink -------------
    end_at = time.monotonic() + float(args.seconds)
    sink = []
    sink_lock = threading.Lock()
    push_done = threading.Event()

    def push_equiv():
        last = 0
        while True:
            time.sleep(0.16)
            if time.monotonic() >= end_at:
                push_done.set()
                return
            try:
                if cap.version == last:
                    continue
                changed, url, ver = cap.render_jpeg()
                if changed and url:
                    with sink_lock:
                        sink.append((time.perf_counter(), url))
                last = ver
            except Exception as ex:
                sys.stderr.write("[T91] render 异常：%r\n" % ex)
                time.sleep(0.5)

    threading.Thread(target=push_equiv, daemon=True).start()

    # UI 线程限时消息泵（PeekMessage 驱动 on_pump 的 WM_TIMER）
    msg = wn.MSG()
    while time.monotonic() < end_at:
        while wn.user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
            if msg.message == wc.WM_QUIT:
                break
            if msg.message == wn.WM_TIMER and int(msg.wParam) == IDT_T91_PUMP:
                on_pump()
            else:
                wn.user32.TranslateMessage(ctypes.byref(msg))
                wn.user32.DispatchMessageW(ctypes.byref(msg))
        time.sleep(0.005)
    push_done.wait(timeout=2)

    # 收口
    wn.user32.KillTimer(anchor.hwnd, IDT_T91_PUMP)
    try:
        wn.user32.DestroyWindow(cap.mag_hwnd)
        ctypes.windll.magnification.MagUninitialize()
    except Exception:
        pass
    wn.user32.PostThreadMessageW(int(dist_tid), wc.WM_QUIT, 0, 0)
    dt.join(timeout=5)
    wn.user32.DestroyWindow(anchor.hwnd)

    # -- 统计 ---------------------------------------------------------------
    with sink_lock:
        events = list(sink)
    times = [e[0] for e in events]
    urls = [e[1] for e in events]
    intervals_ms = []
    if len(times) >= 2:
        intervals_ms = [round((times[i] - times[i - 1]) * 1000, 1)
                        for i in range(1, len(times))]
    window_s = float(args.seconds)
    fps = len(events) / window_s
    jpeg_lens = []
    prefix_ok = True
    for u in urls:
        if not u.startswith("data:image/jpeg;base64,"):
            prefix_ok = False
            continue
        raw = base64.b64decode(u[len("data:image/jpeg;base64,"):])
        jpeg_lens.append(len(raw))
    nonempty_ok = all(n > 0 for n in jpeg_lens) and len(jpeg_lens) > 0
    byte_stats = None
    if jpeg_lens:
        byte_stats = {"min": min(jpeg_lens), "max": max(jpeg_lens),
                      "mean": round(sum(jpeg_lens) / len(jpeg_lens), 1)}

    ok = (args.seconds >= 8 and prefix_ok and nonempty_ok
          and 3.5 <= fps <= 6.5 and len(events) >= 30)

    log_path = os.path.join(evdir, "tr91_liquid_sink.log")
    with open(log_path, "w", encoding="utf-8") as f:
        f.write("# TR-9.1 液态管线边界自测（进程内 sink）\n")
        f.write("# %s\n" % time.strftime("%Y-%m-%d %H:%M:%S"))
        f.write("window_seconds=%s events=%d fps=%.3f\n"
                % (window_s, len(events), fps))
        f.write("payload_prefix_ok=%s nonempty_ok=%s\n"
                % (prefix_ok, nonempty_ok))
        f.write("jpeg_bytes=%s\n" % byte_stats)
        f.write("intervals_ms=%s\n" % intervals_ms)
        f.write("event_records:\n")
        for t, u in events:
            f.write("  t=%.4f url_len=%d\n" % (t, len(u)))
        f.write("pass=%s\n" % ok)
    return 0 if ok else 1


# ============================================================================
# 命令行
# ============================================================================


def main(argv=None):
    here = os.path.dirname(os.path.abspath(__file__))
    default_evidence = os.path.abspath(os.path.join(
        here, "..", "..", "..", ".trae", "team", "evidence", "T04"))

    parser = argparse.ArgumentParser(
        description="TG 视频下载器 v2.0 原生壳（ctypes 直宿主 WebView2）")
    parser.add_argument("--selftest", action="store_true",
                        help="TR-4.1 自测：桩服务器 + 建窗 + 三组尺寸 + 截图")
    parser.add_argument("--liquid-sink", action="store_true",
                        help="TR-9.1 液态管线边界自测（进程内 sink）")
    parser.add_argument("--hold", type=float, default=12.0,
                        help="自测窗口存活秒数（默认 12）")
    parser.add_argument("--seconds", type=float, default=9.0,
                        help="液态管线自测时长（默认 9，要求 >= 8）")
    parser.add_argument("--loader",
                        default=os.path.join(
                            here, "webview2_arm64", "runtimes", "win-arm64",
                            "native", "WebView2Loader.dll"))
    parser.add_argument("--web", default=os.path.join(here, "web"))
    parser.add_argument("--evidence-dir", default=default_evidence)
    parser.add_argument("--user-data",
                        default=os.path.join(default_evidence, "wv2data"))
    parser.add_argument("--debug", action="store_true",
                        help="调试构建：允许浏览器开发者工具（产品恒为关闭）")
    args = parser.parse_args(argv)

    if sys.platform != "win32":
        sys.stderr.write("wv2_app 仅支持 Windows\n")
        return 2
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    if args.selftest:
        return run_selftest(args)
    if args.liquid_sink:
        return run_liquid_sink(args)
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
