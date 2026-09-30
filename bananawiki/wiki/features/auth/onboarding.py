"""The administrator's first-run wizard: basics, features, first users, built-in docs."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from flask import current_app

from ....core.timeutil import now_sql
from ... import accounts, i18n, settings
from ...db import db
from ...registry import Feature, emit, is_enabled, registry, set_enabled
from . import docs

MAX_SITE_NAME = 100
INITIAL_USER_ROWS = 3
INITIAL_USER_ROLES = ("user", "editor", "admin")


@dataclass
class Choices:
    site_name: str
    language: str
    theme: str
    mode: str
    features: set[str]
    new_user_intro: bool
    docs_variant: str | None
    docs_language: str
    users: list[dict[str, Any]] = field(default_factory=list)


def switchable_features() -> list[Feature]:
    """Features the wizard may switch: not core, and not hidden by the operator."""
    cfg = current_app.config["BW"]
    return [
        feature for feature in registry().ordered()
        if feature.toggle != "always"
        and not (cfg.easy_wiki and not feature.easy_wiki)
        and feature.id not in cfg.managed_plugin_denylist
    ]


def feature_options() -> list[dict[str, Any]]:
    return [{"id": f.id, "name": f.name, "description": f.description, "enabled": is_enabled(f.id)}
            for f in switchable_features()]


def _initial_users(form: Any) -> list[dict[str, Any]]:
    names = form.getlist("new_username")
    secrets = form.getlist("new_password")
    roles = form.getlist("new_role")
    forced = set(form.getlist("new_force_password_change"))
    rows = []
    for index, raw_name in enumerate(names[:INITIAL_USER_ROWS]):
        name = (raw_name or "").strip()
        password = secrets[index] if index < len(secrets) else ""
        if not name and not password:
            continue
        if not name or not password:
            raise accounts.AccountError("auth.onboarding.error.user_incomplete")
        role = roles[index] if index < len(roles) else "user"
        if role not in INITIAL_USER_ROLES:
            raise accounts.AccountError("auth.error.invalid_role")
        rows.append({"username": name, "password": password, "role": role,
                     "force_password_change": str(index) in forced})
    return rows


def parse(form: Any) -> Choices:
    """Read and validate the wizard form; raises :class:`accounts.AccountError`."""
    site_name = " ".join((form.get("site_name") or "").split()) or "BananaWiki"
    if len(site_name) > MAX_SITE_NAME:
        raise accounts.AccountError("auth.onboarding.error.site_name_too_long", limit=MAX_SITE_NAME)
    language = form.get("language") or ""
    if language not in i18n.enabled_languages():
        language = settings.get("interface_language") or "en"
    theme = form.get("default_theme_mode") if form.get("default_theme_mode") in ("dark", "light") else "dark"
    mode = "advanced" if form.get("mode") == "advanced" else "easy"
    docs_variant = form.get("docs_variant") if form.get("spawn_docs") else None
    if docs_variant is not None and docs_variant not in docs.VARIANTS:
        docs_variant = "full"
    docs_language = form.get("docs_language") if form.get("docs_language") in docs.LANGUAGES else "en"
    return Choices(
        site_name=site_name, language=language, theme=theme, mode=mode,
        features=set(form.getlist("features")), new_user_intro=bool(form.get("new_user_intro_enabled")),
        docs_variant=docs_variant, docs_language=docs_language, users=_initial_users(form),
    )


def apply(choices: Choices, admin: dict[str, Any]) -> list[dict[str, Any]]:
    """Save the wizard's choices; return the accounts it created."""
    prepared = [
        dict(row, password_hash=accounts.hash_password(accounts.validate_password(row["password"])))
        for row in choices.users
    ]
    created = []
    with db.transaction():
        settings.update({
            "site_name": choices.site_name,
            "interface_language": choices.language,
            "default_theme_mode": choices.theme,
            "new_user_intro_enabled": 1 if choices.new_user_intro else 0,
        })
        for row in prepared:
            created.append(accounts.create(
                row["username"], None, role=row["role"], password_hash=row["password_hash"],
                force_password_change=row["force_password_change"], emit_event=False,
            ))
        for feature in switchable_features():
            wanted = feature.default_enabled if choices.mode == "easy" else feature.id in choices.features
            if wanted != is_enabled(feature.id):
                set_enabled(feature.id, wanted)
        now = now_sql()
        db.execute(
            "UPDATE users SET onboarding_required = 0, onboarding_completed_at = ?, intro_required = 1 WHERE id = ?",
            (now, admin["id"]),
        )
    for user in created:
        emit("user.created", user=user)
    if choices.docs_variant:
        docs.spawn(variant=choices.docs_variant, language=choices.docs_language, actor_id=admin["id"])
    return created


def mark_intro_for_new_user(user: dict[str, Any]) -> None:
    """``user.created`` handler: new accounts see the introduction when the site asks for it."""
    if settings.get("new_user_intro_enabled") and not user.get("intro_required"):
        db.execute("UPDATE users SET intro_required = 1 WHERE id = ?", (user["id"],))
