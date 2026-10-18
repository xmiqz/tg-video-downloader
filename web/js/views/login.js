// ============================================================
// views/login.js — T11 登录视图（v2.2 W4-a）
//
// 契约：
//   t8_contract.md §1 模块形状 { path, title, mount, onEnter, onLeave }；
//   t58_contract.md 登录三步可靠性（七类异常/recoverable 入口/软成功
//   warning/me 重试/login_state 三态/guest 轮询）；
//   t10_contract.md 组件库（本文件 import ../ui/index.js 桶，仅此入口）。
//
// 行为对齐（旧壳出处，均为只读参照）：
//   app.js bootConnect(209-272)/tryConnect(288-310)/showLoginStep(317-323)/
//   doSendCode(447-474)/doVerifyCode(475-489)/doVerifyPassword(490-502)/
//   afterLogin(503-531)/renderMe(535-540)/showMeError(548-558)/retryMe
//   (562-579)/doGuest(418-440)/guestPollTick(394-415)。
//
// 纪律：零 alert/confirm/prompt（错误一律内联）；零新 RPC（只走
// window.pywebview.api 既有方法）；模块顶层零副作用（一切 DOM/监听/轮询
// 在 mount/onEnter 内起、onLeave 全清理）；动效仅 --dur-*/--ease-* 令牌。
// ============================================================

import { el, createCard, createOverlay, createToaster } from "../ui/index.js";

// ------------------------------------------------------------
// ① 契约导出（t8_contract.md §1 恰五员）
// ------------------------------------------------------------
// T17 手势增量（聚焦保持；登录流无返回目标，刻意不挂边缘返回——见 t17_diff_list.md）
import { attachFocusKeep } from "../gestures.js";
let gsDetach = [];

export const path = "login";

// 非空静态 title：router 进入视图后写入 document.title（保 T8 占位原值，
// 禁改——T8 TR-8.1 E2 断言锚点）。
export const title = "登录 - TG 视频下载器";

// ------------------------------------------------------------
// ② seam 纯函数（T11 任务书 §3 S1–S6；命名导出仅供测试，零 DOM 依赖）
// ------------------------------------------------------------

// S1 login_state → "connected" | "guest" | "anonymous"。
// 兜底（T58 2.3 三态口径 + app.js bootConnect/guestPollTick 消费规则纯化）：
//   logged_in===true 且 guest!==true → connected（已授权就绪）；
//   否则 guest===true → guest（游客态，含矛盾形状 logged_in&&guest ——
//     logged_in 须 !guest 才立，游客态保留轮询待快照自愈）；
//   其余（null/缺字段/字符串形状）→ anonymous。
export function resolveEntryState(st) {
  if (st && typeof st === "object") {
    if (st.logged_in === true && st.guest !== true) return "connected";
    if (st.guest === true) return "guest";
  }
  return "anonymous";
}

// S2 send_code 返回 → {step:"code",warning} | {recover:true,error} | {error}
// （app.js doSendCode 447-474 纯化；T58 3.2 分支唯一入口）。
export function resolveSendCodeOutcome(r, fallbackError) {
  const fallback = fallbackError || "发送验证码失败";
  if (r && typeof r === "object") {
    if (r.ok === true) {
      // 完全成功 / 软成功（SendCodeUnavailable——码经 App 消息已到）：
      // 只信 ok → 码步；warning 原句透传内联（软成功文案）。
      return { step: "code", warning: r.warning || "" };
    }
    if (r.recoverable === true) {
      // 网络抖动（码可能已发出但响应回程失败）：停 phone 步 + 明示
      // 恢复入口；缺 error 时给可用兜底文案。
      return {
        recover: true,
        error: r.error || "网络异常，验证码可能已发送到你的 Telegram；如已收到可直接点下方入口输入",
      };
    }
    // flood_wait（文案含秒数，后端拼好透传）/bad_phone/banned/api_invalid/
    // AuthKeyDuplicated/其余：显错无恢复入口。
    return { error: r.error || fallback };
  }
  return { error: fallback };
}

// S3 verify_code / verify_password 共用 → "ready" | "need_password" | {error}
// （app.js doVerifyCode 475-489 / doVerifyPassword 490-502 纯化：
//   ok+ready → 成功；ok+need_password → 密步；ok 其他 stage / !ok / null →
//   {error}（旧壳 loginErr(m||"") 空文案留当前步）；异常前缀由调用方拼）。
export function resolveVerifyOutcome(r) {
  if (r && typeof r === "object") {
    if (r.ok === true && r.stage === "ready") return "ready";
    if (r.ok === true && r.stage === "need_password") return "need_password";
    return { error: r.error || "" };
  }
  return { error: "" };
}

// S4 步骤机（task §3.S4）：三步向导 phone→code→pwd 单链；
//   phone 不可再退（prevStep 钳制）、pwd 末步钳制；未知输入安全默认
//   phone。返回上一步纯前端切步零 RPC、保值（TR11-1 走查）。
const STEP_CHAIN = ["phone", "code", "pwd"];
export function nextStep(cur) {
  const i = STEP_CHAIN.indexOf(cur);
  if (i < 0) return "phone";
  return STEP_CHAIN[Math.min(i + 1, STEP_CHAIN.length - 1)];
}
export function prevStep(cur) {
  const i = STEP_CHAIN.indexOf(cur);
  if (i < 0) return "phone";
  return STEP_CHAIN[Math.max(i - 1, 0)];
}

// S5 游客态轮询裁决 → "connected" | "logged_out" | "keep"
// （app.js guestPollTick 394-415 纯化；T58 FE-D：发码中 poll 不收卡）。
export function resolvePollTick(st) {
  if (st && typeof st === "object") {
    if (st.logged_in === true && st.guest !== true) return "connected";
    if (st.guest !== true && st.logged_in !== true) return "logged_out";
  }
  return "keep";
}

// S6 输入清洗（旧壳口径：手机号/验证码仅 trim —— app.js:450/481，
// 零自创校验不拦合法输入；密码敏感不 trim —— app.js:496 直取 .value）。
export function sanitizePhone(v) {
  return String(v == null ? "" : v).trim();
}
export function sanitizeCode(v) {
  return String(v == null ? "" : v).trim();
}
export function sanitizePwd(v) {
  return String(v == null ? "" : v);
}

// ------------------------------------------------------------
// ③ 视图实现（mount 内构建一次、keep-alive；onEnter 重入重判）
// ------------------------------------------------------------

const POLL_MS = 2000;           // 轮询节拍（app.js:386 setInterval 2000）
const BRIDGE_TIMEOUT_MS = 8000; // bridge 就绪等待上限（桩宿主立即就绪）

let root = null;                // 视图根节点缓存（mount 恰一次）
let epoch = 0;                  // onEnter 世代（重入/离开使旧异步回调失效）
let pollId = null;              // 游客态轮询句柄（onLeave 必清）
let pollBusy = false;
let busy = false;               // 提交互斥（防重复发码/重复校验）

const $ = {};                   // 节点引用（mount 时赋值）
const stepEls = {};             // step 名 → 步骤块元素（phone|code|pwd，动态访问映射）
const submitLabels = new Map(); // busy 换文案用：按钮 -> 默认文案

// —— 桥就绪（bridge 未起时等 pywebviewready；解析后监听即除）——
function bridgeReady(timeoutMs) {
  return new Promise(function (resolve) {
    let done = false;
    function finish(v) {
      if (done) return;
      done = true;
      window.removeEventListener("pywebviewready", finish);
      clearTimeout(timer);
      resolve(v);
    }
    const timer = setTimeout(function () { finish(false); }, timeoutMs);
    window.addEventListener("pywebviewready", finish);
    if (window.pywebview && window.pywebview.api) finish(true);
  });
}

function api() {
  return window.pywebview.api;
}

function idle(my) { return my !== epoch; }

// 导航：去任务库（新壳 router 优先，缺位降级 hash —— 任务书 §1.4）。
function goTasks() {
  const r = window.__t8router;
  if (r && typeof r.navigate === "function") r.navigate("/tasks");
  else window.location.hash = "#/tasks";
}

// —— 登录 = 叠加弹层唯一入口（active/new 的 need_login 与受限动作共用）——
// 受控提示暂存 pendingNotice，弹层重开时由 onEnter 消费（2.1 showLoginCard
// app.js:360-369：phone 步起、清错误、notice 与错误互不残留）。
let pendingNotice = "";
export function openLogin(notice) {
  pendingNotice = notice || "";
  const r = window.__t8router;
  if (r && typeof r.navigate === "function") r.navigate("/login", { force: true });
  else window.location.hash = "#/login";
}

// —— 三步向导 ——
function setStep(step) {
  const s = STEP_CHAIN.indexOf(step) >= 0 ? step : "phone";
  STEP_CHAIN.forEach(function (k) {
    const node = stepEls[k];
    if (node) node.hidden = k !== s;
  });
  // T58 3.2：每次进入 phone 步重置“直接输入”恢复入口。
  if (s === "phone") hideRecover();
}

function hideRecover() {
  if ($.btnRecover) $.btnRecover.hidden = true;
}

// —— 内联错误 / 受控提示（零 alert；role=alert 区域）——
function errShow(msg) {
  if (!$.err) return;
  $.err.textContent = msg || "";
  $.err.hidden = !msg;
}
function noticeShow(msg) {
  if (!$.notice) return;
  $.notice.textContent = msg || "";
  $.notice.hidden = !msg;
}
function errShowAcct(msg) {
  if (!$.acctErr) return;
  $.acctErr.textContent = msg || "";
  $.acctErr.hidden = !msg;
}

// —— busy 呈现（提交即忙态 + 全动作钮禁用防重复，settled 恢复；
//    目标按钮换“发送中…/验证中…/确认中…”文案，app.js withBusy 口径）——
function setBusyUi(on, targetBtn, pendingText) {
  busy = on;
  if ($.btnPhone) $.btnPhone.disabled = on;
  if ($.btnCode) $.btnCode.disabled = on;
  if ($.btnPwd) $.btnPwd.disabled = on;
  if ($.btnGuestEnter) $.btnGuestEnter.disabled = on;
  if ($.btnRetryMe) $.btnRetryMe.disabled = on;
  if (on && targetBtn) {
    if (targetBtn) targetBtn.textContent = pendingText;
  } else {
    submitLabels.forEach(function (label, b) { b.textContent = label; });
  }
  if ($.loginCard) $.loginCard.setAttribute("aria-busy", on ? "true" : "false");
}

async function withSubmit(targetBtn, pendingText, fn) {
  if (busy) return;
  setBusyUi(true, targetBtn, pendingText);
  try {
    await fn();
  } catch (e) {
    // 意外异常兜底（不产生未捕获 rejection；文案由 fn 内部分支处理）。
    console.error("[v22login] 提交异常：", e);
  } finally {
    setBusyUi(false);
  }
}

// —— 三个提交动作（七类 send_code 分支唯一入口 resolveSendCodeOutcome）——
function doSendCode() {
  const phone = sanitizePhone($.inputPhone.value);
  if (!phone) { errShow("请输入手机号"); return; }
  errShow("");
  hideRecover(); // T58 3.2：新一轮发送即重置恢复按钮
  withSubmit($.btnPhone, "发送中…", async function () {
    let r = null;
    let exc = null;
    try { r = await api().send_code(phone); }
    catch (e) { exc = e; }
    const out = resolveSendCodeOutcome(
      r,
      exc ? "发送验证码失败：" + (exc && exc.message ? exc.message : String(exc)) : undefined);
    if (out.step === "code") {
      setStep("code");
      errShow(out.warning || "");   // 软成功 warning 原句内联
    } else if (out.recover) {
      errShow(out.error);           // 停 phone 步显错 + 明示入口
      $.btnRecover.hidden = false;
    } else {
      errShow(out.error);           // flood/bad_phone/banned/api_invalid…无恢复入口
    }
  });
}

function doVerifyCode() {
  errShow("");
  withSubmit($.btnCode, "验证中…", async function () {
    let r = null;
    let exc = null;
    try { r = await api().verify_code(sanitizeCode($.inputCode.value)); }
    catch (e) { exc = e; }
    const out = resolveVerifyOutcome(r);
    if (out === "ready") {
      await afterLoginAction();
    } else if (out === "need_password") {
      setStep("pwd");
    } else {
      // 留码步：可改码重验 / 返回重发（T58 3.2）。
      errShow(exc
        ? "验证码校验失败：" + (exc && exc.message ? exc.message : String(exc))
        : out.error);
    }
  });
}

function doVerifyPassword() {
  errShow("");
  withSubmit($.btnPwd, "确认中…", async function () {
    let r = null;
    let exc = null;
    try { r = await api().verify_password(sanitizePwd($.inputPwd.value)); }
    catch (e) { exc = e; }
    const out = resolveVerifyOutcome(r);
    if (out === "ready") {
      await afterLoginAction();
    } else {
      // 留密步（app.js:490-502）。
      errShow(exc
        ? "两步验证失败：" + (exc && exc.message ? exc.message : String(exc))
        : out.error);
    }
  });
}

// —— 「暂不登录（游客模式）」（2.1 doGuest 单钮合一：app.js:418-440）——
// guest 态（已激活）点击=仅收卡回任务库；anonymous 点击=set_guest(true)
// 成功后收卡跳任务库并显 guest-bar，失败内联显错。
let isGuestEntry = false;
function doGuest() {
  if (isGuestEntry) {
    goTasks();
    return;
  }
  withSubmit($.btnGuestEnter, "进入中…", async function () {
    let r = null;
    let exc = null;
    try { r = await api().set_guest(true); }
    catch (e) { exc = e; }
    if (exc) {
      return errShow("进入游客模式失败：" + (exc && exc.message ? exc.message : String(exc)));
    }
    if (!r || !r.ok) return errShow((r && r.error) || "进入游客模式失败");
    // 2.1 doGuest 成功：showGuestBar()+startGuestPoll（app.js:437-438）→ 跳任务库
    const g = window.__t8guest;
    if (g && typeof g.show === "function") g.show("未登录 · 点此登录");
    const mm = window.__t8me;
    if (mm && typeof mm.reset === "function") mm.reset(); // T19a：侧底 me-box 复位
    goTasks();
  });
}

// —— 登录成功：账号卡 + me（app.js afterLogin 503-531 / T58 3.3）——
function renderMeCard(m) {
  $.meName.textContent = m.name || "已登录";
  $.meUser.textContent = m.username ? "@" + m.username : (m.phone || "");
  $.meAvatar.textContent = (m.name || "?").slice(0, 1); // 首字头像（app.js:539）
  errShowAcct("");
  $.btnRetryMe.hidden = true;
  // T19a：镜像侧底 me-box 三串（router __t8me；2.1 renderMe 同步侧栏口径）
  const mm = window.__t8me;
  if (mm && typeof mm.set === "function") {
    mm.set($.meName.textContent, $.meUser.textContent, $.meAvatar.textContent);
  }
}

function renderMePending() {
  $.meName.textContent = "获取账号资料中…";
  $.meUser.textContent = "";
  $.meAvatar.textContent = "…";
  errShowAcct("");
  $.btnRetryMe.hidden = true;
}

// me 失败：保底“已登录”卡 + 错误文案 + 内联“重试获取资料”
// （T58 3.3 R5：文案不出现“重启应用”；app.js showMeError 548-558）。
function showMeError(text) {
  $.meName.textContent = "已登录";
  $.meUser.textContent = "";
  $.meAvatar.textContent = "·";
  errShowAcct("获取账号资料失败：" + (text || "未知错误"));
  $.btnRetryMe.hidden = false;
  // T19a：镜像侧底 me-box（连兜底三串也精确镜像）
  const mm = window.__t8me;
  if (mm && typeof mm.set === "function") {
    mm.set($.meName.textContent, $.meUser.textContent, $.meAvatar.textContent);
  }
}

async function fetchMe(my) {
  try {
    const m = await api().me();
    if (idle(my)) return;
    if (m && m.ok) renderMeCard(m);
    else showMeError((m && m.error) || "未能获取账号信息");
  } catch (e) {
    if (idle(my)) return;
    showMeError(e && e.message ? e.message : String(e));
  }
}

function showAccountCard() {
  $.loginCard.hidden = true;
  $.acctCard.el.hidden = false;
  renderMePending();
}

function afterLoginAction() {
  stopPoll();
  showAccountCard();
  // 登录成功收起未登录提示条（2.1 afterLogin → hideGuestBar）
  const g = window.__t8guest;
  if (g && typeof g.hide === "function") g.hide();
  return fetchMe(epoch);
}

function doRetryMe() {
  // 手动重试：成功回填资料 + 清错误（T58 3.3 retryMe 562-579）。
  withSubmit($.btnRetryMe, "重试中…", async function () {
    let m = null;
    try { m = await api().me(); }
    catch (e) {
      return errShowAcct("重试获取资料失败：" + (e && e.message ? e.message : String(e)));
    }
    if (!m || !m.ok) {
      return errShowAcct("重试获取资料失败：" + ((m && m.error) || "未能获取账号信息"));
    }
    renderMeCard(m);
  });
}

// —— T19f-D：账号卡「＋ 添加小号」跳转（设置页账号卡 st-add-btn 所在）——
function goSettings() {
  const r = window.__t8router;
  if (r && typeof r.navigate === "function") r.navigate("/settings");
  else window.location.hash = "#/settings";
}

// —— toast（settings.js/tasks.js 同款惰性单例；退出三态反馈承接 alert 逐字）——
let toaster = null;
function toast(type, text, detail) {
  if (!toaster) toaster = createToaster({ max: 5 });
  toaster.show({ type: type, text: text, detail: detail });
}

// —— 确认弹层（settings.js:305-343 同款 Promise 化；createOverlay 叠在登录
//    卡之上——.u-overlay 与登录层同 z，DOM 序后者在上，点窗外只关它）——
let confirmOv = null;
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
    // 先 close 收敛 Promise（destroy 不触发 onClose，直接 destroy 会悬死 await）
    if (confirmOv.isOpen()) confirmOv.close("destroy");
    confirmOv.destroy();
    confirmOv = null;
  }
}

// —— T19f-D：退出登录（settings.js:1207-1285 / app.js:632-693 三态逐字承接；
//    本弹层内收口：promoted 刷账号卡、logged_out 切登录表单手机步）——
let logoutBusy = false;
async function doLogoutAcct(btn) {
  if (logoutBusy) return;
  logoutBusy = true;
  if (btn) btn.disabled = true;
  try {
    // is_active 守卫（app.js:637-640 alert 逐字 → toast 承接）
    try {
      const ready = await bridgeReady(BRIDGE_TIMEOUT_MS);
      if (ready && await api().is_active()) {
        toast("error", "有下载任务正在进行，请先到「下载中」中止下载后再退出。");
        return;
      }
    } catch (e) { /* 探测失败不拦截（app.js:640 空 catch 原状） */ }

    // 有小号 → 提升语义（app.js:642-650）
    let hasAlt = false;
    try {
      const al = await api().account_list();
      if (al && al.ok && Array.isArray(al.accounts)) {
        hasAlt = al.accounts.some(function (x) {
          return x.id && x.id !== "main";
        });
      }
    } catch (e) { /* 拿不到按无小号走原确认 */ }

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
        content: el("div", {}, [
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
    try { r = await api().logout(); }
    catch (e) {
      toast("error", "退出登录失败", (e && e.message) || String(e));
      return;
    }
    if (!r || !r.ok) {
      toast("error", (r && r.error) || "退出登录失败");   // app.js:652 逐字兜底
      return;
    }

    const mm = window.__t8me;
    if (r.stage === "promoted") {
      // 小号提升为主号：不切登录表单，账号卡刷新资料（app.js:609-630）
      toast("ok", "小号已提升为主号",                    // app.js:628 逐字
        r.warning ? "重新连接中，重启后可自动恢复" : null);
      if (mm && typeof mm.sync === "function") {
        try { mm.sync(); } catch (e) { /* 镜像刷新失败不阻塞 */ }
      }
      renderMePending();
      fetchMe(epoch);
      return;
    }

    // stage=logged_out：清光回登录表单（app.js:655-686 收口，弹层内手机步起）
    stopPoll();
    showLoginForm("anonymous");
    setStep("phone");
    const g = window.__t8guest;
    if (g && typeof g.hide === "function") g.hide();       // app.js:677 hideGuestBar
    if (mm && typeof mm.reset === "function") mm.reset();  // 侧底 me 三串回未登录
    errShow("");
    errShowAcct("");
    noticeShow("");
  } finally {
    logoutBusy = false;
    if (btn) btn.disabled = false;
  }
}

// —— 游客态轮询（app.js 379-415：约 2s 一拍、document.hidden 跳过、
//    busy 门；发现 logged_in&&!guest → 转账号卡；发现登出 → 停轮询显手机步）——
function startPoll() {
  if (pollId != null) return;
  pollId = setInterval(pollTick, POLL_MS);
}
function stopPoll() {
  if (pollId != null) {
    clearInterval(pollId);
    pollId = null;
  }
}
async function pollTick() {
  const my = epoch;
  if (pollBusy || document.hidden) return;
  pollBusy = true;
  try {
    const st = await api().login_state();
    if (idle(my)) return;
    const verdict = resolvePollTick(st);
    if (verdict === "connected") {
      stopPoll();
      showAccountCard();
      fetchMe(my);
    } else if (verdict === "logged_out") {
      stopPoll();
      showLoginForm("anonymous");
      setStep("phone");
    }
  } catch (e) {
    /* 轮询静默容错（app.js:412 catch）；epoch 失效自然停 */
  } finally {
    pollBusy = false;
  }
}

// —— 登录卡形态切换 ——
function showLoginForm(entryState) {
  $.acctCard.el.hidden = true;
  $.loginCard.hidden = false;
  isGuestEntry = entryState === "guest";
  // 2.1 #guest-btn 恒显（app.js:66）：guest 态点击=收卡继续浏览；
  // anonymous 点击=进游客模式（doGuest 合一分支）。
  if ($.btnGuestEnter) $.btnGuestEnter.hidden = false;
}

// ------------------------------------------------------------
// DOM 构建（mount 恰一次；自身 try/catch 兜底 —— t8 契约 std2 J 项：
// 失败返回带错误文案的降级元素 + console.error，绝不抛 rejected Promise）
// ------------------------------------------------------------
function buildDom() {
  const labelPhone = el("label", { class: "lg-label", for: "lg-phone", text: "手机号（含国家区号，不带 +）" });
  const labelCode = el("label", { class: "lg-label", for: "lg-code", text: "Telegram 发来的验证码" });
  const labelPwd = el("label", { class: "lg-label", for: "lg-pwd", text: "两步验证密码" });

  $.inputPhone = el("input", {
    class: "lg-input", id: "lg-phone", type: "tel",
    inputmode: "tel", autocomplete: "tel", placeholder: "8613800000000",
    "aria-label": "手机号", enterkeyhint: "go",
  });
  $.inputCode = el("input", {
    class: "lg-input", id: "lg-code", type: "text",
    inputmode: "numeric", autocomplete: "one-time-code", placeholder: "如 12345",
    "aria-label": "验证码", enterkeyhint: "go",
  });
  $.inputPwd = el("input", {
    class: "lg-input", id: "lg-pwd", type: "password",
    autocomplete: "current-password", placeholder: "2FA 密码",
    "aria-label": "两步验证密码", enterkeyhint: "go",
  });

  $.btnPhone = el("button", { class: "u-btn u-btn--primary lg-block", type: "button", text: "发送验证码", onClick: function () { doSendCode(); } });
  // T58 3.2：可恢复错误明示入口（默认隐藏；仅用户主动点击才进码步）。
  $.btnRecover = el("button", {
    class: "u-btn u-btn--ghost lg-block", type: "button", hidden: true,
    text: "已收到验证码？直接输入",
    onClick: function () { $.btnRecover.hidden = true; setStep("code"); },
  });
  $.btnCode = el("button", { class: "u-btn u-btn--primary lg-block", type: "button", text: "登录", onClick: function () { doVerifyCode(); } });
  // 2.1 code 步返回钮逐字「返回」（index.html:57）；pwd 步无返回钮（:59-62）
  $.btnBackCode = el("button", { class: "u-btn u-btn--ghost lg-block", type: "button", text: "返回", onClick: function () { setStep(prevStep("code")); } });
  $.btnPwd = el("button", { class: "u-btn u-btn--primary lg-block", type: "button", text: "确认", onClick: function () { doVerifyPassword(); } });

  $.stepPhone = el("div", { class: "lg-step", dataset: { step: "phone" } },
    [labelPhone, $.inputPhone, $.btnPhone, $.btnRecover]);
  $.stepCode = el("div", { class: "lg-step", dataset: { step: "code" }, hidden: true },
    [labelCode, $.inputCode, $.btnCode, $.btnBackCode]);
  $.stepPwd = el("div", { class: "lg-step", dataset: { step: "pwd" }, hidden: true },
    [labelPwd, $.inputPwd, $.btnPwd]);
  stepEls.phone = $.stepPhone;
  stepEls.code = $.stepCode;
  stepEls.pwd = $.stepPwd;

  $.err = el("p", { class: "lg-err", role: "alert", hidden: true, text: "" });
  $.notice = el("p", { class: "lg-notice", hidden: true, text: "" });

  $.btnGuestEnter = el("button", {
    class: "u-btn u-btn--ghost lg-block lg-guest-btn", type: "button",
    text: "暂不登录（游客模式）",
    onClick: function () { doGuest(); },
  });

  // 登录卡（2.1 .login-card 解剖逐序：logo-badge→h1→sub→三步→err→notice→
  // #guest-btn；旧壳 index.html:40-67。步骤指示点/lg-note 为 2.1 所无，不设）
  const logoBadge = el("div", { class: "lg-logo-badge", "aria-hidden": "true", text: "TG" });
  const cardTitle = el("h1", { class: "lg-title", text: "TG 视频下载器" });
  const cardSub = el("p", { class: "lg-sub", text: "登录后开始下载频道视频" });

  $.loginCard = el("div", {
    class: "u-card lg-card lg-login-card", role: "dialog",
    "aria-label": "登录", "aria-busy": "false",
  }, [logoBadge, cardTitle, cardSub, $.stepPhone, $.stepCode, $.stepPwd,
    $.err, $.notice, $.btnGuestEnter]);

  // 账号卡（已登录态渲染账号信息/资料重试；成功态提供“去任务库”导航）。
  $.meAvatar = el("div", { class: "lg-avatar", "aria-hidden": "true", text: "…" });
  $.meName = el("div", { class: "lg-me-name", text: "" });
  $.meUser = el("div", { class: "lg-me-user", text: "" });
  const meRow = el("div", { class: "lg-me" },
    [$.meAvatar, el("div", { class: "lg-me-meta" }, [$.meName, $.meUser])]);
  $.acctErr = el("p", { class: "lg-err", role: "alert", hidden: true, text: "" });
  $.btnRetryMe = el("button", {
    class: "u-btn u-btn--ghost lg-block", type: "button", hidden: true,
    text: "重试获取资料",
    onClick: function () { doRetryMe(); },
  });
  $.btnGoTasks = el("button", {
    class: "u-btn u-btn--primary lg-block", type: "button",
    text: "去任务库",
    onClick: function () { goTasks(); },
  });
  // T19f-D：添加小号跳转 + 退出登录（文案逐字对齐设置页 st-add-btn/退出钮）
  $.btnAddAcct = el("button", {
    class: "u-btn u-btn--ghost lg-block", type: "button",
    text: "＋ 添加小号",
    onClick: function () { goSettings(); },
  });
  $.btnLogout = el("button", {
    class: "u-btn u-btn--danger lg-block", type: "button",
    text: "退出登录",
    onClick: function () { doLogoutAcct(this); },
  });
  $.acctCard = createCard({ title: "账号", sub: "已登录", ariaLabel: "账号信息" });
  $.acctCard.el.classList.add("lg-card"); // 进场动效挂点（login.css §8）
  $.acctCard.body.append(meRow, $.acctErr, $.btnRetryMe, $.btnGoTasks,
    $.btnAddAcct, $.btnLogout);
  $.acctCard.el.hidden = true;

  submitLabels.set($.btnPhone, "发送验证码");
  submitLabels.set($.btnCode, "登录");
  submitLabels.set($.btnPwd, "确认");
  submitLabels.set($.btnRetryMe, "重试获取资料");
  submitLabels.set($.btnGuestEnter, "暂不登录（游客模式）");

  // 弹层根（2.1 .login-mask：fixed 遮罩盖在当前版面上，只含卡）
  const view = el("div", { class: "lg-view", "data-view-name": "login" },
    [$.loginCard, $.acctCard.el]);
  return view;
}

// mount：缓存节点（router keep-alive 首进恰一次；返回 Node 本值）。
export function mount(container) {
  if (root) return root;
  try {
    root = buildDom();
    return root;
  } catch (e) {
    // 兜底降级元素（契约 R2 教训：不让 rejected Promise 进 mounts 缓存）。
    console.error("[v22login] 登录视图构建失败：", e);
    root = el("div", { class: "lg-view", "data-view-name": "login" },
      el("p", { class: "lg-err", role: "alert", text: "登录视图加载失败，请稍后重试。" }));
    return root;
  }
}

// ------------------------------------------------------------
// 生命周期
// ------------------------------------------------------------

function resetTransient() {
  stopPoll();
  pollBusy = false;
  if (busy) setBusyUi(false);
  if ($.inputPhone) $.inputPhone.value = "";   // 重开卡清值（app.js showLoginCard 360-363）
  if ($.inputCode) $.inputCode.value = "";
  if ($.inputPwd) $.inputPwd.value = "";
  errShow("");
  errShowAcct("");
  noticeShow("");
}

// onEnter：login_state 定初始态 → connect 复核（零发码，T58 2.2/3.1）。
//   connected → 账号卡 + me；
//   guest     → 受限提示条 + 登录卡 + “继续游客浏览”（不调 set_guest）；
//   anonymous → 登录卡 + “暂不登录（游客模式）”（set_guest(true) 成功跳转）；
//   connect 复核：need_code / has_session（本轮已发码）→ 卡首现即码步；
//   need_phone + phone_prefill → 仅预填（绝不触发发送）；
//   已就绪 → 账号卡。guest 态卡片前台期间起 2s 轮询。
export function onEnter(params) {
  resetTransient();
  // 受控提示消费（2.1 showLoginCard(notice) 口径：notice 与错误互不残留；
  // params.notice 经 router.navigate opts 直达，pendingNotice 兜 openLogin 入口）
  const notice = (params && params.notice) || pendingNotice;
  pendingNotice = "";
  if (notice) noticeShow(notice);
  // T17 手势增量：聚焦保持（虚拟键盘遮挡补偿；幂等）
  if (!gsDetach.length && root) gsDetach = [attachFocusKeep(root)];
  const my = ++epoch;
  (async function () {
    const bridgeOk = await bridgeReady(BRIDGE_TIMEOUT_MS);
    if (idle(my)) return;
    if (!bridgeOk) {
      showLoginForm("anonymous");
      setStep("phone");
      noticeShow("正在连接应用服务…完成后即可登录");
      return;
    }
    let st = null;
    try { st = await api().login_state(); } catch (e) { st = null; }
    if (idle(my)) return;
    const entry = resolveEntryState(st);
    if (entry === "connected") {
      showAccountCard();
      fetchMe(my);
      return;
    }
    showLoginForm(entry);
    // 发码中（has_session 新语义=本轮 _code_sent）：卡首现即码步
    //（app.js:241-252 口径；guest 同样只信后端发码信号）。
    setStep(st && st.has_session ? "code" : "phone");
    let r = null;
    try { r = await api().connect(); } catch (e) { r = null; }
    if (idle(my)) return;
    const codeStep = (r && r.ok && r.stage === "need_code") ||
                     (st && st.has_session && !(r && r.busy));
    if (r && r.ok && (r.stage === "ready" || !r.stage)) {
      // connect 复核发现已就绪 → 账号卡（connect 全路径零发码）。
      showAccountCard();
      fetchMe(my);
      return;
    }
    if (codeStep) {
      setStep("code");
      if (r && r.warning) errShow(r.warning);
    } else if (r && r.ok && r.stage === "need_phone") {
      setStep("phone");
      // T58 3.1：cfg 旧手机号仅预填输入框，不触发任何发送。
      if (r.phone_prefill) $.inputPhone.value = r.phone_prefill;
    } else {
      setStep("phone");
      if (r && !r.ok) errShow(r.error || "");
      else if (r && r.warning) errShow(r.warning);
    }
    if (entry === "guest") startPoll();
  })();
}

// onLeave：全局资源全清（轮询 clearInterval；epoch 使在途回调失效；
// 元素级监听随 keep-alive 节点常驻属缓存语义，零 window 级订阅残留）。
export function onLeave() {
  epoch++;
  stopPoll();
  pollBusy = false;
  if (busy) setBusyUi(false);
  destroyConfirm(); // T19f-D：离场收确认弹层（close 先行收敛 await）
  // T17 手势增量对称回收（attach* 均返回 detach）
  for (let i = 0; i < gsDetach.length; i++) gsDetach[i]();
  gsDetach = [];
}
