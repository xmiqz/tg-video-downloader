// T64b 命名区：共用组件纯函数 + 两页文案一致性。
//
// 回归红线（任务书 §2 T64b 验收）：
//   · 默认不改名；互斥两档「改名 / 更宽上下文命名」；
//   · 命名全选/全不选保留，只作用于当前可见集；
//   · 结果两态「已改名（已下载）/ 已登记（未下载）」；
//   · 与任务编辑页共用同一套文案与字段顺序（防两页漂移）；
//   · 命名整块在结果列表上方，不再留在条件区（发现 has no namewide）。
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import {
  NAME_MODES, NAMING_COPY, effectiveName, isRenameOn, nameWideParam,
  normalizeMode, participates, rowNameState, rowNameStateText, tierChanged,
} from "../../web/js/naming.js";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");

test("mode vocabulary is off / rename / wide and unknown falls back to off", () => {
  assert.deepEqual(NAME_MODES, ["off", "rename", "wide"]);
  assert.equal(normalizeMode("off"), "off");
  assert.equal(normalizeMode("rename"), "rename");
  assert.equal(normalizeMode("wide"), "wide");
  assert.equal(normalizeMode("bogus"), "off", "未知档位必须降级为不改名");
  assert.equal(normalizeMode(undefined), "off");
});

test("default mode does not rename", () => {
  assert.equal(isRenameOn("off"), false);
  assert.equal(isRenameOn("rename"), true);
  assert.equal(isRenameOn("wide"), true);
  assert.equal(participates("off", new Set(), "k", "新名字"), false, "默认不改名");
});

test("name_wide param only turns on for the wide tier (RPC 签名不变)", () => {
  assert.equal(nameWideParam("off"), 0);
  assert.equal(nameWideParam("rename"), 0);
  assert.equal(nameWideParam("wide"), 1);
  assert.equal(nameWideParam("bogus"), 0);
});

test("tier change is detected against the scanned mode", () => {
  assert.equal(tierChanged("rename", "rename"), false);
  assert.equal(tierChanged("wide", "rename"), true);
  assert.equal(tierChanged("off", "rename"), true);
  assert.equal(tierChanged("bogus", "off"), false);
});

test("effectiveName prefers inline edits, then suggestion, then empty", () => {
  const item = { rename: " 拟用新名 " };
  assert.equal(effectiveName({}, "频道|1", item), "拟用新名");
  assert.equal(effectiveName({ "频道|1": "  手动改名 " }, "频道|1", item), "手动改名");
  assert.equal(effectiveName({ "频道|1": "" }, "频道|1", item), "", "显式清空=保持原名");
  assert.equal(effectiveName(null, "频道|1", null), "");
  assert.equal(effectiveName({}, "频道|1", { rename: null }), "");
});

test("participates honours tier, per-row opt-out and empty suggestions", () => {
  const off = new Set(["频道|2"]);
  assert.equal(participates("rename", new Set(), "频道|1", "新名"), true);
  assert.equal(participates("rename", off, "频道|2", "新名"), false, "取消勾选的行不参与");
  assert.equal(participates("rename", new Set(), "频道|1", "   "), false, "空拟名不参与");
  assert.equal(participates("wide", new Set(), "频道|1", "新名"), true);
  assert.equal(participates("off", new Set(), "频道|1", "新名"), false);
});

test("row states split downloaded (renamed) from pending (recorded)", () => {
  assert.equal(rowNameState({ status: "done" }), "renamed");
  assert.equal(rowNameState({ status: "exists" }), "renamed");
  assert.equal(rowNameState({ downloaded: true }), "renamed");
  assert.equal(rowNameState({ status: "pending" }), "recorded");
  assert.equal(rowNameState({}), "recorded");
  assert.equal(rowNameStateText({ status: "done" }), "已改名（已下载）");
  assert.equal(rowNameStateText({ status: "pending" }), "已登记（未下载）");
});

test("shared copy keeps the task-book wording", () => {
  assert.equal(NAMING_COPY.toggle, "智能重命名（高级）");
  assert.equal(NAMING_COPY.tierRename, "改名");
  assert.equal(NAMING_COPY.tierWide, "更宽上下文命名");
  assert.equal(NAMING_COPY.selectAll, "命名全选");
  assert.equal(NAMING_COPY.selectNone, "命名全不选");
  assert.equal(NAMING_COPY.fieldLabel, "拟命名");
  assert.equal(NAMING_COPY.stateRenamed, "已改名（已下载）");
  assert.equal(NAMING_COPY.stateRecorded, "已登记（未下载）");
  assert.ok(NAMING_COPY.note.includes("取消勾选=使用原文件名"));
});

test("task edit page uses the same labels in the same order", () => {
  const html = readFileSync(path.join(ROOT, "web", "taskedit.html"), "utf8");
  const block = html.slice(html.indexOf('class="glass ed-rename"'));
  const idx = (s) => block.indexOf(s);
  const order = [
    NAMING_COPY.toggle,
    NAMING_COPY.selectAll,
    NAMING_COPY.selectNone,
    NAMING_COPY.tierRename,
    NAMING_COPY.tierWide,
    NAMING_COPY.note,
  ];
  let prev = -1;
  for (const label of order) {
    const at = idx(label);
    assert.ok(at >= 0, "编辑页缺少文案：" + label);
    assert.ok(at > prev, "编辑页文案顺序漂移：" + label);
    prev = at;
  }
  assert.ok(block.includes('name="ed2-rn-tier"'), "编辑页档位必须是互斥单选");
});

test("new-task page moved the naming block above the result list", () => {
  const js = readFileSync(path.join(ROOT, "web", "js", "views", "new.js"), "utf8");
  assert.ok(!js.includes("nw-namewide"), "条件区不应再出现更宽命名开关");
  assert.ok(!js.includes("refs.nameWide"), "条件区引用未清理");
  const toolbarAt = js.indexOf('class: "nw-toolbar"');
  const namingAt = js.indexOf("namingBar.el");
  const listAt = js.indexOf("\n    list,\n");
  assert.ok(toolbarAt >= 0 && namingAt > toolbarAt, "命名块必须位于工具条之后");
  assert.ok(listAt > namingAt, "命名块必须位于结果列表之前");
  assert.ok(js.includes("createNamingBar"), "必须消费共用命名组件");
});
