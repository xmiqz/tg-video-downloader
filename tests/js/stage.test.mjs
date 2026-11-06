// ============================================================
// T66 下载链路舞台：阶段映射 / 六项物理 / 队列排序 / 槽位规则 /
// 空事件不产帧 / 幂等去重。纯函数与注入式假文档，Node 直接跑。
// ============================================================
import test from "node:test";
import assert from "node:assert/strict";

import {
  STAGE_PHASES, STAGE_EDGES, stageLayout, sampleCubicPath,
  trapezoidProgress, trapezoidSpeed, squashStretch, landingRebound,
  bankAngle, anticipationOffset, trailLength, receiverFeedback,
  stageItemState, queueRank, queueOrder, parseFloodWaitSeconds,
  formatStageSpeed, stagePhaseCopy, stageSlotModel,
  formatDiskFree,
  createStage,
} from "../../web/js/motion/stage.js";
import { flapFrequency } from "../../web/js/motion/xian.js";

// ---------------------------------------------------------- 假文档
function fakeNode(tag) {
  const n = {
    tagName: tag,
    attrs: {},
    children: [],
    parent: null,
    hidden: false,
    disabled: false,
    onclick: null,
    onkeydown: null,
    _text: "",
    _listeners: {},
    setAttribute(k, v) { n.attrs[k] = String(v); },
    removeAttribute(k) { delete n.attrs[k]; },
    getAttribute(k) { return n.attrs[k] == null ? null : n.attrs[k]; },
    appendChild(c) { c.parent = n; n.children.push(c); return c; },
    remove() {
      if (!n.parent) return;
      const i = n.parent.children.indexOf(n);
      if (i >= 0) n.parent.children.splice(i, 1);
      n.parent = null;
    },
    addEventListener(type, fn) {
      (n._listeners[type] || (n._listeners[type] = [])).push(fn);
    },
    removeEventListener(type, fn) {
      const arr = n._listeners[type] || [];
      const i = arr.indexOf(fn);
      if (i >= 0) arr.splice(i, 1);
    },
    fire(type) {
      (n._listeners[type] || []).slice().forEach((fn) => fn({ type, preventDefault() {} }));
    },
    querySelector(sel) { return findFirst(n, sel); },
    querySelectorAll(sel) { const out = []; findAll(n, sel, out); return out; },
    classList: {
      add(c) {
        const list = String(n.attrs.class || "").split(/\s+/).filter(Boolean);
        if (list.indexOf(c) < 0) list.push(c);
        n.attrs.class = list.join(" ");
      },
      contains(c) { return String(n.attrs.class || "").split(/\s+/).indexOf(c) >= 0; },
      remove(c) {
        n.attrs.class = String(n.attrs.class || "").split(/\s+/)
          .filter((x) => x && x !== c).join(" ");
      },
    },
  };
  Object.defineProperty(n, "className", {
    get() { return String(n.attrs.class || ""); },
    set(v) { n.attrs.class = String(v); },
  });
  Object.defineProperty(n, "textContent", {
    get() { return n._text; },
    set(v) { n._text = String(v); n.children.length = 0; },
  });
  return n;
}

function matches(el, sel) {
  if (sel.charAt(0) === ".") {
    return String(el.attrs.class || "").split(/\s+/).indexOf(sel.slice(1)) >= 0;
  }
  return el.tagName === sel;
}
function findFirst(root, sel) {
  for (const c of root.children) {
    if (matches(c, sel)) return c;
    const hit = findFirst(c, sel);
    if (hit) return hit;
  }
  return null;
}
function findAll(root, sel, out) {
  for (const c of root.children) {
    if (matches(c, sel)) out.push(c);
    findAll(c, sel, out);
  }
}

function fakeDoc() {
  return {
    createElement: (tag) => fakeNode(tag),
    createElementNS: (ns, tag) => fakeNode(tag),
    defaultView: null,
  };
}

function fakeWin() {
  const clock = { t: 0 };
  const queue = new Map();
  let nextId = 1;
  const win = {
    requests: 0,
    cancelCalls: 0,
    matchMedia() { return { matches: false }; },
    getComputedStyle() { return { getPropertyValue: () => "720ms" }; },
    performance: { now: () => clock.t },
    requestAnimationFrame(fn) {
      const id = nextId++;
      win.requests++;
      queue.set(id, fn);
      return id;
    },
    cancelAnimationFrame(id) {
      win.cancelCalls++;
      queue.delete(id);
    },
    pending() { return queue.size; },
    step(ms) {
      clock.t = ms;
      const fns = Array.from(queue.values());
      queue.clear();
      fns.forEach((fn) => fn(ms));
    },
  };
  return win;
}

function stageFixture(opts) {
  const doc = fakeDoc();
  const win = fakeWin();
  const host = fakeNode("div");
  const api = createStage(host, Object.assign({
    doc, win, reduced: false, layout: { concurrency: 4 },
  }, opts || {}));
  return { doc, win, host, api };
}

function tasks() {
  // 故意逆序喂入：舞台必须先排序再渲染。
  return [
    { task_id: "t6", title: "己", phase: "done",
      counts: { done: 5 },
      task_total: 5, task_finished: 5, parallel: 1 },
    { task_id: "t5", title: "戊", phase: "error",
      counts: { fail: 3 },
      task_total: 9, task_finished: 5, parallel: 2 },
    { task_id: "t4", title: "丁", phase: "fetching",
      counts: { active: 0, paused: 2 },
      task_total: 8, task_finished: 3, parallel: 2 },
    { task_id: "t3", title: "丙", phase: "fetching",
      counts: { active: 1, retrying: 1 },
      task_total: 4, task_finished: 1, parallel: 2 },
    { task_id: "t2", title: "乙", phase: "flood_wait",
      phase_detail: "FloodWait 30s", counts: { active: 0 },
      task_total: 6, task_finished: 2, parallel: 2 },
    { task_id: "t1", title: "甲", phase: "downloading",
      counts: { active: 2, fail: 0, paused: 0, retrying: 0 },
      task_total: 10, task_finished: 4, parallel: 4 },
  ];
}

function scene(extra) {
  return Object.assign({
    phase: "downloading", pct: 75, speed: 751 * 1024,
    taskTotal: 42, taskFinished: 30, parallel: 4, busy: true,
    counts: { active: 2, done: 30, fail: 0, paused: 0, skip: 0 },
    tasks: tasks(), focusId: "t1", operation: "download",
    items: [
      { name: "a.mp4", status: "下载中", pct: 40, speed: 320 * 1024 },
      { name: "b.mp4", status: "等待中", pct: 0 },
      { name: "c.mp4", status: "失败", pct: 12, error: "连接中断" },
    ],
  }, extra || {});
}

// ---------------------------------------------------------- 阶段映射 100%
test("STAGE_PHASES 每一态都有文案，未知态降级等待", () => {
  assert.equal(STAGE_PHASES.length, 10, "十态");
  for (const phase of STAGE_PHASES) {
    const copy = stagePhaseCopy(phase, { counts: { active: 2 }, taskTotal: 5, taskFinished: 1 });
    assert.ok(copy.label && copy.label.length > 0, phase + " label");
    assert.ok(copy.status && copy.status.length > 0, phase + " status");
    assert.ok(["live", "warn", "ok", "error", "idle"].indexOf(copy.tone) >= 0, phase + " tone");
    assert.ok(["none", "speed", "hold"].indexOf(copy.speedMode) >= 0, phase + " speedMode");
    assert.ok(["scan", "transfer", "hold", "settle", "none"].indexOf(copy.motion) >= 0,
      phase + " motion");
  }
  assert.equal(stagePhaseCopy("").label, "等待");
  assert.equal(stagePhaseCopy(null).label, "等待");
  assert.equal(stagePhaseCopy("uploaded").label, "等待", "内核无上传阶段，不得显示 uploaded");
  assert.equal(stagePhaseCopy("downloading", { counts: { active: 2 }, taskTotal: 5,
    taskFinished: 1 }).status, "正在下载 2 条 · 等待 2 条");
});

test("STAGE_EDGES 覆盖内核合法边且无自环/重复/终态出边", () => {
  assert.equal(STAGE_EDGES.length, 30);
  const seen = new Set();
  for (const e of STAGE_EDGES) {
    assert.ok(STAGE_PHASES.indexOf(e.from) >= 0, "未知起点 " + e.from);
    assert.ok(STAGE_PHASES.indexOf(e.to) >= 0, "未知终点 " + e.to);
    assert.notEqual(e.from, e.to, "不得自环");
    const key = e.from + "→" + e.to;
    assert.ok(!seen.has(key), "重复边 " + key);
    seen.add(key);
    assert.ok(["done", "aborted", "error"].indexOf(e.from) < 0, "终态不得出边");
  }
});

// ---------------------------------------------------------- 速度端点与单调
test("梯形速度：进度 0→1、速度端点归零且巡航峰值 1.35×", () => {
  assert.equal(trapezoidProgress(0), 0);
  assert.equal(trapezoidProgress(1), 1);
  let prev = -1;
  for (let k = 0; k <= 1; k += 0.01) {
    const p = trapezoidProgress(k);
    assert.ok(p > prev, "进度必须单调 " + k);
    assert.ok(p >= 0 && p <= 1, "进度夹在 0–1 " + k);
    prev = p;
  }
  assert.equal(trapezoidSpeed(0), 0);
  assert.equal(trapezoidSpeed(1), 0);
  assert.ok(Math.abs(trapezoidSpeed(0.5) - 1.3514) < 0.01, "巡航峰值应 1.35×");
  assert.ok(trapezoidSpeed(0.1) < trapezoidSpeed(0.5), "加速段递增");
  assert.ok(trapezoidSpeed(0.9) < trapezoidSpeed(0.5), "减速段递减");
});

// ---------------------------------------------------------- 六项物理
test("六项物理各一条断言", () => {
  // 1) 梯形速度峰值 1.35×
  assert.ok(Math.abs(trapezoidSpeed(0.5) - 1.3514) < 0.01, "梯形速度峰值");
  // 2) squash & stretch 纵向限定
  assert.equal(squashStretch(0, 0), 1.25, "起飞拉长");
  assert.ok(Math.abs(squashStretch(0.5, 1.35) - 1.08) < 1e-9, "巡航纵向膨胀 8%");
  assert.ok(squashStretch(0.95, 1.35) < 1, "落地压扁");
  // 3) banking ±24° 一阶滞后 .12
  assert.ok(Math.abs(bankAngle(0, 40, 0) - 2.88) < 1e-9, "首帧 = 24*0.12");
  let angle = 0;
  for (let i = 0; i < 200; i++) angle = bankAngle(0, 40, angle);
  assert.ok(angle <= 24 && angle > 23.9, "收敛且封顶 24°");
  // 4) anticipation 90ms → -6° / 2.2px
  const ant = anticipationOffset(0, 0);
  assert.ok(Math.abs(ant.angle + 6) < 1e-9 && Math.abs(ant.back - 2.2) < 1e-9, "预备 -6°/2.2px");
  assert.deepEqual(anticipationOffset(0.5, 90), { angle: 0, back: 0 }, "过预备窗即归零");
  // 5) 速度尾迹 0→35px
  assert.equal(trailLength(0), 0);
  assert.equal(trailLength(2), 35);
  assert.equal(trailLength(1), 26);
  // 6) 节点接收反馈三态
  assert.equal(receiverFeedback("failed"), "shake");
  assert.equal(receiverFeedback("done"), "squash");
  assert.equal(receiverFeedback(""), "inhale");
  // 落地回弹 0.82 → 1.05 → 1.0 不复用飞行曲线
  assert.equal(landingRebound(0), 0.82);
  assert.ok(landingRebound(0.35) >= 1.049 && landingRebound(0.35) <= 1.05);
  assert.equal(landingRebound(1), 1);
});

// ---------------------------------------------------------- 振翅频率 1–2Hz
test("速度派生振翅频率落在 1–2Hz 且单调", () => {
  assert.ok(Math.abs(flapFrequency(0) - 0.97) < 0.01, "静止 0.97Hz");
  assert.ok(Math.abs(flapFrequency(1.35) - 1.35) < 0.01, "满速 1.35Hz");
  let prev = 0;
  for (let sp = 0; sp <= 1.35; sp += 0.05) {
    const f = flapFrequency(sp);
    assert.ok(f > prev, "频率单调 " + sp);
    assert.ok(f >= 0.95 && f <= 1.36, "频段 1–2Hz " + sp);
    prev = f;
  }
  assert.ok(flapFrequency(99) <= 1.36, "超过巡航不再加速");
});

// ---------------------------------------------------------- 队列排序与槽位
test("队列固定排序：下载中 > FloodWait > 重试 > 暂停 > 失败 > 完成", () => {
  const ordered = queueOrder(tasks());
  assert.deepEqual(ordered.map((t) => t.task_id), ["t1", "t2", "t3", "t4", "t5", "t6"]);
  assert.deepEqual(
    [queueRank(ordered[0]), queueRank(ordered[1]), queueRank(ordered[2]),
      queueRank(ordered[3]), queueRank(ordered[4]), queueRank(ordered[5])],
    [0, 1, 2, 3, 4, 5]);
  assert.deepEqual(queueOrder(null), [], "非数组不炸");
  const same = [{ task_id: "b", phase: "done" }, { task_id: "a", phase: "done" }];
  assert.deepEqual(queueOrder(same).map((t) => t.task_id), ["a", "b"], "同档按 id 数值序");
});

test("分片槽位：1–4 四槽，5–8 按实际，>8 折叠 8 槽 + ×N，单并发不出槽", () => {
  assert.equal(stageSlotModel(0, 0).hidden, true);
  assert.equal(stageSlotModel(1, 0).hidden, true, "单并发不分片");
  assert.deepEqual(stageSlotModel(2, 2), { hidden: false, slots: 4, lit: 2, overflow: 0, parallel: 2 });
  assert.deepEqual(stageSlotModel(4, 4), { hidden: false, slots: 4, lit: 4, overflow: 0, parallel: 4 });
  assert.deepEqual(stageSlotModel(6, 3), { hidden: false, slots: 6, lit: 3, overflow: 0, parallel: 6 });
  assert.deepEqual(stageSlotModel(9, 9), { hidden: false, slots: 8, lit: 8, overflow: 1, parallel: 9 });
  assert.deepEqual(stageSlotModel(8, 8), { hidden: false, slots: 8, lit: 8, overflow: 0, parallel: 8 });
});

test("FloodWait 倒计时从 phase_detail 解析，缺省回落 countdown", () => {
  assert.equal(parseFloodWaitSeconds({ phase_detail: "FloodWait 30s" }), 30);
  assert.equal(parseFloodWaitSeconds({ phase_detail: "限流 12 秒后重试" }), 12);
  assert.equal(parseFloodWaitSeconds({ status_text: "429 · 7s" }), 7);
  assert.equal(parseFloodWaitSeconds({ countdown: 45 }), 45);
  assert.equal(parseFloodWaitSeconds({}), 0);
  assert.equal(parseFloodWaitSeconds(null), 0);
});

test("速度文案三档与异常输入", () => {
  assert.equal(formatStageSpeed(0), "—");
  assert.equal(formatStageSpeed(512), "512 B/s");
  assert.equal(formatStageSpeed(751 * 1024), "751.0 KB/s");
  assert.equal(formatStageSpeed(5 * 1048576), "5.0 MB/s");
  assert.equal(formatStageSpeed(-1), "—");
  assert.equal(formatStageSpeed("x"), "—");
});

test("条目状态映射与未知兜底", () => {
  const want = {
    "下载中": "downloading", "复制中": "copying", "等待中": "waiting",
    "待下载": "waiting", "暂停": "paused", "已暂停": "paused", "完成": "done",
    "已存在": "exists", "跳过": "skipped", "已跳过": "skipped",
    "失败": "failed", "重试中": "retrying", "FloodWait": "floodwait",
  };
  for (const [k, v] of Object.entries(want)) assert.equal(stageItemState(k), v, k);
  assert.equal(stageItemState("未知"), "waiting");
  assert.equal(stageItemState(null), "waiting");
});

// ---------------------------------------------------------- 布局与采样
test("三档布局的 viewBox 与宿主宽高比一致（覆盖层百分比对位）", () => {
  const desktop = stageLayout({ concurrency: 4 });
  const compact = stageLayout({ compact: true, concurrency: 4 });
  const mobile = stageLayout({ mobile: true, concurrency: 4 });
  // 640–1023 的 tablet 档复用桌面几何，只把队列条换行到舱底。
  const tablet = stageLayout({ tablet: true, concurrency: 4 });
  assert.deepEqual(desktop.viewBox, [0, 0, 940, 380]);
  assert.deepEqual(compact.viewBox, [0, 0, 460, 470]);
  assert.deepEqual(mobile.viewBox, [0, 0, 420, 640]);
  assert.deepEqual(tablet.viewBox, [0, 0, 940, 380]);
  assert.equal(tablet.tablet, true);
  assert.equal(tablet.compact, false);
  assert.equal(tablet.mobile, false);
  assert.equal(desktop.tablet, false);
  assert.equal(compact.tablet, false);
  assert.equal(mobile.tablet, false);
  for (const box of [desktop, tablet, compact, mobile]) {
    assert.ok(box.nodes.src && box.nodes.acct && box.nodes.task && box.nodes.disk);
    assert.ok(!box.nodes.uploaded, "上传节点未绑定不得渲染");
  }
  assert.equal(stageLayout({ concurrency: 99 }).concurrency, 8, "并发夹 1–8");
  assert.equal(stageLayout({ concurrency: 0 }).concurrency, 1);
  assert.ok(stageLayout({ nas: true }).nodes.nas, "NAS 绑定后才出现");
});

test("曲线按 8px 预采样且端点贴合误差 <0.5px", () => {
  const d = stageLayout({}).wires.srcTask;
  const pts = sampleCubicPath(d, 8);
  assert.ok(pts.length >= 3, "采样点数量");
  // 8px/点：相邻采样点的弦长不超过一个采样步长（短线也不该退化成两三个点）
  for (let i = 1; i < pts.length; i++) {
    assert.ok(Math.hypot(pts[i].x - pts[i - 1].x, pts[i].y - pts[i - 1].y) <= 8.01,
      "采样步长应 ≤8px");
  }
  assert.ok(Math.abs(pts[0].x - 220) < 0.51 && Math.abs(pts[0].y - 140) < 0.51);
  const last = pts[pts.length - 1];
  assert.ok(Math.abs(last.x - 250) < 0.51 && Math.abs(last.y - 100) < 0.51);
  assert.ok(pts.every((p) => Number.isFinite(p.x) && Number.isFinite(p.y) && Number.isFinite(p.ang)));
  assert.deepEqual(sampleCubicPath("", 8), [], "非路径返回空");
});

// ---------------------------------------------------------- 舞台实例
test("空事件序列 2s 不产帧（rAF 调用次数 0）", () => {
  const { win, api } = stageFixture();
  api.update({});
  for (let t = 0; t <= 2000; t += 16) win.step(t);
  assert.equal(win.requests, 0, "无真实输入不得请求帧");
  assert.equal(api.busy(), false);
  api.destroy();
});

test("幂等与去重：同一快照连喂两次不重复渲染", () => {
  const { host, win, api } = stageFixture();
  api.update(scene());
  const list = host.querySelector(".xian-stage-task-list");
  assert.equal(list.hidden, false, "桌面多任务出队列条");
  assert.equal(list.children.length, 4, "3 条 + 另有 N 条");
  assert.equal(list.children[0].getAttribute("data-phase"), "downloading");
  assert.equal(list.children[1].getAttribute("data-phase"), "flood_wait");
  assert.equal(list.children[2].getAttribute("data-phase"), "fetching");
  assert.match(list.children[3].textContent, /另有 3 条/);
  const requests = win.requests;
  api.update(scene());
  api.update(scene());
  assert.equal(list.children.length, 4, "重复快照不得叠加条目");
  assert.equal(win.requests, requests, "已排帧时不得重复请求");
  api.destroy();
});

test("队列展开/收起就地切换主任务，不重排舞台", () => {
  let picked = null;
  const { host, api } = stageFixture({ onTaskSelect: (id) => { picked = id; } });
  api.update(scene());
  const list = host.querySelector(".xian-stage-task-list");
  const more = list.children[3];
  more.fire("click");
  assert.equal(list.children.length, 7, "展开 6 条 + 收起");
  assert.match(list.children[6].textContent, /收起/);
  const third = list.children[2];
  third.fire("click");
  assert.equal(picked, "t3", "点击队列条切主任务（不跳页）");
  list.children[6].fire("click");
  assert.equal(list.children.length, 4, "再次收起");
  api.destroy();
});

test("焦点任务消失时回落到队列首位，不显示破折号", () => {
  const { host, api } = stageFixture();
  api.update(scene({ focusId: "gone" }));
  const current = host.querySelector(".xian-stage-bay-current");
  assert.ok(current, "任务舱标题存在");
  assert.match(current.textContent, /甲/, "回落到排序首位");
  assert.ok(!/^焦点 —/.test(current.textContent));
  api.destroy();
});

test("飞行帧只走预采样表：tick 有位移，落地后自动停帧", () => {
  const { host, win, api } = stageFixture();
  api.update(scene({ phase: "downloading" }));
  assert.equal(win.requests, 1, "busy 才请求帧");
  win.step(0);
  const flier = host.querySelector(".xian-stage-flier");
  assert.match(flier.getAttribute("transform"), /translate\(/);
  assert.match(flier.getAttribute("transform"), /rotate\(/);
  assert.ok(win.pending() >= 1, "飞行中续帧");
  win.step(900);
  assert.equal(api.busy(), false, "k≥1 后释放 busy");
  assert.equal(win.pending(), 0, "落地后不再续帧");
  assert.equal(host.querySelector(".xian-stage-flight").getAttribute("data-flight"), "idle");
  api.destroy();
});

test("reduced-motion 与 pause 均不产帧", () => {
  const { win, api } = stageFixture({ reduced: true });
  api.update(scene());
  assert.equal(api.busy(), false, "reduced 不进入 busy");
  assert.equal(win.requests, 0);
  const normal = stageFixture();
  normal.api.update(scene());
  assert.equal(normal.win.requests, 1);
  normal.api.pause();
  assert.equal(normal.api.busy(), false);
  assert.equal(normal.win.pending(), 0, "pause 清空待执行帧");
  normal.api.destroy();
});

test("槽位行按真实并发亮灯，单并发隐藏", () => {
  const { host, api } = stageFixture();
  api.update(scene());
  const slots = host.querySelector(".xian-stage-slots");
  assert.equal(slots.hidden, false);
  const lit = slots.querySelectorAll(".xian-stage-slot");
  assert.equal(lit.length, 4, "并发 4 → 4 槽");
  assert.equal(lit.filter((s) => s.getAttribute("data-lit") === "on").length, 2, "亮 2");
  api.update(scene({ parallel: 1, counts: { active: 1 } }));
  assert.equal(host.querySelector(".xian-stage-slots").hidden, true, "单并发不出槽");
  api.destroy();
});

test("8 路并发单帧耗时 < 4ms（Node 侧近似）", () => {
  const { api } = stageFixture({ layout: { concurrency: 8 } });
  api.update(scene({ parallel: 8, counts: { active: 8, done: 30 } }));
  const t0 = process.hrtime.bigint();
  for (let i = 0; i < 600; i++) api.tick();
  const cost = Number(process.hrtime.bigint() - t0) / 1e6 / 600;
  assert.ok(cost < 4, "单帧均值应 <4ms，实际 " + cost.toFixed(3) + "ms");
  api.destroy();
});

test("保存位置显示剩余空间：读数格式化 + 磁盘节点子行/标题", () => {
  assert.equal(formatDiskFree(0), "");
  assert.equal(formatDiskFree(512 * 1024 * 1024), "512 MB");
  assert.equal(formatDiskFree(3.5 * 1024 ** 3), "3.5 GB");
  assert.equal(formatDiskFree(128 * 1024 ** 3), "128 GB");
  assert.equal(formatDiskFree(2 * 1024 ** 4), "2.0 TB");
  const { host, api } = stageFixture();
  api.update(Object.assign(scene(), { disk: { free: 128 * 1024 ** 3, total: 512 * 1024 ** 3 } }));
  const chips = host.querySelectorAll(".xian-stage-node-chip");
  const disk = chips.find((c) => c.getAttribute("data-node") === "disk");
  assert.ok(disk, "磁盘节点必须存在");
  const sub = disk.querySelector(".xian-stage-node-sub");
  assert.equal(sub.textContent, "剩余 128 GB");
  assert.equal(disk.getAttribute("title"), "本地磁盘 · 剩余 128 GB（共 512 GB）");
  api.destroy();
});
test("保存位置没读数时退回原本文案", () => {
  const { host, api } = stageFixture();
  api.update(scene());
  const chips = host.querySelectorAll(".xian-stage-node-chip");
  const disk = chips.find((c) => c.getAttribute("data-node") === "disk");
  const sub = disk.querySelector(".xian-stage-node-sub");
  assert.equal(sub.textContent, "保存位置");
  api.destroy();
});

// 单条视频落地：任务还在下载时，完成数每多一条就补一次飞向磁盘的弦；
// 首次进入/切任务不回放历史完成，任务整体完成仍走 done 计划。
test("单条视频完成补飞磁盘，历史完成不回放", () => {
  const { host, win, api } = stageFixture();
  const items = (bDone) => [
    { name: "a.mp4", status: "完成", pct: 100 },
    { name: "b.mp4", status: bDone ? "完成" : "下载中", pct: bDone ? 100 : 30 },
  ];
  const first = api.update(scene({ items: items(false) }));
  assert.doesNotMatch(first.flightKey, /^pulse:/, "首次进入不触发落地飞行");

  const second = api.update(scene({ items: items(true) }));
  assert.match(second.flightKey, /^pulse:/, "完成数增加 → 单条落地飞行");
  assert.equal(second.busy, true);
  const flightLayer = host.querySelectorAll(".xian-stage-flight")[0];
  assert.equal(flightLayer.getAttribute("data-flight"), "forward");

  win.step(0);                      // 第一帧：从磁盘线起点起飞
  win.step(900);                    // 超过 flightDuration（720ms）→ 落点停住
  assert.equal(flightLayer.getAttribute("data-flight"), "hold", "飞完停在磁盘");
  const flier = host.querySelectorAll(".xian-stage-flier")[0];
  const x = Number(/translate\(([0-9.]+)/.exec(flier.getAttribute("transform") || "")[1]);
  assert.ok(x > 600, "落点应在右侧本地磁盘节点，实际 x=" + x);
  api.destroy();
});
