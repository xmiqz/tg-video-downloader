// ============================================================
// jobdock.js — T19f-B 全局下载悬浮卡（右下角通用作业进度浮层）
// 2.1 蓝本：T45/T57 TgJobProgress（app.js:2601-2934）+ 样式
// （style.css:907-1005、1475-1523）。移植进 T8 新壳：
//   - 挂 #sh-jobdock（router.js buildShellDom 末位；sh- 前缀避开
//     shell.css:43-53 旧壳隐藏名单，样式在 shell.css §T19f-B）；
//   - 事件源 = bridge.js:94-99 转发的 tg:job_update（payload 即完整
//     job 对象）；HTTP 快照 jobs_state 同步（挂载即拉 + 5s + 回前台），
//     updated_ts 单调去重/迟到丢弃与 2.1 同规则；
//   - kind 早退：search → new.js 流式进度承接、move_folder → editor.js
//     弹层承接（2.1 T48/T50 同款防双卡）；download/scan_append 等落通用卡
//     （2.1 T53b-E 口径）；
//   - 收起 = 仅藏浮层零 RPC，折叠留「N 个作业」小胶囊；终态 4s 淡出移除；
//   - 取消下载走 job_cancel（unsupported → 该卡永久隐藏按钮）；
//   - ResizeObserver 写 --job-dock-h（.vw-active 底部避让，2.1
//     style.css:1478 #active-list 同款）。
// 不变量（W4 hard 1）：对桥对象只做同步方法直调，绝不 resolve/await 桥
// 代理本身（thenable 同化会挂死）。
// ============================================================

const JOB_STATE_LABEL = {
  queued: "排队中", running: "进行中", done: "已完成",
  error: "失败", cancelled: "已取消", cancelling: "取消中…",
};
// 通用卡 kind 中文文案（缺失回退原 kind 文本；app.js:2641-2644）
const JOB_KIND_LABEL = {
  search: "搜索", download: "下载", move_folder: "移动目录",
  scan_append: "追加扫描", t45_demo: "演示作业",
};
const JOB_TERMINAL = new Set(["done", "error", "cancelled"]);
// T57 按钮文案（app.js:53-62 T57_LABELS 子集）
const JOB_BTN = { cancel: "取消下载", cancelling: "取消中…", collapse: "收起" };

let host = null;
let syncBusy = false;
let collapsed = false;
let dockRO = null;
const seen = new Map();          // job_id -> updated_ts（去重键；移除后保留，
                                 // 迟到旧事件按 updated_ts 规则丢弃、不复活）
const hiddenCancel = new Set();  // job_cancel 返回 unsupported 后永久隐藏按钮
const pendingCancel = new Set(); // 取消请求在途（防重复点击）

// —— 小工具（2.1 app.js 同义：human app.js:13-28 / qAttr app.js:1090）——
function human(n) {
  n = Number(n) || 0;
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return n.toFixed(n >= 100 || i === 0 ? 0 : 1) + " " + u[i];
}
function qAttr(v) { return String(v).replace(/["\\]/g, "\\$&"); }
function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
    return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
  });
}
function setText(card, sel, txt) {
  const el = card.querySelector(sel);
  if (el) el.textContent = txt;
}

function pctOf(job) {
  const t = Number(job.total) || 0, d = Number(job.done) || 0;
  if (t > 0) return Math.max(0, Math.min(100, d * 100 / t));
  const bt = Number(job.bytes_total) || 0, bd = Number(job.bytes_done) || 0;
  if (bt > 0) return Math.max(0, Math.min(100, bd * 100 / bt));
  return null;                    // 双未知 -> 不定进度
}

// 浮层实际高度写 CSS 变量（.vw-active 底部避让；2.1 T57 C4）
function updateDockHeight() {
  if (!host) return;
  const h = host.getBoundingClientRect().height;
  document.documentElement.style.setProperty(
    "--job-dock-h", (h > 0 ? Math.round(h) : 0) + "px");
}

// 折叠/展开（2.1 T57 C4/补3）：折叠仅留「N 个作业」小胶囊；收起零取消 RPC
function refreshMiniText() {
  if (!host) return;
  const mini = host.querySelector(".job-dock-mini");
  if (!mini) return;
  const n = host.querySelectorAll(".job-card").length;
  mini.classList.toggle("hidden", n === 0);
  mini.textContent = n === 1 ? "1 个作业" : n > 1 ? n + " 个作业" : "";
}

function collapseHost(on) {
  collapsed = !!on;
  if (!host) return;
  host.classList.toggle("collapsed", collapsed);
  let mini = host.querySelector(".job-dock-mini");
  if (collapsed && !mini) {
    mini = document.createElement("button");
    mini.type = "button";
    mini.className = "job-dock-mini";
    mini.setAttribute("aria-label", "展开作业进度浮层");
    mini.addEventListener("click", function () { collapseHost(false); });
    host.appendChild(mini);
  } else if (!collapsed && mini) {
    mini.remove();
  }
  refreshMiniText();
}

function buildCard(id) {
  const card = document.createElement("div");
  card.className = "job-card";
  card.dataset.jobId = id;
  card.innerHTML =
    '<div class="job-head">' +
    '<span class="job-kind"></span>' +
    '<span class="job-state"></span>' +
    '<button class="job-collapse-btn" type="button">' + esc(JOB_BTN.collapse) + "</button>" +
    '<button class="job-cancel-btn" type="button">' + esc(JOB_BTN.cancel) + "</button>" +
    "</div>" +
    '<div class="job-phase"><span class="ph"></span><span class="pd"></span></div>' +
    '<div class="job-bar"><div class="job-fill"></div></div>' +
    '<div class="job-count"><span class="c-items"></span><span class="c-bytes"></span></div>' +
    '<div class="job-err hidden"></div>';
  // 收起＝仅隐藏浮层（零取消 RPC），浮层下次有作业可再出现
  card.querySelector(".job-collapse-btn").addEventListener("click", function (ev) {
    ev.stopPropagation();
    collapseHost(true);
  });
  card.querySelector(".job-cancel-btn").addEventListener("click", function (ev) {
    ev.stopPropagation();
    onCancel(id, ev.currentTarget);
  });
  host.appendChild(card);
  return card;
}

function renderJob(job) {
  if (!job || typeof job.job_id !== "string" || !host) return false;
  const id = job.job_id;
  const ts = Number(job.updated_ts) || 0;
  const prev = seen.get(id);
  if (prev !== undefined) {
    if (ts < prev) return false;   // 迟到旧事件：丢弃不回退
    if (ts === prev) return false; // 同值：不重复渲染
  }
  seen.set(id, ts);

  // search 早退——新建页流式进度区承接（2.1 T48 同款防双卡）
  if (job.kind === "search") return false;
  // move_folder 早退——editor.js 移动目录弹层承接（2.1 T50 同款防双卡）
  if (job.kind === "move_folder") return false;
  // download/scan_append/t45_demo 等落通用卡（2.1 T53b-E 口径）

  let card = host.querySelector('[data-job-id="' + qAttr(id) + '"]');
  if (!card) card = buildCard(id);
  refreshMiniText();               // 折叠态胶囊文案随卡片数更新

  // download 卡根挂 data-phase（英文枚举仅作 CSS 键）；.ph 留空，中文
  // 细节由 .pd 同源直显 phase_detail（2.1 T49 口径，前端不拼第二份文案）
  card.dataset.phase = job.kind === "download" ? (job.phase || "") : "";

  const state = JOB_STATE_LABEL[job.state] ? job.state : "queued";
  setText(card, ".job-kind", JOB_KIND_LABEL[job.kind] || job.kind || "");
  const st = card.querySelector(".job-state");
  // 状态变化才重写 DOM（spinner 动画不因同值重复 set 重启；2.1 T57 C6）
  if (st.dataset.state !== state) {
    st.dataset.state = state;
    st.className = "job-state s-" + state;
    st.textContent = JOB_STATE_LABEL[state];
    if (state === "cancelling")
      st.innerHTML = '<span class="spinner"></span>' + esc(JOB_BTN.cancelling);
  }
  setText(card, ".ph", job.kind === "download" ? "" : (job.phase || ""));
  setText(card, ".pd", job.phase_detail || "");

  const pct = pctOf(job);
  const fill = card.querySelector(".job-fill");
  if (pct === null) {
    fill.className = "job-fill indet";
    fill.style.width = "";
  } else {
    fill.className = "job-fill" +
      (state === "done" ? " f-done" : state === "error" ? " f-error"
        : state === "cancelled" ? " f-cancelled" : "");
    fill.style.width = pct.toFixed(1) + "%";
  }
  const t = Number(job.total) || 0, d = Number(job.done) || 0;
  const bt = Number(job.bytes_total) || 0, bd = Number(job.bytes_done) || 0;
  setText(card, ".c-items", t > 0 ? d + "/" + t : "");
  setText(card, ".c-bytes", bt > 0 ? human(bd) + " / " + human(bt) : "");

  const errEl = card.querySelector(".job-err");
  if (job.error) {
    errEl.classList.remove("hidden");
    errEl.textContent = job.error;
  } else {
    errEl.classList.add("hidden");
  }

  // 取消按钮——真取消语义（「取消下载」）；仅 queued/running/cancelling 显示；
  // cancelling 中间态禁用+转圈「取消中…」；终态由真实收尾驱动（2.1 T57 C6）
  const btn = card.querySelector(".job-cancel-btn");
  const isCancelling = state === "cancelling";
  const showBtn = (state === "queued" || state === "running" ||
                   isCancelling) && !hiddenCancel.has(id);
  btn.style.display = showBtn ? "" : "none";
  btn.disabled = pendingCancel.has(id) || isCancelling;
  if (btn.dataset.state !== state) {
    btn.dataset.state = state;
    btn.classList.toggle("is-busy", isCancelling);
    if (isCancelling)
      btn.innerHTML = '<span class="spinner"></span>' + esc(JOB_BTN.cancelling);
    else btn.textContent = JOB_BTN.cancel;
  }

  // 终态卡片保留 4 秒后淡出移除（2.1 同款）
  if (JOB_TERMINAL.has(state) && !card.dataset.fade) {
    card.dataset.fade = "1";
    setTimeout(function () {
      card.classList.add("out");
      setTimeout(function () {
        card.remove();
        refreshMiniText();         // 卡片移除后胶囊计数同步
      }, 500);
    }, 4000);
  }
  return true;
}

async function onCancel(id, btn) {
  if (pendingCancel.has(id)) return;
  pendingCancel.add(id);
  btn.disabled = true;
  try {
    const r = await window.pywebview.api.job_cancel(id);
    if (r && r.unsupported) {
      hiddenCancel.add(id);        // 该作业卡片永久隐藏取消按钮
      btn.style.display = "none";
    }
    // ok：等待 job_update 事件把卡片置为 cancelled
  } catch (e) {
    console.debug("[jobdock] job_cancel 调用失败：", e);
  } finally {
    pendingCancel.delete(id);
  }
}

async function sync() {
  if (syncBusy) return;
  if (!(typeof window !== "undefined" && window.pywebview && window.pywebview.api)) return;
  syncBusy = true;
  try {
    const r = await window.pywebview.api.jobs_state();
    if (r && r.ok && Array.isArray(r.jobs)) r.jobs.forEach(renderJob);
  } catch (e) {
    console.debug("[jobdock] jobs_state 同步失败（tg:job_update 通道不受影响）：", e);
  } finally {
    syncBusy = false;
  }
}

let mounted = false;
export function mountJobDock() {
  if (mounted) return;
  host = document.getElementById("sh-jobdock");
  if (!host) return;
  mounted = true;
  window.addEventListener("tg:job_update", function (ev) { renderJob(ev.detail); });
  // host 高度变化（建卡/多行/折叠/移除）→ 避让变量重写（2.1 T57 C4）
  if (typeof ResizeObserver !== "undefined") {
    dockRO = new ResizeObserver(updateDockHeight);
    dockRO.observe(host);
  }
  updateDockHeight();
  sync();                                   // 挂载即拉一次快照
  setInterval(sync, 5000);                  // 之后每 5 秒轻量同步
  document.addEventListener("visibilitychange", function () {
    if (!document.hidden) sync();           // 回前台立即补一次
  });
  window.addEventListener("pywebviewready", function () { sync(); });
}
