// API base: set window.API_BASE before this script, or use same-origin / local dev.
const API_BASE = (typeof window !== "undefined" && window.API_BASE)
  ? window.API_BASE.replace(/\/$/, "")
  : (window.location.port === "8000" || window.location.hostname === "localhost"
      ? "http://localhost:8000"
      : window.location.origin);

require(["esri/WebMap","esri/views/MapView","esri/widgets/Home","esri/widgets/Zoom","esri/widgets/Search"],
function(WebMap, MapView, Home, Zoom, Search) {
  const webmap = new WebMap({ portalItem: { id: "93eef5bd592f48b4a04e20815dba13b6" } });
  const view = new MapView({ container: "viewDiv", map: webmap, ui: { components: [] } });
  view.ui.add(new Zoom({ view }), "top-left");
  view.ui.add(new Home({ view }), "top-left");
  view.ui.add(new Search({ view }), "top-right");
  window._mapView = view;
  webmap.when(() => {
    let total = 0;
    const qs = webmap.layers.toArray().filter(l=>l.type==="feature")
      .map(l=>l.load().then(()=>l.queryFeatureCount().then(n=>{total+=n;})));
    Promise.all(qs).then(()=>{
      document.getElementById("record-count").textContent=`Live data · ${total} records`;
    }).catch(()=>{
      document.getElementById("record-count").textContent="Live data · connected";
    });
  });
});

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
      ${p.location?`<button class="btn-dir" onclick="panAndDirect('${escHtml(p.location)}')">📍 Directions</button>`:""}
    </div>`;
  return card;
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
    try {
      if (typeof marked !== "undefined" && marked.parse) {
        proseEl.innerHTML = marked.parse(prose);
      } else if (typeof marked !== "undefined" && typeof marked === "function") {
        proseEl.innerHTML = marked(prose);
      } else {
        proseEl.innerHTML = prose.replace(/\n/g, "<br>");
      }
    } catch (e) {
      proseEl.innerHTML = prose.replace(/\n/g, "<br>");
    }
    bubble.appendChild(proseEl);
  }
  projects.forEach((p) => bubble.appendChild(renderProjectCard(p)));
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
      try {
        // marked@4 uses synchronous marked.parse()
        if (typeof marked !== "undefined" && marked.parse) {
          proseEl.innerHTML = marked.parse(prose);
        } else if (typeof marked !== "undefined" && typeof marked === "function") {
          proseEl.innerHTML = marked(prose);
        } else {
          // Plain text fallback — convert newlines to <br>
          proseEl.innerHTML = prose.replace(/\n/g, "<br>");
        }
      } catch(e) {
        console.warn("marked error:", e);
        proseEl.innerHTML = prose.replace(/\n/g, "<br>");
      }
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
  const row = document.createElement("div"); row.id="typing-row"; row.className="msg-row";
  row.innerHTML=`<div class="msg-bot"><div class="bot-avatar">🏛</div><div class="typing-indicator"><div class="dot"></div><div class="dot"></div><div class="dot"></div></div></div>`;
  messagesEl.appendChild(row); messagesEl.scrollTop=messagesEl.scrollHeight;
}
function removeTyping() { const t=document.getElementById("typing-row"); if(t)t.remove(); }

async function tryStreamChat(question) {
  const res = await fetch(`${API_BASE}/chat/stream`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ question }),
  });
  if (!res.ok || !res.body) return false;

  removeTyping();
  const row = document.createElement("div");
  row.className = "msg-row";
  row.innerHTML = `<div class="msg-bot"><div class="bot-avatar">🏛</div><div class="bubble" id="stream-bubble"></div></div>`;
  messagesEl.appendChild(row);
  const bubble = document.getElementById("stream-bubble");
  const proseEl = document.createElement("div");
  bubble.appendChild(proseEl);
  messagesEl.scrollTop = messagesEl.scrollHeight;

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let donePayload = null;

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const parts = buffer.split("\n\n");
    buffer = parts.pop() || "";
    for (const part of parts) {
      if (!part.startsWith("data: ")) continue;
      const payload = JSON.parse(part.slice(6));
      if (payload.type === "token" && payload.text) {
        proseEl.textContent += payload.text;
        messagesEl.scrollTop = messagesEl.scrollHeight;
      } else if (payload.type === "done") {
        donePayload = payload;
      } else if (payload.type === "error") {
        proseEl.textContent = "⚠️ " + (payload.detail || "Stream error");
      }
    }
  }

  if (donePayload) {
    const projects = (donePayload.projects || []).map(normalizeProject);
    const summary = (donePayload.summary || "").trim();
    if (summary && !proseEl.textContent.trim()) {
      proseEl.innerHTML = summary.replace(/\n/g, "<br>");
    }
    projects.forEach((p) => bubble.appendChild(renderProjectCard(p)));
  }
  messagesEl.scrollTop = messagesEl.scrollHeight;
  return true;
}

async function sendMessage(overrideText) {
  const q = (overrideText || questionEl.value).trim();
  if (!q) return;
  startChat(); questionEl.value=""; autoResize(questionEl);
  appendMsg("user", q); showTyping();
  try {
    const streamed = await tryStreamChat(q);
    if (streamed) return;
    const res = await fetch(`${API_BASE}/chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question: q }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      removeTyping();
      appendMsg("bot", "⚠️ Backend error " + res.status + ": " + (err.detail || "Unknown error"));
      return;
    }
    const data = await res.json();
    removeTyping();
    if (data.projects || data.summary) {
      appendBotResponse(data);
      return;
    }
    let answer = (data.answer || data.response || "Sorry, I couldn't find an answer.")
      .replace(/```json[\s\S]*?```/g, "")
      .replace(/```[\s\S]*?```/g, "")
      .trim();
    if (!answer) {
      appendMsg("bot", "I received an empty response from the backend. Please try again.");
      return;
    }
    appendMsg("bot", answer);
  } catch (e) {
    removeTyping();
    console.error("Fetch error:", e);
    appendMsg("bot", `⚠️ Could not reach the backend at ${API_BASE}. Is uvicorn running?`);
  }
}

function sendChip(el) { sendMessage(el.querySelector("span").textContent); }
function handleKey(e) { if(e.key==="Enter"&&!e.shiftKey){e.preventDefault();sendMessage();} }
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

async function loadCSV() {
  const file = document.getElementById("csv-file-input").files[0];
  if (!file) { alert("Please select a CSV file first."); return; }
  const statusEl = document.getElementById("load-status");
  statusEl.style.color = "var(--text-muted)";
  statusEl.textContent = "⏳ Loading…";
  const formData = new FormData(); formData.append("file", file);
  try {
    const res = await fetch(`${API_BASE}/load`, { method: "POST", body: formData });
    const data = await res.json();
    if (!res.ok) {
      // Show the actual server error detail
      statusEl.style.color = "var(--denied)";
      statusEl.textContent = "❌ " + (data.detail || "Server error " + res.status);
      console.error("Load error:", data);
    } else {
      statusEl.style.color = "var(--success)";
      statusEl.textContent = "✅ " + (data.message || "Loaded!");
    }
  } catch(e) {
    statusEl.style.color = "var(--denied)";
    statusEl.textContent = `❌ Can't reach backend at ${API_BASE}`;
    console.error(e);
  }
}

function expandMap(e) {
  e.preventDefault();
  const app = document.getElementById("app");
  const expanded = app.style.gridTemplateColumns === "1fr";
  app.style.gridTemplateColumns = expanded ? "46% 1fr" : "1fr";
  document.getElementById("expand-btn").textContent = expanded ? "⤢ Expand" : "⤡ Collapse";
  if (window._mapView) setTimeout(()=>window._mapView.resize(),300);
}
function toggleMobileMap() { document.getElementById("map-panel").classList.toggle("mobile-show"); }
