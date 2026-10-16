// ============================================================
// actionbar.js — T10 底部动作条（safe-area 内聚，与选择控制器解耦）
//
// 对齐旧壳 .bulk-bar（style.css:696-703）与 #active-bulk（index.html:
// 142-149：计数 + 主/副按钮排）、「已选 N」计数文案（app.js:1162-1163）
// 及编辑态 #edit-bulk/#bulk-count（index.html:466-471 / app.js:2219）。
// 解耦联动（任务书 §2.8）：本组件不 import selection.js——计数与按钮
// 可用性由消费方在 selection change 订阅里调 setCount/setEnabled 布线
// （TR10-1 H 组以手工布线复验两者联动且互不依赖）。
// safe-area：底边距内聚 env(safe-area-inset-bottom)（FR-23，CSS 侧）；
// 命中区 ≥44px（FR-24）。
// ============================================================

export function createBottomActionBar(opts) {
  const o = opts || {};
  const doc = o.doc || (typeof document !== "undefined" ? document : null);
  if (!doc) throw new Error("[v22ui] createBottomActionBar 需要可用的 document（host 环境）");
  const countFmt = typeof o.countLabel === "function" ? o.countLabel : function (n) { return "已选 " + n; };
  const actionDefs = (Array.isArray(o.actions) ? o.actions : []).map(function (a) {
    return {
      key: a && a.key ? String(a.key) : "",
      label: a && a.label != null ? String(a.label) : "",
      primary: !!(a && a.primary),
      danger: !!(a && a.danger),
      enabled: !(a && a.enabled === false),
    };
  }).filter(function (a) { return a.key; });
  let count = typeof o.count === "number" ? o.count : 0;
  let disposed = false;

  const root = doc.createElement("div");
  root.className = "u-actionbar";
  root.setAttribute("role", "toolbar");
  if (o.ariaLabel) root.setAttribute("aria-label", String(o.ariaLabel));

  const countEl = doc.createElement("span");
  countEl.className = "u-actionbar-count";
  countEl.textContent = countFmt(count);
  root.append(countEl);

  const btnMap = new Map();
  for (let i = 0; i < actionDefs.length; i++) {
    (function (a) {
      const b = doc.createElement("button");
      b.type = "button";
      b.className = "u-btn u-btn--md" + (a.primary ? " u-btn--primary" : a.danger ? " u-btn--danger" : " u-btn--ghost");
      b.dataset.action = a.key;
      b.textContent = a.label;
      b.disabled = !a.enabled;
      b.addEventListener("click", function () {
        if (disposed || b.disabled) return;
        if (typeof o.onAction === "function") {
          try { o.onAction(a.key); }
          catch (e) { console.debug("[v22ui] actionbar onAction 异常：", e); }
        }
      });
      btnMap.set(a.key, b);
      root.append(b);
    })(actionDefs[i]);
  }

  if (o.hidden === true) root.hidden = true;

  return {
    el: root,
    setCount: function (n) {
      if (disposed) return this;
      count = typeof n === "number" ? n : 0;
      countEl.textContent = countFmt(count);
      return this;
    },
    getCount: function () { return count; },
    setEnabled: function (key, b) {
      if (disposed) return this;
      const btn = btnMap.get(String(key));
      if (btn) btn.disabled = !(b === true);
      return this;
    },
    show: function () { if (!disposed) root.hidden = false; return this; },
    hide: function () { if (!disposed) root.hidden = true; return this; },
    destroy: function () {
      if (disposed) return; // 幂等
      disposed = true;
      btnMap.clear();
      if (root.isConnected) root.remove();
    },
  };
}
