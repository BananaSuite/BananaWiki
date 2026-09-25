"""Contributor leaderboard route."""

from datetime import datetime, timedelta

from flask import (
    abort,
    render_template,
    request,
    Response,
)
from helpers._auth import login_required, get_current_user
from helpers._rate_limiting import rate_limit
import db


def register_leaderboard_routes(app):
    """Register contributor leaderboard routes on the Flask app."""
    @app.route("/leaderboard")
    @login_required
    @rate_limit(30, 60)
    def leaderboard():
        """Display the contributor leaderboard."""
        settings = db.get_site_settings() or {}
        if not settings.get("contributor_leaderboard_enabled"):
            abort(404)

        user = get_current_user()
        is_admin = user["role"] in ("admin", "owner")

        sort_by = request.args.get("sort", "score")
        if sort_by not in db.LEADERBOARD_SORTS:
            sort_by = "score"

        date_range = request.args.get("range", "all")
        if date_range not in db.LEADERBOARD_DATE_RANGES:
            date_range = "all"
        days = db.LEADERBOARD_DATE_RANGES[date_range]
        since = None
        if days is not None:
            since = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d")

        leaderboard_result = db.get_leaderboard_stats(
            limit=250,
            sort_by=sort_by,
            include_user_id=user["id"],
            since=since,
        )
        data = leaderboard_result["entries"]
        podium = leaderboard_result["podium"]
        summary = leaderboard_result["summary"]
        current_user_entry = next(
            (entry for entry in data if entry["user_id"] == user["id"]),
            None,
        )

        # Look up profile publication status for each contributor
        profile_map = {}
        if db.is_plugin_enabled("user_profiles"):
            for entry in [*data, *podium]:
                uid = entry["user_id"]
                if uid not in profile_map:
                    profile = db.get_user_profile(uid)
                    profile_map[uid] = profile["page_published"] if profile else False

        return render_template(
            "leaderboard.html",
            leaderboard_data=data,
            podium=podium,
            summary=summary,
            sort_by=sort_by,
            sort_options=db.LEADERBOARD_SORTS,
            current_user_entry=current_user_entry,
            profile_map=profile_map,
            is_admin=is_admin,
            date_range=date_range,
            date_ranges=db.LEADERBOARD_DATE_RANGES,
        )

    @app.route("/leaderboard/export")
    @login_required
    @rate_limit(10, 60, exempt_html_nav=False)
    def leaderboard_export():
        """Export leaderboard data as CSV."""
        settings = db.get_site_settings() or {}
        if not settings.get("contributor_leaderboard_enabled"):
            abort(404)

        sort_by = request.args.get("sort", "score")
        if sort_by not in db.LEADERBOARD_SORTS:
            sort_by = "score"

        date_range = request.args.get("range", "all")
        if date_range not in db.LEADERBOARD_DATE_RANGES:
            date_range = "all"
        days = db.LEADERBOARD_DATE_RANGES[date_range]
        since = None
        if days is not None:
            since = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d")

        leaderboard_result = db.get_leaderboard_stats(
            limit=0,
            sort_by=sort_by,
            since=since,
        )
        csv_data = db.export_leaderboard_csv(leaderboard_result["entries"])
        return Response(
            csv_data,
            mimetype="text/csv",
            headers={"Content-Disposition": "attachment; filename=leaderboard.csv"},
        )
