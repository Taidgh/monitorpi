"""
MonitorPi — client.py
Camera loop: frame capture, detection (lightning/wildlife), rolling buffer,
event upload via queue worker, live stream push.
"""

import cv2
import time
import threading
import queue
import io
import requests
import numpy as np
from collections import deque

# ── Config ────────────────────────────────────────────────────────────────
DEVICE_ID       = "MonitorPi-01"
SERVER          = "http://127.0.0.1:8000"
VIDEO_DEVICE    = "/dev/video0"
FPS_SLEEP       = 0.033            # ~30 fps

# Detection defaults — overridden at runtime by server /api/sensitivity
PEAK_DELTA       = 30.0   # lightning: 99th-pct brightness spike threshold
PRE_FLASH_BUF    = 30     # frames before flash
POST_FLASH_BUF   = 20     # frames after flash

DIFF_THRESHOLD   = 25     # wildlife: pixel diff to count as motion
MOTION_SCORE     = 500    # wildlife: motion pixel count to trigger

# Buffers
PRE_WILDLIFE_BUF = 15     # frames before motion

# Stream
STREAM_EVERY    = 2                # push every Nth frame

# ── Upload queue worker ────────────────────────────────────────────────────
_upload_q: queue.Queue = queue.Queue(maxsize=200)


def _upload_worker():
    """Single dedicated thread for all HTTP uploads."""
    session = requests.Session()
    while True:
        task = _upload_q.get()
        if task is None:
            break
        kind, data = task["kind"], task["data"]
        try:
            if kind == "image":
                session.post(
                    f"{SERVER}/upload/image/{DEVICE_ID}",
                    files={"file": ("capture.jpg", io.BytesIO(data), "image/jpeg")},
                    timeout=10,
                )
            elif kind == "stream":
                session.post(
                    f"{SERVER}/stream/upload/{DEVICE_ID}",
                    files={"file": ("frame.jpg", io.BytesIO(data), "image/jpeg")},
                    timeout=5,
                )
        except Exception as e:
            print(f"[upload worker] {kind} error: {e}")
        _upload_q.task_done()


def _enqueue_image(jpeg_bytes: bytes):
    try:
        _upload_q.put_nowait({"kind": "image", "data": jpeg_bytes})
    except queue.Full:
        print("[client] Upload queue full — dropping image")


def _enqueue_stream(jpeg_bytes: bytes):
    try:
        _upload_q.put_nowait({"kind": "stream", "data": jpeg_bytes})
    except queue.Full:
        pass  # Drop stream frames silently when queue is full


# ── Camera helpers ─────────────────────────────────────────────────────────

def open_camera() -> cv2.VideoCapture:
    cap = cv2.VideoCapture(VIDEO_DEVICE, cv2.CAP_V4L2)
    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
    cap.set(cv2.CAP_PROP_FRAME_WIDTH,  1920)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 1080)
    cap.set(cv2.CAP_PROP_FPS, 30)
    if not cap.isOpened():
        raise RuntimeError(f"Cannot open camera at {VIDEO_DEVICE}")
    return cap


def apply_lightning_camera(cap: cv2.VideoCapture):
    cap.set(cv2.CAP_PROP_AUTO_EXPOSURE, 1)   # manual on V4L2
    cap.set(cv2.CAP_PROP_EXPOSURE, -4)


def apply_wildlife_camera(cap: cv2.VideoCapture):
    cap.set(cv2.CAP_PROP_AUTO_EXPOSURE, 3)   # auto on V4L2
    cap.set(cv2.CAP_PROP_AUTO_WB, 1)


def encode_jpeg(frame, quality=85) -> bytes:
    ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        raise RuntimeError("JPEG encode failed")
    return buf.tobytes()


def brighten_frame(frame):
    return cv2.convertScaleAbs(frame, alpha=4.0, beta=50)


# ── Server polling helpers ─────────────────────────────────────────────────

def poll_mode() -> str:
    try:
        r = requests.get(f"{SERVER}/api/mode", timeout=3)
        return r.json().get("mode", "wildlife")
    except Exception:
        return "wildlife"


def poll_stream() -> bool:
    try:
        r = requests.get(f"{SERVER}/stream/{DEVICE_ID}", timeout=3)
        return r.json().get("stream", False)
    except Exception:
        return False


def poll_sensitivity() -> dict:
    try:
        r = requests.get(f"{SERVER}/api/sensitivity", timeout=3)
        raw = r.json()
        return {k: v["val"] for k, v in raw.items()}
    except Exception:
        return {}


# ── Startup diagnostics ────────────────────────────────────────────────────

def startup_diagnostics(cap: cv2.VideoCapture, mode: str):
    ok, frame = cap.read()
    if ok:
        brightness = float(np.mean(frame))
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        pct99 = float(np.percentile(gray, 99))
        print(f"[client] Startup brightness: {brightness:.1f}  99th-pct: {pct99:.1f}")
    else:
        print("[client] Warning: could not read startup frame")

    try:
        requests.get(f"{SERVER}/api/mode", timeout=3)
        print(f"[client] Server reachable. Mode: {mode}")
    except Exception:
        print("[client] Warning: server not reachable at startup")


# ── Main loop ──────────────────────────────────────────────────────────────

def run():
    # Start upload worker
    worker = threading.Thread(target=_upload_worker, daemon=True)
    worker.start()

    # Open camera
    cap = open_camera()

    # Initial mode
    mode = poll_mode()
    if mode == "lightning":
        apply_lightning_camera(cap)
    else:
        apply_wildlife_camera(cap)

    startup_diagnostics(cap, mode)

    # Rolling pre-event buffers
    pre_lightning = deque(maxlen=PRE_FLASH_BUF)
    pre_wildlife  = deque(maxlen=PRE_WILDLIFE_BUF)

    # Detection state
    prev_pct99: float | None = None
    prev_gray: np.ndarray | None = None

    # Event / cooldown state
    post_count    = 0
    post_target   = 0
    in_event      = False
    cooldown_until = 0.0

    # Stream / mode / sensitivity poll timers
    last_stream_poll      = 0.0
    last_mode_poll        = 0.0
    last_sensitivity_poll = 0.0
    stream_on             = False

    # Live detection values (updated from server every 10 s)
    live_peak_delta     = PEAK_DELTA
    live_diff_threshold = DIFF_THRESHOLD
    live_motion_score   = MOTION_SCORE

    frame_n = 0

    print("[client] Entering capture loop…")

    try:
        while True:
            loop_start = time.monotonic()

            ok, frame = cap.read()
            if not ok:
                print("[client] Frame read failed — reopening camera")
                cap.release()
                time.sleep(1)
                cap = open_camera()
                if mode == "lightning":
                    apply_lightning_camera(cap)
                else:
                    apply_wildlife_camera(cap)
                continue

            now = time.time()
            frame_n += 1

            # ── Poll mode every 5 s ──
            if now - last_mode_poll >= 5:
                last_mode_poll = now
                new_mode = poll_mode()
                if new_mode != mode:
                    mode = new_mode
                    print(f"[client] Mode changed → {mode}")
                    if mode == "lightning":
                        apply_lightning_camera(cap)
                    else:
                        apply_wildlife_camera(cap)
                    # Reset detection state
                    prev_pct99 = None
                    prev_gray  = None
                    in_event   = False
                    post_count = 0

            # ── Poll sensitivity every 10 s ──
            if now - last_sensitivity_poll >= 10:
                last_sensitivity_poll = now
                sv = poll_sensitivity()
                if sv:
                    live_peak_delta     = sv.get("peak_delta",     live_peak_delta)
                    live_diff_threshold = sv.get("diff_threshold", live_diff_threshold)
                    live_motion_score   = sv.get("motion_score",   live_motion_score)

            # ── Poll stream every 2 s ──
            if now - last_stream_poll >= 2:
                last_stream_poll = now
                stream_on = poll_stream()

            # ── Push stream frame (every 2nd frame) ──
            if stream_on and (frame_n % STREAM_EVERY == 0):
                if mode == "lightning":
                    push_frame = brighten_frame(frame)
                else:
                    push_frame = frame
                _enqueue_stream(encode_jpeg(push_frame, quality=70))

            # ── Detection & buffering ──
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

            if mode == "lightning":
                pre_lightning.append(frame.copy())
                pct99 = float(np.percentile(gray, 99))

                if not in_event and now >= cooldown_until and prev_pct99 is not None:
                    if pct99 - prev_pct99 >= live_peak_delta:
                        print(f"[client] ⚡ Lightning trigger! Δ={pct99-prev_pct99:.1f}")
                        in_event   = True
                        post_count = 0
                        post_target = POST_FLASH_BUF
                        # Upload pre-buffer
                        for f in pre_lightning:
                            _enqueue_image(encode_jpeg(f))

                prev_pct99 = pct99

            else:  # wildlife
                pre_wildlife.append(frame.copy())

                if not in_event and now >= cooldown_until and prev_gray is not None:
                    diff  = cv2.absdiff(gray, prev_gray)
                    score = int(np.count_nonzero(diff > live_diff_threshold))
                    if score >= live_motion_score:
                        print(f"[client] 🌿 Motion trigger! score={score}")
                        in_event   = True
                        post_count = 0
                        post_target = 10
                        for f in pre_wildlife:
                            _enqueue_image(encode_jpeg(f))

            # ── Post-event frames ──
            if in_event:
                _enqueue_image(encode_jpeg(frame))
                post_count += 1
                if post_count >= post_target:
                    in_event = False
                    cooldown = 3.0 if mode == "lightning" else 5.0
                    cooldown_until = now + cooldown
                    print(f"[client] Event done — cooldown {cooldown}s")

            prev_gray = gray

            # ── Sleep remainder of frame budget ──
            elapsed = time.monotonic() - loop_start
            sleep_t = max(0.0, FPS_SLEEP - elapsed)
            time.sleep(sleep_t)

    except KeyboardInterrupt:
        print("[client] Interrupted — shutting down")
    finally:
        cap.release()
        _upload_q.put(None)  # stop worker
        worker.join(timeout=5)
        print("[client] Exited cleanly")


if __name__ == "__main__":
    run()
