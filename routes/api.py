"""
BananaWiki: JSON API routes (search, preview, drafts, accessibility, reorder).
"""

import json
import os
import re
import uuid

from flask import request, jsonify, session, make_response, url_for, abort, render_template
from PIL import Image, ImageOps
import db
from db._canvas import sanitize_layout_data
import config
from helpers import (
    login_required, editor_required, admin_required, get_current_user,
    render_markdown, rate_limit, format_datetime, editor_has_category_access,
    user_can_view_category, user_can_view_page,
    normalize_language_selection, highlight_code_html, safe_unlink_in,
)
import wiki_logger
from sync import notify_change, notify_file_upload, notify_file_deleted
from routes.uploads import cleanup_unused_uploads
from routes.kanban import user_can_view_kanban_board


_BACKGROUND_ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}


def _background_upload_error(message, status=400):
    """Return a JSON error response for background image uploads."""
    return jsonify({"error": message}), status


def _background_image_url(filename):
    """Return the public static URL for a stored background image."""
    if not filename:
        return ""
    return url_for("static", filename=f"uploads/{filename}")


def _remove_background_file(filename):
    """Delete a stored background image and notify sync when removed."""
    if not filename:
        return
    if safe_unlink_in(config.UPLOAD_FOLDER, filename):
        notify_file_deleted(filename)


def _save_custom_background(file_storage):
    """Validate, resize, and persist a user background image as a capped JPEG."""
    filename = file_storage.filename or ""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in _BACKGROUND_ALLOWED_EXTENSIONS:
        return None, "Invalid image type."

    file_storage.stream.seek(0, os.SEEK_END)
    upload_size = file_storage.stream.tell()
    file_storage.stream.seek(0)
    max_upload_size = int(getattr(config, "BACKGROUND_IMAGE_MAX_UPLOAD_SIZE", 4 * 1024 * 1024))
    if upload_size > max_upload_size:
        return None, "Background images must be 4 MB or smaller."

    try:
        probe = Image.open(file_storage.stream)
        probe.verify()
        file_storage.stream.seek(0)
        img = Image.open(file_storage.stream)
        width, height = img.size
        max_pixels = int(getattr(config, "BACKGROUND_IMAGE_MAX_PIXELS", 16_000_000))
        if width < 1 or height < 1 or width * height > max_pixels:
            return None, "Background image dimensions are too large."

        max_dimension = int(getattr(config, "BACKGROUND_IMAGE_MAX_DIMENSION", 2560))
        img = ImageOps.exif_transpose(img)
        if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
            rgba = img.convert("RGBA")
            canvas = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
            canvas.alpha_composite(rgba)
            img = canvas.convert("RGB")
        else:
            img = img.convert("RGB")
        resampling = getattr(Image, "Resampling", Image)
        img.thumbnail((max_dimension, max_dimension), resampling.LANCZOS)
    except Exception:
        return None, "File is not a valid image."

    stored_name = f"backgrounds/{uuid.uuid4().hex}.jpg"
    upload_root = os.path.abspath(config.UPLOAD_FOLDER)
    save_path = os.path.abspath(os.path.join(config.UPLOAD_FOLDER, stored_name))
    if os.path.commonpath([upload_root, save_path]) != upload_root:
        return None, "Invalid upload path."
    os.makedirs(os.path.dirname(save_path), exist_ok=True)
    try:
        img.save(save_path, format="JPEG", quality=82, optimize=True, progressive=True)
    except OSError:
        return None, "Failed to save background image."
    return stored_name, None


def _get_public_accessibility_from_cookie():
    """Return sanitized anonymous customization preferences from the public cookie."""
    prefs = dict(db._A11Y_DEFAULTS)
    raw = request.cookies.get("bw_public_accessibility", "")
    if raw:
        try:
            saved = json.loads(raw)
            if isinstance(saved, dict):
                prefs.update({
                    k: db._clean_a11y_pref(k, v)
                    for k, v in saved.items()
                    if k in db._A11Y_DEFAULTS
                })
        except (TypeError, ValueError):
            pass
    if session.get("interface_language"):
        prefs["interface_language"] = session.get("interface_language", "default")
    return prefs


def register_api_routes(app):
    """Register JSON API routes on the Flask app."""

    def _get_editable_page_or_response(page_id, user):
        """Return ``(page, None)`` for allowed editors or an error response tuple.

        Draft-related editor APIs should enforce the same category write-access
        checks as the full edit page route so restricted editors cannot bypass
        category restrictions through background AJAX calls.
        """
        page = db.get_page(page_id)
        if not page:
            return None, (jsonify({"error": "page not found"}), 404)
        if not editor_has_category_access(user, page["category_id"]):
            return None, (
                jsonify({"error": "You do not have permission to edit pages in this category"}),
                403,
            )
        return page, None

    def _get_reorder_pages_error_response(page_ids, user):
        """Return an error response when *user* cannot reorder *page_ids*."""
        for page_id in page_ids:
            _, error = _get_editable_page_or_response(page_id, user)
            if error:
                return error
        return None

    def _get_reorder_categories_error_response(category_ids, user):
        """Return an error response when *user* cannot reorder *category_ids*."""
        if not db.has_permission(user, "category.reorder"):
            return jsonify({"error": "You do not have permission to reorder categories."}), 403
        for category_id in category_ids:
            category = db.get_category(category_id)
            if not category:
                return jsonify({"error": "Category not found"}), 404
            if not editor_has_category_access(user, category_id):
                return jsonify({"error": "You do not have permission to edit categories."}), 403
        return None

    @app.route("/api/pages/search")
    @login_required
    @rate_limit(60, 60)
    def api_pages_search():
        """Search wiki pages by title; returns JSON list of ``{title, slug}`` objects."""
        query = request.args.get("q", "").strip()
        if not query:
            return jsonify([])
        user = get_current_user()
        include_deindexed = bool(user and db.has_permission(user, "page.view_deindexed"))
        include_home = request.args.get("include_home") in {"1", "true", "yes"}
        results = db.search_pages(query, include_deindexed=include_deindexed)
        if include_home:
            home = db.get_home_page()
            if home and query.lower() in (home["title"] or "").lower():
                results = [home] + list(results)
        filtered = [r for r in results if user_can_view_page(user, r)]
        cat_ids = {row["category_id"] for row in filtered if row["category_id"] is not None}
        cat_names = {}
        for cid in cat_ids:
            cat = db.get_category(cid)
            if cat:
                cat_names[cid] = cat["name"]
        unique = []
        seen = set()
        for row in filtered:
            slug = row["slug"]
            if slug in seen:
                continue
            seen.add(slug)
            category_id = row["category_id"]
            unique.append({
                "title": row["title"],
                "slug": slug,
                "is_home": bool(row["is_home"]) if "is_home" in row.keys() else False,
                "category_id": category_id,
                "category_name": cat_names.get(category_id) if category_id is not None else None,
            })
        return jsonify(unique)

    @app.route("/api/pages/preview-by-slug")
    @login_required
    @rate_limit(60, 60)
    def api_page_preview_by_slug():
        """Return a safe HTML preview payload for a page slug."""
        slug = request.args.get("slug", "").strip()
        if not slug:
            return jsonify({"error": "slug is required"}), 400
        page = db.get_page_by_slug(slug)
        if not page:
            return jsonify({"error": "Page not found"}), 404
        user = get_current_user()
        if not user_can_view_page(user, page):
            return jsonify({"error": "Access denied"}), 403
        html = render_markdown(page["content"] or "", embed_videos=True)
        return jsonify({
            "title": page["title"],
            "slug": page["slug"],
            "is_home": bool(page["is_home"]) if "is_home" in page.keys() else False,
            "html": html,
        })

    def _navigation_batch_response(as_json):
        """Render one currently authorized batch as a fragment or a full page."""
        from helpers._navigation import navigation_page_batch
        try:
            category_id = int(request.args.get("category_id", "0"))
            after = int(request.args.get("after", "0"))
            if not (0 <= category_id < 2**63 and 0 <= after < 2**63):
                raise ValueError
        except ValueError:
            abort(400)
        category_id = category_id or None
        user = get_current_user()
        category = db.get_category(category_id) if category_id is not None else None
        if (category_id is not None and not category) or not user_can_view_category(user, category_id):
            abort(404)
        try:
            batch = navigation_page_batch(user, category_id, after)
        except ValueError as error:
            return jsonify({"error": str(error)}), 409
        context = {"navigation_batch": batch, "nav_category_id": category_id,
                   "navigation_category": category, "page": None,
                   "sidebar_reservations": db.get_active_page_reservations_map(
                       user["id"] if user else None, [item["id"] for item in batch["pages"]])}
        if as_json:
            response = jsonify({"html": render_template("wiki/_navigation_batch.html", **context)})
        else:
            response = make_response(render_template("wiki/navigation_pages.html", **context))
        response.headers["Cache-Control"] = "private, no-store"
        return response

    @app.route("/api/sidebar/pages")
    @login_required
    @rate_limit(120, 60)
    def api_sidebar_pages():
        """Load the next bounded batch of visible sidebar links."""
        return _navigation_batch_response(True)

    @app.route("/navigation/pages")
    @login_required
    @rate_limit(120, 60)
    def navigation_pages():
        """Provide a paged navigation fallback without JavaScript."""
        return _navigation_batch_response(False)

    @app.route("/api/category/<int:cat_id>/management")
    @login_required
    @editor_required
    @rate_limit(60, 60)
    def api_category_management(cat_id):
        """Load category controls on demand after checking current access."""
        if not 0 < cat_id < 2**63:
            abort(404)
        category = db.get_category(cat_id)
        if not category or not user_can_view_category(get_current_user(), cat_id):
            abort(404)
        cat = {**dict(category), "page_count": db.count_pages_in_category(cat_id)}
        response = jsonify({"html": render_template("wiki/_category_modal.html", cat=cat)})
        response.headers["Cache-Control"] = "private, no-store"
        return response

    @app.route("/api/sidebar/search")
    @login_required
    @rate_limit(60, 60)
    def api_sidebar_search():
        """Sidebar search: returns matching categories and pages as JSON.

        Query parameters:
          q: search term (required, min 1 char)
          scope: "title" (default) or "content" (also searches page body)

        Response::

            {
              "categories": [{"id": 1, "name": "...", "parent_id": null}, ...],
              "pages":       [{"id": 1, "title": "...", "slug": "...", "category_id": null}, ...]
            }
        """
        query = request.args.get("q", "").strip()
        if not query:
            return jsonify({"categories": [], "pages": []})
        scope = request.args.get("scope", "title")
        search_content = scope == "content"
        user = get_current_user()
        include_deindexed = bool(user and db.has_permission(user, "page.view_deindexed"))
        pages = db.search_pages_full(query, include_deindexed=include_deindexed,
                                     search_content=search_content)
        categories = db.search_categories(query)
        filtered_categories = [c for c in categories if user_can_view_category(user, c["id"])]
        filtered_pages = [p for p in pages if user_can_view_page(user, p)]
        reservation_map = db.get_active_page_reservations_map(
            user["id"] if user else None,
            [p["id"] for p in filtered_pages],
        )
        return jsonify({
            "categories": filtered_categories,
            "pages": [
                {
                    **p,
                    "is_reserved": bool(reservation_map.get(p["id"], {}).get("is_reserved")),
                    "reserved_by_current_user": bool(
                        reservation_map.get(p["id"], {}).get("reserved_by_current_user")
                    ),
                    "reservation_label": reservation_map.get(p["id"], {}).get("reservation_label"),
                    "user_in_cooldown": bool(
                        reservation_map.get(p["id"], {}).get("user_in_cooldown")
                    ),
                    "cooldown_label": reservation_map.get(p["id"], {}).get("cooldown_label"),
                }
                for p in filtered_pages
            ],
        })

    @app.route("/api/preview", methods=["POST"])
    @login_required
    @rate_limit(30, 60)
    def api_preview():
        """Render a Markdown string and return the sanitised HTML as JSON."""
        data = request.get_json(silent=True)
        if not data:
            return jsonify({"error": "Invalid request: missing or malformed JSON"}), 400
        content = data.get("content", "")
        html = render_markdown(content, embed_videos=True, auto_fix_lists=False)
        return jsonify({"html": html})

    @app.route("/api/code/highlight", methods=["POST"])
    @login_required
    @rate_limit(60, 60)
    def api_highlight_code():
        """Return sanitised syntax-highlighted HTML for a code block."""
        data = request.get_json(silent=True)
        if not data:
            return jsonify({"error": "Invalid request: missing or malformed JSON"}), 400
        content = data.get("content", "")
        language = (data.get("language") or "").strip()[:64]
        html = highlight_code_html(content, language=language)
        return jsonify({"html": html})

    @app.route("/api/draft/save", methods=["POST"])
    @login_required
    @editor_required
    @rate_limit(30, 60)
    def api_save_draft():
        """Autosave: insert or update the current user's draft for a page.

        Guards against a race where an autosave request fires right after
        the user commits the page (and the draft was deleted).  If the page
        was edited more recently than 30 seconds ago and no draft currently
        exists, we assume the draft was intentionally cleared and refuse to
        re-create it.
        """
        data = request.get_json(silent=True)
        if not data:
            return jsonify({"error": "invalid request"}), 400
        page_id = data.get("page_id")
        if page_id is None:
            return jsonify({"error": "missing page_id"}), 400
        try:
            page_id = int(page_id)
        except (TypeError, ValueError):
            return jsonify({"error": "invalid page_id"}), 400
        title = data.get("title", "")
        content = data.get("content", "")
        user = get_current_user()
        page, error_response = _get_editable_page_or_response(page_id, user)
        if error_response:
            return error_response

        # Reject drafts with no edits: if content and title match the page
        if title.strip() == (page.get("title") or "").strip() and content.strip() == (page.get("content") or "").strip():
            return jsonify({"ok": True, "message": "No changes to save."})

        # Race-condition guard: if the page was just committed and the draft
        # was deleted, don't silently re-create a stale draft.
        existing = db.get_draft(page_id, user["id"])
        if existing is None and page and page.get("last_edited_at"):
            from datetime import datetime, timezone
            try:
                edited = datetime.fromisoformat(page["last_edited_at"])
                now = datetime.now(timezone.utc)
                delta = (now - edited).total_seconds()
                if 0 <= delta < 30:
                    return jsonify({"ok": True, "message": "Page was recently committed; draft not re-created."})
            except (ValueError, TypeError):
                pass

        db.save_draft(page_id, user["id"], title, content)
        return jsonify({"ok": True, "message": "Draft saved successfully."})

    @app.route("/api/draft/load/<int:page_id>")
    @login_required
    @editor_required
    @rate_limit(60, 60)
    def api_load_draft(page_id):
        """Return the current user's saved draft for *page_id*, or nulls if none exists."""
        user = get_current_user()
        _, error_response = _get_editable_page_or_response(page_id, user)
        if error_response:
            return error_response
        draft = db.get_draft(page_id, user["id"])
        if draft:
            return jsonify({"title": draft["title"], "content": draft["content"],
                            "updated_at": draft["updated_at"]})
        return jsonify({"title": None, "content": None})

    @app.route("/api/draft/others/<int:page_id>")
    @login_required
    @editor_required
    @rate_limit(60, 60)
    def api_other_drafts(page_id):
        """Return a list of other editors' drafts for *page_id* (conflict detection)."""
        user = get_current_user()
        page, error_response = _get_editable_page_or_response(page_id, user)
        if error_response:
            return error_response
        drafts = db.get_drafts_for_page(page_id)
        others = [{"username": d["username"], "user_id": d["user_id"],
                   "updated_at": d["updated_at"]} for d in drafts if d["user_id"] != user["id"]]
        return jsonify({"drafts": others, "page_last_edited_at": page["last_edited_at"]})

    @app.route("/api/draft/transfer", methods=["POST"])
    @login_required
    @admin_required
    @rate_limit(30, 60)
    def api_transfer_draft():
        """Transfer another user's draft to the current admin, replacing any existing draft (admin only)."""
        data = request.get_json(silent=True)
        if not data:
            return jsonify({"error": "invalid request"}), 400
        page_id = data.get("page_id")
        from_user = data.get("from_user_id")
        try:
            page_id = int(page_id)
        except (TypeError, ValueError):
            return jsonify({"error": "invalid page_id or from_user_id"}), 400
        if not from_user:
            return jsonify({"error": "invalid page_id or from_user_id"}), 400
        user = get_current_user()
        _, error_response = _get_editable_page_or_response(page_id, user)
        if error_response:
            return error_response
        if from_user == user["id"]:
            return jsonify({"error": "cannot transfer draft from yourself"}), 400
        source_draft = db.get_draft(page_id, from_user)
        if not source_draft:
            return jsonify({"error": "draft not found"}), 404
        db.transfer_draft(page_id, from_user, user["id"])
        wiki_logger.log_action("transfer_draft", request, user=user, page_id=page_id, from_user=from_user)
        return jsonify({"ok": True, "message": "Draft has been successfully transferred to your account."})

    @app.route("/api/draft/delete", methods=["POST"])
    @login_required
    @editor_required
    @rate_limit(30, 60)
    def api_delete_draft():
        """Delete the current user's draft for the given page."""
        data = request.get_json(silent=True)
        if not data:
            return jsonify({"error": "invalid request"}), 400
        page_id = data.get("page_id")
        if page_id is None:
            return jsonify({"error": "missing page_id"}), 400
        try:
            page_id = int(page_id)
        except (TypeError, ValueError):
            return jsonify({"error": "invalid page_id"}), 400
        user = get_current_user()
        _, error_response = _get_editable_page_or_response(page_id, user)
        if error_response:
            return error_response
        db.delete_draft(page_id, user["id"])
        cleanup_unused_uploads()
        return jsonify({"ok": True, "message": "Draft has been successfully deleted."})

    @app.route("/api/draft/mine")
    @login_required
    @editor_required
    @rate_limit(60, 60)
    def api_my_drafts():
        """List all pending drafts for the current user."""
        user = get_current_user()
        drafts = db.list_user_drafts(user["id"])
        return jsonify([
            {
                "page_id": d["page_id"],
                "page_title": d["page_title"],
                "page_slug": d["page_slug"],
                "title": d["title"],
                "updated_at": d["updated_at"],
                "updated_at_formatted": format_datetime(d["updated_at"]),
            }
            for d in drafts
            if editor_has_category_access(user, d["page_category_id"])
        ])

    _VALID_FONT_SCALES = {0.85, 0.9, 1.0, 1.1, 1.2, 1.35}
    _VALID_CONTRASTS = {0, 1, 2, 3, 4, 5}
    _VALID_LINE_HEIGHTS = {0, 1, 2}
    _VALID_LETTER_SPACINGS = {0, 1, 2}

    @app.route("/api/accessibility", methods=["GET"])
    @login_required
    def api_get_accessibility():
        """Return the current user's accessibility preferences as JSON."""
        user = get_current_user()
        if user:
            return jsonify(db.get_user_accessibility(user["id"]))
        return jsonify(_get_public_accessibility_from_cookie())

    @app.route("/api/accessibility", methods=["POST"])
    @login_required
    @rate_limit(60, 60)
    def api_save_accessibility():
        """Save the current user's accessibility preferences from a JSON body."""
        data = request.get_json(silent=True)
        if not data:
            return jsonify({"error": "invalid request"}), 400
        user = get_current_user()
        current = db.get_user_accessibility(user["id"]) if user else _get_public_accessibility_from_cookie()

        font_scale = data.get("font_scale", current["font_scale"])
        try:
            font_scale = float(font_scale)
        except (TypeError, ValueError):
            font_scale = 1.0
        if font_scale not in _VALID_FONT_SCALES:
            font_scale = min(_VALID_FONT_SCALES, key=lambda x: abs(x - font_scale))

        contrast = data.get("contrast", current["contrast"])
        try:
            contrast = int(contrast)
        except (TypeError, ValueError):
            contrast = 0
        if contrast not in _VALID_CONTRASTS:
            contrast = 0

        sidebar_width = data.get("sidebar_width", current["sidebar_width"])
        try:
            sidebar_width = int(sidebar_width)
            sidebar_width = max(180, min(500, sidebar_width))
        except (TypeError, ValueError):
            sidebar_width = 250

        content_max_width = data.get("content_max_width", current.get("content_max_width", 0))
        try:
            content_max_width = int(content_max_width)
            content_max_width = max(0, min(5000, content_max_width))
        except (TypeError, ValueError):
            content_max_width = 0

        editor_pane_width = data.get("editor_pane_width", current.get("editor_pane_width", 0))
        try:
            editor_pane_width = float(editor_pane_width)
            editor_pane_width = max(15, min(85, editor_pane_width)) if editor_pane_width > 0 else 0
        except (TypeError, ValueError):
            editor_pane_width = 0

        editor_height = data.get("editor_height", current.get("editor_height", 0))
        try:
            editor_height = int(editor_height)
            editor_height = max(300, min(2000, editor_height)) if editor_height > 0 else 0
        except (TypeError, ValueError):
            editor_height = 0

        def _clean_color(val):
            """Return *val* unchanged if it is a valid CSS hex or rgb() color; otherwise return empty string."""
            val = str(val).strip()
            if not val:
                return ""
            if re.match(r'^#[0-9a-fA-F]{6}$', val):
                return val
            if re.match(r'^rgb\(\s*\d+\s*,\s*\d+\s*,\s*\d+\s*\)$', val):
                return val
            return ""

        theme_mode = str(data.get("theme_mode", current.get("theme_mode", "default"))).strip().lower()
        if theme_mode not in {"default", "dark", "light"}:
            theme_mode = "default"
        interface_language = str(
            data.get("interface_language", current.get("interface_language", "default"))
        ).strip().lower()
        settings = db.get_site_settings() or {}
        interface_language = normalize_language_selection(
            interface_language,
            settings,
            default="default",
            allow_default=True,
        )

        prefs = {
            "theme_mode": theme_mode,
            "interface_language": interface_language,
            "font_scale": font_scale,
            "contrast": contrast,
            "sidebar_width": sidebar_width,
            "content_max_width": content_max_width,
            "editor_pane_width": editor_pane_width,
            "editor_height": editor_height,
            "custom_bg": _clean_color(data.get("custom_bg", current.get("custom_bg", ""))),
            "custom_text": _clean_color(data.get("custom_text", current.get("custom_text", ""))),
            "custom_primary": _clean_color(data.get("custom_primary", current.get("custom_primary", ""))),
            "custom_secondary": _clean_color(data.get("custom_secondary", current.get("custom_secondary", ""))),
            "custom_accent": _clean_color(data.get("custom_accent", current.get("custom_accent", ""))),
            "custom_sidebar": _clean_color(data.get("custom_sidebar", current.get("custom_sidebar", ""))),
            "background_image": current.get("background_image", ""),
        }

        line_height = data.get("line_height", current.get("line_height", 0))
        try:
            line_height = int(line_height)
        except (TypeError, ValueError):
            line_height = 0
        if line_height not in _VALID_LINE_HEIGHTS:
            line_height = 0
        prefs["line_height"] = line_height

        letter_spacing = data.get("letter_spacing", current.get("letter_spacing", 0))
        try:
            letter_spacing = int(letter_spacing)
        except (TypeError, ValueError):
            letter_spacing = 0
        if letter_spacing not in _VALID_LETTER_SPACINGS:
            letter_spacing = 0
        prefs["letter_spacing"] = letter_spacing

        reduce_motion = data.get("reduce_motion", current.get("reduce_motion", 0))
        try:
            reduce_motion = 1 if int(reduce_motion) else 0
        except (TypeError, ValueError):
            reduce_motion = 0
        prefs["reduce_motion"] = reduce_motion

        # Semantic Highlighting: 0=off, 1=subtle, 2=strong for each axis.
        for sem_key in (
            "semantic_bold", "semantic_italic", "semantic_code",
            "semantic_link", "semantic_heading",
        ):
            raw_val = data.get(sem_key, current.get(sem_key, 0))
            try:
                int_val = int(raw_val)
            except (TypeError, ValueError):
                int_val = 0
            if int_val not in (0, 1, 2):
                int_val = 0
            prefs[sem_key] = int_val
        if user:
            db.save_user_accessibility(user["id"], prefs)
            return jsonify({"ok": True, "message": "Customization settings saved successfully."})
        resp = make_response(jsonify({"ok": True, "message": "Customization settings saved successfully."}))
        resp.set_cookie(
            "bw_public_accessibility",
            json.dumps(prefs, separators=(",", ":")),
            max_age=60 * 60 * 24 * 365,
            httponly=True,
            samesite="Lax",
            secure=request.is_secure,
        )
        return resp

    @app.route("/api/accessibility/reset", methods=["POST"])
    @login_required
    @rate_limit(10, 60)
    def api_reset_accessibility():
        """Reset the current user's accessibility preferences to the system defaults."""
        user = get_current_user()
        if user:
            current = db.get_user_accessibility(user["id"])
            _remove_background_file(current.get("background_image", ""))
            db.save_user_accessibility(user["id"], dict(db._A11Y_DEFAULTS))
        resp = make_response(jsonify({
            "ok": True,
            "message": "Customization settings have been reset to default.",
            "defaults": db._A11Y_DEFAULTS,
        }))
        if not user:
            resp.delete_cookie("bw_public_accessibility", samesite="Lax")
        return resp

    @app.route("/api/accessibility/background", methods=["POST", "DELETE"])
    @login_required
    @rate_limit(10, 60)
    def api_accessibility_background():
        """Upload or clear the current user's custom wiki background image."""
        user = get_current_user()
        if not user:
            return _background_upload_error("Login required.", 401)

        prefs = db.get_user_accessibility(user["id"])
        old_background = prefs.get("background_image", "")

        if request.method == "DELETE":
            prefs["background_image"] = ""
            db.save_user_accessibility(user["id"], prefs)
            _remove_background_file(old_background)
            return jsonify({"ok": True, "background_image": "", "url": ""})

        if "file" not in request.files:
            return _background_upload_error("No file provided.")
        uploaded = request.files["file"]
        if not uploaded or not uploaded.filename:
            return _background_upload_error("No file provided.")

        stored_name, error = _save_custom_background(uploaded)
        if error:
            status = 413 if "4 MB" in error or "dimensions" in error else 400
            return _background_upload_error(error, status)

        prefs["background_image"] = stored_name
        db.save_user_accessibility(user["id"], prefs)
        if old_background and old_background != stored_name:
            _remove_background_file(old_background)
        save_path = os.path.abspath(os.path.join(config.UPLOAD_FOLDER, stored_name))
        notify_file_upload(stored_name, save_path, display_name=f"Background for {user['username']}")
        wiki_logger.log_action("update_accessibility_background", request, user=user, filename=stored_name)
        return jsonify({
            "ok": True,
            "background_image": stored_name,
            "url": _background_image_url(stored_name),
        })

    @app.route("/api/translations/<lang>")
    @rate_limit(30, 60)
    def api_get_translations(lang):
        """Return all translations for the requested language as JSON."""
        from helpers._translations import get_js_translations
        from helpers._interface_languages import get_enabled_interface_languages
        lang = (lang or "").strip().lower()
        settings = db.get_site_settings() or {}
        supported = set(get_enabled_interface_languages(settings).keys())
        if lang not in supported:
            lang = "en"
        translations = get_js_translations(lang)
        resp = jsonify({"lang": lang, "translations": translations})
        resp.cache_control.max_age = 3600
        return resp

    @app.route("/api/reorder/pages", methods=["POST"])
    @login_required
    @editor_required
    @rate_limit(60, 60)
    def api_reorder_pages():
        """Persist a new page sort order. Body: {"ids": [<page_id>, ...]}"""
        data = request.get_json(silent=True)
        if not isinstance(data, dict) or not isinstance(data.get("ids"), list) or len(data["ids"]) > 10000:
            return jsonify({"error": "invalid request"}), 400
        try:
            ids = [int(i) for i in data["ids"]]
            if len(set(ids)) != len(ids) or any(not 0 < value < 2**63 for value in ids):
                raise ValueError
        except (TypeError, ValueError, OverflowError):
            return jsonify({"error": "invalid ids"}), 400
        user = get_current_user()
        error = _get_reorder_pages_error_response(ids, user)
        if error:
            return error
        try:
            db.update_pages_sort_order(ids)
        except ValueError as error:
            return jsonify({"error": str(error)}), 400
        wiki_logger.log_action("reorder_pages", request, user=user, count=len(ids))
        notify_change("pages_reorder", "Page order updated")
        return jsonify({"ok": True, "message": "Page order saved successfully."})

    @app.route("/api/reorder/categories", methods=["POST"])
    @login_required
    @editor_required
    @rate_limit(60, 60)
    def api_reorder_categories():
        """Persist a new category sort order. Body: {"ids": [<cat_id>, ...]}"""
        data = request.get_json(silent=True)
        if not data or not isinstance(data.get("ids"), list):
            return jsonify({"error": "invalid request"}), 400
        try:
            ids = [int(i) for i in data["ids"]]
        except (TypeError, ValueError):
            return jsonify({"error": "invalid ids"}), 400
        user = get_current_user()
        error = _get_reorder_categories_error_response(ids, user)
        if error:
            return error
        db.update_categories_sort_order(ids)
        wiki_logger.log_action("reorder_categories", request, user=user, count=len(ids))
        notify_change("categories_reorder", "Category order updated")
        return jsonify({"ok": True, "message": "Category order saved successfully."})

    @app.route("/api/pages/<int:page_id>/reservation/status")
    @login_required
    @editor_required
    def api_page_reservation_status(page_id):
        """Get current reservation status for a page."""
        user = get_current_user()
        if not db.reservations_enabled():
            return jsonify({"error": "Page reservations are currently disabled"}), 403
        page = db.get_page(page_id)
        if not page:
            return jsonify({"error": "Page not found"}), 404
        if page["is_home"]:
            return jsonify({"error": "The home page cannot be reserved"}), 400

        # Check if user has category access
        if not editor_has_category_access(user, page["category_id"]):
            return jsonify({"error": "You do not have permission to access this page"}), 403

        status = db.get_page_reservation_status(page_id, user["id"])

        # Format time remaining for display
        response = {
            "is_reserved": status["is_reserved"],
            "reserved_by": status["reserved_by"],
            "reserved_by_username": status["reserved_by_username"],
            "reserved_at": status["reserved_at"],
            "expires_at": status["expires_at"],
            "user_in_cooldown": status.get("user_in_cooldown", False),
            "cooldown_until": status.get("cooldown_until"),
        }

        # Add human-readable time remaining
        if status["time_remaining"]:
            total_seconds = int(status["time_remaining"].total_seconds())
            hours = total_seconds // 3600
            minutes = (total_seconds % 3600) // 60
            response["time_remaining_text"] = f"{hours}h {minutes}m"
        else:
            response["time_remaining_text"] = None

        # Add human-readable cooldown remaining
        if status.get("cooldown_remaining"):
            total_seconds = int(status["cooldown_remaining"].total_seconds())
            hours = total_seconds // 3600
            minutes = (total_seconds % 3600) // 60
            response["cooldown_remaining_text"] = f"{hours}h {minutes}m"
        else:
            response["cooldown_remaining_text"] = None

        return jsonify(response)

    @app.route("/api/pages/<int:page_id>/reservation", methods=["POST"])
    @login_required
    @editor_required
    @rate_limit(30, 60)
    def api_reserve_page(page_id):
        """Reserve a page for exclusive editing."""
        user = get_current_user()
        if not db.reservations_enabled():
            return jsonify({"error": "Page reservations are currently disabled"}), 403
        page = db.get_page(page_id)
        if not page:
            return jsonify({"error": "Page not found"}), 404
        if page["is_home"]:
            return jsonify({"error": "The home page cannot be reserved"}), 400

        # Check if user has category access
        if not editor_has_category_access(user, page["category_id"]):
            return jsonify({"error": "You do not have permission to edit pages in this category"}), 403

        try:
            reservation = db.reserve_page(page_id, user["id"])
            wiki_logger.log_action("reserve_page", request, user=user, page_id=page_id)
            notify_change("page_reservation", f"Page '{page['title']}' reserved by {user['username']}")
            return jsonify({
                "ok": True,
                "message": "Page has been successfully reserved for your editing.",
                "reservation": {
                    "page_id": reservation["page_id"],
                    "reserved_at": reservation["reserved_at"],
                    "expires_at": reservation["expires_at"],
                }
            })
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 409

    @app.route("/api/pages/<int:page_id>/reservation", methods=["DELETE"])
    @login_required
    @editor_required
    @rate_limit(30, 60)
    def api_release_page_reservation(page_id):
        """Release a page reservation."""
        user = get_current_user()
        if not db.reservations_enabled():
            return jsonify({"error": "Page reservations are currently disabled"}), 403
        page = db.get_page(page_id)
        if not page:
            return jsonify({"error": "Page not found"}), 404

        # Check if user has category access
        if not editor_has_category_access(user, page["category_id"]):
            return jsonify({"error": "You do not have permission to edit pages in this category"}), 403

        # Release the reservation (only if user holds it)
        released = db.release_page_reservation(page_id, user["id"])
        if released:
            wiki_logger.log_action("release_page_reservation", request, user=user, page_id=page_id)
            notify_change("page_reservation", f"Page '{page['title']}' reservation released by {user['username']}")
            return jsonify({"ok": True, "message": "Page reservation has been successfully released."})
        else:
            return jsonify({"error": "No active reservation found for this page by you"}), 404

    @app.route("/api/pages/<int:page_id>/home", methods=["POST"])
    @login_required
    @rate_limit(20, 60)
    def api_set_home_page(page_id):
        """Set a page as the home page."""
        user = get_current_user()
        if user["role"] not in ("admin", "owner"):
            return jsonify({"error": "Only administrators can set the home page"}), 403
        page = db.get_page(page_id)
        if not page:
            return jsonify({"error": "Page not found"}), 404

        try:
            already_home = bool(page["is_home"])
            home_page = db.set_home_page(page_id)
            wiki_logger.log_action("set_home_page", request, user=user, page_id=page_id)
            notify_change("set_home_page", f"Page '{home_page['title']}' is now the home page")
            return jsonify({
                "ok": True,
                "already_home": already_home,
                "message": (
                    "This page is already the home page."
                    if already_home else f"'{home_page['title']}' is now the home page."
                ),
                "home_page": {
                    "id": home_page["id"],
                    "title": home_page["title"],
                    "slug": home_page["slug"],
                    "is_home": bool(home_page["is_home"]),
                    "is_deindexed": bool(home_page["is_deindexed"]),
                    "pending_deletion": bool(home_page["pending_deletion"]),
                }
            })
        except ValueError as e:
            status = 404 if str(e) == "Page not found" else 400
            return jsonify({"error": str(e)}), status

    @app.route("/api/embed/canvas/<slug>")
    @login_required
    @rate_limit(120, 60)
    def api_embed_canvas(slug):
        """Return canvas data for embedding in wiki pages."""
        layout = db.canvas_get_layout_by_slug(slug)
        if not layout:
            return jsonify({"error": "Canvas not found"}), 404
        user = get_current_user()
        if not db.canvas_user_can_view(layout["id"], user):
            return jsonify({"error": "Access denied"}), 403
        data = layout["data"]
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except (TypeError, ValueError):
                data = None
        # Clean it the same way GET /canvas/<slug>/data does, so older rows
        # that still hold preview or highlight HTML never reach the embed.
        data = sanitize_layout_data(data)
        return jsonify({
            "slug": layout["slug"],
            "title": layout["title"],
            "data": data,
            "version": layout["version"],
            "seq": db.canvas_latest_event_seq(layout["id"]),
        })

    @app.route("/api/embed/canvas/<slug>/sync")
    @login_required
    @rate_limit(120, 60)
    def api_embed_canvas_sync(slug):
        """Return canvas sync events for embedded real-time updates."""
        layout = db.canvas_get_layout_by_slug(slug)
        if not layout:
            return jsonify({"error": "Canvas not found"}), 404
        user = get_current_user()
        if not db.canvas_user_can_view(layout["id"], user):
            return jsonify({"error": "Access denied"}), 403
        try:
            since = int(request.args.get("since", "0"))
        except (TypeError, ValueError):
            since = 0
        session_id = (request.headers.get("X-Canvas-Session") or "").strip()[:64]
        rows = db.canvas_events_since(
            layout["id"], since,
            exclude_session=session_id or None,
        )
        events = []
        for row in rows or []:
            payload = row["payload"]
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except (TypeError, ValueError):
                    payload = {}
            events.append({
                "id": row["id"],
                "seq": row["seq"],
                "op_type": row["op_type"],
                "payload": payload,
                "by_user_id": row["by_user_id"],
                "by_session": row["by_session"],
                "created_at": row["created_at"],
            })
        head_seq = db.canvas_latest_event_seq(layout["id"])
        return jsonify({"events": events, "seq": head_seq})

    @app.route("/api/embed/kanban/<int:board_id>")
    @login_required
    @rate_limit(120, 60)
    def api_embed_kanban(board_id):
        """Return kanban board data for embedding in wiki pages."""
        board = db.kanban_get_board(board_id)
        if not board:
            return jsonify({"error": "Board not found"}), 404
        user = get_current_user()
        # Same read check as the board page itself, so an embed cannot show a
        # board its reader could not open, and nothing is served while the
        # kanban plugin is disabled.
        if not user_can_view_kanban_board(user, board):
            return jsonify({"error": "Access denied"}), 403
        columns = db.kanban_list_columns(board_id)
        cols_data = []
        for col in columns:
            tickets = db.kanban_list_tickets(col["id"])
            tickets_data = []
            for t_row in tickets:
                assignee_ids = db.kanban_list_ticket_assignee_ids(t_row["id"])
                tickets_data.append({
                    "id": t_row["id"],
                    "title": t_row["title"],
                    "description": t_row["description"] or "",
                    "priority": t_row["priority"] or "medium",
                    "color": t_row["color"] or "",
                    "labels": json.loads(t_row["labels"]) if t_row["labels"] else [],
                    "due_date": t_row["due_date"] or "",
                    "sort_order": t_row["sort_order"],
                    "assignee_ids": assignee_ids,
                })
            cols_data.append({
                "id": col["id"],
                "title": col["title"],
                "sort_order": col["sort_order"],
                "tickets": tickets_data,
            })
        return jsonify({
            "id": board["id"],
            "title": board["title"],
            "description": board["description"] or "",
            "columns": cols_data,
            "seq": db.kanban_latest_event_seq(board_id),
        })

    @app.route("/api/embed/kanban/<int:board_id>/sync")
    @login_required
    @rate_limit(120, 60)
    def api_embed_kanban_sync(board_id):
        """Return kanban sync events for embedded real-time updates."""
        board = db.kanban_get_board(board_id)
        if not board:
            return jsonify({"error": "Board not found"}), 404
        user = get_current_user()
        # Same read check as the board page itself, so an embed cannot show a
        # board its reader could not open, and nothing is served while the
        # kanban plugin is disabled.
        if not user_can_view_kanban_board(user, board):
            return jsonify({"error": "Access denied"}), 403
        try:
            since = int(request.args.get("since", "0"))
        except (TypeError, ValueError):
            since = 0
        session_id = (request.headers.get("X-Kanban-Session") or "").strip()[:64]
        rows = db.kanban_events_since(
            board_id, since,
            exclude_session=session_id or None,
        )
        events = []
        for row in rows or []:
            payload = row["payload"]
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except (TypeError, ValueError):
                    payload = {}
            events.append({
                "id": row["id"],
                "seq": row["seq"],
                "op_type": row["op_type"],
                "payload": payload,
                "by_user_id": row["by_user_id"],
                "by_session": row["by_session"],
                "created_at": row["created_at"],
            })
        head_seq = db.kanban_latest_event_seq(board_id)
        return jsonify({"events": events, "seq": head_seq})
