#!/usr/bin/env python3
"""Print in-process BananaWiki observability counters as JSON."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from ops_observability import get_observability_snapshot


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export in-process observability snapshot")
    parser.add_argument(
        "--output",
        default="",
        help="Optional file path to write JSON output. Prints to stdout when omitted.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    payload = get_observability_snapshot()
    text = json.dumps(payload, indent=2)
    if args.output:
        out = Path(args.output).resolve()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text + "\n", encoding="utf-8")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
