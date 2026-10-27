// Panels must be wired into the DOM, not left detached.
//
// Caught a real break: buildTaskPortPanel() created `list` (the .tp-items
// container) but never appended it to `listWrap`, so toggling the picker
// revealed an empty box while every row rendered into a detached node.
import test from "node:test";
import assert from "node:assert/strict";

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
    addEventListener() {},
    removeEventListener() {},
    appendChild(c) { this.children.push(c); return c; },
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
  const yaml = mod.nasCompose("9911", "ghcr.io/xmiqz/tg-video-downloader:v2.4.0-beta.1", "/vol1/tvg");
  const lines = yaml.split("\n");

  assert.equal(lines[0], "services:");
  // Two-space nesting under services:, four under the service key.
  assert.match(yaml, /^ {2}tg-video-downloader:$/m);
  assert.match(yaml, /^ {4}image: ghcr\.io\/xmiqz\/tg-video-downloader:v2\.4\.0-beta\.1$/m);
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
  assert.match(yaml, /: \/data$/m);
  assert.match(yaml, /read_only: true/);
  assert.match(yaml, /- ALL/);
});

