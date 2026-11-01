// ============================================================
// progress.js — T10 进度条（确定 / 不确定 两型）
//
// 对齐旧壳：
//   - 确定型：.ov-bar/.ov-fill（style.css:385-394，track 8px 内凹槽 +
//     fill 过渡 width .5s）→ 本组件 .u-progress-track/.u-progress-fill；
//   - 不定型：.job-fill.indet（style.css:986-987，38% 宽滑块 1.15s
//     往复）→ 关键帧 u-progress-indet 同口径；
//   - fill 品牌渐变以令牌派生（--color-primary→--color-primary-strong）。
// set(v) 钳 0..100；setDeterminate(bool) 切型（aria-valuemin/max/now
// 与 role=progressbar 同步，无障碍语义完整）。
// 零异步装载、零 Promise：destroy 直接移除节点，幂等。
// ============================================================

export function createProgress(opts) {
  const o = opts || {};
  const doc = o.doc || (typeof document !== "undefined" ? document : null);
  if (!doc) throw new Error("[v22ui] createProgress 需要可用的 document（host 环境）");
  let determinate = o.determinate !== false;
  let value = typeof o.value === "number" ? Math.max(0, Math.min(100, o.value)) : 0;
  let disposed = false;

  const root = doc.createElement("div");
  root.className = "u-progress";
  root.dataset.determinate = "true";
  root.setAttribute("role", "progressbar");
  root.setAttribute("aria-valuemin", "0");
  root.setAttribute("aria-valuemax", "100");
  root.setAttribute("aria-valuenow", "0");

  const track = doc.createElement("div");
  track.className = "u-progress-track";
  const fill = doc.createElement("div");
  fill.className = "u-progress-fill";
  track.append(fill);
  root.append(track);

  const label = doc.createElement("span");
  label.className = "u-progress-label";
  label.hidden = o.label == null;
  label.textContent = String(o.label == null ? "" : o.label);
  root.append(label);

  function render() {
    root.dataset.determinate = determinate ? "true" : "false";
    if (determinate) {
      root.removeAttribute("aria-hidden");
      root.setAttribute("aria-valuenow", String(Math.round(value)));
      fill.style.width = value + "%";
    } else {
      root.setAttribute("aria-hidden", "true"); // 不定型不给假百分比（FR-37 语义）
      root.removeAttribute("aria-valuenow");
      fill.style.width = "";
    }
  }
  render();

  return {
    el: root,
    isDeterminate: function () { return determinate; },
    set: function (v) {
      if (disposed) return this;
      if (typeof v !== "number") return this;
      value = Math.max(0, Math.min(100, v));
      render();
      return this;
    },
    setDeterminate: function (b) {
      if (disposed) return this;
      determinate = b !== false;
      render();
      return this;
    },
    destroy: function () {
      if (disposed) return; // 幂等
      disposed = true;
      if (root.isConnected) root.remove();
    },
  };
}
