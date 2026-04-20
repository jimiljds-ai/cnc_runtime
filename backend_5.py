#!/usr/bin/env python3
"""
CNC Machine Monitor — Multi-Camera Edition
Connects to all 4 CNC floor cameras simultaneously.
Detects indicator lights via blob detection (no fixed ROI boxes).
Counts Working / Idle / Manual Stop machines per camera and as a total.
"""

import cv2
import numpy as np
import threading
import time
import logging
import os
import json
from datetime import datetime, timedelta
from collections import deque
from flask import Flask, jsonify, Response, send_from_directory, request
from flask_cors import CORS
import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ─── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("cnc_monitor.log", mode="a"),
    ],
)
logger = logging.getLogger(__name__)

# ─── NVR Connection ───────────────────────────────────────────────────────────
NVR_HOST     = "192.168.4.10"
NVR_USER     = "admin"
NVR_PASSWORD = "Admin@1234"
NVR_PORT     = 554

# ─── Camera Config Persistence ───────────────────────────────────────────────
CAMERAS_CONFIG_FILE  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cameras.json")
CALIBRATION_FILE     = os.path.join(os.path.dirname(os.path.abspath(__file__)), "calibration.json")
HISTORY_FILE         = os.path.join(os.path.dirname(os.path.abspath(__file__)), "machine_history.json")

# ─── Calibration: per-channel anchor points ──────────────────────────────────
# Structure: { "ch_str": [ {"x_norm": 0.5, "y_norm": 0.3, "label": "machine1", "tol": 60}, ... ] }
# x_norm / y_norm are 0.0–1.0 fractions of the frame so they survive resolution changes.
# tol is the search radius in pixels at actual frame resolution.
_calibration: dict = {}

def load_calibration() -> dict:
    if os.path.exists(CALIBRATION_FILE):
        try:
            with open(CALIBRATION_FILE) as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"Could not load calibration.json: {e}")
    return {}

def save_calibration(data: dict):
    with open(CALIBRATION_FILE, "w") as f:
        json.dump(data, f, indent=2)
    logger.info(f"Calibration saved: {sum(len(v) for v in data.values())} anchor points")

_calibration = load_calibration()

# ─── Machine History Persistence ─────────────────────────────────────────────
def load_machine_history() -> dict:
    """Load persisted machine state history. Returns {ch_str: {label: saved_data}}."""
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE) as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"Could not load machine_history.json: {e}")
    return {}

def save_machine_history(cams: dict):
    """Persist all tracker histories so they survive a server restart."""
    data = {}
    for ch, cam in cams.items():
        ch_data = {}
        for label, tracker in cam._machine_trackers.items():
            with tracker._lock:
                ch_data[label] = {
                    "label":         tracker.label,
                    "x_norm":        tracker.x_norm,
                    "y_norm":        tracker.y_norm,
                    "current_state": tracker.current_state,
                    "state_since":   tracker.state_since.isoformat(),
                    "history":       list(tracker.history),
                }
        if ch_data:
            data[str(ch)] = ch_data
    try:
        with open(HISTORY_FILE, "w") as f:
            json.dump(data, f)
    except Exception as e:
        logger.warning(f"Could not save machine_history.json: {e}")

_saved_history: dict = load_machine_history()

DEFAULT_CNC_CAMERAS = {
    6: "CNC 2",
    7: "CNC 2 PASSAGE",
    8: "CNC 1 PASSAGE",
    9: "CNC 1",
}

def load_camera_config() -> dict:
    """Load saved camera selection from cameras.json. Falls back to defaults."""
    if os.path.exists(CAMERAS_CONFIG_FILE):
        try:
            with open(CAMERAS_CONFIG_FILE) as f:
                data = json.load(f)
            # Convert string keys back to int
            return {int(k): v for k, v in data.get("channels", {}).items()}
        except Exception as e:
            logger.warning(f"Could not load cameras.json: {e}")
    return {}

def save_camera_config(channels: dict):
    """Persist selected channels to cameras.json."""
    with open(CAMERAS_CONFIG_FILE, "w") as f:
        json.dump({"channels": {str(k): v for k, v in channels.items()}}, f, indent=2)
    logger.info(f"Camera config saved: {channels}")

# Load saved selection (empty dict = not configured yet → show setup page)
CNC_CAMERAS: dict = load_camera_config()

def rtsp_urls_for_channel(ch: int) -> list:
    """Return ordered list of RTSP URLs to try for a CP PLUS NVR channel."""
    u, p, h = NVR_USER, NVR_PASSWORD, NVR_HOST
    return [
        # CP PLUS / Dahua primary format
        f"rtsp://{u}:{p}@{h}:{NVR_PORT}/cam/realmonitor?channel={ch}&subtype=0",
        # Sub-stream (lower resolution, more stable)
        f"rtsp://{u}:{p}@{h}:{NVR_PORT}/cam/realmonitor?channel={ch}&subtype=1",
        # Alternative Dahua paths
        f"rtsp://{u}:{p}@{h}:{NVR_PORT}/h264/ch{ch:02d}/main/av_stream",
        f"rtsp://{u}:{p}@{h}:{NVR_PORT}/Streaming/Channels/{ch}01",
        f"rtsp://{u}:{p}@{h}:{NVR_PORT}/ch{ch:02d}/0",
    ]

# ─── Indicator Light Detection (Blob-based, No Fixed ROI) ────────────────────
# Indicator lights are small bright saturated spots.
# Area range is set relative to frame size so it works across resolutions.
# Indicator lights: small saturated LED domes viewed from overhead.
# RTSP H.264 compression reduces apparent saturation, so keep color ranges relaxed.
# The PRIMARY filter against floor tape / reflections is CIRCULARITY (tape ≈ 0.01).
LIGHT_SPECS = {
    "working": {  # Steady Green = Running
        "ranges": [
            (np.array([38, 60, 90]), np.array([88, 255, 255])),
        ],
        "color_bgr": (0, 220, 60),
    },
    "idle": {  # Yellow = Idle
        # Yellow LED after RTSP H.264 compression: S can drop to 70–90.
        # Shape filters (circularity + solidity) are what kill floor tape, NOT S value.
        "ranges": [
            (np.array([18, 70, 100]), np.array([35, 255, 255])),
        ],
        "color_bgr": (0, 200, 240),
    },
    "manual_stop": {  # Red = Manual Stop / Alarm
        "ranges": [
            (np.array([0,   80,  90]), np.array([10, 255, 255])),
            (np.array([165, 80,  90]), np.array([180, 255, 255])),
        ],
        "color_bgr": (50, 50, 240),
    },
    "process_finish": {  # Blinking Green = Process Complete (derived in CameraManager)
        "ranges": [],
        "color_bgr": (255, 210, 0),
    },
}

_MORPH_OPEN_K  = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
_MORPH_CLOSE_K = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (9, 9))  # larger to merge LED sub-blobs


def _merge_nearby_blobs(blobs: list, merge_dist: float = 30.0) -> list:
    """
    Cluster blobs whose centres are within `merge_dist` pixels of each other.
    Keeps only the largest blob from each cluster, preventing one LED from
    being counted as multiple lights.
    """
    if not blobs:
        return blobs
    used = [False] * len(blobs)
    merged = []
    for i, (x1, y1, r1, s1) in enumerate(blobs):
        if used[i]:
            continue
        cluster = [(x1, y1, r1, s1)]
        used[i] = True
        for j, (x2, y2, r2, s2) in enumerate(blobs):
            if used[j] or s2 != s1:
                continue
            if ((x1 - x2) ** 2 + (y1 - y2) ** 2) ** 0.5 < merge_dist:
                cluster.append((x2, y2, r2, s2))
                used[j] = True
        # Keep the blob with the largest radius from the cluster
        best = max(cluster, key=lambda b: b[2])
        merged.append(best)
    return merged


# ─── Per-Machine State Duration Tracker ──────────────────────────────────────
class MachineStateTracker:
    """Tracks state-duration history for one calibrated indicator light (anchor)."""

    STATES = ("working", "idle", "manual_stop", "process_finish", "off")

    def __init__(self, label: str, x_norm: float, y_norm: float):
        self.label      = label
        self.x_norm     = x_norm
        self.y_norm     = y_norm
        self._lock      = threading.Lock()
        self.current_state: str      = "off"
        self.state_since: datetime   = datetime.now()
        # Each history entry: {state, start, end, duration_sec}
        self.history: deque = deque(maxlen=2000)

    def restore_from_saved(self, saved: dict):
        """Re-hydrate history from machine_history.json after a restart."""
        with self._lock:
            self.history = deque(saved.get("history", []), maxlen=2000)
            # Keep current_state as "off" — let live frames set it fresh

    def update(self, state):
        """Call once per frame. state = str or None (→ 'off')."""
        effective = state if state else "off"
        now = datetime.now()
        with self._lock:
            if effective == self.current_state:
                return
            duration = (now - self.state_since).total_seconds()
            if duration >= 2.0:   # ignore sub-second single-frame glitches
                self.history.append({
                    "state":        self.current_state,
                    "start":        self.state_since.isoformat(),
                    "end":          now.isoformat(),
                    "duration_sec": round(duration),
                })
            self.current_state = effective
            self.state_since   = now

    def _compute_summary(self, window_start: datetime, now: datetime) -> dict:
        """Accumulate seconds in each state for the given time window."""
        summary = {s: 0.0 for s in self.STATES}
        for h in self.history:
            try:
                h_start = datetime.fromisoformat(h["start"])
                h_end   = datetime.fromisoformat(h["end"])
            except Exception:
                continue
            if h_end < window_start:
                continue
            overlap = max(0.0, (min(h_end, now) - max(h_start, window_start)).total_seconds())
            key = h["state"] if h["state"] in summary else "off"
            summary[key] += overlap
        # Add current ongoing period
        cur_start = max(self.state_since, window_start)
        summary[self.current_state if self.current_state in summary else "off"] += \
            max(0.0, (now - cur_start).total_seconds())
        return {k: round(v) for k, v in summary.items()}

    def get_status(self) -> dict:
        with self._lock:
            now         = datetime.now()
            elapsed     = (now - self.state_since).total_seconds()
            today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            shift_start = now - timedelta(hours=8)
            return {
                "label":          self.label,
                "x_norm":         self.x_norm,
                "y_norm":         self.y_norm,
                "current_state":  self.current_state,
                "state_since":    self.state_since.isoformat(),
                "duration_sec":   round(elapsed),
                "today_summary":  self._compute_summary(today_start, now),
                "shift_summary":  self._compute_summary(shift_start, now),
                "history":        list(self.history)[-500:],
            }


def detect_indicator_lights(frame: np.ndarray, anchors: list = None) -> dict:
    """
    Two-mode detection:

    CALIBRATED (anchors provided):
      Directly samples the HSV value at each known anchor position in a small
      neighbourhood. No blob detection needed — position is ground truth.
      Classifies colour from the dominant HSV in a patch around each anchor.

    UNCALIBRATED (no anchors):
      Full-frame blob detection with shape + colour filters.
    """
    if frame is None or frame.size == 0:
        return {"working": 0, "idle": 0, "manual_stop": 0, "process_finish": 0, "blobs": []}

    h, w = frame.shape[:2]
    blurred   = cv2.GaussianBlur(frame, (5, 5), 0)
    hsv       = cv2.cvtColor(blurred, cv2.COLOR_BGR2HSV)
    v_channel = hsv[:, :, 2]
    s_channel = hsv[:, :, 1]

    results = {"working": 0, "idle": 0, "manual_stop": 0, "process_finish": 0, "blobs": []}

    # ══════════════════════════════════════════════════════════════════════════
    # CALIBRATED MODE — sample HSV patch at each anchor directly
    # ══════════════════════════════════════════════════════════════════════════
    if anchors:
        PATCH = 12   # minimum search radius in pixels

        def classify_patch(px, py, radius=PATCH):
            """Return state name based on dominant HSV in a patch, or None.

            Uses V×S scoring so we pick the pixel that is both bright AND
            saturated — i.e. the LED itself, not a white specular glare point.
            """
            r = max(PATCH, min(radius, 80))     # clamp to [12, 80] px
            x1 = max(0, px - r); x2 = min(w, px + r)
            y1 = max(0, py - r); y2 = min(h, py + r)
            patch_hsv = hsv[y1:y2, x1:x2]
            if patch_hsv.size == 0:
                return None

            patch_v = patch_hsv[:, :, 2]
            patch_s = patch_hsv[:, :, 1]
            if int(patch_v.max()) < 80:
                return None   # too dark — light is off

            # Score = brightness × saturation → finds coloured LED, not white glare
            score = patch_v.astype(np.float32) * patch_s.astype(np.float32)
            idx   = np.unravel_index(score.argmax(), score.shape)
            hue   = int(patch_hsv[idx[0], idx[1], 0])
            sat   = int(patch_hsv[idx[0], idx[1], 1])
            val   = int(patch_hsv[idx[0], idx[1], 2])

            # Lowered from 40→30: RTSP H.264 compression degrades saturation
            if sat < 30 or val < 50:
                return None

            if 38 <= hue <= 88:             return "working"
            if 18 <= hue <= 37:             return "idle"
            if hue <= 10 or hue >= 165:     return "manual_stop"
            return None

        per_anchor = []
        for anc in anchors:
            px     = int(anc["x_norm"] * w)
            py     = int(anc["y_norm"] * h)
            radius = int(anc.get("tol", PATCH))   # use operator-set tolerance
            state  = classify_patch(px, py, radius)
            if state:
                results[state] += 1
                results["blobs"].append((px, py, 18, state))
            per_anchor.append({
                "label":  anc.get("label", ""),
                "x_norm": anc["x_norm"],
                "y_norm": anc["y_norm"],
                "state":  state,   # None if light is off / unclassified
            })
        results["per_anchor"] = per_anchor
        return results

    # ══════════════════════════════════════════════════════════════════════════
    # UNCALIBRATED MODE — full-frame blob detection
    # ══════════════════════════════════════════════════════════════════════════
    total_px  = h * w
    min_area  = max(15,  int(total_px * 0.000012))
    max_area  = int(total_px * 0.00080)
    v_blur    = cv2.GaussianBlur(v_channel, (21, 21), 0)

    for state, spec in LIGHT_SPECS.items():
        if not spec["ranges"]:
            continue

        mask = np.zeros(hsv.shape[:2], dtype=np.uint8)
        for lo, hi in spec["ranges"]:
            mask = cv2.bitwise_or(mask, cv2.inRange(hsv, lo, hi))

        sat_gate = cv2.inRange(s_channel, 60, 255)
        val_gate = cv2.inRange(v_channel, 90, 255)
        mask     = cv2.bitwise_and(mask, sat_gate)
        mask     = cv2.bitwise_and(mask, val_gate)

        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN,  _MORPH_OPEN_K,  iterations=2)
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, _MORPH_CLOSE_K, iterations=1)

        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        for cnt in contours:
            area = cv2.contourArea(cnt)
            if not (min_area <= area <= max_area):
                continue

            perimeter = cv2.arcLength(cnt, True)
            if perimeter == 0:
                continue
            circularity = 4 * np.pi * area / (perimeter ** 2)
            if circularity < 0.45:
                continue

            hull      = cv2.convexHull(cnt)
            hull_area = cv2.contourArea(hull)
            solidity  = area / hull_area if hull_area > 0 else 0
            if solidity < 0.80:
                continue

            (cx_pre, cy_pre), r_pre = cv2.minEnclosingCircle(cnt)
            x_bb, y_bb, bw_bb, bh_bb = cv2.boundingRect(cnt)
            bbox_diag_half = ((bw_bb**2 + bh_bb**2) ** 0.5) / 2
            if r_pre > bbox_diag_half * 1.6:
                continue

            aspect = bw_bb / bh_bb if bh_bb > 0 else 0
            if not (0.50 <= aspect <= 2.0):
                continue

            ix, iy   = int(cx_pre), int(cy_pre)
            local_bg = float(v_blur[min(iy, h-1), min(ix, w-1)])
            raw_v    = float(v_channel[min(iy, h-1), min(ix, w-1)])

            peak_threshold = 15 if state == "idle" else 12
            if raw_v - local_bg < peak_threshold:
                continue

            abs_floor = 110 if state == "idle" else 100
            if raw_v < abs_floor:
                continue

            results[state] += 1
            results["blobs"].append((ix, iy, max(5, int(r_pre * 2.0)), state))

    merged_blobs = _merge_nearby_blobs(results["blobs"], merge_dist=40.0)
    for state in ("working", "idle", "manual_stop"):
        results[state] = sum(1 for _, _, _, s in merged_blobs if s == state)
    results["blobs"] = merged_blobs

    return results


def annotate_frame(frame: np.ndarray, detections: dict, cam_name: str) -> np.ndarray:
    """Draw detection circles and a status overlay on the frame (no ROI boxes)."""
    if frame is None:
        return frame
    out = frame.copy()
    h, w = out.shape[:2]

    for (cx, cy, r, state) in detections.get("blobs", []):
        color_bgr = LIGHT_SPECS[state]["color_bgr"]
        cv2.circle(out, (cx, cy), r,     color_bgr, 2)
        cv2.circle(out, (cx, cy), r + 3, color_bgr, 1)

    # Overlay: camera name + counts bar
    overlay = out.copy()
    cv2.rectangle(overlay, (0, h - 36), (w, h), (10, 10, 20), -1)
    cv2.addWeighted(overlay, 0.7, out, 0.3, 0, out)

    summary = (
        f"{cam_name}  |  "
        f"Working: {detections['working']}  "
        f"Idle: {detections['idle']}  "
        f"Stop: {detections['manual_stop']}"
    )
    cv2.putText(out, summary, (10, h - 10),
                cv2.FONT_HERSHEY_SIMPLEX, 0.52, (200, 200, 200), 1, cv2.LINE_AA)
    return out


# ─── Single-Channel Camera Manager ───────────────────────────────────────────
class CameraManager:
    def __init__(self, channel: int, name: str):
        self.channel  = channel
        self.name     = name
        self._frame   = None
        self._lock    = threading.Lock()
        self.connected       = False
        self.connection_error: str = None
        self.frame_count     = 0
        self.active_url: str = None
        self._cap: cv2.VideoCapture = None
        self.running         = False
        self._thread: threading.Thread = None
        self._detections     = {"working": 0, "idle": 0, "manual_stop": 0, "process_finish": 0, "blobs": []}
        self._det_lock       = threading.Lock()
        self._history        = deque(maxlen=5000)  # (timestamp, detections)
        self._green_history  = deque(maxlen=30)    # True/False per frame for blink detection
        self._machine_trackers:  dict = {}          # label -> MachineStateTracker
        self._anchor_green_hist: dict = {}          # anchor_idx -> deque[bool] per-anchor blink
        self._last_alert:        dict = {}          # alert_key -> unix timestamp last sent

    def _try_connect(self) -> bool:
        for url in rtsp_urls_for_channel(self.channel):
            try:
                cap = cv2.VideoCapture(url)
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 2)
                cap.set(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 5000)
                cap.set(cv2.CAP_PROP_READ_TIMEOUT_MSEC, 5000)
                if cap.isOpened():
                    ret, frame = cap.read()
                    if ret and frame is not None:
                        logger.info(f"[ch{self.channel}] {self.name}: connected via {url}")
                        self._cap      = cap
                        self.active_url = url
                        return True
                cap.release()
            except Exception as e:
                logger.debug(f"[ch{self.channel}] {url} failed: {e}")
        return False

    def _loop(self):
        reconnect_delay = 5
        while self.running:
            if not self.connected:
                if self._try_connect():
                    self.connected       = True
                    self.connection_error = None
                    reconnect_delay      = 5
                else:
                    self.connection_error = f"ch{self.channel} offline"
                    logger.warning(f"[ch{self.channel}] {self.name}: offline, retry in {reconnect_delay}s")
                    time.sleep(reconnect_delay)
                    reconnect_delay = min(reconnect_delay * 2, 60)
                    continue

            try:
                if self._cap and self._cap.isOpened():
                    ret, frame = self._cap.read()
                    if ret and frame is not None:
                        with self._lock:
                            self._frame      = frame
                            self.frame_count += 1
                        # Detect lights on this frame
                        anchors = _calibration.get(str(self.channel), None)
                        raw_det = detect_indicator_lights(frame, anchors=anchors)

                        # Blink detection: track green presence over last 30 frames
                        self._green_history.append(raw_det["working"] > 0)
                        effective_det = dict(raw_det)
                        if len(self._green_history) >= 20:
                            ratio = sum(self._green_history) / len(self._green_history)
                            transitions = sum(
                                1 for i in range(1, len(self._green_history))
                                if self._green_history[i] != self._green_history[i - 1]
                            )
                            # Widened ratio window (0.10–0.95) and lowered transition
                            # threshold (≥3) to tolerate RTSP frame-drop variance.
                            if 0.10 <= ratio <= 0.95 and transitions >= 3:
                                # Blinking green → Process Finish
                                effective_det["process_finish"] = effective_det["working"]
                                effective_det["working"] = 0
                                effective_det["blobs"] = [
                                    (x, y, r, "process_finish" if s == "working" else s)
                                    for x, y, r, s in effective_det.get("blobs", [])
                                ]

                        # ── Per-anchor machine state tracking ──────────────
                        for idx, ap in enumerate(raw_det.get("per_anchor", [])):
                            label = ap["label"] or f"Light {idx + 1}"
                            raw_state = ap["state"]

                            # Per-anchor blink detection (green blinking → process_finish)
                            if idx not in self._anchor_green_hist:
                                self._anchor_green_hist[idx] = deque(maxlen=30)
                            self._anchor_green_hist[idx].append(raw_state == "working")
                            gh  = self._anchor_green_hist[idx]
                            eff = raw_state
                            if raw_state == "working" and len(gh) >= 20:
                                ratio = sum(gh) / len(gh)
                                trans = sum(1 for k in range(1, len(gh)) if gh[k] != gh[k - 1])
                                if 0.10 <= ratio <= 0.95 and trans >= 3:
                                    eff = "process_finish"

                            if label not in self._machine_trackers:
                                tracker = MachineStateTracker(label, ap["x_norm"], ap["y_norm"])
                                saved_ch = _saved_history.get(str(self.channel), {})
                                if label in saved_ch:
                                    tracker.restore_from_saved(saved_ch[label])
                                self._machine_trackers[label] = tracker
                            self._machine_trackers[label].update(eff)

                        # ── Long-idle / long-stop machine alerts ────────────
                        now_ts = time.time()
                        for lbl, tracker in self._machine_trackers.items():
                            st  = tracker.current_state
                            dur = (datetime.now() - tracker.state_since).total_seconds()
                            thr = None
                            if st == "idle":
                                thr = _alert_config["idle_alert_min"] * 60
                            elif st == "manual_stop":
                                thr = _alert_config["stop_alert_min"] * 60
                            if thr and dur > thr:
                                akey = f"{self.channel}-{lbl}-{st}"
                                if now_ts - self._last_alert.get(akey, 0) > 600:
                                    self._last_alert[akey] = now_ts
                                    mins = round(dur / 60)
                                    msg  = (f"{self.name} / {lbl}: "
                                            f"{st.replace('_',' ')} for {mins} min")
                                    _machine_alert_queue.append({
                                        "ts":       datetime.now().isoformat(),
                                        "channel":  self.channel,
                                        "cam_name": self.name,
                                        "machine":  lbl,
                                        "state":    st,
                                        "dur_min":  mins,
                                        "message":  msg,
                                    })
                                    logger.warning(f"[ALERT] {msg}")

                        with self._det_lock:
                            self._detections = effective_det
                            self._history.append((datetime.now(), {
                                "working":        effective_det["working"],
                                "idle":           effective_det["idle"],
                                "manual_stop":    effective_det["manual_stop"],
                                "process_finish": effective_det["process_finish"],
                            }))
                    else:
                        logger.warning(f"[ch{self.channel}] read failed — reconnecting")
                        self._cap.release()
                        self._cap     = None
                        self.connected = False
                else:
                    self.connected = False
            except Exception as e:
                logger.error(f"[ch{self.channel}] capture error: {e}")
                self.connected = False
                if self._cap:
                    self._cap.release()
                    self._cap = None
                time.sleep(5)

    def start(self):
        self.running = True
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name=f"cam-ch{self.channel}"
        )
        self._thread.start()

    def stop(self):
        self.running = False
        if self._cap:
            self._cap.release()

    def get_frame(self):
        with self._lock:
            return self._frame.copy() if self._frame is not None else None

    def get_detections(self) -> dict:
        with self._det_lock:
            return dict(self._detections)

    def get_annotated_frame(self):
        frame = self.get_frame()
        if frame is None:
            return None
        det = self.get_detections()
        return annotate_frame(frame, det, self.name)

    def get_status(self) -> dict:
        det = self.get_detections()
        ch_anchors = _calibration.get(str(self.channel), [])
        now = datetime.now()
        machine_states = [
            {
                "label":        t.label,
                "state":        t.current_state,
                "duration_sec": round((now - t.state_since).total_seconds()),
            }
            for t in self._machine_trackers.values()
        ]
        return {
            "channel":        self.channel,
            "name":           self.name,
            "connected":      self.connected,
            "error":          self.connection_error,
            "active_url":     self.active_url,
            "frame_count":    self.frame_count,
            "working":        det["working"],
            "idle":           det["idle"],
            "manual_stop":    det["manual_stop"],
            "process_finish": det.get("process_finish", 0),
            "calibrated":     len(ch_anchors) > 0,
            "anchor_count":   len(ch_anchors),
            "machine_states": machine_states,
            "last_updated":   now.isoformat(),
        }

    def get_shift_totals(self) -> dict:
        """Average counts over the last 8-hour shift window."""
        with self._det_lock:
            cutoff = datetime.now() - timedelta(hours=8)
            recent = [(ts, d) for ts, d in self._history if ts >= cutoff]
        if not recent:
            return {"working": 0, "idle": 0, "manual_stop": 0}
        avg = lambda key: round(sum(d[key] for _, d in recent) / len(recent), 1)
        return {"working": avg("working"), "idle": avg("idle"), "manual_stop": avg("manual_stop")}

    def get_machine_report(self) -> list:
        """Return per-machine state + duration history for this camera."""
        return [t.get_status() for t in self._machine_trackers.values()]


# ─── Machine Alert Config + Queue ────────────────────────────────────────────
_alert_config = {
    "idle_alert_min": 30,   # alert when a machine is idle longer than this
    "stop_alert_min": 10,   # alert when a machine is stopped longer than this
}
_machine_alert_queue: deque = deque(maxlen=500)

# ─── Global Camera Pool ────────────────────────────────────────────────────────
cameras: dict = {
    ch: CameraManager(ch, name) for ch, name in CNC_CAMERAS.items()
}

# ─── Periodic History Persistence ────────────────────────────────────────────
import atexit

def _history_saver_loop():
    while True:
        time.sleep(60)
        save_machine_history(cameras)

threading.Thread(target=_history_saver_loop, daemon=True, name="history-saver").start()
atexit.register(lambda: save_machine_history(cameras))

# ─── Flask App ────────────────────────────────────────────────────────────────
app     = Flask(__name__, static_folder=".", static_url_path="")
CORS(app)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

_OFFLINE_JPEG: bytes = None

def _make_offline_jpeg(msg: str = "Camera Offline") -> bytes:
    img = np.zeros((360, 640, 3), dtype=np.uint8)
    img[:] = (15, 15, 25)
    cv2.putText(img, msg, (max(0, 320 - len(msg) * 8), 180),
                cv2.FONT_HERSHEY_SIMPLEX, 0.85, (60, 60, 200), 2, cv2.LINE_AA)
    cv2.putText(img, "Waiting for camera connection...", (130, 220),
                cv2.FONT_HERSHEY_SIMPLEX, 0.5, (80, 80, 80), 1, cv2.LINE_AA)
    _, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 55])
    return buf.tobytes()


# ─── Routes ───────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    # If no cameras configured yet, send user to setup page
    if not cameras:
        from flask import redirect
        return redirect("/setup")
    return send_from_directory(BASE_DIR, "dashboard.html")

@app.route("/setup")
def setup_page():
    return send_from_directory(BASE_DIR, "setup.html")

_NO_CACHE = {"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache", "Expires": "0"}

@app.route("/style.css")
def serve_css():
    r = send_from_directory(BASE_DIR, "style.css")
    r.headers.update(_NO_CACHE)
    return r

@app.route("/app.js")
def serve_js():
    r = send_from_directory(BASE_DIR, "app.js")
    r.headers.update(_NO_CACHE)
    return r

@app.route("/setup.js")
def serve_setup_js():
    r = send_from_directory(BASE_DIR, "setup.js")
    r.headers.update(_NO_CACHE)
    return r


@app.route("/api/scan-snapshot/<int:channel>")
def api_scan_snapshot(channel):
    """
    Grab a single JPEG frame from the given NVR channel.
    Used by the setup page to preview each channel.
    Times out quickly — returns offline placeholder if no feed.
    """
    global _OFFLINE_JPEG
    if _OFFLINE_JPEG is None:
        _OFFLINE_JPEG = _make_offline_jpeg()

    url = (f"rtsp://{NVR_USER}:{NVR_PASSWORD}@{NVR_HOST}:{NVR_PORT}"
           f"/cam/realmonitor?channel={channel}&subtype=1")
    try:
        cap = cv2.VideoCapture(url)
        cap.set(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 5000)
        cap.set(cv2.CAP_PROP_READ_TIMEOUT_MSEC, 5000)
        ret, frame = cap.read()
        cap.release()
        if ret and frame is not None:
            # Resize to 480×270 for faster transfer
            frame = cv2.resize(frame, (480, 270))
            ret2, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 65])
            if ret2:
                return Response(buf.tobytes(), mimetype="image/jpeg",
                                headers={"Cache-Control": "no-cache"})
    except Exception as e:
        logger.debug(f"scan-snapshot ch{channel}: {e}")

    # Return offline placeholder
    placeholder = np.zeros((270, 480, 3), dtype=np.uint8)
    placeholder[:] = (15, 15, 25)
    cv2.putText(placeholder, f"Ch {channel}: No Signal", (100, 135),
                cv2.FONT_HERSHEY_SIMPLEX, 0.7, (60, 60, 100), 1, cv2.LINE_AA)
    _, buf = cv2.imencode(".jpg", placeholder, [cv2.IMWRITE_JPEG_QUALITY, 50])
    return Response(buf.tobytes(), mimetype="image/jpeg",
                    headers={"Cache-Control": "no-cache"})


@app.route("/api/config/save", methods=["POST"])
def api_save_config():
    """
    Save selected camera channels and restart camera managers.
    Body: {"channels": {"1": "CAD CAM", "6": "CNC 2", ...}}
    """
    global cameras, CNC_CAMERAS
    data = request.get_json(force=True) or {}
    new_channels_raw = data.get("channels", {})

    if not new_channels_raw:
        return jsonify({"error": "No channels provided"}), 400

    new_channels = {int(k): str(v) for k, v in new_channels_raw.items()}

    # Stop all running cameras
    for cam in cameras.values():
        cam.stop()

    # Save to disk
    save_camera_config(new_channels)
    CNC_CAMERAS = new_channels

    # Start new camera managers
    cameras = {ch: CameraManager(ch, name) for ch, name in new_channels.items()}
    for cam in cameras.values():
        cam.start()

    logger.info(f"Camera selection updated: {new_channels}")
    return jsonify({
        "success":  True,
        "channels": new_channels,
        "count":    len(new_channels),
    })


@app.route("/calibrate")
def calibrate_page():
    r = send_from_directory(BASE_DIR, "calibrate.html")
    r.headers.update(_NO_CACHE)
    return r

@app.route("/report")
def report_page():
    r = send_from_directory(BASE_DIR, "report.html")
    r.headers.update(_NO_CACHE)
    return r

@app.route("/api/report/<int:channel>")
def api_machine_report(channel):
    """Per-machine state durations and history for a calibrated camera."""
    cam = cameras.get(channel)
    if cam is None:
        return jsonify({"error": "Channel not found"}), 404
    anchors = _calibration.get(str(channel), [])
    return jsonify({
        "channel":    channel,
        "cam_name":   cam.name,
        "calibrated": len(anchors) > 0,
        "machines":   cam.get_machine_report(),
        "timestamp":  datetime.now().isoformat(),
    })


@app.route("/api/export/<int:channel>.csv")
def api_export_csv(channel):
    """Download full state-change history for all machines on a channel as CSV."""
    import io, csv as csv_mod
    cam = cameras.get(channel)
    if cam is None:
        return "Channel not found", 404
    report = cam.get_machine_report()

    buf = io.StringIO()
    w   = csv_mod.writer(buf)
    w.writerow(["Camera", "Machine", "State", "Start", "End", "Duration (s)", "Duration"])
    for m in report:
        for h in m["history"]:
            sec = h["duration_sec"]
            hrs = sec // 3600; mins = (sec % 3600) // 60; secs = sec % 60
            fmt = f"{int(hrs)}h {int(mins)}m {int(secs)}s"
            w.writerow([cam.name, m["label"], h["state"], h["start"], h["end"], sec, fmt])
        # Current (open) period
        now = datetime.now().isoformat()
        sec = m["duration_sec"]
        hrs = sec // 3600; mins = (sec % 3600) // 60; secs = sec % 60
        fmt = f"{int(hrs)}h {int(mins)}m {int(secs)}s (ongoing)"
        w.writerow([cam.name, m["label"], m["current_state"], m["state_since"], now, sec, fmt])

    buf.seek(0)
    fname = f"cnc_ch{channel}_{cam.name.replace(' ','_')}_{datetime.now().strftime('%Y%m%d')}.csv"
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment;filename={fname}"})


@app.route("/api/alerts/machine", methods=["GET"])
def api_machine_alerts():
    """Return recent machine-level duration alerts (idle/stop too long)."""
    return jsonify({
        "alerts": list(_machine_alert_queue),
        "config": _alert_config,
    })


@app.route("/api/config/alert-thresholds", methods=["GET", "PUT"])
def api_alert_thresholds():
    """GET or PUT alert threshold config.
    PUT body: {"idle_alert_min": 30, "stop_alert_min": 10}
    """
    global _alert_config
    if request.method == "PUT":
        body = request.get_json(force=True) or {}
        if "idle_alert_min" in body:
            _alert_config["idle_alert_min"] = max(1, int(body["idle_alert_min"]))
        if "stop_alert_min" in body:
            _alert_config["stop_alert_min"] = max(1, int(body["stop_alert_min"]))
    return jsonify(_alert_config)


# ─── Calibration API ──────────────────────────────────────────────────────────

@app.route("/api/calibration", methods=["GET"])
def api_get_calibration():
    return jsonify(_calibration)


@app.route("/api/calibration/<int:channel>", methods=["POST"])
def api_save_channel_calibration(channel):
    """
    Save calibration anchor points for a channel.
    Body: {"anchors": [{"x_norm": 0.4, "y_norm": 0.3, "label": "Machine 1", "tol": 60}, ...]}
    Replaces any existing calibration for this channel.
    """
    global _calibration
    data = request.get_json(force=True) or {}
    anchors = data.get("anchors", [])
    _calibration[str(channel)] = anchors
    save_calibration(_calibration)
    logger.info(f"[ch{channel}] Calibration updated: {len(anchors)} anchor(s)")
    return jsonify({"success": True, "channel": channel, "anchors": len(anchors)})


@app.route("/api/calibration/<int:channel>", methods=["DELETE"])
def api_clear_channel_calibration(channel):
    """Remove calibration for a channel — reverts to full-frame detection."""
    global _calibration
    _calibration.pop(str(channel), None)
    save_calibration(_calibration)
    return jsonify({"success": True, "channel": channel})


@app.route("/api/calibration/snapshot/<int:channel>")
def api_calibration_snapshot(channel):
    """
    Return a full-resolution annotated JPEG snapshot for the calibration UI.
    Includes the current raw (uncalibrated) blob detections drawn on top
    so the operator can see what the system is currently detecting.
    """
    global _OFFLINE_JPEG
    if _OFFLINE_JPEG is None:
        _OFFLINE_JPEG = _make_offline_jpeg()

    cam = cameras.get(channel)
    if cam is None:
        return Response(_OFFLINE_JPEG, mimetype="image/jpeg")

    frame = cam.get_frame()
    if frame is None:
        return Response(_OFFLINE_JPEG, mimetype="image/jpeg")

    # Run detection WITHOUT calibration so operator sees all raw candidates
    raw_det = detect_indicator_lights(frame, anchors=None)
    annotated = annotate_frame(frame, raw_det, cam.name)

    ret, buf = cv2.imencode(".jpg", annotated, [cv2.IMWRITE_JPEG_QUALITY, 85])
    if not ret:
        return Response(_OFFLINE_JPEG, mimetype="image/jpeg")

    return Response(buf.tobytes(), mimetype="image/jpeg",
                    headers={"Cache-Control": "no-cache"})


@app.route("/api/cameras")
def api_cameras():
    """Return status + light counts for all CNC cameras."""
    cam_data = {str(ch): cam.get_status() for ch, cam in cameras.items()}

    # Aggregate totals across all cameras
    # Note: machines visible in multiple cameras may be counted more than once.
    totals = {
        "working":        sum(c["working"]                  for c in cam_data.values()),
        "idle":           sum(c["idle"]                     for c in cam_data.values()),
        "manual_stop":    sum(c["manual_stop"]              for c in cam_data.values()),
        "process_finish": sum(c.get("process_finish", 0)    for c in cam_data.values()),
    }
    connected_cams = sum(1 for c in cam_data.values() if c["connected"])

    return jsonify({
        "cameras":          cam_data,
        "totals":           totals,
        "connected_cameras": connected_cams,
        "total_cameras":    len(cameras),
        "timestamp":        datetime.now().isoformat(),
    })


@app.route("/api/cameras/<int:channel>")
def api_camera_status(channel):
    if channel not in cameras:
        return jsonify({"error": "Channel not found"}), 404
    return jsonify(cameras[channel].get_status())


@app.route("/api/stream/<int:channel>")
def api_stream_channel(channel):
    """MJPEG stream for a specific camera channel (no ROI boxes, light circles only)."""
    global _OFFLINE_JPEG
    if _OFFLINE_JPEG is None:
        _OFFLINE_JPEG = _make_offline_jpeg()

    cam = cameras.get(channel)

    def generate():
        while True:
            try:
                if cam is not None:
                    frame = cam.get_annotated_frame()
                else:
                    frame = None

                if frame is not None:
                    ret, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 72])
                    jpeg = buf.tobytes() if ret else _OFFLINE_JPEG
                else:
                    jpeg = _OFFLINE_JPEG

                yield (b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + jpeg + b"\r\n")
            except GeneratorExit:
                break
            except Exception as e:
                logger.debug(f"Stream ch{channel} error: {e}")
                yield (b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + _OFFLINE_JPEG + b"\r\n")
            time.sleep(0.1)

    return Response(
        generate(),
        mimetype="multipart/x-mixed-replace; boundary=frame",
        headers={"Cache-Control": "no-cache", "Pragma": "no-cache"},
    )


@app.route("/api/stream")
def api_stream_default():
    """Default stream: first connected camera."""
    for ch in sorted(cameras.keys()):
        if cameras[ch].connected:
            return api_stream_channel(ch)
    # None connected — return offline frame for first channel
    first_ch = next(iter(cameras))
    return api_stream_channel(first_ch)


@app.route("/api/snapshot/<int:channel>")
def api_snapshot(channel):
    """Single JPEG snapshot for embedding in dashboard camera tiles."""
    global _OFFLINE_JPEG
    if _OFFLINE_JPEG is None:
        _OFFLINE_JPEG = _make_offline_jpeg()

    cam = cameras.get(channel)
    if cam is None:
        return Response(_OFFLINE_JPEG, mimetype="image/jpeg")

    frame = cam.get_annotated_frame()
    if frame is None:
        return Response(_OFFLINE_JPEG, mimetype="image/jpeg")

    ret, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
    if not ret:
        return Response(_OFFLINE_JPEG, mimetype="image/jpeg")

    return Response(buf.tobytes(), mimetype="image/jpeg",
                    headers={"Cache-Control": "no-cache"})


@app.route("/api/scan-channels")
def api_scan_channels():
    """
    Probe NVR channels 1-16 and report which are reachable.
    Use this to discover the correct channel numbers for your CNC cameras.
    WARNING: This makes 16 RTSP connections and takes ~60s to complete.
    """
    results = []
    for ch in range(1, 17):
        url = (f"rtsp://{NVR_USER}:{NVR_PASSWORD}@{NVR_HOST}:{NVR_PORT}"
               f"/cam/realmonitor?channel={ch}&subtype=1")
        try:
            cap = cv2.VideoCapture(url)
            cap.set(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 4000)
            cap.set(cv2.CAP_PROP_READ_TIMEOUT_MSEC, 4000)
            opened = cap.isOpened()
            frame_ok = False
            if opened:
                ret, _ = cap.read()
                frame_ok = ret
            cap.release()
            results.append({"channel": ch, "reachable": opened, "frame": frame_ok})
            logger.info(f"Scan ch{ch}: reachable={opened} frame={frame_ok}")
        except Exception as e:
            results.append({"channel": ch, "reachable": False, "error": str(e)})
    return jsonify({"results": results})


@app.route("/api/health")
def api_health():
    connected = sum(1 for c in cameras.values() if c.connected)
    return jsonify({
        "status":            "ok",
        "connected_cameras": connected,
        "total_cameras":     len(cameras),
        "timestamp":         datetime.now().isoformat(),
    })


# ─── Config: add/remove channels at runtime ───────────────────────────────────
@app.route("/api/config/cameras", methods=["GET"])
def api_get_camera_config():
    return jsonify({"channels": {str(k): v for k, v in CNC_CAMERAS.items()}})


@app.route("/api/config/cameras", methods=["PUT"])
def api_set_camera_channels():
    """
    Update which channels are monitored.
    Body: {"channels": {"6": "CNC 2", "7": "CNC 2 PASSAGE", ...}}
    """
    data = request.get_json(force=True) or {}
    new_channels = data.get("channels", {})
    global cameras
    # Stop old cameras not in new list
    for ch, cam in list(cameras.items()):
        if str(ch) not in new_channels:
            cam.stop()
            del cameras[ch]
    # Start new cameras
    for ch_str, name in new_channels.items():
        ch = int(ch_str)
        if ch not in cameras:
            cameras[ch] = CameraManager(ch, name)
            cameras[ch].start()
        else:
            cameras[ch].name = name
    return jsonify({"success": True, "active_channels": list(cameras.keys())})


# ─── Entry Point ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    logger.info("=" * 60)
    logger.info("CNC Machine Monitor — Multi-Camera Edition")
    if cameras:
        logger.info(f"Loaded {len(cameras)} saved cameras: {list(CNC_CAMERAS.values())}")
    else:
        logger.info("No cameras configured — open http://0.0.0.0:5000/setup to select cameras")
    logger.info("=" * 60)

    for cam in cameras.values():
        cam.start()

    if cameras:
        time.sleep(2)

    logger.info("Dashboard: http://0.0.0.0:5000")
    logger.info("Setup:     http://0.0.0.0:5000/setup")
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True, use_reloader=False)
