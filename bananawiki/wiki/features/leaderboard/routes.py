"""/leaderboard and its CSV export."""

from __future__ import annotations

from flask import Response, render_template, request

from ... import auth
from ...registry import feature_blueprint
from . import service

bp = feature_blueprint("leaderboard", "leaderboard", __name__, template_folder="templates")


def _options() -> tuple[str, str]:
    sort = request.args.get("sort", "score")
    range_key = request.args.get("range", "all")
    return (sort if sort in service.SORTS else "score"), (range_key if range_key in service.RANGES else "all")


@bp.get("/leaderboard")
def index():
    viewer = auth.current_user()
    sort, range_key = _options()
    result = service.ranking(viewer, sort=sort, range_key=range_key)
    entries = result["entries"]
    shown = entries[:service.LIMIT]
    mine = next((e for e in entries if e["user_id"] == viewer["id"]), None)
    if mine is not None and mine not in shown:
        shown.append(mine)
    service.attach_details(shown)
    return render_template(
        "leaderboard/index.html", entries=shown, summary=result["summary"], mine=mine,
        sort=sort, range_key=range_key, sorts=service.SORTS, ranges=service.RANGES,
        can_view_profiles=auth.is_admin(viewer) or auth.has_permission("profile.view", viewer),
    )


@bp.get("/leaderboard/export")
def export():
    sort, range_key = _options()
    result = service.ranking(auth.current_user(), sort=sort, range_key=range_key)
    return Response(service.to_csv(result["entries"]), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=leaderboard.csv"})
