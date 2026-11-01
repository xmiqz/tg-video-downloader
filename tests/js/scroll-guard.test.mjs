// T64 点击不漂移：读者滚动守卫。
//
// 回归红线（任务书 §2 T64 验收）：
//   · 三类非读者滚动（编程 / 布局 / 焦点）一律还原到读者上次停留位置；
//   · 只有 isTrusted 的滚轮/触摸/指针/键盘输入才开读者窗口；
//   · 窗口外的滚动同样按非读者处理（不会把布局抖动当读者意图）。
import test from "node:test";
import assert from "node:assert/strict";

import {
  READER_WINDOW_MS, READER_INPUTS, NON_READER_SOURCES, TOUCH_MOMENTUM_MS,
  isReaderInput, windowFor, createGuard, createScrollGuard,
} from "../../web/js/motion/scroll-guard.js";

test("isReaderInput only trusts real reader gestures", () => {
  assert.equal(isReaderInput({ type: "wheel", isTrusted: true }), true);
  assert.equal(isReaderInput({ type: "touchmove", isTrusted: true }), true);
  assert.equal(isReaderInput({ type: "keydown", isTrusted: true }), true);
  assert.equal(isReaderInput({ type: "wheel", isTrusted: false }), false, "合成事件不算读者输入");
  assert.equal(isReaderInput({ type: "scroll", isTrusted: true }), false, "scroll 事件本身不算输入");
  assert.equal(isReaderInput(null), false);
});

test("reader inputs and non-reader sources stay in sync with the doc", () => {
  assert.equal(READER_INPUTS.length, 6);
  for (const t of ["wheel", "touchstart", "touchmove", "pointerdown", "keydown", "mousedown"]) {
    assert.ok(READER_INPUTS.includes(t), "缺少读者输入类型：" + t);
  }
  assert.deepEqual(NON_READER_SOURCES, ["programmatic", "layout", "focus"]);
});

test("reader window lets a trusted wheel scroll through", () => {
  const g = createGuard();
  g.noteInput({ type: "wheel", isTrusted: true }, 100);
  const d = g.decide("a", 40, 100 + READER_WINDOW_MS - 1);
  assert.equal(d.restore, false, "窗口内读者滚动不还原");
  assert.equal(d.top, 40);
  assert.equal(g.topFor("a"), 40);
});

test("programmatic scroll is restored to the reader position", () => {
  const g = createGuard();
  g.noteReader(500);                       // 读者刚离开（上次输入）
  assert.equal(g.decide("p", 10, 500).restore, false);
  const d = g.decide("p", 300, 500 + READER_WINDOW_MS + 1000);
  assert.equal(d.restore, true, "scrollTop 被代码改写必须还原");
  assert.equal(d.top, 10);
});

test("layout scroll is restored to the reader position", () => {
  const g = createGuard();
  g.noteReader(0);
  g.decide("l", 0, 0);
  const d = g.decide("l", 260, READER_WINDOW_MS + 900);
  assert.equal(d.restore, true, "重排导致的位移必须还原");
  assert.equal(d.top, 0);
});

test("focus scroll is restored to the reader position", () => {
  const g = createGuard();
  g.noteReader(0);
  g.decide("f", 12, 0);
  const d = g.decide("f", 480, READER_WINDOW_MS + 300);
  assert.equal(d.restore, true, "focus/scrollIntoView 位移必须还原");
  assert.equal(d.top, 12);
});

test("first non-reader scroll with no history is kept (nothing to restore)", () => {
  const g = createGuard();
  const d = g.decide("fresh", 0, 0);
  assert.equal(d.restore, false);
  assert.equal(d.top, 0);
});

test("touch momentum stays reader scroll for the longer window", () => {
  assert.equal(windowFor("touchmove", READER_WINDOW_MS), TOUCH_MOMENTUM_MS);
  assert.equal(windowFor("wheel", READER_WINDOW_MS), READER_WINDOW_MS);
  const g = createGuard();
  g.noteInput({ type: "touchmove", isTrusted: true }, 0);
  const d = g.decide("t", 100, TOUCH_MOMENTUM_MS - 300);
  assert.equal(d.restore, false, "松手后的惯性滚动不得被还原");
  assert.equal(g.topFor("t"), 100);
  const late = g.decide("t", 400, TOUCH_MOMENTUM_MS + 500);
  assert.equal(late.restore, true);
  assert.equal(late.top, 100);
});

// ---------- DOM 安装器（假 window / 假容器） ----------

function fakeWindow() {
  const handlers = new Map();
  let clock = 0;
  return {
    performance: { now: () => clock },
    advance(ms) { clock += ms; },
    addEventListener(type, fn) {
      if (!handlers.has(type)) handlers.set(type, new Set());
      handlers.get(type).add(fn);
    },
    removeEventListener(type, fn) {
      if (handlers.has(type)) handlers.get(type).delete(fn);
    },
    fire(type, ev) {
      const set = handlers.get(type);
      if (set) set.forEach((fn) => fn(ev));
      return set ? set.size : 0;
    },
    listenerCount() {
      let n = 0;
      handlers.forEach((set) => { n += set.size; });
      return n;
    },
  };
}

function fakeEl(top) {
  const handlers = new Set();
  return {
    scrollTop: top,
    addEventListener(type, fn) { if (type === "scroll") handlers.add(fn); },
    removeEventListener(type, fn) { if (type === "scroll") handlers.delete(fn); },
    fireScroll() { handlers.forEach((fn) => fn({ type: "scroll" })); },
    listenerCount() { return handlers.size; },
  };
}

test("createScrollGuard restores non-reader scroll on the real container", () => {
  const win = fakeWindow();
  const el = fakeEl(0);
  const guard = createScrollGuard({ win });
  assert.equal(guard.attach(el), true);
  assert.equal(win.listenerCount(), READER_INPUTS.length, "读者输入全局监听应就位");

  win.fire("wheel", { type: "wheel", isTrusted: true });   // 读者滚动
  el.scrollTop = 120;
  el.fireScroll();
  assert.equal(el.scrollTop, 120, "读者滚动保留");

  win.advance(READER_WINDOW_MS + 500);
  el.scrollTop = 400;                                    // 非读者（布局/焦点/编程）
  el.fireScroll();
  assert.equal(el.scrollTop, 120, "非读者滚动必须还原到 120");
});

test("createScrollGuard ignores untrusted synthetic input", () => {
  const win = fakeWindow();
  const el = fakeEl(0);
  const guard = createScrollGuard({ win });
  guard.attach(el);
  win.fire("wheel", { type: "wheel", isTrusted: false });
  el.scrollTop = 200;
  el.fireScroll();
  assert.equal(el.scrollTop, 0, "合成输入不开读者窗口，位移被还原");
});

test("destroy detaches every listener", () => {
  const win = fakeWindow();
  const el = fakeEl(0);
  const guard = createScrollGuard({ win });
  guard.attach(el);
  guard.destroy();
  assert.equal(win.listenerCount(), 0, "全局监听应清空");
  assert.equal(el.listenerCount(), 0, "容器 scroll 监听应清空");
  el.scrollTop = 90;
  el.fireScroll();
  assert.equal(el.scrollTop, 90, "卸载后不再干预滚动");
});

test("createScrollGuard degrades without a window", () => {
  const g = createScrollGuard({ win: null });
  assert.equal(typeof g.attach, "function");
  assert.equal(g.attach({}), undefined);
  assert.deepEqual(g.destroy(), undefined);
});
