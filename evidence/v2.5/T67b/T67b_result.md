# T67b 登录页版本构建号与项目地址 — 结果

日期：2026-11-01 · 分支 `v2.5-dev` · 代码基线 `161e584`（本任务主体）· 本批收口提交 `HEAD` 前

## 1. 交付内容

| 文件 | 变更 | 行数 |
| --- | --- | --- |
| `server_config.py` | `PROJECT_URL` / `app_version()` / `app_build()` / `build_info()`（`161e584` 已落） | 已存在 |
| `server_app.py` | `/healthz`、`/auth/status` 各带 `**build_info()`（`161e584` 已落） | 已存在 |
| `Dockerfile` / `compose.yaml` / `.github/workflows/container.yml` | `ARG`→`ENV` 注入与透传（`161e584` 已落） | 已存在 |
| `web/js/build-label.js` | 纯函数 `buildLabel(status)`：缺值回落 `dev`（`161e584` 已落） | 已存在 |
| `web/js/setup.js` | 拉 `/auth/status` → 写页脚（`161e584` 已落） | 已存在 |
| `tests/test_build_info.py` / `tests/js/build-label.test.mjs` | 版本/构建号契约（`161e584` 已落） | 已存在 |
| `web/setup.html` | 页脚链接文案 `GitHub` → `GitHub ↗` | +1 / −1 |
| `web/css/setup.css` | `setup-shell` 改两行网格：首行居中卡片、末行页脚贴底；补 `minmax(0, 1fr)` 列约束 | +6 / −4 |

本批只做两处形态收尾（页脚贴着卡片中心而非页面底部、链接缺外链指示符），
其余实现与测试在 `161e584` 已合入；本次提交为证据与收口。

## 2. 实现口径（对齐任务书 §2 T67b）

- **页脚位置**：初版 `flex column + justify-content:center` 让页脚紧跟在卡片下方，
  不是「页面最下方一行」。改为 `grid-template-rows: 1fr auto`：
  首行吃掉剩余高度并把卡片垂直居中，末行固定给页脚，`align-items:center`
  让页脚内容在行内居中、行本身贴安全边距底部。
- **窄屏不越界**：列模板 `minmax(0, 1fr)` + 卡片 `min-width:0`，
  避免 390 宽下卡片按内容最小宽度撑破视口（实测 `scrollWidth == innerWidth`）。
- **文案**：`buildLabel()` 逐条覆盖 `dev`（无注入）、纯版本、版本+短 SHA 三种形态；
  服务端 `app_build()` 用 `BUILD_SHA_LENGTH=7` 截断，`version`/`build` 均为空时
  前端回落 `dev`，不会出现空行或 `undefined`。
- **不新增接口**：页脚数据复用 `/auth/status`，RPC 仍为 56。

## 3. 实测（详表见 `probe.txt`）

- tag 构建（`TGDL_VERSION=v2.5.0-beta.1`、`TGDL_BUILD=9f3c1a7d…`）：
  `/auth/status` 与 `/healthz` 均含 `version/build/project` 三字段，
  `build` 为 `9f3c1a7`；页脚文本 `v2.5.0-beta.1 · build 9f3c1a7` + `GitHub ↗`。
- 源码直跑（未注入 `TGDL_*`）：两接口 `version="dev"`、`build=""`；页脚文本 `dev`。
- 截图方法：Edge `--headless=new --window-size=390,844` 存在最小外框宽度钳制
  （实测 CSS 视口变成 `492x747`，截出的 390 宽图右侧被裁），
  故四档统一改用 CDP `Emulation.setDeviceMetricsOverride` 精确设视口，dpr=1。

## 4. 目检

| 内容 | 文件 |
| --- | --- |
| 1440×900 浅色：卡片居中、页脚贴底、tag+短 SHA | `shot_1440_light.png` |
| 1440×900 深色 | `shot_1440_dark.png` |
| 390×844 浅色：卡片完整不越界、进度条六段、页脚贴底 | `shot_390_light.png` |
| 390×844 深色 | `shot_390_dark.png` |
| 390×844 浅色 dev 模式：页脚仅 `dev` | `shot_390_light_dev.png` |

## 5. 回归

| 项 | 结果 | 文件 |
| --- | --- | --- |
| pytest | 341 passed, 4 warnings | `pytest.log` |
| node `--test tests/js` | 130 tests / 129 pass / 1 todo / 0 fail | `node.log` |
| RPC 计数 | 56（未变） | `probe.txt` |

## 6. 残余与说明

- 本批未在真实镜像内跑页脚（镜像构建与发版归 T68 / 主会话），tag 形态用
  `TGDL_VERSION`/`TGDL_BUILD` 同字面注入等价复现。
- 深浅两套由 `Emulation.setEmulatedMedia(prefers-color-scheme)` 驱动，
  与页面自身「系统深色跟随 + localStorage 显式覆盖」双路一致。
