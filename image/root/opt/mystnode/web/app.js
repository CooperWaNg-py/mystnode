"use strict";
// Mysterium Node dashboard. Vanilla JS, no build step, no external requests.
// Served at /umbrel/. Every /tequilapi/... request goes to the dashboard
// server, which forwards it to NodeUI with the node credential injected, so
// the browser never holds a node token.

const STATUS_BOOT_MS = 5000;      // status poll until the main view is up
const STATUS_MS = 15000;          // status poll afterwards
const OVERVIEW_MS = 15000;
const REGISTRATION_MS = 10000;    // overview poll while registration is in progress
const SERVICES_MS = 10000;
const LOGS_MS = 5000;
const IDENTITY_RETRY_MS = 5000;
// After "Restart node" the old process may still answer for a moment; the
// node counts as restarted once it was seen down, or after this long.
const RESTART_GRACE_MS = 60000;

const TABS = ["overview", "services", "earnings", "sessions", "settings", "logs"];
// Tabs whose data goes stale on its own; they reload when the page becomes visible again.
const POLLING_TABS = new Set(["overview", "services", "logs"]);
const SERVICES = [
  ["dvpn", "VPN (dVPN)"],
  ["wireguard", "Public (WireGuard)"],
  ["data_transfer", "B2B data transfer"],
  ["scraping", "B2B data scraping"],
];
const SERVICE_LABELS = Object.fromEntries(SERVICES);
const NAT_LABELS = {
  none: "Open",
  fullcone: "Full cone",
  rcone: "Restricted cone",
  prcone: "Port-restricted cone",
  symmetric: "Symmetric",
};
const MONITORING = {
  success: ["Passed", "ready"],
  failed: ["Failed", "failed"],
  pending: ["Pending", "initializing"],
  unknown: ["Unknown", ""],
};
const REGISTRATION_TEXT = {
  Unregistered: "Not registered yet.",
  RegistrationError: "The last registration attempt failed; try again.",
};
// String error codes from the dashboard server (TequilAPI errors are objects).
const BACKEND_ERRORS = {
  node_unreachable: "The node is not reachable right now.",
  node_auth_failed: "The node rejected the dashboard password; it is restored within 15 seconds.",
};
const WALLET_RE = /^0x[0-9a-fA-F]{40}$/;
const LOG_LEVELS = ["trace", "debug", "info", "warn", "error"];

const $ = (id) => document.getElementById(id);

const state = {
  status: null,        // last successful /umbrel/api/status payload
  statusBusy: false,
  statusTimer: null,
  nodeUp: false,       // node reachable and not inside a requested restart
  restart: null,       // {since, sawDown} while a requested restart is in progress
  id: null,            // provider identity address
  idBusy: false,
  idTimer: null,
  tab: "overview",     // tab selected by the hash
  shown: null,         // what is on screen: loading | terms | starting | identity | tab:<name>
  tabGen: 0,           // bumped whenever the shown tab changes; stale loads and timers check it
  tabTimer: null,
  nat: null,           // promise; NAT type is a live STUN probe, fetched once per page load
  regInProgress: false,
  identity: null,      // last GET /identities/{id}, used by Settle now
  services: [],
  svcPending: new Map(), // service type -> requested on/off while its request is in flight
  sessionsPage: 1,
  logsPin: true,       // next log render scrolls to the bottom regardless of position
};

// ---------------------------------------------------------------- helpers

function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k === "class") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else if (k.includes("-")) node.setAttribute(k, v); // aria-*, data-*
    else if (v !== undefined && v !== null && v !== false) node[k] = v;
  }
  for (const c of children) node.append(c);
  return node;
}

const SVG_NS = "http://www.w3.org/2000/svg";
function svg(tag, attrs = {}, ...children) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  for (const c of children) node.append(c);
  return node;
}

// JSON request to the dashboard server. Throws Error(message) on non-2xx,
// with .status and .code; message is the TequilAPI envelope's
// error.message, or the server's string error, or the status text.
async function api(path, opts = {}) {
  const init = { ...opts, headers: { "Content-Type": "application/json" } };
  if (init.body !== undefined && typeof init.body !== "string") init.body = JSON.stringify(init.body);
  let res;
  let text;
  try {
    res = await fetch(path, init);
    text = await res.text();
  } catch {
    throw Object.assign(new Error("Could not reach the dashboard. Check your connection."), { status: 0 });
  }
  let body = null;
  try { body = text ? JSON.parse(text) : null; } catch { body = text; }
  if (!res.ok) {
    const err = body && typeof body === "object" ? body.error : null;
    const code = typeof err === "string" ? err : err?.code;
    const message = err?.message
      || (typeof err === "string" ? BACKEND_ERRORS[err] || err : null)
      || (typeof body === "string" && body.trim())
      || res.statusText
      || `Request failed (HTTP ${res.status})`;
    if (code === "node_unreachable") kickStatus();
    throw Object.assign(new Error(message), { status: res.status, code });
  }
  return body;
}

const tq = (path, opts) => api("/tequilapi" + path, opts);

const myst = (tokens) => tokens?.human || "–";
const mystText = (tokens) => (tokens?.human ? `${myst(tokens)} MYST` : "–");

// Deprecated numeric wei fields only; display precision is all that matters.
function weiToMyst(n) {
  if (n === null || n === undefined || n === "") return "–";
  return (Number(n) / 1e18).toFixed(6);
}

function bytes(n) {
  n = Number(n) || 0;
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return i ? `${n.toFixed(1)} ${units[i]}` : `${n} B`;
}

function hoursMinutes(seconds) {
  const s = Math.max(0, Math.floor(Number(seconds) || 0));
  return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
}

// Go duration string ("25h53m33.54s") → "1d 1h 53m".
function uptime(go) {
  if (!go) return "–";
  const unit = { h: 3600, m: 60, s: 1, ms: 1e-3, us: 1e-6, "µs": 1e-6, ns: 1e-9 };
  let secs = 0;
  for (const [, v, u] of String(go).matchAll(/([\d.]+)(h|ms|us|µs|ns|m|s)/g)) secs += parseFloat(v) * unit[u];
  const d = Math.floor(secs / 86400);
  const h = Math.floor((secs % 86400) / 3600);
  const m = Math.floor((secs % 3600) / 60);
  if (d) return `${d}d ${h}h ${m}m`;
  if (h) return `${h}h ${m}m`;
  return `${m}m ${Math.floor(secs % 60)}s`;
}

function dateTime(s) {
  if (!s) return "–";
  const d = new Date(s);
  return Number.isNaN(d.getTime()) ? String(s) : d.toLocaleString();
}

const shortAddr = (a) => (a && a.length > 14 ? `${a.slice(0, 8)}…${a.slice(-6)}` : a || "–");

function setText(id, text) { $(id).textContent = text; }

function setBadge(id, text, cls) {
  $(id).replaceChildren(el("span", { class: `badge ${cls}`.trim() }, text));
}

function showError(id, message) {
  $(id).textContent = message || "";
  $(id).hidden = !message;
}

function toast(message, kind = "info") {
  const node = el("div", { class: `toast ${kind}`, role: kind === "error" ? "alert" : "status" }, message);
  $("toasts").append(node);
  setTimeout(() => {
    node.classList.add("fade");
    setTimeout(() => node.remove(), 300);
  }, kind === "error" ? 7000 : 4000);
}

// Copies text; navigator.clipboard only exists in secure contexts, and
// Umbrel is usually opened over plain http.
async function copy(text) {
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
    } else {
      const area = el("textarea", { value: text, readOnly: true });
      area.style.position = "fixed";
      area.style.opacity = "0";
      document.body.append(area);
      area.select();
      const ok = document.execCommand("copy");
      area.remove();
      if (!ok) throw new Error("copy refused");
    }
    toast("Copied", "ok");
  } catch {
    toast("Copy failed; select the text and copy it manually.", "error");
  }
}

// Runs fn with `button` disabled; errors become an error toast.
async function withBusy(button, fn) {
  if (button.disabled) return;
  button.disabled = true;
  try {
    await fn();
  } catch (e) {
    toast(e.message, "error");
  } finally {
    button.disabled = false;
  }
}

// Inline SVG bar chart of [{key: "YYYY-MM-DD", value}] (no library).
function barChart(container, points, fmt, label) {
  // Drawn at the container's pixel width so text keeps its CSS size in wide and
  // narrow cards; 600 when the container is not laid out yet.
  const W = Math.max(280, Math.round(container.clientWidth) || 600);
  const H = 170, top = 16, bottom = 20, side = 4;
  const plotH = H - top - bottom;
  const slot = (W - 2 * side) / points.length;
  const barW = Math.max(1, slot * 0.7);
  const max = Math.max(0, ...points.map((p) => p.value));
  const chart = svg("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": label });
  chart.append(svg("line", { class: "axis", x1: side, x2: W - side, y1: top + plotH, y2: top + plotH }));
  points.forEach((p, i) => {
    const h = max > 0 ? (p.value / max) * plotH : 0;
    const x = side + i * slot + (slot - barW) / 2;
    const height = p.value > 0 ? Math.max(h, 1) : 0;
    chart.append(svg("rect", { class: "bar", x, y: top + plotH - height, width: barW, height, rx: 1.5 },
      svg("title", {}, `${p.key}: ${fmt(p.value)}`)));
    if ((points.length - 1 - i) % 7 === 0) {
      // The newest label ends at its bar so it is not clipped at the right edge.
      const last = i === points.length - 1;
      chart.append(svg("text", { x: last ? x + barW : x + barW / 2, y: H - 5, "text-anchor": last ? "end" : "middle" }, p.key.slice(5)));
    }
  });
  chart.append(svg("text", { x: side, y: 11 }, max > 0 ? `max ${fmt(max)}` : "No data yet"));
  container.replaceChildren(chart);
}

// The last 30 UTC days (stats-daily keys are UTC dates), oldest first.
function last30Days(items, value) {
  const now = new Date();
  const out = [];
  for (let i = 29; i >= 0; i--) {
    const key = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate() - i))
      .toISOString().slice(0, 10);
    out.push({ key, value: items && items[key] ? value(items[key]) : 0 });
  }
  return out;
}

// Reads a dotted key ("shaper.bandwidth") from GET /config's nested map.
function configValue(data, key) {
  if (data && key in data) return data[key];
  let v = data;
  for (const part of key.split(".")) {
    if (v && typeof v === "object" && part in v) v = v[part];
    else return undefined;
  }
  return v;
}

// ---------------------------------------------------------------- views

function setPill(kind, text) {
  const pill = $("status-pill");
  pill.className = `pill ${kind}`;
  pill.textContent = text;
}

function desiredView() {
  const s = state.status;
  if (!s) return "loading";
  if (!s.terms_accepted) return "terms";
  if (state.tab === "logs") return "tab:logs"; // the log is readable without the node
  if (!state.nodeUp) return "starting";
  if (!state.id) return "identity";
  return `tab:${state.tab}`;
}

function layout() {
  for (const a of document.querySelectorAll("#tabs a")) {
    a.classList.toggle("active", a.dataset.tab === state.tab);
  }
  const view = desiredView();
  if (view === state.shown) return;
  state.shown = view;
  stopTab();
  $("view-loading").hidden = view !== "loading";
  $("view-terms").hidden = view !== "terms";
  $("view-app").hidden = view === "loading" || view === "terms";
  $("view-starting").hidden = view !== "starting";
  $("view-identity").hidden = view !== "identity";
  for (const t of TABS) $(`tab-${t}`).hidden = view !== `tab:${t}`;
  if (view.startsWith("tab:")) openTab(view.slice(4));
}

function route() {
  const h = location.hash.replace(/^#/, "");
  state.tab = TABS.includes(h) ? h : "overview";
  layout();
}

const LOADERS = {
  overview: loadOverview,
  services: loadServices,
  earnings: loadEarnings,
  sessions: loadSessions,
  settings: loadSettings,
  logs: loadLogs,
};

function stopTab() {
  clearTimeout(state.tabTimer);
  state.tabTimer = null;
  state.tabGen++;
}

function openTab(tab) {
  stopTab();
  LOADERS[tab](state.tabGen);
}

// Reloads the shown tab (after an action changed its data).
function refreshTab() {
  if (state.shown === `tab:${state.tab}`) openTab(state.tab);
}

const live = (gen) => gen === state.tabGen;

// Next poll of the shown tab; dropped when the tab changed or the page is hidden.
function schedule(gen, fn, ms) {
  if (!live(gen) || document.hidden) return;
  clearTimeout(state.tabTimer);
  state.tabTimer = setTimeout(() => fn(gen), ms);
}

// GET a TequilAPI path and hand the result to ok (or the error to fail),
// unless the tab was left meanwhile.
async function load(gen, path, ok, fail) {
  let data;
  try {
    data = await tq(path);
  } catch (e) {
    if (live(gen) && fail) fail(e);
    return;
  }
  if (!live(gen)) return;
  try {
    ok(data ?? {});
  } catch (e) {
    if (fail) fail(e); // unexpected response shape
  }
}

// ---------------------------------------------------------------- status / boot

async function pollStatus() {
  if (state.statusBusy) return;
  state.statusBusy = true;
  clearTimeout(state.statusTimer);
  let s = null;
  try { s = await api("/umbrel/api/status"); } catch { s = null; }
  state.statusBusy = false;
  applyStatus(s);
  state.statusTimer = setTimeout(pollStatus, state.nodeUp && state.id ? STATUS_MS : STATUS_BOOT_MS);
}

function kickStatus() {
  if (state.statusBusy) return;
  clearTimeout(state.statusTimer);
  state.statusTimer = setTimeout(pollStatus, 0);
}

function applyStatus(s) {
  if (!s) {
    // Dashboard server unreachable: keep the current view, flag it.
    setPill("offline", "Offline");
    setText("loading-text", "Cannot reach the dashboard; retrying…");
    return;
  }
  state.status = s;
  let up = Boolean(s.terms_accepted && s.node_reachable);
  if (state.restart) {
    if (!up) state.restart.sawDown = true;
    if (up && (state.restart.sawDown || Date.now() - state.restart.since > RESTART_GRACE_MS)) state.restart = null;
    else up = false;
  }
  state.nodeUp = up;
  if (!s.terms_accepted) setPill("offline", "Offline");
  else setPill(up ? "online" : "starting", up ? "Online" : "Starting");
  if (up && !state.id) ensureIdentity();
  layout();
}

async function ensureIdentity() {
  if (state.idBusy || state.id) return;
  state.idBusy = true;
  clearTimeout(state.idTimer);
  try {
    const r = await tq("/identities");
    const id = r?.identities?.[0]?.id;
    if (id) state.id = id;
  } catch {
    // Node went away; the status poll notices.
  } finally {
    state.idBusy = false;
  }
  if (!state.id && state.nodeUp) state.idTimer = setTimeout(ensureIdentity, IDENTITY_RETRY_MS);
  layout();
}

$("terms-check").addEventListener("change", () => {
  $("terms-accept").disabled = !$("terms-check").checked;
});

$("terms-accept").addEventListener("click", (ev) => withBusy(ev.currentTarget, async () => {
  const s = await api("/umbrel/api/accept-terms", { method: "POST", body: { accept: true } });
  applyStatus(s);
}));

// ---------------------------------------------------------------- overview

async function loadOverview(gen) {
  setText("ov-identity", state.id);
  ovNat();
  await Promise.allSettled([
    ovHealth(gen),
    ovMonitoring(gen),
    ovOracle(gen),
    ovIdentity(gen),
    ovRegistration(gen),
    ovMmn(gen),
    ovWallet(gen),
  ]);
  schedule(gen, loadOverview, state.regInProgress ? REGISTRATION_MS : OVERVIEW_MS);
}

function ovNat() {
  if (!state.nat) {
    state.nat = tq("/nat/type").then((r) => NAT_LABELS[r?.type] || r?.type || "–", () => "–");
  }
  state.nat.then((label) => setText("ov-nat", label));
}

function ovHealth(gen) {
  return load(gen, "/healthcheck", (h) => {
    setText("ov-version", h.version || "–");
    setText("ov-uptime", uptime(h.uptime));
  }, () => {
    setText("ov-version", "–");
    setText("ov-uptime", "–");
  });
}

function ovMonitoring(gen) {
  return load(gen, "/node/monitoring-status", (r) => {
    const [text, cls] = MONITORING[r.status] || [r.status || "–", ""];
    setBadge("ov-monitoring", text, cls);
  }, () => setText("ov-monitoring", "–"));
}

// Oracle-backed (quality.mysterium.network); fails offline, never blocks the card.
function ovOracle(gen) {
  return Promise.allSettled([
    load(gen, "/node/provider/quality", (r) => {
      const q = Number(r.quality);
      setText("ov-quality", r.quality === undefined || Number.isNaN(q) ? "–" : q.toFixed(2));
    }, () => setText("ov-quality", "–")),
    load(gen, "/node/provider/activity-stats", (r) => {
      const p = Number(r.online_percent);
      setText("ov-online", r.online_percent === undefined || Number.isNaN(p) ? "–" : `${p.toFixed(1)} %`);
    }, () => setText("ov-online", "–")),
  ]);
}

function ovIdentity(gen) {
  return load(gen, `/identities/${state.id}`, (ident) => {
    state.identity = ident;
    setText("ov-unsettled", mystText(ident.earnings_tokens));
    setText("ov-lifetime", mystText(ident.earnings_total_tokens));
    setText("ov-balance", mystText(ident.balance_tokens));
  }, () => {
    for (const id of ["ov-unsettled", "ov-lifetime", "ov-balance"]) setText(id, "–");
  });
}

function setCheck(id, mark) {
  $(id).className = mark;
}

function ovRegistration(gen) {
  return load(gen, `/identities/${state.id}/registration`, (r) => {
    const s = r.status;
    state.regInProgress = s === "InProgress";
    const canRegister = s === "Unregistered" || s === "RegistrationError";
    $("reg-actions").hidden = !canRegister;
    if (s === "Registered") {
      setCheck("chk-registration", "done");
      setText("reg-text", "Registered");
    } else if (s === "InProgress") {
      setCheck("chk-registration", "busy");
      setText("reg-text", "Registering…");
    } else if (canRegister) {
      setCheck("chk-registration", "todo");
      setText("reg-text", REGISTRATION_TEXT[s]);
      regInfo(gen);
    } else {
      setCheck("chk-registration", "");
      setText("reg-text", "Registration status unknown; the node is checking the blockchain.");
    }
  }, (e) => {
    state.regInProgress = false;
    setCheck("chk-registration", "");
    setText("reg-text", `Status unavailable: ${e.message}`);
    $("reg-actions").hidden = true;
  });
}

async function regInfo(gen) {
  const [elig, fees] = await Promise.allSettled([
    tq(`/identities/${state.id}/eligibility`),
    tq("/transactor/fees"),
  ]);
  if (!live(gen)) return;
  const parts = [];
  if (elig.status === "fulfilled") {
    parts.push(elig.value?.eligible ? "Eligible for free registration." : "Not eligible for free registration.");
  }
  if (fees.status === "fulfilled") parts.push(`Fee: ${mystText(fees.value?.registration_tokens)}`);
  setText("reg-info", parts.join(" "));
}

$("reg-button").addEventListener("click", async (ev) => {
  await withBusy(ev.currentTarget, async () => {
    try {
      await tq(`/identities/${state.id}/register`, { method: "POST", body: {} });
      toast("Registration submitted", "ok");
    } catch (e) {
      if (e.status !== 422) throw e;
      toast("Registration is already in progress.");
    }
  });
  refreshTab();
});

function ovMmn(gen) {
  return load(gen, "/mmn/api-key", (r) => {
    const linked = Boolean(r.api_key);
    setCheck("chk-mmn", linked ? "done" : "todo");
    setText("mmn-text", linked ? "Linked with API key" : "No API key set.");
    $("mmn-remove").hidden = !linked;
  }, (e) => {
    setCheck("chk-mmn", "");
    setText("mmn-text", `Status unavailable: ${e.message}`);
    $("mmn-remove").hidden = true;
  });
}

$("mmn-claim").addEventListener("click", (ev) => {
  // Open the tab synchronously (popup blockers allow it only inside the
  // click), then point it at the signed claim link once it arrives.
  const win = window.open("", "_blank");
  if (win) win.opener = null;
  withBusy(ev.currentTarget, async () => {
    try {
      const redirect = encodeURIComponent(location.origin + "/umbrel/");
      const r = await tq(`/mmn/claim-link?redirect_uri=${redirect}`);
      if (!r?.link) throw new Error("The node returned no claim link.");
      if (win) win.location.href = r.link;
      else window.open(r.link, "_blank");
    } catch (e) {
      if (win) win.close();
      throw e;
    }
  });
});

$("mmn-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const key = $("mmn-key").value.trim();
  if (key.length < 40) {
    showError("mmn-error", "API keys are at least 40 characters long.");
    return;
  }
  showError("mmn-error", "");
  let saved = false;
  await withBusy(ev.currentTarget.querySelector("button"), async () => {
    try {
      await tq("/mmn/api-key", { method: "POST", body: { api_key: key } });
    } catch (e) {
      // 422: "Identity is locked", already claimed, "Invalid API key"; 400: validation.
      if (e.status !== 422 && e.status !== 400) throw e;
      showError("mmn-error", e.message);
      return;
    }
    saved = true;
    $("mmn-key").value = "";
    toast("API key saved; the node is claimed on mystnodes.com.", "ok");
  });
  if (saved) refreshTab();
});

$("mmn-remove").addEventListener("click", async (ev) => {
  if (!confirm("Remove the mystnodes.com API key from this node? The node stays claimed on mystnodes.com until you unclaim it there.")) return;
  await withBusy(ev.currentTarget, async () => {
    await tq("/mmn/api-key", { method: "DELETE" });
    toast("API key removed", "ok");
  });
  refreshTab();
});

function ovWallet(gen) {
  return load(gen, `/identities/${state.id}/beneficiary-async`, (r) => {
    if (r.address) {
      setCheck("chk-wallet", "done");
      $("wallet-text").replaceChildren(el("code", { title: r.address }, r.address));
    } else {
      setCheck("chk-wallet", "todo");
      $("wallet-text").replaceChildren("Not set. ", el("a", { href: "#earnings" }, "Set a payout wallet"));
    }
  }, (e) => {
    setCheck("chk-wallet", "");
    setText("wallet-text", `Status unavailable: ${e.message}`);
  });
}

$("ov-identity-copy").addEventListener("click", () => {
  if (state.id) copy(state.id);
});

// ---------------------------------------------------------------- services

async function loadServices(gen) {
  await load(gen, "/services?include_all=true", (list) => {
    showError("svc-error", "");
    state.services = Array.isArray(list) ? list : [];
    renderServices();
  }, (e) => {
    showError("svc-error", `Could not load the services: ${e.message}`);
    renderServices();
  });
  schedule(gen, loadServices, SERVICES_MS);
}

function renderServices() {
  $("svc-list").replaceChildren(...SERVICES.map(([type, label]) => {
    const entries = state.services.filter((s) => s.type === type);
    const svc = entries.find((s) => s.id) || entries[0] || { type, status: "NotRunning" };
    const status = svc.status || "NotRunning";
    const pending = state.svcPending.has(type);
    const on = pending ? state.svcPending.get(type) : status === "Running" || status === "Starting";
    const badge = status === "Running" ? el("span", { class: "badge running" }, "Running")
      : status === "Starting" ? el("span", { class: "badge initializing" }, "Starting")
        : el("span", { class: "badge" }, "Off");
    return el("li", {},
      el("div", { class: "grow" },
        el("div", { class: "name" }, label),
        el("div", { class: "muted" }, type)),
      badge,
      el("label", { class: "switch", title: on ? `Stop ${label}` : `Start ${label}` },
        el("input", {
          type: "checkbox",
          checked: on,
          disabled: pending || status === "Starting",
          "aria-label": label,
          onchange: (ev) => toggleService(type, svc, ev.currentTarget.checked),
        }),
        el("span")));
  }));
}

async function toggleService(type, svc, on) {
  if (state.svcPending.has(type)) return;
  state.svcPending.set(type, on);
  renderServices();
  try {
    if (on) {
      await tq("/services", { method: "POST", body: { provider_id: state.id, type } });
    } else {
      if (!svc.id) throw new Error("This service is not running.");
      await tq(`/services/${encodeURIComponent(svc.id)}`, { method: "DELETE" });
    }
  } catch (e) {
    toast(e.message, "error");
  } finally {
    state.svcPending.delete(type);
  }
  renderServices();
  if (state.shown === "tab:services") refreshTab();
}

// ---------------------------------------------------------------- earnings

async function loadEarnings(gen) {
  $("er-ben-saved").hidden = true;
  await Promise.allSettled([
    erBalances(gen),
    erServiceEarnings(gen),
    erChart(gen),
    erBeneficiary(gen),
    erHistory(gen),
  ]);
}

async function erBalances(gen) {
  const [identR, feesR] = await Promise.allSettled([tq(`/identities/${state.id}`), tq("/transactor/fees")]);
  if (!live(gen)) return;
  const ident = identR.status === "fulfilled" ? identR.value : null;
  const fees = feesR.status === "fulfilled" ? feesR.value : null;
  state.identity = ident;
  setText("er-unsettled", mystText(ident?.earnings_tokens));
  setText("er-lifetime", mystText(ident?.earnings_total_tokens));
  setText("er-balance", mystText(ident?.balance_tokens));
  setText("er-fee", mystText(fees?.settlement_tokens));
  let canSettle = false;
  try {
    canSettle = Boolean(ident?.hermes_id)
      && BigInt(ident.earnings_tokens.wei) > BigInt(fees.settlement_tokens.wei);
  } catch {
    canSettle = false; // missing or unparsable amounts
  }
  $("er-settle").disabled = !canSettle;
}

// Oracle-backed; "–" offline. The `total_*` fields of this response are
// network-wide totals (tequilapi/endpoints/node.go), not this node's.
function erServiceEarnings(gen) {
  const fields = {
    "er-public": "public_tokens",
    "er-data-transfer": "data_transfer_tokens",
    "er-scraping": "scraping_tokens",
    "er-dvpn": "dvpn_tokens",
  };
  return load(gen, "/node/provider/service-earnings", (r) => {
    for (const [id, key] of Object.entries(fields)) setText(id, mystText(r[key]));
  }, () => {
    for (const id of Object.keys(fields)) setText(id, "–");
  });
}

function erChart(gen) {
  return load(gen, "/sessions/stats-daily?direction=Provided", (r) => {
    const points = last30Days(r.items, (d) => Number(d.sum_tokens) / 1e18 || 0);
    barChart($("er-chart"), points, (v) => `${v.toFixed(v < 0.01 ? 6 : 4)} MYST`, "Daily earnings, last 30 days");
  }, (e) => {
    $("er-chart").replaceChildren(el("p", { class: "muted" }, `Could not load daily stats: ${e.message}`));
  });
}

function erBeneficiary(gen) {
  const id = state.id;
  return Promise.allSettled([
    load(gen, `/identities/${id}/beneficiary-async`, (r) => {
      setText("er-ben-stored", r.address || "Not set");
    }, () => setText("er-ben-stored", "–")),
    load(gen, `/identities/${id}/beneficiary`, (r) => {
      const unset = r.is_channel_address || !r.beneficiary || /^0x0{40}$/i.test(r.beneficiary);
      setText("er-ben-chain", unset ? "not set on-chain" : r.beneficiary);
    }, () => setText("er-ben-chain", "–")),
    load(gen, `/identities/${id}/beneficiary-status`, (r) => {
      const visible = r.state === "pending" || r.state === "completed";
      $("er-ben-status-label").hidden = !visible;
      $("er-ben-status").hidden = !visible;
      if (!visible) return;
      let text = r.state === "pending" ? `Pending change to ${r.change_to}` : `Changed to ${r.change_to}`;
      if (r.error) text += ` — ${r.error}`;
      setText("er-ben-status", text);
    }, () => {
      $("er-ben-status-label").hidden = true;
      $("er-ben-status").hidden = true;
    }),
  ]);
}

function erHistory(gen) {
  return load(gen, `/transactor/settle/history?provider_id=${state.id}&page_size=10`, (r) => {
    const items = r.items || [];
    if (!items.length) {
      $("er-history").replaceChildren(el("tr", {}, el("td", { class: "empty", colSpan: 6 }, "No settlements yet.")));
      return;
    }
    $("er-history").replaceChildren(...items.map((it) => el("tr", {},
      el("td", {}, dateTime(it.settled_at)),
      el("td", { class: "num" }, it.amount === null || it.amount === undefined ? "failed" : weiToMyst(it.amount)),
      el("td", { class: "num" }, weiToMyst(it.fees)),
      el("td", { title: it.beneficiary || "" }, el("code", {}, shortAddr(it.beneficiary))),
      el("td", {}, it.block_explorer_url
        ? el("a", { href: it.block_explorer_url, target: "_blank", rel: "noopener noreferrer" }, "View")
        : "–"),
      el("td", { class: "wrap" }, it.error || ""))));
  }, (e) => {
    $("er-history").replaceChildren(el("tr", {}, el("td", { class: "empty", colSpan: 6 }, `Could not load the history: ${e.message}`)));
  });
}

$("er-settle").addEventListener("click", async (ev) => {
  const ident = state.identity;
  if (!ident?.hermes_id) return;
  await withBusy(ev.currentTarget, async () => {
    await tq("/transactor/settle/async", {
      method: "POST",
      body: { provider_id: state.id, hermes_ids: [ident.hermes_id] },
    });
    toast("Settlement queued", "ok");
  });
  refreshTab();
});

$("er-ben-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const address = $("er-ben-input").value.trim();
  if (!WALLET_RE.test(address)) {
    showError("er-ben-error", "Enter a wallet address: 0x followed by 40 hexadecimal characters.");
    return;
  }
  showError("er-ben-error", "");
  let saved = false;
  await withBusy(ev.currentTarget.querySelector("button"), async () => {
    await tq(`/identities/${state.id}/beneficiary-async`, { method: "POST", body: { address } });
    saved = true;
  });
  if (!saved) return;
  $("er-ben-input").value = "";
  $("er-ben-saved").hidden = false;
  erBeneficiary(state.tabGen);
});

// ---------------------------------------------------------------- sessions

async function loadSessions(gen) {
  state.sessionsPage = 1;
  await Promise.allSettled([ssAggregate(gen), ssChart(gen), ssTable(gen)]);
}

function ssAggregate(gen) {
  const ids = ["ss-count", "ss-consumers", "ss-sent", "ss-received", "ss-duration", "ss-tokens"];
  return load(gen, "/sessions/stats-aggregated?direction=Provided", (r) => {
    const s = r.stats || {};
    setText("ss-count", String(s.count ?? "–"));
    setText("ss-consumers", String(s.count_consumers ?? "–"));
    setText("ss-sent", bytes(s.sum_bytes_sent));
    setText("ss-received", bytes(s.sum_bytes_received));
    setText("ss-duration", hoursMinutes(s.sum_duration));
    setText("ss-tokens", `${weiToMyst(s.sum_tokens)} MYST`);
  }, () => {
    for (const id of ids) setText(id, "–");
  });
}

function ssChart(gen) {
  return load(gen, "/sessions/stats-daily?direction=Provided", (r) => {
    const points = last30Days(r.items, (d) => (Number(d.sum_bytes_sent) || 0) + (Number(d.sum_bytes_received) || 0));
    barChart($("ss-chart"), points, bytes, "Daily traffic, last 30 days");
  }, (e) => {
    $("ss-chart").replaceChildren(el("p", { class: "muted" }, `Could not load daily stats: ${e.message}`));
  });
}

function ssTable(gen) {
  const page = state.sessionsPage;
  return load(gen, `/sessions?direction=Provided&page=${page}&page_size=20`, (r) => {
    const items = r.items || [];
    const total = Number(r.total_pages) || 0;
    state.sessionsPage = Number(r.page) || page;
    if (!items.length) {
      $("ss-table").replaceChildren(el("tr", {}, el("td", { class: "empty", colSpan: 7 }, "No sessions yet.")));
    } else {
      $("ss-table").replaceChildren(...items.map((it) => el("tr", {},
        el("td", {}, dateTime(it.created_at)),
        el("td", { title: it.service_type || "" }, SERVICE_LABELS[it.service_type] || it.service_type || "–"),
        el("td", {}, it.consumer_country || "–"),
        el("td", { class: "num" }, hoursMinutes(it.duration)),
        el("td", { class: "num" }, bytes((Number(it.bytes_sent) || 0) + (Number(it.bytes_received) || 0))),
        el("td", { class: "num" }, weiToMyst(it.tokens)),
        el("td", {}, it.status || "–"))));
    }
    setText("ss-page", total ? `Page ${state.sessionsPage} of ${total}` : "");
    $("ss-prev").disabled = state.sessionsPage <= 1;
    $("ss-next").disabled = state.sessionsPage >= total;
  }, (e) => {
    $("ss-table").replaceChildren(el("tr", {}, el("td", { class: "empty", colSpan: 7 }, `Could not load sessions: ${e.message}`)));
    $("ss-prev").disabled = true;
    $("ss-next").disabled = true;
  });
}

$("ss-prev").addEventListener("click", () => {
  if (state.sessionsPage <= 1) return;
  state.sessionsPage--;
  ssTable(state.tabGen);
});

$("ss-next").addEventListener("click", () => {
  state.sessionsPage++;
  ssTable(state.tabGen);
});

// ---------------------------------------------------------------- settings

async function loadSettings(gen) {
  $("st-saved").hidden = true;
  showError("st-error", "");
  showError("st-udp-ports-error", "");
  showError("st-shaper-bandwidth-error", "");
  await load(gen, "/config", (r) => {
    const data = r?.data || {};
    const level = String(configValue(data, "log-level") ?? "debug").toLowerCase();
    $("st-log-level").value = LOG_LEVELS.includes(level) ? level : "debug";
    $("st-udp-ports").value = String(configValue(data, "udp.ports") ?? "10000:60000");
    const shaper = configValue(data, "shaper.enabled");
    $("st-shaper-enabled").checked = shaper === true || shaper === "true";
    $("st-shaper-bandwidth").value = String(configValue(data, "shaper.bandwidth") ?? 6250);
  }, (e) => showError("st-error", `Could not load the settings: ${e.message}`));
}

// Returns the POST /config/user data, or null after showing field errors.
function settingsData() {
  const ports = $("st-udp-ports").value.trim();
  const m = /^(\d+):(\d+)$/.exec(ports);
  const portsOk = Boolean(m) && Number(m[1]) >= 1024 && Number(m[1]) < Number(m[2]) && Number(m[2]) <= 65535;
  showError("st-udp-ports-error", portsOk ? "" : "Use FROM:TO with 1024 ≤ FROM < TO ≤ 65535, e.g. 10000:60000.");
  const bandwidth = $("st-shaper-bandwidth").value.trim();
  const bandwidthOk = /^\d+$/.test(bandwidth) && Number(bandwidth) >= 1;
  showError("st-shaper-bandwidth-error", bandwidthOk ? "" : "Enter a whole number of KB/s, at least 1.");
  if (!portsOk || !bandwidthOk) return null;
  return {
    "log-level": $("st-log-level").value,
    "udp.ports": ports,
    "shaper.enabled": $("st-shaper-enabled").checked,
    "shaper.bandwidth": Number(bandwidth),
  };
}

$("st-form").addEventListener("submit", (ev) => {
  ev.preventDefault();
  $("st-saved").hidden = true;
  showError("st-error", "");
  const data = settingsData();
  if (!data) return;
  withBusy($("st-save"), async () => {
    await tq("/config/user", { method: "POST", body: { data } });
    $("st-saved").hidden = false;
  });
});

$("st-form").addEventListener("input", () => { $("st-saved").hidden = true; });

$("st-restart").addEventListener("click", (ev) => {
  if (!confirm("Restart the Mysterium node? It is unavailable for up to a minute and running sessions are dropped.")) return;
  withBusy(ev.currentTarget, async () => {
    await tq("/stop", { method: "POST" });
    state.restart = { since: Date.now(), sawDown: false };
    state.nodeUp = false;
    setPill("starting", "Starting");
    layout();
    kickStatus();
  });
});

// ---------------------------------------------------------------- logs

async function loadLogs(gen) {
  const lines = $("lg-lines").value;
  const level = $("lg-level").value;
  let r;
  try {
    r = await api(`/umbrel/api/logs?lines=${encodeURIComponent(lines)}&level=${encodeURIComponent(level)}`);
  } catch (e) {
    if (live(gen)) setText("lg-meta", `Could not load the log: ${e.message}`);
    if ($("lg-auto").checked) schedule(gen, loadLogs, LOGS_MS);
    return;
  }
  if (!live(gen)) return;
  const pre = $("lg-text");
  // Stay pinned to the bottom unless the user scrolled up to read.
  const pinned = state.logsPin || pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 24;
  state.logsPin = false;
  let text;
  if (!r.exists) {
    text = "No log file yet";
    setText("lg-meta", "");
  } else {
    text = r.lines.length ? r.lines.join("\n") : "(no lines at this level)";
    setText("lg-meta", `${r.file} · ${bytes(r.size)} · last ${r.lines.length} lines`);
  }
  // Unchanged text is left alone so a text selection survives auto-refresh.
  if (pre.textContent !== text) pre.textContent = text;
  if (pinned) pre.scrollTop = pre.scrollHeight;
  if ($("lg-auto").checked) schedule(gen, loadLogs, LOGS_MS);
}

function reloadLogs() {
  state.logsPin = true;
  refreshTab();
}

$("lg-lines").addEventListener("change", reloadLogs);
$("lg-level").addEventListener("change", reloadLogs);
$("lg-refresh").addEventListener("click", reloadLogs);
$("lg-auto").addEventListener("change", () => {
  if ($("lg-auto").checked) refreshTab();
  else clearTimeout(state.tabTimer);
});

// ---------------------------------------------------------------- boot

// Leaving a tab and switching the browser tab away both stop polling.
document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    clearTimeout(state.tabTimer);
    return;
  }
  kickStatus();
  if (POLLING_TABS.has(state.tab)) refreshTab();
});

window.addEventListener("hashchange", route);
route();
pollStatus();
