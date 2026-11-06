import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import { readFile } from "node:fs/promises";
import path from "node:path";
import test from "node:test";
import { fileURLToPath, pathToFileURL } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const read = (name) => readFile(path.join(ROOT, name), "utf8");

test("v2.5.2 login has an independent scrim and a dense reading card", async () => {
  const css = await read("web/css/views/login.css");
  assert.match(css, /\.lg-view::before\s*\{[\s\S]*rgba\(4, 7, 14, \.64\)/,
    "登录需要独立全屏 scrim");
  assert.match(css, /\.lg-view::before\s*\{[\s\S]*pointer-events:\s*none/,
    "scrim 不得接收业务点击");
  assert.match(css,
    /html body \.lg-view \.u-card\.lg-login-card[\s\S]*rgba\(24, 27, 40, \.82\)[\s\S]*blur\(34px\)/,
    "登录卡需要跨主题的 v2.5.1 移动端毛玻璃卡面");
  assert.match(css,
    /html body \.lg-view \.u-card\.lg-login-card[\s\S]*--color-text:\s*#f2f5ff\s*!important[\s\S]*--color-text-strong:\s*#ffffff\s*!important/,
    "静态亮下登录卡正文必须固定浅墨，不能继承亮主题深墨");
});

test("v2.5.2 tablet and desktop share the no-sampling panel guard", async () => {
  const shell = await read("web/css/shell.css");
  const glass = await read("web/css/glass-system.css");
  assert.match(shell,
    /@media \(min-width: 768px\)[\s\S]{0,260}\.tk-card,[\s\S]{0,160}backdrop-filter:\s*none\s*!important/,
    "768 起卡片不得再实时采样背景");
  assert.match(glass,
    /@media \(min-width: 768px\)[\s\S]*--tvg-glass-panel-filter:\s*saturate\(165%\) contrast\(1\.06\)/,
    "玻璃系统同步覆盖平板断点");
});

test("v2.5.2 mobile appearance is a fixed 2x2 grid", async () => {
  const css = await read("web/css/views/settings.css");
  const js = await read("web/js/views/settings.js");
  assert.match(js, /appearanceSeg\.el\.classList\.add\("st-appearance"\)/);
  assert.match(js,
    /const fresh = createSegmented\(appearanceSegs\(HOST_MODE\)[\s\S]{0,180}fresh\.el\.classList\.add\("st-appearance"\)/,
    "宿主模式变化后重建的外观仍须保留 2×2 钩子");
  assert.match(css,
    /\.st-appearance \.u-seg-scroll\s*\{[\s\S]*grid-template-columns:\s*repeat\(2, minmax\(0, 1fr\)\)/,
    "移动外观必须 2×2，不靠无提示横滑");
  assert.match(css,
    /html body \.st-appearance\.u-seg[\s\S]*background:\s*transparent\s*!important/,
    "移动外观外层不再是一整块胶囊");
  assert.match(css,
    /\.st-appearance \.u-seg-item\s*\{[\s\S]*min-height:\s*39px[\s\S]*border-radius:\s*var\(--radius-pill\)/,
    "移动外观四项应与任务导入按钮同级的紧凑胶囊");
  assert.match(css, /\.st-appearance \.u-seg-item\s*\{[\s\S]*white-space:\s*normal/);
});

test("v2.5.2 selected thumbs keep a minimum contrast ring", async () => {
  const shell = await read("web/css/shell.css");
  const glass = await read("web/css/glass-system.css");
  assert.match(shell,
    /\.sh-bar-thumb[\s\S]*color-mix\(in srgb, var\(--color-primary\) 52%/,
    "底栏选中 thumb 需要主色对比环");
  assert.match(shell,
    /\.sh-bar-thumb[\s\S]*box-sizing:\s*border-box/,
    "底栏选中 thumb 的边框不得叠加在测量宽高之外");
  assert.match(glass,
    /html body \.u-seg-thumb[\s\S]*color-mix\(in srgb, var\(--color-primary\) 58%/,
    "分段/外观 thumb 需要同源对比环");
});

test("v2.5.2 checkbox and radio share all five states", async () => {
  const css = await read("web/css/glass-system.css");
  assert.match(css, /appearance:\s*none/);
  assert.match(css, /input\[type="checkbox"\]:checked,/);
  assert.match(css, /input\[type="checkbox"\]:focus-visible,/);
  assert.match(css, /input\[type="checkbox"\]:disabled,/);
  assert.match(css, /input\[type="checkbox"\]:not\(:disabled\):hover/);
  assert.match(css, /input\[type="radio"\]::before/);
});

test("v2.5.2 date range owns enough width and stacks on phones", async () => {
  const js = await read("web/js/views/new.js");
  const css = await read("web/css/views/new.css");
  assert.match(js, /class: "nw-dual nw-dual--date"/);
  assert.match(css, /\.nw-field\.nw-field--date\s*\{\s*flex:\s*1 1 340px/);
  assert.match(css,
    /@media \(max-width: 520px\)[\s\S]*\.nw-dual--date\s*\{\s*grid-template-columns:\s*1fr/,
    "窄屏日期字段必须纵向排列");
});

test("v2.5.2 wallpaper stage and content panels own a reading base", async () => {
  const css = await read("web/css/glass-system.css");
  const tokens = await read("web/css/tokens.css");
  assert.match(css,
    /html\[data-theme="wallpaper"\] body \.xian-stage-bay[\s\S]*var\(--color-surface\) 96%/,
    "壁纸舞台需要 token 匹配阅读底");
  assert.match(css,
    /html\[data-theme="wallpaper"\] body :is\([\s\S]*\.xian-stage-card-name/,
    "任务名需要稳定文字层级");
  assert.match(css,
    /html\[data-theme="wallpaper"\]\[data-lum="bright"\] body :is\([\s\S]*rgba\(255, 255, 255, \.78\)/,
    "亮壁纸内容面板需要白阅读底");
  assert.match(tokens,
    /:root\[data-theme="wallpaper"\]\[data-lum="dark"\],[\s\S]*--raw-ink:\s*#f5f7fe/,
    "暗壁纸必须把整组 raw ink 切回浅墨，不能沿用静态亮的深墨");
});

test("v2.5.2 long paths elide in the middle and expose copy/open", async () => {
  await import(pathToFileURL(path.join(ROOT, "web/js/path-format.js")).href);
  const api = globalThis.TVGPath;
  assert.ok(api, "path-format.js 应暴露 TVGPath");
  const long = "D:\\TVD\\深夜频道\\2026\\11\\06\\最终话-超长文件名.mp4";
  const shown = api.elideMiddle(long, 24);
  assert.ok(shown.startsWith("D:\\"));
  assert.ok(shown.includes("…"));
  assert.ok(shown.endsWith("文件名.mp4"));
  assert.equal(api.elideMiddle("D:\\TVD\\深夜频道", 24), "D:\\TVD\\深夜频道");

  const html = await read("web/taskedit.html");
  const js = await read("web/js/taskedit.js");
  assert.match(html, /id="ed2-folder-copy"[\s\S]*edCopyFolder\(\)/);
  assert.match(html, /id="ed2-folder-open"[\s\S]*edOpenFolder\(\)/);
  assert.match(js, /window\.TVGPath\.fullPath\(input\)/);
  assert.match(js,
    /mode === "bing" \|\| mode === "wallpaper"[\s\S]*root\.setAttribute\("data-theme", "dark"\)/,
    "任务编辑页静态暗必须显式落在 dark 令牌，不能回落到浅色默认");
});

test("v2.5.2 public screenshot matrix is present and README stays neutral", async () => {
  for (const rel of [
    "docs/images/theme-dark.png",
    "docs/images/theme-light.png",
    "docs/images/theme-bing.png",
    "docs/images/theme-liquid.png",
    "docs/images/mobile/settings.png",
    "docs/images/tablet/settings.png",
    "docs/images/compare/login-1920-before-after.png",
  ]) {
    assert.ok(existsSync(path.join(ROOT, rel)), `缺少发布截图 ${rel}`);
  }
  assert.ok(!existsSync(path.join(ROOT, "docs/images/wallpaper-stage.png")),
    "含义不明的 wallpaper-stage.png 必须删除");

  const readme = await read("README.md");
  assert.doesNotMatch(readme, /C:\\Users|xqz|worktree|evidence\/|192\.168\.|测试服务器/i,
    "README 不得包含个人目录、worktree、evidence 路径或测试服务器信息");
  assert.match(readme, /docs\/images\/theme-dark\.png/,
    "README 需要引用四主题截图矩阵");
});
