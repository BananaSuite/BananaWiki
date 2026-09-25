#!/bin/bash
#
# BananaWiki Hosting Platform: Development Server
# ==================================================
# Quick-start script for local development and testing of the hosting portal.
#
# Usage:
#   cd hosting && ./dev.sh              # Start on http://localhost:5099
#   cd hosting && ./dev.sh --port 8099  # Custom port
#   cd hosting && ./dev.sh --host 0.0.0.0  # Bind to all interfaces
#
# Or from the repository root:
#   ./hosting/dev.sh
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

# Default values
PORT="${HOSTING_PORT:-5099}"
HOST="${HOSTING_HOST:-127.0.0.1}"

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
        -h|--help)
            echo "Usage: $0 [OPTIONS]"
            echo ""
            echo "Options:"
            echo "  --port PORT    Port to run on (default: 5099)"
            echo "  --host HOST    Host to bind to (default: 127.0.0.1)"
            echo "  -h, --help     Show this help message"
            echo ""
            echo "Environment variables:"
            echo "  HOSTING_PORT            Portal port (default: 5099)"
            echo "  HOSTING_HOST            Portal bind host (default: 127.0.0.1)"
            echo "  HOSTING_MODE            'port' or 'subdomain' (auto-detected)"
            echo "  HOSTING_PUBLIC_HOST     Public IP/hostname for port-mode URLs"
            echo "  BASE_DOMAIN             Domain for subdomain mode"
            exit 0
            ;;
        *)
            echo -e "${RED}Error: Unknown option: $1${NC}"
            exit 1
            ;;
    esac
done

echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${BLUE}🍌 BananaWiki Hosting Platform: Dev Server${NC}"
echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""

# Check if Python 3 is available
if ! command -v python3 &> /dev/null; then
    echo -e "${RED}Error: Python 3 is not installed${NC}"
    echo "Please install Python 3.10 or newer"
    exit 1
fi

# Check Python version
PYTHON_VERSION=$(python3 -c 'import sys; print(".".join(map(str, sys.version_info[:2])))')
PYTHON_MAJOR=$(echo "$PYTHON_VERSION" | cut -d. -f1)
PYTHON_MINOR=$(echo "$PYTHON_VERSION" | cut -d. -f2)

if [ "$PYTHON_MAJOR" -lt 3 ] || ([ "$PYTHON_MAJOR" -eq 3 ] && [ "$PYTHON_MINOR" -lt 10 ]); then
    echo -e "${RED}Error: Python 3.10+ is required (found: $PYTHON_VERSION)${NC}"
    exit 1
fi

echo -e "${GREEN}✓${NC} Python $PYTHON_VERSION detected"

# Check if venv exists, create if not
if [ ! -d "$REPO_ROOT/venv" ]; then
    echo -e "${YELLOW}⚠${NC}  Virtual environment not found"
    echo -e "   Creating virtual environment..."
    python3 -m venv "$REPO_ROOT/venv"
    echo -e "${GREEN}✓${NC} Virtual environment created"
fi

# Activate venv
echo -e "   Activating virtual environment..."
source "$REPO_ROOT/venv/bin/activate"

# Check if requirements are installed
if ! python -c "import flask" 2>/dev/null; then
    echo -e "${YELLOW}⚠${NC}  Dependencies not installed"
    echo -e "   Installing dependencies from requirements.txt..."
    pip install -q --upgrade pip
    pip install -q -r "$REPO_ROOT/requirements.txt"
    echo -e "${GREEN}✓${NC} Dependencies installed"
else
    echo -e "${GREEN}✓${NC} Dependencies already installed"
fi

# Export environment
export HOSTING_PORT="$PORT"
export HOSTING_HOST="$HOST"

echo ""
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${GREEN}Starting hosting portal dev server...${NC}"
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""
echo -e "  Portal URL:  ${BLUE}http://${HOST}:${PORT}${NC}"
echo -e "  Mode:        ${YELLOW}Development (single-threaded)${NC}"
echo ""
echo -e "${YELLOW}Note: This is NOT suitable for production use!${NC}"
echo -e "      For production, use: ${BLUE}sudo ./banana install --mode hosting${NC}"
echo ""
echo -e "Press ${RED}Ctrl+C${NC} to stop"
echo ""

# Run the hosting portal dev server
cd "$REPO_ROOT"
HOSTING_DEBUG=1 python3 -m hosting
