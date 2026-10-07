# TG 视频下载器

跨平台 Telegram 频道视频批量下载工具。纯 Python 实现，基于 [Telethon](https://github.com/LonamiWebs/Telethon)，无需任何外部下载器或 ffmpeg。

支持 **Windows（x64 / ARM64）、Linux（x64 / ARM64）、macOS（Intel / Apple Silicon）**。

## 特性

- 按频道批量下载视频，支持 1GB 以上大文件
- 服务端关键词搜索（与 Telegram App 内搜索同一机制），支持包含/排除词，可交互勾选
- 可选同时下载路数；单视频内部多连接分片并行
- **任务级断点续传**：关机、关窗口后重启可继续未完成任务；分片不重下，失败视频可单独重试
- 下载面板：进度、实时速度、剩余时间、消息原文，按 `I` 切换简洁/详细，自适应窗口大小
- `S` 跳过当前视频，`Q` 中止全部
- 下载完成自动无损前置 MP4 moov（快速起播，不重编码）
- 下载前自动检查磁盘空间

## 快速开始

到 [Releases](https://github.com/xmiqz/tg-video-downloader/releases) 下载对应平台文件：

| 系统 | 文件 |
| --- | --- |
| Windows x64 | `TG视频下载器.exe` |
| Windows ARM64 | `TG视频下载器_ARM64.exe` |
| Linux x64 | `TG视频下载器_linux_x64` |
| Linux ARM64 | `TG视频下载器_linux_arm64` |
| macOS Intel / Apple Silicon | `TG视频下载器_mac_x64` / `TG视频下载器_mac_arm64`（另附 `.command`，双击启动） |

Linux 下首次运行：

```bash
chmod +x TG视频下载器_linux_x64
./TG视频下载器_linux_x64
```

首次启动输入手机号（含国家区号，不带 `+`）与 Telegram 发来的验证码即可。程序内置官方公开客户端凭证，无需自行申请 API；也可在设置中替换为自己的凭证。

## 数据目录

程序启动时会在自身平级目录生成 `TG视频下载器数据/`：

```
TG视频下载器数据/
├── tg_config.json      # 账号、凭证、下载目录等配置
├── tg_session.session  # 登录会话（免重复登录）
├── tasks/              # 任务清单
└── downloads/          # 下载的视频（默认目录，可修改）
```

整个数据目录随程序一起拷贝即可换电脑使用。

## 从源码运行

需要 Python 3.10+：

```bash
pip install telethon
python tg_video_dl.py
```

## 自行打包

```bash
pip install pyinstaller
python build.py
```

PyInstaller 不支持交叉编译：目标平台的包需在该平台上构建。仓库已配置 GitHub Actions，推送 `v*` 标签即自动构建全部 6 个平台产物并发布 Release。

## 隐私

所有数据仅保存在本地数据目录，不会上传到任何第三方服务器。`.gitignore` 已确保账号会话、配置、任务与下载内容不会进入版本库。

## 免责声明

本工具仅用于下载你有权访问的内容，请遵守当地法律法规与 Telegram 服务条款，自行承担使用责任。
