"""Declarative form fields for site settings.

A :class:`Field` knows how to read its value from a submitted form, check it
and explain the problem, and whether the host has locked it. Views validate a
whole form first and only then write, so a refused form changes nothing.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from ....core.timeutil import is_future, to_sql
from ...templating import from_local_input

_EXTENSION = re.compile(r"^[a-z0-9]{1,16}$")
MB = 1024 * 1024


class FieldError(ValueError):
    """A submitted value was refused; ``key`` is a translation key."""

    def __init__(self, key: str, **values: Any):
        super().__init__(key)
        self.key = key
        self.values = values


@dataclass(frozen=True)
class Field:
    """One form control bound to a ``site_settings`` column.

    ``kind`` selects the control: flag, number, choice, text, textarea,
    until (a future date and time), roles, extensions or megabytes (stored
    in bytes). ``locked`` returns a translation key explaining why the host
    does not allow changes, or None. ``depends_on`` names a flag: when that
    flag is off the value is cleared instead of validated (expiry dates).
    """

    name: str
    kind: str
    minimum: int = 0
    maximum: int = 0
    options: tuple[str, ...] = ()
    max_length: int = 0
    depends_on: str | None = None
    locked: Callable[[], str | None] | None = None
    visible: Callable[[], bool] | None = None
    empty: Any = None
    choices: Callable[[], tuple[str, ...]] | None = field(default=None, compare=False)

    @property
    def label(self) -> str:
        return f"site_admin.settings.{self.name}"

    @property
    def hint(self) -> str:
        return f"site_admin.settings.{self.name}.hint"

    def lock_reason(self) -> str | None:
        return self.locked() if self.locked else None

    def shown(self) -> bool:
        return self.visible() if self.visible else True

    def allowed_options(self) -> tuple[str, ...]:
        return self.choices() if self.choices else self.options

    def display(self, stored: Any) -> Any:
        """The stored value as the form shows it."""
        if self.kind == "megabytes":
            return int(stored or 0) // MB
        if self.kind == "roles":
            return [part.strip() for part in str(stored or "").split(",") if part.strip()]
        return stored

    def parse(self, form: Mapping[str, Any], flags: Mapping[str, int]) -> Any:
        raw = form.get(self.name)
        if self.kind == "flag":
            return 1 if raw in ("1", "on", "true") else 0
        if self.depends_on is not None and not flags.get(self.depends_on):
            return self.empty
        parser = getattr(self, f"_parse_{self.kind}")
        return parser(form, (raw or "").strip() if isinstance(raw, str) else "")

    # Parsers ----------------------------------------------------------

    def _parse_number(self, _form: Mapping[str, Any], text: str) -> int:
        try:
            value = int(text)
        except ValueError:
            raise FieldError("site_admin.error.number", minimum=self.minimum, maximum=self.maximum) from None
        if not self.minimum <= value <= self.maximum:
            raise FieldError("site_admin.error.number", minimum=self.minimum, maximum=self.maximum)
        return value

    def _parse_megabytes(self, form: Mapping[str, Any], text: str) -> int:
        return self._parse_number(form, text) * MB

    def _parse_choice(self, _form: Mapping[str, Any], text: str) -> str:
        if text not in self.allowed_options():
            raise FieldError("site_admin.error.choice")
        return text

    def _parse_text(self, _form: Mapping[str, Any], text: str) -> str:
        if len(text) > self.max_length:
            raise FieldError("site_admin.error.too_long", maximum=self.max_length)
        return text if text or self.empty is None else self.empty

    _parse_textarea = _parse_text

    def _parse_until(self, _form: Mapping[str, Any], text: str) -> str | None:
        if not text:
            return None
        moment = from_local_input(text)
        if moment is None:
            raise FieldError("site_admin.error.datetime")
        try:
            stored = to_sql(moment)
        except (OverflowError, ValueError):
            raise FieldError("site_admin.error.datetime") from None
        if not is_future(stored):
            raise FieldError("site_admin.error.past")
        return stored

    def _parse_roles(self, form: Mapping[str, Any], _text: str) -> str:
        getter = getattr(form, "getlist", None)
        chosen = set(getter(self.name)) if getter else set()
        if chosen - set(self.options):
            raise FieldError("site_admin.error.choice")
        return ",".join([role for role in self.options if role in chosen] or self.options)

    def _parse_extensions(self, _form: Mapping[str, Any], text: str) -> str:
        items = [item.strip().lower().lstrip(".") for item in re.split(r"[\s,;]+", text) if item.strip()]
        bad = [item for item in items if not _EXTENSION.match(item)]
        if bad:
            raise FieldError("site_admin.error.extension", value=bad[0][:20])
        value = ",".join(dict.fromkeys(items))
        if len(value) > self.max_length:
            raise FieldError("site_admin.error.too_long", maximum=self.max_length)
        return value


def parse_form(fields: list[Field], form: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, FieldError]]:
    """Read every unlocked, visible field; return ``(values, errors)``."""
    active = [f for f in fields if f.shown() and not f.lock_reason()]
    flags = {f.name: f.parse(form, {}) for f in active if f.kind == "flag"}
    values: dict[str, Any] = dict(flags)
    errors: dict[str, FieldError] = {}
    for item in active:
        if item.kind == "flag":
            continue
        try:
            values[item.name] = item.parse(form, flags)
        except FieldError as error:
            errors[item.name] = error
    return values, errors
