const API = "/api";
const app = document.getElementById("app");
let publicConfig = { product_name: "WaterFlow", building_name: "", link_base: "" };

if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("/sw.js").catch(() => {});
}

function authHeader() {
  const t = localStorage.getItem("wf_token");
  return t ? { Authorization: "Bearer " + t } : {};
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

  document.getElementById("login-btn").onclick = async () => {
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
        const target = result.role === "super_admin" ? "/super" : "/admin";
        window.location.href = target;
        return;
      }
      route();
    } catch (e) {
      errBox.textContent = "Invalid phone/email or password.";
      errBox.classList.remove("hidden");
    }
  };

  document.getElementById("forgot-btn").onclick = () => {
    document.getElementById("forgot-panel").classList.remove("hidden");
  };
  document.getElementById("forgot-submit").onclick = async () => {
    await api("/auth/reset-request", {
      method: "POST",
      body: JSON.stringify({ identifier: document.getElementById("forgot-id").value }),
    });
    document.getElementById("forgot-msg").classList.remove("hidden");
  };
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

      document.getElementById("accept-btn").onclick = async () => {
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
          window.location.href = result.role === "super_admin" ? "/super" : "/admin";
        } catch (e) {
          errBox.textContent = e.message;
          errBox.classList.remove("hidden");
        }
      };
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
  shell.querySelector('[data-nav="logout"]').onclick = () => {
    localStorage.removeItem("wf_token");
    localStorage.removeItem("wf_role");
    window.location.href = role === "super_admin" ? "/super" : "/admin";
  };
  return document.getElementById("main-content");
}

// ---------------- Super Admin dashboard ----------------

async function renderSuperAdmin() {
  const main = renderShell(
    "super_admin",
    publicConfig.super_admin_app_name,
    [
      { key: "dashboard", label: "Dashboard" },
      { key: "meters", label: "Meters (Phase 2)" },
      { key: "billing", label: "Billing (Phase 3)" },
    ],
    0
  );

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
          ${a.active ? `<button class="btn-danger" data-deactivate="${a.id}">Deactivate</button>` : ""}
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
    document.getElementById("copy-link").onclick = () => navigator.clipboard?.writeText(link);
  }

  loadAdmins();
  loadResets();
}

// ---------------- Admin dashboard ----------------

async function renderAdmin() {
  const main = renderShell(
    "admin",
    publicConfig.admin_app_name,
    [
      { key: "dashboard", label: "Dashboard" },
      { key: "units", label: "Units (Phase 2)" },
      { key: "billing", label: "Billing (Phase 3)" },
      { key: "reports", label: "Reports (Phase 3)" },
    ],
    0
  );

  main.innerHTML = `
    <div class="topbar">
      <div><h1 class="page-title">Dashboard</h1><div class="page-sub">${publicConfig.building_name || ""}</div></div>
    </div>
    <div class="card">
      <p style="color:var(--text-muted);font-size:13.5px;">
        Meter and tenant management arrives in Phase 2. For now: password reset
        requests from tenants will appear here once tenants exist.
      </p>
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
        const box = document.getElementById("reset-result");
        box.innerHTML = `
          <div class="card">
            <h2 class="page-title" style="font-size:16px;">Reset link for ${result.user_name}</h2>
            <div class="link-display">${result.link}</div>
            ${qrImg(result.link)}
          </div>`;
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

// ---------------- Router ----------------

async function route() {
  const segments = window.location.pathname.split("/").filter(Boolean);

  if (segments[0] === "invite" && segments[1]) return renderAcceptToken("invite", segments[1]);
  if (segments[0] === "reset" && segments[1]) return renderAcceptToken("reset", segments[1]);

  const expectedRole = segments[0] === "super" ? "super_admin" : "admin";
  const appName = expectedRole === "super_admin" ? publicConfig.super_admin_app_name : publicConfig.admin_app_name;

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
  else renderAdmin();
}

api("/public-config")
  .then((cfg) => {
    publicConfig = cfg;
    document.title = publicConfig.product_name;
    route();
  })
  .catch(() => route());
