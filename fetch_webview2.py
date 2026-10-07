# -*- coding: utf-8 -*-
"""下载并安装 ARM64 所需的 WebView2 程序集到 webview2_arm64/。

为什么需要它：
    pywebview 自带的 Microsoft.Web.WebView2.WinForms.dll 是 .NET
    Framework(net462) 目标，引用了 .NET 8 已移除的 System.Windows.Forms
   .ContextMenu，无法在 CoreCLR 下加载。NuGet 包内有面向 .NET Core 3.0
    的等价程序集（纯 IL，可在 .NET 8 上运行），及 win-arm64 原生
    WebView2Loader.dll。

本脚本做的事（全部幂等）：
    1. 查询 NuGet 取 Microsoft.Web.WebView2 最新稳定版
       （也可用 `python fetch_webview2.py <版本号>` 指定）；
    2. 下载 .nupkg（zip）到临时目录；
    3. 复制：
       lib_manual/netcoreapp3.0/Microsoft.Web.WebView2.Core.dll
       lib_manual/netcoreapp3.0/Microsoft.Web.WebView2.WinForms.dll
       -> webview2_arm64/
       runtimes/win-arm64/native/WebView2Loader.dll
       -> webview2_arm64/runtimes/win-arm64/native/

以后随 pywebview 升级需要更新这些程序集时，重新运行本脚本即可。
"""
import json
import os
import shutil
import sys
import tempfile
import urllib.request
import zipfile

PKG_ID = "microsoft.web.webview2"
FLAT = "https://api.nuget.org/v3-flatcontainer/" + PKG_ID

MANAGED = [
    "lib_manual/netcoreapp3.0/Microsoft.Web.WebView2.Core.dll",
    "lib_manual/netcoreapp3.0/Microsoft.Web.WebView2.WinForms.dll",
]
NATIVE = "runtimes/win-arm64/native/WebView2Loader.dll"


def _get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "tg-fetch/1.0"})
    with urllib.request.urlopen(req) as r:
        return r.read()


def latest_version():
    data = json.loads(_get(FLAT + "/index.json"))
    stable = [v for v in data["versions"] if "-" not in v]
    if not stable:
        raise RuntimeError("NuGet 上没有稳定版本")
    return stable[-1]


def main():
    version = sys.argv[1] if len(sys.argv) > 1 else latest_version()
    print(">> 使用 Microsoft.Web.WebView2 版本：", version)

    here = os.path.dirname(os.path.abspath(__file__))
    dest = os.path.join(here, "webview2_arm64")

    url = "%s/%s/%s.%s.nupkg" % (FLAT, version, PKG_ID, version)
    with tempfile.TemporaryDirectory() as td:
        pkg = os.path.join(td, "pkg.zip")
        with open(pkg, "wb") as f:
            f.write(_get(url))
        with zipfile.ZipFile(pkg) as z:
            members = MANAGED + [NATIVE]
            for m in members:
                target = os.path.join(dest, os.path.basename(m)
                                      if "/" not in m or m.count("/") < 2
                                      else m)
                # 保留 runtimes/... 的相对目录结构
                if m.startswith("runtimes/"):
                    target = os.path.join(dest, m)
                else:
                    target = os.path.join(dest, os.path.basename(m))
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with z.open(m) as src, open(target, "wb") as out:
                    shutil.copyfileobj(src, out)
                print("   ->", os.path.relpath(target, here))

    print(">> 完成：", dest)


if __name__ == "__main__":
    main()
