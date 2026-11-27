import test from "node:test";
import assert from "node:assert/strict";
import { existsSync, readFileSync, statSync } from "node:fs";

const ROOT = new URL("../../", import.meta.url);
const read = (name) => readFileSync(new URL(name, ROOT), "utf8");
const CSS = read("web/css/glass-system.css");
const ROUTER = read("web/js/router.js");
const TASKEDIT = read("web/js/taskedit.js");

function block(start) {
  const at = CSS.indexOf(start);
  assert.ok(at >= 0, `缺少规则：${start}`);
  const open = CSS.indexOf("{", at);
  let depth = 0;
  for (let i = open; i < CSS.length; i += 1) {
    if (CSS[i] === "{") depth += 1;
    if (CSS[i] === "}" && --depth === 0) return CSS.slice(at, i + 1);
  }
  return "";
}

function alphaRange(start, rgb) {
  return alphaRangeInText(block(start), rgb);
}

function alphaRangeInText(text, rgb) {
  const pattern = new RegExp(`rgba\\(\\s*${rgb.join("\\s*,\\s*")}\\s*,\\s*([\\d.]+)\\s*\\)`, "g");
  return [...text.matchAll(pattern)].map((m) => Number(m[1]));
}

test("v2.7 wallpaper stage is translucent glass on every luminance branch", () => {
  assert.ok(!/html\[data-theme="wallpaper"\] body \.xian-stage-bay[\s\S]{0,600}backdrop-filter:\s*none\s*!important/.test(CSS),
    "壁纸舞台不能禁用 backdrop-filter");
  const stage = block('html[data-theme="wallpaper"] body .xian-stage-bay {');
  assert.match(stage, /-webkit-backdrop-filter:\s*blur\(18px\) saturate\(160%\) !important/);
  assert.match(stage, /backdrop-filter:\s*blur\(18px\) saturate\(160%\) !important/);
  assert.ok(!/color-mix\(in srgb, var\(--color-surface\) 9[26]%/.test(stage),
    "舞台底不能是 v2.5.2 的 96%/92% 实底");

  const dark = alphaRangeInText(stage, [10, 14, 24]);
  assert.deepEqual(dark, [.52, .40]);
  const bright = alphaRange('html[data-theme="wallpaper"][data-lum="bright"] body .xian-stage-bay {',
    [255, 255, 255]);
  assert.deepEqual(bright, [.56, .46]);
});

test("v2.7 stage insets stay readable without a second blur", () => {
  const inset = block('html[data-theme="wallpaper"] body :is(\n  .xian-stage-card,');
  assert.match(inset, /color-mix\(in srgb, var\(--color-surface-raise\) 84%, transparent\)/);
  assert.match(inset, /backdrop-filter:\s*none\s*!important/);
  const dark = alphaRange('html[data-theme="wallpaper"][data-lum="dark"] body :is(\n  .xian-stage-card,',
    [12, 16, 28]);
  const bright = alphaRange('html[data-theme="wallpaper"][data-lum="bright"] body :is(\n  .xian-stage-card,',
    [255, 255, 255]);
  assert.deepEqual(dark, [.80]);
  assert.deepEqual(bright, [.84]);
  assert.ok(dark[0] >= .72 && dark[0] <= .84 && bright[0] >= .72 && bright[0] <= .84);
});

test("v2.7 task cards gain a light blur and brighter wallpaper-only separators", () => {
  const card = block('html[data-theme="wallpaper"] body .tk-card {');
  const blur = card.match(/backdrop-filter:\s*blur\((\d+(?:\.\d+)?)px\)/);
  assert.ok(blur, "任务卡需要 blur");
  assert.ok(Number(blur[1]) >= 10 && Number(blur[1]) <= 14,
    `任务卡 blur 应在 10–14px，实际 ${blur[1]}px`);

  const dark = alphaRange('html[data-theme="wallpaper"][data-lum="dark"] body .tk-card,',
    [22, 28, 46]);
  const darkMid = alphaRange('html[data-theme="wallpaper"][data-lum="dark"] body .tk-card,',
    [8, 11, 20]);
  const bright = alphaRange('html[data-theme="wallpaper"][data-lum="bright"] body .tk-card {',
    [255, 255, 255]);
  const brightMid = alphaRange('html[data-theme="wallpaper"][data-lum="bright"] body .tk-card {',
    [248, 251, 255]);
  const brightEnd = alphaRange('html[data-theme="wallpaper"][data-lum="bright"] body .tk-card {',
    [240, 245, 253]);
  assert.deepEqual(dark, [.60]);
  assert.deepEqual(darkMid, [.54]);
  assert.deepEqual(bright, [.72, .72, .9]);
  assert.deepEqual(brightMid, [.56]);
  assert.deepEqual(brightEnd, [.62]);

  assert.match(CSS,
    /html\[data-theme="wallpaper"\] body \.tk-card :is\(\.tk-acct, \.tk-actions, \.tk-sfoot\)[\s\S]*rgba\(255, 255, 255, \.30\)/);
  assert.match(CSS,
    /html\[data-theme="wallpaper"\] body \.tk-card \.tk-acct-note[\s\S]*opacity:\s*\.9\s*!important/);
});

test("v2.7 danger actions gain higher contrast only under wallpaper", () => {
  const dark = block('html[data-theme="wallpaper"][data-lum="dark"] body .tk-card :is(.tk-mact--danger,');
  const bright = block('html[data-theme="wallpaper"][data-lum="bright"] body .tk-card :is(.tk-mact--danger,');
  assert.match(dark, /var\(--color-danger\) 68%/);
  assert.match(bright, /#a51f35/);
  for (const css of [dark, bright]) {
    assert.match(css, /\.tk-mact--danger/);
    assert.match(css, /border-color:/);
    assert.match(css, /color:/);
  }
});

test("v2.7 no-backdrop-filter fallback uses opaque solid panels", () => {
  const at = CSS.indexOf('@supports not ((backdrop-filter: blur(1px)) or (-webkit-backdrop-filter: blur(1px))) {');
  assert.ok(at > 0);
  const start = CSS.indexOf("{", at);
  let depth = 0;
  let end = start;
  for (; end < CSS.length; end += 1) {
    if (CSS[end] === "{") depth += 1;
    if (CSS[end] === "}" && --depth === 0) break;
  }
  const fallback = CSS.slice(start, end);
  assert.match(fallback, /html\[data-theme="wallpaper"\] body :is\(\s*\.xian-stage-bay,/);
  assert.match(fallback, /background:\s*var\(--tvg-glass-panel-solid\) !important/);
  assert.match(fallback, /text-shadow:\s*none !important/);

  const darkStart = fallback.indexOf('html[data-theme="wallpaper"][data-lum="dark"] body :is(');
  assert.ok(darkStart > 0, "无滤镜回退缺少 wallpaper dark 专属实底");
  const darkStage = fallback.slice(darkStart, fallback.indexOf("}", darkStart) + 1);
  assert.match(darkStage, /\.xian-stage-bay,/);
  assert.match(darkStage, /background:\s*#121624 !important/);

  const brightStart = fallback.indexOf('html[data-theme="wallpaper"][data-lum="bright"] body :is(');
  assert.ok(brightStart > 0, "无滤镜回退缺少 wallpaper bright 专属实底");
  const brightStage = fallback.slice(brightStart, fallback.indexOf("}", brightStart) + 1);
  assert.match(brightStage, /\.xian-stage-bay,/);
  assert.match(brightStage, /background:\s*rgba\(246, 248, 252, \.98\) !important/);
  assert.match(brightStage, /color:\s*#1b2233 !important/);

  const brightPanelsStart = fallback.indexOf('html[data-theme="wallpaper"][data-lum="bright"] body :is(',
    brightStart + 1);
  assert.ok(brightPanelsStart > 0, "无滤镜回退缺少 wallpaper bright 内容面板浅底");
  const brightPanels = fallback.slice(brightPanelsStart,
    fallback.indexOf("}", brightPanelsStart) + 1);
  for (const selector of [".u-card", ".st-card", ".tk-card"]) {
    assert.ok(brightPanels.includes(selector), `bright 内容面板回退未覆盖 ${selector}`);
  }
  assert.match(brightPanels, /background:\s*rgba\(246, 248, 252, \.98\) !important/);
});

test("v2.7 wallpaper-only overrides do not change other themes", () => {
  assert.match(CSS, /html\[data-theme="wallpaper"\] body \.tk-card \{/);
  assert.match(CSS, /html\[data-theme="wallpaper"\] body \.tk-card \.tk-acct-note \{/);
  assert.ok(!/html\[data-theme="dark"\] body \.tk-card \{/.test(CSS));
  assert.ok(!/html\[data-theme="light"\] body \.tk-card \{/.test(CSS));
  assert.ok(!/:root\.liquid body \.xian-stage-bay \{/.test(CSS));
});

test("v2.7 luminance threshold is one shared constant at 140 of 255", () => {
  for (const [name, src] of [["router.js", ROUTER], ["taskedit.js", TASKEDIT]]) {
    assert.match(src, /n?\s*>=\s*140\s*\)/, `${name} 缺少统一亮度阈值 140`);
    assert.ok(!/>=\s*160/.test(src), `${name} 不应保留旧 160 阈值`);
    assert.ok(!/<=\s*120/.test(src), `${name} 不应保留旧 120 滞后阈值`);
  }
  assert.match(ROUTER, /else de\.setAttribute\("data-lum", "dark"\)/,
    "中间亮度也必须落在 bright/dark 分支，不能退回无 data-lum");
  assert.match(TASKEDIT, /else document\.documentElement\.setAttribute\("data-lum", "dark"\)/);
});

test("v2.7 official screenshot matrix exists and is non-empty", () => {
  const screenshots = [];
  for (const layout of ["desktop", "mobile"]) {
    for (const page of ["tasks", "active", "settings"]) {
      for (const wallpaper of ["dark", "bright"]) {
        screenshots.push(`docs/images/v2.7/wallpaper/${layout}/${page}-${wallpaper}.png`);
      }
    }
  }
  assert.equal(screenshots.length, 12);
  for (const relativePath of screenshots) {
    const path = new URL(relativePath, ROOT);
    assert.ok(existsSync(path), `缺少正式截图：${relativePath}`);
    assert.ok(statSync(path).size > 0, `正式截图为空：${relativePath}`);
  }
});
