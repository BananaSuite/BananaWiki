"""Template globals and filters for the portal."""

from __future__ import annotations

import json
import re
from typing import Any

from flask import Flask, current_app, g, request, session
from markupsafe import Markup, escape

from .. import __version__
from ..core import mascot, web
from ..core.assets import asset_url
from ..core.i18n import _PLACEHOLDER
from ..core.timeutil import is_past, parse, utcnow
from . import attention, auth, i18n, notifications, urls


def format_datetime(value: Any, fmt: str = "%Y-%m-%d %H:%M UTC") -> str:
    moment = parse(value)
    return moment.strftime(fmt) if moment else ""


def time_ago(value: Any) -> str:
    """"just now", "5 min ago", "1 day ago", "3 months ago": a past moment in words, singular or plural."""
    moment = parse(value)
    if moment is None:
        return ""
    seconds = int((utcnow() - moment).total_seconds())
    if seconds < 0:
        return i18n.t("time.in_future")
    if seconds < 60:
        return i18n.t("time.just_now")
    for limit, unit, size in ((3600, "minutes", 60), (86400, "hours", 3600),
                              (86400 * 30, "days", 86400), (86400 * 365, "months", 86400 * 30)):
        if seconds < limit:
            count = max(1, seconds // size)
            return i18n.t(f"time.{unit}_ago_{'one' if count == 1 else 'other'}", count=count)
    count = max(1, seconds // (86400 * 365))
    return i18n.t(f"time.years_ago_{'one' if count == 1 else 'other'}", count=count)


_MONTHS = ("january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
           "november", "december")


def format_datetime_long(value: Any) -> str:
    """"14 October 2026, 16:39 UTC" / "14 ottobre 2026, ore 16:39 UTC": a date for a sentence people read."""
    moment = parse(value)
    if moment is None:
        return ""
    return i18n.t("time.datetime_long", day=moment.day, month=i18n.t(f"time.month.{_MONTHS[moment.month - 1]}"),
                  year=moment.year, time=moment.strftime("%H:%M"))


def _duration_parts(seconds: Any) -> list[tuple[str, int]] | None:
    """The two most significant units of *seconds*: ``[("days", 3), ("hours", 4)]``."""
    try:
        seconds = max(0, int(seconds))
    except (TypeError, ValueError):
        return None
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    if days >= 365:
        return [("years", days // 365), ("days", days % 365)]
    if days:
        return [("days", days), ("hours", hours)]
    if hours:
        return [("hours", hours), ("minutes", minutes)]
    return [("minutes", minutes), ("seconds", secs)] if minutes else [("seconds", secs)]


def duration(seconds: Any) -> str:
    """"3d 4h", "2h 5m", "14d" or "45s" for a number of seconds, with the units of the interface language.

    A second unit that is zero is left out ("14d", not "14d 0h").
    """
    parts = _duration_parts(seconds)
    if parts is None:
        return ""
    return " ".join(i18n.t(f"time.short.{unit}", count=count) for index, (unit, count) in enumerate(parts)
                    if count or index == 0)


def duration_long(seconds: Any) -> str:
    """"3 days and 4 hours" for a sentence; a zero second unit is left out."""
    parts = _duration_parts(seconds)
    if parts is None:
        return ""
    words = [i18n.t(f"time.long.{unit}_{'one' if count == 1 else 'other'}", count=count)
             for index, (unit, count) in enumerate(parts) if count or index == 0]
    return i18n.t("time.long.pair", first=words[0], second=words[1]) if len(words) == 2 else words[0]


# An expiry further away than this is the 100-year one of administrator wikis: shown as "never".
NEVER_AFTER_SECONDS = 50 * 365 * 86400


def never_expires(seconds_left: Any) -> bool:
    return seconds_left is None or (isinstance(seconds_left, (int, float)) and seconds_left > NEVER_AFTER_SECONDS)


# Portal languages that write a decimal comma.
DECIMAL_COMMA = frozenset({"de", "it"})


def megabytes(value: Any) -> str:
    """"42", "163,4" (German, Italian) or "163.4" for a size in MB: no pointless ".0"."""
    try:
        number = round(float(value or 0), 1)
    except (TypeError, ValueError):
        return ""
    text = str(int(number)) if number == int(number) else f"{number:.1f}"
    return text.replace(".", ",") if i18n.current_language() in DECIMAL_COMMA else text


def tm(key: str, /, **values: Any) -> Markup:
    """``t()`` for a sentence that carries markup (links): the text is escaped, a Markup value is kept."""
    text = escape(i18n.t(key))
    return Markup(_PLACEHOLDER.sub(lambda m: str(escape(values[m.group(1)])) if m.group(1) in values else m.group(0),
                                   str(text)))


def link(href: str, text: str, cls: str = "") -> Markup:
    """An escaped ``<a>`` to pass into :func:`tm`."""
    if cls:
        return Markup('<a href="{}" class="{}">{}</a>').format(href, cls, text)
    return Markup('<a href="{}">{}</a>').format(href, text)


def strong(text: Any) -> Markup:
    return Markup("<strong>{}</strong>").format(text)


def wbr_dots(text: Any) -> Markup:
    """A host name or DNS record that breaks only after its dots on a narrow screen: each label is kept
    whole (also at a hyphen), unless a label alone is wider than the line."""
    parts = [part for part in re.split(r"(?<=\.)", str(text or "")) if part]
    return Markup("<wbr>").join(Markup('<span class="host-part">{}</span>').format(part) for part in parts)


def wbr_settings(text: Any) -> Markup:
    """Text that names settings (``BW_HOSTING_TENANT_QUOTAS=required``): on a narrow screen such a token may
    break after its ``=``, not inside the value."""
    return Markup(re.sub(r"\b([A-Z][A-Z0-9_]{2,}=)", r"\1<wbr>", str(escape(text))))


def datetime_html(value: Any) -> Markup:
    """A date and time for a narrow column: the date and "16:39 UTC" each stay whole, and only the space
    between them may break (never at the date's hyphens, never leaving "UTC" alone on a line)."""
    moment = parse(value)
    if moment is None:
        return Markup("")
    return Markup('<span class="nowrap">{}</span> <span class="nowrap">{}</span>').format(
        moment.strftime("%Y-%m-%d"), moment.strftime("%H:%M UTC"))


def nowrap(text: Any) -> Markup:
    """*text* kept on one line (a date or version with hyphens), to pass into :func:`tm`."""
    return Markup('<span class="nowrap">{}</span>').format(text)


# Sentences the platform writes in English into reasons and details it stores. They are shown in the
# visitor's language; anything else (an administrator's own words) is shown as written.
_SERVER_TEXT = (
    (re.compile(r"^Automatic storage protection: ([\d.]+) MB used, (\d+) MB allowed\.$"),
     "hosting.server_text.storage", ("used", "limit")),
    (re.compile(r"^(.+): new password; two-step verification turned off; signed out everywhere$"),
     "hosting.server_text.admin_password_reset", ("username",)),
    (re.compile(r"^its files take more than the (\d+) MiB its storage limit allows in a backup "
                r"\(a sparse file at its full size\); stopped at (.+)$"),
     "hosting.server_text.backup_too_large", ("limit", "path")),
    (re.compile(r"^(Token \d+ \([^)]*\)) with scopes (.+)$"), "hosting.server_text.token_scopes", ("token", "scopes")),
    (re.compile(r"^Through (Token \d+ \([^)]*\))$"), "hosting.server_text.through_token", ("token",)),
    (re.compile(r"^previous owner: (none|view|[a-z_]+)$"), "hosting.server_text.previous_owner", ("access",)),
    (re.compile(r"^Password reset by email$"), "hosting.server_text.password_reset_email", ()),
    (re.compile(r"^Password set by an administrator$"), "hosting.server_text.password_set_by_admin", ()),
    (re.compile(r"^Password changed$"), "hosting.server_text.password_changed", ()),
    (re.compile(r"^Timed suspension ended$"), "hosting.server_text.timed_suspension_ended", ()),
    (re.compile(r"^Approval requirement removed\.$"), "hosting.server_text.approval_removed", ()),
    (re.compile(r"^unverified address claimed by another account$"), "hosting.server_text.email_released", ()),
)


_KEY = re.compile(r"hosting\.[a-z0-9_]+(?:\.[a-z0-9_]+)+")


def server_text(value: Any) -> str:
    text = str(value or "")
    if _KEY.fullmatch(text):
        return i18n.t(text, text)
    for pattern, key, names in _SERVER_TEXT:
        match = pattern.match(text)
        if match:
            found = dict(zip(names, match.groups(), strict=True))
            if key == "hosting.server_text.storage":
                found["used"] = megabytes(found["used"])
            if key == "hosting.server_text.previous_owner":
                found["access"] = i18n.t(f"hosting.server_text.previous_owner_{found['access']}", found["access"])
            if key == "hosting.server_text.token_scopes":
                # The permissions in words, as the Account page names them.
                found["scopes"] = "; ".join(
                    i18n.t(f"hosting.api_tokens.scope.{scope.replace(':', '_')}", scope)
                    for scope in found["scopes"].replace(",", " ").split())
            return i18n.t(key, **found)
    return text


def event_label(action: Any) -> str:
    """The moderation log's action code in words; an unknown code is shown as it is."""
    code = str(action or "")
    label = i18n.t(f"hosting.event.{code}", "")
    parts = code.split(".")
    # Codes with a variable part: instance.terminated.<reason>, maintenance.<step>.failed, ...
    for index in range(len(parts) - 1, -1, -1):
        if label:
            break
        pattern = ".".join(parts[:index] + ["*"] + parts[index + 1:])
        label = i18n.t(f"hosting.event.{pattern}", "", detail=parts[index])
    return label or code


def filesize(value: Any) -> str:
    try:
        size = float(value or 0)
    except (TypeError, ValueError):
        return ""
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


_BROWSERS = (("Edg/", "Edge"), ("OPR/", "Opera"), ("Opera", "Opera"), ("Firefox/", "Firefox"),
             ("Chrome/", "Chrome"), ("CriOS/", "Chrome"), ("Safari/", "Safari"))
_PLATFORMS = (("iPhone", "iOS"), ("iPad", "iOS"), ("Android", "Android"), ("Windows", "Windows"),
              ("Macintosh", "macOS"), ("Mac OS X", "macOS"), ("Linux", "Linux"))


def device(user_agent: Any) -> str:
    """"Firefox on Windows" for a stored user agent string."""
    ua = str(user_agent or "")
    browser = next((name for marker, name in _BROWSERS if marker in ua), i18n.t("hosting.account.sessions.unknown_browser"))
    platform = next((name for marker, name in _PLATFORMS if marker in ua), i18n.t("hosting.account.sessions.unknown_platform"))
    return i18n.t("hosting.account.sessions.device_label", browser=browser, platform=platform)


def theme() -> str:
    account = auth.current_account()
    if account and account.get("theme_mode") in ("dark", "light"):
        return account["theme_mode"]
    cookie = request.cookies.get("bw_theme")
    return cookie if cookie in ("dark", "light") else "dark"


def banners() -> list[dict[str, Any]]:
    from . import banners as banner_service

    if g.get("_banners") is None:
        account = auth.current_account()
        g._banners = banner_service.active_for(account["id"] if account else None)
    return g._banners


def attention_items() -> list[dict[str, Any]]:
    return attention.items(auth.current_account())


def attention_banner() -> dict[str, Any]:
    """The after-sign-in summary (administrators) and the account's decision notices."""
    account = auth.current_account()
    if account is None or auth.impersonating():
        return {"login_total": 0, "notices": []}
    notices = attention.notices_for(account["id"])
    if gate_pending():
        # A blocked account cannot open the review pages yet: the summary waits for its first real page,
        # and a decision the gate page itself explains (a denied account request) is not repeated.
        if request.endpoint == "auth.activation_denied":
            notices = [n for n in notices if n.get("source_id") != "accounts.pending"]
        return {"login_total": 0, "notices": notices}
    login_total = attention.total(account) if session.pop(auth.LOGIN_BANNER_KEY, None) else 0
    return {"login_total": login_total, "notices": notices}


def subject_name(subject_type: str, subject_id: str) -> str:
    """The username or wiki name behind a moderation log subject (cached per request), or ''."""
    from .db import db

    cache = g.setdefault("_subject_names", {})
    key = (subject_type, subject_id)
    if key not in cache:
        if subject_type == "account":
            name = db.scalar("SELECT username FROM accounts WHERE id = ?", (subject_id,))
        elif subject_type == "instance":
            row = db.one("SELECT subdomain FROM instances WHERE id = ?", (subject_id,))
            name = (urls.original_slug(row["subdomain"]) or row["subdomain"]) if row else None
        else:
            name = None
        cache[key] = str(name or "")
    return cache[key]


# The form field a refusal is about, for the error shown under it (see routes.common.flash_error).
_FIELD_OF_ERROR = {
    "hosting.accounts.password_mismatch": "confirm",
    "hosting.accounts.password_too_short": "password", "hosting.accounts.password_too_long": "password",
    "hosting.accounts.password_weak": "password",
    "hosting.accounts.invalid_username": "username", "hosting.accounts.username_taken": "username",
    "hosting.accounts.email_required": "email", "hosting.accounts.invalid_email": "email",
    "hosting.accounts.email_taken": "email",
    "hosting.invites.required": "invite", "hosting.invites.invalid": "invite", "hosting.invites.expired": "invite",
    "hosting.invites.exhausted": "invite",
    "hosting.signup.use_case_length": "use_case", "hosting.signup.terms_required": "terms",
}


def field_error(field: str) -> str:
    """The message of the refusal this response reports when it is about *field*, else ''."""
    found = g.get("form_error")
    return found[1] if found and _FIELD_OF_ERROR.get(found[0]) == field else ""


def inline_error() -> str:
    """The message of this response's refusal when a form field shows it (see field_error), else ''."""
    found = g.get("form_error")
    return found[1] if found and found[0] in _FIELD_OF_ERROR else ""


def gate_allows(endpoint: str) -> bool:
    """Whether the current account may open *endpoint* now: false while a gate it is not exempt from is pending,
    so pages offer no link or button that would only send the account back to the gate."""
    account = auth.current_account()
    if account is None or not gate_pending():
        return True
    return auth.pending_gate(account, current_app.view_functions.get(endpoint)) is None


def gate_pending() -> str | None:
    """The page a blocked account must resolve first (see auth.pending_gate), or None."""
    account = auth.current_account()
    if account is None:
        return None
    if "_gate_pending" not in g:
        g._gate_pending = auth.pending_gate(account, None)
    return g._gate_pending


def _json_script(value: Any) -> Markup:
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return Markup(text.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026"))


def install(app: Flask) -> None:
    app.jinja_env.filters.update(datetime=format_datetime, date=lambda v: format_datetime(v, "%Y-%m-%d"),
                                 datetime_html=datetime_html,
                                 datetime_long=format_datetime_long, wbr_dots=wbr_dots, wbr_settings=wbr_settings,
                                 time_ago=time_ago, duration=duration, duration_long=duration_long, filesize=filesize,
                                 json_script=_json_script, device=device, mb=megabytes, server_text=server_text,
                                 event_label=event_label)
    app.jinja_env.tests["never_expiring"] = never_expires
    app.jinja_env.tests["past"] = is_past

    @app.context_processor
    def _globals() -> dict[str, Any]:
        cfg = current_app.config["HOSTING"]
        return {
            "t": i18n.t, "tn": i18n.tn, "lang": i18n.current_language(), "languages": i18n.LANGUAGES,
            "csrf_token": web.csrf_token, "csp_nonce": web.csp_nonce, "asset_url": asset_url,
            "current_account": auth.current_account(), "real_account": auth.real_account(),
            "impersonating": auth.impersonating(), "theme": theme, "banners": banners,
            "js_strings": i18n.js_strings, "contact_email": cfg.contact_email, "source_url": cfg.source_url,
            "service_name": cfg.service_name,
            "app_version": __version__, "hosting_mode": cfg.hosting_mode, "instance_url": urls.instance_url,
            "request_id": g.get("request_id", ""), "attention_items": attention_items,
            "attention_banner": attention_banner, "notice_text": notifications.notice_text,
            "tm": tm, "link": link, "nowrap": nowrap, "strong": strong, "gate_pending": gate_pending, "subject_name": subject_name, "field_error": field_error,
            "inline_error": inline_error, "gate_allows": gate_allows,
            "email_ready": notifications.configured, "mascot": mascot,
        }
