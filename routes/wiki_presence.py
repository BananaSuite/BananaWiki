"""Wiki presence routes."""

from datetime import datetime, timezone
from flask import (
    request, jsonify,
)
import db
from helpers import (
    login_required, get_current_user,
    user_can_view_page, render_markdown, rate_limit,
    format_datetime, time_ago,
)



def register_wiki_presence_routes(app):
    """Register presence endpoints on the application."""

    @app.route("/api/page/<slug>/sync")
    @login_required
    @rate_limit(120, 60)
    def page_sync(slug):
        """Return page changes since the client's last poll.

        Clients send ``?since=<last_edited_at>``: an ISO-8601 UTC timestamp
        of the last version they saw.  Returns ``{changed: false}`` when the
        page hasn't changed, or full page data when it has.

        This lightweight endpoint lets wiki page viewers see edits from other
        users without a manual refresh, following the same polling pattern
        used by the kanban and canvas features.
        """
        page = db.get_page_by_slug(slug)
        if not page:
            return jsonify({"error": "Page not found"}), 404
        user = get_current_user()
        if not user_can_view_page(user, page):
            return jsonify({"error": "Forbidden"}), 403
        since_raw = (request.args.get("since") or "").strip()
        if not since_raw:
            return jsonify({"changed": True, "reload": True})
        page_edited_at = page["last_edited_at"]
        if not page_edited_at:
            return jsonify({"changed": False})
        # URL parsers decode ``+`` to space, but our ISO timestamps use
        # ``+`` for the UTC offset sign (e.g. ``+00:00``).  Restore it
        # before parsing.
        since_clean = since_raw.replace(" ", "+")
        try:
            page_dt = datetime.fromisoformat(page_edited_at.replace("Z", "+00:00"))
            since_dt = datetime.fromisoformat(since_clean.replace("Z", "+00:00"))
            if page_dt.tzinfo is None:
                page_dt = page_dt.replace(tzinfo=timezone.utc)
            if since_dt.tzinfo is None:
                since_dt = since_dt.replace(tzinfo=timezone.utc)
            if page_dt <= since_dt:
                return jsonify({"changed": False})
        except (ValueError, TypeError):
            # Fall back to string comparison if parsing fails.
            if page_edited_at <= since_clean:
                return jsonify({"changed": False})
        content_html = render_markdown(page["content"], embed_videos=True)
        editor_info = None
        if page["last_edited_by"]:
            if str(page["last_edited_by"]) == str(db.SYSTEM_USER_ID):
                editor_info = {
                    "username": "the system",
                    "time_ago": time_ago(page["last_edited_at"]),
                    "edited_at": format_datetime(page["last_edited_at"]),
                }
            else:
                editor = db.get_user_by_id(page["last_edited_by"])
                if editor:
                    editor_info = {
                        "username": editor["username"],
                        "time_ago": time_ago(page["last_edited_at"]),
                        "edited_at": format_datetime(page["last_edited_at"]),
                    }
        return jsonify({
            "changed": True,
            "title": page["title"],
            "content_html": content_html,
            "last_edited_at": page_edited_at,
            "edited_by": editor_info,
        })


    @app.route("/api/page/<slug>/editing/heartbeat", methods=["POST"])
    @login_required
    @rate_limit(120, 60)
    def editing_heartbeat(slug):
        """Send a heartbeat to indicate the user is actively editing."""
        page = db.get_page_by_slug(slug)
        if not page:
            return jsonify({"error": "Page not found"}), 404
        user = get_current_user()
        if not user_can_view_page(user, page):
            return jsonify({"error": "Forbidden"}), 403
        db.heartbeat(page["id"], user["id"], user["username"])
        editors = db.get_active_editors(page["id"])
        return jsonify({
            "ok": True,
            "editors": [e["username"] for e in editors if e["user_id"] != user["id"]],
        })


    @app.route("/api/page/<slug>/editing/check")
    @login_required
    @rate_limit(120, 60)
    def editing_check(slug):
        """Check who is currently editing a page."""
        page = db.get_page_by_slug(slug)
        if not page:
            return jsonify({"error": "Page not found"}), 404
        user = get_current_user()
        if not user_can_view_page(user, page):
            return jsonify({"error": "Forbidden"}), 403
        editors = db.get_active_editors(page["id"])
        return jsonify({
            "editors": [e["username"] for e in editors if e["user_id"] != user["id"]],
        })


    @app.route("/api/page/<slug>/editing/stop", methods=["POST"])
    @login_required
    @rate_limit(120, 60)
    def editing_stop(slug):
        """Remove the user from the active editors list."""
        page = db.get_page_by_slug(slug)
        if not page:
            return jsonify({"error": "Page not found"}), 404
        user = get_current_user()
        db.remove_session(page["id"], user["id"])
        return jsonify({"ok": True})
