# T65 弦 XIAN 角色与九态 — 结果

日期：2026-11-01 · 分支 `v2.5-dev` · 基线 `9ad3983`（T64b）

## 1. 交付内容

| 文件 | 变更 | 行数 |
| --- | --- | --- |
| `web/js/motion/xian-invariants.js` | 新增：四项形体不变量 + 振翅投影高比（纯函数、零 DOM） | +135 |
| `web/js/motion/xian.js` | 新增：四控制点唯一几何 / 圆角路径 / 三层材质 / 九态状态机 / 速度自适应振翅 / 多任务聚合 `xianStateFor` | +427 |
| `web/css/views/xian.css` | 新增：状态色只染前缘、待机漂移令牌化、reduced-motion 双路归零、小尺寸前缘加粗 | +43 |
| `tests/js/xian.test.mjs` | 新增：18 个用例（几何带、投影高比、频率、DOM 层数、九态、降级、CSS 纪律） | +274 |
| `web/css/tokens.css` | `--dur-xian-idle`（6500ms / reduced 0ms） | +3 |
| `web/js/router.js` | 壳品牌位两处挂 `data-xian-brand`，reduced 判定后 `mountXian(30px)`，失败回退 `TG`；样式引导加 `xian` | +13 / −3 |
| `web/js/views/login.js` | 登录徽章内挂 `mountXian(40px)`，失败回退 `TG` | +5 / −1 |
| `web/js/views/active.js` | 总览卡标签行挂 `mountXian(34px)`；`xianStateFor(states, hintReason)` 驱动九态；离页归 idle 停 rAF | +16 / −2 |
| `web/js/views/editor.js` | 详情头部挂 `mountXian(44px)`；条目状态聚合（failed / pending / done） | +25 |
| `web/js/ui/toast.js` | 完成 Toast 用 16px 同源派生，失败回退 `✓` | +10 / −1 |
| `web/css/ui.css` | `.u-toast-xian` 定尺 16px | +2 |
| `web/css/views/active.css` | `.ac-ov-top` / `.ac-ov-xian` 定尺 34px | +12 |
| `web/css/views/editor.css` | `.ev-xian` 定尺 44px | +5 |

## 2. 实现口径（对齐任务书 §2 T65）

- **唯一几何**：`XIAN_GEOMETRY.canon` 四控制点（nose/wing/crown/keel）；上下翼共享前缘 nose→wing；圆角路径 `roundedPath` 由控制点实时生成，不存在第二份剪影。
- **圆角是比例**：`radius = 15.7` → 占 `min(宽,高)` 的 0.171（安全带 0.10–0.26），与设计稿 r≈15 同形。
- **振翅**：翼面绕前缘轴旋转，`FLAP_MAX = 6°`，禁 `scaleY`；`phase += dt/1000 / period * 2π`（先转秒）；`period = .80/(.78+.30*min(1,speed/1.35))` 落在 0.97–1.35Hz；静止/悬停/完成/限流不续帧。
- **三层封顶**：下翼蓝玻璃（`#8ADCF5(.58)→#2563EB(.46)→#1B4FC4(.42)`）/ 上翼乳白（`#F5FAFC(.98)→#DCEBF6→#B9D8EC`）/ 前缘亮边（`.7`，`sm` 尺寸 3.2）；高光并入渐变 stops，不画内部描边，无眼嘴。
- **九态**：`idle / scanning / downloading / sharding / floodwait / retrying / failed / done / uploaded`；`mapPhaseToXian` 覆盖内核全部 `dl_phase`，并发 ≥2 折叠 `sharding`，未知降级 `idle`；`xianStateFor` 聚合多任务快照：`flood_wait > refreshing > downloading/sharding > scanning > failed > done > idle`，`acct_hint.reason=flood_wait` 只在无下载活动时升为 floodwait。
- **降级**：`mountXian` 捕获构造异常返回 `null`；壳品牌位/登录徽章/Toast 回退文字或 `✓`，概览/详情仅不显示，不白屏、不影响功能。
- **reduced-motion**：构造参数 `reduced` 冻结振翅；CSS 双路（`@media (prefers-reduced-motion)` 与 `#sh-root[data-v22-rm="on"]`）停用待机漂移；终态与状态色保留。

## 3. 接入点

| 接入点 | 尺寸 | 状态来源 |
| --- | --- | --- |
| 壳品牌位（侧栏 + 抽屉） | 30px | 常驻 `idle`（待机漂移为唯一允许纯循环） |
| 登录徽章 | 40px | 常驻 `idle` |
| 下载中概览 | 34px | `active_states` 快照 + `acct_hint` → `xianStateFor` |
| 任务详情头部 | 44px | 条目真实状态聚合（failed / pending / done） |
| 完成 Toast | 16px | `done`（`data-xian-size="sm"` 前缘加粗） |

## 4. 证据与截图

| 内容 | 文件 |
| --- | --- |
| 九态 × 三宿主（浅色） | `shot_matrix_light.png` |
| 九态 × 三宿主（深色） | `shot_matrix_dark.png` |
| 九态 × 三宿主（reduced-motion） | `shot_matrix_reduced.png` |
| 九态 96px 色彩取证（浅色） | `shot_states_light.png` |
| 九态 96px 色彩取证（深色） | `shot_states_dark.png` |
| 只读探针（不变量/频率/层数/红线） | `probe.txt` |
| 渲染夹具 | `harness.html` |

取证方式：`python -m http.server 8911` 静态托管仓库，Edge `--headless=new --screenshot --hide-scrollbars` 渲染 `harness.html?mode=matrix|hero&theme=light|dark&rm=0|1`；夹具直接消费生产 `web/js/motion/xian.js`、`xian-invariants.js` 与 `tokens.css`/`views/xian.css`。截图与日志只含状态名与占位尺寸，无频道名、文件名、正文、token。

## 5. 门规结果

```
python -m pytest tests -q            → 334 passed
node --test "tests/js/*.test.mjs"    → 130 tests / 129 pass / 1 todo / 0 fail（新增 18）
python -c "...len(server_app.RPC_METHODS)" → 56
```

红线自查：RPC 恰 56；任务 JSON 顶层 7 键未动；bundle 版本 1 未动；WS 帧 ≤40 未动；`_PHASE_NEXT` 未改；不做存量任务回捞；证据与日志不含正文/频道名/文件名/token。
