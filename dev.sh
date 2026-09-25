#!/bin/bash
#
# BananaWiki Development Server
# ==============================
# Quick-start script for local development and testing
#
# Usage:
#   ./dev.sh                    # Start Flask dev server on http://localhost:5001
#   ./dev.sh --port 8080        # Start on custom port
#   ./dev.sh --host 0.0.0.0     # Bind to all interfaces
#

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Default values
PORT="${PORT:-5001}"
HOST="${HOST:-127.0.0.1}"

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
            echo "  --port PORT    Port to run on (default: 5001)"
            echo "  --host HOST    Host to bind to (default: 127.0.0.1)"
            echo "  -h, --help     Show this help message"
            exit 0
            ;;
        *)
            echo -e "${RED}Error: Unknown option: $1${NC}"
            exit 1
            ;;
    esac
done

echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${BLUE}🍌 BananaWiki Development Server${NC}"
echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""

# Check if Python 3 is available
if ! command -v python3 &> /dev/null; then
    echo -e "${RED}Error: Python 3 is not installed${NC}"
    echo "Please install Python 3.12 or newer"
    exit 1
fi

# Check Python version
PYTHON_VERSION=$(python3 -c 'import sys; print(".".join(map(str, sys.version_info[:2])))')
PYTHON_MAJOR=$(echo $PYTHON_VERSION | cut -d. -f1)
PYTHON_MINOR=$(echo $PYTHON_VERSION | cut -d. -f2)

if [ "$PYTHON_MAJOR" -lt 3 ] || ([ "$PYTHON_MAJOR" -eq 3 ] && [ "$PYTHON_MINOR" -lt 12 ]); then
    echo -e "${RED}Error: Python 3.12+ is required (found: $PYTHON_VERSION)${NC}"
    exit 1
fi

echo -e "${GREEN}✓${NC} Python $PYTHON_VERSION detected"

# Check if venv exists, create if not
if [ ! -d "venv" ]; then
    echo -e "${YELLOW}⚠${NC}  Virtual environment not found"
    echo -e "   Creating virtual environment..."
    python3 -m venv venv
    echo -e "${GREEN}✓${NC} Virtual environment created"
fi

# Activate venv
echo -e "   Activating virtual environment..."
source venv/bin/activate

# Check if requirements are installed
if ! python -c "import flask" 2>/dev/null; then
    echo -e "${YELLOW}⚠${NC}  Dependencies not installed"
    echo -e "   Installing dependencies from requirements.txt..."
    pip install -q --upgrade pip
    pip install -q -r requirements.txt
    echo -e "${GREEN}✓${NC} Dependencies installed"
else
    echo -e "${GREEN}✓${NC} Dependencies already installed"
fi

# Check if instance directory exists
if [ ! -d "instance" ]; then
    echo -e "   Creating instance directory..."
    mkdir -p instance
fi

# Check if database exists
if [ ! -f "instance/bananawiki.db" ]; then
    echo -e "${YELLOW}⚠${NC}  Database not found (will be created on first run)"
fi

echo ""
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${GREEN}Starting development server...${NC}"
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""
echo -e "  URL:  ${BLUE}http://${HOST}:${PORT}${NC}"
echo -e "  Mode: ${YELLOW}Development (single-threaded)${NC}"
echo ""
echo -e "${YELLOW}Note: This is NOT suitable for production use!${NC}"
echo -e "      For production, use: ${BLUE}sudo ./banana install --mode wiki${NC}"
echo ""
echo -e "Press ${RED}Ctrl+C${NC} to stop"
echo ""

# Export host and port for Flask dev server
export FLASK_HOST="$HOST"
export FLASK_PORT="$PORT"

# Run Flask development server
python3 -c "
import sys
import os
from app import app

host = os.environ.get('FLASK_HOST', '127.0.0.1')
port = int(os.environ.get('FLASK_PORT', 5001))

print(f'Starting Flask development server on {host}:{port}...\n', flush=True)
app.run(host=host, port=port, debug=True, use_reloader=False)
"
