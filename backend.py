#!/usr/bin/env python3
"""
AI-Based CNC Machine Status Monitor — Backend
Monitors indicator lights from a surveillance camera feed to classify
the state of 10 CNC machines in real time.
"""

import cv2
import numpy as np
import threading
import time
import logging
import os
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

# ─── Camera Configuration ─────────────────────────────────────────────────────
# CP PLUS NVR at 192.168.4.10
# Channel numbers visible in NVR sidebar (1-indexed). Change ACTIVE_CHANNEL
# to whichever channel shows the most CNC machines with indicator lights.
# Channels seen in NVR: CAD CAM=1, OPC CENTER=2, CONFERENCE=3, PANNEL AREA=4,
#   PASSAGE 1=5, CNC 2=6, CNC 2 PASSAGE=7, CNC 1 PASSAGE=8, CNC 1=9
ACTIVE_CHANNEL = 9   # <-- change this to the channel number that shows all CNCs

CAMERA_CONFIG = {
    "base_url": "https://192.168.4.10",
    "username": "admin",
    "password": "Admin@1234",
    "reconnect_delay": 5,
    "frame_timeout": 10,
    # CP PLUS NVR uses Dahua-compatible RTSP format:
    # rtsp://user:pass@ip:554/cam/realmonitor?channel=N&subtype=0
    "rtsp_urls": [
        # Main stream — try each CNC channel
        f"rtsp://admin:Admin@1234@192.168.4.10:554/cam/realmonitor?channel={ACTIVE_CHANNEL}&subtype=0",
        f"rtsp://admin:Admin@1234@192.168.4.10:554/cam/realmonitor?channel={ACTIVE_CHANNEL}&subtype=1",
        # Try channels 6–9 (CNC 2, CNC 2 PASSAGE, CNC 1 PASSAGE, CNC 1)
        "rtsp://admin:Admin@1234@192.168.4.10:554/cam/realmonitor?channel=6&subtype=0",
        "rtsp://admin:Admin@1234@192.168.4.10:554/cam/realmonitor?channel=7&subtype=0",
        "rtsp://admin:Admin@1234@192.168.4.10:554/cam/realmonitor?channel=8&subtype=0",
        "rtsp://admin:Admin@1234@192.168.4.10:554/cam/realmonitor?channel=9&subtype=0",
        # Alternative CP PLUS NVR formats
        f"rtsp://admin:Admin@1234@192.168.4.10:554/ch{ACTIVE_CHANNEL:02d}/main/av_stream",
        f"rtsp://admin:Admin@1234@192.168.4.10:554/Streaming/Channels/{ACTIVE_CHANNEL}01",
    ],
    # CP PLUS HTTP snapshot fallback
    "stream_paths": [
        f"/cgi-bin/snapshot.cgi?channel={ACTIVE_CHANNEL}",
        f"/cgi-bin/mjpg/video.cgi?channel={ACTIVE_CHANNEL}&subtype=0",
        f"/onvif/snapshot?channel={ACTIVE_CHANNEL}",
        "/cgi-bin/snapshot.cgi",
        "/cgi-bin/mjpg/video.cgi",
        "/video",
        "/stream",
    ],
}

# ─── Machine ROI Configuration ────────────────────────────────────────────────
# Fractional coordinates [x_start, y_start, width, height] relative to frame.
# Indicator light sub-region within each ROI: top-center 30% of the ROI height.
# Adjust these to match the actual camera view of your factory floor.
DEFAULT_MACHINE_ROIS = {
    1:  {"x": 0.02, "y": 0.05, "w": 0.17, "h": 0.42, "name": "CNC-001"},
    2:  {"x": 0.21, "y": 0.05, "w": 0.17, "h": 0.42, "name": "CNC-002"},
    3:  {"x": 0.40, "y": 0.05, "w": 0.17, "h": 0.42, "name": "CNC-003"},
    4:  {"x": 0.59, "y": 0.05, "w": 0.17, "h": 0.42, "name": "CNC-004"},
    5:  {"x": 0.78, "y": 0.05, "w": 0.17, "h": 0.42, "name": "CNC-005"},
    6:  {"x": 0.02, "y": 0.54, "w": 0.17, "h": 0.42, "name": "CNC-006"},
    7:  {"x": 0.21, "y": 0.54, "w": 0.17, "h": 0.42, "name": "CNC-007"},
    8:  {"x": 0.40, "y": 0.54, "w": 0.17, "h": 0.42, "name": "CNC-008"},
    9:  {"x": 0.59, "y": 0.54, "w": 0.17, "h": 0.42, "name": "CNC-009"},
    10: {"x": 0.78, "y": 0.54, "w": 0.17, "h": 0.42, "name": "CNC-010"},
}

# ─── HSV Color Detection Thresholds ──────────────────────────────────────────
# Tuned for typical industrial indicator lights.
# Each entry: list of (lower, upper) HSV bound pairs + minimum pixel coverage %.
COLOR_THRESHOLDS = {
    "red": {
        "ranges": [
            (np.array([0,   120,  80]), np.array([10,  255, 255])),
            (np.array([165, 120,  80]), np.array([180, 255, 255])),
        ],
        "min_pct": 3.0,
    },
    "green": {
        "ranges": [
            (np.array([38,  60,  60]), np.array([88, 255, 255])),
        ],
        "min_pct": 3.0,
    },
    "yellow": {
        "ranges": [
            (np.array([18, 100, 100]), np.array([38, 255, 255])),
        ],
        "min_pct": 3.0,
    },
}

MACHINE_STATES = {
    "working":     {"label": "Working",     "color": "#28a745"},
    "idle":        {"label": "Idle",        "color": "#ffc107"},
    "manual_stop": {"label": "Manual Stop", "color": "#dc3545"},
    "off":         {"label": "OFF",         "color": "#6c757d"},
    "unknown":     {"label": "Unknown",     "color": "#17a2b8"},
}

SHIFT_HOURS = 8


# ─── Machine State Tracker ────────────────────────────────────────────────────
class MachineStateTracker:
    def __init__(self, machine_id: int, name: str):
        self.machine_id = machine_id
        self.name = name
        self._lock = threading.Lock()
        self.current_state = "unknown"
        self.previous_state = None
        self.state_since = datetime.now()
        self.session_start = datetime.now()
        self.state_history: deque = deque(maxlen=20000)
        self.state_history.append((datetime.now(), "unknown"))
        self.manual_stop_count = 0
        self.alerts: deque = deque(maxlen=100)

    def update_state(self, new_state: str):
        with self._lock:
            if new_state == self.current_state:
                return None
            now = datetime.now()
            self.previous_state = self.current_state
            self.current_state = new_state
            self.state_since = now
            self.state_history.append((now, new_state))
            alert = None
            if new_state == "manual_stop":
                self.manual_stop_count += 1
                alert = {
                    "timestamp": now.isoformat(),
                    "machine_id": self.machine_id,
                    "machine_name": self.name,
                    "type": "manual_stop",
                    "message": f"{self.name} entered Manual Stop",
                    "severity": "high",
                }
                self.alerts.appendleft(alert)
            elif new_state == "off" and self.previous_state == "working":
                alert = {
                    "timestamp": now.isoformat(),
                    "machine_id": self.machine_id,
                    "machine_name": self.name,
                    "type": "machine_off",
                    "message": f"{self.name} turned OFF unexpectedly",
                    "severity": "medium",
                }
                self.alerts.appendleft(alert)
            return alert

    def get_metrics(self) -> dict:
        with self._lock:
            now = datetime.now()
            shift_start = now - timedelta(hours=SHIFT_HOURS)
            history = [(ts, st) for ts, st in self.state_history if ts >= shift_start]

            durations = {"working": 0.0, "idle": 0.0, "manual_stop": 0.0, "off": 0.0, "unknown": 0.0}
            for i, (ts, st) in enumerate(history):
                next_ts = history[i + 1][0] if i + 1 < len(history) else now
                secs = (next_ts - ts).total_seconds()
                durations[st] = durations.get(st, 0.0) + secs

            total = max(sum(durations.values()), 1.0)
            current_secs = (now - self.state_since).total_seconds()

            def fmt(seconds: float) -> str:
                m, s = divmod(int(seconds), 60)
                h, m = divmod(m, 60)
                return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"

            return {
                "machine_id": self.machine_id,
                "name": self.name,
                "current_state": self.current_state,
                "state_label": MACHINE_STATES.get(self.current_state, {}).get("label", "Unknown"),
                "state_color": MACHINE_STATES.get(self.current_state, {}).get("color", "#6c757d"),
                "state_since": self.state_since.isoformat(),
                "current_duration_seconds": int(current_secs),
                "current_duration_fmt": fmt(current_secs),
                "uptime_pct": round(durations["working"] / total * 100, 1),
                "idle_pct": round(durations["idle"] / total * 100, 1),
                "manual_stop_pct": round(durations["manual_stop"] / total * 100, 1),
                "working_seconds": int(durations["working"]),
                "idle_seconds": int(durations["idle"]),
                "manual_stop_seconds": int(durations["manual_stop"]),
                "working_fmt": fmt(durations["working"]),
                "idle_fmt": fmt(durations["idle"]),
                "manual_stop_fmt": fmt(durations["manual_stop"]),
                "manual_stop_count": self.manual_stop_count,
                "last_updated": now.isoformat(),
            }

    def get_alerts(self) -> list:
        with self._lock:
            return list(self.alerts)


# ─── Camera Manager ───────────────────────────────────────────────────────────
class CameraManager:
    def __init__(self, config: dict):
        self.config = config
        self._frame = None
        self._lock = threading.Lock()
        self.running = False
        self.connected = False
        self.connection_error: str = None
        self.frame_count = 0
        self.active_url: str = None
        self._cap: cv2.VideoCapture = None
        self._http_mode = False
        self._thread: threading.Thread = None

    # --- Connection helpers ---
    def _try_rtsp(self) -> bool:
        for url in self.config["rtsp_urls"]:
            try:
                cap = cv2.VideoCapture(url)
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 2)
                cap.set(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 5000)
                cap.set(cv2.CAP_PROP_READ_TIMEOUT_MSEC, 5000)
                if cap.isOpened():
                    ret, frame = cap.read()
                    if ret and frame is not None:
                        logger.info(f"Camera connected via RTSP: {url}")
                        self._cap = cap
                        self.active_url = url
                        self._http_mode = False
                        return True
                cap.release()
            except Exception as e:
                logger.debug(f"RTSP {url} failed: {e}")
        return False

    def _try_http(self) -> bool:
        base = self.config["base_url"]
        auth = (self.config["username"], self.config["password"])
        for path in self.config["stream_paths"]:
            url = f"{base}{path}"
            try:
                resp = requests.get(url, auth=auth, stream=True, verify=False, timeout=5)
                if resp.status_code == 200:
                    logger.info(f"Camera connected via HTTP: {url}")
                    self.active_url = url
                    self._http_mode = True
                    resp.close()
                    return True
                resp.close()
            except Exception as e:
                logger.debug(f"HTTP {url} failed: {e}")
        return False

    def _fetch_http_frame(self):
        auth = (self.config["username"], self.config["password"])
        try:
            resp = requests.get(
                self.active_url, auth=auth, stream=True, verify=False,
                timeout=self.config["frame_timeout"]
            )
            buf = b""
            for chunk in resp.iter_content(chunk_size=8192):
                buf += chunk
                a = buf.find(b"\xff\xd8")
                b = buf.find(b"\xff\xd9")
                if a != -1 and b != -1:
                    jpg = buf[a : b + 2]
                    buf = buf[b + 2 :]
                    img = cv2.imdecode(np.frombuffer(jpg, dtype=np.uint8), cv2.IMREAD_COLOR)
                    resp.close()
                    return img
                if len(buf) > 1_000_000:
                    buf = buf[-200_000:]
            resp.close()
        except Exception as e:
            logger.debug(f"HTTP frame error: {e}")
        return None

    # --- Capture loop ---
    def _loop(self):
        while self.running:
            if not self.connected:
                if self._try_rtsp() or self._try_http():
                    self.connected = True
                    self.connection_error = None
                else:
                    self.connection_error = "Cannot connect to camera"
                    logger.warning(
                        f"Camera offline — retrying in {self.config['reconnect_delay']}s"
                    )
                    time.sleep(self.config["reconnect_delay"])
                    continue

            try:
                if not self._http_mode and self._cap and self._cap.isOpened():
                    ret, frame = self._cap.read()
                    if ret and frame is not None:
                        with self._lock:
                            self._frame = frame
                            self.frame_count += 1
                    else:
                        logger.warning("RTSP read failed — reconnecting")
                        self._cap.release()
                        self._cap = None
                        self.connected = False
                else:
                    frame = self._fetch_http_frame()
                    if frame is not None:
                        with self._lock:
                            self._frame = frame
                            self.frame_count += 1
                    else:
                        logger.warning("HTTP frame fetch failed — reconnecting")
                        self.connected = False
                        time.sleep(1)
            except Exception as e:
                logger.error(f"Capture loop error: {e}")
                self.connected = False
                if self._cap:
                    self._cap.release()
                    self._cap = None
                time.sleep(self.config["reconnect_delay"])

    def start(self):
        self.running = True
        self._thread = threading.Thread(target=self._loop, daemon=True, name="camera-capture")
        self._thread.start()
        logger.info("Camera manager started")

    def stop(self):
        self.running = False
        if self._cap:
            self._cap.release()

    def get_frame(self):
        with self._lock:
            return self._frame.copy() if self._frame is not None else None

    def status(self) -> dict:
        return {
            "connected": self.connected,
            "error": self.connection_error,
            "frame_count": self.frame_count,
            "active_url": self.active_url,
        }


# ─── Color Detection ──────────────────────────────────────────────────────────
def detect_light_color(roi: np.ndarray) -> str:
    """Return 'red', 'green', 'yellow', or 'off' for the indicator light in roi."""
    if roi is None or roi.size == 0:
        return "unknown"

    # Focus on the top-center stripe of the ROI where the indicator light sits
    h, w = roi.shape[:2]
    strip_h = max(1, int(h * 0.35))
    strip_x = max(0, int(w * 0.20))
    strip_w = max(1, int(w * 0.60))
    light_region = roi[0:strip_h, strip_x : strip_x + strip_w]

    hsv = cv2.cvtColor(light_region, cv2.COLOR_BGR2HSV)
    total_px = light_region.shape[0] * light_region.shape[1]
    if total_px == 0:
        return "unknown"

    # Check overall brightness — low brightness means no light
    brightness_mask = cv2.inRange(hsv, np.array([0, 40, 80]), np.array([180, 255, 255]))
    lit_pct = cv2.countNonZero(brightness_mask) / total_px * 100
    if lit_pct < 2.5:
        return "off"

    best_color = "off"
    best_pct = 0.0
    for color_name, cfg in COLOR_THRESHOLDS.items():
        mask = np.zeros(hsv.shape[:2], dtype=np.uint8)
        for lo, hi in cfg["ranges"]:
            mask = cv2.bitwise_or(mask, cv2.inRange(hsv, lo, hi))
        pct = cv2.countNonZero(mask) / total_px * 100
        if pct >= cfg["min_pct"] and pct > best_pct:
            best_pct = pct
            best_color = color_name

    return best_color


def color_to_state(color: str) -> str:
    return {"red": "manual_stop", "green": "working", "yellow": "idle", "off": "off"}.get(
        color, "unknown"
    )


def _hex_to_bgr(hex_color: str):
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i : i + 2], 16) for i in (0, 2, 4))
    return (b, g, r)


# ─── Frame Processor ──────────────────────────────────────────────────────────
class FrameProcessor:
    def __init__(self, machine_rois: dict, trackers: dict):
        self.machine_rois = machine_rois
        self.trackers = trackers
        self._alerts: deque = deque(maxlen=200)
        self._lock = threading.Lock()

    def process(self, frame: np.ndarray) -> np.ndarray:
        if frame is None:
            return None
        h, w = frame.shape[:2]
        annotated = frame.copy()

        for mid, roi_cfg in self.machine_rois.items():
            x = max(0, int(roi_cfg["x"] * w))
            y = max(0, int(roi_cfg["y"] * h))
            bw = max(1, min(int(roi_cfg["w"] * w), w - x))
            bh = max(1, min(int(roi_cfg["h"] * h), h - y))

            roi = frame[y : y + bh, x : x + bw]
            color = detect_light_color(roi)
            state = color_to_state(color)

            alert = self.trackers[mid].update_state(state)
            if alert:
                with self._lock:
                    self._alerts.appendleft(alert)

            s_info = MACHINE_STATES.get(state, MACHINE_STATES["unknown"])
            bgr = _hex_to_bgr(s_info["color"])
            cv2.rectangle(annotated, (x, y), (x + bw, y + bh), bgr, 2)
            label = f"{roi_cfg['name']}: {s_info['label']}"
            cv2.putText(
                annotated, label, (x + 2, y - 5),
                cv2.FONT_HERSHEY_SIMPLEX, 0.45, bgr, 1, cv2.LINE_AA,
            )

        return annotated

    def get_global_alerts(self) -> list:
        with self._lock:
            return list(self._alerts)


# ─── Global Singletons ────────────────────────────────────────────────────────
machine_rois = DEFAULT_MACHINE_ROIS
trackers = {i: MachineStateTracker(i, cfg["name"]) for i, cfg in machine_rois.items()}
camera = CameraManager(CAMERA_CONFIG)
processor = FrameProcessor(machine_rois, trackers)

# ─── Flask App ────────────────────────────────────────────────────────────────
app = Flask(__name__, static_folder=".", static_url_path="")
CORS(app)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


@app.route("/")
def index():
    return send_from_directory(BASE_DIR, "dashboard.html")


@app.route("/style.css")
def serve_css():
    return send_from_directory(BASE_DIR, "style.css")


@app.route("/app.js")
def serve_js():
    return send_from_directory(BASE_DIR, "app.js")


@app.route("/api/status")
def api_status():
    machines = {mid: t.get_metrics() for mid, t in trackers.items()}
    states = [m["current_state"] for m in machines.values()]
    summary = {
        "working": states.count("working"),
        "idle": states.count("idle"),
        "manual_stop": states.count("manual_stop"),
        "off": states.count("off"),
        "unknown": states.count("unknown"),
    }
    return jsonify({
        "machines": machines,
        "summary": summary,
        "camera": camera.status(),
        "timestamp": datetime.now().isoformat(),
        "total_machines": len(machines),
    })


@app.route("/api/machines/<int:machine_id>")
def api_machine(machine_id):
    if machine_id not in trackers:
        return jsonify({"error": "Machine not found"}), 404
    return jsonify(trackers[machine_id].get_metrics())


@app.route("/api/alerts")
def api_alerts():
    all_alerts = processor.get_global_alerts()
    for t in trackers.values():
        all_alerts.extend(t.get_alerts())
    all_alerts.sort(key=lambda a: a["timestamp"], reverse=True)
    unique = {a["timestamp"] + a["machine_name"]: a for a in all_alerts}
    return jsonify({"alerts": list(unique.values())[:50]})


@app.route("/api/config/roi", methods=["GET"])
def api_get_roi():
    return jsonify({"rois": machine_rois})


@app.route("/api/config/roi/<int:machine_id>", methods=["PUT"])
def api_update_roi(machine_id):
    if machine_id not in machine_rois:
        return jsonify({"error": "Machine not found"}), 404
    data = request.get_json(force=True) or {}
    for k in ("x", "y", "w", "h"):
        if k in data:
            machine_rois[machine_id][k] = float(data[k])
    if "name" in data:
        machine_rois[machine_id]["name"] = str(data["name"])
        trackers[machine_id].name = str(data["name"])
    processor.machine_rois = machine_rois
    return jsonify({"success": True, "roi": machine_rois[machine_id]})


@app.route("/api/stream")
def api_stream():
    def generate():
        while True:
            frame = camera.get_frame()
            if frame is not None:
                out = processor.process(frame) or frame
            else:
                out = np.zeros((480, 640, 3), dtype=np.uint8)
                cv2.putText(out, "Camera Offline", (150, 240),
                            cv2.FONT_HERSHEY_SIMPLEX, 1.2, (0, 0, 220), 2, cv2.LINE_AA)
            ret, buf = cv2.imencode(".jpg", out, [cv2.IMWRITE_JPEG_QUALITY, 72])
            if ret:
                yield (
                    b"--frame\r\nContent-Type: image/jpeg\r\n\r\n"
                    + buf.tobytes()
                    + b"\r\n"
                )
            time.sleep(0.08)

    return Response(generate(), mimetype="multipart/x-mixed-replace; boundary=frame")


@app.route("/api/frame")
def api_frame():
    frame = camera.get_frame()
    if frame is None:
        return jsonify({"error": "No frame available"}), 503
    out = processor.process(frame) or frame
    ret, buf = cv2.imencode(".jpg", out, [cv2.IMWRITE_JPEG_QUALITY, 85])
    if not ret:
        return jsonify({"error": "Encoding failed"}), 500
    return Response(buf.tobytes(), mimetype="image/jpeg")


@app.route("/api/health")
def api_health():
    return jsonify({
        "status": "ok",
        "camera_connected": camera.connected,
        "frame_count": camera.frame_count,
        "timestamp": datetime.now().isoformat(),
    })


# ─── Background Processing Loop ──────────────────────────────────────────────
def _processing_loop():
    logger.info("Frame processing loop started")
    while True:
        frame = camera.get_frame()
        if frame is not None:
            processor.process(frame)
        time.sleep(0.4)


# ─── Entry Point ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    logger.info("=" * 60)
    logger.info("CNC Machine Monitor — Starting up")
    logger.info("=" * 60)
    camera.start()

    proc_thread = threading.Thread(target=_processing_loop, daemon=True, name="frame-processor")
    proc_thread.start()

    time.sleep(2)
    logger.info("Dashboard available at http://0.0.0.0:5000")
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True, use_reloader=False)
