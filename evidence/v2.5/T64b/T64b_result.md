# T64b 新建任务命名区重构 — 结果

日期：2026-11-01 · 分支 `v2.5-dev` · 基线 `ed501cb`（T64）

## 1. 交付内容

| 文件 | 变更 | 行数 |
| --- | --- | --- |
| `web/js/naming.js` | 新增：命名区共用模块（文案常量 + 档位纯函数 + DOM 组件） | +150 |
| `web/js/views/new.js` | 命名整块迁出条件区；列表上方接入共用组件；行内两态 | +58 / −26 |
| `web/css/views/new.css` | 命名区样式（开关/互斥两档/命名全选/两态标签） | +66 / −3 |
| `web/js/taskedit.js` | 编辑页档位改互斥单选；行内两态标签 | +22 / −9 |
| `web/taskedit.html` | 编辑页文案/顺序对齐共用模块 | +6 / −4 |
| `web/css/taskedit.css` | `.rn-state` 两态标签样式 | +8 |
| `tests/js/naming.test.mjs` | 新增：10 个用例（纯函数 + 两页文案/位置一致性） | +125 |

## 2. 行为口径

- 位置：步骤 2 工具条（筛选框/全选/清空）**下一行**、搜索结果列表**上方**。
- 默认不改名：开关「智能重命名（高级）」默认关，档位与「命名全选/命名全不选」禁用，行只展示原文件名。
- 互斥两档：`改名` / `更宽上下文命名`（原生 radio，同名 `nw-rn-tier`）；开关关闭时两档禁用。
- 档位映射：`改名` → `name_wide=0`；`更宽上下文命名` → `name_wide=1`（只并入既有 `preview` 参数，**RPC 签名零改动**）。
- 命名全选/命名全不选：保留，作用于当前筛选可见集；档位关闭时禁用。
- 结果两态：参与命名的行显示「已登记（未下载）」；文件已落盘（`done`/`exists`）显示「已改名（已下载）」（共用 `rowNameStateText`）。
- 档位在已有结果后切换：拟名由搜索时的档位生成，故提示「命名档位已切换：重新搜索后按新档位生成拟命名」，不静默重扫、不丢选择。
- 关键词/档位随任务持久化不变：`start_download` 的 `query` / `name_wide` 入参沿用既有参数，默认值不写入已有任务。

## 3. 三态截图

| 状态 | 截图 |
| --- | --- |
| 默认态（不改名） | `shot_off.png` |
| 改名态（窄上下文） | `shot_rename.png` |
| 更宽上下文命名态 | `shot_wide.png` |

取证方式：`python -m http.server 8899` 提供仓库静态服务，Edge `--headless=new --screenshot` 渲染 `harness.html?state=off|rename|wide`（1280×760）。夹具直接消费生产 `web/js/naming.js` 与 `web/js/ui/index.js` + `tokens.css/shell.css/views/new.css`，样例数据为占位符（无频道名、文件名、token）。

## 4. 与任务编辑页字段逐项对照

| 顺序 | 字段/控件 | 新建页（`views/new.js`） | 编辑页（`taskedit.html`） | 文案来源 |
| --- | --- | --- | --- | --- |
| 1 | 开关 | `智能重命名（高级）`（`#nw-rn-toggle`，默认关） | `智能重命名（高级）`（`#ed2-rn-toggle`，默认关） | `NAMING_COPY.toggle` |
| 2 | 命名全选 | `命名全选`（禁用随开关） | `命名全选` | `NAMING_COPY.selectAll` |
| 3 | 命名全不选 | `命名全不选`（禁用随开关） | `命名全不选` | `NAMING_COPY.selectNone` |
| 4 | 档位一 | `改名`（radio `nw-rn-tier`） | `改名`（radio `ed2-rn-tier`） | `NAMING_COPY.tierRename` |
| 5 | 档位二 | `更宽上下文命名`（radio，互斥） | `更宽上下文命名`（radio，互斥） | `NAMING_COPY.tierWide` |
| 6 | 行内字段 | `拟命名` + 输入（占位「留空=保持原名」） | `→` 后输入（占位「留空不改」） | `NAMING_COPY.fieldLabel` / `placeholderKeep` |
| 7 | 行内状态 | `已改名（已下载）` / `已登记（未下载）` | 同左（`.rn-state`） | `NAMING_COPY.stateRenamed/stateRecorded` |
| 8 | 说明 | 「勾选=参与命名；取消勾选=使用原文件名；未下载条目下载完成即按此命名」 | 同左 | `NAMING_COPY.note` |

编辑页原有的「应用命名 / 还原命名」为其独有能力，新建页无任务 ID、在创建任务时下发 `renames`，故不引入这两个按钮；其余字段与文案逐字一致（`tests/js/naming.test.mjs` 用 HTML 顺序断言锁死）。

## 5. 门规结果

```
python -m pytest tests -q            → 334 passed
node --test "tests/js/*.test.mjs"    → 112 tests / 111 pass / 1 todo / 0 fail（新增 10）
python -c "...len(server_app.RPC_METHODS)" → 56
```

红线自查：RPC 恰 56；任务 JSON 顶层 7 键未动；bundle 版本未动；WS 帧未动；`_PHASE_NEXT` 未动；无存量任务回捞；证据不含正文/频道名/文件名/token。
