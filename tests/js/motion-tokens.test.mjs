// T64 动效框架与令牌：CSS 裸时长审计 + 节拍纯函数 + 视图变形包裹。
//
// 红线条（任务书 §2 T64 验收）：
//   1) CSS 中除 tokens.css 外无裸 ms/s 时长与裸 cubic-bezier；
//   2) reduced-motion 双路归零（令牌路 + 组件/视图自有媒体块）；
//   3) 数字滚动 620ms 四段缓出、事件帧只改目标值（不堆动画对象）。
import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

import {
  COUNT_MS, COUNT_STEPS, easeOutQuart, rollPlan, prefersReduced, markStill,
  createRoller,
} from "../../web/js/motion/calm.js";
import { canViewTransition, runViewTransition } from "../../web/js/router.js";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const CSS = path.join(ROOT, "web", "css");

// 本波（T64）拥有的样式文件；旧壳切片（shell/taskedit 等）不在治理范围
const OWNED = [
  path.join(CSS, "ui.css"),
  path.join(CSS, "views", "active.css"),
  path.join(CSS, "views", "stage.css"),
];

function stripComments(css) {
  return css.replace(/\/\*[\s\S]*?\*\//g, "");
}

async function ownedRules(file) {
  return stripComments(await readFile(file, "utf8"));
}

test("owned css has no bare ms/s durations (tokens only)", async () => {
  const bad = [];
  for (const file of OWNED) {
    const css = await ownedRules(file);
    const re = /\b\d+(?:\.\d+)?(?:ms|s)\b/g;
    let m;
    while ((m = re.exec(css)) !== null) {
      const line = css.slice(0, m.index).split("\n").length;
      bad.push(`${path.relative(ROOT, file)}:${line} ${m[0]}`);
    }
  }
  assert.deepEqual(bad, [], "裸时长必须换成 --dur-* 令牌：" + bad.join("; "));
});

test("owned css has no bare cubic-bezier (eased via --ease-*)", async () => {
  const bad = [];
  for (const file of OWNED) {
    const css = await ownedRules(file);
    if (css.includes("cubic-bezier(")) bad.push(path.relative(ROOT, file));
  }
  assert.deepEqual(bad, [], "裸 cubic-bezier 必须换成 --ease-* 令牌：" + bad.join("; "));
});

test("tokens.css declares every T64 motion token", async () => {
  const css = stripComments(await readFile(path.join(CSS, "tokens.css"), "utf8"));
  const want = [
    ["--dur-count", "620ms"], ["--dur-flash", "1200ms"],
    ["--dur-toast-out", "240ms"], ["--dur-progress", "500ms"],
    ["--dur-indet", "1150ms"], ["--dur-ambient", "3000ms"],
    ["--dur-spin", "800ms"], ["--dur-pulse", "1600ms"],
    ["--dur-pulse-fast", "1200ms"], ["--dur-pulse-quick", "900ms"],
    ["--dur-pulse-slow", "2000ms"],
  ];
  for (const [name, value] of want) {
    const re = new RegExp("\\" + name + ":\\s*" + value + "[ ;]");
    assert.match(css, re, `${name} 应为 ${value}`);
  }
  assert.match(css, /--ease-spring-li:\s*linear\(/, "弹簧高亮走 linear() 取样表");
});

test("tokens.css zeroes every motion token under reduced-motion", async () => {
  const css = stripComments(await readFile(path.join(CSS, "tokens.css"), "utf8"));
  const rm = css.slice(css.indexOf("prefers-reduced-motion: reduce"));
  assert.ok(rm.length > 0, "缺少 reduced-motion 媒体块");
  for (const name of ["--dur-count", "--dur-flash", "--dur-toast-out",
    "--dur-progress", "--dur-indet", "--dur-ambient", "--dur-spin",
    "--dur-pulse", "--dur-pulse-fast", "--dur-pulse-quick", "--dur-pulse-slow"]) {
    assert.match(rm, new RegExp("\\" + name + ":\\s*0ms"), `${name} 未归零`);
  }
  assert.match(rm, /--ease-spring-li:\s*linear(?:;|[^;\n]*;)/, "弹簧曲线未降级");
});

test("segmented thumb rides the spring token and stops under .still", async () => {
  const css = await ownedRules(path.join(CSS, "ui.css"));
  assert.match(css, /\.u-seg-thumb\s*\{/, "缺少滑行拇指");
  assert.match(css, /transition:\s*transform var\(--dur-md\) var\(--ease-spring-li\)/,
    "拇指过渡必须走 --dur-md + --ease-spring-li");
  assert.match(css, /\.still\s+\.u-seg-thumb\s*\{[^}]*transition:\s*none/,
    ".still（reduced-motion）下滑行必须停用");
});

test("shell slots carry view-transition-name (views morph instead of pop)", async () => {
  const css = await ownedRules(path.join(CSS, "ui.css"));
  for (const name of ["sh-side", "sh-bottombar", "sh-viewhost"]) {
    assert.match(css, new RegExp("\\." + name + "\\s*\\{[^}]*view-transition-name"),
      `.${name} 缺少 view-transition-name`);
  }
  assert.match(css, /\.u-view-enter\s*\{[^}]*backwards/,
    "无 VT 引擎的直切淡入必须 fill: backwards（播完不留动画对象）");
});

test("active view flashes completion via duration token, reduced-motion gated", async () => {
  const css = await ownedRules(path.join(CSS, "views", "active.css"));
  assert.match(css, /\.ac-cfill\.is-flash\s*\{[^}]*var\(--dur-flash\)/,
    "完成闪烁必须走 --dur-flash");
  const rm = css.slice(css.indexOf("prefers-reduced-motion"));
  assert.match(rm, /\.ac-cfill\.is-flash/, "reduced-motion 未停用完成闪烁");
});

// ---------- 节拍纯函数 ----------

test("easeOutQuart clamps at both ends and is monotonic", () => {
  assert.equal(easeOutQuart(-1), 0);
  assert.equal(easeOutQuart(0), 0);
  assert.equal(easeOutQuart(1), 1);
  assert.equal(easeOutQuart(2), 1);
  let prev = -1;
  for (let i = 0; i <= 20; i++) {
    const v = easeOutQuart(i / 20);
    assert.ok(v >= prev, `非单调：t=${i / 20}`);
    prev = v;
  }
});

test("rollPlan yields steps+1 samples with exact endpoints", () => {
  const p = rollPlan(0, 100);
  assert.equal(p.length, COUNT_STEPS + 1);
  assert.equal(p[0], 0);
  assert.equal(p[p.length - 1], 100);
  for (let i = 1; i < p.length; i++) assert.ok(p[i] >= p[i - 1], "取样必须单调");
  assert.equal(rollPlan(-5, 7, 2).length, 3);
});

test("prefersReduced accepts boolean or matchMedia stub", () => {
  assert.equal(prefersReduced(true), true);
  assert.equal(prefersReduced(false), false);
  assert.equal(prefersReduced({ matches: true }), true);
  assert.equal(prefersReduced({ matches: false }), false);
  assert.equal(prefersReduced(null), false);
});

test("markStill toggles the .still class on the shell root", () => {
  const calls = [];
  const root = { classList: { toggle: (c, on) => calls.push([c, on]) } };
  assert.equal(markStill(root, true), true);
  assert.deepEqual(calls[0], ["still", true]);
  assert.equal(markStill(root, false), false);
  assert.deepEqual(calls[1], ["still", false]);
});

// ---------- createRoller：注入时钟逐帧断言 ----------

function fakeClock() {
  const q = { cb: null, id: 0 };
  let t = 0;
  const frames = [];
  const roller = createRoller({
    from: 0, dur: COUNT_MS, media: false,
    now: () => t,
    raf: (cb) => { q.cb = cb; return ++q.id; },
    cancel: () => { q.cb = null; },
    update: (v, done) => frames.push([v, done]),
  });
  return {
    roller, frames,
    at(ts) { t = ts; },
    tick(ts) { const cb = q.cb; q.cb = null; if (cb) { t = ts; cb(ts); } },
    pending: () => q.cb !== null,
  };
}

test("createRoller rolls to target and finishes exactly", () => {
  const c = fakeClock();
  assert.equal(c.roller.set(100), 100);
  assert.ok(c.pending());
  c.tick(0);
  c.tick(310);                 // 半程：四段缓出已收尾近终点
  const mid = c.frames[c.frames.length - 1][0];
  assert.ok(mid > 50 && mid < 100, "半程应在中后段：" + mid);
  c.tick(COUNT_MS);
  assert.equal(c.roller.value(), 100);
  assert.equal(c.frames[c.frames.length - 1][1], true, "末帧必须 done");
  assert.equal(c.pending(), false, "末帧后不得再排帧");
});

test("createRoller mid-flight set only retargets (no restart)", () => {
  const c = fakeClock();
  c.roller.set(100);
  c.tick(0);
  c.tick(310);
  assert.equal(c.roller.set(200), 200);
  assert.ok(c.pending(), "应沿用同一条计时链");
  c.tick(COUNT_MS);
  assert.equal(c.roller.value(), 200, "终值取最后目标");
});

test("createRoller jumps to final value under reduced-motion", () => {
  const frames = [];
  const r = createRoller({
    from: 0, media: { matches: true },
    update: (v, done) => frames.push([v, done]),
    raf: () => { throw new Error("reduced 不应排帧"); },
  });
  assert.equal(r.set(42), 42);
  assert.deepEqual(frames, [[42, true]]);
  assert.equal(r.busy(), false);
});

// ---------- 视图变形（router seam） ----------

test("canViewTransition detects the engine", () => {
  assert.equal(canViewTransition(null), false);
  assert.equal(canViewTransition({}), false);
  assert.equal(canViewTransition({ startViewTransition() {} }), true);
});

test("runViewTransition falls back to direct swap without the engine", () => {
  let n = 0;
  const ret = runViewTransition({}, () => { n++; });
  assert.equal(ret, null);
  assert.equal(n, 1);
});

test("runViewTransition wraps the swap when the engine exists", () => {
  let n = 0;
  const doc = {
    startViewTransition(cb) { cb(); return { ready: Promise.resolve() }; },
  };
  const ret = runViewTransition(doc, () => { n++; });
  assert.ok(ret, "应返回引擎对象");
  assert.equal(n, 1, "DOM 替换必须发生恰一次");
});

test("runViewTransition still swaps when the engine throws", () => {
  let n = 0;
  const doc = {
    startViewTransition() { throw new Error("boom"); },
  };
  const ret = runViewTransition(doc, () => { n++; });
  assert.equal(ret, null);
  assert.equal(n, 1, "引擎异常不得丢切换");
});
