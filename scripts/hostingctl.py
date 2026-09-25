#!/usr/bin/env python3
"""Convenience wrapper for the internal hosting admin CLI."""

import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from hosting.admin_cli import main


if __name__ == "__main__":
    raise SystemExit(main())
