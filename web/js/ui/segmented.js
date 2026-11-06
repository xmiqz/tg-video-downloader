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
  let pendingSync = 0;
  let syncTries = 0;
  let resizeObserver = null;
  let resizeHandler = null;

  const root = doc.createElement("div");
  root.className = "u-seg";
  root.setAttribute("role", "tablist");
  if (o.ariaLabel) root.setAttribute("aria-label", String(o.ariaLabel));

  const scroll = doc.createElement("div");
  scroll.className = "u-seg-scroll";
  scroll.setAttribute("role", "presentation");

  // T64 弹簧高亮：选中态由独立 thumb 承担。thumb 元素常驻不重建，
  // 只更新 transform/width → 重绘后接着 glide，不会跳回起点。
  const thumb = doc.createElement("div");
  thumb.className = "u-seg-thumb";
  thumb.setAttribute("aria-hidden", "true");
  scroll.append(thumb);

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

  function syncThumb() {
    const active = current != null ? String(current) : null;
    let target = null;
    for (let i = 0; i < btns.length; i++) {
      if (active !== null && btns[i].dataset.value === active) { target = btns[i]; break; }
    }
    if (!target) { thumb.classList.remove("is-on"); return; }
    // A segmented control is usually constructed before it is appended to
    // the view. offsetWidth/offsetLeft are then 0/0, so painting the thumb
    // immediately leaves a collapsed sliver at the left edge until a later
    // interaction forces layout. Only reveal it after a real measurement.
    const w = target.offsetWidth || 0;
    const x = target.offsetLeft || 0;
    const h = target.offsetHeight || 0;
    const y = target.offsetTop || 0;
    if (!w || !h) {
      thumb.classList.remove("is-on");
      if (syncTries < 4) scheduleSync();
      return;
    }
    syncTries = 0;
    thumb.style.width = w + "px";
    thumb.style.height = h + "px";
    thumb.style.top = y + "px";
    thumb.style.transform = "translateX(" + x + "px)";
    thumb.classList.add("is-on");
  }

  function scheduleSync() {
    if (disposed || pendingSync) return;
    syncTries++;
    const win = typeof window !== "undefined" ? window : null;
    if (win && typeof win.requestAnimationFrame === "function") {
      pendingSync = win.requestAnimationFrame(function () {
        pendingSync = 0;
        syncThumb();
      });
      return;
    }
    pendingSync = setTimeout(function () {
      pendingSync = 0;
      syncThumb();
    }, 16);
  }

  // The view can append the segmented control one or more frames after
  // construction (cards, overlays, editor panels), and a later responsive
  // reflow can move/rewrap it (notably the 2×2 appearance control). Keep a
  // passive layout observer alive for the component lifetime so the thumb is
  // re-measured after breakpoint, font, or host-mode layout changes. It only
  // schedules a paint when layout actually changes; there is no timer loop.
  function watchLayout() {
    const win = typeof window !== "undefined" ? window : null;
    const RO = win && win.ResizeObserver;
    if (typeof RO === "function") {
      const ro = new RO(function () {
        if (disposed) return;
        scheduleSync();
      });
      resizeObserver = ro;
      ro.observe(root);
    }
    if (win && typeof win.addEventListener === "function") {
      resizeHandler = function () { scheduleSync(); };
      win.addEventListener("resize", resizeHandler, { passive: true });
    }
    // Web fonts can change button widths after the first measurement without
    // resizing the outer root. Re-sync once the font set settles.
    const fonts = doc.fonts;
    if (fonts && fonts.ready && typeof fonts.ready.then === "function") {
      fonts.ready.then(function () {
        if (!disposed) scheduleSync();
      }).catch(function () {});
    }
    if (typeof RO !== "function") scheduleSync();
  }

  function render() {
    const active = current != null ? String(current) : null;
    for (let i = 0; i < btns.length; i++) {
      const on = active !== null && btns[i].dataset.value === active;
      btns[i].classList.toggle("is-active", on);
      btns[i].setAttribute("aria-selected", on ? "true" : "false");
    }
    syncThumb();
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
      if (pendingSync) {
        const win = typeof window !== "undefined" ? window : null;
        if (win && typeof win.cancelAnimationFrame === "function") {
          win.cancelAnimationFrame(pendingSync);
        } else {
          clearTimeout(pendingSync);
        }
        pendingSync = 0;
      }
      if (resizeObserver) {
        resizeObserver.disconnect();
        resizeObserver = null;
      }
      if (resizeHandler) {
        const win = typeof window !== "undefined" ? window : null;
        if (win && typeof win.removeEventListener === "function") {
          win.removeEventListener("resize", resizeHandler);
        }
        resizeHandler = null;
      }
      if (root.isConnected) root.remove();
    },
  };

  if (o.value !== undefined) api.set(o.value, {});
  else render();
  scheduleSync();
  watchLayout();
  return api;
}
