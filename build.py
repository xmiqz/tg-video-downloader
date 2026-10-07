# -*- coding: utf-8 -*-
"""
TG视频下载器 —— 跨平台一键打包脚本（PyInstaller）

  Windows（x64/ARM64）：打包图形界面 gui.py + web/ 资产
                       （pywebview + WebView2 + pythonnet + Pillow）
                       同时打包控制台 TUI 版 tg_video_dl.py（ANSI 界面）
  Linux / macOS       ：打包命令行内核 tg_video_dl.py
  （PyInstaller 不支持交叉编译，每个平台在自己的 runner 上构建）

用法：
  python build.py [输出目录]
"""
import os
import platform
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

# 构建中间目录强制用纯 ASCII 路径，避免中文路径导致的 hook/编码问题
WORK = os.path.join(HERE, "build_tmp")

# Windows runner 经 Git bash 调用时，stdout 默认可能是 cp1252，打印含中文的
# 产物名/命令会直接 UnicodeEncodeError。统一切到 UTF-8，errors=replace 兜底。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# 子进程（PyInstaller）同样强制 UTF-8 输出，避免其内部打印中文名时崩溃
os.environ["PYTHONUTF8"] = "1"
os.environ["PYTHONIOENCODING"] = "utf-8"


# 仅 WPF 使用、WinForms 宿主不需要的程序集/原生库，打包前裁掉以减小体积
_WPF_ONLY = [
    "PresentationFramework.dll", "PresentationCore.dll", "PresentationUI.dll",
    "PresentationFramework.Luna.dll", "PresentationFramework.Aero.dll",
    "PresentationFramework.Aero2.dll", "PresentationFramework.Royale.dll",
    "PresentationFramework.Classic.dll", "PresentationFramework.AeroLite.dll",
    "wpfgfx_cor3.dll", "PresentationNative_cor3.dll", "PenImc_cor3.dll",
    "DirectWriteForwarder.dll", "ReachFramework.dll", "System.Printing.dll",
    "System.Xaml.dll", "System.Windows.Controls.Ribbon.dll",
    "WindowsFormsIntegration.dll", "PresentationFramework-SystemCore.dll",
    "PresentationFramework-SystemData.dll", "PresentationFramework-SystemDrawing.dll",
    "PresentationFramework-SystemXml.dll", "PresentationFramework-SystemXmlLinq.dll",
    "System.Windows.Presentation.dll", "System.Windows.Input.Manipulations.dll",
]


def is_arm64_windows():
    return (platform.system() == "Windows"
            and platform.machine().lower() in ("arm64", "aarch64"))


def prepare_arm64_assets():
    """准备 ARM64 打包所需资产：

    1. 给已安装的 pywebview 打 .NET 8 兼容补丁；
    2. 确保 dotnet_arm64/ 存在（缺失时用 dotnet-install 拉取
       WindowsDesktop 运行时，非 SDK）；
    3. 裁剪仅 WPF 使用的文件。

    返回 (dotnet_dir, webview2_dir)。
    """
    dotnet_dir = os.path.join(HERE, "dotnet_arm64")
    webview2_dir = os.path.join(HERE, "webview2_arm64")

    print(">> 修补 pywebview 以兼容 .NET 8")
    subprocess.check_call([sys.executable, os.path.join(HERE, "patch_pywebview.py")])

    if not os.path.isdir(os.path.join(dotnet_dir, "host", "fxr")):
        print(">> 下载 .NET 8 WindowsDesktop (ARM64) 运行时到 dotnet_arm64")
        ps = (
            "$ErrorActionPreference='Stop';"
            "[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12;"
            "& ([scriptblock]::Create((Invoke-WebRequest -UseBasicParsing "
            "'https://dot.net/v1/dotnet-install.ps1').Content)) "
            "-Runtime windowsdesktop -Channel 8.0 -Architecture arm64 "
            f"-InstallDir '{dotnet_dir}'"
        )
        subprocess.check_call(["powershell", "-ExecutionPolicy", "Bypass",
                               "-Command", ps])

    desktop_root = os.path.join(dotnet_dir, "shared", "Microsoft.WindowsDesktop.App")
    if os.path.isdir(desktop_root):
        for ver in os.listdir(desktop_root):
            vdir = os.path.join(desktop_root, ver)
            for f in _WPF_ONLY:
                p = os.path.join(vdir, f)
                if os.path.isfile(p):
                    os.remove(p)

    if not os.path.isdir(dotnet_dir):
        raise RuntimeError("ARM64 运行时准备失败：缺少 dotnet_arm64/")
    if not os.path.isdir(webview2_dir):
        raise RuntimeError("缺少 webview2_arm64/（.NET Core 版 WebView2 程序集）")

    return dotnet_dir, webview2_dir


def build_windows_gui(dist):
    arm64 = is_arm64_windows()
    name = "TG视频下载器_ARM64" if arm64 else "TG视频下载器"
    web_dir = os.path.join(HERE, "web")
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--onefile", "--windowed",
        "--name", name,
        "--add-data", os.path.join(web_dir, "*") + os.pathsep + "web",
        "--collect-submodules", "telethon",
        "--collect-submodules", "webview",
        "--collect-all", "clr_loader",
        "--hidden-import", "webview.platforms.edgechromium",
        "--hidden-import", "clr",
        "--hidden-import", "PIL",
        "--distpath", dist,
        "--workpath", WORK,
        "--specpath", WORK,
    ]

    if arm64:
        dotnet_dir, webview2_dir = prepare_arm64_assets()
        # 随包分发 CoreCLR(.NET 8) 运行时与 .NET Core 版 WebView2 程序集
        cmd += [
            "--add-data",
            os.path.join(dotnet_dir, "*") + os.pathsep + "dotnet",
            "--add-data",
            os.path.join(webview2_dir, "*") + os.pathsep + "webview2_arm64",
        ]

    cmd.append(os.path.join(HERE, "gui.py"))
    print(">>", " ".join(cmd))
    subprocess.check_call(cmd)


def build_windows_tui(dist):
    machine = platform.machine().lower()
    name = ("TG视频下载器_TUI_ARM64" if machine in ("arm64", "aarch64")
            else "TG视频下载器_TUI")
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--onefile", "--console",
        "--name", name,
        "--collect-submodules", "telethon",
        "--distpath", dist,
        "--workpath", WORK,
        "--specpath", WORK,
        os.path.join(HERE, "tg_video_dl.py"),
    ]
    print(">>", " ".join(cmd))
    subprocess.check_call(cmd)


def build_cli(dist):
    system = platform.system()
    machine = platform.machine().lower()
    if system == "Darwin":
        name = ("TG视频下载器_mac_arm64" if machine == "arm64"
                else "TG视频下载器_mac_x64")
    else:
        name = ("TG视频下载器_linux_arm64"
                if machine in ("aarch64", "arm64")
                else "TG视频下载器_linux_x64")
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--onefile", "--console",
        "--name", name,
        "--collect-submodules", "telethon",
        "--distpath", dist,
        "--workpath", WORK,
        "--specpath", WORK,
        os.path.join(HERE, "tg_video_dl.py"),
    ]
    print(">>", " ".join(cmd))
    subprocess.check_call(cmd)

    if system == "Darwin":
        # 生成 .command 启动器：双击在终端打开，数据目录生成在它旁边
        bin_path = os.path.join(dist, name)
        os.chmod(bin_path, 0o755)
        wrapper = os.path.join(dist, name + ".command")
        with open(wrapper, "w", encoding="utf-8") as f:
            f.write(
                '#!/bin/bash\n'
                'cd "$(dirname "$0")"\n'
                f'"./{name}"\n'
                'echo ""\n'
                'read -n 1 -s -r -p "按任意键关闭窗口..."\n'
            )
        os.chmod(wrapper, 0o755)
        print("已生成可双击启动的 .command 启动器：", wrapper)


def main():
    # 输出目录：命令行参数 > 桌面 > dist/
    if len(sys.argv) > 1:
        dist = os.path.abspath(sys.argv[1])
    else:
        desktop = os.path.join(os.path.expanduser("~"), "Desktop")
        dist = desktop if os.path.isdir(desktop) else os.path.join(HERE, "dist")
    os.makedirs(dist, exist_ok=True)

    if platform.system() == "Windows":
        build_windows_gui(dist)
        build_windows_tui(dist)
    else:
        build_cli(dist)

    shutil.rmtree(WORK, ignore_errors=True)
    print("\n打包完成，输出位置：", dist)


if __name__ == "__main__":
    main()
