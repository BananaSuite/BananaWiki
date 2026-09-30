"""Pages: list, read, search, history, create, update, delete, and bulk operations.

The rules are the web editor's (``features/pages``): a page the caller may
not read answers 404, a change outside their writable categories 403, a
protected or checked-out page 409, and deletion goes through the
``page.delete`` interceptor so Deletion Slowdown answers 202.
"""

from __future__ import annotations

from typing import Any

from flask import request

from .... import auth, registry
from ...pages import access, categories, service
from .. import serialize
from ..errors import (
    ApiError,
    from_service,
    invalid,
    items,
    json_body,
    optional_id,
    page_window,
    query_int,
    row_id,
    text,
    window_fields,
)
from . import bp, caller, etag_value, ok, request_etag, require_admin, requires

MAX_BULK = 100
MAX_SLUG = 200


# ── Helpers shared with the bulk endpoints ────────────────────────────────────


def readable(slug: str) -> dict[str, Any]:
    """The page, or 404 when it is missing or the caller may not read it (never 403)."""
    page = service.get_by_slug(slug)
    if page is None or not service.can_view(page, caller()):
        raise ApiError(404, "page_not_found")
    return page


def _title(value: Any) -> str:
    return text(value, "title", maximum=service.MAX_TITLE, required=True)


def _content(value: Any) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise invalid("content", "string")
    if len(value) > service.MAX_CONTENT:
        raise invalid("content", "too_long", maximum=service.MAX_CONTENT)
    return value


def _new_slug(value: Any, title: str) -> str:
    """The slug a new page gets; like 1.4, an existing slug is a conflict, not a renamed copy."""
    if value not in (None, "") and not isinstance(value, str):
        raise invalid("slug", "string")
    slug = service.slugify(value or title)
    if len(slug) > MAX_SLUG:
        raise invalid("slug", "too_long", maximum=MAX_SLUG)
    if slug in service.RESERVED_SLUGS:
        raise ApiError(400, "slug_reserved", slug=slug)
    if service.get_by_slug(slug, with_content=False) is not None:
        raise ApiError(409, "slug_taken", slug=slug)
    return slug


def _existing_category(category_id: int | None) -> int | None:
    if category_id is not None and categories.get(category_id) is None:
        raise ApiError(400, "category_missing")
    return category_id


def _require_unblocked(page: dict[str, Any]) -> None:
    key = access.edit_blocked(page, caller())
    if key:
        raise ApiError(409, "page_locked", key)


def plan_create(data: dict[str, Any]) -> dict[str, Any]:
    title = _title(data.get("title"))
    return {
        "title": title,
        "content": _content(data.get("content", "")),
        "slug_value": data.get("slug"),
        "category_id": optional_id(data.get("category_id"), "category_id"),
        "edit_message": text(data.get("edit_message"), "edit_message", maximum=service.MAX_EDIT_MESSAGE),
    }


def create_page(plan: dict[str, Any]) -> dict[str, Any]:
    user = caller()
    category_id = _existing_category(plan["category_id"])
    if not service.can_create(category_id, user):
        raise ApiError(403, "cannot_create")
    slug = _new_slug(plan["slug_value"], plan["title"])
    try:
        return service.create(plan["title"], plan["content"], category_id=category_id, author_id=user["id"],
                              slug=slug, edit_message=plan["edit_message"] or "Created via API")
    except service.PageError as error:
        raise from_service(error) from None


def plan_update(data: dict[str, Any]) -> dict[str, Any]:
    plan: dict[str, Any] = {}
    if "title" in data:
        plan["title"] = _title(data["title"])
    if "content" in data:
        plan["content"] = _content(data["content"])
    if "category_id" in data:
        plan["category_id"] = optional_id(data["category_id"], "category_id")
    if data.get("expected_revision") is not None:
        plan["expected_revision"] = row_id(data["expected_revision"], "expected_revision")
    if "edit_message" in data:
        plan["edit_message"] = text(data["edit_message"], "edit_message", maximum=service.MAX_EDIT_MESSAGE)
    return plan


def update_page(page: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
    """Apply a validated change with the editor's checks; return the saved page."""
    user = caller()
    if not service.can_edit(page, user):
        raise ApiError(403, "cannot_edit")
    _require_unblocked(page)
    moving = "category_id" in plan and plan["category_id"] != page.get("category_id")
    renaming = "title" in plan and plan["title"] != page["title"]
    if (moving or renaming) and not access.can_edit_metadata(page, user):
        raise ApiError(403, "cannot_edit_metadata")
    if moving:
        if page.get("is_home"):
            raise ApiError(400, "cannot_move_home")
        destination = _existing_category(plan["category_id"])
        if not auth.can_write_category(destination, user):
            raise ApiError(403, "cannot_edit")
    replace_builder = "content" in plan and bool(page.get("builder_json"))
    try:
        updated = service.update(
            page, author_id=user["id"], title=plan.get("title"), content=plan.get("content"),
            edit_message=plan.get("edit_message") or "Edited via API",
            expected_revision=plan.get("expected_revision"),
            builder_json="" if replace_builder else None, builder_public=False if replace_builder else None,
        )
        if moving:
            updated = service.move(updated, plan["category_id"], actor_id=user["id"])
    except service.EditConflict as conflict:
        raise ApiError(409, "edit_conflict", extra={"revision": conflict.current.get("revision")}) from None
    except service.PageError as error:
        raise from_service(error) from None
    return updated


def delete_page(page: dict[str, Any]) -> bool:
    """Delete like the web interface; return True when it was scheduled instead (Deletion Slowdown)."""
    user = caller()
    if page.get("is_home"):
        raise ApiError(400, "cannot_delete_home")
    if page.get("pending_deletion"):
        raise ApiError(409, "already_pending")
    if not service.can_delete(page, user):
        raise ApiError(403, "cannot_delete")
    _require_unblocked(page)
    if registry.intercept("page.delete", page=page, user=user) is not None:
        after = service.get(page["id"], with_content=False)
        if after is not None and after.get("pending_deletion"):
            return True
        raise ApiError(409, "page_locked")
    service.delete(page, actor_id=user["id"])
    return False


# ── Endpoints ─────────────────────────────────────────────────────────────────


@bp.get("/pages")
@requires("pages")
def list_pages():
    raw_category = request.args.get("category_id", "any").strip()
    if raw_category in ("", "any"):
        category: int | None | str = "any"
    elif raw_category == "none":
        category = None
    else:
        category = row_id(raw_category, "category_id")
    limit, offset = page_window()
    pages = service.list_visible(category_id=category, user=caller(), limit=limit + 1, offset=offset)
    return ok(pages=[serialize.page_summary(page) for page in pages[:limit]],
              **window_fields(limit, offset, len(pages)))


def _page_answer(page: dict[str, Any], status: int = 200):
    """The page with an ``ETag`` naming its revision, for ``If-Match`` on the next update."""
    response, code = ok(status, page=serialize.page_full(page))
    response.headers["ETag"] = etag_value("r", page.get("revision") or 0)
    return response, code


def _check_if_match(page: dict[str, Any], plan: dict[str, Any]) -> None:
    """``If-Match: "r<revision>"`` refuses a stale update with 412 and pins the revision for the save."""
    tags = request_etag()
    if tags is None or "*" in tags:
        return
    revision = int(page.get("revision") or 0)
    if f"r{revision}" not in tags:
        raise ApiError(412, "precondition_failed", extra={"revision": revision})
    plan.setdefault("expected_revision", revision)


@bp.get("/pages/<slug>")
@requires("pages")
def get_page(slug: str):
    return _page_answer(readable(slug))


@bp.get("/pages/<slug>/history")
@requires("pages")
def page_history(slug: str):
    page = readable(slug)
    if not auth.has_permission("history.view", caller()):
        raise ApiError(403, "cannot_view_history")
    limit = query_int("limit", 50, 1, 200)
    offset = query_int("offset", 0, 0, 2**31 - 1)
    entries = service.history(page["id"], limit=limit + 1, offset=offset)
    return ok(history=[serialize.history_entry(entry) for entry in entries[:limit]],
              **window_fields(limit, offset, len(entries)))


@bp.get("/history/<int:entry_id>")
@requires("pages")
def history_entry(entry_id: int):
    entry = service.history_entry(entry_id)
    page = service.get(entry["page_id"], with_content=False) if entry else None
    if entry is None or page is None or not service.can_view(page, caller()):
        raise ApiError(404, "revision_not_found")
    if not auth.has_permission("history.view", caller()):
        raise ApiError(403, "cannot_view_history")
    return ok(entry={**serialize.history_entry(entry, with_content=True), "page_slug": page["slug"]})


@bp.get("/search")
@requires("pages")
def search():
    if not access.can_search(caller()):
        raise ApiError(403, "cannot_search")
    query = request.args.get("q", "")
    limit = query_int("limit", 20, 1, 100)
    offset = query_int("offset", 0, 0, 10000)
    titles_only = request.args.get("titles_only", "") in ("1", "true")
    rows = service.search(query, user=caller(), limit=limit + 1, offset=offset, titles_only=titles_only)
    results = rows[:limit]
    return ok(**window_fields(limit, offset, len(rows)), results=[{
        "id": row["id"], "title": row["title"], "slug": row["slug"], "category_id": row["category_id"],
        "snippet": (row.get("snippet") or "").replace("\x02", "").replace("\x03", ""),
        "last_edited_at": serialize.iso(row.get("last_edited_at")),
    } for row in results])


@bp.post("/pages")
@requires("pages", write=True)
def create():
    return _page_answer(create_page(plan_create(json_body())), 201)


@bp.put("/pages/<slug>")
@requires("pages", write=True)
def update(slug: str):
    page = readable(slug)
    plan = plan_update(json_body())
    _check_if_match(page, plan)
    return _page_answer(update_page(page, plan))


@bp.delete("/pages/<slug>")
@requires("pages", write=True)
def delete(slug: str):
    page = readable(slug)
    if delete_page(page):
        return ok(202, pending_deletion=True, slug=page["slug"])
    return ok(deleted=True, slug=page["slug"])


# ── Bulk (administrators) ─────────────────────────────────────────────────────


@bp.post("/pages/bulk")
@requires("pages", write=True)
def bulk_create():
    """Every item is validated before anything is written; conflicts are reported per item."""
    require_admin()
    entries = items(json_body(), "pages", MAX_BULK)
    if not entries:
        raise invalid("pages", "required")
    plans = []
    for index, item in enumerate(entries):
        if not isinstance(item, dict):
            raise invalid(f"pages[{index}]", "object")
        plans.append(plan_create(item))
    created, errors = [], []
    for plan in plans:
        try:
            page = create_page(plan)
        except ApiError as error:
            errors.append({"title": plan["title"], **_item_error(error)})
        else:
            created.append(serialize.page_summary(page))
    return ok(201 if created else 200, created=created, errors=errors)


@bp.post("/pages/bulk-edit")
@requires("pages", write=True)
def bulk_edit():
    require_admin()
    entries = items(json_body(), "edits", MAX_BULK)
    if not entries:
        raise invalid("edits", "required")
    plans = []
    for index, item in enumerate(entries):
        if not isinstance(item, dict):
            raise invalid(f"edits[{index}]", "object")
        plans.append((text(item.get("slug"), f"edits[{index}].slug", maximum=MAX_SLUG, required=True),
                      plan_update(item)))
    updated, errors = 0, []
    for slug, plan in plans:
        try:
            update_page(readable(slug), plan)
        except ApiError as error:
            errors.append({"slug": slug, **_item_error(error)})
        else:
            updated += 1
    return ok(updated=updated, errors=errors)


@bp.post("/pages/bulk-delete")
@requires("pages", write=True)
def bulk_delete():
    require_admin()
    data = json_body()
    slugs = items(data, "slugs", MAX_BULK)
    ids = items(data, "ids", MAX_BULK)
    if not slugs and not ids:
        raise invalid("slugs", "required")
    if len(slugs) + len(ids) > MAX_BULK:
        raise invalid("ids", "too_many", maximum=MAX_BULK)
    if not all(isinstance(slug, str) for slug in slugs):
        raise invalid("slugs", "string")
    targets = [service.get_by_slug(slug) for slug in slugs]
    targets += [service.get(row_id(value, "ids")) for value in ids]
    deleted = scheduled = skipped = not_found = 0
    seen: set[int] = set()
    for page in targets:
        if page is None or not service.can_view(page, caller()):
            not_found += 1
            continue
        if page["id"] in seen:
            skipped += 1
            continue
        seen.add(page["id"])
        try:
            if delete_page(page):
                scheduled += 1
            else:
                deleted += 1
        except ApiError:
            skipped += 1
    return ok(deleted=deleted, pending_deletion=scheduled, skipped=skipped, not_found=not_found)


def _item_error(error: ApiError) -> dict[str, Any]:
    payload = error.payload()
    return {"error": payload["error"], "code": payload["code"]}

