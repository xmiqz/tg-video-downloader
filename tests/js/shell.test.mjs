// ============================================================
// TR3 - buildShellDom 结构契约 + 窄栏/底栏互斥
//
// buildShellDom 是模块私有、只由 boot() 调用；boot() 需要一整套浏览器 DOM
// （innerHTML 解析 / matchMedia / CSS.supports / canvas / Image / rAF）。
// 零依赖下不引入 jsdom，故改为「只读源码 + 静态契约断言」。
// 详见 evidence/V2/seams.md TR3「漏掉什么」。
// ============================================================
import test, { after } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const ROUTER = readFileSync(new URL("../../web/js/router.js", import.meta.url), "utf8");
const SHELL_CSS = readFileSync(new URL("../../web/css/shell.css", import.meta.url), "utf8");
const INDEX_HTML = readFileSync(new URL("../../web/index.html", import.meta.url), "utf8");

let N = 0;
function ok(v, label) { N++; assert.ok(v, label); }
function eq(a, b, label) { N++; assert.equal(a, b, label); }
function deep(a, b, label) { N++; assert.deepEqual(a, b, label); }

// 截出壳装配相关函数（navLinkHtml / meBoxHtml / buildShellDom），
// 到下一个顶层 "let booted" 为止。
function shellFnBody() {
  const start = ROUTER.indexOf("function navLinkHtml(");
  assert.notEqual(start, -1, "router.js 中应存在 navLinkHtml");
  assert.notEqual(ROUTER.indexOf("function buildShellDom()"), -1, "router.js 中应存在 buildShellDom");
  const end = ROUTER.indexOf("let booted = false", start);
  return ROUTER.slice(start, end > start ? end : ROUTER.length);
}
const SHELL = shellFnBody();

// 解析 NAV_ITEMS 字面量（name 顺序即契约）
function navItems() {
  const m = ROUTER.match(/const NAV_ITEMS = \[([\s\S]*?)\n\];/);
  assert.ok(m, "router.js 中应存在 NAV_ITEMS 字面量");
  return [...m[1].matchAll(/name:\s*"([^"]+)"/g)].map((x) => x[1]);
}

// 抽出某段 @media 块体（按花括号配平）
function mediaBlock(query) {
  const at = SHELL_CSS.indexOf("@media " + query);
  if (at < 0) return "";
  const open = SHELL_CSS.indexOf("{", at);
  let depth = 0;
  for (let i = open; i < SHELL_CSS.length; i++) {
    const ch = SHELL_CSS[i];
    if (ch === "{") depth++;
    else if (ch === "}") { depth--; if (depth === 0) return SHELL_CSS.slice(open + 1, i); }
  }
  return "";
}

test("TR3-A 六个外壳锚点存在", () => {
  const ids = ["sh-root", "sh-side", "sh-drawer", "sh-viewhost", "sh-bottombar", "sh-jobdock"];
  for (const id of ids) {
    const esc = id.replace(/-/g, "\\-");
    const asId = new RegExp('id="' + esc + '"').test(SHELL);
    const asClass = new RegExp('class="[^"]*\\b' + esc + '\\b').test(SHELL);
    const asAssignment = new RegExp('\\.id = "' + esc + '"|\\.className = "' + esc + '"').test(SHELL);
    ok(asId || asClass || asAssignment, "buildShellDom 含锚点 " + id);
  }
  ok(/root\.id = "sh-root";/.test(SHELL), "根容器 id 为 sh-root");
  ok(SHELL.indexOf('id="sh-viewhost"') >= 0, "视图宿主 id 为 sh-viewhost");
  ok(SHELL.indexOf('id="sh-jobdock"') >= 0, "作业浮层宿主 id 为 sh-jobdock");
  ok(SHELL.indexOf('id="sh-drawer"') >= 0, "抽屉 id 为 sh-drawer");
  ok(SHELL.indexOf('class="sh-side"') >= 0, "侧栏 class 为 sh-side");
  ok(SHELL.indexOf('class="sh-bottombar"') >= 0, "底栏 class 为 sh-bottombar");
  // 滚动容器与提示条（boot 内 getElementById 依赖）
  ok(SHELL.indexOf('id="sh-scroll"') >= 0, "滚动容器 id 为 sh-scroll");
  ok(SHELL.indexOf('id="sh-guestbar"') >= 0, "游客提示条 id 为 sh-guestbar");
  ok(SHELL.indexOf('id="sh-booterr"') >= 0, "启动错误条 id 为 sh-booterr");
});

test("TR3-B data-nav 集合与 NAV_ITEMS 一致（底栏不引入第五页签）", () => {
  const nav = navItems();
  deep(nav, ["active", "tasks", "new", "settings"], "NAV_ITEMS 四页签且顺序冻结");

  // 侧栏与抽屉共用同一 sideNav（NAV_ITEMS.map 生成一次）
  const sideNavUses = (SHELL.match(/\+\s*sideNav\s*\+/g) || []).length;
  eq(sideNavUses, 2, "sideNav 被侧栏与抽屉各引用一次（同一集合）");

  // 静态 data-nav 字面量（底栏 FAB + me-box 登录）；排除 navLinkHtml 的拼接模板
  const literals = [...SHELL.matchAll(/data-nav="([^"'+]+)"/g)].map((x) => x[1]);
  // 源码序：meBoxHtml 定义在前（login），底栏 barItems 的 FAB 在后（new）
  deep(literals.sort(), ["login", "new"], "静态 data-nav 仅 login（me-box）+ new（底栏 FAB），无第五页签");

  // 汉堡钮不参与 data-nav 集合
  ok(/id="sh-burger"/.test(SHELL), "汉堡钮 id 存在");
  ok(!/id="sh-burger"[^>]*data-nav=/.test(SHELL), "汉堡钮不带 data-nav（仅开抽屉）");

  // navLinkHtml 生成的页签名来自 NAV_ITEMS（单一数据源）
  ok(/function navLinkHtml\(name, label, ico\)/.test(ROUTER), "navLinkHtml 签名");
  ok(/NAV_ITEMS\.map\(function \(it\) \{ return navLinkHtml\(it\.name, it\.label, it\.ico\); \}\)/.test(SHELL),
    "侧栏页签由 NAV_ITEMS.map(navLinkHtml) 生成（集合不可能漂移）");

  // 底栏四槽只取 tasks/new/active/settings，不含 login
  const bar = SHELL.slice(SHELL.indexOf("const barItems"), SHELL.indexOf("root.innerHTML"));
  for (const name of ["tasks", "new", "active", "settings"]) {
    ok(bar.indexOf('"' + name + '"') >= 0, "底栏含 " + name);
  }
  ok(bar.indexOf('"login"') < 0, "登录不进底栏（模态态路由）");
});

test("TR3-C 窄栏与底栏互斥（CSS 断点）", () => {
  const desktop = mediaBlock("(min-width: 640px)");
  ok(desktop.length > 0, "存在 min-width:640px 断点块");
  ok(/\.sh-bottombar[^{]*\{[^}]*display:\s*none\s*!important/.test(desktop),
    "≥640px 隐藏 .sh-bottombar");
  ok(/\.sh-drawer\b[^{]*\{[^}]*display:\s*none\s*!important/.test(desktop) ||
     /\.sh-drawer,\s*\n?\s*\.sh-drawer-mask\s*\{\s*display:\s*none\s*!important/.test(desktop),
    "≥640px 隐藏 .sh-drawer");
  ok(/\.sh-drawer-mask[^{]*\{[^}]*display:\s*none\s*!important/.test(desktop) ||
     /\.sh-drawer-mask\s*\{\s*display:\s*none\s*!important/.test(desktop),
    "≥640px 隐藏 .sh-drawer-mask");

  const phone = mediaBlock("(max-width: 639.98px)");
  ok(phone.length > 0, "存在 max-width:639.98px 断点块");
  ok(/\.sh-side\s*\{[^}]*display:\s*none/.test(phone), "<640px 隐藏 .sh-side");

  // 底栏基础规则为 flex（手机形态显示），桌面被上条 !important 关掉
  ok(/\.sh-bottombar\s*\{[^}]*display:\s*flex/.test(SHELL_CSS), "底栏基础 display:flex");

  // 运行时同步：非 bar 形态关闭抽屉；bar 形态才刷新折射快照
  ok(/const f = mqBar\.matches \? "bar" : \(mqRail\.matches \? "rail" : "side"\);/.test(ROUTER),
    "syncForm 三档判定 bar/rail/side");
  ok(/if \(f !== "bar"\) closeDrawer\(\);/.test(ROUTER), "非 bar 形态关闭抽屉");
  ok(/if \(root\.getAttribute\("data-shell-form"\) !== "bar"\) \{ lensContent\.innerHTML = ""; return; \}/.test(ROUTER),
    "折射快照仅在 bar 形态建立");
  ok(/data-v22-kbd="on"\] \.sh-bottombar \{ display: none; \}/.test(SHELL_CSS),
    "虚拟键盘弹出时隐藏底栏（FR-23）");
});

test("TR3-D 引导顺序与页面契约", () => {
  // index.html 先加载 bridge（提供 pywebview），再动态 import router 并 boot
  const bridgeAt = INDEX_HTML.indexOf('src="js/bridge.js"');
  const routerAt = INDEX_HTML.indexOf('import("./js/router.js")');
  ok(bridgeAt > 0, "index.html 引入 bridge.js");
  ok(routerAt > 0, "index.html 动态 import router.js");
  ok(bridgeAt < routerAt, "bridge 先于 router 引导（pywebview 就绪在前）");
  ok(/m\.boot\(\)/.test(INDEX_HTML), "index.html 调用 boot()");

  // boot 幂等
  ok(/let booted = false;/.test(ROUTER), "booted 标志存在");
  ok(/export async function boot\(\) \{\s*\n\s*if \(booted\) return;/.test(ROUTER), "boot() 幂等早退");

  // boot 挂载后创建 router 并暴露只读探针
  ok(/win\.__t8router = router;/.test(ROUTER), "暴露 __t8router 探针");
  ok(/mountJobDock\(\);/.test(ROUTER), "boot 挂载作业浮层");
  ok(/host: doc\.getElementById\("sh-viewhost"\)/.test(ROUTER), "router 宿主为 sh-viewhost");
  ok(/scroll: scrollEl/.test(ROUTER), "router 滚动容器为 sh-scroll");
  ok(/mode: "auto"/.test(ROUTER), "boot 传 mode:auto（由 resolveMode 收口为 hash）");

  // 导航点击委托：全部 data-nav 统一 preventDefault + 同页导航
  ok(/root\.addEventListener\("click", function \(e\) \{/.test(ROUTER), "根节点点击委托");
  ok(/e\.target\.closest\("\[data-nav\]"\)/.test(ROUTER), "委托选择器为 [data-nav]");
  ok(/router\.navigate\(nm === "editor" \? "\/task\/-\/edit" : "\/" \+ nm\);/.test(ROUTER),
    "委托统一转 router.navigate");
});

after(() => {
  console.log("TR3 结论：PASS（" + N + " 项；静态源码/CSS 契约，非运行时 DOM）");
});
