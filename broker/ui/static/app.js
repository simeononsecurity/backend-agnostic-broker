// backend-agnostic-broker playground (ADR 0015).
// Vanilla ES module. No external imports, no dynamic code evaluation, no CDN.
// The API key and admin key live ONLY in the `state` object below (tab memory);
// they are never written to localStorage, sessionStorage, or cookies.

"use strict";

const state = {
  baseUrl: "",
  apiKey: "",
  adminKey: "",
  connected: false,
  rerankAvailable: false,
};

const $ = (id) => document.getElementById(id);
const el = (tag, attrs = {}, text) => {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else node.setAttribute(k, v);
  }
  if (text !== undefined) node.textContent = text;
  return node;
};

function base() {
  // Same-origin by default; a trailing slash is trimmed so paths concatenate.
  const b = state.baseUrl.trim();
  return b ? b.replace(/\/+$/, "") : "";
}

function authHeaders(admin = false) {
  const h = { "Content-Type": "application/json" };
  if (state.apiKey) h["Authorization"] = "Bearer " + state.apiKey;
  if (admin && state.adminKey) h["X-Admin-Key"] = state.adminKey;
  return h;
}

function banner(msg, ok = false) {
  const b = $("banner");
  if (!msg) {
    b.hidden = true;
    b.textContent = "";
    return;
  }
  b.hidden = false;
  b.textContent = msg;
  b.className = ok ? "banner ok" : "banner";
}

function setConn(connected, text) {
  state.connected = connected;
  $("conn-dot").className = "dot " + (connected ? "ok" : "err");
  $("conn-text").textContent = text;
}

async function api(path, opts = {}) {
  const resp = await fetch(base() + path, opts);
  return resp;
}

async function readJson(resp) {
  const text = await resp.text();
  try {
    return text ? JSON.parse(text) : {};
  } catch (_e) {
    return { _raw: text };
  }
}

function rateLimitSummary(resp) {
  const parts = [];
  for (const [label, name] of [
    ["rpm", "x-ratelimit-remaining-requests"],
    ["tpm", "x-ratelimit-remaining-tokens"],
  ]) {
    const v = resp.headers.get(name);
    if (v !== null) parts.push(label + "=" + v);
  }
  return parts.join(" ");
}

function renderMeta(target, resp) {
  const route = resp.headers.get("x-broker-route");
  const cache = resp.headers.get("x-broker-cache");
  const rid = resp.headers.get("x-request-id");
  const rl = rateLimitSummary(resp);
  const frag = document.createDocumentFragment();
  if (route) {
    frag.append(el("span", { class: "tag" }, "route: " + route), document.createTextNode("  "));
  }
  if (cache) {
    const cls = cache === "hit" ? "tag-hit" : cache === "miss" ? "tag-miss" : "tag";
    frag.append(el("span", { class: cls }, "cache: " + cache), document.createTextNode("  "));
  }
  if (rl) frag.append(document.createTextNode(rl + "  "));
  if (rid) frag.append(el("span", { class: "muted" }, rid));
  target.replaceChildren(frag);
}

// ---- connection / discovery ---------------------------------------------

async function connect(ev) {
  ev.preventDefault();
  state.baseUrl = $("base-url").value;
  state.apiKey = $("api-key").value;
  state.adminKey = $("admin-key").value;
  banner("");
  try {
    const resp = await api("/v1/models", { headers: authHeaders() });
    if (!resp.ok) {
      setConn(false, "auth failed (" + resp.status + ")");
      banner("Could not list models: HTTP " + resp.status);
      return;
    }
    setConn(true, "connected");
    await refreshModels();
    await probeRerank();
    toggleAdmin();
    banner("Connected.", true);
  } catch (e) {
    setConn(false, "unreachable");
    banner("Connection error: " + e.message);
  }
}

function forgetKeys() {
  state.apiKey = "";
  state.adminKey = "";
  $("api-key").value = "";
  $("admin-key").value = "";
  setConn(false, "keys cleared");
  toggleAdmin();
  banner("Keys cleared from memory.", true);
}

function toggleAdmin() {
  const show = state.connected && !!state.adminKey;
  document.querySelector('.tab[data-tab="admin"]').hidden = !show;
  if (!show && !$("panel-admin").hidden) selectTab("models");
}

async function probeRerank() {
  // Hide the Rerank tab unless /v1/rerank exists (a 404 means not wired in).
  try {
    const resp = await api("/v1/rerank", {
      method: "POST",
      headers: authHeaders(),
      body: JSON.stringify({ model: "", query: "", documents: [] }),
    });
    state.rerankAvailable = resp.status !== 404;
  } catch (_e) {
    state.rerankAvailable = false;
  }
  document.querySelector('.tab[data-tab="rerank"]').hidden = !state.rerankAvailable;
}

async function refreshModels() {
  const body = $("models-body");
  body.replaceChildren();
  const resp = await api("/v1/models", { headers: authHeaders() });
  if (!resp.ok) {
    $("models-empty").hidden = false;
    $("models-empty").textContent = "Error listing models (HTTP " + resp.status + ").";
    return;
  }
  const data = await readJson(resp);
  const models = data.data || [];
  $("models-empty").hidden = models.length > 0;
  const dl = $("model-list");
  dl.replaceChildren();
  for (const m of models) {
    const caps = m.capabilities || m.caps || [];
    const tr = el("tr");
    tr.append(el("td", {}, m.id || ""));
    tr.append(el("td", {}, m.backend || ""));
    const capTd = el("td");
    (Array.isArray(caps) ? caps : Object.keys(caps)).forEach((c) =>
      capTd.append(el("span", { class: "badge cap" }, String(c)))
    );
    tr.append(capTd);
    const poolTd = el("td");
    const cands = m.pool || m.router_candidates || m.members || [];
    (Array.isArray(cands) ? cands : [cands]).forEach((c) =>
      c ? poolTd.append(el("span", { class: "badge" }, String(c))) : null
    );
    tr.append(poolTd);
    tr.append(el("td", {}, String(m.context_limit || m.context_length || "")));
    body.append(tr);
    dl.append(el("option", { value: m.id || "" }));
  }
  dl.append(el("option", { value: "auto" }));
}

// ---- streaming via fetch (SSE over ReadableStream) -----------------------

async function streamSSE(path, payload, onDelta, metaTarget) {
  const resp = await api(path, {
    method: "POST",
    headers: authHeaders(),
    body: JSON.stringify(payload),
  });
  renderMeta(metaTarget, resp);
  if (!resp.ok || !resp.body) {
    const errData = await readJson(resp);
    throw new Error(errData?.error?.message || "HTTP " + resp.status);
  }
  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let idx;
    while ((idx = buf.indexOf("\n\n")) !== -1) {
      const frame = buf.slice(0, idx);
      buf = buf.slice(idx + 2);
      for (const line of frame.split("\n")) {
        const trimmed = line.trim();
        if (!trimmed.startsWith("data:")) continue;
        const data = trimmed.slice(5).trim();
        if (data === "[DONE]") return;
        try {
          onDelta(JSON.parse(data));
        } catch (_e) {
          /* ignore keep-alive / non-JSON frames */
        }
      }
    }
  }
}

// ---- chat ----------------------------------------------------------------

async function chatSend() {
  const out = $("chat-output");
  const meta = $("chat-meta");
  out.textContent = "";
  banner("");
  const messages = [];
  const sys = $("chat-system").value.trim();
  if (sys) messages.push({ role: "system", content: sys });
  messages.push({ role: "user", content: $("chat-input").value });
  const payload = {
    model: $("chat-model").value.trim() || "auto",
    messages,
    temperature: Number($("chat-temp").value),
    stream: $("chat-stream").checked,
  };
  try {
    if (payload.stream) {
      await streamSSE(
        "/v1/chat/completions",
        payload,
        (chunk) => {
          const delta = chunk?.choices?.[0]?.delta?.content;
          if (delta) out.textContent += delta;
        },
        meta
      );
    } else {
      const resp = await api("/v1/chat/completions", {
        method: "POST",
        headers: authHeaders(),
        body: JSON.stringify(payload),
      });
      renderMeta(meta, resp);
      const data = await readJson(resp);
      if (!resp.ok) throw new Error(data?.error?.message || "HTTP " + resp.status);
      out.textContent = data?.choices?.[0]?.message?.content ?? JSON.stringify(data, null, 2);
    }
  } catch (e) {
    banner("Chat error: " + e.message);
  }
}

// ---- completions / responses --------------------------------------------

async function compSend() {
  const out = $("comp-output");
  const meta = $("comp-meta");
  out.textContent = "";
  banner("");
  const payload = {
    model: $("comp-model").value.trim() || "auto",
    prompt: $("comp-input").value,
    stream: $("comp-stream").checked,
  };
  try {
    if (payload.stream) {
      await streamSSE(
        "/v1/completions",
        payload,
        (chunk) => {
          const t = chunk?.choices?.[0]?.text;
          if (t) out.textContent += t;
        },
        meta
      );
    } else {
      const resp = await api("/v1/completions", {
        method: "POST",
        headers: authHeaders(),
        body: JSON.stringify(payload),
      });
      renderMeta(meta, resp);
      const data = await readJson(resp);
      if (!resp.ok) throw new Error(data?.error?.message || "HTTP " + resp.status);
      out.textContent = data?.choices?.[0]?.text ?? JSON.stringify(data, null, 2);
    }
  } catch (e) {
    banner("Completions error: " + e.message);
  }
}

async function respSend() {
  const out = $("resp-output");
  const meta = $("resp-meta");
  out.textContent = "";
  banner("");
  const payload = {
    model: $("resp-model").value.trim() || "auto",
    input: $("resp-input").value,
    stream: $("resp-stream").checked,
  };
  try {
    if (payload.stream) {
      await streamSSE(
        "/v1/responses",
        payload,
        (chunk) => {
          const d = chunk?.delta;
          if (typeof d === "string") out.textContent += d;
        },
        meta
      );
    } else {
      const resp = await api("/v1/responses", {
        method: "POST",
        headers: authHeaders(),
        body: JSON.stringify(payload),
      });
      renderMeta(meta, resp);
      const data = await readJson(resp);
      if (!resp.ok) throw new Error(data?.error?.message || "HTTP " + resp.status);
      out.textContent = JSON.stringify(data, null, 2);
    }
  } catch (e) {
    banner("Responses error: " + e.message);
  }
}

// ---- embeddings / rerank -------------------------------------------------

async function embSend() {
  const out = $("emb-output");
  const meta = $("emb-meta");
  out.textContent = "";
  banner("");
  const input = $("emb-input").value.split("\n").map((s) => s.trim()).filter(Boolean);
  try {
    const resp = await api("/v1/embeddings", {
      method: "POST",
      headers: authHeaders(),
      body: JSON.stringify({ model: $("emb-model").value.trim(), input }),
    });
    renderMeta(meta, resp);
    const data = await readJson(resp);
    if (!resp.ok) throw new Error(data?.error?.message || "HTTP " + resp.status);
    const dims = data?.data?.map((d) => (d.embedding || []).length);
    out.textContent =
      "vectors: " + (data?.data?.length ?? 0) + "  dims: " + JSON.stringify(dims) +
      "\n\n" + JSON.stringify(data, null, 2);
  } catch (e) {
    banner("Embeddings error: " + e.message);
  }
}

async function rrSend() {
  const out = $("rr-output");
  const meta = $("rr-meta");
  out.textContent = "";
  banner("");
  const documents = $("rr-docs").value.split("\n").map((s) => s.trim()).filter(Boolean);
  try {
    const resp = await api("/v1/rerank", {
      method: "POST",
      headers: authHeaders(),
      body: JSON.stringify({
        model: $("rr-model").value.trim(),
        query: $("rr-query").value,
        documents,
      }),
    });
    renderMeta(meta, resp);
    const data = await readJson(resp);
    if (!resp.ok) throw new Error(data?.error?.message || "HTTP " + resp.status);
    out.textContent = JSON.stringify(data, null, 2);
  } catch (e) {
    banner("Rerank error: " + e.message);
  }
}

// ---- status / usage ------------------------------------------------------

async function refreshStatus() {
  banner("");
  try {
    const resp = await api("/status", { headers: authHeaders() });
    const data = await readJson(resp);
    if (!resp.ok) throw new Error(data?.error?.message || "HTTP " + resp.status);
    $("status-output").textContent = JSON.stringify(data, null, 2);
  } catch (e) {
    banner("Status error: " + e.message);
  }
}

async function refreshUsage() {
  banner("");
  try {
    const resp = await api("/usage", { headers: authHeaders() });
    const data = await readJson(resp);
    if (!resp.ok) throw new Error(data?.error?.message || "HTTP " + resp.status);
    $("usage-output").textContent = JSON.stringify(data, null, 2);
  } catch (e) {
    banner("Usage error: " + e.message);
  }
}

// ---- admin ---------------------------------------------------------------

async function refreshAdmin() {
  banner("");
  const body = $("admin-body");
  body.replaceChildren();
  try {
    const resp = await api("/admin/models", { headers: authHeaders(true) });
    const data = await readJson(resp);
    if (!resp.ok) throw new Error(data?.error?.message || "HTTP " + resp.status);
    for (const m of data.models || []) {
      const tr = el("tr");
      tr.append(el("td", {}, m.alias || ""));
      tr.append(el("td", { class: "state-" + (m.state || "") }, m.state || ""));
      tr.append(el("td", {}, String(m.inflight ?? "")));
      const actions = el("td");
      const loadBtn = el("button", { class: "secondary", type: "button" }, "Load");
      loadBtn.addEventListener("click", () => adminAction(m.alias, "load"));
      const unloadBtn = el("button", { class: "danger", type: "button" }, "Unload");
      unloadBtn.addEventListener("click", () => adminAction(m.alias, "unload"));
      actions.append(loadBtn, document.createTextNode(" "), unloadBtn);
      tr.append(actions);
      body.append(tr);
    }
  } catch (e) {
    banner("Admin error: " + e.message);
  }
}

async function adminAction(alias, action) {
  const verb = action === "unload" ? "Unload" : "Load";
  if (!window.confirm(verb + " model '" + alias + "'?")) return;
  try {
    const path = "/admin/models/" + encodeURIComponent(alias) + "/" + action;
    const resp = await api(path, { method: "POST", headers: authHeaders(true) });
    const data = await readJson(resp);
    if (!resp.ok) throw new Error(data?.error?.message || "HTTP " + resp.status);
    banner(verb + " ok: " + JSON.stringify(data), true);
    await refreshAdmin();
  } catch (e) {
    banner("Admin action error: " + e.message);
  }
}

async function adminReload() {
  if (!window.confirm("Reload the registry from disk now?")) return;
  try {
    const resp = await api("/admin/registry/reload", {
      method: "POST",
      headers: authHeaders(true),
    });
    const data = await readJson(resp);
    if (!resp.ok) throw new Error(data?.error?.message || "HTTP " + resp.status);
    banner("Registry reloaded: " + JSON.stringify(data), true);
    await refreshAdmin();
    await refreshModels();
  } catch (e) {
    banner("Reload error: " + e.message);
  }
}

// ---- tabs / wiring -------------------------------------------------------

const PANELS = [
  "models", "chat", "completions", "responses",
  "embeddings", "rerank", "status", "usage", "admin",
];

function selectTab(name) {
  for (const p of PANELS) {
    const panel = $("panel-" + p);
    if (panel) panel.hidden = p !== name;
  }
  document.querySelectorAll(".tab").forEach((t) =>
    t.setAttribute("aria-selected", String(t.dataset.tab === name))
  );
  if (name === "status") refreshStatus();
  if (name === "usage") refreshUsage();
  if (name === "admin") refreshAdmin();
}

const ACTIONS = {
  "refresh-models": refreshModels,
  "chat-send": chatSend,
  "chat-clear": () => {
    $("chat-output").textContent = "";
    $("chat-input").value = "";
    $("chat-meta").textContent = "";
  },
  "comp-send": compSend,
  "resp-send": respSend,
  "emb-send": embSend,
  "rr-send": rrSend,
  "refresh-status": refreshStatus,
  "refresh-usage": refreshUsage,
  "refresh-admin": refreshAdmin,
  "admin-reload": adminReload,
};

function init() {
  $("connect-form").addEventListener("submit", connect);
  $("forget-btn").addEventListener("click", forgetKeys);
  document.querySelectorAll(".tab").forEach((t) =>
    t.addEventListener("click", () => selectTab(t.dataset.tab))
  );
  document.body.addEventListener("click", (ev) => {
    const target = ev.target.closest("[data-action]");
    if (!target) return;
    const fn = ACTIONS[target.dataset.action];
    if (fn) fn();
  });
  // Ctrl/Cmd+Enter sends from the chat message box.
  $("chat-input").addEventListener("keydown", (ev) => {
    if ((ev.ctrlKey || ev.metaKey) && ev.key === "Enter") chatSend();
  });
  setConn(false, "not connected");
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", init);
} else {
  init();
}
