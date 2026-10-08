# -*- coding: utf-8 -*-
"""Win32 原生空白窗口骨架（v2.0 原生重构 Task 2，TR-2.1 / TR-2.2）。

仅用标准库 ctypes 直调 Win32：
  - RegisterClassExW / CreateWindowExW / DefWindowProcW / 标准消息循环；
  - PerMonitorV2 DPI 感知（-4 上下文 -> shcore PER_MONITOR(2) -> System 三级回退，
    与 gui.py main() 既有链一致），逻辑尺寸（CSS 像素语义，默认 1180x780）
    按窗口所在显示器 DPI 换算为物理客户区，再用 AdjustWindowRectEx 求外框；
  - WM_DESTROY / WM_CLOSE / WM_SIZE / WM_MOVE / WM_DPICHANGED 带毫秒时间戳日志；
  - SetTimer/WM_TIMER 无人值守自动关闭；PrintWindow(PW_RENDERFULLCONTENT)
    截图，失败回退 BitBlt，zlib+struct 手写最小 PNG（Truecolor, 8bit/RGB）。

本任务只交付“空白窗口骨架”，不创建 WebView2（Task 3 起）。

官方依据（头文件/文档）：
  - winuser.h：WNDCLASSEXW、CREATESTRUCTW、MSG、WM_* 、GWLP_USERDATA、
    WS_OVERLAPPEDWINDOW、SWP_*、PrintWindow（PW_RENDERFULLCONTENT=2）；
  - wingdi.h：BITMAPINFOHEADER/BITMAPINFO、GetDIBits、BitBlt(SRCCOPY)、
    CreateCompatibleDC/Bitmap、GetStockObject(WHITE_BRUSH)；
  - winbase.h：GetModuleHandleW/GetTickCount64/GetNativeSystemInfo；
  - dpiawarenesscontext.h：DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 =
    (DPI_AWARENESS_CONTEXT)-4（SetProcessDpiAwarenessContext）；
  - shellscalingapi.h：PROCESS_PER_MONITOR_DPI_AWARE = 2。

约束：只允许 ctypes/ctypes.wintypes/sys/json/argparse；PNG 编码经任务明确
特许使用标准库 zlib/struct。不导入 comtypes/pywin32 或任何第三方包。
platform.machine() 等值改用 kernel32!GetNativeSystemInfo 的 wProcessorArchitecture
（12=ARM64，9=AMD64）取得，取值语义与 platform.machine() 对齐，避免超出模块白名单。
"""
import ctypes
from ctypes import wintypes
import sys
import json
import argparse
import struct
import zlib

# ctypes.wintypes（Python 3.12）未导出 HCURSOR；Win32 头中 HCURSOR 与
# HICON 同为 DECLARE_HANDLE（指针宽度不透明句柄），按官方表示对齐。
if not hasattr(wintypes, "HCURSOR"):
    wintypes.HCURSOR = wintypes.HICON

# ============================================================================
# 常量
# ============================================================================

# 窗口样式（winuser.h）
WS_OVERLAPPED = 0x00000000
WS_CAPTION = 0x00C00000
WS_SYSMENU = 0x00080000
WS_THICKFRAME = 0x00040000
WS_MINIMIZEBOX = 0x00020000
WS_MAXIMIZEBOX = 0x00010000
WS_OVERLAPPEDWINDOW = (WS_OVERLAPPED | WS_CAPTION | WS_SYSMENU |
                       WS_THICKFRAME | WS_MINIMIZEBOX | WS_MAXIMIZEBOX)
CS_HREDRAW = 0x0002
CS_VREDRAW = 0x0001

SW_SHOWNORMAL = 1
IDC_ARROW = 32512
WHITE_BRUSH = 0          # wingdi.h: GetStockObject

# 窗口消息（winuser.h）
WM_CREATE = 0x0001
WM_DESTROY = 0x0002
WM_MOVE = 0x0003
WM_SIZE = 0x0005
WM_CLOSE = 0x0010
WM_TIMER = 0x0113
WM_NCCREATE = 0x0081
WM_DPICHANGED = 0x02E0

GWLP_USERDATA = -21
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010

SM_CXSCREEN = 0
SM_CYSCREEN = 1

# 截图（winuser.h / wingdi.h）
PW_RENDERFULLCONTENT = 2
SRCCOPY = 0x00CC0020
DIB_RGB_COLORS = 0
BI_RGB = 0

# 定时器 ID
IDT_SHOT = 1001
IDT_CLOSE = 1002

# wProcessorArchitecture（sysinfoapi.h）
_PROC_ARCH = {0: "x86", 5: "ARM", 6: "IA64", 9: "AMD64", 12: "ARM64"}

CLASS_NAME = "TgVideoDownloaderNativeTask2Window"

# ============================================================================
# 结构体
# ============================================================================


class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG),
                ("y", wintypes.LONG)]


class RECT(ctypes.Structure):
    _fields_ = [("left", wintypes.LONG),
                ("top", wintypes.LONG),
                ("right", wintypes.LONG),
                ("bottom", wintypes.LONG)]


class MSG(ctypes.Structure):
    # winuser.h tagMSG
    _fields_ = [("hwnd", wintypes.HWND),
                ("message", wintypes.UINT),
                ("wParam", wintypes.WPARAM),
                ("lParam", wintypes.LPARAM),
                ("time", wintypes.DWORD),
                ("pt", POINT)]


class WNDCLASSEXW(ctypes.Structure):
    # winuser.h tagWNDCLASSEXW（64 位下 80 字节）
    _fields_ = [("cbSize", wintypes.UINT),
                ("style", wintypes.UINT),
                ("lpfnWndProc", ctypes.c_void_p),
                ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE),
                ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HCURSOR),
                ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR),
                ("hIconSm", wintypes.HICON)]


class CREATESTRUCTW(ctypes.Structure):
    # winuser.h tagCREATESTRUCTW
    _fields_ = [("lpCreateParams", wintypes.LPVOID),
                ("hInstance", wintypes.HINSTANCE),
                ("hMenu", wintypes.HMENU),
                ("hwndParent", wintypes.HWND),
                ("cy", ctypes.c_int),
                ("cx", ctypes.c_int),
                ("y", ctypes.c_int),
                ("x", ctypes.c_int),
                ("style", wintypes.LONG),
                ("lpszName", wintypes.LPCWSTR),
                ("lpszClass", wintypes.LPCWSTR),
                ("dwExStyle", wintypes.DWORD)]


class RGBQUAD(ctypes.Structure):
    _fields_ = [("rgbBlue", wintypes.BYTE),
                ("rgbGreen", wintypes.BYTE),
                ("rgbRed", wintypes.BYTE),
                ("rgbReserved", wintypes.BYTE)]


class BITMAPINFOHEADER(ctypes.Structure):
    # wingdi.h tagBITMAPINFOHEADER（40 字节）
    _fields_ = [("biSize", wintypes.DWORD),
                ("biWidth", wintypes.LONG),
                ("biHeight", wintypes.LONG),
                ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD),
                ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD),
                ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG),
                ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER),
                ("bmiColors", RGBQUAD * 1)]


class _SYSTEM_INFO_UNION(ctypes.Union):
    # SYSTEM_INFO 首个匿名联合体：DWORD 等价于两个 WORD
    _fields_ = [("dwOemId", wintypes.DWORD),
                ("wProcessorArchitecture", wintypes.WORD),
                ("wReserved", wintypes.WORD)]


class SYSTEM_INFO(ctypes.Structure):
    # sysinfoapi.h SYSTEM_INFO（仅布局对齐需要，字段保留原名）
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


# ============================================================================
# Win32 原型绑定
# ============================================================================

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
try:
    shcore = ctypes.WinDLL("shcore", use_last_error=True)
except OSError:
    shcore = None

# --- DPI 感知 -------------------------------------------------------------
# HANDLE 形参必须显式声明为指针宽度：-4 伪句柄须按 64 位传 0xFFFFFFFFFFFFFFFC，
# 否则按 32 位 int 传 0x00000000FFFFFFFC 会被判定为无效句柄。
user32.SetProcessDpiAwarenessContext.argtypes = [wintypes.HANDLE]
user32.SetProcessDpiAwarenessContext.restype = wintypes.BOOL
if shcore is not None:
    shcore.SetProcessDpiAwareness.argtypes = [ctypes.c_int]
    shcore.SetProcessDpiAwareness.restype = ctypes.c_long  # HRESULT
user32.SetProcessDPIAware.argtypes = []
user32.SetProcessDPIAware.restype = wintypes.BOOL
user32.GetDpiForSystem.argtypes = []
user32.GetDpiForSystem.restype = wintypes.UINT
user32.GetDpiForWindow.argtypes = [wintypes.HWND]
user32.GetDpiForWindow.restype = wintypes.UINT

# --- 窗口类/窗口 ----------------------------------------------------------
user32.RegisterClassExW.argtypes = [ctypes.POINTER(WNDCLASSEXW)]
user32.RegisterClassExW.restype = wintypes.ATOM
user32.UnregisterClassW.argtypes = [wintypes.LPCWSTR, wintypes.HINSTANCE]
user32.UnregisterClassW.restype = wintypes.BOOL
user32.CreateWindowExW.argtypes = [
    wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
user32.CreateWindowExW.restype = wintypes.HWND
user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT,
                                  wintypes.WPARAM, wintypes.LPARAM]
user32.DefWindowProcW.restype = ctypes.c_ssize_t
# 任务清单中写作 DefWindowProc_W（即 DefWindowProcW；DefWindowProc 无 A/W 行为差异）
DefWindowProc_W = user32.DefWindowProcW

user32.GetMessageW.argtypes = [ctypes.POINTER(MSG), wintypes.HWND,
                               wintypes.UINT, wintypes.UINT]
user32.GetMessageW.restype = wintypes.BOOL
user32.TranslateMessage.argtypes = [ctypes.POINTER(MSG)]
user32.TranslateMessage.restype = wintypes.BOOL
user32.DispatchMessageW.argtypes = [ctypes.POINTER(MSG)]
user32.DispatchMessageW.restype = ctypes.c_ssize_t
user32.PostQuitMessage.argtypes = [ctypes.c_int]
user32.PostQuitMessage.restype = None
user32.LoadCursorW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR]
user32.LoadCursorW.restype = wintypes.HCURSOR
user32.DestroyWindow.argtypes = [wintypes.HWND]
user32.DestroyWindow.restype = wintypes.BOOL
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.ShowWindow.restype = wintypes.BOOL
user32.UpdateWindow.argtypes = [wintypes.HWND]
user32.UpdateWindow.restype = wintypes.BOOL
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT,
                                wintypes.WPARAM, wintypes.LPARAM]
user32.PostMessageW.restype = wintypes.BOOL
user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND,
                                ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, wintypes.UINT]
user32.SetWindowPos.restype = wintypes.BOOL
user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int,
                                     ctypes.c_ssize_t]
user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
user32.AdjustWindowRectEx.argtypes = [ctypes.POINTER(RECT), wintypes.DWORD,
                                      wintypes.BOOL, wintypes.DWORD]
user32.AdjustWindowRectEx.restype = wintypes.BOOL
# Win10 1607+：按指定 DPI 计算，用于与 AdjustWindowRectEx 交叉核对
try:
    user32.AdjustWindowRectExForDpi.argtypes = [
        ctypes.POINTER(RECT), wintypes.DWORD, wintypes.BOOL, wintypes.DWORD,
        wintypes.UINT]
    user32.AdjustWindowRectExForDpi.restype = wintypes.BOOL
except (OSError, AttributeError):
    pass
user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(RECT)]
user32.GetClientRect.restype = wintypes.BOOL
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(RECT)]
user32.GetWindowRect.restype = wintypes.BOOL
user32.GetSystemMetrics.argtypes = [ctypes.c_int]
user32.GetSystemMetrics.restype = ctypes.c_int
user32.SetTimer.argtypes = [wintypes.HWND, ctypes.c_size_t, wintypes.UINT,
                            ctypes.c_void_p]
user32.SetTimer.restype = ctypes.c_size_t
user32.KillTimer.argtypes = [wintypes.HWND, ctypes.c_size_t]
user32.KillTimer.restype = wintypes.BOOL
user32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
user32.PrintWindow.restype = wintypes.BOOL

# --- GDI（wingdi.h） ------------------------------------------------------
# 注意：GetDC/ReleaseDC 按 winuser.h 由 user32.dll 导出（不是 gdi32）。
user32.GetDC.argtypes = [wintypes.HWND]
user32.GetDC.restype = wintypes.HDC
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
user32.ReleaseDC.restype = ctypes.c_int
gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.DeleteDC.argtypes = [wintypes.HDC]
gdi32.DeleteDC.restype = wintypes.BOOL
gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int,
                                         ctypes.c_int]
gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
gdi32.SelectObject.restype = wintypes.HGDIOBJ
gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
gdi32.DeleteObject.restype = wintypes.BOOL
gdi32.BitBlt.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int,
                         ctypes.c_int, ctypes.c_int, wintypes.HDC,
                         ctypes.c_int, ctypes.c_int, wintypes.DWORD]
gdi32.BitBlt.restype = wintypes.BOOL
gdi32.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT,
                            wintypes.UINT, wintypes.LPVOID,
                            ctypes.POINTER(BITMAPINFO), wintypes.UINT]
gdi32.GetDIBits.restype = ctypes.c_int
gdi32.GetStockObject.argtypes = [ctypes.c_int]
gdi32.GetStockObject.restype = wintypes.HGDIOBJ

# --- kernel32 -------------------------------------------------------------
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
kernel32.GetModuleHandleW.restype = wintypes.HMODULE
kernel32.GetTickCount64.argtypes = []
kernel32.GetTickCount64.restype = ctypes.c_ulonglong
kernel32.GetNativeSystemInfo.argtypes = [ctypes.POINTER(SYSTEM_INFO)]
kernel32.GetNativeSystemInfo.restype = None


# ============================================================================
# 环境探测
# ============================================================================


def native_machine():
    """返回 (架构代码, 架构名)。等价 platform.machine()，取自真实原生架构
    （WOW64 下也返回宿主架构），不导入 platform 模块。"""
    si = SYSTEM_INFO()
    kernel32.GetNativeSystemInfo(ctypes.byref(si))
    code = int(si.u.wProcessorArchitecture)
    return code, _PROC_ARCH.get(code, "UNKNOWN(%d)" % code)


def set_process_dpi_awareness():
    """进程级 DPI 感知三级回退（与 gui.py main() 链一致）。

    返回 (采用级别, 逐次尝试明细)：
      per_monitor_v2 -> per_monitor(shcore,2) -> system_aware。
    """
    tries = []
    # 1) Per-Monitor V2（Win10 1703+）：-4 伪句柄，指针宽度
    try:
        ok = bool(user32.SetProcessDpiAwarenessContext(wintypes.HANDLE(-4)))
        tries.append({"api": "SetProcessDpiAwarenessContext(-4)", "ok": ok})
        if ok:
            return "per_monitor_v2", tries
    except OSError as ex:
        tries.append({"api": "SetProcessDpiAwarenessContext(-4)",
                      "ok": False, "error": str(ex)})
    # 2) shcore!SetProcessDpiAwareness(PROCESS_PER_MONITOR_DPI_AWARE=2)
    if shcore is not None:
        try:
            hr = int(shcore.SetProcessDpiAwareness(2))
            tries.append({"api": "SetProcessDpiAwareness(2)", "hresult": hr})
            if hr == 0:
                return "per_monitor", tries
        except OSError as ex:
            tries.append({"api": "SetProcessDpiAwareness(2)",
                          "ok": False, "error": str(ex)})
    # 3) 最老：System DPI Aware
    ok = bool(user32.SetProcessDPIAware())
    tries.append({"api": "SetProcessDPIAware", "ok": ok})
    return "system_aware", tries


# ============================================================================
# 最小 PNG 编码（Truecolor 8bit RGB；zlib/struct 为任务特许标准库）
# ============================================================================


def _png_chunk(tag, data):
    chunk = tag + data
    return (struct.pack(">I", len(data)) + chunk
            + struct.pack(">I", zlib.crc32(chunk) & 0xFFFFFFFF))


def write_png(path, width, height, bgra):
    """把 top-down BGRA（32bpp）字节流编码成 8-bit RGB PNG。bgra 为 bytes。

    逐像素标量搬运（不用奇数偏移的批量切片赋值），该 ARM64 真机调试期曾
    观测到一次不可复现的奇数偏移 bytearray 切片写入瞬时异常（复现压测
    20 万次/多进程 10 万次均为 0），标量路径最保守；3.68M 像素约 1~2 秒。
    """
    stride = width * 4
    raw = bytearray(width * 3 * height + height)  # 每像素3字节 + 每行1过滤字节
    out = 0
    src = 0
    for _y in range(height):
        raw[out] = 0          # PNG filter type 0 (None)
        out += 1
        end = src + stride
        while src < end:
            raw[out] = bgra[src + 2]      # R
            raw[out + 1] = bgra[src + 1]  # G
            raw[out + 2] = bgra[src]      # B
            out += 3
            src += 4
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    payload = (b"\x89PNG\r\n\x1a\n"
               + _png_chunk(b"IHDR", ihdr)
               + _png_chunk(b"IDAT", zlib.compress(bytes(raw), 9))
               + _png_chunk(b"IEND", b""))
    with open(path, "wb") as f:
        f.write(payload)
    return len(payload)


def readback_png_ok(path, width, height):
    """回读校验 PNG：签名、IHDR 维度/位深/颜色类型、全部 chunk CRC、
    IDAT zlib（adler32）、解压长度与每行 filter 字节。返回 "ok" 或错误串。"""
    with open(path, "rb") as f:
        data = f.read()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        return "bad signature"
    off = 8
    idat = b""
    dims = None
    while off + 8 <= len(data):
        n = struct.unpack(">I", data[off:off + 4])[0]
        tag = data[off + 4:off + 8]
        body = data[off + 8:off + 8 + n]
        crc_stored = struct.unpack(">I",
                                   data[off + 8 + n:off + 12 + n])[0]
        if (zlib.crc32(tag + body) & 0xFFFFFFFF) != crc_stored:
            return "crc mismatch %s" % tag
        if tag == b"IHDR":
            dims = struct.unpack(">IIBBBBB", body)
        elif tag == b"IDAT":
            idat += body
        elif tag == b"IEND":
            break
        off += 12 + n
    if dims is None:
        return "missing IHDR"
    w, h, bd, ct = dims[0], dims[1], dims[2], dims[3]
    if (w, h, bd, ct) != (width, height, 8, 2):
        return "ihdr mismatch %s" % (dims,)
    raw = zlib.decompress(idat)
    expect = height * (3 * width + 1)
    if len(raw) != expect:
        return "raw len %d != %d" % (len(raw), expect)
    row_stride = 3 * width + 1
    for y in range(height):
        if raw[y * row_stride] != 0:
            return "filter byte nonzero at row %d" % y
    return "ok"


# ============================================================================
# 窗口
# ============================================================================

# 长生命周期 WNDPROC 回调：CFUNCTYPE 对象若被 GC，窗口过程指针会悬垂。
# 回调用 c_ssize_t(LRESULT) 签名：(HWND, UINT, WPARAM, LPARAM)。
WNDPROC = ctypes.CFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT,
                           wintypes.WPARAM, wintypes.LPARAM)

_CLASS_ATOM = 0          # 进程内只注册一次窗口类
_WNDPROC_REF = None      # 模块级保活
_MSG_NAMES = {WM_CREATE: "WM_CREATE", WM_DESTROY: "WM_DESTROY",
              WM_MOVE: "WM_MOVE", WM_SIZE: "WM_SIZE", WM_CLOSE: "WM_CLOSE",
              WM_TIMER: "WM_TIMER", WM_NCCREATE: "WM_NCCREATE",
              WM_DPICHANGED: "WM_DPICHANGED"}


def _ensure_window_class():
    """注册窗口类（幂等）。返回 ATOM。"""
    global _CLASS_ATOM, _WNDPROC_REF
    if _CLASS_ATOM:
        return _CLASS_ATOM
    _WNDPROC_REF = WNDPROC(_wnd_proc_dispatch)
    hinst = kernel32.GetModuleHandleW(None)
    wc = WNDCLASSEXW()
    wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
    wc.style = CS_HREDRAW | CS_VREDRAW
    wc.lpfnWndProc = ctypes.cast(_WNDPROC_REF, ctypes.c_void_p)
    wc.cbClsExtra = 0
    wc.cbWndExtra = 0
    wc.hInstance = hinst
    wc.hIcon = None
    wc.hCursor = user32.LoadCursorW(None, wintypes.LPCWSTR(IDC_ARROW))
    wc.hbrBackground = gdi32.GetStockObject(WHITE_BRUSH)  # 白底，便于取证
    wc.lpszMenuName = None
    wc.lpszClassName = CLASS_NAME
    wc.hIconSm = None
    atom = user32.RegisterClassExW(ctypes.byref(wc))
    if not atom:
        raise ctypes.WinError(ctypes.get_last_error())
    _CLASS_ATOM = atom
    return atom


def _rect_dict(r):
    return {"left": int(r.left), "top": int(r.top),
            "right": int(r.right), "bottom": int(r.bottom),
            "w": int(r.right - r.left), "h": int(r.bottom - r.top)}


def _wnd_proc_dispatch(hwnd, msg, wparam, lparam):
    """模块级窗口过程：按 GWLP_USERDATA 取回 NativeWindow 实例。

    Python 异常全部拦截，不允许逃出窗口过程导致进程静默崩溃；
    兜底返回 DefWindowProcW 的默认处理。
    """
    obj = None
    try:
        ptr = user32.GetWindowLongPtrW(hwnd, GWLP_USERDATA)
        if ptr:
            obj = ctypes.cast(ptr,
                              ctypes.POINTER(ctypes.py_object))[0]
        if obj is None and msg == WM_NCCREATE:
            # 建窗期 USERDATA 尚未写入，从 CREATESTRUCTW.lpCreateParams 取 self
            cs = ctypes.cast(lparam, ctypes.POINTER(CREATESTRUCTW))[0]
            if cs.lpCreateParams:
                obj = ctypes.cast(
                    cs.lpCreateParams,
                    ctypes.POINTER(ctypes.py_object))[0]
                obj._attach(hwnd)
        if obj is not None:
            return obj.handle_message(hwnd, msg, wparam, lparam)
    except Exception as ex:  # 窗口过程内异常兜底
        try:
            sys.stderr.write("[wv2_native] WndProc 0x%04X 异常：%r\n"
                             % (msg, ex))
        except Exception:
            pass
    return user32.DefWindowProcW(hwnd, msg, wparam, lparam)


def capture_window_png(hwnd, path):
    """PrintWindow(PW_RENDERFULLCONTENT) 抓整窗（含非客户区），失败回退
    BitBlt(SRCCOPY) 抓屏幕坐标；32bpp top-down DIB 取像素后手写 PNG。

    GDI 句柄在 finally 中按“选回旧位图 -> 删位图 -> 删内存DC -> 释放DC”
    顺序严格释放。
    """
    result = {"path": path, "ok": False}
    wr = RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(wr))
    width = int(wr.right - wr.left)
    height = int(wr.bottom - wr.top)
    result["capture_w"] = width
    result["capture_h"] = height
    if width <= 0 or height <= 0:
        result["error"] = "窗口矩形非法 %dx%d" % (width, height)
        return result

    win_dc = user32.GetDC(hwnd)
    screen_dc = user32.GetDC(None)
    mem_dc = gdi32.CreateCompatibleDC(win_dc)
    bmp = gdi32.CreateCompatibleBitmap(win_dc, width, height)
    old = gdi32.SelectObject(mem_dc, bmp)
    try:
        ok = bool(user32.PrintWindow(hwnd, mem_dc, PW_RENDERFULLCONTENT))
        method = "PrintWindow:PW_RENDERFULLCONTENT(2)"
        if not ok:
            ok = bool(gdi32.BitBlt(mem_dc, 0, 0, width, height,
                                   screen_dc, wr.left, wr.top, SRCCOPY))
            method = "BitBlt:SRCCOPY(fallback)"
        result["method"] = method
        result["blit_ok"] = ok

        bi = BITMAPINFO()
        bh = bi.bmiHeader
        bh.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bh.biWidth = width
        bh.biHeight = -height      # 负值：top-down，免去行翻转
        bh.biPlanes = 1
        bh.biBitCount = 32
        bh.biCompression = BI_RGB
        buf = (ctypes.c_ubyte * (width * 4 * height))()
        lines = gdi32.GetDIBits(win_dc, bmp, 0, height, buf,
                                ctypes.byref(bi), DIB_RGB_COLORS)
        result["getdibits_lines"] = int(lines)
        if lines != height:
            result["error"] = "GetDIBits 行数异常：%d/%d" % (lines, height)
            return result
        raw = ctypes.string_at(buf, width * 4 * height)
        result["bytes"] = write_png(path, width, height, raw)
        result["png_verify"] = readback_png_ok(path, width, height)
        result["ok"] = result["png_verify"] == "ok"
        if not result["ok"]:
            result["error"] = "PNG 回读校验失败：" + result["png_verify"]
    finally:
        if old:
            gdi32.SelectObject(mem_dc, old)
        if bmp:
            gdi32.DeleteObject(bmp)
        if mem_dc:
            gdi32.DeleteDC(mem_dc)
        if win_dc:
            user32.ReleaseDC(hwnd, win_dc)
        if screen_dc:
            user32.ReleaseDC(None, screen_dc)
    return result


class NativeWindow:
    """原生顶层窗口。

    title                窗口标题（Unicode）
    width/height         逻辑尺寸（CSS 像素语义，默认 1180x780）；构造时按
                         窗口所在显示器 DPI（建窗前取系统/主显示器 DPI）
                         换算物理客户区，AdjustWindowRectEx 求外框后建窗。
    run()                标准 GetMessage 循环直到 WM_QUIT，返回 wParam 退出码。
    close()              PostMessageW(WM_CLOSE)，可由定时器或外部线程调用。
    """

    def __init__(self, title, width=1180, height=780):
        self.title = title
        self.logical_w = int(width)
        self.logical_h = int(height)
        self.hwnd = None
        self.atom = 0
        self.exit_code = None
        self.shot_path = None
        self.shot_result = None
        self.msg_log = []
        self.counts = {"WM_MOVE": 0, "WM_SIZE": 0, "WM_DPICHANGED": 0,
                       "WM_CLOSE": 0, "WM_DESTROY": 0, "WM_TIMER": 0}

        # py_object 持有者：把 self 经 lpCreateParams/GWLP_USERDATA 传给窗口
        # 过程。持有者本身存于 self，地址在窗口生命期内保持有效。
        self._holder = ctypes.py_object(self)

        self.dpi_system = int(user32.GetDpiForSystem())
        self.scale = self.dpi_system / 96.0
        self.target_client = (
            int(round(self.logical_w * self.scale)),
            int(round(self.logical_h * self.scale)))

        # AdjustWindowRectEx：以物理客户区反推物理外框（理论值）
        r = RECT(0, 0, self.target_client[0], self.target_client[1])
        if not user32.AdjustWindowRectEx(ctypes.byref(r),
                                         WS_OVERLAPPEDWINDOW, False, 0):
            raise ctypes.WinError(ctypes.get_last_error())
        self.theory = _rect_dict(r)
        outer_w = self.theory["w"]
        outer_h = self.theory["h"]
        self.outer_size = (outer_w, outer_h)

        # AdjustWindowRectExForDpi 交叉核对（老系统无此导出则留空）
        self.theory_for_dpi = None
        fn = getattr(user32, "AdjustWindowRectExForDpi", None)
        if fn is not None:
            r2 = RECT(0, 0, self.target_client[0], self.target_client[1])
            try:
                if fn(ctypes.byref(r2), WS_OVERLAPPEDWINDOW, False, 0,
                      self.dpi_system):
                    self.theory_for_dpi = _rect_dict(r2)
            except OSError:
                self.theory_for_dpi = None

        self.atom = _ensure_window_class()
        hinst = kernel32.GetModuleHandleW(None)

        # 居中于物理主屏（进程已 DPI 感知，SM_CXSCREEN 为物理像素）
        screen_w = user32.GetSystemMetrics(SM_CXSCREEN)
        screen_h = user32.GetSystemMetrics(SM_CYSCREEN)
        x = int((screen_w - outer_w) / 2)
        y = int((screen_h - outer_h) / 3)
        if x < 0:
            x = 0
        if y < 0:
            y = 0
        self.position = [x, y]

        lp = ctypes.cast(ctypes.byref(self._holder), ctypes.c_void_p)
        hwnd = user32.CreateWindowExW(
            0, CLASS_NAME, self.title, WS_OVERLAPPEDWINDOW,
            x, y, outer_w, outer_h, None, None, hinst, lp)
        if not hwnd:
            raise ctypes.WinError(ctypes.get_last_error())
        self.hwnd = hwnd
        self.dpi_window = int(user32.GetDpiForWindow(hwnd))

        user32.ShowWindow(hwnd, SW_SHOWNORMAL)
        user32.UpdateWindow(hwnd)

        wr = RECT()
        cr = RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(wr))
        user32.GetClientRect(hwnd, ctypes.byref(cr))
        self.window_rect = _rect_dict(wr)
        self.client_rect = {"left": 0, "top": 0,
                            "right": int(cr.right), "bottom": int(cr.bottom),
                            "w": int(cr.right - cr.left),
                            "h": int(cr.bottom - cr.top)}

    # -- 实例与窗口过程的绑定 ----------------------------------------------

    def _attach(self, hwnd):
        """WM_NCCREATE 期间调用：写入 GWLP_USERDATA。"""
        self.hwnd = hwnd
        ptr = ctypes.cast(ctypes.byref(self._holder),
                          ctypes.c_void_p).value
        user32.SetWindowLongPtrW(hwnd, GWLP_USERDATA, ptr)

    def _log(self, name, **detail):
        entry = {"t_ms": int(kernel32.GetTickCount64()), "msg": name}
        entry.update(detail)
        self.msg_log.append(entry)

    def handle_message(self, hwnd, msg, wparam, lparam):
        if msg == WM_NCCREATE:
            # self 已在分发器里 _attach；交默认处理（应返回 TRUE）
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam) or 1

        if msg == WM_MOVE:
            x = ctypes.c_short(lparam & 0xFFFF).value
            y = ctypes.c_short((lparam >> 16) & 0xFFFF).value
            self.counts["WM_MOVE"] += 1
            self._log("WM_MOVE", x=x, y=y)
            return 0

        if msg == WM_SIZE:
            cx = int(lparam & 0xFFFF)
            cy = int((lparam >> 16) & 0xFFFF)
            self.counts["WM_SIZE"] += 1
            self._log("WM_SIZE", cx=cx, cy=cy,
                      size_code=int(wparam))
            return 0

        if msg == WM_DPICHANGED:
            dpi_x = int(wparam & 0xFFFF)
            dpi_y = int((wparam >> 16) & 0xFFFF)
            suggested = ctypes.cast(lparam, ctypes.POINTER(RECT))[0]
            # 官方要求：按 lParam 给出的建议矩形 SetWindowPos
            user32.SetWindowPos(
                hwnd, None,
                int(suggested.left), int(suggested.top),
                int(suggested.right - suggested.left),
                int(suggested.bottom - suggested.top),
                SWP_NOZORDER | SWP_NOACTIVATE)
            self.dpi_window = (int(user32.GetDpiForWindow(hwnd))
                               or dpi_y)
            self.counts["WM_DPICHANGED"] += 1
            self._log("WM_DPICHANGED", dpi_x=dpi_x, dpi_y=dpi_y,
                      new_rect=_rect_dict(suggested),
                      dpi_window_now=self.dpi_window)
            return 0

        if msg == WM_TIMER:
            tid = int(wparam)
            self.counts["WM_TIMER"] += 1
            if tid == IDT_SHOT:
                user32.KillTimer(hwnd, tid)
                self._on_shot_timer()
                return 0
            if tid == IDT_CLOSE:
                user32.KillTimer(hwnd, tid)
                self._log("WM_TIMER", id=tid, action="PostMessageW(WM_CLOSE)")
                user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
                return 0
            self._log("WM_TIMER", id=tid)
            return 0

        if msg == WM_CLOSE:
            self.counts["WM_CLOSE"] += 1
            self._log("WM_CLOSE")
            user32.DestroyWindow(hwnd)
            return 0

        if msg == WM_DESTROY:
            self.counts["WM_DESTROY"] += 1
            self._log("WM_DESTROY")
            user32.PostQuitMessage(0)
            return 0

        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    # -- 定时器与生命周期 ----------------------------------------------------

    def schedule_auto_close(self, hold_seconds, shot_path=None):
        """建窗后调用：hold 到期自动 PostMessageW(WM_CLOSE)；
        hold 中段触发一次截图（PrintWindow）。"""
        self.shot_path = shot_path
        hold_ms = max(1, int(round(float(hold_seconds) * 1000)))
        shot_ms = min(1000, max(150, hold_ms - 300))
        t1 = user32.SetTimer(self.hwnd, IDT_SHOT, shot_ms, None)
        t2 = user32.SetTimer(self.hwnd, IDT_CLOSE, hold_ms, None)
        if not t1 or not t2:
            raise ctypes.WinError(ctypes.get_last_error())
        self._log("schedule", hold_ms=hold_ms, shot_ms=shot_ms,
                  shot=shot_path)
        return shot_ms, hold_ms

    def _on_shot_timer(self):
        if not self.shot_path:
            return
        try:
            self.shot_result = capture_window_png(self.hwnd, self.shot_path)
        except Exception as ex:
            self.shot_result = {"path": self.shot_path, "ok": False,
                                "error": "%r" % ex}
        emit({"event": "screenshot", **self.shot_result})

    def run(self):
        """标准 GetMessage 循环，直到 WM_QUIT；返回其 wParam 退出码。

        GetMessage 返回 -1 时（错误）返回 -1。
        """
        msg = MSG()
        code = 0
        while True:
            got = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if got == 0:                      # WM_QUIT
                code = int(msg.wParam)
                break
            if got == -1:                     # 错误
                code = -1
                break
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        self.exit_code = code
        return code

    def close(self):
        """投递 WM_CLOSE。PostMessageW 线程安全，可由定时器/外部线程调用。"""
        if self.hwnd:
            user32.PostMessageW(self.hwnd, WM_CLOSE, 0, 0)


# ============================================================================
# EVIDENCE harness
# ============================================================================


def emit(obj):
    """打印一行 EVIDENCE: 前缀 JSON（ASCII 安全，避免重定向编码问题）。"""
    sys.stdout.write("EVIDENCE: "
                     + json.dumps(obj, ensure_ascii=True, default=str) + "\n")
    sys.stdout.flush()


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Task 2 原生空白窗口骨架（Win32 ctypes，PerMonitorV2）")
    parser.add_argument("--hold", type=float, default=5.0,
                        help="窗口存活秒数，到期自动 WM_CLOSE（默认 5）")
    parser.add_argument("--shot", default=None,
                        help="截图输出 PNG 路径（PrintWindow，失败回退 BitBlt）")
    parser.add_argument("--width", type=int, default=1180,
                        help="逻辑宽度（CSS 像素，默认 1180）")
    parser.add_argument("--height", type=int, default=780,
                        help="逻辑高度（CSS 像素，默认 780）")
    args = parser.parse_args(argv)

    if sys.platform != "win32":
        sys.stderr.write("wv2_native 仅支持 Windows\n")
        return 2
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    arch_code, arch_name = native_machine()
    level, tries = set_process_dpi_awareness()
    dpi_system = int(user32.GetDpiForSystem())
    scale = dpi_system / 96.0
    emit({"event": "start",
          "platform_machine": arch_name,        # 等价 platform.machine()
          "machine_arch_code": arch_code,
          "machine_source": "kernel32!GetNativeSystemInfo",
          "pointer_bits": ctypes.sizeof(ctypes.c_void_p) * 8,
          "python": sys.version.split()[0],
          "dpi_awareness": level,
          "dpi_awareness_tries": tries,
          "dpi_system": dpi_system,
          "scale": scale,
          "scale_percent": int(round(scale * 100)),
          "logical_size": [args.width, args.height],
          "target_client_physical":
              [int(round(args.width * scale)),
               int(round(args.height * scale))]})

    win = NativeWindow(
        "TG 视频下载器 v2.0 · Task2 空白窗口骨架",
        args.width, args.height)

    emit({"event": "window_created",
          "hwnd": int(win.hwnd),
          "class_atom": int(win.atom),
          "dpi_window": win.dpi_window,
          "position": win.position,
          "target_client_physical": list(win.target_client),
          "adjust_window_rect_ex": win.theory,
          "adjust_window_rect_ex_for_dpi": win.theory_for_dpi,
          "outer_theory_physical": list(win.outer_size),
          "window_rect_physical": win.window_rect,
          "client_rect_physical": win.client_rect})

    shot_ms, hold_ms = win.schedule_auto_close(args.hold, args.shot)
    emit({"event": "timers_armed", "shot_ms": shot_ms,
          "close_ms": hold_ms, "shot": args.shot})

    code = win.run()

    emit({"event": "summary",
          "exit_code": int(code),
          "wm_move": win.counts["WM_MOVE"],
          "wm_size": win.counts["WM_SIZE"],
          "wm_dpichanged": win.counts["WM_DPICHANGED"],
          "wm_close": win.counts["WM_CLOSE"],
          "wm_destroy": win.counts["WM_DESTROY"],
          "wm_timer": win.counts["WM_TIMER"],
          "msg_log_len": len(win.msg_log),
          "msg_log_tail": win.msg_log[-12:],
          "shot": win.shot_result})

    return 0 if code == 0 else int(code)


if __name__ == "__main__":
    sys.exit(main())
