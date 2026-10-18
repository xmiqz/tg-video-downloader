// ============================================================
// toast.js — T10 Toast（队列 + 重叠策略 + 三型）
//
// 纯函数（seam S2，node 离线可测）：
//   resolveToastTiming(type, duration?) → {auto, duration}
//     error → {auto:false, duration:0}（永不自动消失：app.js:3742-3746
//       error 卡不带定时器、只手动关闭的同义约束）
//     ok   → {auto:true, duration:4000}（app.js:3787 setTimeout 4000）
//     warn → {auto:true, duration:5000}（本波新档默认，旧壳无 warn）
//     未知 type → 归一 ok（宽松归一不抛错）
//     显式 duration(>0 数字) 覆盖类型默认
//   planToastStack(existing[], incoming, {max=5}) → {evict:[id]|[], accept:true}
//     未达上限 → 不淘汰；
//     满员时 incoming 为 error → 不裁（error 恒展示，错误须持续可见：
//       对齐旧壳 TgToast.error 无自动移除语义 app.js:3742）；
//     满员 + incoming 非 error：淘汰插入序最旧的一条非 error（多条可淘汰
//       时取最旧，error 永不自动淘汰）；
//     满员全 error → 允许超限堆叠。
//
// DOM 引擎 createToaster：顶部居中栈（位置对齐旧壳 host 模式 app.js:3685-
//   3688：top:18px/left:50% 居中、flex 纵向、间隙），类与样式本组件化
//   （旧壳用 cssText 行内，新组件统一 ui.css .u-toast 族）。
//   error 卡 detail 存在时带「详情/收起」切换（对齐 app.js:3732-3736
//   detailBtn 展开行为）；ok/warn 的 detail 为次要行直显。
//   destroy：清除全部自动消失 timer、移除 host 与监听。
// ============================================================

export function resolveToastTiming(type, duration) {
  const t = type === "error" || type === "warn" ? type : "ok";
  if (t === "error") return { auto: false, duration: 0 };
  let d = t === "warn" ? 5000 : 4000;
  if (typeof duration === "number" && duration > 0) d = duration;
  return { auto: true, duration: d };
}

export function planToastStack(existing, incoming, opts) {
  const max = opts && typeof opts.max === "number" && opts.max > 0 ? opts.max : 5;
  const list = Array.isArray(existing) ? existing : [];
  const inc = incoming || {};
  const incType = inc.type === "error" || inc.type === "warn" || inc.type === "ok" ? inc.type : "ok";
  const out = { evict: [], accept: true };
  if (list.length < max) return out;               // ①未达上限
  if (incType === "error") return out;             // ⑤error 入列永不裁队
  for (let i = 0; i < list.length; i++) {          // ②④插入序最旧可淘汰者
    const it = list[i] || {};
    if (it.type !== "error") { out.evict = [it.id]; return out; }
  }
  return out;                                      // ③满员全 error：超限堆叠
}

let toastSeq = 0;

export function createToaster(opts) {
  const o = opts || {};
  const doc = o.doc || (typeof document !== "undefined" ? document : null);
  const max = typeof o.max === "number" && o.max > 0 ? o.max : 5;
  if (!doc) throw new Error("[v22ui] createToaster 需要可用的 document（host 环境）");
  const timers = new Map(); // toastEl -> timer id（destroy 全清）
  let disposed = false;
  let host = null;

  function ensureHost() {
    if (host && host.isConnected) return host;
    host = doc.createElement("div");
    host.className = "u-toaster";
    host.setAttribute("aria-live", "polite");
    doc.body.appendChild(host);
    return host;
  }

  function currentIds() {
    if (!host) return [];
    return Array.prototype.map.call(host.querySelectorAll(".u-toast"), function (el) {
      return { el: el, id: el.dataset.tId, type: el.dataset.tType };
    });
  }

  function removeToast(el, animated) {
    if (!el || !el.isConnected) return;
    const t = timers.get(el);
    if (t) { clearTimeout(t); timers.delete(el); }
    if (animated) {
      el.classList.add("is-out");
      const tm = setTimeout(function () {
        timers.delete(el);
        if (el.isConnected) el.remove();
      }, 240);
      timers.set(el, tm);
      return;
    }
    el.remove();
  }

  function show(spec) {
    if (disposed) return null;
    const s = spec || {};
    const type = s.type === "error" || s.type === "warn" || s.type === "ok" ? s.type : "ok";
    const h = ensureHost();
    // 队列策略（planToastStack）：满员 + 非本条 error → 淘汰插入序最旧非 error
    const stack = currentIds().map(function (r) { return { id: r.id, type: r.type }; });
    const plan = planToastStack(stack, { type: type }, { max: max });
    if (plan.evict.length) {
      const victims = host.querySelectorAll('.u-toast[data-t-id="' + String(plan.evict[0]).replace(/"/g, '\\"') + '"]');
      for (let i = 0; i < victims.length; i++) removeToast(victims[i], true);
    }
    const timing = resolveToastTiming(type, s.duration);
    const card = doc.createElement("div");
    const id = String(++toastSeq);
    card.className = "u-toast u-toast--" + type;
    card.dataset.tId = id;
    card.dataset.tType = type;

    const mark = doc.createElement("span");
    mark.className = "u-toast-mark";
    mark.setAttribute("aria-hidden", "true");
    mark.textContent = type === "error" ? "⚠" : (type === "warn" ? "!" : "✓");

    const text = doc.createElement("span");
    text.className = "u-toast-text";
    text.textContent = String(s.text == null ? "" : s.text);

    const closeBtn = doc.createElement("button");
    closeBtn.type = "button";
    closeBtn.className = "u-toast-pill";
    closeBtn.textContent = "关闭";
    const head = doc.createElement("div");
    head.className = "u-toast-head";
    head.append(mark, text, closeBtn);
    card.append(head);

    closeBtn.addEventListener("click", function () { removeToast(card, true); });

    let detailToggle = null;
    if (s.detail != null && String(s.detail) !== "") {
      if (type === "error") {
        // 「详情/收起」切换（对齐旧壳 app.js:3732-3736）
        detailToggle = doc.createElement("button");
        detailToggle.type = "button";
        detailToggle.className = "u-toast-pill";
        detailToggle.textContent = "详情";
        head.insertBefore(detailToggle, closeBtn);
        const body = doc.createElement("div");
        body.className = "u-toast-detail";
        body.hidden = true;
        body.textContent = String(s.detail);
        card.append(body);
        detailToggle.addEventListener("click", function () {
          // 翻转展开态：当前展开→点后收起（文案回「详情」）；当前收起→点后展开（文案「收起」）
          const open = body.hidden !== true;
          body.hidden = open;
          detailToggle.textContent = open ? "详情" : "收起";
        });
      } else {
        const body = doc.createElement("div");
        body.className = "u-toast-detail is-open";
        body.textContent = String(s.detail);
        card.append(body);
      }
    }

    if (s.count != null) {
      card.dataset.tCount = String(s.count);
    }
    h.append(card);
    if (timing.auto) {
      const tm = setTimeout(function () {
        timers.delete(card);
        removeToast(card, true);
      }, timing.duration);
      timers.set(card, tm);
    }
    return id;
  }

  function dismiss(id) {
    if (!host) return false;
    const target = host.querySelector('.u-toast[data-t-id="' + String(id == null ? "" : id).replace(/"/g, '\\"') + '"]');
    if (!target) return false;
    removeToast(target, true);
    return true;
  }

  function size() { return host ? host.querySelectorAll(".u-toast").length : 0; }

  return {
    show: show,
    dismiss: dismiss,
    clear: function () {
      if (!host) return;
      Array.prototype.forEach.call(host.querySelectorAll(".u-toast"), function (el) { removeToast(el, false); });
    },
    size: size,
    destroy: function () {
      if (disposed) return; // 幂等
      disposed = true;
      timers.forEach(function (t) { clearTimeout(t); });
      timers.clear();
      if (host && host.isConnected) host.remove();
      host = null;
    },
  };
}
