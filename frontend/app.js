// API base: config.js may set window.API_BASE; otherwise same-origin (Cloud Run) or local dev.
const API_BASE = (typeof window !== "undefined" && window.API_BASE)
  ? window.API_BASE.replace(/\/$/, "")
  : (window.location.port === "8000" || window.location.hostname === "localhost"
      ? "http://localhost:8000"
      : window.location.origin);

async function refreshRecordCount() {
  const el = document.getElementById("record-count");
  try {
    const res = await fetch(`${API_BASE}/health`);
    if (!res.ok) throw new Error(String(res.status));
    const data = await res.json();
    if (data.record_count != null) {
      el.textContent = `Live data · ${data.record_count} records`;
      return;
    }
  } catch (_) {
    /* fall through to map count */
  }
}

refreshRecordCount();
fetch(`${API_BASE}/warmup`).catch(() => {});

const WEBMAP_ID = "93eef5bd592f48b4a04e20815dba13b6";
const WEBMAP_OPEN_URL = `https://www.arcgis.com/apps/mapviewer/index.html?webmap=${WEBMAP_ID}`;
const ARCGIS_CSS = "https://js.arcgis.com/4.30/esri/themes/light/main.css";
const ARCGIS_JS = "https://js.arcgis.com/4.30/";
const MARKED_JS = "https://cdn.jsdelivr.net/npm/marked@4.3.0/marked.min.js";

let _arcgisLoading = null;
let _mapStarted = false;
let _markedLoading = null;

function loadScript(src) {
  return new Promise((resolve, reject) => {
    if (document.querySelector(`script[src="${src}"]`)) {
      resolve();
      return;
    }
    const s = document.createElement("script");
    s.src = src;
    s.async = true;
    s.onload = () => resolve();
    s.onerror = () => reject(new Error("Failed to load " + src));
    document.head.appendChild(s);
  });
}

function loadCss(href) {
  if (document.querySelector(`link[href="${href}"]`)) return;
  const link = document.createElement("link");
  link.rel = "stylesheet";
  link.href = href;
  document.head.appendChild(link);
}

function ensureMarked() {
  if (typeof marked !== "undefined") return Promise.resolve();
  if (_markedLoading) return _markedLoading;
  _markedLoading = loadScript(MARKED_JS).catch((err) => {
    console.warn("marked load failed:", err);
  });
  return _markedLoading;
}

function loadArcGisApi() {
  if (typeof require === "function") return Promise.resolve();
  if (_arcgisLoading) return _arcgisLoading;
  loadCss(ARCGIS_CSS);
  _arcgisLoading = loadScript(ARCGIS_JS);
  return _arcgisLoading;
}

function showMapFallback(message) {
  const el = document.getElementById("map-fallback");
  if (!el) return;
  el.classList.add("visible");
  el.innerHTML = `${message}<br><a href="${WEBMAP_OPEN_URL}" target="_blank" rel="noopener">Open map in ArcGIS ↗</a>`;
}

function initMap() {
  if (_mapStarted) return;
  _mapStarted = true;
  const fallback = document.getElementById("map-fallback");
  if (fallback) {
    fallback.classList.add("visible");
    fallback.textContent = "Loading map…";
  }

  loadArcGisApi()
    .then(() => {
      if (typeof require !== "function") {
        showMapFallback("ArcGIS API did not load (blocked network or ad blocker).");
        return;
      }
      require(
        ["esri/WebMap", "esri/views/MapView", "esri/widgets/Home", "esri/widgets/Zoom", "esri/widgets/Search"],
        function (WebMap, MapView, Home, Zoom, Search) {
          const webmap = new WebMap({ portalItem: { id: WEBMAP_ID } });
          const view = new MapView({
            container: "viewDiv",
            map: webmap,
            ui: { components: [] },
          });
          view.ui.add(new Zoom({ view }), "top-left");
          view.ui.add(new Home({ view }), "top-left");
          view.ui.add(new Search({ view }), "top-right");
          window._mapView = view;

          view.when(() => {
            if (fallback) fallback.classList.remove("visible");
            setTimeout(() => view.resize(), 50);
            setTimeout(() => view.resize(), 300);
          }).catch((err) => {
            console.error("MapView failed:", err);
            showMapFallback("Map view failed to start.");
          });

          webmap.when(() => {
            const el = document.getElementById("record-count");
            if (el && el.textContent === "Live data · loading…") {
              let total = 0;
              const qs = webmap.layers
                .toArray()
                .filter((l) => l.type === "feature")
                .map((l) =>
                  l.load().then(() => l.queryFeatureCount().then((n) => { total += n; }))
                );
              Promise.all(qs)
                .then(() => { el.textContent = `Live data · ${total} map features`; })
                .catch(() => { el.textContent = "Live data · connected"; });
            }
          }).catch((err) => {
            console.error("WebMap failed:", err);
            showMapFallback("Could not load the Estero web map.");
          });
        },
        function (err) {
          console.error("ArcGIS modules failed to load:", err);
          showMapFallback("ArcGIS map modules failed to load.");
        }
      );
    })
    .catch((err) => {
      console.error("ArcGIS script failed:", err);
      showMapFallback("ArcGIS API failed to load.");
    });
}

function scheduleDeferredMap() {
  const start = () => initMap();
  // Desktop: idle after first paint. Mobile: wait until map panel is shown.
  const wantsMapNow = window.matchMedia("(min-width: 901px)").matches;
  if (!wantsMapNow) return;
  if ("requestIdleCallback" in window) {
    requestIdleCallback(start, { timeout: 2500 });
  } else {
    setTimeout(start, 1200);
  }
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", scheduleDeferredMap);
} else {
  scheduleDeferredMap();
}


const messagesEl = document.getElementById("messages");
const heroEl     = document.getElementById("hero");
const questionEl = document.getElementById("question");
let chatStarted  = false;

function startChat() {
  if (!chatStarted) {
    chatStarted = true;
    heroEl.style.display = "none";
    messagesEl.classList.add("visible");
  }
}

function normalizeProject(p) {
  return {
    title: p.title || "",
    id: p.id || "",
    location: p.location || "",
    summary: p.summary || "",
    status: p.status || "No decision recorded",
    date: p.date || "",
    documentUrl: p.document_url || p.documentUrl || "",
  };
}

// Legacy text-block parser (fallback when API returns plain answer string only)
function parseProjects(text) {
  const projects = [];

  // Try all known delimiter patterns
  const patterns = [
    /START_PROJECT([\s\S]*?)END_PROJECT/g,
    /===PROJECT===([\s\S]*?)===END===/g,
    /---PROJECT---([\s\S]*?)---END---/g,
  ];

  let matched = false;
  for (const regex of patterns) {
    let match;
    regex.lastIndex = 0;
    while ((match = regex.exec(text)) !== null) {
      matched = true;
      const block = match[1];
      const get = (key) => {
        const m = block.match(new RegExp(key + ":\\s*(.+)"));
        return m ? m[1].trim() : "";
      };
      const p = {
        title:       get("Title"),
        id:          get("ID"),
        location:    get("Location"),
        summary:     get("Summary"),
        status:      get("Status"),
        date:        get("Date"),
        documentUrl: get("DocumentURL"),
      };
      if (p.title || p.id) projects.push(p);
    }
    if (matched) break;
  }

  // Smart fallback: if LLM ignored delimiters entirely, try to parse key:value lines
  if (!matched && text.includes("Title:") && text.includes("ID:")) {
    const get = (key) => {
      const m = text.match(new RegExp(key + ":\\s*(.+)"));
      return m ? m[1].trim() : "";
    };
    const p = {
      title:       get("Title"),
      id:          get("ID"),
      location:    get("Location"),
      summary:     get("Summary"),
      status:      get("Status"),
      date:        get("Date"),
      documentUrl: get("DocumentURL"),
    };
    if (p.title || p.id) {
      projects.push(p);
      matched = true;
    }
  }

  // Strip all block content from prose
  let prose = text;
  for (const regex of patterns) {
    regex.lastIndex = 0;
    prose = prose.replace(regex, "");
  }
  // Also strip loose key:value lines if we parsed them as fallback
  if (matched && projects.length > 0) {
    prose = prose
      .replace(/Title:.*\n?/g, "")
      .replace(/ID:.*\n?/g, "")
      .replace(/Location:.*\n?/g, "")
      .replace(/Summary:.*\n?/g, "")
      .replace(/Status:.*\n?/g, "")
      .replace(/Date:.*\n?/g, "")
      .replace(/DocumentURL:.*\n?/g, "")
      .replace(/START_PROJECT.*\n?/g, "")
      .replace(/END_PROJECT.*\n?/g, "");
  }
  prose = prose.trim();

  return { projects, prose };
}

function statusClass(s) {
  s = (s||"").toLowerCase();
  if (s.includes("approved"))  return "status-approved";
  if (s.includes("denied"))    return "status-denied";
  if (s.includes("continued")) return "status-continued";
  return "status-unknown";
}
function statusEmoji(s) {
  s = (s||"").toLowerCase();
  if (s.includes("approved"))  return "✅";
  if (s.includes("denied"))    return "❌";
  if (s.includes("continued")) return "⏳";
  return "⚪";
}

function renderProjectCard(p) {
  const card = document.createElement("div");
  card.className = "proj-card";
  const meta = [p.id, p.location].filter(Boolean).join(" · ");
  card.innerHTML = `
    <div class="proj-title">${escHtml(p.title)}</div>
    ${meta ? `<div class="proj-meta">${escHtml(meta)}</div>` : ""}
    ${p.summary ? `<div class="proj-body">${escHtml(p.summary)}</div>` : ""}
    <div class="proj-status ${statusClass(p.status)}">${statusEmoji(p.status)} ${escHtml(p.status||"No decision recorded")}${p.date?" · "+escHtml(p.date):""}</div>
    <div class="proj-actions">
      ${p.documentUrl?`<a class="btn-minutes" href="${escHtml(p.documentUrl)}" target="_blank">📄 View Minutes</a>`:""}
      ${p.location?`<button type="button" class="btn-dir proj-directions">📍 Directions</button>`:""}
      <button type="button" class="btn-report proj-report">⚑ Report</button>
    </div>`;
  const dirBtn = card.querySelector(".proj-directions");
  if (dirBtn && p.location) {
    dirBtn.addEventListener("click", () => panAndDirect(p.location));
  }
  const reportBtn = card.querySelector(".proj-report");
  if (reportBtn) {
    reportBtn.addEventListener("click", () => openReportDialog({
      kind: p.location ? "incorrect_location" : "suggest_change",
      application_id: p.id || "",
      location: p.location || "",
      current_value: p.location || p.title || "",
    }));
  }
  return card;
}

function formatProse(proseEl, prose) {
  const text = String(prose || "").trim();
  try {
    // Prefer native bullet rendering for "- item" summaries.
    if (/^[-*•]\s+/m.test(text)) {
      const items = text.split(/\n+/).map((l) => l.replace(/^[-*•]\s+/, "").trim()).filter(Boolean);
      proseEl.innerHTML = "<ul>" + items.map((i) => `<li>${escHtml(i)}</li>`).join("") + "</ul>";
      return;
    }
    if (typeof marked !== "undefined" && marked.parse) {
      proseEl.innerHTML = marked.parse(text);
    } else if (typeof marked !== "undefined" && typeof marked === "function") {
      proseEl.innerHTML = marked(text);
    } else {
      proseEl.innerHTML = escHtml(text).replace(/\n/g, "<br>");
    }
  } catch (e) {
    proseEl.innerHTML = escHtml(text).replace(/\n/g, "<br>");
  }
}

function renderStaleNotice(meta) {
  if (!meta || !meta.stale_sources || !meta.stale_notice) return null;
  const el = document.createElement("div");
  el.className = "stale-notice";
  el.setAttribute("role", "status");
  el.textContent = meta.stale_notice;
  return el;
}

function appendBotResponse(data) {
  const row = document.createElement("div");
  row.className = "msg-row";
  const projects = (data.projects || []).map(normalizeProject);
  const prose = (data.summary || data.answer || "").trim();
  const botDiv = document.createElement("div");
  botDiv.className = "msg-bot";
  const avatar = document.createElement("div");
  avatar.className = "bot-avatar";
  avatar.textContent = "🏛";
  botDiv.appendChild(avatar);
  const bubble = document.createElement("div");
  bubble.className = "bubble";
  if (prose) {
    const proseEl = document.createElement("div");
    proseEl.className = "bot-prose";
    formatProse(proseEl, prose);
    bubble.appendChild(proseEl);
    ensureMarked().then(() => formatProse(proseEl, prose));
  }
  projects.forEach((p) => bubble.appendChild(renderProjectCard(p)));
  const staleEl = renderStaleNotice(data.meta);
  if (staleEl) bubble.appendChild(staleEl);
  if (!prose && projects.length === 0) {
    bubble.appendChild(document.createElement("div")).textContent =
      "Sorry, I couldn't find an answer.";
  }
  botDiv.appendChild(bubble);
  row.appendChild(botDiv);
  messagesEl.appendChild(row);
  messagesEl.scrollTop = messagesEl.scrollHeight;
}

function appendMsg(role, content) {
  const row = document.createElement("div");
  row.className = "msg-row";
  if (role === "user") {
    row.innerHTML = `<div class="msg-user"><div class="bubble">${escHtml(content)}</div></div>`;
  } else {
    const { projects, prose } = parseProjects(content);
    const botDiv = document.createElement("div"); botDiv.className = "msg-bot";
    const avatar = document.createElement("div"); avatar.className = "bot-avatar"; avatar.textContent = "🏛";
    botDiv.appendChild(avatar);
    const bubble = document.createElement("div"); bubble.className = "bubble";
    if (prose) {
      const proseEl = document.createElement("div");
      proseEl.className = "bot-prose";
      formatProse(proseEl, prose);
      bubble.appendChild(proseEl);
    }
    // Project cards
    projects.forEach(p => bubble.appendChild(renderProjectCard(p)));
    // Safety fallback: always show something
    if (!prose && projects.length === 0) {
      const fallback = document.createElement("div");
      fallback.innerHTML = content.replace(/\n/g, "<br>");
      bubble.appendChild(fallback);
    }
    botDiv.appendChild(bubble);
    row.appendChild(botDiv);
  }
  messagesEl.appendChild(row);
  messagesEl.scrollTop = messagesEl.scrollHeight;
}

function showTyping() {
  const row = document.createElement("div");
  row.id = "typing-row";
  row.className = "msg-row";
  row.innerHTML = `
    <div class="msg-bot">
      <div class="bot-avatar">🏛</div>
      <div class="typing-wrap">
        <div class="typing-indicator"><div class="dot"></div><div class="dot"></div><div class="dot"></div></div>
        <div class="typing-status" id="typing-status">Searching meeting records…</div>
      </div>
    </div>`;
  messagesEl.appendChild(row);
  messagesEl.scrollTop = messagesEl.scrollHeight;

  const statuses = [
    "Searching meeting records…",
    "Matching projects and locations…",
    "Writing an answer… this can take up to a minute",
  ];
  let i = 0;
  window._typingTimer = setInterval(() => {
    i = Math.min(i + 1, statuses.length - 1);
    const el = document.getElementById("typing-status");
    if (el) el.textContent = statuses[i];
  }, 4000);
}
function removeTyping() {
  if (window._typingTimer) {
    clearInterval(window._typingTimer);
    window._typingTimer = null;
  }
  const t = document.getElementById("typing-row");
  if (t) t.remove();
}
function setSending(busy) {
  const btn = document.getElementById("send-btn");
  if (btn) btn.disabled = !!busy;
  // Keep the textarea enabled so Enter still works if a request hangs.
  if (questionEl) questionEl.readOnly = !!busy;
}

function parseSseFrames(buffer, onPayload) {
  // Cloud Run / proxies may use CRLF; normalize before splitting frames.
  const normalized = buffer.replace(/\r\n/g, "\n").replace(/\r/g, "\n");
  const parts = normalized.split("\n\n");
  const rest = parts.pop() || "";
  for (const part of parts) {
    const line = part.trim();
    if (!line.startsWith("data:")) continue;
    const jsonText = line.slice(5).trim();
    if (!jsonText) continue;
    try {
      onPayload(JSON.parse(jsonText));
    } catch (err) {
      console.warn("SSE JSON parse failed:", err, jsonText.slice(0, 200));
    }
  }
  return rest;
}

async function tryStreamChat(question) {
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), 120000);
  try {
    const res = await fetch(`${API_BASE}/chat/stream`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question }),
      signal: controller.signal,
    });
    if (!res.ok || !res.body) return false;

    removeTyping();
    const row = document.createElement("div");
    row.className = "msg-row";
    const botDiv = document.createElement("div");
    botDiv.className = "msg-bot";
    const avatar = document.createElement("div");
    avatar.className = "bot-avatar";
    avatar.textContent = "🏛";
    const bubble = document.createElement("div");
    bubble.className = "bubble";
    const proseEl = document.createElement("div");
    bubble.appendChild(proseEl);
    botDiv.appendChild(avatar);
    botDiv.appendChild(bubble);
    row.appendChild(botDiv);
    messagesEl.appendChild(row);
    messagesEl.scrollTop = messagesEl.scrollHeight;

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let donePayload = null;
    let sawError = false;

    const handlePayload = (payload) => {
      if (payload.type === "token" && payload.text) {
        proseEl.textContent += payload.text;
        messagesEl.scrollTop = messagesEl.scrollHeight;
      } else if (payload.type === "done") {
        donePayload = payload;
      } else if (payload.type === "error") {
        sawError = true;
        proseEl.textContent = "⚠️ " + (payload.detail || "Stream error");
      }
    };

    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      buffer = parseSseFrames(buffer, handlePayload);
    }
    buffer += decoder.decode();
    if (buffer.trim()) {
      parseSseFrames(buffer + "\n\n", handlePayload);
    }

    if (donePayload) {
      const projects = (donePayload.projects || []).map(normalizeProject);
      const summary = (donePayload.summary || donePayload.answer || "").trim();
      if (summary) {
        formatProse(proseEl, summary);
      }
      projects.forEach((p) => bubble.appendChild(renderProjectCard(p)));
      const staleEl = renderStaleNotice(donePayload.meta);
      if (staleEl) bubble.appendChild(staleEl);
      if (!proseEl.textContent.trim() && !proseEl.querySelector("li") && projects.length === 0) {
        proseEl.textContent = "Sorry, I couldn't find an answer.";
      }
      messagesEl.scrollTop = messagesEl.scrollHeight;
      return true;
    }

    // Stream opened but produced no usable done event — remove empty bubble
    // and let sendMessage fall back to POST /chat.
    if (!sawError && !proseEl.textContent.trim()) {
      row.remove();
      return false;
    }
    messagesEl.scrollTop = messagesEl.scrollHeight;
    return true;
  } finally {
    clearTimeout(timeoutId);
  }
}

let _sending = false;
async function sendMessage(overrideText) {
  if (_sending) return;
  const q = (overrideText || (questionEl && questionEl.value) || "").trim();
  if (!q) return;
  startChat();
  if (questionEl) { questionEl.value = ""; autoResize(questionEl); }
  appendMsg("user", q);
  _sending = true;
  setSending(true);
  showTyping();
  try {
    // Prefer non-streaming /chat — Cloud Run often buffers SSE so stream hangs
    // and used to leave the UI stuck with a disabled input.
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), 120000);
    let res;
    try {
      res = await fetch(`${API_BASE}/chat`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question: q }),
        signal: controller.signal,
      });
    } finally {
      clearTimeout(timeoutId);
    }
    if (res.ok) {
      const data = await res.json();
      removeTyping();
      if (data.projects || data.summary || data.answer) {
        appendBotResponse(data);
        return;
      }
      appendMsg("bot", "I received an empty response from the backend. Please try again.");
      return;
    }
    // Fall back to stream only if /chat failed.
    console.warn("/chat failed with", res.status, "— trying stream");
    try {
      const streamed = await tryStreamChat(q);
      if (streamed) return;
    } catch (streamErr) {
      console.warn("Stream failed:", streamErr);
    }
    const err = await res.json().catch(() => ({}));
    removeTyping();
    appendMsg("bot", "⚠️ Backend error " + res.status + ": " + (err.detail || "Unknown error"));
  } catch (e) {
    removeTyping();
    console.error("Fetch error:", e);
    appendMsg("bot", `⚠️ Could not reach the backend at ${API_BASE}. The request may have timed out — try a more specific question (e.g. an Application ID).`);
  } finally {
    _sending = false;
    setSending(false);
  }
}

function sendChip(el) { sendMessage(el.querySelector("span").textContent); }
function handleKey(e) {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    sendMessage();
  }
}
function autoResize(el) { el.style.height="auto"; el.style.height=Math.min(el.scrollHeight,100)+"px"; }
function escHtml(s) { return String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;"); }

function panAndDirect(address) {
  window.open(`https://www.google.com/maps/search/?api=1&query=${encodeURIComponent(address+", Estero, FL")}`,"_blank");
  if (window._mapView) {
    require(["esri/rest/locator"], function(locator) {
      locator.addressToLocations("https://geocode.arcgis.com/arcgis/rest/services/World/GeocodeServer",
        { address:{SingleLine:address}, maxLocations:1 })
        .then(r=>{ if(r.length) window._mapView.goTo({target:r[0].location,zoom:15}); })
        .catch(()=>{});
    });
  }
}

function openReportDialog(prefill) {
  const dialog = document.getElementById("report-dialog");
  if (!dialog) return;
  const p = prefill || {};
  document.getElementById("report-kind").value = p.kind || "incorrect_location";
  document.getElementById("report-application-id").value = p.application_id || "";
  document.getElementById("report-location").value = p.location || "";
  document.getElementById("report-current").value = p.current_value || "";
  document.getElementById("report-suggested").value = p.suggested_value || "";
  document.getElementById("report-details").value = p.details || "";
  document.getElementById("report-email").value = p.contact_email || "";
  const status = document.getElementById("report-form-status");
  status.textContent = "";
  status.classList.remove("ok", "err");
  if (typeof dialog.showModal === "function") dialog.showModal();
  else dialog.setAttribute("open", "");
}

function closeReportDialog() {
  const dialog = document.getElementById("report-dialog");
  if (!dialog) return;
  if (typeof dialog.close === "function") dialog.close();
  else dialog.removeAttribute("open");
}

async function submitReport(event) {
  event.preventDefault();
  const status = document.getElementById("report-form-status");
  const btn = document.getElementById("report-submit-btn");
  const payload = {
    kind: document.getElementById("report-kind").value,
    application_id: document.getElementById("report-application-id").value.trim(),
    location: document.getElementById("report-location").value.trim(),
    current_value: document.getElementById("report-current").value.trim(),
    suggested_value: document.getElementById("report-suggested").value.trim(),
    details: document.getElementById("report-details").value.trim(),
    contact_email: document.getElementById("report-email").value.trim(),
    page_url: window.location.href,
  };
  if (payload.details.length < 5) {
    status.textContent = "Please add a few more details.";
    status.classList.add("err");
    return;
  }
  btn.disabled = true;
  status.textContent = "Sending…";
  status.classList.remove("ok", "err");
  try {
    const res = await fetch(`${API_BASE}/reports`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      throw new Error(data.detail || `Could not send (${res.status})`);
    }
    status.textContent = "Thanks — your report was submitted.";
    status.classList.add("ok");
    setTimeout(closeReportDialog, 900);
  } catch (e) {
    status.textContent = e.message || "Could not reach the server.";
    status.classList.add("err");
  } finally {
    btn.disabled = false;
  }
}

function wireReportUi() {
  const openBtn = document.getElementById("report-open-btn");
  if (openBtn) openBtn.addEventListener("click", () => openReportDialog());
  const form = document.getElementById("report-form");
  if (form) form.addEventListener("submit", submitReport);
  const closeBtn = document.getElementById("report-close-btn");
  if (closeBtn) closeBtn.addEventListener("click", closeReportDialog);
  const cancelBtn = document.getElementById("report-cancel-btn");
  if (cancelBtn) cancelBtn.addEventListener("click", closeReportDialog);
}

wireReportUi();

function expandMap(e) {
  e.preventDefault();
  const app = document.getElementById("app");
  const expanded = app.style.gridTemplateColumns === "1fr";
  app.style.gridTemplateColumns = expanded ? "46% 1fr" : "1fr";
  document.getElementById("expand-btn").textContent = expanded ? "⤢ Expand" : "⤡ Collapse";
  if (window._mapView) setTimeout(()=>window._mapView.resize(),300);
}
function toggleMobileMap() {
  const panel = document.getElementById("map-panel");
  panel.classList.toggle("mobile-show");
  if (panel.classList.contains("mobile-show")) initMap();
  if (window._mapView) setTimeout(() => window._mapView.resize(), 100);
}

// Wire controls in JS so Enter/Send work even if inline handlers are blocked.
(function wireChatControls() {
  const q = document.getElementById("question");
  const btn = document.getElementById("send-btn");
  if (q) q.addEventListener("keydown", handleKey);
  if (btn) btn.addEventListener("click", () => sendMessage());
  window.sendMessage = sendMessage;
  window.sendChip = sendChip;
  window.handleKey = handleKey;
  window.autoResize = autoResize;
  window.toggleMobileMap = toggleMobileMap;
  window.expandMap = expandMap;
  window.panAndDirect = panAndDirect;
})();
