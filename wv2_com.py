# -*- coding: utf-8 -*-
"""WebView2 COM 绑定与环境创建 harness（v2.0 原生重构 Task 3，TR-3.x）。

仅用标准库 ctypes 直调 WebView2Loader.dll（ARM64 真机，PE Machine=0xAA64）
与 Win32 COM/消息循环，手写 IUnknown 风格回调对象（不引入 comtypes/pywin32）：

  1. 声明 Loader 导出：
       GetAvailableCoreWebView2BrowserVersionString(PCWSTR, LPWSTR*)
       CreateCoreWebView2EnvironmentWithOptions(PCWSTR, PCWSTR,
           ICoreWebView2EnvironmentOptions*,
           ICoreWebView2CreateCoreWebView2EnvironmentCompletedHandler*)
     全部显式 argtypes/restype，失败 HRESULT 按 0x%08X 格式化；
  2. 按官方 WebView2.h 声明 9 个目标接口的完整 vtable（IUnknown 三槽在前，
     槽位数量/顺序逐方法与头文件一致，本任务不用的方法以 c_void_p 占位，
     并带方法名便于 Task 4 把占位槽替换为带签名的 WINFUNCTYPE）；
  3. 手写 COM 回调（Structure(lpVtbl) + 模块级 vtable + 模块级 WINFUNCTYPE
     保活，this 指针经进程级地址表反查 Python 对象），QueryInterface
     只认自身 IID 与 IID_IUnknown，AddRef/Release 维护计数，异常不允许
     逃出 native 边界；
     实测三个 ARM64/ctypes 关键事实（探针留存于 T03 证据目录）：
       a) 完成回调 native 实际按 (this, hr, result, 运行时内部值 x3)
          四个寄存器传参（x3 指向 EmbeddedBrowserWebView.dll rdata，
          语义未公开），WINFUNCTYPE 按指针宽度声明 4 参接收；
       b) c_void_p 形参为 0 时 ctypes 传入 None 而非 0：hr=S_OK 时
          int(hr) 会抛 TypeError 并被回调边界吞掉，表现为 Invoke
          静默失败（本任务调通前的实际阻塞根因），必须 int(hr or 0)；
       c) result 接口指针只在 Invoke 栈帧内有效：运行时在回调返回后
          立即释放内部引用、对象内存被堆复用（*ptr 从合法 vtable 变成
          0x...000002CD 等垃圾值），因此必须在回调内 AddRef 持引用，
          使用完毕再 Release 平衡；
     另：QI 路径同时实现 IAgileObject 标记与 IMarshal 的进程内自由
     线程封送器（CoCreateFreeThreadedMarshaler），与官方 WRL 样例
     RuntimeClass 的敏捷封送等价，供 MTA/跨套间投递使用；
     调用 native COM 方法一律走标准二次解引用 fn=*(*(obj)+8*slot) 的
     裸槽 cast（_com_slot_addr），不把对象直接 cast 成 vtable 结构；
  4. CoInitializeEx 支持 STA(0x2)/MTA(0x0) 两种套间试验；STA 实测环境
     完成回调在 Create...WithOptions 调用内同线程同步触发（Invoke 内
     PostQuitMessage 的 WM_QUIT 留在队列，Create 返回后 GetMessage
     立即取出）；MTA 行为由 --apartment mta 实测留证；Invoke 内 set
     threading.Event 并向创建线程 PostQuitMessage；SetTimer(NULL,..)
     线程定时器看门狗，超时投递 WM_QUIT(wParam=98)，防无人值守挂死；
  5. __main__ harness：打印 loader 绝对路径与运行时版本串，默认连续两轮
     创建/释放 ICoreWebView2Environment，逐轮打印 EVIDENCE JSON，全部
     成功进程 exit(0)，任一轮失败 exit(1)。

本任务只做环境层（Task 3）；不创建 ICoreWebView2Controller、不导航、
不做虚拟主机目录映射（属 Task 4，本文件只把对应 vtable 槽位占全）。

官方依据（IID 与 vtable 顺序逐方法核对，二进制 GUID 形态以头文件
EXTERN_C const IID 行交叉验证）：
  - Microsoft.Web.WebView2 NuGet 1.0.4258.31（2026 年当前稳定版），
    包内 build/native/include/WebView2.h（本地取证副本：
    .trae/team/evidence/T03/WebView2.h，2,939,599 字节），
    下载源（与 fetch_webview2.py 同源）：
    https://api.nuget.org/v3-flatcontainer/microsoft.web.webview2/
        1.0.4258.31/microsoft.web.webview2.1.0.4258.31.nupkg
    关键行（1.0.4258.31 头文件）：
      3000-3002  Loader STDAPI 三导出原型；
      3015/3019  IID_ICoreWebView2 / MIDL_INTERFACE；
      5314/5318  IID_ICoreWebView2_2（新增 7 方法：5322-5343）；
      6126/6130  IID_ICoreWebView2_3（新增 5 方法：6134-6148，
                 SetVirtualHostNameToFolderMapping/Clear... 在 _3 而非
                 _2 —— 任务书称“_2(SetVirtualHostNameToFolderMapping/Get)”
                 与头文件不符，实现以官方头文件为准；不存在 Get 方法，
                 清除映射用 ClearVirtualHostNameToFolderMapping）；
      39207/39211 IID_ICoreWebView2Controller（23 方法，39215-39286）；
      44181/44185 IID_ICoreWebView2Environment（5 方法，44189-44208）；
      44407/44411 IID_...ControllerCompletedHandler（Invoke 44415-44417）；
      44493/44497 IID_...EnvironmentCompletedHandler（Invoke 44501-44503）；
      63915/63919 IID_ICoreWebView2Settings（18 方法，63923-63975）；
      67571/67575 IID_ICoreWebView2WebMessageReceivedEventArgs
                 （3 方法，67579-67586）。
  - unknwn.h：IUnknown 三方法 QueryInterface/AddRef/Release 恒为 vtable
    前 3 槽；IID_IUnknown = {00000000-0000-0000-C000-000000000046}；
  - objbase.h/objidl.h：CoInitializeEx；COINIT_MULTITHREADED=0x0（MTA）、
    COINIT_APARTMENTTHREADED=0x2（STA）——任务书“0x2 MTA”为笔误，0x2 是
    STA（APARTMENTTHREADED），以头文件常量名为准；
  - winuser.h：MSG/WM_QUIT(0x0012)/PM_REMOVE(1)/SetTimer(hWnd=NULL 的
    线程定时器，由 DispatchMessage 回调 TimerProc)/PeekMessageW；
  - Microsoft Learn WebView2 文档（环境完成回调在“创建线程”的消息泵
    上回调）：
    https://learn.microsoft.com/microsoft-edge/webview2/reference/win32/
    webview2-idl#createcorewebview2environmentwithoptions

约束：只允许 ctypes/ctypes.wintypes/sys/json/os/argparse/struct；
threading.Event 为任务第 4 条明确要求。不导入 comtypes/pywin32。
代码风格与 wv2_native.py（Task 2 已验收）保持一致：显式
argtypes/restype、模块级回调保活、EVIDENCE JSON 行、时间戳双源。
"""
import ctypes
from ctypes import wintypes
import sys
import os
import json
import argparse
import threading

# ============================================================================
# HRESULT / COM / 消息常量（unknwn.h、winerror.h、objbase.h、winuser.h）
# ============================================================================

S_OK = 0
S_FALSE = 1
E_NOTIMPL = 0x80004001
E_NOINTERFACE = 0x80004002
E_POINTER = 0x80004003
E_UNEXPECTED = 0x8000FFFF
E_ABORT = 0x80004004
RPC_E_CHANGED_MODE = 0x80010106

# objbase.h COINIT（注意：0x2 是 STA，不是 MTA；MTA=0x0）
COINIT_MULTITHREADED = 0x0
COINIT_APARTMENTTHREADED = 0x2

WM_QUIT = 0x0012
PM_REMOVE = 1

# 看门狗超时退出码（WM_QUIT.wParam），与正常完成的 0 区分
WATCHDOG_EXIT_CODE = 98

# wProcessorArchitecture（sysinfoapi.h，与 wv2_native.py 一致）
_PROC_ARCH = {0: "x86", 5: "ARM", 6: "IA64", 9: "AMD64", 12: "ARM64"}

# ============================================================================
# GUID（rpcsal/guiddef.h：c_ulong + 2*c_ushort + 8*c_ubyte，16 字节）
# ============================================================================


class GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_ulong),
                ("Data2", ctypes.c_ushort),
                ("Data3", ctypes.c_ushort),
                ("Data4", ctypes.c_ubyte * 8)]


def make_guid(text):
    """把 "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx" 包成 Windows GUID。"""
    parts = text.strip().lower().split("-")
    if (len(parts) != 5 or len(parts[0]) != 8
            or len(parts[1]) != 4 or len(parts[2]) != 4
            or len(parts[3]) != 4 or len(parts[4]) != 12):
        raise ValueError("非法 GUID 字符串：%r" % text)
    tail = bytes(int(parts[3][i:i + 2], 16) for i in (0, 2))
    tail += bytes(int(parts[4][i:i + 2], 16) for i in range(0, 12, 2))
    return GUID(int(parts[0], 16), int(parts[1], 16), int(parts[2], 16),
                (ctypes.c_ubyte * 8)(*tail))


def guid_raw(guid):
    """GUID 的 16 字节线格式，用于 QueryInterface 内存比较。"""
    return ctypes.string_at(ctypes.byref(guid), 16)


# ---- IID（来源见模块 docstring 的 WebView2.h 行号清单） -------------------
IID_IUnknown = make_guid("00000000-0000-0000-C000-000000000046")
IID_ICoreWebView2 = make_guid("76eceacb-0462-4d94-ac83-423a6793775e")
IID_ICoreWebView2_2 = make_guid("9e8f0cf8-e670-4b5e-b2bc-73e061e3184c")
IID_ICoreWebView2_3 = make_guid("a0d6df20-3b92-416d-aa0c-437a9c727857")
IID_ICoreWebView2Controller = make_guid(
    "4d00c0d1-9434-4eb6-8078-8697a560334f")
IID_ICoreWebView2Environment = make_guid(
    "b96d755e-0319-4e92-a296-23436f46a1fc")
IID_ICoreWebView2Settings = make_guid(
    "e562e4f0-d7fa-43ac-8d71-c05150499f00")
IID_ICoreWebView2WebMessageReceivedEventArgs = make_guid(
    "0f99a40c-e962-4207-9e92-e3d542eff849")
IID_ICoreWebView2CreateCoreWebView2ControllerCompletedHandler = make_guid(
    "6c4819f3-c9b7-4260-8127-c9f5bde7f68c")
IID_ICoreWebView2CreateCoreWebView2EnvironmentCompletedHandler = make_guid(
    "4e8a3389-c9d8-4bd2-b6b5-124fee6cc14d")
# objidl.h：跨套间回调所需的两个 COM 内建支持
# IID_IMarshal = {00000003-0000-0000-C000-000000000046}
IID_IMarshal = make_guid("00000003-0000-0000-C000-000000000046")
# IID_IAgileObject = {94EA2B94-E9CC-49E0-C0FF-EE64CA8F507F}（纯标记接口）
IID_IAgileObject = make_guid("94ea2b94-e9cc-49e0-c0ff-ee64ca8f507f")

# ============================================================================
# vtable 类型
#
# COM 方法 ABI 为 STDMETHODCALLTYPE（x86 上 stdcall；x64/ARM64 只有一种
# 调用约定，CFUNCTYPE 与 WINFUNCTYPE 等价）。本机为 ARM64，按官方语义
# 使用 WINFUNCTYPE。所有 COM 方法首参为 this 指针，这里统一 c_void_p。
# ============================================================================

FN_QueryInterface = ctypes.WINFUNCTYPE(
    ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)
FN_AddRef = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)
FN_Release = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)
# 完成 handler 的 Invoke(this, HRESULT, result-ptr)。
# 实测（ARM64 真机，CPython 3.12 原生 ARM64，探针 _probe_regs 起逐轮
# 取证）：native 实际按 (x0=this, x1=hr, x2=result, x3=运行时内部值)
# 传参，x3 恒指向 EmbeddedBrowserWebView.dll 的 rdata（语义未公开），
# 故 4 个参数全部声明为 c_void_p 宽度并显式接收 x3；hr 进 Python 后按
# 32 位无符号截取。另一个必须处理的 ctypes 封送规则：c_void_p 形参在
# 寄存器值为 0 时传入的是 None 而非 0（hr=S_OK 即如此），回调内必须
# int(hr or 0) 转换；直接 int(hr) 会抛 TypeError 并被回调边界吞掉，
# 表现为 Invoke 静默失败（本轮调试的实际根因）。
FN_CompletedInvoke = ctypes.WINFUNCTYPE(
    ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p,
    ctypes.c_void_p, ctypes.c_void_p)


class IUnknownVtbl(ctypes.Structure):
    # unknwn.h IUnknown：所有 vtable 的前 3 槽
    _fields_ = [("QueryInterface", FN_QueryInterface),
                ("AddRef", FN_AddRef),
                ("Release", FN_Release)]


class ICoreWebView2CreateCoreWebView2EnvironmentCompletedHandlerVtbl(
        ctypes.Structure):
    # WebView2.h 44510-44536（C-style vtable）
    _fields_ = [("QueryInterface", FN_QueryInterface),
                ("AddRef", FN_AddRef),
                ("Release", FN_Release),
                ("Invoke", FN_CompletedInvoke)]


class ICoreWebView2CreateCoreWebView2ControllerCompletedHandlerVtbl(
        ctypes.Structure):
    # WebView2.h 44424-44450；本任务不实例化，Task 4 创建控制器时用
    _fields_ = [("QueryInterface", FN_QueryInterface),
                ("AddRef", FN_AddRef),
                ("Release", FN_Release),
                ("Invoke", FN_CompletedInvoke)]


# ---- 以下接口本任务只接收指针/占槽，不调用（Task 4 起逐个补签名） --------
# 方法名严格按 WebView2.h C++ 段 virtual 声明顺序排列；c_void_p 仅表示
# “一个指针宽度的 vtable 槽”，Task 4 使用具体方法时把该槽替换成对应
# WINFUNCTYPE 原型即可（槽位序号不变）。

# ICoreWebView2Environment（44189-44208）：5 个自有方法
_ENV_METHODS = [
    "CreateCoreWebView2Controller",     # HWND, handler*
    "CreateWebResourceResponse",        # IStream*, int, LPCWSTR, LPCWSTR, **
    "get_BrowserVersionString",         # LPWSTR*
    "add_NewBrowserVersionAvailable",   # handler*, EventRegistrationToken*
    "remove_NewBrowserVersionAvailable",  # EventRegistrationToken
]

# ICoreWebView2Controller（39215-39286）：23 个自有方法
_CONTROLLER_METHODS = [
    "get_IsVisible", "put_IsVisible",
    "get_Bounds", "put_Bounds",
    "get_ZoomFactor", "put_ZoomFactor",
    "add_ZoomFactorChanged", "remove_ZoomFactorChanged",
    "SetBoundsAndZoomFactor", "MoveFocus",
    "add_MoveFocusRequested", "remove_MoveFocusRequested",
    "add_GotFocus", "remove_GotFocus",
    "add_LostFocus", "remove_LostFocus",
    "add_AcceleratorKeyPressed", "remove_AcceleratorKeyPressed",
    "get_ParentWindow", "put_ParentWindow",
    "NotifyParentWindowPositionChanged", "Close", "get_CoreWebView2",
]

# ICoreWebView2（3023-3215）：58 个自有方法
_WEBVIEW2_METHODS = [
    "get_Settings", "get_Source", "Navigate", "NavigateToString",
    "add_NavigationStarting", "remove_NavigationStarting",
    "add_ContentLoading", "remove_ContentLoading",
    "add_SourceChanged", "remove_SourceChanged",
    "add_HistoryChanged", "remove_HistoryChanged",
    "add_NavigationCompleted", "remove_NavigationCompleted",
    "add_FrameNavigationStarting", "remove_FrameNavigationStarting",
    "add_FrameNavigationCompleted", "remove_FrameNavigationCompleted",
    "add_ScriptDialogOpening", "remove_ScriptDialogOpening",
    "add_PermissionRequested", "remove_PermissionRequested",
    "add_ProcessFailed", "remove_ProcessFailed",
    "AddScriptToExecuteOnDocumentCreated",
    "RemoveScriptToExecuteOnDocumentCreated",
    "ExecuteScript", "CapturePreview", "Reload",
    "PostWebMessageAsJson", "PostWebMessageAsString",
    "add_WebMessageReceived", "remove_WebMessageReceived",
    "CallDevToolsProtocolMethod", "get_BrowserProcessId",
    "get_CanGoBack", "get_CanGoForward", "GoBack", "GoForward",
    "GetDevToolsProtocolEventReceiver", "Stop",
    "add_NewWindowRequested", "remove_NewWindowRequested",
    "add_DocumentTitleChanged", "remove_DocumentTitleChanged",
    "get_DocumentTitle",
    "AddHostObjectToScript", "RemoveHostObjectFromScript",
    "OpenDevToolsWindow",
    "add_ContainsFullScreenElementChanged",
    "remove_ContainsFullScreenElementChanged",
    "get_ContainsFullScreenElement",
    "add_WebResourceRequested", "remove_WebResourceRequested",
    "AddWebResourceRequestedFilter", "RemoveWebResourceRequestedFilter",
    "add_WindowCloseRequested", "remove_WindowCloseRequested",
]

# ICoreWebView2_2 在 ICoreWebView2 之上新增 7 方法（5322-5343）
_WEBVIEW2_2_METHODS = _WEBVIEW2_METHODS + [
    "add_WebResourceResponseReceived", "remove_WebResourceResponseReceived",
    "NavigateWithWebResourceRequest",
    "add_DOMContentLoaded", "remove_DOMContentLoaded",
    "get_CookieManager", "get_Environment",
]

# ICoreWebView2_3 在 _2 之上新增 5 方法（6134-6148）；虚拟主机映射在此
_WEBVIEW2_3_METHODS = _WEBVIEW2_2_METHODS + [
    "TrySuspend", "Resume", "get_IsSuspended",
    "SetVirtualHostNameToFolderMapping",
    "ClearVirtualHostNameToFolderMapping",
]

# ICoreWebView2Settings（63923-63975）：9 对 get/put，18 个自有方法
_SETTINGS_METHODS = [
    "get_IsScriptEnabled", "put_IsScriptEnabled",
    "get_IsWebMessageEnabled", "put_IsWebMessageEnabled",
    "get_AreDefaultScriptDialogsEnabled",
    "put_AreDefaultScriptDialogsEnabled",
    "get_IsStatusBarEnabled", "put_IsStatusBarEnabled",
    "get_AreDevToolsEnabled", "put_AreDevToolsEnabled",
    "get_AreDefaultContextMenusEnabled",
    "put_AreDefaultContextMenusEnabled",
    "get_AreHostObjectsAllowed", "put_AreHostObjectsAllowed",
    "get_IsZoomControlEnabled", "put_IsZoomControlEnabled",
    "get_IsBuiltInErrorPageEnabled", "put_IsBuiltInErrorPageEnabled",
]

# ICoreWebView2WebMessageReceivedEventArgs（67579-67586）：3 个自有方法
_WEB_MESSAGE_ARGS_METHODS = [
    "get_Source", "get_WebMessageAsJson", "TryGetWebMessageAsString",
]


def _declare_vtbl(name, own_methods):
    """生成“IUnknown 三槽（带签名）+ N 个 c_void_p 占位槽”的 vtable。

    槽位总数 = 3 + len(own_methods)；字段顺序即头文件 vtable 顺序。
    """
    fields = [("QueryInterface", FN_QueryInterface),
              ("AddRef", FN_AddRef),
              ("Release", FN_Release)]
    fields += [(mname, ctypes.c_void_p) for mname in own_methods]
    return type(name, (ctypes.Structure,), {"_fields_": fields,
                                            "_own_count": len(own_methods)})


ICoreWebView2EnvironmentVtbl = _declare_vtbl(
    "ICoreWebView2EnvironmentVtbl", _ENV_METHODS)
ICoreWebView2ControllerVtbl = _declare_vtbl(
    "ICoreWebView2ControllerVtbl", _CONTROLLER_METHODS)
ICoreWebView2Vtbl = _declare_vtbl(
    "ICoreWebView2Vtbl", _WEBVIEW2_METHODS)
ICoreWebView2_2Vtbl = _declare_vtbl(
    "ICoreWebView2_2Vtbl", _WEBVIEW2_2_METHODS)
ICoreWebView2_3Vtbl = _declare_vtbl(
    "ICoreWebView2_3Vtbl", _WEBVIEW2_3_METHODS)
ICoreWebView2SettingsVtbl = _declare_vtbl(
    "ICoreWebView2SettingsVtbl", _SETTINGS_METHODS)
ICoreWebView2WebMessageReceivedEventArgsVtbl = _declare_vtbl(
    "ICoreWebView2WebMessageReceivedEventArgsVtbl",
    _WEB_MESSAGE_ARGS_METHODS)

# 槽位自检：数量若与头文件不符，import 即失败（防漏槽/错序）
_EXPECTED_SLOTS = {
    "ICoreWebView2Environment": 8,
    "ICoreWebView2Controller": 26,
    "ICoreWebView2": 61,
    "ICoreWebView2_2": 68,
    "ICoreWebView2_3": 73,
    "ICoreWebView2Settings": 21,
    "ICoreWebView2WebMessageReceivedEventArgs": 6,
    "ICoreWebView2CreateCoreWebView2ControllerCompletedHandler": 4,
    "ICoreWebView2CreateCoreWebView2EnvironmentCompletedHandler": 4,
}
_VTBL_TYPES = {
    "ICoreWebView2Environment": ICoreWebView2EnvironmentVtbl,
    "ICoreWebView2Controller": ICoreWebView2ControllerVtbl,
    "ICoreWebView2": ICoreWebView2Vtbl,
    "ICoreWebView2_2": ICoreWebView2_2Vtbl,
    "ICoreWebView2_3": ICoreWebView2_3Vtbl,
    "ICoreWebView2Settings": ICoreWebView2SettingsVtbl,
    "ICoreWebView2WebMessageReceivedEventArgs":
        ICoreWebView2WebMessageReceivedEventArgsVtbl,
    "ICoreWebView2CreateCoreWebView2ControllerCompletedHandler":
        ICoreWebView2CreateCoreWebView2ControllerCompletedHandlerVtbl,
    "ICoreWebView2CreateCoreWebView2EnvironmentCompletedHandler":
        ICoreWebView2CreateCoreWebView2EnvironmentCompletedHandlerVtbl,
}
for _nm, _tp in _VTBL_TYPES.items():
    _got = ctypes.sizeof(_tp) // ctypes.sizeof(ctypes.c_void_p)
    assert _got == _EXPECTED_SLOTS[_nm], (
        "%s vtable 槽位 %d != 官方头文件 %d"
        % (_nm, _got, _EXPECTED_SLOTS[_nm]))


def vtbl_slot_counts():
    """返回 {接口名: 槽位数}，供 harness EVIDENCE 取证。"""
    return {nm: ctypes.sizeof(tp) // ctypes.sizeof(ctypes.c_void_p)
            for nm, tp in _VTBL_TYPES.items()}


# ============================================================================
# 手写 COM 回调对象（Python 侧实现的 COM 接口）
#
# 参考做法（Structure 首字段 lpVtbl + 静态 CFUNCTYPE/WINFUNCTYPE vtable）：
#   官方/社区惯用 ctypes COM 单对象布局，见 Python ctypes 文档“Callback
#   functions”与 comtypes 的 ComObject 内存布局（首槽恒为 lpVtbl）。
# 生命周期保活：
#   - WINFUNCTYPE 回调对象与 vtable 结构均为模块级全局（见本类之后），
#     native 持有的函数指针在进程生命期内永不悬垂；
#   - 实例结构按 addressof(self) 登记进 _LIVE，字典同时持有 Python 引用，
#     防 GC 回收导致 native 访问已释放内存。
# ============================================================================

# this 地址 -> Python 对象（进程级，不主动摘除；任务短生命周期，安全简单）
_LIVE = {}
# 供排查：记录 native 端 AddRef/Release 轨迹
QI_CALLS = []


class EnvCompletedHandler(ctypes.Structure):
    """ICoreWebView2CreateCoreWebView2EnvironmentCompletedHandler 的
    Python 实现（vtable 4 槽：QI/AddRef/Release/Invoke）。"""

    _fields_ = [("lpVtbl", ctypes.c_void_p)]

    def __init__(self, round_no):
        super().__init__()
        self.round_no = int(round_no)
        self.refcount = 0
        self.event = threading.Event()
        self.invoke_hr = None          # Invoke 收到的 HRESULT（unsigned 视角）
        self.env_ptr = 0               # ICoreWebView2Environment* 原始指针
        self.env_addref_rc = -1        # 回调内 AddRef 返回的引用计数
        self.invoke_thread_id = 0      # 回调实际投递到的线程
        self.invoke_tick_ms = 0
        # CoCreateFreeThreadedMarshaler 实例（首次 QI(IMarshal) 时创建）
        self.ftm_ptr = 0
        self.qi_iids = []              # 实测被运行时 QI 过的 IID（取证）
        self.lpVtbl = ctypes.cast(ctypes.byref(_ENV_HANDLER_VTBL),
                                  ctypes.c_void_p)
        _LIVE[ctypes.addressof(self)] = self

    def as_com_ptr(self):
        return ctypes.c_void_p(ctypes.addressof(self))


# ---- IUnknown 三方法 + Invoke 的模块级回调（全部吞异常，绝不允许 Python
#      异常穿过 native 调用边界导致进程静默崩溃） ---------------------------


def _find_live(this):
    return _LIVE.get(int(this))


def _iid_text(raw16):
    """16 字节线格式 GUID -> 规范小写字符串（仅用于取证日志）。"""
    d1 = int.from_bytes(raw16[0:4], "little")
    d2 = int.from_bytes(raw16[4:6], "little")
    d3 = int.from_bytes(raw16[6:8], "little")
    return "%08x-%04x-%04x-%02x%02x-%s" % (
        d1, d2, d3, raw16[8], raw16[9],
        "".join("%02x" % b for b in raw16[10:16]))


def _env_handler_query_interface(this, riid, ppv):
    try:
        if not riid or not ppv:
            return E_POINTER
        asked = ctypes.string_at(riid, 16)
        out = ctypes.cast(ppv, ctypes.POINTER(ctypes.c_void_p))
        obj = _find_live(this)
        if obj is None:
            out[0] = None
            return E_NOINTERFACE
        if len(obj.qi_iids) < 64:
            obj.qi_iids.append(_iid_text(asked))
        QI_CALLS.append(("qi", int(this), _iid_text(asked)))
        # 自身接口 IID 与 IID_IUnknown（COM 规则：QI(IUnknown) 必须
        # 返回同一指针），以及 IAgileObject 纯标记接口：直接返回本对象。
        if (asked == guid_raw(
                IID_ICoreWebView2CreateCoreWebView2EnvironmentCompletedHandler)
                or asked == guid_raw(IID_IUnknown)
                or asked == guid_raw(IID_IAgileObject)):
            obj.refcount += 1
            out[0] = ctypes.c_void_p(int(this))
            return S_OK
        # IID_IMarshal：返回进程内自由线程封送器（FTM）。FTM 创建时
        # 会 AddRef 外层对象（即本回调），缓存在实例上复用；重复 QI
        # 时对返回的封送器做一次 AddRef，由调用方 Release 平衡。
        if asked == guid_raw(IID_IMarshal):
            if not obj.ftm_ptr:
                ftm = ctypes.c_void_p(0)
                mhr = int(ole32.CoCreateFreeThreadedMarshaler(
                    ctypes.c_void_p(int(this)),
                    ctypes.byref(ftm))) & 0xFFFFFFFF
                if mhr != S_OK or not ftm.value:
                    out[0] = None
                    return E_NOINTERFACE
                obj.ftm_ptr = int(ftm.value)
            else:
                com_addref(obj.ftm_ptr)
            out[0] = ctypes.c_void_p(obj.ftm_ptr)
            return S_OK
        out[0] = None
        return E_NOINTERFACE
    except Exception:
        return E_UNEXPECTED


def _env_handler_add_ref(this):
    try:
        obj = _find_live(this)
        if obj is not None:
            obj.refcount += 1
            return obj.refcount
        return 1
    except Exception:
        return 0


def _env_handler_release(this):
    try:
        obj = _find_live(this)
        if obj is None:
            return 0
        obj.refcount -= 1
        if obj.refcount < 0:
            obj.refcount = 0
        # 实例不随 refcount=0 释放（_LIVE 持续持有到进程退出），避免
        # native 仍可能回调时内存已归还 Python 分配器。
        return obj.refcount
    except Exception:
        return 0


def _env_handler_invoke(this, hr, env_ptr, _reserved):
    try:
        obj = _find_live(this)
        if obj is None:
            return E_UNEXPECTED
        env_raw = int(env_ptr or 0)
        # 注意：c_void_p 形参在值为 0 时由 ctypes 以 None 传入（不是 0），
        # 不能直接 int(hr)——S_OK(0) 会抛 TypeError 被外层吞掉，表现为
        # Invoke 静默失败（本轮调试的实际根因）。
        obj.invoke_hr = int(hr or 0) & 0xFFFFFFFF
        obj.invoke_thread_id = kernel32.GetCurrentThreadId()
        obj.invoke_tick_ms = int(kernel32.GetTickCount64())
        # 关键生命周期（实测探针 _probe_patch/_probe_life）：
        # 该运行时在回调返回后立即释放它对环境对象的内部引用，对象内存
        # 很快被堆复用（同一地址先后读到合法 vtable、0x...000002CD、
        # 栈 Cookie 形态数据）。因此 result 指针只在本次 Invoke 栈帧内
        # 有效，必须在回调内 AddRef 持引用，结束时由创建线程 Release
        # 平衡。AddRef 与指针记录都必须在本函数内、回调返回前完成。
        obj.env_ptr = env_raw
        obj.env_addref_rc = -1
        if env_raw and obj.invoke_hr == S_OK:
            obj.env_addref_rc = com_addref(env_raw)
        # 先置事件，再退出消息循环（顺序与任务要求一致）
        obj.event.set()
        # 向“创建线程”投递 WM_QUIT(0)：STA 下本回调就在创建线程
        # （实测在 Create...WithOptions 返回前同步触发，WM_QUIT 留在
        # 队列，随后 GetMessage 立即取出）；MTA 下若回调落到 COM 工作
        # 线程，也能唤醒创建线程的 GetMessage。
        user32.PostThreadMessageW(_CREATOR_THREAD_ID, WM_QUIT, 0, 0)
        return S_OK
    except Exception:
        return E_UNEXPECTED


# 模块级 WINFUNCTYPE 实例 + vtable（保活，见上文字说明）
_env_qi_fn = FN_QueryInterface(_env_handler_query_interface)
_env_addref_fn = FN_AddRef(_env_handler_add_ref)
_env_release_fn = FN_Release(_env_handler_release)
_env_invoke_fn = FN_CompletedInvoke(_env_handler_invoke)

_ENV_HANDLER_VTBL = (
    ICoreWebView2CreateCoreWebView2EnvironmentCompletedHandlerVtbl(
        QueryInterface=_env_qi_fn,
        AddRef=_env_addref_fn,
        Release=_env_release_fn,
        Invoke=_env_invoke_fn))

# ============================================================================
# Win32 绑定（ole32 / user32 / kernel32；声明风格同 wv2_native.py）
# ============================================================================

ole32 = ctypes.WinDLL("ole32", use_last_error=True)
user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
ole32.CoInitializeEx.restype = ctypes.c_long
ole32.CoUninitialize.argtypes = []
ole32.CoUninitialize.restype = None
ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
ole32.CoTaskMemFree.restype = None
# objidl.h/ combase：自由线程封送器（FTM）。回调对象在 QI(IID_IMarshal)
# 时返回它，COM 跨套间（STA 创建线程 <-> 运行时内部 MTA 线程）传递
# 本回调时按“进程内直接指针”封送，否则 WebView2Loader 拿不到可封送的
# handler，Invoke 永远不会投递（实测：对 IMarshal 返回 E_NOINTERFACE
# 时两轮均看门狗超时；WRL RuntimeClass/FTM 是官方样例的等价做法）。
ole32.CoCreateFreeThreadedMarshaler.argtypes = [
    ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
ole32.CoCreateFreeThreadedMarshaler.restype = ctypes.c_long


class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class MSG(ctypes.Structure):
    # winuser.h tagMSG（与 wv2_native.py 同一布局）
    _fields_ = [("hwnd", wintypes.HWND),
                ("message", wintypes.UINT),
                ("wParam", wintypes.WPARAM),
                ("lParam", wintypes.LPARAM),
                ("time", wintypes.DWORD),
                ("pt", POINT)]


class SYSTEMTIME(ctypes.Structure):
    _fields_ = [("wYear", wintypes.WORD), ("wMonth", wintypes.WORD),
                ("wDayOfWeek", wintypes.WORD), ("wDay", wintypes.WORD),
                ("wHour", wintypes.WORD), ("wMinute", wintypes.WORD),
                ("wSecond", wintypes.WORD),
                ("wMilliseconds", wintypes.WORD)]


class _SYSTEM_INFO_UNION(ctypes.Union):
    _fields_ = [("dwOemId", wintypes.DWORD),
                ("wProcessorArchitecture", wintypes.WORD),
                ("wReserved", wintypes.WORD)]


class SYSTEM_INFO(ctypes.Structure):
    _fields_ = [("u", _SYSTEM_INFO_UNION),
                ("dwPageSize", wintypes.DWORD),
                ("lpMinimumApplicationAddress", wintypes.LPVOID),
                ("lpMaximumApplicationAddress", wintypes.LPVOID),
                ("dwActiveProcessorMask", ctypes.c_size_t),
                ("dwNumberOfProcessors", wintypes.DWORD),
                ("dwProcessorType", wintypes.DWORD),
                ("dwAllocationGranularity", wintypes.DWORD),
                ("wProcessorLevel", wintypes.WORD),
                ("wProcessorRevision", wintypes.WORD)]


user32.GetMessageW.argtypes = [ctypes.POINTER(MSG), wintypes.HWND,
                               wintypes.UINT, wintypes.UINT]
user32.GetMessageW.restype = wintypes.BOOL
user32.PeekMessageW.argtypes = [ctypes.POINTER(MSG), wintypes.HWND,
                                wintypes.UINT, wintypes.UINT, wintypes.UINT]
user32.PeekMessageW.restype = wintypes.BOOL
user32.TranslateMessage.argtypes = [ctypes.POINTER(MSG)]
user32.TranslateMessage.restype = wintypes.BOOL
user32.DispatchMessageW.argtypes = [ctypes.POINTER(MSG)]
user32.DispatchMessageW.restype = ctypes.c_ssize_t
user32.PostQuitMessage.argtypes = [ctypes.c_int]
user32.PostQuitMessage.restype = None
user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT,
                                      wintypes.WPARAM, wintypes.LPARAM]
user32.PostThreadMessageW.restype = wintypes.BOOL
# hWnd=NULL 的 SetTimer 为线程定时器：WM_TIMER 进线程队列，DispatchMessage
# 时直接调用 TimerProc（winuser.h 文档），无需窗口。
user32.SetTimer.argtypes = [wintypes.HWND, ctypes.c_size_t, wintypes.UINT,
                            ctypes.c_void_p]
user32.SetTimer.restype = ctypes.c_size_t
user32.KillTimer.argtypes = [wintypes.HWND, ctypes.c_size_t]
user32.KillTimer.restype = wintypes.BOOL

kernel32.GetCurrentThreadId.argtypes = []
kernel32.GetCurrentThreadId.restype = wintypes.DWORD
kernel32.GetTickCount64.argtypes = []
kernel32.GetTickCount64.restype = ctypes.c_ulonglong
kernel32.GetLocalTime.argtypes = [ctypes.POINTER(SYSTEMTIME)]
kernel32.GetLocalTime.restype = None
kernel32.GetNativeSystemInfo.argtypes = [ctypes.POINTER(SYSTEM_INFO)]
kernel32.GetNativeSystemInfo.restype = None

# TimerProc：VOID CALLBACK(HWND, UINT, UINT_PTR, DWORD)
FN_TimerProc = ctypes.WINFUNCTYPE(
    None, wintypes.HWND, wintypes.UINT, ctypes.c_size_t, wintypes.DWORD)


def _watchdog_timer_proc(_hwnd, _msg, _idevent, _tick):
    # 仅在事件尚未完成时投递看门狗 WM_QUIT（wParam=98）
    try:
        user32.PostQuitMessage(WATCHDOG_EXIT_CODE)
    except Exception:
        pass


# 模块级保活
_timer_proc_fn = FN_TimerProc(_watchdog_timer_proc)

# 创建线程 ID：main() 开始时写入，Invoke 跨线程回投 WM_QUIT 用
_CREATOR_THREAD_ID = 0

# ============================================================================
# HRESULT 工具
# ============================================================================


def hr_name(hr):
    """0 成功；失败按 0x%08X（无符号视角）格式化。"""
    hr = int(hr) & 0xFFFFFFFF
    if hr == S_OK:
        return "S_OK(0x00000000)"
    known = {S_FALSE: "S_FALSE", E_NOINTERFACE: "E_NOINTERFACE",
             E_POINTER: "E_POINTER", E_UNEXPECTED: "E_UNEXPECTED",
             E_ABORT: "E_ABORT", RPC_E_CHANGED_MODE: "RPC_E_CHANGED_MODE"}
    return "%s(0x%08X)" % (known.get(hr, "HRESULT"), hr)


def check_hr(hr, what):
    hr = int(hr) & 0xFFFFFFFF
    if hr != S_OK:
        raise OSError("%s 失败：%s" % (what, hr_name(hr)))
    return hr


def native_machine():
    """(架构代码, 架构名)，取自 GetNativeSystemInfo，同 wv2_native.py。"""
    si = SYSTEM_INFO()
    kernel32.GetNativeSystemInfo(ctypes.byref(si))
    code = int(si.u.wProcessorArchitecture)
    return code, _PROC_ARCH.get(code, "UNKNOWN(%d)" % code)


def local_time_iso():
    st = SYSTEMTIME()
    kernel32.GetLocalTime(ctypes.byref(st))
    return "%04d-%02d-%02dT%02d:%02d:%02d.%03d" % (
        st.wYear, st.wMonth, st.wDay, st.wHour, st.wMinute, st.wSecond,
        st.wMilliseconds)


def _com_slot_addr(ptr, slot):
    """标准 COM 二次解引用取方法地址：fn = *( (*obj) + 8*slot )。

    obj 的首 qword 是 lpVtbl（指向 rdata 中的函数指针数组），
    返回第 slot 槽里的函数入口裸地址（int）。

    实测（ARM64，探针 _probe_callidiom/_probe_life）：不能把 obj 直接
    cast 成 POINTER(IUnknownVtbl) 后用结构字段调用——那只会做一次
    解引用，拿到的“函数地址”其实是 lpVtbl 本身（rdata 不可执行），
    调用即 NX 访问违例（ctypes 报 access violation writing 该地址）。
    """
    lpvtbl = ctypes.cast(int(ptr),
                         ctypes.POINTER(ctypes.c_void_p))[0]
    return int(ctypes.cast(int(lpvtbl),
                           ctypes.POINTER(ctypes.c_void_p))[slot] or 0)


def com_addref(ptr):
    """对任意 IUnknown* 调 vtable[1] AddRef；返回 AddRef 后引用计数。"""
    ptr = int(ptr or 0)
    if not ptr:
        return -1
    fn = ctypes.cast(_com_slot_addr(ptr, 1), FN_AddRef)
    return int(fn(ctypes.c_void_p(ptr)))


def com_release(ptr):
    """对任意 IUnknown* 调 vtable[2] Release；返回释放后引用计数。"""
    ptr = int(ptr or 0)
    if not ptr:
        return -1
    fn = ctypes.cast(_com_slot_addr(ptr, 2), FN_Release)
    return int(fn(ctypes.c_void_p(ptr)))


# ============================================================================
# Loader
# ============================================================================


def load_webview2_loader(loader_path):
    """加载 WebView2Loader.dll，显式绑定两个导出；失败报清晰错误。"""
    abs_path = os.path.abspath(loader_path)
    if not os.path.isfile(abs_path):
        raise FileNotFoundError(
            "WebView2Loader.dll 不存在：%s（用 --loader 指定正确路径）"
            % abs_path)
    try:
        dll = ctypes.WinDLL(abs_path)
    except OSError as ex:
        arch_code, arch_name = native_machine()
        raise OSError(
            "加载 WebView2Loader.dll 失败：%s\n"
            "  路径：%s\n"
            "  本机原生架构：%s（指针宽度 %d 位）；请确认 DLL 与 Python "
            "同为该架构（任务 loader 已核验 PE Machine=0xAA64/ARM64），"
            "且其依赖的系统 DLL 齐全。原始错误：%r"
            % (abs_path, abs_path, arch_name,
               ctypes.sizeof(ctypes.c_void_p) * 8, ex))
    try:
        fn_ver = dll.GetAvailableCoreWebView2BrowserVersionString
        fn_create = dll.CreateCoreWebView2EnvironmentWithOptions
    except AttributeError as ex:
        raise AttributeError(
            "WebView2Loader.dll 缺少预期导出：%r（文件：%s）"
            % (ex, abs_path))
    # WebView2.h 3001-3002 STDAPI 原型
    fn_ver.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.LPWSTR)]
    fn_ver.restype = ctypes.c_long
    fn_create.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR,
                          ctypes.c_void_p, ctypes.c_void_p]
    fn_create.restype = ctypes.c_long
    return dll, abs_path


def get_browser_version(dll):
    """GetAvailableCoreWebView2BrowserVersionString(NULL, &v)。
    返回 (hresult, version-or-None)；输出串由 CoTaskMemFree 释放。

    注意（实测踩坑，ARM64 真机 0xC0000374）：不能对 c_wchar_p.value
    返回的 Python str 做 ctypes.cast(str, c_void_p)——cast 会成功，
    但给出的是 ctypes 入参“临时宽字符转换缓冲区”的悬垂地址，
    CoTaskMemFree 它等于释放野指针，必现堆破坏。正确做法是先从
    LPWSTR 槽里取出原始指针整数，wstring_at 读串，再释放该指针。"""
    vptr = wintypes.LPWSTR()
    hr = int(dll.GetAvailableCoreWebView2BrowserVersionString(
        None, ctypes.byref(vptr))) & 0xFFFFFFFF
    version = None
    raw = ctypes.cast(ctypes.byref(vptr),
                      ctypes.POINTER(ctypes.c_void_p))[0]
    raw = int(raw or 0)
    if hr == S_OK and raw:
        version = ctypes.wstring_at(raw)
        ole32.CoTaskMemFree(ctypes.c_void_p(raw))
    return hr, version


# ============================================================================
# 一轮环境创建/释放
# ============================================================================


def _purge_wm_quit():
    """清掉线程队列中残留的 WM_QUIT（防上一轮看门狗误投影响下一轮）。"""
    m = MSG()
    while user32.PeekMessageW(ctypes.byref(m), None, WM_QUIT, WM_QUIT,
                              PM_REMOVE):
        pass


def create_environment_round(dll, user_data_folder, round_no, timeout_ms):
    """一轮完整流程：创建（异步）-> 消息泵等 Invoke -> 取证 -> Release。

    返回证据 dict；失败抛 RuntimeError。
    """
    global _CREATOR_THREAD_ID
    _CREATOR_THREAD_ID = int(kernel32.GetCurrentThreadId())

    _purge_wm_quit()
    handler = EnvCompletedHandler(round_no)
    handler_ptr = handler.as_com_ptr()

    create_hr = int(dll.CreateCoreWebView2EnvironmentWithOptions(
        None,                    # browserExecutableFolder：用系统安装运行时
        os.path.abspath(user_data_folder),
        None,                    # environmentOptions：默认
        handler_ptr)) & 0xFFFFFFFF
    create_hr_text = hr_name(create_hr)

    evidence = {
        "event": "environment_completed",
        "round": round_no,
        "create_hresult": create_hr,
        "create_hresult_text": create_hr_text,
        "creator_thread_id": _CREATOR_THREAD_ID,
    }
    if create_hr != S_OK:
        raise RuntimeError("第 %d 轮 Create...WithOptions 返回 %s"
                           % (round_no, create_hr_text))

    # 看门狗：线程定时器，超时投递 WM_QUIT(98)
    timer_id = user32.SetTimer(None, 0, int(timeout_ms),
                               ctypes.cast(_timer_proc_fn, ctypes.c_void_p))
    if not timer_id:
        raise ctypes.WinError(ctypes.get_last_error())

    msg = MSG()
    loop_exit = None
    pump_count = 0
    try:
        while True:
            got = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if got == 0:                         # WM_QUIT
                loop_exit = int(msg.wParam)
                break
            if got == -1:                        # 错误
                loop_exit = -1
                break
            pump_count += 1
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
    finally:
        user32.KillTimer(None, timer_id)

    if not handler.event.is_set():
        raise RuntimeError(
            "第 %d 轮消息循环已退出（wParam=%r，派发消息 %d 条）但未收到 "
            "环境完成 Invoke（套间/消息泵异常或看门狗超时）"
            % (round_no, loop_exit, pump_count))

    if handler.invoke_hr != S_OK:
        raise RuntimeError(
            "第 %d 轮 Invoke HRESULT=%s（回调线程 %d，创建线程 %d，"
            "同线程=%s）"
            % (round_no, hr_name(handler.invoke_hr),
               handler.invoke_thread_id, _CREATOR_THREAD_ID,
               handler.invoke_thread_id == _CREATOR_THREAD_ID))
    if not handler.env_ptr:
        raise RuntimeError("第 %d 轮 Invoke 成功但 env_ptr=0" % round_no)
    if handler.env_addref_rc < 0:
        raise RuntimeError("第 %d 轮未在 Invoke 回调内完成 env AddRef"
                           % round_no)

    # 证据（原始指针如实记录，不做归一化）
    evidence.update({
        "hresult": handler.invoke_hr,
        "hresult_text": hr_name(handler.invoke_hr),
        "env_ptr": handler.env_ptr,
        "env_ptr_nonzero": handler.env_ptr != 0,
        "env_addref_rc_in_invoke": handler.env_addref_rc,
        "invoke_thread_id": handler.invoke_thread_id,
        "invoke_on_creator_thread":
            handler.invoke_thread_id == _CREATOR_THREAD_ID,
        "msg_loop_wparam": loop_exit,
        "msg_pumped": pump_count,
        "handler_refcount_at_invoke": handler.refcount,
        "handler_qi_iids": list(handler.qi_iids),
        "free_threaded_marshaler_ptr": handler.ftm_ptr,
        "ts_local": local_time_iso(),
        "tick_ms": handler.invoke_tick_ms,
    })

    # 释放平衡：Invoke 回调内 AddRef 过一次（对象因此存活到这里），
    # 现在 Release 一次平衡；此后环境对象由运行时内部引用决定生死。
    # handler 由 native 自行 Release（refcount 轨迹留证）。
    ref_after = com_release(handler.env_ptr)
    evidence["env_refcount_after_release"] = ref_after
    evidence["handler_refcount_final"] = handler.refcount
    return evidence


# ============================================================================
# EVIDENCE / harness
# ============================================================================


def emit(obj):
    """打印一行 EVIDENCE: 前缀 JSON（ASCII 安全，避免重定向编码问题），
    风格同 wv2_native.py。"""
    sys.stdout.write("EVIDENCE: "
                     + json.dumps(obj, ensure_ascii=True, default=str) + "\n")
    sys.stdout.flush()


def main(argv=None):
    here = os.path.dirname(os.path.abspath(__file__))
    default_loader = os.path.join(
        here, "webview2_arm64", "runtimes", "win-arm64", "native",
        "WebView2Loader.dll")
    default_user_data = (
        r"c:\Users\xqz\Documents\trae_projects\dayly"
        r"\.trae\team\evidence\T03\wv2data")

    parser = argparse.ArgumentParser(
        description="Task 3 WebView2 COM 环境创建 harness（ctypes 手写 COM）")
    parser.add_argument("--loader", default=default_loader,
                        help="WebView2Loader.dll 路径（默认仓库内 arm64）")
    parser.add_argument("--user-data", default=default_user_data,
                        help="WebView2 用户数据目录（默认 T03 证据/wv2data）")
    parser.add_argument("--rounds", type=int, default=2,
                        help="连续创建/释放环境的轮数（默认 2）")
    parser.add_argument("--timeout", type=float, default=30.0,
                        help="单轮等待 Invoke 的看门狗秒数（默认 30）")
    parser.add_argument("--apartment", choices=["sta", "mta"], default="sta",
                        help="CoInitializeEx 套间：sta=0x2（默认）/mta=0x0")
    args = parser.parse_args(argv)

    if sys.platform != "win32":
        sys.stderr.write("wv2_com 仅支持 Windows\n")
        return 2
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    arch_code, arch_name = native_machine()
    coinit = (COINIT_APARTMENTTHREADED if args.apartment == "sta"
              else COINIT_MULTITHREADED)

    emit({"event": "start",
          "task": "T03-webview2-com-environment",
          "python": sys.version.split()[0],
          "pointer_bits": ctypes.sizeof(ctypes.c_void_p) * 8,
          "platform_machine": arch_name,
          "machine_arch_code": arch_code,
          "loader_requested": args.loader,
          "apartment": args.apartment,
          "coinit_flag": coinit,
          "rounds": args.rounds,
          "timeout_s": args.timeout,
          "vtbl_slots": vtbl_slot_counts()})

    try:
        dll, loader_abs = load_webview2_loader(args.loader)
    except Exception as ex:
        emit({"event": "loader_error", "error": "%r" % ex})
        return 1
    emit({"event": "loader_loaded", "loader": loader_abs})

    # COM 初始化（本线程）。S_OK/S_FALSE 均视为成功；
    # RPC_E_CHANGED_MODE 说明本线程此前已用另一套间初始化，如实记录。
    co_hr = int(ole32.CoInitializeEx(None, coinit)) & 0xFFFFFFFF
    emit({"event": "coinitialize", "hresult": co_hr,
          "hresult_text": hr_name(co_hr), "apartment": args.apartment})
    if co_hr not in (S_OK, S_FALSE):
        emit({"event": "fatal", "stage": "CoInitializeEx"})
        return 1

    try:
        ver_hr, version = get_browser_version(dll)
        emit({"event": "runtime_version", "hresult": ver_hr,
              "hresult_text": hr_name(ver_hr), "version": version})
        if ver_hr != S_OK or not version:
            emit({"event": "fatal", "stage": "runtime_version"})
            return 1

        os.makedirs(os.path.abspath(args.user_data), exist_ok=True)
        emit({"event": "user_data_dir",
              "path": os.path.abspath(args.user_data)})

        timeout_ms = max(100, int(round(args.timeout * 1000)))
        prev_ptr = 0
        env_ptrs = []
        for round_no in range(1, args.rounds + 1):
            ev = create_environment_round(
                dll, args.user_data, round_no, timeout_ms)
            ev["same_as_previous_ptr"] = (
                round_no > 1 and ev["env_ptr"] == prev_ptr)
            prev_ptr = ev["env_ptr"]
            env_ptrs.append(ev["env_ptr"])
            emit(ev)
        emit({"event": "summary", "ok": True, "rounds": args.rounds,
              "env_ptrs": env_ptrs,
              "distinct_env_objects": len(set(env_ptrs)),
              "ts_local": local_time_iso()})
        return 0
    except Exception as ex:
        emit({"event": "fatal", "error": "%r" % ex,
              "ts_local": local_time_iso()})
        return 1
    finally:
        ole32.CoUninitialize()


if __name__ == "__main__":
    sys.exit(main())
