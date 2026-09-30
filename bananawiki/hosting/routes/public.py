"""Public pages: the landing redirect, help centre, legal links, language and theme."""

from __future__ import annotations

from flask import (
    Blueprint,
    abort,
    current_app,
    make_response,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from ...core.web import is_safe_redirect
from .. import accounts, auth, domains, help
from ..i18n import LANGUAGES, current_language
from ..limits import rate_limit

bp = Blueprint("public", __name__)


def _back_to_referrer(default: str):
    ref = request.referrer or ""
    host_prefix = request.host_url
    if ref.startswith(host_prefix) and is_safe_redirect("/" + ref[len(host_prefix):]):
        return redirect("/" + ref[len(host_prefix):])
    return redirect(default)


@bp.get("/")
@auth.public
@auth.exempt(*auth.GATES)
def index():
    if auth.current_account():
        return redirect(url_for("dashboard.dashboard"))
    return redirect(url_for("auth.login"))


def _help_language() -> tuple[str, bool]:
    requested = (request.args.get("lang") or "").strip().lower()
    if requested in help.LANGUAGES:
        return requested, True
    return current_language(), False


@bp.get("/help")
@auth.public
@auth.exempt(*auth.GATES)
def help_index():
    language, explicit = _help_language()
    return render_template("hosting/help_index.html", articles=help.articles(language),
                           help_language=language if explicit else None)


@bp.get("/help/<slug>")
@auth.public
@auth.exempt(*auth.GATES)
def help_article(slug: str):
    language, explicit = _help_language()
    cfg = current_app.config["HOSTING"]
    article = help.article(language, slug, contact_email=cfg.contact_email, source_url=cfg.source_url)
    if article is None:
        abort(404)
    return render_template("hosting/help_article.html", article=article, help_language=language if explicit else None)


def _legal(path: str):
    base = current_app.config["HOSTING"].base_domain
    return redirect(f"https://{base}{path}" if base else url_for("public.help_index"))


@bp.get("/terms")
@auth.public
@auth.exempt(*auth.GATES)
def terms():
    return _legal("/terms/")


@bp.get("/privacy")
@auth.public
@auth.exempt(*auth.GATES)
def privacy():
    return _legal("/privacy/")


@bp.get("/compliance")
@auth.public
@auth.exempt(*auth.GATES)
def compliance():
    return _legal("/terms/")


@bp.post("/language")
@auth.public
@auth.exempt(*auth.GATES)
@rate_limit(30)
def change_language():
    language = (request.form.get("language") or "").strip().lower()
    if language in LANGUAGES:
        session["interface_language"] = language
        if auth.current_account() and not auth.impersonating():
            accounts.set_language(auth.current_account()["id"], language)  # type: ignore[index]
    else:
        session.pop("interface_language", None)
    return _back_to_referrer(url_for("public.index"))


@bp.post("/account/theme")
@auth.exempt(*auth.GATES)
@rate_limit(30)
def set_theme():
    mode = (request.form.get("theme_mode") or "").strip().lower()
    if mode in ("dark", "light", "default"):
        accounts.set_theme(auth.current_account()["id"], mode)  # type: ignore[index]
    response = make_response(_back_to_referrer(url_for("dashboard.dashboard")))
    return response


@bp.get("/internal/domains/authorize")
@auth.public
@auth.exempt(*auth.GATES)
def authorize_certificate():
    """Caddy on-demand TLS "ask" endpoint; answers loopback callers only."""
    if request.remote_addr not in ("127.0.0.1", "::1"):
        abort(404)
    allowed = domains.certificate_allowed(request.args.get("domain", ""))
    return "", 200 if allowed else 403, {"Cache-Control": "no-store"}
