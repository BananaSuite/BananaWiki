"""Page builder: the visual editor, its JSON API and the access setting."""

from __future__ import annotations

from typing import Any

from flask import abort, current_app, jsonify, redirect, render_template, request, url_for

from ... import auth, registry, settings, storage
from ...i18n import t
from ...permissions import ROLES
from ..pages import access as page_access
from ..pages import categories, uploads
from ..pages import service as pages
from . import document, embeds, service, starters

bp = registry.feature_blueprint(
    "page_builder", "page_builder", __name__,
    template_folder="templates", static_folder="static", static_url_path="/static/page_builder",
)
ACCESS_LEVELS = ("admin", "editor", "user")
IMAGE_UPLOADS_PER_MINUTE = 20
PREVIEWS_PER_MINUTE = 120
LOOKUPS_PER_MINUTE = 120


@bp.before_request
def _host_gate():
    if current_app.config["BW"].forbid_page_builder:
        abort(404)


class Refused(Exception):
    """Stops a builder request with a translated JSON error."""

    def __init__(self, status: int, key: str, **values: Any):
        super().__init__(key)
        self.status = status
        self.key = key
        self.values = values


@bp.errorhandler(Refused)
def _refused(error: Refused):
    return jsonify({"ok": False, "error": t(error.key, **error.values)}), error.status


def _buildable_page(slug: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """The page and user for a builder API call, or :class:`Refused`."""
    user = auth.current_user()
    page = pages.get_by_slug(slug)
    if page is None or not pages.can_view(page, user):
        raise Refused(404, "page_builder.error.page_missing")
    if not service.may_build(page, user):
        raise Refused(403, "page_builder.error.forbidden")
    blocked = page_access.edit_blocked(page, user)
    if blocked:
        raise Refused(409, blocked)
    return page, user


def json_payload() -> dict[str, Any]:
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise Refused(400, "page_builder.error.version")
    return data


def _revision(data: dict[str, Any]) -> int:
    value = data.get("base_revision")
    if isinstance(value, bool) or not isinstance(value, int):
        raise Refused(400, "page_builder.error.revision_missing")
    return value


def validated_document(data: dict[str, Any], *, complete: bool) -> dict[str, Any]:
    try:
        return document.validate(data.get("document"), complete=complete)
    except document.DocumentError as error:
        raise Refused(400, error.key) from None


def rate_limit(name: str, user: dict[str, Any], per_minute: int, key: str = "page_builder.error.preview_slow_down") -> None:
    limiter = current_app.extensions["bananawiki.limiter"]
    if not limiter.hit(f"page_builder.{name}:{user['id']}", per_minute, 60):
        raise Refused(429, key)


def _builder_user() -> dict[str, Any]:
    """The signed-in user for page-independent builder calls, or :class:`Refused`."""
    user = auth.current_user()
    if not service.may_use(user):
        raise Refused(403, "page_builder.error.forbidden")
    return user


def preview_response(validated: dict[str, Any], *, page_id: int | None) -> Any:
    """The rendered preview and the checks of a document (shared with custom pages)."""
    checks = [{"block": hint["block"], "message": t(hint["key"])} for hint in document.audit(validated)]
    return jsonify({"ok": True, "html": str(document.render(validated, page_id=page_id)), "checks": checks})


def store_image(user: dict[str, Any]) -> Any:
    """Save an uploaded builder image (shared with custom pages)."""
    rate_limit("image", user, IMAGE_UPLOADS_PER_MINUTE, "page_builder.error.slow_down")
    try:
        stored = uploads.save_image(request.files.get("file"), user)
    except storage.UploadError as error:
        raise Refused(400, error.key, **error.values) from None
    return jsonify({"ok": True, "url": stored["url"]})


def editor_options(user: dict[str, Any]) -> dict[str, Any]:
    """Choices the editor script needs: categories, embeds, layout values, starters and sections."""
    paths = categories.paths(lambda cid: auth.can_read_category(cid, user))
    return {
        "categories": sorted(([cid, path] for cid, path in paths.items() if auth.can_read_category(cid, user)),
                             key=lambda entry: entry[1].casefold()),
        "embeds": embeds.available_kinds(),
        "layout": document.LAYOUT,
        "starters": starters.starters(),
        "sections": [{**section, "delete_url": url_for("page_builder.delete_section", section_id=section["id"])}
                     for section in service.sections()],
        "manage_sections": service.may_manage_sections(user),
    }


def common_urls() -> dict[str, str]:
    """Endpoints every editor uses, whatever it edits."""
    return {
        "search": url_for("pages.api_pages_search"),
        "embeddables": url_for("page_builder.embeddables"),
        "embed_frame": url_for("page_builder.embed_frame"),
        "clipboard": url_for("page_builder.clipboard"),
        "sections": url_for("page_builder.create_section"),
    }


# ── Editor ────────────────────────────────────────────────────────────────────


def header_action(page: dict[str, Any]) -> str:
    """``page.header_actions`` slot: a "Visual builder" button for those who may use it here."""
    if not service.may_build(page):
        return ""
    return render_template("page_builder/_header_action.html", page=page)


@bp.get("/page/<slug>/builder")
def editor(slug: str):
    user = auth.current_user()
    page = pages.get_by_slug(slug)
    if page is None or not pages.can_view(page, user):
        abort(404)
    if not service.may_use(user):
        abort(403)
    if not pages.can_edit(page, user):
        denied = registry.intercept("page.edit_denied", page=page, user=user)
        if denied is None:
            abort(403)
        return denied
    blocked = page_access.edit_blocked(page, user)
    if blocked:
        auth.flash_t(blocked, "error")
        return redirect(url_for("pages.view", slug=page["slug"]))
    draft = service.get_draft(page["id"], user["id"])
    initial, base_revision, stale = service.page_document(page), int(page.get("revision") or 0), False
    if draft:
        try:
            initial = document.load(draft["builder_json"])
            base_revision = int(draft.get("base_revision") or 0)
            stale = base_revision != int(page.get("revision") or 0)
        except document.DocumentError:
            service.delete_draft(page["id"], user["id"])
            draft = None
    urls = {
        **common_urls(),
        "preview": url_for("page_builder.preview", slug=page["slug"]),
        "draft": url_for("page_builder.save_draft", slug=page["slug"]),
        "publish": url_for("page_builder.publish", slug=page["slug"]),
        "upload": url_for("page_builder.upload_image", slug=page["slug"]),
        "view": url_for("pages.view", slug=page["slug"]),
        "reload": url_for("page_builder.editor", slug=page["slug"]),
    }
    return render_template(
        "page_builder/editor.html", page=page, initial=initial, base_revision=base_revision,
        has_draft=bool(draft), stale_draft=stale, can_publish_public=service.may_publish_public(user),
        palette=document.PALETTE, options=editor_options(user), urls=urls,
    )


@bp.post("/api/page/<slug>/builder/preview")
def preview(slug: str):
    page, user = _buildable_page(slug)
    rate_limit("preview", user, PREVIEWS_PER_MINUTE)
    return preview_response(validated_document(json_payload(), complete=False), page_id=page["id"])


@bp.post("/api/page/<slug>/builder/draft")
def save_draft(slug: str):
    page, user = _buildable_page(slug)
    data = json_payload()
    validated = validated_document(data, complete=False)
    if not service.save_draft(page["id"], user["id"], document.dump(validated), _revision(data)):
        raise Refused(409, "page_builder.error.draft_stale")
    return jsonify({"ok": True})


@bp.delete("/api/page/<slug>/builder/draft")
def discard_draft(slug: str):
    page, user = _buildable_page(slug)
    service.delete_draft(page["id"], user["id"])
    return jsonify({"ok": True})


@bp.post("/api/page/<slug>/builder/publish")
def publish(slug: str):
    page, user = _buildable_page(slug)
    data = json_payload()
    title = data.get("title")
    if title is not None and not isinstance(title, str):
        raise Refused(400, "page_builder.error.text_type")
    if not page_access.can_edit_metadata(page, user):
        title = None  # renaming needs page.edit_metadata, as in the Markdown editor
    edit_message = data.get("edit_message") if isinstance(data.get("edit_message"), str) else ""
    try:
        updated = service.publish(page, user, data.get("document"), title=title, edit_message=edit_message.strip(),
                                  public=data.get("public") is True, base_revision=_revision(data))
    except document.DocumentError as error:
        raise Refused(400, error.key) from None
    except pages.EditConflict:
        raise Refused(409, "page_builder.error.conflict") from None
    except pages.PageError as error:
        raise Refused(400, error.key, **error.values) from None
    return jsonify({"ok": True, "redirect": url_for("pages.view", slug=updated["slug"]),
                    "public": bool(updated["builder_public"]), "revision": updated["revision"]})


@bp.post("/api/page/<slug>/builder/image")
def upload_image(slug: str):
    _page, user = _buildable_page(slug)
    return store_image(user)


# ── Page-independent helpers: embeds, clipboard, saved sections ───────────────


@bp.get("/api/page-builder/embeddables")
def embeddables():
    """Canvases and boards the editor may open, for the embed block's picker."""
    user = _builder_user()
    rate_limit("lookup", user, LOOKUPS_PER_MINUTE)
    kind = request.args.get("kind") or None
    if kind is not None and kind not in document.EMBED_KINDS:
        raise Refused(400, "page_builder.error.embed")
    return jsonify({"ok": True, "items": embeds.search(user, request.args.get("q", ""), kind)})


@bp.get("/page-builder/embed-frame")
def embed_frame():
    """One embed as the published page shows it to this user, for the editor's live preview.

    The page holds the same placeholder as a published builder page and the
    same scripts, so the canvas or board is loaded through its own embed API
    with the viewer's permissions.
    """
    user = auth.current_user()
    if not service.may_use(user):
        abort(403)
    kind, ref = request.args.get("kind", ""), request.args.get("ref", "")
    if kind not in embeds.available_kinds():
        abort(404)
    try:
        validated = document.validate({"version": document.VERSION, "blocks": [
            {"type": "embed", "kind": kind, "ref": ref}]})
    except document.DocumentError:
        abort(404)
    response = current_app.make_response(render_template("page_builder/embed_frame.html",
                                                          body=document.render(validated)))
    response.headers["Cache-Control"] = "private, no-store"
    return response


@bp.post("/api/page-builder/clipboard")
def clipboard():
    """Validate blocks pasted from the clipboard (copied on this or another page) before inserting them."""
    user = _builder_user()
    rate_limit("preview", user, PREVIEWS_PER_MINUTE)
    validated = validated_document(json_payload(), complete=False)
    if not validated["blocks"]:
        raise Refused(400, "page_builder.error.clipboard_empty")
    return jsonify({"ok": True, "blocks": validated["blocks"]})


@bp.post("/api/page-builder/sections")
def create_section():
    user = _builder_user()
    if not service.may_manage_sections(user):
        raise Refused(403, "page_builder.error.sections_forbidden")
    data = json_payload()
    try:
        section = service.create_section(data.get("name"), data.get("document"), user)
    except (service.SectionError, document.DocumentError) as error:
        raise Refused(400, error.key) from None
    return jsonify({"ok": True, "section": section,
                    "delete_url": url_for("page_builder.delete_section", section_id=section["id"])})


@bp.delete("/api/page-builder/sections/<int:section_id>")
def delete_section(section_id: int):
    user = _builder_user()
    if not service.may_manage_sections(user):
        raise Refused(403, "page_builder.error.sections_forbidden")
    if not service.delete_section(section_id):
        raise Refused(404, "page_builder.error.section_missing")
    return jsonify({"ok": True})


# ── Settings ──────────────────────────────────────────────────────────────────


@bp.route("/admin/page-builder", methods=["GET", "POST"])
@auth.admin_required
def admin_settings():
    if request.method == "POST":
        level = request.form.get("page_builder_access")
        if level in ACCESS_LEVELS:
            settings.update({"page_builder_access": level})
            auth.flash_t("common.saved", "success")
        return redirect(url_for("page_builder.admin_settings"))
    current = settings.get("page_builder_access")
    return render_template("page_builder/settings.html", levels=ACCESS_LEVELS,
                           current=current if current in ROLES else "admin")
