"""Site settings (administrators, ``settings`` scope). See :mod:`..settings_rules`."""

from __future__ import annotations

from flask import jsonify

from .... import settings
from .. import settings_rules
from ..errors import ApiError, json_body
from . import bp, ok, require_admin, requires


@bp.get("/settings")
@requires("settings")
def get_settings():
    require_admin()
    return ok(settings=settings_rules.readable(settings.load()))


@bp.put("/settings")
@requires("settings", write=True)
def update_settings():
    """All or nothing: any invalid value (400) or refused key (403) and nothing is saved."""
    require_admin()
    data = json_body()
    if not data:
        raise ApiError(400, "no_settings")
    updates, invalid, refused = settings_rules.check(data, settings.load())
    if invalid:
        error = ApiError(400, "invalid_settings", keys=", ".join(sorted(invalid)))
        return jsonify({**error.payload(), "invalid": invalid}), 400
    if refused:
        error = ApiError(403, "refused_settings", keys=", ".join(sorted(refused)))
        return jsonify({**error.payload(), "refused": refused}), 403
    if updates:
        settings.update(settings_rules.with_companions(updates))
    return ok(updated=sorted(updates))
