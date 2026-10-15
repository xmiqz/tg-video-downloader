// ============================================================
// views/settings.js — T8 占位视图：设置（外观/凭据/下载目录/多账号）
// 归属波次：T15 替换本文件与 css/views/settings.css（零改基座）。
// T8 只做「迁移中」空态，禁止实现业务（不预埋上传目标等候选池分区）。
// 主题切换钩子：外壳消费 <html data-theme>（T7 契约），T15 实现交互。
// ============================================================

export const path = "settings";

export const title = "设置 - TG 视频下载器";

let cached = null;
let paramsBox = null;

export function mount(container) {
  if (cached) return cached;
  const el = document.createElement("div");
  el.className = "v22-placeholder-view";
  el.setAttribute("data-view-name", "settings");
  el.innerHTML =
    '<div class="v22-ph-card">' +
    '<div class="v22-ph-ico" aria-hidden="true">⚙</div>' +
    '<h2 class="v22-ph-title">设置</h2>' +
    '<p class="v22-ph-desc">本视图将在 v2.2 后续版本迁移（T15：分组折叠卡片，外观切换即时生效）。</p>' +
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
