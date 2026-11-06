import assert from "node:assert/strict";
import { createRequire } from "node:module";
import fs from "node:fs";
import path from "node:path";

const args = Object.fromEntries(
  process.argv.slice(2).map((item) => {
    const i = item.indexOf("=");
    return i < 0 ? [item.replace(/^--/, ""), true]
      : [item.slice(2, i), item.slice(i + 1)];
  }),
);

const OUT = path.resolve(args.out || "docs/images");
const BASE = String(args["base-url"] || "").replace(/\/+$/, "");
const TOKEN = String(args.token || "");
const TASK_ID = String(args["task-id"] || "t252_active");
const QUICK = !!args.quick;
const RUNTIME = "C:/Users/xqz/.cache/codex-runtimes/codex-primary-runtime/dependencies/node";
const EDGE = "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe";
const require = createRequire(path.join(RUNTIME, "noop.cjs"));
const { chromium } = require("playwright");

if (!BASE || !TOKEN) throw new Error("需要 --base-url 与 --token");
fs.mkdirSync(OUT, { recursive: true });

const log = [];
const record = (name, ok, detail = "") => {
  log.push(`${ok ? "PASS" : "FAIL"} | ${name} | ${detail}`);
  if (!ok) throw new Error(`${name}: ${detail}`);
};
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const enc = encodeURIComponent(TOKEN);
const urlFor = (route) => `${BASE}/#${route}&t=${enc}`;

async function rpc(method, params = []) {
  const resp = await fetch(`${BASE}/rpc`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "Authorization": `Bearer ${TOKEN}`,
    },
    body: JSON.stringify({ method, params }),
  });
  const data = await resp.json().catch(() => null);
  if (!resp.ok) throw new Error(`${method}: HTTP ${resp.status} ${data && data.error || ""}`);
  return data;
}

const browser = await chromium.launch({
  executablePath: EDGE,
  headless: true,
  args: ["--disable-gpu", "--force-device-scale-factor=1"],
});
const context = await browser.newContext({
  viewport: { width: 1920, height: 1080 },
  deviceScaleFactor: 1,
  locale: "zh-CN",
});
await context.addInitScript(() => {
  // The capture harness takes steady-state frames and navigates repeatedly.
  // Let the router use its direct-swap path so a long-running View Transition
  // cannot swallow a later click while a screenshot document is settling.
  try {
    window.__nativeStartViewTransition = Document.prototype.startViewTransition;
    Object.defineProperty(Document.prototype, "startViewTransition", {
      configurable: true,
      value: undefined,
    });
    window.__restoreViewTransition = function () {
      if (window.__nativeStartViewTransition) {
        Document.prototype.startViewTransition = window.__nativeStartViewTransition;
      }
    };
  } catch (_) {}
});
const page = await context.newPage();
page.setDefaultTimeout(20000);
const pageErrors = [];
const consoleErrors = [];
const httpErrors = [];
page.on("pageerror", (err) => pageErrors.push(String(err)));
page.on("console", (msg) => {
  if (msg.type() === "error") consoleErrors.push(msg.text());
});
page.on("response", (resp) => {
  if (resp.status() >= 400) httpErrors.push(`${resp.status()} ${resp.url()}`);
});

async function hideNoise() {
  await page.evaluate(() => {
    for (const n of document.querySelectorAll(
      ".sh-booterr, .sh-guestbar, .u-toaster, .u-toast, .sh-jobdock, #sh-jobdock",
    )) n.remove();
    const root = document.getElementById("sh-root");
    if (root) root.classList.add("still");
  });
}

async function settle(extra = 650) {
  await page.waitForTimeout(extra);
  await page.evaluate(async () => {
    if (document.fonts && document.fonts.ready) await document.fonts.ready;
  });
  await hideNoise();
  await page.waitForTimeout(80);
}

async function gotoMain(route, width, height) {
  await page.setViewportSize({ width, height });
  await page.goto(urlFor(route), { waitUntil: "domcontentloaded" });
  await page.waitForFunction(
    (want) => !!window.__t8router && window.__t8router.current === want,
    route,
  );
  await settle();
}

async function navigateMain(route) {
  await page.evaluate(async (want) => {
    await window.__t8router.navigate(want, { force: true });
  }, route);
  await page.waitForFunction(
    (want) => window.__t8router && window.__t8router.current === want,
    route,
  );
  await settle();
}

async function shot(name, opts = {}) {
  const file = path.join(OUT, name);
  fs.mkdirSync(path.dirname(file), { recursive: true });
  await hideNoise();
  await page.screenshot({ path: file, fullPage: !!opts.fullPage });
  return file;
}

async function clickAppearance(label) {
  await gotoMain("/settings", 1920, 1080);
  await page.waitForFunction(
    () => document.querySelectorAll(".st-appearance .u-seg-item").length === 4,
  );
  const tab = page.locator(".st-appearance .u-seg-item", { hasText: label }).first();
  await tab.click();
  await page.waitForTimeout(450);
}

async function verifyTheme(mode) {
  return page.evaluate((want) => {
    const de = document.documentElement;
    return {
      theme: de.getAttribute("data-theme"),
      liquid: de.classList.contains("liquid"),
      backdrop: (document.getElementById("sh-backdrop")?.style.backgroundImage || ""),
    };
  }, mode);
}

async function activeThemeScreenshot(mode, name, expectedLum = "") {
  await navigateMain("/active");
  if (mode === "liquid") {
    const bg = `${BASE}/wallpaper/wallpaper-dark.jpg?t=${enc}`;
    await page.evaluate((url) => {
      document.documentElement.classList.add("liquid");
      document.documentElement.setAttribute("data-lum", "dark");
      const back = document.getElementById("sh-backdrop");
      if (back) {
        back.style.backgroundImage = `url("${url}")`;
        back.style.backgroundSize = "cover";
        back.style.backgroundPosition = "center";
      }
    }, bg);
    await page.waitForTimeout(350);
  }
  if (mode === "bing") {
    await page.waitForFunction(() =>
      (document.getElementById("sh-backdrop")?.style.backgroundImage || "").includes("/wallpaper/"),
    );
    if (expectedLum) {
      await page.waitForFunction(
        (want) => document.documentElement.getAttribute("data-lum") === want,
        expectedLum,
      );
    }
  }
  await settle(700);
  await shot(name);
}

async function captureCore(prefix, width, height) {
  await gotoMain("/tasks", width, height);
  await page.waitForSelector(".tk-card, .task-card, .u-empty");
  await shot(`${prefix}task-library.png`);

  await gotoMain("/settings", width, height);
  await page.waitForSelector(".st-card");
  await shot(`${prefix}settings.png`);
  const appearance = await page.evaluate(() => {
    const seg = document.querySelector(".st-appearance");
    const items = Array.from(document.querySelectorAll(".st-appearance .u-seg-item"));
    const box = seg?.getBoundingClientRect();
    const importBtn = Array.from(document.querySelectorAll(".tp-actions .u-btn"))
      .find((x) => x.textContent.trim() === "导入任务包");
    const heights = items.map((x) => Math.round(x.getBoundingClientRect().height * 10) / 10);
    const importHeight = importBtn
      ? Math.round(importBtn.getBoundingClientRect().height * 10) / 10
      : null;
    return {
      count: items.length,
      labels: items.map((x) => x.textContent.trim()),
      visible: items.every((x) => {
        const r = x.getBoundingClientRect();
        return r.width > 0 && r.height >= 32 && r.left >= box.left - 1 &&
          r.right <= box.right + 1;
      }),
      overflow: seg ? seg.scrollWidth > seg.clientWidth + 1 : false,
      heights,
      importHeight,
      sameHeight: importHeight !== null &&
        heights.every((h) => Math.abs(h - importHeight) <= 2),
    };
  });
  record(`appearance-visible-${width}`,
    appearance.count === 4 && appearance.visible && !appearance.overflow &&
    (width > 767 || appearance.sameHeight),
    JSON.stringify(appearance));

  await gotoMain("/tasks", width, height);
  await navigateMain("/login");
  await page.waitForSelector(".lg-login-card");
  await settle(1000);
  const login = await page.evaluate(() => {
    const card = document.querySelector(".lg-login-card");
    const scrim = getComputedStyle(document.querySelector(".lg-view"), "::before");
    const cs = getComputedStyle(card);
    return {
      url: location.href,
      cardOpacity: cs.opacity,
      cardBackground: cs.backgroundImage,
      scrim: scrim.backgroundColor,
      scrimPe: scrim.pointerEvents,
    };
  });
  record(`login-layer-${width}`,
    login.cardOpacity === "1" && login.scrim !== "rgba(0, 0, 0, 0)" &&
    login.scrimPe === "none" && /rgba?\(24, 27, 40|rgba?\(18, 21, 33/.test(login.cardBackground),
    JSON.stringify(login));
  await shot(`${prefix}login.png`);

  await gotoMain("/new", width, height);
  const clickProbe = await page.evaluate(() => {
    const btn = document.querySelector("#sh-viewhost .vw-new .nw-adv-toggle");
    const panel = document.querySelector("#sh-viewhost #nw-adv");
    let hit = 0;
    btn.addEventListener("click", () => { hit += 1; }, { once: true });
    // The new-view node is cached across routes. Leave an already-open panel
    // open instead of toggling it closed when the harness re-enters the view.
    if (panel && panel.hidden) btn.click();
    return {
      hit,
      wasHidden: panel ? panel.hidden : null,
      disabled: btn.disabled,
      countToggle: document.querySelectorAll("#sh-viewhost .vw-new .nw-adv-toggle").length,
      countPanel: document.querySelectorAll("#sh-viewhost .nw-adv").length,
    };
  });
  await page.waitForTimeout(300);
  const advanced = await page.evaluate(() => {
    const panel = document.querySelector("#sh-viewhost #nw-adv");
    const btn = document.querySelector("#sh-viewhost .nw-adv-toggle");
    return {
      route: window.__t8router.current,
      hidden: panel?.hidden,
      display: panel ? getComputedStyle(panel).display : "",
      expanded: btn?.getAttribute("aria-expanded"),
      panelConnected: !!panel?.isConnected,
    };
  });
  advanced.clickProbe = clickProbe;
  advanced.errors = pageErrors.slice(-5);
  record(`advanced-${width}`, advanced.hidden === false,
    JSON.stringify(advanced));
  await settle(450);
  if (width === 390) {
    await page.setViewportSize({ width, height: 1100 });
    await settle(250);
  }
  await shot(`${prefix}new-download.png`);
  if (width === 390) {
    await page.setViewportSize({ width, height });
    await settle(200);
  }

  await page.evaluate(() => {
    const input = document.querySelector("#sh-viewhost #nw-channel");
    input.value = "@night_channel";
    input.dispatchEvent(new Event("input", { bubbles: true }));
    document.querySelector("#sh-viewhost .nw-search-btn").click();
  });
  await page.waitForFunction(
    () => document.querySelectorAll("#sh-viewhost .vw-new .nw-row").length >= 8,
  );
  await settle(600);
  const step2 = await page.evaluate(() => ({
    rows: document.querySelectorAll("#sh-viewhost .vw-new .nw-row").length,
    step: document.querySelector("#sh-viewhost .nw-step.is-active")?.textContent || "",
    progress: document.querySelector("#sh-viewhost .nw-phase")?.textContent || "",
  }));
  record(`new-results-${width}`, step2.rows === 8, JSON.stringify(step2));
  await shot(`${prefix}new-download-results.png`);

  await page.goto(`${BASE}/taskedit.html?id=${encodeURIComponent(TASK_ID)}#t=${enc}`,
    { waitUntil: "domcontentloaded" });
  await page.waitForSelector("#ed2-rows .ed-row");
  await settle(800);
  const pathState = await page.evaluate(() => {
    const input = document.getElementById("ed2-folder");
    return {
      shown: input.value,
      full: input.dataset.fullPath || "",
      title: input.title,
      copy: !!document.getElementById("ed2-folder-copy"),
      open: !!document.getElementById("ed2-folder-open"),
    };
  });
  record(`taskedit-path-${width}`,
    pathState.copy && pathState.open && pathState.full.startsWith("T:\\TVD\\") &&
    pathState.full === pathState.title,
    JSON.stringify(pathState));
  await shot(`${prefix}task-edit.png`);
}

try {
  await gotoMain("/active", 1920, 1080);
  await page.waitForSelector(".xian-stage-bay");
  await shot("hero-dashboard.png");

  if (!QUICK) {
    const themeCases = [
      ["dark", "静态暗", "theme-dark.png"],
      ["light", "静态亮", "theme-light.png"],
      ["bing", "每日壁纸", "theme-bing.png"],
      ["liquid", "液态玻璃", "theme-liquid.png"],
    ];
    for (const [mode, label, name] of themeCases) {
      await clickAppearance(label);
      const state = await verifyTheme(mode);
      const ok = mode === "liquid"
        ? state.liquid === true
        : mode === "bing"
          ? state.theme === "wallpaper" && state.backdrop.includes("/wallpaper/")
          : state.theme === mode;
      record(`theme-${mode}`, ok, JSON.stringify(state));
      await activeThemeScreenshot(
        mode,
        name,
        mode === "bing" ? "bright" : "",
      );
      if (mode === "light") {
        await navigateMain("/login");
        await page.waitForSelector(".lg-login-card");
        await settle(900);
        const lightInk = await page.evaluate(() => {
          const card = document.querySelector(".lg-login-card");
          const title = document.querySelector(".lg-title");
          const sub = document.querySelector(".lg-sub");
          const lum = (value) => {
            const nums = String(value).match(/[\d.]+/g) || [];
            const [r = 0, g = 0, b = 0] = nums.map(Number);
            return 0.2126 * r + 0.7152 * g + 0.0722 * b;
          };
          return {
            cardBg: getComputedStyle(card).backgroundImage,
            titleColor: getComputedStyle(title).color,
            subColor: getComputedStyle(sub).color,
            titleLum: lum(getComputedStyle(title).color),
            subLum: lum(getComputedStyle(sub).color),
          };
        });
        record("login-light-1920",
          lightInk.titleLum > 190 && lightInk.subLum > 170 &&
          /rgba?\(24, 27, 40/.test(lightInk.cardBg),
          JSON.stringify(lightInk));
        await shot("login-light.png");
      }
    }

    await rpc("wallpaper_select", ["2026-11-05"]);
    await clickAppearance("每日壁纸");
    await activeThemeScreenshot("bing", "theme-bing-dark.png", "dark");
    await gotoMain("/active", 1920, 1080);
    await settle(700);
    const cdp = await context.newCDPSession(page);
    const frames = [];
    cdp.on("Page.screencastFrame", async ({ data, metadata, sessionId }) => {
      frames.push({ ts: metadata.timestamp, data });
      await cdp.send("Page.screencastFrameAck", { sessionId });
    });
    await page.evaluate(() => window.__restoreViewTransition?.());
    await cdp.send("Page.enable");
    await cdp.send("Page.startScreencast", {
      format: "png", quality: 100, everyNthFrame: 1,
    });
    await page.evaluate(() => {
      document.querySelector('.sh-side [data-nav="settings"]')?.click();
    });
    await page.screenshot({
      path: path.join(OUT, "compare", "nav-wallpaper-immediate.png"),
      animations: "allow",
    });
    await page.waitForTimeout(600);
    await cdp.send("Page.stopScreencast");
    await cdp.detach();
    const frameDir = path.join(OUT, "compare", "nav-wallpaper-frames");
    fs.mkdirSync(frameDir, { recursive: true });
    frames.forEach((frame, index) => {
      fs.writeFileSync(
        path.join(frameDir, `frame-${String(index).padStart(3, "0")}.png`),
        Buffer.from(frame.data, "base64"),
      );
    });
    await page.evaluate(() => {
      Object.defineProperty(Document.prototype, "startViewTransition", {
        configurable: true,
        value: undefined,
      });
    });
    await settle(500);
    await page.screenshot({
      path: path.join(OUT, "compare", "nav-wallpaper-settled.png"),
      animations: "allow",
    });
    await rpc("wallpaper_select", ["2026-11-06"]);

    await clickAppearance("静态暗");
    await gotoMain("/active", 1280, 800);
    await shot("widths/1280-active.png");
    await gotoMain("/active", 834, 1112);
    await shot("widths/834-active.png");
    await gotoMain("/active", 390, 844);
    await shot("widths/390-active.png");

    await gotoMain("/settings", 834, 1112);
    const tablet = await page.evaluate(() => {
      const card = document.querySelector(".st-card");
      const cs = getComputedStyle(card);
      return { filter: cs.backdropFilter || cs.webkitBackdropFilter, width: innerWidth };
    });
    record("tablet-panel-no-sampling", !String(tablet.filter).includes("blur"),
      JSON.stringify(tablet));
    await shot("tablet/settings.png");
    await gotoMain("/tasks", 834, 1112);
    await shot("tablet/tasks.png");

    await gotoMain("/settings", 390, 844);
    const bottom = await page.evaluate(() => {
      const thumb = document.querySelector(".sh-bottombar .sh-bar-thumb");
      const active = document.querySelector(".sh-bottombar .sh-nav-item.is-active");
      const cs = thumb ? getComputedStyle(thumb) : null;
      return {
        width: thumb?.getBoundingClientRect().width || 0,
        opacity: cs?.opacity || "",
        active: active?.textContent.trim() || "",
      };
    });
    record("mobile-bottom-thumb", bottom.width >= 20 && bottom.opacity === "1",
      JSON.stringify(bottom));
    await shot("compare/after-bottom-selected-390.png");
    const mobileAppearance = await page.evaluate(() => {
      const seg = document.querySelector(".st-appearance");
      const items = Array.from(document.querySelectorAll(".st-appearance .u-seg-item"));
      const box = seg.getBoundingClientRect();
      return {
        cols: getComputedStyle(seg.querySelector(".u-seg-scroll")).gridTemplateColumns,
        labels: items.map((x) => x.textContent.trim()),
        inBounds: items.every((x) => {
          const r = x.getBoundingClientRect();
          return r.right <= box.right + 1 && r.left >= box.left - 1 && r.height >= 32;
        }),
      };
    });
    record("mobile-appearance-2x2", mobileAppearance.cols.split(" ").length === 2 &&
      mobileAppearance.inBounds, JSON.stringify(mobileAppearance));
    await shot("compare/after-appearance-390.png");

    const thumbState = () => page.evaluate(() => {
      const seg = document.querySelector(".st-appearance");
      const thumb = seg?.querySelector(".u-seg-thumb");
      const active = seg?.querySelector(".u-seg-item.is-active");
      if (!thumb || !active) return null;
      const t = thumb.getBoundingClientRect();
      const a = active.getBoundingClientRect();
      return {
        dx: Math.round((t.left - a.left) * 10) / 10,
        dy: Math.round((t.top - a.top) * 10) / 10,
        dw: Math.round((t.width - a.width) * 10) / 10,
        dh: Math.round((t.height - a.height) * 10) / 10,
      };
    });
    let thumb = await thumbState();
    record("appearance-thumb-390", thumb && Math.abs(thumb.dx) <= 1.5 &&
      Math.abs(thumb.dy) <= 1.5 && Math.abs(thumb.dw) <= 1.5 &&
      Math.abs(thumb.dh) <= 1.5, JSON.stringify(thumb));
    await page.setViewportSize({ width: 834, height: 1112 });
    await page.waitForTimeout(650);
    thumb = await thumbState();
    record("appearance-thumb-reflow-834", thumb && Math.abs(thumb.dx) <= 1.5 &&
      Math.abs(thumb.dy) <= 1.5 && Math.abs(thumb.dw) <= 1.5 &&
      Math.abs(thumb.dh) <= 1.5, JSON.stringify(thumb));
    await page.setViewportSize({ width: 390, height: 844 });
    await page.waitForTimeout(650);
    thumb = await thumbState();
    record("appearance-thumb-reflow-390", thumb && Math.abs(thumb.dx) <= 1.5 &&
      Math.abs(thumb.dy) <= 1.5 && Math.abs(thumb.dw) <= 1.5 &&
      Math.abs(thumb.dh) <= 1.5, JSON.stringify(thumb));
    const bottomReflow = await page.evaluate(() => {
      const thumbEl = document.querySelector(".sh-bottombar .sh-bar-thumb");
      const active = document.querySelector(".sh-bottombar .sh-nav-item.is-active");
      if (!thumbEl || !active) return null;
      const t = thumbEl.getBoundingClientRect();
      const a = active.getBoundingClientRect();
      return {
        dx: Math.round((t.left - a.left) * 10) / 10,
        dw: Math.round((t.width - a.width) * 10) / 10,
        opacity: getComputedStyle(thumbEl).opacity,
      };
    });
    record("bottom-thumb-reflow-390",
      bottomReflow && Math.abs(bottomReflow.dx) <= 1.5 &&
      Math.abs(bottomReflow.dw) <= 1.5 && bottomReflow.opacity === "1",
      JSON.stringify(bottomReflow));

    await gotoMain("/new", 390, 844);
    await page.evaluate(() => {
      document.querySelector("#sh-viewhost .nw-adv-toggle")?.click();
    });
    const dates = await page.evaluate(() => {
      const out = [];
      for (const id of ["nw-datefrom", "nw-dateto"]) {
        const el = document.querySelector(`#sh-viewhost #${id}`);
        el.value = id.endsWith("from") ? "2026-10-01" : "2026-11-06";
        const r = el.getBoundingClientRect();
        out.push({ id, width: r.width, height: r.height, value: el.value });
      }
      return out;
    });
    record("date-390", dates.every((x) => x.width >= 180), JSON.stringify(dates));
    await shot("compare/after-date-390.png");
    const ctrl = await page.evaluate(() => {
      const cb = document.querySelector('#sh-viewhost #nw-deepscan');
      const cs = getComputedStyle(cb);
      return { appearance: cs.appearance || cs.webkitAppearance, width: cb.getBoundingClientRect().width };
    });
    record("custom-checkbox", ctrl.appearance === "none", JSON.stringify(ctrl));
  }

  await captureCore("", 1920, 1080);
  await captureCore("mobile/", 390, 844);

  const exportable = await rpc("tasks_exportable", []);
  record("tasks-exportable", exportable.ok === true && exportable.tasks.length >= 2,
    `tasks=${exportable.tasks?.length || 0}`);
  const exported = await rpc("export_tasks", [[TASK_ID], true]);
  record("tasks-export", exported.ok === true && exported.count === 1,
    JSON.stringify({ count: exported.count, filename: exported.filename }));
  const imported = await rpc("import_tasks", [exported.bundle]);
  record("tasks-import", imported.ok === true && imported.count === 1,
    JSON.stringify({ count: imported.count }));

  for (const width of [1280, 1024, 834, 390]) {
    await gotoMain("/new", width, width === 1280 ? 800 : (width === 390 ? 844 : 1112));
    await page.evaluate(() => {
      document.querySelector("#sh-viewhost .nw-adv-toggle")?.click();
    });
    const d = await page.evaluate(() => {
      const panel = document.querySelector("#sh-viewhost #nw-adv");
      if (panel) panel.hidden = false;
      const from = document.querySelector("#sh-viewhost #nw-datefrom");
      const to = document.querySelector("#sh-viewhost #nw-dateto");
      from.value = "2026-10-01"; to.value = "2026-11-06";
      const a = from.getBoundingClientRect(), b = to.getBoundingClientRect();
      return { fromW: a.width, toW: b.width, overlap: !(a.right <= b.left || b.right <= a.left || a.bottom <= b.top || b.bottom <= a.top) };
    });
    record(`date-${width}`, d.fromW >= 120 && d.toW >= 120 && !d.overlap, JSON.stringify(d));
  }

  const unexpectedHttp = httpErrors.filter((line) => !line.endsWith("/favicon.ico"));
  record("http-errors", unexpectedHttp.length === 0, unexpectedHttp.join(" ; "));
  const unexpectedConsole = consoleErrors.filter((line) => !/404/.test(line));
  record("no-page-errors", pageErrors.length === 0 && unexpectedConsole.length === 0,
    [...pageErrors, ...unexpectedConsole].join(" ; "));
  fs.writeFileSync(path.join(OUT, "smoke-v252.log"), log.join("\n") + "\n", "utf8");
  console.log(log.join("\n"));
} finally {
  await browser.close();
}
