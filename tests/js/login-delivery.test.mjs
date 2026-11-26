import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

import { resolveSendCodeOutcome } from "../../web/js/views/login.js";

const LOGIN_SRC_URL = new URL("../../web/js/views/login.js", import.meta.url);
const SETTINGS_SRC_URL = new URL("../../web/js/views/settings.js", import.meta.url);

test("send_code outcome carries the delivery notice separately from warnings", () => {
  const outcome = resolveSendCodeOutcome({
    ok: true,
    delivery: "sms",
    notice: "Telegram 本次通过短信提供验证码",
    warning: "升级重发失败",
  });
  assert.equal(outcome.step, "code");
  assert.equal(outcome.notice, "Telegram 本次通过短信提供验证码");
  assert.equal(outcome.warning, "升级重发失败");
});

test("login UI renders the returned delivery notice", async () => {
  const source = await readFile(LOGIN_SRC_URL, "utf8");
  assert.match(source, /noticeShow\(out\.notice \|\| ""\)/);
  assert.match(source, /if \(st && st\.notice\) noticeShow\(st\.notice\)/);
  assert.doesNotMatch(source, /Telegram 发来的验证码/);
});

test("secondary-account UI keeps the delivery notice visible", async () => {
  const source = await readFile(SETTINGS_SRC_URL, "utf8");
  assert.match(source, /addErr\(r\.notice \|\| r\.warning \|\| ""\)/);
});
