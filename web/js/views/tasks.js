// ============================================================
// views/tasks.js — T8 占位视图：任务库（历史任务网格/单列）
// 归属波次：T14 替换本文件与 css/views/tasks.css（零改基座）。
// T8 只做「迁移中」空态，禁止实现业务。
// 注：新壳默认路由为 /tasks（对齐旧壳 app.js bootLogin 的
// switchView("tasks") 运行时事实），见 t8_contract.md §3。
// ============================================================

export const path = "tasks";

export const title = "任务库 - TG 视频下载器";

let cached = null;
let paramsBox = null;

export function mount(container) {
  if (cached) return cached;
  const el = document.createElement("div");
  el.className = "v22-placeholder-view";
  el.setAttribute("data-view-name", "tasks");
  el.innerHTML =
    '<div class="v22-ph-card">' +
    '<div class="v22-ph-ico" aria-hidden="true">◈</div>' +
    '<h2 class="v22-ph-title">任务库</h2>' +
    '<p class="v22-ph-desc">本视图将在 v2.2 后续版本迁移（T14：网格↔单列卡片 / 筛选分段横滑 / 底部弹层操作）。</p>' +
    '<p class="v22-ph-params" data-ph="params"></p>' +
    "</div>";
  paramsBox = el.querySelector('[data-ph="params"]');
  cached = el;
  return el;
}

export function onEnter(params) {
  if (paramsBox) {
    paramsBox.textContent = params && params.id
      ? "路由参数：id=" + String(params.id)
      : "";
  }
}

export function onLeave() {
  /* 占位视图无全局资源需要清理 */
}
