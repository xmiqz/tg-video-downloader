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

// 让按钮在异步操作期间显示转圈+禁用并可换文案，结束后自动恢复原内容。
// 异常照常抛出，由调用方处理；重复点击会被忽略。
async function withBusy(btn, busyText, fn) {
  if (!btn) return fn();
  if (btn.dataset.busy === "1") throw new Error("操作进行中");
  const oldHTML = btn.innerHTML;
  btn.dataset.busy = "1";
  btn.disabled = true;
  btn.classList.add("is-busy");
  btn.innerHTML = `<span class="spinner"></span>` +
    (busyText != null ? esc(busyText) : "");
  try {
    return await fn();
  } finally {
    btn.classList.remove("is-busy");
    btn.disabled = false;
    delete btn.dataset.busy;
    btn.innerHTML = oldHTML;
  }
}

// ---------- 全局状态 ----------
let currentView = "new";
const opts = { mode: "all", scope: "channel", sort: "date" };
let previewData = { items: [], channel: "", title: "" };
let selected = new Set();
let taskFilter = "all";
// T46 游客（离线受限）模式：guestMode 与后端 login_state().guest 保持一致
let guestMode = false;
// 受限操作意图队列（FIFO）：{label, run}，run 为无参 async 函数（续路时直接
// 重新执行对应入口函数，此时 guestMode 已为 false，自然走到真实逻辑）
const pendingIntents = [];

// ============================================================
// T48 大频道流式搜索（preview 作业式 + search_items 增量渲染）
// ============================================================
// 唯一在途搜索作业（帧按 job_id 路由，非当前作业的帧一律忽略）
let searchJob = null;        // {job_id, streaming}
let arrivedKeys = new Set(); // 已到达 (src|mid) 键，跨帧去重（防重复行）
let searchCancelBusy = false;
// phase 中文映射：与 TUI（tg_video_dl.py SEARCH_PHASE_TEXT）同源同文
const SEARCH_PHASE_TEXT = {
  server_search: "服务端搜索",
  local_scan: "本地翻页扫描",
  enrich: "引用内容补全",
  filter: "结果整理",
};
// 引用来源标签：与 TUI REF_SOURCE_MARKS 同文
const REF_SOURCE_LABEL = { reply: "[引用]", album: "[相册]", fwd: "[转发]" };

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
  // 先显示登录卡（带"暂不登录"入口），connect 在途中用户也可立刻点它进入游客态
  document.getElementById("login").classList.remove("hidden");
  await tryConnect(false);
}
// 与 bootLogin 共享的静默连接逻辑：connect + login_state 判定。
// silent=true（游客态从 guest-bar 打开登录卡）时，connect 的非 ready 结果
// 不会把已进入主界面的用户强行锁回卡片——卡片的显隐由调用方/用户控制。
async function tryConnect(silent) {
  const r = await api().connect();
  if (r.ok && r.stage === "ready") {
    guestMode = false;
    return afterLogin();
  }
  const st = await api().login_state();
  // 后端已确认验证码发出（软成功）时直接进入输码步骤，否则按会话状态判断
  showLoginStep(
    (r.ok && r.stage === "need_code") || (st.has_session && !r.busy)
      ? "code" : "phone");
  if (r.warning) loginErr(r.warning);
  if (!r.ok) loginErr(r.error);
  return null;
}
// 游客态顶栏提示条点击：显示登录卡（phone 步、清提示），并做静默连接尝试
function openLoginFromGuest() {
  showLoginCard("");
  tryConnect(true);
}
function showLoginStep(s) {
  ["phone", "code", "pwd"].forEach(x =>
    document.getElementById("login-step-" + x).classList.toggle("hidden", x !== s));
}
const loginBack = () => showLoginStep("phone");
const loginErr = m => (document.getElementById("login-err").textContent = m || "");

// ---------- T46 游客模式 ----------
// 受限功能守卫：游客态点击受限入口时→不发任何 RPC、记意图、弹登录卡。
// 返回 true 表示已拦截（调用方应立即 return）。
function guardRestricted(label, run) {
  if (!guestMode) return false;
  pendingIntents.push({ label, run });
  showLoginCard("该功能需要登录，登录后将自动继续刚才的操作");
  return true;
}
// 通用兜底：任一 RPC 返回 need_login:true 时同样转弹卡（防漏网入口）。
function needLogin(resp) {
  if (resp && resp.need_login) {
    showLoginCard("该功能需要登录，登录后将自动继续刚才的操作");
    return true;
  }
  return false;
}
// 显示登录卡：phone 步起、清错误、可选受控提示（与登录错误文案互不残留）
function showLoginCard(notice) {
  document.getElementById("login-phone").value = "";
  document.getElementById("login-code").value = "";
  document.getElementById("login-pwd").value = "";
  showLoginStep("phone");
  loginErr("");
  document.getElementById("login-notice").textContent = notice || "";
  document.getElementById("login").classList.remove("hidden");
  if (guestMode) startGuestPoll();
}
function showGuestBar() { document.getElementById("guest-bar").classList.remove("hidden"); }
function hideGuestBar() { document.getElementById("guest-bar").classList.add("hidden"); }
// 登录状态轮询：guest 态（卡片或 guest-bar 可见期间）约 2s 一拍，
// 发现 logged_in:true && guest:false → 走 afterLogin（含续路）；
// 发现 guest:false 且未登录（如登出）→ 显卡停轮询。
let guestPollId = null;
let guestPollBusy = false;
function startGuestPoll() {
  if (guestPollId != null) return;
  guestPollId = setInterval(guestPollTick, 2000);
}
function stopGuestPoll() {
  if (guestPollId != null) {
    clearInterval(guestPollId);
    guestPollId = null;
  }
}
async function guestPollTick() {
  if (guestPollBusy || document.hidden) return;
  guestPollBusy = true;
  try {
    const st = await api().login_state();
    if (st && st.logged_in && !st.guest) {
      stopGuestPoll();
      await afterLogin();
      return;
    }
    if (st && !st.guest && !st.logged_in) {
      // 登出或会话被清除：回登录卡并停轮询（不回到游客态）
      stopGuestPoll();
      guestMode = false;
      showLoginCard("");
      return;
    }
  } catch (e) {} finally {
    guestPollBusy = false;
  }
}
// "暂不登录"按钮：游客态已激活时点击仅收卡回主界面；
// 未进入游客态时调用后端 set_guest 并切换为游客模式。
async function doGuest() {
  if (guestMode) {
    document.getElementById("login").classList.add("hidden");
    loginErr("");
    document.getElementById("login-notice").textContent = "";
    return;
  }
  let r;
  try {
    r = await api().set_guest(true);
  } catch (e) {
    return loginErr("进入游客模式失败：" + (e && e.message || e));
  }
  if (!r || !r.ok) return loginErr((r && r.error) || "进入游客模式失败");
  guestMode = true;
  pendingIntents.length = 0;      // 放弃登录：清空受限意图
  document.getElementById("login").classList.add("hidden");
  loginErr("");
  document.getElementById("login-notice").textContent = "";
  showGuestBar();
  startGuestPoll();
  switchView("tasks");            // 停在任务库（离线可浏览）
}

async function doSendCode() {
  loginErr("");
  const phone = document.getElementById("login-phone").value.trim();
  if (!phone) return loginErr("请输入手机号");
  const r = await api().send_code(phone);
  if (r.ok) {
    showLoginStep("code");
    if (r.warning) loginErr(r.warning);
  } else loginErr(r.error);
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
  guestMode = false;
  hideGuestBar();
  stopGuestPoll();
  document.getElementById("login").classList.add("hidden");
  document.getElementById("login-notice").textContent = "";
  const m = await api().me();
  if (m.ok) {
    document.getElementById("me-name").textContent = m.name || "已登录";
    document.getElementById("me-user").textContent =
      m.username ? "@" + m.username : m.phone || "";
    document.getElementById("me-avatar").textContent = (m.name || "?").slice(0, 1);
  }
  // T46 续路：按 FIFO 依次执行全部受限意图（此时 guestMode=false 真实执行）
  const intents = pendingIntents.splice(0);
  for (const it of intents) {
    try { await it.run(); }
    catch (e) { console.debug("[T46] 续路失败：", it.label, e); }
  }
}

// ---------- 退出登录（重置账号） ----------
async function doLogout() {
  try {
    if (await api().is_active())
      return alert("有下载任务正在进行，请先到「下载中」中止下载后再退出。");
  } catch (e) {}
  if (!confirm("退出当前账号？\n\n· 本地登录信息将被删除，重新登录需输入手机号与验证码\n· 任务清单与已下载视频不受影响")) return;
  const r = await api().logout();
  if (!r.ok) return alert(r.error || "退出登录失败");

  // 收起可能打开的滑层/弹层
  closeSheetNoRefresh();
  closeAppend();
  // 重置预览与向导（T48：同时取消在途搜索作业并清空流式态）
  cancelSearchJobQuietly();
  searchJob = null;
  arrivedKeys = new Set();
  previewData = { items: [], channel: "", title: "" };
  selected = new Set();
  gotoStep(1);
  // T49：清空下载阶段位缓存与终态清除计时器（logout 前已无活动下载）
  for (const t of taskPhaseTimers.values()) clearTimeout(t);
  taskPhaseTimers.clear();
  dlPhaseByTask.clear();
  // 重置侧边栏账号信息
  document.getElementById("me-name").textContent = "未登录";
  document.getElementById("me-user").textContent = "";
  document.getElementById("me-avatar").textContent = "·";
  // 显示登录界面（从手机号开始）
  guestMode = false;
  hideGuestBar();
  stopGuestPoll();
  document.getElementById("login-notice").textContent = "";
  loginErr("");
  document.getElementById("login-phone").value = "";
  document.getElementById("login-code").value = "";
  document.getElementById("login-pwd").value = "";
  showLoginStep("phone");
  document.getElementById("login").classList.remove("hidden");
  switchView("new");
  // T52：退出登录后账号区回落（清空已加载的账号列表并重拉/降级渲染）
  TgAccounts.refresh();
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
  document.getElementById("wizard-2").classList.toggle("hidden", n !== 2);
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
  if (guardRestricted("新建搜索", () => doPreview())) return;
  const btn = document.getElementById("preview-btn");
  document.getElementById("wizard-err").textContent = "";
  document.getElementById("wizard-note").textContent = "";
  const params = collectSearchParams();
  if (!params.channel) return (document.getElementById("wizard-err").textContent = "请输入频道");
  // T48：再次点开始搜索时，对旧 job_id 先取消，防后台空跑
  await cancelSearchJobQuietly();
  document.getElementById("wizard-note").textContent =
    "正在扫描频道历史消息，频道消息较多时可能需要等待几十秒…";
  let r;
  try {
    r = await withBusy(btn, "搜索中…", () => api().preview(params));
  } catch (e) { return; }
  document.getElementById("wizard-note").textContent = "";
  if (needLogin(r)) return;   // T46：need_login:true 转弹登录卡
  if (!r.ok) return (document.getElementById("wizard-err").textContent = r.error || "搜索失败");
  // T48 ACK {"ok":true,"job_id":...}：立即进入步骤 2，初始化流式态
  previewData = {
    items: [], channel: params.channel, title: params.channel,
    meta: {}, job_id: r.job_id,
  };
  selected = new Set();
  arrivedKeys = new Set();
  searchJob = { job_id: r.job_id, streaming: true };
  document.getElementById("video-search").value = "";
  resetSearchProgress();
  gotoStep(2);
  renderPreview();
}

function previewEmptyHint(meta) {
  if (!meta.candidates)
    return "没有扫描到任何消息。请确认频道用户名或链接是否正确，以及当前账号是否有权限访问该频道。";
  if (!meta.n_video)
    return `扫描到 ${meta.candidates} 条消息，但其中没有视频文件。`;
  const why = [];
  if (meta.n_small) why.push(`${meta.n_small} 个小于体积下限`);
  if (meta.n_large) why.push(`${meta.n_large} 个大于体积上限`);
  return `共扫描到 ${meta.n_video} 个视频，但都不符合当前筛选条件` +
    (why.length ? `（${why.join("，")}）` : "") +
    "。可返回上一步放宽体积、日期或关键词条件。";
}

function renderPreview() {
  const listEl = document.getElementById("preview-list");
  const q = document.getElementById("video-search").value.trim().toLowerCase();
  // T48 流式等待态：作业已 ACK 但首批结果未到——显示等待而非"没有可选择的视频"
  if (!previewData.items.length && searchJob && searchJob.streaming) {
    listEl.innerHTML = `
      <div class="empty-state">
        <div class="empty-ico">⌛</div>
        <h3>正在搜索频道历史消息…</h3>
        <p>结果会随着扫描推进持续追加，已到达的条目可立即勾选。</p>
      </div>`;
    document.getElementById("sel-count").textContent = 0;
    return;
  }
  // 搜索完成但一条结果都没有：给出具体原因，而不是空白卡住
  if (!previewData.items.length) {
    listEl.innerHTML = `
      <div class="empty-state">
        <div class="empty-ico">∅</div>
        <h3>没有可选择的视频</h3>
        <p>${esc(previewEmptyHint(previewData.meta || {}))}</p>
        <button class="btn primary" onclick="gotoStep(1)">返回修改条件</button>
      </div>`;
    document.getElementById("sel-count").textContent = 0;
    return;
  }
  const rows = previewData.items.filter(it =>
    !q || it.name.toLowerCase().includes(q) || (it.text || "").toLowerCase().includes(q));
  // 有关键词但筛不出：提示清除搜索词，而不是空白
  if (!rows.length) {
    listEl.innerHTML = `
      <div class="empty-state pad">
        <div class="empty-title">没有匹配“${esc(q)}”的视频</div>
        <button class="btn glass-btn" onclick="clearPreviewSearch()">清除搜索词</button>
      </div>`;
    document.getElementById("sel-count").textContent = selected.size;
    return;
  }
  listEl.innerHTML = rows.map(it => previewRowHtml(it)).join("");
  document.getElementById("sel-count").textContent = selected.size;
}
function toggleVideo(k) {
  selected.has(k) ? selected.delete(k) : selected.add(k);
  // T48：流式追加期间做行内更新（不全量重绘），保持滚动位置与增量行
  const row = document.querySelector(
    `#preview-list .video-row[data-key="${qAttr(k)}"]`);
  if (row) {
    const on = selected.has(k);
    row.classList.toggle("sel", on);
    row.querySelector(".cbox").textContent = on ? "✓" : "";
  }
  document.getElementById("sel-count").textContent = selected.size;
}
function clearPreviewSearch() {
  document.getElementById("video-search").value = "";
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

async function doStart(btn) {
  if (guardRestricted("开始下载", () => doStart(btn))) return;
  // 只提交"已到达且勾选"的条目（selected 只能从已到达行勾选/全选产生）
  if (!selected.size) return alert("请选择至少一个视频");
  const keys = [...selected].map(k => {
    const [src, mid] = k.split("|");
    return { src, mid };
  });
  const parallel = document.getElementById("w-parallel").value || 2;
  let r;
  try {
    r = await withBusy(btn, "准备下载…", () =>
      api().start_download(previewData.channel, previewData.title, keys,
                           parallel, "", previewData.job_id || ""));
  } catch (e) { return; }
  if (!r.ok) return alert(r.error);
  // T52：任务创建成功即下发账号策略（edit_task({acct})，T51 契约；
  // 桩模式 t52demo=1 由 probe 应答并记录入参形状）
  TgAccounts.applyNewTaskAcct(r.task_id);
  switchView("active");
}

// ============================================================
// T48 流式搜索：进度区 / search_items 增量渲染 / 取消 / 终态收敛
// ============================================================
function resetSearchProgress() {
  const box = document.getElementById("search-progress");
  box.classList.remove("hidden");
  document.getElementById("search-progress-fill").className = "job-fill indet";
  document.getElementById("search-phase-text").textContent = "正在搜索…";
  document.getElementById("search-counts").textContent = "已扫描 0 条 · 已匹配 0 条";
  const note = document.getElementById("search-note");
  note.textContent = "";
  note.classList.remove("warn", "error");
  const btn = document.getElementById("search-cancel-btn");
  btn.disabled = false;
  btn.classList.remove("hidden");
}

function hideSearchCancelBtn() {
  const btn = document.getElementById("search-cancel-btn");
  btn.disabled = true;
  btn.classList.add("hidden");
}

function previewRowHtml(it) {
  const k = keyOf(it.src, it.mid);
  const on = selected.has(k);
  const mark = (it.match_in && it.match_in !== "self")
    ? `<span class="ref-tag">${esc(REF_SOURCE_LABEL[it.match_in] || it.match_in)}</span>` : "";
  // ref_text：title 属性全文悬浮 + 行内单行截断展示
  const refAttr = it.ref_text ? ` title="${esc(it.ref_text)}"` : "";
  const refInline = it.ref_text
    ? ` <span class="ref-inline">${esc(it.ref_text.slice(0, 60))}</span>` : "";
  return `
    <div class="video-row ${on ? "sel" : ""}" data-key="${esc(k)}" onclick="toggleVideo('${esc(k)}')">
      <div class="cbox">${on ? "✓" : ""}</div>
      <div class="video-info">
        <div class="video-name">${mark}${esc(it.name)}</div>
        <div class="video-sub"${refAttr}>${esc(it.date)} · ${esc((it.text || "").slice(0, 70))}${refInline}</div>
      </div>
      <div class="video-size">
        <span>${human(it.size)}</span>
        <span class="src-tag">${esc(it.src)}</span>
      </div>
    </div>`;
}

function appendPreviewRow(it) {
  const listEl = document.getElementById("preview-list");
  // 首批到达时清掉等待态占位
  const empty = listEl.querySelector(".empty-state");
  if (empty) empty.remove();
  listEl.insertAdjacentHTML("beforeend", previewRowHtml(it));
}

// bridge.js 白名单转发的 tg:search_items（detail = params[0]）。
// 增量帧只 append 新行（按 key 去重），绝不 innerHTML 全量重绘；
// 终帧（final:true）以 data.items 做权威收敛（防极限边界错位）。
function onSearchItems(detail) {
  if (!detail || typeof detail.job_id !== "string") return;
  if (!searchJob || detail.job_id !== searchJob.job_id) return; // 非当前作业忽略
  const data = detail.data || {};
  const items = Array.isArray(data.items) ? data.items : [];
  if (data.final === true) { finalizeSearch(data); return; }
  let fresh = [];
  for (const it of items) {
    const k = keyOf(it.src, it.mid);
    if (arrivedKeys.has(k)) continue;
    arrivedKeys.add(k);
    previewData.items.push(it);
    fresh.push(it);
  }
  if (fresh.length && searchJob.streaming) {
    for (const it of fresh) appendPreviewRow(it);
    document.getElementById("sel-count").textContent = selected.size;
  }
}

function finalizeSearch(data) {
  const items = Array.isArray(data.items) ? data.items : [];
  // 权威收敛：按终帧 items 的顺序/集合重建
  previewData.items = items;
  arrivedKeys = new Set(items.map(it => keyOf(it.src, it.mid)));
  const meta = data.meta || {};
  previewData.meta = meta;
  if (meta.title) previewData.title = meta.title;
  searchJob.streaming = false;
  finishSearchProgress(meta);
  renderPreview();
}

function finishSearchProgress(meta) {
  document.getElementById("search-progress-fill").className = "job-fill f-done";
  const bits = [];
  if (meta.n_small) bits.push(`${meta.n_small} 个小于下限`);
  if (meta.n_large) bits.push(`${meta.n_large} 个大于上限`);
  if (meta.ref_matched) bits.push(`外部文本命中 ${meta.ref_matched} 条`);
  if (meta.capped) bits.push(`消息过多，仅扫描了最近 ${meta.scan_cap} 条`);
  document.getElementById("search-phase-text").textContent =
    `搜索完成 · 共 ${previewData.items.length} 条`;
  document.getElementById("search-counts").textContent = bits.join("，");
  const note = document.getElementById("search-note");
  note.classList.remove("error");
  // truncated/capped 黄行（文案同 TUI 口径）
  if (meta.truncated) {
    note.textContent = "外部文本补抓已达批次预算上限，更早消息的引用/相册文本可能未覆盖";
    note.classList.add("warn");
  } else if (meta.capped) {
    note.textContent = "本地扫描已达上限，更早的结果未覆盖：加关键词可走服务端搜索";
    note.classList.add("warn");
  } else {
    note.textContent = "";
    note.classList.remove("warn");
  }
  hideSearchCancelBtn();
}

// search 作业的 job_update（含 jobs_state 快照恢复路径）：
// running 推进度区文案/计数；cancelled/error/done 收敛终态。
function onSearchJobUpdate(job) {
  if (!job || typeof job.job_id !== "string") return;
  // 断线重连恢复（reload/重开页面后 jobs_state 快照发现仍在途的搜索作业）：
  // 重建流式态，进度区恢复阶段/计数显示与取消按钮；已到达条目页面级
  // 不可恢复（jobs_state 只恢复计数，终帧全量 items 兜底收敛）
  if (!searchJob && job.kind === "search"
      && (job.state === "running" || job.state === "queued")) {
    previewData = { items: [], channel: "", title: "", meta: {},
                    job_id: job.job_id };
    selected = new Set();
    arrivedKeys = new Set();
    searchJob = { job_id: job.job_id, streaming: true };
    resetSearchProgress();
    gotoStep(2);
    renderPreview();
  }
  if (!searchJob || job.job_id !== searchJob.job_id) return;
  const box = document.getElementById("search-progress");
  if (job.state === "running" || job.state === "queued") {
    if (!searchJob.streaming) return;
    if (box.classList.contains("hidden")) box.classList.remove("hidden");
    const phaseZh = SEARCH_PHASE_TEXT[job.phase] || job.phase || "";
    document.getElementById("search-phase-text").textContent =
      "正在搜索…" + phaseZh + (job.phase_detail ? " · " + job.phase_detail : "");
    document.getElementById("search-progress-fill").className = "job-fill indet";
    return;
  }
  if (job.state === "cancelled") {
    searchJob.streaming = false;
    document.getElementById("search-progress-fill").className = "job-fill f-cancelled";
    document.getElementById("search-phase-text").textContent = "已停止搜索";
    document.getElementById("search-counts").textContent =
      `已到达 ${previewData.items.length} 条仍可勾选下载`;
    hideSearchCancelBtn();
    return;
  }
  if (job.state === "error") {
    searchJob.streaming = false;
    document.getElementById("search-progress-fill").className = "job-fill f-error";
    document.getElementById("search-phase-text").textContent = "搜索失败";
    document.getElementById("search-counts").textContent = "";
    const note = document.getElementById("search-note");
    note.classList.remove("warn");
    note.classList.add("error");
    note.textContent = job.error || "搜索失败";
    hideSearchCancelBtn();
    return;
  }
  if (job.state === "done" && searchJob.streaming) {
    // 终帧丢失的兜底（WS 抖动）：按已到达内容收敛显示
    searchJob.streaming = false;
    finishSearchProgress(previewData.meta || {});
  }
}

async function cancelSearch() {
  if (!searchJob || !searchJob.job_id || !searchJob.streaming) return;
  if (searchCancelBusy) return;   // 在途防重复点
  searchCancelBusy = true;
  const btn = document.getElementById("search-cancel-btn");
  btn.disabled = true;
  try {
    await api().preview_cancel(searchJob.job_id);
    // 终态由 cancelled job_update 渲染
  } catch (e) {
    console.debug("[T48] preview_cancel 调用失败：", e);
    btn.disabled = false;
  } finally {
    searchCancelBusy = false;
  }
}

async function cancelSearchJobQuietly() {
  if (searchJob && searchJob.job_id && searchJob.streaming) {
    try { await api().preview_cancel(searchJob.job_id); } catch (e) {}
    searchJob.streaming = false;
  }
}

// 上一步：先取消旧作业防后台空跑，再回条件页
async function backToStep1() {
  await cancelSearchJobQuietly();
  gotoStep(1);
}

// ============================================================
// 下载中（支持多任务分组、单项控制、多选批量）
// ============================================================
const expandedActive = new Set();
const activeSel = new Set();          // 选中卡片：taskId|||src|mid
const activeTexts = new Map();        // cardKey -> 消息原文（展开时才按需拉取）
const qAttr = v => String(v).replace(/["\\]/g, "\\$&");  // 属性选择器值转义
function parseCk(ck) {
  const ix = ck.indexOf("|||");
  const rest = ck.slice(ix + 3);
  const p = rest.lastIndexOf("|");
  return [ck.slice(0, ix), rest.slice(0, p), Number(rest.slice(p + 1))];
}
const cardKey = (tid, src, mid) => `${tid}|||${src}|${mid}`;

const ACTIVE_BADGE_CLS = {
  "完成": "done", "已存在": "done", "失败": "fail",
  "已暂停": "paused", "等待中": "wait", "下载中": "run",
};

async function refreshActive() {
  const states = await api().active_states();
  const running = states.length > 0;
  document.getElementById("nav-dot").classList.toggle("on", running);
  if (!running) {
    activeSel.clear();
    document.getElementById("active-overview").classList.add("hidden");
    document.getElementById("active-bulk").classList.add("hidden");
    document.getElementById("active-list").innerHTML = "";
    document.getElementById("active-empty").classList.remove("hidden");
    return;
  }
  document.getElementById("active-empty").classList.add("hidden");
  document.getElementById("active-overview").classList.remove("hidden");

  // ---- 跨任务聚合总览 ----
  let totBytes = 0, totSize = 0, speed = 0;
  const cc = { done: 0, skip: 0, fail: 0, active: 0, paused: 0 };
  for (const s of states) {
    totBytes += s.total_bytes || 0;
    totSize += s.total_tot || 0;
    speed += s.agg_speed || 0;
    cc.done += s.counts.done || 0;
    cc.skip += s.counts.skip || 0;
    cc.fail += s.counts.fail || 0;
    cc.active += s.counts.active || 0;
    cc.paused += s.counts.paused || 0;
  }
  // T49：fetching 阶段同源展示——文案一律来自后端 phase_text 产物
  // （s.phase_detail），旧硬编码句式已删除，前端不保留第二份中文。
  const fetchDetails = [...new Set(
    states.filter(s => s.phase === "fetching")
          .map(s => s.phase_detail || "").filter(Boolean))];
  const fb = document.getElementById("active-fetch");
  fb.classList.toggle("hidden", fetchDetails.length === 0);
  if (fetchDetails.length) {
    const ftxt = fb.querySelector(".fetch-text");
    if (ftxt) ftxt.textContent = fetchDetails.join("；");
  }
  const pct = totSize ? Math.round(totBytes * 100 / totSize) : 0;
  document.getElementById("ov-fill").style.width = pct + "%";
  document.getElementById("ov-pct").textContent = pct + "%";
  document.getElementById("ov-speed").textContent = humanSpeed(speed);
  const remaining = Math.max(0, totSize - totBytes);
  document.getElementById("ov-eta").textContent =
    speed > 0 ? humanSec(remaining / speed) : "--";
  document.getElementById("ov-detail").textContent =
    `完成 ${cc.done} · 跳过 ${cc.skip} · 失败 ${cc.fail} · 进行 ${cc.active} · 暂停 ${cc.paused}`;
  document.getElementById("active-sub").textContent =
    `${states.length} 个任务下载中，按任务分组显示`;

  // ---- 批量条 ----
  document.getElementById("active-bulk")
    .classList.toggle("hidden", activeSel.size === 0);
  document.getElementById("active-bulk-count").textContent =
    `已选 ${activeSel.size}`;

  // ---- 按任务分组：增量同步 DOM（复用节点，只更新变化字段）----
  syncActiveList(states);
}

function activeItemButtons(tid, it) {
  const b = (action, glyph, title, cls = "") =>
    `<button class="mini-action ${cls}" title="${title}" ` +
    `onclick="event.stopPropagation();activeItem('${esc(tid)}',` +
    `'${esc(it.src)}',${it.mid},'${action}',this)">${glyph}</button>`;
  const st = it.status;
  if (st === "已暂停" || st === "失败")
    return b("resume", "▶", "继续") + b("remove", "✕", "移除", "danger");
  if (st === "完成" || st === "已存在" || st === "已跳过")
    return b("remove", "✕", "移除", "danger");
  // 下载中 / 等待中
  return b("pause", "⏸", "暂停") + b("skip", "⤼", "跳过") +
         b("remove", "✕", "移除", "danger");
}

function activeCard(tid, it) {
  const ck = cardKey(tid, it.src, it.mid);
  const bc = ACTIVE_BADGE_CLS[it.status] || "wait";
  const fc = bc === "done" ? "done" : bc === "fail" ? "fail"
    : bc === "paused" ? "paused" : "";
  const ex = expandedActive.has(ck);
  const on = activeSel.has(ck);
  const etaSingle = it.speed ? (it.size - it.done) / it.speed : null;
  return `
    <div class="dl-card ${ex ? "expanded" : ""} ${on ? "checked" : ""}" data-ck="${esc(ck)}">
      <div class="dl-top" onclick="toggleActiveExpand('${esc(ck)}')">
        <span class="mini-cbox" onclick="event.stopPropagation();toggleActiveSel('${esc(ck)}')">${on ? "✓" : ""}</span>
        <div class="dl-name">${esc(it.name)}</div>
        <div class="dl-meta">
          <span class="sp">${humanSpeed(it.speed)}</span>
          <span class="prog">${human(it.done)} / ${human(it.size)}</span>
          <span class="src-tag">${esc(it.src)}</span>
          <span class="badge ${bc}">${esc(it.status)}</span>
        </div>
        <div class="dl-item-actions">${activeItemButtons(tid, it)}</div>
      </div>
      <div class="dl-bar"><div class="dl-fill ${fc}" style="width:${it.pct}%"></div></div>
      <div class="dl-foot">
        <span class="foot-main">${esc(it.date)} · 剩余 ${humanSec(etaSingle)}</span>
        ${it.error ? `<span class="err">${esc(it.error)}</span>` : ""}
      </div>
      <div class="dl-detail-text"></div>
    </div>`;
}

// ---------- 增量 DOM 同步 ----------
function setText(root, sel, txt) {
  const el = root.querySelector(sel);
  if (el) el.textContent = txt;
}

// ---------- T49 下载阶段位（组头 / 任务库卡片 / dl_phase 事件） ----------
// 最新阶段缓存：task_id -> {phase, detail}（dl_phase 事件更新，
// renderTasks 重建后据此恢复在库卡片的阶段位）
const dlPhaseByTask = new Map();
// 终态条 3s 清除计时器：task_id -> timeout id（防重入）
const taskPhaseTimers = new Map();

function updateGroupPhase(g, phase, detail) {
  const el = g.querySelector(".dl-phase");
  if (!el) return;
  if (el.dataset.phase !== phase) el.dataset.phase = phase;
  const txt = el.querySelector(".dl-phase-text");
  if (txt && txt.textContent !== detail) txt.textContent = detail;
}

function updateTaskPhase(tc, phase, detail) {
  let el = tc.querySelector(".task-phase");
  if (!el) return;   // 模板缺位（异常 DOM）时静默跳过
  if (!phase) { el.classList.add("hidden"); return; }
  el.classList.remove("hidden");
  if (el.dataset.phase !== phase) el.dataset.phase = phase;
  if (el.textContent !== detail) el.textContent = detail;
  const tid = tc.dataset.tid;
  const term = phase === "done" || phase === "aborted" || phase === "error";
  if (term && tid && !taskPhaseTimers.has(tid)) {
    taskPhaseTimers.set(tid, setTimeout(async () => {
      taskPhaseTimers.delete(tid);
      try { await renderTasks(); } catch (e) {}
    }, 3000));
  }
}

// bridge.js 白名单转发的 tg:dl_phase（detail = params[0]，payload 钉死：
// {task_id, phase, phase_detail, phase_index, phase_total}）。
// 同文不重写 DOM：文本相同不触碰节点，与 1s 轮询互不打架。
function onDlPhase(detail) {
  if (!detail || typeof detail.task_id !== "string") return;
  const p = detail.phase || "";
  const d = detail.phase_detail || "";
  dlPhaseByTask.set(detail.task_id, { phase: p, detail: d });
  let g = document.querySelector(
    `#active-list .dl-group[data-gid="${qAttr(detail.task_id)}"]`);
  // T49：会话早期阶段（preparing/disk_check 数百 ms 即过）常在第一次
  // active_states 轮询建组之前到达——组未建则先落骨架组，保证组头从
  // 首帧起同源显示全部阶段；标题/计数由后续轮询 syncActiveList 补全，
  // 终态帧不建组（墓碑快照轮询自会建组显示）。
  if (!g && p && !["done", "aborted", "error"].includes(p)) {
    g = document.createElement("div");
    g.className = "dl-group";
    g.dataset.gid = detail.task_id;
    g.innerHTML = `
        <div class="dl-group-head">
          <div class="dl-group-title">
            <span class="dl-group-name"></span>
            <span class="dl-group-stat"></span>
          </div>
          <span class="dl-phase" data-phase=""><span class="dl-phase-dot"></span><span class="dl-phase-text"></span></span>
          <span class="acct-seg-host segmented" data-acct-tid="${esc(detail.task_id)}"></span>
          <button class="btn danger sm" onclick="abortGroup('${esc(detail.task_id)}',this)">中止任务</button>
        </div>
        <div class="dl-group-body"></div>`;
    document.getElementById("active-list").appendChild(g);
    // T52：dl_phase 骨架组先落位选号控件（标题/计数由轮询补全）
    TgAccounts.renderSegs();
  }
  if (g) updateGroupPhase(g, p, d);
  const tc = document.querySelector(
    `#task-list .task-card[data-tid="${qAttr(detail.task_id)}"]`);
  if (tc) updateTaskPhase(tc, p, d);
}

function syncActiveList(states) {
  const list = document.getElementById("active-list");
  const liveGroups = new Set();
  let madeNew = false;   // T52：仅新建组时需填充选号控件（1s 轮询不重建）
  for (const s of states) {
    liveGroups.add(s.task_id);
    let g = list.querySelector(`[data-gid="${qAttr(s.task_id)}"]`);
    if (!g) {
      madeNew = true;
      g = document.createElement("div");
      g.className = "dl-group";
      g.dataset.gid = s.task_id;
      g.innerHTML = `
        <div class="dl-group-head">
          <div class="dl-group-title">
            <span class="dl-group-name"></span>
            <span class="dl-group-stat"></span>
          </div>
          <span class="dl-phase" data-phase=""><span class="dl-phase-dot"></span><span class="dl-phase-text"></span></span>
          <span class="acct-seg-host segmented" data-acct-tid="${esc(s.task_id)}"></span>
          <button class="btn danger sm" onclick="abortGroup('${esc(s.task_id)}',this)">中止任务</button>
        </div>
        <div class="dl-group-body"></div>`;
      list.appendChild(g);
    }
    g.querySelector(".dl-group-name").textContent = s.title || "任务";
    g.querySelector(".dl-group-stat").textContent =
      `${s.task_finished}/${s.task_total} 完成`;
    // T49：组头阶段位同源更新（终态墓碑窗口内即"完成/已中止/失败 · detail"）
    updateGroupPhase(g, s.phase || "", s.phase_detail || "");
    syncCards(g.querySelector(".dl-group-body"), s);
  }
  for (const oldG of Array.from(list.children)) {
    if (!liveGroups.has(oldG.dataset.gid)) oldG.remove();
  }
  // T52：下载中视图组头选号控件（含新建骨架组）填充
  if (madeNew) TgAccounts.renderSegs();
}

function syncCards(body, s) {
  const live = new Set();
  for (const it of s.items) {
    const ck = cardKey(s.task_id, it.src, it.mid);
    live.add(ck);
    let card = body.querySelector(`[data-ck="${qAttr(ck)}"]`);
    if (!card) {
      const wrap = document.createElement("div");
      wrap.innerHTML = activeCard(s.task_id, it);
      card = wrap.firstElementChild;
      body.appendChild(card);   // 首次刷新时全部条目即建齐，顺序与 items 一致
    }
    updateCard(card, s.task_id, it, ck);
  }
  for (const oldC of Array.from(body.querySelectorAll(".dl-card"))) {
    if (!live.has(oldC.dataset.ck)) oldC.remove();
  }
}

function updateCard(card, tid, it, ck) {
  card.classList.toggle("expanded", expandedActive.has(ck));
  card.classList.toggle("checked", activeSel.has(ck));
  setText(card, ".dl-name", it.name);
  setText(card, ".sp", humanSpeed(it.speed));
  setText(card, ".prog", `${human(it.done)} / ${human(it.size)}`);
  setText(card, ".src-tag", it.src);
  const bc = ACTIVE_BADGE_CLS[it.status] || "wait";
  const badge = card.querySelector(".badge");
  badge.textContent = it.status;
  badge.className = `badge ${bc}`;
  const fill = card.querySelector(".dl-fill");
  fill.style.width = it.pct + "%";
  fill.className =
    "dl-fill " + (bc === "done" ? "done" : bc === "fail" ? "fail"
      : bc === "paused" ? "paused" : "");
  const etaSingle = it.speed ? (it.size - it.done) / it.speed : null;
  setText(card, ".foot-main", `${it.date} · 剩余 ${humanSec(etaSingle)}`);
  // error 有值则更新/补建，空则移除节点，避免旧报错残留显示
  const foot = card.querySelector(".dl-foot");
  let errEl = foot.querySelector(".err");
  if (it.error) {
    if (!errEl) {
      errEl = document.createElement("span");
      errEl.className = "err";
      foot.appendChild(errEl);
    }
    errEl.textContent = it.error;
  } else if (errEl) {
    errEl.remove();
  }
  // 操作按钮只在状态变化时重建（几个小节点，不必每秒重绘）
  if (card.dataset.st !== it.status) {
    card.dataset.st = it.status;
    card.querySelector(".dl-item-actions").innerHTML =
      activeItemButtons(tid, it);
  }
  // 已缓存的消息原文才回填（折叠/未拉取时留空）
  if (activeTexts.has(ck)) {
    setText(card, ".dl-detail-text",
      activeTexts.get(ck) || "（该消息无文字内容）");
  }
}

async function toggleActiveExpand(k) {
  if (guardRestricted("取消息原文", () => toggleActiveExpand(k))) return;
  const opening = !expandedActive.has(k);
  opening ? expandedActive.add(k) : expandedActive.delete(k);
  await refreshActive();
  if (opening && !activeTexts.has(k)) {
    const [tid, src, mid] = parseCk(k);
    try {
      const r = await api().get_item_text(tid, src, mid);
      if (r && r.ok) {
        activeTexts.set(k, r.text || "");
        const dt = document.querySelector(
          `.dl-card[data-ck="${qAttr(k)}"] .dl-detail-text`);
        if (dt) dt.textContent = r.text || "（该消息无文字内容）";
      }
    } catch (e) {}
  }
}
function toggleActiveSel(k) {
  activeSel.has(k) ? activeSel.delete(k) : activeSel.add(k);
  refreshActive();
}
function activeSelClear() {
  activeSel.clear();
  refreshActive();
}

async function activeItem(tid, src, mid, action, btn) {
  try {
    await withBusy(btn, null,
      () => api().item_action(tid, action, [{ src, mid }]));
  } catch (e) { return; }
  setTimeout(refreshActive, 250);
}

async function activeBulk(action, btn) {
  if (!activeSel.size) return;
  if (action === "remove" &&
      !confirm(`从任务中移除选中的 ${activeSel.size} 个条目？本地文件不会删除。`))
    return;
  const byTask = {};
  for (const ck of activeSel) {
    const ix = ck.indexOf("|||");
    const tid = ck.slice(0, ix);
    const [src, mid] = ck.slice(ix + 3).split("|");
    (byTask[tid] ||= []).push({ src, mid: Number(mid) });
  }
  const labels = { resume: "开始中…", pause: "暂停中…", skip: "跳过中…", remove: "移除中…" };
  try {
    await withBusy(btn, labels[action] || "处理中…", async () => {
      for (const [tid, ks] of Object.entries(byTask))
        await api().item_action(tid, action, ks);
    });
  } catch (e) { return; }
  activeSel.clear();
  setTimeout(refreshActive, 250);
}

async function abortGroup(tid, btn) {
  if (!confirm("中止该任务的全部下载？\n未完成视频状态保留，可随时继续。"))
    return;
  try {
    await withBusy(btn, "正在中止…", () => api().abort_task(tid));
  } catch (e) { return; }
  setTimeout(refreshActive, 400);
}

// 常驻轮询（防重叠：上一轮跑完再排下一轮；后端偶发慢响应时
// 多个 refreshActive 不会堆积，杜绝桥调用雪崩）
let activePollBusy = false;
async function activePollTick() {
  let delay = 1000;
  if (!activePollBusy) {
    activePollBusy = true;
    try {
      const running = await api().is_active();
      document.getElementById("nav-dot").classList.toggle("on", running);
      if (currentView === "active") await refreshActive();
      // 无下载且停留在其他页面时退避到 4 秒：空闲期不再每秒做一次
      // JS↔Python 桥往返持续唤醒 CPU（省电、降发热）；开始下载后前端
      // 会主动切到下载页并立即刷新，不依赖这个轮询。
      else if (!running) delay = 4000;
    } catch (e) {} finally {
      activePollBusy = false;
    }
  }
  setTimeout(activePollTick, delay);
}
activePollTick();

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

  // T52：任务账号策略种子（T51 后端在 list_tasks 携带 acct 字段时生效；
  // 用户本次会话内手动切换过的值优先，不被种子覆盖）
  tasks.forEach(t => { if (t.acct) TgAccounts.noteTaskAcct(t.id, t.acct); });

  document.getElementById("task-empty").classList.toggle("hidden", rows.length > 0);
  document.getElementById("task-list").innerHTML = rows.map(t => {
    const pct = t.total ? Math.round(t.finished * 100 / t.total) : 0;
    const tags = [];
    if (t.pending) tags.push(`<span class="mini-tag">待下 ${t.pending}</span>`);
    if (t.failed) tags.push(`<span class="mini-tag fail">失败 ${t.failed}</span>`);
    if (t.skipped) tags.push(`<span class="mini-tag">跳过 ${t.skipped}</span>`);
    return `
      <div class="task-card" data-tid="${esc(t.id)}" onclick="openSheet('${esc(t.id)}')">
        <div class="task-phase hidden" data-phase=""></div>
        <h3>${esc(t.channel_title)}</h3>
        <div class="task-channel">@${esc(t.channel)}</div>
        <div class="tag-row">${tags.join("") || '<span class="mini-tag">全部完成</span>'}</div>
        <div class="task-bar"><div class="task-fill ${t.finished === t.total ? "full" : ""}" style="width:${pct}%"></div></div>
        <div class="task-stat"><span>${t.finished}/${t.total} 完成</span><span>${esc(t.created)}</span></div>
        <div class="task-acct" onclick="event.stopPropagation()">
          <span class="task-acct-label">本任务后续下载使用</span>
          <span class="acct-seg-host segmented" data-acct-tid="${esc(t.id)}"></span>
          <span class="task-acct-note">进行中的条目不受影响</span>
        </div>
        <div class="task-actions">
          ${t.pending + t.failed ? `<button class="btn primary sm" onclick="quickResume('${esc(t.id)}',false,this)">继续</button>` : ""}
          ${t.failed ? `<button class="btn glass-btn sm" onclick="quickResume('${esc(t.id)}',true,this)">重试失败</button>` : ""}
          <button class="btn danger sm" onclick="quickDelete('${esc(t.id)}',this)">删除</button>
        </div>
      </div>`;
  }).join("");
  // T52：卡片重建后填充任务选号分段控件
  TgAccounts.renderSegs();
  // T49：重建后恢复仍在进行中的阶段位（终态不复活——滑层/切视图零残留）
  document.querySelectorAll("#task-list .task-card").forEach(tc => {
    const ph = dlPhaseByTask.get(tc.dataset.tid);
    if (ph && ph.phase && ph.phase !== "done"
        && ph.phase !== "aborted" && ph.phase !== "error") {
      updateTaskPhase(tc, ph.phase, ph.detail);
    }
  });
}
function setTaskFilter(f) {
  taskFilter = f;
  document.querySelectorAll("#task-filters .seg").forEach(el =>
    el.classList.toggle("active", el.dataset.f === f));
  renderTasks();
}
async function quickResume(id, failed, btn) {
  if (guardRestricted("继续下载", () => quickResume(id, failed, btn))) return;
  let r;
  try {
    r = await withBusy(btn, failed ? "重试中…" : "继续中…",
      () => api().resume_task(id, failed));
  } catch (e) { return; }
  if (r.ok || (r.error || "").includes("已在下载")) switchView("active");
  else alert(r.error);
}
async function quickDelete(id, btn) {
  if (!confirm("删除任务记录？已下载视频不会被删除。")) return;
  try {
    await withBusy(btn, "删除中…", () => api().delete_task(id));
  } catch (e) { return; }
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
  startSheetPoll();
}
function closeSheet() {
  stopSheetPoll();
  document.getElementById("sheet").classList.remove("show");
  document.getElementById("sheet-mask").classList.add("hidden");
  setTimeout(() => document.getElementById("sheet").classList.add("hidden"), 450);
  renderTasks();
}

// 滑层打开期间持续刷新：下载中的条目状态即时一致
let sheetPollId = null;
function startSheetPoll() {
  stopSheetPoll();
  sheetPollId = setInterval(async () => {
    if (sheet.taskId == null) return stopSheetPoll();
    try {
      const r = await api().get_task(sheet.taskId);
      if (!r.ok) return;
      sheet.task = r.task;
      sheet.counts = r.counts;
      // 正在输入搜索词时不重绘，避免打断输入
      if (document.activeElement ===
          document.getElementById("detail-q")) return;
      renderSheet();
      renderDetailItems();
    } catch (e) {}
  }, 2000);
}
function stopSheetPoll() {
  if (sheetPollId != null) {
    clearInterval(sheetPollId);
    sheetPollId = null;
  }
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
  document.querySelectorAll("#sheet-status-seg .seg").forEach(el => {
    el.classList.toggle("active", el.dataset.st === sheet.filter);
    let n = c.total;
    if (el.dataset.st === "done") n = c.finished;
    else if (el.dataset.st === "pending") n = c.pending;
    else if (el.dataset.st === "failed") n = c.failed;
    else if (el.dataset.st === "skipped") n = c.skipped;
    el.textContent = `${el.dataset.label} ${n}`;
  });
  document.getElementById("detail-q").value = sheet.q;
  // 底部按钮
  const hasUn = c.pending + c.failed > 0;
  const resumeBtn = document.getElementById("sheet-resume");
  resumeBtn.textContent = c.pending > 0 ? "继续下载" : "重试失败";
  resumeBtn.disabled = !hasUn;
  resumeBtn.style.opacity = hasUn ? 1 : .4;
}
function setDetailFilter(st) {
  sheet.filter = st;
  renderSheet();          // 同步标签高亮（此前漏调，导致 UI 无变化）
  renderDetailItems();
}

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
  // T50：保存按钮全程状态机——点击即锁；瞬时完成/失败/need_login 恢复；
  // 勾选搬运时保持锁定到作业终态（弹层"关闭"后才恢复，允许重试）。
  const btn = document.querySelector(
    '#sheet-edit .edit-block button.primary');
  const oldText = btn ? btn.textContent : "";
  const unlock = () => {
    if (btn) { btn.disabled = false; btn.textContent = oldText; }
  };
  if (btn) { btn.disabled = true; btn.textContent = "保存中…"; }
  const fields = {
    title: document.getElementById("ed-title").value.trim(),
    folder: document.getElementById("ed-folder").value.trim(),
    move_files: document.getElementById("ed-move").checked,
    parallel: document.getElementById("ed-parallel").value || 1,
  };
  let r;
  try {
    r = await api().edit_task(sheet.taskId, fields);
  } catch (e) {
    unlock();
    return alert("保存失败：" + (e && e.message || e));
  }
  if (r && r.need_login) { needLogin(r); unlock(); return; }
  if (!r || !r.ok) { unlock(); return alert((r && r.error) || "保存失败"); }
  if (r.job_id) {
    // T50：搬运异步推进——弹层到终态；done → 关弹层/解锁/刷新/提示；
    // error → 弹层内展示错误，关闭后恢复保存按钮允许重试
    T50Move.watch(r.job_id, {
      onDone: async () => {
        unlock();
        await reloadSheet();
        alert("已保存");
      },
      onClose: unlock,
    });
    return;
  }
  await reloadSheet();
  if (r.warnings && r.warnings.length)
    alert("部分文件移动失败：\n" + r.warnings.join("\n"));
  unlock();
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
  if (guardRestricted("继续下载", () => sheetResume())) return;
  const failedOnly = sheet.counts.pending === 0 && sheet.counts.failed > 0;
  // T49 防重：点击后立即禁用到 RPC 返回（双击不再触发第二个下载作业）
  const btn = document.getElementById("sheet-resume");
  const old = btn.textContent;
  btn.disabled = true;
  btn.textContent = "继续中…";
  let r;
  try {
    r = await api().resume_task(sheet.taskId, failedOnly);
  } catch (e) {
    btn.disabled = false;
    btn.textContent = old;
    return;
  }
  btn.disabled = false;
  btn.textContent = old;
  if (r.ok || (r.error || "").includes("已在下载")) {
    closeSheetNoRefresh();
    switchView("active");
  } else alert(r.error);
}
function closeSheetNoRefresh() {
  stopSheetPoll();
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
  if (guardRestricted("搜索并追加", () => openAppend())) return;
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
  let r;
  try {
    r = await api().scan_append(sheet.taskId, params);
  } catch (e) {
    btn.textContent = "搜索并追加"; btn.disabled = false;
    return (document.getElementById("ap-err").textContent =
      (e && e.message) || String(e));
  }
  btn.textContent = "搜索并追加"; btn.disabled = false;
  if (needLogin(r)) return;   // T46 兜底：漏网入口转为登录弹卡
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
  // T52：账号区（主号卡/小号列表/添加入口）由 TgAccounts 渲染；
  // 正常路径 account_list RPC（T51 契约，未接入前安静降级），
  // 桩模式 t52demo=1 读 window.__tgAcctProbe，不发任何 HTTP。
  TgAccounts.refresh();
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

// ============================================================
// ===== T45 作业事件总线 / 通用进度组件（开始）=====
// 契约（钉死）：
//   - bridge.js 把 job_update/search_items/dl_phase/acct_hint 转发为
//     window 上的 tg:<name> CustomEvent（detail = payload）；
//     job_update 的 payload 即完整 job 对象（根即 job）。
//   - 事件与 HTTP 快照（jobs_state）按 job_id 同键去重：
//     updated_ts 单调比较合并，旧/同等更新不重渲染、计数不翻倍。
//   - 既有 active_states()/is_active() setTimeout 轮询保留为降级通道
//     （上方原逻辑零改动）；本组件的 HTTP 同步独立运行，互不打架。
//   - 测试探针 window.__tgJobProbe（仅计数/时间戳，无敏感信息；
//     随 t45_demo_job 测试挂点一并保留，默认不影响生产，待组长裁决）。
// ============================================================
const TgBus = (() => {
  // 包装映射保证 on/off 对称；listener 收到的 detail 即 payload
  const wrap = new WeakMap();
  const bus = {
    on(name, fn) {
      const w = (ev) => fn(ev.detail);
      wrap.set(fn, w);
      window.addEventListener("tg:" + name, w);
    },
    off(name, fn) {
      const w = wrap.get(fn);
      if (w) window.removeEventListener("tg:" + name, w);
    },
    emit(name, detail) {
      window.dispatchEvent(new CustomEvent("tg:" + name, { detail }));
    },
  };
  return bus;
})();
// 契约（钉死）：全局 window.TgBus（顶层 const 不挂 window，需显式暴露）
window.TgBus = TgBus;

const JOB_STATE_LABEL = {
  queued: "排队中", running: "进行中", done: "已完成",
  error: "失败", cancelled: "已取消",
};
const JOB_TERMINAL = new Set(["done", "error", "cancelled"]);

const TgJobProgress = (() => {
  let host = null;
  let syncBusy = false;
  let demoTried = false;
  let dropped = 0;
  const seen = new Map();         // job_id -> updated_ts（去重键，移除后保留，
                                  // 迟到旧事件按 updated_ts 规则丢弃、不复活）
  const renderCount = new Map();  // job_id -> 实际渲染次数
  const hiddenCancel = new Set(); // job_cancel 返回 unsupported 后永久隐藏按钮
  const pendingCancel = new Set();// 取消请求在途（防重复点击）

  // 最小取证探针（测试专用，随挂点保留待组长裁决）
  const probe = {
    jobs: seen,
    get drops() { return dropped; },
    renders: renderCount,
  };
  window.__tgJobProbe = probe;

  function pctOf(job) {
    const t = Number(job.total) || 0, d = Number(job.done) || 0;
    if (t > 0) return Math.max(0, Math.min(100, d * 100 / t));
    const bt = Number(job.bytes_total) || 0, bd = Number(job.bytes_done) || 0;
    if (bt > 0) return Math.max(0, Math.min(100, bd * 100 / bt));
    return null;                    // 双未知 -> 不定进度
  }

  function buildCard(id) {
    const card = document.createElement("div");
    card.className = "job-card";
    card.dataset.jobId = id;
    card.innerHTML = `
      <div class="job-head">
        <span class="job-kind"></span>
        <span class="job-state"></span>
        <button class="job-cancel-btn" type="button">取消</button>
      </div>
      <div class="job-phase"><span class="ph"></span><span class="pd"></span></div>
      <div class="job-bar"><div class="job-fill"></div></div>
      <div class="job-count"><span class="c-items"></span><span class="c-bytes"></span></div>
      <div class="job-err hidden"></div>`;
    card.querySelector(".job-cancel-btn").addEventListener("click", (ev) => {
      ev.stopPropagation();
      onCancel(id, ev.currentTarget);
    });
    host.appendChild(card);
    return card;
  }

  function renderJob(job) {
    if (!job || typeof job.job_id !== "string") return false;
    const id = job.job_id;
    const ts = Number(job.updated_ts) || 0;
    const prev = seen.get(id);
    if (prev !== undefined) {
      if (ts < prev) { dropped++; return false; }  // 迟到旧事件：丢弃不回退
      if (ts === prev) return false;               // 同值：不重复渲染
    }
    seen.set(id, ts);

    // T48：search 作业早退——不渲染通用悬浮卡（新建页进度区承接，
    // 避免双卡）；事件/快照转交进度区恢复（断线重连补计数与终态）。
    // t45_demo 等其它 kind 行为不变（T45 回归）。
    if (job.kind === "search") {
      onSearchJobUpdate(job);
      return false;
    }

    // T50：move_folder 作业早退——进度由 T50Move 弹层承接（一窗不渲染
    // 通用悬浮卡，避免双卡）；未 watch 时帧丢弃（watch 的 jobs_state
    // 兜底会补最新状态）。其它 kind 行为零变化。
    if (job.kind === "move_folder") {
      if (window.T50Move && T50Move.onJob) T50Move.onJob(job);
      return false;
    }

    let card = host.querySelector(`[data-job-id="${qAttr(id)}"]`);
    if (!card) card = buildCard(id);

    // T49：download 作业走通用卡——卡片根挂 data-phase（英文枚举仅作
    // CSS/data 属性键）供微动效与各阶段配色；.ph 留空（中文细节由
    // 后端 phase_text 产物经 .pd 同源直显，前端不拼第二份文案）
    card.dataset.phase = job.kind === "download" ? (job.phase || "") : "";

    const state = JOB_STATE_LABEL[job.state] ? job.state : "queued";
    setText(card, ".job-kind", job.kind || "");
    const st = card.querySelector(".job-state");
    st.textContent = JOB_STATE_LABEL[state];
    st.className = "job-state s-" + state;
    setText(card, ".ph",
      job.kind === "download" ? "" : (job.phase || ""));
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
    setText(card, ".c-items", t > 0 ? `${d}/${t}` : "");
    setText(card, ".c-bytes", bt > 0 ? `${human(bd)} / ${human(bt)}` : "");

    const errEl = card.querySelector(".job-err");
    if (job.error) {
      errEl.classList.remove("hidden");
      errEl.textContent = job.error;
    } else {
      errEl.classList.add("hidden");
    }

    // 取消按钮：仅 queued/running 显示；unsupported 后永久隐藏；在途禁用
    const btn = card.querySelector(".job-cancel-btn");
    const showBtn = (state === "queued" || state === "running")
      && !hiddenCancel.has(id);
    btn.style.display = showBtn ? "" : "none";
    btn.disabled = pendingCancel.has(id);

    // 终态卡片保留 4 秒后淡出移除
    if (JOB_TERMINAL.has(state) && !card.dataset.fade) {
      card.dataset.fade = "1";
      setTimeout(() => {
        card.classList.add("out");
        setTimeout(() => card.remove(), 500);
      }, 4000);
    }
    renderCount.set(id, (renderCount.get(id) || 0) + 1);
    return true;
  }

  async function onCancel(id, btn) {
    if (pendingCancel.has(id)) return;
    pendingCancel.add(id);
    btn.disabled = true;
    try {
      const r = await api().job_cancel(id);
      if (r && r.unsupported) {
        hiddenCancel.add(id);     // 该作业卡片永久隐藏取消按钮
        btn.style.display = "none";
      }
      // ok：等待 job_update 事件把卡片置为 cancelled
    } catch (e) {
      console.debug("[T45] job_cancel 调用失败：", e);
    } finally {
      pendingCancel.delete(id);
    }
  }

  async function sync() {
    if (syncBusy) return;
    syncBusy = true;
    try {
      const r = await api().jobs_state();
      if (r && r.ok && Array.isArray(r.jobs)) {
        r.jobs.forEach(renderJob);
        maybeAutoDemo();
      }
    } catch (e) {
      console.debug("[T45] jobs_state 同步失败（HTTP 降级通道不受影响）：", e);
    } finally {
      syncBusy = false;
    }
  }

  function maybeAutoDemo() {
    if (demoTried) return;
    demoTried = true;   // 只判断一次；失败也仅 console.debug（门控默认关闭）
    if (!/[#&]t45demo=1/.test(location.hash)) return;
    api().t45_demo_job(8000, 20, true)
      .then(r => { if (!r || !r.ok) console.debug("[T45] 演示作业未启动：", r); })
      .catch(e => console.debug("[T45] 演示作业启动失败（测试门控未开启？）：", e));
  }

  function mount() {
    host = document.getElementById("job-progress-host");
    if (!host) return;
    TgBus.on("job_update", renderJob);
    sync();                                   // 挂载即拉一次快照
    setInterval(sync, 5000);                  // 之后每 5 秒轻量同步
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) sync();           // 回前台立即补一次
    });
  }

  return { mount, sync, render: renderJob };
})();

// T45 启动接线：新增独立 pywebviewready 监听（与上方既有启动监听互不干扰）
window.addEventListener("pywebviewready", () => {
  TgJobProgress.mount();
  // T48：全局只挂一次 search_items 订阅（帧按 job_id 路由，见 onSearchItems）
  TgBus.on("search_items", onSearchItems);
  // T49：全局只挂一次 dl_phase 订阅（帧按 task_id 路由，见 onDlPhase）
  TgBus.on("dl_phase", onDlPhase);
  // T52：全局只挂一次 acct_hint 订阅 + 账号数据源初拉
  //（正常路径 account_list RPC / 桩模式 t52demo=1 读 __tgAcctProbe）
  TgAccounts.mount();
});
// 组件对象也暴露到 window（供后续任务与控制台测试驱动；名字唯一无冲突）
window.TgJobProgress = TgJobProgress;
// ===== T45 作业事件总线 / 通用进度组件（结束）=====

// ============================================================
// ===== T50 移动目录进度弹层（开始）=====
// 移动 job（kind=move_folder）专用遮罩弹层：文件名行/[i/n]/字节/百分比
// 实时渲染；运行中无取消按钮、无关闭 X、点遮罩不关（跨卷半态无法安全
// 回滚，作业不可取消）；done 自动收层回调 onDone，error 展示错误全文 +
// 固定提示行，仅"关闭"按钮可收层并回调 onClose（保存按钮恢复可重试）。
// 事件源：tg:job_update（bridge 转发）+ watch 开始时 jobs_state 快照
// 兜底（防漏帧；快照/事件按 updated_ts 去重，直喂不经通用卡 seen 表）。
// ============================================================
const T50Move = (() => {
  let cur = null;   // {jobId, dom, lastTs, ended, onDone, onClose, handler}

  function buildDom() {
    const mask = document.createElement("div");
    mask.className = "t50-move-mask";
    const box = document.createElement("div");
    box.className = "t50-move-layer";
    box.innerHTML = `
      <div class="t50-move-title">正在移动文件…</div>
      <div class="t50-move-name">准备移动…</div>
      <div class="t50-move-count"></div>
      <div class="t50-move-bar"><div class="t50-move-fill"></div></div>
      <div class="t50-move-bytes"></div>
      <div class="t50-move-err hidden">
        <div class="t50-move-err-text"></div>
        <div class="t50-move-err-hint">可再次执行，已存在文件自动跳过</div>
        <button class="btn primary t50-move-close" type="button">关闭</button>
      </div>`;
    mask.appendChild(box);
    document.body.appendChild(mask);
    return {
      mask, box,
      name: box.querySelector(".t50-move-name"),
      count: box.querySelector(".t50-move-count"),
      fill: box.querySelector(".t50-move-fill"),
      bytes: box.querySelector(".t50-move-bytes"),
      err: box.querySelector(".t50-move-err"),
      errText: box.querySelector(".t50-move-err-text"),
      closeBtn: box.querySelector(".t50-move-close"),
    };
  }

  function render(job) {
    const d = Number(job.done) || 0, t = Number(job.total) || 0;
    const bd = Number(job.bytes_done) || 0, bt = Number(job.bytes_total) || 0;
    cur.dom.name.textContent = job.phase_detail || "";
    cur.dom.count.textContent = t > 0 ? `${d}/${t}` : "";
    cur.dom.bytes.textContent = bt > 0
      ? `${human(bd)} / ${human(bt)}` : "";
    const pct = bt > 0 ? Math.max(0, Math.min(100, bd * 100 / bt)) : 0;
    cur.dom.fill.style.width = pct.toFixed(1) + "%";
  }

  function teardown() {
    if (!cur) return;
    TgBus.off("job_update", cur.handler);
    if (cur.dom && cur.dom.mask && cur.dom.mask.parentNode)
      cur.dom.mask.remove();
    cur = null;
  }

  function handle(job) {
    if (!cur || !job || job.job_id !== cur.jobId) return;
    const ts = Number(job.updated_ts) || 0;
    if (cur.lastTs !== null && ts <= cur.lastTs) return;   // 迟到/同值帧
    cur.lastTs = ts;
    render(job);
    if (job.state === "done" && !cur.ended) {
      cur.ended = true;
      const cb = cur.onDone;
      teardown();                       // 收层并移除监听（后续事件幂等）
      if (cb) cb();
    } else if ((job.state === "error" || job.state === "cancelled")
               && !cur.ended) {
      cur.ended = true;
      cur.dom.err.classList.remove("hidden");
      cur.dom.errText.textContent = job.error || "移动失败";
    }
  }

  function onJob(job) {
    // TgJobProgress renderJob 早路由转入（jobs_state 快照同步同源）
    if (!cur || !job || job.job_id !== cur.jobId) return false;
    handle(job);
    return true;
  }

  function watch(jobId, cbs) {
    if (cur) teardown();                // 终态外重复 watch：旧层先终结
    const dom = buildDom();
    cur = { jobId, dom, lastTs: null, ended: false,
            onDone: cbs.onDone, onClose: cbs.onClose };
    cur.handler = (job) => {
      if (cur && job && job.job_id === cur.jobId) handle(job);
    };
    TgBus.on("job_update", cur.handler);
    dom.closeBtn.addEventListener("click", () => {
      teardown();
      if (cbs.onClose) cbs.onClose();
    });
    // 重连/漏帧兜底：watch 开始即拉一次快照直喂（终态后仍幂等）
    api().jobs_state().then((r) => {
      if (r && r.ok && Array.isArray(r.jobs) && cur
          && cur.jobId === jobId) {
        const mine = r.jobs.find((j) => j.job_id === jobId);
        if (mine) handle(mine);
      }
    }).catch(() => {});
  }

  return { watch, onJob };
})();
window.T50Move = T50Move;
// ===== T50 移动目录进度弹层（结束）=====

// ============================================================
// ===== T46 游客模式（开始）=====
// 最小取证探针（仅状态/计数，无敏感信息；风格对齐 T45 __tgJobProbe）
// 用途：TR-46.2B/46.3 断言 guest 状态机与意图队列行为
window.__tgGuestProbe = {
  get guestMode() { return guestMode; },
  get intentCount() { return pendingIntents.length; },
  get intents() { return pendingIntents.map(i => i.label); },
  get guestBarVisible() {
    const el = document.getElementById("guest-bar");
    return !!el && !el.classList.contains("hidden");
  },
  get loginVisible() {
    return !document.getElementById("login").classList.contains("hidden");
  },
  get pendingFifo() { return pendingIntents.map(i => i.label); },
};
// ===== T46 游客模式（结束）=====

// ============================================================
// ===== T52 多账号前端（开始）=====
// 组成：账号数据源 + 设置页账号区（主号卡/小号列表/添加三步卡/移除
//       二次确认）+ 任务选号分段控件（新建页/任务库卡片/下载中组头）
//       + tg:acct_hint 限速提示（大条可关闭 / 已有小号降级小字行）。
//
// 契约（T51 冻结名，正常路径按冻结名调用；本任务以桩数据接通）：
//   RPC  account_list() -> {ok, accounts:[{id:"main"|"alt1"|…,
//        label, phone, me:{username|first_name},
//        state:"ready"|"connecting"|"absent"}]}
//        account_add_start(account_id, phone) -> {ok} | {ok:false, stage:…}
//        account_verify_code(account_id, code) -> {ok} |
//          {ok:false, stage:"need_password"} | {ok:false, error:…}
//        account_verify_password(account_id, password) -> {ok} | {ok:false,…}
//        account_remove(account_id) -> {ok} | {ok:false, busy:true}
//        edit_task(task_id, {acct:"main"|"alt1"|"auto"})
//        （edit_task 已在 RPC 白名单；acct 字段扩展属 T51 后端，
//         当前后端忽略未知字段并返回 ok，不产生错误）
//   事件 tg:acct_hint（bridge.js 白名单已含 acct_hint）detail：
//        {account_id, reason:"flood_wait"|"slow",
//         suggestion:"add_alt"|"switch_alt", detail}
//
// 桩模式：URL hash 含 t52demo=1 时（门控与 T45 t45demo 同级、默认
//   关闭），账号数据源与账号模块 RPC 改读 window.__tgAcctProbe
//   （测试注入：accounts 数组 / tasks_acct 种子 / busy 标志 /
//   rpc(method, params) 应答并记录入参），对 account_* 与
//   edit_task(acct) 不产生任何 bridge/HTTP 调用；probe 未注入时按
//   空态渲染，同样不发 HTTP。无 hash 时桩代码零副作用（只走 api()
//   正常路径）。桩拆除点登记于 EV\seam_checklist.md，发版前 T54 审计。
// ============================================================
const TgAccounts = (() => {
  let _accounts = [];               // account_list 结果（主号+小号）
  let _source = "none";             // none | rpc | stub（降级口径）
  let _busy = false;                // 退出登录 busy（桩模式经 probe.busy）
  let _hint = null;                 // 最近一条 acct_hint
  const _hintDismissed = new Set(); // 用户已手动关闭的 reason（不再弹大条）
  const taskAcct = new Map();       // task_id -> "main"|"alt1"|"auto"
  let newTaskAcct = "auto";         // 新建任务页选号（创建成功后随 edit_task 下发）
  let addingId = null;              // 添加小号流程中的目标 account_id
  let removingId = null;            // 移除小号二次确认中的 account_id

  // ---------- 桩门控（与 T45 maybeAutoDemo 的 t45demo 同级）----------
  const stubOn = () => /[#&]t52demo=1/.test(location.hash);

  // 账号模块 RPC：桩模式经 probe（零 HTTP）；正常路径经 bridge api()
  function acctCall(method, ...params) {
    if (stubOn()) {
      const probe = window.__tgAcctProbe;
      if (probe && typeof probe.rpc === "function")
        return Promise.resolve(probe.rpc(method, params));
      return Promise.resolve({ ok: false, error: "测试桩未就绪" });
    }
    return api()[method](...params);
  }

  // ---------- 数据源 ----------
  async function refresh() {
    if (stubOn()) {
      // 桩模式：零 bridge/HTTP；probe 未注入按空态渲染
      const probe = window.__tgAcctProbe;
      _accounts = (probe && Array.isArray(probe.accounts)) ? probe.accounts : [];
      _source = "stub";
      _busy = !!(probe && probe.busy);
      if (probe && probe.tasks_acct)
        for (const k of Object.keys(probe.tasks_acct))
          if (!taskAcct.has(k)) taskAcct.set(k, probe.tasks_acct[k]);
    } else {
      // 正常路径：T51 冻结契约 account_list。后端未接入前返回 400
      //（未知方法），此处安静降级为空态（console.debug，不抛错）
      try {
        const r = await api().account_list();
        if (r && r.ok && Array.isArray(r.accounts)) {
          _accounts = r.accounts;
          _source = "rpc";
        } else {
          _accounts = [];
          _source = "none";
        }
      } catch (e) {
        _accounts = [];
        _source = "none";
        console.debug("[T52] account_list 不可用（后端未接入前安静降级）：",
                      (e && e.message) || e);
      }
      _busy = false;
    }
    render();
    renderSegs();
    renderHint();
  }

  const list = () => _accounts;
  const mainAccount = () =>
    _accounts.find(a => a.id === "main") || _accounts[0] || null;
  const altAccounts = () =>
    _accounts.filter(a => a.id && a.id !== "main" && a.state !== "absent");

  const ACCT_STATE_TEXT = { ready: "在线", connecting: "连接中", absent: "未授权" };
  function stateText(a) {
    if (!a) return "";
    if (_hint && _hint.account_id === a.id && _hint.reason)
      return "限速中";
    return ACCT_STATE_TEXT[a.state] || a.state || "";
  }
  function nameOf(a) {
    if (!a) return "";
    const me = a.me || {};
    if (me.username) return "@" + me.username;
    if (me.first_name) return me.first_name;
    return a.label || a.id || "";
  }
  const subOf = a => (a && (a.phone || a.id)) || "";

  // ---------- 设置页账号区渲染 ----------
  function render() {
    const region = document.getElementById("acct-region");
    if (!region) return;
    const addBtn = document.getElementById("acct-add-btn");
    const errEl = document.getElementById("acct-err");
    if (errEl) errEl.textContent = "";
    // 游客态：只显示"未登录 · 点此登录"，不显示添加小号控件（T46 口径）
    if (guestMode) {
      region.innerHTML =
        `<div class="acct-guest-row" onclick="openLoginFromGuest()">未登录 · 点此登录</div>`;
      if (addBtn) addBtn.classList.add("hidden");
      closeAddQuiet();
      return;
    }
    if (!_accounts.length) {
      // 后端方法缺失/未登录：安静降级（沿用旧"当前账号"口径）
      const n = document.getElementById("me-name").textContent;
      const u = document.getElementById("me-user").textContent;
      region.innerHTML = `
        <div class="acct-row">
          <span class="acct-tag">主号</span>
          <div class="acct-info">
            <div class="acct-name">${esc(n || "未登录")}</div>
            <div class="acct-sub">${esc(u || "")}</div>
          </div>
          <button class="btn danger sm" onclick="TgAccounts.logout(this)">退出登录</button>
        </div>`;
      if (addBtn) addBtn.classList.add("hidden");
      return;
    }
    const main = mainAccount();
    const alts = altAccounts();
    const rows = [];
    if (main) {
      rows.push(`
        <div class="acct-row" data-aid="${esc(main.id)}">
          <span class="acct-tag">主号</span>
          <div class="acct-info">
            <div class="acct-name">${esc(nameOf(main))}</div>
            <div class="acct-sub">${esc(subOf(main))}</div>
          </div>
          <span class="acct-state" data-st="${esc(main.state || "")}">${esc(stateText(main))}</span>
          <button class="btn danger sm" onclick="TgAccounts.logout(this)"${_busy ? " disabled" : ""}>退出登录</button>
        </div>`);
    }
    for (const a of alts) {
      rows.push(`
        <div class="acct-row alt" data-aid="${esc(a.id)}">
          <span class="acct-tag alt">小号</span>
          <div class="acct-info">
            <div class="acct-name">${esc(nameOf(a))}</div>
            <div class="acct-sub">${esc(a.id)} · ${esc(subOf(a))}</div>
          </div>
          <span class="acct-state" data-st="${esc(a.state || "")}">${esc(stateText(a))}</span>
          <button class="btn glass-btn sm" onclick="TgAccounts.askRemove('${esc(a.id)}')">移除</button>
        </div>`);
    }
    region.innerHTML = rows.join("");
    if (addBtn) addBtn.classList.remove("hidden");
  }

  // ---------- 添加小号三步卡 ----------
  function setAddStep(step) {
    ["phone", "code", "pwd"].forEach(x =>
      document.getElementById("acct-add-step-" + x)
        .classList.toggle("hidden", x !== step));
  }
  function openAdd() {
    // 下一个可用小号 id：alt1、alt2…（跳过已占用）
    let i = 1;
    const ids = new Set(_accounts.map(a => a.id));
    while (ids.has("alt" + i)) i++;
    addingId = "alt" + i;
    document.getElementById("acct-add-phone").value = "";
    document.getElementById("acct-add-code").value = "";
    document.getElementById("acct-add-pwd").value = "";
    document.getElementById("acct-add-err").textContent = "";
    setAddStep("phone");
    document.getElementById("acct-add-card").classList.remove("hidden");
  }
  function closeAdd() {
    document.getElementById("acct-add-card").classList.add("hidden");
    document.getElementById("acct-add-err").textContent = "";
    addingId = null;
  }
  function closeAddQuiet() {
    const el = document.getElementById("acct-add-card");
    if (el && !el.classList.contains("hidden")) {
      el.classList.add("hidden");
      addingId = null;
    }
  }
  const addErr = m =>
    (document.getElementById("acct-add-err").textContent = m || "");

  async function addStart(btn) {
    addErr("");
    const phone = document.getElementById("acct-add-phone").value.trim();
    if (!phone) return addErr("请输入手机号");
    let r;
    try {
      r = await withBusy(btn, "发送中…",
        () => acctCall("account_add_start", addingId, phone));
    } catch (e) {
      return addErr("发送失败：" + ((e && e.message) || e));
    }
    if (r.ok) setAddStep("code");
    else addErr(r.error || "发送验证码失败");
  }
  async function verifyCode(btn) {
    addErr("");
    let r;
    try {
      r = await withBusy(btn, "验证中…",
        () => acctCall("account_verify_code", addingId,
                       document.getElementById("acct-add-code").value.trim()));
    } catch (e) {
      return addErr("验证失败：" + ((e && e.message) || e));
    }
    if (r.ok) { closeAdd(); refresh(); }
    else if (r.stage === "need_password") setAddStep("pwd");
    else addErr(r.error || "验证码错误，请重试");
  }
  async function verifyPassword(btn) {
    addErr("");
    let r;
    try {
      r = await withBusy(btn, "确认中…",
        () => acctCall("account_verify_password", addingId,
                       document.getElementById("acct-add-pwd").value));
    } catch (e) {
      return addErr("确认失败：" + ((e && e.message) || e));
    }
    if (r.ok) { closeAdd(); refresh(); }
    else addErr(r.error || "两步验证密码错误");
  }

  // ---------- 移除小号（二次确认弹层 + busy 拒绝）----------
  function askRemove(id) {
    removingId = id;
    const a = _accounts.find(x => x.id === id);
    document.getElementById("acct-remove-text").textContent =
      `${nameOf(a) || id}：仅移除本地登录授权，不影响已下载任务。`;
    document.getElementById("acct-remove-confirm").classList.remove("hidden");
  }
  function cancelRemove() {
    document.getElementById("acct-remove-confirm").classList.add("hidden");
    removingId = null;
  }
  async function confirmRemove(btn) {
    const id = removingId;
    if (!id) return cancelRemove();
    let r;
    try {
      r = await withBusy(btn, null, () => acctCall("account_remove", id));
    } catch (e) {
      cancelRemove();
      const errEl = document.getElementById("acct-err");
      if (errEl) errEl.textContent = "移除失败：" + ((e && e.message) || e);
      return;
    }
    cancelRemove();
    if (r.ok) { refresh(); return; }
    // busy 拒绝：小号行保留 + 错误说明
    const errEl = document.getElementById("acct-err");
    if (errEl) errEl.textContent =
      r.busy ? "该小号正被任务使用，暂不能移除" : (r.error || "移除失败");
  }

  // ---------- 退出登录（接现有 logout RPC；busy 时禁用）----------
  async function logout(btn) {
    // busy 禁用：桩模式经 probe.busy；正常路径 doLogout 自身含
    // is_active 守卫（下载中弹窗拦截），此处不重复第二套语义
    if (_busy) {
      const errEl = document.getElementById("acct-err");
      if (errEl) errEl.textContent = "下载进行中，暂不能退出登录";
      return;
    }
    return doLogout();
  }

  // ---------- tg:acct_hint 限速提示 ----------
  function onAcctHint(detail) {
    if (!detail || typeof detail.account_id !== "string") return;
    _hint = detail;
    render();
    renderHint();
  }
  function renderHint() {
    const bar = document.getElementById("acct-hint-bar");
    const line = document.getElementById("acct-hint-line");
    if (!bar || !line) return;
    if (!_hint || !_hint.reason) {
      bar.classList.add("hidden");
      line.classList.add("hidden");
      return;
    }
    if (altAccounts().length >= 1) {
      // 已有 ≥1 个小号：同类提示降级为小字状态行，不反复弹大条
      bar.classList.add("hidden");
      line.textContent = "主号下载较慢（服务器限速）：任务可切换小号或保持自动分流";
      line.classList.remove("hidden");
      return;
    }
    line.classList.add("hidden");
    bar.classList.toggle("hidden", _hintDismissed.has(_hint.reason));
  }
  function dismissHint() {
    if (_hint && _hint.reason) _hintDismissed.add(_hint.reason);
    document.getElementById("acct-hint-bar").classList.add("hidden");
  }
  function hintAddAlt() {
    // 直达三步卡：切到设置页（loadSettings 会 refresh）并展开添加卡
    switchView("settings");
    openAdd();
  }

  // ---------- 任务选号分段控件 ----------
  function segHtml(cur) {
    const alts = altAccounts();
    const btn = (v, label) =>
      `<button class="seg${cur === v ? " active" : ""}" data-acctv="${esc(v)}" ` +
      `onclick="event.stopPropagation();TgAccounts.pickSeg(this)">${label}</button>`;
    let h = btn("main", "主号");
    if (alts.length) h += btn(alts[0].id, "小号");
    h += btn("auto", "自动");
    return h;
  }
  function renderSegs() {
    document.querySelectorAll(".acct-seg-host").forEach(host => {
      const tid = host.dataset.acctTid || "";
      const cur = tid ? (taskAcct.get(tid) || "auto") : newTaskAcct;
      host.innerHTML = segHtml(cur);
    });
  }
  function pickSeg(btnEl) {
    const host = btnEl.closest(".acct-seg-host");
    if (!host) return;
    const value = btnEl.dataset.acctv;
    if (host.dataset.acctTid) {
      const tid = host.dataset.acctTid;
      const prev = taskAcct.get(tid) || "auto";
      if (prev === value) return;
      taskAcct.set(tid, value);
      renderSegs();
      acctEditTask(tid, value, prev);
    } else {
      // 新建任务页：仅记录选择，任务创建成功后由 applyNewTaskAcct 下发
      newTaskAcct = value;
      renderSegs();
    }
  }
  async function acctEditTask(tid, value, prev) {
    // 立即下发 edit_task(task_id, {acct})（T51 冻结形状）；
    // 被拒/异常时回退控件状态（console.debug，不弹错误）
    try {
      const r = await acctCall("edit_task", tid, { acct: value });
      if (r && r.ok === false) {
        taskAcct.set(tid, prev);
        renderSegs();
        console.debug("[T52] edit_task({acct}) 被拒绝，已回退：", r);
      }
    } catch (e) {
      taskAcct.set(tid, prev);
      renderSegs();
      console.debug("[T52] edit_task({acct}) 调用失败，已回退：", e);
    }
  }
  function applyNewTaskAcct(taskId) {
    // 新建任务成功（start_download 返回 task_id）后下发本页选定的策略；
    // fire-and-forget，失败静默（不影响下载已开始的事实）
    if (!taskId) return;
    acctCall("edit_task", taskId, { acct: newTaskAcct })
      .catch(e => console.debug("[T52] 新任务 acct 下发失败：", e));
  }
  function noteTaskAcct(tid, v) {
    // list_tasks 携带 acct 字段时的种子（T51 后端落地后生效）；
    // 用户本会话手动切换过的值优先，不被种子覆盖
    if (tid && v && !taskAcct.has(tid)) taskAcct.set(tid, v);
  }

  // ---------- 挂载 ----------
  function mount() {
    TgBus.on("acct_hint", onAcctHint);
    refresh();
  }

  return {
    mount, refresh, render, renderSegs, pickSeg,
    openAdd, closeAdd, addStart, verifyCode, verifyPassword,
    askRemove, cancelRemove, confirmRemove, logout,
    dismissHint, hintAddAlt, applyNewTaskAcct, noteTaskAcct,
    list, altAccounts, stateText,
    get source() { return _source; },
    get newAcct() { return newTaskAcct; },
    get addingId() { return addingId; },
  };
})();
// 组件对象暴露到 window（控制台/测试驱动；命名与 TgJobProbe 风格一致）
window.TgAccounts = TgAccounts;
// ===== T52 多账号前端（结束）=====
