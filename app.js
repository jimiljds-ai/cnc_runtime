"use strict";

/* ═══════════════════════════════════════════════════════════════
   CNC Machine Monitor — Frontend Live Logic
   Polls /api/status every 2 s and refreshes all dashboard elements
═══════════════════════════════════════════════════════════════ */

const API_BASE = "";
const REFRESH_MS = 2000;
const TOTAL_MACHINES = 10;
const SHIFT_HOURS = 8;

// State colours mapped to CSS class suffixes and hex
const STATE_META = {
  working:     { label: "Working",     icon: "fa-cog fa-spin",          hex: "#22c55e" },
  idle:        { label: "Idle",        icon: "fa-hourglass-half",        hex: "#f59e0b" },
  manual_stop: { label: "Manual Stop", icon: "fa-exclamation-triangle",  hex: "#ef4444" },
  off:         { label: "OFF",         icon: "fa-power-off",             hex: "#64748b" },
  unknown:     { label: "Unknown",     icon: "fa-question-circle",       hex: "#38bdf8" },
};

// Track last-known state per machine to detect transitions
const prevState = {};
let seenAlerts   = new Set();
let alertCount   = 0;
let refreshTimer = null;

// ─── Startup ─────────────────────────────────────────────────────────────────
document.addEventListener("DOMContentLoaded", () => {
  startClock();
  buildMachineCards();
  tick();                          // immediate first fetch
  refreshTimer = setInterval(tick, REFRESH_MS);
});

// ─── Clock & Date ─────────────────────────────────────────────────────────────
function startClock() {
  const clockEl = document.getElementById("live-clock");
  const dateEl  = document.getElementById("live-date");
  function update() {
    const now = new Date();
    clockEl.textContent = now.toLocaleTimeString("en-GB", { hour12: false });
    dateEl.textContent  = now.toLocaleDateString("en-GB", {
      weekday: "short", day: "2-digit", month: "short", year: "numeric",
    });
    updateShiftProgress(now);
  }
  update();
  setInterval(update, 1000);
}

function updateShiftProgress(now) {
  // Assume shift starts at 08:00
  const shiftStartHour = 8;
  const minutesSinceStart =
    (now.getHours() - shiftStartHour) * 60 + now.getMinutes();
  const pct = Math.min(100, Math.max(0, (minutesSinceStart / (SHIFT_HOURS * 60)) * 100));
  const bar   = document.getElementById("shift-progress-bar");
  const label = document.getElementById("shift-pct-label");
  if (bar)   bar.style.width = pct.toFixed(1) + "%";
  if (label) label.textContent = pct.toFixed(0) + "%";
}

// ─── Build Initial Skeleton Cards ────────────────────────────────────────────
function buildMachineCards() {
  const grid = document.getElementById("machine-grid");
  if (!grid) return;
  grid.innerHTML = "";
  for (let i = 1; i <= TOTAL_MACHINES; i++) {
    grid.insertAdjacentHTML("beforeend", machineCardHTML(i, null));
  }
}

function machineCardHTML(id, data) {
  const name     = data ? data.name          : `CNC-${String(id).padStart(3, "0")}`;
  const state    = data ? data.current_state : "unknown";
  const meta     = STATE_META[state] || STATE_META.unknown;
  const uptime   = data ? data.uptime_pct    : 0;
  const idlePct  = data ? data.idle_pct      : 0;
  const stopCnt  = data ? data.manual_stop_count : 0;
  const wFmt     = data ? data.working_fmt   : "--:--";
  const iFmt     = data ? data.idle_fmt      : "--:--";
  const sFmt     = data ? data.manual_stop_fmt : "--:--";
  const curFmt   = data ? data.current_duration_fmt : "--:--";
  const updated  = data ? fmtTimestamp(data.last_updated) : "—";

  return `
<div class="machine-card state-${state}" id="card-${id}" data-machine-id="${id}">
  <div class="card-header-row">
    <span class="machine-id">M-${String(id).padStart(2, "0")}</span>
    <div class="status-light light-${state}"></div>
  </div>
  <div class="machine-name">${escHtml(name)}</div>
  <div class="state-badge badge-${state}">
    <i class="fas ${meta.icon}"></i> ${meta.label}
  </div>
  <div class="card-metrics">
    <div class="metric-row">
      <span class="metric-label">Current Duration</span>
      <span class="metric-value" id="cur-dur-${id}">${curFmt}</span>
    </div>
    <div class="metric-row">
      <span class="metric-label">Working</span>
      <span class="metric-value" style="color:#22c55e">${wFmt}</span>
    </div>
    <div class="metric-row">
      <span class="metric-label">Idle</span>
      <span class="metric-value" style="color:#f59e0b">${iFmt}</span>
    </div>
    <div class="metric-row">
      <span class="metric-label">Manual Stop</span>
      <span class="metric-value" style="color:#ef4444">${sFmt}</span>
    </div>
  </div>

  <div class="uptime-bar-wrap">
    <div class="uptime-bar-label">
      <span>Uptime (shift)</span>
      <strong id="uptime-val-${id}">${uptime}%</strong>
    </div>
    <div class="uptime-bar">
      <div class="uptime-bar-fill" id="uptime-bar-${id}" style="width:${uptime}%"></div>
    </div>
  </div>

  <div class="card-footer-row">
    <span class="stop-count-badge">
      <i class="fas fa-stop-circle"></i> ${stopCnt} stop${stopCnt !== 1 ? "s" : ""}
    </span>
    <span class="last-updated" id="updated-${id}">${updated}</span>
  </div>
</div>`;
}

// ─── Main Poll Tick ───────────────────────────────────────────────────────────
async function tick() {
  try {
    const res = await fetch(`${API_BASE}/api/status`, { cache: "no-store" });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    renderDashboard(data);
  } catch (err) {
    console.warn("Status fetch failed:", err);
    setCameraStatus(false, "Connection error");
    setRefreshLabel("Error — retrying…");
  }
}

// ─── Render Full Dashboard ────────────────────────────────────────────────────
function renderDashboard(data) {
  updateSummary(data.summary, data.machines);
  updateCameraStatus(data.camera);

  for (let id = 1; id <= TOTAL_MACHINES; id++) {
    const m = data.machines[id] || data.machines[String(id)];
    if (m) updateMachineCard(id, m);
  }

  fetchAndRenderAlerts();
  setRefreshLabel(`Updated ${fmtTimestamp(data.timestamp)}`);
}

// ─── Update KPI Summary Bar ───────────────────────────────────────────────────
function updateSummary(summary, machines) {
  setText("sum-working", summary.working);
  setText("sum-idle",    summary.idle);
  setText("sum-stop",    summary.manual_stop);
  setText("sum-off",     summary.off);

  // Floor efficiency = working / (working + idle + stop + off) * 100
  const active = summary.working + summary.idle + (summary.manual_stop || 0) + summary.off;
  const eff = active > 0 ? Math.round((summary.working / active) * 100) : 0;
  setText("sum-efficiency", eff + "%");

  // Flash KPI cards when counts change
  ["working","idle","manual_stop","off"].forEach(state => {
    const kpiId = state === "manual_stop" ? "kpi-stop" : `kpi-${state}`;
    const el = document.getElementById(kpiId);
    if (el) flashElement(el, 300);
  });
}

// ─── Update Single Machine Card ───────────────────────────────────────────────
function updateMachineCard(id, data) {
  const card = document.getElementById(`card-${id}`);
  if (!card) return;

  const state    = data.current_state;
  const meta     = STATE_META[state] || STATE_META.unknown;
  const oldState = prevState[id];

  // Detect state transition
  if (oldState !== undefined && oldState !== state) {
    flashElement(card, 600);
    if (state === "manual_stop") {
      showToast(`${data.name} — Manual Stop!`, "danger");
    } else if (state === "off" && oldState === "working") {
      showToast(`${data.name} — Turned OFF`, "warning");
    } else if (state === "working" && (oldState === "manual_stop" || oldState === "idle")) {
      showToast(`${data.name} — Resumed Working`, "success");
    }
  }
  prevState[id] = state;

  // Rebuild card content in-place (preserves DOM node, avoids flicker)
  const stateClasses = Object.keys(STATE_META).map(s => `state-${s}`).join(" ");
  card.className = `machine-card state-${state}`;

  card.innerHTML = `
  <div class="card-header-row">
    <span class="machine-id">M-${String(id).padStart(2, "0")}</span>
    <div class="status-light light-${state}"></div>
  </div>
  <div class="machine-name">${escHtml(data.name)}</div>
  <div class="state-badge badge-${state}">
    <i class="fas ${meta.icon}"></i> ${meta.label}
  </div>
  <div class="card-metrics">
    <div class="metric-row">
      <span class="metric-label">Current Duration</span>
      <span class="metric-value">${data.current_duration_fmt || "--:--"}</span>
    </div>
    <div class="metric-row">
      <span class="metric-label">Working</span>
      <span class="metric-value" style="color:#22c55e">${data.working_fmt || "--:--"}</span>
    </div>
    <div class="metric-row">
      <span class="metric-label">Idle</span>
      <span class="metric-value" style="color:#f59e0b">${data.idle_fmt || "--:--"}</span>
    </div>
    <div class="metric-row">
      <span class="metric-label">Manual Stop</span>
      <span class="metric-value" style="color:#ef4444">${data.manual_stop_fmt || "--:--"}</span>
    </div>
  </div>
  <div class="uptime-bar-wrap">
    <div class="uptime-bar-label">
      <span>Uptime (shift)</span>
      <strong style="color:#22c55e">${data.uptime_pct}%</strong>
    </div>
    <div class="uptime-bar">
      <div class="uptime-bar-fill" style="width:${data.uptime_pct}%"></div>
    </div>
  </div>
  <div class="card-footer-row">
    <span class="stop-count-badge">
      <i class="fas fa-stop-circle"></i>
      ${data.manual_stop_count} stop${data.manual_stop_count !== 1 ? "s" : ""}
    </span>
    <span class="last-updated">${fmtTimestamp(data.last_updated)}</span>
  </div>`;
}

// ─── Alerts ───────────────────────────────────────────────────────────────────
async function fetchAndRenderAlerts() {
  try {
    const res = await fetch(`${API_BASE}/api/alerts`, { cache: "no-store" });
    if (!res.ok) return;
    const data = await res.json();
    renderAlerts(data.alerts || []);
  } catch (_) {}
}

function renderAlerts(alerts) {
  const log   = document.getElementById("alert-log");
  const badge = document.getElementById("alert-badge");
  if (!log) return;

  const newAlerts = alerts.filter(a => {
    const key = a.timestamp + a.machine_name;
    if (seenAlerts.has(key)) return false;
    seenAlerts.add(key);
    return true;
  });

  if (newAlerts.length > 0) {
    alertCount += newAlerts.length;
    if (badge) badge.textContent = alertCount;

    const noMsg = log.querySelector(".no-alerts-msg");
    if (noMsg) noMsg.remove();

    newAlerts.forEach(a => {
      const div = document.createElement("div");
      div.className = `alert-item alert-severity-${a.severity || "high"}`;
      div.innerHTML = `
        <i class="fas fa-exclamation-circle alert-icon"></i>
        <div class="alert-content">
          <div class="alert-msg">${escHtml(a.message)}</div>
          <div class="alert-time">${fmtTimestamp(a.timestamp)}</div>
        </div>`;
      log.prepend(div);
    });
  }

  // Cap alert log at 50 items
  const items = log.querySelectorAll(".alert-item");
  items.forEach((el, i) => { if (i >= 50) el.remove(); });
}

function clearAlerts() {
  const log = document.getElementById("alert-log");
  const badge = document.getElementById("alert-badge");
  if (log) {
    log.innerHTML = `<div class="no-alerts-msg">
      <i class="fas fa-check-circle text-success me-2"></i>Cleared
    </div>`;
  }
  seenAlerts.clear();
  alertCount = 0;
  if (badge) badge.textContent = "0";
}

// ─── Camera Status ────────────────────────────────────────────────────────────
function updateCameraStatus(cam) {
  setCameraStatus(cam && cam.connected, cam ? cam.error : "Offline");
}

function setCameraStatus(online, errMsg) {
  const dot   = document.getElementById("cam-dot");
  const label = document.getElementById("cam-label");
  if (!dot) return;
  if (online) {
    dot.className   = "cam-dot cam-online";
    label.textContent = "Camera OK";
  } else {
    dot.className   = "cam-dot cam-offline";
    label.textContent = errMsg || "Offline";
  }
}

// ─── Toast Notifications ──────────────────────────────────────────────────────
function showToast(msg, type) {
  const container = document.getElementById("toast-container");
  if (!container) return;

  const iconMap = {
    danger:  "fa-exclamation-triangle",
    warning: "fa-exclamation-circle",
    success: "fa-check-circle",
    info:    "fa-info-circle",
  };
  const colorMap = {
    danger:  "#ef4444",
    warning: "#f59e0b",
    success: "#22c55e",
    info:    "#38bdf8",
  };

  const el = document.createElement("div");
  el.className = "toast-notif";
  el.style.borderColor = colorMap[type] || "#64748b";
  el.innerHTML = `
    <i class="fas ${iconMap[type] || "fa-bell"}" style="color:${colorMap[type]};font-size:16px"></i>
    <span>${escHtml(msg)}</span>`;
  container.appendChild(el);

  setTimeout(() => {
    el.classList.add("toast-exit");
    setTimeout(() => el.remove(), 400);
  }, 4500);
}

// ─── Helpers ──────────────────────────────────────────────────────────────────
function fmtTimestamp(iso) {
  if (!iso) return "—";
  try {
    const d = new Date(iso);
    return d.toLocaleTimeString("en-GB", { hour12: false });
  } catch (_) { return iso; }
}

function setText(id, val) {
  const el = document.getElementById(id);
  if (el) el.textContent = val;
}

function setRefreshLabel(text) {
  const el = document.getElementById("last-refresh");
  if (el) el.textContent = text;
}

function flashElement(el, durationMs) {
  el.style.transition = "opacity 0.15s";
  el.style.opacity = "0.5";
  setTimeout(() => {
    el.style.opacity = "1";
  }, durationMs / 2);
}

function escHtml(str) {
  const d = document.createElement("div");
  d.appendChild(document.createTextNode(String(str)));
  return d.innerHTML;
}
