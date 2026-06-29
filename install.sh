#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
#  MonitorPi Installer
#  Installs MonitorPi on any Raspberry Pi running Raspberry Pi OS (Bookworm)
#
#  Original concept: https://github.com/sheet315/stormwatch-pi
#  Author / maintainer: https://github.com/Taidgh
# ═══════════════════════════════════════════════════════════════════════════
set -euo pipefail

# ── Colours ────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GRN='\033[0;32m'; YLW='\033[1;33m'
CYN='\033[0;36m'; BLD='\033[1m'; RST='\033[0m'

info()    { echo -e "${CYN}[info]${RST}  $*"; }
success() { echo -e "${GRN}[ok]${RST}    $*"; }
warn()    { echo -e "${YLW}[warn]${RST}  $*"; }
error()   { echo -e "${RED}[error]${RST} $*" >&2; }
die()     { error "$*"; exit 1; }
header()  { echo -e "\n${BLD}${CYN}═══ $* ═══${RST}\n"; }

# ── Banner ─────────────────────────────────────────────────────────────────
clear
echo -e "${BLD}${CYN}"
cat << 'BANNER'
  __  __             _ _          ____  _
 |  \/  | ___  _ __ (_) |_ ___  |  _ \(_)
 | |\/| |/ _ \| '_ \| | __/ _ \ | |_) | |
 | |  | | (_) | | | | | || (_) ||  __/| |
 |_|  |_|\___/|_| |_|_|\__\___/ |_|   |_|

 Wildlife & Lightning Camera System
BANNER
echo -e "${RST}"
echo -e " Original concept : ${CYN}https://github.com/sheet315/stormwatch-pi${RST}"
echo -e " Author           : ${CYN}https://github.com/Taidgh${RST}"
echo ""

# ── Root check ─────────────────────────────────────────────────────────────
[[ $EUID -eq 0 ]] || die "Run as root: sudo bash install.sh"

# ── Detect current (non-root) user ─────────────────────────────────────────
if [[ -n "${SUDO_USER:-}" ]]; then
    INSTALL_USER="$SUDO_USER"
else
    # fallback: pick first uid-1000+ user
    INSTALL_USER=$(awk -F: '$3>=1000 && $3<65534 {print $1; exit}' /etc/passwd)
fi
INSTALL_UID=$(id -u "$INSTALL_USER")
INSTALL_HOME=$(eval echo "~$INSTALL_USER")
info "Installing as user: ${BLD}$INSTALL_USER${RST} (uid=$INSTALL_UID, home=$INSTALL_HOME)"

# ══════════════════════════════════════════════════════════════════════════
# Step 1 — App name
# ══════════════════════════════════════════════════════════════════════════
header "App Configuration"
read -rp "$(echo -e "  App name ${CYN}[MonitorPi]${RST}: ")" APP_NAME_INPUT
APP_NAME="${APP_NAME_INPUT:-MonitorPi}"
# Derive a safe slug for directory name, service name, device ID
APP_SLUG=$(echo "$APP_NAME" | tr '[:upper:]' '[:lower:]' | sed 's/[^a-z0-9]/-/g' | sed 's/-\+/-/g' | sed 's/^-\|-$//g')
DEVICE_ID="${APP_NAME}-01"
INSTALL_DIR="$INSTALL_HOME/${APP_SLUG}"
SERVICE_NAME="${APP_SLUG}"
success "App name   : $APP_NAME"
success "Service    : $SERVICE_NAME"
success "Install dir: $INSTALL_DIR"
success "Device ID  : $DEVICE_ID"

# ══════════════════════════════════════════════════════════════════════════
# Step 2 — Camera detection
# ══════════════════════════════════════════════════════════════════════════
header "Camera Detection"
info "Scanning for video devices…"

# Give udev a moment if device was just plugged in
sleep 1

mapfile -t VIDEO_DEVS < <(ls /dev/video* 2>/dev/null | grep -E '/dev/video[0-9]+$' || true)

if [[ ${#VIDEO_DEVS[@]} -eq 0 ]]; then
    warn "No /dev/video* devices found."
    read -rp "  Please plug in your camera and press Enter to rescan…"
    mapfile -t VIDEO_DEVS < <(ls /dev/video* 2>/dev/null | grep -E '/dev/video[0-9]+$' || true)
    [[ ${#VIDEO_DEVS[@]} -eq 0 ]] && die "Still no camera found. Connect a USB webcam and re-run."
fi

# Filter to devices that v4l2 reports as capture devices (not metadata nodes)
CAPTURE_DEVS=()
for dev in "${VIDEO_DEVS[@]}"; do
    if v4l2-ctl --device="$dev" --list-formats 2>/dev/null | grep -q 'MJPG\|YUYV\|H264'; then
        CAPTURE_DEVS+=("$dev")
    fi
done

# Fall back to all video devices if v4l2-ctl not installed yet
if [[ ${#CAPTURE_DEVS[@]} -eq 0 ]]; then
    CAPTURE_DEVS=("${VIDEO_DEVS[@]}")
fi

if [[ ${#CAPTURE_DEVS[@]} -eq 1 ]]; then
    VIDEO_DEVICE="${CAPTURE_DEVS[0]}"
    echo ""
    read -rp "$(echo -e "  Found camera at ${BLD}$VIDEO_DEVICE${RST}. Use this? [Y/n]: ")" yn
    yn="${yn:-Y}"
    if [[ ! "$yn" =~ ^[Yy] ]]; then
        read -rp "  Enter device path manually (e.g. /dev/video2): " VIDEO_DEVICE
    fi
else
    echo ""
    info "Multiple capture devices found:"
    for i in "${!CAPTURE_DEVS[@]}"; do
        # Try to get device name
        dev="${CAPTURE_DEVS[$i]}"
        devname=$(v4l2-ctl --device="$dev" --info 2>/dev/null | grep 'Card type' | sed 's/.*: //' || echo "Unknown")
        echo -e "   ${BLD}$((i+1))${RST}) $dev  — $devname"
    done
    echo ""
    while true; do
        read -rp "  Select camera [1-${#CAPTURE_DEVS[@]}]: " choice
        if [[ "$choice" =~ ^[0-9]+$ ]] && (( choice >= 1 && choice <= ${#CAPTURE_DEVS[@]} )); then
            VIDEO_DEVICE="${CAPTURE_DEVS[$((choice-1))]}"
            break
        fi
        warn "Enter a number between 1 and ${#CAPTURE_DEVS[@]}"
    done
fi

success "Using camera: $VIDEO_DEVICE"

# ══════════════════════════════════════════════════════════════════════════
# Step 3 — System dependencies
# ══════════════════════════════════════════════════════════════════════════
header "System Dependencies"
info "Updating package list…"
apt-get update -qq

info "Installing system packages…"
apt-get install -y -qq \
    python3 \
    python3-venv \
    python3-pip \
    v4l-utils \
    libgl1 \
    libglib2.0-0 \
    avahi-daemon \
    curl \
    2>/dev/null

success "System packages installed"

# Ensure avahi (mDNS / .local) is running
systemctl enable avahi-daemon --quiet 2>/dev/null || true
systemctl start  avahi-daemon          2>/dev/null || true

# ══════════════════════════════════════════════════════════════════════════
# Step 4 — Project files
# ══════════════════════════════════════════════════════════════════════════
header "Project Files"

# Determine where this script lives (so we can copy sibling files)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

info "Creating install directory: $INSTALL_DIR"
mkdir -p "$INSTALL_DIR"

# Copy application files
for f in server.py client.py run.py; do
    if [[ -f "$SCRIPT_DIR/$f" ]]; then
        cp "$SCRIPT_DIR/$f" "$INSTALL_DIR/$f"
        success "Copied $f"
    else
        die "$f not found next to install.sh (expected at $SCRIPT_DIR/$f)"
    fi
done

# Create data directory
mkdir -p "$INSTALL_DIR/data"

# Fix ownership
chown -R "$INSTALL_USER:$INSTALL_USER" "$INSTALL_DIR"
success "Project files ready at $INSTALL_DIR"

# ══════════════════════════════════════════════════════════════════════════
# Step 5 — Python virtual environment & packages
# ══════════════════════════════════════════════════════════════════════════
header "Python Environment"
VENV="$INSTALL_DIR/.venv"

if [[ ! -d "$VENV" ]]; then
    info "Creating virtualenv…"
    sudo -u "$INSTALL_USER" python3 -m venv "$VENV"
fi

info "Installing Python packages (this may take a few minutes on Pi)…"
sudo -u "$INSTALL_USER" "$VENV/bin/pip" install --quiet --upgrade pip
sudo -u "$INSTALL_USER" "$VENV/bin/pip" install --quiet \
    fastapi \
    "uvicorn[standard]" \
    python-multipart \
    opencv-python-headless \
    requests \
    numpy

success "Python packages installed"
"$VENV/bin/python3" -c "import cv2, fastapi, uvicorn, numpy, requests; print('  All imports OK')"

# ══════════════════════════════════════════════════════════════════════════
# Step 6 — systemd service
# ══════════════════════════════════════════════════════════════════════════
header "Systemd Service"

SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

cat > "$SERVICE_FILE" << SVCEOF
[Unit]
Description=${APP_NAME} Camera System
After=network.target

[Service]
User=${INSTALL_UID}
WorkingDirectory=${INSTALL_DIR}
Environment=MONITORPI_APP_NAME=${APP_NAME}
Environment=MONITORPI_DEVICE_ID=${DEVICE_ID}
Environment=MONITORPI_VIDEO_DEV=${VIDEO_DEVICE}
ExecStart=${VENV}/bin/python3 run.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
SVCEOF

success "Service file written: $SERVICE_FILE"

# Stop any old instance of this service if it existed before
systemctl stop "$SERVICE_NAME" 2>/dev/null || true

systemctl daemon-reload
systemctl enable "$SERVICE_NAME" --quiet
systemctl start  "$SERVICE_NAME"

# ══════════════════════════════════════════════════════════════════════════
# Step 7 — Wait for startup and report
# ══════════════════════════════════════════════════════════════════════════
header "Startup Check"
info "Waiting for server to become ready…"

READY=0
for i in $(seq 1 30); do
    if curl -sf http://127.0.0.1:8000/api/mode > /dev/null 2>&1; then
        READY=1
        break
    fi
    sleep 1
    printf "."
done
echo ""

HOSTNAME_VAL=$(hostname)
IP_ADDR=$(hostname -I | awk '{print $1}')

if [[ $READY -eq 1 ]]; then
    success "Server is up!"
    echo ""
    echo -e "${BLD}${GRN}╔══════════════════════════════════════════════════╗${RST}"
    echo -e "${BLD}${GRN}║  ${APP_NAME} is running!${RST}"
    echo -e "${BLD}${GRN}╠══════════════════════════════════════════════════╣${RST}"
    echo -e "${BLD}${GRN}║${RST}  Local  : ${CYN}http://${HOSTNAME_VAL}.local:8000${RST}"
    echo -e "${BLD}${GRN}║${RST}  Network: ${CYN}http://${IP_ADDR}:8000${RST}"
    echo -e "${BLD}${GRN}╠══════════════════════════════════════════════════╣${RST}"
    echo -e "${BLD}${GRN}║${RST}  Camera : ${VIDEO_DEVICE}"
    echo -e "${BLD}${GRN}║${RST}  Service: ${SERVICE_NAME}"
    echo -e "${BLD}${GRN}║${RST}  Dir    : ${INSTALL_DIR}"
    echo -e "${BLD}${GRN}╠══════════════════════════════════════════════════╣${RST}"
    echo -e "${BLD}${GRN}║${RST}  github.com/Taidgh  ·  github.com/sheet315   "
    echo -e "${BLD}${GRN}╚══════════════════════════════════════════════════╝${RST}"
else
    warn "Server did not respond within 30 seconds."
    echo ""
    echo "  Check logs with:"
    echo -e "  ${BLD}sudo journalctl -u ${SERVICE_NAME} -f${RST}"
fi

echo ""
echo -e "  Manage service:"
echo -e "  ${BLD}sudo systemctl status  ${SERVICE_NAME}${RST}"
echo -e "  ${BLD}sudo systemctl restart ${SERVICE_NAME}${RST}"
echo -e "  ${BLD}sudo journalctl -u ${SERVICE_NAME} -f${RST}"
echo ""
