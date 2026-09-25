.PHONY: help dev start test clean

# Default target
help:
	@echo ""
	@echo "🍌 BananaWiki Hosting Platform: Make Commands"
	@echo "================================================"
	@echo ""
	@echo "Development:"
	@echo "  make dev           Start hosting portal dev server (http://localhost:5099)"
	@echo "  make test          Run the hosting platform test suite"
	@echo ""
	@echo "Production:"
	@echo "  make start         Start hosting portal with Gunicorn"
	@echo "  From repo root: sudo ./banana install --mode hosting --domain example.org"
	@echo ""
	@echo "Utilities:"
	@echo "  make clean         Remove cached files"
	@echo ""
	@echo "Quick start (port mode on VPS):"
	@echo "  export HOSTING_PUBLIC_HOST=YOUR_VPS_IP && make dev"
	@echo ""

# Development server
dev:
	@./dev.sh

# Production server
start:
	@./start.sh

# Run hosting tests (must run from repo root)
test:
	@cd .. && if [ ! -d "venv" ]; then \
		echo "Setting up virtual environment..."; \
		python3 -m venv venv; \
	fi
	@cd .. && . venv/bin/activate && pip install -q -r requirements.txt pytest
	@cd .. && . venv/bin/activate && python -m pytest tests/test_hosting.py -v

# Clean up
clean:
	@echo "Cleaning up..."
	@find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	@find . -type f -name "*.pyc" -delete 2>/dev/null || true
	@echo "✓ Cleanup complete"
