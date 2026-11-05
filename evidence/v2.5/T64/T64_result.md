# T64 动效框架与令牌 — 证据

## 改动

| 文件 | 改动 |
| --- | --- |
| `web/css/tokens.css` | §1.9 新增动效令牌（`--dur-count/-flash/-toast-out/-progress/-indet/-ambient/-spin` 与四档 pulse）与 `--ease-spring-li: linear(…)`；§5.1 reduced-motion 全部归零、弹簧降级 `linear` |
| `web/css/ui.css` | 四处裸时长换令牌（toast 离场/空态呼吸/确定与不定进度）；分段控件选中态改独立 `.u-seg-thumb`（读 `offsetLeft/offsetWidth` 后只写 transform/width）；`.still` 抑制滑行；`sh-side` / `sh-bottombar` / `sh-viewhost` 三段 `view-transition-name`；无 VT 引擎的 `.u-view-enter` 淡入（`fill: backwards`） |
| `web/css/views/active.css` | 六处裸时长换令牌（阶段点/抓取点/进度/旋转）；新增 `.ac-cfill.is-flash` 完成闪烁（`var(--dur-flash)`），reduced-motion 归零 |
| `web/js/motion/calm.js` | 新建：`COUNT_MS` / `easeOutQuart` / `rollPlan` / `prefersReduced` / `markStill` / `createRoller`（飞行中 `set()` 只改目标，不重启计时） |
| `web/js/motion/scroll-guard.js` | 新建：`isReaderInput`（仅 `isTrusted`）/ `windowFor`（触摸惯性 1500ms）/ `createGuard`（三类非读者滚动还原）/ `createScrollGuard`（挂载即记基线，`destroy()` 全摘） |
| `web/js/router.js` | 视图替换包 `startViewTransition`（`canViewTransition` / `runViewTransition` 导出，无引擎/抛错同步直切）；旧视图节点延迟到 swap 内摘除；无引擎补淡入并在 `animationend` 摘类；`boot()` 安装滚动守卫 + `markStill`；路由复位前给守卫开读者窗口 |
| `web/js/ui/segmented.js` | 内置常驻 thumb 与 `syncThumb()`（不新增命名导出，桶仍恰 17 个） |
| `web/js/views/active.js` | 总览三个读数接 `createRoller`（620ms 四段缓出；事件帧只改目标）；卡片完成跃迁触发 `.ac-cfill.is-flash` 并在 1200ms 后有摘类 |
| `tests/js/motion-tokens.test.mjs` | 新增 18 条（CSS 裸值审计、令牌/归零、thumb、VT 命名、闪烁、节拍纯函数、roller 注入时钟、VT 包裹三态） |
| `tests/js/scroll-guard.test.mjs` | 新增 12 条（读者输入判定、三类非读者还原、触摸惯性、安装器/卸载/降级） |

## 探针（`probe.txt`，仅计数）

- 本波自有 CSS 裸 `ms`/`s` 时长：`ui.css=0`、`views/active.css=0`
- 本波自有 CSS 裸 `cubic-bezier`：`ui.css=0`、`views/active.css=0`
- 动效令牌声明 30 处（含既有 `--dur-xs/sm/md/lg`）；reduced-motion 归零 15 处
- `--ease-spring-li: linear(…)` 存在；`.still` 抑制 thumb 滑行；`view-transition-name` 3 处

## 门规

- `python -m pytest -q`：334 passed（`pytest.log`）
- `node --test "tests/js/*.test.mjs"`：101 pass + 1 todo + 0 fail（`node.log`，基线 72 → 102）
- `RPC_METHODS`：56（未增删）
- 任务 JSON 顶层键/`BUNDLE_VERSION`/`_PHASE_NEXT` 未触碰（T64 无 Python 改动）
