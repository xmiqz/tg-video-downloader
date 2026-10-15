// ============================================================
// views/login.js — T8 占位视图：登录（手机号/验证码/2FA 三步）
// 契约：t8_contract.md §1 模块形状 { path, title, mount, onEnter, onLeave }
// 归属波次：T11 替换本文件与 css/views/login.css（零改基座）。
// T8 只做「迁移中」空态，禁止实现业务。
// ============================================================

export const path = "login";

export const title = "登录 - TG 视频下载器";

let cached = null;
let paramsBox = null;

export function mount(container) {
  if (cached) return cached;
  const el = document.createElement("div");
  el.className = "v22-placeholder-view";
  el.setAttribute("data-view-name", "login");
  el.innerHTML =
    '<div class="v22-ph-card">' +
    '<div class="v22-ph-ico" aria-hidden="true">🔑</div>' +
    '<h2 class="v22-ph-title">登录</h2>' +
    '<p class="v22-ph-desc">本视图将在 v2.2 后续版本迁移（T11：手机号 / 验证码 / 两步验证三步）。</p>' +
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
  /* 占位视图无全局资源需要清理（T11 如需在离开时清理，在此实现） */
}
