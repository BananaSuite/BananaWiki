"""Template globals and filters for the portal."""

from __future__ import annotations

import hashlib
import json
import os
from functools import lru_cache
from typing import Any

from flask import Flask, current_app, g, request, session, url_for
from markupsafe import Markup

from .. import __version__
from ..core import web
from ..core.timeutil import parse, utcnow
from . import attention, auth, i18n, notifications, urls


def format_datetime(value: Any, fmt: str = "%Y-%m-%d %H:%M UTC") -> str:
    moment = parse(value)
    return moment.strftime(fmt) if moment else ""


def time_ago(value: Any) -> str:
    moment = parse(value)
    if moment is None:
        return ""
    seconds = int((utcnow() - moment).total_seconds())
    if seconds < 0:
        return i18n.t("time.in_future")
    for limit, unit, size in ((60, "seconds", 1), (3600, "minutes", 60), (86400, "hours", 3600),
                              (86400 * 30, "days", 86400), (86400 * 365, "months", 86400 * 30)):
        if seconds < limit:
            return i18n.t(f"time.{unit}_ago", count=max(1, seconds // size))
    return i18n.t("time.years_ago", count=max(1, seconds // (86400 * 365)))


def duration(seconds: Any) -> str:
    """"99y 364d", "3d 4h", "2h 5m" or "45s" for a number of seconds."""
    try:
        seconds = max(0, int(seconds))
    except (TypeError, ValueError):
        return ""
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    if days >= 365:
        return f"{days // 365}y {days % 365}d"
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m {secs}s" if minutes else f"{secs}s"


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


@lru_cache(maxsize=64)
def _file_hash(path: str, mtime: float) -> str:
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()[:10]


def asset_url(filename: str) -> str:
    path = os.path.join(current_app.static_folder or "", filename)
    try:
        version = _file_hash(path, os.path.getmtime(path))
    except OSError:
        version = __version__
    return url_for("static", filename=filename, v=version)


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
    login_total = attention.total(account) if session.pop(auth.LOGIN_BANNER_KEY, None) else 0
    return {"login_total": login_total, "notices": attention.notices_for(account["id"])}


def _json_script(value: Any) -> Markup:
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return Markup(text.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026"))


def install(app: Flask) -> None:
    app.jinja_env.filters.update(datetime=format_datetime, date=lambda v: format_datetime(v, "%Y-%m-%d"),
                                 time_ago=time_ago, duration=duration, filesize=filesize, json_script=_json_script,
                                 device=device)

    @app.context_processor
    def _globals() -> dict[str, Any]:
        cfg = current_app.config["HOSTING"]
        return {
            "t": i18n.t, "lang": i18n.current_language(), "languages": i18n.LANGUAGES,
            "csrf_token": web.csrf_token, "csp_nonce": web.csp_nonce, "asset_url": asset_url,
            "current_account": auth.current_account(), "real_account": auth.real_account(),
            "impersonating": auth.impersonating(), "theme": theme, "banners": banners,
            "js_strings": i18n.js_strings, "contact_email": cfg.contact_email, "source_url": cfg.source_url,
            "app_version": __version__, "hosting_mode": cfg.hosting_mode, "instance_url": urls.instance_url,
            "request_id": g.get("request_id", ""), "attention_items": attention_items,
            "attention_banner": attention_banner, "notice_text": notifications.notice_text,
        }
