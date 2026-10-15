// ============================================================
// views/editor.js — T8 占位视图：任务编辑（全屏路由 task/:id/edit）
// 归属波次：T16 替换本文件与 css/views/editor.js.css，承接旧
// editor.html/editor.js 全部能力；open_editor 第二窗口由组长执行删除。
// T8 只做「迁移中」空态；onEnter(params) 中 params.id 为插槽演示。
// ============================================================

export const path = "task/:id/edit";

export const title = "任务编辑 - TG 视频下载器";

let cached = null;
let paramsBox = null;

export function mount(container) {
  if (cached) return cached;
  const el = document.createElement("div");
  el.className = "v22-placeholder-view";
  el.setAttribute("data-view-name", "editor");
  el.innerHTML =
    '<div class="v22-ph-card">' +
    '<div class="v22-ph-ico" aria-hidden="true">✎</div>' +
    '<h2 class="v22-ph-title">任务编辑</h2>' +
    '<p class="v22-ph-desc">本视图将在 v2.2 后续版本迁移（T16：全屏路由条目表/卡片、筛选、批量状态、保存）。</p>' +
    '<p class="v22-ph-params" data-ph="params"></p>' +
    "</div>";
  paramsBox = el.querySelector('[data-ph="params"]');
  cached = el;
  return el;
}

export function onEnter(params) {
  if (paramsBox) {
    paramsBox.textContent = params && params.id
      ? "任务 ID：" + String(params.id)
      : "";
  }
}

export function onLeave() {
  /* 占位视图无全局资源需要清理 */
}
