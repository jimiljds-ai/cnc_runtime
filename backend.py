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
CAMERAS_CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cameras.json")

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
# Indicator lights are small bright saturated LED domes on top of machines.
# Tighter HSV ranges (high S and V) exclude floor tape, reflections, walls.
LIGHT_SPECS = {
    "working": {  # Steady Green = Running
        "ranges": [
            (np.array([38, 120, 160]), np.array([88, 255, 255])),
        ],
        "color_bgr": (0, 220, 60),
    },
    "idle": {  # Yellow = Idle
        # High saturation minimum (≥150) is critical to exclude yellow floor tape
        "ranges": [
            (np.array([18, 150, 160]), np.array([35, 255, 255])),
        ],
        "color_bgr": (0, 200, 240),
    },
    "manual_stop": {  # Red = Manual Stop / Alarm
        "ranges": [
            (np.array([0,  140, 130]), np.array([10, 255, 255])),
            (np.array([165, 140, 130]), np.array([180, 255, 255])),
        ],
        "color_bgr": (50, 50, 240),
    },
    "process_finish": {  # Blinking Green = Process Complete (derived in CameraManager)
        "ranges": [],  # No direct HSV — detected via blink analysis
        "color_bgr": (255, 210, 0),  # Cyan to distinguish from steady green
    },
}

_MORPH_OPEN_K  = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
_MORPH_CLOSE_K = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))


def detect_indicator_lights(frame: np.ndarray) -> dict:
    """
    Scan the full frame for CNC indicator light blobs.
    Filters aggressively to only match small, bright, circular LED dome lights.
    Returns {"working": N, "idle": N, "manual_stop": N, "process_finish": N, "blobs": [...]}
    """
    if frame is None or frame.size == 0:
        return {"working": 0, "idle": 0, "manual_stop": 0, "process_finish": 0, "blobs": []}

    h, w = frame.shape[:2]
    total_px = h * w
    # Stack lights are small domes — keep a tight size window
    min_area = max(12, int(total_px * 0.000015))   # ~12 px² floor
    max_area = int(total_px * 0.0012)              # ≤0.12% of frame — much smaller than before

    blurred   = cv2.GaussianBlur(frame, (3, 3), 0)
    hsv       = cv2.cvtColor(blurred, cv2.COLOR_BGR2HSV)
    v_channel = hsv[:, :, 2]  # brightness channel for LED validation

    results = {"working": 0, "idle": 0, "manual_stop": 0, "process_finish": 0, "blobs": []}

    for state, spec in LIGHT_SPECS.items():
        if not spec["ranges"]:
            continue  # process_finish derived later

        mask = np.zeros(hsv.shape[:2], dtype=np.uint8)
        for lo, hi in spec["ranges"]:
            mask = cv2.bitwise_or(mask, cv2.inRange(hsv, lo, hi))

        # Open removes single-pixel noise; close fills small holes inside the dome
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN,  _MORPH_OPEN_K)
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, _MORPH_CLOSE_K)

        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        for cnt in contours:
            area = cv2.contourArea(cnt)
            if not (min_area <= area <= max_area):
                continue

            perimeter = cv2.arcLength(cnt, True)
            if perimeter == 0:
                continue
            circularity = 4 * np.pi * area / (perimeter ** 2)
            if circularity < 0.35:  # stack lights are round — reject elongated shapes
                continue

            # Aspect ratio: dome lights are close to square
            x, y, bw, bh = cv2.boundingRect(cnt)
            aspect = bw / bh if bh > 0 else 0
            if not (0.35 <= aspect <= 2.8):
                continue

            # Brightness gate: LED indicator lights are much brighter than floor tape
            cnt_mask = np.zeros(v_channel.shape, dtype=np.uint8)
            cv2.drawContours(cnt_mask, [cnt], -1, 255, -1)
            mean_v = cv2.mean(v_channel, mask=cnt_mask)[0]
            if mean_v < 155:
                continue

            (cx, cy), radius = cv2.minEnclosingCircle(cnt)
            results[state] += 1
            results["blobs"].append((int(cx), int(cy), max(5, int(radius * 2.0)), state))

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
                        raw_det = detect_indicator_lights(frame)

                        # Blink detection: track green presence over last 30 frames
                        self._green_history.append(raw_det["working"] > 0)
                        effective_det = dict(raw_det)
                        if len(self._green_history) >= 20:
                            ratio = sum(self._green_history) / len(self._green_history)
                            transitions = sum(
                                1 for i in range(1, len(self._green_history))
                                if self._green_history[i] != self._green_history[i - 1]
                            )
                            if 0.15 <= ratio <= 0.85 and transitions >= 4:
                                # Blinking green → Process Finish
                                effective_det["process_finish"] = effective_det["working"]
                                effective_det["working"] = 0
                                effective_det["blobs"] = [
                                    (x, y, r, "process_finish" if s == "working" else s)
                                    for x, y, r, s in effective_det.get("blobs", [])
                                ]

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
        return {
            "channel":       self.channel,
            "name":          self.name,
            "connected":     self.connected,
            "error":         self.connection_error,
            "active_url":    self.active_url,
            "frame_count":   self.frame_count,
            "working":        det["working"],
            "idle":           det["idle"],
            "manual_stop":    det["manual_stop"],
            "process_finish": det.get("process_finish", 0),
            "last_updated":   datetime.now().isoformat(),
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


# ─── Global Camera Pool ────────────────────────────────────────────────────────
# Empty dict if not configured yet — setup page will populate it.
cameras: dict = {
    ch: CameraManager(ch, name) for ch, name in CNC_CAMERAS.items()
}

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
