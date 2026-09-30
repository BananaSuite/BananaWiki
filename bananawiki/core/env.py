"""Typed readers for environment variables.

Every setting BananaWiki reads from the environment goes through these helpers
so that invalid values fail loudly at startup instead of silently falling back
to a default that the operator did not choose.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off", ""}


class ConfigError(ValueError):
    """An environment variable holds a value BananaWiki cannot use."""


class Env:
    """Read typed values from a mapping (``os.environ`` by default)."""

    def __init__(self, source: Mapping[str, str] | None = None):
        self._source = os.environ if source is None else source

    def raw(self, name: str, *aliases: str) -> str | None:
        for key in (name, *aliases):
            value = self._source.get(key)
            if value is not None:
                return value
        return None

    def str(self, name: str, default: str = "", *aliases: str) -> str:
        value = self.raw(name, *aliases)
        return default if value is None else value.strip()

    def bool(self, name: str, default: bool = False, *aliases: str) -> bool:
        value = self.raw(name, *aliases)
        if value is None:
            return default
        lowered = value.strip().lower()
        if lowered in _TRUE:
            return True
        if lowered in _FALSE:
            return False
        raise ConfigError(f"{name} must be one of 1/0, true/false, yes/no or on/off (got {value!r}).")

    def int(
        self,
        name: str,
        default: int,
        *,
        minimum: int | None = None,
        maximum: int | None = None,
    ) -> int:
        value = self.raw(name)
        if value is None or not value.strip():
            return default
        try:
            number = int(value.strip())
        except ValueError:
            raise ConfigError(f"{name} must be an integer (got {value!r}).") from None
        if minimum is not None and number < minimum:
            raise ConfigError(f"{name} must be at least {minimum} (got {number}).")
        if maximum is not None and number > maximum:
            raise ConfigError(f"{name} must be at most {maximum} (got {number}).")
        return number

    def float(self, name: str, default: float, *, minimum: float | None = None) -> float:
        value = self.raw(name)
        if value is None or not value.strip():
            return default
        try:
            number = float(value.strip())
        except ValueError:
            raise ConfigError(f"{name} must be a number (got {value!r}).") from None
        if minimum is not None and number < minimum:
            raise ConfigError(f"{name} must be at least {minimum} (got {number}).")
        return number

    def choice(self, name: str, default: str, choices: set[str] | frozenset[str]) -> str:
        value = self.str(name, default).lower()
        if value not in choices:
            allowed = ", ".join(sorted(choices))
            raise ConfigError(f"{name} must be one of {allowed} (got {value!r}).")
        return value

    def list(self, name: str, default: str = "") -> tuple[str, ...]:
        value = self.str(name, default)
        return tuple(item.strip() for item in value.split(",") if item.strip())

    def path(self, name: str, default: str) -> str:
        value = self.str(name, "")
        return os.path.abspath(os.path.expanduser(value or default))
