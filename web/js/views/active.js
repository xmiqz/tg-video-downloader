// ============================================================
// views/active.js — T13 下载中视图（实时进度监控 · 手机卡/桌面紧凑行）
//
// 归属波次：T13（v2.2 W5-a），替换 T8 占位。所有权恰两文件：
//   web/js/views/active.js（本文件）+ web/css/views/active.css。
//
// 行为基线（对齐不回归，逐条 文件:行 注明出处）：
//   - refreshActive 数据流      app.js:1104-1167
//   - 1s 轮询/hidden 停拍/回前台补拉  app.js:1626-1661
//   - 组/卡三层增量同步          app.js:1302-1498（同文不重写）
//   - dl_phase 阶段位/骨架组      app.js:1231-1300（T49）
//   - toggleActiveExpand/get_item_text  app.js:1500-1517
//   - activeItem/item_action     app.js:1530-1552
//   - activeBulk 逐组失败聚合     app.js:1554-1595
//   - abortGroup 确认文案         app.js:1599-1619
//   - PendingOps/reconcile       app.js:71-136 / 1354-1428（T57）
//   - 组头恢复跳过               app.js:1402-1428 / 1748-1787
//   - acct 选号/标记/acct_hint    app.js:3113-3660（T52）/ T57 C1
//
// 组件消费：T10 桶唯一入口 ../ui/index.js（createProgress/createSegmented/
//   createToaster/createOverlay/createSelectionController/
//   createBottomActionBar/el），签名见 t10_contract.md。。
// RPC 消费（白名单 45 全子集）：active_states / is_active / get_item_text /
//   item_action / abort_task / resume_task / account_list / edit_task。
//
// thenable 桥不变量（W4 hard 教训）：bridge.js:69-74 的 api 是 Proxy，
//   任意属性（含 then）访问皆返 RPC 函数——任何 Promise 不得 resolve/
//   await 桥对象本身；就绪等待与 RPC 调用分离（ensureApi 只 resolve
//   布尔；调用一律同步直调 window.pywebview.api.xxx(...)，同 new.js 范式）。
//
// 5A seam 出口（任务书 §5A 授权，T11/T12 同款）：collectParams 式纯决策
//   18 件额外命名导出，仅供测试；本模块顶层零 DOM/window 访问
//   （node import 即证）。
// ============================================================

import {
  el, createProgress, createSegmented, createToaster, createOverlay,
  createSelectionController, createBottomActionBar,
} from "../ui/index.js";
// T17 手势增量（边缘返回/滑动托盘/聚焦保持；见 evidence/T17/t17_diff_list.md）
import {
  attachEdgeBack, attachSwipeActions, attachFocusKeep, trayFromButtons,
} from "../gestures.js";

export const path = "active";

export const title = "下载中 - TG 视频下载器";

// ============================================================
// 一、seam 纯函数（18 件；逐条注明旧壳出处；零 DOM/零 window）
// ============================================================

// S1 卡片键（app.js:1097 cardKey / 1091-1096 parseCk）
export function cardKeyOf(tid, src, mid) {
  return String(tid) + "|||" + String(src) + "|" + Number(mid);
}

export function parseCardKey(ck) {
  const s = String(ck == null ? "" : ck);
  const ix = s.indexOf("|||");
  if (ix < 0) return null;
  const rest = s.slice(ix + 3);
  const p = rest.lastIndexOf("|");
  if (p < 0) return null;
  return { tid: s.slice(0, ix), src: rest.slice(0, p), mid: Number(rest.slice(p + 1)) };
}

// S1 状态→徽位（app.js:1099-1102 ACTIVE_BADGE_CLS；未知 → "wait"）
export function activeBadgeClass(status) {
  const M = {
    "完成": "done", "已存在": "done", "失败": "fail",
    "已暂停": "paused", "等待中": "wait", "下载中": "run",
  };
  return M[status] || "wait";
}

// S2 跨任务聚合总览（app.js:1124-1153）
// 返回 {totBytes, totSize, speed, remaining, pct, etaSec, cc,
//       fetchDetails}（eta：remaining>0 且 speed>0 才有值，否则 null）
export function overviewAggregate(states) {
  const arr = Array.isArray(states) ? states : [];
  let totBytes = 0, totSize = 0, speed = 0;
  const cc = { done: 0, skip: 0, fail: 0, active: 0, paused: 0 };
  const fetchSet = [];
  const seenFetch = new Set();
  for (let i = 0; i < arr.length; i++) {
    const s = arr[i] || {};
    totBytes += Number(s.total_bytes) || 0;
    totSize += Number(s.total_tot) || 0;
    speed += Number(s.agg_speed) || 0;
    const c = s.counts || {};
    cc.done += Number(c.done) || 0;
    cc.skip += Number(c.skip) || 0;
    cc.fail += Number(c.fail) || 0;
    cc.active += Number(c.active) || 0;
    cc.paused += Number(c.paused) || 0;
    // T49：fetching 阶段同源抓取行文案一律来自后端 phase_detail（app.js:1138-1140）
    if (s.phase === "fetching" && s.phase_detail && !seenFetch.has(s.phase_detail)) {
      seenFetch.add(s.phase_detail);
      fetchSet.push(s.phase_detail);
    }
  }
  const pct = totSize ? Math.round(totBytes * 100 / totSize) : 0;
  const remaining = Math.max(0, totSize - totBytes);
  const etaSec = remaining > 0 && speed > 0 ? remaining / speed : null;
  return {
    totBytes: totBytes, totSize: totSize, speed: speed, remaining: remaining,
    pct: pct, etaSec: etaSec, cc: cc, fetchDetails: fetchSet,
  };
}

// S3 概览详情文案（app.js:1154-1155 逐字）
export function overviewDetailText(states) {
  const a = overviewAggregate(states);
  return "完成 " + a.cc.done + " · 跳过 " + a.cc.skip + " · 失败 " + a.cc.fail +
    " · 进行 " + a.cc.active + " · 暂停 " + a.cc.paused;
}

// S3 标题副文案（app.js:1156-1157 逐字）
export function overviewSubText(n) {
  return n + " 个任务下载中，按任务分组显示";
}

// S3 组头计数（app.js:1328-1329 逐字）
export function groupStatText(s) {
  const x = s || {};
  return (x.task_finished || 0) + "/" + (x.task_total || 0) + " 完成";
}

// S3 批量计数（app.js:1162-1163 逐字）
export function selCountText(n) {
  return "已选 " + n;
}

// S3 批量移除确认文案（app.js:1556-1557 逐字；弹层正文原样含句号）
export function removeConfirmText(n) {
  return "从任务中移除选中的 " + n + " 个条目？本地文件不会删除。";
}

// S3 中止任务确认文案（app.js:1600 逐字；弹层正文原样含换行）
export function abortConfirmText() {
  return "中止该任务的全部下载？\n未完成视频状态保留，可随时继续。";
}

// S4 状态→单项动作矩阵（app.js:1181-1191 button glyph/title 归一到 action 名）
export function itemButtonsFor(status) {
  if (status === "已暂停" || status === "失败") return ["resume", "remove"];
  if (status === "已跳过") return ["resume", "remove"];         // C9④ 行内恢复
  if (status === "完成" || status === "已存在") return ["remove"];
  if (status === "下载中" || status === "等待中") return ["pause", "skip", "remove"];
  return [];
}

// S5 选择集合按任务组分发（app.js:1565-1571）
export function bulkGroupKeys(selKeys) {
  const list = Array.isArray(selKeys) ? selKeys : [];
  const out = {};
  for (let i = 0; i < list.length; i++) {
    const r = parseCardKey(list[i]);
    if (!r) continue;
    (out[r.tid] || (out[r.tid] = [])).push({ src: r.src, mid: r.mid });
  }
  return out;
}

// S6 PendingOps 单键释放规则（app.js:1388-1399）
// pendKey 形如 "<cardKey>:<action>"；byCk 支持 Map 与 plain object。
export function pendReleaseRule(pendKey, byCk) {
  const ix = String(pendKey).lastIndexOf(":");
  if (ix < 0) return false;
  const action = String(pendKey).slice(ix + 1);
  const it = typeof byCk.get === "function" ? byCk.get(String(pendKey).slice(0, ix))
    : byCk[String(pendKey).slice(0, ix)];
  switch (action) {
    case "pause": return it == null || it.status === "已暂停";
    case "skip": return it == null || it.status === "已跳过";
    case "remove": return it == null;
    case "resume": return it == null || ["已暂停", "失败", "已跳过"].indexOf(it.status) < 0;
    default: return false;
  }
}

// S7 对账计划（app.js:1354-1386 纯化）：entries=[{key,ctx}] → 应释放 keys
export function planReconcile(entries, states) {
  const list = Array.isArray(entries) ? entries : [];
  if (!list.length) return [];
  const arr = Array.isArray(states) ? states : [];
  const byTask = new Map();
  const byCk = new Map();
  for (let i = 0; i < arr.length; i++) {
    const s = arr[i] || {};
    byTask.set(s.task_id, s);
    const items = Array.isArray(s.items) ? s.items : [];
    for (let j = 0; j < items.length; j++) {
      const it = items[j] || {};
      byCk.set(cardKeyOf(s.task_id, it.src, it.mid), it);
    }
  }
  const released = [];
  for (let i = 0; i < list.length; i++) {
    const e = list[i] || {};
    const key = e.key || "";
    const ctx = e.ctx || {};
    let hit = false;
    if (key.indexOf("abort:") === 0) {
      const s = byTask.get(ctx.tid);
      hit = !s || s.phase === "aborted";
    } else if (key.indexOf("resumeskip:") === 0) {
      const s = byTask.get(ctx.tid);
      if (!s) hit = false;
      else if (!ctx.existedBefore) hit = true;
      else if (Array.isArray(ctx.resumeCks) && ctx.resumeCks.length) {
        hit = ctx.resumeCks.every(function (ck2) {
          const it = byCk.get(ck2);
          return !it || it.status !== "已跳过";
        });
      }
    } else if (key.indexOf("bulk:") === 0) {
      hit = (ctx.keys || []).length > 0 &&
        ctx.keys.every(function (k2) { return pendReleaseRule(k2, byCk); });
    } else if (ctx.ck) {
      hit = pendReleaseRule(key, byCk);
    }
    if (hit) released.push(key);
  }
  return released;
}

// S8 dl_phase 帧分类（app.js:1266-1300 语义收口）
// 返回 {valid, phase, text, index, total, terminal, buildSkeleton}
export function classifyDlPhase(detail) {
  if (!detail || typeof detail !== "object" || typeof detail.task_id !== "string") {
    return { valid: false, phase: "", text: "", index: null, total: null, terminal: false, buildSkeleton: false };
  }
  const TERMINAL = { done: 1, aborted: 1, error: 1 };
  const phase = detail.phase || "";
  const terminal = TERMINAL[phase] === 1;
  // T49：早期阶段常先于首次轮询建组到达——组未建则先落骨架组；
  // 终态帧不建组（墓碑快照轮询自会建组），app.js:1273-1277。
  return {
    valid: true,
    phase: phase,
    text: detail.phase_detail || "",
    index: num(detail.phase_index),
    total: num(detail.phase_total),
    terminal: terminal,
    buildSkeleton: !!phase && !terminal,
  };
}
function num(v) {
  const n = Number(v);
  return Number.isFinite(n) && n > 0 ? n : null;
}

// S9 download 类 job_update 终态分类（app.js:2604-2606 + 2645；终态补拉
// 使 active_states 墓碑快照尽快可见）
export function classifyDownloadJob(job) {
  if (!job || typeof job.job_id !== "string") return { kind: "ignore" };
  if (job.kind !== "download") return { kind: "ignore" };
  if (job.state === "done" || job.state === "error" || job.state === "cancelled") {
    return { kind: "refresh" };
  }
  return { kind: "ignore" };
}

// S10 acct_hint 负载归一（app.js:3521-3529 normHint；T51 嵌套/T52 旧扁平双形状）
export function normAcctHint(raw) {
  if (!raw) return null;
  const d = (raw.data && typeof raw.data === "object") ? raw.data : {};
  const out = {
    account_id: d.from_acct || raw.account_id || "",
    reason: d.reason || raw.reason || "",
    has_alt: typeof d.has_alt === "boolean" ? d.has_alt : null,
  };
  if (!out.account_id) return null;
  return out;
}

// S11 busy 文案（app.js:53-58 T57_LABELS.busy + 兜底）
export function busyLabelFor(action) {
  const M = {
    pause: "暂停中…", skip: "跳过中…", remove: "移除中…", resume: "恢复中…",
    abort: "正在中止…", resumeskip: "恢复跳过中…",
  };
  return M[action] || "处理中…";
}

// ============================================================
// 二、内部工具（非导出；语义与旧壳等价）
// ============================================================

// 体积/速度/时长人性化（app.js:13-28 human/humanSpeed/humanSec 同义）
function human(n) {
  n = Number(n) || 0;
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return n.toFixed(n >= 100 || i === 0 ? 0 : 1) + " " + u[i];
}
const humanSpeed = (n) => human(n) + "/s";
function humanSec(s) {
  if (s == null || !isFinite(s)) return "--";
  s = Math.round(s);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  if (h) return h + ":" + String(m).padStart(2, "0") + ":" + String(sec).padStart(2, "0");
  return m + ":" + String(sec).padStart(2, "0");
}
// 属性选择器值转义（app.js:1090 qAttr）
function qAttr(v) { return String(v).replace(/["\\]/g, "\\$&"); }
// 恢复跳过按钮文案（app.js:63 resumeSkipLabel）
const resumeSkipLabel = (n) => "恢复跳过 " + n + " 个";
// 导航（T12 同款路由探针优先/降级 hash）
function navigate(p, opts) {
  if (typeof window !== "undefined" && window.__t8router && typeof window.__t8router.navigate === "function") {
    window.__t8router.navigate(p, opts);
  } else if (typeof location !== "undefined") {
    location.hash = "#" + p;
  }
}

// ============================================================
// 三、PendingOps（T57 复刻：ACK 不撤忙、15s TTL 兜底、渲染期 view() 回填）
// 新壳纪律差异：TTL 兜底撤忙不 console.warn，记录 warns（探针可断言）。
// node 支撑数组（底部动作条 + 桌面批量条双宿主同 busy）与单元素。
// ============================================================

const T57_PENDING_TTL_MS = 15000;

function createPendingOps() {
  const map = new Map();   // key -> {ts,label,busyText,nodes:[],ctx,timer}
  const warns = [];

  function pendBusyNode(node, busyText) {
    if (!node || node.dataset.busy === "1") return;
    node.dataset.busy = "1";
    node.dataset.oldText = node.textContent;
    node.disabled = true;
    node.classList.add("is-busy");
    node.textContent = "";
    node.append(el("span", { class: "ac-spin", "aria-hidden": "true" }),
      document.createTextNode(busyText != null ? String(busyText) : ""));
  }
  function unpendBusyNode(node) {
    if (!node || node.dataset.busy !== "1") return;
    node.classList.remove("is-busy");
    node.disabled = false;
    if (node.dataset.oldText !== undefined) node.textContent = node.dataset.oldText;
    delete node.dataset.busy;
    delete node.dataset.oldText;
  }
  function pend(key, opts) {
    if (map.has(key)) return false;
    const o = opts || {};
    const e = {
      ts: Date.now(),
      label: o.label || "",
      busyText: o.busyText != null ? o.busyText : o.label || "",
      nodes: o.node == null ? [] : (Array.isArray(o.node) ? o.node : [o.node]),
      ctx: o.ctx || {},
      timer: 0,
    };
    e.timer = setTimeout(function () {
      if (map.has(key)) {
        map.delete(key);
        e.nodes.forEach(unpendBusyNode);
        warns.push({ key: key, at: Date.now() });
      }
    }, T57_PENDING_TTL_MS);
    map.set(key, e);
    e.nodes.forEach(function (n) { pendBusyNode(n, e.busyText); });
    return true;
  }
  function release(key) {
    const e = map.get(key);
    if (!e) return false;
    clearTimeout(e.timer);
    map.delete(key);
    e.nodes.forEach(unpendBusyNode);
    return true;
  }
  function releaseAll() {
    for (const key of Array.from(map.keys())) release(key);
  }
  return {
    pend: pend,
    release: release,
    releaseAll: releaseAll,
    has: (key) => map.has(key),
    view: (key) => (map.has(key) ? { busy: true, label: map.get(key).label } : null),
    setTtl: () => {}, // 探针兼容占位（测试用 TTL 验证由 CDP 补拍覆盖）
    map: map,
    get warns() { return warns; },
  };
}

// ============================================================
// 四、模块级状态（keep-alive 期间保留；import 期零副作用）
// ============================================================
let cached = null;
let refs = null;
let pendingOps = createPendingOps();
let sel = null;              // createSelectionController 实例（mount 时建）
let desktopBulkBtns = null;  // 桌面批量条按钮 map（key->button）
let bottomBar = null;        // createBottomActionBar（手机悬浮）
let toaster = null;
let overlayAbort = null;     // 中止确认弹层（createOverlay）
let overlayRemove = null;    // 批量移除确认弹层
let segs = new Map();        // gid -> segmented 实例（组头选号）
const expandedActive = new Set();  // app.js:1087
const activeTexts = new Map();     // app.js:1089（展开时才按需拉取）
const dlPhaseCache = new Map();    // app.js:1234（task_id -> {phase,text,index,total}）
const taskAcct = new Map();        // app.js:3119（task_id -> "main"|"alt1"|"auto"）
let accounts = [];                // account_list 归一结果（main + alt1…）
let acctTried = false;            // account_list 首拉标志
let lastStates = [];              // 最近一拍快照（探针/断言）
let pollTimer = 0;
let pollBusy = false;             // 防重叠（app.js:1623）
let pollStopped = true;
let refreshBusy = false;          // refreshActive 防重入
const deferTimers = new Set();    // 250/300/400ms 一次性补拉 timer（onLeave 全清）
let offline = false;              // 断网 banner 态
let hintText = "";                // acct_hint 限速提示行文案（空=不显示）
let ptr = { touching: false, startY: 0, top: 0, dy: 0, firing: false };
let ptrX0 = 0; // T17 轴锁起点（横向占优让位滑动托盘）
let winListeners = 0;             // window 监听计数（onLeave 归零探针）
let pyReadyHandler = null;
let apiReadyP = null;

// ============================================================
// 五、RPC 桥（thenable 不变量：就绪 Promise 只 resolve 布尔）
// ============================================================
function bridgeReady() {
  return !!(typeof window !== "undefined" && window.pywebview && window.pywebview.api);
}
function ensureApi() {
  if (bridgeReady()) return Promise.resolve(true);       // 布尔；绝不 resolve 桥对象
  if (!apiReadyP) {
    apiReadyP = new Promise(function (resolve) {
      pyReadyHandler = function () {
        pyReadyHandler = null;
        resolve(bridgeReady());                          // 布尔；绝不 resolve 桥对象
      };
      window.addEventListener("pywebviewready", pyReadyHandler);
      winListeners++;
    }).then(function (ready) {
      apiReadyP = null;
      return ready;                                      // 布尔透传
    });
  }
  return apiReadyP;
}

function toast(type, text, detail) {
  if (!toaster) toaster = createToaster({ max: 5 });
  const spec = { type: type, text: text };
  if (detail != null && detail !== "") spec.detail = String(detail);
  return toaster.show(spec);
}
// 统一错误反馈（对齐旧壳 showErr：摘要+详情展开、不自动消失=T10 error 语义）
function showErr(summary, e) {
  const d = e && e.message ? e.message : (typeof e === "string" ? e : JSON.stringify(e));
  toast("error", summary, d);
}
// 通用 need_login 兜底（app.js:352-358 同义）：开登录弹层，受控提示进卡内
//（2.1 showLoginCard(notice) 口径：提示随卡呈现，不另发 toast）
function needLoginToasted(r) {
  if (r && r.need_login) {
    navigate("/login", { force: true, notice: "该功能需要登录，登录后将自动继续刚才的操作" });
    return true;
  }
  return false;
}

// ============================================================
// 六、mount / onEnter / onLeave（T8 契约 §1）
// ============================================================
export function mount() {
  if (cached) return cached;
  try {
    cached = buildDom();
  } catch (e) {
    // mount 自捕获：失败返回降级元素（同步返回，rejected Promise 不入 mounts 缓存）
    if (typeof console !== "undefined" && console.error) console.error("[T13] active 视图构建失败：", e);
    cached = el("div", { class: "vw-active" }, [
      el("div", { class: "u-empty", role: "status" }, [
        el("div", { class: "u-empty-ico", "aria-hidden": "true", text: "⚠" }),
        el("h3", { class: "u-empty-title", text: "下载中视图暂不可用" }),
        el("p", { class: "u-empty-hint", text: "视图初始化失败，可刷新页面重试。" }),
      ]),
    ]);
  }
  if (typeof window !== "undefined" && !window.__t13probe) {
    // 只读断言探针（TR13-1 onLeave 全清理/轮询停拍断言用；无敏感信息）
    window.__t13probe = {
      pending: pendingOps,
      dlPhase: dlPhaseCache,
      get winBound() { return winListeners; },
      timersArmed: function () {
        return pollTimer !== 0 || deferTimers.size > 0 || pendingOps.map.size > 0 ||
          ptr.firing === true;
      },
      selState: function () { return sel ? sel.state : null; },
      get offline() { return offline; },
      get lastStates() { return lastStates; },
      lastCallCount: function () {
        return (typeof window !== "undefined" && Array.isArray(window.__t13_calls)) ? window.__t13_calls.length : -1;
      },
    };
  }
  return cached;
}

export function onEnter() {
  if (!cached || !refs) return;
  bindEvents();
  startPoll();
  // 首进补拉 + 账号数据源初拉（T52 账号限速提示/组头选号数据）
  refreshActive();
  if (!acctTried) loadAccounts();
}

export function onLeave() {
  // ①停轮询（clearTimeout + 停拍标志；在途 pollOnce 由 stopped 门自行收敛）
  pollStopped = true;
  if (pollTimer) { clearTimeout(pollTimer); pollTimer = 0; }
  // ②清一次性补拉 timer 全集
  for (const t of Array.from(deferTimers)) { clearTimeout(t); deferTimers.delete(t); }
  // ③PendingOps 全释放（TTL timers 一并清零；选择 busy/组头 busy 复位）
  pendingOps.releaseAll();
  // ④解绑全部 window 监听（计数归零是 TR13-1 的 onLeave 探针断言面）
  unbindEvents();
  // ⑤pywebviewready 待定承诺复位
  if (pyReadyHandler) {
    window.removeEventListener("pywebviewready", pyReadyHandler);
    pyReadyHandler = null;
    winListeners--;
  }
  apiReadyP = null;
  // ⑥选择模式退出（卡片勾选态/动作条全收；同步 UI 归位）
  if (sel) sel.exit();
  syncSelUI(sel ? sel.state : null);
  // ⑦未决确认弹层收起 + 底部条隐藏（常驻实例不销毁，重进即用）
  if (overlayAbort && overlayAbort.isOpen()) overlayAbort.close("leave");
  if (overlayRemove && overlayRemove.isOpen()) overlayRemove.close("leave");
  if (bottomBar) bottomBar.hide();
  // ⑧PTR 态复位
  ptr = { touching: false, startY: 0, top: 0, dy: 0, firing: false };
  setPtrUI(null);
}

// ---- window 事件绑定/解绑（计数对称；onLeave 归零）----
function addWin(name, fn) {
  window.addEventListener(name, fn);
  winListeners++;
  return fn;
}
function delWin(name, fn) {
  window.removeEventListener(name, fn);
  winListeners--;
}
let bound = null; // {dlPhase, jobUpd, acctHint, online, offline, vis, touchStart, touchMove, touchEnd, resize}
function bindEvents() {
  if (bound) return;
  bound = {
    dlPhase: addWin("tg:dl_phase", onDlPhaseEvent),
    jobUpd: addWin("tg:job_update", onJobUpdateEvent),
    acctHint: addWin("tg:acct_hint", onAcctHintEvent),
    online: addWin("online", function () { setOffline(false); refreshActive(); }),
    offline: addWin("offline", function () { setOffline(true); }),
    vis: addWin("visibilitychange", function () {
      // 回前台立即补拉（app.js:1655-1661；hidden 拍交 startPoll 门控跳过）
      if (document.hidden || pollBusy) return;
      pollBusy = true;
      pollOnce().catch(function () { }).finally(function () { pollBusy = false; });
    }),
  };
  // PTR（手机）：监听挂视图根节点（不污染 window；随 keep-alive 存续）
  bound.touchStart = function (e) { ptrStart(e); };
  bound.touchMove = function (e) { ptrMove(e); };
  bound.touchEnd = function () { ptrEnd(); };
  bound.touchCancel = function () { ptr.touching = false; ptr.dy = 0; setPtrUI(null); };
  refs.root.addEventListener("touchstart", bound.touchStart, { passive: true });
  refs.root.addEventListener("touchmove", bound.touchMove, { passive: true });
  refs.root.addEventListener("touchend", bound.touchEnd, { passive: true });
  refs.root.addEventListener("touchcancel", bound.touchCancel, { passive: true });
  // T17 手势增量：边缘返回 + 卡片滑动托盘（透传 .ac-action 既有按钮）+ 聚焦保持
  bound.gs = [
    attachEdgeBack(),
    attachSwipeActions(refs.root, {
      itemSel: ".ac-card",
      actions: function (card) {
        return trayFromButtons(card, ".ac-cactions .ac-action", ACTION_TITLE);
      },
    }),
    attachFocusKeep(refs.root),
  ];
}
function unbindEvents() {
  if (!bound) return;
  delWin("tg:dl_phase", bound.dlPhase);
  delWin("tg:job_update", bound.jobUpd);
  delWin("tg:acct_hint", bound.acctHint);
  delWin("online", bound.online);
  delWin("offline", bound.offline);
  delWin("visibilitychange", bound.vis);
  if (refs && refs.root) {
    refs.root.removeEventListener("touchstart", bound.touchStart);
    refs.root.removeEventListener("touchmove", bound.touchMove);
    refs.root.removeEventListener("touchend", bound.touchEnd);
    refs.root.removeEventListener("touchcancel", bound.touchCancel);
  }
  // T17 手势增量对称回收（attach* 均返回 detach）
  if (bound.gs) {
    for (let i = 0; i < bound.gs.length; i++) bound.gs[i]();
    bound.gs = null;
  }
  bound = null;
}

// ============================================================
// 七、DOM 构建（仅 mount 一次执行；顶层与 onEnter 无 DOM 依赖）
// ============================================================
function buildDom() {
  const root = el("div", { class: "vw-active" });

  // —— 页头（index.html:132-140 承接；标题+副标题+选择/全选入口）——
  const sub = el("p", { class: "ac-sub", text: "实时监控所有下载进度，点击任意任务查看消息详情" }); // index.html:135
  // 2.1 空态页头口径（index.html head-actions 静态提示）：无任务只显提示，
  // 选择/全选隐藏（hidden 初始即藏，refreshActive 按数据翻显）。
  const bulkHint = el("span", { class: "ac-sub", text: "勾选卡片可批量操作" });
  const selectBtn = el("button", {
    class: "u-btn u-btn--ghost ac-select-btn", type: "button",
    text: "选择", "aria-pressed": "false", hidden: true, onclick: toggleSelectMode,
  });
  const selAllBtn = el("button", {
    class: "u-btn u-btn--ghost", type: "button",
    text: "全选", disabled: true, hidden: true, onclick: function () { selectAllToggle(); },
  });
  root.append(el("header", { class: "ac-head" }, [
    el("div", { class: "ac-head-main" }, [
      el("h2", { text: "下载中" }),
      sub,
    ]),
    el("div", { class: "ac-head-actions" }, [bulkHint, selAllBtn, selectBtn]),
  ]));

  // —— 断网 banner（行为需求 §6：navigator.onLine + RPC 异常可见）——
  const offlineBar = el("div", { class: "ac-offline", role: "status", hidden: true },
    [el("span", { class: "ac-offline-dot", "aria-hidden": "true" }),
     el("span", { text: "网络连接不可用，正在等待恢复…恢复后将自动刷新" })]);

  // —— acct_hint 限速提示行（app.js:3544-3561 同源文案；可关闭）——
  const hintTextEl = el("span", { class: "ac-hint-text", text: "" });
  const hintBar = el("div", { class: "ac-hint", role: "status", hidden: true }, [
    hintTextEl,
    el("button", {
      class: "ac-hint-x", type: "button", "aria-label": "关闭", text: "✕",
      onclick: function () { hintText = ""; syncHintUI(); },
    }),
  ]);

  // —— 概览面板（index.html:151-168 / app.js:1124-1157）——
  const prog = createProgress({ determinate: true, value: 0 });
  const ovPct = el("span", { class: "ac-ov-pct", text: "0%" });
  const ovDetail = el("div", { class: "ac-ov-detail", text: "" });
  const ovCard1 = el("div", { class: "ac-ov-card ac-ov-card--main" }, [
    el("div", { class: "ac-ov-label", text: "总体进度" }),
    el("div", { class: "ac-ov-prog" }, [prog.el, ovPct]),
    ovDetail,
  ]);
  const ovSpeed = el("div", { class: "ac-ov-big", text: "0 B/s" });
  const ovEta = el("div", { class: "ac-ov-big", text: "--" });
  const ovCard2 = el("div", { class: "ac-ov-card" }, [
    el("div", { class: "ac-ov-label", text: "总速度" }), ovSpeed,
  ]);
  const ovCard3 = el("div", { class: "ac-ov-card" }, [
    el("div", { class: "ac-ov-label", text: "剩余时间" }), ovEta,
  ]);
  const overview = el("div", { class: "ac-overview", hidden: true }, [ovCard1, ovCard2, ovCard3]);

  // —— fetching 阶段同源抓取行（app.js:1138-1146 fetch-banner）——
  const fetchText = el("span", { class: "ac-fetch-text", text: "" });
  const fetchBar = el("div", { class: "ac-fetch", role: "status", hidden: true }, [
    el("span", { class: "ac-fetch-dot", "aria-hidden": "true" }), fetchText,
  ]);

  // —— PTR 下拉刷新指示（行为需求 §5 本波基本实现）——
  const ptrText = el("span", { text: "下拉刷新" });
  const ptrBar = el("div", { class: "ac-ptr", "aria-live": "polite", hidden: true }, [ptrText]);

  // —— 桌面批量条（index.html:142-149 承接；手机由 bottom bar 接管）——
  const bulkCount = el("span", { class: "ac-bulk-count", text: selCountText(0) });
  const mkBulkBtn = (key, cls, labelText) => el("button", {
    class: "u-btn u-btn--" + cls + " u-btn--md", type: "button",
    dataset: { bulk: key }, text: labelText,
    onclick: function () { onBulkAction(key); },
  });
  const bulkBtnEls = {
    resume: mkBulkBtn("resume", "primary", "开始"),
    pause: mkBulkBtn("pause", "ghost", "暂停"),
    skip: mkBulkBtn("skip", "ghost", "跳过"),
    remove: mkBulkBtn("remove", "danger", "移除"),
  };
  const bulkClearBtn = el("button", {
    class: "u-btn u-btn--ghost u-btn--md", type: "button", text: "取消选择",
    onclick: function () { if (sel) { sel.exit(); } },
  });
  desktopBulkBtns = bulkBtnEls;
  const bulkBar = el("div", { class: "ac-bulk", role: "toolbar", "aria-label": "批量操作", hidden: true }, [
    bulkCount, bulkBtnEls.resume, bulkBtnEls.pause, bulkBtnEls.skip, bulkBtnEls.remove, bulkClearBtn,
  ]);

  // —— 组列表 / 空态（index.html:171-177）——
  const list = el("div", { class: "ac-list", "aria-label": "下载任务分组" });
  const empty = el("div", { class: "u-empty", role: "status", hidden: true }, [
    el("div", { class: "u-empty-ico", "aria-hidden": "true", text: "🫧" }),       // index.html:173
    el("h3", { class: "u-empty-title", text: "当前没有进行中的下载" }),          // index.html:174
    el("p", { class: "u-empty-hint", text: "到「新建下载」选择频道与视频开始" }), // index.html:175
    el("button", {
      class: "u-btn u-btn--primary", type: "button", text: "去新建",
      onclick: function () { navigate("/new"); },                                  // index.html:176 switchView("new")
    }),
  ]);

  root.append(offlineBar, hintBar, overview, fetchBar, ptrBar, bulkBar, list, empty);

  // —— 选择控制器 + 底部动作条（手机悬浮；与桌面条解耦联动）——
  sel = createSelectionController({
    actions: [
      { key: "resume", enabledWhen: (s) => s.count > 0 },
      { key: "pause", enabledWhen: (s) => s.count > 0 },
      { key: "skip", enabledWhen: (s) => s.count > 0 },
      { key: "remove", enabledWhen: (s) => s.count > 0 },
    ],
    onChange: syncSelUI,
  });
  bottomBar = createBottomActionBar({
    countLabel: selCountText,
    hidden: true,
    ariaLabel: "批量操作",
    actions: [
      { key: "resume", label: "开始", primary: true },
      { key: "pause", label: "暂停" },
      { key: "skip", label: "跳过" },
      { key: "remove", label: "移除", danger: true },
    ],
    onAction: onBulkAction,
  });
  document.body.append(bottomBar.el);
  // 底部条取消选择补充槽（对齐旧壳批量条「取消选择」index.html:148）
  const barCancel = el("button", {
    class: "u-btn u-btn--ghost u-btn--md", type: "button", text: "取消选择",
    onclick: function () { if (sel) sel.exit(); },
  });
  bottomBar.el.append(barCancel);

  // —— 确认弹层（createOverlay 承接旧 confirm；文案逐字）——
  overlayAbort = createOverlay({
    title: "中止任务",
    ariaLabel: "确认中止任务",
    content: abortConfirmText(),
    actions: [
      { key: "cancel", label: "取消" },
      { key: "ok", label: "中止任务", danger: true },
    ],
    onAction: function (key) {
      if (key !== "ok") return;
      const tid = overlayAbortConfirmed();
      if (tid) doAbort(tid);
    },
  });
  document.body.append(overlayAbort.el);
  overlayRemove = createOverlay({
    title: "移除条目",
    ariaLabel: "确认移除条目",
    content: removeConfirmText(0),
    actions: [
      { key: "cancel", label: "取消" },
      { key: "ok", label: "移除", danger: true },
    ],
    onAction: function (key) {
      if (key !== "ok") return;
      doBulk(overlayRemoveAction());
    },
  });
  document.body.append(overlayRemove.el);
  // 弹层正文按 confirm 原文换行渲染（white-space:pre-line 承接 \n）
  pendingAbortTid = "";
  pendingBulkAction = "";

  refs = {
    root: root, sub: sub, bulkHint: bulkHint,
    selectBtn: selectBtn, selAllBtn: selAllBtn,
    offlineBar: offlineBar, hintBar: hintBar, hintTextEl: hintTextEl,
    overview: overview, prog: prog, ovPct: ovPct, ovDetail: ovDetail,
    ovSpeed: ovSpeed, ovEta: ovEta,
    fetchBar: fetchBar, fetchText: fetchText,
    ptrBar: ptrBar, ptrText: ptrText,
    bulkBar: bulkBar, bulkCount: bulkCount, bulkBtns: bulkBtnEls,
    list: list, empty: empty,
  };
  return root;
}

// ============================================================
// 八、状态刷新主链（refreshActive app.js:1104-1167）
// ============================================================
async function refreshActive() {
  if (!refs || refreshBusy) return;
  refreshBusy = true;
  let states = null;
  try {
    const ready = await ensureApi();                    // 布尔（thenable 不变量）
    if (!ready) return;
    states = await window.pywebview.api.active_states(); // 同步直调
  } catch (e) {
    // RPC 网络异常可见（断网 banner；恢复自动补拉）——不弹错误轰炸
    setOffline(true, e);
    return;
  } finally {
    refreshBusy = false;
  }
  setOffline(false);
  if (!Array.isArray(states)) states = [];
  lastStates = states;

  // T57 C7：对账前置（空态早退之前；abort 键释放必须覆盖空 states 路径）
  reconcileAll(states);

  const running = states.length > 0;
  if (!running) {
    // 空态收口（app.js:1112-1119）：清选择/隐藏概览与批量/清列表/显示空态
    selectionSetKeys([]);
    wrapSelClear(false);                    // app.js:1113 activeSel.clear()
    refs.empty.hidden = false;
    refs.overview.hidden = true;
    refs.fetchBar.hidden = true;
    // 2.1 空态页头口径：选择/全选隐藏，只显静态提示
    refs.selectBtn.hidden = true;
    refs.selAllBtn.hidden = true;
    refs.bulkHint.hidden = false;
    refs.sub.textContent = "实时监控所有下载进度，点击任意任务查看消息详情";
    clearGroups();
    syncSelUI(sel.state);
    return;
  }
  refs.empty.hidden = true;
  refs.overview.hidden = false;
  // 有任务：翻显选择/全选，收起静态提示（2.1 有任务页头含批量入口）
  refs.selectBtn.hidden = false;
  refs.selAllBtn.hidden = false;
  refs.bulkHint.hidden = true;

  // 总览（app.js:1123-1157）
  const a = overviewAggregate(states);
  refs.prog.set(a.pct);
  refs.ovPct.textContent = a.pct + "%";
  refs.ovSpeed.textContent = humanSpeed(a.speed);
  refs.ovEta.textContent = a.etaSec != null ? humanSec(a.etaSec) : "--"; // app.js:1152-1153，speed 0 → "--"
  refs.ovDetail.textContent = overviewDetailText(states);
  refs.sub.textContent = overviewSubText(states.length);

  // fetching 同源抓取行（app.js:1138-1146）
  refs.fetchBar.hidden = a.fetchDetails.length === 0;
  if (a.fetchDetails.length) setTextSafely(refs.fetchText, a.fetchDetails.join("；"));

  // 组/卡增量（app.js:1165-1166 syncActiveList）
  syncGroups(states);
  syncSelUI(sel.state);
}

// 对账（app.js:1354-1386 reconcilePendingOps + 空态路径覆盖）
function reconcileAll(states) {
  if (pendingOps.map.size === 0) return;
  const entries = [];
  for (const [key, e] of pendingOps.map) entries.push({ key: key, ctx: e.ctx });
  const released = planReconcile(entries, states);
  for (let i = 0; i < released.length; i++) pendingOps.release(released[i]);
}

// ============================================================
// 九、组/卡渲染（syncActiveList/syncCards/updateCard app.js:1302-1498）
// ============================================================
function clearGroups() {
  for (const gid of Array.from(segs.keys())) destroySeg(gid);
  while (refs.list.firstChild) refs.list.removeChild(refs.list.firstChild);
}

function syncGroups(states) {
  const live = new Set();
  for (let i = 0; i < states.length; i++) {
    const s = states[i] || {};
    const gid = String(s.task_id || "");
    if (!gid) continue;
    live.add(gid);
    let g = groupByGid(gid);
    let isNew = false;
    if (!g) {
      isNew = true;                     // 仅新组需填选号控件（1s 轮询不重建，app.js:1305）
      g = buildGroup(gid);
      refs.list.append(g);
    }
    // 组头（app.js:1327-1331；title/计数字段每拍同文不重写）
    setTextSafely(g.querySelector(".ac-gname"), s.title || "任务");
    setTextSafely(g.querySelector(".ac-gstat"), groupStatText(s));
    // 轮询侧阶段位（无 index；事件侧 index 不被轮询覆盖）
    updateGroupPhaseEl(g, s.phase || "", s.phase_detail || "", null, null);
    // 恢复跳过按钮三态（app.js:1402-1428）
    updateGroupResumeSkip(g, s);
    // 卡片（app.js:1335）
    syncCards(g.querySelector(".ac-gbody"), s);
    if (isNew) renderSegFor(gid, g);     // T52：新建组一次填入（含骨架组语义承接）
  }
  // 掉组清理（app.js:1337-1339）
  for (const node of Array.from(refs.list.children)) {
    const gid2 = node.getAttribute("data-gid") || "";
    if (!live.has(gid2)) {
      destroySeg(gid2);
      node.remove();
    }
  }
  // 选择 keys 全集（全选数学；selection 独立于 keys——toggle 未知键仍可切）
  const keys = [];
  for (let i = 0; i < states.length; i++) {
    const s = states[i] || {};
    const items = Array.isArray(s.items) ? s.items : [];
    for (let j = 0; j < items.length; j++) {
      keys.push(cardKeyOf(s.task_id, items[j].src, items[j].mid));
    }
  }
  selectionSetKeys(keys);
}

function groupByGid(gid) {
  return refs.list.querySelector('.ac-group[data-gid="' + qAttr(gid) + '"]');
}

function buildGroup(gid) {
  const gAbort = el("button", {
    class: "u-btn u-btn--danger u-btn--md ac-gabort", type: "button",
    text: "中止任务",                                                        // app.js:1322
    onclick: function (ev) { ev.stopPropagation(); askAbort(gid, [gAbort]); },
  });
  const g = el("div", { class: "ac-group", dataset: { gid: gid } }, [
    el("div", { class: "ac-group-head" }, [
      el("div", { class: "ac-gtitle" }, [
        el("span", { class: "ac-gname", text: "" }),
        el("span", { class: "ac-gstat", text: "" }),
      ]),
      el("span", { class: "ac-phase", dataset: { phase: "" } }, [
        el("span", { class: "ac-phase-dot", "aria-hidden": "true" }),
        el("span", { class: "ac-phase-text", text: "" }),
        el("span", { class: "ac-phase-idx", text: "", hidden: true }),
      ]),
      el("div", { class: "ac-acct", dataset: { acctTid: gid } }),
      gAbort,
    ]),
    el("div", { class: "ac-gbody" }),
  ]);
  return g;
}

// 阶段位双更新（app.js:1238-1244 updateGroupPhase；text 全源、index 仅事件源）
function updateGroupPhaseEl(g, phase, text, index, total) {
  const pe = g.querySelector(".ac-phase");
  if (!pe) return;
  if (pe.getAttribute("data-phase") !== phase) pe.setAttribute("data-phase", phase);
  const txt = pe.querySelector(".ac-phase-text");
  if (txt && txt.textContent !== text) txt.textContent = text;
  const idx = pe.querySelector(".ac-phase-idx");
  if (idx) {
    // phase_index/phase_total 进度显示（行为需求 §2；终态/空相位收起）
    if (!phase) idx.hidden = true;
    else if (index != null && total != null) {
      const s = index + "/" + total;
      if (idx.textContent !== s) idx.textContent = s;
      idx.hidden = false;
    }
  }
}

function syncCards(body, s) {
  const items = Array.isArray(s.items) ? s.items : [];
  const live = new Set();
  for (let i = 0; i < items.length; i++) {
    const it = items[i] || {};
    const ck = cardKeyOf(s.task_id, it.src, it.mid);
    live.add(ck);
    let card = body.querySelector('.ac-card[data-ck="' + qAttr(ck) + '"]');
    if (!card) {
      card = buildCard(s.task_id, it, ck);
      body.append(card);   // 首拍建齐，顺序与 items 一致（app.js:1440）
    }
    updateCard(card, s.task_id, it, ck);
  }
  for (const node of Array.from(body.querySelectorAll(".ac-card"))) {
    if (!live.has(node.getAttribute("data-ck"))) node.remove();
  }
}

function buildCard(tid, it, ck) {
  const cbox = el("button", {
    class: "ac-cbox", type: "button", role: "checkbox", "aria-checked": "false",
    "aria-label": "选择 " + String(it.name || ""), tabindex: "-1",
    onclick: function (ev) { ev.stopPropagation(); toggleSelOne(ck); },
  }, [el("span", { class: "ac-cbox-mark", "aria-hidden": "true", text: "" })]);
  const cname = el("div", { class: "ac-cname", text: String(it.name || "") });
  const cmeta = el("div", { class: "ac-cmeta" }, [
    el("span", { class: "ac-sp", text: "" }),
    el("span", { class: "ac-prog", text: "" }),
    el("span", { class: "ac-src", text: String(it.src || "") }),
    el("span", { class: "ac-acct-badge", text: "小号", hidden: true }),  // app.js:1211/1456-1462
  ]);
  const badge = el("span", { class: "ac-badge", text: "" });
  const actionsBox = el("div", { class: "ac-cactions" });
  const card = el("div", { class: "ac-card", dataset: { ck: ck, st: "" } }, [
    el("div", {
      class: "ac-ctop",
      onclick: function () { toggleExpand(ck); },
    }, [
      cbox,
      el("div", { class: "ac-cmain" }, [cname, cmeta]),
      badge,
      actionsBox,
    ]),
    el("div", { class: "ac-cbar" }, [el("div", { class: "ac-cfill", dataset: { state: "" } })]),
    el("div", { class: "ac-cfoot" }, [
      el("span", { class: "ac-foot-main", text: "" }),
      el("span", { class: "ac-cerr", role: "alert", hidden: true, text: "" }),
    ]),
    el("div", { class: "ac-cdetail" }, [el("p", { class: "ac-cdetail-text", text: "" })]),
  ]);
  card._acTid = tid;
  card._acCbox = cbox;
  return card;
}

function updateCard(card, tid, it, ck) {
  // 类切换（app.js:1450-1451；expanded/checked）
  card.classList.toggle("is-expanded", expandedActive.has(ck));
  card.classList.toggle("is-sel", sel && sel.state.active && selSelected.has(ck));
  const cbox = card._acCbox;
  if (cbox) {
    const on = sel && sel.state.active && selSelected.has(ck);
    cbox.setAttribute("aria-checked", on ? "true" : "false");
    const mark = cbox.querySelector(".ac-cbox-mark");
    if (mark && mark.textContent !== (on ? "✓" : "")) mark.textContent = on ? "✓" : "";
    cbox.tabIndex = sel && sel.state.active ? "0" : "-1";
  }
  // 同文不重写（app.js:1452-1455）
  setTextSafely(card.querySelector(".ac-cname"), String(it.name || ""));
  setTextSafely(card.querySelector(".ac-sp"), humanSpeed(it.speed));
  setTextSafely(card.querySelector(".ac-prog"), human(it.done) + " / " + human(it.size));
  setTextSafely(card.querySelector(".ac-src"), String(it.src || ""));
  // 徽位（app.js:1463-1466）
  const bc = activeBadgeClass(it.status);
  const badge = card.querySelector(".ac-badge");
  if (badge) {
    if (badge.textContent !== it.status) badge.textContent = it.status;
    const cls = "ac-badge " + bc;
    if (badge.className !== cls) badge.className = cls;
  }
  // 进度条（app.js:1467-1471）
  const fill = card.querySelector(".ac-cfill");
  if (fill) {
    fill.style.width = (Number(it.pct) || 0) + "%";
    const fs = bc === "done" ? "done" : bc === "fail" ? "fail" : bc === "paused" ? "paused" : "";
    if (fill.getAttribute("data-state") !== fs) fill.setAttribute("data-state", fs);
  }
  // 剩余时间（app.js:1472-1473）
  const etaSingle = it.speed ? (it.size - it.done) / it.speed : null;
  setTextSafely(card.querySelector(".ac-foot-main"),
    String(it.date || "") + " · 剩余 " + humanSec(etaSingle));
  // 错误位（app.js:1474-1486：有值更新/无值隐藏）
  const errEl = card.querySelector(".ac-cerr");
  if (errEl) {
    if (it.error) {
      if (errEl.textContent !== it.error) errEl.textContent = it.error;
      errEl.hidden = false;
    } else {
      errEl.textContent = "";
      errEl.hidden = true;
    }
  }
  // 账号标记（app.js:1456-1462：acct==="alt1" 显示「小号」）
  const ab = card.querySelector(".ac-acct-badge");
  if (ab) ab.hidden = it.acct !== "alt1";
  // 动作按钮只在状态变化时重建（app.js:1488-1492），busy 回填同渲染
  const st = String(it.status || "");
  if (card.getAttribute("data-st") !== st) {
    card.setAttribute("data-st", st);
    rebuildItemActions(card, tid, it, ck);
  } else {
    syncActionBusy(card, ck, it);
  }
  // 已缓存消息原文回填（app.js:1493-1497；未拉取留空）
  if (activeTexts.has(ck)) {
    setTextSafely(card.querySelector(".ac-cdetail-text"),
      activeTexts.get(ck) || "（该消息无文字内容）");
  }
}

// 单项动作按钮（app.js:1169-1192 activeItemButtons；glyph/title 对齐旧壳）
const ACTION_GLYPH = { resume: "▶", remove: "✕", pause: "⏸", skip: "⤼" };
const ACTION_TITLE = { resume: "继续", remove: "移除", pause: "暂停", skip: "跳过" };
function rebuildItemActions(card, tid, it, ck) {
  const box = card.querySelector(".ac-cactions");
  if (!box) return;
  box.textContent = "";
  const acts = itemButtonsFor(it.status);
  for (let i = 0; i < acts.length; i++) {
    (function (action) {
      const b = el("button", {
        class: "ac-action" + (action === "remove" ? " ac-action--danger" : ""),
        type: "button", title: ACTION_TITLE[action] || action,
        "aria-label": ACTION_TITLE[action] || action,
        onclick: function (ev) { ev.stopPropagation(); activeItem(tid, it, action, b, ck); },
      }, [el("span", { class: "ac-glyph", "aria-hidden": "true", text: ACTION_GLYPH[action] || "·" })]);
      b.dataset.action = action;
      box.append(b);
    })(acts[i]);
  }
  syncActionBusy(card, ck, it);
}
// 渲染期读 PendingOps 回填忙态（app.js:1172-1179）
function syncActionBusy(card, ck, it) {
  const box = card.querySelector(".ac-cactions");
  if (!box) return;
  const btns = box.querySelectorAll(".ac-action");
  for (let i = 0; i < btns.length; i++) {
    const b = btns[i];
    const pv = pendingOps.view(ck + ":" + b.dataset.action);
    const busy = !!pv;
    if (b.disabled !== busy) b.disabled = busy;
    b.classList.toggle("is-busy", busy);
    const title = pv ? (pv.label || ACTION_TITLE[b.dataset.action]) : (ACTION_TITLE[b.dataset.action] || "");
    if (b.getAttribute("title") !== title) b.setAttribute("title", title);
    b.setAttribute("aria-busy", busy ? "true" : "false");
  }
}

// 恢复跳过按钮（app.js:1402-1428 updateGroupResumeSkip）
function updateGroupResumeSkip(g, s) {
  const n = (s.counts && Number(s.counts.skip)) || 0;
  let btn = g.querySelector(".ac-gresume");
  if (n <= 0) {
    if (btn) btn.remove();
    return;
  }
  const pv = pendingOps.view("resumeskip:" + s.task_id);
  if (!btn) {
    if (pv) return; // busy 重建窗口：下一拍补建（不闪烁）
    btn = el("button", {
      class: "u-btn u-btn--ghost u-btn--md ac-gresume", type: "button",
      text: resumeSkipLabel(n),
      onclick: function (ev) { ev.stopPropagation(); resumeSkipped(s.task_id, btn, g); },
    });
    g.querySelector(".ac-group-head").append(btn);
  }
  // busy 回填只在态切换时写文本（spinner 动画不重启，app.js:1415-1427 同口径）
  if (pv) {
    if (!btn.classList.contains("is-busy")) {
      btn.disabled = true;
      btn.classList.add("is-busy");
      btn.textContent = "";
      btn.append(el("span", { class: "ac-spin", "aria-hidden": "true" }),
        document.createTextNode("↻ " + busyLabelFor("resumeskip")));
    }
  } else {
    const labelText = resumeSkipLabel(n);
    if (btn.classList.contains("is-busy")) {
      btn.classList.remove("is-busy");
      btn.disabled = false;
      btn.textContent = labelText;
    } else if (btn.textContent !== labelText) {
      btn.textContent = labelText;
    }
  }
}

// 文本安全写（同文不重写：app.js:1225-1229 setText 语义）
function setTextSafely(node, txt) {
  if (!node) return;
  const v = txt == null ? "" : String(txt);
  if (node.textContent !== v) node.textContent = v;
}

// ============================================================
// 十、tg 事件消费（app.js:1266-1300 onDlPhase / job_update / acct_hint）
// ============================================================
function onDlPhaseEvent(ev) {
  const detail = ev && ev.detail ? ev.detail : ev;
  const c = classifyDlPhase(detail);
  if (!c.valid || !refs) return;
  dlPhaseCache.set(detail.task_id, { phase: c.phase, text: c.text, index: c.index, total: c.total });
  let g = groupByGid(detail.task_id);
  // T49 骨架组：早期阶段先于首次轮询建组（app.js:1273-1295）；终态帧不建组
  if (!g && c.buildSkeleton) {
    g = buildGroup(detail.task_id);
    g.classList.add("is-skeleton");
    refs.list.append(g);
    renderSegFor(detail.task_id, g);
  }
  if (g) {
    g.classList.toggle("is-skeleton", false); // 轮询/后续事件补全后即非骨架
    updateGroupPhaseEl(g, c.phase, c.text, c.index, c.total);
  }
}

function onJobUpdateEvent(ev) {
  const job = ev && ev.detail ? ev.detail : ev;
  const c = classifyDownloadJob(job);
  if (c.kind === "refresh" && refs) refreshActive();
}

function onAcctHintEvent(ev) {
  const raw = ev && ev.detail ? ev.detail : ev;
  const h = normAcctHint(raw);
  if (!h || (h.reason !== "flood_wait" && h.reason !== "slow")) return; // app.js:3536-3538
  const hasAlt = altAccounts().length >= 1;
  // 已有小号降级小字（app.js:3553-3558）；无小号大条文案（index.html:123）
  hintText = hasAlt
    ? "主号下载较慢（服务器限速）：任务可切换小号或保持自动分流"
    : "当前账号下载较慢（服务器限速），登录小号可自动分流提速";
  syncHintUI();
}
function syncHintUI() {
  if (!refs) return;
  refs.hintBar.hidden = !hintText;
  setTextSafely(refs.hintTextEl, hintText);
}

// ============================================================
// 十一、选择模式（ui 桶 createSelectionController + 双动作条联动）
// ============================================================
// 选择镜像：createSelectionController 的 selected 集合不导出，但 toggle/
// selectAll/enter（clear）/exit 的全部入口均经本组包装——镜像与真集合
// 同步维护，渲染层读镜像（selSelected）零额外快照开销。
let selSelected = new Set();
let selAllKeys = [];

function selectionSetKeys(keys) {
  if (!sel) return;
  sel.setKeys(keys);
  selAllKeys = keys.slice();
}

function wrapSelToggle(ck) {
  if (!sel) return;
  if (selSelected.has(ck)) selSelected.delete(ck); else selSelected.add(ck);
  sel.toggle(ck);
}
function wrapSelClear(exit) {
  selSelected.clear();
  if (exit && sel) sel.exit(); else if (sel) sel.clear();
}
function wrapSelectAll(on) {
  selSelected.clear();
  if (on && sel) {
    for (const k of selAllKeys) selSelected.add(k);
    sel.selectAll();
  } else if (sel) {
    sel.clear();
  }
}

function toggleSelectMode() {
  if (!sel) return;
  const active = !sel.state.active;
  if (active) sel.enter(); else sel.exit();
  if (!active) selSelected.clear();
  if (refs) {
    refs.selectBtn.textContent = active ? "退出选择" : "选择";
    refs.selectBtn.setAttribute("aria-pressed", active ? "true" : "false");
  }
  syncSelUI(sel.state);
}
function selectAllToggle() {
  if (!sel) return;
  const s = sel.state;
  wrapSelectAll(!s.all);
}
function toggleSelOne(ck) {
  if (!sel || !sel.state.active) return;
  wrapSelToggle(ck);
}

// change 订阅回调：计数/动作可用门/条显隐/半全选文案/卡片勾选态
function syncSelUI(s) {
  if (!refs || !sel) return;
  const st = s || sel.state;
  const active = st.active === true;
  if (!active) selSelected.clear();   // exit/非选择态归零（防下次 enter 残留旧勾选）
  const count = active ? selSelected.size : 0;
  const anySel = active && count > 0;
  // 桌面条（app.js:1160-1163 hidden=size===0）
  refs.bulkBar.hidden = !anySel;
  setTextSafely(refs.bulkCount, selCountText(count));
  // 底部条（u-actionbar <768 fixed 悬浮；桌面形态仅桌面条，双形态互斥）
  const mobileUI = typeof window !== "undefined" && window.innerWidth < 768;
  bottomBar.setCount(count);
  bottomBar[mobileUI && anySel ? "show" : "hide"]();
  // 动作可用门（消费 selection.can()；exit 恒禁）
  for (const key of ["resume", "pause", "skip", "remove"]) {
    const ok = sel.can(key);
    bottomBar.setEnabled(key, ok);
    const b = refs.bulkBtns[key];
    if (b) b.disabled = !ok;
  }
  // 全选按钮（半选/全选两态文案；非选择模式禁用）
  const bAll = refs.selAllBtn;
  if (bAll) {
    bAll.disabled = !active || st.total === 0;
    const lbl = st.all ? "取消全选" : "全选";
    if (bAll.textContent !== lbl) bAll.textContent = lbl;
  }
  // 根节点的选择模式态类（cbox 可见性/命中区 CSS 消费）
  refs.root.classList.toggle("is-selecting", active);
  // 卡片勾选态镜像刷新（is-sel/aria-checked；O(活卡数)）
  const cards = refs.list.querySelectorAll(".ac-card");
  for (let i = 0; i < cards.length; i++) {
    const c = cards[i];
    const on = active && selSelected.has(c.getAttribute("data-ck"));
    c.classList.toggle("is-sel", on);
    const cb = c._acCbox;
    if (cb) {
      cb.setAttribute("aria-checked", on ? "true" : "false");
      const mark = cb.querySelector(".ac-cbox-mark");
      if (mark && mark.textContent !== (on ? "✓" : "")) mark.textContent = on ? "✓" : "";
      cb.tabIndex = active ? "0" : "-1";
    }
  }
}

// ============================================================
// 十二、展开与消息原文（app.js:1500-1517 toggleActiveExpand）
// ============================================================
function toggleExpand(ck) {
  const opening = !expandedActive.has(ck);
  if (opening) expandedActive.add(ck); else expandedActive.delete(ck);
  const card = refs.list.querySelector('.ac-card[data-ck="' + qAttr(ck) + '"]');
  if (card) card.classList.toggle("is-expanded", opening);
  if (opening && !activeTexts.has(ck)) {
    const r = parseCardKey(ck);
    (async function () {
      try {
        const ready = await ensureApi();                     // 布尔（thenable 不变量）
        if (!ready || !r) return;
        const res = await window.pywebview.api.get_item_text(r.tid, r.src, r.mid); // 同步直调
        if (res && res.ok) {
          activeTexts.set(ck, res.text || "");
          const dt = refs.list.querySelector('.ac-card[data-ck="' + qAttr(ck) + '"] .ac-cdetail-text');
          if (dt) dt.textContent = res.text || "（该消息无文字内容）";
        }
      } catch (e) { /* 取原文尽力（app.js:1515 静默） */ }
    })();
  }
}

// ============================================================
// 十三、单项/批量/中止/恢复（app.js:1530-1619 / 1748-1787）
// ============================================================
async function activeItem(tid, it, action, btn, ck) {
  const key = ck + ":" + action;
  if (pendingOps.has(key)) return;                      // 防重零 RPC（app.js:1532）
  const nodes = itemButtonNodes(ck, action);
  pendingOps.pend(key, { label: busyLabelFor(action), node: nodes.length ? nodes : btn, ctx: { ck: ck } });
  let r;
  try {
    const ready = await ensureApi();
    if (!ready) throw new Error("bridge not ready");
    r = await window.pywebview.api.item_action(tid, action, [{ src: it.src, mid: it.mid }]); // 同步直调
  } catch (e) {
    pendingOps.release(key);
    return showErr("条目操作失败", e);                    // app.js:1541
  }
  if (needLoginToasted(r)) { pendingOps.release(key); return; }
  if (!r || !r.ok) {
    pendingOps.release(key);
    return showErr("条目操作失败", (r && r.error) || "操作未生效"); // app.js:1545
  }
  deferRefresh(250);                                     // ACK 不撤忙，观测翻转（app.js:1548）
}
// 同 ck 同 action 的全部按钮节点（桌面/手机同卡仅一份；镜像双条批量按钮用 bulk 键）
function itemButtonNodes(ck, action) {
  const out = [];
  const card = refs.list.querySelector('.ac-card[data-ck="' + qAttr(ck) + '"] .ac-action[data-action="' + action + '"]');
  if (card) out.push(card);
  return out;
}

// 批量入口（桌面条/底部条共用；remove 走确认弹层）
let pendingBulkAction = "";
function onBulkAction(action) {
  if (!sel || !sel.state.active || sel.state.count === 0) return;
  if (action === "remove") {
    pendingBulkAction = "remove";
    const body = overlayRemove.el.querySelector(".u-overlay-body");
    if (body) body.textContent = removeConfirmText(sel.state.count); // 逐字（含句号）
    overlayRemove.open();
    return;
  }
  doBulk(action);
}

function overlayRemoveAction() {
  const a = pendingBulkAction;
  pendingBulkAction = "";
  return a || "";
}

function doBulk(action) {
  if (!sel || !sel.state.active || sel.state.count === 0) return;
  const key = "bulk:" + action;
  if (pendingOps.has(key)) return;                      // app.js:1560
  const selKeys = Array.from(selSelected);
  const nodes = [refs.bulkBtns[action], bottomBar.el.querySelector('[data-action="' + action + '"]')].filter(Boolean);
  pendingOps.pend(key, {
    label: busyLabelFor(action), node: nodes,
    ctx: { keys: selKeys.map(function (k) { return k + ":" + action; }) }, // app.js:1563
  });
  (async function () {
    const byTask = bulkGroupKeys(selKeys);               // app.js:1565-1571
    const fails = [];                                    // 逐组失败聚合（app.js:1572-1583）
    for (const tid of Object.keys(byTask)) {
      let r;
      try {
        const ready = await ensureApi();
        if (!ready) throw new Error("bridge not ready");
        r = await window.pywebview.api.item_action(tid, action, byTask[tid]); // 同步直调
      } catch (e) {
        fails.push(tid + ": " + (e && e.message ? e.message : e));
        continue;
      }
      if (needLoginToasted(r)) { fails.length = 0; break; }
      if (!r || !r.ok) fails.push(tid + ": " + ((r && r.error) || "操作未生效"));
    }
    if (fails.length) {
      pendingOps.release(key);                           // 部分失败立即撤忙（app.js:1585）
      wrapSelClear(false);                               // 失败轮同样清选择（app.js:1588）
      deferRefresh(250);                                 // +250ms 补拉（app.js:1589）
      return showErr("批量操作部分失败", fails.join("\n")); // app.js:1590
    }
    wrapSelClear(false);                                 // app.js:1593（成功撤选择；不撤忙由 reconcile 观测）
    deferRefresh(250);
  })();
}

// 中止任务（app.js:1599-1619 abortGroup；confirm→弹层逐字承接）
let pendingAbortTid = "";
let pendingAbortNodes = [];
function askAbort(tid, nodes) {
  pendingAbortTid = tid;
  pendingAbortNodes = nodes || [];
  const body = overlayAbort.el.querySelector(".u-overlay-body");
  if (body) body.textContent = abortConfirmText();       // 逐字（含换行）
  overlayAbort.open();
}
function overlayAbortConfirmed() {
  const tid = pendingAbortTid;
  pendingAbortTid = "";
  return tid;
}
function doAbort(tid) {
  const key = "abort:" + tid;
  if (pendingOps.has(key)) return;
  // 双宿主（组头按钮 + 无别的入口；nodes 记录自 askAbort）
  const groupBtn = groupByGid(tid);
  const nodes = pendingAbortNodes.length ? pendingAbortNodes
    : (groupBtn ? [groupBtn.querySelector(".ac-gabort")] : null);
  pendingAbortNodes = [];
  pendingOps.pend(key, { label: busyLabelFor("abort"), node: nodes, ctx: { tid: tid } }); // app.js:1605
  (async function () {
    let r;
    try {
      const ready = await ensureApi();
      if (!ready) throw new Error("bridge not ready");
      r = await window.pywebview.api.abort_task(tid);   // 同步直调
    } catch (e) {
      pendingOps.release(key);
      return showErr("中止任务失败", e);                  // app.js:1611
    }
    if (needLoginToasted(r)) { pendingOps.release(key); return; }
    if (!r || !r.ok) {
      pendingOps.release(key);
      return showErr("中止任务失败", (r && r.error) || "操作未生效"); // app.js:1615
    }
    deferRefresh(400);                                   // app.js:1618（墓碑观测）
  })();
}

// 恢复跳过（app.js:1748-1787 resumeSkipped；组头入口）
function resumeSkipped(tid, btn, groupNode) {
  const key = "resumeskip:" + tid;
  if (pendingOps.has(key)) return;
  const existedBefore = true;                              // 组头线上入口
  const resumeCks = [];
  const cards = groupNode.querySelectorAll(".ac-card");
  for (let i = 0; i < cards.length; i++) {
    const bd = cards[i].querySelector(".ac-badge");
    if (bd && bd.textContent === "已跳过") resumeCks.push(cards[i].getAttribute("data-ck"));
  }
  pendingOps.pend(key, {
    label: busyLabelFor("resumeskip"), busyText: "恢复中…",
    node: btn, ctx: { tid: tid, existedBefore: existedBefore, resumeCks: resumeCks },
  });
  (async function () {
    let r;
    try {
      const ready = await ensureApi();
      if (!ready) throw new Error("bridge not ready");
      r = await window.pywebview.api.resume_task(tid, false, true);  // 同步直调（app.js:1772）
    } catch (e) {
      pendingOps.release(key);
      return showErr("恢复跳过失败", e);                   // app.js:1775
    }
    const softErr = String((r && r.error) || "").includes("已在下载"); // app.js:1777
    if (!r || !r.ok) {
      if (!softErr) {
        pendingOps.release(key);
        return showErr("恢复跳过失败", (r && r.error) || "操作未生效");
      }
      // 「已在下载」：会话本就活跃，保持宽松口径（app.js:1783）
    }
    deferRefresh(300);                                     // app.js:1786
  })();
}

// ============================================================
// 十四、账号（T52 组头选号 + 标记；app.js:3113-3660 轻承接）
// ============================================================
function altAccounts() {
  return accounts.filter(function (a) { return a && a.id && a.id !== "main" && a.state !== "absent"; });
}
async function loadAccounts() {
  acctTried = true;
  try {
    const ready = await ensureApi();
    if (!ready) return;
    const r = await window.pywebview.api.account_list();  // 同步直调（app.js:3162）
    if (r && r.ok && Array.isArray(r.accounts)) accounts = r.accounts;
    else accounts = [];
  } catch (e) {
    accounts = [];                                          // 安静降级（app.js:3170-3174）
  }
  // 组头选号控件（含在线骨架组）重建
  for (const node of refs.list.querySelectorAll(".ac-group")) {
    renderSegFor(node.getAttribute("data-gid") || "", node);
  }
}

function renderSegFor(gid, g) {
  if (!gid || !g) return;
  const host = g.querySelector(".ac-acct");
  if (!host) return;
  destroySeg(gid);
  const alts = altAccounts();
  const items = [{ value: "main", label: "主号" }];        // app.js:3579
  if (alts.length) items.push({ value: alts[0].id, label: "小号" }); // app.js:3580
  items.push({ value: "auto", label: "自动" });            // app.js:3581
  const cur = taskAcct.get(gid) || "auto";
  const seg = createSegmented(items, {
    value: cur,
    ariaLabel: "本任务下载账号",
    // 仅用户点击下发 edit_task；编程式 set（本函数初始化 / pickSeg 回退
    // refreshSegValue）flags.byUser 缺省，不下发 RPC（T12 消费同款门；
    // 旧壳 renderSegs 为 innerHTML 直插无 change 事件，行为等价零误发）
    onChange: function (v, flags) {
      if (!flags || flags.byUser !== true) return;
      pickSeg(gid, v);
    },
  });
  host.textContent = "";
  host.append(seg.el);
  segs.set(gid, seg);
}
function destroySeg(gid) {
  const seg = segs.get(gid);
  if (seg) { seg.destroy(); segs.delete(gid); }
}
async function pickSeg(tid, value) {
  const prev = taskAcct.get(tid) || "auto";
  taskAcct.set(tid, value);                                // app.js:3599（乐观 UI）
  try {
    const ready = await ensureApi();
    if (!ready) throw new Error("bridge not ready");
    const r = await window.pywebview.api.edit_task(tid, { acct_pref: value }); // T51 真实字段（app.js:3613）
    if (r && r.ok === false) {
      taskAcct.set(tid, prev);                             // 拒绝回退（app.js:3615-3616）
      refreshSegValue(tid);
    }
  } catch (e) {
    taskAcct.set(tid, prev);                               // 异常回退（app.js:3619-3620）
    refreshSegValue(tid);
  }
}
function refreshSegValue(tid) {
  const seg = segs.get(tid);
  if (seg) seg.set(taskAcct.get(tid) || "auto");
}

// ============================================================
// 十五、轮询（app.js:1626-1661：1s/4s 退避、hidden 停拍、回前台补拉）
// ============================================================
function startPoll() {
  if (!pollStopped && pollTimer) return;
  pollStopped = false;
  pollTimer = setTimeout(pollTick, 200);   // 进入视图尽快首拍
}
async function pollTick() {
  if (pollStopped) return;
  let delay = 1000;
  // hidden 停拍：不发 is_active/refreshActive（app.js:1637）；setTimeout 链保持
  if (!document.hidden && !pollBusy) {
    pollBusy = true;
    try {
      const running = await pollOnce();
      if (!running) delay = 4000;          // 空闲退避（app.js:1644）
    } catch (e) {
      setOffline(true, e);                 // 轮询异常 → 断网 banner（恢复自动补拉）
    } finally {
      pollBusy = false;
    }
  }
  if (pollStopped) return;
  pollTimer = setTimeout(pollTick, delay);
}
async function pollOnce() {
  const ready = await ensureApi();
  if (!ready) return false;
  const running = await window.pywebview.api.is_active(); // 同步直调（app.js:1627）
  await refreshActive();                                   // 视图常驻（轮询随 onLeave 停摆）
  return !!running;
}

// 一次性补拉（250/300/400ms；注册 deferTimers，onLeave 全清零残留）
function deferRefresh(ms) {
  const t = setTimeout(function () {
    deferTimers.delete(t);
    refreshActive();
  }, ms);
  deferTimers.add(t);
}

// ============================================================
// 十六、断网 banner（行为需求 §6；恢复由 online 事件/下一拍成功自动收起）
// ============================================================
function setOffline(b, err) {
  offline = !!b;
  if (refs && refs.offlineBar) refs.offlineBar.hidden = !offline;
}

// ============================================================
// 十七、PTR 下拉刷新（行为需求 §5 本波基本实现；T17 手势波强化）
// ============================================================
function scrollViewTop() {
  const sc = document.querySelector("#sh-scroll");
  return sc ? sc.scrollTop : 0;
}
function isMobileViewport() {
  return typeof window !== "undefined" && window.innerWidth < 768;
}
function ptrStart(e) {
  if (!isMobileViewport()) return;
  const t = e.touches && e.touches[0];
  if (!t) return;
  ptr.touching = true;
  ptr.startY = t.clientY;
  ptrX0 = t.clientX;
  ptr.top = scrollViewTop();
  ptr.dy = 0;
}
function ptrMove(e) {
  if (!ptr.touching || !isMobileViewport()) return;
  const t = e.touches && e.touches[0];
  if (!t) return;
  ptr.dy = Math.round(t.clientY - ptr.startY);
  // T17 轴锁：横向占优让位滑动托盘（dy 熄火防本拍误触发刷新）
  if (Math.abs(t.clientX - ptrX0) > Math.abs(ptr.dy)) { ptr.dy = 0; setPtrUI(null); return; }
  if (ptr.top > 0 || ptr.dy <= 0 || ptr.firing) { setPtrUI(null); return; }
  setPtrUI(ptr.dy >= 64 ? "armed" : "pulling");
}
function ptrEnd() {
  if (!ptr.touching) return;
  ptr.touching = false;
  const dy = ptr.dy;
  ptr.dy = 0;
  if (ptr.firing) return;
  if (dy >= 64 && ptr.top <= 0) {
    ptr.firing = true;
    setPtrUI("firing");
    const done = () => { ptr.firing = false; setPtrUI(null); };
    Promise.resolve(refreshActive()).then(done, done);
  } else {
    setPtrUI(null);
  }
}
function setPtrUI(mode) {
  if (!refs) return;
  const t = refs.ptrText;
  if (!mode) {
    refs.ptrBar.hidden = true;
    return;
  }
  refs.ptrBar.hidden = false;
  const label = mode === "firing" ? "正在刷新…" : (mode === "armed" ? "释放刷新" : "下拉刷新");
  if (t.textContent !== label) t.textContent = label;
  refs.ptrBar.classList.toggle("is-armed", mode === "armed");
  refs.ptrBar.classList.toggle("is-firing", mode === "firing");
}
