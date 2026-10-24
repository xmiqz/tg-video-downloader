// ============================================================
// views/tasks.js — T14 任务库视图（响应式网格↔单列 + 筛选/搜索 +
//                   快捷操作 + 详情弹层 bottom sheet/模态同构）
//
// 归属波次：T14（v2.2 W5 双包 B），替换 T8 占位。所有权恰两文件：
//   web/js/views/tasks.js（本文件）+ web/css/views/tasks.css。
//
// 行为基线（对齐不回归，逐条 文件:行 注明出处；基准只读）：
//   - showTaskListError      app.js:1669-1677（错误可见 + 重试）
//   - renderTasks 列表/过滤   app.js:1679-1735（含 T49 相位恢复 1729-1735）
//   - setTaskFilter          app.js:1737-1746（取值 all/unfinished/done/failed）
//   - resumeSkipped          app.js:1748-1786（resume_task(id,false,true)）
//   - quickResume            app.js:1788-1798（failed 参数语义）
//   - quickDelete            app.js:1800-1811（confirm 逐字）
//   - openSheet/closeSheet   app.js:1825-1862/1863-1869
//   - 2s 轮询                app.js:1873-1896（输入中不重绘）
//   - openSheetEditor        app.js:1898-1900（改为 router 跳转 /task/:id/edit）
//   - renderSheet            app.js:1902-1946（状态/计数）
//   - setDetailFilter/条目列表 app.js:1948-2029
//   - 条目动作 dPlay/dStatus/dRemove app.js:2035-2091（confirm 逐字）
//   - 阶段位族               app.js:1246-1261/1729-1735（终态 3s 重渲染）
//
// 组件消费：T10 桶唯一入口 ../ui/index.js（createSegmented/createOverlay/
//   createToaster/createEmptyState/el/append），签名见 t10_contract.md。
// RPC 白名单消费 11 个（45 集合内）：list_tasks / get_task /
//   items_file_info / resume_task / delete_task / open_item_file /
//   set_item / remove_items / edit_task / account_list（T52 选号）/
//   open_editor（T19h 恢复 2.1 独立编辑窗）。
//
// thenable 桥不变量（W4 hard 教训）：bridge.js:69-74 的
//   window.pywebview.api 是 Proxy，任意属性（含 then）访问皆返回 RPC
//   函数——任何 Promise 不得 await/resolve/return 桥对象本身；就绪
//   Promise 只 resolve 布尔（同 new.js ensureApi 现行写法），调用一律
//   同步直调 window.pywebview.api.xxx(...)。
//
// 5A seam 出口（任务书 §5A 授权，与 T11/T12 同款）：filterTasks/
//   taskPct/cardTags/statusLabel/statusClass/detailItemText/
//   filterDetailItems/segCount/resumeButton/classifyResumeAck/
//   isTerminalPhase/restorablePhase 十二个纯函数 + STATUS_LABEL 常量
//   额外命名导出，仅供测试消费；本模块顶层零 DOM/window 访问。
//
// 范围外（本波不实现不预埋，报告 deviations 登记）：
//   - T52 任务级选号分段（task-acct/acct-seg-host，app.js:1713-1717）
//     ——新壳账号区后续波次承接；
//   - guardRestricted 受限拦截（app.js:1749/1759 登录墙语义）——新壳
//     登录态视图级联后续波次；
//   - sheet 编辑态表单/批量条目管理/目录按钮/删除任务按钮（T16 编辑器
//     波次，本波仅"编辑任务"入口按钮深链跳转）。
// ============================================================

import {
  el, append, createSegmented, createOverlay, createToaster, createEmptyState,
} from "../ui/index.js";
// T17 手势增量（边缘返回/滑动托盘/聚焦保持；见 evidence/T17/t17_diff_list.md）
import {
  attachEdgeBack, attachSwipeActions, attachFocusKeep, trayFromButtons,
} from "../gestures.js";

export const path = "tasks";

export const title = "任务库 - TG 视频下载器";

// ============================================================
// 常量（照搬旧壳语义，出处就近注明）
// ============================================================

// 状态中文映射（app.js:1954-1957 逐字）
export const STATUS_LABEL = {
  done: "完成", exists: "完成", pending: "未下载",
  failed: "失败", skipped: "跳过",
};

// 详情状态分段标签（index.html:436-440 data-label 同文）
const SEG_LABELS = { all: "全部", done: "已完成", pending: "未下载", failed: "失败", skipped: "跳过" };

// ============================================================
// S1 filterTasks — 列表搜索 + 筛选纯函数（app.js:1687-1692）
// q 匹配体 = (channel_title + channel).toLowerCase()（拼接体，无 @）；
// filter：unfinished → pending+failed>0；done → pending+failed===0；
//         failed → failed>0；all → 不筛。零改输入，返回新数组。
// ============================================================
export function filterTasks(tasks, q, filter) {
  const list = Array.isArray(tasks) ? tasks : [];
  const s = String(q == null ? "" : q).trim().toLowerCase();
  let rows = list;
  if (s) {
    rows = rows.filter(function (t) {
      const a = t || {};
      return String(String(a.channel_title == null ? "" : a.channel_title) +
        String(a.channel == null ? "" : a.channel)).toLowerCase().indexOf(s) >= 0;
    });
  }
  const f = String(filter || "all");
  if (f === "unfinished") {
    rows = rows.filter(function (t) { const a = t || {}; return (a.pending || 0) + (a.failed || 0) > 0; });
  } else if (f === "done") {
    rows = rows.filter(function (t) { const a = t || {}; return (a.pending || 0) + (a.failed || 0) === 0; });
  } else if (f === "failed") {
    rows = rows.filter(function (t) { return (t && t.failed ? t.failed : 0) > 0; });
  }
  return rows.slice();
}

// ============================================================
// S2 taskPct / cardTags — 进度百分比与卡片标签（app.js:1700-1710）
// pct = total ? Math.round(finished*100/total) : 0；
// tags：待下 N → 失败 N(fail) → 跳过 N，全零 → [{全部完成}]。
// ============================================================
export function taskPct(finished, total) {
  const t = Number(total) || 0;
  const f = Number(finished) || 0;
  return t ? Math.round(f * 100 / t) : 0;
}

export function cardTags(t) {
  const a = t || {};
  const out = [];
  if (a.pending) out.push({ text: "待下 " + a.pending, fail: false });
  if (a.failed) out.push({ text: "失败 " + a.failed, fail: true });
  if (a.skipped) out.push({ text: "跳过 " + a.skipped, fail: false });
  if (!out.length) out.push({ text: "全部完成", fail: false });
  return out;
}

// ============================================================
// S3 statusLabel / statusClass / detailItemText（app.js:1954-1984/2018-2020）
// ============================================================
export function statusLabel(status) {
  return STATUS_LABEL[status] || String(status || "");
}

export function statusClass(status) {
  if (status === "done" || status === "exists") return "done";
  if (status === "failed") return "fail";
  if (status === "skipped") return "wait";
  return "run";
}

export function detailItemText(ti) {
  const a = ti || {};
  if (a.error) return "错误：" + a.error;
  // T19f-F：有消息文字展示原文（2.1 恒显占位「该消息无文字内容」，用户
  // 下载后打开任务详情看不到消息文字）；无文字/旧任务缺字段保留占位逐字
  return String(a.text || "").trim() || "该消息无文字内容";
}

// ============================================================
// S4 filterDetailItems — 详情条目状态筛选 + 搜索（app.js:1960-1972）
// filter done → done+exists；精确过滤其余；q 匹配 (name + (error||""))
// 小写子串。零改输入。
// ============================================================
export function filterDetailItems(items, filter, q) {
  const list = Array.isArray(items) ? items : [];
  const f = String(filter || "all");
  let rows = list;
  if (f === "done") {
    rows = rows.filter(function (x) { return x && (x.status === "done" || x.status === "exists"); });
  } else if (f !== "all") {
    rows = rows.filter(function (x) { return x && x.status === f; });
  }
  const s = String(q == null ? "" : q).trim().toLowerCase();
  if (s) {
    rows = rows.filter(function (x) {
      const a = x || {};
      return String(String(a.name || "") + String(a.error || "")).toLowerCase().indexOf(s) >= 0;
    });
  }
  return rows.slice();
}

// ============================================================
// S5 segCount / resumeButton — 详情分段计数与继续按钮（app.js:1917-1930）
// ============================================================
export function segCount(filter, counts) {
  const c = counts || {};
  const f = String(filter || "all");
  if (f === "done") return c.finished || 0;
  if (f === "pending") return c.pending || 0;
  if (f === "failed") return c.failed || 0;
  if (f === "skipped") return c.skipped || 0;
  return c.total || 0;
}

export function resumeButton(counts) {
  const c = counts || {};
  const pending = c.pending || 0;
  const failed = c.failed || 0;
  return {
    text: pending > 0 ? "继续下载" : "重试失败",   // app.js:1928
    disabled: !(pending + failed > 0),             // app.js:1929
  };
}

// ============================================================
// S6 classifyResumeAck — resume_task 返回三分支
// （app.js:1777/1781、1795、2317：ok；error 含"已在下载"= 宽松成功；
//   其余 error，兜底文案对齐 resumeSkipped 的"操作未生效"）
// ============================================================
export function classifyResumeAck(r) {
  if (r && r.ok) return { kind: "ok" };
  if (r && typeof r.error === "string" && r.error.indexOf("已在下载") >= 0) return { kind: "already" };
  return { kind: "error", error: (r && r.error) || "操作未生效" };
}

// ============================================================
// S7 阶段位族（app.js:1254 终态集；1729-1732 恢复口径）
// ============================================================
export function isTerminalPhase(phase) {
  const p = String(phase || "");
  return p === "done" || p === "aborted" || p === "error";
}

export function restorablePhase(p) {
  if (!p || !p.phase) return false;
  return !isTerminalPhase(p.phase);
}

// ============================================================
// 内部工具（非导出；语义与旧壳等价）
// ============================================================

// 体积人性化（app.js:13-19 human 逐行同义）
function human(n) {
  n = Number(n) || 0;
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return n.toFixed(n >= 100 || i === 0 ? 0 : 1) + " " + u[i];
}

// 条目键（app.js:29 keyOf 同义；SRC_CHANNEL="频道" tg_video_dl.py:623）
function keyOf(src, mid) {
  return (src || "频道") + "|" + mid;
}

// 导航：优先新壳 router 探针，降级 hash（同 new.js 范式）
function navigate(p) {
  if (typeof window !== "undefined" && window.__t8router &&
      typeof window.__t8router.navigate === "function") {
    window.__t8router.navigate(p);
  } else if (typeof location !== "undefined") {
    location.hash = "#" + p;
  }
}

// 按钮忙态（app.js:33-50 withBusy 同义：disabled + 文案换 + is-busy 类，
// finally 恢复；消费方 catch 决定反馈）。返回恢复函数。
function busyOn(btn, busyText) {
  if (!btn) return function () {};
  const old = btn.textContent;
  btn.dataset.busy = "1";
  btn.disabled = true;
  btn.classList.add("is-busy");
  if (busyText != null) btn.textContent = busyText;
  return function () {
    delete btn.dataset.busy;
    btn.disabled = false;
    btn.classList.remove("is-busy");
    btn.textContent = old;
  };
}

// ============================================================
// RPC 桥（唯一通道 window.pywebview.api；pywebviewready 等就绪）
// 就绪 Promise 只 resolve 布尔；调用点同步直调——绝不
// await/resolve/return 桥对象本身（thenable 桥不变量，见文件头）。
// ============================================================
function bridgeReady() {
  return !!(typeof window !== "undefined" &&
    window.pywebview && window.pywebview.api);
}

let pyReadyHandler = null;
let apiReadyP = null;

function ensureApi() {
  if (bridgeReady()) {
    return Promise.resolve(true);   // 布尔；绝不 resolve 桥对象
  }
  if (!apiReadyP) {
    apiReadyP = new Promise(function (resolve) {
      pyReadyHandler = function () {
        pyReadyHandler = null;
        resolve(bridgeReady());     // 布尔；绝不 resolve 桥对象
      };
      window.addEventListener("pywebviewready", pyReadyHandler);
    }).then(function (ready) {
      apiReadyP = null;
      return ready;                 // 布尔透传
    });
  }
  return apiReadyP;
}

let toaster = null;
function toast(type, text, detail) {
  if (!toaster) toaster = createToaster({ max: 5 });
  toaster.show({ type: type, text: text, detail: detail });
}

// ============================================================
// 视图状态（模块级闭包；keep-alive 下跨进入保留）
// ============================================================
let cached = null;
let refs = null;
let taskFilter = "all";               // app.js 任务库筛选（index.html:191 默认 all）
let tasksCache = [];                  // 最近一次 list_tasks 结果（阶段位恢复不依赖）
let dlPhaseByTask = new Map();        // task_id -> {phase, detail}（app.js:1234）
let taskPhaseTimers = new Map();      // task_id -> timeout id（app.js:1236 防重入）
let listBound = false;                // window tg:dl_phase 监听挂载标志
let dlPhaseHandler = null;            // 解包包装后的监听引用（解绑用）
let sheetOv = null;                   // 详情弹层实例（懒建档复用）
let confirmOv = null;                 // 确认弹层（一次一档）
let sheet = {                         // 详情面板状态（app.js:1816-1820）
  taskId: null, task: null, counts: null,
  filter: "all", q: "", expanded: new Set(), files: {},
};
let detailSeg = null;                 // 详情状态分段组件实例
let sheetPollId = null;               // 2s 轮询句柄（app.js:1872）
const resumeBusy = new Set();         // resumeSkipped 防重键（resumeskip:<tid>）
let skipNextCloseRefresh = false;     // 下次关闭跳过刷新（sheetResume 成功路径 = closeSheetNoRefresh 语义）

// 详情分段 五项（创建一次；renderSheet 只更新 label 计数与 active）
function detailSegItems() {
  return [
    { value: "all", label: "全部 0" },
    { value: "done", label: "已完成 0" },
    { value: "pending", label: "未下载 0" },
    { value: "failed", label: "失败 0" },
    { value: "skipped", label: "跳过 0" },
  ];
}

// ============================================================
// mount / onEnter / onLeave（T8 契约 §1）
// ============================================================
export function mount() {
  if (cached) return cached;
  try {
    cached = buildDom();
  } catch (e) {
    // mount 自捕获（任务书 §1.5）：失败返回降级元素，rejected Promise
    // 不得进 router mounts 缓存——本路径同步返回可用空态元素。
    console.error("[T14] tasks 视图构建失败：", e);
    cached = el("div", { class: "vw-tasks" }, [
      createEmptyState({
        icon: "⚠", title: "任务库暂不可用", hint: "视图初始化失败，可刷新页面重试。",
      }).el,
    ]);
  }
  return cached;
}

export function onEnter() {
  if (cached && refs) {
    bindList();
    attachGestures();   // T17 手势增量（幂等）
    loadTasks();   // 旧壳 switchView("tasks") → renderTasks()（app.js:174）
  }
}

// T17 手势增量：边缘返回 + 卡片滑动托盘（透传 .tk-act 既有按钮）+ 聚焦保持
let gsDetach = [];
function attachGestures() {
  if (gsDetach.length) return;
  gsDetach = [
    attachEdgeBack(),
    attachSwipeActions(refs.root, {
      itemSel: ".tk-card",
      actions: function (card) { return trayFromButtons(card, ".tk-act"); },
    }),
    attachFocusKeep(refs.root),
  ];
}

export function onLeave() {
  // 全清理（任务书铁律 5）：详情轮询/弹层/事件/计时器
  if (sheetPollId != null) { clearInterval(sheetPollId); sheetPollId = null; }
  if (sheetOv && sheetOv.isOpen()) {
    // 路由跳转（如编辑入口）时弹层随视图离开销毁（新版 keep-alive 语义）
    sheetOv.destroy();
    sheetOv = null;
    detailSeg = null;
  }
  sheet.taskId = null; sheet.task = null; sheet.counts = null;
  sheet.filter = "all"; sheet.q = "";
  sheet.expanded = new Set(); sheet.files = {};
  skipNextCloseRefresh = false;
  destroyConfirm();
  resetPhaseTimers();
  unbindList();
  // T17 手势增量对称回收（attach* 均返回 detach）
  for (let i = 0; i < gsDetach.length; i++) gsDetach[i]();
  gsDetach = [];
}

function bindList() {
  if (listBound) return;
  listBound = true;
  // 监听首参是 Event 对象：解包 detail 再进视图逻辑（tg:dl_phase 的
  // detail = params[0]，payload 钉死 {task_id, phase, phase_detail,…}）
  dlPhaseHandler = function (ev) { onDlPhase(ev && ev.detail); };
  window.addEventListener("tg:dl_phase", dlPhaseHandler);
}

function unbindList() {
  if (!listBound) return;
  listBound = false;
  if (dlPhaseHandler) {
    window.removeEventListener("tg:dl_phase", dlPhaseHandler);
    dlPhaseHandler = null;
  }
  if (pyReadyHandler) {
    window.removeEventListener("pywebviewready", pyReadyHandler);
    pyReadyHandler = null;
  }
  apiReadyP = null;
}

function resetPhaseTimers() {
  taskPhaseTimers.forEach(function (tid) { clearTimeout(tid); });
  taskPhaseTimers.clear();
}

// ============================================================
// DOM 构建（仅 mount 一次执行；顶层与 onEnter 无 DOM 依赖）
// ============================================================
function buildDom() {
  const root = el("div", { class: "vw-tasks" });

  // —— 页头（index.html:182-184）——
  root.append(el("header", { class: "tk-head" }, [
    el("h2", { text: "任务库" }),
    el("p", { class: "tk-sub", text: "查看历史任务与已下载视频，继续或编辑任务" }),
  ]));

  // —— 工具条：搜索 + 筛选分段（index.html:185-196）——
  const search = el("input", {
    id: "tk-search", type: "text", class: "tk-input",
    placeholder: "搜索频道名…", autocomplete: "off",
    oninput: function () { loadTasks(); },   // app.js:188 oninput=renderTasks
  });
  const filterSeg = createSegmented([
    { value: "all", label: "全部" },
    { value: "unfinished", label: "未完成" },
    { value: "done", label: "已完成" },
    { value: "failed", label: "有失败" },
  ], {
    value: "all", ariaLabel: "任务筛选",
    onChange: function (v) { setTaskFilter(v); },  // app.js:1737-1742
  });
  root.append(el("div", { class: "tk-toolbar" }, [
    el("div", { class: "tk-search" }, [
      el("span", { class: "tk-search-ico", "aria-hidden": "true", text: "⌕" }),
      search,
    ]),
    filterSeg.el,
    el("label", { class: "tk-sr-only", for: "tk-search", text: "搜索频道名" }),
  ]));

  // —— 列表区（网格容器，空态/错误态就地换装）——
  const grid = el("div", { class: "tk-grid", role: "list", "aria-label": "任务列表" });
  root.append(grid);

  refs = { root: root, search: search, filterSeg: filterSeg, grid: grid };
  return root;
}

// ============================================================
// 列表加载与渲染（app.js:1679-1735 renderTasks 等价）
// ============================================================
async function loadTasks() {
  if (!refs) return;
  let tasks;
  try {
    const ready = await ensureApi();            // 布尔就绪，非桥对象
    if (!ready) throw new Error("bridge not ready");
    tasks = await window.pywebview.api.list_tasks(); // 同步直调（旧壳 app.js:5）
  } catch (e) {
    return showTaskListError(e);
  }
  if (!Array.isArray(tasks)) return showTaskListError("返回数据异常"); // app.js:1686
  tasksCache = tasks;
  renderTasks();
}

// 列表渲染（数据已到；纯本地过滤 + 卡片重建 + 相位恢复）
function renderTasks() {
  if (!refs) return;
  const rows = filterTasks(tasksCache, refs.search.value, taskFilter);
  // T52：t.acct 种子（app.js:1694-1696；会话内手动值优先不被覆盖）
  for (let i = 0; i < tasksCache.length; i++) {
    const t = tasksCache[i];
    if (t.acct && !taskAcct.has(t.id)) taskAcct.set(t.id, t.acct);
  }
  ensureAcctAlts();
  clearGrid();
  if (!rows.length) {
    // 空态（index.html:198-200：🗂 没有符合条件的任务）
    refs.grid.append(createEmptyState({
      icon: "🗂", title: "没有符合条件的任务", hint: null,
    }).el);
    return;
  }
  for (let i = 0; i < rows.length; i++) appendCard(rows[i]);
  // T49：重建后恢复仍在进行中的阶段位（app.js:1729-1735；终态不复活）
  refs.grid.querySelectorAll(".tk-card").forEach(function (card) {
    const ph = dlPhaseByTask.get(card.dataset.tid);
    if (ph && restorablePhase(ph)) updateTaskPhaseCard(card, ph.phase, ph.detail);
  });
}

function clearGrid() {
  while (refs.grid.firstChild) refs.grid.removeChild(refs.grid.firstChild);
}

function appendCard(t) {
  refs.grid.append(buildCard(t));
}

// 任务卡（app.js:1699-1724 结构等价）
// T52 任务级选号（app.js:1713-1717 + TgAccounts app.js:3584-3637）：
// acct_pref 会话映射（t.acct 种子，手动值优先）；换值下发 edit_task(tid,{acct_pref})，
// 被拒/异常静默回退（2.1 console.debug 口径，不弹错误）
const taskAcct = new Map();     // tid -> "main"|altId|"auto"
let acctAlts = null;            // 小号清单（account_list 懒拉；null=未知，[]=无）
let acctAltsLoading = false;

function acctSegItems() {
  const items = [{ value: "main", label: "主号" }];                 // app.js:3579
  if (acctAlts && acctAlts.length) {
    items.push({ value: String(acctAlts[0].id), label: "小号" });   // app.js:3580
  }
  items.push({ value: "auto", label: "自动" });                     // app.js:3581
  return items;
}
function renderAcctSeg(host) {
  if (!host) return;
  const tid = host.getAttribute("data-acct-tid") || "";
  const cur = taskAcct.get(tid) || "auto";                          // app.js:3587
  const scroll = el("div", { class: "u-seg-scroll" });
  acctSegItems().forEach(function (it) {
    const on = it.value === cur;
    scroll.append(el("button", {
      class: "u-seg-item" + (on ? " is-active" : ""),
      type: "button", role: "radio", "aria-selected": on ? "true" : "false",
      text: it.label,
      onclick: function (ev) { ev.stopPropagation(); pickAcctSeg(host, tid, it.value); },
    }));
  });
  host.textContent = "";
  host.append(scroll);
}
async function pickAcctSeg(host, tid, value) {
  const prev = taskAcct.get(tid) || "auto";
  if (prev === value) return;                                       // app.js:3598
  taskAcct.set(tid, value);
  renderAcctSeg(host);
  try {
    const ready = await ensureApi();
    if (!ready) throw new Error("bridge not ready");
    const r = await window.pywebview.api.edit_task(tid, { acct_pref: value });  // app.js:3613
    if (r && r.ok === false) { taskAcct.set(tid, prev); renderAcctSeg(host); }  // app.js:3614-3617
  } catch (e) {
    taskAcct.set(tid, prev); renderAcctSeg(host);                  // app.js:3618-3623
  }
}
function ensureAcctAlts() {
  if (acctAlts !== null || acctAltsLoading) return;
  acctAltsLoading = true;
  ensureApi().then(function (ready) {
    if (!ready) throw new Error("bridge not ready");
    return window.pywebview.api.account_list();
  }).then(function (r) {
    acctAltsLoading = false;
    const list = (r && r.ok && Array.isArray(r.accounts)) ? r.accounts : [];
    acctAlts = list.filter(function (a) {   // settings.js:764-765 小号同口径
      return a && a.id && a.id !== "main" && a.state !== "absent";
    });
    refreshAcctSegs();
  }).catch(function () { acctAltsLoading = false; acctAlts = []; });
}
function refreshAcctSegs() {
  const hosts = document.querySelectorAll(".tk-acct-seg");
  for (let i = 0; i < hosts.length; i++) renderAcctSeg(hosts[i]);
}

function buildCard(t) {
  const pct = taskPct(t.finished, t.total);
  const tags = cardTags(t);
  const tagEls = tags.map(function (g) {
    return el("span", { class: "tk-tag" + (g.fail ? " tk-tag--fail" : "") }, [g.text]);
  });
  // 快捷操作按钮（app.js:1719-1723 条件同款）
  const actions = [];
  if ((t.pending || 0) + (t.failed || 0) > 0) {
    actions.push(el("button", {
      class: "u-btn u-btn--primary tk-act", type: "button", text: "继续",   // 2.1 .btn.primary.sm（app.js:1719）
      onclick: function (ev) { ev.stopPropagation(); quickResume(t.id, false, ev.currentTarget); },
    }));
  }
  if (t.failed) {
    actions.push(el("button", {
      class: "u-btn u-btn--ghost tk-act", type: "button", text: "重试失败",
      onclick: function (ev) { ev.stopPropagation(); quickResume(t.id, true, ev.currentTarget); },
    }));
  }
  if (t.skipped) {
    actions.push(el("button", {
      class: "u-btn u-btn--ghost tk-act", type: "button",
      text: "恢复跳过 " + t.skipped + " 个",   // app.js:63 resumeSkipLabel(n)
      onclick: function (ev) { ev.stopPropagation(); resumeSkipped(t.id, ev.currentTarget); },
    }));
  }
  actions.push(el("button", {
    class: "u-btn u-btn--danger tk-act", type: "button", text: "删除",
    onclick: function (ev) { ev.stopPropagation(); quickDelete(t.id, ev.currentTarget); },
  }));

  const card = el("div", {
    class: "tk-card",
    dataset: { tid: t.id },
    role: "listitem",
    tabindex: "0",
    "aria-label": String(t.channel_title || "") + " 任务卡",
    onclick: function () { openSheet(t.id); },     // app.js:1706
    onkeydown: function (e) {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openSheet(t.id); }
    },
  }, [
    // 阶段位（app.js:1707 默认 hidden）
    el("div", { class: "tk-phase", "data-phase": "", hidden: true, "aria-live": "polite" }),
    el("h3", { class: "tk-title", text: String(t.channel_title || "") }),
    el("div", { class: "tk-channel", text: "@" + String(t.channel || "") }),
    el("div", { class: "tk-tags" }, tagEls),   // app.js:1701-1704
    el("div", { class: "tk-bar" }, [
      el("div", { class: "tk-fill" + (t.finished === t.total ? " tk-fill--full" : ""), style: { width: pct + "%" } }),
    ]),
    el("div", { class: "tk-stat" }, [
      el("span", { text: (t.finished || 0) + "/" + (t.total || 0) + " 完成" }),
      el("span", { text: String(t.created || "") }),
    ]),
    // T52 任务选号行（app.js:1713-1717：label + 选号段 + note，事件不透传卡片）
    el("div", { class: "tk-acct", onclick: function (ev) { ev.stopPropagation(); } }, [
      el("span", { class: "tk-acct-label", text: "本任务后续下载使用" }),
      (function () {
        const host = el("div", {
          class: "tk-acct-seg u-seg", dataset: { "acct-tid": t.id },
          role: "radiogroup", "aria-label": "本任务后续下载账号",
        });
        renderAcctSeg(host);
        return host;
      })(),
      el("span", { class: "tk-acct-note", text: "进行中的条目不受影响" }),
    ]),
    el("div", { class: "tk-actions" }, actions),
  ]);
  return card;
}

// 错误态（app.js:1669-1677 showTaskListError 等价：toast + 内联错误 + 重试）
function showTaskListError(v) {
  toast("error", "任务列表加载失败", (v && v.message) ? v.message : String(v || ""));
  clearGrid();
  refs.grid.append(el("div", { class: "tk-errblock", role: "alert" }, [
    el("h3", { class: "tk-errtitle", text: "任务列表加载失败" }),   // 旧壳 h3 内联文案
    el("p", { class: "tk-errhint", text: "请检查应用后重试；上方错误提示可展开查看详情。" }),
    el("button", {
      class: "u-btn u-btn--ghost", type: "button", text: "重试",
      onclick: function () { loadTasks(); },   // 旧壳 onclick=renderTasks()
    }),
  ]));
}

// 筛选切换（app.js:1737-1742：置键 + 分段高亮 + renderTasks 重拉；
// 分段高亮由 createSegmented 组件自持，视图侧无需 DOM 对账）
function setTaskFilter(f) {
  taskFilter = f;
  loadTasks();          // 旧壳 setTaskFilter → renderTasks → await list_tasks（重拉）
}

// ============================================================
// 阶段位（app.js:1246-1261 updateTaskPhase + 1266-1300 onDlPhase 等价；
// 组头/活跃列表归 T13，本波只做任务库卡片面）
// ============================================================
function updateTaskPhaseCard(card, phase, detail) {
  if (!card) return;
  const elPhase = card.querySelector(".tk-phase");
  if (!elPhase) return;                          // 模板缺位静默跳过（app.js:1248）
  if (!phase) { elPhase.hidden = true; return; }
  elPhase.hidden = false;
  if (elPhase.dataset.phase !== phase) elPhase.dataset.phase = phase;
  if (elPhase.textContent !== detail) elPhase.textContent = detail;
  const tid = card.dataset.tid;
  const term = isTerminalPhase(phase);
  if (term && tid && !taskPhaseTimers.has(tid)) {
    // 终态 3s 后重拉列表（app.js:1255-1259；防重入）
    taskPhaseTimers.set(tid, setTimeout(function () {
      taskPhaseTimers.delete(tid);
      try { loadTasks(); } catch (e) { /* 重拉失败由 showTaskListError 兜底 */ }
    }, 3000));
  }
}

function onDlPhase(detail) {
  if (!detail || typeof detail.task_id !== "string") return;   // app.js:1267
  const p = detail.phase || "";
  const d = detail.phase_detail || "";
  dlPhaseByTask.set(detail.task_id, { phase: p, detail: d });  // app.js:1270
  if (!refs) return;
  const card = refs.grid.querySelector('.tk-card[data-tid="' + String(detail.task_id).replace(/"/g, '\\"') + '"]');
  if (card) updateTaskPhaseCard(card, p, d);                   // app.js:1297-1299
}

// ============================================================
// 快捷操作：quickResume / quickDelete / resumeSkipped
// ============================================================

// app.js:1788-1798 quickResume（failed 参数语义：resume_task(id, failed)）
async function quickResume(id, failed, btn) {
  const restore = busyOn(btn, failed ? "重试中…" : "继续中…");   // app.js:1792
  let r = null;
  try {
    const ready = await ensureApi();
    if (!ready) throw new Error("bridge not ready");
    r = await window.pywebview.api.resume_task(id, failed);      // 同步直调
  } catch (e) {
    restore();
    return;                                        // app.js:1794 网络异常静默
  }
  restore();
  const c = classifyResumeAck(r);
  if (c.kind === "ok" || c.kind === "already") navigate("/active"); // app.js:1795
  else toast("error", c.error);                    // app.js:1796 alert 承接
}

// app.js:1800-1811 quickDelete（confirm 逐字 → T10 确认弹层承接）
function quickDelete(id, btn) {
  confirmAsk({
    text: "删除任务记录？已下载视频不会被删除。",   // app.js:1801 逐字
    confirmLabel: "删除", danger: true,
    onConfirm: async function () {
      const restore = busyOn(btn, "删除中…");      // app.js:1804 withBusy
      let r = null;
      try {
        const ready = await ensureApi();
        if (!ready) throw new Error("bridge not ready");
        r = await window.pywebview.api.delete_task(id);          // 同步直调
      } catch (e) {
        restore();
        toast("error", "删除任务失败", (e && e.message) || String(e)); // app.js:1807
        return;
      }
      restore();
      if (!r || !r.ok) {
        toast("error", "删除任务失败", (r && r.error) || "删除未生效"); // app.js:1809
        return;
      }
      loadTasks();                                 // app.js:1810 renderTasks
    },
  });
}

// app.js:1748-1786 resumeSkipped（只拉 skipped：resume_task(id,false,true)；
// 组在线观测/选号等旧壳全局态本波不实现——新壳账号区后续波次）
async function resumeSkipped(id, btn) {
  if (!id) return;
  const key = "resumeskip:" + id;
  if (resumeBusy.has(key)) return;                 // PendingOps 防重（app.js:1752）
  resumeBusy.add(key);
  const restore = busyOn(btn, "恢复中…");          // app.js:1768 busyText
  let r = null;
  try {
    const ready = await ensureApi();
    if (!ready) throw new Error("bridge not ready");
    r = await window.pywebview.api.resume_task(id, false, true); // 同步直调 app.js:1772
  } catch (e) {
    resumeBusy.delete(key);
    restore();
    toast("error", "恢复跳过失败", (e && e.message) || String(e)); // app.js:1775
    return;
  }
  resumeBusy.delete(key);
  restore();
  const c = classifyResumeAck(r);
  if (c.kind === "error") {
    toast("error", "恢复跳过失败", c.error);        // app.js:1781
    return;
  }
  navigate("/active");                             // app.js:1785 switchView("active")
}

// ============================================================
// 确认弹层（T10 契约 §4 映射表：confirm → overlay 确认型；
// 遮罩/ESC/✕/取消 = 不执行。danger 主键建议）
// ============================================================
function confirmAsk(spec) {
  const s = spec || {};
  destroyConfirm();
  confirmOv = createOverlay({
    title: s.title || "确认操作",
    content: s.text || "",
    form: "modal",
    actions: [
      { key: "ok", label: s.confirmLabel || "确定", primary: !s.danger, danger: !!s.danger },
      { key: "cancel", label: "取消" },
    ],
    onAction: function (k) {
      if (k === "ok" && typeof s.onConfirm === "function") s.onConfirm();
      // 返回 undefined（非 false）→ 组件自动 close("action")（T10 契约
      // §2.3：onAction 返回 false 才是"阻止自动关闭"）
    },
    ariaLabel: s.title || "确认操作",
  });
  document.body.appendChild(confirmOv.el);
  confirmOv.open();
}

function destroyConfirm() {
  if (confirmOv) {
    confirmOv.destroy();
    confirmOv = null;
  }
}

// ============================================================
// 详情弹层（openSheet/closeSheet/轮询/renderSheet/条目全家）
// 桌面 ≥768 居中模态 / 手机 <768 贴底 sheet（createOverlay form:"auto"
// 实时切换，FR-25 新形态；旧壳为右侧滑层 index.html:420，形态差异为
// v2.2 响应式规格授权）
// ============================================================

// app.js:1825-1862 openSheet（get_task → files → 渲染 → 轮询）
async function openSheet(taskId) {
  let r;
  try {
    const ready = await ensureApi();
    if (!ready) throw new Error("bridge not ready");
    r = await window.pywebview.api.get_task(taskId);   // 同步直调
  } catch (e) {
    return toast("error", "打开任务详情失败", (e && e.message) || String(e)); // app.js:1830
  }
  if (!r || !r.ok) {
    return toast("error", "打开任务详情失败", (r && r.error) || "任务读取失败"); // app.js:1833
  }
  try {
    const settings = await window.pywebview.api.get_settings();
    sheet.headless = !!(settings && settings.mode === "headless");
  } catch (e) {
    sheet.headless = false;
  }
  sheet.taskId = taskId;
  sheet.task = r.task;
  sheet.counts = r.counts;
  sheet.filter = "all"; sheet.q = "";               // app.js:1837
  sheet.expanded = new Set();                       // app.js:1838（编辑 sel 不适用本波）
  sheet.files = {};                                 // app.js:1839 切任务清旧文件状态
  ensureSheet();
  resetSheetBody();
  renderSheet();
  sheetOv.open();
  // 批量文件状态：get_task 已含则复用；未携带 fallback（app.js:1848-1859）
  const files = extractFiles(r);
  if (files) sheet.files = files;
  else await loadItemFiles(taskId, false);
  renderDetailItems();
  startSheetPoll();
}

// 惰性建详情弹层容器（标题/sub 随任务重建，body 内容每次 reset）
function ensureSheet() {
  if (sheetOv) return;
  sheetOv = createOverlay({
    title: "",
    sub: "",
    form: "auto",
    ariaLabel: "任务详情",
    onClose: function () { onSheetClosed(); },   // ✕/mask/ESC/action 全路径归一（app.js:1863-1869 closeSheet）
  });
  sheetOv.el.classList.add("tk-sheet-ov");   // T19i：桌面右置滑层/底部常驻样式锚点
  document.body.appendChild(sheetOv.el);
}

// sheet body 内容构建（一次；renderSheet/renderDetailItems 更新数据位）
function resetSheetBody() {
  const body = sheetOv.body;
  while (body.firstChild) body.removeChild(body.firstChild);
  detailSeg = createSegmented(detailSegItems(), {
    value: "all", ariaLabel: "条目状态筛选",
    onChange: function (v) { setDetailFilter(v); },   // app.js:1948-1952
  });
  const searchInput = el("input", {
    id: "tk-detail-q", type: "text", class: "tk-input",
    placeholder: "筛选本任务视频名/消息…", autocomplete: "off",   // index.html:444
    oninput: function () { renderDetailItems(); },  // app.js:444 oninput
  });
  body.append(
    el("div", { class: "tk-sheet" }, [
      el("div", { class: "tk-sprog" }, [
        el("div", { class: "tk-sbar" }, [el("div", { class: "tk-sfill" })]),
        el("div", { class: "tk-scounters" }),
      ]),
      detailSeg.el,
      el("label", { class: "tk-sr-only", for: "tk-detail-q", text: "筛选本任务视频名" }),
    ]),
    el("div", { class: "tk-ssearch" }, [
      el("span", { class: "tk-search-ico", "aria-hidden": "true", text: "⌕" }),
      searchInput,
    ]),
    el("div", { class: "tk-dlist", role: "list", "aria-label": "任务条目" }),
    el("div", { class: "tk-sfoot" }, [
      // 编辑入口（T19h）：open_editor 独立编辑窗（2.1 双窗恢复），
      // 失败回落 router 深链 task/:id/edit
      el("button", {
        class: "u-btn u-btn--ghost", type: "button", text: "编辑任务",
        onclick: function () {
          openSheetEditor(sheet.taskId);
        },
      }),
      // T19f 补齐 2.1 底栏选项（index.html:474-481 逐序：编辑/目录/继续/
      // 恢复跳过/删除；「完成编辑」钮随 v2.2 路由编辑页形态取消）
      el("button", { class: "u-btn u-btn--ghost", type: "button", text: "目录",
        hidden: !!sheet.headless, id: "tk-sheet-folder",
        onclick: function () { sheetOpenFolder(this); } }),   // app.js:2284 sheetOpenFolder
      el("button", { class: "u-btn u-btn--primary", type: "button", id: "tk-sheet-resume", text: "继续下载",
        onclick: function () { sheetResume(this); } }),   // app.js:2299 sheetResume
      el("button", { class: "u-btn u-btn--ghost", type: "button", id: "tk-sheet-skip", text: "恢复跳过",
        hidden: true,
        onclick: function () { resumeSkipped(sheet.taskId, this); } }),
      el("button", { class: "u-btn u-btn--danger", type: "button", text: "删除任务",
        onclick: function () { sheetDelete(this); } }),   // app.js:2331 sheetDelete
    ])
  );
}

// app.js:1863-1869 closeSheet：停轮询 + 复位 + 刷新列表
// （overlay 全关闭路径经 onClose 归一到此；sheetResume 成功路径置
//   skipNextCloseRefresh 走 closeSheetNoRefresh 不刷语义）
function onSheetClosed() {
  stopSheetPoll();
  const skip = skipNextCloseRefresh;
  skipNextCloseRefresh = false;
  sheet.taskId = null;
  if (!skip) loadTasks();               // app.js:1868 renderTasks()
}

// app.js:2299-2321 sheetResume（详情 footer 继续下载；failedOnly 口径）
async function sheetResume(btn) {
  const c = sheet.counts || {};
  const failedOnly = !(c.pending > 0) && (c.failed || 0) > 0;   // app.js:2301
  const restore = busyOn(btn, "继续中…");                       // app.js:2304-2306
  let r = null;
  try {
    const ready = await ensureApi();
    if (!ready) throw new Error("bridge not ready");
    r = await window.pywebview.api.resume_task(sheet.taskId, failedOnly);  // 直调
  } catch (e) {
    restore();
    return;                                        // app.js:2310-2313 网络异常静默
  }
  restore();
  const cls = classifyResumeAck(r);
  if (cls.kind === "ok" || cls.kind === "already") {
    skipNextCloseRefresh = true;        // app.js:2318 closeSheetNoRefresh（不刷）
    stopSheetPoll();
    if (sheetOv) sheetOv.close();      // 触发 onClose → onSheetClosed（跳刷新）
    navigate("/active");               // app.js:2319 switchView("active")
  } else {
    toast("error", cls.error);         // app.js:2320 alert 承接
  }
}

// T19f：底栏「目录」（app.js:2281-2298 sheetOpenFolder：等待返回+失败可见
// +函数级锁防重；open_folder(task.folder) 白名单在位）
let sheetFolderBusy = false;
async function sheetOpenFolder(btn) {
  if (sheetFolderBusy) return;
  sheetFolderBusy = true;
  const restore = busyOn(btn, "打开中…");
  let r = null;
  try {
    const ready = await ensureApi();
    if (!ready) throw new Error("bridge not ready");
    r = await window.pywebview.api.open_folder(sheet.task && sheet.task.folder);
  } catch (e) {
    restore(); sheetFolderBusy = false;
    return toast("error", "打开目录失败", (e && e.message) || String(e));
  }
  restore(); sheetFolderBusy = false;
  if (!r || !r.ok) {
    toast("error", "打开目录失败", (r && r.error) || "目录不存在或无法打开");
  }
}

// T19f：底栏「删除任务」（app.js:2328-2349 sheetDelete：confirm 流程+失败
// 可见+锁防重；成功才关滑层回任务库——close 触发 onSheetClosed 刷列表）
let sheetDeleteBusy = false;
function sheetDelete(btn) {
  confirmAsk({
    title: "删除任务",
    text: "删除整个任务记录？已下载视频不会删除。",   // app.js:2332 逐字
    confirmLabel: "删除",
    danger: true,
    onConfirm: async function () {
      if (sheetDeleteBusy) return;
      sheetDeleteBusy = true;
      const restore = busyOn(btn, "删除中…");
      let r = null;
      try {
        const ready = await ensureApi();
        if (!ready) throw new Error("bridge not ready");
        r = await window.pywebview.api.delete_task(sheet.taskId);
      } catch (e) {
        restore(); sheetDeleteBusy = false;
        return toast("error", "删除任务失败", (e && e.message) || String(e));
      }
      restore(); sheetDeleteBusy = false;
      if (!r || !r.ok) {
        return toast("error", "删除任务失败", (r && r.error) || "删除未生效");
      }
      if (sheetOv) sheetOv.close();    // onClose → onSheetClosed → loadTasks
    },
  });
}

// app.js:1873-1896 2s 轮询：下载中的条目状态即时一致；输入中不重绘
function startSheetPoll() {
  stopSheetPoll();
  sheetPollId = setInterval(async function () {
    if (sheet.taskId == null) { stopSheetPoll(); return; }
    try {
      const ready = await ensureApi();
      if (!ready) throw new Error("bridge not ready");
      const r = await window.pywebview.api.get_task(sheet.taskId);   // 直调
      if (!r.ok) return;                            // app.js:1879
      sheet.task = r.task;
      sheet.counts = r.counts;
      // 正在输入搜索词时不重绘（app.js:1883-1884）
      const box = document.getElementById("tk-detail-q");
      if (document.activeElement === box) return;
      renderSheet();
      renderDetailItems();
    } catch (e) { /* 轮询失败静默（app.js:1887） */ }
  }, 2000);
}

function stopSheetPoll() {
  if (sheetPollId != null) {
    clearInterval(sheetPollId);
    sheetPollId = null;
  }
}

// app.js:1902-1946 renderSheet（状态/计数/footer 按钮面）
function renderSheet() {
  if (!sheetOv || !sheet.task || !sheet.counts) return;
  const t = sheet.task;
  const c = sheet.counts;
  // 标题/sub（app.js:1904-1906）
  const titleEl = sheetOv.el.querySelector(".u-overlay-title");
  const subEl = sheetOv.el.querySelector(".u-overlay-sub");
  if (titleEl) titleEl.textContent = t.channel_title;
  if (subEl) { subEl.textContent = "@" + t.channel + " · 创建 " + t.created; subEl.hidden = false; }
  // 进度（app.js:1907-1908）
  const pct = c.total ? Math.round(c.finished * 100 / c.total) : 0;
  const fill = sheetOv.body.querySelector(".tk-sfill");
  if (fill) fill.style.width = pct + "%";
  // 计数（app.js:1909-1914：`<b>N</b> 完成 …共 <b>N</b>`——数字粗体）
  const cntBox = sheetOv.body.querySelector(".tk-scounters");
  if (cntBox) {
    while (cntBox.firstChild) cntBox.removeChild(cntBox.firstChild);
    const bits = [
      [c.finished, " 完成"], [c.pending, " 未下"], [c.failed, " 失败"],
      [c.skipped, " 跳过"],
    ];
    for (let i = 0; i < bits.length; i++) {
      const b = bits[i];
      cntBox.append(el("span", {}, [
        el("b", { text: String(b[0] == null ? 0 : b[0]) }), b[1],
      ]));
    }
    cntBox.append(el("span", {}, ["共 ", el("b", { text: String(c.total == null ? 0 : c.total) })]));
  }
  // 状态分段计数（app.js:1915-1923：label + N，active 随 filter）
  if (detailSeg) {
    const segEl = detailSeg.el;
    segEl.querySelectorAll(".u-seg-item").forEach(function (b) {
      const v = b.dataset.value;
      b.textContent = (SEG_LABELS[v] || v) + " " + segCount(v, c);
    });
    detailSeg.set(sheet.filter, {});
  }
  // footer 按钮（app.js:1926-1946）
  const rb = resumeButton(c);
  const resumeBtn = sheetOv.body.querySelector("#tk-sheet-resume");
  if (resumeBtn) {
    resumeBtn.textContent = rb.text;
    resumeBtn.disabled = rb.disabled;
    resumeBtn.classList.toggle("is-dim", rb.disabled);   // 旧壳 opacity:.4 语义
  }
  const skipBtn = sheetOv.body.querySelector("#tk-sheet-skip");
  if (skipBtn) {
    skipBtn.hidden = !(c.skipped > 0);                  // app.js:1936（编辑态本波无）
    skipBtn.textContent = "恢复跳过 " + (c.skipped || 0) + " 个";  // app.js:1945
  }
}

// app.js:1948-1952 setDetailFilter
function setDetailFilter(st) {
  sheet.filter = st;
  renderSheet();
  renderDetailItems();
}

// app.js:1959-2025 renderDetailItems（状态/搜索过滤 + 行构建 + 展开）
function renderDetailItems() {
  if (!sheetOv || !sheet.task) return;
  const box = document.getElementById("tk-detail-q");
  sheet.q = box ? box.value.trim().toLowerCase() : "";   // app.js:1960
  const list = sheetOv.body.querySelector(".tk-dlist");
  if (!list) return;
  const items = Array.isArray(sheet.task.items) ? sheet.task.items : [];
  const rows = filterDetailItems(items, sheet.filter, sheet.q);
  while (list.firstChild) list.removeChild(list.firstChild);
  if (!rows.length) {
    list.append(createEmptyState({ icon: "⌕", title: "没有符合条件的条目", hint: null }).el);
    return;
  }
  for (let i = 0; i < rows.length; i++) list.append(buildDetailItem(rows[i]));
}

// 单条目（app.js:1975-2022 结构等价；编辑复选框本波无 → 浏览态）
function buildDetailItem(ti) {
  const src = ti.src || "频道";
  const k = keyOf(src, ti.msg);
  const f = sheet.files[k] || {};
  const ex = sheet.expanded.has(k);
  const stCls = statusClass(ti.status);
  // 操作面（app.js:1991-2001 浏览态条件）
  const acts = el("div", { class: "tk-dacts" });
  if (f.exists && !sheet.headless) {
    acts.append(el("button", {
      class: "tk-mact", type: "button", title: "播放/打开文件", "aria-label": "播放/打开文件",
      text: "▶",
      onclick: function (ev) { ev.stopPropagation(); dPlay(src, ti.msg, ev.currentTarget); },
    }));
  }
  if (ti.status === "pending" || ti.status === "failed" || ti.status === "skipped") {
    acts.append(el("button", {
      class: "tk-mact", type: "button", title: "标记重新下载", "aria-label": "标记重新下载",
      text: "↻",
      onclick: function (ev) { ev.stopPropagation(); dStatus(src, ti.msg, "pending", ev.currentTarget); },
    }));
  }
  if (ti.status !== "skipped") {
    acts.append(el("button", {
      class: "tk-mact", type: "button", title: "标记跳过", "aria-label": "标记跳过",
      text: "⤼",
      onclick: function (ev) { ev.stopPropagation(); dStatus(src, ti.msg, "skipped", ev.currentTarget); },
    }));
  }
  acts.append(el("button", {
    class: "tk-mact tk-mact--danger", type: "button", title: "从任务移除", "aria-label": "从任务移除",
    text: "✕",
    onclick: function (ev) { ev.stopPropagation(); dRemove(src, ti.msg, ev.currentTarget); },
  }));

  const ditem = el("div", { class: "tk-ditem" + (ex ? " is-open" : ""), role: "listitem" }, [
    el("div", { class: "tk-dmain" }, [
      el("div", {
        class: "tk-dinfo", role: "button", tabindex: "0",
        "aria-expanded": ex ? "true" : "false",
        "aria-label": "展开条目详情 " + String(ti.name || ""),
        onclick: function () { toggleDetailExpand(k); },   // app.js:2026-2029
        onkeydown: function (e) {
          if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggleDetailExpand(k); }
        },
      }, [
        el("div", { class: "tk-dname", text: String(ti.name || "") }),
        el("div", { class: "tk-dsub" }, [
          el("span", { class: "tk-badge tk-badge--" + stCls, text: statusLabel(ti.status) }),
          el("span", { text: String(ti.date || "") }),
          el("span", { text: human(ti.size) }),
          el("span", { class: "tk-srctag", text: src }),
          f.partial ? el("span", { class: "tk-half", text: "半成" }) : null,   // app.js:2013
        ]),
      ]),
      acts,
    ]),
    el("div", { class: "tk-dtext", text: detailItemText(ti) }),   // app.js:2018-2020
  ]);
  return ditem;
}

// app.js:2026-2029 toggleDetailExpand
function toggleDetailExpand(k) {
  if (sheet.expanded.has(k)) sheet.expanded.delete(k);
  else sheet.expanded.add(k);
  renderDetailItems();
}

// ============================================================
// 条目动作（app.js:2035-2091：忙态 + 失败可见；confirm 逐字承接）
// ============================================================

// app.js:2035-2050 dPlay（open_item_file；失败 toast）
async function dPlay(src, mid, btn) {
  const restore = busyOn(btn, null);
  let r = null;
  try {
    const ready = await ensureApi();
    if (!ready) throw new Error("bridge not ready");
    r = await window.pywebview.api.open_item_file(sheet.taskId, src, mid);   // 直调
  } catch (e) {
    restore();
    toast("error", "打开文件失败", (e && e.message) || String(e));   // app.js:2043
    return;
  }
  restore();
  if (!r || !r.ok) toast("error", "打开文件失败", (r && r.error) || "文件不存在或无法打开");
}

// app.js:2052-2070 dStatus（set_item → 成功 reloadSheet）
async function dStatus(src, mid, status, btn) {
  const restore = busyOn(btn, null);
  let r = null;
  try {
    const ready = await ensureApi();
    if (!ready) throw new Error("bridge not ready");
    r = await window.pywebview.api.set_item(sheet.taskId, src, mid, status);   // 直调
  } catch (e) {
    restore();
    toast("error", "标记条目状态失败", (e && e.message) || String(e));   // app.js:2060
    return;
  }
  restore();
  if (!r || !r.ok) {
    toast("error", "标记条目状态失败", (r && r.error) || "操作未生效");   // app.js:2066
    return;
  }
  await reloadSheet();
}

// app.js:2072-2091 dRemove（confirm 逐字 → remove_items → reloadSheet）
function dRemove(src, mid, btn) {
  confirmAsk({
    text: "从任务移除该条目？本地文件不会删除。",   // app.js:2073 逐字
    confirmLabel: "移除", danger: true,
    onConfirm: async function () {
      const restore = busyOn(btn, null);
      let r = null;
      try {
        const ready = await ensureApi();
        if (!ready) throw new Error("bridge not ready");
        r = await window.pywebview.api.remove_items(sheet.taskId, [{ src: src, mid: mid }]);   // 直调
      } catch (e) {
        restore();
        toast("error", "移除条目失败", (e && e.message) || String(e));   // app.js:2081
        return;
      }
      restore();
      if (!r || !r.ok) {
        toast("error", "移除条目失败", (r && r.error) || "操作未生效");   // app.js:2087
        return;
      }
      await reloadSheet();
    },
  });
}

// app.js:2096-2125 reloadSheet（get_task + files fallback + 双渲染；
// 失败可见但渲染继续，不白屏）
async function reloadSheet() {
  if (sheet.taskId == null) return;
  let r;
  try {
    const ready = await ensureApi();
    if (!ready) throw new Error("bridge not ready");
    r = await window.pywebview.api.get_task(sheet.taskId);   // 直调
  } catch (e) {
    toast("error", "刷新任务详情失败", (e && e.message) || String(e));   // app.js:2101
    renderSheet(); renderDetailItems();
    return;
  }
  if (!r || !r.ok) {
    toast("error", "刷新任务详情失败", (r && r.error) || "任务读取失败");   // app.js:2106
    renderSheet(); renderDetailItems();
    return;
  }
  sheet.task = r.task;
  sheet.counts = r.counts;
  const files = extractFiles(r);
  if (files) sheet.files = files;
  else await loadItemFiles(sheet.taskId, true);
  renderSheet();
  renderDetailItems();
}

// 条目文件状态 fallback（app.js:1850-1859：items_file_info，失败可见渲染继续）
async function loadItemFiles(taskId, quietOnEmpty) {
  let fr = null;
  try {
    const ready = await ensureApi();
    if (!ready) throw new Error("bridge not ready");
    fr = await window.pywebview.api.items_file_info(taskId);   // 直调
  } catch (e) {
    toast("error", "读取条目文件状态失败", (e && e.message) || String(e));   // app.js:1855
    return;
  }
  if (fr && fr.ok) sheet.files = fr.items || {};
  else if (!quietOnEmpty && fr) {
    toast("error", "读取条目文件状态失败", fr.error || "读取失败");   // app.js:1858
  }
}

// app.js:3825-3854 extractTaskFiles（三处防御性兼容：r.files /
// r.task.files 映射 / ti.file_info 条目级；形状不符视为未携带）
function extractFiles(r) {
  const fromMap = function (m) {
    if (!m || typeof m !== "object") return null;
    const out = {};
    let n = 0;
    for (const k of Object.keys(m)) {
      const v = m[k];
      if (v && typeof v === "object" && typeof v.exists === "boolean") {
        out[k] = { exists: v.exists, partial: !!v.partial };
        n++;
      }
    }
    return n ? out : null;
  };
  let got = fromMap(r && r.files);
  if (got) return got;
  got = fromMap(r && r.task && r.task.files);
  if (got) return got;
  const items = (r && r.task && Array.isArray(r.task.items)) ? r.task.items : [];
  const out = {};
  let n = 0;
  for (let i = 0; i < items.length; i++) {
    const ti = items[i];
    const fi = ti && (ti.file_info || ti.file);
    if (fi && typeof fi === "object" && typeof fi.exists === "boolean") {
      out[keyOf(ti.src || "频道", ti.msg)] = { exists: fi.exists, partial: !!fi.partial };
      n++;
    }
  }
  return n ? out : null;
}

// app.js:1898-1900 openSheetEditor —— T19h 用户裁决恢复 2.1 双窗：
// 编辑入口开独立编辑窗（open_editor 辅助窗 + taskedit.html 2.1 布局；
// T16「禁调 open_editor」口径随用户指令废止），/task/:id/edit 深链视图
// 保留作兜底：开窗失败（含桩返回非 ok）静默回落深链。
function openSheetEditor(taskId) {
  if (!taskId) return;
  const fallback = function () {
    navigate("/task/" + encodeURIComponent(taskId) + "/edit");
  };
  try {
    const p = window.pywebview && window.pywebview.api
      ? window.pywebview.api.open_editor(taskId) : null;
    if (!p || typeof p.then !== "function") return fallback();
    p.then(function (r) {
      if (r && r.ok) return;
      fallback();
    }).catch(fallback);
  } catch (e) {
    fallback();
  }
}
