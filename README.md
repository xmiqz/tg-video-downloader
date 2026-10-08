# TG 视频下载器

[![GitHub release](https://img.shields.io/github/v/release/xmiqz/tg-video-downloader?style=flat-square)](https://github.com/xmiqz/tg-video-downloader/releases)
[![Downloads](https://img.shields.io/github/downloads/xmiqz/tg-video-downloader/total?style=flat-square)](https://github.com/xmiqz/tg-video-downloader/releases)
[![License: MIT](https://img.shields.io/github/license/xmiqz/tg-video-downloader?style=flat-square)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue?style=flat-square)](https://www.python.org/)
[![Platforms](https://img.shields.io/badge/platform-Windows%20%7C%20Linux%20%7C%20macOS-brightgreen?style=flat-square)](#快速开始)

Telegram 频道视频批量下载工具。纯 Python 实现，基于 [Telethon](https://github.com/LonamiWebs/Telethon)，无需任何外部下载器或 ffmpeg。Windows 提供图形界面（pywebview + WebView2）与控制台 TUI 两种形态；Linux / macOS 提供命令行版本。

<!-- 截图占位：Windows GUI 主界面 / 深色液态玻璃 -->
<!-- 截图占位：TUI 控制台下载面板（简洁 / 详细模式） -->

## 目录

- [特性](#特性)
- [快速开始](#快速开始) — Releases 产物对照表
- [首次使用](#首次使用) — 登录步骤、数据目录
- [GUI 基本用法](#gui-基本用法) — Windows 图形界面
- [TUI / 命令行用法](#tui--命令行用法) — 交互模式、命令行参数、快捷键表
- [从源码运行](#从源码运行)
- [自行打包与 CI](#自行打包) — build.py、GitHub Actions
- [常见问题](#常见问题) — FAQ
- [隐私与安全](#隐私与安全)
- [合规与免责声明](#合规与免责声明)
- [License](#license)
- [致谢](#致谢)

## 特性

- **批量扫描与下载**：按频道扫描视频，支持 1 GB 以上大文件
- **服务端关键词搜索**：与 Telegram App 内搜索同一机制，支持包含 / 排除词，可交互勾选
- **高级筛选**：精确短语 / 全部词 / 任一词 / 模糊匹配 / 正则表达式五种匹配模式；按体积上下限、日期范围、频道或评论区范围过滤；按日期或体积排序；条数上限可控
- **多路并发**：可选 1–8 路同时下载，单视频内部 8 MB 分片 × 4 连接并行
- **任务级断点续传**：关机、关窗口后重启可继续未完成任务；分片不重下，失败视频可单独重试
- **moov 无损快启**：下载完成自动把 MP4 的 moov 移到文件开头，任何播放器即点即播，不重编码
- **磁盘空间检查**：下载前按待下总大小粗估，空间不足时提醒
- **大任务自动拆分**：单任务超过 999 条，或同时超过 300 条且总体积超过 300 GB 时，自动按每片 200 GB / 999 条拆分
- **内容去重**：文件名与大小都相同的消息只保留最新
- **内置登录凭证**：自带 Telegram 官方公开客户端 api_id / api_hash，开箱即用；也可替换为自己申请的凭证

## 快速开始

到 [Releases](https://github.com/xmiqz/tg-video-downloader/releases) 下载对应平台文件。Release 资产名为 ASCII（GitHub 会剥离中文名），中文说明见资产标签。

| 平台 | Release 文件名 | 说明 |
| --- | --- | --- |
| Windows x64 | `TGVideoDownloader.exe` | 图形界面版（GUI） |
| Windows x64 | `TGVideoDownloader_TUI.exe` | 控制台版（TUI） |
| Windows ARM64 | `TGVideoDownloader_ARM64.exe` | 图形界面版（GUI） |
| Windows ARM64 | `TGVideoDownloader_TUI_ARM64.exe` | 控制台版（TUI） |
| Linux x64 | `TGVideoDownloader_linux_x64` | 命令行版 |
| Linux ARM64 | `TGVideoDownloader_linux_arm64` | 命令行版 |
| macOS Intel | `TGVideoDownloader_mac_x64` | 命令行版（另附 `.command` 双击启动） |
| macOS Apple Silicon | `TGVideoDownloader_mac_arm64` | 命令行版（另附 `.command` 双击启动） |

**GUI 与 TUI 的区别**：Windows 图形界面版提供完整可视化操作（搜索预览、任务管理、实时进度面板、三种外观主题），适合不熟悉命令行的用户。控制台 TUI 版在终端中运行，提供 ANSI 彩色进度面板与键盘快捷键，适合远程操作或偏好命令行的用户。Linux / macOS 仅有命令行版。

### Linux / macOS 首次运行

```bash
chmod +x TGVideoDownloader_linux_x64
./TGVideoDownloader_linux_x64
```

macOS 双击附带的 `.command` 文件可直接在终端启动。

## 首次使用

1. 启动程序后输入手机号（含国家区号，不带 `+`，例如 `8613800000000`）
2. 在 Telegram App 中查收验证码并输入
3. 若开启了两步验证，输入两步验证密码

程序内置 Telegram 官方公开客户端凭证，无需自行申请 API。若内置凭证被临时限制，程序会引导你到 [my.telegram.org/apps](https://my.telegram.org/apps) 免费申请自己的凭证填入。登录状态保存在本地，下次启动无需重复登录。

### 数据目录

程序启动时会在自身平级目录生成 `TG视频下载器数据/`：

```
TG视频下载器数据/
├── tg_config.json      # 配置（账号、凭证、下载目录、外观偏好）
├── tg_session.session  # 登录会话（免重复登录）
├── tasks/              # 任务清单（断点续传）
└── downloads/          # 下载的视频（默认目录，可修改）
```

整个数据目录随程序一起拷贝即可换电脑使用，无需重新登录。旧版本散在程序目录的配置会自动迁入数据目录。

## GUI 基本用法

Windows 图形界面版提供以下功能：

- **新建下载**：输入频道名或 `t.me/` 链接，设置搜索条件后预览结果，勾选要下载的视频
- **任务库**：查看所有任务的完成进度，继续未完成任务，仅重试失败项，删除任务
- **任务编辑**：弹出独立编辑窗口，修改任务名称、并行路数、保存目录（可选搬移已下载文件）；逐条管理条目状态（标记待下载 / 跳过 / 移除）
- **追加视频**：对已有任务按新条件搜索并追加条目（自动去重）
- **下载控制**：实时进度面板，支持暂停 / 恢复 / 跳过 / 移除单个或批量条目
- **外观主题**：静态暗（默认）、静态亮、液态玻璃（实时采样窗口后方画面，磨砂成透明玻璃效果）
- **文件夹选择**：内置文件夹选择对话框，可自定义下载目录

## TUI / 命令行用法

### 交互模式

直接运行程序（不带参数）进入交互菜单：

```
python tg_video_dl.py
```

菜单提供：新建下载任务、查看 / 继续已有任务、仅重试失败、查看视频明细、条目管理、任务设置（改名 / 并行 / 目录）、追加视频、打开下载目录、删除任务、全局设置（下载目录、API 凭证、退出登录）。

新建任务时交互式收集搜索条件：关键词、匹配模式、结果上限、体积上下限、日期范围、搜索范围（频道 / 评论区 / 两者）、排序方式。

### 命令行参数

```bash
python tg_video_dl.py @频道名 数量 [最小MB] [关键词] [并发数] [all]
# 示例：
python tg_video_dl.py @somechan 20 50 "4K,预告片" 2
```

末尾加 `all` 表示跳过交互勾选，直接下载全部搜索结果。

### 下载中快捷键

| 按键 | 功能 |
| --- | --- |
| `I` | 切换简洁 / 详细显示模式 |
| `P` | 暂停当前视频（释放槽位，队列中下一个立即开始） |
| `R` | 恢复一个暂停的视频 |
| `S` | 跳过当前视频（清理临时文件） |
| `X` | 从任务移除当前视频（保留已下载文件） |
| `Q` | 中止全部下载（未完成视频可在任务中继续） |

## 从源码运行

需要 Python 3.10+。

**仅命令行 / TUI**（Windows / Linux / macOS）：

```bash
pip install telethon
python tg_video_dl.py
```

**Windows 图形界面**（需额外依赖）：

```bash
pip install telethon pywebview pythonnet Pillow
python gui.py
```

从源码运行同样会在项目目录下生成 `TG视频下载器数据/`（已被 `.gitignore` 忽略，账号会话与下载内容不会进入版本库）。

## 自行打包

仓库提供 `build.py` 跨平台打包脚本（基于 PyInstaller）：

```bash
pip install pyinstaller
python build.py [输出目录]
```

- Windows 上会同时构建 GUI 版与 TUI 版
- Linux / macOS 上构建命令行版（macOS 额外生成可双击的 `.command` 启动器）
- ARM64 Windows 需准备 `.NET 8` 运行时与 WebView2 程序集（`build.py` 会自动处理）

PyInstaller 不支持交叉编译，每个平台的包需在该平台构建。

### CI 自动构建

仓库配置了 GitHub Actions（`.github/workflows/build.yml`），覆盖 6 个平台目标：

| 目标 | Runner |
| --- | --- |
| windows-x64 | `windows-latest` |
| windows-arm64 | `windows-11-arm` |
| linux-x64 | `ubuntu-latest` |
| linux-arm64 | `ubuntu-24.04-arm` |
| macos-x64 | `macos-15-intel` |
| macos-arm64 | `macos-15` |

- 推送 `v*` 标签 → 自动构建全部产物并发布 Release
- 在 Actions 页面手动触发 → 生成开发预发布版

## 常见问题

**ARM64 是什么？哪些设备用？**
ARM64（也称 aarch64）是一种 CPU 架构。Windows on ARM 设备（如 Surface Pro X、Surface Pro 11、Copilot+ PC）使用 ARM64 芯片。普通 Intel / AMD 处理器的电脑是 x64。不确定的话下载 x64 版本。

**Windows 提示“未知发布者” / SmartScreen 拦截？**
程序未做代码签名，Windows SmartScreen 可能提示未知发布者。点击“更多信息”→“仍要运行”即可。这不影响程序安全性，仅因未购买代码签名证书。

**Linux 运行提示权限不足？**
首次运行需添加可执行权限：`chmod +x TGVideoDownloader_linux_x64`。

**收不到验证码？**
验证码通过 Telegram App 内消息发送，请确保 Telegram 已登录且能接收消息。若多次请求，使用最新收到的验证码。

**换电脑怎么迁移？**
把 `TG视频下载器数据/` 文件夹整个随程序一起拷贝到新电脑即可，无需重新登录。

**下载的视频存在哪？**
默认在 `TG视频下载器数据/downloads/` 下按频道名建子目录。可在设置中修改下载目录。

## 隐私与安全

- **数据全在本地**：登录会话、配置、任务记录和下载的视频都保存在程序平级的 `TG视频下载器数据/` 目录，不上传任何服务器。
- **只连 Telegram**：网络通信全部经 Telethon（MTProto）与 Telegram 官方服务器交互，用于登录与下载；程序不含统计、广告或第三方数据上报代码，源码可自行审计。
- **保管好会话文件**：数据目录里的 `.session` 文件等同于登录凭证，不要分享给他人；换电脑时整体拷贝该目录即可免登录迁移。

## 合规与免责声明

本工具仅用于下载你有权访问的内容。请遵守当地法律法规与 [Telegram 服务条款](https://telegram.org/tos)，自行承担使用责任。

## License

[MIT License](LICENSE), Copyright (c) 2026 xmiqz

## 致谢

- [Telethon](https://github.com/LonamiWebs/Telethon) — asyncio MTProto 库，本项目的核心依赖
