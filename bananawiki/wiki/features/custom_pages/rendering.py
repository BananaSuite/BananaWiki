"""Responses for each custom page content type.

Security model
--------------
* Author-controlled HTML, CSS and JavaScript (``html``, ``html_styled``,
  ``html_full``, ``markdown``) never run with the wiki's origin. The page's
  path shows a wiki-made wrapper with a sandboxed ``<iframe>``; the author's
  document comes from :func:`document_response` with a
  ``Content-Security-Policy: sandbox`` header, so even when opened directly
  it gets an opaque origin (no cookies, no same-origin requests). Only
  ``html_full`` may run scripts.
* Data types (text, JSON, XML) are sent with ``sandbox; default-src 'none'``.
* Uploaded files go through :func:`storage.send` (inline only for harmless
  types, sandboxed, ``nosniff``).
* Everything shown inside the wiki layout is escaped or passes through
  :func:`markdown.render`.
"""

from __future__ import annotations

import dataclasses
from typing import Any
from urllib.parse import urlencode

from flask import Response, abort, current_app, make_response, redirect, render_template, request
from markupsafe import Markup, escape

from ....core import web
from ... import auth, markdown, settings, storage
from . import service

_DATA_CSP = "sandbox; default-src 'none'"
_DATA_TYPES = {"plain_text": "text/plain", "json_content": "application/json", "xml_content": "application/xml"}


def layout() -> str:
    """The wiki shell for members (and public mode), the bare layout otherwise."""
    return "base.html" if auth.current_user() or settings.public_mode_active() else "base_minimal.html"


def _page_template(name: str, page: dict[str, Any], **context: Any) -> Response:
    return make_response(render_template(f"custom_pages/{name}.html", page=page, layout=layout(), **context))


def _first_file(page: dict[str, Any], prefix: str = "") -> dict[str, Any] | None:
    for row in service.files(page["id"]):
        if (row.get("mime_type") or "").startswith(prefix):
            return row
    return None


def send_file_row(row: dict[str, Any], *, download: bool = False) -> Response:
    return storage.send("custom_page_files", row["filename"], download_name=row["original_name"],
                        inline=not download, blob_id=row.get("blob_id"))


def youtube_embed_url(video_id: str, page: dict[str, Any]) -> str:
    """youtube-nocookie player URL (allowed by the default CSP frame-src)."""
    params: dict[str, str] = {}
    if page.get("video_autoplay"):
        params.update(autoplay="1", mute="1")  # browsers only autoplay muted videos
    if not page.get("video_controls"):
        params["controls"] = "0"
    if page.get("video_loop"):
        params.update(loop="1", playlist=video_id)
    base = f"https://www.youtube-nocookie.com/embed/{video_id}"
    return f"{base}?{urlencode(params)}" if params else base


def _with_frame_origin(response: Response, origin: str | None) -> Response:
    """Widen ``frame-src`` of this one response to the embedded page's origin."""
    if origin:
        policy: web.SecurityPolicy = current_app.extensions.setdefault("bananawiki.csp", web.SecurityPolicy())
        widened = dataclasses.replace(policy, frame_src=[*policy.frame_src, origin])
        response.headers["Content-Security-Policy"] = widened.header(web.csp_nonce())
    return response


def _builder_body(page: dict[str, Any]) -> Markup | None:
    """A builder page's document rendered like a builder wiki page (None: show its Markdown).

    The Markdown twin is shown instead while the builder is off, or when the
    content no longer matches the document (changed by a release without it).
    """
    if not service.is_builder_page(page) or not service.builder_available():
        return None
    from ..page_builder import document

    try:
        loaded = document.load(page["builder_json"])
    except document.DocumentError:
        return None
    if not document.is_current(page, loaded):
        return None
    return document.render(loaded)


def serve(page: dict[str, Any]) -> Response:
    """The response for a visible custom page (404 when it has nothing to show)."""
    kind = page["content_type"]
    if kind == "redirect":
        target = (page.get("redirect_url") or "").strip()
        if not service.is_safe_redirect(target):
            abort(404)
        return redirect(target, code=301 if page.get("redirect_code") == 301 else 302)
    if kind in _DATA_TYPES:
        response = Response(page.get("content") or "", mimetype=_DATA_TYPES[kind])
        response.headers["Content-Security-Policy"] = _DATA_CSP
        return response
    if kind in service.SANDBOXED_TYPES:
        return _page_template("sandboxed", page, sandbox=sandbox_flags(kind in service.SCRIPTED_TYPES))
    if kind in ("image", "file_download"):
        row = _first_file(page)
        if row is None:
            abort(404)
        return send_file_row(row, download=kind == "file_download")
    if kind == "wiki_page":
        builder = _builder_body(page)
        if builder is not None:
            return _page_template("builder", page, body=builder)
        return _page_template("wiki", page, body=Markup(markdown.render(page.get("content") or "")))
    if kind in ("image_page", "file_listing"):
        return _page_template(kind, page, files=service.files(page["id"]))
    if kind == "youtube_video":
        video_id = service.youtube_id(page.get("video_url"))
        embed = youtube_embed_url(video_id, page) if video_id else None
        return _page_template("youtube", page, video_id=video_id, embed_url=embed)
    if kind == "video_hosted":
        return _page_template("video", page, video=_first_file(page, "video/"))
    if kind in ("iframe_embed", "page_embed"):
        url = (page.get("iframe_url") or "").strip()
        origin = service.https_origin(url)
        sandbox = service.sandbox_tokens(page.get("iframe_sandbox"))
        if kind == "iframe_embed" and not sandbox:
            sandbox = service.DEFAULT_EMBED_SANDBOX
        response = _page_template("iframe", page, url=url if origin else None,
                                  height=service.iframe_height(page.get("iframe_height")),
                                  sandbox=sandbox, allow=page.get("iframe_allow") or "fullscreen")
        return _with_frame_origin(response, origin)
    if kind == "link_list":
        return _page_template("link_list", page, links=service.parse_links(page.get("links_json")))
    if kind == "code_snippet":
        highlighted = Markup(markdown.highlight_code(page.get("content") or "", page.get("code_language")))
        return _page_template("code", page, code=highlighted)
    abort(404)


# ── Sandboxed author documents ────────────────────────────────────────────────


def sandbox_flags(scripts: bool) -> str:
    """Tokens for both the iframe ``sandbox`` attribute and the CSP ``sandbox`` directive.

    Never ``allow-same-origin``: the document always has an opaque origin.
    """
    flags = ["allow-popups", "allow-popups-to-escape-sandbox", "allow-top-navigation-by-user-activation"]
    if scripts:
        flags.insert(0, "allow-scripts")
    return " ".join(flags)


def document_csp(scripts: bool) -> str:
    """Policy of a sandboxed author document (its origin is opaque, so inline code is contained)."""
    origin = request.host_url.rstrip("/")
    media = f"'self' {origin} https: data: blob:"
    return "; ".join([
        f"sandbox {sandbox_flags(scripts)}",
        "default-src 'none'",
        "script-src 'unsafe-inline' https:" if scripts else "script-src 'none'",
        "style-src 'unsafe-inline' https:",
        f"img-src {media}",
        f"media-src {media}",
        "font-src https: data:",
        "connect-src https:" if scripts else "connect-src 'none'",
        "frame-src https:",
        "form-action 'none'",
        "base-uri 'none'",
        "frame-ancestors 'self'",
    ])


def _standalone(page: dict[str, Any], body: str, *, css: str = "", js: str = "") -> str:
    """A minimal HTML document around author content (served only sandboxed)."""
    # "</style" would end the element early; "\3c " is the CSS escape for "<".
    style = "<style>" + css.replace("<", "\\3c ") + "</style>\n" if css else ""
    # Likewise "</script"; "<\/" means the same inside JavaScript strings and regexes.
    script = "<script>" + js.replace("</", "<\\/") + "</script>\n" if js else ""
    return (
        "<!DOCTYPE html>\n<html>\n<head>\n<meta charset=\"utf-8\">\n"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">\n"
        f"<title>{escape(page.get('title') or '')}</title>\n<base target=\"_top\">\n{style}"
        f"</head>\n<body>\n{body}\n{script}</body>\n</html>"
    )


def document_response(page: dict[str, Any]) -> Response:
    kind = page["content_type"]
    content = page.get("content") or ""
    if kind == "html":
        html = content
    elif kind == "markdown":
        html = _standalone(page, markdown.render(content), css=page.get("css") or "")
    else:
        html = _standalone(page, content, css=page.get("css") or "",
                           js=(page.get("js") or "") if kind in service.SCRIPTED_TYPES else "")
    response = Response(html, mimetype="text/html")
    response.headers["Content-Security-Policy"] = document_csp(kind in service.SCRIPTED_TYPES)
    response.headers["Cache-Control"] = "private, no-store"
    return response
