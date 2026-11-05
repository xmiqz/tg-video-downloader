# AC-24 动效框架不回归 — 验收结果（T68）

日期：2026-11-01 · 分支 `v2.5-dev` · 基线 `513b5f0`（T66）

## 1. 本轮改动

| 文件 | 变更 | 行数 |
| --- | --- | --- |
| `web/css/tokens.css` | 修复系统降级块（减弱动效 / 无滤镜 / 小屏降模糊）与显式主题块的特异性冲突 | +10 / −4 |
| `evidence/v2.5/acceptance/harness.html` | 新增：验收夹具（消费生产 `stage.js` / `xian.js` / `tokens.css` / `ui.css` / `xian.css` / `stage.css` / `active.css`） | 新建 |
| `evidence/v2.5/acceptance/matrix_probe.txt` | 新增：16 格矩阵探针结果 | 新建 |
| `evidence/v2.5/acceptance/matrix/*.png` | 新增：16 格 × 中途/完成 静帧截图 32 张 | 新建 |
| `evidence/v2.5/acceptance/webview2/` | 新增：WebView2 原生壳冒烟证据 | 新建 |

## 2. 发现并修复的真实缺陷

**现象**：`rm=1` 格仍出现 2 个有限动画，`--dur-sm` 计算值仍为 200ms；系统「减少动态效果」在显式主题下不生效。

**定位**（CSSOM 交叉验证，非猜测）：

- 主题基础块选择器为 `:root, :root[data-theme="light"]` → 特异性 (0,2,0)。
- 降级块（§5.1 减弱动效 / §5.2 无 `backdrop-filter`）原为 `:root` → (0,1,0)。
- 生产 `web/index.html` 启动即写 `data-theme="light"`，router 随后写 `light/dark/wallpaper`，因此显式主题恒定压过降级块。

**修复**：

- §5.1、§5.2 改为 `:root, :root[data-theme]`，与显式主题块同特异性，靠后置顺序胜出。
- §5.3（小屏降模糊）**只对浅色生效**（` :root, :root[data-theme="light"] `）：该块同时定义浅色玻璃原色 `--gl-g-a/b/c`，若对深色生效会把深色玻璃染成浅色。

修复后 rm 格 `document.getAnimations().total = 0`，`--dur-sm = 0ms`。

## 3. 验收矩阵（AC-24）

口径：360×800 / 768×1024 / 1440×900 / 1920×1080 × 浅/深 × reduced-motion 0/1，共 16 格；Chromium 内核（Edge 156）CDP 精确视口，`Emulation.setDeviceMetricsOverride` + `setEmulatedMedia(prefers-reduced-motion)`。

| 视口 | 档位 | 舞台（宽×高） | 状态带高 | 卡片 | 队列条 | 点击漂移（stage/item/card/scroll） | 横向滚动 | rm0 动画 | rm1 动画 | 结果 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 360×800 | mobile | 302×460.2 | 102 | 2 | 3 | 0 / 0 / 0 / 0 | false | 3（全部 idle-drift） | 0 | PASS |
| 768×1024 | tablet | 694×280.5 | 58 | 2 | 3 | 0 / 0 / 0 / 0 | false | 3（全部 idle-drift） | 0 | PASS |
| 1440×900 | desktop | 680×274.9 | 58 | 2 | 3 | 0 / 0 / 0 / 0 | false | 3（全部 idle-drift） | 0 | PASS |
| 1920×1080 | desktop | 760×307.2 | 58 | 4 | 3 | 0 / 0 / 0 / 0 | false | 3（全部 idle-drift） | 0 | PASS |

- 深浅两主题数值一致，表中不重复列；全部 16 格 `ALL PASS 16/16`（`matrix_probe.txt` 末行）。
- 三拍探针（布局稳定 geo / 5.2s 终态 final / 点击后 post）均为：`hscroll=false`、关键容器无越界、卡片不越舱、状态带不压舞台（`overlap=0`）、`speed`/`eta` 定宽标签无裁切。
- 终态断言：状态带进度填充 `fillPct≥99`、`fillStyle=100%`、弦 `heroState=idle`，即 3.0s 完成事件确实落到终态而非停在过场。
- 768 档按设计省略弹性标签 `.ac-stage-counts`（CSS 自带 ellipsis），不计为失败；`speed`/`eta` 不属于弹性标签。
- 1920 深色 rm0 单格有限动画落定耗时 250ms（补帧推进），`active=0` 无残留；其余格 0ms。

## 4. 动画残留判定口径（headless 出帧限制）

- `finite` 用 `isFinite(iterations)` 判定（`Infinity` 也是 number，不能用 `typeof`）。
- `active`（`currentTime>0` 未完成）= 真残留；`pending`（`currentTime===0`）= 已创建未启动。
- headless 按需出帧，无持续 vsync 时新建过渡会停在 `ct=0`；探针在 settle 循环中用 `Page.captureScreenshot` 补帧（等价真实浏览器帧推进），终止条件为 `active=0 且 pending=0`，上限 15 轮。
- 非 rm 格允许的无限动画仅 `animation:xian-idle-drift`（壳品牌/弦待机漂移）；rm 格要求 `total=0`。

## 5. 截图矩阵

- 32 张：`matrix/acc_<视口>_<主题>_rm<n>.png`（下载中 75%）与 `_done.png`（完成 100%）。
- 出图方式：每格独立 Edge 进程 + CDP 精确视口 + `?freeze=mid|done` 静帧，避开浏览器的按需出帧与页面浮层混入。
- 已目检 360 深/浅、768 浅 rm1、1440 深、1920 浅：版式完整、无横向滚动、无浏览器浮层；360 格实测 `innerWidth=360 / docW=360 / hscroll=false`。

## 6. 引擎覆盖

| 引擎 | 覆盖 | 证据 |
| --- | --- | --- |
| Chromium（Edge 156.0.4314.58） | 16 格矩阵 + 32 张截图，全绿 | `matrix_probe.txt` / `matrix/` |
| WebView2（156.0.4314.58，ARM64 原生壳） | 冒烟：loader 加载、环境创建、设置回读、导航、窗口截图、关闭释放全通过（`selftest_summary ok=true`） | `webview2/t4_shell_dark.png`（2386×1631）/ `webview2/tr41_bounds.log` / `webview2/stub_access.log` |
| WebKit | 未覆盖：Windows 取证环境无 WebKit（任务书「可用则加」） | — |

**口径说明（如实标注）**：WebView2 自测加载的是最小冒烟桩页（`index.html` + `style.css` + `app.js`），用于验证原生壳与 WebView2 运行时的创建/渲染/截图/释放链路，**未在 WebView2 内重跑 16 格矩阵**；同版本 Chromium 内核矩阵已覆盖渲染与动画行为，WebView2 内的完整矩阵列为发版冒烟补测项。

## 7. 门规结果

```
python -m pytest tests -q                → 348 passed
node --test "tests/js/*.test.mjs"        → 150 tests / 149 pass / 1 todo / 0 fail
python -c "...len(server_app.RPC_METHODS)" → 56
```

红线自查：RPC 恰 56；任务 JSON 顶层 7 键未动；bundle 版本 1 未动；WS 帧 ≤40 未动；`_PHASE_NEXT` 未改；不做存量任务回捞；证据与日志不含正文、频道名、真实文件名、token（夹具任务名为「任务 · …」、条目名为「视频 N.mp4」占位）。

## 8. 偏差与说明

- **AC-22 状态数口径**：AC 锚点文字为「十种状态」，任务书 §T65 与 `需求与目标.md` 均定义为**九态**（idle / scanning / downloading / sharding / floodwait / retrying / failed / done / uploaded）。实现按任务书九态交付，截图矩阵按九态 × 三宿主 × 深浅/rm 出具；AC-22 为 rubric（1–5）主观评分，本文件只提供截图与锚点对照，不代评审打分。
- **浏览器侧浮层**：桌面深色格曾出现页面 DOM 外的粉色圆标（Edge AI/Copilot 类浮层，`elementFromPoint` 仅返回 `#sh-root`），判定为浏览器 UI 而非产品缺陷；改用「每格独立进程 + 静帧截图」后消失，证据图中无该浮层。
- **产物截图的中途/完成两态为静帧**：动效过程证据由 T66 的 APNG 录屏承担，本矩阵只证明布局与终态不回归。

## 附：v2.5 动效 AC 索引

| AC | 判定 | 证据 | 状态 |
| --- | --- | --- | --- |
| AC-22 吉祥物生命感与状态语义（rubric） | 九态 × 三宿主 × 深浅 / rm 截图矩阵 | `evidence/v2.5/T65/shot_matrix_light.png`、`shot_matrix_dark.png`、`shot_matrix_reduced.png`、`shot_states_light.png`、`shot_states_dark.png` | 证据齐备，评分待发版评审 |
| AC-22a 弦的变形不变量 | 四项不变量 + 振翅投影高比，纯函数自检含越界预设报红 | `evidence/v2.5/T65/probe.txt` | 通过 |
| AC-22b 材质三层封顶 | DOM 层数断言 + 渐变 id 唯一 + 无 sheen/fold | `evidence/v2.5/T65/probe.txt` | 通过 |
| AC-23 下载链路舞台正确性与性能 | 16 格场景图 + 阶段映射契约测试 + 路径贴合 | `evidence/v2.5/T66/probe.txt`、`recording_T66.png`、`tests/test_stage_contract.py` | 通过 |
| AC-23a 六项物理齐备 | 速度规划 / squash & stretch / banking / anticipation / 尾迹 / 接收反馈采样 | `evidence/v2.5/T66/probe.txt` | 通过 |
| AC-23b 循环动效频率可读 | 巡航 1.35Hz / 低速 0.97Hz / >3Hz 零命中 | `evidence/v2.5/T65/probe.txt` | 通过 |
| AC-24 动效框架不回归 | 16 格 × 三拍探针 + 32 张截图 + 终态断言 | 本文件 / `matrix_probe.txt` / `matrix/` | 通过 |
