# -*- coding: utf-8 -*-
"""ARM64 Windows 的 .NET 运行时引导。

背景：pywebview 的 Windows 后端通过 pythonnet 宿主 CLR。默认走 .NET
Framework(netfx)，但 clr-loader 自带的 ClrLoader.dll 没有 ARM64 版本，
导致 ARM64 上 `import clr` 直接失败。

本模块在 `import webview` 之前把 pythonnet 切换到 CoreCLR（.NET 8）：
用随包分发的 self-contained .NET 8 WindowsDesktop 运行时，经 hostfxr
宿主 CoreCLR，纯 IL 的 WinForms/WebView2 程序集在 ARM64 上由 JIT 执行。

x64 Windows 不受影响（继续使用系统自带的 .NET Framework）。
"""
import json
import os
import pathlib
import platform
import sys
import tempfile

_initialized = False


def _is_arm64_windows():
    return sys.platform == "win32" and platform.machine().lower() in (
        "arm64", "aarch64")


def _dotnet_root():
    """定位 .NET 8 运行时根目录。

    打包后：_MEIPASS/dotnet
    开发时：环境变量 TG_DOTNET_ROOT，或项目同级的 dotnet_arm64 目录
    """
    if getattr(sys, "frozen", False):
        p = pathlib.Path(getattr(sys, "_MEIPASS", "")) / "dotnet"
        if p.is_dir():
            return p
        raise RuntimeError("打包产物中缺少 dotnet 运行时目录")

    env = os.environ.get("TG_DOTNET_ROOT")
    if env and pathlib.Path(env).is_dir():
        return pathlib.Path(env)

    local = pathlib.Path(__file__).resolve().parent / "dotnet_arm64"
    if local.is_dir():
        return local

    raise RuntimeError(
        "未找到 .NET 8 ARM64 运行时：请设置 TG_DOTNET_ROOT 环境变量，"
        "或将运行时放到项目内 dotnet_arm64/ 目录")


def _managed_dir():
    """随项目分发的 .NET Core 编译版 WebView2 托管程序集目录。"""
    if getattr(sys, "frozen", False):
        return pathlib.Path(getattr(sys, "_MEIPASS", "")) / "webview2_arm64"
    return pathlib.Path(__file__).resolve().parent / "webview2_arm64"


def init():
    global _initialized
    if _initialized or not _is_arm64_windows():
        return

    dotnet = _dotnet_root()
    os.environ["DOTNET_ROOT"] = str(dotnet)

    desktop = dotnet / "shared" / "Microsoft.WindowsDesktop.App"
    ver = sorted(p.name for p in desktop.iterdir())[-1]

    config = {
        "runtimeOptions": {
            "tfm": "net8.0",
            "framework": {
                "name": "Microsoft.WindowsDesktop.App",
                "version": ver,
            },
        }
    }
    cfg_path = pathlib.Path(tempfile.mkdtemp()) / "pywebview.runtimeconfig.json"
    cfg_path.write_text(json.dumps(config), encoding="utf-8")

    import pythonnet
    from clr_loader import get_coreclr

    runtime = get_coreclr(runtime_config=cfg_path, dotnet_root=dotnet)
    pythonnet.set_runtime(runtime)
    pythonnet.load()

    import clr

    # .NET 8 中 Microsoft.Win32.SystemEvents 位于独立程序集
    sys_events = (
        dotnet / "shared" / "Microsoft.WindowsDesktop.App" / ver
        / "Microsoft.Win32.SystemEvents.dll")
    clr.AddReference(str(sys_events))

    # 让 pywebview 加载 .NET Core 编译版 WebView2 托管程序集
    import webview.util

    managed = _managed_dir()
    _orig_interop_dll_path = webview.util.interop_dll_path

    def _interop_dll_path(name):
        p = managed / name
        return str(p) if p.exists() else _orig_interop_dll_path(name)

    webview.util.interop_dll_path = _interop_dll_path

    _initialized = True
    print("[arm64_runtime] CoreCLR .NET %s 已就绪" % ver)
