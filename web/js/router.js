// ============================================================
// router.js — T8 v2.2 新壳路由与响应式应用外壳（全版地基）
//
// 设计契约：evidence/T8/t8_contract.md（一次冻结，下游 T11–T16
// 只替换 web/js/views/* 占位与其 css 切片，零改本文件/壳样式/引导）。
// fix1（2026-10-15，spec 评审 hard 闭环）：产品默认路由模式恒 hash，
// history 模式实现仅保留为注入面（seam 兼容）；v22history/v22hashmode/
// v22shell/v22theme 测试钩子已随 T18 翻版清理。模式解析收在纯函数
// resolveMode（seam 可离线测）。
//
// 结构分三层（tdd-seams：纯逻辑可离线测，视觉归 TR-8.2 浏览器级）：
//   ① 契约纯函数：ROUTES / DEFAULT_ROUTE / parseHash / buildUrl / matchRoute
//   ② createRouter(deps)：依赖注入（history/location/doc/scroll/views），
//      History API 优先、hash 降级；两种模式深链/后退均可用。
//   ③ boot()：浏览器外壳挂载（侧栏/窄栏/底栏/抽屉/SVG 液态玻璃滤镜）。
//
// 折射技术路线（自写实现，风格借鉴 archisvaze/liquid-glass 的公开思路
// —MIT © Archis Vaze—；未拷贝其任何代码，全部为本文件原创）：
//   Chromium 的 backdrop-filter 不支持 SVG filter url()，故折射采用
//   「内容快照 + filter:url(feDisplacementMap)」方案——对滚动内容快照做
//   边缘位移，叠加在 backdrop 模糊材质之上，仅底栏胶囊一个滤镜面。
// 零构建、零依赖、零 CDN、零外部网络请求（滤镜贴图为 data: URI 内联）。
// ============================================================

import * as loginView from "./views/login.js";
import * as newView from "./views/new.js";
import * as activeView from "./views/active.js";
import * as tasksView from "./views/tasks.js";
import * as settingsView from "./views/settings.js";
import * as editorView from "./views/editor.js";

const views = {
  login: loginView,
  new: newView,
  active: activeView,
  tasks: tasksView,
  settings: settingsView,
  editor: editorView,
};

// ============ ① 契约纯函数（node 可直接测） ============

export const DEFAULT_ROUTE = "/tasks";
const DEFAULT_VIEW = "tasks";

// 路由表（冻结：恰六条，不增不减；参数路由 :id 只匹配非空段）
export const ROUTES = [
  { name: "login", pattern: "/login" },
  { name: "new", pattern: "/new" },
  { name: "active", pattern: "/active" },
  { name: "tasks", pattern: "/tasks" },
  { name: "settings", pattern: "/settings" },
  { name: "editor", pattern: "/task/:id/edit" },
];

// parseHash：'#/task/1/edit&t=x' → { path, extra:[[k,v],…] }
// 约定：hash 首段以 / 开头为路由 path；k=v 段（如 bridge 的 #t=token，
// 见 js/bridge.js readToken）原样保留，由 router 在读写时传递、不吞掉。
export function parseHash(hash) {
  const extra = [];
  let path = "/";
  if (typeof hash === "string" && hash.length > 1) {
    const body = hash.charAt(0) === "#" ? hash.slice(1) : hash;
    const segs = body.split("&");
    if (segs.length && segs[0].charAt(0) === "/") path = segs[0];
    for (let i = 0; i < segs.length; i++) {
      const eq = segs[i].indexOf("=");
      if (eq > 0) extra.push([segs[i].slice(0, eq), segs[i].slice(eq + 1)]);
    }
  }
  return { path: path, extra: extra };
}

// buildUrl：两种模式的统一 URL 构造。
// 参数段（t= 等）按序原样保留——保证 bridge token 在两种模式下存活。
export function buildUrl(opts) {
  const kv = opts.extra || [];
  const q = kv.map(function (p) { return p[0] + "=" + p[1]; }).join("&");
  if (opts.mode === "history") {
    return { pushUrl: opts.path + (q ? "#" + q : ""), has: q ? "#" + q : "" };
  }
  return "#" + opts.path + (q ? "&" + q : "");
}

// resolveMode：路由模式解析纯函数（fix1 R1，seam 离线可测）。
// 入参 spec = { mode, protocol, url, pushStateOk }：
//   mode        — "hash"|"history" 显式注入直返（seam S4/S5 注入面兼容）；
//   protocol    — location.protocol（"file:" 恒 hash，钩子不可覆盖）；
//   url / protocol / pushStateOk — 保留入参位（seam 兼容），钩子清理后不再参与判定。
// 组长裁决（2026-10-15，spec hard 闭环）：产品默认模式恒 hash——loopback、
// file:、普通 http 静态托管、任意 host 均默认 hash。依据：FR-4 规范入口
// 本就是 `http://127.0.0.1:<port>/#t=<token>`；FR-26 要求无 SPA 回退的普通
// http 静态托管可运行（pathname 路由刷新即 404 白页）；FR-27 禁止白屏；
// FR-3 原文允许 "History API / hash 兼容"。history 模式全套实现仅保留为
// 注入面（seam S4/S5）；v22history/v22hashmode 钩子已随 T18 翻版清理。
export function resolveMode(spec) {
  const s = spec || {};
  if (s.mode === "hash" || s.mode === "history") return s.mode;
  return "hash";
}

function safeDecode(s) {
  try { return decodeURIComponent(s); } catch (e) { return s; }
}

// matchRoute：路径段精确匹配；:param 只匹配非空段；返回 {name,params}|null。
export function matchRoute(routes, path) {
  if (typeof path !== "string" || path.charAt(0) !== "/") return null;
  const segs = path.split("/").slice(1);
  if (segs.length && segs[segs.length - 1] === "") return null; // 尾斜杠不匹配
  for (let ri = 0; ri < routes.length; ri++) {
    const pSegs = routes[ri].pattern.split("/").slice(1);
    if (pSegs.length !== segs.length) continue;
    const params = {};
    let hit = true;
    for (let i = 0; i < pSegs.length; i++) {
      const ps = pSegs[i];
      if (ps.charAt(0) === ":") {
        if (!segs[i]) { hit = false; break; }
        params[ps.slice(1)] = safeDecode(segs[i]);
      } else if (ps !== segs[i]) { hit = false; break; }
    }
    if (hit) return { name: routes[ri].name, params: params };
  }
  return null;
}

function resolve(routes, path) {
  const m = matchRoute(routes, path);
  if (m) return { name: m.name, params: m.params, path: path };
  // 根路径与未匹配（404）一律回默认路由；由调用方以 replace 语义写入，
  // 不产生垃圾历史条目。
  return { name: DEFAULT_VIEW, params: {}, path: DEFAULT_ROUTE };
}

// ============ ② createRouter：路由引擎（依赖注入，可在 node 测） ============
//
// 生命周期序（契约）：onLeave(旧) →（首进）mount(新) → onEnter(新, params)
//   → document.title = 新视图 title → 滚动容器复位 scrollTop = 0。
// keep-alive：每个视图首次进入时 mount 一次并缓存节点；再次进入仅重挂
//   + onEnter。视图自身的清理职责在 onLeave 中自管。
export function createRouter(deps) {
  const d = deps || {};
  const routes = d.routes || ROUTES;
  const viewSet = d.views || views;
  const historyApi = d.history;
  const location = d.location;
  const doc = d.doc;
  const scroll = d.scroll || { scrollTop: 0 };
  const host = d.host || null;
  const onNav = typeof d.onNav === "function" ? d.onNav : null;
  // 事件宿主：真实浏览器 = window（popstate/hashchange 均只派发到 window）；
  // 注入环境无 win 时退化为 doc（离线桩把它当事件总线）。
  const win = d.win || (typeof window !== "undefined" ? window : doc);

  // 模式解析（fix1 R1）：产品默认恒 hash；history 仅注入面显式指定。规则
  // 收在纯函数 resolveMode（seam S7 单测）。
  let mode = resolveMode({
    mode: d.mode,
    protocol: location && location.protocol ? String(location.protocol) : "",
    url: location ? String(location.href ||
      ((location.pathname || "/") + (location.search || "") + (location.hash || ""))) : "",
    pushStateOk: !!(historyApi && typeof historyApi.pushState === "function"),
  });

  const state = { current: "", currentView: null };
  const nodes = new Map(); // viewName -> mounted node（keep-alive 缓存）
  const mounts = new Map(); // viewName -> in-flight mount Promise（fix1 R2：异步防重入）
  let navToken = 0;

  function currentExtra() {
    return parseHash(location.hash).extra;
  }

  // —— 登录 = 叠加弹层（对齐 2.1 #login.login-mask：fixed 遮罩盖在当前
  //    版面上、底层视图保持挂载可见；/login 不参与视图切换，进入只叠卡、
  //    离开只收卡，不触发底层视图 onLeave/重入）——
  let loginOpen = false;

  async function renderLogin(params, path) {
    const lv = viewSet.login;
    if (!lv || typeof lv.mount !== "function") {
      throw new Error("[router] 视图模块缺失: login");
    }
    // 深链直进 #/login：先落默认视图（2.1 启动壳先可见），再叠卡
    if (!state.currentView) {
      const t0 = navToken;
      await render(DEFAULT_VIEW, {}, DEFAULT_ROUTE);
      if (navToken !== t0) return; // 期间有更新导航 → 放弃本次叠卡
    }
    const token = navToken;
    let node = nodes.get("login");
    if (!node) {
      let pending = mounts.get("login");
      if (!pending) {
        pending = Promise.resolve(lv.mount(null));
        mounts.set("login", pending);
      }
      const r = await pending;
      if (token !== navToken) return;
      node = r && r.nodeType ? r : null;
      nodes.set("login", node);
      mounts.delete("login");
    }
    // 弹层节点直挂 body（fixed 遮罩；视图宿主之外不占版面流）
    if (node && node.parentNode !== doc.body) doc.body.appendChild(node);
    loginOpen = true;
    if (typeof lv.onEnter === "function") lv.onEnter(params || {});
    if (typeof lv.title === "string" && lv.title) doc.title = lv.title;
    state.current = path;
    // 导航高亮保持底层视图（版面未切换，对齐 2.1 登录卡盖在主界面上）
    if (onNav) { try { onNav(state.currentView, path); } catch (e) { /* 装饰性回调不阻塞路由 */ } }
  }

  function closeLoginOverlay() {
    if (!loginOpen) return;
    loginOpen = false;
    const lv = viewSet.login;
    if (lv && typeof lv.onLeave === "function") lv.onLeave();
    const ln = nodes.get("login");
    if (ln && ln.parentNode) ln.parentNode.removeChild(ln);
  }

  async function render(name, params, path) {
    if (name === "login") return renderLogin(params, path);
    closeLoginOverlay();
    const token = ++navToken;
    const prev = state.currentView;
    if (prev && prev !== name) {
      const pv = viewSet[prev];
      if (pv && typeof pv.onLeave === "function") pv.onLeave();
      const pn = nodes.get(prev);
      if (pn && pn.parentNode) pn.parentNode.removeChild(pn);
    }
    const v = viewSet[name];
    if (!v || typeof v.mount !== "function") {
      throw new Error("[router] 视图模块缺失: " + name);
    }
    let node = nodes.get(name);
    if (!node) {
      // fix1 R2：慢异步 mount（T11+ 可能返回 Promise）期间同视图重入
      // （hashchange/快速导航）复用同一 in-flight Promise——每视图 mount
      // 至多并发一次；恢复时 navToken 已过期（已被切走）则丢弃结果且不写
      // 缓存（不污染），后续重进复用该 Promise 或重新 mount，视图侧无需关心。
      let pending = mounts.get(name);
      if (!pending) {
        pending = Promise.resolve(v.mount(host));
        mounts.set(name, pending);
      }
      const r = await pending;
      if (token !== navToken) return; // 并发导航竞态：放弃过期挂载（不缓存）
      node = r && r.nodeType ? r : null;
      nodes.set(name, node);
      mounts.delete(name);
    }
    if (host && node && node.parentNode !== host) host.appendChild(node);
    if (typeof v.onEnter === "function") v.onEnter(params || {});
    if (typeof v.title === "string" && v.title) doc.title = v.title;
    scroll.scrollTop = 0; // 滚动复位：每次进入视图均回到顶部（契约）
    state.current = path;
    state.currentView = name;
    if (onNav) { try { onNav(name, path); } catch (e) { /* 装饰性回调不阻塞路由 */ } }
  }

  function applyAddress(r, targetPath, replace) {
    const u = buildUrl({ mode: mode, path: r.path, extra: currentExtra() });
    if (mode === "history") {
      const st = { path: r.path };
      const isFallback = r.path !== targetPath; // 兜底/重定向：replace 语义
      if (replace || isFallback) historyApi.replaceState(st, "", u.pushUrl);
      else historyApi.pushState(st, "", u.pushUrl);
    } else {
      if (location.hash !== u) location.hash = u; // 触发 hashchange（已幂等防重）
    }
  }

  async function navigate(path, opts) {
    const r = resolve(routes, path);
    // 同路径幂等（force 除外：/login 重开需重跑 onEnter 收受控提示与连接复核）
    if (!(opts && opts.force) && r.path === path && path === state.current && state.currentView) return;
    applyAddress(r, path, !!(opts && opts.replace));
    // /login 携受控提示（2.1 showLoginCard(notice)）：opts.notice 直达 onEnter
    if (r.name === "login") {
      await renderLogin({ notice: (opts && opts.notice) || "" }, r.path);
      return;
    }
    await render(r.name, r.params, r.path);
  }

  function queueRender(path) {
    const t = ++navToken;
    Promise.resolve().then(async function () {
      if (t !== navToken) return;
      const r = resolve(routes, path);
      await render(r.name, r.params, r.path);
    });
  }

  function onPop(ev) {
    const st = ev && ev.state;
    const p = st && st.path ? st.path : location.pathname;
    queueRender(p);
  }
  function onHash() {
    const p = parseHash(location.hash);
    if (p.path === state.current) return; // navigate 已渲染（幂等防重）
    queueRender(p.path);
  }

  async function start() {
    if (mode === "history") win.addEventListener("popstate", onPop);
    else win.addEventListener("hashchange", onHash);
    let init;
    if (mode === "history") init = { path: location.pathname, extra: parseHash(location.hash).extra };
    else init = parseHash(location.hash);
    const r = resolve(routes, init.path);
    // 引导期归一：根路径/无效深链 → 默认路由；t= 等 token 参数保留；
    // 仅在内容确有差异时写入，避免多余历史/无谓 hashchange。
    const u = buildUrl({ mode: mode, path: r.path, extra: init.extra });
    if (mode === "history") {
      const cur0 = location.pathname;
      if (r.path !== cur0 || location.hash !== u.has) {
        historyApi.replaceState({ path: r.path }, "", u.pushUrl);
      }
    } else {
      if (location.hash !== u) location.hash = u;
    }
    await render(r.name, r.params, r.path);
  }

  return {
    get current() { return state.current; },
    mode: mode,
    navigate: navigate,
    start: start,
  };
}

// ============ ③ 外壳挂载（仅真实浏览器执行；node import 本文件时不触 DOM） ============

const NAV_ITEMS = [
  { name: "active", label: "下载中", ico: "⬇" },
  { name: "tasks", label: "任务库", ico: "◈" },
  { name: "new", label: "新建下载", ico: "＋" },
  { name: "settings", label: "设置", ico: "⚙" },
];

// 位移贴图（data: URI 内联 SVG，零网络请求）：
//   基底 rgb(128,128,128) = 零位移；左右边缘窄带 R 通道偏移 128±23
//   → feDisplacementMap scale 采样出内容向外/内挤压的折射观感；
//   G 恒 128（不做垂直位移），贴图 preserveAspectRatio=none 拉伸到胶囊 bbox。
function lensMapDataUri() {
  const svg =
    '<svg xmlns="http://www.w3.org/2000/svg" width="120" height="48">' +
    '<rect width="120" height="48" fill="rgb(128,128,128)"/>' +
    '<rect x="0" width="22" height="48">' +
    '<linearGradient id="a" x1="0" x2="1">' +
    '<stop offset="0" stop-color="rgb(82,128,128)"/>' +
    '<stop offset="1" stop-color="rgb(128,128,128)"/>' +
    "</linearGradient></rect>" +
    '<rect x="0" width="22" height="48" fill="url(#a)"/>' +
    '<rect x="98" width="22" height="48">' +
    '<linearGradient id="b" x1="0" x2="1">' +
    '<stop offset="0" stop-color="rgb(128,128,128)"/>' +
    '<stop offset="1" stop-color="rgb(174,128,128)"/>' +
    "</linearGradient></rect>" +
    '<rect x="98" width="22" height="48" fill="url(#b)"/>' +
    "</svg>";
  return "data:image/svg+xml," + encodeURIComponent(svg);
}

function navLinkHtml(name, label, ico) {
  return '<a class="sh-nav-item" data-nav="' + name + '" href="#" ' +
    'aria-label="' + label + '"><span class="sh-nav-ico" aria-hidden="true">' +
    ico + "</span><span class=\"sh-nav-label\">" + label + "</span></a>";
}

function buildShellDom() {
  const root = document.createElement("div");
  root.id = "sh-root";
  const sideNav = NAV_ITEMS.map(function (it) { return navLinkHtml(it.name, it.label, it.ico); }).join("");
  // 手机悬浮底栏五槽（对齐参考图形态）：菜单（开抽屉）+ 四页签（含中央 FAB 化“新建”）。
  // IA 承接：旧壳侧栏四页签 active/tasks/new/settings + 侧底 me-box（登录）→
  // 四槽页签 + ☰ 抽屉（含登录/账号区）；登录不进底栏（模态态路由，T11 实现）。
  const barItems =
    '<button class="sh-bar-menu" id="sh-burger" type="button" aria-label="打开导航菜单" ' +
    'aria-haspopup="true" aria-expanded="false"><span aria-hidden="true">☰</span></button>' +
    navLinkHtml("tasks", "任务库", "◈") +
    '<a class="sh-bar-fab" data-nav="new" href="#" aria-label="新建下载">' +
    '<span class="sh-nav-ico" aria-hidden="true">＋</span></a>' +
    navLinkHtml("active", "下载中", "⬇") +
    navLinkHtml("settings", "设置", "⚙");
  root.innerHTML =
    // 液态玻璃折射滤镜 defs：仅新壳 DOM 存在；旧壳（开关关）零出现。
    '<svg class="sh-svgdefs" width="0" height="0" aria-hidden="true" focusable="false">' +
    '<defs><filter id="t8-lens" x="-10%" y="-10%" width="120%" height="120%" ' +
    'color-interpolation-filters="sRGB">' +
    '<feImage href="' + lensMapDataUri() + '" x="0" y="0" width="100%" height="100%" ' +
    'preserveAspectRatio="none" result="map"/>' +
    '<feDisplacementMap in="SourceGraphic" in2="map" scale="46" ' +
    'xChannelSelector="R" yChannelSelector="G"/>' +
    "</filter></defs></svg>" +
    // 侧边栏（≥1024）/窄栏（640–1023）
    '<aside class="sh-side" aria-label="主导航">' +
    '<div class="sh-brand"><span class="sh-logo" aria-hidden="true">TG</span>' +
    '<span class="sh-brand-name">视频下载器</span></div>' +
    '<nav class="sh-side-nav">' + sideNav + "</nav>" +
    '<div class="sh-side-foot">' + navLinkHtml("login", "登录", "👤") + "</div>" +
    "</aside>" +
    // 抽屉（<640 汉堡展开）
    '<div class="sh-drawer-mask" id="sh-drawer-mask" hidden></div>' +
    '<aside class="sh-drawer" id="sh-drawer" aria-label="导航抽屉" aria-hidden="true">' +
    '<div class="sh-drawer-head"><span class="sh-logo" aria-hidden="true">TG</span>' +
    '<span class="sh-brand-name">视频下载器</span></div>' +
    '<nav class="sh-drawer-nav">' + sideNav + "</nav>" +
    '<div class="sh-drawer-foot">' + navLinkHtml("login", "登录", "👤") + "</div>" +
    "</aside>" +
    // 主内容（视图宿主）；guest-bar/boot-err 置顶（对齐 2.1 .main 首子：
    // 旧壳 index.html:117-119 guest-bar 在 #sheet 前、随内容滚动）
    '<main class="sh-main"><div class="sh-scroll" id="sh-scroll">' +
    '<div class="sh-guestbar" id="sh-guestbar" tabindex="0" hidden></div>' +
    '<div class="sh-booterr" id="sh-booterr" hidden></div>' +
    '<div class="sh-viewhost" id="sh-viewhost"></div>' +
    "</div></main>" +
    // 手机悬浮液态玻璃底栏（<640）
    '<nav class="sh-bottombar" aria-label="底部导航">' +
    '<div class="sh-lens" aria-hidden="true"><div class="sh-lens-content" id="sh-lens-content"></div></div>' +
    '<div class="sh-bar-mat" aria-hidden="true"></div>' +
    barItems +
    "</nav>";
  return root;
}

let booted = false;

export async function boot() {
  if (booted) return; // 幂等
  booted = true;
  const doc = document;
  const win = window;

  // 视图层样式按约定路径引导（shell/tokens 由 index.html head 静态引入）
  ["login", "new", "active", "tasks", "settings", "editor"].forEach(function (n) {
    const l = doc.createElement("link");
    l.rel = "stylesheet";
    l.href = "css/views/" + n + ".css";
    doc.head.appendChild(l);
  });
  // T10 组件库样式（t8_contract.md §7 R4-① 预授权的同型引导；随新壳
  // 引导装载）
  const uiCss = doc.createElement("link");
  uiCss.rel = "stylesheet";
  uiCss.href = "css/ui.css";
  doc.head.appendChild(uiCss);

  // —— 视觉底层三件套（对齐 2.1 旧壳 style.css:49-126）——
  // #sh-backdrop（液态玻璃背景图层）/ .sh-ambient（静态暗亮光斑）/
  // #sh-frost（霜层面）；样式在 shell.css §0b。层元素由本壳自建（index.html
  // 零改动，WC-4a 冻结保持绿）。
  const backdropEl = doc.createElement("div");
  backdropEl.id = "sh-backdrop";
  const ambientEl = doc.createElement("div");
  ambientEl.className = "sh-ambient";
  ambientEl.setAttribute("aria-hidden", "true");
  ambientEl.innerHTML = '<div class="sh-blob sh-b1"></div><div class="sh-blob sh-b2"></div>';
  const frostEl = doc.createElement("div");
  frostEl.id = "sh-frost";
  frostEl.setAttribute("aria-hidden", "true");
  doc.body.appendChild(backdropEl);
  doc.body.appendChild(ambientEl);
  doc.body.appendChild(frostEl);

  // bridge.js:85-92 _setBackdrop → window._setBackdrop 消费方（2.1
  // app.js:2546-2548 同口径：写背景图；空串清图）
  win._setBackdrop = function (url) {
    backdropEl.style.backgroundImage = url ? 'url("' + url + '")' : "";
  };

  // 主题镜像（对齐 2.1 app.js:2551-2562 applyAppearance）：localStorage.appearance
  // 同一数据源（后端配置为准、这里仅镜像）。默认 dark；liquid 仅 Windows 生效，
  // 其余平台按静态暗降级（app.js:2555-2556）。html.liquid 切换与 data-theme
  // 只作用于 html 令牌层；非 liquid 清 backdrop 图。
  const IS_WIN = /windows/i.test((win.navigator && win.navigator.userAgent) || "");
  function applyAppearance(mode) {
    const eff = (mode === "liquid" && !IS_WIN) ? "dark" : mode;
    doc.documentElement.setAttribute("data-theme", eff === "light" ? "light" : "dark");
    doc.documentElement.classList.toggle("liquid", eff === "liquid");
    if (eff !== "liquid") win._setBackdrop("");
  }
  try {
    applyAppearance((win.localStorage.getItem("appearance") || "dark").toLowerCase());
  } catch (e) { /* 私有模式等异常沿用系统主题 */ }

  const root = buildShellDom();
  doc.body.appendChild(root);

  // —— 能力降级三档（FR-22/NFR-8；v22degrade=1 为测试钩子模拟不支持环境）——
  // noLens：无 SVG 滤镜能力/模拟降级 → 关折射，保材质；
  // noFrost：无 backdrop-filter/模拟降级 → 不透明实色 nav 面（tokens --raw-solid）。
  const cs = win.CSS;
  const supportsBackdrop = !!(cs && (cs.supports("backdrop-filter", "blur(1px)") ||
    cs.supports("-webkit-backdrop-filter", "blur(1px)")));
  const supportsFilter = !!(cs && cs.supports("filter", "url(#t8-lens)"));
  const forcedDegrade = /[?&#]v22degrade=1/.test(win.location.href);
  const noLens = forcedDegrade || !supportsFilter;
  const noFrost = forcedDegrade || !supportsBackdrop;
  if (noLens) root.setAttribute("data-v22-nolens", "on");
  if (noFrost) root.setAttribute("data-v22-nofrost", "on");
  const reducedMotion = !!(win.matchMedia && win.matchMedia("(prefers-reduced-motion: reduce)").matches);
  if (reducedMotion) root.setAttribute("data-v22-rm", "on");

  // —— 断点形态：matchMedia 订阅，旋转/拖窗实时切换（不刷新页面）——
  const mqBar = win.matchMedia("(max-width: 639.98px)");
  const mqRail = win.matchMedia("(min-width: 640px) and (max-width: 1023.98px)");
  function syncForm() {
    const f = mqBar.matches ? "bar" : (mqRail.matches ? "rail" : "side");
    root.setAttribute("data-shell-form", f);
    if (f !== "bar") closeDrawer();
    refreshLens();
  }
  if (mqBar.addEventListener) mqBar.addEventListener("change", syncForm);
  if (mqRail.addEventListener) mqRail.addEventListener("change", syncForm);
  else { mqBar.addListener(syncForm); mqRail.addListener(syncForm); } // 老 webview 兼容
  // （首次 syncForm 调用延后到装配完成，见「—— 折射层 ——」块之后）

  // 键盘防遮挡（FR-23 布局级保证，注释即证据）：
  // 虚拟键盘弹出 → visualViewport 高度显著小于窗口 → 隐藏悬浮底栏
  // （连同折射层），避免遮挡输入框与系统候选条；收起自动恢复。
  if (win.visualViewport) {
    win.visualViewport.addEventListener("resize", function () {
      const kbdOn = win.visualViewport.height < win.innerHeight * 0.8;
      root.setAttribute("data-v22-kbd", kbdOn ? "on" : "off");
    });
  }

  // —— 折射层（滚动内容快照 + feDisplacementMap，自写实现）——
  const scrollEl = doc.getElementById("sh-scroll");
  const lensContent = doc.getElementById("sh-lens-content");
  const lensEl = root.querySelector(".sh-lens");
  let lensW = 0;
  function refreshLens() {
    if (!lensContent || noLens || reducedMotion) return;
    // 仅手机底栏形态启用；桌面/平板侧面导航无折射面（恒定 1 个滤镜面）
    if (root.getAttribute("data-shell-form") !== "bar") { lensContent.innerHTML = ""; return; }
    // 内容快照：路由渲染完成后重建（滚动中不重建，仅同步 transform）
    lensContent.innerHTML = scrollEl.innerHTML;
    lensW = scrollEl.getBoundingClientRect().width;
    lensContent.style.width = lensW + "px";
    syncLensTransform();
  }
  function syncLensTransform() {
    if (!lensContent || !lensContent.firstChild) return;
    const sc = scrollEl.getBoundingClientRect();
    const ln = lensEl.getBoundingClientRect();
    // 位移采样节流：scroll listener passive + rAF 合帧（见下），每帧至多一次；
    // transform 走合成器，不触发布局/重绘，不重复读 DOM。
    lensContent.style.transform =
      "translate(" + (sc.left - ln.left) + "px," + (sc.top - ln.top - scrollEl.scrollTop) + "px)";
  }
  let lensRaf = 0;
  scrollEl.addEventListener("scroll", function () {
    if (lensRaf || reducedMotion || noLens) return;
    lensRaf = win.requestAnimationFrame(function () { lensRaf = 0; syncLensTransform(); });
  }, { passive: true });

  // —— 抽屉（仅手机形态）——
  const drawerEl = doc.getElementById("sh-drawer");
  const maskEl = doc.getElementById("sh-drawer-mask");
  const burger = doc.getElementById("sh-burger");
  function openDrawer() {
    drawerEl.setAttribute("aria-hidden", "false");
    drawerEl.classList.add("open");
    maskEl.hidden = false;
    burger.setAttribute("aria-expanded", "true");
  }
  function closeDrawer() {
    if (!drawerEl) return;
    drawerEl.setAttribute("aria-hidden", "true");
    drawerEl.classList.remove("open");
    if (maskEl) maskEl.hidden = true;
    if (burger) burger.setAttribute("aria-expanded", "false");
  }
  burger.addEventListener("click", function () {
    if (drawerEl.classList.contains("open")) closeDrawer();
    else openDrawer();
  });
  maskEl.addEventListener("click", closeDrawer);
  doc.addEventListener("keydown", function (e) {
    if (e.key === "Escape") closeDrawer();
  });

  // 首次断点形态同步（syncForm 依赖 closeDrawer/refreshLens；其内部引用的
  // drawerEl/lensContent 等 const 至此全部就绪，避免 TDZ）
  syncForm();

  // —— 导航点击（委托：侧栏/窄栏/底栏/抽屉全部 data-nav）——
  function navHref(name) {
    const routePath = name === "editor" ? "/task/-/edit" : "/" + name;
    const u = router.mode === "history" ? routePath : buildUrl({ mode: "hash", path: routePath, extra: parseHash(win.location.hash).extra });
    return u;
  }
  function refreshNav(name) {
    root.querySelectorAll("[data-nav]").forEach(function (el) {
      const nm = el.getAttribute("data-nav");
      el.classList.toggle("is-active", nm === name);
      el.setAttribute("href", String(navHref(nm)));
    });
  }

  const router = createRouter({
    // 模式由 resolveMode 统一解析（fix1 R1）：产品默认恒 hash；history 仅
    // 注入面（seam）。测试钩子已随 T18 翻版清理。
    mode: "auto",
    history: win.history,
    location: win.location,
    doc: doc,
    win: win,
    scroll: scrollEl,
    host: doc.getElementById("sh-viewhost"),
    views: views,
    onNav: function (name) {
      refreshNav(name);
      refreshLens();
      closeDrawer();
    },
  });
  win.__t8router = router; // TR-8.1 浏览器级断言探针（只读）
  // TR-8.2 探针：折射快照手动刷新（测试注入伪列表后重建快照用），只读调试性质
  win.__t8shell = { refreshLens: refreshLens };

  // —— 导航点击委托（侧栏/窄栏/底栏/抽屉所有 data-nav 元素统一于此；
  //    href 由 refreshNav 维护，仅供语义/中键，点击一律 preventDefault 同页导航）——
  root.addEventListener("click", function (e) {
    const t = e.target && e.target.closest ? e.target.closest("[data-nav]") : null;
    if (!t) return;
    e.preventDefault();
    const nm = t.getAttribute("data-nav");
    router.navigate(nm === "editor" ? "/task/-/edit" : "/" + nm);
  });

  await router.start();

  // —— 启动登录态提示（对齐 2.1 bootLogin/bootConnect：旧壳 app.js:188-272）——
  // 主壳先可见 → guest-bar 显中性「正在连接 Telegram…」不可点 → connect()
  // 按 stage 分流：就绪隐藏条 / 未登录开登录弹层（卡内步骤由 login onEnter
  // 复核定）/ 网络错·超时·busy 留壳内转「未登录 · 点此重试」+ 非阻断小字
  //（#boot-err，app.js:274-284）。点击条 = 开登录弹层（app.js:312-316）。
  const guestBar = doc.getElementById("sh-guestbar");
  const bootErr = doc.getElementById("sh-booterr");
  let booting = false;
  let guestPollId = null;
  let guestPollBusy = false;

  function guestBarShow(text) {
    if (!guestBar) return;
    guestBar.textContent = text || "未登录 · 点此登录";
    guestBar.hidden = false;
  }
  function guestBarHide() {
    if (guestBar) guestBar.hidden = true;
    guestBarBootOff();
    guestPollStop();
  }
  function guestBarBootOn() {
    if (!guestBar) return;
    booting = true;
    guestBar.textContent = "正在连接 Telegram…";
    guestBar.hidden = false;
    guestBar.classList.add("sh-guestbar--booting");
  }
  function guestBarBootOff() {
    booting = false;
    if (guestBar) guestBar.classList.remove("sh-guestbar--booting");
  }
  function bootErrShow(msg) {
    if (!bootErr) return;
    bootErr.textContent = msg || "";
    bootErr.hidden = !msg;
  }

  // 游客态登录状态轮询（app.js:379-415）：guest-bar 可见期间约 2s 一拍，
  // 发现 logged_in&&!guest → 收条；发现登出（!guest&&!logged_in）→ 开登录
  // 弹层停轮询。boot 中性态不发（app.js:395）。
  function guestPollStart() {
    if (guestPollId != null) return;
    guestPollId = win.setInterval(guestPollTick, 2000);
  }
  function guestPollStop() {
    if (guestPollId != null) {
      win.clearInterval(guestPollId);
      guestPollId = null;
    }
  }
  async function guestPollTick() {
    if (booting || guestPollBusy || doc.hidden) return;
    const api = win.pywebview && win.pywebview.api;
    if (!api) return;
    guestPollBusy = true;
    try {
      const st = await api.login_state();
      if (st && st.logged_in && st.guest !== true) {
        guestPollStop();
        guestBarHide();
      } else if (st && st.guest !== true && st.logged_in !== true) {
        guestPollStop();
        guestBarHide();
        if (win.__t8router.current !== "/login") {
          win.__t8router.navigate("/login", { force: true });
        }
      }
    } catch (e) { /* 轮询静默容错（app.js:412 catch） */ } finally {
      guestPollBusy = false;
    }
  }

  // login.js 钩子：doGuest 成功显条+轮询（app.js:437-438）、登录成功收条
  //（afterLogin → hideGuestBar）。sync 为保底同步（按 login_state 裁决）。
  win.__t8guest = {
    show: function (text) {
      guestBarBootOff();
      guestBarShow(text);
      bootErrShow("");
      guestPollStart();
    },
    hide: function () {
      guestBarHide();
      bootErrShow("");
    },
    sync: async function () {
      const api = win.pywebview && win.pywebview.api;
      if (!api) return;
      try {
        const st = await api.login_state();
        if (st && st.logged_in && st.guest !== true) guestBarHide();
        else {
          guestBarShow("未登录 · 点此登录");
          guestPollStart();
        }
      } catch (e) { /* 保底同步静默容错 */ }
    },
  };

  function openLoginOverlay() {
    if (booting) return; // T56 Bug4：boot 中性态条不可点（app.js:313）
    if (win.__t8router.current === "/login") return;
    win.__t8router.navigate("/login", { force: true });
  }
  if (guestBar) {
    guestBar.addEventListener("click", openLoginOverlay);
    guestBar.addEventListener("keydown", function (e) {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        openLoginOverlay();
      }
    });
  }

  // bridge 就绪等待（同 login.js bridgeReady 口径；桩宿主立即就绪）。
  // 只 resolve 布尔、不 resolve api 代理本身——Promise 吸收 thenable 会
  // 取 .then 值，被同形桥桩记成假 RPC（tr15 K1「then」误报教训）。
  function apiReady(timeoutMs) {
    return new Promise(function (res) {
      if (win.pywebview && win.pywebview.api) return res(true);
      let done = false;
      function fin() {
        if (done) return;
        done = true;
        win.removeEventListener("pywebviewready", fin);
        clearTimeout(timer);
        res(!!(win.pywebview && win.pywebview.api));
      }
      const timer = setTimeout(fin, timeoutMs);
      win.addEventListener("pywebviewready", fin);
    });
  }

  async function bootAuth() {
    if (!guestBar) return;
    guestBarBootOn();
    bootErrShow("");
    await apiReady(8000);
    const api = win.pywebview && win.pywebview.api;
    let r = null;
    try {
      r = api ? await api.connect() : { ok: false, error: "应用服务未就绪，请稍后重试" };
    } catch (e) {
      r = { ok: false, error: (e && e.message) || String(e) };
    }
    // 已就绪（stage=ready，或 client 已在场的 {ok:true} 无 stage 形状）
    if (r && r.ok && (r.stage === "ready" || !r.stage)) {
      guestBarHide();
      bootErrShow("");
      return;
    }
    // need_code（含软成功 warning）→ 登录弹层停码步；否则复核 login_state
    //（T58 2.3：has_session=后端内存标志「本轮已发码待验证」，零本地推断）
    let codeStep = !!(r && r.ok && r.stage === "need_code");
    if (!codeStep && api) {
      let st = null;
      try { st = await api.login_state(); } catch (e) { st = null; }
      if (st && st.logged_in) {
        guestBarHide();
        bootErrShow("");
        return;
      }
      codeStep = !!(st && st.has_session && !(r && r.busy));
    }
    if (codeStep || (r && r.ok && r.stage === "need_phone")) {
      // 未登录：收中性条、开登录弹层（need_phone 的 phone_prefill 预填由
      // login onEnter 的 connect 复核承接，app.js:253-263 口径零发码）
      guestBarHide();
      bootErrShow("");
      if (win.__t8router.current !== "/login") {
        win.__t8router.navigate("/login", { force: true });
      }
      return;
    }
    // 网络错/超时/busy：留在壳内，条转可点重试（错误仅非阻断小字）
    guestBarShow("未登录 · 点此重试");
    bootErrShow((r && (r.warning || r.error)) || "连接 Telegram 失败");
  }
  bootAuth();
}
