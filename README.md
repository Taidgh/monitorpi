# MonitorPi 📷

A Raspberry Pi wildlife and lightning camera system with a live web UI.

> **Original concept:** [StormWatch-Pi](https://github.com/sheet315/stormwatch-pi) by [@sheet315](https://github.com/sheet315)
> **Author / maintainer:** [@Taidgh](https://github.com/Taidgh)

---

## Features

| | |
|---|---|
|  **Wildlife mode** | Frame-differencing motion detection, light theme |
|  **Lightning mode** | 99th-percentile brightness spike detection, dark theme |
|  **Live MJPEG stream** | Toggle-able live preview in the browser |
|  **Photo gallery** | Events grouped by time, lightbox, multi-select, download zip |
|  **Sensitivity sliders** | Adjust detection thresholds live from the UI |
|  **Auto-restart** | Client process auto-restarts on crash; server exit stops everything |
|  **mDNS** | Access via `http://<hostname>.local:8000` on your network |

---

## Hardware

- Raspberry Pi 4 Model B (tested on 4 GB, aarch64)
- Logitech C920e USB webcam (or any V4L2/MJPG-capable camera)
- Raspberry Pi OS Bookworm (64-bit)

---

## Quick Install

Clone the repo onto your Pi, then run the installer as root:

```bash
git clone https://github.com/Taidgh/monitorpi.git
cd monitorpi
sudo bash install.sh
```

The installer will:
1. Ask for an app name (default: `MonitorPi`)
2. Detect connected cameras and let you choose one
3. Install all system and Python dependencies
4. Create a Python virtualenv
5. Register and start a `systemd` service
6. Print the URLs when ready

---

## Manual Install

```bash
# 1. Install system deps
sudo apt-get update
sudo apt-get install -y python3 python3-venv v4l-utils libgl1 libglib2.0-0 avahi-daemon

# 2. Create virtualenv and install packages
python3 -m venv .venv
.venv/bin/pip install fastapi "uvicorn[standard]" python-multipart \
    opencv-python-headless requests numpy

# 3. Copy and enable the service
sudo cp monitorpi.service /etc/systemd/system/
# Edit User=, WorkingDirectory=, ExecStart= paths to match your setup
sudo systemctl daemon-reload
sudo systemctl enable monitorpi
sudo systemctl start monitorpi
```

---

## File Overview

| File | Purpose |
|------|---------|
| `server.py` | FastAPI backend — endpoints, MJPEG stream, HTML pages |
| `client.py` | Camera loop — capture, detection, rolling buffer, upload worker |
| `run.py` | Process manager — starts server, waits for readiness, starts client, auto-restarts |
| `install.sh` | Interactive installer for fresh Raspberry Pi OS installs |
| `monitorpi.service` | systemd unit file template |

---

## Configuration

The installer sets these environment variables in the systemd service. You can also edit `/etc/systemd/system/<name>.service` directly:

| Variable | Default | Description |
|----------|---------|-------------|
| `MONITORPI_APP_NAME` | `MonitorPi` | Name shown in the UI |
| `MONITORPI_DEVICE_ID` | `MonitorPi-01` | Device identifier used in URLs and storage paths |
| `MONITORPI_VIDEO_DEV` | `/dev/video0` | V4L2 camera device path |

After editing, reload with:
```bash
sudo systemctl daemon-reload && sudo systemctl restart monitorpi
```

---

## Detection Settings

Adjustable live from the UI sidebar without restarting:

| Setting | Default | Range | Effect |
|---------|---------|-------|--------|
|  Trigger threshold (`peak_delta`) | 30 | 5 – 80 | Lower = triggers on smaller brightness spikes |
|  Pixel diff threshold (`diff_threshold`) | 25 | 10 – 60 | Lower = detects subtler pixel changes |
|  Motion area threshold (`motion_score`) | 500 | 100 – 8000 | Lower = triggers on smaller movement area |

---

## Service Management

```bash
sudo systemctl status  monitorpi
sudo systemctl restart monitorpi
sudo systemctl stop    monitorpi
sudo journalctl -u monitorpi -f
```

---

## License

MIT — do what you like, credit appreciated.

---

*Built on [StormWatch-Pi](https://github.com/sheet315/stormwatch-pi) · [@Taidgh](https://github.com/Taidgh)*
