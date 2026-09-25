"""Minimal WSGI application that serves the redirect page.

Extracts the subdomain from the ``Host`` header, builds a redirect URL
to the same subdomain on the new domain, and returns a self-contained
HTML page.  No templates, no database, no dependencies.

Both domains must be configured.  Old domains are given as a
comma-separated env var::

    LEGACY_OLD_DOMAINS=old.example.com,legacy.example.org
    LEGACY_NEW_DOMAIN=wiki.example.net

``LEGACY_OLD_DOMAIN`` (singular) also works and is added to the list.
Nothing is assumed if either is missing: importing this module then
raises, so a misconfigured service fails at startup instead of sending
visitors to somebody else's domain.
"""

import html
import os


def _parse_old_domains():
    """Return the list of old domains to match against."""
    domains = []
    # New multi-domain var (comma-separated)
    multi = os.environ.get("LEGACY_OLD_DOMAINS", "").strip()
    if multi:
        domains.extend(d.strip().lower() for d in multi.split(",") if d.strip())
    # Legacy single-domain var (backward compat)
    single = os.environ.get("LEGACY_OLD_DOMAIN", "").strip().lower()
    if single and single not in domains:
        domains.append(single)
    if not domains:
        raise RuntimeError(
            "Set LEGACY_OLD_DOMAINS to the domain you are redirecting away "
            "from, as a comma-separated list."
        )
    return domains


def _parse_new_domain():
    """Return the domain to redirect to."""
    new = os.environ.get("LEGACY_NEW_DOMAIN", "").strip().lower()
    if not new:
        raise RuntimeError(
            "Set LEGACY_NEW_DOMAIN to the domain visitors should be sent to."
        )
    return new


OLD_DOMAINS = _parse_old_domains()
NEW_DOMAIN = _parse_new_domain()
NEW_SCHEME = os.environ.get("LEGACY_NEW_SCHEME", "https")


def _extract_subdomain(host):
    """Return (subdomain, matched_old_domain) or (None, None)."""
    hostname = host.split(":")[0].strip().lower()
    for old in OLD_DOMAINS:
        if hostname.endswith("." + old):
            sub = hostname[: -(len(old) + 1)]
            if sub and sub != "www":
                return sub, old
    return None, None


def _build_page(subdomain):
    """Return the full HTML page as a string."""
    safe_sub = html.escape(subdomain) if subdomain else None
    if safe_sub:
        target_url = f"{NEW_SCHEME}://{safe_sub}.{NEW_DOMAIN}"
        safe_target = html.escape(target_url)
    else:
        target_url = f"{NEW_SCHEME}://{NEW_DOMAIN}"
        safe_target = html.escape(target_url)

    wiki_button = ""
    if safe_sub:
        wiki_button = (
            f'<a href="{safe_target}" class="btn primary">'
            f"Go to {safe_sub}.{html.escape(NEW_DOMAIN)}</a>"
        )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>We moved | BananaWiki</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{
            font-family: system-ui, -apple-system, BlinkMacSystemFont,
                         "Segoe UI", Roboto, sans-serif;
            background: #16161f;
            color: #e8e8ef;
            display: flex;
            align-items: center;
            justify-content: center;
            min-height: 100vh;
            padding: 1.5rem;
        }}
        .card {{
            background: #1e1e2c;
            border: 1px solid rgba(255, 255, 255, .08);
            border-radius: 14px;
            padding: 2.5rem 2rem;
            max-width: 520px;
            width: 100%;
            text-align: center;
            box-shadow: 0 8px 32px rgba(0, 0, 0, .35);
        }}
        .icon {{
            margin-bottom: 1rem;
        }}
        h1 {{
            font-size: 1.6rem;
            font-weight: 700;
            color: #fff;
            margin-bottom: .6rem;
        }}
        .lead {{
            color: #b0b0be;
            line-height: 1.65;
            font-size: .95rem;
            margin-bottom: 1.5rem;
        }}
        .btn {{
            display: inline-block;
            padding: .7rem 1.6rem;
            border-radius: 8px;
            font-size: .92rem;
            font-weight: 600;
            text-decoration: none;
            transition: background .2s, transform .15s;
            cursor: pointer;
        }}
        .btn:hover {{ transform: translateY(-1px); }}
        .primary {{
            background: #7e9ada;
            color: #0d0d14;
        }}
        .primary:hover {{ background: #93ade6; }}
        .outline {{
            background: transparent;
            color: #b0b0be;
            border: 1px solid rgba(255, 255, 255, .12);
            margin-top: .6rem;
            font-size: .84rem;
            padding: .55rem 1.2rem;
        }}
        .outline:hover {{
            background: rgba(255, 255, 255, .05);
            color: #e8e8ef;
        }}
        .buttons {{
            display: flex;
            flex-direction: column;
            align-items: center;
            gap: .25rem;
        }}
        .note {{
            margin-top: 1.5rem;
            padding-top: 1.2rem;
            border-top: 1px solid rgba(255, 255, 255, .06);
            color: #777;
            font-size: .82rem;
            line-height: 1.55;
        }}
        .note a {{ color: #7e9ada; text-decoration: none; }}
        .note a:hover {{ text-decoration: underline; }}
        @media (max-width: 480px) {{
            .card {{ padding: 1.8rem 1.3rem; }}
            h1 {{ font-size: 1.3rem; }}
        }}
    </style>
</head>
<body>
    <div class="card">
        <div class="icon">
            <svg viewBox="0 0 24 24" width="48" height="48" fill="none"
                 stroke="currentColor" stroke-width="1.5" stroke-linecap="round"
                 stroke-linejoin="round" style="color:#7e9ada">
                <path d="M15 3h6v6"/>
                <path d="M10 14L21 3"/>
                <path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/>
            </svg>
        </div>
        <h1>We moved!</h1>
        <p class="lead">
            BananaWiki and its services have moved to the new
            <strong>{html.escape(NEW_DOMAIN)}</strong> domain.
            {"" if not safe_sub else f" If you are looking for the wiki <strong>{safe_sub}</strong>, click the button below to be redirected."}
        </p>
        <div class="buttons">
            {wiki_button}
            <a href="{NEW_SCHEME}://{html.escape(NEW_DOMAIN)}" class="btn outline">
                Visit {html.escape(NEW_DOMAIN)}
            </a>
        </div>
        <p class="note">
            We moved to the new domain on July 23, 2026.
            If you are looking for other internal services still hosted on
            this domain (e.g.&nbsp;monitoring, CI), visit their specific
            subdomain directly. Those have not moved.
        </p>
    </div>
</body>
</html>"""


def application(environ, start_response):
    """WSGI entry point."""
    host = environ.get("HTTP_HOST", "")
    subdomain, _matched = _extract_subdomain(host)

    body = _build_page(subdomain).encode("utf-8")
    headers = [
        ("Content-Type", "text/html; charset=utf-8"),
        ("Content-Length", str(len(body))),
        ("Cache-Control", "public, max-age=3600"),
    ]
    start_response("200 OK", headers)
    return [body]
