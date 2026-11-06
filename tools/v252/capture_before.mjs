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
const BASE = String(args["base-url"] || "").replace(/\/+$/, "");
const OUT = path.resolve(args.out || "work/v252-capture/before");
const RUNTIME = "C:/Users/xqz/.cache/codex-runtimes/codex-primary-runtime/dependencies/node";
const EDGE = "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe";
const require = createRequire(path.join(RUNTIME, "noop.cjs"));
const { chromium } = require("playwright");
if (!BASE) throw new Error("需要 --base-url");
fs.mkdirSync(OUT, { recursive: true });

const log = [];
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
  try {
    Object.defineProperty(Document.prototype, "startViewTransition", {
      configurable: true,
      value: undefined,
    });
  } catch (_) {}
});
const page = await context.newPage();
page.setDefaultTimeout(20000);

async function hideNoise() {
  await page.evaluate(() => {
    for (const n of document.querySelectorAll(
      ".sh-booterr, .sh-guestbar, .u-toaster, .u-toast, .sh-jobdock, #sh-jobdock",
    )) n.remove();
    const root = document.getElementById("sh-root");
    if (root) root.classList.add("still");
  });
}
async function settle(ms = 650) {
  await page.waitForTimeout(ms);
  await page.evaluate(async () => {
    if (document.fonts && document.fonts.ready) await document.fonts.ready;
  });
  await hideNoise();
}
async function gotoMain(route, width, height) {
  await page.setViewportSize({ width, height });
  await page.goto(`${BASE}/#${route}`, { waitUntil: "domcontentloaded" });
  await page.waitForFunction(
    (want) => !!window.__t8router && window.__t8router.current === want,
    route,
  );
  await settle();
}
async function shot(name) {
  const file = path.join(OUT, name);
  fs.mkdirSync(path.dirname(file), { recursive: true });
  await hideNoise();
  await page.screenshot({ path: file });
  log.push(`PASS | ${name}`);
}

try {
  await gotoMain("/tasks", 1920, 1080);
  await page.evaluate(async () => {
    await window.__t8router.navigate("/login", { force: true });
  });
  await page.waitForSelector(".lg-login-card");
  await settle(900);
  await shot("before-login-1920.png");

  await gotoMain("/settings", 834, 1112);
  await page.waitForSelector(".st-card");
  await shot("before-tablet-settings-834.png");

  await gotoMain("/settings", 390, 844);
  await page.waitForSelector(".st-appearance, .u-seg");
  await shot("before-appearance-390.png");
  await shot("before-bottom-selected-390.png");

  fs.writeFileSync(path.join(OUT, "smoke-before.log"), log.join("\n") + "\n", "utf8");
  console.log(log.join("\n"));
} finally {
  await browser.close();
}
