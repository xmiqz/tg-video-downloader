// ============================================================
// TR1 - bridge.js 传输桥回归（RPC 代理 / 事件白名单 / WS 鉴权与退避）
//
// bridge.js 是无 import/export 的经典脚本，顶层立即执行 startBridge()。
// 这里用 node:vm 执行源码原文，注入 location/fetch/WebSocket/window 桩。
// 只读源码，不改产品文件。
// ============================================================
import test, { after } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const SRC = readFileSync(new URL("../../web/js/bridge.js", import.meta.url), "utf8");
const ORIGIN = "http://127.0.0.1:8000";

let N = 0;
function ok(v, label) { N++; assert.ok(v, label); }
function eq(a, b, label) { N++; assert.equal(a, b, label); }
function norm(v) {
  if (v === undefined) return undefined;
  return JSON.parse(JSON.stringify(v));
}
function deep(a, b, label) { N++; assert.deepEqual(norm(a), norm(b), label); }
async function msgOf(p) {
  try { await p; return null; } catch (e) { return String((e && e.message) || e); }
}

function makeHarness(opts) {
  const o = opts || {};
  const calls = [];
  const sockets = [];
  const emitted = [];
  const logs = { debug: [], info: [], error: [], warn: [] };
  const timers = [];
  let seq = 0;

  const location = {
    hash: o.hash || "",
    origin: ORIGIN,
    protocol: "http:",
    host: "127.0.0.1:8000",
    href: ORIGIN + "/",
    replaced: [],
    replace(u) { this.replaced.push(u); },
  };

  const win = {
    listeners: Object.create(null),
    pywebview: null,
    _setBackdrop: null,
    backdropCalls: [],
    addEventListener(t, fn) { (this.listeners[t] || (this.listeners[t] = [])).push(fn); },
    removeEventListener(t, fn) {
      const l = this.listeners[t];
      if (!l) return;
      const i = l.indexOf(fn);
      if (i >= 0) l.splice(i, 1);
    },
    dispatchEvent(ev) {
      emitted.push(ev);
      const l = this.listeners[ev.type];
      if (l) l.slice().forEach((fn) => fn(ev));
      return true;
    },
  };

  class FakeEvent { constructor(type) { this.type = type; } }
  class FakeCustomEvent extends FakeEvent {
    constructor(type, init) { super(type); this.detail = init ? init.detail : undefined; }
  }

  class FakeWS {
    constructor(url) {
      this.url = url;
      this.sent = [];
      this.listeners = Object.create(null);
      this.closed = false;
      sockets.push(this);
    }
    addEventListener(t, fn) { (this.listeners[t] || (this.listeners[t] = [])).push(fn); }
    send(d) { this.sent.push(d); }
    // 真实浏览器 close() 会异步派发 close 事件；桩同步派发以便断言重连。
    close() {
      if (this.closed) return;
      this.closed = true;
      this.fire("close", { code: 1005, wasClean: true });
    }
    fire(t, ev) { (this.listeners[t] || []).slice().forEach((fn) => fn(ev)); }
  }

  const ctx = {
    location,
    console: {
      debug: (...a) => logs.debug.push(a),
      info: (...a) => logs.info.push(a),
      error: (...a) => logs.error.push(a),
      warn: (...a) => logs.warn.push(a),
      log: () => {},
    },
    fetch: (url, init) => {
      calls.push({ url, init });
      if (o.fetchImpl) return o.fetchImpl(url, init);
      return Promise.resolve({ ok: true, status: 200, json: async () => ({ ok: true }) });
    },
    WebSocket: FakeWS,
    Event: FakeEvent,
    CustomEvent: FakeCustomEvent,
    setTimeout: (fn, ms) => { const id = ++seq; timers.push({ id, fn, ms }); return id; },
    clearTimeout: (id) => {
      const i = timers.findIndex((t) => t.id === id);
      if (i >= 0) timers.splice(i, 1);
    },
  };
  ctx.window = win;
  ctx.globalThis = ctx;
  win.window = win;

  vm.createContext(ctx);
  vm.runInContext(SRC, ctx, { filename: "web/js/bridge.js" });

  const api = {
    ctx, win, location, calls, sockets, emitted, logs, timers,
    ws() { return sockets[sockets.length - 1]; },
    flush() {
      const t = timers.shift();
      if (!t) return null;
      t.fn();
      return t.ms;
    },
    peek() { return timers.length ? timers[0].ms : null; },
    async settle() {
      for (let i = 0; i < 8; i++) await Promise.resolve();
    },
    /** 走完鉴权：open -> send 首帧 -> auth_ok */
    auth(idx) {
      const s = sockets[idx === undefined ? sockets.length - 1 : idx];
      s.fire("open", {});
      s.fire("message", { data: JSON.stringify({ type: "auth_ok" }) });
      return s;
    },
    push(obj, idx) {
      const s = sockets[idx === undefined ? sockets.length - 1 : idx];
      s.fire("message", { data: JSON.stringify(obj) });
      return s;
    },
    tg(name) { return emitted.filter((e) => e.type === "tg:" + name); },
  };
  return api;
}

function rpcBody(call) { return JSON.parse(call.init.body); }

// ---------------------------------------------------------------- A. RPC
test("TR1-A RPC 代理：方法名/头/体/返回值", async () => {
  const h = makeHarness({ hash: "#/tasks&t=tok-1" });
  await h.settle();
  ok(h.win.pywebview && typeof h.win.pywebview.api === "object", "window.pywebview.api 已就位");
  ok(h.emitted.some((e) => e.type === "pywebviewready"), "已派发 pywebviewready");

  const r = await h.win.pywebview.api.ping(1, "x", { a: true });
  deep(r, { ok: true }, "200 体即方法原返回值");
  eq(h.calls.length, 1, "一次属性调用 = 一次 fetch");
  const c = h.calls[0];
  eq(c.url, ORIGIN + "/rpc", "RPC 端点为同源 /rpc");
  eq(c.init.method, "POST", "方法 POST");
  eq(c.init.headers["Content-Type"], "application/json", "Content-Type");
  eq(c.init.headers.Authorization, "Bearer tok-1", "桌面 Bearer 头");
  eq(c.init.credentials, "same-origin", "带同源 Cookie");
  deep(rpcBody(c), { method: "ping", params: [1, "x", { a: true }] }, "体为 {method,params}");

  ok(typeof h.win.pywebview.api.anyRandomName === "function", "任意属性名 -> RPC 函数");
  ok(typeof h.win.pywebview.api.then === "function", "thenable 不变量：then 也是 RPC 函数");
  eq(h.win.pywebview.api[Symbol.iterator], undefined, "symbol 属性 -> undefined");

  // W4 hard 教训的可执行证据：await 桥对象本身会调名为 then 的 RPC，
  // 且该调用不接收 resolve 参数 -> 永远悬挂。这里用超时赛跑把「悬挂」本身钉成不变量。
  let settled = false;
  const raced = (async () => {
    await h.win.pywebview.api;
    settled = true;
  })();
  const TIMEOUT = Symbol("timeout");
  const winner = await Promise.race([
    raced.then(() => "settled"),
    new Promise((res) => setTimeout(() => res(TIMEOUT), 50)),
  ]);
  eq(winner, TIMEOUT, "await 桥对象本身必须悬挂（W4 hard：不得 resolve 桥本身）");
  eq(settled, false, "悬挂期间不得 settle");
  eq(h.calls.length, 2, "await 桥对象本身触发一次额外 RPC");
  eq(rpcBody(h.calls[1]).method, "then", "该 RPC 方法名恰为 then");
});

test("TR1-B RPC 错误语义：error 字段 / 不可解析体 / 网络异常 / 401", async () => {
  // 非 2xx + 体含 error
  let h = makeHarness({
    hash: "#/tasks&t=t1",
    fetchImpl: async () => ({ ok: false, status: 400, json: async () => ({ ok: false, error: "参数不合法" }) }),
  });
  await h.settle();
  eq(await msgOf(h.win.pywebview.api.bad()), "参数不合法", "非 2xx 取体 error 作为 Error.message");

  // 非 2xx + 体不可解析
  h = makeHarness({
    hash: "#/tasks&t=t1",
    fetchImpl: async () => ({ ok: false, status: 503, json: async () => { throw new Error("not json"); } }),
  });
  await h.settle();
  eq(await msgOf(h.win.pywebview.api.bad()), "HTTP 503", "体不可解析 -> HTTP <status>");

  // 网络层异常必须 reject，不得悬挂
  h = makeHarness({
    hash: "#/tasks&t=t1",
    fetchImpl: async () => { throw new Error("offline"); },
  });
  await h.settle();
  eq(await msgOf(h.win.pywebview.api.bad()), "网络错误：offline", "fetch 抛异常 -> 网络错误：<msg>");

  // 无 token + 401 -> 跳登录
  h = makeHarness({
    fetchImpl: async (url) => {
      if (url === "/auth/status") return { ok: true, status: 200, json: async () => ({ authenticated: true }) };
      return { ok: false, status: 401, json: async () => ({ ok: false, error: "未登录" }) };
    },
  });
  await h.settle();
  eq(h.location.replaced.length, 0, "已认证会话不跳转");
  eq(await msgOf(h.win.pywebview.api.whoami()), "未登录", "401 仍以 error 文本 reject");
  deep(h.location.replaced, ["/login"], "无 token 且 401 -> location.replace(/login)");
});

test("TR1-C 事件白名单：五个放行名 + 未列入只 debug", async () => {
  const h = makeHarness({ hash: "#/tasks&t=t1" });
  await h.settle();
  h.auth();

  const payloads = {
    job_update: { job_id: "j1", kind: "download", state: "running" },
    search_items: { items: [{ src: "s", mid: 1 }] },
    dl_phase: { task_id: "t1", phase: "fetching", phase_detail: "抓取元数据" },
    acct_hint: { account_id: "main", reason: "flood" },
  };
  for (const name of Object.keys(payloads)) {
    h.push({ event: name, params: [payloads[name], "extra"] });
    const evs = h.tg(name);
    eq(evs.length, 1, "转发 " + name + " 为 tg:" + name);
    deep(evs[0].detail, payloads[name], "tg:" + name + " 的 detail = params[0]");
  }

  h.win._setBackdrop = (u) => h.win.backdropCalls.push(u);
  h.push({ event: "_setBackdrop", params: ["data:image/jpeg;base64,AA"] });
  deep(h.win.backdropCalls, ["data:image/jpeg;base64,AA"], "_setBackdrop 调 window._setBackdrop(params[0])");
  eq(h.tg("_setBackdrop").length, 0, "_setBackdrop 不转发为 CustomEvent");

  const before = h.emitted.length;
  h.push({ event: "evil_event", params: [{ x: 1 }] });
  eq(h.emitted.length, before, "白名单外事件不派发任何 CustomEvent");
  ok(h.logs.debug.some((a) => String(a[0]).indexOf("白名单外") >= 0), "白名单外事件只 console.debug");

  // 畸形包络不应抛
  h.push({ event: 123, params: [] });
  h.push({ nope: true });
  h.push({ event: "job_update" }); // params 缺失 -> 视为 []
  eq(h.tg("job_update").length, 2, "params 缺失按 [] 处理仍转发（detail undefined）");
});

// V2-D1 缺陷：EVENT_WHITELIST 是普通对象字面量，原型链上的键会被当成 handler。
test("TR1-D [缺陷 V2-D1] 未列入事件一律只 debug（含原型链键名）", {
  todo: "V2-D1: EVENT_WHITELIST 用对象字面量，msg.event=constructor/toString/valueOf 命中 Object.prototype 上的函数，绕过白名单且不打印 debug",
}, async () => {
  const h = makeHarness({ hash: "#/tasks&t=t1" });
  await h.settle();
  h.auth();
  for (const name of ["constructor", "toString", "valueOf", "hasOwnProperty"]) {
    h.logs.debug.length = 0;
    h.push({ event: name, params: [{ x: 1 }] });
    ok(h.logs.debug.length > 0, "事件名 " + name + " 属未列入，应只 console.debug");
  }
});

// ---------------------------------------------------------------- D. WS
test("TR1-E WS 鉴权首帧语义", async () => {
  const h = makeHarness({ hash: "#/tasks&t=ws-tok" });
  await h.settle();
  eq(h.sockets.length, 1, "boot 后立即建一条 WS");
  const s0 = h.sockets[0];
  eq(s0.url, "ws://127.0.0.1:8000/events", "WS 端点为同源 /events");

  s0.fire("open", {});
  deep(s0.sent.map((x) => JSON.parse(x)), [{ token: "ws-tok" }], "open 后首帧必须是 {token}");

  // 鉴权前收到非 auth_ok -> close，且不转发
  h.push({ event: "job_update", params: [{ job_id: "j" }] }, 0);
  eq(h.tg("job_update").length, 0, "鉴权前的推送不转发");
  ok(s0.closed, "鉴权前非 auth_ok -> close()");

  // close 触发退避重连 -> 新连接上再走完整鉴权
  h.flush();
  eq(h.sockets.length, 2, "close 后退避重连出新连接");
  // 鉴权后推送才转发
  const s1 = h.auth();
  eq(s1.sent.length, 1, "同一连接只发一次首帧");
  eq(s0.sent.length, 1, "旧连接仍只有首帧");
  h.push({ event: "job_update", params: [{ job_id: "j" }] });
  eq(h.tg("job_update").length, 1, "auth_ok 之后推送走白名单");

  // 鉴权阶段收到坏 JSON 不得抛
  h.push({ event: "job_update", params: [] });
  s1.fire("message", { data: "{not json" });
  eq(h.tg("job_update").length, 2, "坏 JSON 静默丢弃，不影响后续");
});

test("TR1-F WS 断线指数退避 500..10000 封顶 + 鉴权复位 + pagehide 停机", async () => {
  const h = makeHarness({ hash: "#/tasks&t=t1" });
  await h.settle();

  const waits = [];
  for (let i = 0; i < 7; i++) {
    h.ws().fire("close", { code: 1006 });
    const ms = h.peek();
    waits.push(ms);
    h.flush();
  }
  deep(waits, [500, 1000, 2000, 4000, 8000, 10000, 10000], "退避序列 500 起翻倍、10000 封顶");
  eq(h.sockets.length, 8, "每次退避都新建连接");

  // 鉴权成功后复位
  h.auth();
  h.ws().fire("close", { code: 1006 });
  eq(h.peek(), 500, "auth_ok 后退避复位到 500");
  h.flush();

  // pagehide 后不再重连
  h.win.dispatchEvent({ type: "pagehide" });
  const before = h.sockets.length;
  h.ws().fire("close", { code: 4401 });
  eq(h.timers.length, 0, "pagehide 后 close 不再排重连定时器");
  eq(h.sockets.length, before, "pagehide 后不再新建连接");
});

test("TR1-G startBridge 会话分流（无 token）", async () => {
  // 已认证 -> boot
  let h = makeHarness({
    fetchImpl: async (url) => {
      if (url === "/auth/status") return { ok: true, status: 200, json: async () => ({ authenticated: true }) };
      return { ok: true, status: 200, json: async () => ({ ok: true }) };
    },
  });
  await h.settle();
  eq(h.calls[0].url, "/auth/status", "无 token 先查会话");
  eq(h.calls[0].init.cache, "no-store", "会话查询禁缓存");
  ok(h.win.pywebview && h.win.pywebview.api, "已认证 -> boot");
  eq(h.calls[0].init.headers, undefined, "无 token 不发 Authorization");

  // 未登录 -> /login
  h = makeHarness({
    fetchImpl: async (url) => {
      if (url === "/auth/status") return { ok: true, status: 200, json: async () => ({ authenticated: false }) };
      return { ok: true, status: 200, json: async () => ({}) };
    },
  });
  await h.settle();
  deep(h.location.replaced, ["/login"], "未认证 -> location.replace(/login)");
  eq(h.win.pywebview, null, "未认证不 boot");

  // 首次安装 -> /setup
  h = makeHarness({
    fetchImpl: async (url) => {
      if (url === "/auth/status") return { ok: true, status: 200, json: async () => ({ setup_required: true }) };
      return { ok: true, status: 200, json: async () => ({}) };
    },
  });
  await h.settle();
  deep(h.location.replaced, ["/setup"], "setup_required -> location.replace(/setup)");
});

after(() => {
  console.log("TR1 结论：PASS（" + N + " 项；另含 1 项缺陷用例 V2-D1 见 TODO）");
});
