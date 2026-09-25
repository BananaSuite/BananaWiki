"""
BananaWiki: Canvas layout routes.

Provides canvas listing, view/edit, JSON data API, export, sharing,
and CRUD endpoints for the visual knowledge canvas feature.
"""

import io
import json
import os
import re
import functools
import tempfile
import uuid
import zipfile
import zlib

from flask import (
    render_template, request, redirect, url_for, flash, jsonify, abort,
    Response,
)
from PIL import Image

import config
import db
from db._canvas import sanitize_layout_data
from helpers import (
    get_current_user, rate_limit, allowed_file, _safe_ext, is_public_mode_active,
    render_markdown, get_effective_max_upload_size,
)
from helpers import t  # noqa: F401  (i18n)
from helpers._storage_quota import invalidate_storage_usage_cache, mutation_would_exceed_quota
from wiki_logger import log_action

# Maximum serialised size (bytes) for canvas data stored in the DB.
_MAX_CANVAS_DATA_BYTES = 5 * 1024 * 1024  # 5 MB

# Cap on the canvas.json of an import.  Exports are pretty-printed, so the
# file can be larger than the stored blob; the parsed data is still held to
# _MAX_CANVAS_DATA_BYTES before anything is saved.
_MAX_CANVAS_IMPORT_JSON_BYTES = 20 * 1024 * 1024  # 20 MB

_IMPORT_COPY_CHUNK = 64 * 1024

# Match canvas node URLs that point at a local upload so they can be bundled.
_LOCAL_UPLOAD_RE = re.compile(r"^/static/uploads/([A-Za-z0-9_\-.]+)$")


def _collect_local_upload_filenames(canvas_data):
    """Walk a canvas data dict and return the set of local upload filenames
    referenced by its nodes (image / video URL fields).
    """
    out = set()
    if not isinstance(canvas_data, dict):
        return out
    nodes = canvas_data.get("nodes") or []
    if not isinstance(nodes, list):
        return out
    for n in nodes:
        if not isinstance(n, dict):
            continue
        url = n.get("url") or ""
        if not isinstance(url, str):
            continue
        m = _LOCAL_UPLOAD_RE.match(url.strip())
        if m:
            out.add(m.group(1))
    return out


def _rewrite_canvas_upload_urls(canvas_data, name_map):
    """In-place rewrite of canvas node URLs whose files were stored under a
    new name during import (bundled images always get a UUID name).
    """
    if not isinstance(canvas_data, dict) or not name_map:
        return
    for n in canvas_data.get("nodes") or []:
        if not isinstance(n, dict):
            continue
        url = n.get("url") or ""
        if not isinstance(url, str):
            continue
        m = _LOCAL_UPLOAD_RE.match(url.strip())
        if not m:
            continue
        old = m.group(1)
        if old in name_map:
            n["url"] = "/static/uploads/" + name_map[old]


def _load_layout_data(layout):
    """Return the sanitised canvas document stored on *layout*."""
    try:
        data = json.loads(layout["data"])
    except (json.JSONDecodeError, TypeError):
        data = None
    return sanitize_layout_data(data)


class _CanvasImportError(Exception):
    """An import was refused; the message is flashed to the user."""


def _import_too_large_error(limit_bytes):
    """Return the error for an import larger than *limit_bytes*."""
    max_mb = max(1, limit_bytes // (1024 * 1024))
    return _CanvasImportError(t("flash.this_upload_is_too_large_the_maximum_allowed", max_mb=max_mb))


def _read_zip_member(z, info, limit):
    """Read one zip member, refusing it once it grows past *limit* bytes.

    The size in the zip header is written by whoever built the file, so the
    bytes are counted while they are decompressed.
    """
    parts = []
    size = 0
    with z.open(info) as src:
        while True:
            chunk = src.read(_IMPORT_COPY_CHUNK)
            if not chunk:
                break
            size += len(chunk)
            if size > limit:
                raise _import_too_large_error(limit)
            parts.append(chunk)
    return b"".join(parts)


def _read_canvas_bundle(z):
    """Return ``(export_obj, asset_members)`` from an open ``.canvas.zip``.

    *asset_members* maps each ``assets/<name>`` file name to its ZipInfo.
    Nothing is extracted to disk here.
    """
    json_member = None
    assets = {}
    for info in z.infolist():
        if info.is_dir():
            continue
        # Reject path traversal and absolute-path entries
        norm = info.filename.replace("\\", "/")
        if norm.startswith("/") or ".." in norm.split("/"):
            raise _CanvasImportError(t("flash.invalid_json_file"))
        if norm == "canvas.json":
            json_member = info
        elif norm.startswith("assets/") and "/" not in norm[len("assets/"):]:
            assets[norm[len("assets/"):]] = info
    if json_member is None:
        raise _CanvasImportError(t("flash.invalid_json_file"))
    raw = _read_zip_member(z, json_member, _MAX_CANVAS_IMPORT_JSON_BYTES)
    try:
        return json.loads(raw), assets
    except (ValueError, TypeError) as exc:
        raise _CanvasImportError(t("flash.invalid_json_file")) from exc


def _is_valid_image_file(path):
    """Return True if Pillow can open and verify the image at *path*."""
    try:
        with Image.open(path) as img:
            img.verify()
    except Exception:  # noqa: BLE001 - any Pillow failure means "not an image"
        return False
    return True


def _restore_bundle_assets(z, members, user, settings):
    """Write the bundled images a canvas uses into the upload folder.

    *members* maps a file name referenced by the canvas to its ZipInfo.
    Each image is held to the same rules as ``/api/upload``: an image
    extension, a Pillow check, a UUID file name and the per-user upload
    quota.  All of them together may not exceed the admin's maximum upload
    size or the managed storage limit.  The files are streamed to temporary
    names first and only moved into place once every check has passed, so a
    refused import leaves nothing behind.

    Returns ``{original_name: stored_name}`` for rewriting node URLs.
    """
    asset_limit = get_effective_max_upload_size(settings)
    if sum(info.file_size for info in members.values()) > asset_limit:
        raise _import_too_large_error(asset_limit)

    upload_root = os.path.abspath(config.UPLOAD_FOLDER)
    os.makedirs(upload_root, exist_ok=True)
    staged = []  # (original_name, temp_path, size) for every checked image
    leftovers = []  # every file written so far, removed again on failure
    total = 0
    try:
        for name, info in sorted(members.items()):
            # Dot-prefixed temp names are skipped by cleanup_unused_uploads.
            fd, temp_path = tempfile.mkstemp(prefix=".canvas-import-", dir=upload_root)
            leftovers.append(temp_path)
            size = 0
            with os.fdopen(fd, "wb") as out, z.open(info) as src:
                while True:
                    chunk = src.read(_IMPORT_COPY_CHUNK)
                    if not chunk:
                        break
                    size += len(chunk)
                    if total + size > asset_limit:
                        raise _import_too_large_error(asset_limit)
                    out.write(chunk)
            total += size
            if not _is_valid_image_file(temp_path):
                raise _CanvasImportError(t("error.file_not_valid_image"))
            staged.append((name, temp_path, size))

        exceeded, _used, _limit = mutation_would_exceed_quota(total)
        if exceeded:
            raise _CanvasImportError(t(
                "flash.storage_limit_reached",
                default="This wiki has reached its storage limit. "
                        "Delete files or contact the hosting administrator.",
            ))
        for _name, _temp_path, size in staged:
            ok, error = db.check_and_record_upload(user["id"], size, settings=settings)
            if not ok:
                raise _CanvasImportError(error)

        name_map = {}
        for name, temp_path, _size in staged:
            stored_name = f"{uuid.uuid4().hex}.{_safe_ext(name)}"
            stored_path = os.path.join(upload_root, stored_name)
            os.replace(temp_path, stored_path)
            leftovers.remove(temp_path)
            leftovers.append(stored_path)
            name_map[name] = stored_name
        leftovers = []
        return name_map
    finally:
        for path in leftovers:
            try:
                os.remove(path)
            except OSError:
                pass
        if total:
            invalidate_storage_usage_cache()


def _discard_restored_uploads(name_map):
    """Remove images written by an import that was refused afterwards."""
    upload_root = os.path.abspath(config.UPLOAD_FOLDER)
    for stored_name in name_map.values():
        try:
            os.remove(os.path.join(upload_root, stored_name))
        except OSError:
            pass


def _parse_canvas_export(data):
    """Return ``(title, description, canvas_data)`` from an export object.

    A missing or malformed ``data`` member becomes an empty canvas, as it
    always has; the node fields are filtered later by the save.
    """
    if not isinstance(data, dict):
        raise _CanvasImportError(t("flash.invalid_json_file"))
    title = data.get("title")
    if not isinstance(title, str) or not title.strip():
        title = "Imported Canvas"
    description = data.get("description")
    if not isinstance(description, str):
        description = ""
    canvas_data = data.get("data")
    if not isinstance(canvas_data, dict) or "nodes" not in canvas_data or "edges" not in canvas_data:
        canvas_data = {"nodes": [], "edges": [], "viewport": {"x": 0, "y": 0, "zoom": 1}}
    return title.strip()[:200], description.strip()[:2000], canvas_data


def _import_canvas_file(stream, user, settings):
    """Read an uploaded canvas export and restore the images it bundles.

    Returns ``(title, description, canvas_data, name_map)``.  The upload
    stays in Werkzeug's spooled file: only canvas.json is read into memory,
    under its own cap, and bundled images are streamed to disk by
    ``_restore_bundle_assets``.  Only images that a node of the imported
    canvas points at are restored; anything else in ``assets/`` is ignored.
    """
    asset_limit = get_effective_max_upload_size(settings)
    stream.seek(0, os.SEEK_END)
    upload_size = stream.tell()
    stream.seek(0)
    if upload_size > _MAX_CANVAS_IMPORT_JSON_BYTES + asset_limit:
        raise _import_too_large_error(_MAX_CANVAS_IMPORT_JSON_BYTES + asset_limit)

    if stream.read(4) != b"PK\x03\x04":
        # Plain .canvas.json (the original export format).
        stream.seek(0)
        raw = stream.read(_MAX_CANVAS_IMPORT_JSON_BYTES + 1)
        if len(raw) > _MAX_CANVAS_IMPORT_JSON_BYTES:
            raise _import_too_large_error(_MAX_CANVAS_IMPORT_JSON_BYTES)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, TypeError, UnicodeDecodeError) as exc:
            raise _CanvasImportError(t("flash.invalid_json_file")) from exc
        title, description, canvas_data = _parse_canvas_export(data)
        return title, description, canvas_data, {}

    stream.seek(0)
    with zipfile.ZipFile(stream) as z:
        data, asset_members = _read_canvas_bundle(z)
        title, description, canvas_data = _parse_canvas_export(data)
        if len(json.dumps(canvas_data).encode("utf-8")) > _MAX_CANVAS_DATA_BYTES:
            raise _CanvasImportError(t("flash.canvas_data_exceeds_the_5_mb_size_limit"))
        referenced = _collect_local_upload_filenames(canvas_data)
        wanted = {
            name: info for name, info in asset_members.items()
            if name in referenced and allowed_file(name)
        }
        name_map = _restore_bundle_assets(z, wanted, user, settings) if wanted else {}
    _rewrite_canvas_upload_urls(canvas_data, name_map)
    return title, description, canvas_data, name_map


def _has_global_canvas_access(user, settings):
    """Return True if the user has global canvas access based on site settings."""
    if not user:
        return False
    role = user["role"]
    if role in ("admin", "owner"):
        return True
    if settings and settings.get("canvas_open_access"):
        return True
    access = "admin"
    if settings and "canvas_access" in settings.keys():
        access = settings["canvas_access"] or "admin"
    if access == "editor" and role == "editor":
        return True
    if access == "all":
        return True
    return False


def _can_write_global(user, settings):
    """Return True if the user has global write access to canvas layouts."""
    role = user["role"]
    if role in ("admin", "owner"):
        return True
    if settings and settings.get("canvas_open_access"):
        return True
    write_access = "admin"
    if settings and "canvas_write_access" in settings.keys():
        write_access = settings["canvas_write_access"] or "admin"
    if write_access == "editor" and role == "editor":
        return True
    if write_access == "all":
        return True
    return False


def _get_canvas_access(settings):
    """Return the current global ``canvas_access`` value from site settings."""
    if settings and "canvas_access" in settings.keys():
        return settings["canvas_access"] or "admin"
    return "admin"


def _get_canvas_write_access(settings):
    """Return the current global ``canvas_write_access`` value from site settings."""
    if settings and "canvas_write_access" in settings.keys():
        return settings["canvas_write_access"] or "admin"
    return "admin"


def _canvas_access_required(f):
    """Decorator that checks the user has canvas access.

    Users who do not meet the global access level but have been individually
    shared on at least one canvas are also allowed through so they can
    access their assigned canvases.
    """
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        """Resolve the canvas and confirm the caller may reach it."""
        user = get_current_user()
        if not user:
            settings = db.get_site_settings()
            if (
                is_public_mode_active()
                and settings
                and settings.get("canvas_public_access_enabled")
                and request.method in ("GET", "HEAD", "OPTIONS")
            ):
                return f(*args, **kwargs)
            return redirect(url_for("login"))

        settings = db.get_site_settings()

        # Primary permission check (honors plugin state)
        if not db.has_permission(user, "canvas.view"):
            abort(404)

        if _has_global_canvas_access(user, settings):
            return f(*args, **kwargs)
        # Allow individually invited users through even if their role is
        # not globally permitted.
        if db.canvas_user_has_any_permission(user["id"]):
            return f(*args, **kwargs)
        flash(t("flash.you_do_not_have_the_required_permissions_to_f6bb9a"), "error")
        abort(403)
    return wrapper


def _canvas_record_history(layout_id, edit_message, user=None, is_revert=False):
    """Record a canvas-layout history snapshot.

    Failures are swallowed so the history machinery never breaks a
    write path.  Snapshots dedupe against the previous entry so no-op
    saves do not pollute the history.
    """
    if not layout_id:
        return
    try:
        user_id = None
        if user is not None:
            try:
                user_id = user["id"]
            except (KeyError, TypeError):
                user_id = getattr(user, "id", None)
        db.canvas_record_layout_history(
            layout_id,
            edited_by=user_id,
            edit_message=edit_message or "",
            is_revert=is_revert,
        )
    except Exception as exc:  # noqa: BLE001
        from wiki_logger import get_logger
        get_logger().warning("Canvas history record failed: %s", exc, exc_info=True)


def register_canvas_routes(app):
    """Register canvas layout routes on *app*."""

    @app.route("/canvas")
    @_canvas_access_required
    def canvas_list():
        """List all canvas layouts the user can view."""
        user = get_current_user()
        settings = db.get_site_settings()
        open_access = bool(settings and settings.get("canvas_open_access"))
        if user:
            layouts = db.canvas_list_layouts_for_user(user, open_access=open_access)
            can_write = _can_write_global(user, settings)
        else:
            layouts = db.canvas_list_public_layouts()
            can_write = False
        # Apply per-user or global ordering
        order_key = None if open_access else (user["id"] if user else None)
        ordered_ids = db.canvas_get_user_layout_order(order_key)
        if ordered_ids:
            layout_map = {l["id"]: l for l in layouts}
            ordered = [layout_map[lid] for lid in ordered_ids if lid in layout_map]
            seen = set(ordered_ids)
            for l in layouts:
                if l["id"] not in seen:
                    ordered.append(l)
            layouts = ordered
        list_order_version = (settings or {}).get("list_order_version", 0)
        return render_template(
            "canvas/list.html",
            layouts=layouts,
            can_write=can_write,
            open_access=open_access,
            list_order_version=list_order_version,
        )

    @app.route("/api/canvas/layout-order", methods=["POST"])
    @_canvas_access_required
    @rate_limit()
    def api_canvas_layout_order():
        """Save the new layout ordering for the current user (or globally
        when canvas_open_access is enabled)."""
        user = get_current_user()
        if not user:
            return jsonify({"error": "Not logged in"}), 401
        settings = db.get_site_settings()
        data = request.get_json(silent=True) or {}
        layout_ids = data.get("layout_ids")
        if not isinstance(layout_ids, list):
            return jsonify({"error": "layout_ids must be a list"}), 400
        open_access = bool(settings and settings.get("canvas_open_access"))
        order_key = None if open_access else user["id"]
        db.canvas_save_user_layout_order(order_key, layout_ids)
        if open_access:
            v = (settings.get("list_order_version") or 0) + 1
            db.update_site_settings(list_order_version=v)
        else:
            v = settings.get("list_order_version") or 0
        return jsonify({"ok": True, "list_order_version": v})

    @app.route("/api/canvas/list-order-version")
    @_canvas_access_required
    @rate_limit(120, 60)
    def api_canvas_list_order_version():
        """Return the canvas list_order_version so clients can detect reorders."""
        settings = db.get_site_settings()
        v = (settings or {}).get("list_order_version", 0)
        return jsonify({"list_order_version": v})

    @app.route("/canvas/create", methods=["POST"])
    @_canvas_access_required
    @rate_limit(10, 60)
    def canvas_create():
        """Create a new canvas layout."""
        user = get_current_user()
        settings = db.get_site_settings()
        if not user:
            return redirect(url_for("login"))
        if not _can_write_global(user, settings):
            flash(t("flash.you_do_not_have_the_required_permissions_to_2376ce"), "error")
            abort(403)
        title = (request.form.get("title") or "").strip()
        if not title:
            flash(t("flash.canvas_title_is_required_to_continue"), "error")
            return redirect(url_for("canvas_list"))
        if len(title) > 200:
            flash(t("flash.canvas_title_cannot_exceed_200_characters"), "error")
            return redirect(url_for("canvas_list"))
        description = (request.form.get("description") or "").strip()
        if len(description) > 2000:
            flash(t("flash.description_cannot_exceed_2000_characters"), "error")
            return redirect(url_for("canvas_list"))
        layout_id = db.canvas_create_layout(
            title=title,
            creator_id=user["id"],
            description=description,
        )
        layout = db.canvas_get_layout(layout_id)
        _canvas_record_history(layout_id, "Created canvas", user=user)
        log_action("canvas_create", request, user=user, canvas_id=layout_id, title=title)
        flash(t("flash.canvas_has_been_successfully_created"), "success")
        return redirect(url_for("canvas_view", slug=layout["slug"]))

    @app.route("/canvas/<slug>")
    @_canvas_access_required
    def canvas_view(slug):
        """View a canvas layout."""
        user = get_current_user()
        layout = db.canvas_get_layout_by_slug(slug)
        if not layout:
            abort(404)
        settings = db.get_site_settings()
        open_access = bool(settings and settings.get("canvas_open_access"))
        if not db.canvas_user_can_view(layout["id"], user, open_access=open_access):
            flash(t("flash.you_do_not_have_the_required_permissions_to_f7261f"), "error")
            abort(403)
        can_edit = db.canvas_user_can_edit(layout["id"], user, open_access=open_access) if user else False
        is_creator = bool(user and layout["creator_id"] == user["id"])
        is_admin = bool(user and user["role"] in ("admin", "owner"))
        canvas_access = _get_canvas_access(settings)
        canvas_write_access = _get_canvas_write_access(settings)
        permissions = []
        if is_creator or is_admin:
            permissions = db.canvas_get_permissions(layout["id"])
        users_list = db.list_users() if (is_creator or is_admin) else []
        return render_template(
            "canvas/view.html",
            layout=layout,
            can_edit=can_edit,
            is_creator=is_creator,
            is_admin=is_admin,
            permissions=permissions,
            users_list=users_list,
            canvas_access=canvas_access,
            canvas_write_access=canvas_write_access,
        )

    @app.route("/canvas/<slug>/data")
    @_canvas_access_required
    @rate_limit(60, 60)
    def canvas_get_data(slug):
        """Return canvas JSON data."""
        user = get_current_user()
        layout = db.canvas_get_layout_by_slug(slug)
        if not layout:
            abort(404)
        settings = db.get_site_settings()
        open_access = bool(settings and settings.get("canvas_open_access"))
        if not db.canvas_user_can_view(layout["id"], user, open_access=open_access):
            abort(403)
        # Sanitised on the way out as well, so a layout saved before
        # whole-document writes were filtered cannot hand stored HTML to a
        # viewer that still runs an older copy of the canvas script.
        data = _load_layout_data(layout)
        # ``seq`` is the latest realtime event the server has for this layout;
        # the client uses it as the resume point for ``/sync`` polling.
        seq = db.canvas_latest_event_seq(layout["id"])
        return jsonify({
            "data": data,
            "version": layout["version"],
            "seq": seq,
        })

    @app.route("/canvas/<slug>/data", methods=["POST"])
    @_canvas_access_required
    @rate_limit(60, 60)
    def canvas_save_data(slug):
        """Save canvas JSON data."""
        user = get_current_user()
        layout = db.canvas_get_layout_by_slug(slug)
        if not layout:
            abort(404)
        settings = db.get_site_settings()
        open_access = bool(settings and settings.get("canvas_open_access"))
        if not db.canvas_user_can_edit(layout["id"], user, open_access=open_access):
            abort(403)
        try:
            payload = request.get_json(force=True)
            data = payload.get("data")
            if data is None:
                return jsonify({"error": "Missing data field"}), 400
        except Exception:
            return jsonify({"error": "Invalid JSON"}), 400
        # Basic validation of data structure
        if not isinstance(data, dict):
            return jsonify({"error": "Data must be a JSON object"}), 400
        if "nodes" not in data or "edges" not in data:
            return jsonify({"error": "Data must contain nodes and edges"}), 400
        # Cap the serialised size to prevent DoS via huge JSON blobs
        data_json = json.dumps(data)
        if len(data_json.encode("utf-8")) > _MAX_CANVAS_DATA_BYTES:
            return jsonify({"error": "Canvas data exceeds the 5 MB size limit."}), 400
        # Stored through sanitize_layout_data, which keeps the same node
        # fields as /ops and drops anything else the client sent.
        db.canvas_save_layout_data(layout["id"], data)
        _canvas_record_history(layout["id"], "Saved canvas snapshot", user=user)
        # Tell other collaborators that the entire blob was replaced.  Their
        # incremental queues are stale, so they need to refetch ``/data``.
        session_id = (request.headers.get("X-Canvas-Session") or "").strip()[:64]
        seq = db.canvas_append_snapshot_event(
            layout["id"], by_user_id=user["id"], by_session=session_id,
        )
        updated = db.canvas_get_layout(layout["id"])
        return jsonify({
            "ok": True,
            "version": updated["version"],
            "seq": seq,
        })

    # The next two endpoints are the incremental path used by editors that
    # already have the canvas open: /ops applies a batch of deltas and /sync
    # returns everything newer than the client's last seq.  The whole-document
    # POST to /data above stays as the fallback for a client that has lost
    # track of seq or is saving after a reconnect.
    @app.route("/canvas/<slug>/ops", methods=["POST"])
    @_canvas_access_required
    @rate_limit(180, 60)
    def canvas_ops(slug):
        """Apply a batch of fine-grained operations to a canvas.

        The request body is ``{"ops": [...]}`` where each op is one of::

            {"type": "upsert_node",   "node":  {...}}
            {"type": "delete_node",   "id":    "..."}
            {"type": "upsert_edge",   "edge":  {...}}
            {"type": "delete_edge",   "id":    "..."}
            {"type": "viewport_set",  "viewport": {"x": .., "y": .., "zoom": ..}}

        The server applies them atomically to the canonical canvas blob and
        appends one event per op so other sessions can pull them via
        ``/sync``.  The response includes ``seq`` (the highest event id
        produced by this request) so the client can advance its cursor.
        """
        user = get_current_user()
        layout = db.canvas_get_layout_by_slug(slug)
        if not layout:
            abort(404)
        settings = db.get_site_settings()
        open_access = bool(settings and settings.get("canvas_open_access"))
        if not db.canvas_user_can_edit(layout["id"], user, open_access=open_access):
            abort(403)
        try:
            payload = request.get_json(force=True) or {}
        except Exception:
            return jsonify({"error": "Invalid JSON"}), 400
        ops = payload.get("ops")
        if not isinstance(ops, list):
            return jsonify({"error": "ops must be a list"}), 400
        if len(ops) > 200:
            return jsonify({"error": "Too many ops in one request (max 200)"}), 400
        session_id = (request.headers.get("X-Canvas-Session") or "").strip()[:64]
        try:
            seq, applied = db.canvas_apply_ops(
                layout["id"], ops,
                by_user_id=user["id"],
                by_session=session_id,
            )
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        # Background-friendly: keep the log small to avoid unbounded growth.
        try:
            db.canvas_prune_events(layout["id"])
        except Exception as exc:  # noqa: BLE001
            from wiki_logger import get_logger
            get_logger().warning("Canvas event pruning failed: %s", exc, exc_info=True)
        # History snapshots are recorded by the bulk ``/data`` save and by
        # explicit board mutations.  Recording on every collaborative op is
        # far too noisy (each tiny drag = a new revision), so we skip it
        # here and rely on the next full save to capture the post-op state.
        return jsonify({
            "ok": True,
            "seq": seq,
            "applied": len(applied or []),
        })

    @app.route("/canvas/<slug>/sync")
    @_canvas_access_required
    @rate_limit(240, 60)
    def canvas_sync(slug):
        """Return all events with ``seq > since`` for the given canvas.

        ``X-Canvas-Session`` is echoed back as the caller's own session
        identifier so the client can ignore its own writes when applying
        remote events.  The server also accepts ``?since=`` so plain
        ``fetch()`` GETs work without custom headers.
        """
        user = get_current_user()
        layout = db.canvas_get_layout_by_slug(slug)
        if not layout:
            abort(404)
        settings = db.get_site_settings()
        open_access = bool(settings and settings.get("canvas_open_access"))
        if not db.canvas_user_can_view(layout["id"], user, open_access=open_access):
            abort(403)
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

    @app.route("/canvas/<slug>/export")
    @_canvas_access_required
    @rate_limit(10, 60, exempt_html_nav=False)
    def canvas_export(slug):
        """Export canvas.  When the canvas references local uploaded assets,
        return a ``.canvas.zip`` bundle containing both ``canvas.json`` and the
        referenced files under ``assets/``.  When no local uploads are
        referenced, return the legacy ``.canvas.json`` so existing import flows
        continue to work unchanged.
        """
        user = get_current_user()
        layout = db.canvas_get_layout_by_slug(slug)
        if not layout:
            abort(404)
        settings = db.get_site_settings()
        open_access = bool(settings and settings.get("canvas_open_access"))
        if not db.canvas_user_can_edit(layout["id"], user, open_access=open_access):
            flash(t("flash.you_do_not_have_the_required_permissions_to_d690ec"), "error")
            abort(403)
        data = _load_layout_data(layout)
        export_obj = {
            "title": layout["title"],
            "description": layout["description"] or "",
            "version": layout["version"],
            "data": data,
        }
        upload_names = _collect_local_upload_filenames(data)
        # When the user explicitly asks for plain JSON, honor that.
        plain = request.args.get("format") == "json"
        if plain or not upload_names:
            json_str = json.dumps(export_obj, indent=2)
            filename = f"{layout['slug']}.canvas.json"
            return Response(
                json_str,
                mimetype="application/json",
                headers={"Content-Disposition": f'attachment; filename="{filename}"'},
            )
        # Bundle: zip with canvas.json + assets/*
        upload_root = os.path.abspath(config.UPLOAD_FOLDER)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("canvas.json", json.dumps(export_obj, indent=2))
            for name in upload_names:
                # Defensive: filenames are validated by the regex above, but
                # we still resolve the path and verify it stays inside the
                # upload root before reading.
                fpath = os.path.abspath(os.path.normpath(os.path.join(upload_root, name)))
                try:
                    if os.path.commonpath([upload_root, fpath]) != upload_root:
                        continue
                except ValueError:
                    continue
                if not os.path.isfile(fpath):
                    continue
                z.write(fpath, arcname=f"assets/{os.path.basename(fpath)}")
        buf.seek(0)
        filename = f"{layout['slug']}.canvas.zip"
        return Response(
            buf.read(),
            mimetype="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @app.route("/canvas/<slug>/edit", methods=["POST"])
    @_canvas_access_required
    @rate_limit(10, 60)
    def canvas_edit(slug):
        """Update canvas title / description."""
        user = get_current_user()
        layout = db.canvas_get_layout_by_slug(slug)
        if not layout:
            abort(404)
        is_creator = layout["creator_id"] == user["id"]
        is_admin = user["role"] in ("admin", "owner")
        if not is_creator and not is_admin:
            flash(t("flash.you_do_not_have_the_required_permissions_to_5dd861"), "error")
            abort(403)
        title = (request.form.get("title") or "").strip()
        if not title:
            flash(t("flash.canvas_title_is_required_to_continue"), "error")
            return redirect(url_for("canvas_view", slug=slug))
        if len(title) > 200:
            flash(t("flash.canvas_title_cannot_exceed_200_characters"), "error")
            return redirect(url_for("canvas_view", slug=slug))
        description = (request.form.get("description") or "").strip()
        if len(description) > 2000:
            flash(t("flash.description_cannot_exceed_2000_characters"), "error")
            return redirect(url_for("canvas_view", slug=slug))
        db.canvas_update_layout(layout["id"], title=title, description=description)
        _canvas_record_history(layout["id"], "Edited canvas info", user=user)
        updated = db.canvas_get_layout(layout["id"])
        log_action("canvas_edit", request, user=user, canvas_id=layout["id"], title=title)
        flash(t("flash.canvas_has_been_successfully_updated"), "success")
        return redirect(url_for("canvas_view", slug=updated["slug"]))

    @app.route("/canvas/<slug>/delete", methods=["POST"])
    @_canvas_access_required
    @rate_limit(10, 60)
    def canvas_delete(slug):
        """Delete a canvas layout."""
        user = get_current_user()
        layout = db.canvas_get_layout_by_slug(slug)
        if not layout:
            abort(404)
        is_creator = layout["creator_id"] == user["id"]
        is_admin = user["role"] in ("admin", "owner")
        if not is_creator and not is_admin:
            flash(t("flash.you_do_not_have_the_required_permissions_to_9d650b"), "error")
            abort(403)
        db.canvas_delete_layout(layout["id"])
        log_action("canvas_delete", request, user=user, canvas_id=layout["id"], title=layout["title"])
        flash(t("flash.canvas_has_been_successfully_deleted"), "success")
        return redirect(url_for("canvas_list"))

    @app.route("/canvas/import", methods=["POST"])
    @_canvas_access_required
    @rate_limit(10, 60)
    def canvas_import():
        """Import a canvas layout from a ``.canvas.json`` or ``.canvas.zip`` file."""
        user = get_current_user()
        settings = db.get_site_settings()
        if not _can_write_global(user, settings):
            flash(t("flash.you_do_not_have_the_required_permissions_to_62359a"), "error")
            return redirect(url_for("canvas_list"))

        if "import_file" not in request.files:
            flash(t("flash.no_file_part"), "error")
            return redirect(url_for("canvas_list"))
        file = request.files["import_file"]
        if file.filename == "":
            flash(t("flash.no_selected_file"), "error")
            return redirect(url_for("canvas_list"))

        try:
            title, description, canvas_data, name_map = _import_canvas_file(
                file.stream, user, settings,
            )
        except _CanvasImportError as exc:
            flash(str(exc), "error")
            return redirect(url_for("canvas_list"))
        except (zipfile.BadZipFile, zlib.error, EOFError, NotImplementedError, RuntimeError):
            # Corrupt, encrypted or unsupported zip members.
            flash(t("flash.invalid_json_file"), "error")
            return redirect(url_for("canvas_list"))
        except OSError:
            flash(t("error.failed_to_save_file"), "error")
            return redirect(url_for("canvas_list"))

        data_json = json.dumps(canvas_data)
        if len(data_json.encode("utf-8")) > _MAX_CANVAS_DATA_BYTES:
            # A bundle was already checked before its images were written;
            # this covers plain JSON and the few bytes the new file names add.
            _discard_restored_uploads(name_map)
            flash(t("flash.canvas_data_exceeds_the_5_mb_size_limit"), "error")
            return redirect(url_for("canvas_list"))

        layout_id = db.canvas_create_layout(
            title=title,
            creator_id=user["id"],
            description=description,
        )
        db.canvas_save_layout_data(layout_id, canvas_data)
        _canvas_record_history(layout_id, "Imported canvas", user=user)
        layout = db.canvas_get_layout(layout_id)
        log_action("canvas_import", request, user=user, canvas_id=layout_id, title=title,
                   images=len(name_map))
        flash(t("flash.canvas_title_has_been_successfully_imported", title=title), "success")
        return redirect(url_for("canvas_view", slug=layout["slug"]))

    @app.route("/canvas/<slug>/share", methods=["POST"])
    @_canvas_access_required
    @rate_limit(20, 60)
    def canvas_share(slug):
        """Update sharing permissions for a canvas."""
        user = get_current_user()
        layout = db.canvas_get_layout_by_slug(slug)
        if not layout:
            abort(404)
        is_creator = layout["creator_id"] == user["id"]
        is_admin = user["role"] in ("admin", "owner")
        if not is_creator and not is_admin:
            flash(t("flash.you_do_not_have_the_required_permissions_to_442ff4"), "error")
            abort(403)

        action = request.form.get("action", "")

        if action == "add_user":
            target_user_id = request.form.get("user_id", "").strip()
            perm = request.form.get("permission", "view")
            if perm not in ("view", "edit", "none"):
                perm = "view"
            if target_user_id:
                target_user = db.get_user_by_id(target_user_id)
                if target_user:
                    db.canvas_set_permission(layout["id"], perm, user_id=target_user_id)
                    flash(t("flash.permission_for_user_has_been_successfully_set"), "success")
                else:
                    flash(t("flash.user_not_found"), "error")

        elif action == "add_role":
            target_role = request.form.get("role", "").strip()
            perm = request.form.get("permission", "view")
            if perm not in ("view", "edit", "none"):
                perm = "view"
            if target_role in ("user", "editor", "admin", "owner"):
                db.canvas_set_permission(layout["id"], perm, role=target_role)
                flash(t("flash.permission_for_role_targetrole_has_been_successfully_set", target_role=target_role), "success")
            else:
                flash(t("flash.invalid_role"), "error")

        elif action == "remove_user":
            target_user_id = request.form.get("user_id", "").strip()
            if target_user_id:
                db.canvas_remove_permission(layout["id"], user_id=target_user_id)
                flash(t("flash.user_permission_has_been_successfully_removed"), "success")

        elif action == "remove_role":
            target_role = request.form.get("role", "").strip()
            if target_role:
                db.canvas_remove_permission(layout["id"], role=target_role)
                flash(t("flash.role_permission_has_been_successfully_removed"), "success")

        elif action == "set_visibility":
            visibility = request.form.get("visibility", "private")
            if visibility in ("private", "shared", "public"):
                db.canvas_update_layout(layout["id"], visibility=visibility)
                flash(t("flash.canvas_visibility_has_been_successfully_updated"), "success")

        elif action == "transfer_ownership":
            target_user_id = request.form.get("user_id", "").strip()
            target_user = db.get_user_by_id(target_user_id)
            if not target_user:
                flash(t("flash.please_select_a_valid_user"), "error")
                return redirect(url_for("canvas_view", slug=slug))
            db.canvas_update_layout(layout["id"], creator_id=target_user_id)
            log_action("canvas_transfer_ownership", request, user=user, canvas_id=layout["id"], new_owner=target_user_id)
            flash(t("flash.canvas_ownership_has_been_successfully_transferred_to_userna", username=target_user['username']), "success")
            return redirect(url_for("canvas_list"))

        return redirect(url_for("canvas_view", slug=slug))

    @app.route("/canvas/<slug>/history")
    @_canvas_access_required
    def canvas_history(slug):
        """Display the revision history of a canvas layout."""
        user = get_current_user()
        layout = db.canvas_get_layout_by_slug(slug)
        if not layout:
            abort(404)
        settings = db.get_site_settings()
        open_access = bool(settings and settings.get("canvas_open_access"))
        if not db.canvas_user_can_view(layout["id"], user, open_access=open_access):
            flash(t("flash.you_do_not_have_the_required_permissions_to_f7261f"), "error")
            abort(403)
        history = db.canvas_list_layout_history(layout["id"])
        can_revert = db.canvas_user_can_edit(layout["id"], user, open_access=open_access)
        is_admin = bool(user and user["role"] in ("admin", "owner"))
        return render_template(
            "canvas/history.html",
            layout=layout,
            history=history,
            can_revert=can_revert,
            is_admin=is_admin,
        )

    @app.route("/canvas/<slug>/history/<int:entry_id>")
    @_canvas_access_required
    def canvas_history_entry(slug, entry_id):
        """Display a specific historical revision of a canvas layout."""
        user = get_current_user()
        layout = db.canvas_get_layout_by_slug(slug)
        if not layout:
            abort(404)
        settings = db.get_site_settings()
        open_access = bool(settings and settings.get("canvas_open_access"))
        if not db.canvas_user_can_view(layout["id"], user, open_access=open_access):
            flash(t("flash.you_do_not_have_the_required_permissions_to_f7261f"), "error")
            abort(403)
        entry = db.canvas_get_layout_history_entry(entry_id)
        if not entry or entry["layout_id"] != layout["id"]:
            abort(404)
        try:
            data = json.loads(entry["data"] or "{}")
        except (TypeError, ValueError):
            data = {}
        node_count = 0
        edge_count = 0
        if isinstance(data, dict):
            nodes = data.get("nodes") or []
            edges = data.get("edges") or []
            if isinstance(nodes, list):
                node_count = len(nodes)
            if isinstance(edges, list):
                edge_count = len(edges)
        can_revert = db.canvas_user_can_edit(layout["id"], user, open_access=open_access)
        is_admin = bool(user and user["role"] in ("admin", "owner"))
        return render_template(
            "canvas/history_entry.html",
            layout=layout,
            entry=entry,
            node_count=node_count,
            edge_count=edge_count,
            can_revert=can_revert,
            is_admin=is_admin,
            description_html=render_markdown(entry["description"] or ""),
        )

    @app.route("/canvas/<slug>/revert/<int:entry_id>", methods=["POST"])
    @_canvas_access_required
    @rate_limit(10, 60)
    def canvas_revert(slug, entry_id):
        """Revert a canvas layout to a previous history entry."""
        user = get_current_user()
        layout = db.canvas_get_layout_by_slug(slug)
        if not layout:
            abort(404)
        settings = db.get_site_settings()
        open_access = bool(settings and settings.get("canvas_open_access"))
        if not db.canvas_user_can_edit(layout["id"], user, open_access=open_access):
            flash(t("flash.you_do_not_have_the_required_permissions_to_5dd861"), "error")
            return redirect(url_for("canvas_history", slug=slug))
        entry = db.canvas_get_layout_history_entry(entry_id)
        if not entry or entry["layout_id"] != layout["id"]:
            abort(404)
        ok = db.canvas_restore_layout_from_snapshot(
            layout["id"],
            entry["data"],
            title=entry["title"] or None,
            description=entry["description"] or "",
        )
        if not ok:
            abort(404)
        _canvas_record_history(
            layout["id"],
            f"Reverted to revision #{entry_id}",
            user=user,
            is_revert=True,
        )
        # Notify collaborators their incremental queues are stale.
        try:
            session_id = (request.headers.get("X-Canvas-Session") or "").strip()[:64]
            db.canvas_append_snapshot_event(
                layout["id"], by_user_id=user["id"], by_session=session_id,
            )
        except Exception as exc:  # noqa: BLE001
            from wiki_logger import get_logger
            get_logger().warning("Canvas snapshot event append failed: %s", exc, exc_info=True)
        log_action("canvas_revert", request, user=user,
                   canvas_id=layout["id"], entry_id=entry_id)
        flash(t("flash.canvas_reverted"), "success")
        return redirect(url_for("canvas_view", slug=slug))

    @app.route("/canvas/<slug>/history/<int:entry_id>/delete", methods=["POST"])
    @_canvas_access_required
    @rate_limit(20, 60)
    def canvas_delete_history_entry(slug, entry_id):
        """Delete a single canvas-history entry (admin-only)."""
        user = get_current_user()
        layout = db.canvas_get_layout_by_slug(slug)
        if not layout:
            abort(404)
        if not user or user["role"] not in ("admin", "owner"):
            flash(t("flash.you_do_not_have_the_required_permissions_to_5dd861"), "error")
            return redirect(url_for("canvas_history", slug=slug))
        entry = db.canvas_get_layout_history_entry(entry_id)
        if not entry or entry["layout_id"] != layout["id"]:
            abort(404)
        db.canvas_delete_layout_history_entry(entry_id)
        log_action("canvas_delete_history_entry", request, user=user,
                   canvas_id=layout["id"], entry_id=entry_id)
        flash(t("flash.canvas_history_entry_deleted"), "success")
        return redirect(url_for("canvas_history", slug=slug))

    @app.route("/canvas/<slug>/history/clear", methods=["POST"])
    @_canvas_access_required
    @rate_limit(10, 60)
    def canvas_clear_history(slug):
        """Delete every history entry attached to a canvas (admin-only)."""
        user = get_current_user()
        layout = db.canvas_get_layout_by_slug(slug)
        if not layout:
            abort(404)
        if not user or user["role"] not in ("admin", "owner"):
            flash(t("flash.you_do_not_have_the_required_permissions_to_5dd861"), "error")
            return redirect(url_for("canvas_history", slug=slug))
        db.canvas_clear_layout_history(layout["id"])
        log_action("canvas_clear_history", request, user=user, canvas_id=layout["id"])
        flash(t("flash.canvas_history_cleared"), "success")
        return redirect(url_for("canvas_history", slug=slug))


def user_has_canvas_sidebar_access(user, settings):
    """Return True if the canvas link should be visible in the sidebar for *user*."""
    if not user:
        return False
    if not db.has_permission(user, "canvas.view"):
        return False
    if settings and settings.get("canvas_open_access"):
        return True
    if _has_global_canvas_access(user, settings):
        return True
    return db.canvas_user_has_any_permission(user["id"])
