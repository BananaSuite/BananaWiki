"""API errors and request-body validation.

Every error the API returns has the same shape::

    {"ok": false, "error": "<translated message>", "code": "<stable code>", ...}

``code`` is meant for programs and never changes; ``error`` is for people
and follows the caller's language. Some errors add fields (``invalid``,
``refused``, ``scope``…), documented in API.md.
"""

from __future__ import annotations

from typing import Any

from flask import current_app, g, jsonify, request
from werkzeug.exceptions import RequestEntityTooLarge

from ...i18n import t

# SQLite stores ids as signed 64-bit integers.
MAX_ROW_ID = 2**63 - 1
MAX_JSON_BODY = 2 * 1024 * 1024
MAX_PAGE_SIZE = 500


class ApiError(Exception):
    """A refused API request. Raise it anywhere below a view."""

    def __init__(self, status: int, code: str, message_key: str | None = None, *,
                 extra: dict[str, Any] | None = None, **values: Any):
        super().__init__(code)
        self.status = status
        self.code = code
        self.message_key = message_key or f"api_service.error.{code}"
        self.values = values
        self.extra = extra or {}

    def payload(self) -> dict[str, Any]:
        return {"ok": False, "error": t(self.message_key, **self.values), "code": self.code, **self.extra}

    def response(self):
        return jsonify(self.payload()), self.status


def invalid(field: str, reason: str, **values: Any) -> ApiError:
    """400 for one bad input field; *reason* is a key under ``api_service.invalid.``."""
    return ApiError(400, "invalid_input", "api_service.error.invalid_input",
                    extra={"field": field}, field=field,
                    reason=t(f"api_service.invalid.{reason}", **values))


def from_service(error: Exception, status: int = 400) -> ApiError:
    """Wrap a service error (``PageError``, ``AccountError``…) that carries a translation key."""
    key = getattr(error, "key", "error.generic.body")
    return ApiError(status, key.rsplit(".", 1)[-1], key, **getattr(error, "values", {}))


def json_body() -> dict[str, Any]:
    """The request's JSON object, read with a size cap even without Content-Length."""
    app_limit = current_app.config.get("MAX_CONTENT_LENGTH")
    limit = MAX_JSON_BODY if not app_limit else min(app_limit, MAX_JSON_BODY)
    if request.content_length is not None and request.content_length > limit:
        raise ApiError(413, "body_too_large")
    request.max_content_length = limit
    try:
        raw = request.get_data(cache=True)
    except RequestEntityTooLarge:
        raise ApiError(413, "body_too_large") from None
    if len(raw) >= limit:
        raise ApiError(413, "body_too_large")
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise ApiError(400, "body_not_object")
    g.api_body = data  # the audit log keeps a redacted summary
    return data


def flag(value: Any, field: str) -> bool:
    """A JSON boolean; 0 and 1 are accepted for older clients."""
    if isinstance(value, bool):
        return value
    if type(value) is int and value in (0, 1):
        return bool(value)
    raise invalid(field, "boolean")


def text(value: Any, field: str, *, maximum: int, required: bool = False, strip: bool = True) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise invalid(field, "string")
    if strip:
        value = value.strip()
    if required and not value:
        raise invalid(field, "required")
    if len(value) > maximum:
        raise invalid(field, "too_long", maximum=maximum)
    return value


def row_id(value: Any, field: str) -> int:
    """A positive row id sent as a JSON number or a string of digits."""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    elif isinstance(value, str):
        digits = value.strip()
        if not (digits.isascii() and digits.isdigit() and len(digits) <= 19):
            raise invalid(field, "id")
        value = int(digits)
    if type(value) is not int or not 0 < value <= MAX_ROW_ID:
        raise invalid(field, "id")
    return value


def optional_id(value: Any, field: str) -> int | None:
    """A row id, or None for the values that mean "none" (null, "", 0)."""
    if value in (None, "", 0):
        return None
    return row_id(value, field)


def items(data: dict[str, Any], field: str, limit: int) -> list[Any]:
    """``data[field]`` as a list of at most *limit* items."""
    value = data.get(field, [])
    if not isinstance(value, list):
        raise invalid(field, "array")
    if len(value) > limit:
        raise invalid(field, "too_many", maximum=limit)
    return value


def query_int(name: str, default: int, low: int, high: int) -> int:
    """An integer query parameter clamped to [low, high]."""
    raw = request.args.get(name, "")
    try:
        value = int(raw) if raw.strip() else default
    except ValueError:
        raise invalid(name, "integer") from None
    return max(low, min(high, value))


def page_window() -> tuple[int, int]:
    """``(limit, offset)`` of a listing. A missing or zero ``limit`` means the largest page, never everything."""
    limit = query_int("limit", 0, 0, MAX_PAGE_SIZE) or MAX_PAGE_SIZE
    return limit, query_int("offset", 0, 0, 2**31 - 1)


def window_fields(limit: int, offset: int, fetched: int) -> dict[str, Any]:
    """What a listing adds to its answer; *fetched* rows were read with ``limit + 1``."""
    return {"limit": limit, "offset": offset, "next_offset": offset + limit if fetched > limit else None}
