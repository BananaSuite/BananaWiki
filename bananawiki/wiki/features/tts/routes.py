"""Read-aloud endpoints (the 1.4 URLs) and the admin page.

Page endpoints: ``/page/<slug>/tts/{status,generate,cancel,audio,download}``.
``status``, ``audio`` and ``download`` are open to anonymous visitors while
public mode and public read-aloud are on; every one of them checks that the
visitor may read *that* page. Generating audio needs an account.

Admin: ``/admin/tts`` (``/admin/tts-status`` redirects there).
"""

from __future__ import annotations

from typing import Any

from flask import abort, current_app, jsonify, redirect, render_template, request, send_file, url_for

from ....core.web import client_ip
from ... import auth, settings
from ...i18n import t
from ...registry import feature_blueprint
from ..pages import service as pages
from . import backends, options, service, text

bp = feature_blueprint("tts", "tts", __name__, template_folder="templates", static_folder="static",
                       static_url_path="/static/tts")

QUEUE_PAGE_SIZE = 50
SPEED_LIMIT = (6, 600)  # new speed conversions per client per 10 minutes


# ── Helpers ───────────────────────────────────────────────────────────────────


def _rate_limited(bucket: str, limit: int, window: int) -> bool:
    user = auth.current_user()
    key = f"tts:{bucket}:{user['id'] if user else client_ip()}"
    return not current_app.extensions["bananawiki.limiter"].hit(key, limit, window)


def _too_many():
    return jsonify({"error": t("tts.error.rate_limited")}), 429, {"Retry-After": "60"}


def _page_for_listening(slug: str) -> dict[str, Any]:
    """The page, if the visitor may listen to it (404 otherwise, never revealing existence)."""
    page = pages.get_by_slug(slug)
    if page is None or not pages.can_view(page):
        abort(404)
    if auth.current_user() is None and not options.public_access():
        abort(404)
    return page


def _error(key: str, status: int, **extra: Any):
    return jsonify({"ok": False, "error": key.rsplit(".", 1)[-1], "message": t(key), **extra}), status


def status_payload(page: dict[str, Any]) -> dict[str, Any]:
    user = auth.current_user()
    audio = service.page_audio(page)
    blocked = service.queue_state(user) if user else "login_required"
    if blocked is None and not options.panel_enabled():
        blocked = "panel_disabled"
    return {
        "ok": True,
        "generation": service.serialize(audio.row, usable=audio.usable),
        "usable": audio.usable,
        "in_flight": audio.in_flight,
        "can_generate": blocked is None and not audio.usable and not audio.in_flight,
        "blocked_reason": blocked,
        "blocked_message": t(f"tts.blocked.{blocked}") if blocked else "",
        "can_cancel": bool(user) and pages.can_edit(page) and audio.row is not None,
    }


# ── Page endpoints ────────────────────────────────────────────────────────────


@bp.get("/page/<slug>/tts/status")
@auth.public_read
def status(slug: str):
    if _rate_limited("status", 60, 60):
        return _too_many()
    return jsonify(status_payload(_page_for_listening(slug)))


@bp.post("/page/<slug>/tts/generate")
def generate(slug: str):
    page = _page_for_listening(slug)
    user = auth.current_user()
    if _rate_limited("generate", 10, 60):
        return _too_many()
    if options.host_disabled():
        return _error("tts.blocked.disabled_by_host", 503)
    if not options.panel_enabled():
        return _error("tts.blocked.panel_disabled", 403)
    payload = request.get_json(silent=True) or {}
    language = payload.get("language") if isinstance(payload, dict) else None
    try:
        outcome = service.request(page, requested_by=user["id"], language=language or request.form.get("language"),
                                  limits=service.manual_limits())
    except service.TtsError as error:
        return _error(error.key, error.status)
    if outcome.reason in ("already_in_progress", "already_generated"):
        audio = service.page_audio(page)
        return _error(f"tts.error.{outcome.reason}", 409,
                      generation=service.serialize(audio.row, usable=audio.usable))
    if outcome.reason in ("queue_full", "user_queue_full"):
        response, code = _error(f"tts.blocked.{outcome.reason}", 429 if outcome.reason == "user_queue_full" else 503)
        return response, code, {"Retry-After": "60"}
    return jsonify({"ok": True, "created": True, "generation": service.serialize(outcome.row)}), 202


@bp.post("/page/<slug>/tts/cancel")
def cancel(slug: str):
    page = _page_for_listening(slug)
    if not pages.can_edit(page):
        return auth.deny(403)
    service.delete_for_page(page["id"])
    return jsonify({"ok": True})


def _send_audio(page: dict[str, Any], *, download: bool):
    audio = service.page_audio(page)
    path = service.audio_path(audio.row) if audio.usable else None
    if path is None or audio.row is None:
        abort(404)
    row = audio.row
    extension = row["filename"].rsplit(".", 1)[-1].lower()
    name = f"{page['slug']}-{row['language']}"
    speed = text.normalize_speed(request.args.get("speed"), 1.0) if download else 1.0
    if abs(speed - 1.0) > 1e-6:
        if _rate_limited("speed", *SPEED_LIMIT):
            return _too_many()
        try:
            variant = service.speed_variant(row, speed)
        except service.SpeedBusy:
            return jsonify({"error": t("tts.error.busy")}), 503, {"Retry-After": "10"}
        if variant is not None:
            path, extension, name = variant, "mp3", f"{name}-{text.speed_tag(speed)}x"
    response = send_file(
        path,
        mimetype="audio/wav" if extension == "wav" else "audio/mpeg",
        as_attachment=download,
        download_name=f"{name}.{extension}",
        conditional=True,
        max_age=0,
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Content-Security-Policy"] = "default-src 'none'; sandbox"
    response.headers["Cache-Control"] = "private, no-cache"
    return response


@bp.get("/page/<slug>/tts/audio")
@auth.public_read
def audio(slug: str):
    if _rate_limited("audio", 120, 60):
        return _too_many()
    return _send_audio(_page_for_listening(slug), download=False)


@bp.get("/page/<slug>/tts/download")
@auth.public_read
def download(slug: str):
    if _rate_limited("download", 60, 60):
        return _too_many()
    return _send_audio(_page_for_listening(slug), download=True)


# ── Admin ─────────────────────────────────────────────────────────────────────


def _system_status() -> dict[str, Any]:
    cfg = options.config()
    backend = backends.current()
    piper = backends.PiperBackend(cfg, options.performance_mode())
    languages = options.enabled_languages()
    return {
        "backend": backend.name,
        "backend_problem": backend.problem(),
        "piper_installed": backends.piper_installed(),
        "ffmpeg": backends.ffmpeg_available(),
        "langdetect": text.langdetect_available(),
        "voices": [(code, text.LANGUAGE_LABELS.get(code, code), piper.voice_state(code)) for code in languages],
        "voice_dir": cfg.piper_voice_dir,
        "inline_worker": cfg.inline_worker,
        "worker_count": cfg.worker_count,
        "host_disabled": cfg.host_disabled,
        "gpu_managed": cfg.gpu_managed_by_host,
        "gpu_from_env": bool(cfg.gpu_url and cfg.gpu_token),
        "limits": service.manual_limits(),
    }


@bp.get("/admin/tts")
@auth.admin_required
def admin():
    status_filter = request.args.get("status") or None
    try:
        page_number = max(1, int(request.args.get("page", 1)))
    except ValueError:
        page_number = 1
    rows, total = service.queue(status=status_filter, limit=QUEUE_PAGE_SIZE,
                                offset=(page_number - 1) * QUEUE_PAGE_SIZE)
    return render_template(
        "tts/admin.html",
        stats=service.stats(),
        rows=rows,
        page=page_number,
        pages=max(1, -(-total // QUEUE_PAGE_SIZE)),
        status_filter=status_filter,
        system=_system_status(),
        values=settings.load(),
        enabled=set(options.enabled_languages()),
        languages=[(code, text.LANGUAGE_LABELS.get(code, code)) for code in text.SUPPORTED_LANGUAGES],
        labels=text.LANGUAGE_LABELS,
        performance_modes=options.PERFORMANCE_MODES,
    )


@bp.get("/admin/tts-status")
@auth.admin_required
def admin_legacy():
    return redirect(url_for("tts.admin"), 301)


def _back():
    return redirect(url_for("tts.admin", status=request.args.get("status") or None))


@bp.post("/admin/tts/settings")
@auth.admin_required
def admin_settings():
    form = request.form
    values: dict[str, Any] = {
        "tts_page_panel_enabled": 1 if form.get("tts_page_panel_enabled") else 0,
        "tts_public_access_enabled": 1 if form.get("tts_public_access_enabled") else 0,
        "tts_auto_generate_enabled": 1 if form.get("generation_mode") == "auto" else 0,
        "tts_enabled_languages": text.serialize_enabled_languages(form.getlist("languages"))
        or ",".join(text.DEFAULT_ENABLED_LANGUAGES),
    }
    mode = form.get("tts_performance_mode", "auto")
    values["tts_performance_mode"] = mode if mode in options.PERFORMANCE_MODES else "auto"
    if not options.config().gpu_managed_by_host:
        url = form.get("tts_gpu_url", "").strip().rstrip("/")
        if url:
            problem = backends.gpu_base_url_problem(url)
            if problem:
                auth.flash_t("tts.admin.gpu_url_invalid", "error")
                return _back()
        timeout = options.gpu_timeout(form.get("tts_gpu_timeout"))
        values.update({
            "tts_gpu_enabled": 1 if form.get("tts_gpu_enabled") and url else 0,
            "tts_gpu_url": url,
            "tts_gpu_timeout": timeout or options.GPU_DEFAULT_TIMEOUT,
        })
        token = form.get("tts_gpu_auth_token", "").strip()
        if form.get("clear_gpu_token"):
            values["tts_gpu_auth_token"] = ""
        elif token:
            if any(ord(char) < 33 or ord(char) > 126 for char in token) or len(token) > 512:
                auth.flash_t("tts.admin.gpu_token_invalid", "error")
                return _back()
            values["tts_gpu_auth_token"] = token
    settings.update(values)
    auth.flash_t("tts.admin.saved", "success")
    return _back()


@bp.post("/admin/tts/backfill")
@bp.post("/admin/tts-status/backfill")
@auth.admin_required
def admin_backfill():
    if options.host_disabled():
        auth.flash_t("tts.blocked.disabled_by_host", "warning")
        return _back()
    result = service.backfill(auth.current_user()["id"])
    auth.flash_t("tts.admin.backfill_done", "success" if result["queued"] else "info", **result)
    return _back()


@bp.post("/admin/tts/clear")
@bp.post("/admin/tts-status/clear")
@auth.admin_required
def admin_clear():
    count = service.clear_all()
    auth.flash_t("tts.admin.cleared", "success", count=count)
    return _back()


@bp.post("/admin/tts/retry-failed")
@auth.admin_required
def admin_retry_failed():
    count = service.retry_failed(auth.current_user()["id"])
    auth.flash_t("tts.admin.retried", "success", count=count)
    return _back()


@bp.post("/admin/tts/<int:generation_id>/retry")
@auth.admin_required
def admin_retry(generation_id: int):
    row = service.generation_by_id(generation_id)
    if row is None:
        abort(404)
    ok = service.retry(row, auth.current_user()["id"])
    auth.flash_t("tts.admin.retried" if ok else "tts.admin.not_retried", "success" if ok else "warning", count=1)
    return _back()


@bp.post("/admin/tts/<int:generation_id>/delete")
@auth.admin_required
def admin_delete(generation_id: int):
    row = service.generation_by_id(generation_id)
    if row is None:
        abort(404)
    service.delete_generation(row)
    auth.flash_t("tts.admin.deleted", "success")
    return _back()


@bp.post("/admin/tts/gpu-test")
@auth.admin_required
def admin_gpu_test():
    url, token, timeout = options.gpu_settings()
    try:
        report = backends.RemoteGpuBackend(url, token, timeout).health()
    except backends.SynthesisError as error:
        auth.flash_t("tts.admin.gpu_test_failed", "error", error=str(error))
    else:
        auth.flash_t("tts.admin.gpu_test_ok", "success", gpu=t("common.yes") if report.get("gpu_available")
                     else t("common.no"))
    return _back()


# ── Page panel (slot) ─────────────────────────────────────────────────────────


def render_panel(page: dict[str, Any] | None = None, **_: Any) -> str:
    """The "Listen to this page" player under the page content."""
    if not page or not page.get("id") or not options.panel_enabled():
        return ""
    user = auth.current_user()
    if user is None and not options.public_access():
        return ""
    full = page if "content" in page else pages.get(page["id"])
    if full is None or not pages.can_view(full):
        return ""
    state = status_payload(full)
    if user is None and not (state["usable"] or state["in_flight"]):
        return ""
    return render_template(
        "tts/panel.html",
        page=full,
        state=state,
        speeds=text.SPEED_PRESETS,
        default_speed=text.DEFAULT_SPEED,
        speed_tag=text.speed_tag,
        urls={
            "status": url_for("tts.status", slug=full["slug"]),
            "generate": url_for("tts.generate", slug=full["slug"]),
            "cancel": url_for("tts.cancel", slug=full["slug"]),
            "audio": url_for("tts.audio", slug=full["slug"]),
            "download": url_for("tts.download", slug=full["slug"]),
        },
        speed_download=backends.ffmpeg_available(),
    )
