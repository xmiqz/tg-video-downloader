import http from "node:http";
import fs from "node:fs";
import path from "node:path";
import os from "node:os";
import { spawn } from "node:child_process";

const REPO = "C:/Users/xqz/Documents/Codex/2026-11-04/tvg-v25-argon-glass-clean";
const WEB = path.join(REPO, "web");
const OUT = path.join(REPO, "evidence/v2.5.1/r9");
const EDGE = "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe";
const MIME = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".svg": "image/svg+xml",
  ".ico": "image/x-icon",
};

fs.mkdirSync(OUT, { recursive: true });

const server = http.createServer((req, res) => {
  const u = new URL(req.url, "http://x");
  let p = decodeURIComponent(u.pathname);
  if (p === "/") p = "/index.html";
  if (p === "/wallpaper/p.jpg") {
    res.writeHead(200, { "content-type": "image/svg+xml", "cache-control": "no-store" });
    res.end(`<svg xmlns="http://www.w3.org/2000/svg" width="1280" height="800">
      <defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">
        <stop stop-color="#b75f4f"/><stop offset=".42" stop-color="#d4b08c"/>
        <stop offset="1" stop-color="#29405c"/></linearGradient></defs>
      <rect width="1280" height="800" fill="url(#g)"/>
      <g fill="none" stroke="#f4c0a0" stroke-width="2" opacity=".22">
        <path d="M0 220 C260 80 410 390 700 220 S1100 60 1280 260"/>
        <path d="M0 470 C220 330 500 610 760 430 S1080 290 1280 500"/>
      </g>
    </svg>`);
    return;
  }
  const f = path.resolve(WEB, "." + p);
  if (!f.startsWith(WEB + path.sep) || !fs.existsSync(f) || !fs.statSync(f).isFile()) {
    res.writeHead(404);
    res.end("not found");
    return;
  }
  res.writeHead(200, {
    "content-type": MIME[path.extname(f)] || "application/octet-stream",
    "cache-control": "no-store",
  });
  fs.createReadStream(f).pipe(res);
});

await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
const port = server.address().port;
const profile = fs.mkdtempSync(path.join(os.tmpdir(), "tvg-edge-"));
const edge = spawn(EDGE, [
  "--headless=new",
  "--disable-gpu",
  "--remote-debugging-port=0",
  `--user-data-dir=${profile}`,
  "about:blank",
], { stdio: "ignore" });

let activePort = "";
for (let i = 0; i < 200; i++) {
  const file = path.join(profile, "DevToolsActivePort");
  if (fs.existsSync(file)) {
    activePort = fs.readFileSync(file, "utf8").split(/\r?\n/)[0];
    break;
  }
  await new Promise((resolve) => setTimeout(resolve, 50));
}

const pages = await (await fetch(`http://127.0.0.1:${activePort}/json/list`)).json();
const page = pages.find((item) => item.type === "page" && item.url === "about:blank")
  || pages.find((item) => item.type === "page");
const ws = new WebSocket(page.webSocketDebuggerUrl);
await new Promise((resolve, reject) => {
  ws.onopen = resolve;
  ws.onerror = reject;
});

let nextId = 0;
const pending = new Map();
const events = [];
ws.onmessage = (event) => {
  const message = JSON.parse(event.data);
  if (message.id && pending.has(message.id)) {
    const { resolve, reject } = pending.get(message.id);
    pending.delete(message.id);
    message.error ? reject(new Error(JSON.stringify(message.error))) : resolve(message.result);
  } else {
    events.push(message);
  }
};
const cmd = (method, params = {}) => new Promise((resolve, reject) => {
  const id = ++nextId;
  pending.set(id, { resolve, reject });
  ws.send(JSON.stringify({ id, method, params }));
});

await cmd("Page.enable");
await cmd("Runtime.enable");
await cmd("Network.enable");

const init = `(() => {
  localStorage.setItem("appearance", "bing");
  class FakeWS {
    constructor() { this.listeners = {}; }
    addEventListener(type, fn) { (this.listeners[type] || (this.listeners[type] = [])).push(fn); }
    send() {}
    close() {}
  }
  window.WebSocket = FakeWS;
  const authMode = sessionStorage.getItem("smokeAuth") !== "false";
  const data = {
    auth: authMode,
    connect: { ok: true, stage: authMode ? "ready" : "need_phone" },
    login_state: { ok: true, logged_in: authMode, guest: false, has_session: false },
    me: { ok: true, name: "Tester", username: "tester" },
    is_active: true,
    account_list: { ok: true, accounts: [{ id: "main", name: "Tester", state: "online" }] },
    list_tasks: [{
      id: "t1", title: "【二次元】白丝萝莉社", channel: "smoke", channel_title: "Smoke",
      folder: "D:/TVD/smoke", created: "2026-11-04", task_total: 12,
      task_finished: 0, task_failed: 0, status: "running", acct: "auto"
    }],
    get_settings: {
      ok: true, download_root: "D:/TVD/smoke", allowed_roots: ["D:/TVD/smoke"],
      parallel: 2, retries: 3, appearance: "bing"
    },
    get_appearance: { ok: true, mode: "bing" },
    wallpaper_status: { ok: true, current: { file: "p.jpg", lum: "dark" }, latest_date: "2026-11-04" },
    wallpaper_info: { ok: true, current: { file: "p.jpg", lum: "dark" } },
    wallpaper_history: { ok: true, items: [] },
    active_states: [{
      task_id: "t1", title: "【二次元】白丝萝莉社", phase: "downloading",
      phase_detail: "正在下载", total_bytes: 123456789, total_tot: 250000000,
      agg_speed: 666468, counts: { done: 0, skip: 0, fail: 0, active: 2, paused: 0 },
      task_total: 12, task_finished: 0, parallel: 2,
      items: [
        { src: "smoke", mid: 1, name: "786_白丝萝莉社_01.mp4", status: "下载中", pct: 15, done: 12345678, size: 82000000, speed: 308000, error: "" },
        { src: "smoke", mid: 2, name: "786_白丝萝莉社_02.mp4", status: "下载中", pct: 9, done: 9000000, size: 100000000, speed: 365000, error: "" },
        { src: "smoke", mid: 3, name: "786_白丝萝莉社_03.mp4", status: "等待中", pct: 0, done: 0, size: 68000000, speed: 0, error: "" }
      ]
    }]
  };
  window.fetch = async function (url, opts) {
    const u = String(url);
    if (u.endsWith("/auth/status")) {
      const setupPage = location.pathname === "/setup.html";
      return {
        ok: true, status: 200,
        json: async () => ({
          ok: true, authenticated: data.auth, configured: !setupPage, setup_required: setupPage,
          version: "v2.5.1", build: "r9"
        })
      };
    }
    if (u.endsWith("/rpc")) {
      let body = {};
      try { body = JSON.parse(opts.body || "{}"); } catch (e) {}
      const value = Object.prototype.hasOwnProperty.call(data, body.method)
        ? data[body.method] : { ok: true };
      return { ok: true, status: 200, json: async () => value };
    }
    return { ok: true, status: 200, json: async () => ({ ok: true }) };
  };
  window.__smokeData = data;
})();`;

await cmd("Page.addScriptToEvaluateOnNewDocument", { source: init });

const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
async function evaluate(expression) {
  const result = await cmd("Runtime.evaluate", {
    expression,
    returnByValue: true,
    awaitPromise: true,
  });
  return result.result.value;
}
async function screenshot(name, width, height, mobile = false) {
  await cmd("Emulation.setDeviceMetricsOverride", {
    width, height, deviceScaleFactor: 1, mobile,
  });
  const png = await cmd("Page.captureScreenshot", { format: "png" });
  fs.writeFileSync(path.join(OUT, name + ".png"), Buffer.from(png.data, "base64"));
}

await cmd("Emulation.setDeviceMetricsOverride", {
  width: 1280, height: 800, deviceScaleFactor: 1, mobile: false,
});
await cmd("Page.navigate", { url: `http://127.0.0.1:${port}/index.html#/active&t=t1` });
await wait(1800);
await evaluate(`document.documentElement.setAttribute('data-lum', 'dark')`);
await wait(120);
const stage = await evaluate(`(() => {
  const bay = document.querySelector('.xian-stage-bay');
  const outer = bay && bay.closest('.ac-ov-card');
  const style = bay && getComputedStyle(bay);
  return {
    theme: document.documentElement.getAttribute('data-theme'),
    lum: document.documentElement.getAttribute('data-lum'),
    stageSelectorMatches: bay?.matches('html[data-theme="wallpaper"][data-lum="dark"] body .xian-stage-bay') || false,
    parent: outer && outer.className,
    bay: !!bay,
    bayRect: bay && bay.getBoundingClientRect().toJSON(),
    background: style && style.backgroundImage,
    border: style && style.borderColor,
    filter: style && (style.backdropFilter || style.webkitBackdropFilter),
    text: document.querySelector('.ac-overview')?.innerText.slice(0, 240) || ''
  };
})()`);
await screenshot("active-wallpaper-stage-desktop", 1280, 800);

await cmd("Emulation.setDeviceMetricsOverride", {
  width: 390, height: 844, deviceScaleFactor: 1, mobile: true,
});
await cmd("Page.navigate", { url: `http://127.0.0.1:${port}/index.html#/active&t=t1` });
await wait(1600);
await screenshot("active-wallpaper-stage-mobile", 390, 844, true);

await cmd("Emulation.setDeviceMetricsOverride", {
  width: 1280, height: 900, deviceScaleFactor: 1, mobile: false,
});
await evaluate(`sessionStorage.setItem("smokeAuth", "false")`);
await cmd("Page.navigate", { url: `http://127.0.0.1:${port}/index.html?smoke=login#/login&t=t1` });
await wait(900);
await evaluate(`window.__t8router.navigate('/login', { force: true })`);
await wait(900);
const login = await evaluate(`(() => {
  const card = document.querySelector('.lg-login-card');
  const foot = document.querySelector('.lg-login-card .lg-build');
  return {
    card: card && card.getBoundingClientRect().toJSON(),
    foot: foot && foot.getBoundingClientRect().toJSON(),
    footText: foot && foot.textContent,
    inside: !!(card && foot && foot.getBoundingClientRect().bottom <= card.getBoundingClientRect().bottom + 1),
    badgeSvg: !!document.querySelector('.lg-logo-badge svg'),
    badgeSize: document.querySelector('.lg-logo-badge svg')?.getBoundingClientRect().toJSON() || null
  };
})()`);
await screenshot("login-build-footer", 1280, 900);

await cmd("Page.navigate", { url: `http://127.0.0.1:${port}/setup.html` });
await wait(1000);
const setup = await evaluate(`(() => {
  const logo = document.querySelector('.setup-logo');
  return {
    tag: logo && logo.tagName,
    src: logo && logo.getAttribute('src'),
    naturalWidth: logo && logo.naturalWidth,
    build: document.querySelector('#build-info')?.textContent || '',
    visible: !!document.querySelector('#admin-form:not([hidden])')
  };
})()`);
await screenshot("setup-shared-icon", 1280, 900);

const errors = events
  .filter((event) => event.method === "Runtime.exceptionThrown")
  .map((event) => event.params.exceptionDetails.exception?.description
    || event.params.exceptionDetails.text);
const consoleErrors = events
  .filter((event) => event.method === "Runtime.consoleAPICalled" && event.params.type === "error")
  .map((event) => event.params.args.map((arg) => arg.value || arg.description).join(" "));
const failed = events
  .filter((event) => event.method === "Network.loadingFailed")
  .map((event) => event.params.errorText);

console.log(JSON.stringify({ stage, login, setup, errors, consoleErrors, failed }, null, 2));

ws.close();
edge.kill();
server.close();
