// ============================================================
// dom.js — T10 组件库 DOM 工具（唯一公共工厂 el）
//
// 契约（t10_contract.md §2）：el(tag, attrs, children)
//   attrs 键约定：
//     class     → className
//     dataset   → {k:v} → data-k（值转 String，v 为 true 时仅置属性名）
//     style     → {k:v} 逐条 style[k]=v
//     text      → 文本内容（与 children 互斥，后写者胜）
//     html      → innerHTML（同上）
//     on*       → addEventListener(去掉 on 前缀，小写)
//     其余常规键 → setAttribute（aria-* / data-* / 布尔属性 String(v)）
//   children：Node | string | number | Array（扁平化，null/undefined 跳过）
// 组件自建自销（t10 任务书 §3：不 import 旧壳；destroy 一律移除监听与节点）。
// ============================================================

const BOOL_ATTRS = new Set(["disabled", "checked", "hidden", "autofocus", "required", "readonly", "multiple", "selected"]);

export function el(tag, attrs, children) {
  const node = document.createElement(tag);
  if (attrs) {
    for (const key of Object.keys(attrs)) {
      const v = attrs[key];
      if (v === undefined || v === null) continue;
      if (key === "class") { node.className = String(v); continue; }
      if (key === "style" && typeof v === "object") { for (const sk of Object.keys(v)) node.style[sk] = v[sk]; continue; }
      if (key === "dataset" && typeof v === "object") {
        for (const dk of Object.keys(v)) {
          const dv = v[dk];
          if (dv === undefined || dv === null) continue;
          if (dv === true) node.setAttribute("data-" + dk, "");
          else node.setAttribute("data-" + dk, String(dv));
        }
        continue;
      }
      if (key === "text") { node.textContent = String(v); continue; }
      if (key === "html") { node.innerHTML = String(v); continue; }
      if (key.slice(0, 2) === "on" && typeof v === "function") { node.addEventListener(key.slice(2).toLowerCase(), v); continue; }
      if (BOOL_ATTRS.has(key)) { if (v === true) node.setAttribute(key, ""); else if (v === false) node.removeAttribute(key); else node.setAttribute(key, String(v)); continue; }
      node.setAttribute(key, String(v));
    }
  }
  if (children !== undefined && children !== null) append(node, children);
  return node;
}

export function append(node, children) {
  const list = Array.isArray(children) ? children : [children];
  for (const c of list) {
    if (c === undefined || c === null) continue;
    if (c.nodeType) node.appendChild(c);
    else node.append(String(c));
  }
  return node;
}
