"""Custom pages: CRUD for admin-defined pages with flexible content types."""
from __future__ import annotations

from datetime import datetime, timezone

from ._connection import get_db_context, retry_on_busy


CONTENT_TYPES = {
    # Static / markup ---
    "redirect": "HTTP Redirect",
    "html": "HTML",
    "html_styled": "HTML + CSS",
    "html_full": "HTML + CSS + JS",
    "markdown": "Markdown",
    "wiki_page": "Wiki Page (Markdown)",
    "plain_text": "Plain Text",
    # Data formats ---
    "json_content": "JSON",
    "xml_content": "XML",
    # Media ---
    "image": "Image (Direct)",
    "image_page": "Image Page",
    "youtube_video": "YouTube Video",
    "video_hosted": "Hosted Video",
    # Files ---
    "file_download": "File Download",
    "file_listing": "File Listing",
    # Embedding ---
    "iframe_embed": "Iframe Embed",
    "page_embed": "Page Embed",
    # Misc ---
    "link_list": "Link List",
    "code_snippet": "Code Snippet",
}

# Content types that group roughly into a category (for UI presentation)
CONTENT_TYPE_GROUPS = {
    "markup": ["redirect", "html", "html_styled", "html_full", "markdown",
               "wiki_page", "plain_text"],
    "data": ["json_content", "xml_content"],
    "media": ["image", "image_page", "youtube_video", "video_hosted"],
    "files": ["file_download", "file_listing"],
    "embed": ["iframe_embed", "page_embed"],
    "misc": ["link_list", "code_snippet"],
}

# Allowed video MIME types for hosted videos
VIDEO_MIME_TYPES = {
    "video/mp4", "video/webm", "video/ogg",
    "video/quicktime", "video/x-msvideo", "video/x-matroska",
}

# Valid YouTube hostname variants
_YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "youtu.be",
                  "m.youtube.com", "music.youtube.com"}


def extract_youtube_video_id(url_or_id: str) -> str | None:
    """Extract a YouTube video ID from a URL or return the raw ID if it looks
    like a standalone 11-character video ID.

    Supported URL formats:
    - https://www.youtube.com/watch?v=VIDEO_ID
    - https://youtu.be/VIDEO_ID
    - https://www.youtube.com/embed/VIDEO_ID
    - https://www.youtube.com/shorts/VIDEO_ID
    - A bare 11-character alphanumeric ID

    Returns the video ID string or ``None`` if it cannot be parsed.
    """
    from urllib.parse import urlparse, parse_qs

    raw = (url_or_id or "").strip()
    if not raw:
        return None

    # Bare video ID: 11 characters, alphanumeric + - and _
    import re
    if re.match(r'^[A-Za-z0-9_-]{11}$', raw):
        return raw

    try:
        parsed = urlparse(raw)
    except Exception:
        return None

    # youtu.be/<id>
    if parsed.netloc.lower() in ("youtu.be",):
        vid = parsed.path.lstrip("/").split("/")[0]
        return vid if re.match(r'^[A-Za-z0-9_-]{11}$', vid) else None

    if parsed.netloc.lower() not in _YOUTUBE_HOSTS:
        return None

    # /watch?v=
    qs = parse_qs(parsed.query)
    if "v" in qs:
        vid = qs["v"][0]
        return vid if re.match(r'^[A-Za-z0-9_-]{11}$', vid) else None

    # /embed/<id> or /shorts/<id> or /v/<id>
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) >= 2 and parts[0] in ("embed", "shorts", "v", "e"):
        vid = parts[1]
        return vid if re.match(r'^[A-Za-z0-9_-]{11}$', vid) else None

    return None


def create_custom_page(path, title, content_type, created_by, **kwargs):
    """Create a new custom page.

    Accepted keyword arguments: ``content``, ``css``, ``js``,
    ``redirect_url``, ``redirect_code``, ``links_json``,
    ``code_language``, ``iframe_url``, ``iframe_height``,
    ``iframe_sandbox``, ``iframe_allow``, ``video_url``,
    ``video_autoplay``, ``video_controls``, ``video_loop``,
    ``video_muted``, ``meta_description``, ``is_published``.

    Returns the new page id.
    """
    with get_db_context() as conn:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        cur = conn.execute(
            "INSERT INTO custom_pages "
            "(path, title, content_type, created_by, "
            " content, css, js, redirect_url, redirect_code, "
            " links_json, code_language, iframe_url, iframe_height, "
            " iframe_sandbox, iframe_allow, video_url, video_autoplay, "
            " video_controls, video_loop, video_muted, "
            " meta_description, is_published, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                path,
                title,
                content_type,
                created_by,
                kwargs.get("content", ""),
                kwargs.get("css", ""),
                kwargs.get("js", ""),
                kwargs.get("redirect_url", ""),
                kwargs.get("redirect_code", 302),
                kwargs.get("links_json", "[]"),
                kwargs.get("code_language", "text"),
                kwargs.get("iframe_url", ""),
                kwargs.get("iframe_height", "600px"),
                kwargs.get("iframe_sandbox", ""),
                kwargs.get("iframe_allow", ""),
                kwargs.get("video_url", ""),
                1 if kwargs.get("video_autoplay") else 0,
                1 if kwargs.get("video_controls", True) else 0,
                1 if kwargs.get("video_loop") else 0,
                1 if kwargs.get("video_muted") else 0,
                kwargs.get("meta_description", ""),
                kwargs.get("is_published", 1),
                now,
                now,
            ),
        )
        conn.commit()
        return cur.lastrowid


def update_custom_page(page_id, **kwargs):
    """Update an existing custom page.

    Accepted keyword arguments: ``path``, ``title``, ``content_type``,
    ``content``, ``css``, ``js``, ``redirect_url``, ``redirect_code``,
    ``links_json``, ``code_language``, ``iframe_url``, ``iframe_height``,
    ``iframe_sandbox``, ``iframe_allow``, ``video_url``, ``video_autoplay``,
    ``video_controls``, ``video_loop``, ``video_muted``,
    ``meta_description``, ``is_published``.

    ``updated_at`` is always set to the current UTC time.
    """
    _ALLOWED = {
        "path", "title", "content_type",
        "content", "css", "js",
        "redirect_url", "redirect_code",
        "links_json", "code_language",
        "iframe_url", "iframe_height", "iframe_sandbox", "iframe_allow",
        "video_url", "video_autoplay", "video_controls", "video_loop", "video_muted",
        "meta_description", "is_published",
    }
    updates = {k: v for k, v in kwargs.items() if k in _ALLOWED}
    if not updates:
        return
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    updates["updated_at"] = now
    set_clause = ", ".join(f"{col} = ?" for col in updates)
    values = list(updates.values()) + [page_id]
    with get_db_context() as conn:
        conn.execute(
            f"UPDATE custom_pages SET {set_clause} WHERE id = ?",  # noqa: S608
            values,
        )
        conn.commit()


def delete_custom_page(page_id):
    """Delete a custom page and its associated file records.

    Returns ``True`` if the page was deleted, ``False`` otherwise.
    """
    with get_db_context() as conn:
        conn.execute(
            "DELETE FROM custom_page_files WHERE custom_page_id = ?",
            (page_id,),
        )
        cur = conn.execute(
            "DELETE FROM custom_pages WHERE id = ?", (page_id,)
        )
        conn.commit()
        return cur.rowcount > 0


@retry_on_busy
def get_custom_page(page_id):
    """Return a custom page row by ID, or ``None``."""
    with get_db_context() as conn:
        return conn.execute(
            "SELECT * FROM custom_pages WHERE id = ?", (page_id,)
        ).fetchone()


@retry_on_busy
def get_custom_page_by_path(path):
    """Return a custom page row by its URL path, or ``None``."""
    with get_db_context() as conn:
        return conn.execute(
            "SELECT * FROM custom_pages WHERE path = ?", (path,)
        ).fetchone()


@retry_on_busy
def list_custom_pages():
    """Return all custom pages ordered by path."""
    with get_db_context() as conn:
        return conn.execute(
            "SELECT * FROM custom_pages ORDER BY path"
        ).fetchall()


@retry_on_busy
def count_custom_pages():
    """Return the total number of custom pages."""
    with get_db_context() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM custom_pages"
        ).fetchone()
        return row["cnt"] if row else 0


def add_custom_page_file(custom_page_id, filename, original_name,
                         mime_type, file_size, blob_id=None):
    """Add a file record associated with a custom page.

    Returns the new file id.
    """
    with get_db_context() as conn:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        cur = conn.execute(
            "INSERT INTO custom_page_files "
            "(custom_page_id, filename, original_name, mime_type, "
            " file_size, uploaded_at, blob_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (custom_page_id, filename, original_name, mime_type,
             file_size, now, blob_id),
        )
        conn.commit()
        return cur.lastrowid


@retry_on_busy
def get_custom_page_file(file_id):
    """Return a file record by ID, or ``None``."""
    with get_db_context() as conn:
        return conn.execute(
            "SELECT * FROM custom_page_files WHERE id = ?", (file_id,)
        ).fetchone()


@retry_on_busy
def list_custom_page_files(custom_page_id):
    """Return all file records for a custom page ordered by upload time."""
    with get_db_context() as conn:
        return conn.execute(
            "SELECT * FROM custom_page_files "
            "WHERE custom_page_id = ? ORDER BY uploaded_at",
            (custom_page_id,),
        ).fetchall()


def delete_custom_page_file(file_id):
    """Delete a file record and its associated blob.

    Returns ``True`` if the record was deleted, ``False`` otherwise.
    """
    with get_db_context() as conn:
        row = conn.execute("SELECT blob_id FROM custom_page_files WHERE id = ?", (file_id,)).fetchone()
        blob_id = row["blob_id"] if row else None
        cur = conn.execute(
            "DELETE FROM custom_page_files WHERE id = ?", (file_id,)
        )
        if blob_id:
            conn.execute("DELETE FROM file_blobs WHERE id=?", (blob_id,))
        conn.commit()
        return cur.rowcount > 0
