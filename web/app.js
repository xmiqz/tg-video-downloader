// ============================================================
// TG 视频下载器 —— 前端逻辑（液态玻璃 GUI）
// 桥接对象：window.pywebview.api
// ============================================================
const api = () => window.pywebview.api;

// ---------- 工具 ----------
function esc(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}
function human(n) {
  n = Number(n) || 0;
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return n.toFixed(n >= 100 || i === 0 ? 0 : 1) + " " + u[i];
}
const humanSpeed = n => human(n) + "/s";
function humanSec(s) {
  if (s == null || !isFinite(s)) return "--";
  s = Math.round(s);
  const h = Math.floor(s / 3600),
        m = Math.floor((s % 3600) / 60), sec = s % 60;
  if (h) return `${h}:${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}`;
  return `${m}:${String(sec).padStart(2, "0")}`;
}
const keyOf = (src, mid) => `${src || "频道"}|${mid}`;

// ---------- 全局状态 ----------
let currentView = "new";
const opts = { mode: "all", scope: "channel", sort: "date" };
let previewData = { items: [], channel: "", title: "" };
let selected = new Set();
let taskFilter = "all";

// ---------- 视图切换 ----------
function switchView(v) {
  currentView = v;
  document.querySelectorAll(".view").forEach(el => el.classList.add("hidden"));
  document.getElementById("view-" + v).classList.remove("hidden");
  document.querySelectorAll(".nav-item").forEach(el =>
    el.classList.toggle("active", el.dataset.view === v));
  if (v === "tasks") renderTasks();
  if (v === "active") refreshActive();
  if (v === "settings") loadSettings();
}

// ============================================================
// 登录
// ============================================================
async function bootLogin() {
  const r = await api().connect();
  if (r.ok && r.stage === "ready") return afterLogin();
  document.getElementById("login").classList.remove("hidden");
  const st = await api().login_state();
  showLoginStep(st.has_session && !r.busy ? "code" : "phone");
  if (!r.ok) loginErr(r.error);
}
function showLoginStep(s) {
  ["phone", "code", "pwd"].forEach(x =>
    document.getElementById("login-step-" + x).classList.toggle("hidden", x !== s));
}
const loginBack = () => showLoginStep("phone");
const loginErr = m => (document.getElementById("login-err").textContent = m || "");

async function doSendCode() {
  loginErr("");
  const phone = document.getElementById("login-phone").value.trim();
  if (!phone) return loginErr("请输入手机号");
  const r = await api().send_code(phone);
  r.ok ? showLoginStep("code") : loginErr(r.error);
}
async function doVerifyCode() {
  loginErr("");
  const r = await api().verify_code(document.getElementById("login-code").value.trim());
  if (r.ok && r.stage === "ready") afterLogin();
  else if (r.ok && r.stage === "need_password") showLoginStep("pwd");
  else loginErr(r.error);
}
async function doVerifyPassword() {
  loginErr("");
  const r = await api().verify_password(document.getElementById("login-pwd").value);
  r.ok ? afterLogin() : loginErr(r.error);
}
async function afterLogin() {
  document.getElementById("login").classList.add("hidden");
  const m = await api().me();
  if (m.ok) {
    document.getElementById("me-name").textContent = m.name || "已登录";
    document.getElementById("me-user").textContent =
      m.username ? "@" + m.username : m.phone || "";
    document.getElementById("me-avatar").textContent = (m.name || "?").slice(0, 1);
  }
}

// ============================================================
// 新建下载 —— 步骤/高级选项
// ============================================================
function toggleAdv() {
  const btn = document.querySelector(".adv-toggle");
  const panel = document.getElementById("adv-panel");
  btn.classList.toggle("open");
  panel.classList.toggle("hidden");
}
function setMode(m) {
  opts.mode = m;
  document.querySelectorAll("#mode-seg .seg").forEach(el =>
    el.classList.toggle("active", el.dataset.m === m));
}
function setScope(s) {
  opts.scope = s;
  document.querySelectorAll("#scope-seg .seg").forEach(el =>
    el.classList.toggle("active", el.dataset.s === s));
}
function setSort(o) {
  opts.sort = o;
  document.querySelectorAll("#sort-seg .seg").forEach(el =>
    el.classList.toggle("active", el.dataset.o === o));
}
function gotoStep(n) {
  document.getElementById("wizard-1").classList.toggle("hidden", n !== 1);
  document.getElementById("wizard-3").classList.toggle("hidden", n !== 3);
  document.querySelectorAll(".steps .step").forEach((el, i) => {
    el.classList.toggle("active", i === n - 1);
    el.classList.toggle("done", i < n - 1);
  });
}

function collectSearchParams() {
  return {
    channel: document.getElementById("w-channel").value.trim(),
    query: document.getElementById("w-keywords").value.trim(),
    mode: opts.mode, scope: opts.scope, sort: opts.sort,
    limit: document.getElementById("w-limit").value || 30,
    min_mb: document.getElementById("w-minmb").value || 0,
    max_mb: document.getElementById("w-maxmb").value || 0,
    date_from: document.getElementById("w-datefrom").value || "",
    date_to: document.getElementById("w-dateto").value || "",
  };
}

async function doPreview() {
  const btn = document.getElementById("preview-btn");
  document.getElementById("wizard-err").textContent = "";
  document.getElementById("wizard-note").textContent = "";
  const params = collectSearchParams();
  if (!params.channel) return (document.getElementById("wizard-err").textContent = "请输入频道");
  btn.textContent = "搜索中…"; btn.disabled = true;
  const r = await api().preview(params);
  btn.textContent = "开始搜索"; btn.disabled = false;
  if (!r.ok) return (document.getElementById("wizard-err").textContent = r.error);
  previewData = r;
  selected = new Set(r.items.map(it => keyOf(it.src, it.mid)));
  document.getElementById("video-search").value = "";
  renderPreview();
  gotoStep(2);
  const bits = [];
  if (r.meta.n_small) bits.push(`${r.meta.n_small} 个小于下限`);
  if (r.meta.n_large) bits.push(`${r.meta.n_large} 个大于上限`);
  document.getElementById("wizard-note").textContent = bits.length
    ? `已按体积过滤：${bits.join("，")}` : "";
}

function renderPreview() {
  const q = document.getElementById("video-search").value.trim().toLowerCase();
  const rows = previewData.items.filter(it =>
    !q || it.name.toLowerCase().includes(q) || (it.text || "").toLowerCase().includes(q));
  document.getElementById("preview-list").innerHTML = rows.map(it => {
    const k = keyOf(it.src, it.mid);
    const on = selected.has(k);
    return `
      <div class="video-row ${on ? "sel" : ""}" onclick="toggleVideo('${esc(k)}')">
        <div class="cbox">${on ? "✓" : ""}</div>
        <div class="video-info">
          <div class="video-name">${esc(it.name)}</div>
          <div class="video-sub">${esc(it.date)} · ${esc((it.text || "").slice(0, 70))}</div>
        </div>
        <div class="video-size">
          <span>${human(it.size)}</span>
          <span class="src-tag">${esc(it.src)}</span>
        </div>
      </div>`;
  }).join("");
  document.getElementById("sel-count").textContent = selected.size;
}
function toggleVideo(k) {
  selected.has(k) ? selected.delete(k) : selected.add(k);
  renderPreview();
}
function selectAll(v) {
  const q = document.getElementById("video-search").value.trim().toLowerCase();
  if (v) previewData.items
    .filter(it => !q || it.name.toLowerCase().includes(q) || (it.text||"").toLowerCase().includes(q))
    .forEach(it => selected.add(keyOf(it.src, it.mid)));
  else selected.clear();
  renderPreview();
}

async function doStart() {
  if (!selected.size) return alert("请选择至少一个视频");
  const keys = [...selected].map(k => {
    const [src, mid] = k.split("|");
    return { src, mid };
  });
  const parallel = document.getElementById("w-parallel").value || 2;
  const r = await api().start_download(
    previewData.channel, previewData.title, keys, parallel
  );
  if (!r.ok) return alert(r.error);
  switchView("active");
}

// ============================================================
// 下载中
// ============================================================
const expandedActive = new Set();

async function refreshActive() {
  const s = await api().active_state();
  const running = await api().is_active();
  document.getElementById("nav-dot").classList.toggle("on", running);
  if (!s) {
    document.getElementById("active-overview").classList.add("hidden");
    document.getElementById("active-list").innerHTML = "";
    document.getElementById("active-empty").classList.remove("hidden");
    return;
  }
  document.getElementById("active-empty").classList.add("hidden");
  document.getElementById("active-overview").classList.remove("hidden");

  const pct = s.total_tot ? Math.round(s.total_bytes * 100 / s.total_tot) : 0;
  document.getElementById("ov-fill").style.width = pct + "%";
  document.getElementById("ov-pct").textContent = pct + "%";
  document.getElementById("ov-speed").textContent = humanSpeed(s.agg_speed);
  document.getElementById("ov-eta").textContent = humanSec(s.eta);
  const c = s.counts;
  document.getElementById("ov-detail").textContent =
    `完成 ${c.done} · 跳过 ${c.skip} · 失败 ${c.fail} · 进行 ${c.active} · 已用 ${humanSec(s.elapsed)}`;
  document.getElementById("active-sub").textContent = s.title;

  document.getElementById("active-list").innerHTML = s.items.map(it => {
    const done = it.status === "完成", fail = it.status === "失败";
    const bc = done ? "done" : fail ? "fail" : it.pct > 0 ? "run" : "wait";
    const fc = done ? "done" : fail ? "fail" : "";
    const k = keyOf("频道", it.mid);
    const ex = expandedActive.has(k);
    const etaSingle = it.speed ? (it.size - it.done) / it.speed : null;
    return `
      <div class="dl-card ${ex ? "expanded" : ""}">
        <div class="dl-top" onclick="toggleActiveExpand('${esc(k)}')">
          <div class="dl-name">${esc(it.name)}</div>
          <div class="dl-meta">
            <span class="sp">${humanSpeed(it.speed)}</span>
            <span>${human(it.done)} / ${human(it.size)}</span>
            <span class="badge ${bc}">${esc(it.status)}</span>
          </div>
        </div>
        <div class="dl-bar"><div class="dl-fill ${fc}" style="width:${it.pct}%"></div></div>
        <div class="dl-foot">
          <span>${esc(it.date)} · 剩余 ${humanSec(etaSingle)}</span>
          ${it.error ? `<span class="err">${esc(it.error)}</span>` : ""}
        </div>
        <div class="dl-detail-text">${esc(it.text) || "（该消息无文字内容）"}</div>
      </div>`;
  }).join("");
}
function toggleActiveExpand(k) {
  expandedActive.has(k) ? expandedActive.delete(k) : expandedActive.add(k);
  refreshActive();
}
async function abortAll() { await api().abort(); setTimeout(refreshActive, 450); }
async function skipCurrent() { await api().skip_current(); setTimeout(refreshActive, 300); }

// 常驻轮询
setInterval(async () => {
  try {
    const running = await api().is_active();
    document.getElementById("nav-dot").classList.toggle("on", running);
    if (currentView === "active") await refreshActive();
  } catch (e) {}
}, 1000);

// ============================================================
// 任务库
// ============================================================
async function renderTasks() {
  const tasks = await api().list_tasks();
  const q = document.getElementById("task-search").value.trim().toLowerCase();
  let rows = tasks.filter(t =>
    !q || (t.channel_title + t.channel).toLowerCase().includes(q));
  if (taskFilter === "unfinished") rows = rows.filter(t => t.pending + t.failed > 0);
  if (taskFilter === "done") rows = rows.filter(t => t.pending + t.failed === 0);
  if (taskFilter === "failed") rows = rows.filter(t => t.failed > 0);

  document.getElementById("task-empty").classList.toggle("hidden", rows.length > 0);
  document.getElementById("task-list").innerHTML = rows.map(t => {
    const pct = t.total ? Math.round(t.finished * 100 / t.total) : 0;
    const tags = [];
    if (t.pending) tags.push(`<span class="mini-tag">待下 ${t.pending}</span>`);
    if (t.failed) tags.push(`<span class="mini-tag fail">失败 ${t.failed}</span>`);
    if (t.skipped) tags.push(`<span class="mini-tag">跳过 ${t.skipped}</span>`);
    return `
      <div class="task-card" onclick="openSheet('${esc(t.id)}')">
        <h3>${esc(t.channel_title)}</h3>
        <div class="task-channel">@${esc(t.channel)}</div>
        <div class="tag-row">${tags.join("") || '<span class="mini-tag">全部完成</span>'}</div>
        <div class="task-bar"><div class="task-fill ${t.finished === t.total ? "full" : ""}" style="width:${pct}%"></div></div>
        <div class="task-stat"><span>${t.finished}/${t.total} 完成</span><span>${esc(t.created)}</span></div>
        <div class="task-actions">
          ${t.pending + t.failed ? `<button class="btn primary sm" onclick="quickResume('${esc(t.id)}',false)">继续</button>` : ""}
          ${t.failed ? `<button class="btn glass-btn sm" onclick="quickResume('${esc(t.id)}',true)">重试失败</button>` : ""}
          <button class="btn danger sm" onclick="quickDelete('${esc(t.id)}')">删除</button>
        </div>
      </div>`;
  }).join("");
}
function setTaskFilter(f) {
  taskFilter = f;
  document.querySelectorAll("#task-filters .seg").forEach(el =>
    el.classList.toggle("active", el.dataset.f === f));
  renderTasks();
}
async function quickResume(id, failed) {
  const r = await api().resume_task(id, failed);
  if (!r.ok) alert(r.error); else switchView("active");
}
async function quickDelete(id) {
  if (!confirm("删除任务记录？已下载视频不会被删除。")) return;
  await api().delete_task(id);
  renderTasks();
}

// ============================================================
// 任务详情滑层
// ============================================================
const sheet = {
  taskId: null, task: null, counts: null,
  filter: "all", q: "", edit: false,
  sel: new Set(), expanded: new Set(), files: {},
};

async function openSheet(taskId) {
  const r = await api().get_task(taskId);
  if (!r.ok) return;
  sheet.taskId = taskId;
  sheet.task = r.task;
  sheet.counts = r.counts;
  sheet.filter = "all"; sheet.q = ""; sheet.edit = false;
  sheet.sel = new Set(); sheet.expanded = new Set();
  toggleEdit(false, true);
  renderSheet();
  document.getElementById("sheet-mask").classList.remove("hidden");
  document.getElementById("sheet").classList.remove("hidden");
  requestAnimationFrame(() =>
    document.getElementById("sheet").classList.add("show"));
  // 批量文件状态
  const fr = await api().items_file_info(taskId);
  if (fr.ok) sheet.files = fr.items;
  renderDetailItems();
}
function closeSheet() {
  document.getElementById("sheet").classList.remove("show");
  document.getElementById("sheet-mask").classList.add("hidden");
  setTimeout(() => document.getElementById("sheet").classList.add("hidden"), 450);
  renderTasks();
}

// 在独立专业窗口中编辑本任务（TG 消息 / 时长 / 日期 / 批量管理）
async function openSheetEditor() {
  await api().open_editor(sheet.taskId);
}

function renderSheet() {
  const t = sheet.task, c = sheet.counts;
  document.getElementById("sheet-title").textContent = t.channel_title;
  document.getElementById("sheet-sub").textContent =
    `@${t.channel} · 创建 ${t.created}`;
  const pct = c.total ? Math.round(c.finished * 100 / c.total) : 0;
  document.getElementById("sheet-fill").style.width = pct + "%";
  document.getElementById("sheet-counters").innerHTML =
    `<span><b>${c.finished}</b> 完成</span>` +
    `<span><b>${c.pending}</b> 未下</span>` +
    `<span><b>${c.failed}</b> 失败</span>` +
    `<span><b>${c.skipped}</b> 跳过</span>` +
    `<span>共 <b>${c.total}</b></span>`;
  document.querySelectorAll("#sheet-status-seg .seg").forEach(el =>
    el.classList.toggle("active", el.dataset.st === sheet.filter));
  document.getElementById("detail-q").value = sheet.q;
  // 底部按钮
  const hasUn = c.pending + c.failed > 0;
  const resumeBtn = document.getElementById("sheet-resume");
  resumeBtn.textContent = c.pending > 0 ? "继续下载" : "重试失败";
  resumeBtn.disabled = !hasUn;
  resumeBtn.style.opacity = hasUn ? 1 : .4;
}
function setDetailFilter(st) { sheet.filter = st; renderDetailItems(); }

const STATUS_LABEL = {
  done: "完成", exists: "完成", pending: "未下载",
  failed: "失败", skipped: "跳过",
};

function renderDetailItems() {
  sheet.q = document.getElementById("detail-q").value.trim().toLowerCase();
  let items = sheet.task.items.map(ti => ({ ti }));
  if (sheet.filter !== "all") {
    if (sheet.filter === "done")
      items = items.filter(x => ["done", "exists"].includes(x.ti.status));
    else if (sheet.filter === "pending")
      items = items.filter(x => x.ti.status === "pending");
    else
      items = items.filter(x => x.ti.status === sheet.filter);
  }
  if (sheet.q)
    items = items.filter(x =>
      (x.ti.name + (x.ti.error || "")).toLowerCase().includes(sheet.q));

  const container = document.getElementById(sheet.edit ? "edit-items" : "detail-items");
  container.innerHTML = items.map(({ ti }) => {
    const src = ti.src || "频道";
    const k = keyOf(src, ti.msg);
    const f = sheet.files[k] || {};
    const ex = sheet.expanded.has(k);
    const ck = sheet.sel.has(k);
    const stLabel = STATUS_LABEL[ti.status] || ti.status;
    const stCls = ["done", "exists"].includes(ti.status) ? "done"
      : ti.status === "failed" ? "fail"
      : ti.status === "skipped" ? "wait" : "run";
    const checkbox = sheet.edit
      ? `<span class="mini-cbox" onclick="toggleEditSel('${esc(k)}')">${ck ? "✓" : ""}</span>`
      : "";
    // 浏览态操作
    let acts = "";
    if (!sheet.edit) {
      if (f.exists)
        acts += `<button class="mini-action" title="播放/打开文件" onclick="dPlay('${esc(src)}',${ti.msg})">▶</button>`;
      if (["pending", "failed"].includes(ti.status))
        acts += `<button class="mini-action" title="标记重新下载" onclick="dStatus('${esc(src)}',${ti.msg},'pending')">↻</button>`;
      if (ti.status !== "skipped")
        acts += `<button class="mini-action" title="标记跳过" onclick="dStatus('${esc(src)}',${ti.msg},'skipped')">⤼</button>`;
      acts += `<button class="mini-action danger" title="从任务移除" onclick="dRemove('${esc(src)}',${ti.msg})">✕</button>`;
    }
    return `
      <div class="ditem ${ck ? "checked" : ""} ${ex ? "expanded" : ""}">
        <div class="ditem-main">
          ${checkbox}
          <div class="ditem-info" onclick="toggleDetailExpand('${esc(k)}')">
            <div class="ditem-name">${esc(ti.name)}</div>
            <div class="ditem-sub">
              <span class="badge ${stCls}">${stLabel}</span>
              <span>${esc(ti.date)}</span><span>${human(ti.size)}</span>
              <span class="src-tag">${esc(src)}</span>
              ${f.partial ? "<span style='color:#ffb84d'>半成</span>" : ""}
            </div>
          </div>
          <div class="ditem-actions">${acts}</div>
        </div>
        <div class="ditem-text">
          ${esc(ti.error ? "错误：" + ti.error : "该消息无文字内容")}
        </div>
      </div>`;
  }).join("");

  if (sheet.edit) renderBulkBar();
}
function toggleDetailExpand(k) {
  sheet.expanded.has(k) ? sheet.expanded.delete(k) : sheet.expanded.add(k);
  renderDetailItems();
}

// 浏览态单条目操作
async function dPlay(src, mid) {
  await api().open_item_file(sheet.taskId, src, mid);
}
async function dStatus(src, mid, status) {
  await api().set_item(sheet.taskId, src, mid, status);
  await reloadSheet();
}
async function dRemove(src, mid) {
  if (!confirm("从任务移除该条目？本地文件不会删除。")) return;
  await api().remove_items(sheet.taskId, [{ src, mid }]);
  await reloadSheet();
}
async function reloadSheet() {
  const r = await api().get_task(sheet.taskId);
  if (r.ok) { sheet.task = r.task; sheet.counts = r.counts; }
  const fr = await api().items_file_info(sheet.taskId);
  if (fr.ok) sheet.files = fr.items;
  renderSheet(); renderDetailItems();
}

// ---------- 编辑模式 ----------
function toggleEdit(on, force) {
  sheet.edit = on;
  document.getElementById("sheet-view").classList.toggle("hidden", on);
  document.getElementById("sheet-edit").classList.toggle("hidden", !on);
  document.getElementById("sheet-edit-btn").classList.toggle("hidden", on);
  document.getElementById("sheet-done-btn").classList.toggle("hidden", !on);
  document.getElementById("sheet-resume").classList.toggle("hidden", on);
  if (on && !force) fillEditForm();
  if (on) renderDetailItems();
}
function fillEditForm() {
  const t = sheet.task;
  document.getElementById("ed-title").value = t.channel_title;
  document.getElementById("ed-folder").value = t.folder;
  document.getElementById("ed-move").checked = false;
  document.getElementById("ed-parallel").value =
    t.params && t.params.parallel ? t.params.parallel : 1;
}
async function editChooseFolder() {
  const r = await api().choose_folder();
  if (r.ok && r.path) document.getElementById("ed-folder").value = r.path;
}
async function saveEditTask() {
  const fields = {
    title: document.getElementById("ed-title").value.trim(),
    folder: document.getElementById("ed-folder").value.trim(),
    move_files: document.getElementById("ed-move").checked,
    parallel: document.getElementById("ed-parallel").value || 1,
  };
  const r = await api().edit_task(sheet.taskId, fields);
  if (!r.ok) return alert(r.error);
  await reloadSheet();
  if (r.warnings && r.warnings.length)
    alert("部分文件移动失败：\n" + r.warnings.join("\n"));
  alert("已保存");
}
function toggleEditSel(k) {
  sheet.sel.has(k) ? sheet.sel.delete(k) : sheet.sel.add(k);
  renderDetailItems();
}
function renderBulkBar() {
  const bar = document.getElementById("edit-bulk");
  bar.classList.toggle("hidden", sheet.sel.size === 0);
  document.getElementById("bulk-count").textContent = `已选 ${sheet.sel.size}`;
}
function bulkKeys() {
  return [...sheet.sel].map(k => {
    const [src, mid] = k.split("|");
    return { src, mid: Number(mid) };
  });
}
async function bulkStatus(status) {
  for (const k of bulkKeys())
    await api().set_item(sheet.taskId, k.src, k.mid, status);
  sheet.sel.clear();
  await reloadSheet();
}
async function bulkRemove() {
  if (!confirm("从任务移除选中条目？本地文件不会删除。")) return;
  await api().remove_items(sheet.taskId, bulkKeys());
  sheet.sel.clear();
  await reloadSheet();
}

// ---------- 滑层其它操作 ----------
function sheetOpenFolder() { api().open_folder(sheet.task.folder); }
async function sheetResume() {
  const failedOnly = sheet.counts.pending === 0 && sheet.counts.failed > 0;
  const r = await api().resume_task(sheet.taskId, failedOnly);
  if (!r.ok) return alert(r.error);
  closeSheetNoRefresh();
  switchView("active");
}
function closeSheetNoRefresh() {
  document.getElementById("sheet").classList.remove("show");
  document.getElementById("sheet-mask").classList.add("hidden");
  setTimeout(() => document.getElementById("sheet").classList.add("hidden"), 450);
}
async function sheetDelete() {
  if (!confirm("删除整个任务记录？已下载视频不会删除。")) return;
  await api().delete_task(sheet.taskId);
  closeSheet();
}

// ============================================================
// 追加视频
// ============================================================
const ap = { mode: "all", scope: "channel", sort: "date" };

function buildApSegments() {
  const defs = [
    ["ap-mode-seg", "m", "mode", [
      ["all", "全部词"], ["phrase", "精确短语"], ["any", "任一词"],
      ["fuzzy", "模糊"], ["regex", "正则"]]],
    ["ap-scope-seg", "s", "scope", [
      ["channel", "频道"], ["comments", "评论区"], ["both", "两者"]]],
    ["ap-sort-seg", "o", "sort", [
      ["date", "最新"], ["size", "最大"]]],
  ];
  for (const [cid, attr, field, options] of defs) {
    const box = document.getElementById(cid);
    box.innerHTML = options.map(([v, label]) =>
      `<button class="seg ${ap[field] === v ? "active" : ""}" ` +
      `data-${attr}="${v}" onclick="setAp('${field}','${v}')">${label}</button>`
    ).join("");
  }
}
function setAp(field, v) { ap[field] = v; buildApSegments(); }

function openAppend() {
  buildApSegments();
  ["ap-keywords", "ap-minmb", "ap-maxmb", "ap-datefrom", "ap-dateto"]
    .forEach(id => (document.getElementById(id).value = ""));
  document.getElementById("ap-limit").value = 30;
  document.getElementById("ap-err").textContent = "";
  document.getElementById("append-modal").classList.remove("hidden");
}
function closeAppend() { document.getElementById("append-modal").classList.add("hidden"); }

async function runAppend() {
  const btn = document.getElementById("ap-run-btn");
  document.getElementById("ap-err").textContent = "";
  const params = {
    query: document.getElementById("ap-keywords").value.trim(),
    mode: ap.mode, scope: ap.scope, sort: ap.sort,
    limit: document.getElementById("ap-limit").value || 30,
    min_mb: document.getElementById("ap-minmb").value || 0,
    max_mb: document.getElementById("ap-maxmb").value || 0,
    date_from: document.getElementById("ap-datefrom").value || "",
    date_to: document.getElementById("ap-dateto").value || "",
  };
  btn.textContent = "搜索中…"; btn.disabled = true;
  const r = await api().scan_append(sheet.taskId, params);
  btn.textContent = "搜索并追加"; btn.disabled = false;
  if (!r.ok) return (document.getElementById("ap-err").textContent = r.error);
  closeAppend();
  await reloadSheet();
  alert(`已追加 ${r.added} 个新视频` +
    (r.meta && r.meta.n_small ? `（${r.meta.n_small} 个体积过小被过滤）` : ""));
}

// ============================================================
// 设置
// ============================================================
async function loadSettings() {
  const r = await api().get_settings();
  document.getElementById("set-root").value = r.download_root;
}
async function chooseFolder() {
  const r = await api().choose_folder();
  if (r.ok && r.path) document.getElementById("set-root").value = r.path;
}
function openRoot() { api().open_folder(document.getElementById("set-root").value); }
async function saveRoot() {
  await api().set_download_root(document.getElementById("set-root").value);
  alert("已保存");
}

// ---------- 实时环境取光 ----------
// 由 Python UI 线程在帧变化时单向推送
window._setBackdrop = function (url) {
  document.getElementById("backdrop").style.backgroundImage = `url("${url}")`;
};

// ---------- 外观模式 ----------
const IS_WIN = /windows/i.test(navigator.userAgent || "");

function applyAppearance(mode) {
  // 非 Windows 无法实时采样，液态玻璃降级为静态暗
  const eff = (mode === "liquid" && !IS_WIN) ? "dark" : mode;
  document.documentElement.setAttribute(
    "data-theme", eff === "light" ? "light" : "dark");
  document.body.classList.toggle("liquid", eff === "liquid");
  if (eff !== "liquid") {
    const bd = document.getElementById("backdrop");
    if (bd) bd.style.backgroundImage = "";
  }
  document.querySelectorAll("#set-appearance-seg .seg").forEach(el =>
    el.classList.toggle("active", el.dataset.mode === mode));
}

async function setAppearance(mode) {
  try { localStorage.setItem("appearance", mode); } catch (e) {}
  applyAppearance(mode);
  try { await api().set_appearance(mode); } catch (e) {}
}

async function bootAppearance() {
  try {
    const r = await api().get_appearance();
    if (r && r.ok) {
      try { localStorage.setItem("appearance", r.mode); } catch (e) {}
      applyAppearance(r.mode);
    }
  } catch (e) {}
}

// ---------- 启动 ----------
window.addEventListener("pywebviewready", async () => {
  await bootAppearance();
  bootLogin();
});
