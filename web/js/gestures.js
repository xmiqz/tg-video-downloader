// web/js/gestures.js — T17 触摸手势增量（边缘返回 / 列表滑动快捷 / 聚焦保持）。
// 所有权：T17 新建；消费方 = 六视图触摸小改。零依赖、node import 安全（无顶层副作用）。
//
// 不变量（tr17_2 纪律断言锚）：
// - 全部 passive 监听，绝不 preventDefault（滚动/浏览器手势不被劫持）；
// - 单指手势 + 轴锁：|dx|>|dy| 认横向（滑动/边缘返回），反之认纵向（滚动/PTR）；
// - 每个 attach* 返回 detach，幂等可重入；detach 全清监听并回收注入 DOM/内联样式；
// - 边缘返回起手区（x≤24px）与滑动托盘互斥：左缘起手让位边缘返回。

export const GS_EDGE_W = 24;        // 边缘返回起手宽度（px，FR-24）
export const GS_BACK_DX = 64;       // 边缘返回触发位移（px）
export const GS_AXIS_SLOP = 8;      // 轴锁判定最小位移（px）
export const GS_OPEN_RATIO = 0.6;   // 托盘展开松手阈值（托盘宽 × 0.6）

// ---------- 返回（goBack） ----------
// history 有来路 → 系统语义返回；无来路（深链直达）→ 回默认路由，绝不卡死。
export function goBack() {
  const w = typeof window !== "undefined" ? window : null;
  if (!w) return;
  if (w.history && w.history.length > 1) { w.history.back(); return; }
  if (w.__t8router && typeof w.__t8router.navigate === "function") {
    w.__t8router.navigate("/active");
  } else if (typeof w.location !== "undefined") {
    w.location.hash = "#/active";
  }
}

// ---------- 1. 边缘滑动返回 ----------
export function attachEdgeBack(opts) {
  const w = typeof window !== "undefined" ? window : null;
  if (!w) return function () {};
  const o = opts || {};
  const onBack = typeof o.onBack === "function" ? o.onBack : goBack;
  const edgeW = o.edgeW || GS_EDGE_W;
  let st = null; // {x0,y0,fired}

  function start(e) {
    const t = e.touches && e.touches[0];
    if (!t || e.touches.length !== 1) { st = null; return; }
    st = { x0: t.clientX, y0: t.clientY, fired: false };
  }
  function move(e) {
    if (!st || st.fired) return;
    const t = e.touches && e.touches[0];
    if (!t) return;
    const dx = t.clientX - st.x0;
    const dy = t.clientY - st.y0;
    // 只认左缘起手、右滑、横向占优
    if (st.x0 > edgeW || dx < GS_BACK_DX || Math.abs(dy) > Math.abs(dx)) return;
    st.fired = true;
    onBack();
  }
  function end() { st = null; }

  w.addEventListener("touchstart", start, { passive: true });
  w.addEventListener("touchmove", move, { passive: true });
  w.addEventListener("touchend", end, { passive: true });
  w.addEventListener("touchcancel", end, { passive: true });
  return function detach() {
    w.removeEventListener("touchstart", start);
    w.removeEventListener("touchmove", move);
    w.removeEventListener("touchend", end);
    w.removeEventListener("touchcancel", end);
    st = null;
  };
}

// ---------- 2. 列表项滑动快捷动作（托盘） ----------
// cfg = { itemSel, actions(itemEl) -> [{key,label,tone,run(btn)}] }
// 托盘按钮 run 由视图提供（范式：透传既有按钮 btn.click()，零语义复制）。
export function attachSwipeActions(root, cfg) {
  if (!root || !cfg || !cfg.itemSel) return function () {};
  ensureGesturesCss(root.ownerDocument || (typeof document !== "undefined" ? document : null));
  const build = typeof cfg.actions === "function" ? cfg.actions : function () { return []; };
  const BTN_W = 72;
  let gst = null;      // 进行中的拖动 {item,tray,x0,y0,dx,axis,open0}
  let openHost = null; // 当前展开的 host

  function closeHost() {
    if (!openHost) return;
    const it = openHost.querySelector(".gs-swipe-item");
    if (it) it.style.transform = "";
    openHost.classList.remove("gs-open", "gs-drag");
    openHost = null;
  }
  function openHostFor(item) {
    const host = item.parentNode;
    if (!host || !host.classList) return null;
    if (!host.classList.contains("gs-swipe-host")) host.classList.add("gs-swipe-host");
    let tray = host.querySelector(":scope > .gs-tray");
    if (!tray) {
      const acts = build(item) || [];
      if (!acts.length) return null;
      tray = (root.ownerDocument || document).createElement("div");
      tray.className = "gs-tray";
      for (let i = 0; i < acts.length; i++) {
        (function (a) {
          const b = (root.ownerDocument || document).createElement("button");
          b.type = "button";
          b.className = "gs-tray-btn" + (a.tone === "danger" ? " gs-tray-btn--danger" : "");
          b.textContent = a.label || a.key || "";
          b.style.width = BTN_W + "px";
          b.addEventListener("click", function (ev) {
            ev.stopPropagation();
            closeHost();
            if (typeof a.run === "function") a.run(b);
          });
          tray.appendChild(b);
        })(acts[i]);
      }
      host.insertBefore(tray, item.nextSibling);
    }
    // 托盘随行定位（host 相对定位；行高/行位变化按手势当拍同步）
    tray.style.top = item.offsetTop + "px";
    tray.style.height = item.offsetHeight + "px";
    return { host: host, tray: tray };
  }
  // 拖后浏览器合成 click 吞掉，防误触行点击：
  // 只吞「手势结束后 400ms 内、落在被拖 host 域内、且非托盘钮」的 click——
  // 晚来的真实点击（如托盘按钮首击）绝不误吞（once 滞留会吃掉首击）。
  let swallowHost = null;
  let swallowTimer = null;
  function armSwallow(host) {
    swallowHost = host;
    if (swallowTimer) clearTimeout(swallowTimer);
    swallowTimer = setTimeout(function () { swallowHost = null; swallowTimer = null; }, 400);
  }
  function swallowClick(e) {
    if (!swallowHost || !swallowHost.contains(e.target)) return;
    if (e.target.closest && e.target.closest(".gs-tray")) return;
    swallowHost = null;
    e.stopPropagation();
  }
  root.addEventListener("click", swallowClick, { capture: true });

  function start(e) {
    const t = e.touches && e.touches[0];
    if (!t || e.touches.length !== 1) { gst = null; return; }
    // 左缘起手让位边缘返回
    if (t.clientX <= GS_EDGE_W) { gst = null; return; }
    const item = e.target && e.target.closest ? e.target.closest(cfg.itemSel) : null;
    if (!item || !root.contains(item)) { gst = null; return; }
    const host = item.parentNode;
    // 已有展开：点非托盘区一律收起并吞掉合成 click（同行=收起结束；他行=收起后可继续新手势）
    if (openHost && !(e.target.closest && e.target.closest(".gs-tray"))) {
      const sameHost = host === openHost;
      armSwallow(openHost);
      closeHost();
      if (sameHost) { gst = null; return; }
    }
    gst = {
      item: item, tray: null, x0: t.clientX, y0: t.clientY,
      dx: 0, axis: 0, open0: item.style.transform ? -1 : 0, // open0: 起手时已展开
    };
  }
  function move(e) {
    if (!gst) return;
    const t = e.touches && e.touches[0];
    if (!t) return;
    const dx = t.clientX - gst.x0;
    const dy = t.clientY - gst.y0;
    if (!gst.axis) {
      if (Math.abs(dx) > GS_AXIS_SLOP + Math.abs(dy)) gst.axis = 1;       // 横向
      else if (Math.abs(dy) > GS_AXIS_SLOP + Math.abs(dx)) gst.axis = -1; // 纵向
      else return;
    }
    if (gst.axis < 0) return; // 纵向滚动，让位
    if (!gst.tray) {
      const got = openHostFor(gst.item);
      if (!got) { gst = null; return; }
      gst.tray = got.tray;
      // 起手时已展开 → 从展开位继续
      if (gst.open0) gst.x0 += gst.tray.offsetWidth;
      if (openHost && openHost !== got.host) closeHost();
      openHost = got.host;
    }
    gst.dx = dx;
    const openW = gst.tray.offsetWidth || BTN_W;
    const base = gst.open0 ? -openW : 0;
    let x = Math.min(0, Math.max(-openW, base + dx));
    gst.item.parentNode.classList.add("gs-drag");
    gst.item.classList.add("gs-swipe-item");
    gst.item.style.transform = "translateX(" + x + "px)";
  }
  function end() {
    if (!gst) return;
    const g = gst;
    gst = null;
    if (g.axis > 0 && g.tray) {
      const openW = g.tray.offsetWidth || BTN_W;
      const cur = parseFloat((g.item.style.transform.match(/-?[\d.]+/) || ["0"])[0]) || 0;
      const shouldOpen = Math.abs(cur) >= openW * GS_OPEN_RATIO;
      const host = g.item.parentNode;
      host.classList.remove("gs-drag");
      if (shouldOpen) {
        g.item.classList.add("gs-swipe-item");
        g.item.style.transform = "translateX(" + -openW + "px)";
        host.classList.add("gs-open");
        openHost = host;
      } else {
        g.item.style.transform = "";
        host.classList.remove("gs-open");
        if (openHost === host) openHost = null;
      }
      // 拖动后浏览器合成 click 吞掉，防误触行点击（400ms 时窗）
      armSwallow(host);
    }
  }

  root.addEventListener("touchstart", start, { passive: true });
  root.addEventListener("touchmove", move, { passive: true });
  root.addEventListener("touchend", end, { passive: true });
  root.addEventListener("touchcancel", end, { passive: true });
  return function detach() {
    root.removeEventListener("touchstart", start);
    root.removeEventListener("touchmove", move);
    root.removeEventListener("touchend", end);
    root.removeEventListener("touchcancel", end);
    root.removeEventListener("click", swallowClick, true);
    if (swallowTimer) { clearTimeout(swallowTimer); swallowTimer = null; }
    swallowHost = null;
    closeHost();
    gst = null;
    // 回收注入 DOM/内联样式/类（视图重渲染也会清，这里保证 detach 即净）
    const doc = root.ownerDocument;
    if (doc) {
      const trays = root.querySelectorAll(".gs-tray");
      for (let i = 0; i < trays.length; i++) trays[i].remove();
    }
    const hosts = root.querySelectorAll(".gs-swipe-host");
    for (let i = 0; i < hosts.length; i++) hosts[i].classList.remove("gs-swipe-host", "gs-open", "gs-drag");
    const items = root.querySelectorAll(".gs-swipe-item");
    for (let i = 0; i < items.length; i++) {
      items[i].classList.remove("gs-swipe-item");
      items[i].style.transform = "";
    }
  };
}

// ---------- 3. 虚拟键盘 / 聚焦保持 ----------
// 表单域聚焦（弹出虚拟键盘）后滚入可视中区；visualViewport 变化（键盘开合）同样补偿。
export function attachFocusKeep(root) {
  if (!root) return function () {};
  const w = typeof window !== "undefined" ? window : null;
  const vv = w && w.visualViewport;
  let timer = null;
  const FIELD = "input, textarea, select";

  function scrollIn(el) {
    if (!el || typeof el.scrollIntoView !== "function") return;
    try { el.scrollIntoView({ block: "center" }); } catch (e) { /* 老引擎缺 options 忽略 */ }
  }
  function onFocusIn(e) {
    const el = e.target;
    if (!el || !el.matches || !el.matches(FIELD)) return;
    if (timer) clearTimeout(timer);
    timer = setTimeout(function () { timer = null; scrollIn(el); }, 250);
  }
  function onVvResize() {
    const a = root.ownerDocument && root.ownerDocument.activeElement;
    if (a && a.matches && a.matches(FIELD) && root.contains(a)) scrollIn(a);
  }

  root.addEventListener("focusin", onFocusIn, { passive: true });
  if (vv) vv.addEventListener("resize", onVvResize);
  return function detach() {
    root.removeEventListener("focusin", onFocusIn);
    if (vv) vv.removeEventListener("resize", onVvResize);
    if (timer) { clearTimeout(timer); timer = null; }
  };
}

// ---------- 托盘动作=透传既有按钮（零语义复制范式） ----------
// 从 item 内既有快捷按钮族生成托盘动作：run = btn.click()，复用其忙态/确认/toast。
// labelByKey：可选 {data-action: 文案} 覆盖（无 title 的字形按钮用）。
export function trayFromButtons(item, sel, labelByKey) {
  if (!item || !item.querySelectorAll) return [];
  const btns = item.querySelectorAll(sel);
  const out = [];
  for (let i = 0; i < btns.length; i++) {
    (function (b) {
      const key = b.dataset && b.dataset.action ? b.dataset.action : (b.title || b.textContent || "");
      const mapped = labelByKey && b.dataset ? labelByKey[b.dataset.action] : null;
      out.push({
        key: key,
        label: mapped || (b.textContent || "").trim() || b.title || key,
        tone: /danger/.test(String(b.className)) ? "danger" : "",
        run: function () { if (!b.disabled) b.click(); },
      });
    })(btns[i]);
  }
  return out;
}

// ---------- 托盘样式引导（同 router boot 注入 ui.css 范式；幂等） ----------
function ensureGesturesCss(doc) {
  if (!doc || doc.getElementById("gs-css")) return;
  const l = doc.createElement("link");
  l.id = "gs-css";
  l.rel = "stylesheet";
  l.href = "css/gestures.css";
  doc.head.appendChild(l);
}
