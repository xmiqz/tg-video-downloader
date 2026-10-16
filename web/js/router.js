// ============================================================
// router.js — T8 v2.2 新壳路由与响应式应用外壳（全版地基）
//
// 设计契约：evidence/T8/t8_contract.md（一次冻结，下游 T11–T16
// 只替换 web/js/views/* 占位与其 css 切片，零改本文件/壳样式/引导）。
// fix1（2026-10-15，spec 评审 hard 闭环）：产品默认路由模式恒 hash，
// history 模式实现保留仅供具备 SPA 回退的宿主经测试钩子 v22history=1
// 显式启用（与 v22shell 同待遇、净化不进历史）；模式解析收进纯函数
// resolveMode（T18 翻版时 v22history/v22hashmode 与 v22shell 一并清理）。
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

// parseHash：'#/task/1/edit&t=x&v22shell=1' → { path, extra:[[k,v],…] }
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
// 净化规则（fix1 R1）：v22shell 与 v22history 均为一次性测试钩子，router
// 永不把它们写进 URL（v22history 与 v22shell 同等待遇，T18 翻版一并清理）；
// 其余参数段（t=、v22hashmode、v22theme 等）按序保留——保证 bridge token
// 在两种模式下存活。
export function buildUrl(opts) {
  const kv = (opts.extra || []).filter(function (p) {
    return p[0] !== "v22shell" && p[0] !== "v22history";
  });
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
//   url         — location.href（或 pathname+search+hash 拼接），识别测试钩子；
//   pushStateOk — 宿主 history.pushState 可用性。
// 组长裁决（2026-10-15，spec hard 闭环）：产品默认模式恒 hash——loopback、
// file:、普通 http 静态托管、任意 host 均默认 hash。依据：FR-4 规范入口
// 本就是 `http://127.0.0.1:<port>/#t=<token>`；FR-26 要求无 SPA 回退的普通
// http 静态托管可运行（pathname 路由刷新即 404 白页）；FR-27 禁止白屏；
// FR-3 原文允许 "History API / hash 兼容"。history 模式全套实现保留，仅供
// 具备 SPA 回退的宿主经显式测试钩子 v22history=1（hash 或查询串）启用；
// v22hashmode=1 保留为强制 hash（与默认等价，测试用）。
export function resolveMode(spec) {
  const s = spec || {};
  if (s.mode === "hash" || s.mode === "history") return s.mode;
  const url = String(s.url || "");
  if (/[?&#]v22hashmode=1(?!\w)/.test(url)) return "hash";
  const proto = String(s.protocol || "").toLowerCase();
  const isFile = proto.indexOf("file") === 0;
  if (/[?&#]v22history=1(?!\w)/.test(url) && !isFile && s.pushStateOk) return "history";
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

  // 模式解析（fix1 R1）：默认 hash；仅测试钩子 v22history=1 在具备 pushState
  // 的非 file: 宿主显式启用 history；v22hashmode=1 强制 hash。全部规则收在
  // 纯函数 resolveMode（seam S7 单测）。
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

  async function render(name, params, path) {
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
    if (r.path === path && path === state.current && state.currentView) return; // 同路径幂等
    applyAddress(r, path, !!(opts && opts.replace));
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
    // 引导期归一：根路径/无效深链 → 默认路由；无论是否重定向，地址中的
    // v22shell 开关一律净化（buildUrl 恒不写出开关），t= 等 token 参数保留；
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
  root.setAttribute("data-v22shell", "on");
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
    // 主内容（视图宿主）
    '<main class="sh-main"><div class="sh-scroll" id="sh-scroll">' +
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

  // 视图层样式按约定路径引导（shell/tokens 由 index.html boot 片段引入）
  ["login", "new", "active", "tasks", "settings", "editor"].forEach(function (n) {
    const l = doc.createElement("link");
    l.rel = "stylesheet";
    l.href = "css/views/" + n + ".css";
    doc.head.appendChild(l);
  });
  // T10 组件库样式（t8_contract.md §7 R4-① 预授权的同型引导；仅 v22shell=1
  // 新壳路径装载，开关默认关闭时零请求）
  const uiCss = doc.createElement("link");
  uiCss.rel = "stylesheet";
  uiCss.href = "css/ui.css";
  doc.head.appendChild(uiCss);

  // 主题镜像：与 index.html 旧壳引导脚本同一数据源（localStorage.appearance，
  // 后端配置为准、这里仅镜像）。默认 dark（对齐产品默认"静态暗"）；liquid
  // 亦为暗底玻璃。data-theme 只作用于 html 令牌层，旧壳此时已被外壳隐藏，
  // 开关关闭路径本代码不执行，旧壳渲染不受任何影响。
  // v22theme=light|dark（hash 段）：仅测试钩子（截图矩阵深浅覆盖），
  // T18 翻版时与 v22shell 一并清理。
  try {
    var th = (String(win.location.hash).match(/[#&]v22theme=(light|dark)/) || [])[1];
    var m = th || (win.localStorage.getItem("appearance") || "dark").toLowerCase();
    doc.documentElement.setAttribute("data-theme", m === "light" ? "light" : "dark");
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
    // 模式由 resolveMode 统一解析（fix1 R1）：产品默认恒 hash；测试钩子
    // v22history=1（hash/查询串）显式启用 history（供具备 SPA 回退的宿主
    // 测试）；v22hashmode=1 强制 hash。三钩子 T18 翻版时与 v22shell/
    // v22theme 一并清理。
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
}
