// ============================================================
// naming.js — T64b 命名区共用模块（新建任务视图 + 任务编辑视图同源）
//
// 所有权：本文件 + 两个消费方（views/new.js、taskedit.js）。
// 目标（任务书 §2 T64b）：
//   · 命名整块从条件区搬到结果列表上方（工具条下一行）；
//   · 默认不改名；互斥两档「改名 / 更宽上下文命名」；
//   · 与任务编辑页共用同一套文案与字段顺序；
//   · 保留命名全选 / 命名全不选；结果两态「已改名（已下载）/
//     已登记（未下载）」。
//
// 纪律：
//   - 纯函数 + 文案常量在本模块；DOM 构建只经注入的 el()，node 测试
//     只消费纯函数（顶层零 DOM / window 访问，import 即证）。
//   - 档位语义单一映射：off→不参与命名；rename→按窄上下文拟名；
//     wide→按更宽上下文拟名（搜索参数 name_wide=1）。
//   - 不改任何 RPC 签名：档位只并入既有 preview 参数 name_wide。
// ============================================================

// 两页同源文案（逐字对照任务编辑页 taskedit.html）
export const NAMING_COPY = {
  toggle: "智能重命名（高级）",
  tierRename: "改名",
  tierWide: "更宽上下文命名",
  selectAll: "命名全选",
  selectNone: "命名全不选",
  fieldLabel: "拟命名",
  placeholderKeep: "留空=保持原名",
  stateRenamed: "已改名（已下载）",
  stateRecorded: "已登记（未下载）",
  note: "勾选=参与命名；取消勾选=使用原文件名；未下载条目下载完成即按此命名",
  staleTier: "命名档位已切换：重新搜索后按新档位生成拟命名",
};

// 互斥三态（默认 off = 不改名）
export const NAME_MODES = ["off", "rename", "wide"];

export function normalizeMode(mode) {
  return NAME_MODES.indexOf(mode) >= 0 ? mode : "off";
}

export function isRenameOn(mode) {
  return normalizeMode(mode) !== "off";
}

// 档位 → preview 参数 name_wide（宽档 1，其余 0；与 gui.py 布尔词表同义）
export function nameWideParam(mode) {
  return normalizeMode(mode) === "wide" ? 1 : 0;
}

// 档案位 → 「是否重新搜索才能生成拟名」判据
export function tierChanged(mode, scannedMode) {
  return normalizeMode(mode) !== normalizeMode(scannedMode);
}

// 行内拟用名：编辑优先，空=保持原名（与旧壳 rnEffective 同义）
export function effectiveName(edits, key, item) {
  const src = edits && (key in edits) ? edits[key] : ((item && item.rename) || "");
  return String(src == null ? "" : src).trim();
}

// 某行是否参与命名（档位关 → 全部不参与；显式取消勾选 → 不参与；
// 拟名为空 → 不参与，展示原文件名）
export function participates(mode, offSet, key, name) {
  if (!isRenameOn(mode)) return false;
  if (offSet && typeof offSet.has === "function" && offSet.has(String(key))) return false;
  return String(name == null ? "" : name).trim() !== "";
}

// 结果两态：文件已落盘=已改名（已下载）；否则=已登记（未下载）
export function rowNameState(item) {
  const st = String((item && item.status) || "");
  if (st === "done" || st === "exists") return "renamed";
  if (item && item.downloaded === true) return "renamed";
  return "recorded";
}

export function rowNameStateText(item) {
  return rowNameState(item) === "renamed"
    ? NAMING_COPY.stateRenamed
    : NAMING_COPY.stateRecorded;
}

// ============================================================
// DOM 组件（注入 el；本模块不直连 ui 桶，避免锁死 17 导出口径）
// ============================================================
// opts: {mode, onModeChange(mode), onSelectAll(bool), note}
// 返回 {el, setMode, setNote, state()}：调用方持有引用，无需重建。
export function createNamingBar(el, opts) {
  const o = opts || {};
  const listeners = {
    mode: typeof o.onModeChange === "function" ? o.onModeChange : function () {},
    selectAll: typeof o.onSelectAll === "function" ? o.onSelectAll : function () {},
  };
  let mode = normalizeMode(o.mode);

  const toggle = el("input", {
    id: "nw-rn-toggle", type: "checkbox", class: "nw-rn-toggle",
    checked: mode !== "off",
  });
  const radioRename = el("input", {
    type: "radio", name: "nw-rn-tier", value: "rename",
    checked: mode === "rename", disabled: mode === "off",
  });
  const radioWide = el("input", {
    type: "radio", name: "nw-rn-tier", value: "wide",
    checked: mode === "wide", disabled: mode === "off",
  });
  const selAll = el("button", {
    class: "u-btn u-btn--ghost nw-rn-all", type: "button",
    text: NAMING_COPY.selectAll, disabled: mode === "off",
    title: "所有行启用命名（使用拟用新名）",
  });
  const selNone = el("button", {
    class: "u-btn u-btn--ghost nw-rn-none", type: "button",
    text: NAMING_COPY.selectNone, disabled: mode === "off",
    title: "所有行取消命名（使用并展示原文件名）",
  });
  const note = el("p", { class: "nw-naming-note", role: "status" });

  function sync(next) {
    mode = normalizeMode(next);
    toggle.checked = mode !== "off";
    radioRename.disabled = mode === "off";
    radioWide.disabled = mode === "off";
    radioRename.checked = mode === "rename";
    radioWide.checked = mode === "wide";
    selAll.disabled = mode === "off";
    selNone.disabled = mode === "off";
  }

  function emit(next) {
    sync(next);
    listeners.mode(mode);
  }

  toggle.addEventListener("change", function () {
    emit(toggle.checked ? "rename" : "off");
  });
  radioRename.addEventListener("change", function () {
    if (radioRename.checked) emit("rename");
  });
  radioWide.addEventListener("change", function () {
    if (radioWide.checked) emit("wide");
  });
  selAll.addEventListener("click", function () { listeners.selectAll(true); });
  selNone.addEventListener("click", function () { listeners.selectAll(false); });

  const tiers = el("div", {
    class: "nw-naming-tiers", role: "radiogroup", "aria-label": "命名档位",
  }, [
    el("label", { class: "nw-naming-tier" }, [radioRename,
      el("span", { text: NAMING_COPY.tierRename })]),
    el("label", { class: "nw-naming-tier" }, [radioWide,
      el("span", { text: NAMING_COPY.tierWide })]),
  ]);

  const root = el("div", { class: "nw-naming", "aria-label": "命名设置" }, [
    el("div", { class: "nw-naming-head" }, [
      el("label", { class: "nw-naming-toggle" }, [toggle,
        el("span", { text: NAMING_COPY.toggle })]),
      tiers,
      el("div", { class: "nw-naming-acts" }, [selAll, selNone]),
    ]),
    note,
  ]);

  function setNote(text) {
    note.textContent = text || "";
    note.hidden = !text;
  }
  setNote(o.note || NAMING_COPY.note);
  sync(mode);

  return {
    el: root,
    setMode: sync,
    setNote: setNote,
    state: function () { return mode; },
  };
}
