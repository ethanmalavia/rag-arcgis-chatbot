// Administrator console: status, corpus upload, report inbox.
const API_BASE = (typeof window !== "undefined" && window.API_BASE)
  ? window.API_BASE.replace(/\/$/, "")
  : (window.location.port === "8000" || window.location.hostname === "localhost"
      ? "http://localhost:8000"
      : window.location.origin);

const TOKEN_KEY = "ee_admin_token";

function getToken() {
  return sessionStorage.getItem(TOKEN_KEY) || "";
}

function setToken(token) {
  if (token) sessionStorage.setItem(TOKEN_KEY, token);
  else sessionStorage.removeItem(TOKEN_KEY);
}

function authHeaders(extra) {
  const headers = Object.assign({}, extra || {});
  const token = getToken();
  if (token) headers.Authorization = `Bearer ${token}`;
  return headers;
}

function setStatus(el, text, ok) {
  if (!el) return;
  el.textContent = text || "";
  el.classList.remove("ok", "err");
  if (ok === true) el.classList.add("ok");
  if (ok === false) el.classList.add("err");
}

function esc(s) {
  return String(s ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function kindLabel(kind) {
  if (kind === "incorrect_location") return "Incorrect location";
  if (kind === "suggest_change") return "Suggest a change";
  return "Other";
}

function showAuthed(authed) {
  document.getElementById("login-panel").hidden = authed;
  document.getElementById("admin-app").hidden = !authed;
  document.getElementById("logout-btn").hidden = !authed;
}

async function adminFetch(path, options) {
  const opts = Object.assign({ headers: {} }, options || {});
  opts.headers = authHeaders(opts.headers);
  const res = await fetch(`${API_BASE}${path}`, opts);
  let data = null;
  const ct = res.headers.get("content-type") || "";
  if (ct.includes("application/json")) {
    data = await res.json().catch(() => null);
  } else {
    data = { detail: await res.text() };
  }
  if (!res.ok) {
    const detail = (data && (data.detail || data.message)) || `HTTP ${res.status}`;
    const err = new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
    err.status = res.status;
    throw err;
  }
  return data;
}

async function refreshStatus() {
  const grid = document.getElementById("status-grid");
  const ops = document.getElementById("ops-status");
  try {
    const data = await adminFetch("/admin/status");
    const reports = data.reports || {};
    grid.innerHTML = `
      <dt>Index ready</dt><dd>${data.chain_ready ? "Yes" : "No"}</dd>
      <dt>Records</dt><dd>${esc(data.record_count)}</dd>
      <dt>Chunks</dt><dd>${esc(data.chunk_count)}</dd>
      <dt>Embedding</dt><dd>${esc(data.embedding_model)}</dd>
      <dt>Reranker</dt><dd>${esc(data.reranker_model)}</dd>
      <dt>Open reports</dt><dd>${esc(reports.open ?? 0)} / ${esc(reports.total ?? 0)}</dd>
      <dt>CSV path</dt><dd>${esc(data.csv_path)}</dd>
    `;
    setStatus(ops, "Status updated", true);
  } catch (e) {
    setStatus(ops, e.message || "Status failed", false);
    if (e.status === 401 || e.status === 503) {
      setToken("");
      showAuthed(false);
    }
  }
}

async function runWarmup() {
  const ops = document.getElementById("ops-status");
  const btn = document.getElementById("warmup-btn");
  btn.disabled = true;
  setStatus(ops, "Warming…");
  try {
    const data = await fetch(`${API_BASE}/warmup`).then(async (res) => {
      const body = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(body.detail || `HTTP ${res.status}`);
      return body;
    });
    setStatus(ops, `Warm · retrieve=${data.retrieve} llm=${data.llm}`, true);
  } catch (e) {
    setStatus(ops, e.message || "Warmup failed", false);
  } finally {
    btn.disabled = false;
  }
}

async function loadCSV() {
  const fileInput = document.getElementById("csv-file-input");
  const statusEl = document.getElementById("load-status");
  const btn = document.getElementById("load-btn");
  const file = fileInput.files && fileInput.files[0];
  if (!file) {
    setStatus(statusEl, "Choose a CSV file first", false);
    return;
  }
  btn.disabled = true;
  setStatus(statusEl, "Uploading & rebuilding…");
  const formData = new FormData();
  formData.append("file", file);
  try {
    const data = await adminFetch("/load", { method: "POST", body: formData });
    setStatus(statusEl, data.message || "Loaded", true);
    await refreshStatus();
  } catch (e) {
    setStatus(statusEl, e.message || "Upload failed", false);
  } finally {
    btn.disabled = false;
  }
}

async function refreshReports() {
  const list = document.getElementById("reports-list");
  const filter = document.getElementById("report-filter").value;
  const q = filter ? `?status=${encodeURIComponent(filter)}` : "";
  try {
    const rows = await adminFetch(`/admin/reports${q}`);
    if (!rows.length) {
      list.innerHTML = `<div class="empty-reports">No reports in this filter.</div>`;
      return;
    }
    list.innerHTML = rows.map((r) => `
      <article class="report-item ${esc(r.status)}" data-id="${esc(r.id)}">
        <div class="report-kind">${esc(kindLabel(r.kind))}</div>
        <div class="report-meta">${esc(r.created_at)} · ${esc(r.status)} · ${esc(r.id.slice(0, 8))}</div>
        <div class="report-details">${esc(r.details)}</div>
        <div class="report-fields">
          ${r.application_id ? `<div><strong>Application ID:</strong> ${esc(r.application_id)}</div>` : ""}
          ${r.location ? `<div><strong>Location:</strong> ${esc(r.location)}</div>` : ""}
          ${r.current_value ? `<div><strong>Current:</strong> ${esc(r.current_value)}</div>` : ""}
          ${r.suggested_value ? `<div><strong>Suggested:</strong> ${esc(r.suggested_value)}</div>` : ""}
          ${r.contact_email ? `<div><strong>Contact:</strong> ${esc(r.contact_email)}</div>` : ""}
          ${r.admin_note ? `<div><strong>Admin note:</strong> ${esc(r.admin_note)}</div>` : ""}
        </div>
        <div class="report-actions">
          <select class="report-status-select" aria-label="Update status">
            <option value="open"${r.status === "open" ? " selected" : ""}>Open</option>
            <option value="acknowledged"${r.status === "acknowledged" ? " selected" : ""}>Acknowledged</option>
            <option value="resolved"${r.status === "resolved" ? " selected" : ""}>Resolved</option>
            <option value="dismissed"${r.status === "dismissed" ? " selected" : ""}>Dismissed</option>
          </select>
          <input type="text" class="report-note-input" placeholder="Admin note (optional)" value="" />
          <button type="button" class="admin-btn secondary report-save-btn">Save</button>
        </div>
      </article>
    `).join("");
  } catch (e) {
    list.innerHTML = `<div class="empty-reports">${esc(e.message || "Failed to load reports")}</div>`;
    if (e.status === 401 || e.status === 503) {
      setToken("");
      showAuthed(false);
    }
  }
}

async function saveReport(article) {
  const id = article.getAttribute("data-id");
  const status = article.querySelector(".report-status-select").value;
  const note = article.querySelector(".report-note-input").value;
  const btn = article.querySelector(".report-save-btn");
  btn.disabled = true;
  try {
    await adminFetch(`/admin/reports/${encodeURIComponent(id)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ status, admin_note: note }),
    });
    await refreshReports();
    await refreshStatus();
  } catch (e) {
    alert(e.message || "Update failed");
  } finally {
    btn.disabled = false;
  }
}

async function tryLogin() {
  const key = document.getElementById("admin-key").value.trim();
  const status = document.getElementById("login-status");
  if (!key) {
    setStatus(status, "Enter the API key", false);
    return;
  }
  setToken(key);
  setStatus(status, "Checking…");
  try {
    await adminFetch("/admin/status");
    setStatus(status, "");
    showAuthed(true);
    document.getElementById("admin-key").value = "";
    await refreshStatus();
    await refreshReports();
  } catch (e) {
    setToken("");
    showAuthed(false);
    setStatus(status, e.message || "Login failed", false);
  }
}

function wire() {
  document.getElementById("login-btn").addEventListener("click", tryLogin);
  document.getElementById("admin-key").addEventListener("keydown", (e) => {
    if (e.key === "Enter") tryLogin();
  });
  document.getElementById("logout-btn").addEventListener("click", () => {
    setToken("");
    showAuthed(false);
    setStatus(document.getElementById("login-status"), "Signed out", true);
  });
  document.getElementById("refresh-status-btn").addEventListener("click", refreshStatus);
  document.getElementById("warmup-btn").addEventListener("click", runWarmup);
  document.getElementById("load-btn").addEventListener("click", loadCSV);
  document.getElementById("refresh-reports-btn").addEventListener("click", refreshReports);
  document.getElementById("report-filter").addEventListener("change", refreshReports);
  document.getElementById("reports-list").addEventListener("click", (e) => {
    const btn = e.target.closest(".report-save-btn");
    if (!btn) return;
    const article = btn.closest(".report-item");
    if (article) saveReport(article);
  });

  if (getToken()) {
    showAuthed(true);
    refreshStatus();
    refreshReports();
  }
}

wire();
