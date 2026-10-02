"""Custom pages: admin-defined content served at arbitrary paths of the wiki.

Published custom pages (and their files) are public **by design**: they are
served to anonymous visitors even while the rest of the wiki needs a sign-in,
so an administrator can publish a landing page, an imprint or a download on a
private wiki. Unpublished pages are visible to their managers only. Managing
them (``custom_page.manage``) is reserved to administrators.

A custom page never overrides a route of the application: the serving rule is
a catch-all that Werkzeug only picks when no other rule matches, and paths
whose first segment belongs to a route (or to the reserved list) are refused
when a page is saved.
"""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any
from urllib.parse import parse_qs, urlsplit

from flask import current_app
from werkzeug.datastructures import FileStorage

from ....core.timeutil import now_sql
from ... import auth, settings, storage
from ...config import ATTACHMENT_EXTENSIONS, IMAGE_EXTENSIONS, MIB
from ...db import db

CONTENT_TYPE_GROUPS: dict[str, tuple[str, ...]] = {
    "markup": ("redirect", "builder", "html", "html_styled", "html_full", "markdown", "wiki_page", "plain_text"),
    "data": ("json_content", "xml_content"),
    "media": ("image", "image_page", "youtube_video", "video_hosted"),
    "files": ("file_download", "file_listing"),
    "embed": ("iframe_embed", "page_embed"),
    "misc": ("link_list", "code_snippet"),
}
CONTENT_TYPES = tuple(kind for kinds in CONTENT_TYPE_GROUPS.values() for kind in kinds)
# Pages made with the visual builder: stored as a ``wiki_page`` whose ``content``
# is the Markdown twin of ``builder_json`` (the column's CHECK constraint predates
# the builder, and releases that do not know it still show the Markdown).
BUILDER_TYPE = "builder"
BUILDER_STORED_TYPE = "wiki_page"

# Author HTML/CSS/JS: only ever served from the sandboxed document route.
SANDBOXED_TYPES = frozenset({"html", "html_styled", "html_full", "markdown"})
SCRIPTED_TYPES = frozenset({"html_full"})
IMAGE_TYPES = frozenset({"image", "image_page"})

# Which form sections each type uses (the others keep their stored values).
TYPE_FIELDS: dict[str, frozenset[str]] = {
    "redirect": frozenset({"redirect"}),
    "builder": frozenset({"builder", "meta"}),
    "html": frozenset({"content", "meta"}),
    "html_styled": frozenset({"content", "css", "meta"}),
    "html_full": frozenset({"content", "css", "js", "meta"}),
    "markdown": frozenset({"content", "css", "meta"}),
    "wiki_page": frozenset({"content", "meta"}),
    "plain_text": frozenset({"content"}),
    "json_content": frozenset({"content"}),
    "xml_content": frozenset({"content"}),
    "image": frozenset({"file"}),
    "image_page": frozenset({"file", "meta"}),
    "youtube_video": frozenset({"youtube", "video", "meta"}),
    "video_hosted": frozenset({"video", "file", "meta"}),
    "file_download": frozenset({"file"}),
    "file_listing": frozenset({"file", "meta"}),
    "iframe_embed": frozenset({"iframe", "meta"}),
    "page_embed": frozenset({"iframe", "meta"}),
    "link_list": frozenset({"links", "meta"}),
    "code_snippet": frozenset({"content", "code", "meta"}),
}

# First path segments that belong to BananaWiki (1.4 list plus 1.6 routes).
# Every first segment of a registered route is reserved too, see reserved_segments().
RESERVED_SEGMENTS = frozenset({
    "_cpf", "_cpd", "admin", "announcements", "api", "api-docs", "badges", "canvas", "chats", "create",
    "federation", "global-settings", "groups", "health", "healthz", "kanban", "leaderboard", "lockdown",
    "login", "logout", "maintenance", "onboarding", "page", "reservations", "robots.txt", "search",
    "session-conflict", "settings", "setup", "signup", "source", "static", "users", "wiki", "favicon.ico",
})
CATCH_ALL_ENDPOINT = "custom_page"

MAX_PATH = 500
MAX_TITLE = 200
MAX_META = 300
MAX_CONTENT = 1_000_000
MAX_CODE_FIELD = 200_000
MAX_LINKS = 500
DEFAULT_IFRAME_HEIGHT = "600px"
DEFAULT_EMBED_SANDBOX = "allow-scripts allow-same-origin allow-popups"
SANDBOX_TOKENS = frozenset({
    "allow-downloads", "allow-forms", "allow-modals", "allow-orientation-lock", "allow-pointer-lock",
    "allow-popups", "allow-popups-to-escape-sandbox", "allow-presentation", "allow-same-origin",
    "allow-scripts", "allow-storage-access-by-user-activation", "allow-top-navigation-by-user-activation",
})
FILE_EXTENSIONS = ATTACHMENT_EXTENSIONS | frozenset({"ogv", "mov", "m4v", "mkv", "avi", "flac", "aac", "avif"})

_SEGMENT = re.compile(r"^[\w.~@+-]+$")
_HEIGHT = re.compile(r"^\d{1,4}(?:px|vh|%|em|rem)$")
_ALLOW_DIRECTIVE = re.compile(r"^[a-z][a-z-]*(?:\s+(?:'self'|'src'|'none'|\*|https://[^\s;'\"<>]+))*$")
_CODE_LANGUAGE = re.compile(r"^[A-Za-z0-9_+#.-]{1,40}$")
_YOUTUBE_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_YOUTUBE_HOSTS = frozenset({"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"})
_URL_IGNORED = re.compile(r"[\x00-\x20\x7f]")


class CustomPageError(ValueError):
    """A refused change; ``key`` is a translation key."""

    def __init__(self, key: str, **values: Any):
        super().__init__(key)
        self.key = key
        self.values = values


# ── Access ───────────────────────────────────────────────────────────────────


def can_manage(user: dict[str, Any] | None = None) -> bool:
    """Administrators holding ``custom_page.manage`` (false while the feature is off)."""
    user = auth.current_user() if user is None else user
    return bool(user) and auth.is_admin(user) and auth.has_permission("custom_page.manage", user)


def is_visible(page: dict[str, Any] | None, user: dict[str, Any] | None = None) -> bool:
    """Published pages are public on purpose; unpublished ones are for managers only."""
    if page is None:
        return False
    return bool(page.get("is_published")) or can_manage(user)


# ── Reading ──────────────────────────────────────────────────────────────────


def get(page_id: int) -> dict[str, Any] | None:
    return db.one("SELECT * FROM custom_pages WHERE id = ?", (page_id,))


def is_builder_page(page: dict[str, Any]) -> bool:
    return page.get("content_type") == BUILDER_STORED_TYPE and bool(page.get("builder_json"))


def form_type(page: dict[str, Any]) -> str:
    """The content type the editor shows (``builder`` for builder pages)."""
    return BUILDER_TYPE if is_builder_page(page) else page["content_type"]


def builder_available() -> bool:
    """Whether the visual builder may be used here (feature on, not forbidden by the host)."""
    from ..page_builder import service as page_builder

    return page_builder.is_active()


def builder_document(page: dict[str, Any]) -> dict[str, Any]:
    """The page's builder document, or its text as Markdown blocks when it has none (yet)."""
    from ..page_builder import document

    try:
        loaded = document.load(page.get("builder_json") or "")
    except document.DocumentError:
        loaded = None
    if loaded is not None and document.is_current(page, loaded):
        return loaded
    return document.from_markdown(page.get("content") or "")


def get_by_path(path: str) -> dict[str, Any] | None:
    return db.one("SELECT * FROM custom_pages WHERE path = ?", (path,))


def list_all() -> list[dict[str, Any]]:
    return db.all("SELECT * FROM custom_pages ORDER BY path")


def files(page_id: int) -> list[dict[str, Any]]:
    return db.all("SELECT * FROM custom_page_files WHERE custom_page_id = ? ORDER BY uploaded_at, id", (page_id,))


def get_file(file_id: int) -> dict[str, Any] | None:
    return db.one("SELECT * FROM custom_page_files WHERE id = ?", (file_id,))


# ── Paths ────────────────────────────────────────────────────────────────────


def normalise_path(raw: str | None) -> str:
    """``" a//b/ "`` -> ``"/a/b"``; an empty value becomes ``"/"``."""
    parts = [part.strip() for part in (raw or "").strip().split("/") if part.strip()]
    return "/" + "/".join(parts)


def reserved_segments() -> frozenset[str]:
    """First path segments claimed by the application's routes, plus the fixed list."""
    rules = list(current_app.url_map.iter_rules())
    cache = current_app.extensions.get("custom_pages.reserved")
    if cache is not None and cache[0] == len(rules):
        return cache[1]
    claimed = set(RESERVED_SEGMENTS)
    for rule in rules:
        if rule.endpoint == CATCH_ALL_ENDPOINT:
            continue
        first = rule.rule.lstrip("/").split("/", 1)[0]
        if first and "<" not in first:
            claimed.add(first.lower())
    result = frozenset(claimed)
    current_app.extensions["custom_pages.reserved"] = (len(rules), result)
    return result


def is_reserved(path: str) -> bool:
    first = path.strip("/").split("/", 1)[0].lower()
    return not first or first in reserved_segments()


def validate_path(raw: str | None, *, page_id: int | None = None) -> str:
    path = normalise_path(raw)
    if path == "/" or len(path) > MAX_PATH:
        raise CustomPageError("custom_pages.error.path_required")
    segments = path.strip("/").split("/")
    if any(seg in (".", "..") or not _SEGMENT.match(seg) for seg in segments):
        raise CustomPageError("custom_pages.error.path_invalid")
    if is_reserved(path):
        raise CustomPageError("custom_pages.error.path_reserved")
    existing = get_by_path(path)
    if existing is not None and existing["id"] != page_id:
        raise CustomPageError("custom_pages.error.path_taken")
    return path


# ── URL checks ───────────────────────────────────────────────────────────────


def url_scheme(url: str | None) -> str | None:
    """Scheme as a browser reads it ("" for relative URLs, None when unusable)."""
    cleaned = _URL_IGNORED.sub("", url if isinstance(url, str) else "")
    if not cleaned:
        return None
    try:
        return urlsplit(cleaned).scheme.lower()
    except ValueError:
        return None


def is_local_path(url: str) -> bool:
    return url.startswith("/") and not url.startswith("//") and "\\" not in url and not _URL_IGNORED.search(url)


def is_safe_redirect(url: str | None) -> bool:
    """A path on this wiki or an absolute http(s) URL."""
    url = (url or "").strip()
    if not url:
        return False
    if is_local_path(url):
        return True
    if _URL_IGNORED.search(url):
        return False
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    return parts.scheme.lower() in ("http", "https") and bool(parts.netloc)


def is_safe_link(url: str | None) -> bool:
    """Link-list targets: redirect targets plus ``mailto:``."""
    url = (url or "").strip()
    return is_safe_redirect(url) or (url_scheme(url) == "mailto" and not _URL_IGNORED.search(url))


def https_origin(url: str | None) -> str | None:
    """``https://host[:port]`` of an https URL, else None."""
    if not url or _URL_IGNORED.search(url):
        return None
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    host = parts.hostname or ""
    if parts.scheme.lower() != "https" or not re.fullmatch(r"[a-z0-9.-]+", host) or parts.username:
        return None
    return f"https://{host}:{port}" if port else f"https://{host}"


def youtube_id(url_or_id: str | None) -> str | None:
    """Video id from a YouTube URL (watch, youtu.be, embed, shorts, v) or a bare id."""
    raw = (url_or_id or "").strip()
    if _YOUTUBE_ID.match(raw):
        return raw
    try:
        parts = urlsplit(raw)
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    candidate = ""
    if host in ("youtu.be", "www.youtu.be"):
        candidate = parts.path.lstrip("/").split("/")[0]
    elif host in _YOUTUBE_HOSTS:
        query = parse_qs(parts.query)
        if "v" in query:
            candidate = query["v"][0]
        else:
            segments = [s for s in parts.path.split("/") if s]
            if len(segments) >= 2 and segments[0] in ("embed", "shorts", "v", "e"):
                candidate = segments[1]
    return candidate if _YOUTUBE_ID.match(candidate) else None


def iframe_height(value: str | None) -> str:
    value = (value or "").strip()
    return value if _HEIGHT.match(value) else DEFAULT_IFRAME_HEIGHT


def sandbox_tokens(value: str | None) -> str:
    """Stored sandbox tokens that are still valid (legacy rows may hold others)."""
    return " ".join(token for token in (value or "").split() if token in SANDBOX_TOKENS)


def parse_links(raw: str | None) -> list[dict[str, str]]:
    """Links of a link list page; entries with an unsafe URL are dropped."""
    try:
        data = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    links = []
    for item in data:
        if isinstance(item, dict) and isinstance(item.get("url"), str) and is_safe_link(item["url"]):
            links.append({
                "url": item["url"].strip(),
                "title": str(item.get("title") or "")[:300],
                "description": str(item.get("description") or "")[:1000],
            })
    return links


# ── Validation ───────────────────────────────────────────────────────────────


def _text(form: Any, name: str, limit: int, *, strip: bool = True) -> str:
    value = str(form.get(name) or "").replace("\r\n", "\n")
    value = value.strip() if strip else value
    if len(value) > limit:
        raise CustomPageError("custom_pages.error.too_long", field=name)
    return value


def _clean_links(raw: str) -> str:
    try:
        data = json.loads(raw or "[]")
    except ValueError:
        raise CustomPageError("custom_pages.error.links_json") from None
    if not isinstance(data, list) or len(data) > MAX_LINKS:
        raise CustomPageError("custom_pages.error.links_json")
    links = []
    for item in data:
        if not isinstance(item, dict) or not isinstance(item.get("url"), str):
            raise CustomPageError("custom_pages.error.links_json")
        if not is_safe_link(item["url"]):
            raise CustomPageError("custom_pages.error.link_url", url=item["url"][:100])
        link = {"title": str(item.get("title") or "")[:300], "url": item["url"].strip()}
        if item.get("description"):
            link["description"] = str(item["description"])[:1000]
        links.append(link)
    return json.dumps(links, ensure_ascii=False)


def _clean_section(section: str, content_type: str, form: Any) -> dict[str, Any]:
    """Validated columns of one form section."""
    if section == "content":
        content = _text(form, "content", MAX_CONTENT, strip=False)
        if content_type == "json_content" and content.strip():
            try:
                json.loads(content)
            except ValueError:
                raise CustomPageError("custom_pages.error.json_invalid") from None
        return {"content": content}
    if section == "css":
        return {"css": _text(form, "css", MAX_CODE_FIELD, strip=False)}
    if section == "js":
        return {"js": _text(form, "js", MAX_CODE_FIELD, strip=False)}
    if section == "meta":
        return {"meta_description": _text(form, "meta_description", MAX_META)}
    if section == "redirect":
        url = _text(form, "redirect_url", 2048)
        if url and not is_safe_redirect(url):
            raise CustomPageError("custom_pages.error.redirect_url")
        code = str(form.get("redirect_code") or "302")
        return {"redirect_url": url, "redirect_code": 301 if code == "301" else 302}
    if section == "iframe":
        url = _text(form, "iframe_url", 2048)
        if url and https_origin(url) is None:
            raise CustomPageError("custom_pages.error.iframe_url")
        height = _text(form, "iframe_height", 20) or DEFAULT_IFRAME_HEIGHT
        if not _HEIGHT.match(height):
            raise CustomPageError("custom_pages.error.iframe_height")
        tokens = _text(form, "iframe_sandbox", 500).split()
        if any(token not in SANDBOX_TOKENS for token in tokens):
            raise CustomPageError("custom_pages.error.iframe_sandbox")
        allow = _text(form, "iframe_allow", 500)
        directives = [d.strip() for d in allow.split(";") if d.strip()]
        if any(not _ALLOW_DIRECTIVE.match(d) for d in directives):
            raise CustomPageError("custom_pages.error.iframe_allow")
        return {"iframe_url": url, "iframe_height": height, "iframe_sandbox": " ".join(tokens),
                "iframe_allow": "; ".join(directives)}
    if section == "youtube":
        url = _text(form, "video_url", 2048)
        if url and youtube_id(url) is None:
            raise CustomPageError("custom_pages.error.youtube_url")
        return {"video_url": url}
    if section == "video":
        return {name: 1 if form.get(name) else 0
                for name in ("video_autoplay", "video_controls", "video_loop", "video_muted")}
    if section == "code":
        language = _text(form, "code_language", 40) or "text"
        if not _CODE_LANGUAGE.match(language):
            raise CustomPageError("custom_pages.error.code_language")
        return {"code_language": language}
    if section == "links":
        return {"links_json": _clean_links(_text(form, "links_json", MAX_CONTENT))}
    return {}


def _builder_values(current: dict[str, Any] | None) -> dict[str, Any]:
    """Columns that turn a page into a builder page, keeping an existing document or text."""
    from ..page_builder import document

    if current is not None and is_builder_page(current):
        return {"content_type": BUILDER_STORED_TYPE}
    if not builder_available():
        raise CustomPageError("custom_pages.error.builder_off")
    start = builder_document(current) if current is not None else document.empty()
    return {"content_type": BUILDER_STORED_TYPE, "builder_json": document.dump(start),
            "content": document.to_markdown(start)}


def clean(form: Any, *, page_id: int | None = None, current: dict[str, Any] | None = None) -> dict[str, Any]:
    """Columns to store from a submitted form (*current*: the stored page, when editing).

    Only the sections the chosen content type uses are read and validated;
    the other columns keep their stored values.
    """
    content_type = str(form.get("content_type") or "")
    if content_type not in CONTENT_TYPES:
        raise CustomPageError("custom_pages.error.content_type")
    title = _text(form, "title", MAX_TITLE)
    values: dict[str, Any] = {
        "path": validate_path(form.get("path"), page_id=page_id),
        "title": title,
        "content_type": content_type,
        "is_published": 1 if form.get("is_published") else 0,
    }
    for section in TYPE_FIELDS[content_type]:
        values.update(_clean_section(section, content_type, form))
    if content_type == BUILDER_TYPE:
        values.update(_builder_values(current))
    elif current is not None and current.get("builder_json"):
        values["builder_json"] = ""  # another type chosen: the page stops being a builder page
    return values


def save_builder_document(page: dict[str, Any], validated: dict[str, Any], *, title: str | None) -> None:
    """Store a validated builder document and its Markdown twin on a builder page."""
    from ..page_builder import document

    values: dict[str, Any] = {"builder_json": document.dump(validated), "content": document.to_markdown(validated),
                              "content_type": BUILDER_STORED_TYPE}
    if title is not None:
        values["title"] = title
    update(page["id"], values)


# ── Writing ──────────────────────────────────────────────────────────────────


def create(values: dict[str, Any], *, created_by: str) -> int:
    now = now_sql()
    try:
        return db.insert("custom_pages", {**values, "created_by": created_by, "created_at": now, "updated_at": now})
    except sqlite3.IntegrityError:
        raise CustomPageError("custom_pages.error.path_taken") from None


def update(page_id: int, values: dict[str, Any]) -> None:
    try:
        db.update("custom_pages", {**values, "updated_at": now_sql()}, "id = ?", (page_id,))
    except sqlite3.IntegrityError:
        raise CustomPageError("custom_pages.error.path_taken") from None


def _remove_file_rows(rows: list[dict[str, Any]]) -> None:
    """Delete stored files, their 1.4 database copies and their rows."""
    with db.transaction():
        for row in rows:
            db.execute("DELETE FROM custom_page_files WHERE id = ?", (row["id"],))
            if row.get("blob_id"):
                db.execute("DELETE FROM file_blobs WHERE id = ?", (row["blob_id"],))
    for row in rows:
        storage.delete("custom_page_files", row["filename"])


def delete(page: dict[str, Any]) -> None:
    _remove_file_rows(files(page["id"]))
    db.execute("DELETE FROM custom_pages WHERE id = ?", (page["id"],))


def delete_file(file_row: dict[str, Any]) -> None:
    _remove_file_rows([file_row])


def max_video_bytes() -> int:
    configured = int(settings.get("custom_pages_max_video_size_mb", 0) or 0)
    return configured * MIB if configured > 0 else current_app.config["BW"].max_custom_page_video_size


def add_file(page: dict[str, Any], upload: FileStorage) -> dict[str, Any]:
    """Store one uploaded file for *page*. Raises :class:`storage.UploadError`."""
    cfg = current_app.config["BW"]
    ext = storage.extension(upload.filename)
    is_video = page["content_type"] == "video_hosted" or ext in {"mp4", "webm", "ogv", "mov", "m4v", "mkv", "avi"}
    images_only = page["content_type"] in IMAGE_TYPES
    stored = storage.save(
        upload, "custom_page_files",
        allowed=IMAGE_EXTENSIONS if images_only else FILE_EXTENSIONS,
        max_bytes=max_video_bytes() if is_video else storage.max_upload_bytes(cfg.max_custom_page_file_size),
        images_only=images_only,
    )
    file_id = db.insert("custom_page_files", {
        "custom_page_id": page["id"], "filename": stored.filename, "original_name": stored.original_name,
        "mime_type": stored.mime_type, "file_size": stored.size, "uploaded_at": now_sql(),
    })
    row = get_file(file_id)
    assert row is not None
    return row
