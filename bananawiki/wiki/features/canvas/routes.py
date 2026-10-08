"""Canvas pages and JSON endpoints (same URLs as 1.4).

Every view loads the canvas by slug and checks the current user against
:mod:`.access` itself: views reachable anonymously in public mode are marked
``@auth.public_read`` and still refuse canvases the visitor may not read.
A canvas the user cannot read answers 404, so private canvases do not reveal
that they exist; a canvas they can read but not change answers 403.
"""

from __future__ import annotations

from typing import Any

from flask import (
    Response,
    abort,
    current_app,
    jsonify,
    make_response,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)

from ....core.web import client_ip
from ... import accounts, auth, settings
from ...i18n import t
from ...registry import feature_blueprint
from ..pages import service as pages
from . import access, model, present, presets, service, transfer

bp = feature_blueprint("canvas", "canvas", __name__, template_folder="templates", static_folder="static",
                       static_url_path="/static/canvas")

HISTORY_MESSAGES = {
    "created": "canvas.history.msg.created", "Created canvas": "canvas.history.msg.created",
    "edited": "canvas.history.msg.edited", "saved": "canvas.history.msg.saved",
    "Saved canvas snapshot": "canvas.history.msg.saved", "imported": "canvas.history.msg.imported",
    "Imported canvas": "canvas.history.msg.imported", "info": "canvas.history.msg.info",
    "Edited canvas info": "canvas.history.msg.info",
}


# ── Helpers ───────────────────────────────────────────────────────────────────


def _limit(bucket: str, limit: int, window: int = 60) -> None:
    user = auth.current_user()
    key = f"canvas:{bucket}:{user['id'] if user else client_ip()}"
    if not current_app.extensions["bananawiki.limiter"].hit(key, limit, window):
        if auth.wants_json():
            abort(make_response(jsonify({"error": t("canvas.error.rate_limited")}), 429))
        abort(429)


def _deny(status: int = 403) -> None:
    if auth.current_user() is None:
        abort(auth.redirect_to_login())
    if auth.wants_json():
        abort(make_response(jsonify({"error": t("canvas.error.forbidden" if status == 403 else "canvas.error.not_found")}),
                            status))
    abort(status)


def _layout(slug: str, need: str = "view") -> tuple[dict[str, Any], str]:
    """Load a canvas the current user may *need* (``view``, ``edit`` or ``own``)."""
    layout = service.get_by_slug(slug)
    user = auth.current_user()
    level = access.level(user, layout)
    if layout is None or level is None:
        _deny(404)
    if need == "edit" and level != "edit":
        _deny(403)
    if need == "own" and not access.is_owner(user, layout):
        _deny(403)
    return layout, level  # type: ignore[return-value]


def _session_id() -> str:
    return (request.headers.get("X-Canvas-Session") or "").strip()[:64]


def _json_body() -> dict[str, Any]:
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        abort(make_response(jsonify({"error": t("canvas.error.bad_request")}), 400))
    return payload


def _error_json(error: service.CanvasError | model.DocumentError, status: int = 400):
    return jsonify({"error": t(error.key, **error.values)}), status


def history_message(message: str | None) -> str:
    message = message or ""
    if message.startswith("reverted:"):
        return t("canvas.history.msg.reverted", entry=message.split(":", 1)[1])
    key = HISTORY_MESSAGES.get(message)
    return t(key) if key else message


@bp.app_template_global("canvas_history_message")
def _history_message_global(message: str | None) -> str:
    return history_message(message)


@bp.app_template_global("canvas_outline_label")
def outline_label(item: dict[str, Any]) -> str:
    """How the text outline names an element: its type, its name and its group."""
    label = t(f"canvas.node.{item['type']}")
    if item["name"]:
        label += f": {item['name']}"
    if item["group"]:
        label += f" ({t('canvas.outline.group', number=item['group'])})"
    return label


# ── List, create, import, order ───────────────────────────────────────────────


def _order_owner(user: dict[str, Any]) -> str | None:
    return None if access.open_access() else user["id"]


@bp.get("/canvas")
@auth.public_read
def index():
    user = auth.current_user()
    if not access.can_open_canvas(user):
        _deny(403)
    layouts = access.visible_layouts(user)
    if user is not None:
        layouts = service.apply_order(layouts, service.order_ids(_order_owner(user)))
    return render_template(
        "canvas/list.html", layouts=layouts, can_create=access.can_create(user),
        can_reorder=user is not None, shared_order=access.open_access(),
        order_version=service.order_version(), templates=presets.names(),
    )


@bp.post("/canvas/create")
def create():
    _limit("create", 10)
    user = auth.current_user()
    if not access.can_create(user):
        _deny(403)
    template = (request.form.get("template") or "").strip()
    data = presets.document(template) if template else None
    if template and data is None:
        auth.flash_t("canvas.error.bad_template", "error")
        return redirect(url_for("canvas.index"))
    try:
        layout = service.create(request.form.get("title"), request.form.get("description"), user["id"],
                                data=data)
    except service.CanvasError as error:
        auth.flash_t(error.key, "error", **error.values)
        return redirect(url_for("canvas.index"))
    auth.flash_t("canvas.flash.created", "success")
    return redirect(url_for("canvas.view", slug=layout["slug"]))


@bp.post("/canvas/import")
def import_canvas():
    _limit("import", 10)
    user = auth.current_user()
    if not access.can_create(user):
        _deny(403)
    try:
        title, description, doc, stored = transfer.read_import(request.files.get("import_file"))
    except (transfer.CanvasImportError, model.DocumentError) as error:
        auth.flash_t(error.key, "error", **error.values)
        return redirect(url_for("canvas.index"))
    try:
        layout = service.create(title, description, user["id"], data=doc, message="imported")
    except (service.CanvasError, model.DocumentError) as error:
        transfer.discard(stored)
        auth.flash_t(error.key, "error", **error.values)
        return redirect(url_for("canvas.index"))
    auth.flash_t("canvas.flash.imported", "success", title=layout["title"])
    return redirect(url_for("canvas.view", slug=layout["slug"]))


@bp.post("/api/canvas/layout-order")
def save_order():
    _limit("order", 30)
    user = auth.current_user()
    if not access.can_open_canvas(user):
        _deny(403)
    raw = _json_body().get("layout_ids")
    if not isinstance(raw, list) or len(raw) > 5000:
        return jsonify({"error": t("canvas.error.bad_request")}), 400
    visible = {layout["id"] for layout in access.visible_layouts(user)}
    ids: list[int] = []
    for item in raw:
        if isinstance(item, (int, str)) and not isinstance(item, bool) and str(item).isdigit():
            layout_id = int(item)
            if layout_id in visible and layout_id not in ids:
                ids.append(layout_id)
    owner = _order_owner(user)
    if owner is None:
        # The shared order also holds canvases this user cannot see: keep their places.
        ids += [layout_id for layout_id in service.order_ids(None) if layout_id not in ids]
    service.save_order(owner, ids)
    return jsonify({"ok": True, "list_order_version": service.order_version()})


@bp.get("/api/canvas/list-order-version")
def list_order_version():
    if not access.can_open_canvas(auth.current_user()):
        _deny(403)
    return jsonify({"list_order_version": service.order_version()})


# ── The canvas ────────────────────────────────────────────────────────────────


@bp.get("/canvas/<slug>")
@auth.public_read
def view(slug: str):
    layout, level = _layout(slug)
    user = auth.current_user()
    return render_template("canvas/view.html", layout=layout, can_edit=level == "edit",
                           is_owner=access.is_owner(user, layout))


@bp.get("/canvas/<slug>/data")
@auth.public_read
def get_data(slug: str):
    _limit("data", 120)
    layout, level = _layout(slug)
    shown = present.present_document(service.document(layout["id"]), auth.current_user())
    return jsonify({**shown, "version": layout["version"], "seq": service.head_seq(layout["id"]),
                    "can_edit": level == "edit"})


@bp.post("/canvas/<slug>/data")
def save_data(slug: str):
    _limit("save", 60)
    layout, _level = _layout(slug, "edit")
    payload = _json_body()
    expected = payload.get("expected_version")
    if expected is not None and (isinstance(expected, bool) or not isinstance(expected, int)):
        return jsonify({"error": t("canvas.error.bad_request")}), 400
    try:
        result = service.save_document(layout, payload.get("data"), expected_version=expected,
                                       user_id=auth.current_user()["id"], session_id=_session_id())
    except service.VersionConflict as conflict:
        return jsonify({"error": t(conflict.key), "version": conflict.current_version}), 409
    except (service.CanvasError, model.DocumentError) as error:
        return _error_json(error)
    return jsonify({"ok": True, **result})


@bp.post("/canvas/<slug>/ops")
def apply_ops(slug: str):
    _limit("ops", 240)
    layout, _level = _layout(slug, "edit")
    user = auth.current_user()
    try:
        result = service.apply(layout, _json_body().get("ops"), user_id=user["id"], session_id=_session_id(),
                               skip_locked=True)
    except (service.CanvasError, model.DocumentError) as error:
        return _error_json(error)
    rejected = result["rejected"]
    shown = present.present_nodes([op["node"] for op in result["applied"] if op["op"] == "upsert_node"], user)
    nodes = iter(shown["nodes"])
    applied = [{"op": op["op"], "node": next(nodes)} if op["op"] == "upsert_node" else op
               for op in result["applied"]]
    extra = {"rejected": rejected, "error": t("canvas.error.locked", count=len(rejected),
                                              ids=", ".join(item["id"] for item in rejected[:10]))} if rejected else {}
    return jsonify({"ok": True, "version": result["version"], "seq": result["seq"], "applied": applied,
                    "rendered": shown["rendered"], "pages": shown["pages"], **extra})


def _sync_response(layout: dict[str, Any]):
    try:
        since = max(0, int(request.args.get("since", "0")))
    except ValueError:
        since = 0
    result = service.events_since(layout["id"], since, exclude_session=_session_id())
    extra = present.present_events(result["events"], auth.current_user())
    version = service.get(layout["id"])["version"]
    return jsonify({**result, **extra, "version": version})


@bp.get("/canvas/<slug>/sync")
@auth.public_read
def sync(slug: str):
    _limit("sync", 240)
    layout, _level = _layout(slug)
    return _sync_response(layout)


@bp.post("/canvas/<slug>/render")
def render_node(slug: str):
    """Preview the HTML of a text or code node while it is being edited."""
    _limit("render", 120)
    _layout(slug, "edit")
    node = model.clean_node(dict(_json_body().get("node") or {}, id="preview"))
    if node is None:
        return jsonify({"error": t("canvas.error.bad_request")}), 400
    return jsonify(present.rendering(node) or {})


@bp.post("/canvas/<slug>/upload")
def upload(slug: str):
    """Store an image for an image node in the shared upload folder."""
    _limit("upload", 30)
    from ... import storage

    _layout(slug, "edit")
    try:
        stored = storage.save(request.files.get("file"), "uploads", allowed=None, images_only=True,
                              max_bytes=storage.max_upload_bytes(current_app.config["BW"].max_attachment_size))
    except storage.UploadError as error:
        return jsonify({"error": t(error.key, **error.values)}), 400
    return jsonify({"ok": True, "url": url_for("uploaded_file", filename=stored.filename)})


@bp.get("/canvas/<slug>/pages")
def page_search(slug: str):
    """Pages the editor may link to, for the wiki-page node picker."""
    _limit("pages", 120)
    _layout(slug, "edit")
    query = (request.args.get("q") or "").strip()
    results = pages.search(query, limit=15, titles_only=True) if query else []
    return jsonify({"pages": [{"id": row["id"], "title": row["title"], "slug": row["slug"]} for row in results]})


@bp.get("/canvas/<slug>/export")
def export(slug: str):
    _limit("export", 10)
    layout, _level = _layout(slug, "edit")
    doc = present.redacted_document(service.document(layout["id"]), auth.current_user())
    file, mimetype, filename = transfer.build_export(layout, doc, plain=request.args.get("format") == "json")
    return send_file(file, mimetype=mimetype, as_attachment=True, download_name=filename, max_age=0)


@bp.get("/canvas/<slug>/outline")
@auth.public_read
def outline(slug: str):
    """The canvas as text: every element in reading order with its connections."""
    _limit("outline", 60)
    layout, _level = _layout(slug)
    items = present.outline(service.document(layout["id"]), auth.current_user())
    if request.args.get("format") == "md":
        body = transfer.outline_markdown(layout, items, outline_label)
        response = Response(body, mimetype="text/markdown")
        response.headers["Content-Disposition"] = f'attachment; filename="{layout["slug"]}.md"'
        return response
    return render_template("canvas/outline.html", layout=layout, items=items,
                           by_id={item["id"]: item for item in items})


# ── Settings, sharing, deletion ───────────────────────────────────────────────


@bp.get("/canvas/<slug>/settings")
def manage(slug: str):
    layout, level = _layout(slug, "edit")
    owner = access.is_owner(auth.current_user(), layout)
    return render_template(
        "canvas/manage.html", layout=layout, is_owner=owner,
        permissions=service.permissions(layout["id"]) if owner else [],
        roles=access.shareable_roles(), visibilities=access.VISIBILITIES,
    )


@bp.post("/canvas/<slug>/edit")
def edit_info(slug: str):
    _limit("edit", 20)
    layout, _level = _layout(slug, "edit")
    try:
        service.update_info(layout, request.form.get("title"), request.form.get("description"),
                            user_id=auth.current_user()["id"])
    except service.CanvasError as error:
        auth.flash_t(error.key, "error", **error.values)
    else:
        auth.flash_t("canvas.flash.updated", "success")
    return redirect(url_for("canvas.manage", slug=layout["slug"]))


@bp.post("/canvas/<slug>/delete")
def delete(slug: str):
    _limit("delete", 10)
    layout, _level = _layout(slug, "own")
    service.delete(layout, actor_id=auth.current_user()["id"])
    auth.flash_t("canvas.flash.deleted", "success")
    return redirect(url_for("canvas.index"))


def _share_target_user() -> dict[str, Any] | None:
    value = (request.form.get("new_owner") or request.form.get("username") or "").strip()
    if value:
        return accounts.by_username(value)
    return accounts.by_id((request.form.get("user_id") or "").strip() or None)


@bp.post("/canvas/<slug>/share")
def share(slug: str):
    """Visibility, per-user and per-role permissions and ownership (1.4 ``action`` values)."""
    _limit("share", 30)
    layout, _level = _layout(slug, "own")
    action = request.form.get("action", "")
    permission = request.form.get("permission", "view")
    back = redirect(url_for("canvas.manage", slug=layout["slug"]))
    if action in ("add_user", "transfer_ownership"):
        target = _share_target_user()
        if target is None:
            auth.flash_t("canvas.error.user_not_found", "error")
            return back
        if action == "transfer_ownership":
            if target["id"] == layout["creator_id"]:
                return back
            service.transfer(layout, target["id"])
            auth.flash_t("canvas.flash.transferred", "success", username=target["username"])
            still_owner = access.is_owner(auth.current_user(), service.get(layout["id"]))
            return back if still_owner else redirect(url_for("canvas.index"))
        if target["id"] == layout["creator_id"] or permission not in access.PERMISSIONS:
            auth.flash_t("canvas.error.bad_request", "error")
            return back
        service.set_permission(layout["id"], permission, user_id=target["id"])
        auth.flash_t("canvas.flash.shared", "success")
    elif action == "add_role":
        role = request.form.get("role", "")
        if role not in access.shareable_roles() or permission not in access.PERMISSIONS:
            auth.flash_t("canvas.error.bad_role", "error")
            return back
        service.set_permission(layout["id"], permission, role=role)
        auth.flash_t("canvas.flash.shared", "success")
    elif action == "remove_user":
        service.remove_permission(layout["id"], user_id=(request.form.get("user_id") or "").strip() or None)
        auth.flash_t("canvas.flash.unshared", "success")
    elif action == "remove_role":
        service.remove_permission(layout["id"], role=(request.form.get("role") or "").strip() or None)
        auth.flash_t("canvas.flash.unshared", "success")
    elif action == "set_visibility":
        try:
            service.set_visibility(layout, request.form.get("visibility", ""))
        except service.CanvasError as error:
            auth.flash_t(error.key, "error")
            return back
        auth.flash_t("canvas.flash.visibility", "success")
    else:
        auth.flash_t("canvas.error.bad_request", "error")
    return back


# ── History ───────────────────────────────────────────────────────────────────


@bp.get("/canvas/<slug>/history")
@auth.public_read
def history(slug: str):
    layout, level = _layout(slug)
    return render_template("canvas/history.html", layout=layout, entries=service.history(layout["id"]),
                           can_revert=level == "edit", is_admin=access.is_admin(auth.current_user()))


@bp.get("/canvas/<slug>/history/<int:entry_id>")
@auth.public_read
def history_entry(slug: str, entry_id: int):
    layout, level = _layout(slug)
    entry = service.history_entry(layout, entry_id)
    if entry is None:
        abort(404)
    shown = present.present_document(model.clean_document(entry["data"]), auth.current_user())
    return render_template("canvas/history_entry.html", layout=layout, entry=entry, snapshot=shown,
                           can_revert=level == "edit", is_admin=access.is_admin(auth.current_user()))


@bp.post("/canvas/<slug>/revert/<int:entry_id>")
def revert(slug: str, entry_id: int):
    _limit("revert", 10)
    layout, _level = _layout(slug, "edit")
    entry = service.history_entry(layout, entry_id)
    if entry is None:
        abort(404)
    try:
        service.revert(layout, entry, user_id=auth.current_user()["id"], session_id=_session_id())
    except model.DocumentError as error:
        auth.flash_t(error.key, "error", **error.values)
        return redirect(url_for("canvas.history", slug=layout["slug"]))
    auth.flash_t("canvas.flash.reverted", "success")
    return redirect(url_for("canvas.view", slug=layout["slug"]))


@bp.post("/canvas/<slug>/history/<int:entry_id>/delete")
def delete_history_entry(slug: str, entry_id: int):
    layout, _level = _layout(slug)
    if not access.is_admin(auth.current_user()):
        _deny(403)
    if not service.delete_history_entry(layout, entry_id):
        abort(404)
    auth.flash_t("canvas.flash.history_entry_deleted", "success")
    return redirect(url_for("canvas.history", slug=layout["slug"]))


@bp.post("/canvas/<slug>/history/clear")
def clear_history(slug: str):
    layout, _level = _layout(slug)
    if not access.is_admin(auth.current_user()):
        _deny(403)
    service.clear_history(layout)
    auth.flash_t("canvas.flash.history_cleared", "success")
    return redirect(url_for("canvas.history", slug=layout["slug"]))


# ── Embeds in wiki pages ──────────────────────────────────────────────────────


@bp.get("/api/embed/canvas/<slug>")
@auth.public_read
def embed_data(slug: str):
    _limit("embed", 120)
    layout, _level = _layout(slug)
    shown = present.present_document(service.document(layout["id"]), auth.current_user())
    return jsonify({**shown, "slug": layout["slug"], "title": layout["title"],
                    "url": url_for("canvas.view", slug=layout["slug"]), "version": layout["version"],
                    "seq": service.head_seq(layout["id"])})


@bp.get("/api/embed/canvas/<slug>/sync")
@auth.public_read
def embed_sync(slug: str):
    _limit("embed-sync", 120)
    layout, _level = _layout(slug)
    return _sync_response(layout)


# ── Site settings ─────────────────────────────────────────────────────────────

SETTING_FIELDS = ("canvas_access", "canvas_write_access")
SETTING_SWITCHES = ("canvas_public_access_enabled", "canvas_open_access")


@bp.route("/admin/canvas", methods=["GET", "POST"])
@auth.admin_required
def admin_settings():
    if request.method == "POST":
        values: dict[str, Any] = {}
        for name in SETTING_FIELDS:
            value = request.form.get(name, "admin")
            values[name] = value if value in access.ACCESS_SETTING_VALUES else "admin"
        for name in SETTING_SWITCHES:
            values[name] = 1 if request.form.get(name) else 0
        settings.update(values)
        auth.flash_t("canvas.flash.settings_saved", "success")
        return redirect(url_for("canvas.admin_settings"))
    return render_template("canvas/admin_settings.html", current=settings.load(),
                           choices=access.ACCESS_SETTING_VALUES)
