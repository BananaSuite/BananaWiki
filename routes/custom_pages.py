"""
BananaWiki: Custom pages management and serving routes.

Admin routes for creating, editing, and deleting custom pages.
Public custom pages are served via the 404 error handler (see routes/errors.py).

Published custom pages and their files are public on purpose: they are
served to anonymous visitors even when public mode is off, so an admin can
publish a landing page, an imprint or a download for people without an
account.  Only admins and owners can manage them.
"""

import io
import json
import mimetypes
import os
import re
import secrets
import uuid
from urllib.parse import urlparse

from flask import (
    Response,
    abort,
    flash,
    g,
    make_response,
    redirect,
    render_template,
    request,
    send_file,
    send_from_directory,
    url_for,
)
from werkzeug.utils import secure_filename

import config
import db
from helpers import (
    allowed_attachment,
    get_current_user,
    login_required,
    rate_limit,
    render_markdown,
    is_joke_audio_extension, get_real_audio_ext,
    get_joke_fail_message, convert_joke_audio,
    t,
)
from sync import notify_change, notify_file_upload, notify_file_deleted
from wiki_logger import log_action


def _custom_page_files_dir():
    """Return the directory for custom page file uploads, creating it if needed."""
    d = config.CUSTOM_PAGE_FILES_FOLDER
    os.makedirs(d, exist_ok=True)
    return d


def _extract_https_origin(url):
    """Return the ``https://host[:port]`` origin of *url* or ``None``.

    Used to populate the ``X-Frame-Src-Override`` response header for custom
    pages that embed an external iframe.  Only ``https://`` URLs with a host
    are accepted; everything else returns ``None`` so the security-headers
    handler will fall back to the default safe ``frame-src`` set.

    Note: ``app.set_security_headers`` re-validates the value with stricter
    rules, so this is purely a convenience extractor: a bogus URL slipping
    through here is dropped at the response layer.
    """
    if not url:
        return None
    try:
        parsed = urlparse(url.strip())
    except (TypeError, ValueError):
        return None
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        return None
    host = parsed.hostname
    if parsed.port:
        return f"https://{host}:{parsed.port}"
    return f"https://{host}"


def _normalise_path(raw):
    """Normalise a custom page path.

    Ensures the path starts with ``/``, strips trailing slashes (except for
    the root), collapses multiple slashes, and strips whitespace.
    """
    path = raw.strip().strip("/").strip()
    # Collapse multiple slashes and rebuild
    parts = [p for p in path.split("/") if p]
    return "/" + "/".join(parts) if parts else "/"


# Paths that are reserved and cannot be used as custom pages
_RESERVED_PREFIXES = (
    "/admin", "/login", "/logout", "/signup", "/setup", "/maintenance",
    "/lockdown",
    "/session-conflict", "/static", "/api", "/wiki", "/chats", "/groups",
    "/kanban", "/badges", "/users", "/page", "/settings",
    "/global-settings", "/reservations", "/announcements", "/_cpf",
    "/canvas",
)


def _is_path_reserved(path):
    """Return True if the given path conflicts with built-in routes."""
    lower = path.lower()
    if lower == "/":
        return True
    for prefix in _RESERVED_PREFIXES:
        if lower == prefix or lower.startswith(prefix + "/"):
            return True
    return False


def _can_manage_custom_pages(user):
    """Return True if *user* may create, edit or delete custom pages.

    A custom page is served at any unreserved path of the wiki origin, can
    send visitors to another site and publishes uploaded files to anonymous
    visitors, so this is an admin task.  The permission catalog already
    refuses to give ``custom_page.manage`` to editors and users; the role
    check here keeps the routes closed even if that ever changes.
    ``has_permission`` also answers False while the plugin is disabled.
    """
    if not user or user["role"] not in ("admin", "owner"):
        return False
    return db.has_permission(user, "custom_page.manage")


# Characters a browser drops or ignores while parsing a URL (C0 controls and
# space).  They are removed before the scheme check so "java\tscript:" cannot
# slip past it.
_URL_IGNORED_CHARS_RE = re.compile(r"[\x00-\x20\x7f]")


def _url_scheme(url):
    """Return the lower-case scheme of *url* as a browser would read it.

    Returns ``""`` for a relative URL and ``None`` for an empty or
    unparseable value.
    """
    cleaned = _URL_IGNORED_CHARS_RE.sub("", url if isinstance(url, str) else "")
    if not cleaned:
        return None
    try:
        return urlparse(cleaned).scheme.lower()
    except ValueError:
        return None


def _is_allowed_redirect_target(url):
    """Return True if *url* is acceptable as a redirect page target.

    Relative targets (a path on this wiki) and absolute ``http``/``https``
    URLs are accepted.  Any other scheme (``javascript:``, ``data:``,
    ``file:``...) is refused.
    """
    return _url_scheme(url) in ("", "http", "https")


def _is_allowed_link_url(url):
    """Return True if *url* may be used as a link on a link list page.

    Same rule as redirects, plus ``mailto:`` for contact links.
    """
    return _url_scheme(url) in ("", "http", "https", "mailto")


def _redirect_target_rejected(content_type, url):
    """Return True if a submitted redirect page has a target we refuse.

    An empty target is allowed so a redirect page can be saved as a draft;
    it is simply not served until a target is filled in.
    """
    return content_type == "redirect" and bool(url) and not _is_allowed_redirect_target(url)


def _redirect_target_error():
    """Return the flash message shown when a redirect target is refused."""
    return t(
        "flash.custom_page_redirect_url_invalid",
        default="The redirect URL must be a path on this wiki or an http(s) address.",
    )


def _get_max_video_size():
    """Return the configured max video size in bytes.

    Reads the site setting ``custom_pages_max_video_size_mb`` when available,
    falling back to ``config.CUSTOM_PAGE_MAX_VIDEO_SIZE``.
    """
    try:
        settings = db.get_site_settings()
        if settings and settings.get("custom_pages_max_video_size_mb"):
            return int(settings["custom_pages_max_video_size_mb"]) * 1024 * 1024
    except Exception:
        pass
    return config.CUSTOM_PAGE_MAX_VIDEO_SIZE


def try_serve_custom_page(request_obj):
    """Try to serve a custom page for the current request path.

    Returns a Flask response if a matching published custom page exists,
    or ``None`` if no custom page matches.  Called from the 404 error
    handler so that custom pages do not interfere with normal routing.
    """
    # Only serve GET / HEAD requests as custom pages
    if request_obj.method not in ("GET", "HEAD"):
        return None

    path = request_obj.path
    if not path:
        return None

    page = db.get_custom_page_by_path(path)
    if not page:
        return None

    if not page["is_published"]:
        user = get_current_user()
        if not user or user["role"] not in ("admin", "owner"):
            return None

    return _render_custom_page(page)


def _render_custom_page(page):
    """Build and return a Flask response for the given custom page row."""
    ct = page["content_type"]
    files = db.list_custom_page_files(page["id"])

    # Redirect ---
    if ct == "redirect":
        target = (page["redirect_url"] or "").strip()
        # The save form refuses other schemes; this also covers rows stored
        # before that check existed.
        if not target or not _is_allowed_redirect_target(target):
            return None
        code = page["redirect_code"] if page["redirect_code"] in (301, 302) else 302
        return redirect(target, code=code)

    # Plain text ---
    if ct == "plain_text":
        return Response(page["content"], mimetype="text/plain")

    # JSON ---
    if ct == "json_content":
        return Response(page["content"], mimetype="application/json")

    # XML ---
    # Browsers render XML as a document and run script from an XHTML
    # namespace inside it, so it is sandboxed like the raw HTML types.
    if ct == "xml_content":
        return _sandbox_active_custom_page(
            Response(page["content"], mimetype="application/xml")
        )

    # Image (direct) ---
    if ct == "image":
        if not files:
            return None
        return _send_custom_page_file(files[0])

    # File download ---
    if ct == "file_download":
        if not files:
            return None
        return _send_custom_page_file(files[0], force_download=True)

    # HTML ---
    if ct == "html":
        return _sandbox_active_custom_page(
            Response(page["content"], mimetype="text/html")
        )

    # HTML + CSS ---
    if ct == "html_styled":
        html = _wrap_html(page["title"], page["content"],
                          css=page["css"])
        return _sandbox_active_custom_page(Response(html, mimetype="text/html"))

    # HTML + CSS + JS ---
    if ct == "html_full":
        html = _wrap_html(page["title"], page["content"],
                          css=page["css"], js=page["js"])
        return _sandbox_active_custom_page(Response(html, mimetype="text/html"))

    # Markdown ---
    # The body is sanitised Markdown, but the page is a standalone document
    # with the author's own CSS, so it gets the same sandbox as the raw HTML
    # types rather than running with the wiki origin.
    if ct == "markdown":
        rendered = render_markdown(page["content"])
        html = _wrap_html(page["title"], rendered, css=page["css"])
        return _sandbox_active_custom_page(Response(html, mimetype="text/html"))

    # Wiki page ---
    if ct == "wiki_page":
        rendered = render_markdown(page["content"])
        return make_response(render_template(
            "custom_page/wiki.html",
            page=page,
            rendered_content=rendered,
        ))

    # Image page ---
    if ct == "image_page":
        return make_response(render_template(
            "custom_page/image_page.html",
            page=page,
            files=files,
        ))

    # YouTube video ---
    if ct == "youtube_video":
        video_id = db.extract_youtube_video_id(page["video_url"])
        embed_url = None
        if video_id:
            embed_url = _build_youtube_embed_url(video_id, page)
        return make_response(render_template(
            "custom_page/youtube.html",
            page=page,
            video_id=video_id,
            embed_url=embed_url,
        ))

    # Hosted video ---
    if ct == "video_hosted":
        # Find the primary video file
        video_file = None
        for f in files:
            if (f["mime_type"] or "").startswith("video/"):
                video_file = f
                break
        return make_response(render_template(
            "custom_page/video.html",
            page=page,
            video_file=video_file,
        ))

    # File listing ---
    if ct == "file_listing":
        return make_response(render_template(
            "custom_page/file_listing.html",
            page=page,
            files=files,
        ))

    # Iframe embed ---
    if ct == "iframe_embed":
        resp = make_response(render_template(
            "custom_page/iframe.html",
            page=page,
        ))
        origin = _extract_https_origin(page["iframe_url"] if "iframe_url" in page.keys() else None)
        if origin:
            resp.headers["X-Frame-Src-Override"] = origin
        return resp

    # Page embed (general page embedding) ---
    if ct == "page_embed":
        resp = make_response(render_template(
            "custom_page/page_embed.html",
            page=page,
        ))
        embed_url = page["embed_url"] if "embed_url" in page.keys() else (
            page["iframe_url"] if "iframe_url" in page.keys() else None
        )
        origin = _extract_https_origin(embed_url)
        if origin:
            resp.headers["X-Frame-Src-Override"] = origin
        return resp

    # Link list ---
    if ct == "link_list":
        try:
            links = json.loads(page["links_json"])
        except (json.JSONDecodeError, TypeError):
            links = []
        if not isinstance(links, list):
            links = []
        # A link whose URL names another scheme (javascript:, data:...) is
        # left out rather than rendered as a clickable href.
        links = [
            link for link in links
            if isinstance(link, dict) and _is_allowed_link_url(link.get("url"))
        ]
        return make_response(render_template(
            "custom_page/link_list.html",
            page=page,
            links=links,
        ))

    # Code snippet ---
    if ct == "code_snippet":
        return make_response(render_template(
            "custom_page/code.html",
            page=page,
        ))

    return None


def register_custom_pages_routes(app):
    """Register custom pages admin and serving routes on *app*."""

    @app.route("/admin/custom-pages")
    @login_required
    def admin_custom_pages():
        """Admin page listing all custom pages."""
        user = get_current_user()
        if not _can_manage_custom_pages(user):
            abort(403)
        pages = db.list_custom_pages()
        return render_template(
            "admin/custom_pages.html",
            pages=pages,
            content_types=db.CUSTOM_PAGE_CONTENT_TYPES,
            content_type_groups=db.CUSTOM_PAGE_CONTENT_TYPE_GROUPS,
        )

    @app.route("/admin/custom-pages/create", methods=["GET", "POST"])
    @login_required
    @rate_limit(10, 60)
    def admin_custom_page_create():
        """Create a new custom page."""
        user = get_current_user()
        if not _can_manage_custom_pages(user):
            abort(403)
        if request.method == "POST":
            path = _normalise_path(request.form.get("path", ""))
            title = request.form.get("title", "").strip()
            content_type = request.form.get("content_type", "html").strip()

            if not path or path == "/":
                flash(t("flash.a_valid_path_is_required"), "error")
                return redirect(url_for("admin_custom_page_create"))

            if _is_path_reserved(path):
                flash(t("flash.that_path_is_reserved_by_the_system"), "error")
                return redirect(url_for("admin_custom_page_create"))

            if content_type not in db.CUSTOM_PAGE_CONTENT_TYPES:
                flash(t("flash.invalid_content_type"), "error")
                return redirect(url_for("admin_custom_page_create"))

            existing = db.get_custom_page_by_path(path)
            if existing:
                flash(t("flash.a_custom_page_with_that_path_already_exists"), "error")
                return redirect(url_for("admin_custom_page_create"))

            user = get_current_user()
            kwargs = _extract_page_fields(request.form)
            if _redirect_target_rejected(content_type, kwargs["redirect_url"]):
                flash(_redirect_target_error(), "error")
                return redirect(url_for("admin_custom_page_create"))

            try:
                page_id = db.create_custom_page(
                    path, title, content_type, user["id"], **kwargs,
                )
            except db.IntegrityError:
                flash(t("flash.a_custom_page_with_that_path_already_exists"), "error")
                return redirect(url_for("admin_custom_page_create"))

            # Handle file upload for types that need it
            upload_error = _handle_file_upload(page_id, content_type)
            if upload_error:
                flash(upload_error, "error")

            log_action("custom_page_create", request, user=user,
                       path=path, content_type=content_type)
            notify_change("custom_page_create", "NORMAL")
            flash(t("flash.custom_page_created_successfully"), "success")
            return redirect(url_for("admin_custom_pages"))

        return render_template(
            "admin/custom_page_edit.html",
            page=None,
            content_types=db.CUSTOM_PAGE_CONTENT_TYPES,
            content_type_groups=db.CUSTOM_PAGE_CONTENT_TYPE_GROUPS,
            mode="create",
            max_video_size_mb=_get_max_video_size() // (1024 * 1024),
        )

    @app.route("/admin/custom-pages/<int:page_id>/edit", methods=["GET", "POST"])
    @login_required
    @rate_limit(10, 60)
    def admin_custom_page_edit(page_id):
        """Edit an existing custom page."""
        user = get_current_user()
        if not _can_manage_custom_pages(user):
            abort(403)
        page = db.get_custom_page(page_id)
        if not page:
            abort(404)

        if request.method == "POST":
            path = _normalise_path(request.form.get("path", ""))
            title = request.form.get("title", "").strip()
            content_type = request.form.get("content_type", "html").strip()

            if not path or path == "/":
                flash(t("flash.a_valid_path_is_required"), "error")
                return redirect(url_for("admin_custom_page_edit", page_id=page_id))

            if _is_path_reserved(path):
                flash(t("flash.that_path_is_reserved_by_the_system"), "error")
                return redirect(url_for("admin_custom_page_edit", page_id=page_id))

            if content_type not in db.CUSTOM_PAGE_CONTENT_TYPES:
                flash(t("flash.invalid_content_type"), "error")
                return redirect(url_for("admin_custom_page_edit", page_id=page_id))

            # Check for path conflict with other pages
            existing = db.get_custom_page_by_path(path)
            if existing and existing["id"] != page_id:
                flash(t("flash.another_custom_page_with_that_path_already_exists"), "error")
                return redirect(url_for("admin_custom_page_edit", page_id=page_id))

            kwargs = _extract_page_fields(request.form)
            if _redirect_target_rejected(content_type, kwargs["redirect_url"]):
                flash(_redirect_target_error(), "error")
                return redirect(url_for("admin_custom_page_edit", page_id=page_id))
            kwargs["path"] = path
            kwargs["title"] = title
            kwargs["content_type"] = content_type

            db.update_custom_page(page_id, **kwargs)

            # Handle file upload
            upload_error = _handle_file_upload(page_id, content_type)
            if upload_error:
                flash(upload_error, "error")

            user = get_current_user()
            log_action("custom_page_update", request, user=user,
                       path=path, content_type=content_type)
            notify_change("custom_page_update", "NORMAL")
            flash(t("flash.custom_page_updated_successfully"), "success")
            return redirect(url_for("admin_custom_pages"))

        files = db.list_custom_page_files(page_id)
        return render_template(
            "admin/custom_page_edit.html",
            page=page,
            files=files,
            content_types=db.CUSTOM_PAGE_CONTENT_TYPES,
            content_type_groups=db.CUSTOM_PAGE_CONTENT_TYPE_GROUPS,
            mode="edit",
            max_video_size_mb=_get_max_video_size() // (1024 * 1024),
        )

    @app.route("/admin/custom-pages/<int:page_id>/delete", methods=["POST"])
    @login_required
    @rate_limit(10, 60)
    def admin_custom_page_delete(page_id):
        """Delete a custom page and its files."""
        user = get_current_user()
        if not _can_manage_custom_pages(user):
            abort(403)
        page = db.get_custom_page(page_id)
        if not page:
            abort(404)

        # Delete physical files
        files = db.list_custom_page_files(page_id)
        for f in files:
            filepath = os.path.join(_custom_page_files_dir(), f["filename"])
            if os.path.isfile(filepath):
                os.remove(filepath)
            notify_file_deleted(f["filename"])

        db.delete_custom_page(page_id)

        user = get_current_user()
        log_action("custom_page_delete", request, user=user,
                   path=page["path"])
        notify_change("custom_page_delete", "NORMAL")
        flash(t("flash.custom_page_deleted_successfully"), "success")
        return redirect(url_for("admin_custom_pages"))

    @app.route("/admin/custom-pages/files/<int:file_id>/delete", methods=["POST"])
    @login_required
    @rate_limit(10, 60)
    def admin_custom_page_file_delete(file_id):
        """Delete a file attached to a custom page."""
        user = get_current_user()
        if not _can_manage_custom_pages(user):
            abort(403)
        file_row = db.get_custom_page_file(file_id)
        if not file_row:
            abort(404)

        filepath = os.path.join(_custom_page_files_dir(), file_row["filename"])
        if os.path.isfile(filepath):
            os.remove(filepath)

        page_id = file_row["custom_page_id"]
        db.delete_custom_page_file(file_id)
        notify_file_deleted(file_row["filename"])
        log_action("custom_page_file_delete", request, user=user,
                   page_id=page_id, filename=file_row["original_name"])

        flash(t("flash.file_deleted"), "success")
        return redirect(url_for("admin_custom_page_edit", page_id=page_id))

    @app.route("/_cpf/<int:file_id>/<path:filename>")
    def custom_page_file_download(file_id, filename):
        """Serve a custom page file for download / display.

        Files of published pages are public, like the pages themselves.
        See ``_send_custom_page_file`` for which files are shown inline.
        """
        file_row = db.get_custom_page_file(file_id)
        if not file_row:
            abort(404)

        # Verify the page is published
        page = db.get_custom_page(file_row["custom_page_id"])
        if not page or not page["is_published"]:
            # Allow admins to access unpublished page files
            user = get_current_user()
            if not user or user["role"] not in ("admin", "owner"):
                abort(404)

        response = _send_custom_page_file(file_row)
        if response is None:
            abort(404)
        return response


def _extract_page_fields(form):
    """Extract custom page fields from the submitted form data."""
    fields = {}
    fields["content"] = form.get("content", "")
    fields["css"] = form.get("css", "")
    fields["js"] = form.get("js", "")
    fields["redirect_url"] = form.get("redirect_url", "").strip()
    fields["iframe_url"] = form.get("iframe_url", "").strip()
    fields["iframe_height"] = form.get("iframe_height", "600px").strip() or "600px"
    fields["iframe_sandbox"] = form.get("iframe_sandbox", "").strip()
    fields["iframe_allow"] = form.get("iframe_allow", "").strip()
    fields["video_url"] = form.get("video_url", "").strip()
    fields["video_autoplay"] = 1 if form.get("video_autoplay") else 0
    fields["video_controls"] = 1 if form.get("video_controls") else 0
    fields["video_loop"] = 1 if form.get("video_loop") else 0
    fields["video_muted"] = 1 if form.get("video_muted") else 0
    fields["code_language"] = form.get("code_language", "text").strip()
    fields["meta_description"] = form.get("meta_description", "").strip()
    fields["is_published"] = 1 if form.get("is_published") else 0

    # Redirect code
    try:
        rc = int(form.get("redirect_code", 302))
        fields["redirect_code"] = rc if rc in (301, 302) else 302
    except (ValueError, TypeError):
        fields["redirect_code"] = 302

    # Links JSON
    raw_links = form.get("links_json", "[]").strip()
    try:
        json.loads(raw_links)
        fields["links_json"] = raw_links
    except (json.JSONDecodeError, TypeError):
        fields["links_json"] = "[]"

    return fields


# Types a browser can only show as a picture, a video, a sound or plain text.
# Custom page files with one of these types are served inline so image,
# video and file listing pages can embed them.  Every other file is sent as
# ``application/octet-stream`` with an attachment disposition.  That is an
# allowlist on purpose: the wiki's CSP allows ``script-src 'self'``, so a
# .js or .css file served with its real type would be usable as a script or
# stylesheet by any markup on the wiki origin, and some XML types render as
# active documents.  With the global ``nosniff`` header a browser refuses to
# run or apply an octet-stream response, whatever the file contains.
_INLINE_SAFE_MIME_TYPES = frozenset({
    "image/png", "image/jpeg", "image/gif", "image/webp", "image/avif",
    "image/bmp",
    "video/mp4", "video/webm", "video/ogg", "video/quicktime",
    "video/x-msvideo", "video/x-matroska", "video/matroska",
    "audio/mpeg", "audio/ogg", "audio/wav", "audio/x-wav", "audio/webm",
    "audio/flac", "audio/aac", "audio/mp4",
    "text/plain",
})


def _serving_mime_type(mime):
    """Return ``(mimetype, inline)`` for serving a stored custom page file.

    *inline* is False for anything outside ``_INLINE_SAFE_MIME_TYPES``;
    those files are sent as ``application/octet-stream`` attachments.
    """
    normalised = (mime or "").split(";")[0].strip().lower()
    if normalised in _INLINE_SAFE_MIME_TYPES:
        return normalised, True
    return "application/octet-stream", False


def _send_custom_page_file(file_row, force_download=False):
    """Send a stored custom page file, or return ``None`` if it is missing.

    The DB blob copy is preferred and the disk copy is the fallback.  The
    response also carries the custom page sandbox marker, so a file opened
    directly in a tab gets a unique origin instead of the wiki's.
    """
    mimetype, inline = _serving_mime_type(file_row["mime_type"])
    as_attachment = force_download or not inline
    download_name = file_row["original_name"]

    blob_id = file_row["blob_id"] if "blob_id" in file_row.keys() else None
    content = db.get_blob_content(blob_id) if blob_id else None
    if content:
        response = send_file(
            io.BytesIO(content),
            download_name=download_name,
            as_attachment=as_attachment,
            mimetype=mimetype,
        )
    else:
        directory = _custom_page_files_dir()
        stored_name = file_row["filename"]
        if not os.path.isfile(os.path.join(directory, stored_name)):
            return None
        response = send_from_directory(
            directory, stored_name,
            download_name=download_name,
            as_attachment=as_attachment,
            mimetype=mimetype,
        )
    return _sandbox_active_custom_page(response)


def _handle_file_upload(page_id, content_type=None):
    """Handle file upload for a custom page.

    Returns an error message string on failure, or ``None`` on success /
    when no file was provided.
    """
    f = request.files.get("file")
    if not f or not f.filename:
        return None

    original_name = secure_filename(f.filename)
    if not original_name:
        return None

    # Uploads follow the same extension rules as page attachments: the
    # always-blocked list (HTML, SVG, executables...), the platform blacklist
    # and the upload mode chosen in the admin settings.
    if not allowed_attachment(original_name, settings=db.get_site_settings()):
        return t("flash.file_type_not_allowed")

    mime = mimetypes.guess_type(original_name)[0] or "application/octet-stream"
    ext = ""
    if "." in original_name:
        ext = "." + original_name.rsplit(".", 1)[1].lower()

    # Joke audio: detect mp5/mp7 and use real extension for storage
    ext_without_dot = ext.lstrip(".")
    is_joke = is_joke_audio_extension(ext_without_dot)
    if is_joke:
        real_ext = get_real_audio_ext(ext_without_dot)
        ext = "." + real_ext
        mime = "audio/mpeg"

    stored_name = uuid.uuid4().hex + ext
    directory = _custom_page_files_dir()
    dest = os.path.join(directory, stored_name)

    # Validate destination path stays within the upload directory
    if os.path.commonpath([os.path.abspath(directory),
                           os.path.abspath(dest)]) != os.path.abspath(directory):
        return "Invalid file path."

    # Determine size limit before writing so oversized files never hit disk
    is_video = mime.startswith("video/") or content_type == "video_hosted"
    if is_video:
        max_size = _get_max_video_size()
    else:
        max_size = config.CUSTOM_PAGE_MAX_FILE_SIZE
    size_label = f"{max_size // (1024 * 1024)} MB"

    # Fast-reject when the Content-Length header already exceeds the cap
    if request.content_length and request.content_length > max_size:
        return f"File too large. Maximum allowed size is {size_label}."

    # Stream-write with a byte cap to prevent disk-fill attacks.
    # The size is checked *before* each chunk is written so at most
    # max_size bytes ever reach the destination file.
    _CHUNK = 8192
    bytes_written = 0
    exceeded = False
    _blob_buf = io.BytesIO()  # accumulate content for DB blob (dual-write)
    try:
        with open(dest, "wb") as out:
            while True:
                chunk = f.stream.read(_CHUNK)
                if not chunk:
                    break
                if bytes_written + len(chunk) > max_size:
                    exceeded = True
                    break
                out.write(chunk)
                _blob_buf.write(chunk)
                bytes_written += len(chunk)
    except OSError:
        if os.path.exists(dest):
            os.remove(dest)
        return "Failed to save file."

    if exceeded:
        try:
            os.remove(dest)
        except OSError:
            pass
        return f"File too large. Maximum allowed size is {size_label}."

    # Joke audio: convert mp5/mp7 → mp3 via ffmpeg
    if is_joke:
        ok, err = convert_joke_audio(dest, dest)
        if not ok:
            try:
                os.remove(dest)
            except OSError:
                pass
            return f"{get_joke_fail_message(ext_without_dot)} {err or ''}"

    # Dual-write: store buffered content in DB blob for DB-first reads.
    # For joke audio, re-read the converted file so the blob matches disk.
    _blob_id = None
    try:
        blob_data = _blob_buf.getvalue()
        if is_joke:
            try:
                with open(dest, "rb") as cf:
                    blob_data = cf.read()
            except OSError:
                pass
        _blob_id = db.store_blob(stored_name, blob_data, mime or "application/octet-stream")
    except Exception:
        pass  # blob storage is non-critical; disk remains source of truth
    db.add_custom_page_file(page_id, stored_name, original_name, mime,
                            bytes_written, blob_id=_blob_id)
    notify_file_upload(stored_name, dest, display_name=original_name)
    return None


def _escape_style_text(css):
    """Make *css* safe to place between ``<style>`` tags.

    The HTML parser ends a style element at the first ``</style``, whatever
    the CSS around it, so the CSS field could otherwise close the element
    and add markup to the page.  ``<`` has no meaning in CSS outside strings
    and comments, and ``\\3c `` is the CSS escape for the same character,
    so rewriting it keeps every legitimate stylesheet working.
    """
    return css.replace("<", "\\3c ")


def _wrap_html(title, body, css="", js=""):
    """Build a minimal standalone HTML document."""
    nonce = getattr(g, "_csp_nonce", None)
    if nonce is None:
        nonce = secrets.token_hex(16)
        g._csp_nonce = nonce
    style_block = f'<style nonce="{nonce}">{_escape_style_text(css)}</style>' if css else ""
    script_block = f'<script nonce="{nonce}">{js}</script>' if js else ""
    return (
        "<!DOCTYPE html>\n"
        "<html>\n<head>\n"
        f"<meta charset=\"utf-8\">\n"
        f"<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">\n"
        f"<title>{_escape_html(title)}</title>\n"
        f"{style_block}\n"
        "</head>\n<body>\n"
        f"{body}\n"
        f"{script_block}\n"
        "</body>\n</html>"
    )


def _sandbox_active_custom_page(response):
    """Mark a custom page response for a unique-origin browser sandbox.

    Used for every standalone document whose markup or styles come from the
    page author (the HTML types, Markdown, XML) and for uploaded files.
    Admins may intentionally write JavaScript in the HTML types.  The sandbox
    keeps that code from inheriting the wiki origin, reading authenticated
    pages, or accessing cookies while still allowing the explicitly authored
    script to run.  The application security-header hook consumes this
    internal marker.
    """
    response.headers["X-Custom-Page-Sandbox"] = "1"
    return response


def _escape_html(text):
    """Minimal HTML escaping for use in title tags."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )

def _build_youtube_embed_url(video_id, page):
    """Build the YouTube nocookie embed URL for a custom page.

    Applies autoplay, controls, and loop parameters from page settings.
    YouTube requires muted=1 when autoplay=1, so muted is auto-applied.
    """
    from urllib.parse import urlencode

    params = {}
    if page["video_autoplay"]:
        params["autoplay"] = "1"
        params["mute"] = "1"       # required for autoplay
    if not page["video_controls"]:
        params["controls"] = "0"
    if page["video_loop"]:
        params["loop"] = "1"
        params["playlist"] = video_id  # required for loop to work

    base = f"https://www.youtube-nocookie.com/embed/{video_id}"
    if params:
        return base + "?" + urlencode(params)
    return base
