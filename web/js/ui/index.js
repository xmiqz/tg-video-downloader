// ============================================================
// index.js — T10 组件库桶（T11–T16 并行视图唯一消费入口）
//
// 全部命名导出（零默认导出，杜绝形状歧义）；S0-17 桶全量再导出清单
// 与 seam 断言同名锁定（seams.md S0），新增组件必须同步更新本桶与
// t10_contract.md（否则 seam S0-17 红）。
// 桶自身无逻辑、无副作用、无顶层 DOM 访问。
// ============================================================

export { el, append } from "./dom.js";
export { createCard } from "./card.js";
export { createOverlay, resolveOverlayForm, OVERLAY_FORM_BREAKPOINT } from "./overlay.js";
export { createToaster, planToastStack, resolveToastTiming } from "./toast.js";
export { createEmptyState } from "./empty.js";
export { createProgress } from "./progress.js";
export { createSegmented, resolveSegmentedValue, nextSegmentIndex, clampScrollLeft } from "./segmented.js";
export { createBottomActionBar } from "./actionbar.js";
export { createSelectionController } from "./selection.js";
