// ============================================================
// views/new.js — T12 新建下载视图（手机分步向导 + T48 流式搜索 + 批量勾选）
//
// 归属波次：T12（v2.2 W4-b），替换 T8 占位。所有权恰两文件：
//   web/js/views/new.js（本文件）+ web/css/views/new.css。
//
// 行为基线（对齐不回归，逐条 文件:行 注明出处）：
//   - 分步/折叠/收集参数      app.js:696-739
//   - doPreview/need_login     app.js:741-771
//   - 空态文案 previewEmptyHint app.js:773-784
//   - renderPreview/行内更新   app.js:786-838, 839-850
//   - doStart（alert→toast）   app.js:852-872（T10 契约 §4 映射表承接）
//   - T48 流式全家             app.js:877-1082
//
// 组件消费：T10 桶唯唯一入口 ../ui/index.js（createCard/createSegmented/
//   createProgress/createToaster/el/append），签名见 t10_contract.md。
// RPC 白名单消费恰五个：preview / preview_cancel / start_download /
//   account_list / edit_task（45 集合内；后两者 T19a 任务选号承接）。
// 导航：window.__t8router.navigate（降级 location.hash）。
//
// 5A seam 出口（任务书 §3 授权，与 T11 同款）：collectParams/
//   classifyPreviewAck/mergeItems/reconcileFinal/emptyReason/selectionPatch/
//   selectAllPatch/classifySearchJob/filterRows 九个纯函数额外命名导出，
//   仅供测试消费；本模块顶层零 DOM/window 访问（node import 即证）。
//
// T19a 承接 TgAccounts.applyNewTaskAcct（任务选号策略，app.js:3625-3631）：
//   新建页选号随 edit_task({acct_pref}) fire-and-forget 下发（见 doStart 尾）。
// ============================================================

import {
  el, append, createCard, createProgress, createSegmented, createToaster,
} from "../ui/index.js";

// T17 手势增量（边缘返回/聚焦保持；见 evidence/T17/t17_diff_list.md）
import { attachEdgeBack, attachFocusKeep } from "../gestures.js";

export const path = "new";

export const title = "新建下载 - TG 视频下载器";

// ============================================================
// 常量（照搬旧壳语义，出处就近注明）
// ============================================================

// phase 中文映射：与 TUI（tg_video_dl.py SEARCH_PHASE_TEXT）同源同文（app.js:158-163）
const SEARCH_PHASE_TEXT = {
  server_search: "服务端搜索",
  local_scan: "本地翻页扫描",
  enrich: "引用内容补全",
  filter: "结果整理",
};
// 引用来源标签：与 TUI REF_SOURCE_MARKS 同文（app.js:165）
const REF_SOURCE_LABEL = { reply: "[引用]", album: "[相册]", fwd: "[转发]" };

// ============================================================
// S1 collectParams — 表单收集 + 频道空校验（app.js:728-739 / 747）
// 入参 f：{channel, keywords, mode, scope, sort, limit, minMb, maxMb,
//          dateFrom, dateTo}（DOM 原始字符串值）
// 出参：{ok:true, params:{channel,query,mode,scope,sort,limit,min_mb,
//        max_mb,date_from,date_to}} 或 {ok:false,error:"请输入频道"}
// 默认值口径照旧壳 `value || x`：空串 limit→30、min/max→0、date→""
// ============================================================
export function collectParams(f) {
  const src = f || {};
  const channel = String(src.channel == null ? "" : src.channel).trim();
  if (!channel) return { ok: false, error: "请输入频道" };
  return {
    ok: true,
    params: {
      channel: channel,
      query: String(src.keywords == null ? "" : src.keywords).trim(),
      mode: src.mode,
      scope: src.scope,
      sort: src.sort,
      limit: src.limit || 30,
      min_mb: src.minMb || 0,
      max_mb: src.maxMb || 0,
      date_from: src.dateFrom || "",
      date_to: src.dateTo || "",
    },
  };
}

// ============================================================
// S2 classifyPreviewAck — preview 返回三分支（app.js:752-758 判序：
// 先 need_login 后 !ok；错误兜底文案 "搜索失败"）
// 出参：{kind:"ok",jobId} | {kind:"needLogin"} | {kind:"error",error}
// ============================================================
export function classifyPreviewAck(r) {
  if (r && r.need_login) return { kind: "needLogin" };
  if (!r || !r.ok) return { kind: "error", error: (r && r.error) || "搜索失败" };
  if (typeof r.job_id === "string" && r.job_id) return { kind: "ok", jobId: r.job_id };
  return { kind: "error", error: "搜索失败" };
}

// ============================================================
// S3 mergeItems — 增量帧去重合并（app.js:933-951）
// 纯不可变：入参 {items:[],arrivedKeys:[]} 与帧条目数组均零改；
// 出参 {state:{items,arrivedKeys},fresh:[]} 全新结构。
// key 口径（app.js:29）：`${src || "频道"}|${mid}`（SRC_CHANNEL="频道"
// tg_video_dl.py:623）
// ============================================================
function keyOf(src, mid) {
  return (src || "频道") + "|" + mid;
}

export function mergeItems(state, frameItems) {
  const s = state || {};
  const items = Array.isArray(s.items) ? s.items.slice() : [];
  const keys = new Set(Array.isArray(s.arrivedKeys) ? s.arrivedKeys : []);
  const fresh = [];
  const its = Array.isArray(frameItems) ? frameItems : [];
  for (let i = 0; i < its.length; i++) {
    const it = its[i] || {};
    const k = keyOf(it.src, it.mid);
    if (keys.has(k)) continue;
    keys.add(k);
    items.push(it);
    fresh.push(it);
  }
  return { state: { items: items, arrivedKeys: Array.from(keys) }, fresh: fresh };
}

// ============================================================
// S3 reconcileFinal — 终帧权威收敛（app.js:958-969）
// final:true 帧按 data.items 顺序/集合重建；meta 透传，meta.title
// 存在时作为频道标题回填（previewData.title）。
// ============================================================
export function reconcileFinal(data) {
  const d = data || {};
  const items = Array.isArray(d.items) ? d.items : [];
  const meta = d.meta && typeof d.meta === "object" ? d.meta : {};
  return {
    items: items,
    arrivedKeys: items.map(function (it) { return keyOf(it && it.src, it && it.mid); }),
    meta: meta,
    title: meta.title || null,
  };
}

// ============================================================
// S4 emptyReason — 三类空态文案决策（app.js:773-784 previewEmptyHint 逐字）
// ============================================================
export function emptyReason(meta) {
  const m = meta || {};
  if (!m.candidates)
    return "没有扫描到任何消息。请确认频道用户名或链接是否正确，以及当前账号是否有权限访问该频道。";
  if (!m.n_video)
    return "扫描到 " + m.candidates + " 条消息，但其中没有视频文件。";
  const why = [];
  if (m.n_small) why.push(m.n_small + " 个小于体积下限");
  if (m.n_large) why.push(m.n_large + " 个大于体积上限");
  return "共扫描到 " + m.n_video + " 个视频，但都不符合当前筛选条件" +
    (why.length ? "（" + why.join("，") + "）" : "") +
    "。可返回上一步放宽体积、日期或关键词条件。";
}

// ============================================================
// S5 selectionPatch / selectAllPatch — 选择集合纯变更（app.js:827-850）
// toggle：有则删无则加（app.js:828）；selectAll(true)：恰"当前搜索词
// 过滤后可见集"全部 keys（app.js:843-847）；selectAll(false)：清空。
// ============================================================
export function selectionPatch(selected, key) {
  const list = Array.isArray(selected) ? selected.slice() : [];
  const k = String(key);
  const i = list.indexOf(k);
  if (i >= 0) list.splice(i, 1);
  else list.push(k);
  return list;
}

export function selectAllPatch(items, q, v) {
  if (!v) return [];
  return filterRows(items, q).map(function (it) { return keyOf(it.src, it.mid); });
}

// ============================================================
// S6 classifySearchJob — job_update 六分类（app.js:999-1052）
// 出参：{kind:"running",text} | {kind:"cancelled"} |
//       {kind:"error",error} | {kind:"done"} | {kind:"ignore"}
// running/queued 文案："正在搜索…" + SEARCH_PHASE_TEXT[phase]（未知
// phase 原样回显，app.js:1020 兜底链）+ phase_detail 拼接 " · "
// （app.js:1021-1022）；error 兜底 "搜索失败"（app.js:1043）。
// ============================================================
export function classifySearchJob(job, currentJobId) {
  if (!job || typeof job.job_id !== "string") return { kind: "ignore" };
  if (currentJobId == null || job.job_id !== currentJobId) return { kind: "ignore" };
  const st = job.state;
  if (st === "running" || st === "queued") {
    const zh = SEARCH_PHASE_TEXT[job.phase] || job.phase || "";
    return {
      kind: "running",
      text: "正在搜索…" + zh + (job.phase_detail ? " · " + job.phase_detail : ""),
    };
  }
  if (st === "cancelled") return { kind: "cancelled" };
  if (st === "error") return { kind: "error", error: job.error || "搜索失败" };
  if (st === "done") return { kind: "done" };
  return { kind: "ignore" };
}

// ============================================================
// S7 filterRows — 结果内关键词过滤（app.js:812-813/844-846）
// name/text 子串、小写比对；q trim（app.js:788/844）+ toLowerCase。
// ============================================================
export function filterRows(items, q) {
  const list = Array.isArray(items) ? items : [];
  const s = String(q == null ? "" : q).trim().toLowerCase();
  if (!s) return list.slice();
  return list.filter(function (it) {
    const a = it || {};
    return String(a.name || "").toLowerCase().indexOf(s) >= 0 ||
      String(a.text || "").toLowerCase().indexOf(s) >= 0;
  });
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

// 导航：优先新壳 router 探针，降级 hash（任务书 §1.4）
function navigate(path, opts) {
  if (typeof window !== "undefined" && window.__t8router &&
      typeof window.__t8router.navigate === "function") {
    window.__t8router.navigate(path, opts);
  } else if (typeof location !== "undefined") {
    location.hash = "#" + path;
  }
}

// ============================================================
// 视图状态（模块级闭包；keep-alive 下跨进入保留，与旧壳 DOM 常驻同义）
// ============================================================
let cached = null;
let refs = null;
let opts = { mode: "all", scope: "channel", sort: "date" }; // app.js:140
let previewData = { items: [], channel: "", title: "", meta: {}, job_id: "" };
let selected = new Set();
let renameEdits = {};        // 2.3 智能重命名：行内编辑的拟用新名（key → 名；空=保持原名）
let renameOff = new Set();   // 2.3：显式取消勾选的 key（使用并展示原文件名）
let expandedKeys = new Set(); // 2.3：高级详情展开的 key
let arrivedKeys = new Set();
let searchJob = null;        // {job_id, streaming}（app.js:154）
let cancelBusy = false;      // app.js:156 在途防重
let toaster = null;
let listBound = false;       // window tg:* 监听挂载标志
let pyReadyHandler = null;   // pywebviewready 待清理引用
let apiReadyP = null;
const rowByKey = new Map();  // 全量重绘重建；行内更新专用索引（免 CSS.escape）
// T52 任务选号（app.js:3120/3574-3631 轻承接）：本页选择仅记录，
// 创建成功后随 edit_task({acct_pref}) fire-and-forget 下发
let newTaskAcct = "auto";   // app.js:3120 默认自动
let acctAlts = [];          // 小号清单（app.js:3575 altAccounts；空=只显 主号|自动）
let acctSeg = null;

// ============================================================
// mount / onEnter / onLeave（T8 契约 §1）
// ============================================================
export function mount() {
  if (cached) return cached;
  try {
    cached = buildDom();
  } catch (e) {
    // mount 自捕获（任务书 §1.2）：失败返回降级元素，rejected Promise
    // 不得进 router mounts 缓存——本路径同步返回元素，且视图语义已"可用"。
    console.error("[T12] new 视图构建失败：", e);
    cached = el("div", { class: "vw-new" }, [
      el("div", { class: "u-empty", role: "status" }, [
        el("div", { class: "u-empty-ico", "aria-hidden": "true", text: "⚠" }),
        el("h3", { class: "u-empty-title", text: "新建下载暂不可用" }),
        el("p", { class: "u-empty-hint", text: "视图初始化失败，可刷新页面重试。" }),
      ]),
    ]);
  }
  return cached;
}

export function onEnter() {
  if (cached && refs) {
    bindList();
    attachGestures();   // T17 手势增量（幂等）
    loadAcctAlts();     // T52 小号清单补拉（幂等；失败静默=只显 主号|自动）
  }
}

// T17 手势增量：边缘返回 + 聚焦保持（虚拟键盘遮挡补偿；本视图无列表不挂托盘）
let gsDetach = [];
function attachGestures() {
  if (gsDetach.length) return;
  gsDetach = [attachEdgeBack(), attachFocusKeep(refs.root)];
}

export function onLeave() {
  // ①静默取消在途搜索作业（app.js:1071-1076 尽力调用，防后台空跑）
  cancelSearchJobQuietly();
  // ②移除 window 事件监听（tg:search_items / tg:job_update / pywebviewready）
  unbindList();
  // ②b T17 手势增量对称回收（attach* 均返回 detach）
  for (let i = 0; i < gsDetach.length; i++) gsDetach[i]();
  gsDetach = [];
  // ③toaster 生命周期：view 常驻不随 onLeave 销毁（TR12-1 G5 揪出的产品
  //   缺陷：need_login 分支 toast 提示后立即 navigate('/login')，若离开
  //   视图即销毁 host，提示被闪杀、用户永远看不到）。卡自动消失后 host
  //   留空无渲染无视觉副作用；自动消失 timer 到点自清无泄漏。
}

// 视图私有 toaster：惰性创建、跨进入常驻（见 onLeave ③）

function bindList() {
  if (listBound) return;
  listBound = true;
  // 監聽首参是 Event 对象：解包 detail 再进视图逻辑
  // （旧壳 app.js 事件总线同款箭头解包：onSearchItems(ev.detail)；
  //   首跑红态：直接绑函数收到 Event 对象致 job_id 恒 undefined、帧全丢弃）
  window.addEventListener("tg:search_items", onItemsEvent);
  window.addEventListener("tg:job_update", onJobEvent);
}

function unbindList() {
  if (!listBound) return;
  listBound = false;
  window.removeEventListener("tg:search_items", onItemsEvent);
  window.removeEventListener("tg:job_update", onJobEvent);
  if (pyReadyHandler) {
    window.removeEventListener("pywebviewready", pyReadyHandler);
    pyReadyHandler = null;
  }
  apiReadyP = null;
}

function onItemsEvent(ev) { onSearchItems(ev && ev.detail); }
function onJobEvent(ev) { onSearchJobUpdate(ev && ev.detail); }

// ============================================================
// DOM 构建（仅在 mount 一次执行；顶层与 onEnter 无 DOM 依赖）
// ============================================================
function buildDom() {
  const root = el("div", { class: "vw-new" });

  // —— 页头（index.html:205-207）——
  root.append(el("header", { class: "nw-head" }, [
    el("h2", { text: "新建下载" }),
    el("p", { class: "nw-sub", text: "基础设置 + 搜索引擎级高级筛选" }),
  ]));

  // —— 步骤条（index.html:209-213，样式对照 style.css:512-522）——
  const step1 = el("div", { class: "nw-step is-active" }, [
    el("i", { text: "1", "aria-hidden": "true" }),
    el("span", { text: "搜索" }),
  ]);
  const step2 = el("div", { class: "nw-step" }, [
    el("i", { text: "2", "aria-hidden": "true" }),
    el("span", { text: "选择视频" }),
  ]);
  root.append(el("div", { class: "nw-steps", role: "group", "aria-label": "向导步骤" }, [
    step1, el("span", { class: "nw-step-line", "aria-hidden": "true" }), step2,
  ]));
  // 步骤条态切换（app.js:722-725：active=当前、done=已过）

  // ============ 步骤 1：条件页（wizard-1，index.html:215-302） ============
  const wiz1 = el("div", { class: "nw-wiz" });

  const channel = el("input", {
    id: "nw-channel", type: "text", class: "nw-input",
    placeholder: "@durov 或 https://t.me/durov",
    autocomplete: "off",
  });
  const keywords = el("input", {
    id: "nw-keywords", type: "text", class: "nw-input",
    placeholder: "如 4K,预告片,-广告", autocomplete: "off",
  });
  const minMb = el("input", { id: "nw-minmb", type: "number", min: "0", placeholder: "最小", class: "nw-input" });
  const maxMb = el("input", { id: "nw-maxmb", type: "number", min: "0", placeholder: "最大", class: "nw-input" });
  const dateFrom = el("input", { id: "nw-datefrom", type: "date", class: "nw-input" });
  const dateTo = el("input", { id: "nw-dateto", type: "date", class: "nw-input" });
  const limit = el("input", { id: "nw-limit", type: "number", value: "30", min: "1", class: "nw-input" });
  const parallel = el("input", { id: "nw-parallel", type: "number", value: "2", min: "1", max: "8", class: "nw-input" });
  // 2.3 FIX5K P1：深度扫描开关（突破 5000 条本地扫描上限；旧壳 w-deepscan 同义）
  const deepScan = el("input", {
    id: "nw-deepscan", type: "checkbox",
    "aria-label": "深度扫描（可突破 5000 条）",
  });

  // 三个 segmented（选项/默认值严格对齐 index.html:229-244/269-273）
  const modeSeg = createSegmented([
    { value: "all", label: "全部词", title: "同时包含所有词" },
    { value: "phrase", label: "精确短语", title: "完整短语连续出现" },
    { value: "any", label: "任一词", title: "包含任一词" },
    { value: "fuzzy", label: "模糊", title: "容错/近似匹配" },
    { value: "regex", label: "正则", title: "正则表达式" },
  ], { value: "all", ariaLabel: "匹配模式", onChange: function (v) { opts.mode = v; } });
  const scopeSeg = createSegmented([
    { value: "channel", label: "频道消息" },
    { value: "comments", label: "评论区" },
    { value: "both", label: "频道+评论" },
  ], { value: "channel", ariaLabel: "搜索范围", onChange: function (v) { opts.scope = v; } });
  const sortSeg = createSegmented([
    { value: "date", label: "最新优先" },
    { value: "size", label: "最大优先" },
  ], { value: "date", ariaLabel: "排序方式", onChange: function (v) { opts.sort = v; } });

  // 高级折叠区（index.html:225-275；默认收起=旧壳 adv-panel.hidden）
  // 折叠区内 segmented/双输入的 label 与控件无一一关联（组容器），
  // 无 for 属性；a11y 由 u-seg 自带 role=tablist/aria-label 承载
  const advBtn = el("button", {
    class: "u-btn nw-adv-toggle", type: "button",
    "aria-expanded": "false", "aria-controls": "nw-adv",
    onclick: toggleAdv,
  }, [
    el("span", { class: "nw-caret", "aria-hidden": "true", text: "⌄" }),
    el("span", { text: "高级筛选" }),
    el("span", { class: "nw-hint", text: "匹配模式 · 评论区 · 日期体积区间 · 排序" }),
  ]);
  // 行序逐行对照 2.1（index.html:226-274）：adv-row[匹配模式|搜索范围] →
  // 关键词全宽 → adv-row[体积|日期|排序]（.adv-row/.adv-field 槽宽
  // style.css:551-553）；parallel 移出并入 form-grid（index.html:277-286）
  const advPanel = el("div", { id: "nw-adv", class: "nw-adv", hidden: true }, [
    el("div", { class: "nw-advrow" }, [
      el("div", { class: "nw-field" }, [
        el("span", { class: "nw-label", text: "匹配模式" }),
        modeSeg.el,
      ]),
      el("div", { class: "nw-field" }, [
        el("span", { class: "nw-label", text: "搜索范围" }),
        scopeSeg.el,
      ]),
    ]),
    el("div", { class: "nw-field" }, [
      el("label", { for: "nw-keywords", text: "关键词（逗号分隔，词首 - 排除；模糊/正则模式下直接写完整表达式）" }),
      keywords,
    ]),
    el("div", { class: "nw-advrow" }, [
      el("div", { class: "nw-field" }, [
        el("span", { class: "nw-label", text: "体积区间 (MB，可只填一个)" }),
        el("div", { class: "nw-dual" }, [minMb, el("span", { "aria-hidden": "true", text: "—" }), maxMb]),
      ]),
      el("div", { class: "nw-field" }, [
        el("span", { class: "nw-label", text: "发布日期区间" }),
        el("div", { class: "nw-dual" }, [dateFrom, el("span", { "aria-hidden": "true", text: "—" }), dateTo]),
      ]),
      el("div", { class: "nw-field nw-field--small" }, [
        el("span", { class: "nw-label", text: "排序方式" }),
        sortSeg.el,
      ]),
      el("div", { class: "nw-field nw-field--small" }, [
        el("span", { class: "nw-label", text: "深度扫描（可突破 5000 条）" }),
        deepScan,
      ]),
    ]),
  ]);

  const errBox = el("p", { class: "nw-err", role: "alert" });
  const noteBox = el("p", { class: "nw-note" });
  const searchBtn = el("button", {
    class: "u-btn u-btn--primary nw-search-btn", type: "button",
    text: "开始搜索", onclick: function () { doPreview(); },
  });

  const card = createCard({ ariaLabel: "新建下载条件" });
  // T52 任务选号行（index.html:288-294 / style.css:1397-1405 task-acct 系）
  const acctSegHost = el("div", { class: "nw-acctseg" });
  const taskAcctRow = el("div", { class: "nw-taskacct" }, [
    el("span", { class: "nw-taskacct-label", text: "本任务后续下载使用" }),
    acctSegHost,
    el("span", { class: "nw-taskacct-note", text: "进行中的条目不受影响" }),
  ]);
  card.body.append(
    el("div", { class: "nw-field" }, [
      el("label", { for: "nw-channel", text: "频道用户名或链接" }),
      channel,
    ]),
    advBtn,
    advPanel,
    // form-grid 二列（index.html:277-286 / style.css:525-526）：limit | parallel
    el("div", { class: "nw-grid2" }, [
      el("div", { class: "nw-field" }, [
        el("label", { for: "nw-limit", text: "结果数量上限" }),
        limit,
      ]),
      el("div", { class: "nw-field" }, [
        el("label", { for: "nw-parallel", text: "同时下载几路（1-8）" }),
        parallel,
      ]),
    ]),
    taskAcctRow,
    el("div", { class: "nw-actions" }, [searchBtn]),
    errBox,
    noteBox
  );
  wiz1.append(card.el);

  // ============ 步骤 2：结果页（wizard-2，index.html:304-332） ============
  const wiz2 = el("div", { class: "nw-wiz", hidden: true });

  const prog = createProgress({ determinate: false });
  const cancelBtn = el("button", {
    class: "u-btn u-btn--ghost nw-cancel-btn", type: "button",
    text: "停止搜索", onclick: function () { cancelSearch(); },
  });
  const phaseBox = el("div", { class: "nw-phase", "aria-live": "polite" });
  const countsBox = el("div", { class: "nw-counts" });
  const pnoteBox = el("div", { class: "nw-pnote" });
  const progWrap = el("div", { class: "nw-prog", hidden: true, "aria-label": "搜索进度" }, [
    el("div", { class: "nw-prog-head" }, [prog.el, cancelBtn]),
    phaseBox, countsBox, pnoteBox,
  ]);

  const vsearch = el("input", {
    id: "nw-search", type: "text", class: "nw-input nw-search-input",
    placeholder: "在结果中筛选名称/消息原文…", autocomplete: "off",
    oninput: function () { renderPreview(); },
  });
  const selAllBtn = el("button", {
    class: "u-btn u-btn--ghost", type: "button", text: "全选",
    onclick: function () { selectAll(true); },
  });
  const clearBtn = el("button", {
    class: "u-btn u-btn--ghost", type: "button", text: "清空",
    onclick: function () { selectAll(false); },
  });
  // 2.3：命名一键全选/取消全选（作用于当前筛选可见集）
  const rnAllBtn = el("button", {
    class: "u-btn u-btn--ghost", type: "button", text: "命名全选",
    title: "所有行启用重命名（使用拟用新名）",
    onclick: function () { renameAll(true); },
  });
  const rnNoneBtn = el("button", {
    class: "u-btn u-btn--ghost", type: "button", text: "命名全不选",
    title: "所有行取消重命名（使用并展示原文件名）",
    onclick: function () { renameAll(false); },
  });
  const list = el("div", { class: "nw-list", "aria-label": "搜索结果" });

  const backBtn = el("button", {
    class: "u-btn u-btn--ghost", type: "button", text: "上一步",
    onclick: function () { backToStep1(); },
  });
  const selCount = el("span", { text: "0" });
  const startBtn = el("button", {
    class: "u-btn u-btn--primary", type: "button",
    onclick: function () { doStart(); },
  }, ["开始下载 (", selCount, ")"]);

  wiz2.append(
    progWrap,
    el("div", { class: "nw-toolbar" }, [
      el("div", { class: "nw-searchbox" }, [
        el("span", { class: "nw-search-ico", "aria-hidden": "true", text: "⌕" }),
        vsearch,
      ]),
      el("div", { class: "nw-selact" }, [selAllBtn, clearBtn, rnAllBtn, rnNoneBtn]),
      el("label", { class: "nw-sr-only", for: "nw-search", text: "在结果中筛选" }),
    ]),
    list,
    el("div", { class: "nw-actions nw-actions--between" }, [
      backBtn,
      el("div", { class: "nw-start-box" }, [startBtn]),
    ])
  );

  root.append(wiz1, wiz2);

  refs = {
    root: root, step1: step1, step2: step2, wiz1: wiz1, wiz2: wiz2,
    channel: channel, keywords: keywords, limit: limit, parallel: parallel,
    minMb: minMb, maxMb: maxMb, dateFrom: dateFrom, dateTo: dateTo,
    deepScan: deepScan,
    advBtn: advBtn, advPanel: advPanel,
    searchBtn: searchBtn, errBox: errBox, noteBox: noteBox,
    modeSeg: modeSeg, scopeSeg: scopeSeg, sortSeg: sortSeg,
    progWrap: progWrap, prog: prog, cancelBtn: cancelBtn,
    phaseBox: phaseBox, countsBox: countsBox, pnoteBox: pnoteBox,
    vsearch: vsearch, selAllBtn: selAllBtn, clearBtn: clearBtn,
    list: list, backBtn: backBtn, startBtn: startBtn, selCount: selCount,
    acctSegHost: acctSegHost,
  };
  renderAcctSeg();   // T52 选号 seg 首渲（小号清单随后台异步补拉重建）
  return root;
}

// ============================================================
// 分步与折叠（app.js:698-726）
// ============================================================
function gotoStep(n) {
  if (!refs) return;
  refs.wiz1.hidden = n !== 1;
  refs.wiz2.hidden = n !== 2;
  refs.step1.classList.toggle("is-active", n === 1);
  refs.step1.classList.toggle("is-done", n > 1);   // app.js:724 i < n-1
  refs.step2.classList.toggle("is-active", n === 2);
  refs.step2.classList.toggle("is-done", n > 2);
}

function toggleAdv() {
  if (!refs) return;
  const open = refs.advPanel.hidden === true;
  refs.advPanel.hidden = !open;
  refs.advBtn.classList.toggle("open", open);
  refs.advBtn.setAttribute("aria-expanded", open ? "true" : "false");
}

// ============================================================
// RPC 桥（唯一通道 window.pywebview.api；pywebviewready 等就绪）
// ============================================================
// 不变量（W4 评审 hard 1 修复）：对桥对象只做同步方法直调——任何
// Promise 的 resolve 值 / return 值 / await 对象都不得是桥代理对象本身。
// 根因：bridge.js:69-74 的 Proxy 对任意属性访问（含 then）都返回把属性名
// 当方法名的 RPC 函数，桥对象因此是 thenable；一旦被 Promise 同化
// （Promise.resolve(api)/resolve(api)/await api），机制会调用
// api.then(res,rej)——实际发出名为 "then" 的 RPC，res/rej 永不回调，
// await 永久 pending（修复前 doPreview 等 4 个调用点全部挂死）。
// 守法：就绪 Promise 只 resolve 布尔（同 login.js bridgeReady 范式），
// 调用方拿布尔后同步直读 window.pywebview.api.xxx(...) 直调（同旧壳
// app.js:5 const api = () => window.pywebview.api 的同步消费语义）。
function bridgeReady() {
  return !!(typeof window !== "undefined" &&
    window.pywebview && window.pywebview.api);
}

function ensureApi() {
  if (bridgeReady()) {
    return Promise.resolve(true);   // 布尔；绝不 resolve 桥对象（见上不变量）
  }
  if (!apiReadyP) {
    apiReadyP = new Promise(function (resolve) {
      pyReadyHandler = function () {
        pyReadyHandler = null;
        resolve(bridgeReady());     // 布尔；绝不 resolve 桥对象（见上不变量）
      };
      window.addEventListener("pywebviewready", pyReadyHandler);
    }).then(function (ready) {
      apiReadyP = null; // 消费后复位（onLeave 同步置 null）
      return ready;     // 布尔透传
    });
  }
  return apiReadyP;
}

function toast(type, text) {
  if (!toaster) toaster = createToaster({ max: 5 });
  toaster.show({ type: type, text: text });
}

// ============================================================
// 开始搜索（app.js:741-771 doPreview）
// ============================================================
async function doPreview() {
  if (!refs) return;
  refs.errBox.textContent = "";              // app.js:744
  refs.noteBox.textContent = "";
  const r0 = collectParams({
    channel: refs.channel.value,
    keywords: refs.keywords.value,
    mode: opts.mode, scope: opts.scope, sort: opts.sort,
    limit: refs.limit.value, minMb: refs.minMb.value, maxMb: refs.maxMb.value,
    dateFrom: refs.dateFrom.value, dateTo: refs.dateTo.value,
  });
  if (!r0.ok) { refs.errBox.textContent = r0.error; return; } // app.js:747
  // 2.3 FIX5K P1：deep_scan 扩展键——S1 契约 params 恰 10 键冻结面不动
  // （run_seams S1-8），收集后按 T52 扩展键模式并入下发参数
  r0.params.deep_scan = refs.deepScan && refs.deepScan.checked ? 1 : 0;
  await cancelSearchJobQuietly();            // app.js:749 T48 旧作业先取消
  refs.noteBox.textContent =
    "正在扫描频道历史消息，频道消息较多时可能需要等待几十秒…"; // app.js:750-751
  const btn = refs.searchBtn;
  const oldText = btn.textContent;
  btn.disabled = true;
  btn.textContent = "搜索中…";
  let r = null;
  try {
    const ready = await ensureApi();            // 布尔就绪，非桥对象（见 ensureApi 不变量）
    if (!ready) throw new Error("bridge not ready");
    r = await window.pywebview.api.preview(r0.params); // 同步直调（旧壳 app.js:5）
  } catch (e) {
    btn.disabled = false; btn.textContent = oldText;
    refs.noteBox.textContent = "";
    return;                                  // app.js:755 网络异常静默返回
  }
  btn.disabled = false;
  btn.textContent = oldText;
  refs.noteBox.textContent = "";
  const c = classifyPreviewAck(r);
  if (c.kind === "needLogin") {
    // T46：need_login:true（app.js:757）→ 登录弹层 + 卡内受控提示
    //（2.1 showLoginCard(notice) 口径：提示随卡呈现，不另发 toast）
    navigate("/login", { force: true, notice: "该功能需要登录，登录后将自动继续刚才的操作" });
    return;
  }
  if (c.kind === "error") {
    refs.errBox.textContent = c.error;       // app.js:758 内联显错
    return;
  }
  // ACK {ok,job_id}：立即进步骤 2，初始化流式等待态（app.js:759-770）
  previewData = {
    items: [], channel: r0.params.channel, title: r0.params.channel,
    meta: {}, job_id: c.jobId, params: r0.params,
  };
  selected = new Set();
  arrivedKeys = new Set();
  searchJob = { job_id: c.jobId, streaming: true };
  refs.vsearch.value = "";
  resetSearchProgress();
  gotoStep(2);
  renderPreview();
}

// ============================================================
// 结果渲染（app.js:786-826 renderPreview + 897-926 行构造/追加）
// ============================================================
function clearList() {
  rowByKey.clear();
  while (refs.list.firstChild) refs.list.removeChild(refs.list.firstChild);
}

// 2.3 智能重命名：行内候选名（编辑优先，空=保持原名）
function rnEffective(k, it) {
  return (((k in renameEdits) ? renameEdits[k] : (it && it.rename) || "")).trim();
}

function buildRow(it) {
  const k = keyOf(it.src, it.mid);
  const on = selected.has(k);
  const eff = rnEffective(k, it);
  const useRn = !renameOff.has(k) && eff !== "";
  const nameWrap = el("div", { class: "nw-name" });
  if (it.match_in && it.match_in !== "self") {
    nameWrap.append(el("span", {
      class: "nw-reftag",
      text: REF_SOURCE_LABEL[it.match_in] || it.match_in, // app.js:900-901
    }));
  }
  // 名称区展示「将要使用的名字」：勾选=拟用新名（随编辑实时联动），
  // 取消勾选=原文件名（2.3 口径）
  const nameText = document.createTextNode(useRn ? eff : String(it.name || ""));
  nameWrap.append(nameText);
  // 是否重命名勾选（2.3）：取消勾选=使用并展示原文件名
  const rnCheck = el("input", {
    class: "nw-rn-check", type: "checkbox", checked: useRn,
    title: "是否用新名命名（取消勾选=使用并展示原文件名）",
    "aria-label": "是否重命名 " + String(it.name || ""),
    onclick: function (ev) { ev.stopPropagation(); },
    onchange: function () {
      if (rnCheck.checked) renameOff.delete(k); else renameOff.add(k);
      rnInput.disabled = !rnCheck.checked;
      nameText.nodeValue = rnCheck.checked
        ? (rnInput.value.trim() || String(it.name || ""))
        : String(it.name || "");
    },
  });
  const rnInput = el("input", {
    class: "nw-rn-input", type: "text", value: eff,
    placeholder: "留空=保持原名", disabled: !useRn,
    title: "拟用新名（可编辑；任务创建后下载完成即按此命名）",
    onclick: function (ev) { ev.stopPropagation(); },
    onkeydown: function (ev) { ev.stopPropagation(); }, // 防空格/回车触发行勾选
    oninput: function () {
      onRenameInput(k, rnInput.value);
      if (rnInput.value.trim() && !rnCheck.checked) {
        rnCheck.checked = true;
        renameOff.delete(k);
        rnInput.disabled = false;
      }
      nameText.nodeValue = rnCheck.checked
        ? (rnInput.value.trim() || String(it.name || ""))
        : String(it.name || "");
    },
  });
  // 高级详情展开钮（2.3：完整文件名/正文/引用）
  const exp = expandedKeys.has(k);
  const detBtn = el("button", {
    class: "nw-det-btn", type: "button",
    "aria-expanded": exp ? "true" : "false",
    title: "展开高级详情（完整文件名/正文/引用）",
    text: exp ? "收起" : "详情",
    onclick: function (ev) {
      ev.stopPropagation();
      if (expandedKeys.has(k)) expandedKeys.delete(k); else expandedKeys.add(k);
      const row0 = rowByKey.get(k);
      const fresh = buildRow(it);
      if (row0 && row0.parentNode) row0.parentNode.replaceChild(fresh, row0);
      rowByKey.set(k, fresh);
    },
  });
  const rnWrap = el("div", { class: "nw-rename" }, [
    rnCheck,
    el("span", { class: "nw-rename-label", text: "拟命名" }),
    rnInput,
    detBtn,
  ]);
  const sub = el("div", { class: "nw-sub" });
  if (it.ref_text) sub.title = String(it.ref_text);       // title 全文悬浮 app.js:903
  sub.append(document.createTextNode(
    (it.date ? it.date + " · " : "") + String(it.text || "").slice(0, 70))); // app.js:911
  if (it.ref_text) {
    sub.append(el("span", {
      class: "nw-refinline",                  // 行内截断 app.js:904-905
      text: " " + String(it.ref_text).slice(0, 60),
    }));
  }
  // 高级详情块（2.3）：展开展示完整信息
  const detWrap = exp ? el("div", { class: "nw-det" }, [
    el("div", {
      class: "nw-det-line",
      text: "原文件名：" + String(it.name || ""),
    }),
    el("div", {
      class: "nw-det-line nw-det-meta",
      text: [it.date || "", human(it.size), String(it.src || ""),
        "id " + it.mid].filter(Boolean).join(" · "),
    }),
    it.text ? el("div", {
      class: "nw-det-line nw-det-text", text: "消息正文：" + String(it.text),
    }) : null,
    it.ref_text ? el("div", {
      class: "nw-det-line nw-det-text", text: "引用文本：" + String(it.ref_text),
    }) : null,
  ]) : null;
  const row = el("div", {
    class: "nw-row" + (on ? " is-sel" : ""),
    dataset: { key: k },
    role: "checkbox",
    "aria-checked": on ? "true" : "false",
    "aria-label": String(it.name || ""),
    tabindex: "0",
    onclick: function () { toggleVideo(k); },
    onkeydown: function (e) {
      if (e.key === " " || e.key === "Enter") { e.preventDefault(); toggleVideo(k); }
    },
  }, [
    el("div", { class: "nw-cbox", "aria-hidden": "true", text: on ? "✓" : "" }),
    el("div", { class: "nw-info" }, [nameWrap, rnWrap, sub, detWrap]),
    el("div", { class: "nw-side" }, [
      el("span", { class: "nw-size", text: human(it.size) }),
      el("span", { class: "nw-srctag", text: String(it.src || "") }),
    ]),
  ]);
  return row;
}

function appendRow(it) {
  const ph = refs.list.querySelector(".u-empty");
  if (ph) ph.remove();                        // 首批到达清等待/空态占位 app.js:922-924
  const row = buildRow(it);
  refs.list.append(row);
  rowByKey.set(keyOf(it.src, it.mid), row);
}

// 2.3 智能重命名：行内拟命名编辑登记（仅记录，随 doStart 统一下发）
function onRenameInput(k, v) {
  renameEdits[k] = v;
}

function setSelCount(n) {
  refs.selCount.textContent = String(n);      // app.js:837
}

function renderEmpty(state) {
  refs.list.append(state);
}

function renderPreview() {
  if (!refs) return;
  const q = refs.vsearch.value.trim().toLowerCase(); // app.js:788
  const streamingEmpty = !previewData.items.length && searchJob && searchJob.streaming;
  if (streamingEmpty) {
    // T48 流式等待空态（app.js:790-799）：⌛ 等待，绝不能显示"没有视频"
    clearList();
    renderEmpty(el("div", { class: "u-empty", role: "status" }, [
      el("div", { class: "u-empty-ico", "aria-hidden": "true", text: "⌛" }),
      el("h3", { class: "u-empty-title", text: "正在搜索频道历史消息…" }),
      el("p", { class: "u-empty-hint", text: "结果会随着扫描推进持续追加，已到达的条目可立即勾选。" }),
    ]));
    setSelCount(0);
    return;
  }
  if (!previewData.items.length) {
    // 搜索完成零结果：三类具体原因（app.js:800-811）
    clearList();
    const st = el("div", { class: "u-empty", role: "status" }, [
      el("div", { class: "u-empty-ico", "aria-hidden": "true", text: "∅" }),
      el("h3", { class: "u-empty-title", text: "没有可选择的视频" }),
      el("p", { class: "u-empty-hint", text: emptyReason(previewData.meta || {}) }),
      el("button", {
        class: "u-btn u-btn--primary", type: "button",
        text: "返回修改条件",
        onclick: function () { gotoStep(1); },
      }),
    ]);
    renderEmpty(st);
    setSelCount(0);
    return;
  }
  const rows = filterRows(previewData.items, q);
  if (!rows.length) {
    // 有关键词但筛不出（app.js:814-823）
    clearList();
    const st = el("div", { class: "u-empty", role: "status" }, [
      el("div", { class: "u-empty-ico", "aria-hidden": "true", text: "⌕" }),
      el("h3", { class: "u-empty-title", text: "没有匹配“" + q + "”的视频" }),
      el("button", {
        class: "u-btn u-btn--ghost", type: "button",
        text: "清除搜索词",
        onclick: function () { refs.vsearch.value = ""; renderPreview(); }, // app.js:839-842
      }),
    ]);
    renderEmpty(st);
    setSelCount(selected.size);
    return;
  }
  clearList();
  for (let i = 0; i < rows.length; i++) appendRow(rows[i]);
  setSelCount(selected.size);
}

// ============================================================
// 勾选（app.js:827-838 行内更新 / 843-850 selectAll）
// ============================================================
function toggleVideo(k) {
  if (selected.has(k)) selected.delete(k); else selected.add(k); // app.js:828
  const on = selected.has(k);
  // T48 流式追加期间行内更新，不全量重绘（app.js:829-836）
  const row = rowByKey.get(k);
  if (row) {
    row.classList.toggle("is-sel", on);
    const cb = row.querySelector(".nw-cbox");
    if (cb) cb.textContent = on ? "✓" : "";
    row.setAttribute("aria-checked", on ? "true" : "false");
  }
  setSelCount(selected.size);
}

function selectAll(v) {
  const q = refs.vsearch.value.trim().toLowerCase();
  selected = new Set(selectAllPatch(previewData.items, q, v)); // 可见集语义 app.js:843-849
  renderPreview();
}

// 2.3：命名一键全选/取消全选（可见集语义；取消=使用并展示原文件名）
function renameAll(v) {
  const rows = filterRows(previewData.items, refs.vsearch.value.trim().toLowerCase());
  for (let i = 0; i < rows.length; i++) {
    const k = keyOf(rows[i].src, rows[i].mid);
    if (v) renameOff.delete(k); else renameOff.add(k);
  }
  renderPreview();
}

// ============================================================
// 流式：search_items / job_update（app.js:933-1052）
// ============================================================
function onSearchItems(detail) {
  if (!refs) return;
  if (!detail || typeof detail.job_id !== "string") return;
  if (!searchJob || detail.job_id !== searchJob.job_id) return; // 非当前作业忽略 app.js:940
  const data = (detail && detail.data) || {};
  if (data.final === true) { finalizeSearch(data); return; }    // app.js:943
  const m = mergeItems(
    { items: previewData.items, arrivedKeys: Array.from(arrivedKeys) },
    data.items);
  previewData.items = m.state.items;
  arrivedKeys = new Set(m.state.arrivedKeys);
  if (m.fresh.length && searchJob.streaming) {
    for (let i = 0; i < m.fresh.length; i++) appendRow(m.fresh[i]); // 只 append 新行
    setSelCount(selected.size);                                // app.js:954
  }
}

function finalizeSearch(data) {
  const rec = reconcileFinal(data);
  previewData.items = rec.items;           // 权威收敛：顺序/集合重建 app.js:960-961
  arrivedKeys = new Set(rec.arrivedKeys);
  previewData.meta = rec.meta;
  if (rec.title) previewData.title = rec.title;
  searchJob.streaming = false;
  finishSearchProgress(rec.meta);
  renderPreview();
}

function resetSearchProgress() {           // app.js:877-889
  renameEdits = {};                        // 2.3：新一轮搜索清空行内拟命名编辑
  renameOff = new Set();
  expandedKeys = new Set();
  refs.progWrap.hidden = false;
  refs.progWrap.dataset.state = "running";
  refs.prog.setDeterminate(false);
  refs.phaseBox.textContent = "正在搜索…";
  refs.countsBox.textContent = "已扫描 0 条 · 已匹配 0 条";
  refs.pnoteBox.textContent = "";
  refs.pnoteBox.classList.remove("warn", "error");
  refs.cancelBtn.disabled = false;
  refs.cancelBtn.hidden = false;
}

function hideSearchCancelBtn() {           // app.js:891-895
  refs.cancelBtn.disabled = true;
  refs.cancelBtn.hidden = true;
}

// 2.3 FIX5K P3：场景化提示判据（文案分流由调用侧参数计算，内核 meta
// 键集合冻结不变）：use_server 口径 = terms 非空且 mode 属服务端可搜
// （与内核同式）；deepScan 口径同内核布尔解析词表
function searchFlags() {
  const p = previewData.params || {};
  const hasTerms = String(p.query || "").split(",")
    .some(function (w) { const s = w.trim(); return s && !s.startsWith("-"); });
  return {
    useServer: hasTerms && ["phrase", "all", "any"].includes(String(p.mode || "all")),
    deepScan: ["1", "true", "yes", "y", "on"].includes(
      String(p.deep_scan || "").trim().toLowerCase()),
  };
}

function finishSearchProgress(meta) {      // app.js:971-995
  refs.prog.setDeterminate(true);
  refs.prog.set(100);
  refs.progWrap.dataset.state = "done";
  const bits = [];
  if (meta.n_small) bits.push(meta.n_small + " 个小于下限");
  if (meta.n_large) bits.push(meta.n_large + " 个大于上限");
  if (meta.ref_matched) bits.push("外部文本命中 " + meta.ref_matched + " 条");
  if (meta.capped) bits.push("消息过多，仅扫描了最近 " + meta.scan_cap + " 条");
  refs.phaseBox.textContent = "搜索完成 · 共 " + previewData.items.length + " 条";
  refs.countsBox.textContent = bits.join("，");
  refs.pnoteBox.classList.remove("error");
  // truncated/capped 黄行（文案同 TUI 口径，按 use_server/命中类型分流）
  if (meta.truncated) {
    refs.pnoteBox.textContent = "外部文本补抓已达批次预算上限，更早消息的引用/相册文本可能未覆盖：可填起始日期缩小扫描窗口后重试";
    refs.pnoteBox.classList.add("warn");
  } else if (meta.capped) {
    const flags = searchFlags();
    const head = flags.deepScan
      ? "深度扫描已触 " + meta.scan_cap + " 条安全上限，更早的结果未覆盖："
      : "本地扫描已达上限，更早的结果未覆盖：";
    if (flags.useServer) {
      refs.pnoteBox.textContent = head + "已走服务端搜索，加词已不能扩大覆盖：可填起始日期扩大扫描范围";
    } else if (meta.ref_matched && meta.ref_matched >= previewData.items.length
      && previewData.items.length > 0) {
      // 纯外部文本命中：关键词在引用/相册/转发原文里，服务端搜索只索引
      // 消息自身文本，加词走服务端对这类命中无效
      refs.pnoteBox.textContent = head + "命中均为外部文本（引用/相册/转发），加词走服务端搜索对这类命中无效：可填起始日期扩大扫描范围";
    } else {
      refs.pnoteBox.textContent = head + "加关键词可走服务端搜索";
    }
    refs.pnoteBox.classList.add("warn");
  } else {
    refs.pnoteBox.textContent = "";
    refs.pnoteBox.classList.remove("warn");
  }
  hideSearchCancelBtn();
}

function onSearchJobUpdate(job) {
  if (!refs) return;
  if (!job || typeof job.job_id !== "string") return;
  // 断线重连恢复（app.js:1004-1014）：无当前作业且收到 running/queued
  // 的 search 作业 → 重建流式态（终帧 items 兜底收敛已到内容）
  if (!searchJob && job.kind === "search" &&
      (job.state === "running" || job.state === "queued")) {
    previewData = { items: [], channel: "", title: "", meta: {}, job_id: job.job_id };
    selected = new Set();
    arrivedKeys = new Set();
    searchJob = { job_id: job.job_id, streaming: true };
    refs.vsearch.value = "";
    resetSearchProgress();
    gotoStep(2);
    renderPreview();
  }
  const cls = classifySearchJob(job, searchJob ? searchJob.job_id : null);
  if (cls.kind === "ignore") return;
  if (cls.kind === "running") {
    if (!searchJob.streaming) return;      // app.js:1018 终态后到达的 running 帧
    refs.progWrap.hidden = false;
    refs.phaseBox.textContent = cls.text;
    refs.prog.setDeterminate(false);
    refs.progWrap.dataset.state = "running";
    return;
  }
  if (cls.kind === "cancelled") {
    searchJob.streaming = false;
    refs.prog.setDeterminate(true);
    refs.prog.set(100);
    refs.progWrap.dataset.state = "cancelled";
    refs.phaseBox.textContent = "已停止搜索";
    refs.countsBox.textContent = "已到达 " + previewData.items.length + " 条仍可勾选下载";
    refs.pnoteBox.textContent = "";
    refs.pnoteBox.classList.remove("warn", "error");
    hideSearchCancelBtn();
    return;
  }
  if (cls.kind === "error") {
    searchJob.streaming = false;
    refs.prog.setDeterminate(true);
    refs.prog.set(100);
    refs.progWrap.dataset.state = "error";
    refs.phaseBox.textContent = "搜索失败";
    refs.countsBox.textContent = "";
    refs.pnoteBox.classList.remove("warn");
    refs.pnoteBox.classList.add("error");
    refs.pnoteBox.textContent = cls.error;
    hideSearchCancelBtn();
    return;
  }
  if (cls.kind === "done" && searchJob.streaming) {
    // 终帧丢失的兜底（WS 抖动，app.js:1047-1051）
    searchJob.streaming = false;
    finishSearchProgress(previewData.meta || {});
  }
}

// ============================================================
// 取消（app.js:1054-1082）
// ============================================================
async function cancelSearch() {
  if (!refs || !searchJob || !searchJob.job_id || !searchJob.streaming) return;
  if (cancelBusy) return;                  // 在途防重复点 app.js:1056
  cancelBusy = true;
  refs.cancelBtn.disabled = true;
  try {
    const ready = await ensureApi();            // 布尔就绪，非桥对象（见 ensureApi 不变量）
    if (!ready) throw new Error("bridge not ready");
    await window.pywebview.api.preview_cancel(searchJob.job_id); // 同步直调（旧壳 app.js:5）
    // 终态由 cancelled job_update 渲染（app.js:1062）
  } catch (e) {
    console.debug("[T12] preview_cancel 调用失败：", e); // app.js:1064 同义
    refs.cancelBtn.disabled = false;
  } finally {
    cancelBusy = false;
  }
}

async function cancelSearchJobQuietly() {  // app.js:1071-1076
  if (searchJob && searchJob.job_id && searchJob.streaming) {
    try {
      const ready = await ensureApi();          // 布尔就绪，非桥对象（见 ensureApi 不变量）
      if (ready) await window.pywebview.api.preview_cancel(searchJob.job_id); // 同步直调
    } catch (e) { /* 静默尽力 */ }
    searchJob.streaming = false;
  }
}

async function backToStep1() {             // app.js:1078-1082
  await cancelSearchJobQuietly();
  gotoStep(1);
}

// ============================================================
// T52 任务选号（app.js:3574-3631 轻承接；行样式 style.css:1397-1405）
// ============================================================
function renderAcctSeg() {
  if (!refs || !refs.acctSegHost) return;
  const items = [{ value: "main", label: "主号" }];           // app.js:3579
  if (acctAlts.length) items.push({ value: acctAlts[0].id, label: "小号" }); // app.js:3580
  items.push({ value: "auto", label: "自动" });               // app.js:3581
  if (acctSeg) acctSeg.destroy();
  acctSeg = createSegmented(items, {
    value: newTaskAcct,
    ariaLabel: "本任务后续下载使用",
    // 仅记录选择（app.js:3602-3605 新建页不即发 RPC，创建成功后统一下发）
    onChange: function (v) { newTaskAcct = v; },
  });
  refs.acctSegHost.textContent = "";
  refs.acctSegHost.append(acctSeg.el);
}

// 小号清单异步补拉（2.1 renderSegs 于账号刷新后重建同义）；
// 失败静默降级=只显 主号|自动（app.js:3170-3174 同款）
async function loadAcctAlts() {
  try {
    const ready = await ensureApi();          // 布尔就绪，非桥对象（见 ensureApi 不变量）
    if (!ready) return;
    const r = await window.pywebview.api.account_list();   // 同步直调（app.js:3162）
    const list = (r && r.ok && Array.isArray(r.accounts)) ? r.accounts : [];
    const alts = list.filter(function (a) {
      return a && a.id && a.id !== "main" && a.state !== "absent";  // active.js:1535 同款
    });
    const changed = alts.length !== acctAlts.length ||
      (alts.length > 0 && acctAlts.length > 0 && alts[0].id !== acctAlts[0].id);
    if (changed) {
      acctAlts = alts;
      renderAcctSeg();
    }
  } catch (e) {
    /* 静默降级：账号清单不可得时不阻断新建流程 */
  }
}

// ============================================================
// 开始下载（app.js:852-872；两处 alert → toast，T10 契约 §4 承接）
// ============================================================
async function doStart() {
  if (!refs) return;
  if (!selected.size) {
    toast("warn", "请选择至少一个视频");    // app.js:855 alert 承接
    return;
  }
  // 只提交"已到达且勾选"的条目（selected 只能从已到达行产生）
  const keys = Array.from(selected).map(function (k) {
    const i = k.indexOf("|");
    return { src: k.slice(0, i), mid: k.slice(i + 1) };
  });
  const parallel = refs.parallel.value || 2; // app.js:860
  // 2.3 智能重命名：拟用新名（行内编辑优先，默认 it.rename 算句结果；
  // 显式取消勾选的行不下发=保持原名；空/与原名相同不下发）
  const renames = [];
  selected.forEach(function (k) {
    if (renameOff.has(k)) return;
    const i = k.indexOf("|");
    const it = previewData.items.find(function (x) { return keyOf(x.src, x.mid) === k; });
    const name = ((k in renameEdits) ? renameEdits[k] : (it && it.rename) || "").trim();
    if (it && name && name !== it.name) {
      renames.push({ src: k.slice(0, i), mid: Number(k.slice(i + 1)), name: name });
    }
  });
  const btn = refs.startBtn;
  btn.disabled = true;
  btn.textContent = "准备下载…";
  function restoreBtn() {
    btn.disabled = false;
    btn.textContent = "";
    btn.append("开始下载 (", refs.selCount, ")");
  }
  let r = null;
  try {
    const ready = await ensureApi();            // 布尔就绪，非桥对象（见 ensureApi 不变量）
    if (!ready) throw new Error("bridge not ready");
    r = await window.pywebview.api.start_download(previewData.channel, previewData.title,
      keys, parallel, "", previewData.job_id || "");   // 同步直调（旧壳 app.js:5）
  } catch (e) {
    restoreBtn();
    return;                                    // app.js:866 异常静默
  }
  if (!r || !r.ok) {
    restoreBtn();
    if (r) toast("error", r.error || "开始下载失败"); // app.js:867 alert 承接
    return;
  }
  restoreBtn(); // 成功路径同样恢复（TR12-1 G4 揪出：keep-alive 返回视图后按钮需可再点）
  // T52 任务选号承接（app.js:870 applyNewTaskAcct → 3625-3631）：本页选定的
  // acct_pref 随 edit_task fire-and-forget 下发，失败静默（下载已开始不受影响）
  if (r && r.task_id) {
    Promise.resolve(window.pywebview.api.edit_task(r.task_id, { acct_pref: newTaskAcct }))
      .catch(function (e) { console.debug("[T19a] 新任务 acct_pref 下发失败：", e); });
    // 2.3：拟用新名随任务下发（item.rename 落盘记录，后续编辑/续传可见；
    // 未下载条目下载完成即按此命名。失败不阻断建任务）
    if (renames.length) {
      Promise.resolve(window.pywebview.api.edit_task(r.task_id, { renames: renames }))
        .catch(function (e) { console.debug("[rename] 新任务拟用新名下发失败：", e); });
    }
  }
  navigate("/active");                      // app.js:871 switchView("active")
}
