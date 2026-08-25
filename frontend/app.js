const API = "/api";
const app = document.getElementById("app");
let publicConfig = {
  product_name: "WaterFlow",
  building_name: "",
  super_admin_app_name: "WaterFlow Super Admin",
  admin_app_name: "WaterFlow Admin",
  tenant_app_name: "WaterFlow",
  link_base: "",
};

if ("serviceWorker" in navigator) {
  navigator.serviceWorker
    .register("/sw.js")
    .then((reg) => {
      // Browsers throttle automatic update checks on an already-registered
      // service worker - sometimes for up to 24h - which is why a fresh
      // deploy can silently not reach an already-installed PWA for a
      // while. Force an immediate check every time the app loads instead
      // of waiting on the browser's own schedule.
      reg.update().catch(() => {});
      reg.addEventListener("updatefound", () => {
        const installing = reg.installing;
        if (!installing) return;
        installing.addEventListener("statechange", () => {
          if (installing.state === "installed" && navigator.serviceWorker.controller) {
            showUpdateBanner();
          }
        });
      });
    })
    .catch(() => {});

  let reloadedForUpdate = false;
  navigator.serviceWorker.addEventListener("controllerchange", () => {
    if (reloadedForUpdate) return;
    reloadedForUpdate = true;
    window.location.reload();
  });
}

function showUpdateBanner() {
  if (document.getElementById("wf-update-banner")) return;
  const banner = document.createElement("div");
  banner.id = "wf-update-banner";
  banner.style.cssText =
    "position:fixed;left:0;right:0;bottom:0;z-index:1000;background:var(--accent-mint);color:#0a0e16;" +
    "padding:12px 16px;display:flex;justify-content:space-between;align-items:center;gap:12px;" +
    "font-size:13.5px;font-weight:600;box-shadow:0 -2px 12px rgba(0,0,0,0.3);";
  banner.innerHTML =
    `<span>A new version of this app is available.</span>` +
    `<button style="background:#0a0e16;color:#fff;border:none;padding:8px 14px;border-radius:8px;font-weight:600;cursor:pointer;">Refresh</button>`;
  banner.querySelector("button").onclick = () => window.location.reload();
  document.body.appendChild(banner);
}

function authHeader() {
  const t = localStorage.getItem("wf_token");
  return t ? { Authorization: "Bearer " + t } : {};
}

// ---------------- WebAuthn (fingerprint / Face ID) helpers ----------------

function b64urlToBuf(s) {
  s = s.replace(/-/g, "+").replace(/_/g, "/");
  while (s.length % 4) s += "=";
  const bin = atob(s);
  const buf = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) buf[i] = bin.charCodeAt(i);
  return buf.buffer;
}

function bufToB64url(buf) {
  const bytes = new Uint8Array(buf);
  let bin = "";
  for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
  return btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function webauthnSupported() {
  return !!(window.PublicKeyCredential && navigator.credentials && navigator.credentials.create);
}

// ---------------- Type-to-confirm modal (for destructive actions) ----------------

function confirmWithTyping({ title, message, expectedText, confirmLabel = "Delete", onConfirm }) {
  const backdrop = el(`
    <div style="position:fixed;inset:0;background:rgba(0,0,0,0.6);z-index:999;display:flex;align-items:center;justify-content:center;padding:20px;">
      <div class="card" style="max-width:420px;width:100%;border-color:var(--accent-red);">
        <h2 class="page-title" style="font-size:16px;color:var(--accent-red);">${title}</h2>
        <p style="color:var(--text-muted);font-size:13px;margin-bottom:14px;">${message}</p>
        <label>Type <b style="color:var(--text);">${expectedText}</b> to confirm</label>
        <input id="ctc-input" autocomplete="off" />
        <div id="ctc-error" class="error hidden" style="margin-top:8px;"></div>
        <div style="display:flex;gap:8px;margin-top:14px;">
          <button class="btn-danger" id="ctc-confirm" style="flex:1;">${confirmLabel}</button>
          <button class="btn-ghost" id="ctc-cancel" style="flex:1;">Cancel</button>
        </div>
      </div>
    </div>
  `);
  document.body.appendChild(backdrop);
  const input = backdrop.querySelector("#ctc-input");
  input.focus();
  backdrop.querySelector("#ctc-cancel").onclick = () => backdrop.remove();
  backdrop.addEventListener("click", (e) => {
    if (e.target === backdrop) backdrop.remove();
  });
  backdrop.querySelector("#ctc-confirm").onclick = async () => {
    const errBox = backdrop.querySelector("#ctc-error");
    if (input.value.trim() !== expectedText) {
      errBox.textContent = "That doesn't match - type it exactly as shown.";
      errBox.classList.remove("hidden");
      return;
    }
    const btn = backdrop.querySelector("#ctc-confirm");
    btn.disabled = true;
    btn.textContent = "Working...";
    try {
      await onConfirm();
      backdrop.remove();
    } catch (e) {
      errBox.textContent = e.message || "Something went wrong.";
      errBox.classList.remove("hidden");
      btn.disabled = false;
      btn.textContent = confirmLabel;
    }
  };
}

async function webauthnEnableOnThisDevice() {
  const opts = await api("/auth/webauthn/register/options", { method: "POST" });
  const publicKey = {
    ...opts.options,
    challenge: b64urlToBuf(opts.options.challenge),
    user: { ...opts.options.user, id: b64urlToBuf(opts.options.user.id) },
    excludeCredentials: (opts.options.excludeCredentials || []).map((c) => ({
      ...c,
      id: b64urlToBuf(c.id),
    })),
  };
  const cred = await navigator.credentials.create({ publicKey });
  const label =
    (navigator.userAgentData && navigator.userAgentData.platform) ||
    navigator.platform ||
    "This device";
  const credentialJson = {
    id: cred.id,
    rawId: bufToB64url(cred.rawId),
    type: cred.type,
    response: {
      clientDataJSON: bufToB64url(cred.response.clientDataJSON),
      attestationObject: bufToB64url(cred.response.attestationObject),
      transports: cred.response.getTransports ? cred.response.getTransports() : undefined,
    },
  };
  await api("/auth/webauthn/register/verify", {
    method: "POST",
    body: JSON.stringify({ challenge_id: opts.challenge_id, credential: credentialJson, device_label: label }),
  });
}

async function webauthnLogin() {
  const opts = await api("/auth/webauthn/login/options", { method: "POST" });
  const publicKey = {
    ...opts.options,
    challenge: b64urlToBuf(opts.options.challenge),
    allowCredentials: (opts.options.allowCredentials || []).map((c) => ({
      ...c,
      id: b64urlToBuf(c.id),
    })),
  };
  const assertion = await navigator.credentials.get({ publicKey });
  const credentialJson = {
    id: assertion.id,
    rawId: bufToB64url(assertion.rawId),
    type: assertion.type,
    response: {
      clientDataJSON: bufToB64url(assertion.response.clientDataJSON),
      authenticatorData: bufToB64url(assertion.response.authenticatorData),
      signature: bufToB64url(assertion.response.signature),
      userHandle: assertion.response.userHandle ? bufToB64url(assertion.response.userHandle) : undefined,
    },
  };
  return api("/auth/webauthn/login/verify", {
    method: "POST",
    body: JSON.stringify({ challenge_id: opts.challenge_id, credential: credentialJson }),
  });
}

async function api(path, opts = {}) {
  const res = await fetch(API + path, {
    ...opts,
    headers: { "Content-Type": "application/json", ...authHeader(), ...(opts.headers || {}) },
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || res.statusText);
  }
  const ct = res.headers.get("content-type") || "";
  return ct.includes("application/json") ? res.json() : res;
}

function linkFor(path) {
  const base = publicConfig.link_base || window.location.origin;
  return `${base}${path}`;
}

function qrImg(link) {
  return `<div class="qr-box"><img src="${API}/qrcode?data=${encodeURIComponent(link)}" alt="QR code" /></div>`;
}

function el(html) {
  const d = document.createElement("div");
  d.innerHTML = html.trim();
  return d.firstChild;
}

function periodPicker(onPick) {
  const wrap = el(`
    <div style="margin-bottom:10px;">
      <div class="row" style="display:flex;gap:8px;flex-wrap:wrap;">
        <button class="btn-ghost btn-small" data-p="daily">Today</button>
        <button class="btn-ghost btn-small" data-p="weekly">Week</button>
        <button class="btn-ghost btn-small" data-p="monthly">Month</button>
        <button class="btn-ghost btn-small" data-p="custom">Custom</button>
      </div>
      <div class="custom-range hidden" style="display:flex;gap:8px;margin-top:8px;align-items:flex-end;flex-wrap:wrap;">
        <div><label style="font-size:11px;">Start</label><input type="date" class="range-start" style="margin:0;" /></div>
        <div><label style="font-size:11px;">End</label><input type="date" class="range-end" style="margin:0;" /></div>
        <button class="btn btn-small range-go">Download</button>
      </div>
    </div>
  `);
  const rangeBox = wrap.querySelector(".custom-range");
  wrap.querySelectorAll("button[data-p]").forEach((b) => {
    b.onclick = () => {
      if (b.dataset.p === "custom") {
        rangeBox.classList.toggle("hidden");
        return;
      }
      onPick(b.dataset.p);
    };
  });
  wrap.querySelector(".range-go").onclick = () => {
    const start = wrap.querySelector(".range-start").value;
    const end = wrap.querySelector(".range-end").value;
    if (!start || !end) {
      alert("Pick both a start and end date.");
      return;
    }
    // Backend expects full ISO datetimes; treat the end date as inclusive
    // through end-of-day so a same-day range (start === end) still covers
    // that whole day instead of resolving to a zero-width window.
    onPick("custom", `${start}T00:00:00`, `${end}T23:59:59`);
  };
  return wrap;
}

function downloadPdf(path, filename) {
  fetch(API + path, { headers: authHeader() })
    .then((r) => r.blob())
    .then((blob) => {
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = filename;
      a.click();
    });
}

async function copyToClipboard(text, buttonEl) {
  const originalLabel = buttonEl.textContent;
  let ok = false;
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      ok = true;
    }
  } catch (e) {
    ok = false;
  }
  if (!ok) {
    // Fallback for plain http:// (clipboard API is unavailable outside secure
    // contexts) - classic textarea+execCommand trick, still broadly supported.
    try {
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.focus();
      ta.select();
      ok = document.execCommand("copy");
      document.body.removeChild(ta);
    } catch (e) {
      ok = false;
    }
  }
  buttonEl.textContent = ok ? "Copied!" : "Couldn't copy - select manually";
  setTimeout(() => (buttonEl.textContent = originalLabel), 1800);
}

// ---------------- Auth screens ----------------

function renderLogin(expectedRole, appName) {
  app.innerHTML = "";
  app.appendChild(el(`
    <div class="auth-wrap">
      <div class="auth-card">
        <div class="auth-brand">
          <div class="brand-mark">${publicConfig.product_name[0]}</div>
          <div class="auth-title">${appName}</div>
          <div class="auth-sub">${publicConfig.building_name || publicConfig.product_name}</div>
        </div>
        <div id="login-error" class="error hidden"></div>
        ${
          webauthnSupported()
            ? `<button class="btn-ghost" id="webauthn-login-btn" style="width:100%;margin-bottom:14px;">🔐 Unlock with Fingerprint / Face ID</button>
               <div style="text-align:center;color:var(--text-muted);font-size:12px;margin-bottom:14px;">or log in with a password</div>`
            : ""
        }
        <label>Phone or Email</label>
        <input id="li-id" placeholder="e.g. 082 123 4567" />
        <label>Password</label>
        <input id="li-pw" type="password" />
        <button class="btn" id="login-btn" style="width:100%;margin-top:6px;">Log In</button>
        <div style="text-align:center;margin-top:16px;">
          <button class="link" id="forgot-btn">Forgot password?</button>
        </div>
        <div id="forgot-panel" class="hidden" style="margin-top:16px;border-top:1px solid var(--border);padding-top:16px;">
          <label>Phone or Email</label>
          <input id="forgot-id" placeholder="Enter your phone or email" />
          <button class="btn-ghost" id="forgot-submit" style="width:100%;">Request Reset</button>
          <div id="forgot-msg" class="hidden" style="margin-top:10px;color:var(--accent-mint);font-size:13px;">
            If that account exists, your request has been sent to be approved. You'll be contacted with a reset link.
          </div>
        </div>
      </div>
    </div>
  `));

  document.getElementById("login-btn").onclick = doLogin;
  ["li-id", "li-pw"].forEach((id) => {
    document.getElementById(id).addEventListener("keydown", (e) => {
      if (e.key === "Enter") doLogin();
    });
  });

  const webauthnBtn = document.getElementById("webauthn-login-btn");
  if (webauthnBtn) {
    webauthnBtn.onclick = async () => {
      const errBox = document.getElementById("login-error");
      errBox.classList.add("hidden");
      const originalLabel = webauthnBtn.textContent;
      webauthnBtn.textContent = "Waiting for fingerprint / Face ID...";
      webauthnBtn.disabled = true;
      try {
        const result = await webauthnLogin();
        localStorage.setItem("wf_token", result.token);
        localStorage.setItem("wf_role", result.role);
        if (result.role !== expectedRole) {
          window.location.href =
            result.role === "super_admin" ? "/super" : result.role === "tenant" ? "/tenant" : "/admin";
          return;
        }
        route();
      } catch (e) {
        // A cancelled/failed biometric prompt shouldn't look like a hard
        // error - it's an expected outcome if the user backs out or this
        // device isn't enrolled, so just let them fall back to the password
        // fields below rather than showing a scary red error box.
        webauthnBtn.textContent = originalLabel;
        webauthnBtn.disabled = false;
      }
    };
  }

  async function doLogin() {
    const errBox = document.getElementById("login-error");
    errBox.classList.add("hidden");
    try {
      const result = await api("/auth/login", {
        method: "POST",
        body: JSON.stringify({
          identifier: document.getElementById("li-id").value,
          password: document.getElementById("li-pw").value,
        }),
      });
      localStorage.setItem("wf_token", result.token);
      localStorage.setItem("wf_role", result.role);
      if (result.role !== expectedRole) {
        const target = result.role === "super_admin" ? "/super"
          : result.role === "tenant" ? "/tenant"
          : "/admin";
        window.location.href = target;
        return;
      }
      route();
    } catch (e) {
      errBox.textContent = "Invalid phone/email or password.";
      errBox.classList.remove("hidden");
    }
  }

  document.getElementById("forgot-btn").onclick = () => {
    document.getElementById("forgot-panel").classList.remove("hidden");
  };
  document.getElementById("forgot-submit").onclick = doForgot;
  document.getElementById("forgot-id").addEventListener("keydown", (e) => {
    if (e.key === "Enter") doForgot();
  });
  async function doForgot() {
    await api("/auth/reset-request", {
      method: "POST",
      body: JSON.stringify({ identifier: document.getElementById("forgot-id").value }),
    });
    document.getElementById("forgot-msg").classList.remove("hidden");
  }
}

function renderAcceptToken(kind, token) {
  app.innerHTML = "";
  const infoUrl = kind === "invite" ? `/invite/${token}` : `/reset/${token}`;
  const acceptUrl = infoUrl + "/accept";

  api(infoUrl)
    .then((info) => {
      app.appendChild(el(`
        <div class="auth-wrap">
          <div class="auth-card">
            <div class="auth-brand">
              <div class="brand-mark">${publicConfig.product_name[0]}</div>
              <div class="auth-title">Welcome, ${info.name}</div>
              <div class="auth-sub">${kind === "invite" ? "Set a password to activate your account" : "Set a new password"}</div>
            </div>
            <div id="accept-error" class="error hidden"></div>
            <label>New Password</label>
            <input id="ac-pw" type="password" />
            <label>Confirm Password</label>
            <input id="ac-pw2" type="password" />
            <button class="btn" id="accept-btn" style="width:100%;">Set Password &amp; Log In</button>
          </div>
        </div>
      `));

      document.getElementById("accept-btn").onclick = doAccept;
      ["ac-pw", "ac-pw2"].forEach((id) => {
        document.getElementById(id).addEventListener("keydown", (e) => {
          if (e.key === "Enter") doAccept();
        });
      });
      async function doAccept() {
        const pw = document.getElementById("ac-pw").value;
        const pw2 = document.getElementById("ac-pw2").value;
        const errBox = document.getElementById("accept-error");
        if (pw.length < 8) {
          errBox.textContent = "Password must be at least 8 characters.";
          errBox.classList.remove("hidden");
          return;
        }
        if (pw !== pw2) {
          errBox.textContent = "Passwords don't match.";
          errBox.classList.remove("hidden");
          return;
        }
        try {
          const result = await api(acceptUrl, { method: "POST", body: JSON.stringify({ password: pw }) });
          localStorage.setItem("wf_token", result.token);
          localStorage.setItem("wf_role", result.role);
          window.location.href = result.role === "super_admin" ? "/super"
            : result.role === "tenant" ? "/tenant"
            : "/admin";
        } catch (e) {
          errBox.textContent = e.message;
          errBox.classList.remove("hidden");
        }
      }
    })
    .catch(() => {
      app.appendChild(el(`
        <div class="auth-wrap"><div class="auth-card">
          <div class="auth-title">Link invalid or expired</div>
          <div class="auth-sub" style="margin-top:8px;">Ask whoever sent you this link to generate a new one.</div>
        </div></div>
      `));
    });
}

// ---------------- Shell (sidebar + main) ----------------

function renderShell(role, appName, navItems, activeIdx) {
  app.innerHTML = "";
  const nav = navItems
    .map(
      (n, i) =>
        `<div class="nav-item ${i === activeIdx ? "active" : ""}" data-nav="${n.key}"><span class="nav-dot"></span>${n.label}</div>`
    )
    .join("");
  const shell = el(`
    <div class="layout">
      <div class="sidebar">
        <div class="brand">
          <div class="brand-mark">${publicConfig.product_name[0]}</div>
          <div>
            <div class="brand-name">${appName}</div>
            <div class="brand-sub">${publicConfig.building_name || publicConfig.product_name}</div>
          </div>
        </div>
        ${nav}
        <div style="flex:1"></div>
        <div class="nav-item" data-nav="logout"><span class="nav-dot"></span>Log Out</div>
      </div>
      <div class="main" id="main-content"></div>
    </div>
  `);
  app.appendChild(shell);
  const loginPath = role === "super_admin" ? "/super" : role === "tenant" ? "/tenant" : "/admin";
  shell.querySelector('[data-nav="logout"]').onclick = () => {
    localStorage.removeItem("wf_token");
    localStorage.removeItem("wf_role");
    window.location.href = loginPath;
  };
  return document.getElementById("main-content");
}

// ---------------- Biometric Login card (embedded in Settings/Account, any role) ----------------

function renderBiometricCard(container) {
  if (!webauthnSupported()) return;
  const card = document.createElement("div");
  card.className = "card";
  card.innerHTML = `
    <h2 class="page-title" style="font-size:15px;">🔐 Biometric Login</h2>
    <p style="color:var(--text-muted);font-size:13px;margin-bottom:10px;">
      Unlock this app with fingerprint / Face ID instead of typing a password each time.
    </p>
    <button class="btn-ghost" id="manage-biometric-btn">Manage Devices</button>
  `;
  container.appendChild(card);
  card.querySelector("#manage-biometric-btn").onclick = () => openBiometricPanel();
}

// ---------------- Biometric device management (overlay, any role) ----------------

async function openBiometricPanel() {
  const backdrop = el(`
    <div id="bio-backdrop" style="position:fixed;inset:0;background:rgba(0,0,0,0.6);z-index:999;display:flex;align-items:center;justify-content:center;padding:20px;">
      <div class="card" style="max-width:420px;width:100%;max-height:80vh;overflow:auto;">
        <h2 class="page-title" style="font-size:16px;">🔐 Biometric Login</h2>
        <p style="color:var(--text-muted);font-size:13px;margin-bottom:14px;">
          Devices below can log in with fingerprint / Face ID instead of a password.
        </p>
        <div id="bio-list"><p style="color:var(--text-muted);font-size:13px;">Loading...</p></div>
        <button class="btn" id="bio-add-btn" style="width:100%;margin-top:14px;">+ Enable on This Device</button>
        <button class="btn-ghost" id="bio-close-btn" style="width:100%;margin-top:8px;">Close</button>
      </div>
    </div>
  `);
  document.body.appendChild(backdrop);
  backdrop.addEventListener("click", (e) => {
    if (e.target === backdrop) backdrop.remove();
  });
  document.getElementById("bio-close-btn").onclick = () => backdrop.remove();

  async function refreshList() {
    const list = document.getElementById("bio-list");
    try {
      const creds = await api("/auth/webauthn/credentials");
      list.innerHTML = creds.length
        ? creds
            .map(
              (c) => `
        <div class="list-row" data-cred-row="${c.id}">
          <div>
            <div class="name">${c.device_label || "Unnamed device"}</div>
            <div class="meta">Added ${new Date(c.created_at).toLocaleDateString()}${
                c.last_used_at ? " · last used " + new Date(c.last_used_at).toLocaleDateString() : ""
              }</div>
          </div>
          <button class="btn-danger btn-small" data-remove-cred="${c.id}">Remove</button>
        </div>`
            )
            .join("")
        : `<p style="color:var(--text-muted);font-size:13px;">No devices enabled yet.</p>`;
      list.querySelectorAll("[data-remove-cred]").forEach((btn) => {
        btn.onclick = async () => {
          await api(`/auth/webauthn/credentials/${btn.dataset.removeCred}`, { method: "DELETE" });
          refreshList();
        };
      });
    } catch (e) {
      list.innerHTML = `<p class="error">${e.message}</p>`;
    }
  }

  document.getElementById("bio-add-btn").onclick = async (e) => {
    const btn = e.target;
    const original = btn.textContent;
    btn.textContent = "Follow the prompt...";
    btn.disabled = true;
    try {
      await webauthnEnableOnThisDevice();
      refreshList();
    } catch (err) {
      alert("Couldn't enable biometric login: " + (err.message || err));
    }
    btn.textContent = original;
    btn.disabled = false;
  };

  refreshList();
}

// ---------------- Super Admin dashboard ----------------

async function renderSuperAdmin() {
  const activeIdx =
    window.location.hash === "#units" ? 1 :
    window.location.hash === "#tenants" ? 2 :
    window.location.hash === "#settings" ? 3 : 0;
  const main = renderShell(
    "super_admin",
    publicConfig.super_admin_app_name,
    [
      { key: "dashboard", label: "Dashboard" },
      { key: "units", label: "Units & Meters" },
      { key: "tenants", label: "Tenants" },
      { key: "settings", label: "Settings" },
    ],
    activeIdx
  );

  main.parentElement.querySelector('[data-nav="units"]').onclick = () => {
    window.location.hash = "#units";
    renderSuperAdmin();
  };
  main.parentElement.querySelector('[data-nav="tenants"]').onclick = () => {
    window.location.hash = "#tenants";
    renderSuperAdmin();
  };
  main.parentElement.querySelector('[data-nav="dashboard"]').onclick = () => {
    window.location.hash = "";
    renderSuperAdmin();
  };
  main.parentElement.querySelector('[data-nav="settings"]').onclick = () => {
    window.location.hash = "#settings";
    renderSuperAdmin();
  };

  if (window.location.hash === "#units") return renderAdminUnits(main);
  if (window.location.hash === "#tenants") return renderAllTenants(main);
  if (window.location.hash === "#settings") return renderSuperAdminSettings(main);

  main.innerHTML = `
    <div class="topbar">
      <div><h1 class="page-title">Admins</h1><div class="page-sub">Manage caretaker/admin accounts for ${publicConfig.building_name || "this building"}</div></div>
      <button class="btn" id="invite-admin-btn">+ Invite Admin</button>
    </div>
    <div id="invite-result"></div>
    <div class="card" id="admins-card">Loading...</div>
    <div class="card">
      <h2 class="page-title" style="font-size:16px;">Pending Password Resets</h2>
      <div id="resets-list">Loading...</div>
    </div>
  `;

  document.getElementById("invite-admin-btn").onclick = () => showInviteForm("admin");

  async function loadAdmins() {
    const admins = await api("/super/admins");
    const card = document.getElementById("admins-card");
    if (admins.length === 0) {
      card.innerHTML = `<p style="color:var(--text-muted);font-size:13.5px;">No admins yet — invite one above.</p>`;
      return;
    }
    card.innerHTML = admins
      .map(
        (a) => `
      <div class="list-row">
        <div>
          <div class="name">${a.name}</div>
          <div class="meta">${a.phone || a.email || "—"}</div>
        </div>
        <div style="display:flex;align-items:center;gap:10px;">
          <span class="badge ${a.activated ? "ok" : "warn"}">
            <span class="badge-dot"></span>${a.activated ? "Active" : "Invite Pending"}
          </span>
          ${a.active
            ? `<button class="btn-danger" data-deactivate="${a.id}">Deactivate</button>`
            : `<button class="btn-danger btn-small" data-delete-admin="${a.id}">Delete Permanently</button>`}
        </div>
      </div>`
      )
      .join("");
    card.querySelectorAll("[data-deactivate]").forEach((btn) => {
      btn.onclick = async () => {
        await api(`/super/admins/${btn.dataset.deactivate}/deactivate`, { method: "PUT" });
        loadAdmins();
      };
    });
    card.querySelectorAll("[data-delete-admin]").forEach((btn) => {
      btn.onclick = async () => {
        if (!confirm("Permanently delete this admin account? This cannot be undone.")) return;
        await api(`/super/admins/${btn.dataset.deleteAdmin}`, { method: "DELETE" });
        loadAdmins();
      };
    });
  }

  async function loadResets() {
    const requests = await api("/reset-requests");
    const list = document.getElementById("resets-list");
    if (requests.length === 0) {
      list.innerHTML = `<p style="color:var(--text-muted);font-size:13.5px;">No pending requests.</p>`;
      return;
    }
    list.innerHTML = requests
      .map(
        (r) => `
      <div class="list-row">
        <div><div class="name">${r.name}</div><div class="meta">${r.phone || ""} · requested ${new Date(r.created_at).toLocaleString()}</div></div>
        <div style="display:flex;gap:8px;">
          <button class="btn btn-small" data-approve="${r.id}">Approve</button>
          <button class="btn-ghost btn-small" data-deny="${r.id}">Deny</button>
        </div>
      </div>`
      )
      .join("");
    list.querySelectorAll("[data-approve]").forEach((btn) => {
      btn.onclick = async () => {
        const result = await api(`/reset-requests/${btn.dataset.approve}/approve`, { method: "POST" });
        showLinkResult(`Reset link for ${result.user_name}`, result.link);
        loadResets();
      };
    });
    list.querySelectorAll("[data-deny]").forEach((btn) => {
      btn.onclick = async () => {
        await api(`/reset-requests/${btn.dataset.deny}/deny`, { method: "POST" });
        loadResets();
      };
    });
  }

  function showInviteForm(role) {
    const box = document.getElementById("invite-result");
    box.innerHTML = `
      <div class="card">
        <h2 class="page-title" style="font-size:16px;">Invite ${role === "admin" ? "Admin" : "Tenant"}</h2>
        <label>Name</label><input id="inv-name" />
        <label>Phone (for WhatsApp link)</label><input id="inv-phone" />
        <label>Email (optional)</label><input id="inv-email" />
        <button class="btn" id="inv-submit">Generate Invite</button>
      </div>
    `;
    document.getElementById("inv-submit").onclick = async () => {
      const result = await api("/super/admins/invite", {
        method: "POST",
        body: JSON.stringify({
          name: document.getElementById("inv-name").value,
          phone: document.getElementById("inv-phone").value,
          email: document.getElementById("inv-email").value,
        }),
      });
      showLinkResult("Admin invite link", result.link);
      loadAdmins();
    };
  }

  function showLinkResult(title, link) {
    const box = document.getElementById("invite-result");
    box.innerHTML = `
      <div class="card">
        <h2 class="page-title" style="font-size:16px;">${title}</h2>
        <div class="link-display">${link}</div>
        ${qrImg(link)}
        <button class="btn-ghost" id="copy-link" style="width:100%;">Copy Link</button>
      </div>
    `;
    document.getElementById("copy-link").onclick = (e) => copyToClipboard(link, e.target);
  }

  loadAdmins();
  loadResets();
}

async function renderTariffCard(container) {
  const card = document.createElement("div");
  card.className = "card";
  card.innerHTML = `
    <h2 class="page-title" style="font-size:15px;">Municipal Water Rate</h2>
    <p style="color:var(--text-muted);font-size:13px;margin-bottom:10px;">
      Used to calculate liters purchased on prepaid top-ups and Rand owed on monthly statements.
    </p>
    <label>Rate (R per kL / R per 1000 L)</label>
    <div class="row" style="display:flex;gap:8px;">
      <input id="tariff-rate" type="number" step="0.01" style="margin:0;" />
      <button class="btn btn-small" id="tariff-save">Save</button>
    </div>
    <div id="tariff-result" style="margin-top:8px;font-size:13px;"></div>
  `;
  container.appendChild(card);

  const tariff = await api("/admin/tariff");
  card.querySelector("#tariff-rate").value = tariff.rate_rand_per_kl;

  card.querySelector("#tariff-save").onclick = async () => {
    const rate = parseFloat(card.querySelector("#tariff-rate").value);
    const resultBox = card.querySelector("#tariff-result");
    if (!rate || rate <= 0) {
      resultBox.style.color = "var(--accent-red)";
      resultBox.textContent = "Enter a valid rate.";
      return;
    }
    try {
      await api("/admin/tariff", { method: "PUT", body: JSON.stringify({ rate_rand_per_kl: rate }) });
      resultBox.style.color = "var(--accent-mint)";
      resultBox.textContent = `Saved - R${rate.toFixed(2)}/kL now applies to future top-ups and statements.`;
    } catch (e) {
      resultBox.style.color = "var(--accent-red)";
      resultBox.textContent = e.message;
    }
  };
}

// ---------------- Admin Settings (tariff + own password) ----------------

async function renderAdminSettings(main) {
  main.innerHTML = `<div class="topbar"><h1 class="page-title">Settings</h1></div>`;
  await renderTariffCard(main);

  const pwCard = document.createElement("div");
  pwCard.className = "card";
  pwCard.innerHTML = `
    <h2 class="page-title" style="font-size:15px;">Change My Password</h2>
    <label>Current Password</label><input id="cp-current" type="password" />
    <label>New Password</label><input id="cp-new" type="password" />
    <button class="btn" id="cp-submit">Change Password</button>
    <div id="cp-result" style="margin-top:8px;font-size:13px;"></div>
  `;
  main.appendChild(pwCard);

  document.getElementById("cp-submit").onclick = async () => {
    const resultBox = document.getElementById("cp-result");
    try {
      const result = await api("/auth/change-password", {
        method: "POST",
        body: JSON.stringify({
          current_password: document.getElementById("cp-current").value,
          new_password: document.getElementById("cp-new").value,
        }),
      });
      localStorage.setItem("wf_token", result.token);
      resultBox.style.color = "var(--accent-mint)";
      resultBox.textContent = "Password changed.";
      document.getElementById("cp-current").value = "";
      document.getElementById("cp-new").value = "";
    } catch (err) {
      resultBox.style.color = "var(--accent-red)";
      resultBox.textContent = err.message;
    }
  };

  renderBiometricCard(main);
}

// ---------------- Admin dashboard ----------------

async function renderAdmin() {
  const main = renderShell(
    "admin",
    publicConfig.admin_app_name,
    [
      { key: "dashboard", label: "Dashboard" },
      { key: "units", label: "Units & Meters" },
      { key: "tenants", label: "Tenants" },
      { key: "settings", label: "Settings" },
    ],
    window.location.hash === "#units" ? 1 : window.location.hash === "#tenants" ? 2 : window.location.hash === "#settings" ? 3 : 0
  );

  main.parentElement.querySelector('[data-nav="units"]').onclick = () => {
    window.location.hash = "#units";
    renderAdmin();
  };
  main.parentElement.querySelector('[data-nav="tenants"]').onclick = () => {
    window.location.hash = "#tenants";
    renderAdmin();
  };
  main.parentElement.querySelector('[data-nav="settings"]').onclick = () => {
    window.location.hash = "#settings";
    renderAdmin();
  };
  main.parentElement.querySelector('[data-nav="dashboard"]').onclick = () => {
    window.location.hash = "";
    renderAdmin();
  };

  if (window.location.hash === "#units") return renderAdminUnits(main);
  if (window.location.hash === "#tenants") return renderAllTenants(main);
  if (window.location.hash === "#settings") return renderAdminSettings(main);

  main.innerHTML = `
    <div class="topbar">
      <div><h1 class="page-title">Dashboard</h1><div class="page-sub">${publicConfig.building_name || ""}</div></div>
    </div>
    <div class="card">
      <h2 class="page-title" style="font-size:16px;">Pending Password Resets</h2>
      <div id="resets-list">Loading...</div>
    </div>
    <div id="reset-result"></div>
  `;

  async function loadResets() {
    const requests = await api("/reset-requests");
    const list = document.getElementById("resets-list");
    if (requests.length === 0) {
      list.innerHTML = `<p style="color:var(--text-muted);font-size:13.5px;">No pending requests.</p>`;
      return;
    }
    list.innerHTML = requests
      .map(
        (r) => `
      <div class="list-row">
        <div><div class="name">${r.name}</div><div class="meta">${r.phone || ""} · requested ${new Date(r.created_at).toLocaleString()}</div></div>
        <div style="display:flex;gap:8px;">
          <button class="btn btn-small" data-approve="${r.id}">Approve</button>
          <button class="btn-ghost btn-small" data-deny="${r.id}">Deny</button>
        </div>
      </div>`
      )
      .join("");
    list.querySelectorAll("[data-approve]").forEach((btn) => {
      btn.onclick = async () => {
        const result = await api(`/reset-requests/${btn.dataset.approve}/approve`, { method: "POST" });
        document.getElementById("reset-result").innerHTML = `
          <div class="card">
            <h2 class="page-title" style="font-size:16px;">Reset link for ${result.user_name}</h2>
            <div class="link-display">${result.link}</div>
            ${qrImg(result.link)}
            <button class="btn-ghost" id="copy-reset-link" style="width:100%;">Copy Link</button>
          </div>`;
        document.getElementById("copy-reset-link").onclick = (e) => copyToClipboard(result.link, e.target);
        loadResets();
      };
    });
    list.querySelectorAll("[data-deny]").forEach((btn) => {
      btn.onclick = async () => {
        await api(`/reset-requests/${btn.dataset.deny}/deny`, { method: "POST" });
        loadResets();
      };
    });
  }
  loadResets();
}

async function renderSuperAdminSettings(main) {
  main.innerHTML = `<div class="topbar"><h1 class="page-title">Settings</h1></div><div class="card">Loading...</div>`;
  const data = await api("/super/settings");
  const values = data.values;
  const restartKeys = new Set(data.restart_required_keys);
  const sensitiveKeys = new Set(data.sensitive_keys);

  const groups = [
    {
      title: "Branding",
      fields: [
        ["product_name", "Product Name", "text"],
        ["building_name", "Building Name", "text"],
        ["super_admin_app_name", "Super Admin App Name", "text"],
        ["admin_app_name", "Admin App Name", "text"],
        ["tenant_app_name", "Tenant App Name", "text"],
      ],
    },
    {
      title: "ChirpStack Connection",
      fields: [
        ["chirpstack_mqtt_host", "MQTT Host", "text"],
        ["chirpstack_mqtt_port", "MQTT Port", "number"],
        ["chirpstack_mqtt_user", "MQTT Username", "text"],
        ["chirpstack_mqtt_pass", "MQTT Password", "password"],
        ["chirpstack_rest_url", "REST API URL", "text"],
        ["chirpstack_api_key", "REST API Key", "password"],
        ["chirpstack_region", "Region", "text"],
        ["chirpstack_tenant_name", "Tenant Name (exact match)", "text"],
      ],
    },
    {
      title: "Domain",
      fields: [["domain_name", "Domain Name (no https://)", "text"]],
    },
    {
      title: "Home Assistant MQTT Discovery (optional)",
      fields: [
        ["enable_ha_mqtt_discovery", "Enable", "checkbox"],
        ["ha_mqtt_host", "HA MQTT Host", "text"],
        ["ha_mqtt_port", "HA MQTT Port", "number"],
        ["ha_mqtt_user", "HA MQTT Username", "text"],
        ["ha_mqtt_pass", "HA MQTT Password", "password"],
      ],
    },
    {
      title: "Notifications",
      fields: [
        ["smtp_host", "SMTP Host", "text"],
        ["smtp_port", "SMTP Port", "number"],
        ["smtp_user", "SMTP Username", "text"],
        ["smtp_pass", "SMTP Password", "password"],
        ["smtp_from", "SMTP From Address", "text"],
        ["telegram_bot_token", "Telegram Bot Token", "password"],
        ["telegram_audit_chat_id", "Telegram Audit Chat ID", "text"],
        ["telegram_digest_chat_id", "Telegram Digest Chat ID", "text"],
        ["digest_send_time", "Digest Send Time (HH:MM, local)", "text"],
        ["digest_timezone_offset_hours", "Digest Time Zone Offset (hours from UTC, e.g. 2 for SAST)", "number"],
      ],
    },
  ];

  main.innerHTML = `
    <div class="topbar">
      <h1 class="page-title">Settings</h1>
      <div style="display:flex;gap:10px;">
        <button class="btn-ghost" id="export-btn">Export Backup</button>
        <label class="btn-ghost" style="cursor:pointer;margin:0;display:inline-flex;align-items:center;">
          Import Backup
          <input type="file" id="import-file" accept="application/json" class="hidden" />
        </label>
      </div>
    </div>
    <div class="card" style="border-color:var(--accent-amber);">
      <p style="font-size:13px;color:var(--accent-amber);margin:0;">
        ⚠ Exported backups contain real secrets (API keys, passwords) in plain text.
        Delete the file once you've confirmed an update worked - don't leave it sitting
        in a cloud-synced folder.
      </p>
    </div>
    <div id="restart-banner" class="hidden card" style="border-color:var(--accent-red);">
      <p style="font-size:13.5px;margin:0 0 10px;">Some changes need a restart to take effect.</p>
      <button class="btn-danger" id="restart-btn">Restart WaterFlow Now</button>
    </div>
    <div id="settings-form"></div>
    <div class="card">
      <h2 class="page-title" style="font-size:15px;">Change My Password</h2>
      <label>Current Password</label><input id="cp-current" type="password" />
      <label>New Password</label><input id="cp-new" type="password" />
      <button class="btn" id="cp-submit">Change Password</button>
      <div id="cp-result" style="margin-top:8px;font-size:13px;"></div>
    </div>
  `;

  const form = document.getElementById("settings-form");
  groups.forEach((group) => {
    const card = document.createElement("div");
    card.className = "card";
    card.innerHTML = `<h2 class="page-title" style="font-size:15px;">${group.title}</h2>`;
    group.fields.forEach(([key, label, type]) => {
      const val = values[key];
      const wrap = document.createElement("div");
      if (type === "checkbox") {
        wrap.innerHTML = `
          <label style="display:flex;align-items:center;gap:8px;">
            <input type="checkbox" id="set-${key}" ${val ? "checked" : ""} style="width:auto;margin:0;" />
            ${label}${restartKeys.has(key) ? ' <span class="meta">(restart required)</span>' : ""}
          </label>`;
      } else {
        wrap.innerHTML = `
          <label>${label}${restartKeys.has(key) ? ' <span class="meta">(restart required)</span>' : ""}</label>
          <input id="set-${key}" type="${type}" value="${val ?? ""}" />`;
      }
      card.appendChild(wrap);
    });
    form.appendChild(card);
    if (group.title === "Notifications") {
      const testCard = document.createElement("div");
      testCard.className = "card";
      testCard.innerHTML = `
        <p style="font-size:13px;color:var(--text-muted);margin-bottom:8px;">
          Sends yesterday's digest immediately, using whatever's saved above right now (save first if you just changed the token/chat ID) - the quickest way to confirm Telegram delivery works without waiting for the scheduled time.
        </p>
        <button class="btn-ghost" id="test-digest-btn">Send Test Digest Now</button>
        <div id="test-digest-result" style="margin-top:8px;font-size:13px;"></div>
      `;
      form.appendChild(testCard);
      testCard.querySelector("#test-digest-btn").onclick = async () => {
        const resultBox = testCard.querySelector("#test-digest-result");
        const btn = testCard.querySelector("#test-digest-btn");
        btn.disabled = true;
        btn.textContent = "Sending...";
        try {
          await api("/super/digest/send-test", { method: "POST" });
          resultBox.style.color = "var(--accent-mint)";
          resultBox.textContent = "Sent - check the Telegram chat.";
        } catch (e) {
          resultBox.style.color = "var(--accent-red)";
          resultBox.textContent = e.message;
        }
        btn.disabled = false;
        btn.textContent = "Send Test Digest Now";
      };
    }
  });

  const saveBar = document.createElement("div");
  saveBar.className = "card";
  saveBar.innerHTML = `<button class="btn" id="save-settings-btn" style="width:100%;">Save Settings</button>`;
  form.appendChild(saveBar);

  document.getElementById("save-settings-btn").onclick = async () => {
    const updates = {};
    groups.forEach((g) =>
      g.fields.forEach(([key, , type]) => {
        const el = document.getElementById(`set-${key}`);
        if (type === "checkbox") updates[key] = el.checked;
        else if (type === "number") updates[key] = parseInt(el.value, 10) || 0;
        else updates[key] = el.value;
      })
    );
    const result = await api("/super/settings", { method: "PUT", body: JSON.stringify(updates) });
    if (result.restart_required.length > 0) {
      document.getElementById("restart-banner").classList.remove("hidden");
      window.scrollTo(0, 0);
    } else {
      alert("Settings saved.");
    }
  };

  document.getElementById("restart-btn").onclick = async () => {
    if (!confirm("Restart WaterFlow now? This takes a few seconds and will briefly disconnect everyone.")) return;
    await api("/super/restart", { method: "POST" });
    alert("Restarting - give it about 15-30 seconds, then refresh.");
  };

  document.getElementById("export-btn").onclick = async () => {
    const res = await fetch(API + "/super/settings/export", { headers: authHeader() });
    const blob = await res.blob();
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "waterflow-settings-backup.json";
    a.click();
  };

  document.getElementById("import-file").onchange = async (e) => {
    const file = e.target.files[0];
    if (!file) return;
    const text = await file.text();
    try {
      const data = JSON.parse(text);
      const result = await api("/super/settings/import", { method: "POST", body: JSON.stringify(data) });
      alert("Settings imported.");
      if (result.restart_required.length > 0) {
        document.getElementById("restart-banner").classList.remove("hidden");
      }
      renderSuperAdminSettings(main);
    } catch (err) {
      alert("Import failed: " + err.message);
    }
  };

  document.getElementById("cp-submit").onclick = async () => {
    const resultBox = document.getElementById("cp-result");
    try {
      const result = await api("/auth/change-password", {
        method: "POST",
        body: JSON.stringify({
          current_password: document.getElementById("cp-current").value,
          new_password: document.getElementById("cp-new").value,
        }),
      });
      localStorage.setItem("wf_token", result.token);
      resultBox.style.color = "var(--accent-mint)";
      resultBox.textContent = "Password changed.";
      document.getElementById("cp-current").value = "";
      document.getElementById("cp-new").value = "";
    } catch (err) {
      resultBox.style.color = "var(--accent-red)";
      resultBox.textContent = err.message;
    }
  };

  renderBiometricCard(main);
  renderTariffCard(main);
}

// ---------------- All Tenants (flat, cross-unit view - Super Admin & Admin) ----------------

async function renderAllTenants(main) {
  main.innerHTML = `
    <div class="topbar">
      <div><h1 class="page-title">Tenants</h1><div class="page-sub">Every tenant account in ${publicConfig.building_name || "this building"}</div></div>
    </div>
    <div class="card" id="all-tenants-card">Loading...</div>
  `;

  async function refresh() {
    const tenants = await api("/admin/tenants");
    const card = document.getElementById("all-tenants-card");
    if (tenants.length === 0) {
      card.innerHTML = `<p style="color:var(--text-muted);font-size:13px;">No tenants yet.</p>`;
      return;
    }
    card.innerHTML = tenants
      .map(
        (t) => `
      <div class="list-row" data-tenant-row="${t.id}">
        <div>
          <div class="name">${t.name}${!t.active ? " (inactive)" : ""}</div>
          <div class="meta">${t.unit_number || "No unit"} · ${t.phone || t.email || "—"}</div>
        </div>
        <div style="display:flex;gap:6px;flex-wrap:wrap;justify-content:flex-end;align-items:center;">
          <span class="badge ${t.activated ? "ok" : "warn"}"><span class="badge-dot"></span>${t.activated ? "Active" : "Invite Pending"}</span>
          ${t.active ? `
            <button class="btn-ghost btn-small" data-edit-tenant="${t.id}">Edit</button>
            ${!t.activated ? `<button class="btn-ghost btn-small" data-resend="${t.id}">Resend Invite</button>` : ""}
            <button class="btn-danger btn-small" data-remove-tenant="${t.id}">Remove</button>
          ` : `<button class="btn-danger btn-small" data-delete-tenant="${t.id}">Delete Permanently</button>`}
        </div>
      </div>
      <div id="edit-tenant-form-${t.id}" class="hidden" style="padding:10px 0;"></div>
    `
      )
      .join("");

    card.querySelectorAll("[data-edit-tenant]").forEach((btn) => {
      btn.onclick = () => {
        const t = tenants.find((x) => x.id === btn.dataset.editTenant);
        const form = document.getElementById(`edit-tenant-form-${t.id}`);
        form.classList.toggle("hidden");
        if (form.classList.contains("hidden")) return;
        form.innerHTML = `
          <label>Name</label><input id="at-name-${t.id}" value="${t.name}" />
          <label>Phone</label><input id="at-phone-${t.id}" value="${t.phone || ""}" />
          <button class="btn btn-small" id="at-save-${t.id}">Save</button>
        `;
        document.getElementById(`at-save-${t.id}`).onclick = async () => {
          await api(`/admin/tenants/${t.id}`, {
            method: "PUT",
            body: JSON.stringify({
              name: document.getElementById(`at-name-${t.id}`).value,
              phone: document.getElementById(`at-phone-${t.id}`).value,
            }),
          });
          refresh();
        };
      };
    });
    card.querySelectorAll("[data-remove-tenant]").forEach((btn) => {
      btn.onclick = async () => {
        if (!confirm("Remove this tenant's access? This frees the unit for a new tenant.")) return;
        await api(`/admin/tenants/${btn.dataset.removeTenant}/deactivate`, { method: "PUT" });
        refresh();
      };
    });
    card.querySelectorAll("[data-delete-tenant]").forEach((btn) => {
      btn.onclick = async () => {
        if (!confirm("Permanently delete this tenant account? This cannot be undone. Billing and reading history for the unit itself is kept - only the account is removed.")) return;
        await api(`/admin/tenants/${btn.dataset.deleteTenant}`, { method: "DELETE" });
        refresh();
      };
    });
    card.querySelectorAll("[data-resend]").forEach((btn) => {
      btn.onclick = async () => {
        const result = await api(`/admin/tenants/${btn.dataset.resend}/resend-invite`, { method: "POST" });
        alert("New invite link copied below - share it with the tenant:\n" + result.link);
        try {
          await copyToClipboard(result.link, btn);
        } catch (e) {}
      };
    });
  }

  refresh();
}

async function renderAdminUnits(main) {
  main.innerHTML = `
    <div class="topbar">
      <div><h1 class="page-title">Units &amp; Meters</h1><div class="page-sub">${publicConfig.building_name || ""}</div></div>
      <div style="display:flex;gap:10px;">
        <button class="btn-ghost" id="add-unit-btn">+ Add Unit</button>
        <button class="btn" id="add-meter-btn">+ Add Meter</button>
      </div>
    </div>
    <div id="form-panel"></div>
    <div class="card" id="unassigned-card"></div>
    <div class="card" id="units-card">Loading...</div>
    <div id="detail-panel"></div>
  `;

  async function refresh() {
    const [units, unassigned] = await Promise.all([api("/admin/units"), api("/admin/meters/unassigned")]);
    renderUnassigned(unassigned, units);
    renderUnitsList(units);
  }

  function renderUnassigned(unassigned, units) {
    const card = document.getElementById("unassigned-card");
    if (unassigned.length === 0) {
      card.innerHTML = `<p style="color:var(--text-muted);font-size:13px;">No unassigned meters.</p>`;
      return;
    }
    card.innerHTML = `<h2 class="page-title" style="font-size:15px;">Unassigned Meters (${unassigned.length})</h2>` +
      unassigned
        .map(
          (m) => `
        <div class="list-row">
          <div><div class="name mono">${m.dev_eui}</div><div class="meta">${m.chirpstack_device_name || ""}</div></div>
          <div style="display:flex;gap:8px;align-items:center;">
            <select id="assign-${m.id}" style="width:auto;margin:0;">
              <option value="">Assign to unit...</option>
              ${units.map((u) => `<option value="${u.unit_id}">${u.unit_number}</option>`).join("")}
            </select>
            <button class="btn btn-small" data-assign="${m.id}">Assign</button>
          </div>
        </div>`
        )
        .join("");
    card.querySelectorAll("[data-assign]").forEach((btn) => {
      btn.onclick = async () => {
        const sel = document.getElementById(`assign-${btn.dataset.assign}`);
        if (!sel.value) return alert("Pick a unit first");
        await api(`/admin/meters/${btn.dataset.assign}/assign`, {
          method: "PUT",
          body: JSON.stringify({ unit_id: sel.value }),
        });
        refresh();
      };
    });
  }

  function renderUnitsList(units) {
    const card = document.getElementById("units-card");
    if (units.length === 0) {
      card.innerHTML = `<p style="color:var(--text-muted);font-size:13px;">No units yet — add one above.</p>`;
      return;
    }
    card.innerHTML = units
      .map(
        (u) => `
      <div class="list-row">
        <div>
          <div class="name">${u.unit_number}</div>
          <div class="meta">${u.tenant_names || "No tenant"} · ${u.dev_eui ? `<span class="mono">${u.dev_eui}</span>` : "No meter"}</div>
        </div>
        <div style="display:flex;align-items:center;gap:10px;">
          <span class="badge ${u.billing_mode === "prepaid" ? "warn" : "ok"}">
            <span class="badge-dot"></span>${u.billing_mode || "monthly"}
          </span>
          <span class="mono meta">${u.last_reading_m3 != null ? u.last_reading_m3.toFixed(3) + " m³" : "—"}</span>
          <button class="btn-ghost btn-small" data-manage="${u.unit_id}">Manage</button>
        </div>
      </div>`
      )
      .join("");
    card.querySelectorAll("[data-manage]").forEach((btn) => {
      btn.onclick = () => renderUnitDetail(units.find((u) => u.unit_id === btn.dataset.manage));
    });
  }

  async function renderUnitDetail(u) {
    const panel = document.getElementById("detail-panel");
    const billing = await api(`/admin/units/${u.unit_id}/billing`);
    panel.innerHTML = `
      <div class="card">
        <div style="display:flex;justify-content:space-between;align-items:flex-start;">
          <h2 class="page-title" style="font-size:16px;">Manage ${u.unit_number}</h2>
          <button class="link" id="edit-unit-toggle">Rename</button>
        </div>
        <div id="edit-unit-form" class="hidden">
          <label>Unit Number</label>
          <input id="edit-unit-number" value="${u.unit_number}" />
          <div class="row" style="display:flex;gap:8px;">
            <button class="btn btn-small" id="save-unit-rename">Save</button>
            <button class="btn-ghost btn-small" id="cancel-unit-rename">Cancel</button>
          </div>
        </div>

        <div style="margin:14px 0;">
          <label>Meter</label>
          ${u.dev_eui
            ? `<p class="mono meta" style="margin-bottom:8px;">${u.dev_eui}</p>
               <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px;">
                 <span class="badge ${u.battery_voltage == null ? "" : u.battery_low ? "warn" : "ok"}">
                   <span class="badge-dot"></span>Battery: ${
                     u.battery_voltage == null ? "Not available" : (u.battery_low ? "Low" : "Good") + ` (${u.battery_voltage.toFixed(2)}V)`
                   }
                 </span>
                 <span class="badge ${u.valve_confirmed_state === "open" ? "ok" : u.valve_confirmed_state === "closed" ? "alert" : ""}">
                   <span class="badge-dot"></span>Valve: ${
                     u.valve_confirmed_state === "open" ? "Open" : u.valve_confirmed_state === "closed" ? "Closed" : "Not available"
                   }
                 </span>
                 <span class="badge">
                   <span class="badge-dot"></span>Last seen: ${u.last_reading_at ? new Date(u.last_reading_at).toLocaleString() : "Never"}
                 </span>
               </div>
               <button class="btn-danger" id="remove-meter-btn">Remove Meter</button>`
            : `<p style="font-size:13px;color:var(--text-muted);">No meter assigned — use "+ Add Meter" above.</p>`}
        </div>

        <div style="margin:14px 0;">
          <label>Billing Mode</label>
          <div class="row" style="display:flex;gap:8px;">
            <button class="btn-small ${billing.mode === "monthly" ? "btn" : "btn-ghost"}" id="mode-monthly">Monthly</button>
            <button class="btn-small ${billing.mode === "prepaid" ? "btn" : "btn-ghost"}" id="mode-prepaid">Prepaid</button>
          </div>
        </div>

        <div id="mode-specific"></div>

        <div style="margin-top:16px;border-top:1px solid var(--border);padding-top:14px;">
          <div style="display:flex;justify-content:space-between;align-items:center;">
            <label style="margin:0;">Tenants</label>
            <button class="link" id="invite-tenant-btn">+ Invite Tenant</button>
          </div>
          <div id="tenants-list" style="margin-top:8px;">Loading...</div>
        </div>

        <div style="margin-top:16px;border-top:1px solid var(--border);padding-top:14px;">
          <label>Reports</label>
          <div id="report-period"></div>
        </div>

        <div id="action-result" style="margin-top:12px;"></div>

        <div style="margin-top:20px;border-top:1px solid var(--border);padding-top:14px;">
          <button class="btn-danger" id="delete-unit-btn">Delete Unit</button>
          <p class="meta" id="delete-unit-hint" style="margin-top:6px;"></p>
        </div>
      </div>
    `;

    let currentTenants = [];

    async function loadTenants() {
      currentTenants = await api(`/admin/units/${u.unit_id}/tenants`);
      const list = document.getElementById("tenants-list");
      const active = currentTenants.filter((t) => t.active);
      document.getElementById("delete-unit-hint").textContent =
        (u.dev_eui ? "Remove the meter" : "") + (u.dev_eui && active.length ? " and " : "") +
        (active.length ? "deactivate all tenants" : "") +
        (u.dev_eui || active.length ? " first to enable deletion." : "");

      if (currentTenants.length === 0) {
        list.innerHTML = `<p style="font-size:13px;color:var(--text-muted);">No tenant yet.</p>`;
        return;
      }
      list.innerHTML = currentTenants
        .map(
          (t) => `
        <div class="list-row" data-tenant-row="${t.id}">
          <div>
            <div class="name">${t.name}${!t.active ? " (inactive)" : ""}</div>
            <div class="meta">${t.phone || t.email || "—"}</div>
          </div>
          <div style="display:flex;gap:6px;flex-wrap:wrap;justify-content:flex-end;">
            <span class="badge ${t.activated ? "ok" : "warn"}"><span class="badge-dot"></span>${t.activated ? "Active" : "Invite Pending"}</span>
            ${t.active ? `
              <button class="btn-ghost btn-small" data-edit-tenant="${t.id}">Edit</button>
              ${!t.activated ? `<button class="btn-ghost btn-small" data-resend="${t.id}">Resend Invite</button>` : ""}
              <button class="btn-danger btn-small" data-remove-tenant="${t.id}">Remove</button>
            ` : `<button class="btn-danger btn-small" data-delete-tenant="${t.id}">Delete Permanently</button>`}
          </div>
        </div>
        <div id="edit-tenant-form-${t.id}" class="hidden" style="padding:10px 0;"></div>
      `
        )
        .join("");

      list.querySelectorAll("[data-edit-tenant]").forEach((btn) => {
        btn.onclick = () => {
          const t = currentTenants.find((x) => x.id === btn.dataset.editTenant);
          const form = document.getElementById(`edit-tenant-form-${t.id}`);
          form.classList.toggle("hidden");
          if (form.classList.contains("hidden")) return;
          form.innerHTML = `
            <label>Name</label><input id="et-name-${t.id}" value="${t.name}" />
            <label>Phone</label><input id="et-phone-${t.id}" value="${t.phone || ""}" />
            <button class="btn btn-small" id="et-save-${t.id}">Save</button>
          `;
          document.getElementById(`et-save-${t.id}`).onclick = async () => {
            await api(`/admin/tenants/${t.id}`, {
              method: "PUT",
              body: JSON.stringify({
                name: document.getElementById(`et-name-${t.id}`).value,
                phone: document.getElementById(`et-phone-${t.id}`).value,
              }),
            });
            loadTenants();
            refresh();
          };
        };
      });
      list.querySelectorAll("[data-remove-tenant]").forEach((btn) => {
        btn.onclick = async () => {
          if (!confirm("Remove this tenant's access? This frees the unit for a new tenant.")) return;
          await api(`/admin/tenants/${btn.dataset.removeTenant}/deactivate`, { method: "PUT" });
          loadTenants();
          refresh();
        };
      });
      list.querySelectorAll("[data-delete-tenant]").forEach((btn) => {
        btn.onclick = async () => {
          if (!confirm("Permanently delete this tenant account? This cannot be undone. Billing and reading history for the unit itself is kept - only the account is removed.")) return;
          await api(`/admin/tenants/${btn.dataset.deleteTenant}`, { method: "DELETE" });
          loadTenants();
          refresh();
        };
      });
      list.querySelectorAll("[data-resend]").forEach((btn) => {
        btn.onclick = async () => {
          const result = await api(`/admin/tenants/${btn.dataset.resend}/resend-invite`, { method: "POST" });
          document.getElementById("action-result").innerHTML = `
            <div class="link-display">${result.link}</div>
            ${qrImg(result.link)}
            <button class="btn-ghost" id="copy-resend-link" style="width:100%;">Copy Link</button>
          `;
          document.getElementById("copy-resend-link").onclick = (e) => copyToClipboard(result.link, e.target);
        };
      });
    }

    document.getElementById("invite-tenant-btn").onclick = () => {
      document.getElementById("action-result").innerHTML = `
        <label>Tenant Name</label><input id="t-name" />
        <label>Phone</label><input id="t-phone" />
        <button class="btn btn-small" id="t-invite-submit">Generate Invite</button>
      `;
      document.getElementById("t-invite-submit").onclick = async () => {
        const result = await api("/admin/tenants/invite", {
          method: "POST",
          body: JSON.stringify({
            unit_id: u.unit_id,
            name: document.getElementById("t-name").value,
            phone: document.getElementById("t-phone").value,
          }),
        });
        document.getElementById("action-result").innerHTML = `
          <div class="link-display">${result.link}</div>
          ${qrImg(result.link)}
          <button class="btn-ghost" id="copy-tenant-link" style="width:100%;">Copy Link</button>
        `;
        document.getElementById("copy-tenant-link").onclick = (e) => copyToClipboard(result.link, e.target);
        loadTenants();
      };
    };

    loadTenants();

    document.getElementById("edit-unit-toggle").onclick = () => {
      document.getElementById("edit-unit-form").classList.toggle("hidden");
    };
    document.getElementById("cancel-unit-rename").onclick = () => {
      document.getElementById("edit-unit-form").classList.add("hidden");
    };
    document.getElementById("save-unit-rename").onclick = async () => {
      const newNumber = document.getElementById("edit-unit-number").value.trim();
      if (!newNumber) return;
      try {
        await api(`/admin/units/${u.unit_id}`, { method: "PUT", body: JSON.stringify({ unit_number: newNumber }) });
        refresh();
      } catch (e) {
        alert(e.message);
      }
    };

    const removeMeterBtn = document.getElementById("remove-meter-btn");
    if (removeMeterBtn) {
      removeMeterBtn.onclick = () => {
        confirmWithTyping({
          title: "Remove Meter",
          message: `This deletes the meter from ChirpStack too and unassigns it from ${u.unit_number}. This cannot be undone.`,
          expectedText: u.dev_eui,
          confirmLabel: "Remove Meter",
          onConfirm: async () => {
            await api(`/admin/meters/${u.meter_id}`, { method: "DELETE" });
            document.getElementById("detail-panel").innerHTML = "";
            refresh();
          },
        });
      };
    }

    document.getElementById("delete-unit-btn").onclick = () => {
      confirmWithTyping({
        title: "Delete Unit",
        message: `This permanently deletes unit ${u.unit_number}. This cannot be undone.`,
        expectedText: u.unit_number,
        confirmLabel: "Delete Unit",
        onConfirm: async () => {
          await api(`/admin/units/${u.unit_id}`, { method: "DELETE" });
          document.getElementById("detail-panel").innerHTML = "";
          refresh();
        },
      });
    };

    function renderModeSpecific() {
      const box = document.getElementById("mode-specific");
      if (billing.mode === "prepaid") {
        box.innerHTML = `
          <div class="meta" style="margin-bottom:8px;">
            Balance: <b>${(billing.balance_liters_remaining ?? 0).toFixed(1)} L</b> ·
            Valve: <span class="badge ${billing.valve_target_state === "open" ? "ok" : "alert"}">${billing.valve_target_state}</span>
          </div>
          <label>Record Top-up (R)</label>
          <div class="row" style="display:flex;gap:8px;">
            <input id="topup-amount" type="number" placeholder="e.g. 100" style="margin:0;" />
            <button class="btn btn-small" id="topup-btn">Add</button>
          </div>
        `;
        document.getElementById("topup-btn").onclick = async () => {
          const amt = parseFloat(document.getElementById("topup-amount").value);
          if (!amt) return;
          const resultBox = document.getElementById("action-result");
          const btn = document.getElementById("topup-btn");
          btn.disabled = true;
          try {
            const result = await api(`/admin/units/${u.unit_id}/topup`, {
              method: "POST", body: JSON.stringify({ amount_rand: amt }),
            });
            resultBox.innerHTML =
              `<p style="color:var(--accent-mint);font-size:13px;">` +
              `R${amt.toFixed(2)} added &rarr; <b>${result.liters_purchased.toFixed(1)} L</b> at R${result.tariff_used.toFixed(2)}/kL. ` +
              `New balance: <b>${result.balance_liters_remaining.toFixed(1)} L</b>.</p>`;
            document.getElementById("topup-amount").value = "";
            refresh();
          } catch (e) {
            // A silent failure here (e.g. this unit's meter has never sent a
            // reading yet, so there's no baseline to calculate a target
            // from) used to look identical to a successful top-up with
            // nothing happening - always show the actual reason.
            resultBox.innerHTML = `<p class="error" style="font-size:13px;">${e.message}</p>`;
          }
          btn.disabled = false;
        };
      } else {
        box.innerHTML = `
          <div class="meta" style="margin-bottom:8px;">
            Valve: <span class="badge ${billing.valve_target_state === "open" ? "ok" : "alert"}">${billing.valve_target_state}</span>
          </div>
          <div class="row" style="display:flex;gap:8px;">
            <button class="btn-ghost btn-small" id="valve-close">Manual Cutoff</button>
            <button class="btn-ghost btn-small" id="valve-open">Reopen Valve</button>
          </div>
        `;
        document.getElementById("valve-close").onclick = () => sendValve(false);
        document.getElementById("valve-open").onclick = () => sendValve(true);
      }
    }

    async function sendValve(open) {
      await api(`/admin/units/${u.unit_id}/valve`, { method: "POST", body: JSON.stringify({ open }) });
      document.getElementById("action-result").innerHTML =
        `<p style="color:var(--accent-amber);font-size:13px;">Valve command queued — delivers on the meter's next check-in (Class A device, not instant).</p>`;
    }

    renderModeSpecific();

    document.getElementById("mode-monthly").onclick = async () => {
      const r = await api(`/admin/units/${u.unit_id}/billing/mode`, { method: "PUT", body: JSON.stringify({ mode: "monthly" }) });
      document.getElementById("action-result").innerHTML = `<p style="font-size:13px;color:var(--text-muted);">${r.note || "Switched to monthly."}</p>`;
      renderUnitDetail({ ...u });
      refresh();
    };
    document.getElementById("mode-prepaid").onclick = async () => {
      const r = await api(`/admin/units/${u.unit_id}/billing/mode`, { method: "PUT", body: JSON.stringify({ mode: "prepaid" }) });
      document.getElementById("action-result").innerHTML = `<p style="font-size:13px;color:var(--text-muted);">${r.note || "Switched to prepaid."}</p>`;
      renderUnitDetail({ ...u });
      refresh();
    };

    const reportBox = document.getElementById("report-period");
    reportBox.appendChild(
      periodPicker((period, start, end) => {
        let url = `/admin/units/${u.unit_id}/report.pdf?period=${period}`;
        if (period === "custom") url += `&start=${encodeURIComponent(start)}&end=${encodeURIComponent(end)}`;
        downloadPdf(url, `${u.unit_number}_${period}.pdf`);
      })
    );
  }

  document.getElementById("add-unit-btn").onclick = () => {
    document.getElementById("form-panel").innerHTML = `
      <div class="card">
        <label>Unit Number</label><input id="new-unit-number" placeholder="e.g. A204" />
        <button class="btn" id="submit-unit">Create Unit</button>
      </div>`;
    document.getElementById("submit-unit").onclick = async () => {
      const num = document.getElementById("new-unit-number").value.trim();
      if (!num) return;
      await api("/admin/units", { method: "POST", body: JSON.stringify({ unit_number: num }) });
      document.getElementById("form-panel").innerHTML = "";
      refresh();
    };
  };

  document.getElementById("add-meter-btn").onclick = async () => {
    const units = await api("/admin/units");
    document.getElementById("form-panel").innerHTML = `
      <div class="card">
        <label>DevEUI</label><input id="new-dev-eui" placeholder="From the meter's label" />
        <label>AppKey</label><input id="new-app-key" placeholder="From the meter's label" />
        <label>Assign to Unit (optional)</label>
        <select id="new-meter-unit"><option value="">Leave unassigned</option>
          ${units.map((u) => `<option value="${u.unit_id}">${u.unit_number}</option>`).join("")}
        </select>
        <button class="btn" id="submit-meter">Register Meter</button>
        <div id="meter-add-error" class="error hidden"></div>
      </div>`;
    document.getElementById("submit-meter").onclick = async () => {
      try {
        await api("/admin/meters", {
          method: "POST",
          body: JSON.stringify({
            dev_eui: document.getElementById("new-dev-eui").value.replace(/[:\s]/g, ""),
            app_key: document.getElementById("new-app-key").value.replace(/[:\s]/g, ""),
            unit_id: document.getElementById("new-meter-unit").value || null,
          }),
        });
        document.getElementById("form-panel").innerHTML = "";
        refresh();
      } catch (e) {
        const err = document.getElementById("meter-add-error");
        err.textContent = e.message;
        err.classList.remove("hidden");
      }
    };
  };

  refresh();
}

// ---------------- Tenant dashboard ----------------

async function renderTenant() {
  const main = renderShell("tenant", publicConfig.tenant_app_name, [{ key: "dashboard", label: "My Usage" }], 0);
  const unit = await api("/tenant/me/unit");

  main.innerHTML = `
    <div class="topbar">
      <div><h1 class="page-title">Unit ${unit.unit_number}</h1><div class="page-sub">${publicConfig.building_name || ""}</div></div>
    </div>
    <div class="stat-grid">
      <div class="stat-card">
        <div class="stat-icon mint">◇</div>
        <div><div class="stat-number">${unit.last_reading_m3 != null ? unit.last_reading_m3.toFixed(2) : "—"}</div><div class="stat-label">Current reading (m³)</div></div>
      </div>
      <div class="stat-card">
        <div class="stat-icon ${unit.mode === "prepaid" ? "amber" : "neutral"}">◔</div>
        <div><div class="stat-number">${
          unit.mode === "prepaid"
            ? (unit.balance_liters_remaining ?? 0).toFixed(0) + " L"
            : unit.used_this_cycle_liters != null
              ? unit.used_this_cycle_liters.toFixed(0) + " L"
              : "—"
        }</div>
        <div class="stat-label">${unit.mode === "prepaid" ? "Balance remaining" : "Used this cycle"}</div></div>
      </div>
    </div>
    <div class="card">
      <h2 class="page-title" style="font-size:15px;">Download Report</h2>
      <div id="report-period"></div>
    </div>
    <div class="card">
      <h2 class="page-title" style="font-size:15px;">Statements</h2>
      <div id="statements-list">Loading...</div>
    </div>
  `;

  document.getElementById("report-period").appendChild(
    periodPicker((period, start, end) => {
      let url = `/tenant/me/report.pdf?period=${period}`;
      if (period === "custom") url += `&start=${encodeURIComponent(start)}&end=${encodeURIComponent(end)}`;
      downloadPdf(url, `my_usage_${period}.pdf`);
    })
  );

  const statements = await api("/tenant/me/statements");
  const list = document.getElementById("statements-list");
  if (statements.length === 0) {
    list.innerHTML = `<p style="color:var(--text-muted);font-size:13px;">No statements yet.</p>`;
  } else {
    list.innerHTML = statements
      .map(
        (s) => `
      <div class="list-row">
        <div><div class="name">${s.period_start?.slice(0, 10)} – ${s.period_end?.slice(0, 10)}</div><div class="meta">${s.consumption_m3?.toFixed(3)} m³</div></div>
        <button class="btn-ghost btn-small" data-dl="${s.id}">R ${s.amount_due_rand?.toFixed(2)} · PDF</button>
      </div>`
      )
      .join("");
    list.querySelectorAll("[data-dl]").forEach((btn) => {
      btn.onclick = () => downloadPdf(`/tenant/me/statements/${btn.dataset.dl}/pdf`, `statement.pdf`);
    });
  }

  const pwCard = document.createElement("div");
  pwCard.className = "card";
  pwCard.innerHTML = `
    <h2 class="page-title" style="font-size:15px;">Change My Password</h2>
    <label>Current Password</label><input id="cp-current" type="password" />
    <label>New Password</label><input id="cp-new" type="password" />
    <button class="btn" id="cp-submit">Change Password</button>
    <div id="cp-result" style="margin-top:8px;font-size:13px;"></div>
  `;
  main.appendChild(pwCard);
  document.getElementById("cp-submit").onclick = async () => {
    const resultBox = document.getElementById("cp-result");
    try {
      const result = await api("/auth/change-password", {
        method: "POST",
        body: JSON.stringify({
          current_password: document.getElementById("cp-current").value,
          new_password: document.getElementById("cp-new").value,
        }),
      });
      localStorage.setItem("wf_token", result.token);
      resultBox.style.color = "var(--accent-mint)";
      resultBox.textContent = "Password changed.";
      document.getElementById("cp-current").value = "";
      document.getElementById("cp-new").value = "";
    } catch (err) {
      resultBox.style.color = "var(--accent-red)";
      resultBox.textContent = err.message;
    }
  };

  renderBiometricCard(main);
}

// ---------------- Router ----------------

async function route() {
  const segments = window.location.pathname.split("/").filter(Boolean);

  if (segments[0] === "invite" && segments[1]) return renderAcceptToken("invite", segments[1]);
  if (segments[0] === "reset" && segments[1]) return renderAcceptToken("reset", segments[1]);

  let expectedRole = "admin";
  if (segments[0] === "super") expectedRole = "super_admin";
  else if (segments[0] === "tenant") expectedRole = "tenant";

  const appName =
    expectedRole === "super_admin" ? publicConfig.super_admin_app_name :
    expectedRole === "tenant" ? publicConfig.tenant_app_name : publicConfig.admin_app_name;

  const token = localStorage.getItem("wf_token");
  const role = localStorage.getItem("wf_role");
  if (!token || role !== expectedRole) return renderLogin(expectedRole, appName);

  try {
    await api("/auth/me"); // validates session is still live
  } catch (e) {
    localStorage.removeItem("wf_token");
    localStorage.removeItem("wf_role");
    return renderLogin(expectedRole, appName);
  }

  if (expectedRole === "super_admin") renderSuperAdmin();
  else if (expectedRole === "tenant") renderTenant();
  else renderAdmin();
}

api("/public-config")
  .then((cfg) => {
    publicConfig = cfg;
    document.title = publicConfig.product_name;
    route();
  })
  .catch(() => route());
