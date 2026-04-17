"use strict";

/* ═══════════════════════════════════════════════════════════════
   Camera Setup — Logic
   Scans NVR channels, shows snapshots, lets user pick cameras,
   saves selection and redirects to dashboard.
═══════════════════════════════════════════════════════════════ */

// Track per-channel state
const channelState = {};   // { ch: { hasSignal, selected, name } }
let scanning    = false;
let allSelected = false;

// ─── Scan ─────────────────────────────────────────────────────────────────────
async function startScan() {
  if (scanning) return;

  const from = parseInt(document.getElementById("ch-from").value, 10) || 1;
  const to   = parseInt(document.getElementById("ch-to").value,   10) || 16;
  if (from < 1 || to < from || to > 64) {
    showToast("Invalid channel range. Use 1–64.", "error");
    return;
  }

  scanning = true;
  const btn      = document.getElementById("btn-scan");
  const icon     = document.getElementById("scan-icon");
  const label    = document.getElementById("scan-label");
  const progress = document.getElementById("scan-progress");

  btn.disabled  = true;
  icon.className = "fas fa-spinner fa-spin";
  label.textContent = "Scanning…";

  // Clear grid
  const grid = document.getElementById("channel-grid");
  grid.innerHTML = "";

  // Reset state
  Object.keys(channelState).forEach(k => delete channelState[k]);

  const channels = [];
  for (let ch = from; ch <= to; ch++) channels.push(ch);

  progress.textContent = `0 / ${channels.length}`;

  // Build skeleton cards first
  channels.forEach(ch => {
    channelState[ch] = { hasSignal: false, selected: false, name: `Camera ${ch}` };
    grid.insertAdjacentHTML("beforeend", cardSkeletonHTML(ch));
  });

  // Fetch snapshots in batches of 4 (avoid overwhelming the NVR)
  const BATCH = 4;
  let done = 0;
  for (let i = 0; i < channels.length; i += BATCH) {
    const batch = channels.slice(i, i + BATCH);
    await Promise.all(batch.map(ch => fetchSnapshot(ch)));
    done += batch.length;
    progress.textContent = `${done} / ${channels.length}`;
  }

  scanning = false;
  btn.disabled   = false;
  icon.className  = "fas fa-search";
  label.textContent = "Re-Scan";
  progress.textContent = `Done — ${channels.length} channels checked`;

  updateSelectionBar();
}

// ─── Fetch one snapshot ───────────────────────────────────────────────────────
async function fetchSnapshot(ch) {
  const img   = document.getElementById(`thumb-${ch}`);
  const spin  = document.getElementById(`spin-${ch}`);
  const badge = document.getElementById(`badge-no-signal-${ch}`);
  const card  = document.getElementById(`card-ch${ch}`);

  try {
    // Use a cachebust timestamp so browser doesn't cache the image
    const url  = `/api/scan-snapshot/${ch}?t=${Date.now()}`;
    const resp = await fetch(url, { cache: "no-store" });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);

    const blob    = await resp.blob();
    const objUrl  = URL.createObjectURL(blob);

    // Check if it's a real image (>5 KB = likely has video content)
    const hasContent = blob.size > 5000;

    if (spin)  spin.remove();
    if (img) {
      img.src   = objUrl;
      img.style.display = "block";
    }

    channelState[ch].hasSignal = hasContent;

    if (!hasContent) {
      if (badge) badge.style.display = "block";
      if (card)  card.classList.add("no-signal");
    }
  } catch (err) {
    if (spin)  spin.innerHTML = `<div style="font-size:11px;color:#64748b">Error loading</div>`;
    channelState[ch].hasSignal = false;
    if (badge) badge.style.display = "block";
    if (card)  card.classList.add("no-signal");
  }
}

// ─── Card HTML ────────────────────────────────────────────────────────────────
function cardSkeletonHTML(ch) {
  return `
<div class="ch-card" id="card-ch${ch}" onclick="toggleCard(${ch})">
  <div class="ch-thumb-wrap">
    <img class="ch-thumb" id="thumb-${ch}" src="" alt="" style="display:none" />
    <div class="ch-thumb-loading" id="spin-${ch}">
      <div class="ch-spinner"></div>
      Loading…
    </div>
    <div class="ch-tick"><i class="fas fa-check"></i></div>
    <div class="ch-no-signal" id="badge-no-signal-${ch}" style="display:none">NO SIGNAL</div>
  </div>
  <div class="ch-body">
    <div class="ch-num">CHANNEL ${ch}</div>
    <input
      class="ch-name-input"
      id="name-${ch}"
      type="text"
      placeholder="Camera name…"
      value="Camera ${ch}"
      onclick="event.stopPropagation()"
      oninput="channelState[${ch}].name = this.value"
    />
  </div>
</div>`;
}

// ─── Toggle card selection ────────────────────────────────────────────────────
function toggleCard(ch) {
  const state = channelState[ch];
  if (!state) return;
  // Optionally allow selecting no-signal channels (user might know better)
  state.selected = !state.selected;
  const card = document.getElementById(`card-ch${ch}`);
  if (card) card.classList.toggle("selected", state.selected);
  updateSelectionBar();
}

// ─── Select All / Deselect All (signal channels only) ─────────────────────────
function toggleSelectAll() {
  const signalChannels = Object.keys(channelState)
    .map(Number)
    .filter(ch => channelState[ch].hasSignal);

  if (signalChannels.length === 0) return;

  // If any are unselected, select all; otherwise deselect all
  const anyUnselected = signalChannels.some(ch => !channelState[ch].selected);
  signalChannels.forEach(ch => {
    channelState[ch].selected = anyUnselected;
    const card = document.getElementById(`card-ch${ch}`);
    if (card) card.classList.toggle("selected", anyUnselected);
  });

  document.getElementById("btn-toggle-all").textContent =
    anyUnselected ? "Deselect All" : "Select All";

  updateSelectionBar();
}

// ─── Update bottom bar ────────────────────────────────────────────────────────
function updateSelectionBar() {
  const selected = Object.keys(channelState).filter(ch => channelState[ch].selected);
  const count    = selected.length;

  document.getElementById("sel-count").textContent = count;
  document.getElementById("btn-apply").disabled    = count === 0;
}

// ─── Apply selection ──────────────────────────────────────────────────────────
async function applySelection() {
  const btn     = document.getElementById("btn-apply");
  const status  = document.getElementById("apply-status");

  const selected = Object.keys(channelState)
    .filter(ch => channelState[ch].selected)
    .reduce((acc, ch) => {
      const name = document.getElementById(`name-${ch}`)?.value?.trim()
                   || channelState[ch].name
                   || `Camera ${ch}`;
      acc[ch] = name;
      return acc;
    }, {});

  if (Object.keys(selected).length === 0) {
    showToast("Please select at least one camera.", "error");
    return;
  }

  btn.disabled      = true;
  status.textContent = "Saving…";

  try {
    const resp = await fetch("/api/config/save", {
      method:  "POST",
      headers: { "Content-Type": "application/json" },
      body:    JSON.stringify({ channels: selected }),
    });

    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      throw new Error(err.error || `HTTP ${resp.status}`);
    }

    const data = await resp.json();
    showToast(`✓ Saved ${data.count} camera(s). Redirecting to dashboard…`, "success");
    status.textContent = "";

    setTimeout(() => { window.location.href = "/"; }, 1800);
  } catch (err) {
    showToast(`Save failed: ${err.message}`, "error");
    btn.disabled      = false;
    status.textContent = "";
  }
}

// ─── Toast ────────────────────────────────────────────────────────────────────
function showToast(msg, type) {
  const wrap = document.getElementById("toast-wrap");
  const div  = document.createElement("div");
  div.className  = `toast-msg toast-${type}`;
  div.textContent = msg;
  wrap.appendChild(div);
  setTimeout(() => div.remove(), 4500);
}

// ─── On load: if cameras already saved, pre-select them ──────────────────────
document.addEventListener("DOMContentLoaded", async () => {
  try {
    const resp = await fetch("/api/config/cameras");
    if (!resp.ok) return;
    const data = await resp.json();
    // Store existing names so scan can pre-select them
    window._savedChannels = data.channels || {};
  } catch (_) {}
});

// After scan completes, auto-check previously saved channels
const _origFetchSnapshot = fetchSnapshot;
// We patch the update after each scan to restore saved selections
const _scanObserver = new MutationObserver(() => {
  if (window._savedChannels) {
    Object.keys(window._savedChannels).forEach(ch => {
      const chNum = parseInt(ch, 10);
      if (channelState[chNum] && !channelState[chNum].selected) {
        channelState[chNum].selected = true;
        channelState[chNum].name     = window._savedChannels[ch];
        const card  = document.getElementById(`card-ch${chNum}`);
        const input = document.getElementById(`name-${chNum}`);
        if (card)  card.classList.add("selected");
        if (input) input.value = window._savedChannels[ch];
      }
    });
    updateSelectionBar();
  }
});
_scanObserver.observe(document.getElementById("channel-grid") || document.body,
  { childList: true, subtree: true });
