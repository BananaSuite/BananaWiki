"""Canvases: list, read, create, change, save the document, apply operations, history, delete.

Scope ``canvas``; everything answers 404 while the Canvas feature is off. The
rules are ``features/canvas/access.py``: a canvas the token's owner cannot
see answers 404, one they can see but not edit 403; deleting belongs to the
creator and administrators; creating needs ``canvas.create`` and global write
access. Documents are returned with wiki-page nodes the caller may not read
stripped of their title and slug, as in exports.

``GET /canvas/<slug>`` answers with ``ETag: "v<version>"``; sending it back
as ``If-Match`` on ``PUT /canvas/<slug>/document`` refuses the save with 412
when someone changed the canvas in between (``expected_version`` in the body
does the same with 409). Operations and saves that would change or remove a
locked element are refused as a whole (400, code ``locked``, with the
element ids in ``locked``).
"""

from __future__ import annotations

from typing import Any

from ...canvas import access, model, present, service
from .. import serialize
from ..errors import ApiError, from_service, invalid, json_body, page_window, text, window_fields
from . import bp, caller, caller_token, etag_value, ok, request_etag, requires

FEATURE = "canvas"
LOCKED_KEY = "canvas.error.locked"


def _layout(slug: str, need: str = "view") -> tuple[dict[str, Any], str]:
    layout = service.get_by_slug(slug)
    level = access.level(caller(), layout)
    if layout is None or level is None:
        raise ApiError(404, "canvas_not_found")
    if need == "edit" and level != "edit":
        raise ApiError(403, "canvas_forbidden")
    if need == "own" and not access.is_owner(caller(), layout):
        raise ApiError(403, "canvas_forbidden")
    return layout, level


def _payload(layout: dict[str, Any], level: str | None) -> dict[str, Any]:
    return serialize.canvas(layout, level=level, is_owner=access.is_owner(caller(), layout))


def _session() -> str:
    """Operations from the API are tagged like an editor session, so open editors pick them up."""
    return f"api-{caller_token()['id']}"


def _run(action, *args: Any, **kwargs: Any) -> Any:
    try:
        return action(*args, **kwargs)
    except service.VersionConflict as conflict:
        raise ApiError(409, "edit_conflict", extra={"version": conflict.current_version}) from None
    except (service.CanvasError, model.DocumentError) as error:
        if getattr(error, "key", "") == LOCKED_KEY:
            values = getattr(error, "values", {})
            ids = [item for item in str(values.get("ids", "")).split(", ") if item]
            raise ApiError(400, "locked", LOCKED_KEY, extra={"locked": ids, "locked_count": values.get("count", 0)},
                           **values) from None
        raise from_service(error) from None


def _document_data(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or not isinstance(value.get("nodes"), list) \
            or not isinstance(value.get("edges"), list):
        raise invalid("data", "document")
    return value


def _answer(layout: dict[str, Any], level: str | None, status: int = 200, **extra: Any):
    response, code = ok(status, canvas=_payload(layout, level), **extra)
    response.headers["ETag"] = etag_value("v", layout.get("version") or 0)
    return response, code


@bp.get("/canvas")
@requires("canvas", feature=FEATURE)
def list_canvases():
    limit, offset = page_window()
    layouts = access.visible_layouts(caller())
    window = layouts[offset:offset + limit + 1]
    return ok(canvases=[_payload(layout, layout["level"]) for layout in window[:limit]],
              **window_fields(limit, offset, len(window)))


@bp.post("/canvas")
@requires("canvas", write=True, feature=FEATURE)
def create_canvas():
    """``{title, description?, data?}``; ``data`` is a document ``{nodes, edges, viewport?}``."""
    user = caller()
    if not access.can_create(user):
        raise ApiError(403, "cannot_create_canvas")
    data = json_body()
    title = text(data.get("title"), "title", maximum=service.MAX_TITLE, required=True)
    description = text(data.get("description"), "description", maximum=service.MAX_DESCRIPTION)
    document = _document_data(data["data"]) if data.get("data") is not None else None
    layout = _run(service.create, title, description, user["id"], data=document, message="created")
    return _answer(layout, "edit", 201)


@bp.get("/canvas/<slug>")
@requires("canvas", feature=FEATURE)
def get_canvas(slug: str):
    """The canvas and its document (``data``)."""
    layout, level = _layout(slug)
    document = present.redacted_document(service.document(layout["id"]), caller())
    return _answer(layout, level, data=document)


@bp.put("/canvas/<slug>")
@requires("canvas", write=True, feature=FEATURE)
def update_canvas(slug: str):
    """``{title?, description?, visibility?}``; the slug never changes, so links keep working."""
    layout, level = _layout(slug, "edit")
    data = json_body()
    if "visibility" in data:
        if not access.is_owner(caller(), layout):
            raise ApiError(403, "canvas_forbidden")
        if data["visibility"] not in access.VISIBILITIES:
            raise invalid("visibility", "choice", options=", ".join(access.VISIBILITIES))
    if "title" in data or "description" in data:
        title = text(data.get("title", layout["title"]), "title", maximum=service.MAX_TITLE, required=True)
        description = text(data.get("description", layout.get("description") or ""), "description",
                           maximum=service.MAX_DESCRIPTION)
        _run(service.update_info, layout, title, description, user_id=caller()["id"])
    if "visibility" in data:
        _run(service.set_visibility, layout, data["visibility"], actor_id=caller()["id"])
    return _answer(service.get(layout["id"]), level)


@bp.put("/canvas/<slug>/document")
@requires("canvas", write=True, feature=FEATURE)
def save_canvas_document(slug: str):
    """Replace the whole document: ``{data, expected_version?}`` and/or ``If-Match: "v<version>"``."""
    layout, level = _layout(slug, "edit")
    body = json_body()
    document = _document_data(body.get("data"))
    expected = body.get("expected_version")
    if expected is not None and (type(expected) is not int or expected < 0):
        raise invalid("expected_version", "integer")
    tags = request_etag()
    if tags is not None and "*" not in tags:
        current = int(layout.get("version") or 0)
        if f"v{current}" not in tags:
            raise ApiError(412, "precondition_failed", extra={"version": current})
        expected = current if expected is None else expected
    result = _run(service.save_document, layout, document, expected_version=expected, user_id=caller()["id"],
                  session_id=_session())
    return _answer(service.get(layout["id"]), level, seq=result["seq"])


@bp.post("/canvas/<slug>/ops")
@requires("canvas", write=True, feature=FEATURE)
def apply_canvas_ops(slug: str):
    """Apply editor operations atomically (``upsert_node``, ``delete_node``, ``upsert_edge``, …)."""
    layout, level = _layout(slug, "edit")
    ops = json_body().get("ops")
    if not isinstance(ops, list) or not ops:
        raise invalid("ops", "array")
    if len(ops) > model.MAX_OPS:
        raise invalid("ops", "too_many", maximum=model.MAX_OPS)
    result = _run(service.apply, layout, ops, user_id=caller()["id"], session_id=_session())
    return _answer(service.get(layout["id"]), level, applied=len(result["applied"]), seq=result["seq"])


@bp.get("/canvas/<slug>/history")
@requires("canvas", feature=FEATURE)
def canvas_history(slug: str):
    layout, _level = _layout(slug)
    limit, offset = page_window()
    entries = service.history(layout["id"])
    window = entries[offset:offset + limit + 1]
    return ok(history=[{
        "id": entry["id"], "title": entry.get("title"), "edited_by": entry.get("edited_by"),
        "editor": entry.get("username"), "edit_message": entry.get("edit_message") or "",
        "is_revert": bool(entry.get("is_revert")), "size": entry.get("size"),
        "created_at": serialize.iso(entry.get("created_at")),
    } for entry in window[:limit]], **window_fields(limit, offset, len(window)))


@bp.delete("/canvas/<slug>")
@requires("canvas", write=True, feature=FEATURE)
def delete_canvas(slug: str):
    layout, _level = _layout(slug, "own")
    service.delete(layout, actor_id=caller()["id"])
    return ok(deleted=True, slug=layout["slug"])
