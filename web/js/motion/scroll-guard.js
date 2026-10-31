// ============================================================
// scroll-guard.js — T64 点击不漂移：读者滚动守卫
//
// 规则：只有读者发起的事件（isTrusted 的滚轮/触摸/指针/键盘）在窗口期内
// 允许改变滚动位置；其余三类非读者滚动——
//   ① 代码调用 scrollTo/scrollTop 的「编程滚动」
//   ② 重绘/布局变化导致的「布局滚动」
//   ③ focus()/scrollIntoView 带来的「焦点滚动」
// —— 一律还原到读者上次停留的位置，避免被点控件从指针下滑走。
//
// 纯状态机 createGuard 可在 node 断言；createScrollGuard 才碰 DOM。
// ============================================================

/** 读者输入窗口：最后一次 isTrusted 输入后多久内的滚动仍算读者滚动 */
export const READER_WINDOW_MS = 350;

/** 触摸惯性余量：松手后手指惯性仍会持续派发 scroll，不能当布局位移还原 */
export const TOUCH_MOMENTUM_MS = 1500;

/** 读者输入类型（真事件才采信；合成事件一律不算） */
export const READER_INPUTS = ["wheel", "touchstart", "touchmove", "pointerdown", "keydown", "mousedown"];

/** 非读者滚动来源（三类都要被还原） */
export const NON_READER_SOURCES = ["programmatic", "layout", "focus"];

/** 某类输入对应的读者窗口：触摸走惯性余量，其余走默认窗口。 */
export function windowFor(type, fallback) {
  const t = String(type || "");
  const d = typeof fallback === "number" ? fallback : READER_WINDOW_MS;
  return (t === "touchstart" || t === "touchmove") ? Math.max(d, TOUCH_MOMENTUM_MS) : d;
}

/** 一次输入是否算读者输入：必须 isTrusted 且类型在白名单内。 */
export function isReaderInput(ev) {
  if (!ev || ev.isTrusted !== true) return false;
  const type = String(ev.type || "");
  return READER_INPUTS.indexOf(type) >= 0;
}

/**
 * 守卫状态机（无 DOM）。
 *
 * decide(key, top, now) → { restore: boolean, top: number }
 *   restore=true 时调用方应把滚动位置写回 top。
 */
export function createGuard(opts) {
  const o = opts || {};
  const winMs = typeof o.windowMs === "number" ? o.windowMs : READER_WINDOW_MS;
  const tops = new Map();
  let readerAt = -Infinity;
  let readerWin = winMs;
  return {
    noteInput(ev, now) {
      if (!isReaderInput(ev)) return false;
      readerAt = typeof now === "number" ? now : 0;
      readerWin = windowFor(ev.type, winMs);
      return true;
    },
    noteReader(now) {
      readerAt = typeof now === "number" ? now : 0;
      readerWin = winMs;
    },
    decide(key, top, now) {
      const t = typeof now === "number" ? now : 0;
      const cur = Number(top) || 0;
      const reader = (t - readerAt) <= readerWin;
      if (reader) {
        tops.set(key, cur);
        return { restore: false, top: cur };
      }
      const prev = tops.has(key) ? tops.get(key) : cur;
      if (!tops.has(key)) tops.set(key, cur);   // 首帧非读者滚动：记住原地
      return { restore: prev !== cur, top: prev };
    },
    topFor(key) { return tops.get(key) || 0; },
  };
}

/**
 * DOM 安装器：把守卫挂到容器元素上。
 * opts: { win, doc, el }；el 为承接滚动的容器（可多个 attach）。
 * 返回 { attach(el), destroy() }。
 */
export function createScrollGuard(opts) {
  const o = opts || {};
  const win = o.win || (typeof window !== "undefined" ? window : null);
  if (!win) return { attach() {}, destroy() {}, guard: createGuard(o) };
  const guard = createGuard(o);
  const now = function () {
    return (win.performance && win.performance.now) ? win.performance.now() : Date.now();
  };
  const bound = [];
  let destroyed = false;

  function onInput(ev) { guard.noteInput(ev, now()); }

  function attach(el) {
    if (destroyed || !el || !el.addEventListener) return false;
    const key = bound.length;
    // 基线 = 挂载时的滚动位置：此后任何非读者滚动都还原到此，
    // 避免「尚未发生读者输入」时把布局位移当作读者意图采信。
    guard.decide(key, el.scrollTop, 0);
    function onScroll() {
      const d = guard.decide(key, el.scrollTop, now());
      if (d.restore) el.scrollTop = d.top;
    }
    el.addEventListener("scroll", onScroll, { passive: true });
    bound.push({ el, onScroll });
    return true;
  }

  READER_INPUTS.forEach(function (type) {
    win.addEventListener(type, onInput, { passive: true, capture: true });
  });

  return {
    guard,
    attach,
    destroy() {
      if (destroyed) return;
      destroyed = true;
      READER_INPUTS.forEach(function (type) {
        win.removeEventListener(type, onInput, { capture: true });
      });
      bound.forEach(function (b) { b.el.removeEventListener("scroll", b.onScroll); });
      bound.length = 0;
    },
  };
}
