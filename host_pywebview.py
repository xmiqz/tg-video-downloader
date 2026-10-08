# -*- coding: utf-8 -*-
"""pywebview 宿主适配（迁移期实现，Task 1）。

本模块是仓库内唯一允许直接出现 webview/pythonnet/CLR 符号的文件。
行为与 v1.2.5 gui.main() 完全等价：
  - ARM64 上先切换到随包 CoreCLR 再 import webview；
  - 主窗/辅助窗参数逐字保持；
  - on_shown 由 pywebview 在非 UI 线程触发，再经 form.Invoke 回到 UI 线程；
  - 周期定时器用 System.Windows.Forms.Timer（Tick 在 UI 线程）。
Task 9 将由 wv2_native.NativeHost 取代本实现。
"""
import threading

# ARM64 Windows：在 import webview 前把 pythonnet 切到随包分发的
# CoreCLR(.NET 8)；x64 上此调用为空操作。
import arm64_runtime
arm64_runtime.init()

import webview

import host


class _TimerHandle(host.TimerHandle):
    """WinForms Timer 包装；持有 EventHandler 委托防 GC 回收。"""

    def __init__(self):
        import clr
        clr.AddReference("System.Windows.Forms")
        from System.Windows.Forms import Timer

        self._timer = Timer()
        self._handler = None
        self._running = False

    def configure(self, callback, ms):
        self._timer.Interval = int(ms)

        def _tick(sender, event):
            callback()

        from System import EventHandler
        self._handler = EventHandler(_tick)
        self._timer.Tick += self._handler
        return self

    def start(self):
        if not self._running:
            self._timer.Start()
            self._running = True

    def stop(self):
        if self._running:
            self._timer.Stop()
            self._running = False


class _PyWebViewWindow(host.HostWindow):
    def __init__(self, win):
        self._win = win

    def evaluate_js(self, script):
        return self._win.evaluate_js(script)

    @property
    def hwnd(self):
        native = getattr(self._win, "native", None)
        if native is None:
            return 0
        return native.Handle.ToInt64()


class PyWebViewHost(host.HostApp):
    def __init__(self):
        self._main_native = None     # pywebview 原生窗（WinForms form）
        self._main_wrapper = None

    # ---------- 建窗 ----------
    def create_window(self, title, url, js_api, width, height,
                      min_size, background_color, on_shown):
        win = webview.create_window(
            title,
            url=url,
            js_api=js_api,
            width=width,
            height=height,
            min_size=min_size,
            transparent=False,             # 玻璃完全由网页内 backdrop 层渲染
            background_color=background_color,
            frameless=False,
        )
        wrapper = _PyWebViewWindow(win)
        self._main_native = win
        self._main_wrapper = wrapper

        def _shown():
            on_shown(wrapper)

        win.events.shown += _shown
        return wrapper

    def create_aux_window(self, title, url, js_api, width, height,
                          min_size, background_color):
        win = webview.create_window(
            title,
            url=url,
            js_api=js_api,
            width=width,
            height=height,
            min_size=min_size,
            background_color=background_color,
        )
        return _PyWebViewWindow(win)

    # ---------- 消息循环 ----------
    def run(self):
        webview.start(debug=False)

    # ---------- UI 线程 ----------
    def ui_invoke(self, fn):
        """同步 marshal 到 WinForms UI 线程（须在 on_shown 之后调用）。"""
        from System import Action
        # 保存结果/异常：Action 委托无返回值
        box = {}

        def _runner():
            try:
                box["value"] = fn()
            except BaseException as e:  # noqa: BLE001 - 需原样回抛调用线程
                box["error"] = e

        self._main_native.native.Invoke(Action(_runner))
        if "error" in box:
            raise box["error"]
        return box.get("value")

    # ---------- 定时器（须在 UI 线程创建） ----------
    def set_interval(self, callback, ms):
        return _TimerHandle().configure(callback, ms)
