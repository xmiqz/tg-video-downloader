const $ = (id) => document.getElementById(id);
const steps = {
  admin: "admin-form",
  login: "login-form",
  welcome: "welcome-step",
  credentials: "credentials-form",
  phone: "phone-form",
  code: "code-form",
  password: "password-form",
  done: "done-step",
};
const progressOrder = ["admin", "welcome", "credentials", "phone", "code", "done"];
let phase = location.pathname === "/login" ? "login" : "admin";
let setupMode = location.pathname === "/setup";

function showError(message) {
  const node = $("error");
  node.textContent = message || "";
  node.hidden = !message;
}

function setPhase(next) {
  phase = next;
  Object.entries(steps).forEach(([name, id]) => {
    $(id).hidden = name !== next;
  });
  const index = progressOrder.indexOf(next === "login" || next === "password"
    ? (next === "password" ? "code" : "admin") : next);
  document.querySelectorAll("#progress li").forEach((node, i) => {
    const phaseName = node.dataset.phase;
    const phaseIndex = progressOrder.indexOf(phaseName);
    node.classList.toggle("active", phaseIndex === index);
    node.classList.toggle("done", phaseIndex >= 0 && phaseIndex < index);
  });
  const focus = document.querySelector(`#${steps[next]} input, #${steps[next]} button`);
  if (focus) setTimeout(() => focus.focus(), 40);
}

function busy(form, on, label) {
  const button = form.querySelector("button[type=submit]");
  if (!button) return;
  if (on) {
    button.dataset.label = button.textContent;
    button.textContent = label || button.dataset.busy || "处理中…";
    button.disabled = true;
  } else {
    button.textContent = button.dataset.label || button.textContent;
    button.disabled = false;
  }
}

async function jsonFetch(url, body) {
  const resp = await fetch(url, {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  let data = null;
  try { data = await resp.json(); } catch (e) {}
  if (!resp.ok || !data || data.ok === false) {
    if (resp.status === 401 && url !== "/auth/login") {
      location.replace("/login");
    }
    throw new Error((data && data.error) || `HTTP ${resp.status}`);
  }
  return data;
}

async function rpc(method, ...params) {
  const resp = await fetch("/rpc", {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ method, params }),
  });
  let data = null;
  try { data = await resp.json(); } catch (e) {}
  if (resp.status === 401) location.replace("/login");
  if (!resp.ok) throw new Error((data && data.error) || `HTTP ${resp.status}`);
  return data;
}

async function continueAfterAdmin() {
  const state = await rpc("login_state").catch(() => null);
  if (state && state.logged_in) {
    setPhase("done");
  } else if (setupMode) {
    setPhase("welcome");
  } else {
    location.replace("/");
  }
}

async function init() {
  let status;
  try {
    const resp = await fetch("/auth/status", { cache: "no-store" });
    status = await resp.json();
  } catch (e) {
    showError("无法连接服务，请刷新页面重试");
    return;
  }
  if (status.setup_required) {
    setupMode = true;
    setPhase("admin");
    return;
  }
  if (!status.configured) {
    setPhase("welcome");
    return;
  }
  if (status.authenticated) {
    await continueAfterAdmin();
    return;
  }
  setupMode = false;
  setPhase("login");
}

$("admin-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  showError("");
  const first = $("admin-password").value;
  if (first !== $("admin-password-2").value) {
    showError("两次输入的密码不一致");
    return;
  }
  const form = event.currentTarget;
  busy(form, true, "设置中…");
  try {
    await jsonFetch("/auth/setup", {
      bootstrap_token: $("bootstrap-token").value,
      password: first,
    });
    $("admin-password").value = "";
    $("admin-password-2").value = "";
    $("bootstrap-token").value = "";
    await continueAfterAdmin();
  } catch (e) {
    showError(e.message);
  } finally {
    busy(form, false);
  }
});

$("login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  showError("");
  const form = event.currentTarget;
  busy(form, true, "登录中…");
  try {
    await jsonFetch("/auth/login", { password: $("login-password").value });
    $("login-password").value = "";
    location.replace("/");
  } catch (e) {
    showError(e.message);
  } finally {
    busy(form, false);
  }
});

document.querySelectorAll("[data-back]").forEach((button) => {
  button.addEventListener("click", () => setPhase(button.dataset.back));
});
document.querySelectorAll("[data-next]").forEach((button) => {
  button.addEventListener("click", () => setPhase(button.dataset.next));
});

$("credentials-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  showError("");
  const form = event.currentTarget;
  const apiId = $("api-id").value.trim();
  const apiHash = $("api-hash").value.trim();
  if (!!apiId !== !!apiHash) {
    showError("api_id 与 api_hash 需要同时填写，或两者都留空");
    return;
  }
  busy(form, true, "保存中…");
  try {
    if (apiId) await rpc("set_api_credentials", apiId, apiHash);
    setPhase("phone");
  } catch (e) {
    showError(e.message);
  } finally {
    busy(form, false);
  }
});

$("phone-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  showError("");
  const form = event.currentTarget;
  const phone = $("phone").value.trim().replace(/^\+/, "");
  if (!phone) {
    showError("请输入手机号");
    return;
  }
  busy(form, true, "发送中…");
  try {
    const result = await rpc("send_code", phone);
    if (!result || !result.ok) throw new Error((result && result.error) || "发送失败");
    if (result.warning) showError(result.warning);
    setPhase("code");
  } catch (e) {
    showError(e.message);
  } finally {
    busy(form, false);
  }
});

$("code-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  showError("");
  const form = event.currentTarget;
  busy(form, true, "验证中…");
  try {
    const result = await rpc("verify_code", $("code").value.trim());
    if (!result || !result.ok) throw new Error((result && result.error) || "验证失败");
    if (result.stage === "need_password") {
      setPhase("password");
    } else {
      $("code").value = "";
      setPhase("done");
    }
  } catch (e) {
    showError(e.message);
  } finally {
    busy(form, false);
  }
});

$("password-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  showError("");
  const form = event.currentTarget;
  busy(form, true, "验证中…");
  try {
    const result = await rpc("verify_password", $("tg-password").value);
    if (!result || !result.ok) throw new Error((result && result.error) || "验证失败");
    $("tg-password").value = "";
    setPhase("done");
  } catch (e) {
    showError(e.message);
  } finally {
    busy(form, false);
  }
});

$("enter-app").addEventListener("click", () => location.replace("/"));
init();
