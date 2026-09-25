#!/bin/bash
#
# BananaWiki GPU TTS Server: Installation Script
# =================================================
# Sets up the Piper GPU-accelerated TTS server on a Linux machine with
# an NVIDIA GPU.  Designed for Ubuntu/Debian but should work on most
# systemd-based distros.
#
# Usage:
#   sudo ./install.sh                     # Interactive install
#   sudo ./install.sh --non-interactive   # Use defaults
#   sudo ./install.sh --uninstall         # Remove the service
#
# What it does:
#   1. Checks for NVIDIA GPU + CUDA toolkit
#   2. Creates a dedicated system user (banana-tts)
#   3. Sets up a Python venv with onnxruntime-gpu + piper-tts
#   4. Downloads default voice models (en, it)
#   5. Generates an auth token (stored in a root-only environment file)
#   6. Installs a systemd service listening on a private address
#

set -euo pipefail

# ── Colors ───────────────────────────────────────────────────────────────────

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
BOLD='\033[1m'
DIM='\033[2m'
NC='\033[0m'

# ── Defaults ─────────────────────────────────────────────────────────────────

APP_NAME="banana-tts-gpu"
APP_DIR="/opt/banana-tts-gpu"
SERVICE_USER="banana-tts"
SERVICE_GROUP="banana-tts"
PORT=8787
BIND_HOST=""
AUTH_TOKEN=""
VOICE_DIR=""
USE_CUDA=true
NON_INTERACTIVE=false
UNINSTALL=false

# ── Helpers ──────────────────────────────────────────────────────────────────

info()    { echo -e "  ${BLUE}ℹ${NC}  $*"; }
ok()      { echo -e "  ${GREEN}✓${NC}  $*"; }
warn()    { echo -e "  ${YELLOW}⚠${NC}  $*"; }
fail()    { echo -e "  ${RED}✗${NC}  $*"; }
step()    { echo -e "\n${BOLD}${BLUE}[$1]${NC} ${BOLD}$2${NC}"; }
blank()   { echo ""; }

ask() {
    local prompt="$1" default="${2:-}"
    local display_default=""
    [ -n "$default" ] && display_default=" ${DIM}[${default}]${NC}"
    local v
    read -rp "$(echo -e "  ${CYAN}?${NC}  ${prompt}${display_default}: ")" v
    echo "${v:-$default}"
}

ask_yn() {
    local prompt="$1" default="${2:-Y}"
    local v
    read -rp "$(echo -e "  ${CYAN}?${NC}  ${prompt} ${DIM}[${default}]${NC}: ")" v
    v="${v:-$default}"
    [[ "${v^^}" == "Y" ]] && return 0 || return 1
}

die() {
    echo ""
    fail "$*"
    echo -e "  ${YELLOW}Aborted. Review the error above.${NC}"
    echo ""
    exit 1
}

require_root() {
    [ "$EUID" -eq 0 ] || die "This script must be run as root: sudo $0"
}

# ── Parse CLI ────────────────────────────────────────────────────────────────

while [[ $# -gt 0 ]]; do
    case $1 in
        --non-interactive)   NON_INTERACTIVE=true; shift ;;
        --uninstall)         UNINSTALL=true; shift ;;
        --port)              PORT="$2"; shift 2 ;;
        --host)              BIND_HOST="$2"; shift 2 ;;
        --auth-token)        AUTH_TOKEN="$2"; shift 2 ;;
        --voice-dir)         VOICE_DIR="$2"; shift 2 ;;
        --app-dir)           APP_DIR="$2"; shift 2 ;;
        --no-cuda)           USE_CUDA=false; shift ;;
        -h|--help)
            cat <<HELP

${BOLD}BananaWiki GPU TTS Server: Installer${NC}

${BOLD}Usage:${NC}
  sudo ./install.sh [OPTIONS]

${BOLD}Options:${NC}
  --non-interactive    Use defaults, skip prompts
  --uninstall          Remove the service and clean up
  --port PORT          Server port (default: 8787)
  --host ADDR          Address to listen on (default: this machine's
                       Tailscale IPv4 address if Tailscale is up,
                       otherwise 127.0.0.1)
  --auth-token TOKEN   Pre-shared auth token (auto-generated if omitted)
  --voice-dir DIR      Directory for Piper voice models
  --app-dir DIR        Install directory (default: /opt/banana-tts-gpu)
  --no-cuda            Install without CUDA (CPU-only fallback)
  -h, --help           Show this help

HELP
            exit 0
            ;;
        *) die "Unknown option: $1" ;;
    esac
done

require_root

# ── Uninstall mode ───────────────────────────────────────────────────────────

if [ "$UNINSTALL" = true ]; then
    echo -e "\n  ${BOLD}Uninstalling ${APP_NAME}...${NC}\n"

    systemctl stop "${APP_NAME}.service" 2>/dev/null || true
    systemctl disable "${APP_NAME}.service" 2>/dev/null || true
    rm -f "/etc/systemd/system/${APP_NAME}.service"
    rm -f "/etc/${APP_NAME}.env"
    systemctl daemon-reload

    if [ -d "$APP_DIR" ]; then
        rm -rf "$APP_DIR"
        ok "Removed $APP_DIR"
    fi

    if id "$SERVICE_USER" &>/dev/null; then
        userdel "$SERVICE_USER" 2>/dev/null || true
        ok "Removed user $SERVICE_USER"
    fi

    ok "Uninstall complete"
    exit 0
fi

# ── Banner ───────────────────────────────────────────────────────────────────

echo ""
echo -e "${BLUE}${BOLD}  ╔══════════════════════════════════════════════════════════════╗${NC}"
echo -e "${BLUE}${BOLD}  ║     🍌  BananaWiki GPU TTS Server: Installer               ║${NC}"
echo -e "${BLUE}${BOLD}  ╚══════════════════════════════════════════════════════════════╝${NC}"
echo ""

# ═════════════════════════════════════════════════════════════════════════════
#  STEP 1: Check prerequisites
# ═════════════════════════════════════════════════════════════════════════════

step "1/6" "Checking prerequisites"

# Python 3.10+
if ! command -v python3 &>/dev/null; then
    die "python3 not found. Install Python 3.10+ first."
fi
PY_VERSION=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
PY_MAJOR=$(echo "$PY_VERSION" | cut -d. -f1)
PY_MINOR=$(echo "$PY_VERSION" | cut -d. -f2)
if [ "$PY_MAJOR" -lt 3 ] || ([ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 10 ]); then
    die "Python 3.10+ required, found $PY_VERSION"
fi
ok "Python $PY_VERSION"

# ffmpeg (needed for MP3 output)
if command -v ffmpeg &>/dev/null; then
    ok "ffmpeg found: $(ffmpeg -version 2>/dev/null | head -1)"
else
    warn "ffmpeg not found: MP3 output will not work"
    info "Install with: apt install ffmpeg"
fi

# NVIDIA GPU + CUDA (optional but recommended)
GPU_FOUND=false
if command -v nvidia-smi &>/dev/null; then
    GPU_INFO=$(nvidia-smi --query-gpu=name,memory.total --format=csv,noheader 2>/dev/null || echo "unknown")
    ok "NVIDIA GPU detected: $GPU_INFO"
    GPU_FOUND=true

    # Check CUDA toolkit
    if command -v nvcc &>/dev/null; then
        CUDA_VERSION=$(nvcc --version | grep "release" | sed 's/.*release \([0-9.]*\).*/\1/')
        ok "CUDA toolkit: $CUDA_VERSION"
    else
        warn "nvcc not found: CUDA toolkit may not be installed"
        info "Install with: apt install nvidia-cuda-toolkit"
    fi
else
    if [ "$USE_CUDA" = true ]; then
        warn "No NVIDIA GPU detected: will install CPU-only fallback"
        USE_CUDA=false
    fi
fi

# ═════════════════════════════════════════════════════════════════════════════
#  STEP 2: Interactive config (if needed)
# ═════════════════════════════════════════════════════════════════════════════

step "2/6" "Configuration"

# Listen on the private address the wikis use, not on every interface. The
# Tailscale address is the usual choice; loopback suits a GPU on the same host.
DEFAULT_HOST="127.0.0.1"
if command -v tailscale &>/dev/null; then
    TS_IP=$(tailscale ip -4 2>/dev/null | head -1 || true)
    [ -n "$TS_IP" ] && DEFAULT_HOST="$TS_IP"
fi

if [ "$NON_INTERACTIVE" = false ]; then
    PORT=$(ask "Server port" "$PORT")
    BIND_HOST=$(ask "Listen address (the private address BananaWiki connects to)" "${BIND_HOST:-$DEFAULT_HOST}")
    if [ -z "$AUTH_TOKEN" ]; then
        ask_yn "Generate a random auth token?" "Y" && \
            AUTH_TOKEN=$(python3 -c "import secrets; print(secrets.token_hex(32))")
    fi
    if [ -z "$VOICE_DIR" ]; then
        VOICE_DIR=$(ask "Voice models directory" "${APP_DIR}/voices")
    fi
fi

# Generate token if still empty
if [ -z "$AUTH_TOKEN" ]; then
    AUTH_TOKEN=$(python3 -c "import secrets; print(secrets.token_hex(32))")
fi
VOICE_DIR="${VOICE_DIR:-${APP_DIR}/voices}"
BIND_HOST="${BIND_HOST:-$DEFAULT_HOST}"
# uvicorn wants a bare IPv6 address, without the brackets a URL uses.
BIND_HOST="${BIND_HOST#[}"
BIND_HOST="${BIND_HOST%]}"

# The token ends up in a systemd environment file, which strips quotes and
# treats backslashes and spaces specially, and in an HTTP header on the wiki
# side. Keep it to characters that survive both unchanged.
case "$AUTH_TOKEN" in
    *[!A-Za-z0-9._~+/=-]*)
        die "The auth token may only contain letters, digits and . _ ~ + / = -"
        ;;
esac

case "$BIND_HOST" in
    0.0.0.0|::)
        warn "Listening on every interface exposes the TTS server on all networks this machine is on"
        info "Prefer the Tailscale address, or keep a firewall in front of port $PORT"
        ;;
esac

echo ""
info "Listen:    $BIND_HOST"
info "Port:      $PORT"
info "Voice dir: $VOICE_DIR"
info "CUDA:      $USE_CUDA"
info "Auth token: ${AUTH_TOKEN:0:8}...${AUTH_TOKEN: -4}"
blank

# ═════════════════════════════════════════════════════════════════════════════
#  STEP 3: Create system user + directories
# ═════════════════════════════════════════════════════════════════════════════

step "3/6" "Creating system user and directories"

if ! id "$SERVICE_USER" &>/dev/null; then
    useradd --system --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"
    ok "Created user: $SERVICE_USER"
else
    ok "User $SERVICE_USER already exists"
fi

mkdir -p "$APP_DIR"
mkdir -p "$VOICE_DIR"

# Copy source files to install dir
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cp "$SCRIPT_DIR/server.py" "$APP_DIR/"
cp "$SCRIPT_DIR/config.py" "$APP_DIR/"
cp "$SCRIPT_DIR/requirements.txt" "$APP_DIR/"

chown -R "${SERVICE_USER}:${SERVICE_GROUP}" "$APP_DIR"
ok "Installed to $APP_DIR"

# ═════════════════════════════════════════════════════════════════════════════
#  STEP 4: Python venv + dependencies
# ═════════════════════════════════════════════════════════════════════════════

step "4/6" "Setting up Python environment"

VENV_DIR="${APP_DIR}/venv"
if [ ! -d "$VENV_DIR" ]; then
    sudo -u "$SERVICE_USER" python3 -m venv "$VENV_DIR"
    ok "Created virtual environment"
else
    ok "Virtual environment already exists"
fi

# Install dependencies
info "Installing Python packages (this may take a few minutes)..."
sudo -u "$SERVICE_USER" "$VENV_DIR/bin/pip" install --quiet --upgrade pip

if [ "$USE_CUDA" = true ]; then
    sudo -u "$SERVICE_USER" "$VENV_DIR/bin/pip" install --quiet \
        -r "$APP_DIR/requirements.txt"
else
    # CPU-only: install onnxruntime instead of onnxruntime-gpu
    sudo -u "$SERVICE_USER" "$VENV_DIR/bin/pip" install --quiet \
        piper-tts fastapi "uvicorn[standard]" pydantic
    sudo -u "$SERVICE_USER" "$VENV_DIR/bin/pip" install --quiet \
        "onnxruntime>=1.17.0,<2.0"
fi

ok "Dependencies installed"

# ═════════════════════════════════════════════════════════════════════════════
#  STEP 5: Download default voice models
# ═════════════════════════════════════════════════════════════════════════════

step "5/6" "Downloading default voice models"

DEFAULT_VOICES="en_US-lessac-medium it_IT-paola-medium"
for voice in $DEFAULT_VOICES; do
    model_file="${VOICE_DIR}/${voice}.onnx"
    if [ -f "$model_file" ]; then
        ok "Voice $voice already downloaded"
    else
        info "Downloading $voice..."
        sudo -u "$SERVICE_USER" "$VENV_DIR/bin/python" -c "
from piper.download_voices import download_voice
from pathlib import Path
download_voice('${voice}', Path('${VOICE_DIR}'))
print('  Downloaded: ${voice}')
" || warn "Failed to download $voice. You can download it manually later"
    fi
done

chown -R "${SERVICE_USER}:${SERVICE_GROUP}" "$VOICE_DIR"
ok "Voice models ready"

# ═════════════════════════════════════════════════════════════════════════════
#  STEP 6: Systemd service
# ═════════════════════════════════════════════════════════════════════════════

step "6/6" "Installing systemd service"

# The token goes in a root-only file: unit files under /etc/systemd/system
# are world-readable, so an Environment= line would show it to every local
# user. systemd reads this file as root before starting the service.
ENV_FILE="/etc/${APP_NAME}.env"
(
    umask 077
    printf 'TTS_AUTH_TOKEN=%s\n' "$AUTH_TOKEN" > "$ENV_FILE"
)
chown root:root "$ENV_FILE"
chmod 600 "$ENV_FILE"
ok "Auth token stored in $ENV_FILE"

cat > "/etc/systemd/system/${APP_NAME}.service" <<EOF
[Unit]
Description=BananaWiki GPU TTS Server
# A Tailscale listen address only exists once tailscaled is up.
Wants=network-online.target
After=network-online.target tailscaled.service

[Service]
Type=simple
User=${SERVICE_USER}
Group=${SERVICE_GROUP}
WorkingDirectory=${APP_DIR}
Environment="PATH=${VENV_DIR}/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
Environment="PYTHONPATH=${APP_DIR}"
Environment="TTS_PORT=${PORT}"
Environment="TTS_HOST=${BIND_HOST}"
EnvironmentFile=${ENV_FILE}
Environment="TTS_USE_CUDA=$( [ "$USE_CUDA" = true ] && echo 1 || echo 0 )"
Environment="PIPER_VOICE_DIR=${VOICE_DIR}"
Environment="TTS_LOG_LEVEL=INFO"
ExecStart=${VENV_DIR}/bin/python server.py
Restart=always
RestartSec=5
StartLimitIntervalSec=60
StartLimitBurst=5
StandardOutput=journal
StandardError=journal
SyslogIdentifier=${APP_NAME}

# Security hardening
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=false
ReadWritePaths=${VOICE_DIR} /tmp

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable "${APP_NAME}.service" 2>/dev/null

ok "Systemd service installed"

# ═════════════════════════════════════════════════════════════════════════════
#  Done
# ═════════════════════════════════════════════════════════════════════════════

case "$BIND_HOST" in
    0.0.0.0) URL_HOST="127.0.0.1" ;;
    ::) URL_HOST="[::1]" ;;
    *:*) URL_HOST="[${BIND_HOST}]" ;;
    *) URL_HOST="$BIND_HOST" ;;
esac

echo ""
echo -e "${GREEN}${BOLD}  ╔══════════════════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}${BOLD}  ║  ✓  BananaWiki GPU TTS Server installed!                      ║${NC}"
echo -e "${GREEN}${BOLD}  ╚══════════════════════════════════════════════════════════════╝${NC}"
blank
echo -e "  ${BOLD}Quick start:${NC}"
echo -e "    sudo systemctl start ${APP_NAME}"
echo -e "    sudo journalctl -u ${APP_NAME} -f"
blank
echo -e "  ${BOLD}Test it:${NC}"
echo -e "    curl -H 'Authorization: Bearer ${AUTH_TOKEN:0:8}...${AUTH_TOKEN: -4}' \\"
echo -e "         -H 'Content-Type: application/json' \\"
echo -e "         -d '{\"text\": \"Hello world\", \"language\": \"en\"}' \\"
echo -e "         http://${URL_HOST}:${PORT}/synthesize -o test.mp3"
blank
echo -e "  ${BOLD}Health check:${NC}"
echo -e "    curl http://${URL_HOST}:${PORT}/health"
blank
echo -e "  ${BOLD}Auth token:${NC}  ${AUTH_TOKEN}"
blank
echo -e "  ${YELLOW}Save this token: you'll need it for BananaWiki configuration.${NC}"
blank
