# -*- coding: utf-8 -*-
"""
TG视频下载器 —— 跨平台一键打包脚本（PyInstaller）

  Windows（x64/ARM64）：打包图形界面 gui.py + web/ 资产
                       （pywebview + WebView2 + pythonnet + Pillow）
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


def build_windows_gui(dist):
    machine = platform.machine().lower()
    name = ("TG视频下载器_ARM64" if machine in ("arm64", "aarch64")
            else "TG视频下载器")
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
        os.path.join(HERE, "gui.py"),
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
    else:
        build_cli(dist)

    shutil.rmtree(WORK, ignore_errors=True)
    print("\n打包完成，输出位置：", dist)


if __name__ == "__main__":
    main()
