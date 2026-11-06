// ============================================================
// stage.js — T66 下载链路舞台（FR-56）
//
// 1) 只消费快照/事件（dl_phase / job_update / acct_hint），不自己猜进度；
// 2) 几何来自 xian.js，本文件不持第二份飞机拓扑；
// 3) 阶段边 STAGE_EDGES 镜像内核 tg_video_dl._PHASE_NEXT（契约测试逐边核对）；
// 4) 曲线在启动时按 8px 预采样，每帧零 getPointAtLength；
// 5) rAF 只在 busy() 时请求；reduced-motion 下零位移。
// ============================================================

// Download-stage model and rAF gate. Geometry comes from xian.js, never a
// second scaled aircraft constant.

import { createXian, mapPhaseToXian } from "./xian.js";

// 飞行时长兜底与 --motion-flight-ms 同值；不引预设表，避免第二个时长源。
const FLIGHT_FALLBACK_MS = 720;

export const STAGE_PHASES = Object.freeze([
  "preparing", "disk_check", "reconciling", "fetching", "downloading",
  "refreshing", "flood_wait", "done", "aborted", "error",
]);

// Edges mirror tg_video_dl._PHASE_NEXT. Terminal states have no outbound
// flow edges; visual-only back edges are deliberately absent.
export const STAGE_EDGES = Object.freeze([
  { from: "preparing", to: "disk_check" },
  { from: "preparing", to: "error" },
  { from: "preparing", to: "aborted" },
  { from: "disk_check", to: "reconciling" },
  { from: "disk_check", to: "fetching" },
  { from: "disk_check", to: "downloading" },
  { from: "disk_check", to: "done" },
  { from: "disk_check", to: "error" },
  { from: "disk_check", to: "aborted" },
  { from: "reconciling", to: "fetching" },
  { from: "reconciling", to: "downloading" },
  { from: "reconciling", to: "error" },
  { from: "reconciling", to: "aborted" },
  { from: "fetching", to: "downloading" },
  { from: "fetching", to: "flood_wait" },
  { from: "fetching", to: "done" },
  { from: "fetching", to: "error" },
  { from: "fetching", to: "aborted" },
  { from: "downloading", to: "refreshing" },
  { from: "downloading", to: "flood_wait" },
  { from: "downloading", to: "done" },
  { from: "downloading", to: "error" },
  { from: "downloading", to: "aborted" },
  { from: "refreshing", to: "downloading" },
  { from: "refreshing", to: "error" },
  { from: "refreshing", to: "aborted" },
  { from: "flood_wait", to: "fetching" },
  { from: "flood_wait", to: "downloading" },
  { from: "flood_wait", to: "error" },
  { from: "flood_wait", to: "aborted" },
]);

const PHASE_ID = Object.freeze(Object.fromEntries(
  STAGE_PHASES.map((p, i) => [p, i]),
));

// 弦的线路表：九态 → 走哪条数据线 / 怎么走 / 飞完停在哪。
//   fly  = 事件持续期间往返飞（下载/重试/分片）
//   once = 事件切换的那一帧飞一次，落点固定（完成落磁盘、失败退回来源）
//   hold = 停在锚点上不动（扫描停在 Telegram、限流停在账号池）
const FLIGHT_PLAN = Object.freeze({
  idle: Object.freeze({ path: "srcTask", mode: "hold", at: "start" }),
  scanning: Object.freeze({ path: "srcTask", mode: "hold", at: "start" }),
  downloading: Object.freeze({ path: "srcTask", mode: "fly" }),
  sharding: Object.freeze({ path: "srcTask", mode: "fly" }),
  floodwait: Object.freeze({ path: "acctTask", mode: "hold", at: "start" }),
  retrying: Object.freeze({ path: "acctTask", mode: "fly" }),
  failed: Object.freeze({
    path: "srcTask", mode: "once", reverse: true,
    park: Object.freeze({ path: "srcTask", at: "start" }),
  }),
  done: Object.freeze({
    path: "taskDisk", mode: "once",
    park: Object.freeze({ path: "taskDisk", at: "end" }),
  }),
});

export function stageLayout(opts) {
  const o = opts || {};
  const concurrency = Math.max(1, Math.min(8, Number(o.concurrency) || 1));
  const lowEnd = !!o.lowEnd;
  const nas = !!o.nas;
  if (o.compact) {
    const compactNodes = {
      src: { id: "src", x: 8, y: 18, w: 150, h: 44, label: "Telegram" },
      acct: { id: "acct", x: 8, y: 74, w: 150, h: 44, label: "账号池" },
      // The compact task node is a geometry anchor for the flight path only;
      // its visible surface is the HTML bay overlaid below the link diagram.
      task: { id: "task", x: 10, y: 124, w: 440, h: 342, label: "任务舱" },
      disk: { id: "disk", x: 302, y: 18, w: 150, h: 44, label: "本地磁盘" },
    };
    if (nas) compactNodes.nas = { id: "nas", x: 302, y: 74, w: 150, h: 44, label: "NAS" };
    return {
      viewBox: [0, 0, 460, 470],
      nodes: compactNodes,
      wires: {
        srcTask: "M158 40 C205 40 207 104 230 124",
        acctTask: "M158 96 C196 96 208 114 230 124",
        taskDisk: "M230 124 C258 104 272 40 302 40",
        ...(nas ? { diskNas: "M377 62 C377 68 377 71 377 74" } : {}),
      },
      compact: true,
      mobile: false,
      tablet: false,
      nas,
      lowEnd,
      concurrency,
      formation: lowEnd ? 1 : concurrency,
      branchCount: concurrency,
      lanes: lowEnd ? ["host"] : Array.from({ length: concurrency }, (_, i) => "lane" + i),
    };
  }
  if (o.mobile) {
    // 单列：来源 → 账号池 → 任务舱 → 磁盘（同一列宽，四个框同宽同圆角）。
    // 任务舱拿到 46.9% 的高度，才装得下队列条 + 卡片 + 读数行。
    const mobileNodes = {
      src: { id: "src", x: 30, y: 8, w: 360, h: 62, label: "Telegram" },
      acct: { id: "acct", x: 30, y: 80, w: 360, h: 62, label: "账号池" },
      task: { id: "task", x: 30, y: 164, w: 360, h: 300, label: "任务舱" },
      disk: { id: "disk", x: 30, y: 478, w: 360, h: 62, label: "本地磁盘" },
    };
    if (nas) mobileNodes.nas = { id: "nas", x: 30, y: 548, w: 360, h: 62, label: "NAS / WebDAV" };
    return {
      viewBox: [0, 0, 420, 640],
      nodes: mobileNodes,
      // 手机单列里不能画「来源直连任务舱」的长线：那条线会从账号池卡片中间穿过去
      // （截图里账号池被一条竖线切成两半）。只画相邻两框之间的短接线。
      wires: {
        srcTask: "M210 70 C210 73 210 77 210 80",
        acctTask: "M210 142 C210 150 210 157 210 164",
        taskDisk: "M210 464 C210 469 210 473 210 478",
        ...(nas ? { diskNas: "M210 540 C210 542 210 546 210 548" } : {}),
      },
      compact: false,
      mobile: true,
      tablet: false,
      nas,
      lowEnd,
      concurrency,
      formation: lowEnd ? 1 : concurrency,
      branchCount: concurrency,
      lanes: lowEnd ? ["host"] : Array.from({ length: concurrency }, (_, i) => "lane" + i),
    };
  }
  // 三框同宽（190×80）：框宽不一致是「大小不一致」观感的直接来源。
  const nodes = {
    src: { id: "src", x: 30, y: 100, w: 190, h: 80, label: "Telegram" },
    acct: { id: "acct", x: 30, y: 240, w: 190, h: 80, label: "账号池" },
    task: { id: "task", x: 250, y: 28, w: 440, h: 320, label: "任务舱" },
    disk: { id: "disk", x: 720, y: 100, w: 190, h: 80, label: "本地磁盘" },
  };
  if (nas) nodes.nas = { id: "nas", x: 720, y: 250, w: 190, h: 60, label: "NAS / WebDAV" };
  const wires = {
    srcTask: "M220 140 C 236 140 240 100 250 100",
    acctTask: "M220 280 C 236 280 240 300 250 300",
    taskDisk: "M690 140 C 712 140 715 140 720 140",
  };
  if (nas) wires.diskNas = "M815 180 C 860 215 860 235 815 250";
  return {
    viewBox: [0, 0, 940, 380],
    nodes,
    wires,
    compact: false,
    mobile: false,
    // 640–1023：沿用桌面三列几何，队列条换行到舱内下方（任务书 768 行）。
    tablet: !!o.tablet,
    nas,
    lowEnd,
    concurrency,
    formation: lowEnd ? 1 : concurrency,
    branchCount: concurrency,
    lanes: lowEnd ? ["host"] : Array.from({ length: concurrency }, (_, i) => "lane" + i),
  };
}

// 结构档位名：compact/mobile 各自成形；tablet 复用桌面几何但队列换行。
function layoutName(box) {
  if (!box) return "desktop";
  if (box.mobile) return "mobile";
  if (box.tablet) return "tablet";
  if (box.compact) return "compact";
  return "desktop";
}

export function sampleCubicPath(d, step) {
  const text = String(d || "");
  const numbers = (text.match(/-?\d+(?:\.\d+)?/g) || []).map(Number);
  if (numbers.length < 8) return [];
  let previous = [numbers[0], numbers[1]];
  const poly = [previous];
  for (let i = 2; i + 5 < numbers.length; i += 6) {
    const c = [
      [numbers[i], numbers[i + 1]], [numbers[i + 2], numbers[i + 3]],
      [numbers[i + 4], numbers[i + 5]],
    ];
    const hull = Math.hypot(c[0][0] - previous[0], c[0][1] - previous[1]) +
      Math.hypot(c[1][0] - c[0][0], c[1][1] - c[0][1]) +
      Math.hypot(c[2][0] - c[1][0], c[2][1] - c[1][1]);
    const count = Math.max(4, Math.ceil(hull / .5));
    for (let j = 1; j <= count; j++) {
      const t = j / count;
      const u = 1 - t;
      const a = previous;
      poly.push([
        u * u * u * a[0] + 3 * u * u * t * c[0][0] +
          3 * u * t * t * c[1][0] + t * t * t * c[2][0],
        u * u * u * a[1] + 3 * u * u * t * c[0][1] +
          3 * u * t * t * c[1][1] + t * t * t * c[2][1],
      ]);
    }
    previous = c[2];
  }
  const cumulative = [0];
  for (let i = 1; i < poly.length; i++) {
    cumulative.push(cumulative[i - 1] +
      Math.hypot(poly[i][0] - poly[i - 1][0], poly[i][1] - poly[i - 1][1]));
  }
  const length = cumulative[cumulative.length - 1] || 0;
  const spacing = Math.max(1, Number(step) || 8);
  const n = Math.max(2, Math.ceil(length / spacing));
  const out = [];
  for (let i = 0; i <= n; i++) {
    const target = i / n * length;
    let lo = 0;
    let hi = cumulative.length - 1;
    while (hi - lo > 1) {
      const mid = (lo + hi) >> 1;
      if (cumulative[mid] <= target) lo = mid; else hi = mid;
    }
    const f = cumulative[hi] > cumulative[lo]
      ? (target - cumulative[lo]) / (cumulative[hi] - cumulative[lo]) : 0;
    out.push({
      x: poly[lo][0] + (poly[hi][0] - poly[lo][0]) * f,
      y: poly[lo][1] + (poly[hi][1] - poly[lo][1]) * f,
    });
  }
  for (let i = 0; i < out.length; i++) {
    const a = out[Math.max(0, i - 1)];
    const b = out[Math.min(out.length - 1, i + 1)];
    out[i].ang = Math.atan2(b.y - a.y, b.x - a.x) * 180 / Math.PI;
  }
  return out;
}

const ACCEL = .22;
const CRUISE = .48;
const VMAX = .6;

function rawProgress(k) {
  const x = Math.max(0, Math.min(1, k));
  if (x < ACCEL) {
    const u = x / ACCEL;
    return .5 * VMAX * ACCEL * u * u;
  }
  if (x < ACCEL + CRUISE) return .5 * VMAX * ACCEL + VMAX * (x - ACCEL);
  const u = (x - ACCEL - CRUISE) / .30;
  return .5 * VMAX * ACCEL + VMAX * CRUISE +
    .5 * VMAX * .30 * (2 * u - u * u);
}
const NORM = rawProgress(1);

export function trapezoidProgress(k) {
  return rawProgress(k) / NORM;
}

export function trapezoidSpeed(k) {
  const x = Math.max(0, Math.min(1, k));
  if (x <= 0 || x >= 1) return 0;
  if (x < ACCEL) return VMAX * (x / ACCEL) / NORM;
  if (x < ACCEL + CRUISE) return VMAX / NORM;
  const u = (x - ACCEL - CRUISE) / .30;
  return VMAX * (1 - u) / NORM;
}

export function squashStretch(k, speed) {
  const x = Math.max(0, Math.min(1, k));
  const sp = Math.max(0, Number(speed) || 0);
  if (x <= .10) return 1.25;
  if (x > .90) {
    const u = (x - .90) / .10;
    if (x >= .999) return .82;
    if (u < .35) return 1 - .18 * (u / .35) * (u / .35);
    const r = (u - .35) / .65;
    return .82 + .23 * Math.sin(r * Math.PI);
  }
  return 1 + .08 * Math.min(1, sp / 1.35);
}

// Landing feedback is a separate phase: touchdown 0.82, rebound 1.05,
// settle 1.0. Keeping it out of squashStretch preserves the flight curve.
export function landingRebound(k) {
  const x = Math.max(0, Math.min(1, Number(k) || 0));
  if (x <= 0) return .82;
  if (x < .35) {
    const u = x / .35;
    return .82 + .23 * (u * u * (3 - 2 * u));
  }
  if (x >= 1) return 1;
  const u = (x - .35) / .65;
  return 1.05 - .05 * (u * u * (3 - 2 * u));
}
export function bankAngle(tangent, curvature, previous) {
  const target = Math.max(-24, Math.min(24, Number(curvature) * .9));
  const prev = Number(previous) || 0;
  return prev + (target - prev) * .12;
}

export function anticipationOffset(k, tangent) {
  const x = Math.max(0, Math.min(1, k));
  if (x >= .035) return { angle: 0, back: 0 };
  const u = 1 - x / .035;
  const angle = -6 * u;
  const back = 2.2 * u;
  return { angle, back, dx: -back * Math.cos(Number(tangent) * Math.PI / 180),
    dy: -back * Math.sin(Number(tangent) * Math.PI / 180) };
}

export function trailLength(speed) {
  return Math.min(35, Math.max(0, Number(speed) || 0) * 26);
}

export function receiverFeedback(kind) {
  if (kind === "failed") return "shake";
  if (kind === "done") return "squash";
  return "inhale";
}

const ITEM_STATE = Object.freeze({
  "下载中": "downloading",
  "复制中": "copying",
  "等待中": "waiting",
  "待下载": "waiting",
  "暂停": "paused",
  "已暂停": "paused",
  "完成": "done",
  "已存在": "exists",
  "exists": "exists",
  "跳过": "skipped",
  "已跳过": "skipped",
  "失败": "failed",
  "重试中": "retrying",
  "FloodWait": "floodwait",
});

export function stageItemState(status) {
  const key = String(status || "").trim();
  return Object.prototype.hasOwnProperty.call(ITEM_STATE, key)
    ? ITEM_STATE[key] : "waiting";
}

// 任务队列固定排序：下载中 > FloodWait > 重试 > 暂停 > 失败 > 完成。
// 失败项排在前六档（而非榜尾）但仍留在队列里，不允许被自动吞掉。
const QUEUE_LIVE = new Set([
  "preparing", "disk_check", "reconciling", "fetching", "downloading", "refreshing",
]);

export function queueRank(task) {
  const t = task || {};
  const phase = String(t.phase || "");
  const counts = t.counts || {};
  const retrying = Math.max(0, Number(counts.retrying) || 0);
  const paused = Math.max(0, Number(counts.paused) || 0);
  const failed = Math.max(0, Number(counts.fail) || 0);
  if (phase === "downloading") return 0;
  if (phase === "flood_wait") return 1;
  if (retrying > 0) return 2;
  if (paused > 0) return 3;
  if (phase === "error" || failed > 0) return 4;
  if (phase === "done") return 5;
  if (QUEUE_LIVE.has(phase)) return 0.5;
  return 6;
}

export function queueOrder(tasks) {
  const list = Array.isArray(tasks) ? tasks.slice() : [];
  return list.sort(function (a, b) {
    const d = queueRank(a) - queueRank(b);
    if (d) return d;
    const left = String(a && a.task_id != null ? a.task_id : "");
    const right = String(b && b.task_id != null ? b.task_id : "");
    return left.localeCompare(right, undefined, { numeric: true, sensitivity: "base" });
  });
}

// Parse the remaining flood-wait seconds from a task's human phase_detail.
// Falls back to the numeric countdown field. Accepts "FloodWait 30s",
// "限流 30 秒后重试", "429 · 12s" and similar surface strings.
export function parseFloodWaitSeconds(task) {
  const t = task || {};
  const sources = [
    typeof t.phase_detail === "string" ? t.phase_detail : "",
    typeof t.status_text === "string" ? t.status_text : "",
    typeof t.detail === "string" ? t.detail : "",
  ];
  for (const src of sources) {
    if (!src) continue;
    const m = src.match(/(\d+)\s*(?:s|秒)/i);
    if (m) {
      const n = parseInt(m[1], 10);
      if (Number.isFinite(n)) return n;
    }
  }
  const cd = Number(t.countdown);
  return Number.isFinite(cd) && cd > 0 ? Math.floor(cd) : 0;
}

// 分片槽位规则（按真实并发渲染，不写死 4）：
// 并发 1 及以下不分片 → 不出槽；1–4 固定 4 槽，亮起 = 活跃分片；
// 5–8 按实际数量；>8 折叠 8 槽 + 「×N」计数牌。
export function stageSlotModel(parallel, active) {
  const p = Math.max(0, Number(parallel) || 0);
  const a = Math.max(0, Number(active) || 0);
  if (p <= 1) return { hidden: true, slots: 0, lit: 0, overflow: 0, parallel: p };
  const slots = p <= 4 ? 4 : Math.min(8, p);
  return {
    hidden: false,
    slots,
    lit: Math.min(slots, a),
    overflow: p > 8 ? p - 8 : 0,
    parallel: p,
  };
}

export function formatStageSpeed(value) {
  const n = Math.max(0, Number(value) || 0);
  if (n <= 0) return "—";
  if (n < 1024) return Math.round(n) + " B/s";
  if (n < 1048576) return (n / 1024).toFixed(1) + " KB/s";
  return (n / 1048576).toFixed(1) + " MB/s";
}

// 保存位置剩余容量：二进制单位，≥10 的取整（「128 GB」不写成 128.4）。
export function formatDiskFree(bytes) {
  const n = Math.max(0, Number(bytes) || 0);
  if (n <= 0) return "";
  if (n >= 1099511627776) return (n / 1099511627776).toFixed(1) + " TB";
  if (n >= 1073741824) {
    const gb = n / 1073741824;
    return (gb >= 10 ? gb.toFixed(0) : gb.toFixed(1)) + " GB";
  }
  return Math.max(1, Math.round(n / 1048576)) + " MB";
}

export function stagePhaseCopy(phase, scene) {
  const s = scene || {};
  const counts = s.counts || {};
  const active = Math.max(0, Number(counts.active) || 0);
  const failed = Math.max(0, Number(counts.fail) || 0);
  const paused = Math.max(0, Number(counts.paused) || 0);
  const total = Math.max(0, Number(s.taskTotal) || 0);
  const finished = Math.max(0, Number(s.taskFinished) || 0);
  const countdown = Math.max(0, Number(s.countdown) || 0);
  const waiting = Math.max(0, total - finished - failed - paused - active);
  if (phase === "preparing") {
    return {
      label: "准备", status: "正在准备下载任务", tone: "live",
      motion: "scan", speedMode: "none", sub: "准备下载",
    };
  }
  if (phase === "disk_check") {
    return {
      label: "磁盘检查", status: "正在检查保存位置与可用空间", tone: "live",
      motion: "scan", speedMode: "none", sub: "检查磁盘",
    };
  }
  if (phase === "reconciling") {
    return {
      label: "对账", status: "正在核对本地文件与断点", tone: "live",
      motion: "scan", speedMode: "none", sub: "核对文件",
    };
  }
  if (phase === "fetching") {
    return {
      label: "读取清单", status: "正在读取 Telegram 清单", tone: "live",
      motion: "scan", speedMode: "none", sub: "读取清单",
    };
  }
  if (phase === "downloading") {
    return {
      label: "下载中",
      status: "正在下载 " + active + " 条" + (waiting ? " · 等待 " + waiting + " 条" : ""),
      tone: "live", motion: "transfer", speedMode: "speed",
      sub: active ? "活跃视频块 · " + active + " 条正在搬运" : "等待调度",
    };
  }
  if (phase === "refreshing") {
    return {
      label: "刷新引用", status: "正在刷新文件引用", tone: "live",
      motion: "scan", speedMode: "none", sub: "刷新引用",
    };
  }
  if (phase === "flood_wait") {
    return {
      label: "限流等待",
      status: "账号限流 · " + countdown + "s 后自动重试",
      tone: "warn", motion: "hold", speedMode: "hold",
      sub: "限流等待 · " + paused + " 条暂停",
    };
  }
  if (phase === "done") {
    return {
      label: failed ? "完成但有失败" : "完成",
      status: failed ? "任务结束 · " + failed + " 条失败" : "全部搬运完成",
      tone: failed ? "warn" : "ok", motion: "settle", speedMode: "none",
      sub: "任务结束 · 完成 " + finished + "/" + total,
    };
  }
  if (phase === "aborted") {
    return {
      label: "已中止", status: "任务已中止，可继续未完成条目", tone: "warn",
      motion: "none", speedMode: "none", sub: "任务已中止",
    };
  }
  if (phase === "error") {
    return {
      label: "失败",
      status: "任务失败" + (failed ? " · " + failed + " 条可重试" : ""),
      tone: "error", motion: "none", speedMode: "none",
      sub: failed ? "失败 " + failed + " 条 · 可重试" : "任务失败",
    };
  }
  return {
    label: "等待", status: "等待下载任务", tone: "idle",
    motion: "none", speedMode: "none", sub: "等待任务",
  };
}

function isReduced(win, explicit) {
  if (explicit === true) return true;
  if (explicit === false) return false;
  try {
    return !!(win && typeof win.matchMedia === "function" &&
      win.matchMedia("(prefers-reduced-motion: reduce)").matches);
  } catch (e) { return false; }
}

function readFlightDuration(host, win) {
  const fallback = FLIGHT_FALLBACK_MS;
  try {
    if (!host || !win || typeof win.getComputedStyle !== "function") return fallback;
    const raw = win.getComputedStyle(host).getPropertyValue("--motion-flight-ms").trim();
    const n = parseFloat(raw);
    if (!Number.isFinite(n) || n <= 0) return fallback;
    return /ms\s*$/.test(raw) ? n : n * 1000;
  } catch (e) {
    return fallback;
  }
}

function pointAt(points, k) {
  if (!points.length) return null;
  const f = Math.max(0, Math.min(1, Number(k) || 0)) * (points.length - 1);
  const i = Math.floor(f);
  const j = Math.min(points.length - 1, i + 1);
  const r = f - i;
  const a = points[i];
  const b = points[j];
  return {
    x: a.x + (b.x - a.x) * r,
    y: a.y + (b.y - a.y) * r,
    ang: a.ang + (b.ang - a.ang) * r,
  };
}

function clockNow(win) {
  try {
    if (win && win.performance && typeof win.performance.now === "function") {
      return win.performance.now();
    }
  } catch (e) { /* fall through */ }
  return Date.now();
}

export function createStage(host, opts) {
  const options = opts || {};
  const doc = options.doc || (host && host.ownerDocument) || null;
  const win = options.win || null;
  const reduced = isReduced(win, options.reduced);
  const loopFlights = options.loop === true;
  const layout = stageLayout(options.layout || options);
  // 同一宿主重复挂载（视图重进 / 档位重建）先清掉上一份舞台节点：
  // 否则宿主里会叠出第二套 SVG 与气泡（截图里的「双框」与高度翻倍）。
  if (host && host.children) {
    const stale = Array.prototype.slice.call(host.children).filter(function (c) {
      const cls = c && typeof c.getAttribute === "function"
        ? String(c.getAttribute("class") || "") : "";
      return cls.indexOf("xian-stage-") === 0;
    });
    stale.forEach(function (c) { if (typeof c.remove === "function") c.remove(); });
  }
  if (host && typeof host.setAttribute === "function") {
    host.setAttribute("data-stage-layout", layoutName(layout));
  }
  let raf = 0;
  let destroyed = false;
  let busy = false;
  let current = "idle";
  let lastScene = null;
  let flight = null;
  let flightXian = null;
  let cardsLayer = null;
  let slotEl = null;
  let nodeOverlay = null;
  let bandStatusEl = null;
  let taskListLayer = null;
  let bayLayer = null;
  let aggregateEl = null;
  let footEl = null;
  let currentEl = null;
  let bayCountEl = null;
  let pagerEl = null;
  let railKeydownHandler = null;
  let railExpanded = false;
  let workbenchEl = null;
  let activeLayout = layout;
  let sceneAccounts = [];
  let accountsKey = "";
  let diskKey = "";
  let diskSub = "保存位置";
  let diskTitle = "本地磁盘 · 保存位置";
  let currentPlan = null;
  let prevXianState = "";
  // 单条视频落地：任务还在跑时，每完成一条就飞一次磁盘（任务整体完成仍走 done 计划）。
  // 首次进入/切换主任务时把当前已完成数作为基线，不回放历史完成。
  let doneSeen = 0;
  let doneFocusId = "";
  let pulseToken = 0;
  const flightDuration = readFlightDuration(host, win);
  let samples = Object.fromEntries(
    Object.entries(layout.wires).map(([key, d]) => [key, sampleCubicPath(d, 8)]),
  );
  const svgRoot = doc && typeof doc.createElementNS === "function"
    ? buildStageSvg(doc, layout) : null;
  if (svgRoot && host && typeof host.appendChild === "function") host.appendChild(svgRoot);
  if (doc && typeof doc.createElement === "function" && host &&
    typeof host.appendChild === "function") {
    nodeOverlay = doc.createElement("div");
    nodeOverlay.className = "xian-stage-node-overlay";
    nodeOverlay.setAttribute("aria-hidden", "true");
    host.appendChild(nodeOverlay);
    bandStatusEl = doc.createElement("div");
    bandStatusEl.className = "xian-stage-band-status";
    host.appendChild(bandStatusEl);
    cardsLayer = doc.createElement("div");
    cardsLayer.className = "xian-stage-cards";
    cardsLayer.setAttribute("aria-hidden", "true");
    host.appendChild(cardsLayer);
    taskListLayer = doc.createElement("div");
    taskListLayer.className = "xian-stage-task-list";
    taskListLayer.setAttribute("role", "group");
    taskListLayer.setAttribute("aria-label", "任务列表");
    taskListLayer.hidden = true;
    host.appendChild(taskListLayer);
    bayLayer = doc.createElement("div");
    bayLayer.className = "xian-stage-bay";
    bayLayer.setAttribute("aria-label", "任务舱");
    bayLayer.hidden = true;
    host.appendChild(bayLayer);
    const bayHead = doc.createElement("div");
    bayHead.className = "xian-stage-bay-head";
    const bayTitle = doc.createElement("div");
    bayTitle.className = "xian-stage-bay-title";
    // 舱名直接是焦点任务名（renderBay 每帧写入），不再写抽象的「任务舱」。
    currentEl = doc.createElement("span");
    currentEl.className = "xian-stage-bay-current";
    bayCountEl = doc.createElement("span");
    bayCountEl.className = "xian-stage-bay-count";
    bayCountEl.setAttribute("aria-hidden", "true");
    bayTitle.appendChild(currentEl);
    bayTitle.appendChild(bayCountEl);
    bayHead.appendChild(bayTitle);
    bayLayer.appendChild(bayHead);
    const body = doc.createElement("div");
    body.className = "xian-stage-bay-body";
    // Mobile/compact in-bay pager: "1 / N · 当前任务名" + 上一项/下一项。
    // 桌面索引栏不显示此分页器；单任务隐藏。
    const pager = doc.createElement("div");
    pager.className = "xian-stage-pager";
    pager.setAttribute("role", "group");
    pager.setAttribute("aria-label", "任务分页");
    pager.hidden = true;
    const pagerPrev = doc.createElement("button");
    pagerPrev.className = "xian-stage-pager-btn xian-stage-pager-prev";
    pagerPrev.setAttribute("type", "button");
    pagerPrev.setAttribute("aria-label", "上一项任务");
    pagerPrev.textContent = "‹";
    const pagerText = doc.createElement("span");
    pagerText.className = "xian-stage-pager-text";
    const pagerNext = doc.createElement("button");
    pagerNext.className = "xian-stage-pager-btn xian-stage-pager-next";
    pagerNext.setAttribute("type", "button");
    pagerNext.setAttribute("aria-label", "下一项任务");
    pagerNext.textContent = "›";
    pager.appendChild(pagerPrev);
    pager.appendChild(pagerText);
    pager.appendChild(pagerNext);
    pagerEl = pager;
    body.appendChild(pager);
    const rail = doc.createElement("div");
    rail.className = "xian-stage-task-rail";
    rail.appendChild(taskListLayer);
    body.appendChild(rail);
    const workbench = doc.createElement("section");
    workbench.className = "xian-stage-workbench";
    workbenchEl = workbench;
    cardsLayer.setAttribute("class", "xian-stage-cards xian-stage-video-grid");
    cardsLayer.setAttribute("aria-label", "活跃视频块");
    workbench.appendChild(cardsLayer);
    slotEl = doc.createElement("div");
    slotEl.className = "xian-stage-slots";
    slotEl.setAttribute("aria-hidden", "true");
    slotEl.hidden = true;
    workbench.appendChild(slotEl);
    aggregateEl = doc.createElement("div");
    aggregateEl.className = "xian-stage-aggregate";
    workbench.appendChild(aggregateEl);
    body.appendChild(workbench);
    bayLayer.appendChild(body);
    footEl = doc.createElement("div");
    footEl.className = "xian-stage-bay-foot";
    bayLayer.appendChild(footEl);
    renderNodeOverlay();
  }
  function buildWireGroup(d, box) {
    const NS = "http://www.w3.org/2000/svg";
    const group = d.createElementNS(NS, "g");
    group.setAttribute("class", "xian-stage-wires");
    for (const key of Object.keys(box.wires)) {
      const path = d.createElementNS(NS, "path");
      path.setAttribute("class", "xian-stage-wire wire-" + key);
      path.setAttribute("d", box.wires[key]);
      group.appendChild(path);
    }
    return group;
  }
  function buildNodeGroup(d, box) {
    const NS = "http://www.w3.org/2000/svg";
    const nodes = d.createElementNS(NS, "g");
    nodes.setAttribute("class", "xian-stage-nodes");
    for (const key of Object.keys(box.nodes)) {
      const n = box.nodes[key];
      const node = d.createElementNS(NS, "g");
      node.setAttribute("class", "xian-stage-node node-" + key);
      node.setAttribute("data-node", key);
      const rect = d.createElementNS(NS, "rect");
      rect.setAttribute("x", n.x);
      rect.setAttribute("y", n.y);
      rect.setAttribute("width", n.w);
      rect.setAttribute("height", n.h);
      rect.setAttribute("rx", 16);
      node.appendChild(rect);
      const label = d.createElementNS(NS, "text");
      label.setAttribute("x", n.x + 15);
      label.setAttribute("y", n.y + 25);
      if (key === "task") label.setAttribute("class", "xian-stage-task-label");
      label.textContent = n.label;
      node.appendChild(label);
      if (key === "task") {
        const sub = d.createElementNS(NS, "text");
        sub.setAttribute("class", "xian-stage-task-sub");
        sub.setAttribute("x", n.x + 16);
        sub.setAttribute("y", n.y + 48);
        sub.textContent = "活跃视频块";
        node.appendChild(sub);
        const foot = d.createElementNS(NS, "text");
        foot.setAttribute("class", "xian-stage-task-foot");
        foot.setAttribute("x", n.x + 16);
        foot.setAttribute("y", n.y + n.h - 18);
        foot.textContent = "队列 0 · 并发 0";
        node.appendChild(foot);
      }
      nodes.appendChild(node);
    }
    return nodes;
  }
  // 档位切换（桌面 ⇄ 平板 ⇄ 手机）时 SVG 里的节点矩形与数据线必须一起换，
  // 只改 viewBox 会把旧几何缩放进新画布：手机上就看到右侧一多出来的深色面板。
  function rebuildGeometry(box) {
    if (!svgRoot || !doc || typeof doc.createElementNS !== "function") return;
    const oldWires = svgRoot.querySelector(".xian-stage-wires");
    const oldNodes = svgRoot.querySelector(".xian-stage-nodes");
    if (oldWires && oldWires.remove) oldWires.remove();
    if (oldNodes && oldNodes.remove) oldNodes.remove();
    if (!svgRoot.appendChild) return;
    svgRoot.appendChild(buildWireGroup(doc, box));
    svgRoot.appendChild(buildNodeGroup(doc, box));
    // 飞行体恒在最上层：几何组重建后把它挪回队尾（真实 DOM 会移动而非复制）
    if (flightLayer && flightLayer.parentNode === svgRoot) {
      svgRoot.appendChild(flightLayer);
    }
  }
  function buildStageSvg(d, box) {
    const NS = "http://www.w3.org/2000/svg";
    const svg = d.createElementNS(NS, "svg");
    svg.setAttribute("class", "xian-stage-svg");
    svg.setAttribute("viewBox", box.viewBox.join(" "));
    svg.setAttribute("preserveAspectRatio", "xMidYMid meet");
    svg.setAttribute("aria-hidden", "true");
    svg.appendChild(buildWireGroup(d, box));
    svg.appendChild(buildNodeGroup(d, box));
    const band = d.createElementNS(NS, "g");
    band.setAttribute("class", "xian-stage-band");
    const status = d.createElementNS(NS, "text");
    status.setAttribute("x", box.compact ? 116 : 40);
    status.setAttribute("y", box.viewBox[3] - 18);
    status.setAttribute("class", "xian-stage-status");
    status.textContent = "等待下载任务";
    band.appendChild(status);
    svg.appendChild(band);
    // The moving body is the same XIAN factory used by the overview host.
    // No second aircraft topology is drawn here.
    const flightLayer = d.createElementNS(NS, "g");
    flightLayer.setAttribute("class", "xian-stage-flight");
    flightLayer.setAttribute("aria-hidden", "true");
    flightLayer.setAttribute("data-flight", "idle");
    const flier = d.createElementNS(NS, "g");
    flier.setAttribute("class", "xian-stage-flier");
    flightLayer.appendChild(flier);
    svg.appendChild(flightLayer);
    return svg;
  }
  let statusEl = svgRoot && svgRoot.querySelector
    ? svgRoot.querySelector(".xian-stage-status") : null;
  const taskSubEl = svgRoot && svgRoot.querySelector
    ? svgRoot.querySelector(".xian-stage-task-sub") : null;
  const taskFootEl = svgRoot && svgRoot.querySelector
    ? svgRoot.querySelector(".xian-stage-task-foot") : null;
  const flightLayer = svgRoot && svgRoot.querySelector
    ? svgRoot.querySelector(".xian-stage-flight") : null;
  const flier = flightLayer && flightLayer.querySelector
    ? flightLayer.querySelector(".xian-stage-flier") : null;
  if (flier && doc && typeof doc.createElementNS === "function") {
    try {
      flightXian = createXian(flier, {
        doc, win, state: "downloading", size: 44, reduced,
        // The stage drives the same factory from its own pre-sampled flight
        // loop. Avoid a second independent rAF loop per stage.
        autoTick: false, speed: .85, decorative: true,
      });
      flightXian.el.setAttribute("x", "-22");
      flightXian.el.setAttribute("y", "-22");
    } catch (e) {
      flightXian = null;
    }
  }
  // 线路 → 预采样表；缺表时退回主链，保证不会出现 undefined。
  function planTable(plan) {
    const key = plan && plan.path;
    return (key && samples[key]) || samples.srcTask || [];
  }
  function planPoint(plan) {
    const table = planTable(plan);
    if (!table.length) return { x: 240, y: 150, ang: 0 };
    // 线路端点就是气泡边缘：往里退 3 个采样点（约 24px），弦才停在两框之间的空档里，
    // 而不是半截身子被气泡压住（截图里限流/完成的停靠点看不见）。
    const atEnd = plan && plan.at === "end";
    const last = table.length - 1;
    const index = Math.max(0, Math.min(last, (atEnd ? last : 0) + (atEnd ? -3 : 3)));
    const at = table[index];
    return { x: at.x, y: at.y, ang: at.ang || 0 };
  }
  // 静止锚点：停在该事件该停的位置（停放点优先于线路起点）。
  function anchorFor(plan) {
    if (!plan) return null;
    return planPoint(plan.park || plan);
  }
  function placeFlier(p, sy) {
    if (!flier || !p) return;
    const scale = Number(sy) > 0 ? Number(sy) : 1;
    flier.setAttribute("transform",
      "translate(" + p.x.toFixed(2) + " " + p.y.toFixed(2) + ") " +
      "rotate(" + Number(p.ang || 0).toFixed(2) + ") " +
      "scale(" + (2 - scale).toFixed(3) + " " + scale.toFixed(3) + ")");
  }
  const stage = {
    layout,
    samples,
    el: host,
    update(scene) {
      const s = scene || {};
      const phase = STAGE_PHASES.includes(s.phase) ? s.phase : "idle";
      current = phase;
      const counts = s.counts || {};
      const xianState = mapPhaseToXian(phase, {
        concurrency: layout.concurrency,
        failed: phase === "error" || Number(counts.fail) > 0,
        retrying: phase === "refreshing" || Number(counts.retrying) > 0,
      });
      const stateChanged = xianState !== prevXianState;
      prevXianState = xianState;
      const itemExists = s.itemStatus === "exists";
      // 单条落地计数：任务还在跑时「完成/已存在」每多一条，就补一次飞向磁盘。
      let doneNow = 0;
      const sceneItems = Array.isArray(s.items) ? s.items : [];
      for (let i = 0; i < sceneItems.length; i++) {
        const st = sceneItems[i] && sceneItems[i].status;
        if (st === "完成" || st === "已存在") doneNow++;
      }
      const focusKey = String(s.focusId || "");
      if (focusKey !== doneFocusId) {
        doneFocusId = focusKey;          // 切主任务/首次进入：历史完成不回放
        doneSeen = doneNow;
      }
      const pulse = doneNow > doneSeen && xianState !== "done";
      if (pulse) pulseToken += 1;        // 每次落地都换飞行 key，tick 才会重新起飞
      doneSeen = doneNow;
      currentPlan = pulse ? FLIGHT_PLAN.done : (FLIGHT_PLAN[xianState] || null);
      const isError = xianState === "failed";
      const held = phase === "flood_wait";
      const isDone = xianState === "done";
      const pct = Math.max(0, Math.min(100, Number(s.pct) || 0));
      const phaseCopy = stagePhaseCopy(phase, s);
      // 弦按事件走固定线路：扫描停在 Telegram、下载在来源→任务舱、限流停在
      // 账号池、重试从账号池进任务舱、失败原路退回、完成飞向磁盘。
      // 手机单列时四框是上下堆叠的短距离，弦再沿曲线飞只会穿过卡片/气泡；
      // 移动端只保留「停在事件对应的节点旁」，不做飞行。
      const canFly = !reduced && !itemExists && !!currentPlan && !activeLayout.mobile;
      const animate = canFly && (currentPlan.mode === "fly"
        ? !!s.busy
        : (currentPlan.mode === "once" && (stateChanged || pulse)));
      if (canFly && !animate) placeFlier(anchorFor(currentPlan), 1);
      const result = {
        phase,
        xianState,
        reduced,
        flights: animate ? 1 : 0,
        reverse: !!(currentPlan && currentPlan.reverse),
        held,
        countdown: held ? Number(s.countdown) || 0 : null,
        pct,
        receiver: isDone ? "ready" : (isError ? "shake" : (held ? "hold" : "idle")),
        busy: animate,
        flightKey: pulse ? "pulse:" + pulseToken : xianState,
      };
      lastScene = result;
      busy = result.busy;
      const accounts = Array.isArray(s.accounts) ? s.accounts : [];
      const nextAccountsKey = accounts.map(function (a) {
        return String(a && a.id || "") + ":" + String(a && a.name || "");
      }).join("|");
      if (nextAccountsKey !== accountsKey) {
        accountsKey = nextAccountsKey;
        sceneAccounts = accounts;
        renderNodeOverlay();
      }
      // 保存位置剩余空间（后端 disk_space）：只带数字，格式化在舞台里做。
      const freeText = formatDiskFree(s.disk && s.disk.free);
      const totalText = formatDiskFree(s.disk && s.disk.total);
      const nextDiskKey = freeText + "\u0000" + totalText;
      if (nextDiskKey !== diskKey) {
        diskKey = nextDiskKey;
        diskSub = freeText ? "剩余 " + freeText : "保存位置";
        diskTitle = freeText
          ? "本地磁盘 · 剩余 " + freeText + (totalText ? "（共 " + totalText + "）" : "")
          : "本地磁盘 · 保存位置";
        renderNodeOverlay();
      }
      if (flightXian) {
        try { flightXian.setState(xianState); } catch (e) { /* 视觉降级 */ }
      }
      if (flightLayer) flightLayer.setAttribute("data-flight",
        !canFly ? "idle" : (animate ? (result.reverse ? "reverse" : "forward") : "hold"));
      if (svgRoot && svgRoot.setAttribute) {
        svgRoot.setAttribute("data-phase", phase);
        svgRoot.setAttribute("data-phase-tone", phaseCopy.tone);
        svgRoot.setAttribute("data-busy", busy ? "on" : "off");
      }
      if (statusEl) {
        statusEl.textContent = phaseCopy.status;
        if (phaseCopy.speedMode === "speed" && Number(s.speed) > 0) {
          statusEl.textContent += " · " + formatStageSpeed(s.speed);
        }
      }
      if (bandStatusEl) bandStatusEl.textContent = phaseCopy.status;
      const activeCount = Math.max(0, Number(counts.active) || 0);
      const failedCount = Math.max(0, Number(counts.fail) || 0);
      const pausedCount = Math.max(0, Number(counts.paused) || 0);
      const skippedCount = Math.max(0, Number(counts.skip) || 0);
      const totalCount = Math.max(0, Number(s.taskTotal) || 0);
      const finishedCount = Math.max(0, Number(s.taskFinished) || 0);
      const pendingCount = Math.max(
        0, totalCount - finishedCount - failedCount - skippedCount - pausedCount,
      );
      if (taskSubEl) {
        taskSubEl.textContent = phaseCopy.sub;
      }
      if (taskFootEl) {
        taskFootEl.textContent = "待处理 " + pendingCount + " · 完成 " +
          finishedCount + "/" + totalCount + " · 并发 " +
          activeCount + "/" + Math.max(0, Number(s.parallel) || 0) +
          (failedCount ? " · 失败 " + failedCount : "") +
          (pausedCount ? " · 暂停 " + pausedCount : "");
      }
      const aggregated = aggregateItems(s.items);
      renderCards(s.items, s.speed, s.operation);
      renderSlots(s);
      renderTaskList(s.tasks, s.focusId);
      renderBay(s, phaseCopy, aggregated);
      layoutBay();
      if (busy) schedule();
      else if (raf && win && typeof win.cancelAnimationFrame === "function") {
        try { win.cancelAnimationFrame(raf); } catch (e) { /* ignore */ }
        raf = 0;
      }
      return result;
    },
    busy() { return busy; },
    tick() {
      // All flight points come from the pre-sampled tables; no live path read.
      if (busy && flightLayer && flier) {
        const now = clockNow(win);
        const plan = currentPlan;
        const table = planTable(plan);
        const reverse = !!(plan && plan.reverse);
        if (!flight || flight.key !== (lastScene && lastScene.flightKey)) {
          const start = reverse ? table[table.length - 1] : table[0];
          flight = {
            key: lastScene && lastScene.flightKey,
            started: now,
            start: start || { x: 240, y: 150, ang: 0 },
          };
        }
        const elapsed = Math.max(0, now - flight.started);
        const k = Math.max(0, Math.min(1, elapsed / flightDuration));
        const p = pointAt(table, reverse ? 1 - k : k) ||
          flight.start;
        const ant = anticipationOffset(k, p.ang);
        const sy = squashStretch(k, trapezoidSpeed(k));
        const bank = bankAngle(p.ang, 0, 0);
        const tx = p.x + (ant.dx || 0);
        const ty = p.y + (ant.dy || 0);
        flier.setAttribute("transform",
          "translate(" + tx.toFixed(2) + " " + ty.toFixed(2) + ") " +
          "rotate(" + (p.ang + bank + ant.angle).toFixed(2) + ") " +
          "scale(" + (2 - sy).toFixed(3) + " " + sy.toFixed(3) + ")");
        if (flightXian) {
          flightXian.setState(lastScene.xianState);
          flightXian.tick(now, .85);
        }
        if (k >= 1) {
          if (loopFlights && lastScene && lastScene.busy) {
            // Keep the task's live line occupied while the task is busy;
            // this is still one shared rAF, not one loop per item.
            flight.started = now;
          } else {
            busy = false;
            flight = null;
            if (plan && plan.park) {
              // 「飞完有落点」的事件（完成/失败）停在落点上，不消失。
              placeFlier(anchorFor(plan), 1);
              if (flightLayer) flightLayer.setAttribute("data-flight", "hold");
            } else if (flightLayer) {
              flightLayer.setAttribute("data-flight", "idle");
            }
            if (svgRoot && svgRoot.setAttribute) {
              svgRoot.setAttribute("data-busy", "off");
            }
          }
        }
      }
      return lastScene;
    },
    destroy() {
      destroyed = true;
      busy = false;
      if (raf && win && typeof win.cancelAnimationFrame === "function") {
        try { win.cancelAnimationFrame(raf); } catch (e) { /* ignore */ }
      }
      raf = 0;
      if (flightXian && typeof flightXian.destroy === "function") {
        try { flightXian.destroy(); } catch (e) { /* ignore */ }
        flightXian = null;
      }
      if (host && typeof host.removeAttribute === "function") {
        host.removeAttribute("data-stage-busy");
      }
    },
    // onLeave 用：暂停共享 rAF（不再产生新帧），释放 busy，但保留宿主节点
    // 与已渲染 DOM；回页后由下一次 update 正常恢复。
    pause() {
      busy = false;
      flight = null;
      if (raf && win && typeof win.cancelAnimationFrame === "function") {
        try { win.cancelAnimationFrame(raf); } catch (e) { /* ignore */ }
      }
      raf = 0;
      if (flightLayer && host && svgRoot && svgRoot.setAttribute) {
        flightLayer.setAttribute("data-flight", "idle");
        svgRoot.setAttribute("data-busy", "off");
      }
      if (host && typeof host.setAttribute === "function") {
        host.setAttribute("data-stage-busy", "off");
      }
      return { paused: true, busy: busy, pending: win && typeof win.pending === "function" ? win.pending() : 0 };
    },
  };
  function aggregateItems(items) {
  return buildAggregate(items);
}
function buildAggregate(items) {
    const list = Array.isArray(items) ? items : [];
    const agg = { failed: 0, retrying: 0, copying: 0, waiting: 0, paused: 0, done: 0, exists: 0, skipped: 0, total: 0, visible: 0 };
    for (const item of list) {
      const st = stageItemState(item && item.status);
      agg.total++;
      if (st === "failed") agg.failed++;
      else if (st === "retrying") agg.retrying++;
      else if (st === "copying") agg.copying++;
      else if (st === "waiting") agg.waiting++;
      else if (st === "paused") agg.paused++;
      else if (st === "done") agg.done++;
      else if (st === "exists") agg.exists++;
      else if (st === "skipped") agg.skipped++;
    }
    return agg;
  }
function safeTaskTitle(task) {
  if (!task) return "—";
  return String(task.title || task.channel_title || task.channel || "未命名任务");
}
// 视频块上限：移动/紧凑/平板 2；桌面按条数自适应——8 条以内全部铺开
//（2 列 × 4 行，卡高约 31px），超过 8 条回到 6 条 + 一行精确聚合。
function cardLimit(total) {
  const box = activeLayout || layout;
  if (box.compact || box.mobile || box.tablet) return 2;
  const n = Math.max(0, Number(total) || 0);
  return n <= 8 ? 8 : 6;
}
  function renderCards(items, speed, operation) {
    if (!cardsLayer || !doc || typeof doc.createElement !== "function") return;
    const list = Array.isArray(items) ? items : [];
    const limit = cardLimit(list.length);
    const compact = activeLayout.compact || activeLayout.mobile || activeLayout.tablet;
    const rank = {
      floodwait: 0, failed: 1, retrying: 2, downloading: 3,
      copying: 4, waiting: 5, paused: 6, done: 7, exists: 8, skipped: 9,
    };
    const sorted = list.slice().sort(function (a, b) {
      return (rank[stageItemState(a && a.status)] ?? 99) -
        (rank[stageItemState(b && b.status)] ?? 99);
    });
    // Failure items must never be pushed out by ordinary downloads:
    // keep every failed block first, then fill remaining slots.
    const failed = sorted.filter((it) => stageItemState(it && it.status) === "failed");
    const others = sorted.filter((it) => stageItemState(it && it.status) !== "failed");
    const visible = failed.concat(others).slice(0, limit);
    const visibleKeys = new Set(visible.map(function (it) {
      return String(it && it.name || "") + "\u0000" + String(it && it.status || "");
    }));
    const hiddenItems = list.filter(function (it) {
      const key = String(it && it.name || "") + "\u0000" + String(it && it.status || "");
      return !visibleKeys.has(key);
    });
    // Aggregate must reflect exactly the hidden (not-shown) entries so the
    // bucket never double-counts a visible card nor silently drops
    // retrying/copying states.
    const aggregated = buildAggregate(hiddenItems);
    if (bayCountEl && cardsLayer) bayCountEl.textContent = String(list.length);
    cardsLayer.textContent = "";
    for (const item of visible) {
      const state = stageItemState(item && item.status);
      const card = doc.createElement("div");
      card.className = "xian-stage-card";
      card.setAttribute("data-state", state);
      const head = doc.createElement("div");
      head.className = "xian-stage-card-head";
      const name = doc.createElement("span");
      name.className = "xian-stage-card-name";
      name.textContent = String(item && item.name || "");
      name.setAttribute("title", String(item && item.name || ""));
      const stateLabel = doc.createElement("span");
      stateLabel.className = "xian-stage-card-state";
      stateLabel.textContent = String(item && item.status || "等待");
      head.appendChild(name);
      head.appendChild(stateLabel);
      const bar = doc.createElement("div");
      bar.className = "xian-stage-card-bar";
      const fill = doc.createElement("i");
      const pct = Number(item && item.pct);
      const pctValue = Number.isFinite(pct) ? Math.max(0, Math.min(100, pct)) : 0;
      fill.setAttribute("style", "width:" + pctValue.toFixed(1) + "%");
      bar.appendChild(fill);
      const meta = doc.createElement("span");
      meta.className = "xian-stage-card-meta";
      const per = Number(item && item.speed);
      const errText = String(item && item.error || "");
      // 错误并入读数行（单行省略 + title），避免失败卡片多一行被裁掉。
      // 没有速度就不写占位「—」：旧卡片「0% · —」看起来像坏掉的读数。
      const metaParts = [Number.isFinite(pct) ? pctValue.toFixed(0) + "%" : "等待"];
      if (Number.isFinite(per) && per > 0) metaParts.push(formatStageSpeed(per));
      if (errText) metaParts.push(errText);
      meta.textContent = metaParts.join(" · ");
      if (errText) {
        meta.setAttribute("data-has-error", "on");
        meta.setAttribute("title", errText);
      }
      card.appendChild(head);
      card.appendChild(bar);
      card.appendChild(meta);
      cardsLayer.appendChild(card);
    }
    // 隐藏桶中的失败数（visible 已优先展示失败，故隐藏失败不重复进卡片）
    const hiddenFails = aggregated.failed;
    if (aggregateEl && doc && typeof doc.createElement === "function") {
      renderAggregate(aggregateEl, aggregated, hiddenItems.length, hiddenFails);
    }
    if (cardsLayer.setAttribute) {
      cardsLayer.setAttribute("data-layout",
        compact ? "compact" : "desktop");
      cardsLayer.setAttribute("data-operation", String(operation || "download"));
      cardsLayer.setAttribute("data-speed", String(Number(speed) || 0));
    }
  }
  function renderSlots(s) {
    if (!slotEl || !doc || typeof doc.createElement !== "function") return;
    const model = stageSlotModel(s && s.parallel, s && s.counts && s.counts.active);
    if (model.hidden) {
      slotEl.hidden = true;
      slotEl.textContent = "";
      return;
    }
    slotEl.hidden = false;
    slotEl.textContent = "";
    for (let i = 0; i < model.slots; i++) {
      const slot = doc.createElement("span");
      slot.className = "xian-stage-slot";
      slot.setAttribute("data-lit", i < model.lit ? "on" : "off");
      slotEl.appendChild(slot);
    }
    if (model.overflow > 0) {
      const badge = doc.createElement("span");
      badge.className = "xian-stage-slot-more";
      badge.textContent = "×" + model.parallel;
      slotEl.appendChild(badge);
    }
  }
  function renderAggregate(node, aggregated, hidden, hiddenFails) {
    if (!node) return;
    node.textContent = "";
    node.setAttribute("data-hidden", String(hidden));
    node.setAttribute("data-hidden-fails", String(hiddenFails));
    const agg = aggregated || { failed: 0, retrying: 0, copying: 0, waiting: 0, paused: 0, done: 0, exists: 0, skipped: 0 };
    if (hidden <= 0 && hiddenFails <= 0 && agg.retrying <= 0 && agg.copying <= 0) {
      // 没有隐藏桶就没有这一行：旧文案「全部视频已在此列出」占着一行高度。
      node.hidden = true;
      return;
    }
    node.hidden = false;
    const dots = doc.createElement("span");
    dots.className = "xian-stage-agg-dots";
    const parts = [
      ["fail", "失败", agg.failed],
      ["retry", "重试", agg.retrying],
      ["copy", "复制", agg.copying],
      ["wait", "等待", agg.waiting],
      ["paused", "暂停", agg.paused],
      ["done", "完成", agg.done],
      ["exists", "已存在", agg.exists],
      ["skipped", "已跳过", agg.skipped],
    ];
    for (const [cls, label, n] of parts) {
      if (!n) continue;
      const dot = doc.createElement("span");
      dot.className = "xian-stage-agg-dot " + cls;
      dot.textContent = label + " " + n;
      dots.appendChild(dot);
    }
    node.appendChild(dots);
    if (hidden > 0) {
      const more = doc.createElement("span");
      more.className = "xian-stage-agg-more";
      const failText = hiddenFails > 0 ? "（含 " + hiddenFails + " 条失败）" : "";
      more.textContent = "另有 " + hidden + " 条" + failText + " ›";
      node.appendChild(more);
    }
  }
  function renderBay(s, phaseCopy, aggregated) {
    layoutBay();
    if (!bayLayer || !doc || typeof doc.createElement !== "function") return;
    const list = Array.isArray(s.tasks) ? s.tasks : [];
    const multi = list.length > 1;
    bayLayer.hidden = !list.length;
    if (!list.length) return;
    bayLayer.setAttribute("data-many", multi ? "on" : "off");
    // 焦点被移除时回落到队列首位，不让标题退化成 “—”。
    const task = list.find(function (t) {
      return t && String(t.task_id || "") === String(s.focusId || "");
    }) || queueOrder(list)[0] || null;
    const counts = (task && task.counts) || {};
    const tTotal = Math.max(0, Number(task && task.task_total) || 0);
    const tFinished = Math.max(0, Number(task && task.task_finished) || 0);
    const tActive = Math.max(0, Number(counts.active) || 0);
    const tFailed = Math.max(0, Number(counts.fail) || 0);
    const tPaused = Math.max(0, Number(counts.paused) || 0);
    const tParallel = Math.max(0, Number(task && task.parallel) || 0);
    if (currentEl) {
      // 舱名 = 焦点任务名：用户要从标题直接认出是哪个任务。
      const title = task ? safeTaskTitle(task) : "任务舱";
      currentEl.textContent = title;
      currentEl.setAttribute("title", title);
    }
    if (footEl) {
      const parts = [
        "待处理 " + Math.max(0, tTotal - tFinished - tFailed - tPaused),
        "完成 " + tFinished + "/" + tTotal,
        "并发 " + tActive + "/" + tParallel,
      ];
      if (tFailed) parts.push("失败 " + tFailed);
      if (tPaused) parts.push("暂停 " + tPaused);
      footEl.textContent = parts.join(" · ");
      footEl.setAttribute("data-has-fail", tFailed ? "on" : "off");
    }
  }
  // Desktop and compact both use the structured HTML bay. Compact hides the
  // rail and places the same task surface below the link diagram.
  function layoutBay() {
    if (!bayLayer || !cardsLayer || !host || typeof host.appendChild !== "function") return;
    if (workbenchEl && cardsLayer.parentNode !== workbenchEl) {
      workbenchEl.appendChild(cardsLayer);
    }
  }
// 竖列队列条最多 3 条；1024–1279 舱高只容 2 条 + 「另有 N 条」。
function queueMax() {
  const box = activeLayout || layout;
  // 手机舱宽只有 ~266px：3 条会把任务名压成几个字，2 条 + 「另有 N 条」才读得出。
  if (box.mobile) return 2;
  if (box.compact || box.tablet) return 3;
  const width = win && Number.isFinite(win.innerWidth) ? win.innerWidth : 1920;
  return width < 1280 ? 2 : 3;
}
  function renderTaskList(tasks, focusId) {
    if (!taskListLayer || !doc || typeof doc.createElement !== "function") return;
    const list = Array.isArray(tasks) ? tasks : [];
    // 多任务在一切布局下都渲染队列条：桌面为左侧竖列，
    // compact/mobile 由 CSS 折成舞台下方的横向胶囊（可滑动）。
    const multi = list.length > 1;
    taskListLayer.hidden = !multi;
    if (!multi) {
      taskListLayer.textContent = "";
      if (pagerEl) pagerEl.hidden = true;
      return;
    }
    const focus = String(focusId || "");
    // 固定排序：下载中 > FloodWait > 重试 > 暂停 > 失败 > 完成（失败项永不消失）。
    const ordered = queueOrder(list);
    const QUEUE_MAX = queueMax();
    const shown = railExpanded ? ordered : ordered.slice(0, QUEUE_MAX);
    taskListLayer.setAttribute("data-count", String(list.length));
    taskListLayer.textContent = "";
    shown.forEach(function (task, index) {
      const t = task || {};
      const tid = String(t.task_id || "");
      const counts = t.counts || {};
      const total = Math.max(0, Number(t.task_total) || 0);
      const finished = Math.max(0, Number(t.task_finished) || 0);
      const active = Math.max(0, Number(counts.active) || 0);
      const failed = Math.max(0, Number(counts.fail) || 0);
      // flood_wait 用真实倒计时（从 phase_detail 解析），不再写死的 0s。
      const cd = t.phase === "flood_wait" ? parseFloodWaitSeconds(t) : 0;
      const copy = stagePhaseCopy(t.phase, {
        counts: counts, taskTotal: total, taskFinished: finished,
        countdown: cd,
      });
      const item = doc.createElement("button");
      item.className = "xian-stage-task-item" + (tid === focus ? " is-active" : "");
      item.setAttribute("type", "button");
      item.setAttribute("data-task-id", tid);
      item.setAttribute("data-phase", String(t.phase || "idle"));
      item.setAttribute("aria-pressed", tid === focus ? "true" : "false");
      const title = String(t.title || t.channel_title || t.channel || "未命名任务");
      item.setAttribute("title", title);
      item.setAttribute("aria-label", title + " · " + copy.label);
      const ordinal = doc.createElement("span");
      ordinal.className = "xian-stage-task-index";
      ordinal.textContent = "T" + (index + 1);
      const name = doc.createElement("span");
      name.className = "xian-stage-task-name";
      name.textContent = title;
      name.setAttribute("title", title);
      const bar = doc.createElement("span");
      bar.className = "xian-stage-task-bar";
      const fill = doc.createElement("i");
      const pct = total > 0 ? Math.max(0, Math.min(100, finished * 100 / total)) : 0;
      fill.setAttribute("style", "width:" + pct.toFixed(1) + "%");
      bar.appendChild(fill);
      item.appendChild(ordinal);
      item.appendChild(name);
      item.appendChild(bar);
      item.addEventListener("click", function () {
        if (typeof options.onTaskSelect === "function") {
          options.onTaskSelect(tid);
        }
      });
        taskListLayer.appendChild(item);
      });
      // 队列最多 3 条：溢出收敛为一行「另有 N 条」，点开就地展开，不重排舞台。
      if (ordered.length > QUEUE_MAX) {
        const more = doc.createElement("button");
        more.className = "xian-stage-queue-more";
        more.setAttribute("type", "button");
        more.textContent = railExpanded
          ? "收起 ‹"
          : "另有 " + (ordered.length - QUEUE_MAX) + " 条 ›";
        more.addEventListener("click", function () {
          railExpanded = !railExpanded;
          renderTaskList(list, focusId);
        });
        taskListLayer.appendChild(more);
      }

    // 键盘可达性：桌面索引栏支持 ArrowUp/ArrowDown/Home/End 切换焦点，
    // 并同步 aria-pressed；移动/紧凑端也保留键盘可达性以便外接键盘。
    if (taskListLayer && typeof taskListLayer.removeEventListener === "function") {
      taskListLayer.onkeydown = null;
      taskListLayer.removeEventListener("keydown", railKeydownHandler);
      const items = ordered.slice();
      railKeydownHandler = function (ev) {
        const key = ev && ev.key;
        if (key !== "ArrowUp" && key !== "ArrowDown" &&
          key !== "Home" && key !== "End") return;
        ev.preventDefault();
        let idx = items.findIndex(function (t) {
          return String(t && t.task_id || "") === focus;
        });
        if (idx < 0) idx = 0;
        if (key === "ArrowUp") idx = (idx - 1 + items.length) % items.length;
        else if (key === "ArrowDown") idx = (idx + 1) % items.length;
        else if (key === "Home") idx = 0;
        else if (key === "End") idx = items.length - 1;
        const nextId = items[idx] && items[idx].task_id;
        if (nextId) {
          if (typeof options.onTaskSelect === "function") {
            options.onTaskSelect(String(nextId));
          }
          // Keyboard navigation must update the visible aria-pressed state
          // in the same frame, before the next polling refresh arrives.
          renderTaskList(ordered, String(nextId));
        }
      };
      taskListLayer.addEventListener("keydown", railKeydownHandler);
      taskListLayer.setAttribute("role", "listbox");
      taskListLayer.setAttribute("aria-label", "任务队列 · 方向键切换");
      shown.forEach(function (t, i) {
        const btn = taskListLayer.children[i];
        if (!btn) return;
        btn.setAttribute("role", "option");
        btn.setAttribute("aria-selected", String(t && String(t.task_id || "") === focus));
      });
    }

    // 移动/紧凑舱内分页器：1 / N · 当前任务名 + 上一项/下一项；
    // 桌面（rail 可见）与单任务（<=1）均隐藏。
    const compactMode = activeLayout.compact || activeLayout.mobile;
    const focusIndex = ordered.findIndex(function (t) {
      return String(t && t.task_id || "") === focus;
    });
    if (pagerEl) {
      const showPager = compactMode && ordered.length > 1;
      pagerEl.hidden = !showPager;
      if (showPager) {
        const cur = ordered[focusIndex < 0 ? 0 : focusIndex] || null;
        const curName = safeTaskTitle(cur);
        pagerEl.setAttribute("data-focus-index", String(focusIndex < 0 ? 0 : focusIndex));
        const textNode = pagerEl.querySelector(".xian-stage-pager-text");
        if (textNode) {
          textNode.textContent = (focusIndex < 0 ? 1 : focusIndex + 1) +
            " / " + ordered.length + " · " + curName;
        }
        const prevBtn = pagerEl.querySelector(".xian-stage-pager-prev");
        const nextBtn = pagerEl.querySelector(".xian-stage-pager-next");
        const step = function (dir) {
          const raw = Number(pagerEl.getAttribute("data-focus-index"));
          let i = Number.isFinite(raw) ? Math.max(0, Math.min(ordered.length - 1, raw)) : 0;
          i = (i + dir + ordered.length) % ordered.length;
          pagerEl.setAttribute("data-focus-index", String(i));
          const id = ordered[i] && ordered[i].task_id;
          if (id && typeof options.onTaskSelect === "function") {
            options.onTaskSelect(String(id));
          }
        };
        if (prevBtn) {
          prevBtn.onclick = function () { step(-1); };
          prevBtn.disabled = ordered.length <= 1;
        }
        if (nextBtn) {
          nextBtn.onclick = function () { step(1); };
          nextBtn.disabled = ordered.length <= 1;
        }
      }
    }
  }
  function renderNodeOverlay() {
    if (!nodeOverlay || !doc || typeof doc.createElement !== "function") return;
    nodeOverlay.textContent = "";
    const box = activeLayout || layout;
    const width = Math.max(1, Number(box.viewBox[2]) || 1);
    const height = Math.max(1, Number(box.viewBox[3]) || 1);
    const codes = { src: "TG", acct: "AC", disk: "DS", nas: "NS" };
    // 账号池不写抽象名：直接显示真实账号（首个）+ 总数，否则认不出是哪个号。
    const acctList = Array.isArray(sceneAccounts) ? sceneAccounts : [];
    const first = acctList[0] || null;
    let acctLabel = first
      ? String(first.name || first.id || "账号池") : "账号池";
    if (acctList.length > 1) {
      // The node is small, but dropping the small account made the stage look
      // like only the main account existed. Keep one compact suffix.
      acctLabel += " + " + String(acctList[1].name || "小号");
    }
    const acctSub = acctList.length
      ? "账号池 · 共 " + acctList.length + " 个" : "账号池 · 未登录";
    const labels = {
      src: "Telegram", acct: acctLabel, disk: "本地磁盘", nas: "NAS / WebDAV",
    };
    const subs = {
      src: "来源 · 读取清单", acct: acctSub, disk: diskSub, nas: "备份目标",
    };
    for (const key of Object.keys(box.nodes || {})) {
      if (key === "task") continue;
      const n = box.nodes[key];
      if (!n) continue;
      const chip = doc.createElement("div");
      chip.className = "xian-stage-node-chip";
      chip.setAttribute("data-node", key);
      // 磁盘节点带上剩余空间读数：这条读数比「保存位置」四个字有用。
      if (key === "disk") {
        chip.setAttribute("title", diskTitle);
      }
      chip.setAttribute("style",
        "left:" + (n.x / width * 100).toFixed(4) + "%;" +
        "top:" + (n.y / height * 100).toFixed(4) + "%;" +
        "width:" + (n.w / width * 100).toFixed(4) + "%;" +
        "height:" + (n.h / height * 100).toFixed(4) + "%;");
      const code = doc.createElement("span");
      code.className = "xian-stage-node-code";
      code.textContent = codes[key] || key.toUpperCase();
      const text = doc.createElement("div");
      text.className = "xian-stage-node-text";
      const label = doc.createElement("span");
      label.className = "xian-stage-node-label";
      label.textContent = labels[key] || n.label;
      label.setAttribute("title", label.textContent);
      text.appendChild(label);
      const subText = subs[key] || "";
      if (subText) {
        const sub = doc.createElement("span");
        sub.className = "xian-stage-node-sub";
        sub.textContent = subText;
        text.appendChild(sub);
      }
      chip.appendChild(code);
      chip.appendChild(text);
      nodeOverlay.appendChild(chip);
    }
  }
  function schedule() {
    if (destroyed || raf || !win || typeof win.requestAnimationFrame !== "function") return;
    const loop = () => {
      raf = 0;
      if (destroyed || !busy) return;
      stage.tick();
      if (busy) raf = win.requestAnimationFrame(loop);
    };
    raf = win.requestAnimationFrame(loop);
  }
  stage.setLayout = function setLayout(next) {
    const target = stageLayout(next || {});
    if (target.mobile === activeLayout.mobile &&
      target.compact === activeLayout.compact &&
      target.tablet === activeLayout.tablet &&
      target.concurrency === activeLayout.concurrency &&
      target.nas === activeLayout.nas) return false;
    activeLayout = target;
    stage.layout = target;
    if (host && typeof host.setAttribute === "function") {
      host.setAttribute("data-stage-layout", layoutName(target));
    }
    if (svgRoot && typeof svgRoot.setAttribute === "function") {
      svgRoot.setAttribute("viewBox", target.viewBox.join(" "));
    }
    // 只改 viewBox 会把桌面几何拉进手机画布（右侧多出一块深色面板）。
    samples = Object.fromEntries(
      Object.entries(target.wires).map(([key, d]) => [key, sampleCubicPath(d, 8)]),
    );
    stage.samples = samples;
    flight = null;
    rebuildGeometry(target);
    if (cardsLayer && typeof cardsLayer.setAttribute === "function") {
      cardsLayer.setAttribute("data-layout", layoutName(target));
    }
    renderNodeOverlay();
    layoutBay();
    return true;
  };
  return stage;
}
