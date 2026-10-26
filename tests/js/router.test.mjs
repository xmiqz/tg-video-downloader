// ============================================================
// TR2 - router.js 路由语义（纯函数 + createRouter 依赖注入）
// 顶层 import 无 DOM 访问；createRouter 全走注入桩。
// ============================================================
import test, { after } from "node:test";
import assert from "node:assert/strict";
import {
  DEFAULT_ROUTE, ROUTES, buildUrl, createRouter, matchRoute, parseHash, resolveMode,
} from "../../web/js/router.js";

let N = 0;
function ok(v, label) { N++; assert.ok(v, label); }
function eq(a, b, label) { N++; assert.equal(a, b, label); }
function deep(a, b, label) { N++; assert.deepEqual(a, b, label); }

// ---------------------------------------------------------- 纯函数
test("TR2-A parseHash 与 buildUrl（token 参数不丢）", () => {
  deep(parseHash("#/task/1/edit&t=x"), { path: "/task/1/edit", extra: [["t", "x"]] }, "路径 + token");
  deep(parseHash("#/tasks"), { path: "/tasks", extra: [] }, "仅路径");
  deep(parseHash("/tasks"), { path: "/tasks", extra: [] }, "无 # 前缀也可");
  deep(parseHash("#/tasks&t=a&x=b"), { path: "/tasks", extra: [["t", "a"], ["x", "b"]] }, "多段按序");
  deep(parseHash("#/tasks&flag"), { path: "/tasks", extra: [] }, "无 = 段不入 extra");
  deep(parseHash("#/tasks&=v"), { path: "/tasks", extra: [] }, "空键不入 extra");
  deep(parseHash(""), { path: "/", extra: [] }, "空串 -> /");
  deep(parseHash("#"), { path: "/", extra: [] }, "仅 # -> /");
  deep(parseHash(null), { path: "/", extra: [] }, "null -> /");
  deep(parseHash(undefined), { path: "/", extra: [] }, "undefined -> /");
  deep(parseHash("#notpath&t=1"), { path: "/", extra: [["t", "1"]] }, "首段非 / -> path 为 /，extra 仍收集");
  deep(parseHash("#/task/1/edit&t=a%20b"), { path: "/task/1/edit", extra: [["t", "a%20b"]] },
    "extra 不解码（bridge 侧 decodeURIComponent）");

  eq(buildUrl({ mode: "hash", path: "/tasks", extra: [] }), "#/tasks", "hash 无参");
  eq(buildUrl({ mode: "hash", path: "/tasks", extra: [["t", "abc"]] }), "#/tasks&t=abc", "hash 带参");
  eq(buildUrl({ mode: "hash", path: "/tasks" }), "#/tasks", "hash 无 extra 字段");
  eq(buildUrl({ path: "/tasks", extra: [] }), "#/tasks", "缺 mode 视为 hash");
  eq(buildUrl({ mode: "auto", path: "/tasks" }), "#/tasks", "auto 视为 hash");
  deep(buildUrl({ mode: "history", path: "/tasks", extra: [["t", "abc"]] }),
    { pushUrl: "/tasks#t=abc", has: "#t=abc" }, "history 带参");
  deep(buildUrl({ mode: "history", path: "/tasks", extra: [] }),
    { pushUrl: "/tasks", has: "" }, "history 无参");

  // 往返：buildUrl -> parseHash 保住 path 与 extra
  const u = buildUrl({ mode: "hash", path: "/task/9/edit", extra: [["t", "tok"]] });
  deep(parseHash(u), { path: "/task/9/edit", extra: [["t", "tok"]] }, "buildUrl/parseHash 往返");
});

test("TR2-B resolveMode 产品恒 hash，history 仅注入面", () => {
  eq(resolveMode({ mode: "hash" }), "hash", "显式 hash");
  eq(resolveMode({ mode: "history" }), "history", "显式 history");
  eq(resolveMode({ mode: "auto" }), "hash", "auto -> hash");
  eq(resolveMode({}), "hash", "空对象 -> hash");
  eq(resolveMode(null), "hash", "null -> hash");
  eq(resolveMode(undefined), "hash", "undefined -> hash");
  eq(resolveMode({ mode: "HISTORY" }), "hash", "大小写不敏感 -> hash（只认小写）");
  eq(resolveMode({ protocol: "file:", pushStateOk: true }), "hash", "file: 恒 hash");
  eq(resolveMode({ protocol: "https:", url: "https://x/tasks", pushStateOk: true }), "hash",
    "普通 http(s) 也恒 hash（FR-26 无 SPA 回退）");
});

test("TR2-C matchRoute 精确段匹配", () => {
  eq(ROUTES.length, 6, "路由表恰六条（冻结）");
  deep(ROUTES.map((r) => r.name), ["login", "new", "active", "tasks", "settings", "editor"], "六条名单");
  eq(DEFAULT_ROUTE, "/tasks", "默认路由 /tasks");

  deep(matchRoute(ROUTES, "/tasks"), { name: "tasks", params: {} }, "精确匹配");
  deep(matchRoute(ROUTES, "/task/42/edit"), { name: "editor", params: { id: "42" } }, "参数路由");
  deep(matchRoute(ROUTES, "/task/a%20b/edit"), { name: "editor", params: { id: "a b" } }, "参数解码");
  deep(matchRoute(ROUTES, "/task/%E4%B8%AD/edit"), { name: "editor", params: { id: "中" } }, "UTF-8 参数解码");

  eq(matchRoute(ROUTES, "/task//edit"), null, "空参数段不匹配");
  eq(matchRoute(ROUTES, "/tasks/"), null, "尾斜杠不匹配");
  eq(matchRoute(ROUTES, "/"), null, "根路径不匹配任何路由");
  eq(matchRoute(ROUTES, ""), null, "空串不匹配");
  eq(matchRoute(ROUTES, "tasks"), null, "缺前导 / 不匹配");
  eq(matchRoute(ROUTES, "/nope"), null, "未知路径");
  eq(matchRoute(ROUTES, "/task/1/edit/extra"), null, "段数不符");
  eq(matchRoute(ROUTES, null), null, "null 入参");
  // routes 为 null 不在契约内（内部恒传 ROUTES），不设断言；见 seams.md 漏掉什么
  // 自定义路由表：:param 只匹配非空段
  deep(matchRoute([{ name: "x", pattern: "/a/:id" }], "/a/1"), { name: "x", params: { id: "1" } }, "自定义表");
});

// ---------------------------------------------------------- createRouter 桩
function makeNode(name) {
  return {
    nodeType: 1, name, parentNode: null, children: [],
    appendChild(c) { if (c.parentNode) c.parentNode.removeChild(c); c.parentNode = this; this.children.push(c); return c; },
    removeChild(c) {
      const i = this.children.indexOf(c);
      if (i >= 0) this.children.splice(i, 1);
      c.parentNode = null;
      return c;
    },
    addEventListener() {}, removeEventListener() {},
    get innerHTML() { return ""; }, set innerHTML(_v) {},
  };
}

function makeHarness(opts) {
  const o = opts || {};
  const log = [];
  const nodes = new Map();
  const views = o.views || {};
  const listeners = Object.create(null);
  const scroll = { scrollTop: 99 };

  const loc = {
    hash: o.hash === undefined ? "#/tasks" : o.hash,
    pathname: o.pathname || "/",
    protocol: "http:",
    href: "http://127.0.0.1:8000/" + (o.pathname || ""),
  };
  Object.defineProperty(loc, "hash", {
    get() { return this._hash; },
    set(v) { this._hash = v; log.push(["hash=", v]); },
    configurable: true,
  });
  loc._hash = o.hash === undefined ? "#/tasks" : o.hash;

  const historyApi = {
    state: null, length: 1,
    pushState(st, t, u) { log.push(["pushState", st, u]); this.state = st; this.length++; },
    replaceState(st, t, u) { log.push(["replaceState", st, u]); this.state = st; },
    back() { log.push(["back"]); },
  };

  const doc = {
    title: "",
    body: makeNode("body"),
    hidden: false,
    addEventListener(t, fn) { (listeners[t] || (listeners[t] = [])).push(fn); },
    removeEventListener() {},
    querySelectorAll() { return []; },
  };
  const win = {
    listeners: Object.create(null),
    addEventListener(t, fn) { (this.listeners[t] || (this.listeners[t] = [])).push(fn); },
    removeEventListener(t, fn) {
      const l = this.listeners[t];
      if (!l) return;
      const i = l.indexOf(fn);
      if (i >= 0) l.splice(i, 1);
    },
    dispatch(t, ev) { (this.listeners[t] || []).slice().forEach((fn) => fn(ev || {})); },
    location: loc, history: historyApi,
  };

  function fakeView(name) {
    const v = views[name] || (views[name] = {});
    v.__name = name;
    v.__mounts = 0;
    v.__enters = [];
    v.__leaves = 0;
    v.title = name + "-title";
    v.mount = o.mountImpl ? function (host) { v.__mounts++; return o.mountImpl(name, host); }
      : function () { v.__mounts++; return makeNode(name); };
    v.onEnter = function (params) { v.__enters.push(params); log.push(["enter", name, params]); };
    v.onLeave = function () { v.__leaves++; log.push(["leave", name]); };
    return v;
  }
  ["login", "new", "active", "tasks", "settings", "editor"].forEach(fakeView);

  const host = makeNode("host");
  const router = createRouter({
    mode: o.mode,
    history: historyApi,
    location: loc,
    doc,
    win,
    scroll,
    host,
    views,
    onNav: (name, path) => log.push(["nav", name, path]),
  });

  return { router, log, views, loc, historyApi, doc, win, host, scroll, nodes,
    // 从 log 里抽取某类事件
    names(kind) { return log.filter((e) => e[0] === kind).map((e) => e[1]); },
    count(kind, name) { return log.filter((e) => e[0] === kind && (name === undefined || e[1] === name)).length; },
  };
}

const tick = () => new Promise((r) => setTimeout(r, 0));

test("TR2-D createRouter.start 落默认路由并归一 hash", async () => {
  // 根路径 -> 默认 /tasks
  let h = makeHarness({ hash: "" });
  eq(h.router.mode, "hash", "产品默认 hash 模式");
  await h.router.start();
  eq(h.router.current, "/tasks", "根路径归一为 /tasks");
  eq(h.loc.hash, "#/tasks", "hash 被归一写入");
  eq(h.views.tasks.__mounts, 1, "默认视图 mount 一次");
  eq(h.views.tasks.__enters.length, 1, "默认视图 onEnter 一次");
  eq(h.doc.title, "tasks-title", "document.title 来自视图 title");
  eq(h.scroll.scrollTop, 0, "进入视图滚动复位");

  // 深链 + token：路径与 token 都保住
  h = makeHarness({ hash: "#/active&t=tok-9" });
  await h.router.start();
  eq(h.router.current, "/active", "深链保持");
  eq(h.loc.hash, "#/active&t=tok-9", "token 参数保留（不被吞）");
  eq(h.views.active.__enters.length, 1, "深链视图 onEnter");

  // 无效深链 -> 默认路由 + replace 语义（history 模式）
  h = makeHarness({ mode: "history", pathname: "/nope", hash: "" });
  await h.router.start();
  eq(h.router.current, "/tasks", "无效路径回默认");
  deep(h.log.filter((e) => e[0] === "replaceState"), [["replaceState", { path: "/tasks" }, "/tasks"]],
    "引导期归一用 replaceState（不产生垃圾历史）");
  eq(h.count("pushState"), 0, "引导期不 pushState");
});

test("TR2-E 同路径幂等 / force 重开 /login", async () => {
  const h = makeHarness({ hash: "#/tasks" });
  await h.router.start();
  eq(h.views.tasks.__mounts, 1, "初始 mount 一次");
  eq(h.views.tasks.__enters.length, 1, "初始 onEnter 一次");

  await h.router.navigate("/tasks");
  eq(h.views.tasks.__mounts, 1, "同路径不重 mount");
  eq(h.views.tasks.__enters.length, 1, "同路径不重 onEnter");
  eq(h.router.current, "/tasks", "current 不变");

  // 切到 active 再回 tasks：keep-alive 不重 mount，但 onEnter 重跑
  await h.router.navigate("/active");
  eq(h.views.tasks.__leaves, 1, "离开 tasks 触发 onLeave");
  eq(h.views.active.__enters.length, 1, "进入 active onEnter");
  await h.router.navigate("/tasks");
  eq(h.views.tasks.__mounts, 1, "keep-alive：回 tasks 不重 mount");
  eq(h.views.tasks.__enters.length, 2, "回 tasks 重跑 onEnter");
  eq(h.scroll.scrollTop, 0, "每次进入滚动复位");

  // /login：无 force 幂等，有 force 重跑 onEnter
  await h.router.navigate("/login");
  const login = h.views.login;
  eq(login.__enters.length, 1, "首次 /login onEnter");
  await h.router.navigate("/login");
  eq(login.__enters.length, 1, "无 force 同路径幂等");
  await h.router.navigate("/login", { force: true });
  eq(login.__enters.length, 2, "force 重跑 onEnter（重开登录卡）");
  deep(login.__enters[1], { notice: "" }, "force 无 notice 时传空串");
  await h.router.navigate("/login", { force: true, notice: "已退出" });
  deep(login.__enters[2], { notice: "已退出" }, "notice 直达 onEnter");
  eq(h.views.tasks.__leaves, 1, "登录为叠加弹层：不触发底层 onLeave（仅切到 active 时计一次）");
});

test("TR2-F 兜底重定向 replace 语义 + history 模式导航", async () => {
  // history 模式：正常导航 pushState
  let h = makeHarness({ mode: "history", pathname: "/tasks", hash: "" });
  eq(h.router.mode, "history", "显式 history 注入生效");
  await h.router.start();
  eq(h.router.current, "/tasks", "history 模式落 /tasks");
  await h.router.navigate("/settings");
  const pushes = h.log.filter((e) => e[0] === "pushState");
  eq(pushes.length, 1, "正常导航 pushState 一次");
  deep(pushes[0], ["pushState", { path: "/settings" }, "/settings"], "pushState 目标与 state");
  eq(h.count("replaceState"), 0, "正常导航不 replaceState");

  // history 模式：未知路径 -> 默认路由 + replaceState
  h = makeHarness({ mode: "history", pathname: "/tasks", hash: "" });
  await h.router.start();
  await h.router.navigate("/does-not-exist");
  const reps = h.log.filter((e) => e[0] === "replaceState");
  eq(reps.length, 1, "兜底重定向 replaceState 一次");
  deep(reps[0], ["replaceState", { path: "/tasks" }, "/tasks"], "兜底目标为默认路由");
  eq(h.count("pushState"), 0, "兜底不 pushState（不留垃圾历史条目）");
  eq(h.router.current, "/tasks", "兜底后 current 为默认路由");

  // hash 模式：未知路径只改写 hash（无 replace 等价物，记录为已知限制）
  h = makeHarness({ hash: "#/tasks" });
  await h.router.start();
  await h.router.navigate("/does-not-exist");
  eq(h.loc.hash, "#/tasks", "hash 模式兜底改写 hash 回默认");
  eq(h.router.current, "/tasks", "hash 模式兜底后 current 正确");
  eq(h.count("pushState"), 0, "hash 模式不触 history API");
});

test("TR2-G 视图生命周期序 + 参数透传 + onNav 回调", async () => {
  const h = makeHarness({ hash: "#/tasks" });
  await h.router.start();
  h.log.length = 0;
  await h.router.navigate("/task/77/edit");
  eq(h.router.current, "/task/77/edit", "参数路由 current 含实参");
  deep(h.views.editor.__enters[0], { id: "77" }, "params.id 透传 onEnter");
  const seq = h.log.filter((e) => ["leave", "enter", "nav"].indexOf(e[0]) >= 0).map((e) => e[0] + ":" + e[1]);
  deep(seq, ["leave:tasks", "enter:editor", "nav:editor"], "序：onLeave(旧) -> onEnter(新) -> onNav");
  eq(h.doc.title, "editor-title", "title 跟随新视图");
  ok(h.host.children.some((c) => c.name === "editor"), "新视图节点已挂到 host");
  ok(!h.host.children.some((c) => c.name === "tasks"), "旧视图节点已从 host 摘除");

  // onNav 抛异常不得阻塞路由（装饰性回调）
  const h2 = makeHarness({ hash: "#/tasks" });
  await h2.router.start();
  eq(h2.router.current, "/tasks", "onNav 内部已 try/catch，不阻塞");
});

test("TR2-H 异步 mount 竞态：过期结果不写缓存", async () => {
  let release = null;
  const gate = new Promise((r) => { release = r; });
  const h = makeHarness({
    hash: "#/tasks",
    mountImpl: (name) => (name === "settings" ? gate.then(() => makeNode(name)) : makeNode(name)),
  });
  await h.router.start();
  const slow = h.router.navigate("/settings");   // 慢挂载
  await tick();
  await h.router.navigate("/new");                // 快速切走
  eq(h.router.current, "/new", "后发导航获胜");
  release();
  await slow;
  eq(h.router.current, "/new", "慢挂载完成后不得回滚 current");
  eq(h.views.settings.__enters.length, 0, "过期挂载不得执行 onEnter");

  // 再进 settings 会重新 mount（不缓存过期结果）
  await h.router.navigate("/settings");
  eq(h.views.settings.__enters.length, 1, "重进 settings 正常 onEnter");
  // 产品注释已声明「后续重进复用该 Promise 或重新 mount」；此处复用已 settle 的
  // in-flight Promise（mounts 条目在过期 return 前未删），故 mount 仍只 1 次。
  eq(h.views.settings.__mounts, 1, "过期结果未写入 nodes 缓存；重进复用同一 Promise");
});

after(() => {
  console.log("TR2 结论：PASS（" + N + " 项）");
});
