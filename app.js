"use strict";

/* ═══════════════════════════════════════════════════════════════
   CNC Machine Monitor — Multi-Camera Frontend
   Polls /api/cameras every 2 s and renders 4 live camera tiles
   with per-camera working/idle/stop counts.
═══════════════════════════════════════════════════════════════ */

const API_BASE    = "";
const REFRESH_MS  = 2000;

// Known CNC camera channels (must match backend CNC_CAMERAS)
const CNC_CHANNELS = [6, 7, 8, 9];

const STATE_COLORS = {
  working:     "#22c55e",
  idle:        "#f59e0b",
  manual_stop: "#ef4444",
};

let prevTotals  = {};
let alertCount  = 0;
let seenAlerts  = new Set();
let tilesBuilt  = false;

// ─── Init ─────────────────────────────────────────────────────────────────────
document.addEventListener("DOMContentLoaded", () => {
  startClock();
  buildCameraTiles();
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

// ─── Build Camera Tile Grid ───────────────────────────────────────────────────
function buildCameraTiles() {
  const grid = document.getElementById("camera-grid");
  if (!grid) return;
  grid.innerHTML = "";
  CNC_CHANNELS.forEach(ch => {
    grid.insertAdjacentHTML("beforeend", cameraTileHTML(ch, null));
  });
  tilesBuilt = true;
}

function cameraTileHTML(ch, camData) {
  const name      = camData ? escHtml(camData.name) : `Channel ${ch}`;
  const connected = camData ? camData.connected : false;
  const working   = camData ? (camData.working || 0)     : 0;
  const idle      = camData ? (camData.idle || 0)        : 0;
  const stopped   = camData ? (camData.manual_stop || 0) : 0;
  const statusCls = connected ? "cam-tile-online" : "cam-tile-offline";
  const dotCls    = connected ? "dot-online"       : "dot-offline";

  return `
<div class="camera-tile ${statusCls}" id="tile-ch${ch}">
  <div class="tile-header">
    <span class="tile-dot ${dotCls}"></span>
    <span class="tile-name">${name}</span>
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
    <div class="tile-counts-overlay" id="overlay-ch${ch}">
      <span class="count-pill pill-working"  id="cnt-w-${ch}">
        <i class="fas fa-circle" style="font-size:8px"></i> ${working}
      </span>
      <span class="count-pill pill-idle"     id="cnt-i-${ch}">
        <i class="fas fa-circle" style="font-size:8px"></i> ${idle}
      </span>
      <span class="count-pill pill-stop"     id="cnt-s-${ch}">
        <i class="fas fa-circle" style="font-size:8px"></i> ${stopped}
      </span>
    </div>
  </div>
</div>`;
}

function handleFeedError(img, ch) {
  img.src = "";
  img.alt = "Camera Offline";
  const tile = document.getElementById(`tile-ch${ch}`);
  if (tile) tile.classList.add("cam-tile-offline");
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
    updateCamStatus(0, Object.keys(CNC_CHANNELS).length);
  }
}

// ─── Render Dashboard ─────────────────────────────────────────────────────────
function renderDashboard(data) {
  const cams    = data.cameras || {};
  const totals  = data.totals  || {};
  const connCnt = data.connected_cameras || 0;
  const total   = totals.working + totals.idle + totals.manual_stop;
  const effPct  = total > 0 ? Math.round(totals.working / total * 100) : 0;

  // KPI bar
  setText("sum-working",    totals.working     || 0);
  setText("sum-idle",       totals.idle        || 0);
  setText("sum-stop",       totals.manual_stop || 0);
  setText("sum-efficiency", total > 0 ? effPct + "%" : "—");
  setText("sum-cams",       `${connCnt}/${data.total_cameras || CNC_CHANNELS.length}`);

  // Flash KPI if changed
  ["sum-working","sum-idle","sum-stop"].forEach(id => {
    const el = document.getElementById(id);
    if (el && el.textContent !== String(prevTotals[id] || "")) flashElement(el);
  });
  prevTotals = { "sum-working": totals.working, "sum-idle": totals.idle, "sum-stop": totals.manual_stop };

  // Camera status dot
  updateCamStatus(connCnt, data.total_cameras || CNC_CHANNELS.length);

  // Update camera tiles
  CNC_CHANNELS.forEach(ch => updateCameraTile(ch, cams[String(ch)]));

  // Per-camera counts sidebar
  renderPerCamCounts(cams);

  // Alerts: detect manual stops from per-camera data
  detectAlerts(cams);

  setText("last-refresh", `Updated ${fmtTime(data.timestamp)}`);
}

// ─── Update One Camera Tile ───────────────────────────────────────────────────
function updateCameraTile(ch, camData) {
  const tile = document.getElementById(`tile-ch${ch}`);
  if (!tile) return;

  const connected = camData && camData.connected;
  const working   = camData ? (camData.working || 0)     : 0;
  const idle      = camData ? (camData.idle || 0)        : 0;
  const stopped   = camData ? (camData.manual_stop || 0) : 0;
  const name      = camData ? camData.name : `Channel ${ch}`;

  // Update connection class
  tile.className = `camera-tile ${connected ? "cam-tile-online" : "cam-tile-offline"}`;

  // Update dot
  const dot = tile.querySelector(".tile-dot");
  if (dot) dot.className = `tile-dot ${connected ? "dot-online" : "dot-offline"}`;

  // Update name
  const nameEl = tile.querySelector(".tile-name");
  if (nameEl) nameEl.textContent = name;

  // Update count pills
  setText(`cnt-w-${ch}`, working);
  setText(`cnt-i-${ch}`, idle);
  setText(`cnt-s-${ch}`, stopped);

  // Refresh feed src periodically so it reconnects after camera offline
  // The MJPEG src is already streaming; no need to reset unless error occurred.
}

// ─── Per-Camera Counts Sidebar ────────────────────────────────────────────────
function renderPerCamCounts(cams) {
  const el = document.getElementById("per-cam-counts");
  if (!el) return;
  let html = "";
  CNC_CHANNELS.forEach(ch => {
    const c = cams[String(ch)];
    const name      = c ? escHtml(c.name) : `Channel ${ch}`;
    const connected = c && c.connected;
    const w = c ? (c.working || 0)     : 0;
    const i = c ? (c.idle || 0)        : 0;
    const s = c ? (c.manual_stop || 0) : 0;

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
  </div>
</div>`;
  });
  el.innerHTML = html || `<div class="text-muted text-center py-3 small">No cameras</div>`;
}

// ─── Alerts ───────────────────────────────────────────────────────────────────
function detectAlerts(cams) {
  CNC_CHANNELS.forEach(ch => {
    const c = cams[String(ch)];
    if (!c) return;
    if (c.manual_stop > 0) {
      const key = `stop-${ch}-${Math.floor(Date.now() / 30000)}`; // dedupe per 30s window
      if (!seenAlerts.has(key)) {
        seenAlerts.add(key);
        addAlert(`${c.name}: ${c.manual_stop} machine(s) in Manual Stop`, "high");
        showToast(`${c.name} — Manual Stop detected!`, "danger");
      }
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

  // Cap at 50 entries
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
  if (connected > 0) {
    dot.className   = "cam-dot cam-online";
    label.textContent = `${connected}/${total} Cams`;
  } else {
    dot.className   = "cam-dot cam-offline";
    label.textContent = "No Cams";
  }
}

// ─── Toast ────────────────────────────────────────────────────────────────────
function showToast(msg, type) {
  const container = document.getElementById("toast-container");
  if (!container) return;
  const colorMap = { danger: "#ef4444", warning: "#f59e0b", success: "#22c55e", info: "#38bdf8" };
  const iconMap  = { danger: "fa-exclamation-triangle", warning: "fa-exclamation-circle",
                     success: "fa-check-circle", info: "fa-info-circle" };
  const el = document.createElement("div");
  el.className = "toast-notif";
  el.style.borderColor = colorMap[type] || "#64748b";
  el.innerHTML = `<i class="fas ${iconMap[type] || "fa-bell"}" style="color:${colorMap[type]};font-size:16px"></i>
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
