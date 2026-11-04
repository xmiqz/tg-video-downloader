# TVG v2.5.1 Windows x64 实验构建结果

- 日期：2026-11-04
- 基线：`v2.5.0` / `c04449c`
- 分支：`v2.5-argon-glass`
- 版本戳：`v2.5.1`
- 目的：在 TVG 现有玻璃材质上建立 `nav / panel / overlay / inset` 四层语义，不改成 Argon 整体外观。

## 实现

- `web/css/glass-system.css`：四层玻璃 token 与材质规则
- `web/js/router.js`：在所有视图 CSS 之后加载 `glass-system.css`
- `web/taskedit.html` / `web/setup.html`：同步加载
- 移动底栏保留原有 `nav` 识别度；桌面侧栏、抽屉对齐同一套材质参数
- 账号脚注改为 nav 内透明内容行，去掉第二层白色模糊
- panel 按 `data-lum` 选择暗/亮透明材质；桌面关闭空间取景采样，去掉侧栏 hover 拉进卡片左缘的亮条
- 中性按钮、分段选择框和文本输入复用 nav 玻璃，提供 `xs / sm / md / lg` 四级高度
- panel 恢复上一版亮度感知玻璃、1px 轻边、单层顶部高光；彩虹仅保留在移动底栏
- 分段选择框 thumb 改为中性液态玻璃环（轻 tint、1px 镜面渐变边、3px 局部模糊）
- 分段选择框首屏等待真实布局尺寸后再显示 thumb，修复零宽时只剩左侧细竖条
- 独立任务编辑页补齐 `tokens.css`，筛选分段接入共享 control 与中性液态玻璃 thumb，批量条进入 overlay
- 移动底栏新增独立滑动指示器，页签切换按 transform/width 弹簧位移；FAB/登录不参与
- 卡片内视频块、任务行、队列块降为无模糊 `inset`
- 登录、确认、详情、Toast、动作条统一为 `overlay`
- 壁纸主题增加阅读底并移除玻璃面板内光晕
- 暗壁纸下舞台外壳从 `inset` 提升为 `panel`，并增加独立暗色阅读底；视频块/任务行仍保持 `inset`
- 首次设置页品牌位改为同源 `xian-icon.svg`
- 登录与账号弹层底部显示版本号 + 构建号，构建信息进入卡片内部
- `package.json` / `CHANGELOG.md` / `release-notes/v2.5.1.md` 更新

## 自动化

- 前端：`167 tests / 167 passed / 0 todo / 0 failed`
- 后端：`348 passed, 3 warnings`
- 日志：
  - `evidence/v2.5.1/r9/frontend-final.log`
  - `evidence/v2.5.1/r9/backend-final.log`
  - `evidence/v2.5.1/r9/visual-smoke.log`

## 视觉夹具

- `evidence/v2.5/argon/r5-seg-initial.png`
- `evidence/v2.5/argon/taskedit-controls-preview-2.png`
- `evidence/v2.5/argon/r4-preview-mobile.png`
- `evidence/v2.5/argon/unified-final-bright-v251.png`
- `evidence/v2.5.1/r9/active-wallpaper-stage-desktop.png`
- `evidence/v2.5.1/r9/active-wallpaper-stage-mobile.png`
- `evidence/v2.5.1/r9/login-build-footer.png`
- `evidence/v2.5.1/r9/setup-shared-icon.png`
- 对照：`stock-*.png`、`argon-*.png`

## 产物

- 目录：`C:\Users\xqz\Documents\Codex\2026-11-04\tvg-v2.5.1-manual\windows-x64-r8`
- ZIP：`C:\Users\xqz\Documents\Codex\2026-11-04\tvg-v2.5.1-manual\TVG-v2.5.1-windows-x64-r8.zip`
- 桌面副本：`C:\Users\xqz\Desktop\TVG-v2.5.1-windows-x64-r8.zip`
- r9 目录：`C:\Users\xqz\Documents\Codex\2026-11-04\tvg-v2.5.1-manual\windows-x64-r9`
- r9 GUI SHA-256：`5D52DD9499C71CA1B15535FD1571A254C1ABDB3305B6F10A92D33B65FE16A466`
- r9 TUI SHA-256：`369A465E3500BF21070403392E839C5A4E6C37570DE77FDDEDF6BF6086F4E547`
- r9 ZIP：`C:\Users\xqz\Documents\Codex\2026-11-04\tvg-v2.5.1-manual\TVG-v2.5.1-windows-x64-r9.zip`
- r9 ZIP SHA-256：`33CA0939882B5006E0983E12850BA29FB3D49758288AEBC4C037916B17EE3B0D`
- r9 构建日志：`evidence/v2.5.1/r9/windows-x64-build-r9.log`

## 飞牛 ARM64 测试实例

- 地址：`http://192.168.2.111:18793`
- 容器：`tg-video-downloader-v251-data-test`
- 镜像：`tgvd-v2.5.1-local:arm64-r9`
- 数据目录：`/volume1/docker/tgvd-v251-r8-test/data-from-v25-1559`
- `/healthz`：`{"ok":true,"service":"tg-video-downloader","version":"v2.5.1","build":"r9"}`
- v2.5.0 正式测试容器仍健康：`http://192.168.2.111:8787`

## 未验证

- Windows WebView2 真机视觉矩阵
- 真实 Telegram 登录/下载流程
- 低端 GPU 性能、HiDPI/125%/150% 全矩阵
- 当前 worktree 有 v2.5.0 index CRLF 噪声；实际代码差异以 `git diff --ignore-space-at-eol` 为准
