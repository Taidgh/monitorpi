"""
MonitorPi — server.py
FastAPI backend: image storage, MJPEG streaming, mode management, stats.
App name, device ID and video device are read from environment variables
so the installer can configure them without editing this file.
"""

import io
import json
import os
import time
import zipfile
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import (
    HTMLResponse, StreamingResponse, FileResponse, JSONResponse
)
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# ── Runtime config (set by installer via environment / service file) ────────
APP_NAME  = os.environ.get("MONITORPI_APP_NAME",  "MonitorPi")

# Favicon embedded as base64 so no static file server is needed
_FAVICON_TAG = '<link rel="icon" type="image/x-icon" href="data:image/x-icon;base64,AAABAAEAEBAAAAAAIAD9AAAAFgAAAIlQTkcNChoKAAAADUlIRFIAAAAQAAAAEAgGAAAAH/P/YQAAAMRJREFUeJxjYBhowIhLwnmB+390sb0JOzHUM1HqArgBabuz/iPTxAK4k9J2Z/2/+/Qug7K0MsPdp3cJaoR5B+4CmCZkzTf23YRjZHBj3014GOEMgxv7bjIIywvBMcwQdBqrATDNF5vOBl5sOhsoqiwKN0TDSZ2BgYEBTuN0gaiyKE4+TDMDAwMDCy4DGBgYGFwWeqzHJ4/TBRpO6gyv775GEXt99zWKzQRdIKosimIIupcIGoBPEzKAewFbOscHSFWPEwAAQsdLbLVBqTkAAAAASUVORK5CYII=">' 
DEVICE_ID = os.environ.get("MONITORPI_DEVICE_ID", "MonitorPi-01")
VIDEO_DEV = os.environ.get("MONITORPI_VIDEO_DEV", "/dev/video0")

DATA_ROOT      = Path("data")
STREAM_FPS_CAP = 15
BOUNDARY       = b"--frame"

app = FastAPI(title=APP_NAME)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
                   allow_headers=["*"])

# ── Sensitivity config ────────────────────────────────────────────────────
#   Each key: val=default, min=least sensitive, max=most sensitive, step
#   Lightning: lower PEAK_DELTA  → triggers more easily
#   Wildlife : lower DIFF/SCORE  → triggers more easily
SENSITIVITY_CFG = {
    "peak_delta":     {"val": 30.0, "min": 5.0,  "max": 80.0, "step": 1.0},
    "diff_threshold": {"val": 35,   "min": 20,   "max": 80,   "step": 1},
    "motion_score":   {"val": 1500, "min": 500,  "max": 12000, "step": 100},
    # Lightning exposure — V4L2 manual exposure scale (more negative = shorter exposure)
    "exposure":       {"val": -4,   "min": -11,  "max": -1,   "step": 1},
}

# ── In-memory state ────────────────────────────────────────────────────────
_state: dict = {
    "mode": "wildlife",
    "stream_enabled": {},
    "latest_frame": {},
    "stats": {},
    "start_time": time.time(),
    "sensitivity": {k: v["val"] for k, v in SENSITIVITY_CFG.items()},
}

# ── Persistent config (survives reboots) ───────────────────────────────────
# Stored in data/ so it's never wiped by reinstalls and isn't lost with the
# rest of the application code; only the captured images directory is bigger.
CONFIG_PATH = DATA_ROOT / "config.json"


def _load_config():
    """Load persisted mode + sensitivity from disk, if present."""
    if not CONFIG_PATH.exists():
        return
    try:
        with open(CONFIG_PATH, "r") as f:
            saved = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"[server] Could not read {CONFIG_PATH}: {e}")
        return

    if saved.get("mode") in ("lightning", "wildlife"):
        _state["mode"] = saved["mode"]

    saved_sens = saved.get("sensitivity", {})
    for key, cfg in SENSITIVITY_CFG.items():
        if key in saved_sens:
            val = saved_sens[key]
            # Clamp to current configured range in case ranges changed since save
            try:
                clamped = max(cfg["min"], min(cfg["max"], val))
                _state["sensitivity"][key] = clamped
            except TypeError:
                pass

    print(f"[server] Loaded saved config: mode={_state['mode']}, "
          f"sensitivity={_state['sensitivity']}")


def _save_config():
    """Persist mode + sensitivity to disk so they survive reboots."""
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    payload = {
        "mode": _state["mode"],
        "sensitivity": _state["sensitivity"],
    }
    try:
        tmp_path = CONFIG_PATH.with_suffix(".json.tmp")
        with open(tmp_path, "w") as f:
            json.dump(payload, f, indent=2)
        tmp_path.replace(CONFIG_PATH)  # atomic write
    except OSError as e:
        print(f"[server] Could not save {CONFIG_PATH}: {e}")


_load_config()


def _device_dir(device_id: str) -> Path:
    p = DATA_ROOT / device_id / "images"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _ensure_stats(device_id: str):
    if device_id not in _state["stats"]:
        _state["stats"][device_id] = {
            "total_frames": 0,
            "event_count": 0,
            "first_seen": None,
            "last_seen": None,
        }


# ══════════════════════════════════════════════════════════════════════════
# Image upload / serve / delete
# ══════════════════════════════════════════════════════════════════════════

@app.post("/upload/image/{device_id}")
async def upload_image(device_id: str, file: UploadFile = File(...)):
    _ensure_stats(device_id)
    d = _device_dir(device_id)
    fname = f"{time.time():.6f}.jpg"
    fpath = d / fname
    data = await file.read()
    fpath.write_bytes(data)
    s = _state["stats"][device_id]
    now = time.time()
    s["last_seen"] = now
    if s["first_seen"] is None:
        s["first_seen"] = now
    s["event_count"] += 1
    return {"filename": fname, "size": len(data)}


@app.get("/image/{device_id}/{filename}")
async def serve_image(device_id: str, filename: str):
    p = _device_dir(device_id) / filename
    if not p.exists():
        raise HTTPException(404, "Image not found")
    return FileResponse(str(p), media_type="image/jpeg")


@app.delete("/image/{device_id}/{filename}")
async def delete_image(device_id: str, filename: str):
    p = _device_dir(device_id) / filename
    if not p.exists():
        raise HTTPException(404, "Image not found")
    p.unlink()
    return {"deleted": filename}


# ══════════════════════════════════════════════════════════════════════════
# Live stream
# ══════════════════════════════════════════════════════════════════════════

@app.post("/stream/upload/{device_id}")
async def stream_upload(device_id: str, file: UploadFile = File(...)):
    data = await file.read()
    _state["latest_frame"][device_id] = data
    _ensure_stats(device_id)
    _state["stats"][device_id]["total_frames"] += 1
    return {"ok": True}


@app.get("/stream/{device_id}")
async def stream_state(device_id: str):
    return {"stream": _state["stream_enabled"].get(device_id, False)}


@app.post("/stream/{device_id}/toggle")
async def stream_toggle(device_id: str):
    current = _state["stream_enabled"].get(device_id, False)
    _state["stream_enabled"][device_id] = not current
    return {"stream": _state["stream_enabled"][device_id]}


async def _async_sleep(seconds: float):
    import asyncio
    await asyncio.sleep(seconds)


async def _mjpeg_generator(device_id: str):
    while True:
        frame = _state["latest_frame"].get(device_id)
        if frame:
            yield (
                BOUNDARY + b"\r\n"
                b"Content-Type: image/jpeg\r\n\r\n"
                + frame + b"\r\n"
            )
        await _async_sleep(1 / STREAM_FPS_CAP)


@app.get("/mjpeg/{device_id}")
async def mjpeg_stream(device_id: str):
    return StreamingResponse(
        _mjpeg_generator(device_id),
        media_type="multipart/x-mixed-replace; boundary=frame",
        headers={"Cache-Control": "no-cache"},
    )


# ══════════════════════════════════════════════════════════════════════════
# Mode API
# ══════════════════════════════════════════════════════════════════════════

class ModeBody(BaseModel):
    mode: str


@app.get("/api/mode")
async def get_mode():
    return {"mode": _state["mode"]}


@app.post("/api/mode")
async def set_mode(body: ModeBody):
    if body.mode not in ("lightning", "wildlife"):
        raise HTTPException(400, "mode must be 'lightning' or 'wildlife'")
    _state["mode"] = body.mode
    _save_config()
    return {"mode": _state["mode"]}


# ══════════════════════════════════════════════════════════════════════════
# Sensitivity API
# ══════════════════════════════════════════════════════════════════════════

class SensitivityBody(BaseModel):
    peak_delta:     float | None = None
    diff_threshold: int   | None = None
    motion_score:   int   | None = None
    exposure:       int   | None = None


@app.get("/api/sensitivity")
async def get_sensitivity():
    s = _state["sensitivity"]
    cfg = SENSITIVITY_CFG
    return {
        "peak_delta":     {"val": s["peak_delta"],     **{k:v for k,v in cfg["peak_delta"].items()     if k!="val"}},
        "diff_threshold": {"val": s["diff_threshold"], **{k:v for k,v in cfg["diff_threshold"].items() if k!="val"}},
        "motion_score":   {"val": s["motion_score"],   **{k:v for k,v in cfg["motion_score"].items()   if k!="val"}},
        "exposure":       {"val": s["exposure"],       **{k:v for k,v in cfg["exposure"].items()       if k!="val"}},
    }


@app.post("/api/sensitivity")
async def set_sensitivity(body: SensitivityBody):
    s = _state["sensitivity"]
    cfg = SENSITIVITY_CFG
    if body.peak_delta is not None:
        s["peak_delta"] = max(cfg["peak_delta"]["min"], min(cfg["peak_delta"]["max"], body.peak_delta))
    if body.diff_threshold is not None:
        s["diff_threshold"] = max(cfg["diff_threshold"]["min"], min(cfg["diff_threshold"]["max"], int(body.diff_threshold)))
    if body.motion_score is not None:
        s["motion_score"] = max(cfg["motion_score"]["min"], min(cfg["motion_score"]["max"], int(body.motion_score)))
    if body.exposure is not None:
        s["exposure"] = max(cfg["exposure"]["min"], min(cfg["exposure"]["max"], int(body.exposure)))
    _save_config()
    return {"sensitivity": s}


# ══════════════════════════════════════════════════════════════════════════
# Bulk operations
# ══════════════════════════════════════════════════════════════════════════

class DownloadBody(BaseModel):
    filenames: list[str]


@app.post("/api/download/{device_id}")
async def download_zip(device_id: str, body: DownloadBody):
    d = _device_dir(device_id)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for fname in body.filenames:
            p = d / fname
            if p.exists():
                zf.write(p, fname)
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename={APP_NAME.lower().replace(' ','_')}_export.zip"},
    )


@app.delete("/api/images/{device_id}")
async def delete_all_images(device_id: str):
    d = _device_dir(device_id)
    count = 0
    for f in d.glob("*.jpg"):
        f.unlink()
        count += 1
    _ensure_stats(device_id)
    s = _state["stats"][device_id]
    s["event_count"] = 0
    s["first_seen"] = None
    s["last_seen"] = None
    return {"deleted": count}


# ══════════════════════════════════════════════════════════════════════════
# Stats
# ══════════════════════════════════════════════════════════════════════════

@app.get("/api/stats/{device_id}")
async def get_stats(device_id: str):
    _ensure_stats(device_id)
    s = _state["stats"][device_id]
    d = _device_dir(device_id)
    total_photos = len(list(d.glob("*.jpg")))
    events = s["event_count"]
    avg_per_event = round(total_photos / events, 1) if events else 0
    uptime_days = round((time.time() - _state["start_time"]) / 86400, 1)
    return {
        "total_frames": s["total_frames"],
        "event_count": events,
        "total_photos": total_photos,
        "last_seen": s["last_seen"],
        "first_seen": s["first_seen"],
        "avg_frames_per_event": avg_per_event,
        "uptime_days": uptime_days,
        "stream": _state["stream_enabled"].get(device_id, False),
        "mode": _state["mode"],
        "device_id": device_id,
        "app_name": APP_NAME,
    }


# ══════════════════════════════════════════════════════════════════════════
# Shared HTML helpers
# ══════════════════════════════════════════════════════════════════════════

def _theme_vars(mode: str) -> str:
    if mode == "lightning":
        return """
        --bg: #121212; --surface: #1e1e1e; --surface2: #2c2c2c;
        --on-bg: #e0e0e0; --on-surface: #cfcfcf;
        --accent: #ffa726; --accent2: #ffca28;
        --live-dot: #66bb6a; --border: #333;
        --chip-bg: #2c2c2c; --del-btn: #ef5350; --dl-btn: #42a5f5;
        --nav-active: #ffa726; --footer-bg: #0a0a0a;
        """
    else:
        return """
        --bg: #fafafa; --surface: #ffffff; --surface2: #f5f5f5;
        --on-bg: #212121; --on-surface: #424242;
        --accent: #43a047; --accent2: #66bb6a;
        --live-dot: #1e88e5; --border: #e0e0e0;
        --chip-bg: #e8f5e9; --del-btn: #e53935; --dl-btn: #1e88e5;
        --nav-active: #43a047; --footer-bg: #eeeeee;
        """


_COMMON_CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  font-family: 'Roboto', sans-serif;
  background: var(--bg); color: var(--on-bg);
  min-height: 100vh; display: flex; flex-direction: column;
}
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }

.topbar {
  position: sticky; top: 0; z-index: 100;
  background: var(--surface); border-bottom: 1px solid var(--border);
  display: flex; align-items: center;
  padding: 0 20px; height: 56px;
  box-shadow: 0 2px 4px rgba(0,0,0,.15); gap: 16px;
}
.topbar .brand {
  font-size: 1.2rem; font-weight: 700; color: var(--accent);
  letter-spacing: .5px; display: flex; align-items: center; gap: 8px;
}
.topbar nav { display: flex; gap: 4px; }
.topbar nav a {
  padding: 6px 14px; border-radius: 20px;
  font-size: .9rem; font-weight: 500; color: var(--on-surface);
  transition: background .15s;
}
.topbar nav a:hover { background: var(--surface2); text-decoration: none; }
.topbar nav a.active { color: var(--nav-active); background: var(--chip-bg); }
.topbar .spacer { flex: 1; }

.mode-toggle { display: flex; align-items: center; gap: 8px; font-size: .85rem; color: var(--on-surface); }
.toggle-track {
  position: relative; width: 44px; height: 24px;
  background: var(--surface2); border-radius: 12px;
  cursor: pointer; border: 1px solid var(--border); transition: background .2s;
}
.toggle-track.lightning { background: #ffa726; }
.toggle-thumb {
  position: absolute; top: 3px; left: 3px;
  width: 16px; height: 16px; background: #fff;
  border-radius: 50%; transition: transform .2s;
}
.toggle-track.lightning .toggle-thumb { transform: translateX(20px); }

.btn {
  display: inline-flex; align-items: center; gap: 6px;
  padding: 8px 18px; border-radius: 4px;
  font-size: .9rem; font-weight: 500; cursor: pointer;
  border: none; transition: opacity .15s;
}
.btn:disabled { opacity: .4; cursor: default; }
.btn:not(:disabled):hover { opacity: .85; }
.btn-accent { background: var(--accent); color: #fff; }
.btn-del { background: var(--del-btn); color: #fff; }
.btn-dl { background: var(--dl-btn); color: #fff; }
.btn-ghost { background: transparent; color: var(--on-surface); border: 1px solid var(--border); }

.card { background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 16px; }

/* Footer */
.site-footer {
  margin-top: auto;
  background: var(--footer-bg);
  border-top: 1px solid var(--border);
  padding: 14px 20px;
  text-align: center;
  font-size: .78rem;
  color: var(--on-surface);
  opacity: .65;
  line-height: 1.7;
}
.site-footer a { color: var(--accent); opacity: 1; }

/* Sensitivity card */
.sens-card { margin-top: 12px; }
.sens-title {
  font-weight: 600; margin-bottom: 12px;
  display: flex; align-items: center; gap: 6px;
}
.sens-group { margin-bottom: 14px; }
.sens-group:last-of-type { margin-bottom: 0; }
.sens-label {
  display: flex; justify-content: space-between;
  font-size: .82rem; color: var(--on-surface);
  margin-bottom: 4px;
}
.sens-label .sens-val { font-weight: 600; color: var(--accent); }
.sens-range {
  width: 100%; -webkit-appearance: none; appearance: none;
  height: 4px; border-radius: 2px;
  background: linear-gradient(to right, var(--accent) 0%, var(--accent) var(--pct,50%), var(--border) var(--pct,50%));
  outline: none; cursor: pointer;
}
.sens-range::-webkit-slider-thumb {
  -webkit-appearance: none; width: 16px; height: 16px;
  border-radius: 50%; background: var(--accent);
  border: 2px solid var(--surface); box-shadow: 0 1px 3px rgba(0,0,0,.3);
}
.sens-range::-moz-range-thumb {
  width: 14px; height: 14px; border-radius: 50%;
  background: var(--accent); border: 2px solid var(--surface);
}
.sens-hint { font-size: .74rem; color: var(--on-surface); opacity: .5; margin-top: 2px; }
.sens-apply {
  width: 100%; margin-top: 10px; padding: 8px;
  background: var(--accent); color: #fff; border: none;
  border-radius: 4px; font-size: .88rem; font-weight: 600;
  cursor: pointer; opacity: 1; transition: opacity .15s;
}
.sens-apply:hover { opacity: .85; }
.sens-apply.saved { background: #66bb6a; }
"""


def _footer_html() -> str:
    return """<footer class="site-footer">
  Built by <a href="https://github.com/Taidgh" target="_blank" rel="noopener">github.com/Taidgh</a>
  &nbsp;·&nbsp;
  Based on <a href="https://github.com/sheet315/stormwatch-pi" target="_blank" rel="noopener">StormWatch-Pi</a> by sheet315
</footer>"""


def _topbar_html(mode: str, device_id: str, active: str) -> str:
    is_lightning = mode == "lightning"
    toggle_cls = "toggle-track lightning" if is_lightning else "toggle-track"
    mode_label = "⚡ Lightning" if is_lightning else "🌿 Wildlife"
    live_cls   = "active" if active == "live" else ""
    photo_cls  = "active" if active == "photos" else ""
    return f"""<div class="topbar">
  <div class="brand"><span class="material-icons">videocam</span>{APP_NAME}</div>
  <nav>
    <a href="/" class="{live_cls}">Live</a>
    <a href="/events/{device_id}" class="{photo_cls}">Photos</a>
  </nav>
  <div class="spacer"></div>
  <div class="mode-toggle">
    🌿
    <div class="{toggle_cls}" id="modeToggle" onclick="toggleMode()" title="Switch mode">
      <div class="toggle-thumb"></div>
    </div>
    🌙
    <span id="modeLabel" style="margin-left:4px;font-weight:500">{mode_label}</span>
  </div>
</div>"""


_TOGGLE_MODE_JS = """
async function toggleMode() {
  const cur = document.getElementById('modeToggle').classList.contains('lightning');
  const newMode = cur ? 'wildlife' : 'lightning';
  await fetch('/api/mode', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({mode:newMode})});
  location.reload();
}
"""


# ══════════════════════════════════════════════════════════════════════════
# HTML pages
# ══════════════════════════════════════════════════════════════════════════

@app.get("/", response_class=HTMLResponse)
async def live_page():
    mode = _state["mode"]
    device_id = DEVICE_ID

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{APP_NAME} — Live</title>
{_FAVICON_TAG}
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Roboto:wght@400;500;700&display=swap" rel="stylesheet">
<link href="https://fonts.googleapis.com/icon?family=Material+Icons" rel="stylesheet">
<style>
:root {{ {_theme_vars(mode)} }}
{_COMMON_CSS}

.page {{ display: flex; gap: 20px; padding: 20px; max-width: 1400px; margin: 0 auto; flex: 1; }}
.main {{ flex: 1; min-width: 63rem; }}
.video-wrap {{
  position: relative; width: 100%; padding-top: 56.25%;
  background: #000; border-radius: 8px; overflow: hidden;
}}
.video-wrap img, .video-wrap .placeholder {{
  position: absolute; inset: 0; width: 100%; height: 100%; object-fit: cover;
}}
.placeholder {{
  display: flex; flex-direction: column; align-items: center;
  justify-content: center; gap: 12px; color: #555;
}}
.placeholder .material-icons {{ font-size: 64px; color: #333; }}
.stream-ctrl {{ margin-top: 12px; display: flex; align-items: center; gap: 12px; }}
.live-badge {{ display: flex; align-items: center; gap: 6px; font-size: .8rem; font-weight: 600; color: var(--live-dot); }}
.live-dot {{ width: 8px; height: 8px; background: var(--live-dot); border-radius: 50%; animation: pulse 1.2s infinite; }}
@keyframes pulse {{ 0%,100% {{ opacity:1; }} 50% {{ opacity:.3; }} }}

.sidebar {{ width: 280px; flex-shrink: 0; }}
.stat-row {{
  display: flex; justify-content: space-between;
  padding: 8px 0; border-bottom: 1px solid var(--border); font-size: .88rem;
}}
.stat-row:last-child {{ border-bottom: none; }}
.stat-label {{ color: var(--on-surface); opacity: .7; }}
.stat-val {{ font-weight: 500; }}
.sidebar .card + .card {{ margin-top: 12px; }}
.view-link {{
  display: block; text-align: center; margin-top: 12px;
  padding: 8px; border-radius: 4px;
  background: var(--chip-bg); color: var(--accent);
  font-size: .9rem; font-weight: 500;
}}
</style>
</head>
<body>
{_topbar_html(mode, device_id, "live")}

<div class="page">
  <div class="main">
    <div class="video-wrap" id="videoWrap">
      <div class="placeholder" id="placeholder">
        <span class="material-icons">videocam_off</span>
        <span>Camera Off</span>
      </div>
      <img id="mjpegImg" style="display:none" alt="Live feed">
    </div>
    <div class="stream-ctrl">
      <button class="btn btn-accent" onclick="toggleStream()" id="streamBtn">
        <span class="material-icons" id="streamIcon">play_arrow</span>
        <span id="streamTxt">Start Stream</span>
      </button>
      <div class="live-badge" id="liveBadge" style="display:none">
        <div class="live-dot"></div>LIVE
      </div>
    </div>
  </div>

  <aside class="sidebar">
    <div class="card" id="statsCard">
      <div style="font-weight:600;margin-bottom:10px;display:flex;align-items:center;gap:6px">
        <span class="material-icons" style="font-size:18px">bar_chart</span>Stats
      </div>
      <div id="statsRows"><div class="stat-row"><span class="stat-label">Loading…</span></div></div>
    </div>
    <a href="/events/{device_id}" class="view-link">View All Photos →</a>

    <!-- Sensitivity card — content injected by JS based on mode -->
    <div class="card sens-card" id="sensCard">
      <div class="sens-title">
        <span class="material-icons" style="font-size:18px">tune</span>Sensitivity
      </div>
      <div id="sensControls"><!-- populated by loadSensitivity() --></div>
      <button class="sens-apply" id="sensApply" onclick="applySensitivity()">Apply</button>
    </div>
  </aside>
</div>

{_footer_html()}

<script>
const DEVICE = "{device_id}";
const CURRENT_MODE = "{mode}";
let streaming = false;

async function toggleStream() {{
  const r = await fetch(`/stream/${{DEVICE}}/toggle`, {{method:'POST'}});
  const j = await r.json();
  setStream(j.stream);
}}

function setStream(on) {{
  streaming = on;
  const img = document.getElementById('mjpegImg');
  const ph  = document.getElementById('placeholder');
  const icon = document.getElementById('streamIcon');
  const txt  = document.getElementById('streamTxt');
  const badge = document.getElementById('liveBadge');
  if (on) {{
    img.src = `/mjpeg/${{DEVICE}}?t=${{Date.now()}}`;
    img.style.display = '';
    ph.style.display = 'none';
    badge.style.display = '';
    icon.textContent = 'stop';
    txt.textContent = 'Stop Stream';
  }} else {{
    img.src = '';
    img.style.display = 'none';
    ph.style.display = '';
    badge.style.display = 'none';
    icon.textContent = 'play_arrow';
    txt.textContent = 'Start Stream';
  }}
}}

async function loadStats() {{
  try {{
    const r = await fetch(`/api/stats/${{DEVICE}}`);
    const s = await r.json();
    const fmt = ts => ts ? new Date(ts*1000).toLocaleString() : '—';
    document.getElementById('statsRows').innerHTML = `
      <div class="stat-row"><span class="stat-label">Total Photos</span><span class="stat-val">${{s.total_photos}}</span></div>
      <div class="stat-row"><span class="stat-label">Events Captured</span><span class="stat-val">${{s.event_count}}</span></div>
      <div class="stat-row"><span class="stat-label">Photos/Event</span><span class="stat-val">${{s.avg_frames_per_event}}</span></div>
      <div class="stat-row"><span class="stat-label">Last Capture</span><span class="stat-val" style="font-size:.8rem">${{fmt(s.last_seen)}}</span></div>
      <div class="stat-row"><span class="stat-label">First Capture</span><span class="stat-val" style="font-size:.8rem">${{fmt(s.first_seen)}}</span></div>
      <div class="stat-row"><span class="stat-label">Days Running</span><span class="stat-val">${{s.uptime_days}}</span></div>
      <div class="stat-row"><span class="stat-label">Device</span><span class="stat-val">${{s.device_id}}</span></div>
      <div class="stat-row"><span class="stat-label">Stream</span><span class="stat-val">${{s.stream ? '🟢 On' : '⚫ Off'}}</span></div>
      <div class="stat-row"><span class="stat-label">Mode</span><span class="stat-val">${{s.mode}}</span></div>
      <div class="stat-row"><span class="stat-label">Time</span><span class="stat-val" id="clock"></span></div>
    `;
    setStream(s.stream);
  }} catch(e) {{ console.error(e); }}
}}

function tickClock() {{
  const el = document.getElementById('clock');
  if (el) el.textContent = new Date().toLocaleTimeString();
}}

{_TOGGLE_MODE_JS}

// ── Sensitivity sliders ──────────────────────────────────────────────────
let _sensitivity = {{}};

function _pct(val, min, max) {{
  return Math.round(((val - min) / (max - min)) * 100);
}}

function _sliderHtml(id, label, hint, val, min, max, step) {{
  const pct = _pct(val, min, max);
  return `
  <div class="sens-group">
    <div class="sens-label">
      <span>${{label}}</span>
      <span class="sens-val" id="lbl_${{id}}">${{val}}</span>
    </div>
    <input type="range" class="sens-range" id="sldr_${{id}}"
      min="${{min}}" max="${{max}}" step="${{step}}" value="${{val}}"
      style="--pct:${{pct}}%"
      oninput="onSlide('${{id}}', this)">
    <div class="sens-hint">${{hint}}</div>
  </div>`;
}}

function onSlide(id, el) {{
  const val = parseFloat(el.value);
  document.getElementById('lbl_' + id).textContent = val;
  const min = parseFloat(el.min), max = parseFloat(el.max);
  el.style.setProperty('--pct', _pct(val, min, max) + '%');
}}

async function loadSensitivity() {{
  try {{
    const r = await fetch('/api/sensitivity');
    _sensitivity = await r.json();
    renderSensitivity();
  }} catch(e) {{ console.error('sensitivity load failed', e); }}
}}

function renderSensitivity() {{
  const s = _sensitivity;
  let html = '';
  if (CURRENT_MODE === 'lightning') {{
    html += _sliderHtml('peak_delta', '⚡ Trigger threshold',
      'Lower = more sensitive (triggers on smaller brightness spikes)',
      s.peak_delta.val, s.peak_delta.min, s.peak_delta.max, s.peak_delta.step);
    html += _sliderHtml('exposure', '⚡ Exposure time',
      'More negative = shorter exposure (less light, freezes fast flashes). Less negative = longer exposure (more light, may blow out bright strikes)',
      s.exposure.val, s.exposure.min, s.exposure.max, s.exposure.step);
  }} else {{
    html += _sliderHtml('diff_threshold', '🌿 Pixel diff threshold',
      'Lower = more sensitive (detects subtler pixel changes)',
      s.diff_threshold.val, s.diff_threshold.min, s.diff_threshold.max, s.diff_threshold.step);
    html += _sliderHtml('motion_score', '🌿 Motion area threshold',
      'Lower = more sensitive (triggers on smaller movement area)',
      s.motion_score.val, s.motion_score.min, s.motion_score.max, s.motion_score.step);
  }}
  document.getElementById('sensControls').innerHTML = html;
}}

async function applySensitivity() {{
  const body = {{}};
  if (CURRENT_MODE === 'lightning') {{
    const el = document.getElementById('sldr_peak_delta');
    if (el) body.peak_delta = parseFloat(el.value);
    const ex = document.getElementById('sldr_exposure');
    if (ex) body.exposure = parseInt(ex.value);
  }} else {{
    const d = document.getElementById('sldr_diff_threshold');
    const m = document.getElementById('sldr_motion_score');
    if (d) body.diff_threshold = parseInt(d.value);
    if (m) body.motion_score   = parseInt(m.value);
  }}
  try {{
    await fetch('/api/sensitivity', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify(body),
    }});
    const btn = document.getElementById('sensApply');
    btn.textContent = '✓ Saved';
    btn.classList.add('saved');
    setTimeout(() => {{ btn.textContent = 'Apply'; btn.classList.remove('saved'); }}, 1500);
  }} catch(e) {{ console.error('sensitivity save failed', e); }}
}}

loadStats();
loadSensitivity();
setInterval(loadStats, 10000);
setInterval(tickClock, 1000);
tickClock();
</script>
</body>
</html>"""
    return HTMLResponse(html)


@app.get("/events/{device_id}", response_class=HTMLResponse)
async def events_page(device_id: str):
    mode = _state["mode"]
    is_lightning = mode == "lightning"
    page_title = "⚡ Lightning Strikes" if is_lightning else "🌿 Wildlife Sightings"

    d = _device_dir(device_id)
    files = sorted(d.glob("*.jpg"), key=lambda p: float(p.stem), reverse=True)

    events: list[list[str]] = []
    current: list[str] = []
    prev_ts: Optional[float] = None
    MAX_EVENTS = 30
    MAX_THUMBS = 200  # show all; thumb-row is height-capped with scroll

    for p in files:
        try:
            ts = float(p.stem)
        except ValueError:
            continue
        if prev_ts is not None and (prev_ts - ts) > 10:
            events.append(current)
            current = []
            if len(events) >= MAX_EVENTS:
                break
        current.append(p.name)
        prev_ts = ts

    if current and len(events) < MAX_EVENTS:
        events.append(current)

    total_photos = sum(len(e) for e in events)

    gallery_html = ""
    for ev in events:
        thumbs = ev[:MAX_THUMBS]
        ts = float(thumbs[0].split(".")[0]) if thumbs else 0
        dt_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
        gallery_html += f"""
<div class="event-group">
  <div class="event-header">
    <span class="event-time">{dt_str}</span>
    <span class="event-chip">{len(ev)} photo{'s' if len(ev)!=1 else ''}</span>
  </div>
  <div class="thumb-row-wrap">
  <div class="thumb-row">
"""
        for fname in thumbs:
            gallery_html += f"""    <div class="thumb-wrap" data-fname="{fname}"
        onclick="thumbTap(this,event)"
        oncontextmenu="thumbLong(this,event)">
      <img loading="lazy" src="/image/{device_id}/{fname}" alt="">
    </div>
"""
        gallery_html += "  </div>\n  </div>\n</div>\n"  # close thumb-row, thumb-row-wrap, event-group

    if not gallery_html:
        gallery_html = '<div class="empty-state"><span class="material-icons">photo_library</span><p>No photos yet</p></div>'

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{APP_NAME} — Photos</title>
{_FAVICON_TAG}
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Roboto:wght@400;500;700&display=swap" rel="stylesheet">
<link href="https://fonts.googleapis.com/icon?family=Material+Icons" rel="stylesheet">
<style>
:root {{ {_theme_vars(mode)} }}
{_COMMON_CSS}

.page {{ padding: 20px; max-width: 1400px; margin: 0 auto; flex: 1; }}
.page-header {{ display: flex; align-items: center; gap: 12px; margin-bottom: 20px; flex-wrap: wrap; }}
.page-header h1 {{ font-size: 1.4rem; font-weight: 700; }}
.count-chip {{ background: var(--chip-bg); color: var(--accent); padding: 4px 12px; border-radius: 16px; font-size: .85rem; font-weight: 500; }}
.header-actions {{ margin-left: auto; display: flex; gap: 8px; flex-wrap: wrap; }}

.event-group {{ margin-bottom: 28px; }}
.event-header {{ display: flex; align-items: center; gap: 10px; margin-bottom: 10px; }}
.event-time {{ font-size: .85rem; color: var(--on-surface); opacity: .7; }}
.event-chip {{ background: var(--chip-bg); color: var(--accent); padding: 2px 10px; border-radius: 12px; font-size: .78rem; }}
.thumb-row-wrap {{
  position: relative;
}}
.thumb-row {{
  display: flex; flex-wrap: wrap; gap: 8px;
  max-height: 286px;  /* exactly 3 rows: 3×90px + 2×8px gap */
  overflow-y: auto;
  overflow-x: hidden;
  scroll-behavior: smooth;
  padding-bottom: 2px;
}}
/* Subtle scrollbar styling */
.thumb-row::-webkit-scrollbar {{ width: 4px; }}
.thumb-row::-webkit-scrollbar-track {{ background: transparent; }}
.thumb-row::-webkit-scrollbar-thumb {{ background: var(--border); border-radius: 2px; }}
/* Fade hint when content overflows */
.thumb-row-wrap::after {{
  content: '';
  position: absolute; bottom: 0; left: 0; right: 0; height: 32px;
  background: linear-gradient(transparent, var(--bg));
  pointer-events: none;
  opacity: 0;
  transition: opacity .2s;
}}
.thumb-row-wrap.overflows::after {{ opacity: 1; }}
.thumb-wrap {{
  width: 140px; height: 90px; border-radius: 6px; overflow: hidden;
  cursor: pointer; position: relative; flex-shrink: 0;
  border: 2px solid transparent; transition: border-color .15s;
  background: var(--surface2);
}}
.thumb-wrap img {{ width: 100%; height: 100%; object-fit: cover; display: block; }}
.thumb-wrap.selected {{ border-color: var(--accent); }}
.thumb-wrap.selected::after {{
  content: '✓'; position: absolute; top: 4px; right: 6px;
  background: var(--accent); color: #fff;
  width: 20px; height: 20px; border-radius: 50%;
  display: flex; align-items: center; justify-content: center; font-size: 12px;
}}

.empty-state {{ text-align: center; padding: 80px 20px; color: var(--on-surface); opacity: .4; }}
.empty-state .material-icons {{ font-size: 64px; display: block; margin-bottom: 12px; }}

.lightbox {{
  display: none; position: fixed; inset: 0; z-index: 200;
  background: rgba(0,0,0,.92); flex-direction: column;
  align-items: center; justify-content: center;
}}
.lightbox.open {{ display: flex; }}
.lightbox img {{ max-width: 90vw; max-height: 80vh; object-fit: contain; border-radius: 4px; }}
.lb-bar {{
  position: fixed; top: 0; left: 0; right: 0;
  padding: 12px 16px; display: flex; align-items: center; gap: 8px;
  background: rgba(0,0,0,.6);
}}
.lb-counter {{ flex: 1; text-align: center; color: #fff; font-size: .9rem; }}
.lb-nav {{
  position: fixed; top: 50%; transform: translateY(-50%);
  background: rgba(255,255,255,.15); border: none; color: #fff;
  width: 44px; height: 44px; border-radius: 50%; cursor: pointer;
  font-size: 24px; display: flex; align-items: center; justify-content: center;
  transition: background .15s;
}}
.lb-nav:hover {{ background: rgba(255,255,255,.3); }}
#lbPrev {{ left: 12px; }} #lbNext {{ right: 12px; }}
.lb-close, .lb-del {{ background: none; border: none; cursor: pointer; color: #fff; font-size: 18px; padding: 4px 8px; border-radius: 4px; display: flex; align-items: center; gap: 4px; }}
.lb-del {{ color: #ef9a9a; }}
.lb-close:hover {{ background: rgba(255,255,255,.1); }}
.lb-del:hover {{ background: rgba(239,83,80,.2); }}

.sel-bar {{
  display: none; position: fixed; bottom: 0; left: 0; right: 0;
  background: var(--surface); border-top: 1px solid var(--border);
  padding: 12px 20px; flex-direction: row; align-items: center;
  gap: 10px; z-index: 150; box-shadow: 0 -2px 8px rgba(0,0,0,.1);
}}
.sel-bar.open {{ display: flex; }}
.sel-count {{ flex: 1; font-size: .9rem; font-weight: 500; }}

.overlay {{ display: none; position: fixed; inset: 0; z-index: 300; background: rgba(0,0,0,.6); align-items: center; justify-content: center; }}
.overlay.open {{ display: flex; }}
.dialog {{ background: var(--surface); border-radius: 8px; padding: 24px; max-width: 380px; width: 90%; box-shadow: 0 8px 32px rgba(0,0,0,.3); }}
.dialog h3 {{ margin-bottom: 12px; }}
.dialog p {{ color: var(--on-surface); opacity: .7; margin-bottom: 20px; font-size: .9rem; }}
.dialog-btns {{ display: flex; justify-content: flex-end; gap: 8px; }}
</style>
</head>
<body>
{_topbar_html(mode, device_id, "photos")}

<div class="page">
  <div class="page-header">
    <h1>{page_title}</h1>
    <span class="count-chip">{total_photos} photos · {len(events)} events</span>
    <div class="header-actions">
      <button class="btn btn-dl" id="dlBtn" disabled onclick="downloadSelected()">
        <span class="material-icons" style="font-size:18px">download</span>Download
      </button>
      <button class="btn btn-del" onclick="confirmDeleteAll()">
        <span class="material-icons" style="font-size:18px">delete_sweep</span>Delete All
      </button>
    </div>
  </div>
  {gallery_html}
</div>

{_footer_html()}

<!-- Lightbox -->
<div class="lightbox" id="lightbox">
  <div class="lb-bar">
    <button class="lb-close" onclick="lbClose()"><span class="material-icons">close</span></button>
    <span class="lb-counter" id="lbCounter"></span>
    <button class="lb-del" onclick="lbDelete()"><span class="material-icons">delete</span>Delete</button>
  </div>
  <button class="lb-nav" id="lbPrev" onclick="lbNav(-1)"><span class="material-icons">chevron_left</span></button>
  <img id="lbImg" src="" alt="">
  <button class="lb-nav" id="lbNext" onclick="lbNav(1)"><span class="material-icons">chevron_right</span></button>
</div>

<!-- Multi-select bar -->
<div class="sel-bar" id="selBar">
  <span class="sel-count" id="selCount">0 selected</span>
  <button class="btn btn-ghost" onclick="cancelSelect()">Cancel</button>
  <button class="btn btn-del" onclick="deleteSelected()"><span class="material-icons" style="font-size:18px">delete</span>Delete</button>
  <button class="btn btn-dl" onclick="downloadSelected()"><span class="material-icons" style="font-size:18px">download</span>Download</button>
</div>

<!-- Delete All confirm -->
<div class="overlay" id="delAllOverlay">
  <div class="dialog">
    <h3>Delete All Photos?</h3>
    <p id="delAllMsg"></p>
    <div class="dialog-btns">
      <button class="btn btn-ghost" onclick="closeDelAll()">Cancel</button>
      <button class="btn btn-del" onclick="doDeleteAll()">Delete All</button>
    </div>
  </div>
</div>

<script>
const DEVICE = "{device_id}";
const allThumbs = Array.from(document.querySelectorAll('.thumb-wrap'));
let lbIndex = 0;
let lbFiles = allThumbs.map(t => t.dataset.fname);
let selectMode = false;
let selected = new Set();
let longPressTimer = null;

function lbOpen(fname) {{
  lbIndex = lbFiles.indexOf(fname);
  if (lbIndex < 0) lbIndex = 0;
  lbShow();
  document.getElementById('lightbox').classList.add('open');
}}
function lbShow() {{
  document.getElementById('lbImg').src = `/image/${{DEVICE}}/${{lbFiles[lbIndex]}}`;
  document.getElementById('lbCounter').textContent = `${{lbIndex+1}} of ${{lbFiles.length}}`;
}}
function lbNav(dir) {{ lbIndex = (lbIndex + dir + lbFiles.length) % lbFiles.length; lbShow(); }}
function lbClose() {{ document.getElementById('lightbox').classList.remove('open'); }}
async function lbDelete() {{
  const fname = lbFiles[lbIndex];
  await fetch(`/image/${{DEVICE}}/${{fname}}`, {{method:'DELETE'}});
  lbFiles.splice(lbIndex, 1);
  const el = allThumbs.find(t => t.dataset.fname === fname);
  if (el) el.remove();
  if (!lbFiles.length) {{ lbClose(); return; }}
  lbIndex = Math.min(lbIndex, lbFiles.length-1);
  lbShow();
}}
document.addEventListener('keydown', e => {{
  if (!document.getElementById('lightbox').classList.contains('open')) return;
  if (e.key === 'ArrowLeft') lbNav(-1);
  if (e.key === 'ArrowRight') lbNav(1);
  if (e.key === 'Escape') lbClose();
  if (e.key === 'Delete') lbDelete();
}});

function thumbTap(el, e) {{
  e.preventDefault();
  if (selectMode) toggleSelect(el);
  else lbOpen(el.dataset.fname);
}}
function thumbLong(el, e) {{
  e.preventDefault();
  if (!selectMode) {{ selectMode = true; document.getElementById('selBar').classList.add('open'); }}
  toggleSelect(el);
}}
function toggleSelect(el) {{
  const f = el.dataset.fname;
  if (selected.has(f)) {{ selected.delete(f); el.classList.remove('selected'); }}
  else {{ selected.add(f); el.classList.add('selected'); }}
  updateSelBar();
}}
function updateSelBar() {{
  document.getElementById('selCount').textContent = `${{selected.size}} selected`;
  document.getElementById('dlBtn').disabled = selected.size === 0;
}}
function cancelSelect() {{
  selectMode = false; selected.clear();
  document.querySelectorAll('.thumb-wrap.selected').forEach(e => e.classList.remove('selected'));
  document.getElementById('selBar').classList.remove('open');
  updateSelBar();
}}

allThumbs.forEach(el => {{
  el.addEventListener('touchstart', () => {{ longPressTimer = setTimeout(() => thumbLong(el, {{preventDefault:()=>{{}}}}), 500); }});
  el.addEventListener('touchend',  () => clearTimeout(longPressTimer));
  el.addEventListener('touchmove', () => clearTimeout(longPressTimer));
}});

async function downloadSelected() {{
  const fnames = Array.from(selected);
  if (!fnames.length) return;
  const r = await fetch(`/api/download/${{DEVICE}}`, {{method:'POST', headers:{{'Content-Type':'application/json'}}, body:JSON.stringify({{filenames:fnames}})}});
  const blob = await r.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a'); a.href = url; a.download = 'export.zip'; a.click();
  URL.revokeObjectURL(url);
}}

// ── Thumb-row overflow fade ──────────────────────────────────────────────
function checkOverflows() {{
  document.querySelectorAll('.thumb-row-wrap').forEach(wrap => {{
    const row = wrap.querySelector('.thumb-row');
    if (!row) return;
    const overflows = row.scrollHeight > row.clientHeight + 4;
    wrap.classList.toggle('overflows', overflows);
    // Remove fade when scrolled near bottom
    row.addEventListener('scroll', () => {{
      const atBottom = row.scrollHeight - row.scrollTop - row.clientHeight < 20;
      wrap.classList.toggle('overflows', overflows && !atBottom);
    }}, {{ passive: true }});
  }});
}}
// Run after images may have loaded
window.addEventListener('load', checkOverflows);
setTimeout(checkOverflows, 500);

function confirmDeleteAll() {{
  const count = allThumbs.length;
  document.getElementById('delAllMsg').textContent = `Delete ALL ${{count}} photo${{count===1?'':'s'}}? This cannot be undone.`;
  document.getElementById('delAllOverlay').classList.add('open');
}}
function closeDelAll() {{ document.getElementById('delAllOverlay').classList.remove('open'); }}
async function doDeleteAll() {{
  await fetch(`/api/images/${{DEVICE}}`, {{method:'DELETE'}});
  closeDelAll();
  document.querySelectorAll('.thumb-wrap').forEach(e=>e.remove());
  document.querySelectorAll('.event-group').forEach(e=>e.remove());
  document.querySelector('.page-header .count-chip').textContent = '0 photos · 0 events';
  document.querySelector('.page').insertAdjacentHTML('beforeend',
    `<div class="empty-state"><span class="material-icons">photo_library</span><p>No photos yet</p></div>`);
}}

{_TOGGLE_MODE_JS}
</script>
</body>
</html>"""
    return HTMLResponse(html)
