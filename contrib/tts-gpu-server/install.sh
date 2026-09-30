#!/usr/bin/env bash
# Install the BananaWiki GPU speech server as a systemd service.
#
#   sudo ./install.sh [--host ADDRESS] [--port 8787] [--cpu] [--app-dir /opt/bananawiki-tts-gpu]
#   sudo ./install.sh --uninstall
#
# Creates a system user, a virtualenv with the dependencies, a root-only
# environment file holding a fresh TTS_AUTH_TOKEN, and a hardened unit.
set -euo pipefail

NAME="bananawiki-tts-gpu"
APP_DIR="/opt/${NAME}"
SERVICE_USER="bananawiki-tts"
ENV_FILE="/etc/${NAME}.env"
UNIT_FILE="/etc/systemd/system/${NAME}.service"
HOST=""
PORT="8787"
CPU_ONLY=0
UNINSTALL=0

die() { echo "error: $*" >&2; exit 1; }

while [[ $# -gt 0 ]]; do
    case "$1" in
        --host) HOST="${2:?}"; shift 2 ;;
        --port) PORT="${2:?}"; shift 2 ;;
        --app-dir) APP_DIR="${2:?}"; shift 2 ;;
        --cpu) CPU_ONLY=1; shift ;;
        --uninstall) UNINSTALL=1; shift ;;
        -h|--help) sed -n '2,9p' "$0"; exit 0 ;;
        *) die "unknown option $1" ;;
    esac
done

[[ $EUID -eq 0 ]] || die "run as root (sudo $0)"
[[ "$PORT" =~ ^[0-9]{1,5}$ ]] || die "invalid port $PORT"

if [[ $UNINSTALL -eq 1 ]]; then
    systemctl disable --now "${NAME}.service" 2>/dev/null || true
    rm -f "$UNIT_FILE" "$ENV_FILE"
    systemctl daemon-reload
    rm -rf -- "$APP_DIR"
    id "$SERVICE_USER" >/dev/null 2>&1 && userdel "$SERVICE_USER"
    echo "Removed ${NAME}."
    exit 0
fi

command -v python3 >/dev/null || die "python3 is required"
python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' || die "Python 3.10 or newer is required"
command -v ffmpeg >/dev/null || echo "warning: ffmpeg not found; install it or set TTS_OUTPUT_FORMAT=wav" >&2

if [[ -z "$HOST" ]]; then
    # Prefer the Tailscale address; never listen on every interface by default.
    HOST="$(command -v tailscale >/dev/null && tailscale ip -4 2>/dev/null | head -n1 || true)"
    HOST="${HOST:-127.0.0.1}"
fi

id "$SERVICE_USER" >/dev/null 2>&1 || useradd --system --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
install -d -m 0755 "$APP_DIR"
install -d -m 0750 -o "$SERVICE_USER" -g "$SERVICE_USER" "$APP_DIR/voices"
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
install -m 0644 "$SOURCE_DIR/server.py" "$SOURCE_DIR/requirements.txt" "$APP_DIR/"

python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install --quiet --upgrade pip
if [[ $CPU_ONLY -eq 1 ]]; then
    sed 's/^onnxruntime-gpu/onnxruntime/' "$APP_DIR/requirements.txt" > "$APP_DIR/requirements-cpu.txt"
    "$APP_DIR/venv/bin/pip" install --quiet -r "$APP_DIR/requirements-cpu.txt"
    CUDA=0
else
    "$APP_DIR/venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"
    CUDA=1
fi

if [[ ! -f "$ENV_FILE" ]]; then
    TOKEN="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
    umask 077
    cat > "$ENV_FILE" <<ENV
TTS_AUTH_TOKEN=${TOKEN}
TTS_HOST=${HOST}
TTS_PORT=${PORT}
TTS_USE_CUDA=${CUDA}
PIPER_VOICE_DIR=${APP_DIR}/voices
ENV
    chmod 0600 "$ENV_FILE"
fi

cat > "$UNIT_FILE" <<UNIT
[Unit]
Description=BananaWiki GPU speech server
After=network-online.target tailscaled.service
Wants=network-online.target

[Service]
User=${SERVICE_USER}
Group=${SERVICE_USER}
EnvironmentFile=${ENV_FILE}
WorkingDirectory=${APP_DIR}
ExecStart=${APP_DIR}/venv/bin/python ${APP_DIR}/server.py
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ReadWritePaths=${APP_DIR}/voices

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable --now "${NAME}.service"
echo "Installed ${NAME}, listening on ${HOST}:${PORT}."
echo "The access token is TTS_AUTH_TOKEN in ${ENV_FILE} (readable by root only)."
echo "In BananaWiki: Admin > Read aloud > Remote GPU server, address http://${HOST}:${PORT}."
