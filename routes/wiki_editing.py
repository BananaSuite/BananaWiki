"""Wiki editing routes."""

from flask import (
    render_template, request, redirect, url_for, session, flash, abort, g,
)
import db
from bananawiki_sdk import emit_hook
from helpers import (
    login_required, editor_required, admin_required, get_current_user,
    editor_has_category_access, user_can_view_page, slugify, rate_limit,
    _is_valid_hex_color, t,
)
from helpers._validation import get_effective_max_upload_size
from wiki_logger import log_action
from sync import notify_change
from routes.uploads import cleanup_unused_uploads

from .wiki_common import (
    _page_reservations_enabled,
    _get_page_protection_context,
    _get_reservation_context,
    _reservation_block_message,
    _page_protection_block_message,
    _guard_destructive_page_edit,
    _MAX_PAGE_CONTENT_LENGTH,
)


def register_wiki_editing_routes(app):
    """Register editing endpoints on the application."""

    @app.route("/page/<slug>/edit", methods=["GET", "POST"])
    @login_required
    @editor_required
    @rate_limit(20, 60)
    def edit_page(slug):
        """Display and handle submission of the page editing form."""
        page = db.get_page_by_slug(slug)
        if not page:
            abort(404)
        user = get_current_user()
        if not editor_has_category_access(user, page["category_id"]):
            flash(t("flash.you_do_not_have_permission_to_edit_pages"), "error")
            return redirect(url_for("view_page", slug=slug))

        # Block editing of pages that are pending deletion.
        if page["pending_deletion"]:
            flash(
                t("flash.this_page_is_pending_deletion_and_cannot_be"),
                "error",
            )
            return redirect(url_for("view_page", slug=slug))

        # Check protection and reservation status
        protection_context = _get_page_protection_context(page, user)
        if protection_context and protection_context["edit_locked"]:
            flash(_page_protection_block_message(protection_context, t("flash.action.edit_this_page")), "error")
            return redirect(url_for("view_page", slug=slug))
        reservation_context = _get_reservation_context(page, user)
        reservation_status = reservation_context["status"] if reservation_context else None

        if reservation_context and reservation_context["edit_locked"]:
            flash(_reservation_block_message(reservation_status, t("flash.action.edit_this_page")), "error")
            return redirect(url_for("view_page", slug=slug))

        # Check for existing drafts from other users
        other_drafts = [
            d for d in db.get_drafts_for_page(page["id"]) if d["user_id"] != user["id"]
        ]

        if request.method == "POST":
            # Verify user still has edit access (reservation may have expired)
            protection_context = _get_page_protection_context(page, user)
            if protection_context and protection_context["edit_locked"]:
                flash(_page_protection_block_message(protection_context, t("flash.action.save_changes_to_this_page")), "error")
                return redirect(url_for("view_page", slug=slug))
            reservation_context = _get_reservation_context(page, user)
            reservation_status = reservation_context["status"] if reservation_context else None
            if reservation_context and reservation_context["edit_locked"]:
                flash(_reservation_block_message(reservation_status, t("flash.action.save_changes_to_this_page")), "error")
                return redirect(url_for("view_page", slug=slug))

            title = request.form.get("title", page["title"]).strip()
            content = request.form.get("content", "")
            edit_message = request.form.get("edit_message", "").strip()

            if len(content) > _MAX_PAGE_CONTENT_LENGTH:
                flash(t("flash.page_content_is_too_large_maximum_1_mb"), "error")
                settings = db.get_site_settings()
                return render_template(
                    "wiki/edit.html",
                    page=page,
                    content=content,
                    max_attachment_size_bytes=get_effective_max_upload_size(settings),
                )

            if not title:
                title = page["title"]
            elif len(title) > 200:
                # The 200-character cap is enforced here too, so the limit
                # matches the rename and create-page routes no matter which
                # form the title arrived through.
                flash(t("flash.page_title_cannot_exceed_200_characters"), "error")
                title = page["title"]

            # Track whether any actual change was made
            changes_made = False

            # Parse category_id early
            new_cat_id = request.form.get("category_id")
            try:
                new_cat_id = int(new_cat_id) if new_cat_id else None
            except (TypeError, ValueError):
                new_cat_id = page["category_id"]

            # Collect contributor names from other users' drafts
            all_drafts = db.get_drafts_for_page(page["id"])
            contributors = [d["username"] for d in all_drafts if d["user_id"] != user["id"]]

            # Build commit message with contributors
            if contributors:
                contributor_list = ", ".join(contributors)
                if edit_message:
                    edit_message = f"{edit_message} (contributors: {contributor_list})"
                else:
                    edit_message = f"Contributors: {contributor_list}"

            # Update page content/title if changed
            original_content = (page["content"] or "").strip()
            if content.strip() != original_content or title != page["title"]:
                db.update_page(
                    page["id"], title, content, user["id"], edit_message,
                    builder_json="", builder_public=False,
                )
                changes_made = True

            # Update category if provided and changed
            if new_cat_id != page["category_id"]:
                if page["is_home"]:
                    flash(t("flash.cannot_move_the_home_page"), "error")
                elif new_cat_id and not db.get_category(new_cat_id):
                    flash(t("flash.category_update_skipped_the_selected_category_no_longer"), "error")
                elif not editor_has_category_access(user, new_cat_id):
                    flash(t("flash.category_update_skipped_you_do_not_have_permission"), "error")
                else:
                    if db.update_page_category(page["id"], new_cat_id):
                        log_action("move_page", request, user=user, page=slug, category_id=new_cat_id)
                        changes_made = True

            # Update difficulty tag if provided (only when plugin is enabled)
            if getattr(g, 'enabled_plugins', {}).get('difficulty_tags'):
                tag = request.form.get("difficulty_tag", "").strip().lower()
                if tag in db.VALID_DIFFICULTY_TAGS:
                    custom_label = ""
                    custom_color = ""
                    if tag == "custom":
                        custom_label = request.form.get("tag_custom_label", "").strip()[:50]
                        custom_color = request.form.get("tag_custom_color", "").strip()
                        if not custom_label:
                            flash(t("flash.custom_tag_requires_a_label_to_be_specified"), "error")
                            tag = ""
                        elif not _is_valid_hex_color(custom_color):
                            flash(t("flash.custom_tag_requires_a_valid_hex_color_code"), "error")
                            tag = ""
                    if tag in db.VALID_DIFFICULTY_TAGS:
                        db.update_page_tag(page["id"], tag, custom_label, custom_color)
                        changes_made = True
                elif tag:
                    flash(t("flash.invalid_difficulty_tag"), "error")

            if not changes_made:
                flash(t("flash.no_changes_were_made_to_the_page"), "info")
                return redirect(url_for("view_page", slug=slug))

            # Clean up all drafts for this page (committer + contributors)
            db.delete_draft(page["id"], user["id"])
            for d in all_drafts:
                if d["user_id"] != user["id"]:
                    db.delete_draft(page["id"], d["user_id"])

            cleanup_unused_uploads()
            log_action("edit_page", request, user=user, page=slug, message=edit_message)
            notify_change("page_edit", f"Page '{slug}' edited")
            updated_page = db.get_page_by_slug(slug)
            emit_hook("after_page_update", page=updated_page, user=user)

            # Check and award auto-triggered badges
            newly_awarded = db.check_and_award_auto_badges(user["id"])
            if newly_awarded:
                session["badge_notifications"] = session.get("badge_notifications", 0) + len(newly_awarded)

            # Reserve page if the editor requested it
            if _page_reservations_enabled() and request.form.get("reserve_after_commit") == "1" and not page["is_home"]:
                try:
                    db.reserve_page(page["id"], user["id"])
                    flash(t("flash.page_has_been_successfully_updated_and_reserved_for"), "success")
                except ValueError:
                    flash(t("flash.page_has_been_successfully_updated_reservation_failed_page"), "success")
            else:
                flash(t("flash.page_has_been_successfully_updated"), "success")
            return redirect(url_for("view_page", slug=slug))

        # Load draft if exists
        draft = db.get_draft(page["id"], user["id"])
        attachments = db.get_page_attachments(page["id"])
        settings = db.get_site_settings()
        max_attachment_size_bytes = get_effective_max_upload_size(settings)
        return render_template(
            "wiki/edit.html",
            page=page,
            draft=draft,
            other_drafts=other_drafts,
            attachments=attachments,
            reservation_status=reservation_status,
            reservation_context=reservation_context,
            protection_context=protection_context,
            max_attachment_size_bytes=max_attachment_size_bytes,
        )


    @app.route("/page/<slug>/edit/title", methods=["POST"])
    @login_required
    @editor_required
    @rate_limit(20, 60)
    def edit_page_title(slug):
        """Inline title edit: update the title of a page without opening the full editor."""
        page = db.get_page_by_slug(slug)
        if not page:
            abort(404)
        user = get_current_user()
        if not editor_has_category_access(user, page["category_id"]):
            flash(t("flash.you_do_not_have_permission_to_edit_pages"), "error")
            return redirect(url_for("view_page", slug=slug))
        blocked_response = _guard_destructive_page_edit(page, user, t("flash.action.change_this_page_title"))
        if blocked_response:
            return blocked_response
        new_title = request.form.get("title", "").strip()
        if not new_title:
            flash(t("flash.title_is_required"), "error")
        elif len(new_title) > 200:
            flash(t("flash.page_title_cannot_exceed_200_characters"), "error")
        else:
            db.update_page_title(page["id"], new_title, user["id"])
            log_action("edit_page_title", request, user=user, page=slug, new_title=new_title)
            notify_change("page_title_edit", f"Page '{slug}' title changed to '{new_title}'")
            emit_hook("after_page_update", page=db.get_page(page["id"]), user=user)
            flash(t("flash.page_title_has_been_successfully_updated"), "success")
        return redirect(url_for("view_page", slug=slug))


    @app.route("/create-page", methods=["GET", "POST"])
    @login_required
    @editor_required
    @rate_limit(20, 60)
    def create_page():
        """Display the new-page form and handle page creation."""
        user = get_current_user()

        if request.method == "POST":
            title = request.form.get("title", "").strip()
            content = request.form.get("content", "")
            cat_id = request.form.get("category_id")
            form_data = {
                "title": title, "content": content, "category_id": cat_id or "",
                "difficulty_tag": request.form.get("difficulty_tag", ""),
                "tag_custom_label": request.form.get("tag_custom_label", ""),
                "tag_custom_color": request.form.get("tag_custom_color", "#4a90d9"),
            }
            try:
                cat_id = int(cat_id) if cat_id else None
            except (TypeError, ValueError):
                flash(t("flash.invalid_category"), "error")
                return render_template("wiki/create_page.html", form=form_data)
            if len(content) > _MAX_PAGE_CONTENT_LENGTH:
                flash(t("flash.page_content_is_too_large_maximum_1_mb"), "error")
                return render_template("wiki/create_page.html", form=form_data)
            if not title:
                flash(t("flash.title_is_required"), "error")
                return render_template("wiki/create_page.html", form=form_data)
            if len(title) > 200:
                flash(t("flash.page_title_cannot_exceed_200_characters"), "error")
                return render_template("wiki/create_page.html", form=form_data)
            if cat_id and not db.get_category(cat_id):
                flash(t("flash.selected_category_does_not_exist"), "error")
                return render_template("wiki/create_page.html", form=form_data)
            if not editor_has_category_access(user, cat_id):
                flash(t("flash.you_do_not_have_permission_to_create_pages"), "error")
                return render_template("wiki/create_page.html", form=form_data)
            slug = slugify(title)
            # ensure unique slug
            base_slug = slug
            counter = 1
            while db.get_page_by_slug(slug):
                slug = f"{base_slug}-{counter}"
                counter += 1
            # Take the id straight from create_page(): looking the slug up
            # again can return None and blow up on the subscript.
            page_id = db.create_page(title, slug, content, cat_id, user["id"])
            # Apply initial difficulty tag if specified (only when plugin is enabled)
            if getattr(g, 'enabled_plugins', {}).get('difficulty_tags'):
                tag = request.form.get("difficulty_tag", "").strip().lower()
                if tag in db.VALID_DIFFICULTY_TAGS and tag:
                    custom_label = ""
                    custom_color = ""
                    if tag == "custom":
                        custom_label = request.form.get("tag_custom_label", "").strip()[:50]
                        custom_color = request.form.get("tag_custom_color", "").strip()
                        if not custom_label or not _is_valid_hex_color(custom_color):
                            tag = ""
                    if tag:
                        db.update_page_tag(page_id, tag, custom_label, custom_color)
            cleanup_unused_uploads()
            log_action("create_page", request, user=user, page=slug)
            notify_change("page_create", f"Page '{slug}' created")
            new_page = db.get_page(page_id)
            emit_hook("after_page_create", page=new_page, user=user)

            # Check and award auto-triggered badges
            newly_awarded = db.check_and_award_auto_badges(user["id"])
            if newly_awarded:
                session["badge_notifications"] = session.get("badge_notifications", 0) + len(newly_awarded)

            flash(t("flash.page_has_been_successfully_created_you_can_now"), "success")
            return redirect(url_for("view_page", slug=slug))

        return render_template("wiki/create_page.html")


    @app.route("/page/<slug>/delete", methods=["POST"])
    @login_required
    @editor_required
    @rate_limit(10, 60)
    def delete_page_route(slug):
        """Delete a wiki page."""
        page = db.get_page_by_slug(slug)
        if not page:
            abort(404)
        user = get_current_user()
        if page["is_home"]:
            flash(t("flash.cannot_delete_the_home_page"), "error")
            return redirect(url_for("view_page", slug=slug))
        # Editing pages does not include deleting them: editors need the
        # separate page.delete permission, which they do not get by default.
        if not db.has_permission(user, "page.delete"):
            flash(
                t("flash.page_delete_permission_required",
                  default="You do not have permission to delete pages."),
                "error",
            )
            return redirect(url_for("view_page", slug=slug))
        if not editor_has_category_access(user, page["category_id"]):
            flash(t("flash.you_do_not_have_permission_to_delete_pages"), "error")
            return redirect(url_for("view_page", slug=slug))
        blocked_response = _guard_destructive_page_edit(page, user, t("flash.action.delete_this_page"))
        if blocked_response:
            return blocked_response
        # Block manual deletion when a scheduled auto-deletion is pending.
        temp_expiry = db.get_page_expiry(page["id"])
        if temp_expiry is not None:
            flash(
                t("flash.this_page_is_scheduled_for_automatic_deletion_remove"),
                "error",
            )
            return redirect(url_for("view_page", slug=slug))
        # Block deletion when page is already in pending-deletion state.
        if page["pending_deletion"]:
            flash(t("flash.this_page_is_already_pending_deletion"), "error")
            return redirect(url_for("view_page", slug=slug))
        # When deletion_slowdown plugin is enabled, route through the grace period
        # instead of immediately deleting the page.
        # Exception: docs pages bypass the slowdown when the flag is set.
        _bypass_slowdown = (
            db.is_docs_category(page["category_id"])
            and db.get_site_settings().get("docs_bypass_deletion_slowdown")
        )
        if db.is_plugin_enabled("deletion_slowdown") and not _bypass_slowdown:
            if not db.mark_page_pending_deletion(page["id"], user["id"]):
                flash(t("flash.cannot_delete_the_home_page"), "error")
                return redirect(url_for("view_page", slug=slug))
            log_action("page_pending_deletion", request, user=user, page=slug)
            notify_change("page_pending_delete", f"Page '{slug}' queued for deletion (48h grace period)")
            emit_hook("after_page_delete", page=page, user=user)
            flash(
                t("flash.page_has_been_queued_for_deletion_it_will", PENDING_DELETION_HOURS=db.PENDING_DELETION_HOURS),
                "success",
            )
            return redirect(url_for("view_page", slug=slug))
        db.delete_page(page["id"])
        cleanup_unused_uploads()
        log_action("delete_page", request, user=user, page=slug)
        notify_change("page_delete", f"Page '{slug}' deleted")
        emit_hook("after_page_delete", page=page, user=user)
        flash(t("flash.page_deleted"), "success")
        return redirect(url_for("home"))


    @app.route("/page/<slug>/set-home", methods=["POST"])
    @login_required
    @admin_required
    @rate_limit(10, 60)
    def set_home_page_route(slug):
        """Set a page as the home page (admin only)."""
        page = db.get_page_by_slug(slug)
        if not page:
            abort(404)
        user = get_current_user()
        if user["role"] not in ("admin", "owner"):
            flash(t("flash.you_do_not_have_the_required_permissions_to_perform"), "error")
            return redirect(url_for("view_page", slug=slug))
        if page["is_home"]:
            db.set_home_page(page["id"])
            flash(t("flash.this_page_is_already_the_home_page"), "info")
            return redirect(url_for("view_page", slug=slug))
        try:
            home_page = db.set_home_page(page["id"])
        except ValueError:
            flash(t("flash.page_not_found"), "error")
            return redirect(url_for("home"))
        log_action("set_home_page", request, user=user, page=slug)
        notify_change("set_home_page", f"Page '{home_page['title']}' is now the home page")
        flash(t("flash.page_is_now_the_home_page", title=home_page["title"]), "success")
        return redirect(url_for("home"))


    @app.route("/page/<slug>/move", methods=["POST"])
    @login_required
    @editor_required
    @rate_limit(20, 60)
    def move_page(slug):
        """Move a wiki page to a different category."""
        page = db.get_page_by_slug(slug)
        if not page:
            abort(404)
        if page["is_home"]:
            flash(t("flash.cannot_move_the_home_page"), "error")
            return redirect(url_for("view_page", slug=slug))
        cat_id = request.form.get("category_id")
        try:
            cat_id = int(cat_id) if cat_id else None
        except (TypeError, ValueError):
            flash(t("flash.invalid_category"), "error")
            return redirect(url_for("view_page", slug=slug))
        if cat_id and not db.get_category(cat_id):
            flash(t("flash.selected_category_does_not_exist"), "error")
            return redirect(url_for("view_page", slug=slug))
        user = get_current_user()
        if not editor_has_category_access(user, page["category_id"]):
            flash(t("flash.you_do_not_have_permission_to_move_pages"), "error")
            return redirect(url_for("view_page", slug=slug))
        if not editor_has_category_access(user, cat_id):
            flash(t("flash.you_do_not_have_permission_to_move_pages_4f862e"), "error")
            return redirect(url_for("view_page", slug=slug))
        db.update_page_category(page["id"], cat_id)
        log_action("move_page", request, user=user, page=slug, category_id=cat_id)
        notify_change("page_move", f"Page '{slug}' moved")
        flash(t("flash.page_moved"), "success")
        return redirect(url_for("view_page", slug=slug))


    @app.route("/page/<slug>/tag", methods=["POST"])
    @login_required
    @editor_required
    @rate_limit(20, 60)
    def update_page_tag(slug):
        """Set or update the difficulty tag for a wiki page."""
        page = db.get_page_by_slug(slug)
        if not page:
            abort(404)
        user = get_current_user()
        if not editor_has_category_access(user, page["category_id"]):
            flash(t("flash.you_do_not_have_permission_to_edit_pages"), "error")
            return redirect(url_for("view_page", slug=slug))
        blocked_response = _guard_destructive_page_edit(page, user, t("flash.action.change_this_page_tag"))
        if blocked_response:
            return blocked_response
        tag = request.form.get("difficulty_tag", "").strip().lower()
        if tag not in db.VALID_DIFFICULTY_TAGS:
            flash(t("flash.invalid_difficulty_tag"), "error")
            return redirect(url_for("view_page", slug=slug))
        custom_label = ""
        custom_color = ""
        if tag == "custom":
            custom_label = request.form.get("tag_custom_label", "").strip()[:50]
            custom_color = request.form.get("tag_custom_color", "").strip()
            if not custom_label:
                flash(t("flash.custom_tag_requires_a_label_to_be_specified"), "error")
                return redirect(url_for("view_page", slug=slug))
            if not _is_valid_hex_color(custom_color):
                flash(t("flash.custom_tag_requires_a_valid_hex_color_code"), "error")
                return redirect(url_for("view_page", slug=slug))
        db.update_page_tag(page["id"], tag, custom_label, custom_color)
        log_action("update_page_tag", request, user=user, page=slug, tag=tag)
        notify_change("page_tag", f"Page '{slug}' tag updated to '{tag}'")
        flash(t("flash.tag_updated"), "success")
        return redirect(url_for("view_page", slug=slug))


    @app.route("/page/<slug>/rename", methods=["POST"])
    @login_required
    @editor_required
    @rate_limit(10, 60)
    def rename_page_slug(slug):
        """Rename the URL slug of a page and rewrite all internal links atomically."""
        page = db.get_page_by_slug(slug)
        if not page:
            abort(404)
        user = get_current_user()
        if page["is_home"]:
            flash(t("flash.cannot_change_the_url_of_the_home_page"), "error")
            return redirect(url_for("view_page", slug=slug))
        if not editor_has_category_access(user, page["category_id"]):
            flash(t("flash.you_do_not_have_permission_to_edit_pages"), "error")
            return redirect(url_for("view_page", slug=slug))
        blocked_response = _guard_destructive_page_edit(page, user, t("flash.action.change_this_page_url"))
        if blocked_response:
            return blocked_response
        new_slug = request.form.get("new_slug", "").strip()
        if not new_slug:
            flash(t("flash.new_url_slug_is_required"), "error")
            return redirect(url_for("view_page", slug=slug))
        new_slug = slugify(new_slug)
        if not new_slug:
            flash(t("flash.invalid_slug"), "error")
            return redirect(url_for("view_page", slug=slug))
        if new_slug == slug:
            flash(t("flash.new_url_is_the_same_as_the_current"), "info")
            return redirect(url_for("view_page", slug=slug))
        if db.get_page_by_slug(new_slug):
            flash(t("flash.that_url_slug_is_already_in_use_by"), "error")
            return redirect(url_for("view_page", slug=slug))
        db.update_page_slug(page["id"], new_slug)
        log_action("rename_page_slug", request, user=user, page=slug, new_slug=new_slug)
        notify_change("page_rename", f"Page '{slug}' renamed to '{new_slug}'")
        emit_hook("after_page_update", page=db.get_page(page["id"]), user=user)
        flash(t("flash.page_url_updated_all_internal_links_have_been"), "success")
        return redirect(url_for("view_page", slug=new_slug))


    @app.route("/page/<slug>/deindex", methods=["POST"])
    @login_required
    @editor_required
    @rate_limit(20, 60)
    def toggle_page_deindex(slug):
        """Toggle the deindexed flag on a wiki page (hide from sidebar and search)."""
        page = db.get_page_by_slug(slug)
        if not page:
            abort(404)
        user = get_current_user()
        if page["is_home"]:
            flash(t("flash.the_home_page_cannot_be_deindexed"), "error")
            return redirect(url_for("view_page", slug=slug))
        if not editor_has_category_access(user, page["category_id"]):
            flash(t("flash.you_do_not_have_permission_to_edit_pages"), "error")
            return redirect(url_for("view_page", slug=slug))
        blocked_response = _guard_destructive_page_edit(page, user, t("flash.action.change_this_page_visibility"))
        if blocked_response:
            return blocked_response
        # Block manual toggle when the page has an active temporary index state.
        temp_idx = db.get_page_temp_index_state(page["id"])
        if temp_idx is not None:
            flash(
                t("flash.this_page_has_a_temporary_index_state_scheduled"),
                "error",
            )
            return redirect(url_for("view_page", slug=slug))
        new_state = not bool(page["is_deindexed"])
        db.set_page_deindexed(page["id"], new_state)
        action = "deindexed" if new_state else "reindexed"
        log_action(f"page_{action}", request, user=user, page=slug)
        notify_change("page_deindex", f"Page '{slug}' {action}")
        flash(t("flash.page_action", action=action), "success")
        updated_page = db.get_page(page["id"])
        if user_can_view_page(user, updated_page):
            return redirect(url_for("view_page", slug=slug))
        return redirect(url_for("home"))
