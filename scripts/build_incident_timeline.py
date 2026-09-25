#!/usr/bin/env python3
"""Build a reliability incident timeline from log files.

Parses nginx/systemd/app logs and reports first/last-seen timestamps for:
  - HTTP 500, 502, 504
  - deploy/restart/startup events
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, List, Optional

ISO_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?")
NGINX_RE = re.compile(r"\[(\d{2}/[A-Za-z]{3}/\d{4}:\d{2}:\d{2}:\d{2} [+-]\d{4})\]")

HTTP_500_RE = re.compile(r"(?:\s|\"|^)(500)(?:\s|\"|$)")
HTTP_502_RE = re.compile(r"(?:\s|\"|^)(502)(?:\s|\"|$)")
HTTP_504_RE = re.compile(r"(?:\s|\"|^)(504)(?:\s|\"|$)")

DEPLOY_RE = re.compile(
    r"(deploy\.sh|starting deployment|update complete|systemctl restart|"
    r'"operation"\s*:\s*"(?:install|update|restore|recover|restart)"|'
    r"failed to start|failed to bind|connection in use|bad gateway|gateway timeout|"
    r"gunicorn.*(starting|boot|worker))",
    flags=re.IGNORECASE,
)
_MAX_EVENT_LINE_LENGTH = 400
_MAX_CORRELATIONS = 2000


@dataclass
class Event:
    timestamp: datetime
    event_type: str
    file: str
    line: str


def _parse_timestamp(line: str) -> Optional[datetime]:
    iso_match = ISO_RE.search(line)
    if iso_match:
        raw = iso_match.group(0).replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(raw)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except ValueError:
            pass

    nginx_match = NGINX_RE.search(line)
    if nginx_match:
        try:
            dt = datetime.strptime(nginx_match.group(1), "%d/%b/%Y:%H:%M:%S %z")
            return dt.astimezone(timezone.utc)
        except ValueError:
            pass
    return None


def _iter_lines(paths: Iterable[Path]) -> Iterable[tuple[Path, str]]:
    for path in paths:
        if not path.exists() or not path.is_file():
            continue
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                yield path, line.rstrip("\n")


def classify_line(line: str) -> List[str]:
    kinds: List[str] = []
    if HTTP_500_RE.search(line):
        kinds.append("http_500")
    if HTTP_502_RE.search(line):
        kinds.append("http_502")
    if HTTP_504_RE.search(line):
        kinds.append("http_504")
    if DEPLOY_RE.search(line):
        kinds.append("deploy_or_restart")
    return kinds


def build_events(paths: Iterable[Path]) -> List[Event]:
    events: List[Event] = []
    for path, line in _iter_lines(paths):
        ts = _parse_timestamp(line)
        if not ts:
            continue
        for kind in classify_line(line):
            events.append(
                Event(
                    timestamp=ts,
                    event_type=kind,
                    file=str(path),
                    line=line[:_MAX_EVENT_LINE_LENGTH],
                )
            )
    events.sort(key=lambda e: e.timestamp)
    return events


def summarize(events: List[Event]) -> dict:
    by_type = {}
    for kind in ("http_500", "http_502", "http_504", "deploy_or_restart"):
        subset = [e for e in events if e.event_type == kind]
        if not subset:
            by_type[kind] = {"count": 0, "first_seen": None, "last_seen": None}
            continue
        by_type[kind] = {
            "count": len(subset),
            "first_seen": subset[0].timestamp.isoformat(),
            "last_seen": subset[-1].timestamp.isoformat(),
            "first_file": subset[0].file,
            "last_file": subset[-1].file,
        }

    # Correlate each symptom with nearest preceding deploy/restart marker.
    deploy_events = [e for e in events if e.event_type == "deploy_or_restart"]
    correlations = []
    for symptom in [e for e in events if e.event_type in ("http_500", "http_502", "http_504")]:
        nearest = None
        for dep in deploy_events:
            if dep.timestamp <= symptom.timestamp:
                nearest = dep
            else:
                break
        correlations.append(
            {
                "symptom": symptom.event_type,
                "symptom_at": symptom.timestamp.isoformat(),
                "symptom_file": symptom.file,
                "nearest_deploy_at": nearest.timestamp.isoformat() if nearest else None,
                "nearest_deploy_file": nearest.file if nearest else None,
            }
        )

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary": by_type,
        "correlations": correlations[:_MAX_CORRELATIONS],
    }


def default_log_paths() -> List[Path]:
    return [
        Path("/var/log/nginx/access.log"),
        Path("/var/log/nginx/error.log"),
        Path("/opt/bananawiki/data/logs/bananawiki.log"),
        Path("/opt/bananawiki/config/history.jsonl"),
        Path("/var/log/syslog"),
    ]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build BananaWiki reliability incident timeline")
    parser.add_argument(
        "--log",
        action="append",
        default=[],
        help="Log file path (repeatable). Defaults to common nginx/app/syslog paths.",
    )
    parser.add_argument(
        "--output",
        default="",
        help="Optional output JSON file path. If omitted, prints to stdout.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    paths = [Path(p).resolve() for p in args.log] if args.log else default_log_paths()
    events = build_events(paths)
    payload = summarize(events)
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
