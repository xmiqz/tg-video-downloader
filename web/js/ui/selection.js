// ============================================================
// selection.js — T10 选择模式控制器（零 DOM 纯状态机）
//
// 对齐旧壳行为点（app.js）：
//   - activeSel = new Set()（app.js:1088）→ 内部 selected Set；
//   - toggleActiveSel 有则删无则加（app.js:1518-1519）→ toggle(key)；
//   - activeSelClear（app.js:1522）→ clear()；
//   - 批量条 hidden 切换 = activeSel.size===0（app.js:1160-1161）与
//     计数文案 `已选 ${activeSel.size}`（app.js:1162-1163）→ 由消费方
//     订阅 change 后走 bottom action bar 的 setCount（TR10-1 H 组联动）；
//   - 编辑态 bulk-count 同口径（app.js:2219）。
//   语义钉死（seams.md S1）：选择集合独立于 keys 清单——toggle 未知 key
//   仍切换集合；keys 清单只影响 selectAll / all / partial 数学。
//   与底部动作条解耦联动：本组件不发任何 DOM，动作可用门经 can()/状态
//   里的 actions 快照；布线（谁订阅、谁 setEnabled）归消费方块（任务书§2.8）。
//
// destroy 语义：清空订阅 + disposed=true（后续一切方法静默/安全），
// 再 destroy 幂等不抛（seam S1-12）。
// ============================================================

export function createSelectionController(opts) {
  const o = opts || {};
  const actionDefs = (Array.isArray(o.actions) ? o.actions : []).map(function (a) {
    return {
      key: a && a.key ? String(a.key) : "",
      enabledWhen: typeof (a && a.enabledWhen) === "function" ? a.enabledWhen : function () { return true; },
    };
  }).filter(function (a) { return a.key; });

  let keys = [];
  const selected = new Set();
  let active = false;
  let disposed = false;
  const subs = new Set();
  if (typeof o.onChange === "function") subs.add(o.onChange);

  function snapshot() {
    const total = keys.length;
    const count = selected.size;
    const all = total > 0 && count >= total;
    const none = count === 0;
    const partial = count > 0 && !all;
    const acts = {};
    const base = { active: active, count: count, total: total, all: all, none: none, partial: partial };
    for (let i = 0; i < actionDefs.length; i++) {
      acts[actionDefs[i].key] = active === true ? actionDefs[i].enabledWhen(base) === true : false;
    }
    base.actions = acts;
    return base;
  }

  function emit() {
    if (disposed) return;
    const s = snapshot();
    const list = Array.from(subs);
    for (let i = 0; i < list.length; i++) {
      try { list[i](s); }
      catch (e) {
        // 裸 console 禁令豁免：[v22ui] 前缀 catch 日志（t10 任务书 §3，T8 同口径）
        console.debug("[v22ui] selection change 订阅者异常：", e);
      }
    }
  }

  const api = {
    get state() { return snapshot(); },
    setKeys: function (list) {
      if (disposed) return api;
      keys = Array.isArray(list) ? list.map(function (k) { return String(k); }) : [];
      return api;
    },
    enter: function () {
      if (disposed) return api;
      active = true;
      selected.clear();
      emit();
      return api;
    },
    exit: function () {
      if (disposed) return api;
      active = false;
      selected.clear();
      emit();
      return api;
    },
    toggle: function (key) {
      if (disposed) return api;
      const k = String(key);
      if (selected.has(k)) selected.delete(k); else selected.add(k);
      emit();
      return api;
    },
    selectAll: function () {
      if (disposed) return api;
      selected.clear();
      for (let i = 0; i < keys.length; i++) selected.add(keys[i]);
      emit();
      return api;
    },
    clear: function () {
      if (disposed) return api;
      selected.clear();
      emit();
      return api;
    },
    can: function (actionKey) {
      if (disposed) return false;
      const def = actionDefs.find(function (a) { return a.key === String(actionKey); });
      if (!def) return false;
      if (active !== true) return false;
      const s = snapshot();
      return def.enabledWhen({ active: s.active, count: s.count, total: s.total, all: s.all, none: s.none, partial: s.partial }) === true;
    },
    on: function (event, cb) {
      if (disposed) return api;
      if (event === "change" && typeof cb === "function") subs.add(cb);
      return api;
    },
    off: function () {
      subs.clear();
      return api;
    },
    destroy: function () {
      if (disposed) return; // 幂等
      disposed = true;
      subs.clear();
      selected.clear();
      keys = [];
    },
  };
  return api;
}
