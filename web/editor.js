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
  const r = await api().editor_data(TASK_ID);
  if (!r || !r.ok) {
    document.body.innerHTML =
      `<div style="padding:40px">加载失败：${esc(r && r.error)}</div>`;
    return;
  }
  state.task = r.task;
  state.items = r.items || [];

  document.getElementById("ed2-title").value = r.task.channel_title || "";
  document.getElementById("ed2-channel").textContent =
    "频道：" + (r.task.channel || "");
  document.getElementById("ed2-created").textContent =
    "创建：" + (r.task.created || "");
  document.getElementById("ed2-folder").value = r.task.folder || "";
  document.getElementById("ed2-parallel").value = r.task.parallel || 1;

  edRender();
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
function edOneStatus(k, status) {
  api().editor_set_status(TASK_ID, [keyObj(k)], status).then(() => edReload());
}

function edOneRemove(k) {
  if (!confirm("从任务清单移除该条目？不会删除本地视频文件。")) return;
  api().editor_remove(TASK_ID, [keyObj(k)]).then(() => edReload());
}

function edOpenFile(k) {
  const o = keyObj(k);
  api().open_item_file(TASK_ID, o.src, o.mid);
}

// ---------- 批量操作 ----------
function selectedKeys() {
  return [...state.selected].map(keyObj);
}

function edBulkStatus(status) {
  api().editor_set_status(TASK_ID, selectedKeys(), status)
    .then(() => { state.selected.clear(); edReload(); });
}

function edBulkRemove() {
  if (!confirm(`从任务清单移除选中的 ${state.selected.size} 个条目？\n不会删除本地视频文件。`))
    return;
  api().editor_remove(TASK_ID, selectedKeys())
    .then(() => { state.selected.clear(); edReload(); });
}

// ---------- 任务设置 ----------
function edChooseFolder() {
  api().choose_folder().then((r) => {
    if (r && r.ok && r.folder)
      document.getElementById("ed2-folder").value = r.folder;
  });
}

function edSave() {
  const fields = {
    title: document.getElementById("ed2-title").value,
    folder: document.getElementById("ed2-folder").value,
    parallel: Number(document.getElementById("ed2-parallel").value),
    move_files: document.getElementById("ed2-move").checked,
  };
  api().editor_save(TASK_ID, fields).then((r) => {
    if (r && r.ok) alert("已保存");
    else alert("保存失败：" + ((r && r.error) || ""));
  });
}

// ---------- 过滤 ----------
function edSetFilter(f) {
  state.filter = f;
  document.querySelectorAll("#ed2-filter .seg")
    .forEach((b) => b.classList.toggle("active", b.dataset.f === f));
  edRender();
}

// 操作后重新拉取最新数据
function edReload() {
  edLoad();
}

window.addEventListener("pywebviewready", edLoad);
