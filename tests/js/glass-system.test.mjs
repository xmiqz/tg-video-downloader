import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const ROOT = new URL("../../", import.meta.url);
const read = (name) => readFileSync(new URL(name, ROOT), "utf8");

const CSS = read("web/css/glass-system.css");
const ROUTER = read("web/js/router.js");
const TASKEDIT = read("web/taskedit.html");
const TASKEDIT_JS = read("web/js/taskedit.js");
const SETUP = read("web/setup.html");
const PACKAGE = JSON.parse(read("package.json"));

test("glass system: four semantic tiers are present", () => {
  for (const tier of ["nav", "panel", "overlay", "inset"]) {
    assert.match(CSS, new RegExp(`--tvg-glass-${tier}`));
  }
  assert.match(CSS, /--font-ui/);
  assert.match(CSS, /backdrop-filter:\s*var\(--tvg-glass-panel-filter\)/);
  assert.match(CSS, /backdrop-filter:\s*none\s*!important/);
});

test("glass system: all production entry points load it last", () => {
  assert.match(ROUTER, /css\/glass-system\.css/);
  assert.match(TASKEDIT, /css\/glass-system\.css/);
  assert.match(SETUP, /\/css\/glass-system\.css/);
  assert.doesNotMatch(ROUTER + TASKEDIT + SETUP, /argon-glass-experiment/);
});

test("glass system: wallpaper text gets a reading surface and no halo", () => {
  assert.match(CSS, /html\[data-theme="wallpaper"\][\s\S]*text-shadow:\s*none\s*!important/);
  assert.match(CSS, /html\[data-theme="wallpaper"\][\s\S]*background:\s*var\(--tvg-glass-panel-bg\)\s*!important/);
  assert.doesNotMatch(CSS, /color-mix\(in srgb, var\(--color-surface\) 86%/,
    "壁纸 panel 不能被一个接近实色的表面盖掉玻璃材质");
});

test("glass system: wallpaper and liquid panels follow background luminance", () => {
  assert.match(CSS, /:root\[data-theme="wallpaper"\]\[data-lum="bright"\][\s\S]*--tvg-glass-panel-bg:/);
  assert.match(CSS, /:root\[data-theme="wallpaper"\][\s\S]*--tvg-glass-panel-bg:/);
  assert.match(CSS, /:root\.liquid\[data-lum="bright"\][\s\S]*--tvg-glass-panel-bg:/);
});

test("glass system: download stage is a panel, not a matte inset", () => {
  const panel = CSS.slice(CSS.indexOf("/* panel:"),
    CSS.indexOf("html body :is(.lg-login-card, .lg-card)"));
  const inset = CSS.slice(CSS.indexOf("/* inset:"), CSS.indexOf("/* liquid"));
  assert.match(panel, /\.xian-stage-bay/,
    "舞台外壳必须使用 panel 材质，保证壁纸下可读性");
  assert.doesNotMatch(inset, /\.xian-stage-bay/,
    "舞台外壳不能继续落在 inset 哑光槽");
  assert.match(CSS,
    /html\[data-theme="wallpaper"\] body :is\([\s\S]*?\.xian-stage-bay/,
    "壁纸主题必须显式覆盖舞台 panel");
  assert.match(CSS,
    /html\[data-theme="wallpaper"\]\[data-lum="dark"\] body \.xian-stage-bay[\s\S]*rgba\(8, 10, 18, \.42\)/,
    "暗壁纸下舞台需要独立阅读底，不能只靠透明 panel");
});

test("glass system: account footer does not add a second nav glass sheet", () => {
  assert.doesNotMatch(CSS, /:is\(\.sh-side, \.sh-drawer, \.sh-bar-mat, \.sh-me\)/);
  assert.match(CSS, /html body \.sh-me[\s\S]{0,260}backdrop-filter:\s*none\s*!important/);
});

test("glass system: desktop panels do not sample the sidebar hover highlight", () => {
  assert.match(CSS,
    /@media \(min-width: 1024px\)[\s\S]*--tvg-glass-panel-filter:\s*saturate\(165%\) contrast\(1\.06\)/,
    "桌面 panel 必须保留 v2.5.0 的无空间采样护栏");
});

test("glass system: controls reuse nav glass and expose shared sizes", () => {
  assert.match(CSS, /--tvg-glass-control-bg:\s*var\(--tvg-glass-nav-bg\)/);
  assert.match(CSS, /--tvg-glass-control-filter:/);
  for (const size of ["xs", "sm", "md", "lg"]) {
    assert.match(CSS, new RegExp(`--tvg-control-h-${size}:`));
  }
  assert.match(CSS, /\.u-btn:not\(\.u-btn--primary\):not\(\.u-btn--danger\)/);
  assert.match(CSS, /\.btn:not\(\.primary\):not\(\.danger\)/);
  assert.match(CSS, /html body \.u-seg[\s\S]*background:\s*var\(--tvg-glass-control-bg\)/);
  assert.match(CSS, /html body \.u-seg-thumb[\s\S]*var\(--tvg-thumb-bg\)\s*padding-box/);
  assert.match(CSS, /input\[type="text"\][\s\S]*select,[\s\S]*min-height:\s*var\(--tvg-control-h-md\)/);
});

test("glass system: selected segment uses a neutral liquid-glass ring", () => {
  assert.match(CSS, /--tvg-thumb-frame:\s*linear-gradient/);
  assert.match(CSS, /html body \.u-seg-thumb[\s\S]*var\(--tvg-thumb-frame\)\s*border-box/);
  assert.match(CSS, /html body \.u-seg-thumb[\s\S]*backdrop-filter:\s*blur\(3px\)/);
  assert.match(CSS, /html body \.u-seg-thumb[\s\S]*inset 0 0 0 1px rgba\(255, 255, 255, \.12\)/);
  assert.doesNotMatch(CSS, /--tvg-glass-iridescence/,
    "彩虹不能泄漏到 panel/control/seg");
});

test("glass system: standalone task editor uses the shared control language", () => {
  assert.match(TASKEDIT, /class="seg-thumb"/, "任务编辑缺少分段选中 thumb");
  assert.match(TASKEDIT, /href="css\/tokens\.css"[\s\S]{0,120}href="css\/taskedit\.css"/,
    "任务编辑必须先加载共享 token");
  assert.match(CSS, /html body \.segmented\s*\{[\s\S]*--tvg-glass-control-bg/,
    "任务编辑分段轨道未接入 control 材质");
  assert.match(CSS, /html body \.seg-thumb\s*\{[\s\S]*--tvg-thumb-frame/,
    "任务编辑分段 thumb 未接入共享镜片");
  assert.match(TASKEDIT_JS, /function edSyncFilterThumb\(\)/,
    "任务编辑缺少 thumb 布局同步");
  assert.match(TASKEDIT_JS, /if \(!w \|\| !h\)[\s\S]{0,180}classList\.remove\("is-on"\)/,
    "任务编辑首屏零尺寸时不得显示 thumb");
  assert.match(CSS, /\.bulk-bar[\s\S]{0,120}--tvg-glass-overlay-bg/,
    "任务编辑批量条未进入 overlay");
});

test("glass system: release version is 2.5.1", () => {
  assert.equal(PACKAGE.version, "2.5.1");
});
