// ============================================================
// TR4 - 组件桶 S0 锁定（web/js/ui/index.js 再导出清单）
//
// 桶是 T11-T16 视图的唯一消费入口（index.js 头部 S0-17 契约）。
// 桶被删项/改名 => 消费方 import 直接 SyntaxError => 整页白屏。
// ============================================================
import test, { after } from "node:test";
import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";

import * as barrel from "../../web/js/ui/index.js";
import * as dom from "../../web/js/ui/dom.js";
import * as card from "../../web/js/ui/card.js";
import * as overlay from "../../web/js/ui/overlay.js";
import * as toast from "../../web/js/ui/toast.js";
import * as empty from "../../web/js/ui/empty.js";
import * as progress from "../../web/js/ui/progress.js";
import * as segmented from "../../web/js/ui/segmented.js";
import * as actionbar from "../../web/js/ui/actionbar.js";
import * as selection from "../../web/js/ui/selection.js";

let N = 0;
function ok(v, label) { N++; assert.ok(v, label); }
function eq(a, b, label) { N++; assert.equal(a, b, label); }
function deep(a, b, label) { N++; assert.deepEqual(a, b, label); }

// 契约清单（与 index.js 头部 S0-17 逐字对应；新增组件必须同步本表）
const EXPECTED = {
  "dom.js": ["el", "append"],
  "card.js": ["createCard"],
  "overlay.js": ["createOverlay", "resolveOverlayForm", "OVERLAY_FORM_BREAKPOINT"],
  "toast.js": ["createToaster", "planToastStack", "resolveToastTiming"],
  "empty.js": ["createEmptyState"],
  "progress.js": ["createProgress"],
  "segmented.js": ["createSegmented", "resolveSegmentedValue", "nextSegmentIndex", "clampScrollLeft"],
  "actionbar.js": ["createBottomActionBar"],
  "selection.js": ["createSelectionController"],
};

const MODULES = { "dom.js": dom, "card.js": card, "overlay.js": overlay, "toast.js": toast,
  "empty.js": empty, "progress.js": progress, "segmented.js": segmented,
  "actionbar.js": actionbar, "selection.js": selection };

const sorted = (a) => a.slice().sort();

test("TR4-A 桶导出键集合 = 子模块命名导出并集（不增不减）", () => {
  const union = [];
  for (const [file, names] of Object.entries(EXPECTED)) {
    const actual = Object.keys(MODULES[file]).sort();
    deep(actual, sorted(names), file + " 命名导出与契约表一致");
    union.push(...names);
  }
  const barrelKeys = Object.keys(barrel).sort();
  deep(barrelKeys, sorted(union), "桶导出集合恰等于九个子模块命名导出并集");
  eq(barrelKeys.length, 17, "S0-17：桶共 17 个命名导出");
});

test("TR4-B 形状：唯一常量 + 其余全为函数，无 default", () => {
  ok(!Object.prototype.hasOwnProperty.call(barrel, "default"), "桶无 default 导出（零默认导出）");
  eq(barrel.OVERLAY_FORM_BREAKPOINT, overlay.OVERLAY_FORM_BREAKPOINT, "断点常量与源模块同值");
  eq(typeof barrel.OVERLAY_FORM_BREAKPOINT, "number", "OVERLAY_FORM_BREAKPOINT 为 number");
  ok(Number.isFinite(barrel.OVERLAY_FORM_BREAKPOINT) && barrel.OVERLAY_FORM_BREAKPOINT > 0,
    "断点为正有限数");
  for (const k of Object.keys(barrel)) {
    if (k === "OVERLAY_FORM_BREAKPOINT") continue;
    eq(typeof barrel[k], "function", k + " 为 function");
    eq(barrel[k], MODULES[Object.keys(EXPECTED).find((f) => EXPECTED[f].includes(k))][k],
      k + " 为同一引用（纯再导出，未包壳）");
  }
});

test("TR4-C 消费方导入名 100% 命中桶（防桶被改坏）", () => {
  const dirs = ["views", "."];
  const base = new URL("../../web/js/", import.meta.url);
  let checked = 0;
  for (const dir of dirs) {
    const d = new URL(dir === "." ? "" : dir + "/", base);
    for (const f of readdirSync(d)) {
      if (!f.endsWith(".js")) continue;
      const src = readFileSync(new URL(f, d), "utf8");
      // 抓 `import { a, b as c } from "<...>/ui/index.js";`（支持多行）
      const re = /import\s*\{([\s\S]*?)\}\s*from\s*"([^"]*ui\/index\.js)"/g;
      let m;
      while ((m = re.exec(src))) {
        const names = m[1].split(",").map((s) => s.trim().split(/\s+as\s+/)[0].trim()).filter(Boolean);
        for (const n of names) {
          ok(Object.prototype.hasOwnProperty.call(barrel, n),
            dir + "/" + f + " 导入的 " + n + " 必须由桶导出");
          checked++;
        }
      }
    }
  }
  ok(checked >= 7, "至少检查到 7 个桶导入名（active 7 件）实际 " + checked);
});

test("TR4-D 桶无顶层 DOM 副作用（Node 直接 import 即证）", () => {
  eq(typeof globalThis.document, "undefined", "import 后无全局 document");
  eq(typeof globalThis.window, "undefined", "import 后无全局 window");
  const src = readFileSync(new URL("../../web/js/ui/index.js", import.meta.url), "utf8");
  ok(!/^\s*(const|let|var)\s+\w+\s*=\s*(document|window)\b/m.test(src), "桶源码无顶层 DOM 取值");
  ok(!/^\s*(document|window)\./m.test(src), "桶源码无顶层 DOM 调用");
});

after(() => {
  console.log("TR4 结论：PASS（" + N + " 项）");
});