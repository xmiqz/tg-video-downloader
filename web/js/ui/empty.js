// ============================================================
// empty.js — T10 空态组件
//
// 对齐旧壳 .empty-state（style.css:595-599：居中、大留白）与 index.html
// #active-empty / #task-empty 结构（index.html:172-177 / 198-200：
// .empty-ico + h3 + p + 可选动作按钮）。
// 空态图标 bob 浮动（对齐 .empty-ico 3s 动画 style.css:596）；
// prefers-reduced-motion 下动画归零（tokens.css 5.1）。
// ============================================================

export function createEmptyState(opts) {
  const o = opts || {};
  const doc = o.doc || (typeof document !== "undefined" ? document : null);
  if (!doc) throw new Error("[v22ui] createEmptyState 需要可用的 document（host 环境）");
  let disposed = false;

  const root = doc.createElement("div");
  root.className = "u-empty";
  root.setAttribute("role", "status");

  const icoEl = doc.createElement("div");
  icoEl.className = "u-empty-ico";
  icoEl.setAttribute("aria-hidden", "true");
  icoEl.textContent = o.icon == null ? "🫧" : String(o.icon);

  const titleEl = doc.createElement("h3");
  titleEl.className = "u-empty-title";
  titleEl.textContent = String(o.title == null ? "" : o.title);

  const hintEl = doc.createElement("p");
  hintEl.className = "u-empty-hint";
  hintEl.textContent = String(o.hint == null ? "" : o.hint);
  hintEl.hidden = o.hint == null;

  root.append(icoEl, titleEl, hintEl);

  return {
    el: root,
    set: function (s) {
      if (disposed || !s) return this;
      if (s.icon !== undefined) icoEl.textContent = String(s.icon == null ? "" : s.icon);
      if (s.title !== undefined) titleEl.textContent = String(s.title == null ? "" : s.title);
      if (s.hint !== undefined) {
        hintEl.textContent = String(s.hint == null ? "" : s.hint);
        hintEl.hidden = s.hint == null;
      }
      return this;
    },
    destroy: function () {
      if (disposed) return; // 幂等
      disposed = true;
      if (root.isConnected) root.remove();
    },
  };
}
