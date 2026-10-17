// views/editor.js — T16 任务编辑器（应用内全屏路由 task/:id/edit）。
// 所有权恰两文件（+ web/css/views/editor.css）。行为基线=旧壳
// editor.html + editor.js（首拉 61-91 / 回捞 93-130 / 筛选 132-146 /
// 渲染 148-209 / 批量 211-234、277-312 / 单条 236-275 / 设置 314-341 /
// T50 搬运 343-429 / 保存 431-471），只读对照逐条保留不回归。
// RPC 白名单消费恰 7：editor_data/editor_set_status/editor_remove/
// open_item_file/editor_save/choose_folder/job_state；禁 open_editor。
// thenable 桥不变量（bridge.js:69-74 Proxy）：ensureApi 只 resolve 布尔，
// 调用同步直调 window.pywebview.api.xxx(...)，绝不 await/resolve/return 桥对象。
// 5A seam 九纯函数命名导出 + REFETCH_DELAYS；顶层零 DOM/window。
// ev- 前缀（不用 ed-：旧壳 .ed-* 全局泄漏，style.css:730-800）。

import {
  el, createSegmented, createOverlay, createToaster,
  createEmptyState, createProgress, createBottomActionBar,
} from "../ui/index.js";
// T17 手势增量（边缘返回/滑动托盘/聚焦保持；见 evidence/T17/t17_diff_list.md）
import {
  attachEdgeBack, attachSwipeActions, attachFocusKeep, trayFromButtons,
} from "../gestures.js";

export const path = "task/:id/edit";

export const title = "任务编辑 - TG 视频下载器";

// 状态徽标（editor.js:52-58；exists 归已完成）
export const STATUS_LABEL = {
  done: ["已完成", "done"],
  pending: ["未下载", "pending"],
  failed: ["失败", "failed"],
  skipped: ["跳过", "skipped"],
  exists: ["已完成", "done"],
};

// 筛选五档（editor.html:63-67）
const FILTER_SEGS = [
  { value: "all", label: "全部" },
  { value: "done", label: "已完成" },
  { value: "pending", label: "未下载" },
  { value: "failed", label: "失败" },
  { value: "skipped", label: "跳过" },
];

// 回捞节拍（editor.js:107 DELAYS 逐字）
export const REFETCH_DELAYS = [1500, 3000, 4500];

// ---- S1 keyOf / S2 keyObj（editor.js:19-26） ----
export function keyOf(ti) {
  return ((ti && ti.src) || "channel") + ":" + ((ti && ti.msg) == null ? "" : ti.msg);
}

export function keyObj(k) {
  const i = String(k).indexOf(":");
  return { src: String(k).slice(0, i), mid: Number(String(k).slice(i + 1)) };
}

// ---- S3 humanSize（editor.js:28-34） ----
export function humanSize(n) {
  n = Number(n) || 0;
  if (n >= 1073741824) return (n / 1073741824).toFixed(2) + " GB";
  if (n >= 1048576) return (n / 1048576).toFixed(1) + " MB";
  if (n >= 1024) return (n / 1024).toFixed(0) + " KB";
  return n + " B";
}

// ---- S4 fmtDur（editor.js:36-44） ----
export function fmtDur(s) {
  s = Number(s) || 0;
  if (!s) return "—";
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const ss = s % 60;
  const pad = function (x) { return String(x).padStart(2, "0"); };
  return h ? h + ":" + pad(m) + ":" + pad(ss) : m + ":" + pad(ss);
}

// ---- S5 statusLabel（editor.js:52-58；未知原样透传） ----
export function statusLabel(s) {
  return STATUS_LABEL[s] || [String(s == null ? "" : s), ""];
}

// ---- S6 filterItems（editor.js:133-146；done 含 exists，q 命中 name/text） ----
export function filterItems(items, filter, q) {
  const list = Array.isArray(items) ? items : [];
  const qq = String(q == null ? "" : q).trim().toLowerCase();
  return list.filter(function (ti) {
    if (filter === "done" && ti.status !== "done" && ti.status !== "exists")
      return false;
    if (filter !== "all" && filter !== "done" && ti.status !== filter)
      return false;
    if (qq && !((ti.name || "").toLowerCase().includes(qq) ||
                (ti.text || "").toLowerCase().includes(qq)))
      return false;
    return true;
  });
}

// ---- S7 needEnrich（editor.js:101-103） ----
export function needEnrich(items) {
  return (Array.isArray(items) ? items : []).some(function (it) {
    return !it.text || !it.duration;
  });
}

// ---- S8 pctOf（editor.js:399-401） ----
export function pctOf(bd, bt) {
  const done = Number(bd) || 0, total = Number(bt) || 0;
  return total > 0
    ? Math.max(0, Math.min(100, done * 100 / total)) : 0;
}

// ---- S9 saveFields（editor.js:440-445；parallel 仅 Number 归一不加钳） ----
export function saveFields(titleV, folderV, parallelV, moveV) {
  return {
    title: String(titleV == null ? "" : titleV),
    folder: String(folderV == null ? "" : folderV),
    parallel: Number(parallelV),
    move_files: !!moveV,
  };
}

// ---- RPC 桥（就绪 Promise 只 resolve 布尔） ----
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
      return ready;
    });
  }
  return apiReadyP;
}

// 导航：优先 router 探针，降级 hash（同 tasks.js 范式）
function navigate(p) {
  if (typeof window !== "undefined" && window.__t8router &&
      typeof window.__t8router.navigate === "function") {
    window.__t8router.navigate(p);
    return;
  }
  if (typeof location !== "undefined") location.hash = "#" + p;
}

function busyOn(btn, text) {
  if (!btn) return function () {};
  const prevText = btn.textContent;
  const prevDisabled = btn.disabled;
  btn.disabled = true;
  if (text) btn.textContent = text;
  return function () {
    btn.disabled = prevDisabled;
    btn.textContent = prevText;
  };
}

// ---- 视图状态（mount 一次建 DOM；onEnter 重置数据态） ----
const state = {
  taskId: "",
  task: null,
  items: [],
  selected: new Set(),   // "src:mid"
  expanded: new Set(),
  filter: "all",
  q: "",
  loading: false,
  error: "",
};

let refs = null;
let cached = null;
let toaster = null;
let confirmOv = null;
let refetchGen = 0;
let moveSt = null;        // {jobId, dom, handler, lastTs, ended, onDone, onClose}

function toast(type, text, detail) {
  if (!toaster) return;
  toaster.show({ type: type, text: text, detail: detail });
}

// ---- 确认弹层（T10 §4：遮罩/ESC/✕/取消=不执行） ----
function confirmAskP(opts) {
  return new Promise(function (resolve) {
    let settled = false;
    const done = function (v) {
      if (settled) return;
      settled = true;
      resolve(v);
    };
    destroyConfirm();
    confirmOv = createOverlay({
      title: (opts && opts.title) || "确认操作",
      content: (opts && opts.content) || "",
      form: "modal",
      actions: [
        {
          key: "ok",
          label: (opts && opts.confirmLabel) || "确定",
          primary: !(opts && opts.danger),
          danger: !!(opts && opts.danger),
        },
        { key: "cancel", label: "取消" },
      ],
      onAction: function (k) { done(k === "ok"); },
      onClose: function () { done(false); },
      ariaLabel: (opts && opts.title) || "确认操作",
    });
    document.body.appendChild(confirmOv.el);
    confirmOv.open();
  });
}

function destroyConfirm() {
  if (confirmOv) {
    try { confirmOv.destroy(); } catch (e) {}
    confirmOv = null;
  }
}

// ---- buildDom 四区：设置头 / 工具栏 / 条目区 / 批量条 ----
function buildDom() {
  const root = el("div", { class: "vw-editor" });

  toaster = createToaster();

  const btnBack = el("button", {
    class: "u-btn u-btn--ghost ev-back", type: "button", text: "返回任务库",
    onclick: function () { navigate("/tasks"); },
  });
  const titleInput = el("input", {
    class: "ev-title-input", type: "text", spellcheck: "false",
    "aria-label": "任务标题", id: "ev-title",
  });
  const metaChannel = el("span", { class: "ev-meta-item", text: "" });
  const metaCreated = el("span", { class: "ev-meta-item", text: "" });

  const folderInput = el("input", {
    class: "ev-folder-input", type: "text", readonly: true,
    "aria-label": "保存目录", id: "ev-folder",
  });
  const btnBrowse = el("button", {
    class: "u-btn u-btn--ghost ev-sm", type: "button", text: "浏览",
    onclick: function () { chooseFolder(btnBrowse); },
  });
  const parallelInput = el("input", {
    class: "ev-parallel-input", type: "number", min: "1", max: "8",
    "aria-label": "并发", id: "ev-parallel",
  });
  const moveCheck = el("input", {
    type: "checkbox", id: "ev-move",
  });
  const btnSave = el("button", {
    class: "u-btn u-btn--primary", type: "button", text: "保存设置",
    onclick: function () { save(btnSave); },
  });

  const head = el("div", { class: "ev-head u-card" }, [
    el("div", { class: "ev-head-main" }, [
      btnBack,
      el("div", { class: "ev-task-info" }, [
        titleInput,
        el("div", { class: "ev-meta" }, [metaChannel, metaCreated]),
      ]),
    ]),
    el("div", { class: "ev-settings" }, [
      el("div", { class: "ev-field" }, [
        el("label", { class: "ev-label", for: "ev-folder", text: "保存目录" }),
        el("div", { class: "ev-path-row" }, [folderInput, btnBrowse]),
      ]),
      el("div", { class: "ev-field ev-field--narrow" }, [
        el("label", { class: "ev-label", for: "ev-parallel", text: "并发 (1-8)" }),
        parallelInput,
      ]),
      el("label", { class: "ev-check-line" }, [
        moveCheck,
        el("span", { text: "同时移动已下载文件" }),
      ]),
      btnSave,
    ]),
  ]);

  const qInput = el("input", {
    class: "ev-q-input", type: "text",
    placeholder: "筛选文件名 / TG 消息原文…", id: "ev-q",
    oninput: function () {
      state.q = qInput.value;
      render();
    },
  });
  let segSilent = true;   // createSegmented 构造 set 触发 onChange，静默吞掉
  const filterSeg = createSegmented(FILTER_SEGS, {
    value: "all",
    ariaLabel: "条目状态筛选",
    onChange: function (v) {
      if (segSilent) return;
      state.filter = v;
      render();
    },
  });
  segSilent = false;
  const btnAll = el("button", {
    class: "u-btn u-btn--ghost ev-sm", type: "button", text: "全选",
    onclick: function () { selectAllVisible(true); },
  });
  const btnNone = el("button", {
    class: "u-btn u-btn--ghost ev-sm", type: "button", text: "清空",
    onclick: function () { selectAllVisible(false); },
  });
  const countsEl = el("span", { class: "ev-counts", text: "" });

  const toolbar = el("div", { class: "ev-toolbar u-card" }, [
    el("div", { class: "ev-search" }, [
      el("span", { class: "ev-search-ico", "aria-hidden": "true", text: "⌕" }),
      qInput,
    ]),
    filterSeg.el,
    el("div", { class: "ev-sel-btns" }, [btnAll, btnNone]),
    countsEl,
  ]);

  const loadingEl = el("p", { class: "ev-loading", role: "status", text: "加载中…" });
  const errorEl = el("p", { class: "ev-load-err", role: "alert", hidden: true, text: "" });
  const emptyWrap = el("div", { class: "ev-empty-wrap", hidden: true });
  const listHead = el("div", { class: "ev-lhead", role: "row" }, [
    el("div", { class: "ev-hcell ev-c-check", role: "columnheader", text: "" }),
    el("div", { class: "ev-hcell ev-c-name", role: "columnheader", text: "文件名" }),
    el("div", { class: "ev-hcell ev-c-msg", role: "columnheader", text: "TG 消息原文" }),
    el("div", { class: "ev-hcell ev-c-dur", role: "columnheader", text: "时长" }),
    el("div", { class: "ev-hcell ev-c-date", role: "columnheader", text: "日期" }),
    el("div", { class: "ev-hcell ev-c-size", role: "columnheader", text: "大小" }),
    el("div", { class: "ev-hcell ev-c-st", role: "columnheader", text: "状态" }),
    el("div", { class: "ev-hcell ev-c-act", role: "columnheader", text: "操作" }),
  ]);
  const rowsEl = el("div", { class: "ev-rows", role: "list", "aria-label": "任务条目" });
  const list = el("div", { class: "ev-list u-card" }, [listHead, rowsEl]);
  const main = el("div", { class: "ev-main" }, [loadingEl, errorEl, emptyWrap, list]);

  // 批量条（T10 §4 #edit-bulk 承接）
  const bulk = createBottomActionBar({
    actions: [
      { key: "pending", label: "标记重新下载", enabled: true },
      { key: "skipped", label: "标记跳过", enabled: true },
      { key: "remove", label: "移除条目", danger: true, enabled: true },
    ],
    count: 0,
    hidden: true,
    ariaLabel: "批量操作",
    onAction: function (k) {
      if (k === "pending") bulkStatus("pending");
      else if (k === "skipped") bulkStatus("skipped");
      else bulkRemove();
    },
  });

  root.append(head, toolbar, main, bulk.el);

  refs = {
    root: root,
    titleInput: titleInput,
    metaChannel: metaChannel,
    metaCreated: metaCreated,
    folderInput: folderInput,
    btnBrowse: btnBrowse,
    parallelInput: parallelInput,
    moveCheck: moveCheck,
    btnSave: btnSave,
    qInput: qInput,
    filterSeg: filterSeg,
    countsEl: countsEl,
    loadingEl: loadingEl,
    errorEl: errorEl,
    emptyWrap: emptyWrap,
    listHead: listHead,
    rowsEl: rowsEl,
    list: list,
    bulk: bulk,
    emptyState: null,
  };

  refs.emptyState = createEmptyState({
    icon: "∅", title: "没有可显示的条目", hint: "可切换筛选或清空搜索关键词",
  });
  emptyWrap.appendChild(refs.emptyState.el);
}

// ---- 装载（editor.js:61-91；失败两形"加载失败：…"） ----
async function load() {
  if (!refs || !state.taskId) return;
  state.loading = true;
  state.error = "";
  renderStatus();
  let r = null;
  try {
    const ready = await ensureApi();
    if (!ready) throw new Error("bridge not ready");
    r = await window.pywebview.api.editor_data(state.taskId);   // 同步直调
  } catch (e) {
    state.loading = false;
    state.error = "加载失败：" + ((e && e.message) || e);
    renderStatus();
    return;
  }
  state.loading = false;
  if (!r || !r.ok) {
    state.error = "加载失败：" + ((r && r.error) || "");
    renderStatus();
    return;
  }
  state.task = r.task || null;
  state.items = r.items || [];
  fillTask();
  render();
  if (needEnrich(state.items)) startRefetch();   // editor.js:90
  else stopRefetch();
}

function fillTask() {
  const t = state.task || {};
  refs.titleInput.value = t.channel_title || "";
  refs.metaChannel.textContent = "频道：" + (t.channel || "");
  refs.metaCreated.textContent = "创建：" + (t.created || "");
  refs.folderInput.value = t.folder || "";
  refs.parallelInput.value = t.parallel || 1;
  // move 勾选不回填（基线 edLoad 不设 #ed2-move，editor.js:61-91）
}

function renderStatus() {
  if (!refs) return;
  refs.loadingEl.hidden = !state.loading;
  refs.errorEl.hidden = !state.error;
  refs.errorEl.textContent = state.error || "";
  const showList = !state.loading && !state.error;
  refs.list.hidden = !showList;
  refs.emptyWrap.hidden = true;
}

// ---- 渲染（editor.js:148-209；表↔卡片纯 CSS 变形，DOM 同构） ----
function render() {
  if (!refs || state.loading || state.error) {
    renderStatus();
    return;
  }
  state.q = refs.qInput.value;
  const rows = filterItems(state.items, state.filter, state.q);
  const frag = document.createDocumentFragment();

  for (const ti of rows) {
    const k = keyOf(ti);
    const checked = state.selected.has(k);
    const isOpen = state.expanded.has(k);
    const st = statusLabel(ti.status);
    const text = ti.text || "";

    const cbox = el("button", {
      class: "ev-cbox" + (checked ? " is-on" : ""), type: "button",
      "aria-pressed": checked ? "true" : "false",
      "aria-label": (checked ? "取消选择 " : "选择 ") + (ti.name || k),
      text: checked ? "✓" : "",
      onclick: function () { toggleSelect(k); },
    });
    const msgCell = el("div", {
      class: "ev-cell ev-c-msg" + (text ? "" : " is-empty"),
      role: "cell", tabindex: "0",
      onclick: function () { toggleExpand(k); },
      onkeydown: function (e) {
        if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggleExpand(k); }
      },
    }, [el("span", { class: "ev-msg-line", text: text || "（无消息文本，点击仍可展开）" })]);

    const row = el("div", {
      class: "ev-row" + (checked ? " is-checked" : ""), role: "listitem",
      dataset: { k: k },
    }, [
      el("div", { class: "ev-cell ev-c-check", role: "cell" }, [cbox]),
      el("div", {
        class: "ev-cell ev-c-name", role: "cell",
        title: String(ti.name == null ? "" : ti.name),
        text: String(ti.name == null ? "" : ti.name),
      }),
      msgCell,
      el("div", { class: "ev-cell ev-c-dur", role: "cell", text: fmtDur(ti.duration) }),
      el("div", { class: "ev-cell ev-c-date", role: "cell", text: String(ti.date || "—") }),
      el("div", { class: "ev-cell ev-c-size", role: "cell", text: humanSize(ti.size) }),
      el("div", { class: "ev-cell ev-c-st", role: "cell" }, [
        el("span", { class: "ev-badge ev-badge--" + st[1], text: st[0] }),
      ]),
      el("div", { class: "ev-cell ev-c-act", role: "cell" }, [
        el("button", {
          class: "u-btn u-btn--ghost ev-sm", type: "button", text: "文件",
          title: "打开本地文件",
          onclick: function () { openFile(k); },
        }),
        el("button", {
          class: "u-btn u-btn--ghost ev-sm", type: "button", text: "跳过",
          title: "标记跳过",
          onclick: function () { oneStatus(k, "skipped"); },
        }),
        el("button", {
          class: "u-btn u-btn--ghost ev-sm ev-danger", type: "button", text: "移除",
          title: "从清单移除",
          onclick: function () { oneRemove(k); },
        }),
      ]),
    ]);
    frag.appendChild(row);

    if (isOpen) {
      // 详情行（editor.js:187-200）
      frag.appendChild(el("div", { class: "ev-detail", role: "listitem" }, [
        el("div", { class: "ev-detail-label" },
          "TG 消息原文 · #" + ti.msg +
          (ti.error ? " · 失败原因：" + ti.error : "")),
        el("pre", { class: "ev-detail-text",
          text: text || "（该条目没有保存消息文本）" }),
      ]));
    }
  }

  while (refs.rowsEl.firstChild) refs.rowsEl.removeChild(refs.rowsEl.firstChild);
  refs.rowsEl.appendChild(frag);

  refs.countsEl.textContent =
    "显示 " + rows.length + " / " + state.items.length + " 条";   // editor.js:205-207
  const empty = state.items.length === 0;
  refs.list.hidden = empty;
  refs.emptyWrap.hidden = !empty;
  renderStatus2();
  syncBulk();
}

function renderStatus2() {
  if (!refs) return;
  if (state.loading || state.error) {
    refs.list.hidden = true;
    refs.emptyWrap.hidden = true;
  }
}

function syncBulk() {
  if (!refs) return;
  const n = state.selected.size;
  refs.bulk.setCount(n);
  if (n > 0) refs.bulk.show();
  else refs.bulk.hide();
}

// ---- 选择（editor.js:219-234；全选=当前筛选集；清空=全清） ----
function toggleSelect(k) {
  if (state.selected.has(k)) state.selected.delete(k);
  else state.selected.add(k);
  render();
}

function selectAllVisible(on) {
  const rows = filterItems(state.items, state.filter, refs.qInput.value);
  if (on) {
    for (const ti of rows) state.selected.add(keyOf(ti));
  } else {
    state.selected.clear();
  }
  render();
}

function toggleExpand(k) {
  if (state.expanded.has(k)) state.expanded.delete(k);
  else state.expanded.add(k);
  render();
}

// ---- 单条操作（editor.js:240-275；成功本地最小变更零重拉） ----
async function oneStatus(k, status) {
  try {
    const ready = await ensureApi();
    if (!ready) throw new Error("bridge not ready");
    const r = await window.pywebview.api.editor_set_status(
      state.taskId, [keyObj(k)], status);                    // 同步直调
    if (!r || !r.ok)
      return toast("error", "标记失败：" + ((r && r.error) || ""));
    const ti = state.items.find(function (it) { return keyOf(it) === k; });
    if (ti) ti.status = status;
    render();
  } catch (e) {
    toast("error", "标记失败：" + ((e && e.message) || e));
  }
}

function oneRemove(k) {
  // editor.js:253 逐字
  confirmAskP({
    title: "移除条目",
    content: "从任务清单移除该条目？不会删除本地视频文件。",
    confirmLabel: "移除",
    danger: true,
  }).then(async function (yes) {
    if (!yes) return;
    try {
      const ready = await ensureApi();
      if (!ready) throw new Error("bridge not ready");
      const r = await window.pywebview.api.editor_remove(
        state.taskId, [keyObj(k)]);                          // 同步直调
      if (!r || !r.ok)
        return toast("error", "移除失败：" + ((r && r.error) || ""));
      state.items = state.items.filter(function (it) { return keyOf(it) !== k; });
      state.selected.delete(k);
      state.expanded.delete(k);
      render();
    } catch (e) {
      toast("error", "移除失败：" + ((e && e.message) || e));
    }
  });
}

async function openFile(k) {
  const o = keyObj(k);
  try {
    const ready = await ensureApi();
    if (!ready) throw new Error("bridge not ready");
    const r = await window.pywebview.api.open_item_file(
      state.taskId, o.src, o.mid);                           // 同步直调
    if (!r || !r.ok) toast("error", "打开失败：" + ((r && r.error) || ""));
  } catch (e) {
    toast("error", "打开失败：" + ((e && e.message) || e));
  }
}

// ---- 批量操作（editor.js:283-312） ----
async function bulkStatus(status) {
  const keys = [...state.selected].map(keyObj);
  if (!keys.length) return;
  try {
    const ready = await ensureApi();
    if (!ready) throw new Error("bridge not ready");
    const r = await window.pywebview.api.editor_set_status(
      state.taskId, keys, status);                           // 同步直调
    if (!r || !r.ok)
      return toast("error", "批量标记失败：" + ((r && r.error) || ""));
    const sel = new Set(state.selected);
    for (const ti of state.items)
      if (sel.has(keyOf(ti))) ti.status = status;
    state.selected.clear();
    render();
  } catch (e) {
    toast("error", "批量标记失败：" + ((e && e.message) || e));
  }
}

function bulkRemove() {
  const n = state.selected.size;
  if (!n) return;
  // editor.js:298 逐字（两行）
  confirmAskP({
    title: "移除条目",
    content: "从任务清单移除选中的 " + n + " 个条目？\n不会删除本地视频文件。",
    confirmLabel: "移除",
    danger: true,
  }).then(async function (yes) {
    if (!yes) return;
    const keys = [...state.selected].map(keyObj);
    try {
      const ready = await ensureApi();
      if (!ready) throw new Error("bridge not ready");
      const r = await window.pywebview.api.editor_remove(
        state.taskId, keys);                                 // 同步直调
      if (!r || !r.ok)
        return toast("error", "批量移除失败：" + ((r && r.error) || ""));
      const sel = new Set(state.selected);
      state.items = state.items.filter(function (it) { return !sel.has(keyOf(it)); });
      state.selected.clear();
      for (const k of [...state.expanded])
        if (sel.has(k)) state.expanded.delete(k);
      render();
    } catch (e) {
      toast("error", "批量移除失败：" + ((e && e.message) || e));
    }
  });
}

// ---- 选择目录（editor.js:323-341；P1-5 r.path 回填 + P1-3 busy 防重） ----
let choosing = false;

async function chooseFolder(btn) {
  if (choosing) return;                 // JS 锁兜底（disabled 之外）
  choosing = true;
  const restore = busyOn(btn);
  try {
    const ready = await ensureApi();
    if (!ready) throw new Error("bridge not ready");
    const r = await window.pywebview.api.choose_folder();    // 同步直调
    choosing = false;
    restore();
    if (!r || !r.ok)
      return toast("error", "选择目录失败：" + ((r && r.error) || ""));
    if (r.path) refs.folderInput.value = r.path;   // P1-5：以 path 为准
  } catch (e) {
    choosing = false;
    restore();
    toast("error", "选择目录失败：" + ((e && e.message) || e));
  }
}

// ---- 保存（editor.js:431-471；点击即锁、need_login 逐字、job_id 走搬运） ----
async function save(btn) {
  const restore = busyOn(btn, "保存中…");
  const fields = saveFields(
    refs.titleInput.value, refs.folderInput.value,
    refs.parallelInput.value, refs.moveCheck.checked);
  try {
    const ready = await ensureApi();
    if (!ready) throw new Error("bridge not ready");
    const r = await window.pywebview.api.editor_save(state.taskId, fields);  // 同步直调
    if (r && r.need_login) {
      restore();
      // editor.js:448-449 逐字（alert→warn toast）
      return toast("warn", "该功能需要登录，请在主窗口登录后重试");
    }
    if (!r || !r.ok) {
      restore();
      return toast("error", "保存失败：" + ((r && r.error) || ""));
    }
    if (r.job_id) {
      watchMove(r.job_id,
        function () {                 // done：收层+刷新+"已保存"
          restore();
          load();
          toast("ok", "已保存");
        },
        restore);                     // 错误"关闭"→恢复可重试
      return;
    }
    restore();
    toast("ok", "已保存");
  } catch (e) {
    restore();
    toast("error", "保存失败：" + ((e && e.message) || e));
  }
}

// ---- T50 搬运作业弹层（editor.js:343-429）：运行中无关闭、
// done 收层刷新、error 全文+固定提示+关闭；updated_ts 去重 ----
function buildMoveDom() {
  const prog = createProgress({ value: 0 });
  const errBlock = el("div", { class: "ev-move-err", hidden: true }, [
    el("div", { class: "ev-move-err-text", text: "" }),
    el("div", {
      class: "ev-move-err-hint",
      text: "可再次执行，已存在文件自动跳过",     // editor.js:363 逐字
    }),
    el("button", {
      class: "u-btn u-btn--primary", type: "button", text: "关闭",
      onclick: function () {
        const st = moveSt;
        const cb = st && st.onClose;
        const unlock = st && st.unlock;
        teardownMove();
        if (unlock) unlock();
        if (cb) cb();
      },
    }),
  ]);
  const mask = el("div", { class: "ev-move-mask" }, [
    el("div", {
      class: "ev-move-layer", role: "dialog", "aria-label": "正在移动文件",
    }, [
      el("div", { class: "ev-move-title", text: "正在移动文件…" }),
      el("div", { class: "ev-move-name", text: "准备移动…" }),
      el("div", { class: "ev-move-count", text: "" }),
      prog.el,
      el("div", { class: "ev-move-bytes", text: "" }),
      errBlock,
    ]),
  ]);
  document.body.appendChild(mask);
  return {
    mask: mask,
    name: mask.querySelector(".ev-move-name"),
    count: mask.querySelector(".ev-move-count"),
    bytes: mask.querySelector(".ev-move-bytes"),
    err: errBlock,
    errText: errBlock.querySelector(".ev-move-err-text"),
    prog: prog,
  };
}

function handleMove(job) {
  const st = moveSt;
  if (!st || !job || job.job_id !== st.jobId) return;
  const ts = Number(job.updated_ts) || 0;
  if (st.lastTs !== null && ts <= st.lastTs) return;    // 迟到/同值帧
  st.lastTs = ts;
  const d = Number(job.done) || 0, t = Number(job.total) || 0;
  const bd = Number(job.bytes_done) || 0, bt = Number(job.bytes_total) || 0;
  st.dom.name.textContent = job.phase_detail || "";
  st.dom.count.textContent = t > 0 ? d + "/" + t : "";
  st.dom.bytes.textContent = bt > 0
    ? humanSize(bd) + " / " + humanSize(bt) : "";
  st.dom.prog.set(pctOf(bd, bt));
  if (job.state === "done" && !st.ended) {
    st.ended = true;
    const cb = st.onDone;
    teardownMove();
    if (cb) cb();
  } else if ((job.state === "error" || job.state === "cancelled") && !st.ended) {
    st.ended = true;
    st.dom.err.hidden = false;
    st.dom.errText.textContent = job.error || "移动失败";
  }
}

function watchMove(jobId, onDone, onClose) {
  teardownMove();
  const dom = buildMoveDom();
  moveSt = {
    jobId: jobId, dom: dom, lastTs: null, ended: false,
    onDone: onDone, onClose: onClose,
    unlock: function () {},
  };
  moveSt.handler = function (ev) { handleMove(ev && ev.detail); };
  window.addEventListener("tg:job_update", moveSt.handler);
  // 挂载即拉快照兜底（防 WS 漏帧）editor.js:425-428
  ensureApi().then(function (ready) {
    if (!ready || !moveSt || moveSt.jobId !== jobId) return;
    window.pywebview.api.job_state(jobId).then(function (r) {
      if (r && r.ok && r.job) handleMove(r.job);
    }).catch(function () {});
  });
}

function teardownMove() {
  if (!moveSt) return;
  window.removeEventListener("tg:job_update", moveSt.handler);
  if (moveSt.dom && moveSt.dom.mask && moveSt.dom.mask.parentNode)
    moveSt.dom.mask.remove();
  if (moveSt.dom && moveSt.dom.prog) {
    try { moveSt.dom.prog.destroy(); } catch (e) {}
  }
  moveSt = null;
}

// ---- 补全回捞（editor.js:93-130 单飞轮询；代际作废；至多 3 拍） ----
function stopRefetch() {
  refetchGen++;
}

function startRefetch() {
  const gen = ++refetchGen;
  const delays = REFETCH_DELAYS.slice();
  const tick = function () {
    if (gen !== refetchGen) return;
    if (!delays.length) return;
    const wait = delays.shift();
    setTimeout(function () {
      if (gen !== refetchGen) return;
      // 搜索框聚焦的拍跳过（不打断输入）
      if (document.activeElement === refs.qInput) { tick(); return; }
      ensureApi().then(function (ready) {
        if (!ready) throw new Error("bridge not ready");
        return window.pywebview.api.editor_data(state.taskId, false);  // 仅取本地
      }).then(function (r) {
        if (gen !== refetchGen) return;
        if (r && r.ok) {
          state.items = r.items || state.items;
          render();
          if (!needEnrich(state.items)) return;   // 补齐即停
        }
        tick();
      }).catch(function () {                     // 回捞失败静默续拍
        if (gen === refetchGen) tick();
      });
    }, wait);
  };
  tick();
}

// ---- 五员契约 ----
export function mount(container) {
  if (cached) return cached;
  try {
    buildDom();
  } catch (e) {
    // 降级元素：唯一 console.error（[T16] 标签）
    console.error("[T16] 编辑器视图构建失败：", e);
    const fb = el("div", {
      class: "vw-editor vw-editor--fallback",
      "data-view-name": "editor",
    }, [el("p", { class: "ev-load-err", text: "编辑器加载失败，请返回任务库重试" })]);
    cached = fb;
    return cached;
  }
  refs.root.setAttribute("data-view-name", "editor");
  cached = refs.root;
  return cached;
}

export function onEnter(params) {
  stopRefetch();
  teardownMove();
  destroyConfirm();
  state.taskId = String((params && params.id) || "");
  state.task = null;
  state.items = [];
  state.selected = new Set();
  state.expanded = new Set();
  state.filter = "all";
  state.q = "";
  state.loading = false;
  state.error = "";
  if (!refs) return;
  attachGestures();   // T17 手势增量（幂等）
  refs.qInput.value = "";
  refs.filterSeg.set("all");
  refs.titleInput.value = "";
  refs.metaChannel.textContent = "";
  refs.metaCreated.textContent = "";
  refs.folderInput.value = "";
  refs.parallelInput.value = 1;
  refs.moveCheck.checked = false;
  if (state.taskId) load();
  else {
    state.error = "加载失败：任务 ID 缺失";
    renderStatus();
  }
}

export function onLeave() {
  stopRefetch();
  teardownMove();
  destroyConfirm();
  // T17 手势增量对称回收（attach* 均返回 detach）
  for (let i = 0; i < gsDetach.length; i++) gsDetach[i]();
  gsDetach = [];
}

// T17 手势增量：边缘返回 + 行滑动托盘（透传 .ev-c-act 既有按钮）+ 聚焦保持
let gsDetach = [];
function attachGestures() {
  if (gsDetach.length) return;
  gsDetach = [
    attachEdgeBack(),
    attachSwipeActions(refs.root, {
      itemSel: ".ev-row",
      actions: function (row) { return trayFromButtons(row, ".ev-c-act .u-btn"); },
    }),
    attachFocusKeep(refs.root),
  ];
}
