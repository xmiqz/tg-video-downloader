import test from "node:test";
import assert from "node:assert/strict";
import { buildLabel } from "../../web/js/build-label.js";

test("版本与构建号齐全", () => {
  assert.equal(
    buildLabel({ version: "v2.5.0-beta.1", build: "9f3c1a7" }),
    "v2.5.0-beta.1 · build 9f3c1a7",
  );
});

test("缺少构建号只显示版本", () => {
  assert.equal(buildLabel({ version: "v2.5.0-beta.1" }), "v2.5.0-beta.1");
});

test("开发态统一显示 dev", () => {
  assert.equal(buildLabel({ version: "dev", build: "" }), "dev");
  assert.equal(buildLabel(null), "dev");
  assert.equal(buildLabel(undefined), "dev");
  assert.equal(buildLabel({}), "dev");
});

test("空白值不产生多余分隔符", () => {
  assert.equal(buildLabel({ version: "  ", build: "  " }), "dev");
  assert.equal(buildLabel({ version: "v2.5.0", build: "  " }), "v2.5.0");
});

test("dev 版本带构建号时保留构建号", () => {
  assert.equal(buildLabel({ version: "dev", build: "9f3c1a7" }), "dev · build 9f3c1a7");
});
