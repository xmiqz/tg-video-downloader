# -*- coding: utf-8 -*-
"""
TG视频下载器 —— 跨平台一键打包脚本（PyInstaller）

在哪个平台运行，就产出哪个平台的可执行文件：
  Windows x64   -> TG视频下载器.exe
  Windows ARM64 -> TG视频下载器_ARM64.exe        （需在 WOA 设备/虚拟机上运行）
  macOS Intel   -> TG视频下载器_mac_x64
  macOS Apple Silicon -> TG视频下载器_mac_arm64
                  另附同名 .command，Finder 双击即可在终端运行

用法：
  python build.py

注意：PyInstaller 不支持交叉编译——Windows 上打不出 macOS/ARM 包，反之亦然。
"""
import os
import platform
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "tg_video_dl.py")

# 构建中间目录强制用纯 ASCII 路径，避免中文路径导致的 hook/编码问题
WORK = os.path.join(HERE, "build_tmp")


def main():
    system = platform.system()
    machine = platform.machine().lower()

    if system == "Windows":
        if machine in ("arm64", "aarch64"):
            name = "TG视频下载器_ARM64"
        else:
            name = "TG视频下载器"
    elif system == "Darwin":
        if machine == "arm64":
            name = "TG视频下载器_mac_arm64"
        else:
            name = "TG视频下载器_mac_x64"
    else:
        if machine in ("aarch64", "arm64"):
            name = "TG视频下载器_linux_arm64"
        else:
            name = "TG视频下载器_linux_x64"

    # 输出目录：命令行参数 > 桌面 > dist/
    if len(sys.argv) > 1:
        dist = os.path.abspath(sys.argv[1])
    else:
        desktop = os.path.join(os.path.expanduser("~"), "Desktop")
        dist = desktop if os.path.isdir(desktop) else os.path.join(HERE, "dist")
    os.makedirs(dist, exist_ok=True)

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--onefile", "--console",
        "--name", name,
        "--collect-submodules", "telethon",
        "--distpath", dist,
        "--workpath", WORK,
        "--specpath", WORK,
        SRC,
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

    shutil.rmtree(WORK, ignore_errors=True)
    print("\n打包完成，输出位置：", dist)


if __name__ == "__main__":
    main()
