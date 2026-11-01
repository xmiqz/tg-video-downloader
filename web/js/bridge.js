// ============================================================
// TG 视频下载器 v2.0 —— 前端传输桥（HTTP/WS 唯一通道）
//
// 对接契约（server_app，钉死）：
//   RPC  POST /rpc   Authorization: Bearer <token>（桌面）
//                    或同源 HttpOnly 会话 Cookie（v2.4 浏览器）
//        体 {"method": str, "params": [...]}；200 体即方法原返回值；
//        非 2xx 体 {"ok":false,"error": str}
//   WS   /events    连上首消息 {"token": token}，服务端回 {"type":"auth_ok"}
//        推送包络 {"event": name, "params": [...]}
//
// 本模块顶替 v1.2.6 的 window.pywebview.api 接缝：app.js / editor.js
// 零改动——模块默认 defer，于文档解析后执行，此时两页脚本的监听均已注册。
// 无外部依赖、无 polyfill、无 postMessage 分支。
// ============================================================

// ---------- ① 鉴权：桌面 Bearer 或浏览器会话 Cookie ----------
function readToken() {
  // 形如 #t=<token>；兼容与其他片段以 & 共存的情况，其余片段保持原样
  const m = location.hash.match(/[#&]t=([^&]*)/);
  if (!m) return "";
  try {
    return decodeURIComponent(m[1]);
  } catch (e) {
    return m[1];
  }
}

const token = readToken();

// The shell starts before the asynchronous auth probe finishes. Use the
// injected webview API as the synchronous host hint when available. The
// standalone taskedit page is also opened from the browser with a short-lived
// #t=<token> for asset URLs, so a token alone is not a reliable desktop hint.
// This prevents a Windows browser pointing at a container from briefly showing
// desktop-only themes (and keeps the first paint on the correct host mode).
if (typeof window !== "undefined") {
  const page = String((typeof location !== "undefined" && location.pathname) || "");
  const standaloneEditor = /\/taskedit\.html$/i.test(page);
  const hasNativeHost = !!(window.pywebview && window.pywebview.api);
  window.__TVG_HOST_MODE = (hasNativeHost || (token && !standaloneEditor))
    ? "desktop" : "headless";
}

async function startBridge() {
  if (token) {
    boot();
    return;
  }
  try {
    const resp = await fetch("/auth/status", { cache: "no-store" });
    const status = resp.ok ? await resp.json() : null;
    if (status && status.setup_required) {
      location.replace("/setup");
      return;
    }
    if (!status || !status.authenticated) {
      location.replace("/login");
      return;
    }
  } catch (e) {
    console.error("[bridge] 无法确认浏览器会话：", e);
    return;
  }
  boot();
}

startBridge();

function boot() {
  const baseUrl = location.origin; // 页面与服务同源

  // ---------- ② RPC：fetch POST /rpc ----------
  async function rpc(method, args) {
    let resp;
    try {
      const headers = { "Content-Type": "application/json" };
      if (token) headers.Authorization = "Bearer " + token;
      resp = await fetch(baseUrl + "/rpc", {
        method: "POST",
        headers: headers,
        credentials: "same-origin",
        body: JSON.stringify({ method: String(method), params: Array.from(args) }),
      });
    } catch (e) {
      // 网络层错误（含服务端已终止）：必须 reject，不得悬挂
      throw new Error("网络错误：" + (e && e.message ? e.message : e));
    }

    let data = null;
    try {
      data = await resp.json();
    } catch (e) {
      data = null;
    }

    if (resp.ok) return data; // 200：体即方法原返回值
    if (resp.status === 401 && !token) {
      location.replace("/login");
    }
    const detail = data && data.error ? data.error : ("HTTP " + resp.status);
    throw new Error(detail);
  }

  // 任意属性访问 -> 返回把该属性名当方法名的 RPC 函数
  const proxy = new Proxy({}, {
    get(_t, prop) {
      if (typeof prop === "symbol") return undefined;
      return (...args) => rpc(prop, args);
    },
  });

  // ---------- ③ 全局接缝 + pywebviewready ----------
  window.pywebview = { api: proxy };
  window.dispatchEvent(new Event("pywebviewready"));

  // ---------- ④ WS：鉴权、白名单投递、断线指数退避重连 ----------
  const wsUrl =
    (location.protocol === "https:" ? "wss:" : "ws:") +
    "//" + location.host + "/events";

  // 推送事件白名单：未列入的事件只 console.debug，绝不执行
  const EVENT_WHITELIST = {
    _setBackdrop: (params) => {
      if (typeof window._setBackdrop === "function") {
        window._setBackdrop(params[0], params[1]);
      }
    },
  };

  // T45：job_update/search_items/dl_phase/acct_hint 统一转发为
  // window 上的 tg:<name> CustomEvent（detail = params[0]），
  // 由 app.js 事件总线订阅；job_update 的 payload 即完整 job 对象。
  ["job_update","search_items","dl_phase","acct_hint"].forEach(function (name) {
    EVENT_WHITELIST[name] = function (params) {
      window.dispatchEvent(
        new CustomEvent("tg:" + name, { detail: params[0] }));
    };
  });

  function dispatchEvent(msg) {
    if (!msg || typeof msg.event !== "string") return;
    // hasOwnProperty guard: a plain object lookup would otherwise resolve
    // Object.prototype members, so an event named "toString" would be called
    // as if it were a whitelisted handler.
    const handler = Object.prototype.hasOwnProperty.call(EVENT_WHITELIST, msg.event)
      ? EVENT_WHITELIST[msg.event]
      : null;
    if (handler) {
      try {
        handler(Array.isArray(msg.params) ? msg.params : []);
      } catch (e) {
        console.error("[bridge] 事件处理异常（" + msg.event + "）：", e);
      }
    } else {
      console.debug("[bridge] 忽略白名单外事件：", msg.event, msg.params);
    }
  }

  const BACKOFF_START = 500;      // 首次重连等待
  const BACKOFF_MAX = 10000;      // 退避封顶 10s
  let backoff = BACKOFF_START;
  let stopped = false;            // 页面卸载即停止重连
  let reconnectTimer = 0;

  function scheduleReconnect() {
    if (stopped) return;
    const wait = backoff;
    console.info("[bridge] WS 断开，" + wait + "ms 后重连");
    reconnectTimer = setTimeout(connectWs, wait);
    backoff = Math.min(backoff * 2, BACKOFF_MAX);
  }

  function connectWs() {
    if (stopped) return;
    const ws = new WebSocket(wsUrl);
    let authed = false;

    ws.addEventListener("open", () => {
      // 连上后首消息必须是鉴权
      ws.send(JSON.stringify({ token: token }));
    });

    ws.addEventListener("message", (ev) => {
      let msg;
      try {
        msg = JSON.parse(ev.data);
      } catch (e) {
        return;
      }
      if (!authed) {
        if (msg && msg.type === "auth_ok") {
          authed = true;
          backoff = BACKOFF_START; // 鉴权成功，退避复位
          console.info("[bridge] WS 已连接并通过鉴权");
        } else {
          // 鉴权阶段收到非 auth_ok：关闭后走重连
          try { ws.close(); } catch (e) {}
        }
        return;
      }
      dispatchEvent(msg);
    });

    ws.addEventListener("error", () => {
      try { ws.close(); } catch (e) {}
    });

    ws.addEventListener("close", (ev) => {
      if (authed || ev.code === 4401) {
        // 4401：鉴权失败；仍按退避重试（服务重启后可能恢复）
      }
      scheduleReconnect();
    });
  }

  window.addEventListener("pagehide", () => {
    stopped = true;
    if (reconnectTimer) clearTimeout(reconnectTimer);
  });

  // WS 未就绪不阻塞 HTTP，也不影响已派发的 pywebviewready
  connectWs();
}
