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

test("settings no longer renders an in-app NAS deploy card", async () => {
  installDom();
  const mod = await import("../../web/js/views/settings.js");
  const root = mod.mount();
  const nodes = collect(root && root.el ? root.el : root, []);
  const hasNas = nodes.some((n) => n.cls.split(" ").includes("st-nas"));
  assert.equal(hasNas, false,
    "NAS 部署说明应放在 GitHub README，而不是程序设置页");
});

