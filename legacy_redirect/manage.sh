#!/usr/bin/env bash
# =============================================================================
#  Legacy Redirect Manager
# =============================================================================
#
#  Manage the legacy domain redirect daemon that shows a "We moved" page
#  on wildcard subdomains of old domains.
#
#  Usage:
#      sudo bash legacy_redirect/manage.sh <command> [args]
#
#  Commands:
#      status              Show service status and configured domains
#      list                List all old domains currently configured
#      add <domain>        Add an old domain to redirect
#      remove <domain>     Remove an old domain from the redirect list
#      set-target <domain> Change the new (target) domain
#      install             Install and start the systemd service + nginx
#      uninstall           Stop, disable, and remove everything
#      enable              Start the service (if installed but stopped)
#      disable             Stop the service temporarily (keeps config)
#      restart             Restart the service
#      logs                Show recent service logs
#
# =============================================================================

set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'

ok()   { echo -e "  ${GREEN}✓${NC}  $*"; }
info() { echo -e "  ${CYAN}→${NC}  $*"; }
warn() { echo -e "  ${YELLOW}⚠${NC}  $*"; }
die()  { echo -e "\n  ${RED}✗  ERROR: $*${NC}\n" >&2; exit 1; }

SERVICE="bananawiki-legacy-redirect"
UNIT_FILE="/etc/systemd/system/${SERVICE}.service"
NGINX_AVAIL="/etc/nginx/sites-available/${SERVICE}"
NGINX_ENABLED="/etc/nginx/sites-enabled/${SERVICE}"

# ---------------------------------------------------------------------------
#  Helpers
# ---------------------------------------------------------------------------

_require_root() {
    [[ "$(id -u)" -eq 0 ]] || die "This command must be run as root (sudo)."
}

_get_domains() {
    # Read LEGACY_OLD_DOMAINS from the systemd unit file
    if [[ ! -f "$UNIT_FILE" ]]; then
        echo ""
        return
    fi
    grep -oP 'LEGACY_OLD_DOMAINS=\K.*' "$UNIT_FILE" 2>/dev/null || echo ""
}

_get_target() {
    if [[ ! -f "$UNIT_FILE" ]]; then
        echo ""
        return
    fi
    grep -oP 'LEGACY_NEW_DOMAIN=\K.*' "$UNIT_FILE" 2>/dev/null || echo ""
}

_set_domains() {
    local domains="$1"
    _require_root
    [[ -f "$UNIT_FILE" ]] || die "Service not installed. Run: sudo $0 install"
    sed -i "s|LEGACY_OLD_DOMAINS=.*|LEGACY_OLD_DOMAINS=${domains}|" "$UNIT_FILE"
    systemctl daemon-reload
    _rebuild_nginx "$domains"
}

_rebuild_nginx() {
    local domains="$1"
    if [[ -z "$domains" ]]; then
        rm -f "$NGINX_ENABLED" "$NGINX_AVAIL"
        nginx -t &>/dev/null && systemctl reload nginx 2>/dev/null || true
        return
    fi
    # Build server_name line from comma-separated domains
    local server_names=""
    IFS=',' read -ra parts <<< "$domains"
    for d in "${parts[@]}"; do
        d="$(echo "$d" | xargs)"  # trim whitespace
        [[ -n "$d" ]] && server_names+=" *.${d}"
    done
    cat > "$NGINX_AVAIL" <<EOF
# Legacy domain redirect: managed by legacy_redirect/manage.sh
server {
    listen 80;
    server_name${server_names};
    location / {
        proxy_pass http://127.0.0.1:8090;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
    }
}
EOF
    ln -sf "$NGINX_AVAIL" "$NGINX_ENABLED"
    nginx -t &>/dev/null && systemctl reload nginx \
        && ok "nginx config updated" \
        || warn "nginx config test failed: check $NGINX_AVAIL"
}

# ---------------------------------------------------------------------------
#  Commands
# ---------------------------------------------------------------------------

cmd_status() {
    echo ""
    echo -e "  ${BOLD}Legacy Redirect Status${NC}"
    echo ""
    if [[ ! -f "$UNIT_FILE" ]]; then
        info "Not installed"
        return
    fi
    if systemctl is-active --quiet "$SERVICE" 2>/dev/null; then
        ok "Service: running"
    else
        warn "Service: stopped"
    fi
    local domains=$(_get_domains)
    local target=$(_get_target)
    echo -e "  Old domains: ${BOLD}${domains:-<none>}${NC}"
    echo -e "  Target:      ${BOLD}${target}${NC}"
    echo -e "  Nginx:       $([ -L "$NGINX_ENABLED" ] && echo "enabled" || echo "disabled")"
    echo ""
}

cmd_list() {
    local domains=$(_get_domains)
    if [[ -z "$domains" ]]; then
        echo "No domains configured."
        return
    fi
    IFS=',' read -ra parts <<< "$domains"
    for d in "${parts[@]}"; do
        d="$(echo "$d" | xargs)"
        [[ -n "$d" ]] && echo "  *.${d}"
    done
}

cmd_add() {
    _require_root
    local new_domain="${1:-}"
    [[ -n "$new_domain" ]] || die "Usage: $0 add <domain>"
    new_domain="$(echo "$new_domain" | xargs | tr '[:upper:]' '[:lower:]')"
    local current=$(_get_domains)
    # Check if already present
    if echo ",$current," | grep -q ",${new_domain},"; then
        info "${new_domain} is already in the list"
        return
    fi
    if [[ -n "$current" ]]; then
        current="${current},${new_domain}"
    else
        current="$new_domain"
    fi
    _set_domains "$current"
    systemctl restart "$SERVICE" 2>/dev/null || true
    ok "Added ${new_domain}, now redirecting *.${new_domain}"
}

cmd_remove() {
    _require_root
    local rm_domain="${1:-}"
    [[ -n "$rm_domain" ]] || die "Usage: $0 remove <domain>"
    rm_domain="$(echo "$rm_domain" | xargs | tr '[:upper:]' '[:lower:]')"
    local current=$(_get_domains)
    # Remove the domain from the comma-separated list
    local new_list=""
    IFS=',' read -ra parts <<< "$current"
    for d in "${parts[@]}"; do
        d="$(echo "$d" | xargs)"
        [[ "$d" == "$rm_domain" ]] && continue
        [[ -n "$d" ]] && new_list="${new_list:+${new_list},}${d}"
    done
    _set_domains "$new_list"
    systemctl restart "$SERVICE" 2>/dev/null || true
    ok "Removed ${rm_domain}"
}

cmd_set_target() {
    _require_root
    local new_target="${1:-}"
    [[ -n "$new_target" ]] || die "Usage: $0 set-target <domain>"
    [[ -f "$UNIT_FILE" ]] || die "Service not installed."
    sed -i "s|LEGACY_NEW_DOMAIN=.*|LEGACY_NEW_DOMAIN=${new_target}|" "$UNIT_FILE"
    systemctl daemon-reload
    systemctl restart "$SERVICE" 2>/dev/null || true
    ok "Target domain set to ${new_target}"
}

cmd_install() {
    _require_root
    local app_dir="${1:-/opt/bananawiki}"
    [[ -f "${app_dir}/legacy_redirect/bananawiki-legacy-redirect.service" ]] \
        || die "Service template not found at ${app_dir}/legacy_redirect/"

    cp "${app_dir}/legacy_redirect/bananawiki-legacy-redirect.service" "$UNIT_FILE"
    # Patch paths for this deployment
    local service_user
    service_user=$(stat -c '%U' "${app_dir}/hosting/data" 2>/dev/null || echo "bananawiki")
    sed -i "s|/opt/bananawiki|${app_dir}|g" "$UNIT_FILE"
    sed -i "s|User=bananawiki|User=${service_user}|" "$UNIT_FILE"
    sed -i "s|Group=bananawiki|Group=${service_user}|" "$UNIT_FILE"

    systemctl daemon-reload
    systemctl enable "$SERVICE" 2>/dev/null || true
    systemctl start "$SERVICE" 2>/dev/null || true

    local domains=$(_get_domains)
    [[ -n "$domains" ]] && _rebuild_nginx "$domains"

    ok "Legacy redirect installed and started"
    cmd_status
}

cmd_uninstall() {
    _require_root
    read -rp "  Remove the legacy redirect daemon completely? [y/N] " confirm
    [[ "$confirm" =~ ^[yY] ]] || { echo "  Aborted."; return; }
    systemctl stop "$SERVICE" 2>/dev/null || true
    systemctl disable "$SERVICE" 2>/dev/null || true
    rm -f "$UNIT_FILE"
    systemctl daemon-reload
    rm -f "$NGINX_ENABLED" "$NGINX_AVAIL"
    nginx -t &>/dev/null && systemctl reload nginx 2>/dev/null || true
    ok "Legacy redirect uninstalled"
}

cmd_enable() {
    _require_root
    systemctl start "$SERVICE" && ok "Service started" || warn "Failed to start"
}

cmd_disable() {
    _require_root
    systemctl stop "$SERVICE" && ok "Service stopped" || warn "Failed to stop"
}

cmd_restart() {
    _require_root
    systemctl restart "$SERVICE" && ok "Service restarted" || warn "Failed to restart"
}

cmd_logs() {
    journalctl -u "$SERVICE" -n 30 --no-pager
}

# ---------------------------------------------------------------------------
#  Dispatch
# ---------------------------------------------------------------------------

case "${1:-}" in
    status)     cmd_status ;;
    list)       cmd_list ;;
    add)        cmd_add "${2:-}" ;;
    remove)     cmd_remove "${2:-}" ;;
    set-target) cmd_set_target "${2:-}" ;;
    install)    cmd_install "${2:-}" ;;
    uninstall)  cmd_uninstall ;;
    enable)     cmd_enable ;;
    disable)    cmd_disable ;;
    restart)    cmd_restart ;;
    logs)       cmd_logs ;;
    *)
        echo ""
        echo -e "  ${BOLD}Legacy Redirect Manager${NC}"
        echo ""
        echo "  Usage: sudo $0 <command> [args]"
        echo ""
        echo "  Commands:"
        echo "    status              Show service status and configured domains"
        echo "    list                List all old domains"
        echo "    add <domain>        Add an old domain to redirect"
        echo "    remove <domain>     Remove an old domain"
        echo "    set-target <domain> Change the target (new) domain"
        echo "    install [app-dir]   Install the service (default: /opt/bananawiki)"
        echo "    uninstall           Remove the service completely"
        echo "    enable              Start the service"
        echo "    disable             Stop the service temporarily"
        echo "    restart             Restart the service"
        echo "    logs                Show recent logs"
        echo ""
        ;;
esac
