"use strict";

/* ═══════════════════════════════════════════════════════════════
   CNC Machine Monitor — Multi-Camera Frontend
   Channels are loaded dynamically from /api/cameras — no
   hardcoded channel list. Works with any camera selection.
═══════════════════════════════════════════════════════════════ */

const API_BASE   = "";
const REFRESH_MS = 2000;

let knownChannels = [];   // populated on first API response
let prevTotals    = {};
let alertCount    = 0;
let seenAlerts    = new Set();

// ─── Init ─────────────────────────────────────────────────────────────────────
document.addEventListener("DOMContentLoaded", () => {
  startClock();
  tick();
  setInterval(tick, REFRESH_MS);
});

// ─── Clock ────────────────────────────────────────────────────────────────────
function startClock() {
  const clockEl = document.getElementById("live-clock");
  const dateEl  = document.getElementById("live-date");
  function update() {
    const now = new Date();
    clockEl.textContent = now.toLocaleTimeString("en-GB", { hour12: false });
    dateEl.textContent  = now.toLocaleDateString("en-GB", {
      weekday: "short", day: "2-digit", month: "short", year: "numeric",
    });
  }
  update();
  setInterval(update, 1000);
}

// ─── Main Poll Tick ───────────────────────────────────────────────────────────
async function tick() {
  try {
    const res = await fetch(`${API_BASE}/api/cameras`, { cache: "no-store" });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    renderDashboard(data);
  } catch (err) {
    console.warn("Poll failed:", err);
    document.getElementById("last-refresh").textContent = "Connection error — retrying…";
    updateCamStatus(0, knownChannels.length || "?");
  }
}

// ─── Render Dashboard ─────────────────────────────────────────────────────────
function renderDashboard(data) {
  const cams    = data.cameras || {};
  const totals  = data.totals  || {};
  const connCnt = data.connected_cameras || 0;
  const total   = (totals.working || 0) + (totals.idle || 0) + (totals.manual_stop || 0) + (totals.process_finish || 0);
  const effPct  = total > 0 ? Math.round(((totals.working || 0) + (totals.process_finish || 0)) / total * 100) : 0;

  // Discover channels from API response (dynamic — no hardcoding)
  const apiChannels = Object.keys(cams).map(Number).sort((a, b) => a - b);

  // If channel list changed (setup was reconfigured), rebuild tiles
  const channelListChanged =
    apiChannels.length !== knownChannels.length ||
    apiChannels.some((ch, i) => ch !== knownChannels[i]);

  if (channelListChanged) {
    knownChannels = apiChannels;
    buildCameraTiles(cams);
  }

  // KPI bar
  setText("sum-working",    totals.working        || 0);
  setText("sum-idle",       totals.idle           || 0);
  setText("sum-stop",       totals.manual_stop    || 0);
  setText("sum-finish",     totals.process_finish || 0);
  setText("sum-efficiency", total > 0 ? effPct + "%" : "—");
  setText("sum-cams",       `${connCnt}/${data.total_cameras || knownChannels.length}`);

  // Flash KPI on change
  ["sum-working","sum-idle","sum-stop"].forEach(id => {
    const el = document.getElementById(id);
    if (el && el.textContent !== String(prevTotals[id] ?? "")) flashElement(el);
  });
  prevTotals = {
    "sum-working": totals.working,
    "sum-idle":    totals.idle,
    "sum-stop":    totals.manual_stop,
  };

  updateCamStatus(connCnt, data.total_cameras || knownChannels.length);

  // Update each camera tile
  knownChannels.forEach(ch => updateCameraTile(ch, cams[String(ch)]));

  // Per-camera counts sidebar
  renderPerCamCounts(cams);

  // Alert detection
  detectAlerts(cams);

  setText("last-refresh", `Updated ${fmtTime(data.timestamp)}`);
}

// ─── Build Camera Tile Grid ───────────────────────────────────────────────────
function buildCameraTiles(cams) {
  const grid = document.getElementById("camera-grid");
  if (!grid) return;
  grid.innerHTML = "";

  if (knownChannels.length === 0) {
    grid.innerHTML = `<div style="color:#64748b;font-size:13px;padding:40px;grid-column:1/-1;text-align:center">
      No cameras configured. <a href="/setup" style="color:#f59e0b">Go to Setup →</a>
    </div>`;
    return;
  }

  knownChannels.forEach(ch => {
    const camData = cams ? cams[String(ch)] : null;
    grid.insertAdjacentHTML("beforeend", cameraTileHTML(ch, camData));
  });
}

function cameraTileHTML(ch, camData) {
  const name      = camData ? escHtml(camData.name) : `Channel ${ch}`;
  const connected = camData ? camData.connected : false;
  const working   = camData ? (camData.working        || 0) : 0;
  const idle      = camData ? (camData.idle           || 0) : 0;
  const stopped   = camData ? (camData.manual_stop    || 0) : 0;
  const finished  = camData ? (camData.process_finish || 0) : 0;
  const calBadge  = camData && camData.calibrated
    ? `<span class="tile-cal-badge cal-on"  title="${camData.anchor_count} light(s) tracked">CAL</span>`
    : `<span class="tile-cal-badge cal-off" title="Not calibrated — go to /calibrate">UNCAL</span>`;

  return `
<div class="camera-tile ${connected ? "cam-tile-online" : "cam-tile-offline"}" id="tile-ch${ch}">
  <div class="tile-header">
    <span class="tile-dot ${connected ? "dot-online" : "dot-offline"}"></span>
    <span class="tile-name" id="tilename-ch${ch}">${name}</span>
    <span id="tile-cal-${ch}">${calBadge}</span>
    <span class="tile-ch">CH ${ch}</span>
  </div>
  <div class="tile-feed-wrap">
    <img
      class="tile-feed"
      id="feed-ch${ch}"
      src="/api/stream/${ch}"
      alt="${name}"
      onerror="handleFeedError(this, ${ch})"
    />
    <div class="tile-counts-overlay">
      <span class="count-pill pill-working" id="cnt-w-${ch}">${working}</span>
      <span class="count-pill pill-idle"    id="cnt-i-${ch}">${idle}</span>
      <span class="count-pill pill-stop"    id="cnt-s-${ch}">${stopped}</span>
      <span class="count-pill pill-finish"  id="cnt-f-${ch}">${finished}</span>
    </div>
  </div>
</div>`;
}

function handleFeedError(img, ch) {
  img.src = "";
  img.alt = "Camera Offline";
}

// ─── Update One Camera Tile ───────────────────────────────────────────────────
function updateCameraTile(ch, camData) {
  const tile = document.getElementById(`tile-ch${ch}`);
  if (!tile) return;

  const connected = camData && camData.connected;
  tile.className  = `camera-tile ${connected ? "cam-tile-online" : "cam-tile-offline"}`;

  const dot = tile.querySelector(".tile-dot");
  if (dot) dot.className = `tile-dot ${connected ? "dot-online" : "dot-offline"}`;

  const nameEl = document.getElementById(`tilename-ch${ch}`);
  if (nameEl && camData) nameEl.textContent = camData.name || `Channel ${ch}`;

  const calWrap = document.getElementById(`tile-cal-${ch}`);
  if (calWrap && camData) {
    calWrap.innerHTML = camData.calibrated
      ? `<span class="tile-cal-badge cal-on"  title="${camData.anchor_count} light(s) tracked">CAL</span>`
      : `<span class="tile-cal-badge cal-off" title="Not calibrated — go to /calibrate">UNCAL</span>`;
  }

  setText(`cnt-w-${ch}`, camData ? (camData.working        || 0) : 0);
  setText(`cnt-i-${ch}`, camData ? (camData.idle           || 0) : 0);
  setText(`cnt-s-${ch}`, camData ? (camData.manual_stop    || 0) : 0);
  setText(`cnt-f-${ch}`, camData ? (camData.process_finish || 0) : 0);
}

// ─── Per-Camera Counts Sidebar ────────────────────────────────────────────────
function renderPerCamCounts(cams) {
  const el = document.getElementById("per-cam-counts");
  if (!el) return;

  if (knownChannels.length === 0) {
    el.innerHTML = `<div class="text-muted text-center py-3 small">No cameras selected</div>`;
    return;
  }

  let html = "";
  knownChannels.forEach(ch => {
    const c         = cams[String(ch)];
    const name      = c ? escHtml(c.name) : `Channel ${ch}`;
    const connected = c && c.connected;
    const w = c ? (c.working || 0)     : 0;
    const i = c ? (c.idle || 0)        : 0;
    const s = c ? (c.manual_stop || 0) : 0;

    const f = c ? (c.process_finish || 0) : 0;
    html += `
<div class="per-cam-row ${connected ? "" : "per-cam-offline"}">
  <div class="per-cam-name">
    <span class="tile-dot ${connected ? "dot-online" : "dot-offline"}" style="margin-right:6px"></span>
    ${name}
  </div>
  <div class="per-cam-pills">
    <span class="mini-pill pill-working">${w} <span class="pill-label">work</span></span>
    <span class="mini-pill pill-idle">${i} <span class="pill-label">idle</span></span>
    <span class="mini-pill pill-stop">${s} <span class="pill-label">stop</span></span>
    <span class="mini-pill pill-finish">${f} <span class="pill-label">fin</span></span>
  </div>
</div>`;
  });
  el.innerHTML = html;
}

// ─── Alerts ───────────────────────────────────────────────────────────────────
function detectAlerts(cams) {
  knownChannels.forEach(ch => {
    const c = cams[String(ch)];
    if (!c || !c.manual_stop) return;
    const key = `stop-${ch}-${Math.floor(Date.now() / 30000)}`;
    if (!seenAlerts.has(key)) {
      seenAlerts.add(key);
      addAlert(`${c.name}: ${c.manual_stop} machine(s) in Manual Stop`, "high");
      showToast(`${c.name} — Manual Stop detected!`, "danger");
    }
  });
}

function addAlert(msg, severity) {
  const log   = document.getElementById("alert-log");
  const badge = document.getElementById("alert-badge");
  if (!log) return;
  const noMsg = log.querySelector(".no-alerts-msg");
  if (noMsg) noMsg.remove();
  alertCount++;
  if (badge) badge.textContent = alertCount;
  const div = document.createElement("div");
  div.className = `alert-item alert-severity-${severity}`;
  div.innerHTML = `
    <i class="fas fa-exclamation-circle alert-icon"></i>
    <div class="alert-content">
      <div class="alert-msg">${escHtml(msg)}</div>
      <div class="alert-time">${fmtTime(new Date().toISOString())}</div>
    </div>`;
  log.prepend(div);
  const items = log.querySelectorAll(".alert-item");
  items.forEach((el, i) => { if (i >= 50) el.remove(); });
}

function clearAlerts() {
  const log   = document.getElementById("alert-log");
  const badge = document.getElementById("alert-badge");
  if (log) log.innerHTML = `<div class="no-alerts-msg"><i class="fas fa-check-circle text-success me-2"></i>Cleared</div>`;
  alertCount = 0;
  seenAlerts.clear();
  if (badge) badge.textContent = "0";
}

// ─── Camera Status Dot ────────────────────────────────────────────────────────
function updateCamStatus(connected, total) {
  const dot   = document.getElementById("cam-dot");
  const label = document.getElementById("cam-label");
  if (!dot) return;
  dot.className     = connected > 0 ? "cam-dot cam-online" : "cam-dot cam-offline";
  label.textContent = `${connected}/${total} Cams`;
}

// ─── Toast ────────────────────────────────────────────────────────────────────
function showToast(msg, type) {
  const container = document.getElementById("toast-container");
  if (!container) return;
  const colorMap = { danger:"#ef4444", warning:"#f59e0b", success:"#22c55e", info:"#38bdf8" };
  const iconMap  = { danger:"fa-exclamation-triangle", warning:"fa-exclamation-circle",
                     success:"fa-check-circle", info:"fa-info-circle" };
  const el = document.createElement("div");
  el.className = "toast-notif";
  el.style.borderColor = colorMap[type] || "#64748b";
  el.innerHTML = `<i class="fas ${iconMap[type]||"fa-bell"}" style="color:${colorMap[type]};font-size:16px"></i>
                  <span>${escHtml(msg)}</span>`;
  container.appendChild(el);
  setTimeout(() => { el.classList.add("toast-exit"); setTimeout(() => el.remove(), 400); }, 4500);
}

// ─── Helpers ──────────────────────────────────────────────────────────────────
function setText(id, val) {
  const el = document.getElementById(id);
  if (el) el.textContent = val;
}
function fmtTime(iso) {
  if (!iso) return "—";
  try { return new Date(iso).toLocaleTimeString("en-GB", { hour12: false }); }
  catch (_) { return iso; }
}
function flashElement(el) {
  el.style.transition = "opacity 0.15s";
  el.style.opacity    = "0.4";
  setTimeout(() => { el.style.opacity = "1"; }, 200);
}
function escHtml(str) {
  const d = document.createElement("div");
  d.appendChild(document.createTextNode(String(str)));
  return d.innerHTML;
}
