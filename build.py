# -*- coding: utf-8 -*-
"""
TG视频下载器 —— 跨平台一键打包脚本（PyInstaller）

  Windows（x64/ARM64）：打包图形界面 gui.py + web/ 与 lib/ 原生资产
                       （系统 WebView2 壳 wv2_app + 随包 WebView2Loader + Pillow）
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


def is_arm64_windows():
    return (platform.system() == "Windows"
            and platform.machine().lower() in ("arm64", "aarch64"))


def _write_stdio_runtime_hook():
    """生成 GUI runtime hook 到 WORK 并返回路径。

    --windowed 冻结态（同 pythonw）PyInstaller 不提供控制台，sys.stdout/
    sys.stderr 为 None。uvicorn 0.54 的 DefaultFormatter.__init__ 直接调用
    sys.stdout.isatty()（uvicorn/logging.py 第 42 行，无 None 防护），
    server_app 启动配置日志时即抛 AttributeError，进程弹"Unhandled exception"。
    在任何业务模块导入前把 None 流替换为 devnull 文本流，isatty() 返回 False，
    行为与"无控制台、日志丢弃"一致；崩溃时 PyInstaller 自带异常对话框不受影响。

    失效条件：uvicorn 上游对 None stdout 加防护，或 PyInstaller windowed 模式
    提供默认 stdout 后，可连同 --runtime-hook 一并移除。
    """
    os.makedirs(WORK, exist_ok=True)
    hook = os.path.join(WORK, "rthook_stdio.py")
    with open(hook, "w", encoding="utf-8") as f:
        f.write(
            "# PyInstaller runtime hook: windowed mode has no console streams\n"
            "import os\n"
            "import sys\n"
            "for _name in ('stdout', 'stderr'):\n"
            "    if getattr(sys, _name) is None:\n"
            "        setattr(sys, _name,\n"
            "                open(os.devnull, 'w', encoding='utf-8'))\n"
            "del _name\n"
        )
    return hook


def build_windows_gui(dist):
    arm64 = is_arm64_windows()
    name = "TG视频下载器_ARM64" if arm64 else "TG视频下载器"
    web_dir = os.path.join(HERE, "web")
    lib_dir = os.path.join(HERE, "lib")
    rthook = _write_stdio_runtime_hook()
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--onefile", "--windowed",
        "--name", name,
        "--add-data", os.path.join(web_dir, "*") + os.pathsep + "web",
        # onefile 解包后 wv2_app 按相对路径找 lib/<arch>/WebView2Loader.dll，
        # 两个架构的 loader 都随包带上（每个约 0.2MB，壳按架构选用）
        "--add-data", os.path.join(lib_dir, "*") + os.pathsep + "lib",
        # 见 _write_stdio_runtime_hook：修复 windowed 态 stdout=None 崩溃
        "--runtime-hook", rthook,
        "--collect-submodules", "telethon",
        "--hidden-import", "PIL",
        "--distpath", dist,
        "--workpath", WORK,
        "--specpath", WORK,
    ]

    # v2.0 已彻底移除 pywebview/pythonnet(CLR) 栈；显式排除，防止环境里
    # 残留安装或传递依赖把它们重新带进包（fastapi/uvicorn 不依赖它们）
    for mod in ("webview", "clr", "pythonnet", "clr_loader",
                "tkinter", "unittest", "test", "pydoc"):
        cmd += ["--exclude-module", mod]

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
            q = '"'
            f.write(
                '#!/bin/bash\n'
                'cd "$(dirname "$0")"\n'
                f'{q}./{name}{q}\n'
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
