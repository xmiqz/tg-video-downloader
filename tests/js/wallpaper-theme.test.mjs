// The daily-wallpaper theme is a photo the user chose to look at, so it must
// not be buried under a dark scrim, its panels must be real frosted glass, and
// its text must adapt to how bright the photo is.
//
// Caught a real regression: the theme shipped with a 34-58% black overlay, flat
// translucent cards (no backdrop-filter), and a single fixed dark ink set, so
// the picture looked muddy and light photos made the headings unreadable.
import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");

async function css(name) {
  return readFile(path.join(ROOT, "web", "css", name), "utf8");
}

function rule(cssText, selector) {
  const i = cssText.indexOf(selector + " {");
  if (i < 0) return "";
  return cssText.slice(i, cssText.indexOf("}", i) + 1);
}

test("wallpaper scrim stays light instead of dimming the photo", async () => {
  const shell = await css("shell.css");
  const block = rule(shell, 'html[data-theme="wallpaper"] #sh-backdrop::after');
  assert.ok(block, "缺少壁纸遮罩规则");

  // Any rgba(8,10,18, .34) or heavier would bury the image again.
  const alphas = [...block.matchAll(/rgba\(\s*8\s*,\s*10\s*,\s*18\s*,\s*([\d.]+)\s*\)/g)]
    .map((m) => Number(m[1]));
  assert.ok(alphas.length > 0, "遮罩应使用 rgba(8,10,18,α)");
  for (const a of alphas) {
    assert.ok(a <= 0.30, `遮罩过暗：α=${a} 会把壁纸压成灰糊`);
  }
});

test("wallpaper panels keep a frosted-glass backdrop", async () => {
  const shell = await css("shell.css");
  const i = shell.indexOf('html[data-theme="wallpaper"] .sh-side,');
  assert.ok(i > 0, "缺少壁纸玻璃材质规则");
  const block = shell.slice(i, shell.indexOf("}", i) + 1);

  assert.match(block, /backdrop-filter:\s*blur\(/,
    "壁纸主题下的卡片必须有 backdrop-filter 玻璃质感");
  assert.ok(!/backdrop-filter:\s*none/.test(block),
    "壁纸主题不应禁用 backdrop-filter");
  // The liquid block that kills backdrop-filter must not cover the wallpaper
  // selectors, otherwise the frosted panels silently lose their blur.
  const liquidBlock = rule(shell, "html.liquid .sh-side, html.liquid .sh-bar-mat, html.liquid .sh-drawer,\nhtml.liquid .v22-ph-card, html.liquid .u-card, html.liquid .u-actionbar,\nhtml.liquid .u-overlay-mask, html.liquid .ac-ov-card, html.liquid .ac-bulk,\nhtml.liquid .ac-group, html.liquid .ac-card, html.liquid .tk-card,\nhtml.liquid .ev-move-mask");
  if (liquidBlock) {
    assert.ok(!liquidBlock.includes("wallpaper"),
      "禁用模糊的 liquid 规则不应覆盖壁纸主题");
  }
});

test("wallpaper switches ink tokens on data-lum like liquid does", async () => {
  const tokens = await css("tokens.css");
  const bright = rule(tokens, ':root[data-theme="wallpaper"][data-lum="bright"]');
  const dark = rule(tokens, ':root[data-theme="wallpaper"][data-lum="dark"]');

  assert.ok(bright, "缺少亮壁纸原料表");
  assert.ok(dark, "缺少暗壁纸原料表");
  // Bright wallpaper needs dark ink; without this the headings stay near-white.
  assert.match(bright, /--raw-ink:\s*#1b2233/, "亮壁纸应换深色文字");
  // Dark wallpaper keeps the light ink set from the default theme.
  assert.ok(!/--raw-ink:\s*#1b2233/.test(dark), "暗壁纸不应换成深色文字");
});

test("router samples wallpaper brightness through _setBackdrop", async () => {
  const js = await readFile(path.join(ROOT, "web", "js", "router.js"), "utf8");
  assert.match(js, /themeNeedsLum\(\)/,
    "亮度采样应覆盖壁纸主题，而不只是 liquid");
  assert.match(js, /data-theme[\s\S]{0,40}wallpaper/,
    "themeNeedsLum 应识别 wallpaper 主题");
});

test("settings assignments go through the brightness hook", async () => {
  const js = await readFile(
    path.join(ROOT, "web", "js", "views", "settings.js"), "utf8");
  assert.match(js, /export function setBackdropImage/,
    "缺少统一的壁纸设置入口");
  assert.match(js, /window\._setBackdrop\(url,\s*lum\)/,
    "壁纸设置必须经过 _setBackdrop 才能触发亮度采样");
  // Every direct style assignment would bypass sampling.
  const direct = [...js.matchAll(/back\.style\.backgroundImage\s*=\s*bingBackdropCss/g)];
  assert.equal(direct.length, 0,
    "不应再有绕过采样器的直接 backgroundImage 赋值");
});

test("settings cards are separated, not fused edge to edge", async () => {
  const cssText = await css("views/settings.css");
  const root = rule(cssText, ".vw-settings");
  assert.ok(root, "缺少 .vw-settings");

  const m = root.match(/gap:\s*var\(--space-(\d+)\)/);
  assert.ok(m, "卡片栈应使用间距令牌");
  // 0 was the 2.1 value: adjacent glass cards shared a border and read as one
  // block chopped in half, which the user reported as "挨得太近".
  assert.ok(Number(m[1]) >= 4,
    `卡片间距过小（--space-${m[1]}），玻璃面会粘在一起`);
});

test("glass cards carry a specular highlight layer", async () => {
  for (const [name, selector] of [
    ["views/settings.css", ".st-card"],
    ["ui.css", ".u-card"],
  ]) {
    const cssText = await css(name);
    const card = rule(cssText, selector);
    assert.ok(card, `缺少 ${selector}`);
    assert.match(card, /isolation:\s*isolate/,
      `${selector} 需要独立层叠上下文来承载高光层`);

    const i = cssText.indexOf(`${selector}::before {`);
    assert.ok(i > 0, `${selector} 缺少高光伪元素`);
    const glow = cssText.slice(i, cssText.indexOf("}", i) + 1);
    assert.match(glow, /linear-gradient\(155deg/,
      `${selector} 高光应为斜向镜面渐变`);
    assert.match(glow, /pointer-events:\s*none/,
      `${selector} 高光层不能拦截点击`);

    // Content must paint above the highlight, or text is washed out.
    assert.match(cssText, new RegExp(
      `${selector.replace(".", "\\.")} > \\* \\{[^}]*z-index:\\s*1`),
      `${selector} 的子元素需要在更高层`);
  }
});

test("the sidebar footer does not paint a dark band over the glass", async () => {
  const shell = await css("shell.css");
  const foot = rule(shell, ".sh-side-foot");
  assert.ok(foot, "缺少 .sh-side-foot");
  // A solid 1px border plus the translucent sidebar read as a dark block in
  // the lower-left corner under the liquid theme.
  assert.ok(!/border-top:\s*var\(--line-1\)\s+solid/.test(foot),
    "侧底不应再有实色上边框");
  assert.match(foot, /border-top:\s*0/,
    "侧底上边框应被移除");
});

test("the login card is opaque enough to be readable over any backdrop", async () => {
  const cssText = await css("views/login.css");
  const liquidCard = rule(cssText, "html.liquid .lg-login-card");
  assert.ok(liquidCard, "缺少液态玻璃登录卡规则");

  const blur = liquidCard.match(/blur\((\d+)px\)/);
  assert.ok(blur, "登录卡应有 backdrop-filter 模糊");
  assert.ok(Number(blur[1]) >= 30,
    `登录卡模糊半径过小（${blur[1]}px），下层内容会透出来干扰阅读`);

  // The fill must be dense enough that the card behind it stops competing.
  const alphas = [...liquidCard.matchAll(/rgba\(\s*\d+\s*,\s*\d+\s*,\s*\d+\s*,\s*([\d.]+)\s*\)/g)]
    .map((m) => Number(m[1])).filter((a) => a >= 0.5);
  assert.ok(alphas.length >= 2,
    "登录卡底色应足够不透明（至少两个 ≥0.5 的填充停点）");
});

test("login ink stays light even when the liquid theme flips to dark ink", async () => {
  const cssText = await css("views/login.css");
  const i = cssText.indexOf("--color-text: #f2f5ff");
  assert.ok(i > 0, "登录卡应固定浅色文字令牌");
  const block = cssText.slice(cssText.lastIndexOf(".lg-login-card", i),
    cssText.indexOf("}", i) + 1);
  assert.match(block, /--color-text-strong:\s*#ffffff/,
    "登录卡标题色应固定为白，不随亮底令牌翻转");
});

test("editor primary actions stay readable on a bright wallpaper", async () => {
  const cssText = await css("taskedit.css");
  const primary = rule(cssText, ".btn.primary");
  assert.ok(primary, "缺少编辑页主按钮规则");
  assert.match(primary, /color:\s*#fff/i,
    "亮壁纸会切换深色 --ink，主按钮必须固定白字");
});

test("bright wallpaper gives editor badges and destructive actions dark ink", async () => {
  const cssText = await css("taskedit.css");
  assert.match(cssText,
    /\[data-lum="bright"\]\s+\.st-pending[\s\S]{0,160}color:\s*#7c5100/,
    "亮壁纸的未下载徽标必须换成深色文字");
  assert.match(cssText,
    /\[data-lum="bright"\]\s+\.mini-action\.danger[\s\S]{0,180}color:\s*#a51f35/,
    "亮壁纸的移除按钮不能继续使用浅粉文字");
});

test("switching from Bing to liquid clears the stale photo and luminance", async () => {
  const router = await readFile(
    path.join(ROOT, "web", "js", "router.js"), "utf8");
  const start = router.indexOf("function applyAppearance(mode)");
  const block = router.slice(start, router.indexOf("\n  }", start) + 4);
  assert.ok(start > 0, "缺少 router.applyAppearance");
  assert.match(block, /removeAttribute\("data-lum"\)/,
    "切换主题前应清除旧壁纸亮度");
  assert.match(block, /win\._setBackdrop\(""\)/,
    "切换主题前应清除旧背景图");

  const settings = await readFile(
    path.join(ROOT, "web", "js", "views", "settings.js"), "utf8");
  const s = settings.indexOf("function applyAppearance(mode)");
  const settingsBlock = settings.slice(s, settings.indexOf("\n}", s) + 2);
  assert.ok(s > 0, "缺少 settings.applyAppearance");
  assert.match(settingsBlock, /removeAttribute\("data-lum"\)/,
    "设置页切主题前应清除旧亮度");
  assert.match(settingsBlock, /window\._setBackdrop\(""\)/,
    "设置页切主题前应清除旧背景图");
});

test("re-entering the same material preserves the live/static backdrop", async () => {
  const router = await readFile(
    path.join(ROOT, "web", "js", "router.js"), "utf8");
  const settings = await readFile(
    path.join(ROOT, "web", "js", "views", "settings.js"), "utf8");
  for (const [name, text] of [["router", router], ["settings", settings]]) {
    assert.match(text, /sameMaterial/,
      `${name} 应区分同材质重入与真正切换`);
    assert.match(text, /if \(!sameMaterial\)[\s\S]{0,80}_setBackdrop\(""\)/,
      `${name} 同材质重入不应清空背景`);
  }
  assert.match(settings, /function bindBackdropRecovery/,
    "缺少窗口尺寸变化背景恢复入口");
  assert.match(settings, /applyBingBackdrop\(\)/,
    "窗口尺寸变化后应能恢复丢失的 Bing 背景");
  assert.match(settings, /bindBackdropRecovery\(\);[\s\S]{0,120}loadAll\(\)/,
    "设置页进入时应绑定背景恢复监听");
  assert.match(settings, /visibilitychange[\s\S]{0,600}applyBingBackdrop/,
    "窗口从最小化恢复后应能恢复 Bing 背景");
});

test("stale liquid capture events cannot overwrite the daily wallpaper", async () => {
  const router = await readFile(
    path.join(ROOT, "web", "js", "router.js"), "utf8");
  const start = router.indexOf("win._setBackdrop = function");
  const block = router.slice(start, router.indexOf("\n  };", start) + 5);
  assert.ok(start > 0, "缺少 _setBackdrop 消费方");
  assert.match(block,
    /data-theme[^\n]+wallpaper[\s\S]{0,140}return;/,
    "每日壁纸模式下必须丢弃延迟到达的液态采样帧");
});

test("standalone editor consumes the shared backdrop hook and syncs tabs", async () => {
  const js = await readFile(
    path.join(ROOT, "web", "js", "taskedit.js"), "utf8");
  assert.match(js, /window\._setBackdrop\s*=\s*function/,
    "编辑页必须提供 bridge 可调用的背景设置钩子");
  assert.match(js, /ev\.key === "appearance"[\s\S]{0,80}wallpaper-rev/,
    "编辑页应监听外观和壁纸选择变化");
  assert.match(js, /edSampleLum\(url\)/,
    "后端缺少 lum 时编辑页应有 canvas 亮度兜底");
});

test("browser task editing uses same-tab navigation, never a popup", async () => {
  const js = await readFile(
    path.join(ROOT, "web", "js", "views", "tasks.js"), "utf8");
  const start = js.indexOf("function openSheetEditor(taskId)");
  const block = js.slice(start, js.indexOf("\n}", start) + 2);
  assert.ok(start > 0, "缺少 openSheetEditor");
  assert.match(block, /location\.assign\(directUrl\)/,
    "浏览器模式应同页打开编辑页");
  assert.ok(!/window\.open/.test(block),
    "编辑入口不应再依赖会被拦截的 window.open");
  assert.match(block, /target\.origin !== location\.origin/,
    "宿主返回 URL 必须做同源校验");
});
