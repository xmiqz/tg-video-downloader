// ============================================================
// card.js — T10 卡片容器
//
// 对齐旧壳 .glass 卡容器（style.css:175）：玻璃材质全部经令牌派生
// （--glass-card-*，tokens.css §2.2 玻璃三档之卡片档），本组件不复制
// shell.css 的玻璃底栏实现（任务书 §3：不得复制 .sh-bottombar 的
// 玻璃底栏实现，通过令牌派生）。
// 结构：.u-card > .u-card-head(.u-card-title + .u-card-sub) + .u-card-body。
// 标题/副题均为可选（空→head 折叠 hidden），head 支持右侧插槽
// hostRight（Node，如分段控件）。
// ============================================================

export function createCard(opts) {
  const o = opts || {};
  const doc = o.doc || (typeof document !== "undefined" ? document : null);
  if (!doc) throw new Error("[v22ui] createCard 需要可用的 document（host 环境）");
  let disposed = false;

  const root = doc.createElement("section");
  root.className = "u-card" + (o.pad === false ? " u-card--flush" : "");
  if (o.ariaLabel) root.setAttribute("aria-label", String(o.ariaLabel));

  const head = doc.createElement("div");
  head.className = "u-card-head";
  const titleEl = doc.createElement("h3");
  titleEl.className = "u-card-title";
  titleEl.textContent = String(o.title == null ? "" : o.title);
  const subEl = doc.createElement("p");
  subEl.className = "u-card-sub";
  subEl.textContent = String(o.sub == null ? "" : o.sub);
  subEl.hidden = o.sub == null;
  head.append(titleEl, subEl);
  if (o.hostRight && o.hostRight.nodeType) head.append(o.hostRight);
  head.hidden = o.title == null && o.sub == null;
  root.append(head);

  const body = doc.createElement("div");
  body.className = "u-card-body";
  if (o.content) {
    if (o.content.nodeType) body.append(o.content);
    else body.textContent = String(o.content);
  }
  root.append(body);

  function syncHead() {
    head.hidden = titleEl.textContent === "" && subEl.hidden && head.children.length <= 2;
  }

  return {
    el: root,
    body: body,
    head: head,
    set: function (s) {
      if (disposed || !s) return this;
      if (s.title !== undefined) { titleEl.textContent = String(s.title == null ? "" : s.title); }
      if (s.sub !== undefined) {
        subEl.textContent = String(s.sub == null ? "" : s.sub);
        subEl.hidden = s.sub == null;
      }
      syncHead();
      return this;
    },
    add: function (child) {
      if (disposed) return this;
      if (child && child.nodeType) body.append(child);
      return this;
    },
    destroy: function () {
      if (disposed) return; // 幂等
      disposed = true;
      if (root.isConnected) root.remove();
    },
  };
}
