// ============================================================
// segmented.js — T10 分段控件（单选 + 手机横滑）
//
// 纯函数（seam S4，node 离线可测）：
//   resolveSegmentedValue(items, value) → 合法值原样 | 未知值 null
//     （set() 拒绝未知值且不触发 onChange——app.js 时代 setTaskFilter
//      只接受已知过滤键的同义约束）；
//   nextSegmentIndex(count, current, delta) → index 钳制 [0, count-1]，
//     边界不回绕、越界步长钳至边界、delta 0 不动；
//     （键盘 ←/→ 专用；count<=1 或非法输入恒 0）
//   clampScrollLeft(left, scrollWidth, clientWidth) → 横滑边界数学：
//     max = scrollWidth - clientWidth；max<=0（无溢出）恒 0；负值钳 0；
//     超 max 钳 max（编程式 scrollTo 用；对齐旧壳筛选器可横滑
//      FR-25 ④「筛选器可横滑」与 t8 契约 §5 窄容器场景）。
//
// DOM（对齐旧壳 .segmented/.seg 胶囊形态 style.css:360-377 + index.html
// #task-filters 结构）：.u-seg > .u-seg-scroll > .u-seg-item[]。
// is-active/aria-selected 单选；scroll 容器 overflow-x:auto 天然支持
// 触摸横滑；keydown ←/→ 走 nextSegmentIndex。
// ============================================================

export function resolveSegmentedValue(items, value) {
  const list = Array.isArray(items) ? items : [];
  const v = String(value);
  for (let i = 0; i < list.length; i++) {
    const it = list[i] || {};
    if (String(it.value) === v) return value;
  }
  return null;
}

export function nextSegmentIndex(count, current, delta) {
  const n = typeof count === "number" ? count : 0;
  const c = typeof current === "number" ? current : 0;
  const d = typeof delta === "number" ? delta : 0;
  if (!(n >= 1)) return 0;
  const last = n - 1;
  const idx = Math.max(0, Math.min(last, Math.max(0, Math.floor(c))));
  const step = Math.trunc(d);
  if (step === 0) return idx;
  return Math.max(0, Math.min(last, idx + step));
}

export function clampScrollLeft(left, scrollWidth, clientWidth) {
  const sw = typeof scrollWidth === "number" ? scrollWidth : 0;
  const cw = typeof clientWidth === "number" ? clientWidth : 0;
  const max = sw - cw;
  if (!(max > 0)) return 0; // 无溢出恒 0
  const l = typeof left === "number" ? left : 0;
  return Math.max(0, Math.min(max, l));
}

export function createSegmented(items, opts) {
  const o = opts || {};
  const doc = o.doc || (typeof document !== "undefined" ? document : null);
  if (!doc) throw new Error("[v22ui] createSegmented 需要可用的 document（host 环境）");
  const list = (Array.isArray(items) ? items : []).map(function (it, i) {
    const x = it || {};
    return { value: String(x.value != null ? x.value : String(i)), label: String(x.label != null ? x.label : x.value), title: x.title != null ? String(x.title) : null };
  });
  let current = null;
  let disposed = false;

  const root = doc.createElement("div");
  root.className = "u-seg";
  root.setAttribute("role", "tablist");
  if (o.ariaLabel) root.setAttribute("aria-label", String(o.ariaLabel));

  const scroll = doc.createElement("div");
  scroll.className = "u-seg-scroll";
  scroll.setAttribute("role", "presentation");

  const btns = [];
  for (let i = 0; i < list.length; i++) {
    (function (item, idx) {
      const b = doc.createElement("button");
      b.type = "button";
      b.className = "u-seg-item";
      b.dataset.value = item.value;
      b.setAttribute("role", "tab");
      b.setAttribute("aria-selected", "false");
      b.textContent = item.label;
      if (item.title) b.title = item.title;
      b.addEventListener("click", function () {
        api.set(item.value, { byUser: true });
      });
      btns.push(b);
      scroll.append(b);
    })(list[i], i);
  }
  root.append(scroll);

  function render() {
    const active = current != null ? String(current) : null;
    for (let i = 0; i < btns.length; i++) {
      const on = active !== null && btns[i].dataset.value === active;
      btns[i].classList.toggle("is-active", on);
      btns[i].setAttribute("aria-selected", on ? "true" : "false");
    }
  }

  function nav(delta) {
    const idx = nextSegmentIndex(list.length, indexOf(current), delta);
    if (list.length) api.set(list[idx].value, { byUser: true, byKey: true });
  }

  function indexOf(v) {
    if (v == null) return 0;
    for (let i = 0; i < list.length; i++) if (list[i].value === String(v)) return i;
    return 0;
  }

  root.addEventListener("keydown", function (e) {
    if (disposed) return;
    if (e.key === "ArrowLeft") { e.preventDefault(); nav(-1); }
    else if (e.key === "ArrowRight") { e.preventDefault(); nav(1); }
  });

  const api = {
    el: root,
    get: function () { return current; },
    set: function (value, flags) {
      if (disposed) return api;
      const v = resolveSegmentedValue(list, value);
      if (v === null) return api; // 未知值拒绝（不触发 onChange）
      if (current != null && String(v) === String(current)) return api; // 幂等
      current = v;
      render();
      if (typeof o.onChange === "function") {
        try { o.onChange(v, flags || {}); }
        catch (e) { console.debug("[v22ui] segmented onChange 异常：", e); }
      }
      return api;
    },
    scrollToIndex: function (i) {
      if (disposed || !scroll || !list.length) return api;
      const target = scroll.children[Math.max(0, Math.min(list.length - 1, i))];
      if (!target) return api;
      scroll.scrollLeft = clampScrollLeft(target.offsetLeft - 4 /* 4px 栅格（--space-1 等值；滚动边距组件固有常量） */, scroll.scrollWidth, scroll.clientWidth);
      return api;
    },
    destroy: function () {
      if (disposed) return; // 幂等
      disposed = true;
      if (root.isConnected) root.remove();
    },
  };

  if (o.value !== undefined) api.set(o.value, {});
  else render();
  return api;
}
