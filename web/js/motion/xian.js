// ============================================================
// xian.js — T65 弦 XIAN 角色与九态（状态机 + SVG 工厂）
//
// 三条不可妥协原则写进实现：
//   1) 形态先成立：四控制点唯一几何，圆角路径实时生成，无第二份几何；
//   2) 生命不靠拟人：无眼嘴、无内部描边，靠姿态与频率表现状态；
//   3) 动效必须有因：九态由 dl_phase/并发/acct_hint 映射，未知态降级 idle。
//
// 振翅 = 翼面绕前缘轴旋转（FLAP_MAX=6°），禁止 scaleY 翻转；
// 相位推进先把 dt(ms) 转秒（防 1000 倍频率 bug）；
// 周期随速度自适应并锁在 1–2Hz：flapPeriod(sp) = .80/(.78+.30*min(1,sp/1.35))。
// reduced-motion → 静止 + 状态色；autoTick:false 供舞台共享单一 rAF。
// 模块顶层零 DOM 访问（node import 即证）。
// ============================================================

import {
  canonicalPoints,
  measureXianGeometry,
  projectedHeightRatio,
  xianInvariantReport,
} from "./xian-invariants.js";

export const XIAN_GEOMETRY = Object.freeze({
  canon: Object.freeze({
    nose: Object.freeze([56, -18]),
    wing: Object.freeze([-44, -20]),
    crown: Object.freeze([-10, -48]),
    keel: Object.freeze([-24, 44]),
  }),
  upperWing: Object.freeze(["nose", "wing", "crown"]),
  lowerWing: Object.freeze(["nose", "wing", "keel"]),
  sharedEdge: Object.freeze(["nose", "wing"]),
  radius: 15.7,         // 占 min(宽,高) 0.171（安全带 0.10–0.26；demo r=15 同形）
});

export const XIAN_STATES = Object.freeze([
  "idle", "scanning", "downloading", "sharding", "floodwait",
  "retrying", "failed", "done", "uploaded",
]);

export const FLAP_MAX = 6;
const SVG_NS = "http://www.w3.org/2000/svg";
const STATE_TONE = Object.freeze({
  idle: "neutral",
  scanning: "info",
  downloading: "success",
  sharding: "info",
  floodwait: "warning",
  retrying: "warning",
  failed: "danger",
  done: "success",
  uploaded: "archive",
});
const PHASE_MAP = Object.freeze({
  preparing: "scanning",
  disk_check: "scanning",
  reconciling: "scanning",
  fetching: "scanning",
  downloading: "downloading",
  refreshing: "retrying",
  flood_wait: "floodwait",
  done: "done",
  aborted: "idle",
  error: "failed",
});
const LEGAL_NEXT = Object.freeze({
  idle: Object.freeze(["scanning", "downloading", "sharding", "failed", "done"]),
  scanning: Object.freeze(["idle", "downloading", "sharding", "failed", "done"]),
  downloading: Object.freeze(["sharding", "floodwait", "retrying", "failed", "done"]),
  sharding: Object.freeze(["downloading", "floodwait", "retrying", "failed", "done"]),
  floodwait: Object.freeze(["downloading", "sharding", "retrying", "failed"]),
  retrying: Object.freeze(["scanning", "downloading", "sharding", "floodwait", "failed"]),
  failed: Object.freeze(["idle", "scanning", "downloading", "retrying"]),
  done: Object.freeze(["idle", "uploaded"]),
  uploaded: Object.freeze(["idle"]),
});

export function isXianState(value) {
  return XIAN_STATES.includes(value);
}

export function nextXianState(current, target) {
  if (!isXianState(target)) return "idle";
  if (!isXianState(current)) return target;
  const legal = LEGAL_NEXT[current] || [];
  return legal.includes(target) ? target : "idle";
}

export function xianVisual(state) {
  const normalized = isXianState(state) ? state : "idle";
  return Object.freeze({
    state: normalized,
    tone: STATE_TONE[normalized],
    className: "xian xian--" + normalized + " xian-tone--" + STATE_TONE[normalized],
    still: normalized === "floodwait" || normalized === "idle",
    airborne: normalized !== "done" && normalized !== "uploaded",
  });
}

// 速度自适应振翅周期（秒/次）。speed 归一化到 1.35 巡航上限。
export function flapPeriod(speed) {
  const sp = Math.max(0, Number(speed) || 0);
  return .80 / (.78 + .30 * Math.min(1, sp / 1.35));
}

export function flapFrequency(speed) {
  return 1 / flapPeriod(speed);
}

export function mapPhaseToXian(phase, opts) {
  const options = opts || {};
  if (phase === "downloading" && Number(options.concurrency) >= 2) return "sharding";
  const mapped = Object.prototype.hasOwnProperty.call(PHASE_MAP, phase)
    ? PHASE_MAP[phase] : "idle";
  if (mapped === "downloading" && options.failed) return "failed";
  if (mapped === "downloading" && options.retrying) return "retrying";
  return mapped;
}

// 多任务快照 → 单一可读状态：正在发生的事优先，静默期才报失败/完成。
// 优先级 flood_wait > refreshing > downloading/sharding > scanning > failed
// > done > idle；acct_hint 含 flood 且无实际下载活动时升为 floodwait。
const SCANNING_PHASES = Object.freeze({
  preparing: true, disk_check: true, reconciling: true, fetching: true,
});
const TERMINAL_PHASES = Object.freeze({ done: true, aborted: true, error: true });
export function xianStateFor(states, hint) {
  const arr = Array.isArray(states) ? states : [];
  const reason = hint && typeof hint === "object"
    ? String(hint.reason || "") : String(hint || "");
  const hintFlood = /flood|wait/i.test(reason);
  if (!arr.length) return hintFlood ? "floodwait" : "idle";
  let scanning = false, downloading = false, refreshing = false;
  let active = 0, busy = 0, failed = false;
  for (let i = 0; i < arr.length; i++) {
    const s = arr[i] || {};
    const phase = String(s.phase || "");
    const c = s.counts || {};
    active += Number(c.active) || 0;
    if (Number(c.fail) > 0) failed = true;
    if (phase === "flood_wait") return "floodwait";
    if (phase === "refreshing") refreshing = true;
    else if (phase === "downloading") downloading = true;
    else if (SCANNING_PHASES[phase]) scanning = true;
    else if (phase === "error") failed = true;
    if (!TERMINAL_PHASES[phase]) busy++;
  }
  if (refreshing) return "retrying";
  if (downloading) return active >= 2 ? "sharding" : "downloading";
  if (hintFlood) return "floodwait";
  if (scanning) return "scanning";
  if (failed) return "failed";
  return busy ? "idle" : "done";
}

function isReduced(win, explicit) {
  if (explicit === true) return true;
  if (explicit === false) return false;
  try {
    return !!(win && typeof win.matchMedia === "function" &&
      win.matchMedia("(prefers-reduced-motion: reduce)").matches);
  } catch (e) {
    return false;
  }
}

function svgElement(doc, tag, attrs) {
  const node = doc.createElementNS(SVG_NS, tag);
  for (const [key, value] of Object.entries(attrs || {})) node.setAttribute(key, value);
  return node;
}

function format(value) {
  return Number(value).toFixed(2).replace(/\.?0+$/, "");
}

// 二次圆角多边形：控制点是唯一拓扑输入，调用方无法传入第二份剪影。
export function roundedPath(points, radius) {
  const pts = points.map((p) => [Number(p[0]), Number(p[1])]);
  const r = Math.max(0, Number(radius));
  let out = "";
  const n = pts.length;
  function d(i, j) { return Math.hypot(pts[i][0] - pts[j][0], pts[i][1] - pts[j][1]); }
  function unit(i, j) {
    const length = d(i, j) || 1;
    return [(pts[j][0] - pts[i][0]) / length, (pts[j][1] - pts[i][1]) / length];
  }
  for (let i = 0; i < n; i++) {
    const prev = (i - 1 + n) % n;
    const next = (i + 1) % n;
    const ri = Math.min(r, d(i, prev) / 2, d(i, next) / 2);
    const u1 = unit(i, prev);
    const u2 = unit(i, next);
    const p1 = [pts[i][0] + u1[0] * ri, pts[i][1] + u1[1] * ri];
    const p2 = [pts[i][0] + u2[0] * ri, pts[i][1] + u2[1] * ri];
    out += (i === 0 ? "M" : "L") + format(p1[0]) + " " + format(p1[1]);
    out += "Q" + format(pts[i][0]) + " " + format(pts[i][1]) +
      " " + format(p2[0]) + " " + format(p2[1]);
  }
  return out + "Z";
}

function rotateAround(point, axis, degrees) {
  const rad = degrees * Math.PI / 180;
  const cos = Math.cos(rad);
  const sin = Math.sin(rad);
  const dx = point[0] - axis[0];
  const dy = point[1] - axis[1];
  return [axis[0] + dx * cos - dy * sin, axis[1] + dx * sin + dy * cos];
}

// 绕前缘轴旋转上下翼。翼尖是旋转轴上的不动点 → 不塌陷、不膨胀出带。
export function paintWings(body, angleDeg) {
  if (!body || !body._w1 || !body._w2 || !body._ed) return false;
  const raw = Number(angleDeg);
  const angle = Number.isFinite(raw)
    ? Math.max(-FLAP_MAX, Math.min(FLAP_MAX, raw)) : 0;
  const named = canonicalPoints(body.points || XIAN_GEOMETRY.canon);
  const axis = [
    (named.nose[0] + named.wing[0]) / 2,
    (named.nose[1] + named.wing[1]) / 2,
  ];
  const crown = rotateAround(named.crown, axis, angle);
  const keel = rotateAround(named.keel, axis, -angle);
  body._w1.setAttribute("d", roundedPath([named.nose, named.wing, crown], body.radius));
  body._w2.setAttribute("d", roundedPath([named.nose, named.wing, keel], body.radius));
  body._ed.setAttribute("d", "M" + format(named.nose[0]) + " " + format(named.nose[1]) +
    "L" + format(named.wing[0]) + " " + format(named.wing[1]));
  body.flapAngle = angle;
  return true;
}

export function createXian(host, opts) {
  const options = opts || {};
  const doc = options.doc || (host && host.ownerDocument) || null;
  if (!doc || typeof doc.createElementNS !== "function") {
    throw new TypeError("createXian requires a document");
  }
  const state = isXianState(options.state) ? options.state : "idle";
  const visual = xianVisual(state);
  const win = options.win || (doc.defaultView || null);
  const reduced = isReduced(win, options.reduced);
  const size = Number(options.size) > 0 ? Number(options.size) : 118;
  const uid = "xian" + Math.random().toString(36).slice(2, 9);
  const root = svgElement(doc, "svg", {
    class: "xian-svg " + visual.className,
    viewBox: "-70 -70 140 140",
    width: size,
    height: size,
    role: "img",
    "aria-label": "弦 XIAN · " + state,
    "data-xian-state": state,
    "data-xian-size": size <= 48 ? "sm" : "lg",   // 小尺寸前缘加粗，状态色可辨
    "data-reduced": reduced ? "on" : "off",
    "aria-hidden": options.decorative === false ? "false" : "true",
    focusable: "false",
  });
  const defs = svgElement(doc, "defs", {});
  const paper = svgElement(doc, "linearGradient", {
    id: uid + "-paper", x1: "0", y1: "0", x2: "1", y2: "1",
  });
  for (const [offset, color] of [
    ["0", "#F5FAFC"], [".55", "#DCEBF6"], ["1", "#B9D8EC"],
  ]) paper.appendChild(svgElement(doc, "stop", { offset, "stop-color": color }));
  const glass = svgElement(doc, "linearGradient", {
    id: uid + "-glass", x1: "0", y1: "0", x2: "1", y2: "1",
  });
  for (const [offset, color, opacity] of [
    ["0", "#8ADCF5", ".58"], [".55", "#2563EB", ".46"], ["1", "#1B4FC4", ".42"],
  ]) glass.appendChild(svgElement(doc, "stop", {
    offset, "stop-color": color, "stop-opacity": opacity,
  }));
  defs.appendChild(paper);
  defs.appendChild(glass);
  root.appendChild(defs);

  const chord = svgElement(doc, "g", {
    class: "x-chord", fill: "none", stroke: "#8ADCF5", "stroke-width": "2.2",
    "stroke-linecap": "round", "aria-hidden": "true",
  });
  chord.appendChild(svgElement(doc, "path", {
    class: "x-chord-main", d: "M-54 26 C -28 12 -8 32 26 14",
  }));
  chord.appendChild(svgElement(doc, "path", {
    class: "x-chord-branch", d: "M-48 32 C -24 22 -4 40 24 26",
    "stroke-width": "1.4", opacity: ".7",
  }));
  chord.appendChild(svgElement(doc, "path", {
    class: "x-chord-branch", d: "M-42 38 C -18 30 0 46 22 36",
    "stroke-width": "1", opacity: ".45",
  }));
  root.appendChild(chord);

  // 三层封顶：下翼蓝玻璃 / 上翼乳白玻璃 / 前缘亮边。高光并入渐变 stops，
  // 不单独绘制；不画内部折线描边。
  const body = svgElement(doc, "g", { class: "x-body" });
  const w2 = svgElement(doc, "path", {
    class: "x-w2", fill: "url(#" + uid + "-glass)",
    "aria-hidden": "true",
  });
  const w1 = svgElement(doc, "path", {
    class: "x-w1", fill: "url(#" + uid + "-paper)",
    "aria-hidden": "true",
  });
  const edge = svgElement(doc, "path", {
    class: "x-edge", fill: "none", stroke: "rgba(255,255,255,.72)",
    "stroke-width": ".7", "stroke-linecap": "round", "aria-hidden": "true",
  });
  body.appendChild(w2);
  body.appendChild(w1);
  body.appendChild(edge);
  body._w1 = w1;
  body._w2 = w2;
  body._ed = edge;
  body.points = XIAN_GEOMETRY.canon;
  body.radius = XIAN_GEOMETRY.radius;
  paintWings(body, 0);
  root.appendChild(body);

  const nodes = svgElement(doc, "g", {
    class: "x-node", fill: "#8ADCF5", "aria-hidden": "true",
  });
  for (const [cx, cy, r, opacity] of [
    [26, 14, 3.6, null], [24, 26, 2.8, ".75"], [22, 36, 2.2, ".5"],
  ]) {
    const attrs = { cx, cy, r };
    if (opacity) attrs.opacity = opacity;
    nodes.appendChild(svgElement(doc, "circle", attrs));
  }
  root.appendChild(nodes);
  // 光弦与节点只表达静态「在飞」姿态（idle/floodwait/done 由 CSS 显隐）。
  chord.classList.add("x-static-only");
  nodes.classList.add("x-static-only");
  if (host && typeof host.appendChild === "function") host.appendChild(root);

  let raf = 0;
  let phase = 0;
  let last = 0;
  let current = state;
  let cancelled = false;
  let loopStarted = false;
  function stopLoop() {
    if (raf && win && typeof win.cancelAnimationFrame === "function") {
      try { win.cancelAnimationFrame(raf); } catch (e) { /* ignore */ }
    }
    raf = 0;
    loopStarted = false;
  }
  function startLoop() {
    if (loopStarted || cancelled || reduced || !win ||
      typeof win.requestAnimationFrame !== "function") return;
    // GPU：idle 是纯展示姿态（翼面角恒 0），不要常驻 rAF——只保留已画好的
    // 静态一帧。状态离开 idle 时才启动；回到 idle 后由 loop/setState 停掉。
    if (current === "idle") return;
    loopStarted = true;
    const loop = (now) => {
      if (cancelled) return;
      if (current === "idle") {              // 回落 idle：立即停帧，不空转
        loopStarted = false;
        raf = 0;
        return;
      }
      if (!xianVisual(current).airborne) {   // done/uploaded 无循环
        loopStarted = false;
        raf = 0;
        return;
      }
      api.tick(now, options.speed == null ? 1 : options.speed);
      raf = win.requestAnimationFrame(loop);
    };
    raf = win.requestAnimationFrame(loop);
  }
  const api = {
    el: root,
    body,
    get state() { return current; },
    get reduced() { return reduced; },
    setState(next, nextOpts) {
      // 真实任务阶段不是演示步进：idle → downloading 是正常首帧。
      // 已知状态直接接受，只有未知目标降级 idle。
      const target = isXianState(next) ? next : "idle";
      current = target;
      root.setAttribute("data-xian-state", target);
      root.setAttribute("aria-label", "弦 XIAN · " + target);
      root.setAttribute("class", "xian-svg " + xianVisual(target).className);
      if (target === "floodwait" || target === "idle" || reduced) {
        paintWings(body, 0);
        stopLoop();
      } else if (xianVisual(target).airborne && options.autoTick !== false) {
        startLoop();
      }
      if (typeof nextOpts === "object" && nextOpts && nextOpts.reason) {
        root.setAttribute("data-xian-reason", String(nextOpts.reason));
      }
      return target;
    },
    tick(now, speed) {
      if (cancelled || reduced || current === "floodwait" || current === "idle") return false;
      const dt = Math.max(0, Number(now) - (last || Number(now)));
      last = Number(now);
      const sp = Math.max(0, Number(speed) || 0);
      const period = flapPeriod(sp);
      phase += dt / 1000 / period * Math.PI * 2;
      paintWings(body, Math.sin(phase) * FLAP_MAX * Math.min(1, sp / 1.2));
      return true;
    },
    cancel() {
      if (cancelled) return;
      cancelled = true;
      stopLoop();
      paintWings(body, 0);
      root.removeAttribute("data-motion-busy");
    },
    destroy() {
      api.cancel();
      if (root && typeof root.remove === "function") root.remove();
    },
    invariantReport() {
      return xianInvariantReport(body.points, body.radius);
    },
    projectedHeight(degrees) {
      return projectedHeightRatio(degrees, body.points);
    },
  };
  root._xian = api;
  // 首帧：只有非 idle 的 airborne 状态才启动循环（startLoop 内也会拦 idle）。
  if (!reduced && current !== "idle" && xianVisual(current).airborne &&
    win && typeof win.requestAnimationFrame === "function" && options.autoTick !== false) {
    startLoop();
  }
  return api;
}

// 宿主挂载糖：失败返回 null，调用方保留文字兜底（降级不白屏）。
export function mountXian(host, opts) {
  if (!host || typeof host.appendChild !== "function") return null;
  try {
    return createXian(host, opts);
  } catch (e) {
    return null;
  }
}

// 小尺寸派生：≤24px 只保留剪影与关键折面（去光弦/玻璃细节）。
export function xianIconGeometry(size) {
  const px = Number(size) || 32;
  const simple = px <= 24;
  const named = canonicalPoints(XIAN_GEOMETRY.canon);
  return Object.freeze({
    size: px,
    simple,
    points: Object.freeze(named),
    chord: !simple,
    glass: !simple,
    upperWing: XIAN_GEOMETRY.upperWing,
    lowerWing: XIAN_GEOMETRY.lowerWing,
    sharedEdge: XIAN_GEOMETRY.sharedEdge,
  });
}

export { measureXianGeometry, projectedHeightRatio, xianInvariantReport };
