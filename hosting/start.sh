#!/bin/bash
#
# BananaWiki Hosting Platform: Production Start Script
# ======================================================
# Start the hosting portal with Gunicorn for production use.
#
# Usage:
#   cd hosting && ./start.sh                    # Start with gunicorn.conf.py settings
#   cd hosting && ./start.sh --port 8099        # Override port
#   cd hosting && ./start.sh --workers 8        # Override worker count
#
# Or from the repository root:
#   ./hosting/start.sh
#

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Resolve the repository root (parent of hosting/)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ "$(basename "$SCRIPT_DIR")" = "hosting" ]; then
    REPO_ROOT="$(dirname "$SCRIPT_DIR")"
else
    REPO_ROOT="$SCRIPT_DIR"
fi

# Default values (can be overridden by command line)
PORT=""
HOST=""
WORKERS=""
BIND=""

# Parse command line arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --port)
            PORT="$2"
            shift 2
            ;;
        --host)
            HOST="$2"
            shift 2
            ;;
        --workers)
            WORKERS="$2"
            shift 2
            ;;
        --bind)
            BIND="$2"
            shift 2
            ;;
        -h|--help)
            echo "Usage: $0 [OPTIONS]"
            echo ""
            echo "Options:"
            echo "  --port PORT       Override port (default: 5099)"
            echo "  --host HOST       Override host (default: 127.0.0.1)"
            echo "  --bind ADDR       Bind to address (e.g., 0.0.0.0:5099)"
            echo "  --workers N       Override worker count"
            echo "  -h, --help        Show this help message"
            echo ""
            echo "Environment variables:"
            echo "  HOSTING_PORT            Portal port (default: 5099)"
            echo "  HOSTING_HOST            Portal bind host (default: 127.0.0.1)"
            echo "  HOSTING_MODE            'port' or 'subdomain' (auto-detected)"
            echo "  HOSTING_PUBLIC_HOST     Public IP/hostname for port-mode URLs"
            echo "  BASE_DOMAIN             Domain for subdomain mode"
            echo "  INSTANCE_PORT_START     First port for instances (default: 6001)"
            echo "  INSTANCE_PORT_END       Last port for instances (default: 7000)"
            echo ""
            echo "Examples:"
            echo "  $0                        # Use gunicorn.conf.py defaults"
            echo "  $0 --port 8099            # Start on port 8099"
            echo "  $0 --bind 0.0.0.0:5099    # Bind to all interfaces"
            exit 0
            ;;
        *)
            echo -e "${RED}Error: Unknown option: $1${NC}"
            exit 1
            ;;
    esac
done

echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${BLUE}🍌 BananaWiki Hosting Platform: Production${NC}"
echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""

# Check if venv exists
if [ ! -d "$REPO_ROOT/venv" ]; then
    echo -e "${RED}Error: Virtual environment not found${NC}"
    echo "Please run './hosting/dev.sh' first to set up the environment, or use './banana install --mode hosting' for a managed server."
    exit 1
fi

# Activate venv
source "$REPO_ROOT/venv/bin/activate"

# Check if gunicorn is installed
if ! command -v gunicorn &> /dev/null; then
    echo -e "${RED}Error: Gunicorn is not installed${NC}"
    echo "Installing gunicorn..."
    pip install gunicorn
fi

echo -e "${GREEN}✓${NC} Starting Gunicorn production server..."
echo ""

# Build gunicorn command as an array for safe argument handling
cd "$REPO_ROOT"
GUNICORN_ARGS=(gunicorn hosting.wsgi:app -c hosting/gunicorn.conf.py)

# Apply overrides via environment variables so gunicorn.conf.py picks them up
if [ -n "$HOST" ]; then
    export HOSTING_HOST="$HOST"
fi
if [ -n "$PORT" ]; then
    export HOSTING_PORT="$PORT"
fi

# Add command-line overrides
if [ -n "$BIND" ]; then
    GUNICORN_ARGS+=(--bind "$BIND")
    echo -e "  Bind address: ${BLUE}$BIND${NC}"
elif [ -n "$HOST" ] || [ -n "$PORT" ]; then
    _HOST="${HOST:-${HOSTING_HOST:-127.0.0.1}}"
    _PORT="${PORT:-${HOSTING_PORT:-5099}}"
    GUNICORN_ARGS+=(--bind "$_HOST:$_PORT")
    echo -e "  Bind address: ${BLUE}$_HOST:$_PORT${NC}"
else
    _HOST="${HOSTING_HOST:-127.0.0.1}"
    _PORT="${HOSTING_PORT:-5099}"
    echo -e "  Bind address: ${BLUE}$_HOST:$_PORT${NC} (from config)"
fi

if [ -n "$WORKERS" ]; then
    GUNICORN_ARGS+=(--workers "$WORKERS")
    echo -e "  Workers:      ${BLUE}$WORKERS${NC}"
fi

echo ""
echo -e "${YELLOW}Press Ctrl+C to stop${NC}"
echo ""

# Run gunicorn
exec "${GUNICORN_ARGS[@]}"
