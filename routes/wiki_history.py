"""Wiki history routes."""

from datetime import datetime, timezone
from flask import (
    render_template, request, redirect, url_for, flash, jsonify, abort, Response,
)
import db
import config
from bananawiki_sdk import emit_hook
from helpers import (
    login_required, editor_required, admin_required, get_current_user,
    editor_has_category_access, user_can_view_page, render_markdown, compute_char_diff,
    compute_diff_html, compute_formatted_diff_html, rate_limit,
    format_datetime, t,
)
from wiki_logger import log_action
from sync import notify_change

from .wiki_common import (
    _guard_destructive_page_edit,
    _abort_if_page_hidden,
    _pdf_export_allowed,
    _MAX_PAGE_CONTENT_LENGTH,
)


def register_wiki_history_routes(app):
    """Register history endpoints on the application."""

    @app.route("/page/<slug>/history")
    @login_required
    def page_history(slug):
        """Display the revision history for a wiki page."""
        if not config.PAGE_HISTORY_ENABLED:
            abort(404)
        page = db.get_page_by_slug(slug)
        if not page:
            abort(404)
        current_user = get_current_user()
        _abort_if_page_hidden(page, current_user)
        history = db.get_page_history(page["id"])
        all_users = db.list_users() if current_user and current_user["role"] in ("admin", "owner") else []
        # Compute per-entry char diffs (compare each entry to the next older one)
        history_list = list(history)
        diff_stats = {}
        for idx, entry in enumerate(history_list):
            prev_content = history_list[idx + 1]["content"] if idx + 1 < len(history_list) else ""
            added, deleted = compute_char_diff(prev_content, entry["content"])
            diff_stats[entry["id"]] = {"added": added, "deleted": deleted}
        return render_template(
            "wiki/history.html",
            page=page,
            history=history_list,
            diff_stats=diff_stats,
            all_users=all_users,
        )


    @app.route("/page/<slug>/history/<int:entry_id>")
    @login_required
    def view_history_entry(slug, entry_id):
        """Display a specific historical revision of a wiki page with diff view."""
        if not config.PAGE_HISTORY_ENABLED:
            abort(404)
        page = db.get_page_by_slug(slug)
        if not page:
            abort(404)
        _abort_if_page_hidden(page, get_current_user())
        entry = db.get_history_entry(entry_id)
        if not entry or entry["page_id"] != page["id"]:
            abort(404)
        # Find the previous (older) history entry to diff against
        history = db.get_page_history(page["id"])
        history_list = list(history)
        prev_content = None
        for idx, h in enumerate(history_list):
            if h["id"] == entry_id and idx + 1 < len(history_list):
                prev_content = history_list[idx + 1]["content"]
                break
        if prev_content is not None:
            diff_html = compute_diff_html(prev_content, entry["content"])
            formatted_diff_html = compute_formatted_diff_html(prev_content, entry["content"])
        else:
            diff_html = None
            formatted_diff_html = None
        content_html = render_markdown(entry["content"])
        return render_template(
            "wiki/history_entry.html",
            page=page,
            entry=entry,
            content_html=content_html,
            diff_html=diff_html,
            formatted_diff_html=formatted_diff_html,
            raw_content=entry["content"],
        )


    @app.route("/page/<slug>/export-pdf")
    @login_required
    @rate_limit(5, 60, exempt_html_nav=False)
    def export_page_pdf(slug):
        """Download the current page as a PDF file."""
        from flask import send_file as _send_file
        from helpers._pdf import generate_page_pdf as _gen_pdf

        page = db.get_page_by_slug(slug)
        if not page:
            abort(404)
        user = get_current_user()
        if not user_can_view_page(user, page):
            abort(403)
        if not _pdf_export_allowed(user):
            flash(t("flash.you_do_not_have_the_required_permissions_to_cbf05f"), "error")
            return redirect(url_for("view_page", slug=slug))

        settings = db.get_site_settings()
        site_name = settings["site_name"] if settings else "BananaWiki"

        author = None
        edited_at_str = None
        if page["last_edited_by"]:
            editor = db.get_user_by_id(page["last_edited_by"])
            if editor:
                author = editor["username"]
        if page["last_edited_at"]:
            edited_at_str = format_datetime(page["last_edited_at"])

        buf = _gen_pdf(
            title=page["title"],
            markdown_content=page["content"] or "",
            site_name=site_name,
            author=author,
            edited_at=edited_at_str,
        )
        log_action("export_page_pdf", request, user=user, page=slug)
        return _send_file(
            buf,
            mimetype="application/pdf",
            as_attachment=True,
            download_name=f"{slug}.pdf",
        )


    @app.route("/page/<slug>/history/<int:entry_id>/export-pdf")
    @login_required
    @rate_limit(5, 60, exempt_html_nav=False)
    def export_history_entry_pdf(slug, entry_id):
        """Download a specific page history revision as a PDF file."""
        if not config.PAGE_HISTORY_ENABLED:
            abort(404)
        from flask import send_file as _send_file
        from helpers._pdf import generate_page_pdf as _gen_pdf

        page = db.get_page_by_slug(slug)
        if not page:
            abort(404)
        user = get_current_user()
        _abort_if_page_hidden(page, user)
        if not _pdf_export_allowed(user):
            flash(t("flash.you_do_not_have_the_required_permissions_to_cbf05f"), "error")
            return redirect(url_for("view_page", slug=slug))

        entry = db.get_history_entry(entry_id)
        if not entry or entry["page_id"] != page["id"]:
            abort(404)

        settings = db.get_site_settings()
        site_name = settings["site_name"] if settings else "BananaWiki"

        author = entry["username"] if "username" in entry else None
        edited_at_str = format_datetime(entry["created_at"]) if entry["created_at"] else None

        buf = _gen_pdf(
            title=entry["title"],
            markdown_content=entry["content"] or "",
            site_name=site_name,
            author=author,
            edited_at=edited_at_str,
            is_history=True,
        )
        log_action("export_history_pdf", request, user=user, page=slug, entry_id=entry_id)
        return _send_file(
            buf,
            mimetype="application/pdf",
            as_attachment=True,
            download_name=f"{slug}-revision-{entry_id}.pdf",
        )


    @app.route("/page/<slug>/export-md")
    @login_required
    @editor_required
    @rate_limit(20, 60, exempt_html_nav=False)
    def export_page_markdown(slug):
        """Download the current page body as a ``.md`` file with a small
        YAML front-matter block describing the source page.  Intended to
        be paired with :func:`import_page_markdown` so editors can move
        page contents between instances or back them up locally.
        """
        settings = db.get_site_settings()
        if not settings or not settings.get("markdown_export_enabled"):
            abort(404)
        page = db.get_page_by_slug(slug)
        if not page:
            abort(404)
        user = get_current_user()
        if not editor_has_category_access(user, page["category_id"]):
            flash(t("flash.you_do_not_have_permission_to_edit_pages"), "error")
            return redirect(url_for("view_page", slug=slug))
        _abort_if_page_hidden(page, user)

        category_name = ""
        if page["category_id"]:
            cat = db.get_category(page["category_id"])
            if cat:
                category_name = cat["name"]

        def _yaml_escape(value):
            """Return *value* wrapped in double quotes so any colons,
            quotes, or backslashes round-trip cleanly through YAML."""
            if value is None:
                return '""'
            return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'

        exported_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        frontmatter = (
            "---\n"
            f"title: {_yaml_escape(page['title'])}\n"
            f"slug: {_yaml_escape(page['slug'])}\n"
            f"category: {_yaml_escape(category_name)}\n"
            f"exported_at: {_yaml_escape(exported_at)}\n"
            "---\n\n"
        )
        body = page["content"] or ""
        log_action("export_page_markdown", request, user=user, page=slug)
        return Response(
            frontmatter + body,
            mimetype="text/markdown; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="{page["slug"]}.md"',
            },
        )


    @app.route("/page/<slug>/import-md", methods=["POST"])
    @login_required
    @editor_required
    @rate_limit(20, 60)
    def import_page_markdown(slug):
        """Return the contents of an uploaded ``.md`` / ``.txt`` file
        as JSON so the editor can populate the textarea client-side
        *without* immediately overwriting the page.  Any YAML front
        matter is stripped and (when present) the ``title`` field is
        returned separately so the title input can be updated.
        """
        page = db.get_page_by_slug(slug)
        if not page:
            abort(404)
        user = get_current_user()
        if not editor_has_category_access(user, page["category_id"]):
            return jsonify({"error": "forbidden"}), 403

        if "import_file" not in request.files:
            return jsonify({"error": "no_file"}), 400
        file = request.files["import_file"]
        if not file or not file.filename:
            return jsonify({"error": "no_file"}), 400

        # 1 MB cap mirrors ``_MAX_PAGE_CONTENT_LENGTH`` so an import
        # cannot smuggle in content the editor would later reject on
        # save anyway.
        raw = file.read(_MAX_PAGE_CONTENT_LENGTH + 1)
        if len(raw) > _MAX_PAGE_CONTENT_LENGTH:
            return jsonify({"error": "too_large"}), 400
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            try:
                text = raw.decode("latin-1")
            except (UnicodeDecodeError, AttributeError):
                return jsonify({"error": "invalid_encoding"}), 400

        # Normalise line endings so the front-matter regex below works
        # on Windows / classic-Mac files too.
        text = text.replace("\r\n", "\n").replace("\r", "\n")

        title = None
        body = text
        if text.startswith("---\n"):
            end = text.find("\n---", 4)
            if end != -1:
                fm = text[4:end]
                body = text[end + len("\n---"):].lstrip("\n")
                for line in fm.split("\n"):
                    line = line.strip()
                    if not line or ":" not in line:
                        continue
                    key, _, value = line.partition(":")
                    key = key.strip().lower()
                    value = value.strip()
                    if (value.startswith('"') and value.endswith('"')) or \
                       (value.startswith("'") and value.endswith("'")):
                        value = value[1:-1]
                    if key == "title" and value:
                        title = value[:300]

        log_action("import_page_markdown", request, user=user, page=slug)
        return jsonify({
            "content": body,
            "title": title,
        })


    @app.route("/page/<slug>/revert/<int:entry_id>", methods=["POST"])
    @login_required
    @editor_required
    @rate_limit(20, 60)
    def revert_page(slug, entry_id):
        """Revert a wiki page to a specific historical revision."""
        if not config.PAGE_HISTORY_ENABLED:
            abort(404)
        page = db.get_page_by_slug(slug)
        if not page:
            abort(404)
        user = get_current_user()
        _abort_if_page_hidden(page, user)
        entry = db.get_history_entry(entry_id)
        if not entry or entry["page_id"] != page["id"]:
            abort(404)
        if not editor_has_category_access(user, page["category_id"]):
            flash(t("flash.you_do_not_have_permission_to_edit_pages"), "error")
            return redirect(url_for("view_page", slug=slug))
        blocked_response = _guard_destructive_page_edit(page, user, t("flash.action.revert_this_page"))
        if blocked_response:
            return blocked_response
        db.update_page(
            page["id"], entry["title"], entry["content"], user["id"],
            f"Reverted to version from {entry['created_at']}", is_revert=True,
            builder_json=entry["builder_json"] if "builder_json" in entry.keys() else "",
            builder_public=entry["builder_public"] if "builder_public" in entry.keys() else False,
        )
        log_action("revert_page", request, user=user, page=slug, entry_id=entry_id)
        notify_change("page_revert", f"Page '{slug}' reverted")
        emit_hook("after_page_update", page=db.get_page(page["id"]), user=user)
        flash(t("flash.page_has_been_successfully_reverted_to_the_selected"), "success")
        return redirect(url_for("view_page", slug=slug))


    @app.route("/page/<slug>/history/<int:entry_id>/transfer", methods=["POST"])
    @login_required
    @admin_required
    @rate_limit(20, 60)
    def transfer_attribution(slug, entry_id):
        """Transfer authorship of a single history entry to a different user."""
        if not config.PAGE_HISTORY_ENABLED:
            abort(404)
        page = db.get_page_by_slug(slug)
        if not page:
            abort(404)
        entry = db.get_history_entry(entry_id)
        if not entry or entry["page_id"] != page["id"]:
            abort(404)
        new_user_id = request.form.get("new_user_id", "").strip()
        target_user = db.get_user_by_id(new_user_id) if new_user_id else None
        if not target_user:
            flash(t("flash.the_selected_user_is_invalid_please_choose_a"), "error")
            return redirect(url_for("page_history", slug=slug))
        user = get_current_user()
        db.transfer_history_attribution(entry_id, new_user_id)
        log_action("transfer_attribution", request, user=user, page=slug,
                   entry_id=entry_id, new_user=target_user["username"])
        notify_change("transfer_attribution",
                      f"Attribution of entry {entry_id} on '{slug}' transferred to '{target_user['username']}'")
        flash(t("flash.attribution_has_been_successfully_transferred_to_username", username=target_user['username']), "success")
        return redirect(url_for("page_history", slug=slug))


    @app.route("/page/<slug>/history/bulk-transfer", methods=["POST"])
    @login_required
    @admin_required
    @rate_limit(20, 60)
    def bulk_transfer_attribution(slug):
        """Transfer all history attributions on a page from one user to another."""
        if not config.PAGE_HISTORY_ENABLED:
            abort(404)
        page = db.get_page_by_slug(slug)
        if not page:
            abort(404)
        from_user_id = request.form.get("from_user_id", "").strip()
        new_user_id = request.form.get("new_user_id", "").strip()
        from_user = db.get_user_by_id(from_user_id) if from_user_id else None
        target_user = db.get_user_by_id(new_user_id) if new_user_id else None
        if not from_user or not target_user:
            flash(t("flash.the_selected_users_are_invalid_please_verify_your"), "error")
            return redirect(url_for("page_history", slug=slug))
        user = get_current_user()
        count = db.bulk_transfer_history_attribution(page["id"], from_user_id, new_user_id)
        log_action("bulk_transfer_attribution", request, user=user, page=slug,
                   from_user=from_user["username"], to_user=target_user["username"], count=count)
        notify_change("bulk_transfer_attribution",
                      f"Bulk attribution on '{slug}' transferred from '{from_user['username']}' to '{target_user['username']}'")
        flash(t("flash.successfully_transferred_count_contributions_to_username", count=count, username=target_user['username']), "success")
        return redirect(url_for("page_history", slug=slug))


    @app.route("/page/<slug>/history/<int:entry_id>/deattribute", methods=["POST"])
    @login_required
    @admin_required
    @rate_limit(20, 60)
    def deattribute_entry(slug, entry_id):
        """Admin: remove attribution from a single page history entry."""
        if not config.PAGE_HISTORY_ENABLED:
            abort(404)
        page = db.get_page_by_slug(slug)
        if not page:
            abort(404)
        entry = db.get_history_entry(entry_id)
        if not entry or entry["page_id"] != page["id"]:
            abort(404)
        user = get_current_user()
        db.deattribute_contribution(entry_id)
        log_action("deattribute_entry", request, user=user, page=slug,
                   entry_id=entry_id)
        notify_change("deattribute_entry",
                      f"Attribution removed from entry {entry_id} on '{slug}'")
        flash(t("flash.attribution_removed"), "success")
        return redirect(url_for("page_history", slug=slug))


    @app.route("/page/<slug>/history/<int:entry_id>/delete", methods=["POST"])
    @login_required
    @admin_required
    @rate_limit(20, 60)
    def delete_history_entry(slug, entry_id):
        """Admin: delete a single page history entry."""
        if not config.PAGE_HISTORY_ENABLED:
            abort(404)
        page = db.get_page_by_slug(slug)
        if not page:
            abort(404)
        entry = db.get_history_entry(entry_id)
        if not entry or entry["page_id"] != page["id"]:
            abort(404)
        user = get_current_user()
        db.delete_history_entry(entry_id)
        log_action("delete_history_entry", request, user=user, page=slug,
                   entry_id=entry_id)
        notify_change("delete_history_entry",
                      f"History entry {entry_id} deleted from '{slug}'")
        flash(t("flash.history_entry_has_been_successfully_deleted"), "success")
        return redirect(url_for("page_history", slug=slug))


    @app.route("/page/<slug>/history/clear", methods=["POST"])
    @login_required
    @admin_required
    @rate_limit(20, 60)
    def clear_page_history(slug):
        """Admin: delete all history entries for a page."""
        if not config.PAGE_HISTORY_ENABLED:
            abort(404)
        page = db.get_page_by_slug(slug)
        if not page:
            abort(404)
        user = get_current_user()
        count = db.clear_page_history(page["id"])
        log_action("clear_page_history", request, user=user, page=slug, count=count)
        notify_change("clear_page_history",
                      f"All history ({count} entries) cleared for '{slug}'")
        flash(t("flash.all_page_history_has_been_cleared_count_entries", count=count), "success")
        return redirect(url_for("page_history", slug=slug))
