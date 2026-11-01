"""Announcements: site-wide banners (tables ``announcements`` and
``announcement_audience_users``, unchanged from 1.4).

An announcement is shown while it is active and not expired, to signed-in
visitors, anonymous visitors or both (``visibility``), and optionally only
to (``allowlist``) or to everyone except (``denylist``) chosen accounts.
Every edit bumps ``revision`` so a banner a visitor dismissed shows again
after it changed. Creating, changing and deleting one is audited: a banner
reaches every member, owners included, and can carry links anywhere.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlsplit

from ....core.timeutil import is_future, now_sql, parse, sql_in, to_sql
from ...db import db
from ...markdown import render as render_markdown
from ...templating import datetime_local_input, from_local_input
from ..audit.service import record

COLORS = ("orange", "red", "yellow", "blue", "green")
SIZES = ("small", "normal", "large")
VISIBILITIES = ("both", "logged_in", "logged_out")
AUDIENCES = ("all", "allowlist", "denylist")
MAX_CONTENT = 2000
BAR_EXCERPT = 500  # longer announcements link to their own page
RETENTION_DAYS = 30
HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")
AUDIT_EXCERPT = 200
log = logging.getLogger("bananawiki.announcements")
_LINK = re.compile(r"(?i)(?:\b[a-z][a-z0-9+.-]*:)?//[^\s<>()\[\]\"']+")
_URL_SCHEME = re.compile(r"(?i)^([a-z][a-z0-9+.-]*):(.*)$")
_SPECIAL_SCHEMES = {"http", "https", "ws", "wss", "ftp"}
_C0_AND_SPACE = "".join(map(chr, range(0x21)))


@dataclass
class AnnouncementInput:
    content: str = ""
    color: str = "orange"
    text_size: str = "normal"
    visibility: str = "both"
    audience_mode: str = "all"
    audience_usernames: str = ""
    expires_at: str | None = None
    expires_input: str = ""
    is_active: bool = True
    not_removable: bool = True
    show_countdown: bool = True
    custom_colors: bool = False
    custom_background: str = "#333333"
    custom_text_color: str = "#ffffff"
    audience_user_ids: list[str] = field(default_factory=list)
    errors: list[tuple[str, dict[str, Any]]] = field(default_factory=list)


def _choice(value: str | None, allowed: tuple[str, ...]) -> str | None:
    return value if value in allowed else None


def parse_form(form: Any, *, editing: bool) -> AnnouncementInput:
    """Validate the create/edit form; problems are collected in ``errors``."""
    data = AnnouncementInput(
        content=(form.get("content") or "").strip(),
        audience_usernames=(form.get("audience_users") or "").strip(),
        expires_input=(form.get("expires_at") or "").strip(),
        is_active=bool(form.get("is_active")) if editing else True,
        not_removable=bool(form.get("not_removable")),
        show_countdown=bool(form.get("show_countdown")),
        custom_colors=bool(form.get("custom_colors")),
        custom_background=(form.get("custom_background") or "").strip(),
        custom_text_color=(form.get("custom_text_color") or "").strip(),
    )
    errors = data.errors
    if not data.content:
        errors.append(("announcements.error.content_required", {}))
    elif len(data.content) > MAX_CONTENT:
        errors.append(("announcements.error.content_too_long", {"maximum": MAX_CONTENT}))
    for name, allowed in (("color", COLORS), ("text_size", SIZES), ("visibility", VISIBILITIES),
                          ("audience_mode", AUDIENCES)):
        value = _choice(form.get(name, getattr(data, name)), allowed)
        if value is None:
            errors.append((f"announcements.error.invalid_{name}", {}))
        else:
            setattr(data, name, value)
    if data.custom_colors and not (HEX_COLOR.match(data.custom_background) and HEX_COLOR.match(data.custom_text_color)):
        errors.append(("announcements.error.invalid_custom_color", {}))
    if data.expires_input:
        moment = from_local_input(data.expires_input)
        if moment is None:
            errors.append(("announcements.error.invalid_expiry", {}))
        else:
            data.expires_at = to_sql(moment)
    _resolve_audience(data)
    return data


def _resolve_audience(data: AnnouncementInput) -> None:
    names = list(dict.fromkeys(n.strip() for n in re.split(r"[,\n]", data.audience_usernames) if n.strip()))
    if data.audience_mode == "all":
        return
    if not names:
        data.errors.append(("announcements.error.audience_required", {}))
        return
    unknown = []
    for name in names:
        row = db.one("SELECT id FROM users WHERE username = ? COLLATE NOCASE", (name,))
        if row is None:
            unknown.append(name)
        elif row["id"] not in data.audience_user_ids:
            data.audience_user_ids.append(row["id"])
    if unknown:
        data.errors.append(("announcements.error.unknown_users", {"names": ", ".join(unknown)}))


def _columns(data: AnnouncementInput) -> dict[str, Any]:
    return {
        "content": data.content, "color": data.color, "text_size": data.text_size, "visibility": data.visibility,
        "expires_at": data.expires_at, "is_active": 1 if data.is_active else 0,
        "not_removable": 1 if data.not_removable else 0, "show_countdown": 1 if data.show_countdown else 0,
        "audience_mode": data.audience_mode,
        "custom_background": data.custom_background if data.custom_colors else None,
        "custom_text_color": data.custom_text_color if data.custom_colors else None,
    }


def _set_audience(announcement_id: int, user_ids: list[str]) -> None:
    db.execute("DELETE FROM announcement_audience_users WHERE announcement_id = ?", (announcement_id,))
    db.executemany("INSERT INTO announcement_audience_users (announcement_id, user_id) VALUES (?, ?)",
                   [(announcement_id, user_id) for user_id in user_ids])


class _Hrefs(HTMLParser):
    """The ``href`` and ``src`` values of rendered HTML, entities decoded."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.values: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.values.extend(value for name, value in attrs if name in ("href", "src") and value)


def _url_host(url: str) -> str | None:
    """The host a browser sends *url* to, read as it does: tabs and newlines dropped, ``\\`` as ``/``."""
    url = re.sub(r"[\t\n\r]", "", url).strip(_C0_AND_SPACE)
    match = _URL_SCHEME.match(url)
    if match and match.group(1).lower() not in _SPECIAL_SCHEMES:
        return urlsplit(url).hostname
    rest = (match.group(2) if match else url).replace("\\", "/")
    if not match and not rest.startswith("//"):
        return None  # relative: this wiki
    # After a special scheme any number of slashes, or none ("http:host" from an https page), precede the host
    authority = rest.lstrip("/")
    return urlsplit("//" + authority).hostname or None


def _link_hosts(content: str) -> list[str]:
    """The hosts the links in *content* lead to, each once.

    Read from the sanitised HTML the banner shows, so every spelling a browser
    follows counts (``https:\\\\host``, ``/\\host``, entities, tabs), and from
    the source for bare ``//`` addresses.
    """
    parser = _Hrefs()
    try:
        parser.feed(render_markdown(content or "", highlight=False, mentions=False))
    except Exception:  # noqa: BLE001 - auditing must never break the audited action
        log.exception("Could not read the links of an announcement")
    urls = [*parser.values, *(match.group(0) for match in _LINK.finditer(content or ""))]
    hosts = (_url_host(url) for url in urls)
    return sorted({host for host in hosts if host})


def _audit(action: str, announcement_id: int, columns: dict[str, Any], audience: int,
           previous: dict[str, Any] | None = None) -> None:
    """Record a change: who sees the banner, until when, where its links lead and how it begins."""
    content = str(columns.get("content") or "")
    details: dict[str, Any] = {
        "visibility": columns.get("visibility"), "audience_mode": columns.get("audience_mode"),
        "audience": audience, "is_active": columns.get("is_active"), "expires_at": columns.get("expires_at"),
        "link_hosts": ", ".join(_link_hosts(content)), "excerpt": content[:AUDIT_EXCERPT],
    }
    if previous is not None:
        details["changed"] = ", ".join(sorted(name for name, value in columns.items() if previous.get(name) != value))
    record(action, target_type="announcement", target_id=announcement_id, details=details)


def _audience_count(announcement_id: int) -> int:
    return int(db.scalar("SELECT COUNT(*) FROM announcement_audience_users WHERE announcement_id = ?",
                         (announcement_id,), default=0))


def create(data: AnnouncementInput, *, actor_id: str) -> int:
    columns = _columns(data)
    with db.transaction():
        announcement_id = db.insert("announcements", {**columns, "created_by": actor_id, "created_at": now_sql()})
        _set_audience(announcement_id, data.audience_user_ids)
    _audit("announcement.created", announcement_id, columns, len(data.audience_user_ids))
    return announcement_id


def update(announcement_id: int, data: AnnouncementInput) -> None:
    columns = _columns(data)
    assignments = ", ".join(f"{name} = ?" for name in columns)
    with db.transaction():
        previous = get(announcement_id)
        db.execute(f"UPDATE announcements SET {assignments}, revision = revision + 1 WHERE id = ?",
                   [*columns.values(), announcement_id])
        _set_audience(announcement_id, data.audience_user_ids)
    if previous is not None:
        _audit("announcement.updated", announcement_id, columns, len(data.audience_user_ids), previous)


def delete(announcement_id: int) -> None:
    with db.transaction():
        previous = get(announcement_id)
        audience = _audience_count(announcement_id)
        deleted = db.execute("DELETE FROM announcements WHERE id = ?", (announcement_id,)).rowcount
    if deleted and previous is not None:  # the row is gone: the entry keeps what it said
        _audit("announcement.deleted", announcement_id, previous, audience)


def get(announcement_id: int) -> dict[str, Any] | None:
    return db.one("SELECT * FROM announcements WHERE id = ?", (announcement_id,))


def audience_usernames(announcement_id: int) -> list[str]:
    return db.column(
        "SELECT u.username FROM announcement_audience_users a JOIN users u ON u.id = a.user_id "
        "WHERE a.announcement_id = ? ORDER BY u.username COLLATE NOCASE", (announcement_id,),
    )


def as_input(row: dict[str, Any]) -> AnnouncementInput:
    """The edit form's values for a stored announcement."""
    custom = bool(row["custom_background"] and row["custom_text_color"])
    return AnnouncementInput(
        content=row["content"], color=row["color"], text_size=row["text_size"], visibility=row["visibility"],
        audience_mode=row["audience_mode"], audience_usernames=", ".join(audience_usernames(row["id"])),
        expires_at=row["expires_at"], expires_input=datetime_local_input(row["expires_at"]),
        is_active=bool(row["is_active"]), not_removable=bool(row["not_removable"]),
        show_countdown=bool(row["show_countdown"]), custom_colors=custom,
        custom_background=row["custom_background"] if custom else "#333333",
        custom_text_color=row["custom_text_color"] if custom else "#ffffff",
    )


def list_all() -> list[dict[str, Any]]:
    rows = db.all(
        "SELECT a.*, u.username AS creator_name, "
        "(SELECT GROUP_CONCAT(tu.username, ', ') FROM announcement_audience_users t "
        " JOIN users tu ON tu.id = t.user_id WHERE t.announcement_id = a.id) AS audience_names "
        "FROM announcements a LEFT JOIN users u ON u.id = a.created_by ORDER BY a.created_at DESC, a.id DESC"
    )
    for row in rows:
        row["expired"] = not _unexpired(row)
    return rows


# Visibility


def _unexpired(row: dict[str, Any]) -> bool:
    """No expiry, or one in the future. An unreadable expiry counts as expired."""
    return not row["expires_at"] or is_future(row["expires_at"])


def visible_for(user: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Active announcements *user* (None: an anonymous visitor) should see, newest first."""
    user_id = user["id"] if user else None
    rows = db.all(
        "SELECT * FROM announcements a WHERE a.is_active = 1 "
        "AND a.visibility IN ('both', ?) "
        "AND (a.audience_mode = 'all' "
        "  OR (a.audience_mode = 'allowlist' AND EXISTS (SELECT 1 FROM announcement_audience_users t "
        "      WHERE t.announcement_id = a.id AND t.user_id = ?)) "
        "  OR (a.audience_mode = 'denylist' AND NOT EXISTS (SELECT 1 FROM announcement_audience_users t "
        "      WHERE t.announcement_id = a.id AND t.user_id = ?))) "
        "ORDER BY a.created_at DESC, a.id DESC",
        ("logged_in" if user else "logged_out", user_id, user_id),
    )
    return [row for row in rows if _unexpired(row)]


def visible_one(announcement_id: int, user: dict[str, Any] | None) -> dict[str, Any] | None:
    return next((row for row in visible_for(user) if row["id"] == announcement_id), None)


def custom_style(row: dict[str, Any]) -> str:
    """Inline colours for an announcement with valid custom colours, else ''."""
    background, text = row.get("custom_background") or "", row.get("custom_text_color") or ""
    if HEX_COLOR.match(background) and HEX_COLOR.match(text):
        return f"background:{background};color:{text}"
    return ""


def expires_iso(row: dict[str, Any]) -> str:
    """Expiry as an ISO 8601 UTC string for the countdown script."""
    moment = parse(row.get("expires_at"))
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ") if moment else ""


# Housekeeping


def prune_expired() -> int:
    """Delete announcements that expired more than RETENTION_DAYS ago."""
    cutoff = sql_in(days=-RETENTION_DAYS)
    return db.execute(
        "DELETE FROM announcements WHERE expires_at IS NOT NULL AND expires_at != '' AND expires_at < ?", (cutoff,)
    ).rowcount
