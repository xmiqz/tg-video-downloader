// ============================================================
// overlay.js — T10 模态 / 底部弹层 同构组件
//
// 一套 API 两种呈现（FR-25 / seams.md S3）：
//   宽度 ≥768px（含端）→ 居中模态（data-form="modal"）；
//   <768px / 无效宽度  → 贴底 bottom sheet（data-form="sheet"，移动安全默认）。
//   OVERLAY_FORM_BREAKPOINT === 768（FR-23 ≥768 平板起为桌面形态口径，
//   t8_contract.md §5 断点族的弹层子断点）。
//
// 行为（对齐旧壳映射，详 t10_contract.md 映射表）：
//   - .modal-mask 居中模态（style.css:706-716 + index.html .modal-mask）；
//   - 底部弹层为新形态（FR-24 手机端底部弹层替代右键/悬浮栏；旧壳
//     #sheet 为右侧滑层 index.html:420，本组件为贴底，形态差异为
//     v2.2 响应式规格 FR-25 的新设计，交互反馈模式对齐：遮罩点击关闭、
//     打开过渡 + 关闭延迟隐藏（style.css .show/closeSheet app.js:1863-
//     1867 的 450ms 延迟模式，本组件用 420ms = --dur-lg）。
//   - ESC 关闭（对齐 T8 抽屉 closeDrawer 的 Escape 键语义 router.js:511-513）
//   - 滚动锁定：open 时锁定文档滚动（.u-scroll-lock on documentElement）；
//   - 焦点陷阱基础：open 焦点入容器、Tab 在容器可聚焦元素间循环；
//   - destroy：移除监听（resize/keydown/mask click 中的 document 级）、
//     清理计时器、解锁滚动、节点移除；幂等（R2 教训：无异步装载，无
//     缓存态；失败路径无 Promise）。
// ============================================================

export const OVERLAY_FORM_BREAKPOINT = 768;

export function resolveOverlayForm(width) {
  const w = typeof width === "number" ? width : Number(width);
  return Number.isFinite(w) && w >= OVERLAY_FORM_BREAKPOINT ? "modal" : "sheet";
}

export function createOverlay(opts) {
  const o = opts || {};
  const doc = o.doc || (typeof document !== "undefined" ? document : null);
  const win = o.win || (typeof window !== "undefined" ? window : null);
  if (!doc) throw new Error("[v22ui] createOverlay 需要可用的 document（host 环境）");
  const formFixed = o.form === "modal" || o.form === "sheet" ? o.form : "auto";
  let disposed = false;
  let opened = false;
  let closeTimer = 0;
  const FOCUSABLE = 'a[href],button:not([disabled]),input:not([disabled]):not([type="hidden"]),select:not([disabled]),textarea:not([disabled]),[tabindex]:not([tabindex="-1"])';

  const root = doc.createElement("div");
  root.className = "u-overlay";
  root.dataset.form = "sheet";
  root.hidden = true;
  root.setAttribute("role", "dialog");
  root.setAttribute("aria-modal", "true");
  if (o.ariaLabel) root.setAttribute("aria-label", String(o.ariaLabel));

  const mask = doc.createElement("div");
  mask.className = "u-overlay-mask";
  root.append(mask);

  const panel = doc.createElement("div");
  panel.className = "u-overlay-panel";

  const head = doc.createElement("div");
  head.className = "u-overlay-head";
  const titleEl = doc.createElement("h3");
  titleEl.className = "u-overlay-title";
  titleEl.textContent = String(o.title == null ? "" : o.title);
  const xBtn = doc.createElement("button");
  xBtn.type = "button";
  xBtn.className = "u-overlay-x";
  xBtn.setAttribute("aria-label", "关闭");
  xBtn.textContent = "✕";
  head.append(titleEl, xBtn);
  panel.append(head);

  const sub = doc.createElement("p");
  sub.className = "u-overlay-sub";
  sub.textContent = String(o.sub == null ? "" : o.sub);
  sub.hidden = true;
  panel.append(sub);

  const body = doc.createElement("div");
  body.className = "u-overlay-body";
  if (o.content) {
    if (o.content.nodeType) body.append(o.content);
    else body.textContent = String(o.content);
  }
  panel.append(body);

  const actionsRow = doc.createElement("div");
  actionsRow.className = "u-overlay-actions";
  const actionDefs = (Array.isArray(o.actions) ? o.actions : []).map(function (a) {
    return {
      key: a && a.key ? String(a.key) : "",
      label: a && a.label != null ? String(a.label) : "",
      primary: !!(a && a.primary),
      danger: !!(a && a.danger),
    };
  }).filter(function (a) { return a.key; });
  for (let i = 0; i < actionDefs.length; i++) {
    const a = actionDefs[i];
    const b = doc.createElement("button");
    b.type = "button";
    b.className = "u-btn" + (a.primary ? " u-btn--primary" : a.danger ? " u-btn--danger" : " u-btn--ghost");
    b.dataset.action = a.key;
    b.textContent = a.label;
    actionsRow.append(b);
  }
  if (actionDefs.length) panel.append(actionsRow);
  root.append(panel);

  const lockTarget = o.lockScrollTarget || doc.documentElement;

  const mqListeners = [];

  function syncForm() {
    if (disposed) return;
    const f = formFixed !== "auto" ? formFixed : resolveOverlayForm(win && win.innerWidth ? win.innerWidth : (doc.documentElement ? doc.documentElement.clientWidth : 0));
    if (root.dataset.form !== f) root.dataset.form = f;
  }

  function focusables() {
    const list = root.querySelectorAll(FOCUSABLE);
    return Array.prototype.filter.call(list, function (n) {
      return n.offsetParent !== null && n.offsetWidth + n.offsetHeight > 0;
    });
  }

  function onKeydown(e) {
    if (disposed || !opened) return;
    if (e.key === "Escape") {
      e.preventDefault();
      api.close("esc");
      return;
    }
    if (e.key === "Tab") {
      const f = focusables();
      if (!f.length) { e.preventDefault(); return; }
      const first = f[0];
      const last = f[f.length - 1];
      const cur = doc.activeElement && root.contains(doc.activeElement) ? doc.activeElement : null;
      if (!cur) { e.preventDefault(); first.focus(); return; }
      if (e.shiftKey && (cur === first || cur === root)) { e.preventDefault(); last.focus(); return; }
      if (!e.shiftKey && cur === last) { e.preventDefault(); first.focus(); return; }
    }
  }

  function onResize() { syncForm(); }

  mask.addEventListener("click", function () { api.close("mask"); });
  xBtn.addEventListener("click", function () { api.close("x"); });
  doc.addEventListener("keydown", onKeydown);
  if (win && win.addEventListener) {
    win.addEventListener("resize", onResize);
    if (win.matchMedia) {
      try {
        // 宽度断点实时切换（matchMedia 与 resize 双路，旋转/拖窗不刷新）
        const mq = win.matchMedia("(min-width: " + OVERLAY_FORM_BREAKPOINT + "px)");
        const onChange = function () { syncForm(); };
        if (mq.addEventListener) mq.addEventListener("change", onChange);
        else if (mq.addListener) mq.addListener(onChange);
        mqListeners.push({ mq: mq, fn: onChange });
      } catch (e) { /* 环境无 matchMedia：resize 兜底 */ }
    }
  }

  syncForm();

  const api = {
    el: root,
    panel: panel,
    body: body,
    get form() { return root.dataset.form; },
    isOpen: function () { return opened; },
    open: function () {
      if (disposed || opened) return api;
      syncForm();
      if (closeTimer) { clearTimeout(closeTimer); closeTimer = 0; }
      root.hidden = false;
      // 双 rAF：等 hidden=false 后挂 .is-open 触发进入过渡（对齐旧壳
      // closeSheet 的 rAF+show 类模式 app.js:1844-1845）
      (win && win.requestAnimationFrame ? win.requestAnimationFrame(function () {
        root.classList.add("is-open");
      }) : root.classList.add("is-open"));
      opened = true;
      lockTarget.classList.add("u-scroll-lock");
      const f = focusables();
      if (f.length) { try { f[0].focus(); } catch (e) { /* focus 失败不阻塞打开 */ } }
      return api;
    },
    close: function (reason) {
      if (disposed || !opened) return api;
      opened = false;
      const r = reason == null ? "x" : String(reason);
      root.classList.remove("is-open");
      lockTarget.classList.remove("u-scroll-lock");
      // 退出过渡完成后再 hidden（对齐旧壳 closeSheet 延迟隐藏模式）
      closeTimer = setTimeout(function () {
        closeTimer = 0;
        root.hidden = true;
      }, 420);
      if (typeof o.onClose === "function") {
        try { o.onClose(r); }
        catch (e) { console.debug("[v22ui] overlay onClose 异常：", e); }
      }
      return api;
    },
    destroy: function () {
      if (disposed) return; // 幂等
      disposed = true;
      if (closeTimer) { clearTimeout(closeTimer); closeTimer = 0; }
      root.classList.remove("is-open");
      lockTarget.classList.remove("u-scroll-lock");
      doc.removeEventListener("keydown", onKeydown);
      if (win && win.removeEventListener) win.removeEventListener("resize", onResize);
      for (let i = 0; i < mqListeners.length; i++) {
        const m = mqListeners[i];
        try {
          if (m.mq.removeEventListener) m.mq.removeEventListener("change", m.fn);
          else if (m.mq.removeListener) m.mq.removeListener(m.fn);
        } catch (e) { /* 匹配器已失效 */ }
      }
      mqListeners.length = 0;
      if (root.isConnected) root.remove();
      opened = false;
    },
  };

  // 动作按钮（在 api 定义后绑定，走公开 onAction 语义）
  for (let i = 0; i < actionDefs.length; i++) {
    (function (a) {
      root.querySelector('.u-overlay-actions [data-action="' + a.key + '"]').addEventListener("click", function () {
        if (disposed || !opened) return;
        let keep = false;
        if (typeof o.onAction === "function") {
          try { keep = o.onAction(a.key) === false; }
          catch (e) { console.debug("[v22ui] overlay onAction 异常：", e); }
        }
        if (!keep) api.close("action");
      });
    })(actionDefs[i]);
  }

  return api;
}
