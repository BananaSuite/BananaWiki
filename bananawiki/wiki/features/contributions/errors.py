"""The error raised by contribution services."""

from __future__ import annotations

from typing import Any


class ContributionError(ValueError):
    """A refused contribution action; ``key`` is a translation key."""

    def __init__(self, key: str, **values: Any):
        super().__init__(key)
        self.key = key
        self.values = values
