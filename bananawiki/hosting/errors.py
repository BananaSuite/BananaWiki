"""The error type services raise for anything the user should be told."""

from __future__ import annotations

from typing import Any


class ServiceError(Exception):
    """A refused action. ``key`` is a translation key, ``values`` its placeholders."""

    def __init__(self, key: str, **values: Any):
        self.key = key
        self.values = values
        super().__init__(key)
