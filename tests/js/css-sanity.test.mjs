// CSS sanity: every stylesheet must have balanced comment markers and braces.
//
// Caught a real break: an unterminated /* comment in shell.css swallowed the
// three `html[data-theme="wallpaper"]` rules, so the wallpaper theme set the
// attribute but the backdrop stayed display:none. Browsers silently ignore
// the swallowed rules, so nothing in the console pointed at the cause.
import test from "node:test";
import assert from "node:assert/strict";
import { readFile, readdir } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const CSS_DIR = path.join(ROOT, "web", "css");

async function walk(dir) {
  const out = [];
  for (const entry of await readdir(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) out.push(...(await walk(full)));
    else if (entry.name.endsWith(".css")) out.push(full);
  }
  return out;
}

function stripStrings(css) {
  return css.replace(/"(?:\\.|[^"\\])*"/g, '""')
            .replace(/'(?:\\.|[^'\\])*'/g, "''");
}

function countCommentOpens(css) {
  const m = stripStrings(css).match(/\/\*/g);
  return m ? m.length : 0;
}

function countCommentCloses(css) {
  const m = stripStrings(css).match(/\*\//g);
  return m ? m.length : 0;
}

test("every css file has balanced /* */ comments", async () => {
  const files = await walk(CSS_DIR);
  assert.ok(files.length > 0);
  const broken = [];
  for (const file of files) {
    const css = await readFile(file, "utf8");
    const opens = countCommentOpens(css);
    const closes = countCommentCloses(css);
    if (opens !== closes) {
      broken.push(`${path.relative(ROOT, file)}: ${opens} /* vs ${closes} */`);
    }
  }
  assert.deepEqual(broken, [], "CSS 注释未闭合：" + broken.join("; "));
});

test("every css file has balanced braces outside comments", async () => {
  const files = await walk(CSS_DIR);
  const broken = [];
  for (const file of files) {
    const css = await readFile(file, "utf8");
    // strip comments then strings, then count braces
    const noComments = stripStrings(css).replace(/\/\*[\s\S]*?\*\//g, "");
    let depth = 0;
    let minDepth = 0;
    for (const ch of noComments) {
      if (ch === "{") depth++;
      else if (ch === "}") { depth--; if (depth < minDepth) minDepth = depth; }
    }
    if (depth !== 0 || minDepth < 0) {
      broken.push(`${path.relative(ROOT, file)}: depth=${depth} min=${minDepth}`);
    }
  }
  assert.deepEqual(broken, [], "CSS 花括号不平衡：" + broken.join("; "));
});

test("wallpaper theme rules are actually parsed (not inside a comment)", async () => {
  const css = await readFile(path.join(CSS_DIR, "shell.css"), "utf8");
  const noComments = css.replace(/\/\*[\s\S]*?\*\//g, "");
  assert.match(
    noComments,
    /html\[data-theme="wallpaper"\] #sh-backdrop\s*\{\s*display:\s*block/,
    'shell.css 的 wallpaper 规则被注释吞掉了',
  );
});

test("task editor uses one scroll chain on desktop and mobile", async () => {
  const css = (await readFile(path.join(CSS_DIR, "taskedit.css"), "utf8"))
    .replace(/\/\*[\s\S]*?\*\//g, "");

  const appRule = css.match(/\.editor-app\s*\{([^}]*)\}/)?.[1] ?? "";
  assert.match(appRule, /overflow-y\s*:\s*auto/,
    "编辑器外层必须承接滚轮，不能只在表格区域滚动");
  assert.match(appRule, /overscroll-behavior-y\s*:\s*contain/,
    "编辑器滚动不应带动外层页面");

  const mainRule = css.match(/\.editor-main\s*\{([^}]*)\}/)?.[1] ?? "";
  assert.match(mainRule, /flex\s*:\s*none/,
    "编辑表格应随页面自然流高，不能再次形成桌面专用内滚层");
  assert.match(mainRule, /overflow\s*:\s*visible/,
    "桌面表格容器不能吞掉鼠标滚轮");
});
