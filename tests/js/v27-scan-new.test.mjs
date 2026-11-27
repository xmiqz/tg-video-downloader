// v2.7 T1 · 扫新重构：tasks 走预填、new 消费令牌、in_task 默认不选、
// 编辑页独立「追加扫描」。纯源码/纯函数断言，不引入 DOM 测试依赖。
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import {
  countInTask, defaultSelection, mergeScanParams,
} from "../../web/js/views/new.js";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
const read = (rel) => readFileSync(path.join(ROOT, rel), "utf8");

test("tasks scan-new prefills the new-download view without append or polling", () => {
  const js = read("web/js/views/tasks.js");
  const fn = js.slice(js.indexOf("async function scanNewTask"));
  assert.match(fn, /api\.scan_prefill\(id\)/);
  assert.match(fn, /window\.__tvdScanPrefill\s*=\s*r/);
  assert.match(fn, /navigate\("\/new"\)/);
  assert.match(fn, /r\.need_login/);
  assert.match(fn, /r\.busy/);
  assert.doesNotMatch(fn, /scan_append/);
  assert.doesNotMatch(fn, /job_status/);
  assert.equal((js.match(/text: "扫新"/g) || []).length, 2);
});

test("new view consumes prefill once and clears the window handoff", () => {
  const js = read("web/js/views/new.js");
  assert.match(js, /window\.__tvdScanPrefill/);
  assert.match(js, /delete window\.__tvdScanPrefill/);
  assert.match(js, /refs\.dateFrom\.value\s*=\s*String\(p\.date_from \|\| src\.date_from \|\| ""\)/);
  assert.match(js, /refs\.modeSeg\.set\(opts\.mode\)[\s\S]*refs\.scopeSeg\.set\(opts\.scope\)[\s\S]*refs\.sortSeg\.set\(opts\.sort\)/);
  assert.match(js, /已在任务中的条目会自动跳过/);
  assert.match(js, /api\.scan_release\(token\)/);
});

test("scan token, min_id and date_from reach preview; plain new stays clean", () => {
  const base = {
    channel: "@demo", query: "", mode: "all", scope: "channel", sort: "date",
    date_from: "2026-11-01",
  };
  const merged = mergeScanParams(base, {
    token: "tok", taskId: "t1", baseTitle: "base", scanDate: "2026-11-27",
    minId: 12345,
  });
  assert.equal(merged.scan_token, "tok");
  assert.equal(merged.scan_task_id, "t1");
  assert.equal(merged.scan_base_title, "base");
  assert.equal(merged.scan_date, "2026-11-27");
  assert.equal(merged.min_id, 12345);
  assert.equal(merged.date_from, "2026-11-01");
  assert.deepEqual(mergeScanParams(base, null), base);

  const js = read("web/js/views/new.js");
  assert.match(js, /r0\.params\s*=\s*mergeScanParams\(r0\.params, scanFlow\)/);
  assert.match(js, /api\.preview\(r0\.params\)/);
});

test("in_task rows are not selected by default but stay clickable", () => {
  const items = [
    { src: "频道", mid: 1, name: "old", in_task: true },
    { src: "频道", mid: 2, name: "new" },
  ];
  assert.deepEqual(defaultSelection(items), ["频道|2"]);
  assert.equal(countInTask(items), 1);
  assert.equal(countInTask(items, { in_task_count: 3 }), 3);
  assert.equal(countInTask(items, { in_task_count: 0 }), 0);

  const js = read("web/js/views/new.js");
  assert.match(js, /class: "nw-row" \+ \(it\.in_task \? " is-in-task" : ""\)/);
  assert.match(js, /text: "已在任务"/);
  assert.match(js, /onclick: function \(\) \{ toggleVideo\(k\); \}/);
  assert.match(js, /selected = new Set\(defaultSelection\(previewData\.items\)\)/);
  assert.match(js, /已跳过 " \+ skipped \+ " 条已在任务/);

  const onSearchItems = js.slice(
    js.indexOf("function onSearchItems("),
    js.indexOf("function finalizeSearch("));
  assert.doesNotMatch(onSearchItems, /selected\.add\(/,
    "新到达行必须保持手动勾选语义，不得自动加入 selected");
});

test("start_download carries scan as the ninth argument only in scan flow", () => {
  const js = read("web/js/views/new.js");
  const call = js.slice(js.indexOf("api.start_download("), js.indexOf("} catch (e) {", js.indexOf("api.start_download(")));
  assert.match(call, /scanFlow \? \{/);
  assert.match(call, /token: scanFlow\.token/);
  assert.match(call, /task_id: scanFlow\.taskId/);
  assert.match(call, /base_title: scanFlow\.baseTitle/);
  assert.match(call, /scan_date: scanFlow\.scanDate/);
  assert.match(call, /folder: scanFlow\.folder/);
  assert.match(call, /params: Object\.assign\(\{\}, previewData\.params \|\| \{\}\)/,
    "scan 必须携带完整搜索参数副本供后端建任务");
  assert.match(call, /: undefined\)/);
  assert.match(js, /skipped_in_task/);
  assert.match(js, /已创建新任务/);

  const doStart = js.slice(js.indexOf("async function doStart()"));
  const catchBranch = doStart.slice(
    doStart.indexOf("} catch (e) {"),
    doStart.indexOf("if (!r || !r.ok)"));
  assert.match(catchBranch, /releaseScanFlow\(\)/,
    "start_download 异常时后端可能已释放锁，前端必须同步清理失效 token");
  const failedBranch = doStart.slice(
    doStart.indexOf("if (!r || !r.ok) {"),
    doStart.indexOf("restoreBtn(); // 成功路径"));
  assert.match(failedBranch, /toast\("error", r\.error \|\| "开始下载失败"\)/);
  assert.match(failedBranch, /releaseScanFlow\(\)/,
    "start_download 失败时后端可能已释放锁，前端必须同步清理失效 token");
});

test("both editors expose the separate append scan action", () => {
  const appEditor = read("web/js/views/editor.js");
  assert.match(appEditor, /text: "追加扫描"/);
  assert.match(appEditor, /api\.scan_append\(\s*state\.taskId, \{ incremental: true \}\)/);
  assert.doesNotMatch(appEditor, /追加扫描[\s\S]{0,200}扫新/);
  const html = read("web/taskedit.html");
  const standalone = read("web/js/taskedit.js");
  assert.match(html, /onclick="edAppendScan\(this\)">追加扫描/);
  assert.match(standalone, /async function edAppendScan\(btn\)/);
  assert.match(standalone, /api\(\)\.scan_append\(TASK_ID, \{ incremental: true \}\)/);
});

test("new.css marks in-task rows without blocking interaction", () => {
  const css = read("web/css/views/new.css");
  assert.match(css, /\.nw-row\.is-in-task \{[^}]*border-style: dashed/);
  assert.match(css, /\.nw-row\.is-in-task:not\(\.is-sel\)/);
  assert.match(css, /\.nw-intask \{[^}]*display: inline-block/);
  assert.match(css, /\.nw-intask \{[^}]*color: var\(--color-text-muted\)/);
});
