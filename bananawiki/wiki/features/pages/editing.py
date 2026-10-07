"""Writing: create, edit (with conflict handling), details, hiding, deletion, home page."""

from __future__ import annotations

from typing import Any

from flask import abort, redirect, render_template, request, url_for

from ... import auth, registry
from . import access, categories, diff, presence, rendering, service
from .blueprint import bp, page_url, rate_limited, visible_page_or_404


def _category_arg(value: str | None) -> int | None:
    if value in (None, "", "0"):
        return None
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        abort(400)


def _denied_edit(page: dict[str, Any]):
    response = registry.intercept("page.edit_denied", page=page, user=auth.current_user())
    return response if response is not None else abort(403)


def _blocked_redirect(page: dict[str, Any]):
    """Redirect with an explanation when another feature (or deletion) freezes *page*."""
    blocked = access.edit_blocked(page)
    if blocked:
        auth.flash_t(blocked, "error")
        return redirect(page_url(page))
    return None


def _editable(slug: str) -> tuple[dict[str, Any], Any]:
    """The page and ``None``, or the page and the response refusing the edit."""
    page = visible_page_or_404(slug)
    if not service.can_edit(page):
        return page, _denied_edit(page)
    return page, _blocked_redirect(page)


# ── Create ────────────────────────────────────────────────────────────────────


def _create_denied(category_id: int | None):
    response = registry.intercept("page.create_denied", user=auth.current_user(), category_id=category_id)
    return response if response is not None else abort(403)


@bp.route("/create", methods=["GET", "POST"])
@bp.route("/create-page", methods=["GET", "POST"])
@rate_limited("create", 20)
def create():
    user = auth.current_user()
    category_id = _category_arg(request.values.get("category_id"))
    if category_id is not None and categories.get(category_id) is None:
        category_id = None
    if request.method == "GET":
        if not access.can_create_somewhere(user):
            return _create_denied(category_id)
        if category_id is not None and not service.can_create(category_id, user):
            category_id = None
        return render_template("pages/create.html", form={"category_id": category_id, "title": "", "content": ""},
                               category_choices=access.category_choices(user),
                               can_uncategorized=service.can_create(None, user))
    if not service.can_create(category_id, user):
        return _create_denied(category_id)
    form = {"title": request.form.get("title", ""), "content": request.form.get("content", ""),
            "category_id": category_id, "edit_message": request.form.get("edit_message", "")}
    try:
        page = service.create(form["title"], form["content"], category_id=category_id, author_id=user["id"],
                              edit_message=form["edit_message"].strip() or "Created")
    except service.PageError as exc:
        auth.flash_t(exc.key, "error", **exc.values)
        return render_template("pages/create.html", form=form, category_choices=access.category_choices(user),
                               can_uncategorized=service.can_create(None, user)), 400
    registry.intercept("page.saved", page=page, user=user)
    auth.flash_t("pages.flash.created", "success")
    return redirect(url_for("pages.view", slug=page["slug"]))


# ── Edit ──────────────────────────────────────────────────────────────────────


def _editor(page: dict[str, Any], *, title: str, content: str, category_id: int | None, revision: int,
            edit_message: str = "", conflict: dict[str, Any] | None = None, status: int = 200):
    user = auth.current_user()
    return render_template(
        "pages/edit.html", page=page, title=title, content=content, category_id=category_id, revision=revision,
        edit_message=edit_message, conflict=conflict,
        can_edit_metadata=access.can_edit_metadata(page, user),
        category_choices=access.category_choices(user),
        can_uncategorized=auth.can_write_category(None, user),
        others=presence.active_editors(page["id"], exclude_user_id=user["id"]),
    ), status


@bp.route("/page/<slug>/edit", methods=["GET", "POST"])
@rate_limited("edit", 30)
def edit(slug: str):
    page, refused = _editable(slug)
    if refused is not None:
        return refused
    user = auth.current_user()
    if request.method == "GET":
        return _editor(page, title=page["title"], content=page["content"], category_id=page["category_id"],
                       revision=page["revision"])

    content = request.form.get("content", "")
    edit_message = request.form.get("edit_message", "").strip()
    revision = request.form.get("revision", type=int)
    may_change_details = access.can_edit_metadata(page, user)
    title = request.form.get("title", page["title"]) if may_change_details else page["title"]
    category_id = page["category_id"]
    if may_change_details and not page["is_home"] and "category_id" in request.form:
        category_id = _category_arg(request.form.get("category_id"))
        if category_id != page["category_id"] and not auth.can_write_category(category_id, user):
            auth.flash_t("pages.error.category_not_writable", "error")
            return _editor(page, title=title, content=content, category_id=page["category_id"],
                           revision=revision or page["revision"], edit_message=edit_message, status=403)
    try:
        # Saving from the Markdown editor turns a page-builder page back into Markdown (as in 1.4).
        updated = service.update(page, author_id=user["id"], title=title, content=content,
                                 edit_message=edit_message, expected_revision=revision,
                                 builder_json="", builder_public=False)
        if category_id != page["category_id"]:
            updated = service.move(updated, category_id, actor_id=user["id"])
    except service.EditConflict as conflict:
        return _conflict(page, conflict.current, title=title, content=content, category_id=category_id,
                         edit_message=edit_message)
    except service.PageError as exc:
        auth.flash_t(exc.key, "error", **exc.values)
        return _editor(page, title=title, content=content, category_id=category_id,
                       revision=revision or page["revision"], edit_message=edit_message, status=400)
    presence.stop(page["id"], user["id"])
    registry.intercept("page.saved", page=updated, user=user)
    changed = updated["revision"] != page["revision"] or updated["category_id"] != page["category_id"]
    auth.flash_t("pages.flash.saved" if changed else "pages.flash.no_changes", "success" if changed else "info")
    return redirect(page_url(updated))


def _conflict(page: dict[str, Any], current: dict[str, Any], *, title: str, content: str,
              category_id: int | None, edit_message: str):
    """Someone saved in between: keep the user's text and show what changed meanwhile.

    The form now carries the current revision, so saving again deliberately
    replaces the other version with the (merged) text in the editor.
    """
    changes = diff.source_diff(current["content"], content)
    info = {
        "current": current,
        "their_editor": rendering.editor_name(current.get("last_edited_by")),
        "diff": changes.html,
        "diff_complete": changes.complete,
        "title_changed": current["title"] != title,
    }
    auth.flash_t("pages.flash.conflict", "warning")
    return _editor(current, title=title, content=content, category_id=category_id, revision=current["revision"],
                   edit_message=edit_message, conflict=info, status=409)


# ── Details ───────────────────────────────────────────────────────────────────


def _details_page(slug: str) -> tuple[dict[str, Any], Any]:
    page, refused = _editable(slug)
    if refused is None and not access.can_edit_metadata(page):
        refused = abort(403)
    return page, refused


@bp.post("/page/<slug>/edit/title")
@rate_limited("details", 20)
def edit_title(slug: str):
    page, refused = _details_page(slug)
    if refused is not None:
        return refused
    try:
        service.update(page, author_id=auth.current_user()["id"], title=request.form.get("title", ""),
                       edit_message=f"Title changed from '{page['title']}'")
    except service.PageError as exc:
        auth.flash_t(exc.key, "error", **exc.values)
    else:
        auth.flash_t("pages.flash.title_changed", "success")
    return redirect(page_url(page))


@bp.post("/page/<slug>/move")
@rate_limited("details", 20)
def move(slug: str):
    page, refused = _details_page(slug)
    if refused is not None:
        return refused
    if page["is_home"]:
        auth.flash_t("pages.error.home_fixed", "error")
        return redirect(page_url(page))
    category_id = _category_arg(request.form.get("category_id"))
    if not auth.can_write_category(category_id):
        abort(403)
    try:
        service.move(page, category_id, actor_id=auth.current_user()["id"])
    except service.PageError as exc:
        auth.flash_t(exc.key, "error", **exc.values)
    else:
        auth.flash_t("pages.flash.moved", "success")
    return redirect(page_url(page))


@bp.post("/page/<slug>/rename")
@rate_limited("details", 10)
def rename(slug: str):
    page, refused = _details_page(slug)
    if refused is not None:
        return refused
    if page["is_home"]:
        auth.flash_t("pages.error.home_fixed", "error")
        return redirect(page_url(page))
    new_slug = request.form.get("new_slug", "").strip()
    if not new_slug or not service.slugify(new_slug).strip("-"):
        auth.flash_t("pages.error.slug_required", "error")
        return redirect(page_url(page))
    try:
        changed = service.change_slug(page, new_slug, actor_id=auth.current_user()["id"])
    except service.PageError as exc:
        auth.flash_t(exc.key, "error", **exc.values)
        return redirect(page_url(page))
    auth.flash_t("pages.flash.renamed", "success")
    return redirect(url_for("pages.view", slug=changed["slug"]))


@bp.post("/page/<slug>/deindex")
@rate_limited("details", 20)
def deindex(slug: str):
    page, refused = _editable(slug)
    if refused is not None:
        return refused
    if not access.can_deindex(page):
        abort(403)
    hidden = not page["is_deindexed"]
    service.set_fields(page["id"], is_deindexed=1 if hidden else 0)
    auth.flash_t("pages.flash.hidden" if hidden else "pages.flash.unhidden", "success")
    if service.can_view(service.get(page["id"])):
        return redirect(page_url(page))
    return redirect(url_for("pages.home"))


@bp.post("/page/<slug>/delete")
@rate_limited("delete", 10)
def delete(slug: str):
    page, refused = _editable(slug)
    if refused is not None:
        return refused
    user = auth.current_user()
    if page["is_home"]:
        auth.flash_t("wiki.error.cannot_delete_home", "error")
        return redirect(page_url(page))
    if not service.can_delete(page, user):
        abort(403)
    handled = registry.intercept("page.delete", page=page, user=user)
    if handled is not None:
        return handled
    service.delete(page, actor_id=user["id"])
    auth.flash_t("pages.flash.deleted", "success", title=page["title"])
    return redirect(url_for("pages.home"))


@bp.post("/page/<slug>/set-home")
@rate_limited("details", 10)
def set_home(slug: str):
    page = visible_page_or_404(slug)
    if not access.can_set_home():
        abort(403)
    service.set_home(page)
    auth.flash_t("pages.flash.home_set", "success", title=page["title"])
    return redirect(url_for("pages.home"))
