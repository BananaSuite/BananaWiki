"""JSON decoding shared by HTTP endpoints in both applications.

Python's decoder accepts non-finite numbers and unpaired Unicode surrogates.
Neither can safely be passed to storage or the document consumers. Reject
them, including in ignored fields and object keys, before a view runs. A
nesting limit also bounds work in consumers that recursively walk JSON.
"""

from __future__ import annotations

import json
import math
from itertools import chain
from typing import Any

from flask.json.provider import DefaultJSONProvider

MAX_JSON_DEPTH = 64


def _reject_constant(_value: str) -> Any:
    raise ValueError("JSON numbers must be finite")


def validate(value: Any) -> None:
    """Validate decoded JSON without recursive calls or a breadth-sized stack."""
    frames = [(iter((value,)), 0)]
    while frames:
        children, depth = frames[-1]
        try:
            item = next(children)
        except StopIteration:
            frames.pop()
            continue
        if isinstance(item, str):
            item.encode("utf-8")
        elif isinstance(item, float) and not math.isfinite(item):
            raise ValueError("JSON numbers must be finite")
        elif isinstance(item, (dict, list)):
            if depth >= MAX_JSON_DEPTH:
                raise ValueError("JSON nesting is too deep")
            contents = chain(item.keys(), item.values()) if isinstance(item, dict) else iter(item)
            frames.append((iter(contents), depth + 1))


class SafeJSONProvider(DefaultJSONProvider):
    """Make unsafe JSON behave like other parse errors, including silent reads."""

    def loads(self, value: str | bytes, **kwargs: Any) -> Any:
        return loads(value, **kwargs)


def loads(value: str | bytes, **kwargs: Any) -> Any:
    """Decode a bounded-depth JSON document from an HTTP body or uploaded file."""
    kwargs.setdefault("parse_constant", _reject_constant)
    try:
        decoded = json.loads(value, **kwargs)
        validate(decoded)
    except (UnicodeError, RecursionError) as error:
        # Request.get_json catches ValueError and translates it into the
        # endpoint's normal malformed-body response. Do not leak input.
        raise ValueError("Invalid JSON text") from error
    return decoded
