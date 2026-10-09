// ============================================================
// TG 视频下载器 v2.0 —— 前端传输桥（HTTP/WS 唯一通道）
//
// 对接契约（server_app，钉死）：
//   RPC  POST /rpc   Authorization: Bearer <token>
//        体 {"method": str, "params": [...]}；200 体即方法原返回值；
//        非 2xx 体 {"ok":false,"error": str}
//   WS   /events    连上首消息 {"token": token}，服务端回 {"type":"auth_ok"}
//        推送包络 {"event": name, "params": [...]}
//
// 本模块顶替 v1.2.6 的 window.pywebview.api 接缝：app.js / editor.js
// 零改动——模块默认 defer，于文档解析后执行，此时两页脚本的监听均已注册。
// 无外部依赖、无 polyfill、无 postMessage 分支。
// ============================================================

// ---------- ① token：仅取自 location.hash，不改动 hash ----------
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
if (!token) {
  // 缺 token：不启动（不安装接缝、不派发事件、不连 WS）
  console.error("[bridge] 缺少访问令牌（location.hash 中无 #t=），桥接未启动");
} else {
  boot();
}

function boot() {
  const baseUrl = location.origin; // 页面与服务同源

  // ---------- ② RPC：fetch POST /rpc ----------
  async function rpc(method, args) {
    let resp;
    try {
      resp = await fetch(baseUrl + "/rpc", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Authorization": "Bearer " + token,
        },
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
        window._setBackdrop(params[0]);
      }
    },
  };

  function dispatchEvent(msg) {
    if (!msg || typeof msg.event !== "string") return;
    const handler = EVENT_WHITELIST[msg.event];
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
