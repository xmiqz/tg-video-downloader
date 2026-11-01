# T66 下载链路舞台（FR-56）— 结果

日期：2026-11-01 · 分支 `v2.5-dev` · 基线 `13e0250`（T67b）

## 1. 交付内容

| 文件 | 变更 | 行数 |
| --- | --- | --- |
| `web/js/motion/stage.js` | 新增：10 阶段 / 30 条阶段边 / 六项物理纯函数 / 预采样曲线 / 队列与槽位 / 舞台实例 | +1389 |
| `web/css/views/stage.css` | 新增：四档宿主与任务舱 / 队列条 / 卡片 / 槽位 / 深色与 reduced-motion | +750 |
| `tests/js/stage.test.mjs` | 新增：20 个用例（映射、物理、队列、槽位、空事件、幂等） | +450 |
| `tests/test_stage_contract.py` | 新增：7 个用例（解析内核 `_PHASE_NEXT` 与 `STAGE_EDGES` 逐边核对） | +116 |
| `web/js/views/active.js` | 概览三卡 → 舞台 + 状态带；`buildStageScene` 纯函数；折叠记忆；`resize` 切档；`dl_phase` 事件先行推进 | +257 / −34 |
| `web/css/views/active.css` | 删除三张统计卡样式，新增状态带（进度 180 / 计数 / 速度 / 剩余 / 折叠）、收起态三段、窄屏换行 | +88 / −51 |
| `web/js/router.js` | 视图样式引导加 `stage`（升级后不复用旧 CSS） | +1 / −1 |
| `tests/js/motion-tokens.test.mjs` | `stage.css` 纳入「令牌 / 无裸时长 / 无裸 cubic-bezier」纪律检查 | +1 |

## 2. 实现口径（对齐任务书 §T66）

- **只消费事件**：输入为 `active_states` 快照 + `acct_hint`（`tg:dl_phase` 事件先于轮询到达时，用临时焦点任务补一帧，不写回快照）；`buildStageScene` 为纯函数，零 DOM、零查询、零后端进度推算。
- **阶段机一致**：`STAGE_PHASES` 10 态、`STAGE_EDGES` 30 条；`test_stage_contract.py` 解析内核 `tg_video_dl._PHASE_NEXT`（2070–2087 行）并逐边比对，终态无出边、无自环、无重复。
- **六项物理**：梯形速度（22/48/30，峰值 1.3514×，端点归零且单调）、squash & stretch（纵向限定）、banking ±24° 一阶滞后、anticipation 90ms（−6°/2.2px）、速度尾迹 0→35px、接收反馈（吸气 1.03 / 失败横抖 4 下 / 完成 squash）；跃迁只播一次，跨阶段直接进终态。
- **性能**：曲线启动时 `sampleCubicPath(d, 8)` 预采样；飞行帧只查预采样表，每帧 `getPointAtLength` 0 次；rAF 仅在 `busy()` 时请求，落地/暂停/离页/收起即停；屏幕外 `content-visibility: auto`。
- **队列规则**：排序 下载中 > FloodWait > 重试 > 暂停 > 失败 > 完成；最多 3 条 + 「另有 N 条」，点开就地展开/切换主任务（不跳页、不重排舞台）；焦点任务消失回落到队列首位。
- **槽位规则**：并发 ≤1 隐藏；1–4 渲染 4 槽；5–8 按实际；>8 折叠 8 槽 + 「×N」；亮灯只由真实分片状态驱动，空闲槽不呼吸。
- **展示上限**：移动/平板/紧凑 2 张；桌面 <1920 为 2 张 + 精确聚合，≥1920 为 4 张；失败项置顶始终可见；错误并入读数行单行省略（`data-has-error`），失败卡不多占一行。
- **取消三卡**：`总体进度 / 总速度 / 剩余时间` 并入状态带一行；进度条固定 180px（窄屏 120px 自适应）；手动收起记 `localStorage`（仅 UI 偏好），收起后只剩状态带三段。
- **降级**：`createStage` 构造失败返回 `null`，`active` 视图仅不显示舞台；`breakpoint` 跨档只 `setLayout`，不重建实例、不重放跃迁。

## 3. 尺寸矩阵实测（16 格全绿）

| 视口 | 舞台（宽×高） | 队列 | 视频块 | 状态带 |
| --- | --- | --- | --- | --- |
| 390×844 | 329.2×501.6（≤60vh 506.4） | 3×132×28，横向胶囊 | 2×64 | 82（两行） |
| 768×1024 | 720×291.1（任务书 260–320） | 换行至舱底，item 160×28 | 2×41.7 | 58 |
| 1024×768 | 590×238.5（200–240） | 2×160×28 + 「另有 3 条」 | 2×40.8 | 58 |
| 1280×800 | 640×258.7（220–260） | 3×180×28 | 2×47.8 | 58 |
| 1440×900 | 680×274.9（260–300） | 3×180×28（展开 5 条滚动可达） | 2×61.5 | 58 |
| 1920×1080 | 760×307.2（280–320） | 3×180×28 | 4×40.8 | 58 |
| 2560×1440 | 760×307.2 | 3×180×28 | 4×40.8 | 58 |

逐格探针（每格截图前 `Runtime.evaluate`）：`hscroll=false`、`docW=视口宽`、`chipOverlap=0`、`bandOverStage=0`、`outOfBay=0`、`cardCut=0`、固定标签 `clipped=[]`。另覆盖 `1440 展开 / 390 展开 / 1440 深色 / 390 深色 / 1440 reduced-motion / flood / single / done / folded` 共 16 格。

## 4. 证据

| 内容 | 文件 |
| --- | --- |
| 七格尺寸矩阵 + 扩展场景（16 张成品图） | `shot_390x844.png` / `shot_768x1024.png` / `shot_1024x768.png` / `shot_1280x800.png` / `shot_1440x900.png` / `shot_1920x1080.png` / `shot_2560x1440.png` / `shot_1440x900_expand.png` / `shot_390x844_expand.png` / `shot_1440x900_dark.png` / `shot_390x844_dark.png` / `shot_1440x900_reduced.png` / `shot_1440x900_flood.png` / `shot_1440x900_single.png` / `shot_1440x900_done.png` / `shot_1440x900_folded.png` |
| 逐格几何探针 | `probe.txt` |
| 录屏（4 并发 + 1 FloodWait + 1 重试 → 完成） | `recording_T66.png`（APNG：20 帧 × 300ms，720×450，无限循环） |
| 渲染夹具 | `harness.html` |
| 测试日志 | `pytest.log` / `node.log` |

取证方式：仓库根 `python -m http.server 8933` 静态托管；Edge `--headless=new` + CDP `Emulation.setDeviceMetricsOverride` 精确视口、`Page.captureScreenshot` 截图；录屏用同一夹具的 `?script=1` 时间线逐帧截取后无损封装为 APNG（浏览器原生播放）。夹具消费生产 `stage.js` / `stage.css` / `tokens.css` / `active.css`，页面外框（`.vw-active` 边距）仅由夹具提供。

## 5. 门规结果

```
python -m pytest -q                      → 348 passed（含新增契约 7）
node --test "tests/js/*.test.mjs"        → 150 tests / 149 pass / 1 todo / 0 fail（新增 20）
python -c "...len(server_app.RPC_METHODS)" → 56
```

红线自查：RPC 恰 56；任务 JSON 顶层 7 键未动；bundle 版本 1 未动；WS 帧 ≤40 未动；`_PHASE_NEXT` 未改；不做存量任务回捞；证据与日志不含正文/频道名/文件名/token。

## 6. 偏差与说明

- **移动档单屏 1 张卡**：390 档舞台 501.6 已贴 60vh 上限，第二张卡在任务舱内部滚动可达；任务书 `<640` 明确允许「≤60vh 内部滚动」，探针按此口径只校验卡片自身不裁切。
- **1024 档文字省略**：沿用桌面三列几何，任务名与错误行单行省略（任务书「文字单行省略」）；磁盘节点位于任务行右侧（行内），队列收窄 160。
- **桌面视频块数量**：任务书「桌面最多 4」为上限。为保单卡高度不压成横条，1280–1919 展示 2 张 + 精确聚合，≥1920 才满 4 张；失败项始终参与展示。
- **展开态队列**：1440 展开 5 条时舱内滚动可达（探针 `outOfBay=0`），不撑破舞台。
- **录屏格式**：取证环境无 `ffmpeg`，以无损 APNG（`recording_T66.png`，浏览器原生循环播放）替代 mp4；帧内容对应 4 并发 → FloodWait 30s → 重试恢复 → 完成（含 1 条失败），全部为内核合法阶段边。
- **未取真实 App 内截图**：当前证据为夹具渲染（生产组件 + 生产数据契约），真实应用内状态带/舞台一致；如需 App 内截图，可在 T68 验收矩阵一并补。
