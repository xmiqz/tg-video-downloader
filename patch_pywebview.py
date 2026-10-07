# -*- coding: utf-8 -*-
"""构建期补丁：让 pywebview 的 WinForms 后端兼容 .NET 8 (CoreCLR)。

仅在 ARM64 Windows 上需要（该平台改用随包分发的 .NET 8 运行时）。
pywebview 6.x 的 winforms.py 存在两处 .NET Framework 时代写法：

1. `Assembly.LoadWithPartialName(...)` 在 .NET Core/8 已被移除，返回 None；
2. `OpenFolderDialog` 依赖 internal 类型
   `FileDialogNative+IFileDialog/FOS`，.NET 8 重构后这些类型已删除，
   导致模块 import 阶段即 AttributeError。

本脚本做幂等的文本替换：
1. 从已加载的 WinForms Form 类型取程序集；
2. 用 .NET 8 仍公开支持的 FolderBrowserDialog 重写 OpenFolderDialog。
"""
import re
import sys

NEW_CLASS = '''class OpenFolderDialog:
    @classmethod
    def show(cls, parent=None, initialDirectory=None, allow_multiple=False, title=None):
        dialog = WinForms.FolderBrowserDialog()
        try:
            if title:
                dialog.Description = title
            if initialDirectory and os.path.isdir(initialDirectory):
                dialog.SelectedPath = initialDirectory
            result = dialog.ShowDialog()
            if result == WinForms.DialogResult.OK:
                return (dialog.SelectedPath,)
            return None
        finally:
            dialog.Dispose()

'''


def patch(path):
    src = open(path, "r", encoding="utf-8").read()
    original = src

    src = src.replace(
        "Assembly.LoadWithPartialName('System.Windows.Forms')",
        "WinForms.Form().GetType().Assembly",
    )

    pattern = re.compile(
        r"class OpenFolderDialog:.*?\n(?=_main_window_created = Event\(\))",
        re.DOTALL,
    )
    if "FolderBrowserDialog" not in src:
        src, n = pattern.subn(NEW_CLASS, src)
        if n != 1:
            raise RuntimeError("未能定位 OpenFolderDialog 类块：%s" % path)

    if src == original:
        print("[patch_pywebview] 已是补丁后状态，跳过：", path)
    else:
        open(path, "w", encoding="utf-8").write(src)
        print("[patch_pywebview] 已修补：", path)


def main():
    import webview
    from pathlib import Path

    target = Path(webview.__file__).parent / "platforms" / "winforms.py"
    if not target.exists():
        print("[patch_pywebview] 未找到 winforms.py，跳过")
        return
    patch(str(target))


if __name__ == "__main__":
    main()
