# TVG v2.5.1 r9 验证记录

## 自动化

- `node --test "tests/js/*.test.mjs"`：`167 tests / 167 passed / 0 failed / 0 todo`
- `python -m pytest -q -p no:cacheprovider --basetemp=...`：`348 passed / 3 warnings`
- 图标与构建信息专项：`10 passed`

## 浏览器 smoke

- 每日壁纸暗底：舞台 `data-lum=dark` 命中，`.xian-stage-bay` 使用 panel + 暗色阅读底。
- 登录弹层：构建信息位于 `.lg-login-card` 内，文本为 `v2.5.1 · build r9`。
- 首次设置：`.setup-logo` 为 `IMG /assets/xian-icon.svg`，自然宽度 256。
- 运行时错误：`errors=[]`，`consoleErrors=[]`，`failed=[]`。
- 夹具：
  - `evidence/v2.5.1/r9/active-wallpaper-stage-desktop.png`
  - `evidence/v2.5.1/r9/active-wallpaper-stage-mobile.png`
  - `evidence/v2.5.1/r9/login-build-footer.png`
  - `evidence/v2.5.1/r9/setup-shared-icon.png`

## Windows x64 r9

- GUI SHA-256：`5D52DD9499C71CA1B15535FD1571A254C1ABDB3305B6F10A92D33B65FE16A466`
- TUI SHA-256：`369A465E3500BF21070403392E839C5A4E6C37570DE77FDDEDF6BF6086F4E547`
- ZIP SHA-256：`33CA0939882B5006E0983E12850BA29FB3D49758288AEBC4C037916B17EE3B0D`
- EXE 内 `assets/version.json`：`{"version":"v2.5.1","build":"r9"}`

## 飞牛 ARM64

- `http://192.168.2.111:18793/healthz` 返回 v2.5.1 / r9。
- 容器为 healthy，数据目录沿用 v2.5 快照。
- v2.5.0 容器 `http://192.168.2.111:8787` 仍 healthy。

## 仍未验证

- Windows WebView2 真机视觉矩阵。
- 真实 Telegram 登录/下载全流程。
- 低端 GPU、HiDPI/125%/150% 全矩阵。
