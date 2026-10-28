// Panels must be wired into the DOM, not left detached.
//
// Caught a real break: buildTaskPortPanel() created `list` (the .tp-items
// container) but never appended it to `listWrap`, so toggling the picker
// revealed an empty box while every row rendered into a detached node.
import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

function mkNode(tag) {
  const node = {
    nodeType: 1,
    tagName: String(tag || "div").toUpperCase(),
    children: [],
    style: {},
    dataset: {},
    _attrs: {},
    textContent: "",
    innerHTML: "",
    hidden: false,
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    setAttribute(k, v) { this._attrs[k] = v; if (k === "hidden") this.hidden = true; },
    getAttribute(k) { return this._attrs[k]; },
    removeAttribute(k) { delete this._attrs[k]; if (k === "hidden") this.hidden = false; },
    _listeners: {},
    addEventListener(type, fn) {
      (this._listeners[type] = this._listeners[type] || []).push(fn);
    },
    removeEventListener(type, fn) {
      const list = this._listeners[type] || [];
      const i = list.indexOf(fn);
      if (i >= 0) list.splice(i, 1);
    },
    appendChild(c) { c.parentNode = this; this.children.push(c); return c; },
    removeChild(c) {
      const i = this.children.indexOf(c);
      if (i >= 0) this.children.splice(i, 1);
      c.parentNode = null;
      return c;
    },
    append(...cs) { cs.forEach((c) => this.children.push(c)); },
    insertBefore(c) { this.children.push(c); return c; },
    remove() {},
    replaceChild() {},
    querySelector() { return null; },
    querySelectorAll() { return []; },
    closest() { return null; },
    focus() {},
    click() {},
    scrollIntoView() {},
    get firstChild() { return this.children[0] || null; },
  };
  return node;
}

function installDom() {
  const doc = {
    createElement: mkNode,
    createTextNode: (t) => ({ nodeType: 3, textContent: t }),
    documentElement: mkNode("html"),
    body: mkNode("body"),
    getElementById: () => null,
    addEventListener() {},
    removeEventListener() {},
    querySelector() { return null; },
    querySelectorAll() { return []; },
  };
  globalThis.document = doc;
  globalThis.window = {
    addEventListener() {},
    removeEventListener() {},
    matchMedia: () => ({ matches: false, addEventListener() {} }),
    localStorage: { getItem: () => null, setItem() {} },
    pywebview: { api: {} },
    document: doc,
  };
  globalThis.localStorage = globalThis.window.localStorage;
  globalThis.CustomEvent = class { constructor(t, i) { this.type = t; this.detail = i && i.detail; } };
  globalThis.Event = class { constructor(t) { this.type = t; } };
}

// Walk the built tree and collect every node by class name.
function collect(node, out) {
  if (!node) return out;
  const cls = (node._attrs && node._attrs.class) || node.className || "";
  if (cls) out.push({ node, cls: String(cls) });
  for (const c of node.children || []) collect(c, out);
  return out;
}

test("task export panel wires .tp-items into .tp-list", async () => {
  installDom();
  const mod = await import("../../web/js/views/settings.js");
  const root = mod.mount();
  const view = root && root.el ? root.el : root;
  const nodes = collect(view, []);
  const hasList = nodes.some((n) => n.cls.split(" ").includes("tp-list"));
  const hasItems = nodes.some((n) => n.cls.split(" ").includes("tp-items"));
  assert.ok(hasList, "缺少 .tp-list");
  assert.ok(hasItems, ".tp-items 不在 DOM 树里（列表会是空的）");
});

test("task export actions wait for the bridge instead of racing it", async () => {
  const src = await readFile(
    new URL("../../web/js/views/settings.js", import.meta.url), "utf8");
  for (const name of [
    "toggleTaskPicker",
    "exportSelectedTasks",
    "importTaskBundle",
  ]) {
    const start = src.indexOf(`async function ${name}`);
    assert.ok(start > 0, `缺少 ${name}`);
    const block = src.slice(start, src.indexOf("\n}", start) + 2);
    assert.match(block, /await ensureApi\(\)/,
      `${name} 必须先等待 pywebviewready，不能直接读桥`);
  }
});

test("wallpaper panel wires .wp-hist-list into .wp-hist", async () => {
  installDom();
  const mod = await import("../../web/js/views/settings.js");
  const root = mod.mount();
  const view = root && root.el ? root.el : root;
  const nodes = collect(view, []);
  const hasHist = nodes.some((n) => n.cls.split(" ").includes("wp-hist"));
  const hasList = nodes.some((n) => n.cls.split(" ").includes("wp-hist-list"));
  assert.ok(hasHist, "缺少 .wp-hist");
  assert.ok(hasList, ".wp-hist-list 不在 DOM 树里（历史会是空的）");
});

// The NAS deploy card hands the user a compose.yaml to paste verbatim, so the
// generator's indentation IS the contract: a stray space yields
// "yaml: line N: did not find expected key" on their NAS.
test("nasCompose emits valid, parseable compose structure", async () => {
  installDom();
  const mod = await import("../../web/js/views/settings.js");
  const yaml = mod.nasCompose("9911", "ghcr.io/xmiqz/tg-video-downloader:v2.4.0-beta.2", "/vol1/tvg");
  const lines = yaml.split("\n");

  assert.equal(lines[0], "services:");
  // Two-space nesting under services:, four under the service key.
  assert.match(yaml, /^ {2}tg-video-downloader:$/m);
  assert.match(yaml, /^ {4}image: ghcr\.io\/xmiqz\/tg-video-downloader:v2\.4\.0-beta\.2$/m);
  // The port the user typed must reach the published mapping, not a default.
  assert.match(yaml, /^ {6}- "9911:8787"$/m);
  // Data dir override must land in the volumes list.
  assert.match(yaml, /^ {6}- \/vol1\/tvg:\/data$/m);

  // No tabs anywhere, no trailing whitespace, exactly one document start.
  assert.ok(!yaml.includes("\t"), "compose yaml must not contain tabs");
  assert.equal(lines.filter((l) => l.trimEnd() !== l).length, 0,
    "compose yaml must not carry trailing whitespace");
  assert.equal(lines.filter((l) => l === "services:").length, 1);

  // Indentation must only ever step by two spaces.
  for (const line of lines) {
    if (!line.trim()) continue;
    const indent = line.length - line.trimStart().length;
    assert.equal(indent % 2, 0, "indent not a multiple of 2: " + JSON.stringify(line));
  }
});

test("nasCompose defaults are usable without any input", async () => {
  installDom();
  const mod = await import("../../web/js/views/settings.js");
  const yaml = mod.nasCompose();
  assert.match(yaml, /8787:8787/);
  assert.match(yaml, /read_only: true/);
  assert.match(yaml, /- ALL/);
});

// A host-created bind mount is root:root 0755 while the container runs as
// uid 10001 with cap_drop ALL: it cannot create /data/downloads there and
// dies at boot with "Permission denied: '/data/downloads'". The default must
// therefore be a named volume, which inherits /data's owner from the image.
test("nasCompose defaults to a named volume, not a host bind mount", async () => {
  installDom();
  const mod = await import("../../web/js/views/settings.js");
  const yaml = mod.nasCompose();

  assert.match(yaml, /^ {6}- tgvd-data:\/data$/m, "默认应为命名卷");
  assert.ok(!/^ {6}- \.\/data:\/data$/m.test(yaml), "默认不应生成绑定挂载");
  assert.match(yaml, /^volumes:$/m, "缺少顶层 volumes 声明");
  assert.match(yaml, /^ {2}tgvd-data:$/m);
  assert.match(yaml, /^ {4}name: tg-video-downloader-data$/m);
});

test("nasCompose still honours an explicit host path as a bind mount", async () => {
  installDom();
  const mod = await import("../../web/js/views/settings.js");
  const yaml = mod.nasCompose("8787", "", "/vol1/1000/docker/tvg");

  assert.match(yaml, /^ {6}- \/vol1\/1000\/docker\/tvg:\/data$/m,
    "显式路径应生成绑定挂载");
  assert.ok(!/tgvd-data:\/data/.test(yaml), "显式路径时不应再用命名卷");
});

// WebView2 refuses navigator.clipboard.writeText, so the copy buttons must go
// through execCommand on a scratch textarea first. If that path regresses the
// NAS card silently copies nothing, which is exactly what shipped once.
test("nas copy buttons use execCommand before the clipboard API", async () => {
  installDom();
  const mod = await import("../../web/js/views/settings.js");

  let execCalls = 0;
  let clipboardCalls = 0;
  let execResult = true;
  const scratchLog = [];

  const realCreate = globalThis.document.createElement;
  globalThis.document.createElement = function (tag) {
    const n = realCreate(tag);
    if (String(tag).toLowerCase() === "textarea") {
      n.select = () => scratchLog.push("select");
      n.setSelectionRange = () => scratchLog.push("range");
    }
    return n;
  };
  globalThis.document.body.appendChild = function (n) {
    n.parentNode = globalThis.document.body;
    return n;
  };
  globalThis.document.body.removeChild = function (n) { n.parentNode = null; };
  globalThis.document.execCommand = function (cmd) {
    if (cmd !== "copy") return false;
    execCalls += 1;
    return execResult;
  };
  Object.defineProperty(globalThis, "navigator", {
    configurable: true,
    value: { clipboard: { writeText: async () => { clipboardCalls += 1; } } },
  });

  const root = mod.mount();
  const nodes = collect(root && root.el ? root.el : root, []);
  const byText = (label) => nodes.find((n) => n.node.textContent === label);
  const copyYaml = byText("复制 compose.yaml");
  assert.ok(copyYaml, "缺少「复制 compose.yaml」按钮");

  const handlers = copyYaml.node._listeners && copyYaml.node._listeners.click;
  assert.ok(Array.isArray(handlers) && handlers.length, "复制按钮未绑定 click");

  await handlers[0]();

  assert.equal(execCalls, 1, "应优先走 execCommand");
  assert.equal(clipboardCalls, 0, "execCommand 成功时不应再调 clipboard API");
  assert.deepEqual(scratchLog, ["select", "range"], "应选中临时 textarea");
});

test("nas copy falls back to clipboard API when execCommand refuses", async () => {
  installDom();
  const mod = await import("../../web/js/views/settings.js");

  let clipboardCalls = 0;
  globalThis.document.execCommand = () => false;
  Object.defineProperty(globalThis, "navigator", {
    configurable: true,
    value: { clipboard: { writeText: async () => { clipboardCalls += 1; } } },
  });

  const root = mod.mount();
  const nodes = collect(root && root.el ? root.el : root, []);
  const copyCmd = nodes.find((n) => n.node.textContent === "复制启动命令");
  assert.ok(copyCmd, "缺少「复制启动命令」按钮");
  const handlers = copyCmd.node._listeners.click;
  await handlers[0]();

  assert.equal(clipboardCalls, 1, "execCommand 失败时应回落 clipboard API");
});

