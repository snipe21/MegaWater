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

function periodPicker(onPick) {
  const wrap = el(`
    <div class="row" style="display:flex;gap:8px;margin-bottom:10px;">
      <button class="btn-ghost btn-small" data-p="daily">Today</button>
      <button class="btn-ghost btn-small" data-p="weekly">Week</button>
      <button class="btn-ghost btn-small" data-p="monthly">Month</button>
    </div>
  `);
  wrap.querySelectorAll("button").forEach((b) => {
    b.onclick = () => onPick(b.dataset.p);
  });
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
        const target = result.role === "super_admin" ? "/super" : "/admin";
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
          window.location.href = result.role === "super_admin" ? "/super" : "/admin";
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
      { key: "units", label: "Units & Meters" },
    ],
    window.location.hash === "#units" ? 1 : 0
  );

  main.parentElement.querySelector('[data-nav="units"]').onclick = () => {
    window.location.hash = "#units";
    renderSuperAdmin();
  };
  main.parentElement.querySelector('[data-nav="dashboard"]').onclick = () => {
    window.location.hash = "";
    renderSuperAdmin();
  };

  if (window.location.hash === "#units") return renderAdminUnits(main);

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
    document.getElementById("copy-link").onclick = (e) => copyToClipboard(link, e.target);
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
      { key: "units", label: "Units & Meters" },
    ],
    window.location.hash === "#units" ? 1 : 0
  );

  main.parentElement.querySelector('[data-nav="units"]').onclick = () => {
    window.location.hash = "#units";
    renderAdmin();
  };
  main.parentElement.querySelector('[data-nav="dashboard"]').onclick = () => {
    window.location.hash = "";
    renderAdmin();
  };

  if (window.location.hash === "#units") return renderAdminUnits(main);

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
          <div class="meta">${u.tenant_name || "No tenant"} · ${u.dev_eui ? `<span class="mono">${u.dev_eui}</span>` : "No meter"}</div>
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
          <label>Tenant</label>
          ${u.tenant_name ? `<p style="font-size:13.5px;">${u.tenant_name}</p>` : `<button class="btn-ghost btn-small" id="invite-tenant-btn">Invite Tenant</button>`}
        </div>

        <div style="margin-top:16px;border-top:1px solid var(--border);padding-top:14px;">
          <label>Reports</label>
          <div id="report-period"></div>
        </div>

        <div id="action-result" style="margin-top:12px;"></div>

        <div style="margin-top:20px;border-top:1px solid var(--border);padding-top:14px;">
          <button class="btn-danger" id="delete-unit-btn">Delete Unit</button>
          ${u.dev_eui || u.tenant_name ? `<p class="meta" style="margin-top:6px;">Remove the meter${u.tenant_name ? " and tenant" : ""} first to enable deletion.</p>` : ""}
        </div>
      </div>
    `;

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
      removeMeterBtn.onclick = async () => {
        if (!confirm(`Remove the meter from ${u.unit_number}? This deletes it from ChirpStack too.`)) return;
        await api(`/admin/meters/${u.meter_id}`, { method: "DELETE" });
        document.getElementById("detail-panel").innerHTML = "";
        refresh();
      };
    }

    document.getElementById("delete-unit-btn").onclick = async () => {
      if (!confirm(`Delete unit ${u.unit_number}? This cannot be undone.`)) return;
      try {
        await api(`/admin/units/${u.unit_id}`, { method: "DELETE" });
        document.getElementById("detail-panel").innerHTML = "";
        refresh();
      } catch (e) {
        alert(e.message);
      }
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
          const result = await api(`/admin/units/${u.unit_id}/topup`, {
            method: "POST", body: JSON.stringify({ amount_rand: amt }),
          });
          document.getElementById("action-result").innerHTML =
            `<p style="color:var(--accent-mint);font-size:13px;">Top-up recorded. New target reading: ${result.target_reading_m3} m³</p>`;
          refresh();
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

    const inviteBtn = document.getElementById("invite-tenant-btn");
    if (inviteBtn) {
      inviteBtn.onclick = () => {
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
        };
      };
    }

    const reportBox = document.getElementById("report-period");
    reportBox.appendChild(
      periodPicker((period) => downloadPdf(`/admin/units/${u.unit_id}/report.pdf?period=${period}`, `${u.unit_number}_${period}.pdf`))
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
        <div><div class="stat-number">${unit.mode === "prepaid" ? (unit.balance_liters_remaining ?? 0).toFixed(0) + "L" : unit.mode}</div>
        <div class="stat-label">${unit.mode === "prepaid" ? "Balance remaining" : "Billing mode"}</div></div>
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
    periodPicker((period) => downloadPdf(`/tenant/me/report.pdf?period=${period}`, `my_usage_${period}.pdf`))
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
