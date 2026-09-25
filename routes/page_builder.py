"""Core drag-and-drop page builder routes."""

from flask import abort, jsonify, render_template, request

import config
import db
from bananawiki_sdk import emit_hook
from helpers import (
    BuilderValidationError,
    compile_builder_markdown,
    dump_builder_payload,
    get_current_user,
    load_builder_payload,
    login_required,
    rate_limit,
    render_markdown,
    user_can_use_page_builder,
    user_can_view_page,
)
from routes.uploads import cleanup_unused_uploads
from sync import notify_change
from wiki_logger import log_action


def register_page_builder_routes(app):
    """Register core builder routes. Runtime gates keep the feature opt-in."""

    def _error(message, status=400):
        """Return a consistent JSON error for builder requests."""
        return jsonify({"ok": False, "error": message}), status

    def _get_accessible_page(slug, *, write=False):
        """Check page visibility and, for writes, edit permissions and locks."""
        page = db.get_page_by_slug(slug)
        if not page:
            abort(404)
        user = get_current_user()
        if not user_can_use_page_builder(user):
            abort(403)
        if not user_can_view_page(user, page):
            abort(403)
        if not write:
            return page, user, None
        if page["pending_deletion"]:
            return page, user, _error("Pages pending deletion cannot be edited.", 409)
        if user["role"] == "editor" and not db.has_category_write_access(user, page["category_id"]):
            return page, user, _error("You cannot edit pages in this category.", 403)
        if page["protected_by"] and page["protected_by"] != user["id"]:
            return page, user, _error("This page is protected by another user.", 409)
        settings = db.get_site_settings() or {}
        if settings.get("page_reservations_enabled"):
            status = db.get_page_reservation_status(page["id"], user["id"])
            reserved_by_other = status.get("is_reserved") and status.get("reserved_by") != user["id"]
            if reserved_by_other and user["role"] not in ("admin", "owner"):
                return page, user, _error("This page is reserved by another editor.", 409)
        return page, user, None

    def _payload_from_request(*, allow_incomplete=False):
        """Read and validate the builder document in a JSON request."""
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            raise BuilderValidationError("Send a valid builder document.")
        payload = data.get("document")
        normalized_json = dump_builder_payload(payload, allow_incomplete=allow_incomplete)
        return data, payload, normalized_json

    @app.route("/page/<slug>/builder")
    @login_required
    @rate_limit(60, 60)
    def page_builder(slug):
        """Open the visual editor with the user's saved draft when available."""
        page, user, _ = _get_accessible_page(slug)
        draft = db.get_page_builder_draft(page["id"], user["id"])
        source = draft["builder_json"] if draft else page["builder_json"]
        if source:
            try:
                document = load_builder_payload(source)
            except BuilderValidationError:
                document = {"version": 1, "blocks": []}
        elif page["content"]:
            document = {
                "version": 1,
                "blocks": [{"type": "text", "text": page["content"][:20_000]}],
            }
        else:
            document = {"version": 1, "blocks": []}
        return render_template(
            "wiki/page_builder.html",
            page=page,
            builder_document=document,
            has_builder_draft=bool(draft),
            can_publish_public=(
                user["role"] in ("admin", "owner")
                and not getattr(config, "FORBID_PUBLIC_BUILDER_PAGES", False)
            ),
        )

    @app.route("/api/page/<slug>/builder/preview", methods=["POST"])
    @login_required
    @rate_limit(60, 60)
    def page_builder_preview(slug):
        """Render a validated draft without changing the page."""
        _get_accessible_page(slug)
        try:
            _data, payload, _encoded = _payload_from_request(allow_incomplete=True)
            markdown = compile_builder_markdown(payload, allow_incomplete=True)
        except BuilderValidationError as exc:
            return _error(str(exc))
        return jsonify({"ok": True, "html": render_markdown(markdown, embed_videos=True)})

    @app.route("/api/page/<slug>/builder/draft", methods=["POST"])
    @login_required
    @rate_limit(30, 60)
    def page_builder_save_draft(slug):
        """Save a draft only when its base page revision is current."""
        page, user, blocked = _get_accessible_page(slug, write=True)
        if blocked:
            return blocked
        try:
            data, _payload, encoded = _payload_from_request(allow_incomplete=True)
        except BuilderValidationError as exc:
            return _error(str(exc))
        if "base_edited_at" not in data:
            return _error("A page revision token is required.")
        base_edited_at = data.get("base_edited_at") or ""
        saved = db.save_page_builder_draft_if_current(
            page["id"], user["id"], encoded, base_edited_at
        )
        if not saved:
            return _error("This draft belongs to an older page revision. Reload the builder.", 409)
        return jsonify({"ok": True})

    @app.route("/api/page/<slug>/builder/publish", methods=["POST"])
    @login_required
    @rate_limit(20, 60)
    def page_builder_publish(slug):
        """Publish a validated document after checking the page revision."""
        page, user, blocked = _get_accessible_page(slug, write=True)
        if blocked:
            return blocked
        try:
            data, payload, encoded = _payload_from_request()
            markdown = compile_builder_markdown(payload)
        except BuilderValidationError as exc:
            return _error(str(exc))

        if "base_edited_at" not in data:
            return _error("A page revision token is required.")
        base_edited_at = data.get("base_edited_at") or ""
        title = str(data.get("title") or page["title"]).strip()
        if not title or len(title) > 200:
            return _error("The page title must contain 1-200 characters.")
        public_requested = bool(data.get("public"))
        builder_public = bool(
            public_requested
            and user["role"] in ("admin", "owner")
            and not getattr(config, "FORBID_PUBLIC_BUILDER_PAGES", False)
        )
        edit_message = str(data.get("edit_message") or "Updated with visual page builder").strip()[:500]
        published = db.update_page(
            page["id"],
            title,
            markdown,
            user["id"],
            edit_message,
            builder_json=encoded,
            builder_public=builder_public,
            expected_last_edited_at=base_edited_at,
        )
        if not published:
            return _error("This page changed in another session. Reload before publishing.", 409)
        db.delete_page_builder_draft(page["id"], user["id"])
        cleanup_unused_uploads()
        updated_page = db.get_page(page["id"])
        log_action("publish_page_builder", request, user=user, page=slug)
        notify_change("page_edit", f"Page '{slug}' published with the visual builder")
        emit_hook("after_page_update", page=updated_page, user=user)
        return jsonify({
            "ok": True,
            "redirect": f"/page/{slug}",
            "public": builder_public,
            "last_edited_at": updated_page["last_edited_at"],
        })
