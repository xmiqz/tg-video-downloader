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

  // 有缺失 text/duration 时，后台补全完成后延时静默回捞一次（不触发新的
  // 网络补全）；用户正在搜索则跳过，避免打断输入。
  const needEnrich = (state.items || []).some(
    (it) => !it.text || !it.duration);
  if (needEnrich) {
    setTimeout(async () => {
      try {
        const q = document.getElementById("ed2-q");
        if (document.activeElement === q) return;
        const r2 = await api().editor_data(TASK_ID, false);
        if (!r2 || !r2.ok) return;
        state.items = r2.items || state.items;
        edRender();
      } catch (e) {}
    }, 3500);
  }
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
// P1-5 修复：后端 choose_folder 返回形为 {"ok": true, "path": p}
// （gui.Api.choose_folder），旧码误读返回体的 folder 键（其值为
// undefined）致勾选目录后回填永不生效——统一以后端实际返参 path 为准。
function edChooseFolder() {
  api().choose_folder().then((r) => {
    if (r && r.ok && r.path)
      document.getElementById("ed2-folder").value = r.path;
  });
}

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
  };
  api().editor_save(TASK_ID, fields).then((r) => {
    if (r && r.need_login) {
      unlock();
      return alert("该功能需要登录，请在主窗口登录后重试");
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

// 操作后重新拉取最新数据
function edReload() {
  edLoad();
}

window.addEventListener("pywebviewready", edLoad);
