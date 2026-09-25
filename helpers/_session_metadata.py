"""Safe display metadata for persistent login sessions."""

import ipaddress
import re


def normalize_session_ip(value):
    """Return a canonical, bounded client IP for display and auditing."""
    raw = (value or "").strip()
    if not raw:
        return ""
    try:
        address = ipaddress.ip_address(raw)
        if getattr(address, "ipv4_mapped", None):
            address = address.ipv4_mapped
        return str(address)
    except ValueError:
        return raw[:45]


def normalize_session_user_agent(value):
    """Strip control characters and bound a user agent for safe storage."""
    cleaned = re.sub(r"[\x00-\x1f\x7f]+", " ", value or "")
    return " ".join(cleaned.split())[:512]


def describe_session_user_agent(value):
    """Return a compact browser and operating-system description."""
    ua = value or ""
    if "Edg/" in ua:
        browser = "Edge"
    elif "OPR/" in ua or "Opera" in ua:
        browser = "Opera"
    elif "Firefox/" in ua:
        browser = "Firefox"
    elif "Chrome/" in ua or "CriOS/" in ua:
        browser = "Chrome"
    elif "Safari/" in ua:
        browser = "Safari"
    else:
        browser = "Unknown browser"

    if "iPhone" in ua or "iPad" in ua:
        platform = "iOS"
    elif "Android" in ua:
        platform = "Android"
    elif "Windows" in ua:
        platform = "Windows"
    elif "Macintosh" in ua or "Mac OS X" in ua:
        platform = "macOS"
    elif "Linux" in ua:
        platform = "Linux"
    else:
        platform = "Unknown device"
    return f"{browser} on {platform}"
