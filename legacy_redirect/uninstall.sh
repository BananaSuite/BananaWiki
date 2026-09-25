#!/usr/bin/env bash
# =============================================================================
#  Uninstall the legacy domain redirect daemon
# =============================================================================
#
#  Stops and removes the systemd service and the nginx server block.
#  The Python code in legacy_redirect/ is left in place (it ships with
#  the BananaWiki repo and is harmless when not running).
#
#  Usage:
#      sudo bash legacy_redirect/uninstall.sh
#
#  Manual removal (same steps this script performs):
#
#      # 1. Stop and disable the service
#      sudo systemctl stop bananawiki-legacy-redirect
#      sudo systemctl disable bananawiki-legacy-redirect
#
#      # 2. Remove the systemd unit file
#      sudo rm /etc/systemd/system/bananawiki-legacy-redirect.service
#      sudo systemctl daemon-reload
#
#      # 3. Remove the nginx server block
#      sudo rm /etc/nginx/sites-enabled/bananawiki-legacy-redirect
#      sudo rm /etc/nginx/sites-available/bananawiki-legacy-redirect
#      sudo nginx -t && sudo systemctl reload nginx
#
# =============================================================================

set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BOLD='\033[1m'; NC='\033[0m'

ok()   { echo -e "  ${GREEN}✓${NC}  $*"; }
info() { echo -e "  →  $*"; }
warn() { echo -e "  ${YELLOW}⚠${NC}  $*"; }
die()  { echo -e "\n  ${RED}✗  ERROR: $*${NC}\n" >&2; exit 1; }

[[ "$(id -u)" -eq 0 ]] || die "This script must be run as root (sudo)."

SERVICE="bananawiki-legacy-redirect"
NGINX_AVAIL="/etc/nginx/sites-available/${SERVICE}"
NGINX_ENABLED="/etc/nginx/sites-enabled/${SERVICE}"

echo ""
echo -e "  ${BOLD}Uninstalling legacy domain redirect daemon${NC}"
echo ""

# ── Systemd ──────────────────────────────────────────────────────────────────
if systemctl list-unit-files "${SERVICE}.service" &>/dev/null 2>&1; then
    info "Stopping ${SERVICE}..."
    systemctl stop "$SERVICE" 2>/dev/null || true
    systemctl disable "$SERVICE" 2>/dev/null || true
    rm -f "/etc/systemd/system/${SERVICE}.service"
    systemctl daemon-reload
    ok "Systemd service removed"
else
    info "Systemd service not found: skipping"
fi

# ── Nginx ────────────────────────────────────────────────────────────────────
if [ -f "$NGINX_AVAIL" ] || [ -L "$NGINX_ENABLED" ]; then
    rm -f "$NGINX_ENABLED" "$NGINX_AVAIL"
    if command -v nginx &>/dev/null; then
        nginx -t &>/dev/null && systemctl reload nginx \
            && ok "Nginx config removed and reloaded" \
            || warn "Nginx config removed but reload failed: check manually"
    else
        ok "Nginx config files removed"
    fi
else
    info "Nginx config not found: skipping"
fi

echo ""
echo -e "  ${GREEN}${BOLD}Legacy redirect daemon uninstalled.${NC}"
echo ""
echo "  The Python code in legacy_redirect/ was left in place."
echo "  For a replacement redirect, configure the new hostname in your reverse proxy."
echo "  and answer 'y' to the domain change + legacy redirect prompts."
echo ""
