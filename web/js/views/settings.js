// ============================================================
// views/settings.js — T15 设置视图（T19b 回 2.1 三卡平铺形态：
//                   账号 / 界面外观 / 下载根目录）
//
// T19b（2026-10-18，用户实测 Q1/Q3）：撤销 T15 五折叠分组（外观/凭据/
//   下载目录/多账号区/游客相关），逐值回 2.1 三张 .glass.form-card.narrow
//   平铺卡（index.html:342-409：label 卡题、无折叠、卡片间距 0）；凭据+
//   多账号区合并回「账号」卡（主号/小号行同区，2.1 #acct-region 单区），
//   游客相关卡删除（2.1 无此卡，游客态仅账号行兜登录入口）。
// 归属波次：T15（v2.2 W6），替换 T8 占位。所有权恰两文件：
//   web/js/views/settings.js（本文件）+ web/css/views/settings.css。
//
// 行为基线（对齐不回归，逐条 文件:行 注明出处；基准只读）：
//   - 设置页三卡布局          index.html:336-410（外观/下载根目录/账号区）
//   - loadSettings            app.js:2476-2483（get_settings + 账号区刷新）
//   - chooseFolder            app.js:2486-2501（互斥锁 + 失败可见 + 取消静默）
//   - openRoot                app.js:2505-2519（防重 + 失败可见）
//   - saveRoot                app.js:2524-2542（防重 + 成功才提示"已保存"）
//   - applyAppearance         app.js:2553-2565（data-theme 即时生效）
//   - setAppearance           app.js:2571-2583（本地先行 + 后端保存失败文案）
//   - bootAppearance          app.js:2585-2593（get_appearance 引导）
//   - TgAccounts 数据源/三态  app.js:3113-3257（normAccounts/uiState/stateText/
//                             nameOf/subOf + T56 Bug3 暖机补刷）
//   - 渲染（主号/小号/游客）   app.js:3260-3323（T46 游客口径：游客态只显
//                             "未登录 · 点此登录"、不显添加小号控件）
//   - 添加小号三步卡          app.js:3325-3442（T58 r2 契约 3.2：warning 透传/
//                             recoverable"已收到验证码？直接输入"/"返回"纯前端
//                             切步 keepRecover/2FA stage 优先判定）
//   - 移除小号二次确认        app.js:3445-3474（文案逐字 + busy 拒绝语义）
//   - 主号待连接重试          app.js:3476-3502（T58 契约 3.4，行内错误不弹层）
//   - 退出登录三态            app.js:632-693 doLogout + 585-607 提升确认 +
//                             609-630 afterPromote（is_active 守卫逐字/
//                             提升语义/清光回登录）
//
// 组件消费：T10 桶唯一入口 ../ui/index.js（createCard/createSegmented/
//   createOverlay/createToaster/el），签名见 t10_contract.md。
// RPC 白名单消费恰 16 个（45 集合内）：login_state / me / get_settings /
//   choose_folder / open_folder / set_download_root / get_appearance /
//   set_appearance / account_list / account_add_start / account_verify_code /
//   account_verify_password / account_remove / connect / is_active / logout。
//   禁 open_editor、禁 set_guest（游客进入是登录视图 T11 的职责）。
//
// thenable 桥不变量（W4 hard 教训）：bridge.js:69-74 的
//   window.pywebview.api 是 Proxy，任意属性（含 then）访问皆返回 RPC
//   函数——任何 Promise 不得 await/resolve/return 桥对象本身；就绪
//   Promise 只 resolve 布尔（ensureApi 现行写法），调用一律同步直调
//   window.pywebview.api.xxx(...)。
//
// 5A seam 出口（与 T11–T14 同款，仅供测试消费）：normAccounts/uiState/
//   stateText/nameOf/subOf/nextAltId/effectiveTheme/classifyLogin/
//   classifyAddStart/classifyVerifyCode/removeFailText/normHint 十二个
//   纯函数 + ACCT_STATE_TEXT/RPC_STATE_MAP 常量额外命名导出；本模块顶层
//   零 DOM/window 访问。
//
// 外观即时生效口径（T7 契约：外壳消费 <html data-theme>）：静态暗→
//   data-theme="dark"、静态亮→"light"、液态玻璃→html.liquid 预烘焙
//   玻璃态（shell.css §0b #sh-backdrop 背景层 + _setBackdrop 消费方，
//   router.js 启动引导），仅 Windows 生效——同旧壳非 Windows 降级口径
//   （app.js:2555-2556）与编辑器窗口口径（editor.js 无采样按静态暗）；
//   set_appearance 仍保存 "liquid" 原值，
//   localStorage.appearance 与 router.js 启动引导同一数据源。
//
// 范围外（本波不实现不预埋）：
//   - 上传目标分区等候选池禁项（kickoff 铁令）；
//   - acct_hint 限速大条/小字（active.js T13 已承接）；本视图仅消费
//     tg:acct_hint 把账号行状态字显为"限速中"（app.js:3243-3245）；
//   - t52demo 桩模式 __tgAcctProbe（T52 测试挂点，随旧壳 T18 一并消失）；
//   - 任务选号分段（acct-seg-host，新/下载中/任务库视图承接）。
// ============================================================

import {
  el, createSegmented, createOverlay, createToaster,
} from "../ui/index.js";

// T17 手势增量（边缘返回/聚焦保持；见 evidence/T17/t17_diff_list.md）
import { attachEdgeBack, attachFocusKeep } from "../gestures.js";

export const path = "settings";

export const title = "设置 - TG 视频下载器";

// ============================================================
// 常量（照搬旧壳语义，出处就近注明）
// ============================================================

// 账号状态中文映射（app.js:3190-3191 逐字）
export const ACCT_STATE_TEXT = {
  ready: "在线", connecting: "连接中", standby: "待连接", absent: "未授权",
};

// T51 AccountPool.snapshot() 状态枚举 -> UI 三态（app.js:3193-3199 逐字）
export const RPC_STATE_MAP = {
  ready: "ready",
  adding: "connecting",      // 小号添加三步进行中
  has_session: "connecting", // 已有授权、待暖机
  logged_out: "absent",
  error: "absent",
};

// 外观分段（index.html:393-397 data-mode/label 逐字）
// 液态玻璃依赖 Windows 原生窗口后方取光，非 Windows 宿主（浏览器 / 容器 /
// 其它平台）根本没有这个能力，展示出来只会误导，故按平台裁剪。
// Liquid glass samples the pixels behind a NATIVE window, which only exists in
// the desktop shell. A browser on Windows hitting the container is still a
// browser, so UA sniffing is wrong here: the host tells us its runtime_mode
// via get_settings(). Until that arrives we assume "not available" (the
// conservative choice: no broken option is shown).
let HOST_MODE = "headless";

function appearanceSegs(hostMode) {
  const segs = [
    { value: "dark", label: "静态暗" },
    { value: "light", label: "静态亮" },
    { value: "bing", label: "每日壁纸" },
  ];
  if (hostMode !== "headless") segs.push({ value: "liquid", label: "液态玻璃" });
  return segs;
}

// 暖机补刷节拍（app.js:3205-3206：3s × 6 次，对齐后端 warm_alt 暖机）
const WARM_TICK_MS = 3000;
const WARM_MAX_TICKS = 6;

// ============================================================
// S1 normAccounts —— T51 状态归一（app.js:3142-3146）
// rawState 保留原枚举（adding/has_session 的 UI 收敛依赖），state 归一。
// ============================================================
export function normAccounts(arr) {
  const list = Array.isArray(arr) ? arr : [];
  return list.map(function (x) {
    const a = x || {};
    const raw = a.state || "";
    return Object.assign({}, a, {
      rawState: raw,
      state: RPC_STATE_MAP[raw] || raw,
    });
  });
}

// ============================================================
// S2 uiState —— 展示态（app.js:3236-3240 T56 Bug3）
// 暖机窗口尽后仍 has_session 的 connecting 行转 standby；adding 恒
// connecting（用户正在流程中）。
// ============================================================
export function uiState(a, warmDone) {
  if (a && a.state === "connecting" && a.rawState !== "adding" && warmDone)
    return "standby";
  return a ? a.state : "";
}

// ============================================================
// S3 stateText —— 状态字（app.js:3241-3246）
// hint 命中（限速中）优先于状态字；hint 为 normHint 归一结果或 null。
// ============================================================
export function stateText(a, hint, warmDone) {
  if (!a) return "";
  if (hint && hint.account_id === a.id && hint.reason) return "限速中";
  const st = uiState(a, warmDone);
  return ACCT_STATE_TEXT[st] || a.state || "";
}

// ============================================================
// S4 nameOf / subOf —— 行名与副行（app.js:3247-3257）
// me 缓存 name/first_name 双形状兼容；缺失中性兜底不回落 id。
// ============================================================
export function nameOf(a) {
  if (!a) return "";
  const me = a.me || {};
  if (me.username) return "@" + me.username;
  if (me.first_name || me.name) return me.first_name || me.name;
  return a.id === "main" ? "主号（未获取资料）" : "小号（未获取资料）";
}

export function subOf(a) {
  return (a && a.phone) || "";
}

// ============================================================
// S5 nextAltId —— 下一个小号 id（app.js:3337-3341 openAdd）
// alt1、alt2… 跳过已占用。
// ============================================================
export function nextAltId(ids) {
  const used = new Set(Array.isArray(ids) ? ids : []);
  let i = 1;
  while (used.has("alt" + i)) i++;
  return "alt" + i;
}

// ============================================================
// S6 effectiveTheme —— 外观模式 → data-theme（app.js:2553-2560 + T7 契约）
// liquid 令牌基底 = dark（2.1 液态玻璃为暗底玻璃），liquid 态由
// html.liquid 单独承载（applyAppearance 消费）。
// ============================================================
export function effectiveTheme(mode) {
  return String(mode || "") === "light" ? "light" : "dark";
}

// ---------------------------------------------------------------
// Daily wallpaper theme (Bing photo of the day)
//
// The image is fetched and cached by the BACKEND (wallpaper.py), because the
// desktop shell starts the server on port 0: the page origin changes every
// launch, so a localStorage cache would be wiped on each restart. The backend
// also serves the bytes same-origin at /wallpaper/<file>, which means the
// page never talks to www.bing.com and the wallpaper still works offline.
// ---------------------------------------------------------------

export function bingBackdropCss(url) {
  const u = String(url || "").trim();
  if (!u) return "";
  return "url(\"" + u.replace(/["\\]/g, "\\$&") + "\")";
}

export async function applyBingBackdrop() {
  const back = document.getElementById("sh-backdrop");
  if (!back) return;
  let st = null;
  try {
    st = await wpApi("wallpaper_status", []);
  } catch (e) {
    st = null;
  }
  const cur = st && st.ok ? st.current : null;
  if (cur && cur.file) {
    back.style.backgroundImage = bingBackdropCss("/wallpaper/" + cur.file);
    if (cur.copyright) back.title = cur.copyright;
    return;
  }
  // Nothing cached yet: ask the backend to sync once, then render.
  try {
    st = await wpApi("wallpaper_refresh", [false]);
  } catch (e) {
    st = null;
  }
  const fresh = st && st.ok ? st.current : null;
  if (fresh && fresh.file) {
    back.style.backgroundImage = bingBackdropCss("/wallpaper/" + fresh.file);
    if (fresh.copyright) back.title = fresh.copyright;
  }
}

export function classifyLogin(st) {
  if (st && st.logged_in === true && st.guest !== true) return "connected";
  if (st && st.guest === true) return "guest";
  return "anonymous";
}

// ============================================================
// S8 classifyAddStart —— 发码结果分支（app.js:3400-3412）
// ok（含 warning 软成功）→ code；recoverable → recover（停 phone 步 +
// 恢复入口）；其余 → error。
// ============================================================
export function classifyAddStart(r) {
  if (r && r.ok) return "code";
  if (r && r.recoverable) return "recover";
  return "error";
}

// ============================================================
// S9 classifyVerifyCode —— 验码分支（app.js:3424-3428）
// 2FA：stage 字段优先判定（真实形 {ok:true,stage} 与桩形
// {ok:false,stage} 同走 need_password 步）。
// ============================================================
export function classifyVerifyCode(r) {
  if (r && r.stage === "need_password") return "pwd";
  if (r && r.ok) return "done";
  return "error";
}

// ============================================================
// S10 removeFailText —— 移除失败文案（app.js:3471-3473 逐字）
// ============================================================
export function removeFailText(r) {
  if (r && r.busy) return "该小号正被任务使用，暂不能移除";
  return (r && r.error) || "移除失败";
}

// ============================================================
// S11 normHint —— tg:acct_hint 负载归一（app.js:3521-3529；T51 嵌套/
// T52 旧扁平双形状兼容，与 active.js S10 同源口径）
// ============================================================
export function normHint(raw) {
  if (!raw) return null;
  const d = (raw.data && typeof raw.data === "object") ? raw.data : {};
  return {
    account_id: d.from_acct || raw.account_id || "",
    reason: d.reason || raw.reason || "",
    has_alt: typeof d.has_alt === "boolean" ? d.has_alt : null,
  };
}

// ============================================================
// RPC 桥（唯一通道 window.pywebview.api；pywebviewready 等就绪）
// 就绪 Promise 只 resolve 布尔；调用点同步直调——绝不
// await/resolve/return 桥对象本身（thenable 桥不变量，见文件头）。
// ============================================================
function bridgeReady() {
  return !!(typeof window !== "undefined" &&
    window.pywebview && window.pywebview.api);
}

let pyReadyHandler = null;
let apiReadyP = null;

function ensureApi() {
  if (bridgeReady()) {
    return Promise.resolve(true);   // 布尔；绝不 resolve 桥对象
  }
  if (!apiReadyP) {
    apiReadyP = new Promise(function (resolve) {
      pyReadyHandler = function () {
        pyReadyHandler = null;
        resolve(bridgeReady());     // 布尔；绝不 resolve 桥对象
      };
      window.addEventListener("pywebviewready", pyReadyHandler);
    }).then(function (ready) {
      apiReadyP = null;
      return ready;                 // 布尔透传
    });
  }
  return apiReadyP;
}

let toaster = null;
function toast(type, text, detail) {
  if (!toaster) toaster = createToaster({ max: 5 });
  toaster.show({ type: type, text: text, detail: detail });
}

// 忙碌按钮（app.js withBusy 同构）：禁用 + 临时文案，返回还原函数
function busyOn(btn, text) {
  if (!btn) return function () {};
  const prevText = btn.textContent;
  const prevDisabled = btn.disabled;
  btn.disabled = true;
  if (text) btn.textContent = text;
  return function () {
    btn.disabled = prevDisabled;
    btn.textContent = prevText;
  };
}

// 导航：优先新壳 router 探针，降级 hash（同 new.js/tasks.js 范式）
function navigate(p) {
  if (typeof window !== "undefined" && window.__t8router &&
      typeof window.__t8router.navigate === "function") {
    window.__t8router.navigate(p);
    return;
  }
  location.hash = "#" + p;
}

// ============================================================
// 确认弹层（Promise 化；旧 confirm/modal 文案逐字承接）
// ok → resolve(true)；✕/遮罩/ESC/取消 → resolve(false)。
// ============================================================
function confirmAskP(opts) {
  return new Promise(function (resolve) {
    let settled = false;
    const done = function (v) {
      if (settled) return;
      settled = true;
      resolve(v);
    };
    destroyConfirm();
    confirmOv = createOverlay({
      title: (opts && opts.title) || "确认操作",
      content: (opts && opts.content) || "",
      form: "modal",
      actions: [
        {
          key: "ok",
          label: (opts && opts.confirmLabel) || "确定",
          primary: !(opts && opts.danger),
          danger: !!(opts && opts.danger),
        },
        { key: "cancel", label: "取消" },
      ],
      onAction: function (k) {
        if (k === "ok") done(true);   // 非 false → 组件自动 close
      },
      onClose: function () { done(false); },
      ariaLabel: (opts && opts.title) || "确认操作",
    });
    document.body.appendChild(confirmOv.el);
    confirmOv.open();
  });
}

function destroyConfirm() {
  if (confirmOv) {
    confirmOv.destroy();
    confirmOv = null;
  }
}

// ============================================================
// 视图状态（模块级闭包；keep-alive 下跨进入保留）
// ============================================================
let cached = null;
let refs = null;
let loginMode = "anonymous";      // connected | guest | anonymous
let accounts = [];                // account_list 归一结果
let acctSource = "none";          // none | rpc（降级口径）
let meCache = null;               // me() 结果（降级行渲染用）
let hint = null;                  // 最近一条限速类 acct_hint（normHint）
let logoutBusy = false;           // 退出登录在途（按钮禁用）
let warmTicks = 0;
let warmTimer = null;
let warmDone = false;
let addingId = null;              // 添加小号流程中的目标 account_id
let removingId = null;            // 移除确认中的 account_id
let folderPickBusy = false;
let openRootBusy = false;
let saveRootBusy = false;
let confirmOv = null;
let segSilent = false;            // 程序化 set 分段不触发 onChange
let hintBound = false;
let hintHandler = null;

// ============================================================
// mount / onEnter / onLeave（T8 契约 §1）
// ============================================================
export function mount() {
  if (cached) return cached;
  try {
    cached = buildDom();
  } catch (e) {
    // mount 自捕获（任务书 §1.5）：失败返回降级元素，rejected Promise
    // 不得进 router mounts 缓存——本路径同步返回可用空态元素。
    console.error("[T15] settings 视图构建失败：", e);
    cached = el("div", { class: "vw-settings" }, [
      el("p", {
        class: "st-err-line", role: "alert",
        text: "设置视图加载失败，请稍后重试。",
      }),
    ]);
  }
  return cached;
}

export function onEnter() {
  if (cached && refs) {
    bindHint();
    attachGestures();   // T17 手势增量（幂等）
    loadAll();
  }
}

// T17 手势增量：边缘返回 + 聚焦保持（虚拟键盘遮挡补偿）
let gsDetach = [];
function attachGestures() {
  if (gsDetach.length) return;
  gsDetach = [attachEdgeBack(), attachFocusKeep(refs.root)];
}

export function onLeave() {
  // 全清理（任务书铁律 5）：暖机计时/弹层/事件/添加向导瞬态
  stopWarm();
  destroyConfirm();
  closeAddQuiet();
  unbindHint();
  // T17 手势增量对称回收（attach* 均返回 detach）
  for (let i = 0; i < gsDetach.length; i++) gsDetach[i]();
  gsDetach = [];
  removingId = null;
  logoutBusy = false;
}

function bindHint() {
  if (hintBound) return;
  hintBound = true;
  hintHandler = function (ev) { onAcctHint(ev && ev.detail); };
  window.addEventListener("tg:acct_hint", hintHandler);
}

function unbindHint() {
  if (!hintBound) return;
  hintBound = false;
  if (hintHandler) {
    window.removeEventListener("tg:acct_hint", hintHandler);
    hintHandler = null;
  }
}

// 仅限速类 reason 驱动账号行状态字（app.js:3530-3543；大条/小字归
// active.js T13 承接，本视图只消费状态字语义）
function onAcctHint(raw) {
  const d = normHint(raw);
  if (!d || typeof d.account_id !== "string" || !d.account_id) return;
  if (d.reason !== "flood_wait" && d.reason !== "slow") return;
  hint = d;
  renderAccounts();
}

// ============================================================
// DOM 构建（仅 mount 一次执行；顶层与 onEnter 无 DOM 依赖）
// ============================================================
function buildDom() {
  const root = el("div", { class: "vw-settings", dataset: { viewName: "settings" } });

  // —— 页头（index.html:337）——
  root.append(el("header", { class: "st-head" }, [
    el("h2", { text: "设置" }),
    el("p", { class: "st-sub", text: "外观、下载目录与数据" }),
  ]));

  // —— 卡一：账号（index.html:342-379 逐序：label→账号行区→错误行→
  //    添加小号→三步卡→hint；T19b 凭据/多账号合并回 2.1 单区，
  //    主号/小号行同落 #acct-region 等价位）——
  const acctRegion = el("div", {
    class: "st-region", role: "list", "aria-label": "账号凭据",
  });
  const acctErr = el("p", { class: "st-err-line", role: "alert", text: "" });
  const btnAdd = el("button", {
    class: "u-btn u-btn--ghost st-add-btn", type: "button",
    text: "＋ 添加小号",
    onclick: function () { openAdd(); },
  });
  const addCard = buildAddCard();
  const credHint = el("p", {
    class: "st-hint",
    // index.html:378 逐字（2.1 静态恒显，不随登录态隐现）
    text: "退出后本地登录授权将被删除，重新登录需输入手机号与验证码；任务清单和已下载视频不会被删除。下载进行中无法退出。",
  });
  root.append(makeCard("账号",
    // addCard.el：DOM 节点才可入树（el() 对非节点子项会 String() 化）
    [acctRegion, acctErr, btnAdd, addCard.el, credHint]));

  // —— 卡二：界面外观（index.html:391-399）——
  // createSegmented 构造 set(初值) 会触发 onChange（segmented.js:149→129）：
  // 静默构造，杜绝 mount 期 set_appearance 副作用覆盖已存外观。
  segSilent = true;
  const appearanceSeg = createSegmented(appearanceSegs(HOST_MODE), {
    value: "dark", ariaLabel: "界面外观",
    onChange: function (v) { onAppearanceSeg(v); },
  });
  segSilent = false;
  // 壁纸面板：仅在选中「每日壁纸」时展开
  const wpPanel = buildWallpaperPanel();
  root.append(makeCard("界面外观", [
    appearanceSeg.el,
    el("p", {
      class: "st-hint",
      text: HOST_MODE !== "headless"
        ? "静态暗（默认）：经典深色玻璃；静态亮：浅色玻璃；每日壁纸：Bing 每日一图，每天 00:00 自动更新；液态玻璃：实时采样窗口正后方的真实画面（仅 Windows），更耗性能。"
        : "静态暗（默认）：经典深色玻璃；静态亮：浅色玻璃；每日壁纸：Bing 每日一图，每天 00:00 自动更新。",
    }),
    wpPanel.el,
  ]));

  // —— 卡：任务导入 / 导出（仅元数据，绝不含视频本体）——
  const taskPanel = buildTaskPortPanel();
  root.append(makeCard("任务导入 / 导出", [
    el("p", {
      class: "st-hint",
      text: "导出的任务包只含条目元数据（名称/大小/日期/状态），不含任何视频文件；"
          + "导入时会按本机下载根目录重新落位，条目一律回到待下载状态。",
    }),
    taskPanel.el,
  ]));

  // —— 卡三：下载根目录（index.html:400-409）——
  const rootInput = el("input", {
    id: "st-root", type: "text", class: "st-input", readonly: true,
    autocomplete: "off",
  });
  const btnChoose = el("button", {
    class: "u-btn u-btn--ghost", type: "button", text: "浏览",
    onclick: function () { chooseFolder(rootInput, btnChoose); },
  });
  const btnOpen = el("button", {
    class: "u-btn u-btn--ghost", type: "button", text: "打开",
    onclick: function () { openRoot(rootInput, btnOpen); },
  });
  const btnSave = el("button", {
    class: "u-btn u-btn--primary", type: "button", text: "保存",
    onclick: function () { saveRoot(rootInput, btnSave); },
  });
  const pathHint = el("p", {
    class: "st-hint",
    // index.html:408 逐字
    text: "账号会话、任务清单与下载内容都保存在程序平级的「TG视频下载器数据」目录，不会上传。",
  });
  root.append(makeCard("下载根目录", [
    el("div", { class: "st-path-row" }, [
      el("label", { class: "st-sr-only", for: "st-root", text: "下载根目录" }),
      rootInput,
      btnChoose,
      btnOpen,
    ]),
    btnSave,
    pathHint,
  ]));

  const btnWebLogout = el("button", {
    class: "u-btn u-btn--ghost", type: "button", text: "退出 Web 服务登录",
    hidden: true,
    onclick: logoutWeb,
  });
  const webCard = makeCard("Web 服务", [
    btnWebLogout,
    el("p", {
      class: "st-hint",
      text: "只结束浏览器管理员会话；Telegram 登录与下载任务保持不变。",
    }),
  ]);
  webCard.hidden = true;
  root.append(webCard);

  refs = {
    root: root,
    appearanceSeg: appearanceSeg,
    wpPanel: wpPanel,
    tpPanel: taskPanel,
    acctRegion: acctRegion,
    acctErr: acctErr,
    btnAdd: btnAdd,
    addCard: addCard.el,
    add: addCard,
    credHint: credHint,
    rootInput: rootInput,
    btnChoose: btnChoose,
    btnOpen: btnOpen,
    pathHint: pathHint,
    btnWebLogout: btnWebLogout,
    webCard: webCard,
  };
  return root;
}

// 2.1 平铺窄卡（.glass.form-card.narrow + label 卡题，index.html:342-409）：
// T19b 回 2.1 形态——无折叠、无分组标题行，label 即卡题；内部节奏走
// 2.1 margin 语汇（label 16/7、hint 18、block 钮 10），不做 flex gap。
function makeCard(label, children) {
  return el("div", { class: "st-card" },
    [el("label", { class: "st-card-label", text: label })].concat(children));
}

// ============================================================
// 数据装载（onEnter 全量刷新；app.js switchView("settings") → loadSettings）
// ============================================================
async function loadAll() {
  if (!refs) return;
  let ready = false;
  try {
    ready = await ensureApi();
  } catch (e) {
    ready = false;
  }
  if (!ready) {
    renderAccounts();
    return;
  }
  // 登录态三态（TgAccounts.refresh 的游客分支前置条件）
  try {
    const st = await window.pywebview.api.login_state();
    loginMode = classifyLogin(st);
  } catch (e) {
    loginMode = "anonymous";
  }
  if (loginMode === "connected") {
    try {
      const m = await window.pywebview.api.me();
      if (m && m.ok) meCache = m;
    } catch (e) { /* 旧壳 me 失败静默（app.js:620-626） */ }
    await refreshAccounts();
  } else {
    accounts = [];
    acctSource = "none";
    stopWarm();
    closeAddQuiet();
    renderAccounts();
  }
  // 下载根目录（app.js:2477-2478）
  try {
    const r = await window.pywebview.api.get_settings();
    if (r && r.download_root != null && refs.rootInput) {
      refs.rootInput.value = String(r.download_root);
    }
    if (r && typeof r.mode === "string" && r.mode !== HOST_MODE) {
      HOST_MODE = r.mode;
      rebuildAppearanceSeg();
    }
    if (r && r.mode === "headless") {
      refs.rootInput.readOnly = false;
      refs.btnChoose.hidden = true;
      refs.btnOpen.hidden = true;
      refs.btnWebLogout.hidden = false;
      refs.webCard.hidden = false;
      const roots = Array.isArray(r.allowed_roots) ? r.allowed_roots : [];
      refs.pathHint.textContent = roots.length
        ? "浏览器模式请直接输入服务器路径；允许范围：" + roots.join("、")
        : "浏览器模式请直接输入服务器路径。";
    }
  } catch (e) { /* 失败保留输入现值 */ }
  // 外观引导（app.js:2585-2593 bootAppearance）
  try {
    const r = await window.pywebview.api.get_appearance();
    if (r && r.ok && r.mode) {
      try { localStorage.setItem("appearance", r.mode); } catch (e) {}
      applyAppearance(r.mode);
    }
  } catch (e) { /* 引导容错空 catch（app.js:2592 原状） */ }
}

async function logoutWeb() {
  try {
    await fetch("/auth/logout", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    });
  } finally {
    location.replace("/login");
  }
}

// ============================================================
// 外观（app.js:2553-2593）
// ============================================================
function applyAppearance(mode) {
  // 即时生效（对齐 2.1 app.js:2551-2562）：data-theme 令牌层（T7 契约）+
  // html.liquid 态（shell.css §0b 预烘焙玻璃材质）；liquid 仅 Windows 生效，
  // 其余平台按静态暗降级（app.js:2555-2556）。非 liquid 清 backdrop 图。
  // Liquid glass only exists in the desktop shell; any other host (browser,
  // container) falls back to the static dark theme.
  const canLiquid = HOST_MODE !== "headless";
  const eff = (String(mode || "") === "liquid" && !canLiquid) ? "dark" : mode;
  const isBing = eff === "bing";
  // wallpaper 主题沿用暗色令牌（照片上压深色遮罩），data-theme 只用于选层
  document.documentElement.setAttribute(
    "data-theme", isBing ? "wallpaper" : effectiveTheme(eff));
  document.documentElement.classList.toggle("liquid", eff === "liquid");
  if (eff !== "liquid" && typeof window !== "undefined" &&
      typeof window._setBackdrop === "function") {
    window._setBackdrop("");
  }
  const wp = refs && refs.wpPanel;
  if (wp) {
    wp.el.hidden = !isBing;
    if (isBing) refreshWallpaper(false);
  }
  if (isBing) {
    applyBingBackdrop();
  } else {
    const back = document.getElementById("sh-backdrop");
    if (back) { back.style.backgroundImage = ""; back.removeAttribute("title"); }
  }
  syncSeg(mode);
}

function syncSeg(mode) {
  if (!refs || !refs.appearanceSeg) return;
  if (refs.appearanceSeg.get() === mode) return;
  segSilent = true;
  try {
    refs.appearanceSeg.set(mode);
  } finally {
    segSilent = false;
  }
}

// ---------------------------------------------------------------
// Task export / import panel
// ---------------------------------------------------------------
function buildTaskPortPanel() {
  const listWrap = el("div", { class: "tp-list", hidden: true });
  const list = el("div", { class: "tp-items" });
  const statusLine = el("p", { class: "st-hint", text: "" });
  const openPickerBtn = el("button", {
    class: "u-btn u-btn--ghost u-btn--sm", type: "button", text: "选择要导出的任务",
    onclick: function () { toggleTaskPicker(); },
  });
  const exportBtn = el("button", {
    class: "u-btn u-btn--primary u-btn--sm", type: "button", text: "导出所选",
    onclick: function () { exportSelectedTasks(); },
  });
  const importInput = el("input", {
    type: "file", accept: "application/json,.json", hidden: true,
  });
  const importBtn = el("button", {
    class: "u-btn u-btn--ghost u-btn--sm", type: "button", text: "导入任务包",
    onclick: function () { importInput.click(); },
  });
  importInput.addEventListener("change", function () {
    const f = importInput.files && importInput.files[0];
    importInput.value = "";
    if (f) importTaskBundle(f);
  });
  return {
    el: el("div", { class: "tp-panel" }, [
      el("div", { class: "tp-actions" }, [
        openPickerBtn, exportBtn, importBtn, importInput,
      ]),
      statusLine, listWrap,
    ]),
    list, listWrap, statusLine, openPickerBtn,
  };
}

async function toggleTaskPicker() {
  const panel = refs && refs.tpPanel;
  if (!panel) return;
  if (!panel.listWrap.hidden) {
    panel.listWrap.hidden = true;
    return;
  }
  let r = null;
  try { r = await tpApi("tasks_exportable", []); } catch (e) {}
  const tasks = (r && r.ok && Array.isArray(r.tasks)) ? r.tasks : [];
  panel.list.textContent = "";
  if (!tasks.length) {
    panel.statusLine.textContent = "没有可导出的任务。";
    return;
  }
  tasks.forEach(function (t) {
    const cb = el("input", { type: "checkbox", value: t.id });
    panel.list.append(el("label", { class: "tp-row" }, [
      cb,
      el("span", { class: "tp-title", text: t.title || t.id }),
      el("span", { class: "tp-meta",
        text: t.count + " 条 · 已完成 " + t.done + " · " + (t.created || "") }),
    ]));
  });
  panel.listWrap.hidden = false;
  panel.statusLine.textContent = "勾选后点「导出所选」。";
}

function tpApi(name, args) {
  if (!(window.pywebview && window.pywebview.api)) return null;
  const fn = window.pywebview.api[name];
  if (typeof fn !== "function") return null;
  return fn.apply(window.pywebview.api, args || []);
}

function downloadJson(filename, data) {
  const blob = new Blob([JSON.stringify(data, null, 1)],
                        { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(function () { URL.revokeObjectURL(url); }, 4000);
}

async function exportSelectedTasks() {
  const panel = refs && refs.tpPanel;
  if (!panel) return;
  const ids = Array.from(panel.list.querySelectorAll("input:checked"))
    .map(function (c) { return c.value; });
  if (!ids.length) {
    toast("error", "没有选择任务", "请先勾选要导出的任务");
    return;
  }
  let r = null;
  try { r = await tpApi("export_tasks", [ids]); } catch (e) {}
  if (!r || !r.ok) {
    toast("error", "导出失败", (r && r.error) || "未知错误");
    return;
  }
  downloadJson(r.filename, r.bundle);
  panel.statusLine.textContent = "已导出 " + r.count + " 个任务。";
}

async function importTaskBundle(file) {
  const panel = refs && refs.tpPanel;
  let bundle = null;
  try {
    bundle = JSON.parse(await file.text());
  } catch (e) {
    toast("error", "任务包无法解析", "不是有效的 JSON 文件");
    return;
  }
  let r = null;
  try { r = await tpApi("import_tasks", [bundle]); } catch (e) {}
  if (!r || !r.ok) {
    toast("error", "导入失败", (r && r.error) || "未知错误");
    return;
  }
  if (panel) panel.statusLine.textContent = "已导入 " + r.count + " 个任务。";
  toast("success", "导入完成", r.count + " 个任务已加入任务库");
}

// ---------------------------------------------------------------
// Wallpaper panel: history picker / per-item delete / upload
// ---------------------------------------------------------------
// Rebuild the appearance segment once the host mode is known.
//
// loadSettings() can run more than once per mount (mount + onEnter), so this
// must be idempotent: drop every stray .u-seg already in the card, then insert
// the fresh one at the old position. Relying on replaceChild alone left
// duplicates behind when the old node had already been detached.
function rebuildAppearanceSeg() {
  if (!refs || !refs.appearanceSeg) return;
  const cur = refs.appearanceSeg.get();
  const oldEl = refs.appearanceSeg.el;
  const fresh = createSegmented(appearanceSegs(HOST_MODE), {
    value: cur, ariaLabel: "界面外观",
    onChange: function (v) { onAppearanceSeg(v); },
  });
  const parent = oldEl && oldEl.parentNode;
  if (parent) {
    // Scope the cleanup to this card only. router.refreshLens() mirrors the
    // whole view into #sh-lens-content as a refraction snapshot, so a global
    // querySelectorAll(".u-seg") also sees the mirror and must not touch it.
    const card = oldEl.closest ? oldEl.closest(".st-card") : null;
    if (card && card !== oldEl) {
      card.querySelectorAll(".u-seg").forEach(function (n) {
        if (n !== oldEl) n.remove();
      });
    }
    parent.replaceChild(fresh.el, oldEl);
  }
  refs.appearanceSeg = fresh;
}

function buildWallpaperPanel() {
  const statusLine = el("p", { class: "st-hint", text: "" });
  // 历史默认折叠：只有一行摘要，点开才铺网格。攒到 60 张时否则会把设置页
  // 拉成几万像素高。
  const histWrap = el("div", { class: "wp-hist", hidden: true });
  const histList = el("div", { class: "wp-hist-list" });
  const histSummary = el("span", { class: "wp-hist-sum", text: "已保存的壁纸" });
  const histCaret = el("span", { class: "wp-hist-caret", "aria-hidden": "true", text: "▸" });
  const histToggle = el("button", {
    class: "wp-hist-toggle", type: "button", "aria-expanded": "false",
    onclick: function () { toggleWallpaperHistory(); },
  }, [histCaret, histSummary]);
  const histBox = el("div", { class: "wp-hist-box" }, [histToggle, histWrap]);
  const saveToggle = el("input", { type: "checkbox", id: "wp-save-hist" });
  const uploadInput = el("input", {
    type: "file", id: "wp-upload", accept: "image/*", hidden: true,
  });
  const uploadBtn = el("button", {
    class: "u-btn u-btn--ghost u-btn--sm", type: "button", text: "上传背景",
    onclick: function () { uploadInput.click(); },
  });
  const refreshBtn = el("button", {
    class: "u-btn u-btn--ghost u-btn--sm", type: "button", text: "立即同步",
    onclick: function () { refreshWallpaper(true); },
  });
  const row = el("div", { class: "wp-actions" }, [refreshBtn, uploadBtn, uploadInput]);

  uploadInput.addEventListener("change", function () {
    const f = uploadInput.files && uploadInput.files[0];
    uploadInput.value = "";
    if (f) uploadWallpaper(f);
  });
  saveToggle.addEventListener("change", function () {
    setWallpaperHistory(!!saveToggle.checked);
  });

  const el_ = el("div", { class: "wp-panel", hidden: true }, [
    statusLine,
    el("label", { class: "check-line" }, [
      saveToggle, document.createTextNode(" 保存历史壁纸（之后可随时切回）"),
    ]),
    row,
    histBox,
  ]);
  histWrap.append(histList);
  return {
    el: el_, statusLine, histList, histWrap, saveToggle,
    histSummary, histCaret, histToggle, expanded: false,
  };
}

// 展开/收起历史网格。摘要行始终可见，折叠时只显示数量与最近一张日期。
function toggleWallpaperHistory(force) {
  const panel = refs && refs.wpPanel;
  if (!panel || !panel.histWrap) return;
  const next = typeof force === "boolean" ? force : !panel.expanded;
  panel.expanded = next;
  panel.histWrap.hidden = !next;
  panel.histToggle.setAttribute("aria-expanded", next ? "true" : "false");
  if (panel.histCaret) panel.histCaret.textContent = next ? "▾" : "▸";
}

function wpApi(name, args) {
  if (!(window.pywebview && window.pywebview.api)) return null;
  const fn = window.pywebview.api[name];
  if (typeof fn !== "function") return null;
  return fn.apply(window.pywebview.api, args || []);
}

async function refreshWallpaper(force) {
  const panel = refs && refs.wpPanel;
  if (!panel) return;
  let st = null;
  try {
    st = await (force ? wpApi("wallpaper_refresh", [true])
                      : wpApi("wallpaper_status", []));
  } catch (e) {
    panel.statusLine.textContent = "壁纸状态读取失败";
    return;
  }
  if (!st || !st.ok) {
    panel.statusLine.textContent = (st && st.error) || "壁纸同步失败";
    return;
  }
  panel.saveToggle.checked = !!st.save_history;
  const cur = st.current;
  if (!cur) {
    panel.statusLine.textContent = "尚未缓存壁纸，点「立即同步」获取今天的图。";
  } else {
    const stale = st.outdated && st.latest_date
      ? "（Bing 已更新为 " + st.latest_date + "，点「立即同步」换新）" : "";
    panel.statusLine.textContent = "当前：" + (cur.date || "—")
      + (cur.copyright ? " · " + cur.copyright : "") + stale;
    if (cur.file) setWallpaperImage(cur.file);
  }
  renderWallpaperHistory();
}

async function renderWallpaperHistory() {
  const panel = refs && refs.wpPanel;
  if (!panel) return;
  let r = null;
  try { r = await wpApi("wallpaper_history", []); } catch (e) { return; }
  const items = (r && r.ok && Array.isArray(r.items)) ? r.items : [];
  panel.histList.textContent = "";
  panel.histToggle.hidden = items.length === 0;
  if (!items.length) {
    toggleWallpaperHistory(false);
  }
  if (panel.histSummary) {
    const latest = items.length ? (items[0].date || "") : "";
    panel.histSummary.textContent = items.length
      ? "已保存的壁纸 · " + items.length + " 张"
        + (latest ? "（最近 " + latest + "）" : "")
      : "已保存的壁纸";
  }
  items.forEach(function (it) {
    const isUpload = String(it.date || "").startsWith("upload-");
    const thumb = el("img", {
      class: "wp-thumb", src: "/wallpaper/" + it.file, alt: it.title || it.date || "",
      loading: "lazy",
    });
    const dateLabel = el("span", {
      class: "wp-date", text: isUpload ? "自定义背景" : (it.date || ""),
    });
    const badge = isUpload
      ? el("span", { class: "wp-badge", text: "自定义" })
      : null;
    const line1 = el("div", { class: "wp-line1" }, badge ? [dateLabel, badge] : [dateLabel]);
    const titleText = String(it.title || it.copyright || "");
    const titleEl = el("div", {
      class: "wp-title", title: titleText, text: titleText || "（无标题）",
    });
    const use = el("button", {
      class: "wp-use", type: "button", text: "使用",
      title: "使用这张壁纸",
      onclick: function () { selectWallpaper(it.date, use); },
    });
    const thumbWrap = el("div", { class: "wp-thumbwrap" }, [thumb, use]);
    const del = el("button", {
      class: "wp-del", type: "button", text: "✕", title: "删除这张",
      onclick: function () { deleteWallpaper(it.date, del); },
    });
    panel.histList.append(el("article", { class: "wp-item" }, [
      thumbWrap, el("div", { class: "wp-meta" }, [line1, titleEl]), del,
    ]));
  });
}

function setWallpaperImage(file) {
  const back = document.getElementById("sh-backdrop");
  if (back && file) back.style.backgroundImage = 'url("/wallpaper/' + file + '")';
}

async function selectWallpaper(date, btn) {
  btn.disabled = true;
  let r = null;
  try { r = await wpApi("wallpaper_select", [date]); } catch (e) {}
  btn.disabled = false;
  if (!r || !r.ok) {
    toast("error", "切换壁纸失败", (r && r.error) || "未知错误");
    return;
  }
  if (r.current && r.current.file) setWallpaperImage(r.current.file);
  refreshWallpaper(false);
}

async function deleteWallpaper(date, btn) {
  btn.disabled = true;
  let r = null;
  try { r = await wpApi("wallpaper_delete", [date]); } catch (e) {}
  btn.disabled = false;
  if (!r || !r.ok) {
    toast("error", "删除失败", (r && r.error) || "未知错误");
    return;
  }
  refreshWallpaper(false);
}

async function setWallpaperHistory(enabled) {
  let r = null;
  try { r = await wpApi("set_wallpaper_history", [enabled]); } catch (e) {}
  if (!r || !r.ok) {
    toast("error", "保存设置失败", (r && r.error) || "未知错误");
    return;
  }
  refreshWallpaper(false);
}

async function uploadWallpaper(file) {
  const name = encodeURIComponent(file.name || "upload.jpg");
  let resp = null;
  try {
    resp = await fetch("/wallpaper/upload?name=" + name, {
      method: "POST",
      body: file,
      headers: { "content-type": file.type || "application/octet-stream" },
    });
  } catch (e) {
    toast("error", "上传失败", (e && e.message) || String(e));
    return;
  }
  let body = null;
  try { body = await resp.json(); } catch (e) {}
  if (!resp.ok || !body || !body.ok) {
    toast("error", "上传失败", (body && body.error) || ("HTTP " + resp.status));
    return;
  }
  if (body.current && body.current.file) setWallpaperImage(body.current.file);
  refreshWallpaper(false);
}

function onAppearanceSeg(mode) {
  if (segSilent) return;
  setAppearance(mode);
}

async function setAppearance(mode) {
  try { localStorage.setItem("appearance", mode); } catch (e) {}
  applyAppearance(mode);
  let r = null;
  try {
    const ready = await ensureApi();
    if (!ready) throw new Error("bridge not ready");
    r = await window.pywebview.api.set_appearance(mode);   // 同步直调
  } catch (e) {
    return toast("error",
      "外观设置保存失败（本地已切换，重启应用后需重新选择）",  // app.js:2578 逐字
      (e && e.message) || String(e));
  }
  if (!r || !r.ok) {
    toast("error",
      "外观设置保存失败（本地已切换，重启应用后需重新选择）",  // app.js:2581 逐字
      (r && (r.error || r)) || "后端保存未生效");
  }
}

// ============================================================
// 下载目录（app.js:2486-2542；锁/失败可见/成功才提示）
// ============================================================
async function chooseFolder(input, btn) {
  if (folderPickBusy) return;                       // app.js:2487 互斥锁
  folderPickBusy = true;
  const restore = busyOn(btn, null);
  let r = null;
  try {
    const ready = await ensureApi();
    if (!ready) throw new Error("bridge not ready");
    r = await window.pywebview.api.choose_folder();  // 同步直调
  } catch (e) {
    restore();
    folderPickBusy = false;
    toast("error", "选择目录失败", (e && e.message) || String(e));  // app.js:2495
    return;
  }
  restore();
  folderPickBusy = false;
  if (!r || !r.ok) {
    // 用户取消保持静默（app.js:2498-2499 有 path 才回填；错误可见）
    if (r && r.error) toast("error", "选择目录失败", r.error);
    return;
  }
  if (r.path) input.value = String(r.path);
}

async function openRoot(input, btn) {
  if (openRootBusy) return;                         // app.js:2506 防重
  openRootBusy = true;
  const restore = busyOn(btn, null);
  let r = null;
  try {
    const ready = await ensureApi();
    if (!ready) throw new Error("bridge not ready");
    r = await window.pywebview.api.open_folder(input.value);  // 同步直调
  } catch (e) {
    restore();
    openRootBusy = false;
    toast("error", "打开目录失败", (e && e.message) || String(e));  // app.js:2516
    return;
  }
  restore();
  openRootBusy = false;
  if (!r || !r.ok) {
    toast("error", "打开目录失败",                  // app.js:2518
      (r && r.error) || "目录不存在或无法打开");
  }
}

async function saveRoot(input, btn) {
  if (saveRootBusy) return;                         // app.js:2525 防重
  saveRootBusy = true;
  const restore = busyOn(btn, "保存中…");
  let r = null;
  try {
    const ready = await ensureApi();
    if (!ready) throw new Error("bridge not ready");
    r = await window.pywebview.api.set_download_root(input.value);  // 同步直调
  } catch (e) {
    restore();
    saveRootBusy = false;
    toast("error", "保存下载目录失败", (e && e.message) || String(e));  // app.js:2535
    return;
  }
  restore();
  saveRootBusy = false;
  if (!r || !r.ok) {
    toast("error", "保存下载目录失败",              // app.js:2538
      (r && r.error) || "保存未生效");
    return;
  }
  toast("ok", "已保存");                            // app.js:2541（仅真正成功）
}

// ============================================================
// 账号区渲染（app.js:3260-3323；T46 游客口径 + 降级行）
// ============================================================
async function refreshAccounts() {
  try {
    const ready = await ensureApi();
    if (!ready) throw new Error("bridge not ready");
    const r = await window.pywebview.api.account_list();   // 同步直调
    if (r && r.ok && Array.isArray(r.accounts)) {
      accounts = normAccounts(r.accounts);
      acctSource = "rpc";
    } else {
      accounts = [];
      acctSource = "none";
    }
  } catch (e) {
    // HTTP 非 2xx/网络错：安静降级空态（app.js:3168-3175；console.debug
    // 在新壳纪律下取消——静默降级，降级行仍可见可操作）
    accounts = [];
    acctSource = "none";
  }
  logoutBusy = false;
  renderAccounts();
  scheduleWarmRefresh();   // T56 Bug3：存在 connecting 行时按节拍补刷
}

function mainAccount() {
  return accounts.find(function (a) { return a.id === "main"; }) ||
    accounts[0] || null;
}

function altAccounts() {
  return accounts.filter(function (a) {
    return a.id && a.id !== "main" && a.state !== "absent";
  });
}

function renderAccounts() {
  if (!refs) return;
  const region = refs.acctRegion;
  while (region.firstChild) region.removeChild(region.firstChild);
  setErr("");

  // 游客/未登录：账号行="未登录 · 点此登录"，不显添加小号控件（T46 口径；
  // 2.1 游客分支同步 closeAddQuiet，app.js:3266-3272）
  if (loginMode !== "connected") {
    region.append(guestRow());
    refs.btnAdd.hidden = true;
    closeAddQuiet();
    return;
  }

  if (!accounts.length) {
    // 后端方法缺失/返回异常：安静降级"当前账号"口径（app.js:3274-3288）
    region.append(fallbackRow());
    refs.btnAdd.hidden = true;
    return;
  }
  const main = mainAccount();
  if (main) region.append(mainRow(main));
  // 2.1 #acct-region 单区：小号行随主号行同区排下（app.js:3309-3321）
  for (const a of altAccounts()) region.append(altRow(a));
  refs.btnAdd.hidden = false;
}

// app.js:3268-3270 游客行（旧 onclick=openLoginFromGuest → 新壳 /login）
function guestRow() {
  return el("div", {
    class: "st-guest-row", role: "listitem", tabindex: "0",
    text: "未登录 · 点此登录",
    onclick: function () { navigate("/login"); },
    onkeydown: function (e) {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        navigate("/login");
      }
    },
  });
}

// 降级行（app.js:3275-3286）：主号 tag + me 资料兜底 + 退出登录
function fallbackRow() {
  const m = meCache || {};
  return el("div", { class: "st-row", role: "listitem" }, [
    el("span", { class: "st-tag", text: "主号" }),
    el("div", { class: "st-info" }, [
      el("div", { class: "st-name", text: m.name || "未登录" }),
      el("div", {
        class: "st-asub",
        text: m.username ? "@" + m.username : (m.phone || ""),
      }),
    ]),
    logoutButton(),
  ]);
}

function mainRow(main) {
  const standby = uiState(main, warmDone) === "standby";
  const nodes = [
    el("span", { class: "st-tag", text: "主号" }),
    el("div", { class: "st-info" }, [
      el("div", { class: "st-name", text: nameOf(main) }),
      el("div", { class: "st-asub", text: subOf(main) }),
    ]),
    el("span", {
      class: "st-state", dataset: { st: uiState(main, warmDone) },
      text: stateText(main, hint, warmDone),
    }),
  ];
  if (standby) {
    // T58 契约 3.4（R6）：待连接行重试（app.js:3476-3502）
    const reconnErr = el("span", { class: "st-reconn-err", text: "" });
    const btnReconn = el("button", {
      class: "u-btn u-btn--ghost st-sm", type: "button", text: "重试连接",
      onclick: function () { reconnectMain(btnReconn, reconnErr); },
    });
    nodes.push(btnReconn, reconnErr);
  }
  nodes.push(logoutButton());
  return el("div", {
    class: "st-row", role: "listitem", dataset: { aid: main.id },
  }, nodes);
}

function altRow(a) {
  return el("div", {
    class: "st-row st-row--alt", role: "listitem", dataset: { aid: a.id },
  }, [
    el("span", { class: "st-tag st-tag--alt", text: "小号" }),
    el("div", { class: "st-info" }, [
      el("div", { class: "st-name", text: nameOf(a) }),
      el("div", {
        class: "st-asub",
        text: a.id + " · " + (subOf(a) || "—"),   // app.js:3315 占位
      }),
    ]),
    el("span", {
      class: "st-state", dataset: { st: uiState(a, warmDone) },
      text: stateText(a, hint, warmDone),
    }),
    el("button", {
      class: "u-btn u-btn--ghost st-sm", type: "button", text: "移除",
      onclick: function () { askRemove(a.id); },
    }),
  ]);
}

function logoutButton() {
  return el("button", {
    class: "u-btn u-btn--danger st-sm", type: "button", text: "退出登录",
    disabled: logoutBusy,
    onclick: function (e) { doLogout(e.currentTarget); },
  });
}

// 2.1 .login-err 口径（style.css:250）：只写文案、不隐现——常驻 min-height
// 占位（app.js:3265/3362 同口径），卡内节奏与 2.1 逐像素一致
function setErr(msg) {
  if (!refs || !refs.acctErr) return;
  refs.acctErr.textContent = msg || "";
}

// ============================================================
// 暖机补刷（app.js:3200-3231 T56 Bug3）：3s 节拍 × 上限 6 次；
// 全部非 connecting 提前停；document.hidden 拍跳过；窗口尽后
// has_session 行转"待连接"。
// ============================================================
function stopWarm() {
  if (warmTimer != null) {
    clearTimeout(warmTimer);
    warmTimer = null;
  }
}

function resetWarmWindow() {
  warmTicks = 0;
  warmDone = false;
  stopWarm();
}

function scheduleWarmRefresh() {
  const hasConnecting = accounts.some(function (a) {
    return a.state === "connecting";
  });
  if (!hasConnecting || warmDone || warmTimer != null) return;
  warmTimer = setTimeout(warmTick, WARM_TICK_MS);
}

async function warmTick() {
  warmTimer = null;
  if (document.hidden) {
    warmTimer = setTimeout(warmTick, WARM_TICK_MS);   // 隐藏拍不发不计数
    return;
  }
  warmTicks++;
  if (warmTicks >= WARM_MAX_TICKS) warmDone = true;   // 先置满再落图
  await refreshAccounts();
}

// ============================================================
// 主号待连接重试（app.js:3476-3502 T58 契约 3.4）
// 成功→刷新行（按钮随行重建消失）；失败→行内错误（不弹层）。
// ============================================================
async function reconnectMain(btn, errSlot) {
  const restore = busyOn(btn, "重试中…");
  let r = null;
  try {
    const ready = await ensureApi();
    if (!ready) throw new Error("bridge not ready");
    r = await window.pywebview.api.connect();         // 同步直调
  } catch (e) {
    restore();
    errSlot.textContent = (e && e.message) || String(e);
    return;
  }
  restore();
  if (r && r.ok) {
    resetWarmWindow();
    await refreshAccounts();
    return;
  }
  errSlot.textContent = (r && (r.error || r.warning)) || "连接失败";
}

// ============================================================
// 添加小号三步卡（app.js:3325-3442；T58 r2 契约 3.2 逐条承接）
// ============================================================
function buildAddCard() {
  const inputPhone = el("input", {
    id: "st-add-phone", type: "tel", class: "st-input",   /* FR-24 数字键盘正确（T17） */
    placeholder: "8613800000000", autocomplete: "off",
  });
  const inputCode = el("input", {
    id: "st-add-code", type: "text", class: "st-input",
    placeholder: "如 12345", autocomplete: "off",
  });
  const inputPwd = el("input", {
    id: "st-add-pwd", type: "password", class: "st-input",
    placeholder: "2FA 密码", autocomplete: "off",
  });
  const addErrEl = el("p", { class: "st-err-line", role: "alert", text: "" });

  const btnRecover = el("button", {
    class: "u-btn u-btn--ghost st-block", type: "button", hidden: true,
    // index.html:356 逐字（T58 补充1 可恢复入口）
    text: "已收到验证码？直接输入",
    onclick: function () { addRecover(); },
  });

  const stepPhone = el("div", { class: "st-step" }, [
    el("label", { class: "st-label", for: "st-add-phone",
      text: "小号手机号（含国家区号，不带 +）" }),
    inputPhone,
    el("button", {
      class: "u-btn u-btn--primary st-block", type: "button", text: "发送验证码",
      onclick: function (e) { addStart(e.currentTarget); },
    }),
    btnRecover,
    el("button", {
      class: "u-btn u-btn--ghost st-block", type: "button", text: "取消",
      onclick: function () { closeAdd(); },
    }),
  ]);

  const stepCode = el("div", { class: "st-step", hidden: true }, [
    el("label", { class: "st-label", for: "st-add-code",
      text: "Telegram 发来的验证码" }),
    inputCode,
    el("button", {
      class: "u-btn u-btn--primary st-block", type: "button", text: "验证并登录",
      onclick: function (e) { verifyCode(e.currentTarget); },
    }),
    el("button", {
      class: "u-btn u-btn--ghost st-block", type: "button", text: "返回",
      onclick: function () { addCodeBack(); },
    }),
    el("button", {
      class: "u-btn u-btn--ghost st-block", type: "button", text: "取消",
      onclick: function () { closeAdd(); },
    }),
  ]);

  const stepPwd = el("div", { class: "st-step", hidden: true }, [
    el("label", { class: "st-label", for: "st-add-pwd", text: "两步验证密码" }),
    inputPwd,
    el("button", {
      class: "u-btn u-btn--primary st-block", type: "button", text: "确认",
      onclick: function (e) { verifyPassword(e.currentTarget); },
    }),
    el("button", {
      class: "u-btn u-btn--ghost st-block", type: "button", text: "返回",
      onclick: function () { addPwdBack(); },
    }),
    el("button", {
      class: "u-btn u-btn--ghost st-block", type: "button", text: "取消",
      onclick: function () { closeAdd(); },
    }),
  ]);

  const elRoot = el("div", {
    class: "st-add-card", hidden: true,
  }, [stepPhone, stepCode, stepPwd, addErrEl]);

  return {
    el: elRoot,
    stepPhone: stepPhone, stepCode: stepCode, stepPwd: stepPwd,
    inputPhone: inputPhone, inputCode: inputCode, inputPwd: inputPwd,
    btnRecover: btnRecover, errEl: addErrEl,
  };
}

function setAddStep(step, keepRecover) {
  if (!refs || !refs.add) return;
  const a = refs.add;
  a.stepPhone.hidden = step !== "phone";
  a.stepCode.hidden = step !== "code";
  a.stepPwd.hidden = step !== "pwd";
  if (step === "phone" && !keepRecover) hideAddRecover();  // T58 补充1
}

function openAdd() {
  if (!refs || !refs.add) return;
  resetWarmWindow();   // T56 补充②：显式添加=新暖机会话
  addingId = nextAltId(accounts.map(function (a) { return a.id; }));
  const a = refs.add;
  a.inputPhone.value = "";
  a.inputCode.value = "";
  a.inputPwd.value = "";
  addErr("");
  setAddStep("phone");
  a.el.hidden = false;
}

function closeAdd() {
  if (!refs || !refs.add) return;
  refs.add.el.hidden = true;
  addErr("");
  addingId = null;
}

function closeAddQuiet() {
  if (!refs || !refs.add) return;
  if (!refs.add.el.hidden) {
    refs.add.el.hidden = true;
    addingId = null;
  }
}

function addErr(msg) {
  if (!refs || !refs.add) return;
  refs.add.errEl.textContent = msg || "";   // 2.1 同 setErr 口径（app.js:3362）
}

function showAddRecover() {
  if (refs && refs.add) refs.add.btnRecover.hidden = false;
}

function hideAddRecover() {
  if (refs && refs.add) refs.add.btnRecover.hidden = true;
}

// T58 批准书补充1：恢复入口（仅 recoverable 显示，点击才进码步）
function addRecover() {
  hideAddRecover();
  setAddStep("code");
}

// T58 r2 契约 3.2：返回=纯前端切步（不调 RPC、不清 addingId/输入值、
// keepRecover 保持恢复按钮实况）
function addCodeBack() { setAddStep("phone", true); }
function addPwdBack() { setAddStep("code"); }

async function addStart(btn) {
  addErr("");
  hideAddRecover();   // T58 补充1：新一轮发送即重置
  const phone = refs.add.inputPhone.value.trim();
  if (!phone) return addErr("请输入手机号");
  const restore = busyOn(btn, "发送中…");
  let r = null;
  try {
    const ready = await ensureApi();
    if (!ready) throw new Error("bridge not ready");
    r = await window.pywebview.api.account_add_start(addingId, phone);  // 同步直调
  } catch (e) {
    restore();
    return addErr("发送失败：" + ((e && e.message) || e));   // app.js:3399
  }
  restore();
  const c = classifyAddStart(r);
  if (c === "code") {
    // 软成功 warning 原句先落错误行再切码步（app.js:3402-3405）
    if (r.warning) addErr(r.warning);
    setAddStep("code");
  } else if (c === "recover") {
    addErr(r.error);
    showAddRecover();
  } else {
    addErr((r && r.error) || "发送验证码失败");
  }
}

async function verifyCode(btn) {
  addErr("");
  const restore = busyOn(btn, "验证中…");
  let r = null;
  try {
    const ready = await ensureApi();
    if (!ready) throw new Error("bridge not ready");
    r = await window.pywebview.api.account_verify_code(
      addingId, refs.add.inputCode.value.trim());            // 同步直调
  } catch (e) {
    restore();
    return addErr("验证失败：" + ((e && e.message) || e));   // app.js:3422
  }
  restore();
  const c = classifyVerifyCode(r);
  if (c === "pwd") setAddStep("pwd");
  else if (c === "done") { closeAdd(); refreshAccounts(); }
  else addErr((r && r.error) || "验证码错误，请重试");
}

async function verifyPassword(btn) {
  addErr("");
  const restore = busyOn(btn, "确认中…");
  let r = null;
  try {
    const ready = await ensureApi();
    if (!ready) throw new Error("bridge not ready");
    r = await window.pywebview.api.account_verify_password(
      addingId, refs.add.inputPwd.value);                    // 同步直调
  } catch (e) {
    restore();
    return addErr("确认失败：" + ((e && e.message) || e));   // app.js:3438
  }
  restore();
  if (r && r.ok) { closeAdd(); refreshAccounts(); }
  else addErr((r && r.error) || "两步验证密码错误");
}

// ============================================================
// 移除小号（app.js:3445-3474；二次确认文案逐字 + busy 拒绝语义）
// ============================================================
async function askRemove(id) {
  removingId = id;
  const a = accounts.find(function (x) { return x.id === id; });
  const ok = await confirmAskP({
    title: "移除小号？",                                  // index.html:383 逐字
    // index.html:384 + app.js:3448-3449 动态串（逐字模板）
    content: (nameOf(a) || id) + "：仅移除本地登录授权，不影响已下载任务。",
    confirmLabel: "移除",
    danger: true,
  });
  const rid = removingId;
  removingId = null;
  if (!ok || !rid) return;
  let r = null;
  try {
    const ready = await ensureApi();
    if (!ready) throw new Error("bridge not ready");
    r = await window.pywebview.api.account_remove(rid);      // 同步直调
  } catch (e) {
    setErr("移除失败：" + ((e && e.message) || e));          // app.js:3465
    return;
  }
  if (r && r.ok) {
    resetWarmWindow();
    await refreshAccounts();
    return;
  }
  setErr(removeFailText(r));
}

// ============================================================
// 退出登录（app.js:632-693 doLogout + 585-630 提升语义）
// ============================================================
async function doLogout(btn) {
  if (btn && btn.disabled) return;                  // app.js:634 在途防重
  logoutBusy = true;
  if (btn) btn.disabled = true;
  try {
    // is_active 守卫（app.js:637-640 alert 逐字 → toast 承接）
    try {
      const ready = await ensureApi();
      if (!ready) throw new Error("bridge not ready");
      if (await window.pywebview.api.is_active()) {
        toast("error",
          "有下载任务正在进行，请先到「下载中」中止下载后再退出。");
        return;
      }
    } catch (e) { /* 探测失败不拦截（app.js:640 空 catch 原状） */ }

    // T56 Bug5：有小号（任何在列行）→ 提升语义（app.js:642-650）
    let hasAlt = false;
    try {
      const al = await window.pywebview.api.account_list();
      if (al && al.ok && Array.isArray(al.accounts))
        hasAlt = al.accounts.some(function (x) {
          return x.id && x.id !== "main";
        });
    } catch (e) {}

    if (hasAlt) {
      const ok = await confirmAskP({
        title: "退出当前主号？",                        // app.js:593 逐字
        content: "登录中的小号将自动成为主号；本地任务与已下载文件不受影响。",
        confirmLabel: "退出并提升小号",                  // app.js:597 逐字
        danger: true,
      });
      if (!ok) return;
    } else {
      const ok = await confirmAskP({
        title: "退出当前账号？",                        // app.js:650 逐字首行
        content: el("div", { class: "st-confirm-lines" }, [
          // app.js:650 confirm 串逐行逐字（换行改节点承载）
          el("p", { text: "· 本地登录信息将被删除，重新登录需输入手机号与验证码" }),
          el("p", { text: "· 任务清单与已下载视频不受影响" }),
        ]),
        confirmLabel: "确定",
        danger: true,
      });
      if (!ok) return;
    }

    let r = null;
    try {
      r = await window.pywebview.api.logout();             // 同步直调
    } catch (e) {
      toast("error", "退出登录失败", (e && e.message) || String(e));
      return;
    }
    if (!r || !r.ok) {
      toast("error", (r && r.error) || "退出登录失败");   // app.js:652 逐字兜底
      return;
    }
    // T56 Bug5：小号提升为主号——不弹登录卡不重置（app.js:654/609-630）
    if (r.stage === "promoted") {
      await afterPromote(r);
      return;
    }
    // stage=logged_out：清光回登录（app.js:656-689 的新壳等价收口）
    loginMode = "anonymous";
    accounts = [];
    acctSource = "none";
    meCache = null;
    hint = null;
    resetWarmWindow();
    closeAddQuiet();
    renderAccounts();
    navigate("/login");
  } finally {
    logoutBusy = false;
    if (btn) btn.disabled = false;
  }
}

// app.js:613-630 afterPromote：资料刷新 + 账号区刷新 + 轻提示
async function afterPromote(r) {
  try {
    const m = await window.pywebview.api.me();
    if (m && m.ok) meCache = m;
  } catch (e) {
    // warning 形态（重连失败）下 me 可能拿不到：保持原资料不阻断
    //（app.js:620-626）
  }
  resetWarmWindow();
  await refreshAccounts();
  toast("ok", "小号已提升为主号",                       // app.js:628 逐字
    r && r.warning ? "重新连接中，重启后可自动恢复" : null);
}
