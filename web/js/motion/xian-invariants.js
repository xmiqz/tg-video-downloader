// ============================================================
// xian-invariants.js — T65 弦 XIAN 形体不变量（纯函数，零 DOM）
//
// 四项可判定安全带 + 振翅投影高比。全部在形体局部几何空间度量：
// 位移、漂移、整体缩放这类刚性运动不参与判定（不改变剪影身份）。
// ============================================================

export const XIAN_BANDS = Object.freeze({
  noseAng: Object.freeze([48, 76]),
  aspect: Object.freeze([0.85, 1.35]),
  cg: Object.freeze([0.55, 0.75]),
  radius: Object.freeze([0.10, 0.26]),
  projectedHeight: Object.freeze([0.55, 1.05]),
});

function point(value, name) {
  if (!Array.isArray(value) || value.length !== 2 ||
    !Number.isFinite(value[0]) || !Number.isFinite(value[1])) {
    throw new TypeError("XIAN " + name + " must be [x, y]");
  }
  return [value[0], value[1]];
}

function cross(o, a, b) {
  return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0]);
}

function hull(points) {
  const sorted = points.slice().sort((a, b) => a[0] - b[0] || a[1] - b[1]);
  const out = [];
  for (const p of sorted) {
    while (out.length >= 2 && cross(out[out.length - 2], out[out.length - 1], p) <= 0) out.pop();
    out.push(p);
  }
  const lower = out.length + 1;
  for (let i = sorted.length - 1; i >= 0; i--) {
    const p = sorted[i];
    while (out.length >= lower && cross(out[out.length - 2], out[out.length - 1], p) <= 0) out.pop();
    out.push(p);
  }
  out.pop();
  return out;
}

function distance(a, b) {
  return Math.hypot(b[0] - a[0], b[1] - a[1]);
}

function rotate(pointValue, axis, degrees) {
  const rad = degrees * Math.PI / 180;
  const cos = Math.cos(rad);
  const sin = Math.sin(rad);
  const dx = pointValue[0] - axis[0];
  const dy = pointValue[1] - axis[1];
  return [axis[0] + dx * cos - dy * sin, axis[1] + dx * sin + dy * cos];
}

// 四个控制点可传数组或具名对象；返回具名副本，防止调用方改动基线。
export function canonicalPoints(points) {
  if (Array.isArray(points)) {
    if (points.length !== 4) throw new TypeError("XIAN expects four control points");
    return {
      nose: point(points[0], "nose"),
      wing: point(points[1], "wing"),
      crown: point(points[2], "crown"),
      keel: point(points[3], "keel"),
    };
  }
  const source = points || {};
  return {
    nose: point(source.nose, "nose"),
    wing: point(source.wing, "wing"),
    crown: point(source.crown, "crown"),
    keel: point(source.keel, "keel"),
  };
}

export function measureXianGeometry(points, radius) {
  const named = canonicalPoints(points);
  const pts = [named.nose, named.wing, named.crown, named.keel];
  const h = hull(pts);
  const nose = h.reduce((a, b) => (b[0] - .35 * b[1]) > (a[0] - .35 * a[1]) ? b : a);
  const i = h.indexOf(nose);
  const prev = h[(i - 1 + h.length) % h.length];
  const next = h[(i + 1) % h.length];
  const v1 = [prev[0] - nose[0], prev[1] - nose[1]];
  const v2 = [next[0] - nose[0], next[1] - nose[1]];
  const dot = v1[0] * v2[0] + v1[1] * v2[1];
  const cos = dot / (Math.hypot(v1[0], v1[1]) * Math.hypot(v2[0], v2[1]));
  const noseAng = Math.acos(Math.max(-1, Math.min(1, cos))) * 180 / Math.PI;
  const xs = pts.map((p) => p[0]);
  const ys = pts.map((p) => p[1]);
  const width = Math.max(...xs) - Math.min(...xs);
  const height = Math.max(...ys) - Math.min(...ys);
  const cx = xs.reduce((a, b) => a + b, 0) / xs.length;
  const span = width;
  const cg = 1 - (cx - Math.min(...xs)) / (span || 1);
  const edges = [];
  for (let k = 0; k < h.length; k++) edges.push(distance(h[k], h[(k + 1) % h.length]));
  const minEdge = Math.min(...edges);
  const r = Number(radius);
  if (!Number.isFinite(r) || r < 0) throw new TypeError("XIAN radius must be finite and >= 0");
  return {
    noseAng,
    aspect: width / (height || 1),
    cg,
    radius: r / (Math.min(width, height) || 1),
    minEdge,
    width,
    height,
  };
}

export function xianInvariantReport(points, radius) {
  const m = measureXianGeometry(points, radius);
  const bad = [];
  if (m.noseAng < XIAN_BANDS.noseAng[0] || m.noseAng > XIAN_BANDS.noseAng[1]) bad.push("noseAng");
  if (m.aspect < XIAN_BANDS.aspect[0] || m.aspect > XIAN_BANDS.aspect[1]) bad.push("aspect");
  if (m.cg < XIAN_BANDS.cg[0] || m.cg > XIAN_BANDS.cg[1]) bad.push("cg");
  if (m.radius < XIAN_BANDS.radius[0] || m.radius > XIAN_BANDS.radius[1]) bad.push("radius");
  return { ok: bad.length === 0, bad, metrics: m };
}

// 可见剪影是上下两翼投影的并集。绕前缘旋转不得塌陷或膨胀出带。
export function projectedHeightRatio(degrees, points) {
  const named = canonicalPoints(points || {
    nose: [56, -18], wing: [-44, -20], crown: [-10, -48], keel: [-24, 44],
  });
  const axis = [
    (named.nose[0] + named.wing[0]) / 2,
    (named.nose[1] + named.wing[1]) / 2,
  ];
  const angle = Number(degrees);
  if (!Number.isFinite(angle)) throw new TypeError("flap angle must be finite");
  const crown = rotate(named.crown, axis, angle);
  const keel = rotate(named.keel, axis, -angle);
  const ys = [named.nose[1], named.wing[1], crown[1], keel[1]];
  const height = Math.max(...ys) - Math.min(...ys);
  const base = 92; // CANON y extent: -48..44
  return height / base;
}

export function isProjectedHeightSafe(degrees) {
  const ratio = projectedHeightRatio(degrees);
  return ratio >= XIAN_BANDS.projectedHeight[0] && ratio <= XIAN_BANDS.projectedHeight[1];
}
