// ============================================================
// TR5 - views/active.js 纯决策函数（18 件 5A seam）
// 模块顶层零 DOM/window，Node 可直接 import。
// ============================================================
import test, { after } from "node:test";
import assert from "node:assert/strict";
import * as A from "../../web/js/views/active.js";

let N = 0;
function ok(v, label) { N++; assert.ok(v, label); }
function eq(a, b, label) { N++; assert.equal(a, b, label); }
function deep(a, b, label) { N++; assert.deepEqual(a, b, label); }

// ---------------------------------------------------------- S1 卡片键
test("TR5-A cardKeyOf / parseCardKey 往返（含含竖线的 src）", () => {
  eq(A.cardKeyOf("t1", "s1", 7), "t1|||s1|7", "cardKeyOf 格式 tid|||src|mid");
  deep(A.parseCardKey("t1|||s1|7"), { tid: "t1", src: "s1", mid: 7 }, "往返还原三字段");
  eq(A.cardKeyOf(1, "x", "3"), "1|||x|3", "tid/src 转字符串、mid 转 Number");

  // src 自身含 "|"：parseCardKey 取最后一个 | 作为 mid 分隔
  const ck = A.cardKeyOf("t2", "a|b|c", 4);
  deep(A.parseCardKey(ck), { tid: "t2", src: "a|b|c", mid: 4 }, "src 含竖线仍可解析");

  eq(A.parseCardKey("no-separator"), null, "缺 ||| 返回 null");
  eq(A.parseCardKey("t|||s"), null, "缺 mid 分隔返回 null");
  eq(A.parseCardKey(null), null, "null 入参返回 null");
  eq(A.parseCardKey(undefined), null, "undefined 入参返回 null");
  eq(A.parseCardKey(""), null, "空串返回 null");
  eq(A.parseCardKey(12345), null, "非字符串入参返回 null（String 后无分隔）");
});

test("TR5-B activeBadgeClass 六状态 + 未知兜底", () => {
  const cases = [
    ["完成", "done"], ["已存在", "done"], ["失败", "fail"],
    ["已暂停", "paused"], ["等待中", "wait"], ["下载中", "run"],
  ];
  for (const [k, v] of cases) eq(A.activeBadgeClass(k), v, k + " -> " + v);
  for (const k of ["已跳过", "未知态", "", null, undefined, 0]) {
    eq(A.activeBadgeClass(k), "wait", "未映射 " + JSON.stringify(k) + " -> wait");
  }
});

// ---------------------------------------------------------- S2 总览聚合
test("TR5-C overviewAggregate 累加 / pct / eta / fetchDetails 去重", () => {
  const empty = A.overviewAggregate([]);
  deep(empty, {
    totBytes: 0, totSize: 0, speed: 0, remaining: 0, pct: 0, etaSec: null,
    cc: { done: 0, skip: 0, fail: 0, active: 0, paused: 0 }, fetchDetails: [],
  }, "空数组零值、etaSec=null");

  deep(A.overviewAggregate(null), A.overviewAggregate([]), "非数组等同空数组");
  deep(A.overviewAggregate(undefined), A.overviewAggregate([]), "undefined 等同空数组");

  const states = [
    { total_bytes: 100, total_tot: 400, agg_speed: 50, counts: { done: 1, skip: 0, fail: 0, active: 1, paused: 0 },
      phase: "fetching", phase_detail: "抓取元数据" },
    { total_bytes: 100, total_tot: 400, agg_speed: 50, counts: { done: 0, skip: 1, fail: 1, active: 0, paused: 1 },
      phase: "fetching", phase_detail: "抓取元数据" }, // 重复 detail
    null, // 容错
    { total_bytes: "x", total_tot: undefined, agg_speed: null }, // 脏值 -> 0
  ];
  const r = A.overviewAggregate(states);
  eq(r.totBytes, 200, "totBytes 累加");
  eq(r.totSize, 800, "totSize 累加");
  eq(r.speed, 100, "speed 累加");
  eq(r.remaining, 600, "remaining = totSize - totBytes");
  eq(r.pct, 25, "pct = round(200/800*100)");
  eq(r.etaSec, 6, "etaSec = remaining / speed");
  deep(r.cc, { done: 1, skip: 1, fail: 1, active: 1, paused: 1 }, "counts 五类累加");
  deep(r.fetchDetails, ["抓取元数据"], "fetching 阶段 detail 去重");

  // 边界：超量（totBytes > totSize）时 remaining 夹 0、eta null
  const over = A.overviewAggregate([{ total_bytes: 900, total_tot: 100, agg_speed: 10 }]);
  eq(over.remaining, 0, "剩余量不为负");
  eq(over.etaSec, null, "无剩余 -> eta null");
  eq(over.pct, 900, "pct 不做 100 上限夹取（既有语义：仅进度条侧夹）");

  // 边界：有剩余但速度为 0 -> eta null
  const stalled = A.overviewAggregate([{ total_bytes: 0, total_tot: 100, agg_speed: 0 }]);
  eq(stalled.remaining, 100, "有剩余");
  eq(stalled.etaSec, null, "速度为 0 -> eta null");
});

test("TR5-D 文案函数逐字", () => {
  eq(A.overviewDetailText([{ counts: { done: 2, skip: 1, fail: 3, active: 4, paused: 5 } }]),
    "完成 2 · 跳过 1 · 失败 3 · 进行 4 · 暂停 5", "overviewDetailText");
  eq(A.overviewDetailText([]), "完成 0 · 跳过 0 · 失败 0 · 进行 0 · 暂停 0", "空态文案");
  eq(A.overviewSubText(3), "3 个任务下载中，按任务分组显示", "overviewSubText");
  eq(A.groupStatText({ task_finished: 2, task_total: 5 }), "2/5 完成", "groupStatText");
  eq(A.groupStatText({}), "0/0 完成", "groupStatText 缺字段");
  eq(A.groupStatText(null), "0/0 完成", "groupStatText null");
  eq(A.selCountText(7), "已选 7", "selCountText");
  eq(A.removeConfirmText(2), "从任务中移除选中的 2 个条目？本地文件不会删除。", "removeConfirmText");
  eq(A.abortConfirmText(), "中止该任务的全部下载？\n未完成视频状态保留，可随时继续。", "abortConfirmText 含换行");
  const busy = {
    pause: "暂停中…", skip: "跳过中…", remove: "移除中…", resume: "恢复中…",
    abort: "正在中止…", resumeskip: "恢复跳过中…",
  };
  for (const k of Object.keys(busy)) eq(A.busyLabelFor(k), busy[k], "busyLabelFor " + k);
  eq(A.busyLabelFor("nope"), "处理中…", "busyLabelFor 兜底");
  eq(A.busyLabelFor(undefined), "处理中…", "busyLabelFor undefined 兜底");
});

test("TR5-E itemButtonsFor 状态动作矩阵", () => {
  deep(A.itemButtonsFor("已暂停"), ["resume", "remove"], "已暂停");
  deep(A.itemButtonsFor("失败"), ["resume", "remove"], "失败");
  deep(A.itemButtonsFor("已跳过"), ["resume", "remove"], "已跳过（C9 行内恢复）");
  deep(A.itemButtonsFor("完成"), ["remove"], "完成");
  deep(A.itemButtonsFor("已存在"), ["remove"], "已存在");
  deep(A.itemButtonsFor("下载中"), ["pause", "skip", "remove"], "下载中");
  deep(A.itemButtonsFor("等待中"), ["pause", "skip", "remove"], "等待中");
  for (const k of ["未知", "", null, undefined]) deep(A.itemButtonsFor(k), [], "未知态 " + JSON.stringify(k));
});

// ---------------------------------------------------------- S5/S6/S7
test("TR5-F bulkGroupKeys 按任务分组 + 坏键跳过", () => {
  const out = A.bulkGroupKeys(["t1|||a|1", "t1|||b|2", "t2|||c|3", "bad", null]);
  deep(out, { t1: [{ src: "a", mid: 1 }, { src: "b", mid: 2 }], t2: [{ src: "c", mid: 3 }] },
    "按 tid 分组、坏键跳过");
  deep(A.bulkGroupKeys([]), {}, "空数组 -> {}");
  deep(A.bulkGroupKeys(null), {}, "非数组 -> {}");
});

test("TR5-G pendReleaseRule 单键释放规则（Map / plain object）", () => {
  const ck = "t|||s|1";
  const mk = (status) => new Map([[ck, { status }]]);
  const obj = (status) => ({ [ck]: { status } });

  // 缺失项：除 resume 外一律可释放
  eq(A.pendReleaseRule(ck + ":pause", new Map()), true, "pause 缺失 -> 释放");
  eq(A.pendReleaseRule(ck + ":skip", new Map()), true, "skip 缺失 -> 释放");
  eq(A.pendReleaseRule(ck + ":remove", new Map()), true, "remove 缺失 -> 释放");
  eq(A.pendReleaseRule(ck + ":resume", new Map()), true, "resume 缺失 -> 释放");

  eq(A.pendReleaseRule(ck + ":pause", mk("已暂停")), true, "pause 已暂停 -> 释放");
  eq(A.pendReleaseRule(ck + ":pause", mk("下载中")), false, "pause 仍在下载 -> 保持");
  eq(A.pendReleaseRule(ck + ":skip", mk("已跳过")), true, "skip 已跳过 -> 释放");
  eq(A.pendReleaseRule(ck + ":skip", mk("下载中")), false, "skip 未跳过 -> 保持");
  eq(A.pendReleaseRule(ck + ":remove", mk("下载中")), false, "remove 项仍在 -> 保持");

  // resume 反向语义：项仍处于暂停/失败/跳过 -> 保持；否则释放
  for (const s of ["已暂停", "失败", "已跳过"]) {
    eq(A.pendReleaseRule(ck + ":resume", mk(s)), false, "resume 项仍 " + s + " -> 保持");
    eq(A.pendReleaseRule(ck + ":resume", obj(s)), false, "plain object 同语义 " + s);
  }
  for (const s of ["下载中", "完成", "等待中"]) {
    eq(A.pendReleaseRule(ck + ":resume", mk(s)), true, "resume 项已 " + s + " -> 释放");
  }

  eq(A.pendReleaseRule(ck + ":bogus", mk("下载中")), false, "未知 action -> false");
  eq(A.pendReleaseRule("nocolon", new Map()), false, "无冒号 -> false");
  // 用最后一个冒号切分（src 含冒号不影响）
  const ck2 = "t|||http://x|1";
  eq(A.pendReleaseRule(ck2 + ":pause", new Map([[ck2, { status: "已暂停" }]])), true,
    "按最后一个冒号切分 action");
});

test("TR5-H planReconcile 四类键（abort / resumeskip / bulk / ck）", () => {
  deep(A.planReconcile([], []), [], "空 entries -> []");
  deep(A.planReconcile(null, null), [], "非数组 -> []");

  const states = [
    { task_id: "t1", phase: "aborted", items: [{ src: "a", mid: 1, status: "已暂停" }] },
    { task_id: "t2", phase: "running", items: [{ src: "b", mid: 2, status: "下载中" }] },
  ];

  // abort：任务不存在或已 aborted -> 释放
  deep(A.planReconcile([{ key: "abort:t1", ctx: { tid: "t1" } },
                        { key: "abort:t2", ctx: { tid: "t2" } },
                        { key: "abort:t9", ctx: { tid: "t9" } }], states),
    ["abort:t1", "abort:t9"], "abort 命中 aborted 或任务消失");

  // resumeskip：任务不存在 -> 不释放；existedBefore=false -> 释放
  deep(A.planReconcile([{ key: "resumeskip:t1", ctx: { tid: "t1", existedBefore: false } },
                        { key: "resumeskip:t9", ctx: { tid: "t9", existedBefore: false } }], states),
    ["resumeskip:t1"], "任务消失不释放；新出现的任务直接释放");

  // resumeskip + resumeCks：全部不再是「已跳过」才释放
  deep(A.planReconcile([
    { key: "resumeskip:t1", ctx: { tid: "t1", existedBefore: true, resumeCks: ["t1|||a|1"] } },
  ], states), ["resumeskip:t1"], "resumeCks 项状态已离开跳过 -> 释放");
  deep(A.planReconcile([
    { key: "resumeskip:t1", ctx: { tid: "t1", existedBefore: true, resumeCks: ["t1|||a|1", "t1|||z|9"] } },
  ], [{ task_id: "t1", phase: "running", items: [{ src: "a", mid: 1, status: "已跳过" }] }]),
    [], "任一仍为已跳过 -> 保持");
  deep(A.planReconcile([
    { key: "resumeskip:t1", ctx: { tid: "t1", existedBefore: true } },
  ], states), [], "existedBefore 且无 resumeCks -> 不释放");

  // bulk：所有子键都满足释放规则才释放
  const byCkStates = [
    { task_id: "t1", phase: "running", items: [
      { src: "a", mid: 1, status: "已暂停" },
      { src: "b", mid: 2, status: "下载中" },
    ] },
  ];
  deep(A.planReconcile([{ key: "bulk:pause", ctx: { keys: ["t1|||a|1:pause"] } }], byCkStates),
    ["bulk:pause"], "bulk 全部子键释放 -> 释放");
  deep(A.planReconcile([{ key: "bulk:pause", ctx: { keys: ["t1|||a|1:pause", "t1|||b|2:pause"] } }], byCkStates),
    [], "bulk 有子键未释放 -> 保持");
  deep(A.planReconcile([{ key: "bulk:pause", ctx: { keys: [] } }], byCkStates),
    [], "bulk 空 keys -> 不释放");

  // 单项 ck
  deep(A.planReconcile([{ key: "t1|||a|1:pause", ctx: { ck: "t1|||a|1" } }], byCkStates),
    ["t1|||a|1:pause"], "单项 ck 命中 -> 释放");

  // 无 ctx 的键不参与
  deep(A.planReconcile([{ key: "mystery", ctx: {} }], byCkStates), [], "未知键不释放");
  deep(A.planReconcile([null, undefined], byCkStates), [], "空条目容错");
});

// ---------------------------------------------------------- S8/S9/S10
test("TR5-I classifyDlPhase 帧分类与骨架组", () => {
  const invalid = { valid: false, phase: "", text: "", index: null, total: null, terminal: false, buildSkeleton: false };
  deep(A.classifyDlPhase(null), invalid, "null 无效");
  deep(A.classifyDlPhase(undefined), invalid, "undefined 无效");
  deep(A.classifyDlPhase("str"), invalid, "字符串无效");
  deep(A.classifyDlPhase({}), invalid, "缺 task_id 无效");
  deep(A.classifyDlPhase({ task_id: 1 }), invalid, "task_id 非 string 无效");

  const fetching = A.classifyDlPhase({ task_id: "t1", phase: "fetching", phase_detail: "抓取元数据", phase_index: 1, phase_total: 3 });
  deep(fetching, { valid: true, phase: "fetching", text: "抓取元数据", index: 1, total: 3, terminal: false, buildSkeleton: true },
    "早期阶段 -> 建骨架组");

  for (const phase of ["done", "aborted", "error"]) {
    const r = A.classifyDlPhase({ task_id: "t1", phase });
    eq(r.terminal, true, phase + " 为终态");
    eq(r.buildSkeleton, false, phase + " 终态不建组（墓碑快照轮询自会建）");
    eq(r.valid, true, phase + " 仍为有效帧");
  }

  const noPhase = A.classifyDlPhase({ task_id: "t1" });
  eq(noPhase.valid, true, "有 task_id 即有效");
  eq(noPhase.buildSkeleton, false, "无 phase 不建骨架组");
  eq(noPhase.text, "", "无 phase_detail -> 空串");

  // index/total 归一：仅有限正数保留，其余 null
  const norm = A.classifyDlPhase({ task_id: "t1", phase: "x", phase_index: "2", phase_total: "0" });
  eq(norm.index, 2, "字符串正数转数值");
  eq(norm.total, null, "0 归一为 null");
  const neg = A.classifyDlPhase({ task_id: "t1", phase: "x", phase_index: -1, phase_total: "abc" });
  eq(neg.index, null, "负数 -> null");
  eq(neg.total, null, "非数 -> null");
});

test("TR5-J classifyDownloadJob 仅 download 终态触发补拉", () => {
  deep(A.classifyDownloadJob(null), { kind: "ignore" }, "null -> ignore");
  deep(A.classifyDownloadJob({}), { kind: "ignore" }, "缺 job_id -> ignore");
  deep(A.classifyDownloadJob({ job_id: 1, kind: "download", state: "done" }), { kind: "ignore" },
    "job_id 非 string -> ignore");
  deep(A.classifyDownloadJob({ job_id: "j", kind: "export", state: "done" }), { kind: "ignore" },
    "非 download 类 -> ignore");
  for (const s of ["done", "error", "cancelled"]) {
    deep(A.classifyDownloadJob({ job_id: "j", kind: "download", state: s }), { kind: "refresh" },
      "download " + s + " -> refresh");
  }
  for (const s of ["running", "queued", "cancelling", "", undefined]) {
    deep(A.classifyDownloadJob({ job_id: "j", kind: "download", state: s }), { kind: "ignore" },
      "download " + JSON.stringify(s) + " 非终态 -> ignore");
  }
});

test("TR5-K normAcctHint 嵌套 / 扁平 / 缺 account_id", () => {
  eq(A.normAcctHint(null), null, "null -> null");
  eq(A.normAcctHint(undefined), null, "undefined -> null");
  eq(A.normAcctHint({}), null, "缺 account_id -> null");
  eq(A.normAcctHint({ reason: "flood_wait" }), null, "只有 reason 无 account_id -> null");

  deep(A.normAcctHint({ account_id: "main", reason: "flood_wait" }),
    { account_id: "main", reason: "flood_wait", has_alt: null }, "T52 扁平形状");
  deep(A.normAcctHint({ data: { from_acct: "alt1", reason: "slow", has_alt: true }, account_id: "main" }),
    { account_id: "alt1", reason: "slow", has_alt: true }, "T51 嵌套形状优先取 data.from_acct");
  deep(A.normAcctHint({ data: { has_alt: false }, account_id: "main" }),
    { account_id: "main", reason: "", has_alt: false }, "data 仅含 has_alt 时回退扁平 account_id");
  deep(A.normAcctHint({ data: "not-an-object", account_id: "main" }),
    { account_id: "main", reason: "", has_alt: null }, "data 非对象按空处理");
  deep(A.normAcctHint({ data: { from_acct: "" }, account_id: "main" }),
    { account_id: "main", reason: "", has_alt: null }, "空 from_acct 回退扁平");
  deep(A.normAcctHint({ data: { from_acct: "x", has_alt: "yes" } }),
    { account_id: "x", reason: "", has_alt: null }, "非 boolean has_alt 归一 null");
});

test("TR5-L 模块形状与零副作用", () => {
  eq(A.path, "active", "path 导出");
  eq(A.title, "下载中 - TG 视频下载器", "title 导出");
  for (const n of ["mount", "onEnter", "onLeave"]) ok(typeof A[n] === "function", n + " 为函数");
  eq(typeof globalThis.window, "undefined", "import 后未创建全局 window（顶层零副作用）");
});

after(() => {
  console.log("TR5 结论：PASS（" + N + " 项）");
});