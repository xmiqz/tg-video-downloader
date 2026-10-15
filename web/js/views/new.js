// ============================================================
// views/new.js — T8 占位视图：新建下载（搜索 + 预览分步）
// 归属波次：T12 替换本文件与 css/views/new.css（零改基座）。
// T8 只做「迁移中」空态，禁止实现业务（不预埋命名模板等候选池字段）。
// ============================================================

export const path = "new";

export const title = "新建下载 - TG 视频下载器";

let cached = null;
let paramsBox = null;

export function mount(container) {
  if (cached) return cached;
  const el = document.createElement("div");
  el.className = "v22-placeholder-view";
  el.setAttribute("data-view-name", "new");
  el.innerHTML =
    '<div class="v22-ph-card">' +
    '<div class="v22-ph-ico" aria-hidden="true">＋</div>' +
    '<h2 class="v22-ph-title">新建下载</h2>' +
    '<p class="v22-ph-desc">本视图将在 v2.2 后续版本迁移（T12：单列分步 / 高级筛选折叠 / 预览批量勾选）。</p>' +
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
