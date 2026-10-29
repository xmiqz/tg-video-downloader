/* ============================================================
   独立任务编辑窗口：任务设置 + 全条目专业表格管理
   文件名 / TG 消息原文 / 时长 / 日期 / 大小 / 状态
   ============================================================ */
const api = () => window.pywebview.api;

const TASK_ID = new URLSearchParams(location.search).get("id");

const state = {
  task: null,
  items: [],
  selected: new Set(),   // "src:mid"
  expanded: new Set(),
  filter: "all",
  q: "",
};

// ---------- 工具 ----------
function keyOf(ti) {
  return (ti.src || "channel") + ":" + ti.msg;
}

function keyObj(k) {
  const i = k.indexOf(":");
  return { src: k.slice(0, i), mid: Number(k.slice(i + 1)) };
}

function humanSize(n) {
  n = Number(n) || 0;
  if (n >= 1073741824) return (n / 1073741824).toFixed(2) + " GB";
  if (n >= 1048576) return (n / 1048576).toFixed(1) + " MB";
  if (n >= 1024) return (n / 1024).toFixed(0) + " KB";
  return n + " B";
}

function fmtDur(s) {
  s = Number(s) || 0;
  if (!s) return "—";
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const ss = s % 60;
  const pad = (x) => String(x).padStart(2, "0");
  return h ? `${h}:${pad(m)}:${pad(ss)}` : `${m}:${pad(ss)}`;
}

function esc(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

const STATUS_LABEL = {
  done: ["已完成", "st-done"],
  pending: ["未下载", "st-pending"],
  failed: ["失败", "st-failed"],
  skipped: ["跳过", "st-skipped"],
  exists: ["已存在", "st-done"],
};

// ---------- 数据加载 ----------
async function edLoad() {
  let r;
  try {
    r = await api().editor_data(TASK_ID);
  } catch (e) {
    // P1-2：首拉被 reject（桥接断链/服务端异常）与 r.ok=false 同形的
    // 内联可见反馈，不产生 unhandled rejection。
    document.body.innerHTML =
      `<div style="padding:40px">加载失败：${esc(e && e.message || e)}</div>`;
    return;
  }
  if (!r || !r.ok) {
    document.body.innerHTML =
      `<div style="padding:40px">加载失败：${esc(r && r.error)}</div>`;
    return;
  }
  state.task = r.task;
  state.items = r.items || [];
  await edApplyHostMode();

  document.getElementById("ed2-title").value = r.task.channel_title || "";
  document.getElementById("ed2-channel").textContent =
    "频道：" + (r.task.channel || "");
  document.getElementById("ed2-created").textContent =
    "创建：" + (r.task.created || "");
  document.getElementById("ed2-folder").value = r.task.folder || "";
  document.getElementById("ed2-parallel").value = r.task.parallel || 1;
  document.getElementById("ed2-retries").value =
    (r.task && r.task.retries !== undefined && r.task.retries !== null
      && r.task.retries !== "") ? r.task.retries : "";   // 空=默认

  edRender();
  edRenameSync();
  // 2.3：命名上下文档位默认跟随任务建档档位
  const rnWide = document.getElementById("ed2-rn-wide");
  if (rnWide) rnWide.checked = String((r.task && r.task.name_wide) || "") === "1";

  if (_edNeedEnrich()) _edRefetchStart();
}

function edFolderIsHeadless() {
  return String(window.__TVG_HOST_MODE || "") === "headless";
}

async function edApplyHostMode() {
  let mode = window.__TVG_HOST_MODE;
  if (mode !== "headless" && mode !== "desktop") {
    try {
      const st = await api().get_settings();
      mode = st && st.mode;
      if (mode) window.__TVG_HOST_MODE = mode;
    } catch (e) { /* keep the synchronous bridge hint */ }
  }
  const input = document.getElementById("ed2-folder");
  const browse = document.getElementById("ed2-folder-browse");
  if (!input) return;
  const headless = mode === "headless";
  input.readOnly = !headless;
  if (browse) browse.hidden = false;
  input.title = headless ? "可输入服务器路径，或使用浏览按钮选择目录" : "";
  if (headless) input.placeholder = "输入或浏览服务器目录";
  else input.removeAttribute("placeholder");
}

// ---------- 补全回捞（P1-7：单飞收敛式轮询）----------
// 仅当条目确有缺失 text/duration 时启动一条轮询链：间隔递增
// （1.5s→3s→4.5s），editor_data 第二参数传 false 仅取本地、不触发新的
// 网络补全；条目补齐即停，至多 3 拍兜底即停；用户正在搜索框输入的拍
// 跳过（保留旧语义，不打断输入）；新链启动时代际号 +1 使旧链作废，
// 任何时刻至多一条链在飞。替代旧的固定 setTimeout(…, 3500) 一次性回捞。
let _edRefetchGen = 0;

function _edNeedEnrich() {
  return (state.items || []).some((it) => !it.text || !it.duration);
}

function _edRefetchStart() {
  const gen = ++_edRefetchGen;
  const DELAYS = [1500, 3000, 4500];
  const tick = () => {
    if (gen !== _edRefetchGen) return;   // 已被新链作废
    if (!DELAYS.length) return;           // 至多 3 拍兜底即停
    const wait = DELAYS.shift();
    setTimeout(() => {
      if (gen !== _edRefetchGen) return;
      const q = document.getElementById("ed2-q");
      if (document.activeElement === q) { tick(); return; }  // 聚焦跳过本拍
      api().editor_data(TASK_ID, false).then((r) => {
        if (gen !== _edRefetchGen) return;
        if (r && r.ok) {
          state.items = r.items || state.items;
          edRender();
          edRenameSync();
          if (!_edNeedEnrich()) return;    // 条目补齐即停
        }
        tick();                            // 仍缺失：下一拍
      }).catch(() => {                     // 回捞失败非首屏主路径，静默续拍
        if (gen === _edRefetchGen) tick();
      });
    }, wait);
  };
  tick();
}

// ---------- 筛选 ----------
function filtered() {
  const q = state.q.trim().toLowerCase();
  return state.items.filter((ti) => {
    if (state.filter === "done" && ti.status !== "done" && ti.status !== "exists")
      return false;
    if (state.filter !== "all" && state.filter !== "done" &&
        ti.status !== state.filter)
      return false;
    if (q && !((ti.name || "").toLowerCase().includes(q) ||
               (ti.text || "").toLowerCase().includes(q)))
      return false;
    return true;
  });
}

// ---------- 渲染表格 ----------
function edRender() {
  state.q = document.getElementById("ed2-q").value;
  const rows = filtered();
  const tbody = document.getElementById("ed2-rows");
  const html = [];

  for (const ti of rows) {
    const k = keyOf(ti);
    const checked = state.selected.has(k);
    const isOpen = state.expanded.has(k);
    const [stText, stCls] = STATUS_LABEL[ti.status] || [ti.status, ""];
    const text = ti.text || "";

    html.push(`
      <tr class="ed-row ${checked ? "checked" : ""}" data-k="${esc(k)}">
        <td class="c-check">
          <div class="mini-cbox ${checked ? "on" : ""}"
               onclick="edToggle('${esc(k)}')">${checked ? "✓" : ""}</div>
        </td>
        <td class="c-name" title="${esc(ti.name)}">${esc(ti.name)}</td>
        <td class="c-msg ${text ? "" : "empty"}"
            onclick="edToggleExpand('${esc(k)}')">
          <span class="msg-line">${esc(text) || "（无消息文本，点击仍可展开）"}</span>
        </td>
        <td class="c-dur">${fmtDur(ti.duration)}</td>
        <td class="c-date">${esc(ti.date || "—")}</td>
        <td class="c-size">${humanSize(ti.size)}</td>
        <td class="c-st"><span class="st-badge ${stCls}">${stText}</span></td>
        <td class="c-act">
          <button class="mini-action" title="打开本地文件"
            onclick="edOpenFile('${esc(k)}')">文件</button>
          <button class="mini-action" title="标记跳过"
            onclick="edOneStatus('${esc(k)}','skipped')">跳过</button>
          <button class="mini-action danger" title="从清单移除"
            onclick="edOneRemove('${esc(k)}')">移除</button>
        </td>
      </tr>`);

    if (isOpen) {
      html.push(`
        <tr class="ed-detail-row">
          <td></td>
          <td colspan="7">
            <div class="ed-detail">
              <div class="ed-detail-label">
                TG 消息原文 · #${ti.msg}${ti.error ? " · 失败原因：" + esc(ti.error) : ""}
              </div>
              <pre class="ed-detail-text">${esc(text) || "（该条目没有保存消息文本）"}</pre>
            </div>
          </td>
        </tr>`);
    }
  }

  tbody.innerHTML = html.join("");

  const c = state.items.length;
  document.getElementById("ed2-counts").textContent =
    `显示 ${rows.length} / ${c} 条`;
  edRenderBulk();
}

function edRenderBulk() {
  const bar = document.getElementById("ed2-bulk");
  const n = state.selected.size;
  document.getElementById("ed2-bulk-count").textContent = `已选 ${n}`;
  bar.classList.toggle("hidden", n === 0);
}

// ---------- 选择 ----------
function edToggle(k) {
  if (state.selected.has(k)) state.selected.delete(k);
  else state.selected.add(k);
  edRender();
}

function edSelectAll(on) {
  state.selected = new Set(on ? filtered().map(keyOf) : []);
  edRender();
}

function edToggleExpand(k) {
  if (state.expanded.has(k)) state.expanded.delete(k);
  else state.expanded.add(k);
  edRender();
}

// ---------- 单条操作 ----------
// P1-2：失败路径 alert 可见（文案含后端 error / reject 原因）。
// P1-7：成功后不再全量重拉 editor_data（edReload），改为本地 state
// 最小变更 + edRender()——操作本身那 1 次 RPC 之外 0 次 editor_data。
function edOneStatus(k, status) {
  api().editor_set_status(TASK_ID, [keyObj(k)], status)
    .then((r) => {
      if (!r || !r.ok)
        return alert("标记失败：" + ((r && r.error) || ""));
      const ti = state.items.find((it) => keyOf(it) === k);
      if (ti) ti.status = status;
      edRender();
    })
    .catch((e) => alert("标记失败：" + (e && e.message || e)));
}

function edOneRemove(k) {
  if (!confirm("从任务清单移除该条目？不会删除本地视频文件。")) return;
  api().editor_remove(TASK_ID, [keyObj(k)])
    .then((r) => {
      if (!r || !r.ok)
        return alert("移除失败：" + ((r && r.error) || ""));
      state.items = state.items.filter((it) => keyOf(it) !== k);
      state.selected.delete(k);
      state.expanded.delete(k);
      edRender();
    })
    .catch((e) => alert("移除失败：" + (e && e.message || e)));
}

// window.open(url, "_blank", "noopener") returns null BY SPEC, so using its
// return value to detect a blocked popup reports "被浏览器拦截" even when the
// tab opened fine. Open without the noopener feature (which keeps the returned
// WindowProxy) and sever opener manually instead - same isolation, but the
// return value is usable.
function openExternal(url) {
  let w = null;
  try {
    w = window.open(url, "_blank");
  } catch (e) {
    return null;
  }
  if (w) {
    try { w.opener = null; } catch (e) {}
  }
  return w;
}

function edOpenFile(k) {
  const o = keyObj(k);
  // P1-2（QA 复核：此点为无 .then 的 fire-and-forget 形态）——补返回体
  // 检查 + rejection 兜底：{ok:false, error:"文件不存在"} 不再静默。
  api().open_item_file(TASK_ID, o.src, o.mid)
    .then((r) => {
      // Headless: the server cannot open files locally, but it can serve
      // them. Let the browser play/save the item from a same-origin URL.
      if (r && r.headless && r.url) {
        const w = openExternal(r.url);
        if (!w) alert("播放页被浏览器拦截，请允许本站弹出窗口后重试");
        return;
      }
      if (!r || !r.ok) alert("打开失败：" + ((r && r.error) || ""));
    })
    .catch((e) => alert("打开失败：" + (e && e.message || e)));
}

// ---------- 批量操作 ----------
function selectedKeys() {
  return [...state.selected].map(keyObj);
}

// P1-2/P1-7 同单条操作：失败可见；成功本地最小变更（含清空选中）。
function edBulkStatus(status) {
  api().editor_set_status(TASK_ID, selectedKeys(), status)
    .then((r) => {
      if (!r || !r.ok)
        return alert("批量标记失败：" + ((r && r.error) || ""));
      const keys = new Set(state.selected);
      for (const ti of state.items)
        if (keys.has(keyOf(ti))) ti.status = status;
      state.selected.clear();
      edRender();
    })
    .catch((e) => alert("批量标记失败：" + (e && e.message || e)));
}

function edBulkRemove() {
  if (!confirm(`从任务清单移除选中的 ${state.selected.size} 个条目？\n不会删除本地视频文件。`))
    return;
  api().editor_remove(TASK_ID, selectedKeys())
    .then((r) => {
      if (!r || !r.ok)
        return alert("批量移除失败：" + ((r && r.error) || ""));
      const keys = new Set(state.selected);
      state.items = state.items.filter((it) => !keys.has(keyOf(it)));
      state.selected.clear();
      for (const k of [...state.expanded])
        if (keys.has(k)) state.expanded.delete(k);
      edRender();
    })
    .catch((e) => alert("批量移除失败：" + (e && e.message || e)));
}

// ---------- 任务设置 ----------
// P1-5 修复：后端 choose_folder 返回形为 {"ok": true, "path": p}
// （gui.Api.choose_folder），旧码误读返回体的 folder 键（其值为
// undefined）致勾选目录后回填永不生效——统一以后端实际返参 path 为准。
// P1-3（B 包顺带）：浏览按钮 busy 防重——pending 期间按钮 disabled，
// 连点只发 1 次 choose_folder；成功回填 / 用户取消（path=""）不动 /
// 失败（含 reject）alert 可见，三者后按钮复位。
let _edChoosing = false;

// ============================================================
// 2.3 智能重命名（高级）：命名前后预览可编辑 + 是否重命名勾选 +
// 一键全选/取消全选 + 应用命名 + 还原命名（勾选多选）
// ============================================================
const rnState = { edits: {}, off: new Set() };

function edRenameTogglePanel() {
  const on = document.getElementById("ed2-rn-toggle").checked;
  document.getElementById("ed2-rn-panel").classList.toggle("hidden", !on);
  if (on) edRenameRender();
}

function edRenameSync() {
  const p = document.getElementById("ed2-rn-panel");
  if (p && !p.classList.contains("hidden")) edRenameRender();
}

// 2.3「更宽上下文命名」开关：按新档位重取拟名建议（用户已编辑内容保留）
async function edRenameWideToggle() {
  const on = document.getElementById("ed2-rn-wide").checked;
  try {
    const r = await api().editor_data(TASK_ID, false, on);
    if (r && r.ok) {
      state.items = r.items || state.items;
      edRender();
    }
  } catch (e) { /* 静默：保留现建议 */ }
  edRenameRender();
}

function rnEff(k, ti) {
  return (((k in rnState.edits) ? rnState.edits[k] : (ti && ti.rename) || "")).trim();
}

function edRenameRender() {
  const box = document.getElementById("ed2-rn-rows");
  if (!box) return;
  const rows = state.items || [];
  box.innerHTML = rows.length ? rows.map((ti) => {
    const k = keyOf(ti);
    const eff = rnEff(k, ti);
    const on = !rnState.off.has(k) && eff !== "";
    return `<div class="rn-row">
      <input type="checkbox" class="rn-check" data-k="${esc(k)}"${on ? " checked" : ""}
        title="是否重命名（取消勾选=使用并展示原文件名）"
        onchange="edRenameRowToggle('${esc(k)}', this)">
      <span class="rn-before" title="${esc(ti.name)}">${esc(ti.name)}</span>
      <span class="rn-arrow">→</span>
      <input class="rn-input" data-k="${esc(k)}"
        value="${esc(on ? eff : ti.name)}" placeholder="留空不改"${on ? "" : " disabled"}
        oninput="edRenameInput('${esc(k)}', this)">
    </div>`;
  }).join("") : `<div class="rn-empty">没有可处理的条目</div>`;
}

function edRenameRowToggle(k, el) {
  const ti = (state.items || []).find((it) => keyOf(it) === k);
  const row = el.closest(".rn-row");
  const inp = row && row.querySelector(".rn-input");
  if (el.checked) {
    rnState.off.delete(k);
    if (inp) {
      inp.disabled = false;
      inp.value = rnEff(k, ti);
    }
  } else {
    rnState.off.add(k);
    if (inp) {
      inp.disabled = true;
      inp.value = ti ? ti.name : "";   // 取消勾选=使用并展示原文件名
    }
  }
}

function edRenameInput(k, el) {
  rnState.edits[k] = el.value;
  // 手工输入即视为启用重命名
  const row = el.closest(".rn-row");
  const cb = row && row.querySelector(".rn-check");
  if (el.value.trim() && cb && !cb.checked) {
    cb.checked = true;
    rnState.off.delete(k);
  }
}

function edRenameAll(on) {
  for (const ti of state.items || []) {
    const k = keyOf(ti);
    if (on) rnState.off.delete(k); else rnState.off.add(k);
  }
  edRenameRender();
}

async function _edRnReload() {
  try {
    const r = await api().editor_data(TASK_ID, false);
    if (r && r.ok) {
      state.items = r.items || state.items;
      edRender();
    }
  } catch (e) { /* 静默：保留本地 state */ }
  rnState.edits = {};
  rnState.off = new Set();
  edRenameRender();
}

let rnApplyBusy = false;
async function edRenameApply(btn) {
  const renames = [];
  for (const ti of state.items || []) {
    const k = keyOf(ti);
    if (rnState.off.has(k)) continue;          // 取消勾选=保持原名
    const name = rnEff(k, ti);
    if (name && name !== ti.name) renames.push(Object.assign(keyObj(k), { name: name }));
  }
  if (!renames.length) return alert("没有可应用的命名（勾选行并在「→」后填写新名）");
  if (rnApplyBusy) return;
  rnApplyBusy = true;
  const old = btn.textContent;
  btn.disabled = true;
  btn.textContent = "改名中…";
  let r;
  try {
    r = await api().edit_task(TASK_ID, { renames: renames });
  } catch (e) {
    btn.disabled = false; btn.textContent = old; rnApplyBusy = false;
    return alert("改名失败：" + (e && e.message || e));
  }
  btn.disabled = false; btn.textContent = old; rnApplyBusy = false;
  if (!r || !r.ok) return alert("改名失败：" + ((r && r.error) || "操作未生效"));
  alert(`已改名 ${r.renamed} 条（文件已重命名，请刷新文件夹查看）；` +
    `已登记 ${r.recorded} 条（未下载条目，下载完成即按此命名）` +
    ((r.warnings && r.warnings.length) ? "\n" + r.warnings.join("\n") : ""));
  await _edRnReload();
}

let rnRestoreBusy = false;
async function edRenameRestore(btn) {
  const keys = [];
  for (const ti of state.items || []) {
    const k = keyOf(ti);
    const eff = rnEff(k, ti);
    if (!rnState.off.has(k) && eff !== "") keys.push(keyObj(k));  // 勾选条目
  }
  if (!keys.length) return alert("请先勾选要还原命名的条目");
  if (!confirm(`还原选中 ${keys.length} 条为原始命名？`)) return;
  if (rnRestoreBusy) return;
  rnRestoreBusy = true;
  const old = btn.textContent;
  btn.disabled = true;
  btn.textContent = "还原中…";
  let r;
  try {
    r = await api().edit_task(TASK_ID, { restore: keys });
  } catch (e) {
    btn.disabled = false; btn.textContent = old; rnRestoreBusy = false;
    return alert("还原命名失败：" + (e && e.message || e));
  }
  btn.disabled = false; btn.textContent = old; rnRestoreBusy = false;
  if (!r || !r.ok) return alert("还原命名失败：" + ((r && r.error) || "操作未生效"));
  alert(`已还原 ${r.restored} 条` +
    (r.skipped ? `，跳过 ${r.skipped} 条（未改名）` : "") +
    ((r.errors && r.errors.length) ? "\n" + r.errors.join("\n") : ""));
  await _edRnReload();
}

function edChooseFolder() {
  const btn = document.querySelector(".path-row .glass-btn");
  if (_edChoosing) return;                 // JS 锁兜底（disabled 之外）
  if (edFolderIsHeadless()) {
    edOpenFolderPicker();
    return;
  }
  _edChoosing = true;
  if (btn) btn.disabled = true;
  const settle = () => {
    _edChoosing = false;
    if (btn) btn.disabled = false;
  };
  api().choose_folder().then((r) => {
    settle();
    // Browser / container mode has no server-side folder dialog. Rather than
    // an alert that says "失败", make the field editable and put the caret in
    // it so the user can type the server path directly.
    if (r && r.unsupported_in_headless) {
      const input = document.getElementById("ed2-folder");
      if (input) {
        input.readOnly = false;
        input.focus();
        input.select();
      }
      return;
    }
    if (!r || !r.ok)
      return alert("选择目录失败：" + ((r && r.error) || ""));
    if (r.path) document.getElementById("ed2-folder").value = r.path;
  }).catch((e) => {
    settle();
    alert("选择目录失败：" + (e && e.message || e));
  });
}

// ---------- 浏览器服务端目录选择器 ----------
let _edFolderPath = "";
let _edFolderParent = "";
let _edFolderBusy = false;

function edFolderModal() {
  return document.getElementById("ed2-folder-modal");
}

function edOpenFolderPicker() {
  const modal = edFolderModal();
  if (!modal) return;
  modal.classList.remove("hidden");
  const input = document.getElementById("ed2-folder");
  const start = input ? input.value.trim() : "";
  edBrowseFolder(start);
}

function edCloseFolderPicker() {
  const modal = edFolderModal();
  if (modal) modal.classList.add("hidden");
}

function edFolderUp() {
  if (_edFolderParent && !_edFolderBusy) edBrowseFolder(_edFolderParent);
}

function edUseCurrentFolder() {
  if (!_edFolderPath) return;
  const input = document.getElementById("ed2-folder");
  if (input) input.value = _edFolderPath;
  edCloseFolderPicker();
}

function edFolderStatus(text) {
  const node = document.getElementById("ed2-folder-status");
  if (node) node.textContent = text || "";
}

function edRenderFolderBrowser(r) {
  const roots = document.getElementById("ed2-folder-roots");
  const list = document.getElementById("ed2-folder-list");
  const current = document.getElementById("ed2-folder-current");
  const up = document.getElementById("ed2-folder-up");
  if (!roots || !list || !current || !up) return;

  roots.textContent = "";
  (r.roots || []).forEach(function (root) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "btn glass-btn sm";
    b.textContent = root.name || root.path;
    b.title = root.path;
    b.addEventListener("click", function () { edBrowseFolder(root.path); });
    roots.appendChild(b);
  });

  current.value = r.path || "";
  up.disabled = !r.parent || _edFolderBusy;
  list.textContent = "";
  const dirs = Array.isArray(r.dirs) ? r.dirs : [];
  if (!dirs.length) {
    const empty = document.createElement("div");
    empty.className = "ed-folder-empty";
    empty.textContent = "此目录下没有子文件夹";
    list.appendChild(empty);
  } else {
    dirs.forEach(function (dir) {
      const row = document.createElement("button");
      row.type = "button";
      row.className = "ed-folder-row";
      row.title = dir.path;
      const icon = document.createElement("span");
      icon.className = "folder-ico";
      icon.textContent = "▰";
      const name = document.createElement("span");
      name.className = "folder-name";
      name.textContent = dir.name;
      row.append(icon, name);
      row.addEventListener("click", function () { edBrowseFolder(dir.path); });
      list.appendChild(row);
    });
  }
  edFolderStatus(r.writable === false ? "当前目录不可写，请选择其它目录。" : "");
}

async function edBrowseFolder(path) {
  if (_edFolderBusy) return;
  _edFolderBusy = true;
  edFolderStatus("正在读取目录…");
  const list = document.getElementById("ed2-folder-list");
  if (list) list.setAttribute("aria-busy", "true");
  try {
    const r = await api().browse_folders(path || "");
    if (!r || !r.ok) {
      edFolderStatus("读取目录失败：" + ((r && r.error) || "未知错误"));
      return;
    }
    _edFolderPath = r.path || "";
    _edFolderParent = r.parent || "";
    edRenderFolderBrowser(r);
  } catch (e) {
    edFolderStatus("读取目录失败：" + (e && e.message || e));
  } finally {
    _edFolderBusy = false;
    const up = document.getElementById("ed2-folder-up");
    if (up) up.disabled = !_edFolderParent;
    if (list) list.removeAttribute("aria-busy");
  }
}

document.addEventListener("keydown", function (ev) {
  const modal = edFolderModal();
  if (ev.key === "Escape" && modal && !modal.classList.contains("hidden")) {
    edCloseFolderPicker();
  }
});
document.addEventListener("DOMContentLoaded", function () {
  const modal = edFolderModal();
  if (modal) {
    modal.addEventListener("click", function (ev) {
      if (ev.target === modal) edCloseFolderPicker();
    });
  }
});

// T50：二窗搬运作业弹层（editor.html 零改，DOM 由 JS 动态建；class 与
// 主窗 T50Move 共用 style.css 的 t50-move-* 样式）。运行中无取消/关闭
// X/点遮罩关闭；done → 收层刷新；error → 错误全文 + 固定提示 + "关闭"
// 按钮收层并恢复保存按钮。事件源：原生 tg:job_update 监听（editor.js
// 无 TgBus）+ 挂载时 job_state 快照兜底；按 updated_ts 去重幂等。
let _edMove = null;   // {jobId, dom, handler, lastTs, ended, onDone, onClose}

function _edMoveBuildDom() {
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

function _edMoveTeardown() {
  if (!_edMove) return;
  window.removeEventListener("tg:job_update", _edMove.handler);
  if (_edMove.dom && _edMove.dom.mask && _edMove.dom.mask.parentNode)
    _edMove.dom.mask.remove();
  _edMove = null;
}

function _edMoveHandle(job) {
  const st = _edMove;
  if (!st || !job || job.job_id !== st.jobId) return;
  const ts = Number(job.updated_ts) || 0;
  if (st.lastTs !== null && ts <= st.lastTs) return;    // 迟到/同值帧
  st.lastTs = ts;
  const d = Number(job.done) || 0, t = Number(job.total) || 0;
  const bd = Number(job.bytes_done) || 0, bt = Number(job.bytes_total) || 0;
  st.dom.name.textContent = job.phase_detail || "";
  st.dom.count.textContent = t > 0 ? `${d}/${t}` : "";
  st.dom.bytes.textContent = bt > 0
    ? `${humanSize(bd)} / ${humanSize(bt)}` : "";
  st.dom.fill.style.width =
    (bt > 0 ? Math.max(0, Math.min(100, bd * 100 / bt)) : 0).toFixed(1) + "%";
  if (job.state === "done" && !st.ended) {
    st.ended = true;
    const cb = st.onDone;
    _edMoveTeardown();
    if (cb) cb();
  } else if ((job.state === "error" || job.state === "cancelled")
             && !st.ended) {
    st.ended = true;
    st.dom.err.classList.remove("hidden");
    st.dom.errText.textContent = job.error || "移动失败";
  }
}

function edWatchMove(jobId, onDone, onClose) {
  if (_edMove) _edMoveTeardown();
  const dom = _edMoveBuildDom();
  _edMove = { jobId, dom, lastTs: null, ended: false, onDone, onClose };
  _edMove.handler = (ev) => _edMoveHandle(ev && ev.detail);
  window.addEventListener("tg:job_update", _edMove.handler);
  dom.closeBtn.addEventListener("click", () => {
    _edMoveTeardown();
    if (onClose) onClose();
  });
  // 挂载即拉一次快照兜底（防 WS 漏帧；终态外幂等）
  api().job_state(jobId).then((r) => {
    if (r && r.ok && r.job) _edMoveHandle(r.job);
  }).catch(() => {});
}

function edSave() {
  // T50：保存按钮状态机——点击即锁；瞬时/失败/need_login 恢复；搬运
  // 作业保持锁定到终态（错误弹层"关闭"后恢复，允许重试）。
  const btn = document.querySelector('.editor-head button.primary');
  const oldText = btn ? btn.textContent : "";
  const unlock = () => {
    if (btn) { btn.disabled = false; btn.textContent = oldText; }
  };
  if (btn) { btn.disabled = true; btn.textContent = "保存中…"; }
  const fields = {
    title: document.getElementById("ed2-title").value,
    folder: document.getElementById("ed2-folder").value,
    parallel: Number(document.getElementById("ed2-parallel").value),
    move_files: document.getElementById("ed2-move").checked,
    // 2.3 T22：任务级重试次数（空=恢复默认；后端仅收 0..8 整数，转 Number）
    retries: document.getElementById("ed2-retries").value === ""
      ? "" : Number(document.getElementById("ed2-retries").value),
  };
  api().editor_save(TASK_ID, fields).then((r) => {
    if (r && r.need_login) {
      unlock();
      return alert("该操作需要 Telegram 登录，请在主窗口登录后重试");
    }
    if (!r || !r.ok) {
      unlock();
      return alert("保存失败：" + ((r && r.error) || ""));
    }
    if (r.job_id) {
      edWatchMove(r.job_id,
        async () => {
          unlock();
          await edLoad();
          alert("已保存");
        },
        unlock);
      return;
    }
    unlock();
    alert("已保存");
  }).catch((e) => {
    unlock();
    alert("保存失败：" + (e && e.message || e));
  });
}

// ---------- 过滤 ----------
function edSetFilter(f) {
  state.filter = f;
  document.querySelectorAll("#ed2-filter .seg")
    .forEach((b) => b.classList.toggle("active", b.dataset.f === f));
  edRender();
}

// P1-7：条目操作后的刷新已改本地 state 最小变更 + edRender()，
// 全量 edReload()/editor_data 重拉路径移除；全量 edLoad 仅保留在
// 首屏与 T50 搬运 done 回调（edSave 内）。

// T19h：外观跟随主程序（2.1 editor.html:9-15 静态脚本读 localStorage 的
// v2.2 适配；独立窗无实时采样，液态玻璃按静态暗处理——同 2.1 原注）。
function edApplyAppearance() {
  try {
    api().get_appearance().then(function (r) {
      const mode = r && r.mode;
      const root = document.documentElement;
      const body = document.body;
      const back = document.getElementById("sh-backdrop");
      root.removeAttribute("data-theme");
      root.removeAttribute("data-lum");
      body.classList.remove("liquid");
      if (back) {
        back.style.backgroundImage = "";
        back.removeAttribute("title");
      }
      if (mode === "light") {
        root.setAttribute("data-theme", "light");
        return;
      }
      // 每日壁纸：与主壳同一套 data-theme="wallpaper" + #sh-backdrop，
      // 图片走后端缓存（Bing 每日一图），独立窗也要跟随，否则编辑页
      // 停在深色而主窗是壁纸。
      if (mode === "bing" || mode === "wallpaper") {
        root.setAttribute("data-theme", "wallpaper");
        edApplyBackdrop();
      }
    }).catch(function () {});
  } catch (e) {}
}

// Token for asset URLs: <img>/CSS cannot send Authorization headers, and the
// desktop shell's only credential is the token in the page URL hash.
function edAssetToken() {
  try {
    const m = String(location.hash || "").match(/[#&]t=([^&]*)/);
    return m ? decodeURIComponent(m[1]) : "";
  } catch (e) {
    return "";
  }
}

function edApplyBackdrop() {
  const back = document.getElementById("sh-backdrop");
  if (!back) return;
  const tok = edAssetToken();
  const q = tok ? "?t=" + encodeURIComponent(tok) : "";
  const paint = function (cur) {
    if (!cur || !cur.file) return;
    const url = "/wallpaper/" + cur.file + q;
    if (typeof window._setBackdrop === "function") {
      window._setBackdrop(url, cur.lum);
    } else {
      back.style.backgroundImage = 'url("' + url + '")';
      edSetLum(cur.lum);
    }
    if (cur.copyright) back.title = cur.copyright;
  };
  api().wallpaper_status().then(function (st) {
    const cur = st && st.ok ? st.current : null;
    if (cur && cur.file) {
      paint(cur);
      return null;
    }
    return api().wallpaper_refresh([false]).then(function (fresh) {
      const c2 = fresh && fresh.ok ? fresh.current : null;
      if (c2) paint(c2);
    });
  }).catch(function () {});
}

let _edLumUrl = "";
const _edLumCanvas = document.createElement("canvas");
_edLumCanvas.width = 12;
_edLumCanvas.height = 8;

function edSetLum(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) {
    document.documentElement.removeAttribute("data-lum");
    return false;
  }
  if (n >= 160) document.documentElement.setAttribute("data-lum", "bright");
  else if (n <= 120) document.documentElement.setAttribute("data-lum", "dark");
  else document.documentElement.removeAttribute("data-lum");
  return true;
}

function edSampleLum(url) {
  if (!url || url === _edLumUrl) return;
  _edLumUrl = url;
  const ctx = _edLumCanvas.getContext("2d", { willReadFrequently: true });
  if (!ctx) return;
  const img = new Image();
  img.onload = function () {
    try {
      ctx.drawImage(img, 0, 0, _edLumCanvas.width, _edLumCanvas.height);
      const d = ctx.getImageData(0, 0, _edLumCanvas.width,
        _edLumCanvas.height).data;
      let sum = 0;
      for (let i = 0; i < d.length; i += 4) {
        sum += 0.2126 * d[i] + 0.7152 * d[i + 1] + 0.0722 * d[i + 2];
      }
      edSetLum(sum / (d.length / 4));
    } catch (e) { /* canvas failure: keep the dark fallback */ }
  };
  img.src = url;
}

window._setBackdrop = function (url, lum) {
  const back = document.getElementById("sh-backdrop");
  if (!back) return;
  back.style.backgroundImage = url ? 'url("' + url + '")' : "";
  if (!url) {
    document.documentElement.removeAttribute("data-lum");
    _edLumUrl = "";
    return;
  }
  if (!edSetLum(lum)) edSampleLum(url);
};

window.addEventListener("storage", function (ev) {
  if (ev.key === "appearance" || ev.key === "wallpaper-rev") {
    edApplyAppearance();
  }
});
window.addEventListener("pywebviewready", function () {
  edApplyAppearance();
  edLoad();
});
