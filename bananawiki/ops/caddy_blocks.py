"""Caddyfile fragments shared by the managed Caddyfile and the agent's per-wiki routes.

Both files must behave the same behind Cloudflare's proxy (orange-cloud DNS
records), so the fragments live here, imported by :mod:`bananawiki.ops.caddy`
and :mod:`bananawiki.ops.runtime_agent` (which cannot import each other).

* **Real client address:** Caddy trusts ``CF-Connecting-IP`` only from
  Cloudflare's edge ranges (``trusted_proxies``) and hands the application
  ``X-Forwarded-For: {client_ip}``, one hop, as ``ProxyFix(x_for=1)`` expects.
  Without it every visitor behind Cloudflare shared the edge address, and with
  it the rate limits.
* **Plain HTTP:** every site answers on port 80 too. Caddy serves ACME HTTP-01
  challenges before any route; other requests are redirected to HTTPS, except
  requests from Cloudflare's edge whose visitor already used HTTPS
  (``CF-Visitor: {"scheme":"https"}``, Cloudflare's "Flexible" SSL mode),
  which are served instead of being redirected in an endless loop.

Standard library only (see :mod:`bananawiki.ops`).
"""

from __future__ import annotations

import re

from ..core import cloudflare

# ``client_ip_headers`` and the ``{client_ip}`` placeholder (Caddy 2.7); a managed
# wildcard certificate used for the subdomain sites it covers instead of
# obtaining one per site (Caddy 2.10) for the Cloudflare DNS mode.
CLIENT_IP_VERSION = (2, 7)
WILDCARD_VERSION = (2, 10)
HSTS = '\theader Strict-Transport-Security "max-age=31536000"\n'
# Above Cloudflare's 900 s reuse of idle origin connections: a shorter idle
# timeout makes Cloudflare reuse a connection Caddy just closed (HTTP 520).
IDLE_TIMEOUT = "16m"


def parse_version(text: str) -> tuple[int, int, int] | None:
    """``(2, 10, 2)`` from ``caddy version`` output such as ``v2.10.2 h1:…``; None when unknown."""
    match = re.search(r"\bv?(\d+)\.(\d+)(?:\.(\d+))?", text or "")
    if not match:
        return None
    return int(match.group(1)), int(match.group(2)), int(match.group(3) or 0)


def supports_client_ip(version: tuple[int, ...] | None) -> bool:
    """Unknown versions are assumed current (the official packages are)."""
    return version is None or tuple(version[:2]) >= CLIENT_IP_VERSION


def cloudflare_ranges() -> str:
    return " ".join(cloudflare.RANGES)


def servers_options(*, client_ip: bool = True) -> str:
    """The global ``servers`` block (inside the global options braces)."""
    body = f"\t\ttimeouts {{\n\t\t\tread_header 10s\n\t\t\tread_body 5m\n\t\t\tidle {IDLE_TIMEOUT}\n\t\t}}\n"
    if client_ip:
        body += f"\t\ttrusted_proxies static {cloudflare_ranges()}\n\t\tclient_ip_headers CF-Connecting-IP\n"
    return f"\tservers {{\n{body}\t}}\n"


def https_policy(indent: str = "\t") -> str:
    """Redirect plain HTTP to HTTPS unless Cloudflare already served the visitor over HTTPS."""
    i = indent
    return (
        f"{i}@needs_https {{\n{i}\tprotocol http\n{i}\tnot {{\n{i}\t\tremote_ip {cloudflare_ranges()}\n"
        f'{i}\t\theader CF-Visitor *"scheme":"https"*\n{i}\t}}\n{i}}}\n'
        f"{i}redir @needs_https https://{{host}}{{uri}} 308\n"
    )


def proxy(upstream: str, *, streaming: bool, client_ip: bool = True, indent: str = "\t",
          extra: tuple[str, ...] = ()) -> str:
    """A ``reverse_proxy`` to the application (``X-Forwarded-Prefix`` is never passed on)."""
    i = indent
    lines = [*extra]
    if streaming:
        lines.insert(0, "flush_interval -1")
    lines.append("header_up -X-Forwarded-Prefix")
    if client_ip:
        lines.append("header_up X-Forwarded-For {client_ip}")
    body = "".join(f"{i}\t{line}\n" for line in lines)
    return f"{i}reverse_proxy {upstream} {{\n{body}{i}}}\n"


def addresses(hosts: list[str] | tuple[str, ...]) -> str:
    """``a, b, http://a, http://b``: HTTPS (automatic certificates) and plain HTTP for the same site."""
    return ", ".join([*hosts, *(f"http://{host}" for host in hosts)])
