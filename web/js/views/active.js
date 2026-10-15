// ============================================================
// views/active.js — T8 占位视图：下载中（实时进度监控）
// 归属波次：T13 替换本文件与 css/views/active.css（零改基座）。
// T8 只做「迁移中」空态，禁止实现业务。
// ============================================================

export const path = "active";

export const title = "下载中 - TG 视频下载器";

let cached = null;
let paramsBox = null;

export function mount(container) {
  if (cached) return cached;
  const el = document.createElement("div");
  el.className = "v22-placeholder-view";
  el.setAttribute("data-view-name", "active");
  el.innerHTML =
    '<div class="v22-ph-card">' +
    '<div class="v22-ph-ico" aria-hidden="true">⬇</div>' +
    '<h2 class="v22-ph-title">下载中</h2>' +
    '<p class="v22-ph-desc">本视图将在 v2.2 后续版本迁移（T13：纵向任务卡 / 紧凑行展开 / 选择模式）。</p>' +
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
