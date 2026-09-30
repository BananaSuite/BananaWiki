"""Turning governance errors into translated, localised messages."""

from __future__ import annotations

from flask import flash

from ...i18n import t
from ...templating import format_datetime
from .errors import GovernanceError

_TIMESTAMP_VALUES = ("until", "ready_at")


def error_text(exc: GovernanceError) -> str:
    values = {key: format_datetime(value) if key in _TIMESTAMP_VALUES else value
              for key, value in exc.values.items()}
    return t(exc.key, **values)


def flash_error(exc: GovernanceError) -> None:
    flash(error_text(exc), "error")
