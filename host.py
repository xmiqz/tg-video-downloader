# -*- coding: utf-8 -*-
"""宿主适配协议层（v2.0 原生重构 Task 1）。

gui.py 的全部窗口/线程/JS 调用只面向本模块的抽象基类，不依赖任何具体
窗口技术。可替换实现：
  - host_pywebview.PyWebViewHost：v1.x 旧实现（pywebview + pythonnet），
    仅作为迁移期兜底，Task 9 后主入口切换到 Win32/WebView2 原生实现；
  - 后续 wv2_native.NativeHost：ctypes 直宿主 WebView2（FR-1）；
  - v2.1：HTTP/WebSocket 传输的 headless Web 宿主（NAS）；
  - v2.2：Android JavascriptInterface 宿主。

桥接传输（RpcTransport）与窗口宿主（HostApp/HostWindow）正交：同一套
Api 不感知页面消息是 WebView2 postMessage、WebSocket 还是 JS 接口注入。
"""
import abc


class TimerHandle(abc.ABC):
    """周期定时器句柄。语义对齐 Win32 SetTimer/KillTimer：

    - 创建后默认不启动，由 start() 开始回调；
    - stop() 后可再次 start()，间隔不变；
    - 回调在 UI 线程触发（与窗口消息循环同线程）。
    """

    @abc.abstractmethod
    def start(self):
        """开始周期回调（幂等：已启动时无操作）。"""

    @abc.abstractmethod
    def stop(self):
        """暂停周期回调（幂等：未启动时无操作）。"""


class HostWindow(abc.ABC):
    """主窗口抽象。Api 与取光驱动只通过本接口触碰窗口。"""

    @abc.abstractmethod
    def evaluate_js(self, script):
        """同步执行一段 JS 并返回结果。仅允许在非 UI 线程调用，
        或在实现明确标注安全的场景下调用（与 pywebview 约束一致）。"""

    @property
    @abc.abstractmethod
    def hwnd(self):
        """顶层窗口原生句柄（HWND，整数）；窗口创建前为 0。"""


class HostApp(abc.ABC):
    """窗口宿主应用：建窗、消息循环、UI 线程投递、定时器。"""

    @abc.abstractmethod
    def create_window(self, title, url, js_api, width, height,
                      min_size, background_color, on_shown):
        """创建主窗口并返回 HostWindow。

        on_shown(win: HostWindow) 在窗口首次可见后于 UI 线程触发，
        此时 win.hwnd 有效。
        """

    @abc.abstractmethod
    def create_aux_window(self, title, url, js_api, width, height,
                          min_size, background_color):
        """创建辅助窗口并返回 HostWindow。

        v2.0 仅任务编辑器第二窗口使用；Task 16 改为应用内路由后，
        原生宿主可不实现本方法（抛 NotImplementedError）。
        """

    @abc.abstractmethod
    def run(self):
        """进入窗口消息循环；全部窗口关闭后返回（进程随后退出）。"""

    @abc.abstractmethod
    def ui_invoke(self, fn):
        """把无参可调用对象同步投递到 UI 线程执行并返回其结果；
        fn 内异常原样向调用线程抛出。"""

    @abc.abstractmethod
    def set_interval(self, callback, ms):
        """创建周期定时器（须在 UI 线程调用），返回 TimerHandle。
        callback 无参，在 UI 线程触发。"""


class RpcTransport(abc.ABC):
    """前端桥接传输协议（FR-2 / FR-7）。

    页面与 Api 之间的 JSON-RPC 经由本接口收发，与窗口技术解耦：
      - WebView2 原生宿主：出站 evaluate_js，入站 WebMessageReceived；
      - NAS headless：出站 WebSocket send，入站 WS on_message；
      - Android：出站 evaluateJavascript，入站 JavascriptInterface。

    入站报文约定（Task 5 固化）：{"id": str, "method": str, "params": [...]}；
    出站回执：{"id": str, "ok": bool, "result"?: any, "error"?: str}。
    """

    @abc.abstractmethod
    def send_to_page(self, javascript):
        """宿主 -> 页面：执行一段 JS（回执推送/主动事件）。"""

    @abc.abstractmethod
    def set_inbound_handler(self, handler):
        """页面 -> 宿主：注册处理器；handler 收一个 JSON 字符串报文。
        传输层负责线程 marshal，handler 内不做线程假设。"""
