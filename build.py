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
import json
import os
import platform
import re
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
    if platform.system() != "Windows":
        return False
    # platform.machine() 在 x64 模拟层会误报成物理 CPU 的 ARM64，
    # 于是用 x64 解释器打出来的包也会叫「_ARM64」。改看解释器自身的平台串
    # （win-arm64 / win-amd64），它随解释器架构走，模拟层下也准确。
    import sysconfig
    return "arm64" in sysconfig.get_platform().lower()


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

    同一 hook 还负责把 SSL_CERT_FILE 指向随包 cacert.pem。冻结态的 OpenSSL
    找不到构建机的默认证书路径，ssl.create_default_context() 直接抛
    FileNotFoundError——桌面端「每日壁纸」同步就是这样失败的（源码态正常，
    只有打包后才复现）。
    """
    os.makedirs(WORK, exist_ok=True)
    hook = os.path.join(WORK, "rthook_stdio.py")
    with open(hook, "w", encoding="utf-8") as f:
        f.write(
            "# PyInstaller runtime hook: console streams + CA bundle\n"
            "import os\n"
            "import sys\n"
            "for _name in ('stdout', 'stderr'):\n"
            "    _stream = getattr(sys, _name, None)\n"
            "    _broken = _stream is None\n"
            "    if not _broken:\n"
            "        try:\n"
            "            _stream.flush()\n"
            "        except (OSError, ValueError, AttributeError):\n"
            "            _broken = True\n"
            "    if _broken:\n"
            "        setattr(sys, _name,\n"
            "                open(os.devnull, 'w', encoding='utf-8'))\n"
            "del _stream, _broken\n"
            "del _name\n"
            "_base = getattr(sys, '_MEIPASS', None)\n"
            "if _base:\n"
            "    _ca = os.path.join(_base, 'certifi', 'cacert.pem')\n"
            "    if os.path.isfile(_ca):\n"
            "        os.environ['SSL_CERT_FILE'] = _ca\n"
            "        os.environ.setdefault('REQUESTS_CA_BUNDLE', _ca)\n"
            "del _base\n"
        )
    return hook


def _ssl_reject_pem():
    """Return the ``codex-rejected-cas.pem`` shipped beside this ssl.py, if any.

    The Codex runtime patches ssl.SSLContext.__new__ to unconditionally
    ``load_verify_locations(cafile=os.path.join(os.path.dirname(ssl.__file__),
    'codex-rejected-cas.pem'))``. Once frozen, ssl.__file__ lives inside
    _MEIPASS and that file is absent, so *every* SSLContext construction raises
    FileNotFoundError - which is exactly why the packaged desktop build could
    not fetch the daily wallpaper while the source tree could.

    Stock CPython has no such file, so this returns None there and the build is
    unchanged.
    """
    try:
        import ssl
    except Exception:
        return None
    cand = os.path.join(os.path.dirname(ssl.__file__ or ""), "codex-rejected-cas.pem")
    return cand if os.path.isfile(cand) else None


def _ssl_data_args():
    pem = _ssl_reject_pem()
    return ["--add-data", pem + os.pathsep + "."] if pem else []


def _project_version():
    """发布版本：优先 TGDL_VERSION，其次 CHANGELOG 最新条目标题，最后 dev。"""
    env = os.environ.get("TGDL_VERSION", "").strip()
    if env:
        return env
    try:
        with open(os.path.join(HERE, "CHANGELOG.md"), encoding="utf-8") as f:
            for line in f:
                m = re.match(r"##\s*\[([^\]]+)\]", line)
                if m and re.match(r"\d", m.group(1)):
                    return "v" + m.group(1)
    except OSError:
        pass
    return "dev"


def _git_short_sha():
    """Best-effort source identity for local builds; CI injects TGDL_BUILD."""
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--short=7", "HEAD"],
            cwd=HERE, text=True, stderr=subprocess.DEVNULL,
        ).strip()
        return out if re.fullmatch(r"[0-9a-fA-F]{7,40}", out) else ""
    except Exception:
        return ""


def _project_build():
    """Build identity is always a commit short SHA, never a manual round label."""
    for candidate in (os.environ.get("TGDL_BUILD", "").strip(), _git_short_sha()):
        if re.fullmatch(r"[0-9a-fA-F]{7,40}", candidate):
            return candidate[:7].lower()
    return ""


def _write_version_stamp():
    """桌面包没有 Docker 的 TGDL_VERSION 注入，把版本写进 assets/version.json
    随包带走；server_config.build_info() 在冻结态读它，登录页页脚才有版本号。"""
    path = os.path.join(WORK, "assets", "version.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    data = {
        "version": _project_version(),
        "build": _project_build(),
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    return path


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
        # gui._choose_folder_win compiles folderpicker.cs at runtime with the
        # system .NET compiler, so the shim has to sit beside gui.py inside the
        # bundle. Without it, 浏览 dies with "缺少 folderpicker.cs".
        "--add-data", os.path.join(HERE, "folderpicker.cs") + os.pathsep + ".",
        # 窗口类图标与 exe 图标同一母版；冻结态由 wv2_native 从 assets/ 载入
        "--add-data", os.path.join(HERE, "build", "tvg.ico") + os.pathsep + "assets",
        # 版本戳：登录页页脚显示版本号（桌面端无 TGDL_VERSION 注入）
        "--add-data", _write_version_stamp() + os.pathsep + "assets",
        "--icon", os.path.join(HERE, "build", "tvg.ico"),
        # 见 _write_stdio_runtime_hook：修复 windowed 态 stdout=None 崩溃
        "--runtime-hook", rthook,
        "--collect-submodules", "telethon",
        # 冻结态的 OpenSSL 找不到系统证书路径，cacert.pem 必须随包（见 hook）
        "--collect-data", "certifi",
    ] + _ssl_data_args() + [
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
    name = ("TG视频下载器_TUI_ARM64" if is_arm64_windows()
            else "TG视频下载器_TUI")
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--onefile", "--console",
        "--name", name,
        "--icon", os.path.join(HERE, "build", "tvg.ico"),
        "--collect-submodules", "telethon",
        "--collect-data", "certifi",
        "--runtime-hook", _write_stdio_runtime_hook(),
    ] + _ssl_data_args() + [
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
        "--collect-data", "certifi",
        "--runtime-hook", _write_stdio_runtime_hook(),
    ] + _ssl_data_args() + [
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
