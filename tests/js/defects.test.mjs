// ============================================================
// V2 缺陷红灯清单（pinned defects）
//
// 本文件故意为红：每条断言写的是「契约应有的行为」，当前实现不满足。
// 产品代码未做任何修改（任务书 §3：测试暴露缺陷 -> 失败测试 + 上报，STOP）。
// 单跑：node --test tests/js/defects.test.mjs
// 默认 `npm test` 不含本文件，保证回归套件保持绿。
//
// 修复后本文件转绿 -> 请把对应用例移入常规套件并删除此处的 [DEFECT] 标记。
// ============================================================
import test, { after } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";
import * as A from "../../web/js/views/active.js";

const BRIDGE_SRC = readFileSync(new URL("../../web/js/bridge.js", import.meta.url), "utf8");

let N = 0;
function ok(v, label) { N++; assert.ok(v, label); }
function eq(a, b, label) { N++; assert.equal(a, b, label); }

// ---------------------------------------------------------------- bridge 桩
function bridgeHarness() {
  const debug = [];
  const emitted = [];
  const sockets = [];
  const listeners = Object.create(null);
  const win = {
    pywebview: null,
    addEventListener(t, fn) { (listeners[t] || (listeners[t] = [])).push(fn); },
    removeEventListener() {},
    dispatchEvent(ev) { emitted.push(ev); (listeners[ev.type] || []).slice().forEach((f) => f(ev)); return true; },
  };
  class E { constructor(t) { this.type = t; } }
  class CE extends E { constructor(t, i) { super(t); this.detail = i ? i.detail : undefined; } }
  class WS {
    constructor(u) { this.url = u; this.sent = []; this.listeners = Object.create(null); sockets.push(this); }
    addEventListener(t, f) { (this.listeners[t] || (this.listeners[t] = [])).push(f); }
    send(d) { this.sent.push(d); }
    close() {}
    fire(t, ev) { (this.listeners[t] || []).slice().forEach((f) => f(ev)); }
  }
  const ctx = {
    location: { hash: "#/tasks&t=t1", origin: "http://127.0.0.1:8000", protocol: "http:",
      host: "127.0.0.1:8000", href: "http://127.0.0.1:8000/", replace() {} },
    console: { debug: (...a) => debug.push(a), info() {}, error() {}, warn() {}, log() {} },
    fetch: async () => ({ ok: true, status: 200, json: async () => ({ ok: true }) }),
    WebSocket: WS, Event: E, CustomEvent: CE,
    setTimeout: () => 1, clearTimeout() {},
  };
  ctx.window = win;
  ctx.globalThis = ctx;
  win.window = win;
  vm.createContext(ctx);
  vm.runInContext(BRIDGE_SRC, ctx, { filename: "web/js/bridge.js" });
  const s = sockets[sockets.length - 1];
  s.fire("open", {});
  s.fire("message", { data: JSON.stringify({ type: "auth_ok" }) });
  return { win, debug, emitted, push: (o) => s.fire("message", { data: JSON.stringify(o) }) };
}

// ================================================================ D1
test("[DEFECT V2-D1] bridge：白名单外事件名不得命中 Object.prototype", () => {
  const h = bridgeHarness();
  const names = ["constructor", "toString", "valueOf", "hasOwnProperty", "__proto__"];
  for (const name of names) {
    h.debug.length = 0;
    const before = h.emitted.length;
    h.push({ event: name, params: [{ x: 1 }] });
    // 契约：未列入白名单只 console.debug，绝不执行
    ok(h.debug.length > 0, "事件名 " + name + " 未列入白名单，应只 console.debug（实际：被当成 handler 调用）");
    eq(h.emitted.length, before, "事件名 " + name + " 不得派发任何事件");
  }
});

// ================================================================ D2
test("[DEFECT V2-D2] active：bulkGroupKeys 遇到 Object.prototype 键名不得抛错", () => {
  for (const tid of ["__proto__", "constructor", "toString", "hasOwnProperty"]) {
    let out;
    let err = null;
    try { out = A.bulkGroupKeys([tid + "|||src|1"]); } catch (e) { err = e; }
    eq(err, null, "tid=" + tid + " 不应抛错（实际：" + (err && err.message) + "）");
    if (!err) ok(Array.isArray(out[tid]), "tid=" + tid + " 应得到数组分组");
  }
});

// ================================================================ D3
test("[DEFECT V2-D3] active：activeBadgeClass 未知态恒返回 wait", () => {
  for (const s of ["constructor", "toString", "valueOf", "__proto__"]) {
    eq(A.activeBadgeClass(s), "wait", "状态 " + s + " 未映射，应兜底 wait（实际：undefined）");
  }
});

// ================================================================ D4
test("[DEFECT V2-D4] active：pendReleaseRule 的 plain object 路径不得读原型链", () => {
  // 契约（源码注释）：byCk 支持 Map 与 plain object，两者语义必须一致
  for (const ck of ["__proto__", "constructor", "toString"]) {
    const viaMap = A.pendReleaseRule(ck + ":pause", new Map());
    const viaObj = A.pendReleaseRule(ck + ":pause", {});
    eq(viaObj, viaMap, "ck=" + ck + " plain object 与 Map 结果必须一致");
    eq(viaObj, true, "ck=" + ck + " 缺失项 -> pause 应可释放（实际：false，永久卡忙）");
  }
  // 更危险方向：constructor:resume 在 plain object 下被误判为可释放
  const objResume = A.pendReleaseRule("constructor:resume", {});
  const mapResume = A.pendReleaseRule("constructor:resume", new Map());
  eq(objResume, mapResume, "constructor:resume plain object 与 Map 结果必须一致");
});

after(() => {
  console.log("TR6 结论：FAIL（缺陷：4 项原型链命中红灯，详见各用例；断言计数 " + N + " 项）");
});