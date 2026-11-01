// ============================================================
// calm.js — T64 动效框架「节拍」层（纯函数 + 注入式 rAF 驱动）
//
// 只做三件事：
//   1) 统一的节拍常量与缓出曲线（620ms 四段缓出）；
//   2) 数字滚动驱动器 createRoller：事件帧只改目标值，不重建 DOM；
//   3) reduced-motion 判定与 .still 抑制类开关（视觉终态保留）。
//
// 不涉及 DOM 布局读写，node 侧可用桩时钟逐帧断言。
// ============================================================

/** 数字滚动总时长（与 tokens.css --dur-count 同值） */
export const COUNT_MS = 620;
/** 缓出分档数（四段缓出） */
export const COUNT_STEPS = 4;

/** 标准缓出（quart）：起步快、收尾慢，适合读数滚到新值。 */
export function easeOutQuart(t) {
  const x = typeof t === "number" ? t : 0;
  if (x <= 0) return 0;
  if (x >= 1) return 1;
  return 1 - Math.pow(1 - x, 4);
}

/** 620ms 四段缓出的取样表：返回 steps+1 个值，首尾精确等于端点。 */
export function rollPlan(from, to, steps = COUNT_STEPS) {
  const a = Number(from) || 0;
  const b = Number(to) || 0;
  const n = Math.max(1, Math.floor(typeof steps === "number" ? steps : COUNT_STEPS));
  const out = [];
  for (let i = 0; i <= n; i++) out.push(a + (b - a) * easeOutQuart(i / n));
  out[0] = a;
  out[n] = b;
  return out;
}

/** reduced-motion 判定：接受 matchMedia 桩（{matches:true}）或直接布尔。 */
export function prefersReduced(media) {
  if (typeof media === "boolean") return media;
  return !!(media && media.matches === true);
}

/** 宿主 <html>/<root> 上挂 .still：抑制弹簧滑行与过渡（终态不动）。 */
export function markStill(root, still) {
  if (!root || !root.classList) return false;
  root.classList.toggle("still", !!still);
  return !!still;
}

/**
 * 数字滚动驱动器。
 *
 * opts:
 *   update(value, done)  每帧回调（只写文本，不重建 DOM）
 *   now() / raf(cb) / cancel(id)  可注入，默认取全局
 *   media                  matchMedia 结果或布尔（reduced → 直接跳终值）
 *   onDone()
 *
 * 动画飞行中再次 set() 只改目标值（from 保持当前显示值），
 * 不会重启计时 → 事件帧密集时也不产生多份动画对象。
 */
export function createRoller(opts) {
  const o = opts || {};
  const now = typeof o.now === "function" ? o.now : function () {
    return (typeof performance !== "undefined" && performance.now)
      ? performance.now() : Date.now();
  };
  const raf = typeof o.raf === "function" ? o.raf : function (cb) {
    return typeof requestAnimationFrame === "function"
      ? requestAnimationFrame(cb) : setTimeout(function () { cb(now()); }, 16);
  };
  const cancel = typeof o.cancel === "function" ? o.cancel : function (id) {
    if (typeof cancelAnimationFrame === "function") cancelAnimationFrame(id);
    else clearTimeout(id);
  };
  const update = typeof o.update === "function" ? o.update : function () {};
  const dur = typeof o.dur === "number" ? o.dur : COUNT_MS;

  let shown = typeof o.from === "number" ? o.from : 0;
  let target = shown;
  let startFrom = shown;
  let startAt = 0;
  let rafId = null;

  function reduced() {
    return prefersReduced(o.media);
  }

  function step(ts) {
    const t = dur <= 0 ? 1 : Math.min(1, (Number(ts) - startAt) / dur);
    shown = startFrom + (target - startFrom) * easeOutQuart(t);
    if (t >= 1) {
      shown = target;
      rafId = null;
      update(shown, true);
      if (typeof o.onDone === "function") o.onDone(shown);
      return;
    }
    update(shown, false);
    rafId = raf(step);
  }

  return {
    set(value) {
      target = Number(value) || 0;
      if (reduced()) {
        if (rafId != null) { cancel(rafId); rafId = null; }
        shown = target;
        startFrom = target;
        update(shown, true);
        if (typeof o.onDone === "function") o.onDone(shown);
        return shown;
      }
      if (rafId != null) return target;   // 飞行中：只改目标
      if (shown === target) { update(shown, true); return shown; }
      startFrom = shown;
      startAt = now();
      rafId = raf(step);
      return target;
    },
    value() { return shown; },
    busy() { return rafId != null; },
    stop() {
      if (rafId != null) { cancel(rafId); rafId = null; }
      shown = target;
      startFrom = target;
    },
  };
}
