import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

const SETUP_JS = new URL("../../web/js/setup.js", import.meta.url);
const LOGIN_JS = new URL("../../web/js/views/login.js", import.meta.url);
const SETUP_HTML = new URL("../../web/setup.html", import.meta.url);
const RPC_TEST = new URL("../../tests/test_v1_rpc.py", import.meta.url);

test("v2.6 setup wizard starts QR login and displays notices", async () => {
  const source = await readFile(SETUP_JS, "utf8");
  assert.match(source, /qr_login_start/);
  assert.match(source, /qr_login_poll/);
  assert.match(source, /if \(result\.notice\) showNotice\(result\.notice\)/);
  assert.doesNotMatch(source, /data-next="phone"/);
});

test("v2.6 setup page never reloads itself on RPC 401", async () => {
  // /login 的 401 必须回到管理员登录表单；同一页面 location.replace("/login")
  // 会整页重载并把用户扔回欢迎步骤，形成初始化循环。
  const source = await readFile(SETUP_JS, "utf8");
  assert.match(source, /if \(resp\.status === 401\) \{\s*\n\s*if \(location\.pathname !== "\/login"\) location\.replace\("\/login"\);\s*\n\s*setPhase\("login"\);/);
});

test("new browser must log in before the Telegram setup wizard", async () => {
  // 已登录的服务：未认证浏览器访问 /login 必须停在管理员密码表单，
  // 不能直接进入“准备你的下载服务 / API 凭据”步骤。
  const source = await readFile(SETUP_JS, "utf8");
  assert.doesNotMatch(source, /canStartTelegramSetup/);
  assert.match(source, /if \(status\.authenticated\) \{[\s\S]{0,200}setupMode = false;\s*\n\s*setPhase\("login"\);/);
});

test("v2.6 login view exposes QR login", async () => {
  const source = await readFile(LOGIN_JS, "utf8");
  assert.match(source, /扫码登录（推荐）/);
  assert.match(source, /qr_login_start/);
  assert.match(source, /need_password/);
});

test("v2.6 QR placeholder is revealed once the SVG is injected", async () => {
  const source = await readFile(LOGIN_JS, "utf8");
  // The placeholder is created hidden; it must be explicitly revealed or the
  // injected SVG never renders (the 2.6 manual-test failure).
  assert.match(source, /\$\.qrImage\.innerHTML = result\.svg \|\| "";/);
  assert.match(source, /setStep\("qr"\);\s*\n\s*if \(\$\.qrImage\) \$\.qrImage\.hidden = false;/);
  assert.match(source, /st\.qr && st\.qr\.active[\s\S]{0,400}\$\.qrImage\.hidden = false/);
  assert.match(source, /stage === "qr_waiting"[\s\S]{0,400}\$\.qrImage\.hidden = false/);
});

test("v2.6 setup HTML no longer claims code always arrives in the app", async () => {
  const source = await readFile(SETUP_HTML, "utf8");
  assert.match(source, /id="qr-step"/);
  assert.doesNotMatch(source, /验证码会发送到 Telegram App/);
});

test("RPC whitelist includes QR login methods", async () => {
  const source = await readFile(RPC_TEST, "utf8");
  for (const method of [
    "qr_login_start", "qr_login_status", "qr_login_poll",
    "qr_login_cancel", "qr_login_password",
  ]) {
    assert.match(source, new RegExp(method));
  }
});
