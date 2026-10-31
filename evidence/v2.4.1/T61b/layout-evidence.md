# T61b 宽屏结果区栅格 — 验收证据

## 改动范围
`web/css/views/new.css` 一个文件，纯 CSS；DOM / JS / RPC / 移动端断点零改动。

- ≥1280px：`.nw-wiz:has(.nw-list)` 取消 760px 固定窄带（步骤 1 条件卡仍按原 760px）。
- ≥1280px：`.nw-list` 单列 flex → 双列栅格（`repeat(2, minmax(0, 1fr))`），gap 沿用原 `var(--space-2)`；
  `max-height` 由 44vh 放宽到 52vh（否则 1440×900 仅坐满 3 行，达不到 ≥8 条验收）。
- ≥1280px：`.nw-row` 内层模板由 4 列（末列无对应子元素）改 3 列，避免名称列被空列挤占；
  卡片 padding / 行高沿用原值（实测 106px，改动前后一致）。
- ≥1280px：工具条一行铺开，筛选框 `flex:1 1 auto; max-width:560px`，四个批量按钮右对齐到容器右缘。
- ≥1600px：三列 `repeat(3, minmax(0, 1fr))`。
- ≥1920px：三列 `repeat(3, minmax(0, 640px))` + `justify-content:start`，单卡片不超 640px。
- <1280px（含 640–1023 单列与 <640 手机）全部规则未触碰。

## 渲染方式
本机无浏览器自动化依赖（无 playwright/selenium），使用 Microsoft Edge headless 打开同构测量页：
- DOM 片段取自 `web/js/views/new.js` 的真实结构（`.nw-wiz > .nw-prog / .nw-toolbar / .nw-list > .nw-row[.nw-cbox /.nw-info(.nw-name/.nw-rename/.nw-sub) /.nw-side]`），12 条示意行；
- 样式表直接加载仓库真实 `tokens.css` / `ui.css` / `views/new.css`（改动前为 `HEAD` 版本，改动后为工作区版本）；
- 页面底部 `measure` 行为运行时实测 JSON（`visible` = 完整可见条目数，`row` = 卡片高度，`cols` = 栅格列宽）。

## 实测数字（改动前 → 改动后）
| 视口 | 列数 | 首屏完整可见条目 | 卡片高 | 列表宽 |
|------|------|------------------|--------|--------|
| 1920×1080 | 1 → 3（600px/列，≤640） | 4 → 12 | 106 → 106 | 760 → 1830 |
| 1440×900 | 1 → 2（673px/列） | 3 → 8 | 106 → 106 | 760 → 1358 |
| 390×844 | 1 → 1 | 不变 | 106 → 106 | 同一 PNG 哈希 |

390×844 改动前后 PNG 哈希完全相同：`CBDBF5DB7EA201EF739E79BA861971E11319D716C37C12A97239D2453F2D4A6F`，
即窄屏零回退。

## 截图
- `before-1920x1080.png` / `after-1920x1080.png`
- `before-1440x900.png` / `after-1440x900.png`
- `before-390x844.png` / `after-390x844.png`

说明：本机 Edge headless 的 CSS 视口高度 = 窗口高度 − 98px（窗口边框），为保证 1440×900 / 1920×1080 / 390×844
为真实 CSS 像素，截图按 `窗口高度 + 98` 渲染后裁切到目标尺寸。
