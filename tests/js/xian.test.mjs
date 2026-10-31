// T65 弦 XIAN：四项基线 / 振翅投影高比 / 频率 / 越界预设 / 三层封顶 / 降级。
// 纯几何与注入式假文档，node 侧可全量断言（不引 DOM 测试库）。
import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

import {
  XIAN_BANDS, canonicalPoints, measureXianGeometry, projectedHeightRatio,
  xianInvariantReport,
} from "../../web/js/motion/xian-invariants.js";
import {
  FLAP_MAX, XIAN_GEOMETRY, XIAN_STATES, createXian, flapFrequency,
  flapPeriod, isXianState, mapPhaseToXian, mountXian, nextXianState,
  paintWings, roundedPath, xianIconGeometry, xianVisual, xianStateFor,
} from "../../web/js/motion/xian.js";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const CSS = path.join(ROOT, "web", "css");

function inBand(v, band) {
  return v >= band[0] && v <= band[1];
}

// 最小假文档：只实现 createXian 用到的节点能力（无布局、无选择器）。
function fakeDoc() {
  function node(tag) {
    const n = {
      tagName: tag, attrs: {}, children: [], parent: null,
      _classes: [],
      setAttribute(k, v) { this.attrs[k] = String(v); },
      removeAttribute(k) { delete this.attrs[k]; },
      getAttribute(k) { return this.attrs[k] == null ? null : this.attrs[k]; },
      appendChild(c) { c.parent = this; this.children.push(c); return c; },
      remove() {
        if (!this.parent) return;
        const i = this.parent.children.indexOf(this);
        if (i >= 0) this.parent.children.splice(i, 1);
        this.parent = null;
      },
      classList: {
        add(c) { if (n._classes.indexOf(c) < 0) n._classes.push(c); },
        contains(c) { return n._classes.indexOf(c) >= 0; },
      },
    };
    return n;
  }
  return { createElementNS(ns, tag) { return node(tag); } };
}

// ---------- 四项基线（AC-22a） ----------

test("canonical XIAN sits inside all four baseline bands", () => {
  const m = measureXianGeometry(XIAN_GEOMETRY.canon, XIAN_GEOMETRY.radius);
  assert.ok(inBand(m.noseAng, XIAN_BANDS.noseAng), "机头张角 " + m.noseAng);
  assert.ok(inBand(m.aspect, XIAN_BANDS.aspect), "翼展比 " + m.aspect);
  assert.ok(inBand(m.cg, XIAN_BANDS.cg), "重心 " + m.cg);
  assert.ok(inBand(m.radius, XIAN_BANDS.radius), "圆角占比 " + m.radius);
  // 设计基线：63.4° / 1.122 / 0.620 / 0.171（张角容差 1.5°，凸包相邻点口径差异）
  assert.ok(Math.abs(m.noseAng - 63.4) < 1.5, "基线张角应约 63.4°，实际 " + m.noseAng);
  assert.ok(Math.abs(m.aspect - 1.122) < 0.06, "基线翼展比应约 1.122，实际 " + m.aspect);
  assert.ok(Math.abs(m.cg - 0.620) < 0.02, "基线重心应约 0.620，实际 " + m.cg);
  assert.ok(Math.abs(m.radius - 0.171) < 0.01, "基线圆角占比应约 0.171，实际 " + m.radius);
  assert.deepEqual(xianInvariantReport(XIAN_GEOMETRY.canon, XIAN_GEOMETRY.radius).bad, []);
});

test("flapping rotates around the leading edge, never collapses the silhouette", () => {
  const six = projectedHeightRatio(FLAP_MAX);
  assert.ok(six >= 0.942 && six <= 1.047, "6° 投影高比应 0.942–1.047：" + six);
  assert.ok(inBand(six, XIAN_BANDS.projectedHeight));
  const negSix = projectedHeightRatio(-FLAP_MAX);
  assert.ok(negSix >= 0.942 && negSix <= 1.047, "-6° 投影高比应 0.942–1.047：" + negSix);
  const eight = projectedHeightRatio(FLAP_MAX + 2);
  assert.ok(eight > XIAN_BANDS.projectedHeight[1], "8° 必须越界：" + eight);
  assert.ok(projectedHeightRatio(0) <= 1.0);
});

// ---------- 频率（AC-23b） ----------

test("flap period stays in the readable 1–2Hz band and rises with speed", () => {
  assert.equal(flapPeriod(1.35), 0.8 / 1.08);
  assert.ok(Math.abs(flapFrequency(0) - 0.97) < 0.01, "speed 0 → 0.97Hz");
  assert.ok(Math.abs(flapFrequency(0.5) - 1.11) < 0.02, "speed 0.5 → 1.11Hz");
  assert.ok(Math.abs(flapFrequency(1.0) - 1.25) < 0.02, "speed 1.0 → 1.25Hz");
  assert.ok(Math.abs(flapFrequency(1.35) - 1.35) < 0.01, "speed 1.35 → 1.35Hz");
  assert.ok(flapFrequency(99) <= 1.35, "超过巡航上限不得更快");
  let prev = 0;
  for (let sp = 0; sp <= 1.35; sp += 0.05) {
    const f = flapFrequency(sp);
    assert.ok(f > prev, "频率必须随速度单调：" + sp);
    prev = f;
  }
});

test("tick converts milliseconds to seconds (no 1000x frequency bug)", () => {
  const doc = fakeDoc();
  const api = createXian(null, { doc, state: "downloading", speed: 1, reduced: false });
  // 以 0.7407s 周期喂 1 秒 → 相位只前进约 1.35 个周期，翼面角度受限 ≤6°
  let t = 0;
  for (let i = 0; i < 60; i++) { t += 16; api.tick(t, 1.35); }
  assert.ok(Math.abs(api.body.flapAngle) <= FLAP_MAX + 1e-9, "振幅不得超过 FLAP_MAX");
});

// ---------- 越界预设必须报红 ----------

const PRESETS = {
  overround: {
    points: { nose: [56, -18], wing: [-44, -20], crown: [-10, -48], keel: [-24, 44] },
    radius: 34,
    bad: "radius",
  },
  fan: {
    points: { nose: [56, -18], wing: [-44, -20], crown: [-60, -40], keel: [-60, 40] },
    radius: 15.7,
    bad: "noseAng",
  },
  noseheavy: {
    points: { nose: [56, -18], wing: [-44, -20], crown: [26, -46], keel: [30, 46] },
    radius: 15,
    bad: "cg",
  },
};

test("out-of-band presets report red (the safety band is not decorative)", () => {
  for (const [name, preset] of Object.entries(PRESETS)) {
    const rep = xianInvariantReport(preset.points, preset.radius);
    assert.equal(rep.ok, false, name + " 应越界");
    assert.ok(rep.bad.indexOf(preset.bad) >= 0, name + " 应报 " + preset.bad + "，实际 " + rep.bad);
  }
  // 合法呼吸/落地形仍在带内
  for (const points of [
    { nose: [56, -18.7], wing: [-44, -20.8], crown: [-10, -49.9], keel: [-24, 45.8] },
    { nose: [56, -12], wing: [-44, -14], crown: [-10, -40], keel: [-24, 54] },
  ]) {
    assert.equal(xianInvariantReport(points, 17).ok, true, "合法姿态不得报红");
  }
});

// ---------- 三层封顶（AC-22b） ----------

test("an instance draws exactly three body layers and no inner strokes", () => {
  const doc = fakeDoc();
  const api = createXian(null, { doc, state: "downloading", reduced: false });
  const body = api.body;
  assert.equal(body.children.length, 3, "绘制层必须恰 3 层");
  assert.deepEqual(body.children.map((c) => c.attrs.class), ["x-w2", "x-w1", "x-edge"]);
  const tags = [];
  (function walk(node) { tags.push(node.attrs.class || ""); node.children.forEach(walk); })(api.el);
  assert.ok(tags.indexOf("x-fold") < 0, "不得有内部折线描边");
  assert.ok(tags.indexOf("x-sheen") < 0, "不得有独立高光层");
  assert.equal(body.children[2].children.length, 0, "亮边层不得再挂子路径");
});

test("gradient ids are unique per instance", () => {
  const doc = fakeDoc();
  const a = createXian(null, { doc });
  const b = createXian(null, { doc });
  const idA = a.el.children[0].children[0].attrs.id;
  const idB = b.el.children[0].children[0].attrs.id;
  assert.notEqual(idA, idB);
});

test("shared leading edge is the only geometry input", () => {
  assert.deepEqual(XIAN_GEOMETRY.sharedEdge, ["nose", "wing"]);
  assert.deepEqual(XIAN_GEOMETRY.upperWing, ["nose", "wing", "crown"]);
  assert.deepEqual(XIAN_GEOMETRY.lowerWing, ["nose", "wing", "keel"]);
  const d1 = roundedPath([XIAN_GEOMETRY.canon.nose, XIAN_GEOMETRY.canon.wing,
    XIAN_GEOMETRY.canon.crown], XIAN_GEOMETRY.radius);
  assert.match(d1, /^M/);
  assert.match(d1, /Z$/);
  const body = { points: XIAN_GEOMETRY.canon, radius: XIAN_GEOMETRY.radius };
  body._w1 = { setAttribute() {} };
  body._w2 = { setAttribute() {} };
  body._ed = { setAttribute() {} };
  assert.equal(paintWings(body, 8), true);
  assert.equal(body.flapAngle, FLAP_MAX, "越界角度必须被 clamp 到 FLAP_MAX");
});

// ---------- 九态与映射 ----------

test("nine states, unknown degrades to idle, illegal transitions fall back", () => {
  assert.equal(XIAN_STATES.length, 9);
  for (const s of XIAN_STATES) {
    assert.equal(isXianState(s), true);
    assert.equal(xianVisual(s).state, s);
  }
  assert.equal(isXianState("paused"), false);
  assert.equal(xianVisual("paused").state, "idle");
  assert.equal(nextXianState("idle", "scanning"), "scanning");
  assert.equal(nextXianState("uploaded", "downloading"), "idle", "非法边降级 idle");
  assert.equal(nextXianState("idle", "nope"), "idle");
});

test("dl_phase maps to XIAN states, concurrency folds into sharding", () => {
  const want = {
    preparing: "scanning", disk_check: "scanning", reconciling: "scanning",
    fetching: "scanning", downloading: "downloading", refreshing: "retrying",
    flood_wait: "floodwait", done: "done", aborted: "idle", error: "failed",
  };
  for (const [phase, state] of Object.entries(want)) {
    assert.equal(mapPhaseToXian(phase), state, phase);
  }
  assert.equal(mapPhaseToXian("downloading", { concurrency: 4 }), "sharding");
  assert.equal(mapPhaseToXian("downloading", { retrying: true }), "retrying");
  assert.equal(mapPhaseToXian("downloading", { failed: true }), "failed");
  assert.equal(mapPhaseToXian("unknown-phase"), "idle", "未知阶段不得猜");
});

test("setState accepts live phases directly and paints on idle/floodwait", () => {
  const doc = fakeDoc();
  const api = createXian(null, { doc, state: "idle", autoTick: false, reduced: true });
  assert.equal(api.setState("downloading"), "downloading");
  assert.equal(api.el.attrs["data-xian-state"], "downloading");
  assert.equal(api.setState("bogus"), "idle");
  assert.equal(api.el.attrs["data-xian-state"], "idle");
  assert.equal(api.body.flapAngle, 0);
});

test("reduced-motion freezes the wings and exposes no movement", () => {
  const doc = fakeDoc();
  const api = createXian(null, { doc, state: "downloading", reduced: true });
  assert.equal(api.reduced, true);
  assert.equal(api.el.attrs["data-reduced"], "on");
  assert.equal(api.tick(1000, 1.35), false, "reduced 不得推进相位");
  assert.equal(api.body.flapAngle, 0, "reduced 下翼面必须归零");
  api.cancel();
  assert.equal(api.tick(2000, 1.35), false);
});

test("mountXian degrades to null instead of throwing", () => {
  assert.equal(mountXian(null, {}), null);
  assert.equal(mountXian({}, {}), null);
});

test("small icon geometry drops chord and glass below 24px", () => {
  assert.equal(xianIconGeometry(16).simple, true);
  assert.equal(xianIconGeometry(16).chord, false);
  assert.equal(xianIconGeometry(24).simple, true);
  assert.equal(xianIconGeometry(32).simple, false);
  assert.equal(xianIconGeometry(32).glass, true);
});

// ---------- 多任务快照聚合（active_states → 单态） ----------

test("xianStateFor prioritises what is actually happening", () => {
  assert.equal(xianStateFor([], ""), "idle");
  assert.equal(xianStateFor([{ phase: "flood_wait" },
    { phase: "downloading", counts: { active: 2 } }]), "floodwait",
  "限流压过一切进行中状态");
  assert.equal(xianStateFor([{ phase: "refreshing" },
    { phase: "downloading", counts: { active: 1 } }]), "retrying");
  assert.equal(xianStateFor([{ phase: "downloading", counts: { active: 2 } }]), "sharding");
  assert.equal(xianStateFor([{ phase: "downloading", counts: { active: 1 } }]), "downloading");
  assert.equal(xianStateFor([{ phase: "fetching" }]), "scanning");
  assert.equal(xianStateFor([{ phase: "error" }]), "failed");
  assert.equal(xianStateFor([{ phase: "done", counts: { done: 3 } },
    { phase: "aborted" }]), "done");
  assert.equal(xianStateFor([{ phase: "" }]), "idle", "未知阶段降级 idle");
});

test("xianStateFor does not let a stale failure mask live work", () => {
  assert.equal(xianStateFor([{ phase: "downloading", counts: { active: 1, fail: 2 } }]),
    "downloading");
  assert.equal(xianStateFor([{ phase: "preparing", counts: { fail: 1 } }]), "scanning");
  assert.equal(xianStateFor([], "flood_wait"), "floodwait", "空快照 + 限流提示仍报 floodwait");
  assert.equal(xianStateFor([{ phase: "fetching" }], "flood_wait"), "floodwait");
});

test("done/uploaded never keep an unexplained flap loop", () => {
  const doc = fakeDoc();
  let scheduled = 0;
  const queue = [];
  const win = {
    matchMedia: function () { return { matches: false }; },
    requestAnimationFrame: function (fn) { scheduled++; queue.push(fn); return scheduled; },
    cancelAnimationFrame: function () {},
    step: function (now) { queue.splice(0).forEach(function (fn) { fn(now); }); },
  };
  const host = { children: [], appendChild: function (c) { this.children.push(c); } };
  const api = createXian(host, { doc: doc, win: win, state: "downloading" });
  assert.equal(scheduled, 1);
  api.setState("done");
  win.step(16);
  win.step(32);
  assert.equal(scheduled, 1, "完成态不得续帧（一次性落地回弹由 CSS 播完释放）");
  api.cancel();
});

// ---------- CSS 纪律 ----------

test("xian.css uses tokens only and zeroes motion on both reduced paths", async () => {
  const css = (await readFile(path.join(CSS, "views", "xian.css"), "utf8"))
    .replace(/\/\*[\s\S]*?\*\//g, "");
  const bare = css.match(/\b\d+(?:\.\d+)?(?:ms|s)\b/g) || [];
  assert.deepEqual(bare, [], "xian.css 不得裸写时长：" + bare.join(","));
  assert.ok(css.indexOf("cubic-bezier(") < 0, "不得裸写曲线");
  assert.match(css, /var\(--dur-xian-idle\)/, "待机漂移必须走 --dur-xian-idle");
  assert.match(css, /@media \(prefers-reduced-motion: reduce\)/);
  assert.match(css, /prefers-reduced-motion[\s\S]*\.xian-svg\s*\{\s*animation:\s*none/,
    "reduced-motion 必须停用待机漂移");
  assert.match(css, /#sh-root\[data-v22-rm="on"\]\s*\.xian-svg\s*\{\s*animation:\s*none/,
    "壳级 reduced-motion 钩子必须同步归零");
});
