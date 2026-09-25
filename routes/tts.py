"""
BananaWiki: Text-to-speech generate / status / playback routes.

These routes belong to the built-in ``tts`` plugin.  When the plugin is
disabled, ``app.before_request_hook`` (via ``_BUILTIN_PLUGIN_PATH_MATCHERS``)
returns a 404 for every path under ``/page/<slug>/tts/...``.
"""

import os
import tempfile

from flask import (
    abort, jsonify, request, send_from_directory, send_file, current_app,
    render_template, after_this_request, redirect, url_for,
)
from werkzeug.exceptions import NotFound

import db
import config
from helpers import (
    login_required, editor_required, admin_required,
    get_current_user, rate_limit, user_can_view_page, is_public_mode_active,
)
from helpers._tts import (
    TTS_SUPPORTED_LANGUAGES, TTS_SUPPORTED_LANGUAGE_SET,
    TTS_LANGUAGE_LABELS,
    TTS_SPEED_PRESETS, TTS_DEFAULT_PLAYBACK_SPEED,
    TTS_FALLBACK_LANGUAGE,
    tts_content_hash_for_text, tts_normalize_text,
    run_generation_async, remove_tts_files,
    detect_tts_language, normalize_tts_language,
    normalize_tts_speed, ffmpeg_speed_available,
    write_speed_adjusted_mp3, tts_mimetype_for_filename,
    recover_stuck_generations,
    recover_orphaned_pending_generations,
    start_tts_pending_recovery_watchdog,
    piper_voice_availability_map,
    inline_tts_worker_enabled,
    tts_generation_disabled_by_host,
)
from wiki_logger import log_action


# Shown when the host (BW_MANAGED_TTS_DISABLED or EasyWiki mode) has switched
# TTS generation off. app/static/js/tts.js carries the same text as its own
# fallback for the ``tts_disabled_by_hosting`` error.
_HOST_DISABLED_MESSAGE = (
    "Audio generation has been temporarily disabled by the "
    "server administrator."
)


def _abort_if_tts_plugin_disabled():
    """Return 404 when the TTS plugin is disabled or its state is unavailable."""
    try:
        enabled = db.is_plugin_enabled("tts")
    except Exception:
        enabled = False
    if not enabled:
        abort(404)


def _anonymous_tts_access_enabled(settings=None):
    """Return True when public visitors may use read-only TTS endpoints."""
    if not is_public_mode_active():
        return False
    if settings is None:
        settings = db.get_site_settings() or {}
    return bool(
        settings.get("tts_page_panel_enabled")
        and settings.get("tts_public_access_enabled", 1)
    )


def _abort_if_tts_not_public(user, settings=None):
    """Require auth unless the TTS public playback setting allows anonymous use."""
    if user:
        return
    if not _anonymous_tts_access_enabled(settings):
        abort(403)


def _get_tts_folder():
    """Return the configured on-disk folder for cached TTS audio files."""
    return getattr(
        config, "TTS_FOLDER",
        os.path.join(config.BASE_DIR, "instance", "tts"),
    )


def _serialize_generation(row):
    """Convert a ``tts_generations`` row into a JSON-friendly dict."""
    if not row:
        return None
    status = row["status"]
    return {
        "id": row["id"],
        "page_id": row["page_id"],
        "language": row["language"],
        "language_label": TTS_LANGUAGE_LABELS.get(
            row["language"], row["language"],
        ),
        "status": status,
        "file_size": row["file_size"],
        "retry_count": row["retry_count"] if "retry_count" in row.keys() else 0,
        "requested_at": row["requested_at"],
        "started_at": row["started_at"],
        "completed_at": row["completed_at"],
        "error_message": row["error_message"] if status == "failed" else None,
        "has_file": bool(row["filename"]) and row["status"] == "completed",
    }


def _queue_missing_tts_pages_for_backfill(requested_by):
    """Queue missing page audio as durable pending rows without starting work.

    This is the safe bulk-generation path: the admin request only fills the DB
    queue, and the standalone TTS worker consumes it later with its configured
    concurrency. It deliberately does not call ``run_generation_async``.
    """
    stats = {
        "queued": 0,
        "skipped": 0,
        "empty": 0,
        "errors": 0,
    }
    pages = db.list_tts_backfill_candidates()
    for page in pages:
        try:
            spoken = tts_normalize_text(page["title"], page["content"])
            if not spoken.strip():
                stats["empty"] += 1
                continue
            language = detect_tts_language(spoken)
            content_hash = tts_content_hash_for_text(spoken, language)
            _, created = db.request_tts_generation(
                page_id=page["id"],
                language=language,
                content_hash=content_hash,
                requested_by=requested_by,
            )
            if created:
                stats["queued"] += 1
            else:
                stats["skipped"] += 1
        except Exception:  # noqa: BLE001 - one page must not abort backfill
            stats["errors"] += 1
            current_app.logger.exception(
                "TTS backfill queue failed for page_id=%s",
                page["id"] if page else None,
            )
    stats["considered"] = len(pages)
    return stats


def _normalize_language(value, *, allow_auto=False, allowed=None):
    """Coerce *value* to a supported TTS language code.

    When *allow_auto* is true, the special string ``"auto"`` is preserved
    so the caller can run the language detector against the page content.
    Otherwise the result is one of the codes in *allowed* (defaults to the
    full TTS catalogue when ``None``).  When *value* is missing or
    disabled, the function returns the first sensible fallback (default
    → English → first allowed code).
    """
    return normalize_tts_language(
        value, default=TTS_FALLBACK_LANGUAGE,
        allow_auto=allow_auto, allowed=allowed,
    )


def _resolve_page_or_404(slug):
    """Return the page row for *slug*, or abort 404."""
    page = db.get_page_by_slug(slug)
    if not page:
        abort(404)
    return page


def _manual_tts_queue_limits():
    """Return configured active-job caps for manual TTS requests."""
    total = getattr(config, "TTS_MANUAL_MAX_ACTIVE_JOBS", 1)
    per_user = getattr(config, "TTS_MANUAL_MAX_ACTIVE_PER_USER", 1)
    try:
        total = max(1, int(total))
    except (TypeError, ValueError):
        total = 1
    try:
        per_user = max(1, int(per_user))
    except (TypeError, ValueError):
        per_user = 1
    return total, per_user


def _manual_tts_busy_message(reason):
    """Return the user-facing message for a manual TTS capacity block."""
    if reason == "tts_user_queue_full":
        return (
            "You already have a TTS generation waiting. Please let it finish "
            "before starting another."
        )
    return (
        "The TTS generator is busy. Please try again after the current audio "
        "job finishes."
    )


def _manual_tts_queue_state(user):
    """Return current manual TTS queue capacity for UI/status responses."""
    max_total, max_per_user = _manual_tts_queue_limits()
    active_total = db.count_active_tts_generations()
    if tts_generation_disabled_by_host():
        return {
            "accepting": False,
            "reason": "tts_disabled_by_hosting",
            "message": _HOST_DISABLED_MESSAGE,
            "active_jobs": active_total,
            "max_active_jobs": max_total,
        }
    if active_total >= max_total:
        return {
            "accepting": False,
            "reason": "tts_queue_full",
            "message": _manual_tts_busy_message("tts_queue_full"),
            "active_jobs": active_total,
            "max_active_jobs": max_total,
        }

    if user:
        active_for_user = db.count_active_tts_generations_for_requester(user["id"])
        if active_for_user >= max_per_user:
            return {
                "accepting": False,
                "reason": "tts_user_queue_full",
                "message": _manual_tts_busy_message("tts_user_queue_full"),
                "active_jobs": active_for_user,
                "max_active_jobs": max_per_user,
            }

    return {
        "accepting": True,
        "reason": None,
        "message": "",
        "active_jobs": active_total,
        "max_active_jobs": max_total,
    }


def _manual_tts_queue_rejection(outcome):
    """Return a Flask response tuple for a manual TTS capacity outcome."""
    reason = (outcome or {}).get("reason")
    if reason not in {"tts_queue_full", "tts_user_queue_full"}:
        return None
    status = 429 if reason == "tts_user_queue_full" else 503
    return jsonify({
        "ok": False,
        "error": reason,
        "message": (outcome or {}).get("message") or _manual_tts_busy_message(reason),
        "active_jobs": (outcome or {}).get("active_jobs", 0),
        "max_active_jobs": (outcome or {}).get("max_active_jobs", 1),
    }), status, {"Retry-After": "60"}


def _abort_if_hidden(page, user):
    """Mirror the wiki visibility check used by the page view."""
    if not user_can_view_page(user, page):
        abort(403)


def start_tts_runtime_services(tts_folder=None):
    """Start TTS background recovery services for the current process."""
    if not inline_tts_worker_enabled():
        import logging
        logging.getLogger("bananawiki.tts").info(
            "TTS inline worker disabled; run scripts/tts_worker.py separately"
        )
        return False

    folder = tts_folder or _get_tts_folder()
    try:
        n = recover_stuck_generations(tts_folder=folder)
        if n:
            import logging
            logging.getLogger("bananawiki.tts").info(
                "Startup: auto-recovered %s stuck TTS generation(s)", n,
            )
    except Exception:
        import logging
        logging.getLogger("bananawiki.tts").exception(
            "Startup TTS recovery failed"
        )

    try:
        start_tts_pending_recovery_watchdog(tts_folder=folder)
    except Exception:
        import logging
        logging.getLogger("bananawiki.tts").exception(
            "TTS pending recovery watchdog failed to start"
        )


def register_tts_routes(app):
    """Register the TTS plugin routes on *app*."""

    @app.route("/page/<slug>/tts/status", methods=["GET"])
    @login_required
    @rate_limit(60, 60)
    def tts_status(slug):
        """Return the current generation status for *slug* as JSON."""
        _abort_if_tts_plugin_disabled()
        page = _resolve_page_or_404(slug)
        user = get_current_user()
        settings = db.get_site_settings() or {}
        _abort_if_tts_not_public(user, settings)
        _abort_if_hidden(page, user)
        row = db.get_tts_generation(page["id"])
        has_usable_audio = bool(
            row
            and row["status"] == "completed"
            and row["filename"]
            and row["file_size"] > 0
        )
        in_flight = bool(row and row["status"] in ("pending", "processing"))
        queue_state = _manual_tts_queue_state(user)
        voice_availability = piper_voice_availability_map(TTS_SUPPORTED_LANGUAGES)
        return jsonify({
            "ok": True,
            "page_id": page["id"],
            "page_slug": page["slug"],
            "page_last_edited_at": page["last_edited_at"] or "",
            "supported_languages": [
                {
                    "code": code,
                    "label": TTS_LANGUAGE_LABELS.get(code, code),
                    "local_voice_available": voice_availability.get(code, False),
                }
                for code in TTS_SUPPORTED_LANGUAGES
            ],
            "speed_presets": [float(s) for s in TTS_SPEED_PRESETS],
            "default_speed": float(TTS_DEFAULT_PLAYBACK_SPEED),
            "download_speed_supported": ffmpeg_speed_available(),
            "can_generate": bool(
                settings.get("tts_page_panel_enabled")
                and not has_usable_audio
                and not in_flight
                and queue_state["accepting"]
                and (user or _anonymous_tts_access_enabled(settings))
            ),
            "generate_blocked_reason": queue_state["reason"],
            "generate_blocked_message": queue_state["message"],
            "manual_queue": {
                "accepting": queue_state["accepting"],
                "active_jobs": queue_state["active_jobs"],
                "max_active_jobs": queue_state["max_active_jobs"],
            },
            "generation": _serialize_generation(row),
        })

    @app.route("/page/<slug>/tts/generate", methods=["POST"])
    @login_required
    @rate_limit(10, 60)
    def tts_generate(slug):
        """Initiate on-demand local audio generation for *slug*.

        Refuses the request when the admin has disabled the in-page TTS
        panel (``tts_page_panel_enabled = 0``).  Any visitor who can view
        the page may request missing audio, including public-mode guests
        when public TTS is enabled.  Existing usable audio is never replaced
        here; page edits refresh already-requested audio via the update hook.
        """
        _abort_if_tts_plugin_disabled()
        if tts_generation_disabled_by_host():
            return jsonify({
                "ok": False,
                "error": "tts_disabled_by_hosting",
                "message": _HOST_DISABLED_MESSAGE,
            }), 503
        settings = db.get_site_settings() or {}
        if not settings.get("tts_page_panel_enabled"):
            return jsonify({
                "ok": False,
                "error": "panel_disabled",
                "message": (
                    "Manual TTS generation is disabled. Ask an admin to "
                    "turn on the in-page TTS panel in Site Settings."
                ),
            }), 403
        page = _resolve_page_or_404(slug)
        user = get_current_user()
        settings = db.get_site_settings() or {}
        _abort_if_tts_not_public(user, settings)
        _abort_if_hidden(page, user)

        existing = db.get_tts_generation(page["id"])
        if existing and existing["status"] in ("pending", "processing"):
            return jsonify({
                "ok": False,
                "error": "already_in_progress",
                "message": (
                    "Audio is already being generated for this page. "
                    "Please wait a few seconds and refresh."
                ),
                "generation": _serialize_generation(existing),
            }), 409
        if (
            existing
            and existing["status"] == "completed"
            and existing["filename"]
            and existing["file_size"] > 0
        ):
            return jsonify({
                "ok": False,
                "error": "already_generated",
                "message": "Audio has already been generated for this page.",
                "generation": _serialize_generation(existing),
            }), 409

        if existing and existing["status"] == "completed":
            filename, _ = db.delete_tts_generation(page["id"])
            if filename:
                remove_tts_files(_get_tts_folder(), [filename])

        # Accept the language from JSON or form body.
        if request.is_json:
            payload = request.get_json(silent=True) or {}
            raw_language = payload.get("language")
        else:
            raw_language = request.form.get("language")
        language = _normalize_language(
            raw_language, allow_auto=True,
        )

        spoken = tts_normalize_text(page["title"], page["content"])
        if not spoken.strip():
            return jsonify({
                "ok": False,
                "error": "empty_page",
                "message": "Page is empty, so there is nothing to read aloud.",
            }), 400

        # ``auto`` (or an omitted language) runs the language detector.
        # We do this *after* normalisation so the detector sees the same
        # text the synthesiser will speak.
        if language == "auto":
            language = detect_tts_language(spoken)
        if language not in TTS_SUPPORTED_LANGUAGE_SET:
            return jsonify({
                "ok": False,
                "error": "unsupported_language",
                "message": "The requested language is not supported.",
            }), 400
        content_hash = tts_content_hash_for_text(spoken, language)

        existing = db.get_tts_generation(page["id"])
        if existing and existing["status"] in ("pending", "processing"):
            return jsonify({
                "ok": False,
                "error": "already_in_progress",
                "message": (
                    "Audio is already being generated for this page. "
                    "Please wait a few seconds and refresh."
                ),
                "generation": _serialize_generation(existing),
            }), 409
        if (
            existing
            and existing["status"] == "completed"
            and existing["filename"]
            and existing["file_size"] > 0
        ):
            return jsonify({
                "ok": False,
                "error": "already_generated",
                "message": "Audio has already been generated for this page.",
                "generation": _serialize_generation(existing),
            }), 409

        max_total, max_per_user = _manual_tts_queue_limits()
        row, created, outcome = db.request_limited_tts_generation(
            page_id=page["id"],
            language=language,
            content_hash=content_hash,
            requested_by=(user["id"] if user else None),
            max_active_jobs=max_total,
            max_active_for_requester=max_per_user,
        )
        if outcome:
            reason = outcome.get("reason")
            if reason == "already_in_progress":
                return jsonify({
                    "ok": False,
                    "error": "already_in_progress",
                    "message": (
                        "Audio is already being generated for this page. "
                        "Please wait a few seconds and refresh."
                    ),
                    "generation": _serialize_generation(row),
                }), 409
            if reason == "already_generated":
                return jsonify({
                    "ok": False,
                    "error": "already_generated",
                    "message": "Audio has already been generated for this page.",
                    "generation": _serialize_generation(row),
                }), 409
            outcome.setdefault("message", _manual_tts_busy_message(reason))
            queue_rejection = _manual_tts_queue_rejection(outcome)
            if queue_rejection:
                return queue_rejection

        if created:
            run_generation_async(
                generation_id=row["id"],
                page_id=page["id"],
                language=language,
                content_hash=content_hash,
                text=spoken,
                tts_folder=_get_tts_folder(),
            )
            log_action("tts_generate", request, user=user, page=slug,
                       language=language)

        # Re-fetch so the response reflects the most recent state (the worker
        # thread may already have transitioned past 'pending').
        fresh = db.get_tts_generation(page["id"]) or row
        return jsonify({
            "ok": True,
            "created": bool(created),
            "generation": _serialize_generation(fresh),
        }), (202 if created else 200)

    @app.route("/page/<slug>/tts/cancel", methods=["POST"])
    @login_required
    @editor_required
    @rate_limit(10, 60)
    def tts_cancel(slug):
        """Drop the cached/in-flight TTS row + on-disk audio for *slug*."""
        _abort_if_tts_plugin_disabled()
        page = _resolve_page_or_404(slug)
        user = get_current_user()
        _abort_if_hidden(page, user)
        filename, _ = db.delete_tts_generation(page["id"])
        if filename:
            remove_tts_files(_get_tts_folder(), [filename])
        log_action("tts_cancel", request, user=user, page=slug)
        return jsonify({"ok": True})

    @app.route("/page/<slug>/tts/audio", methods=["GET"])
    @login_required
    @rate_limit(120, 60, exempt_html_nav=False)
    def tts_audio(slug):
        """Stream the cached audio inline so the <audio> tag can play it."""
        return _send_tts_file(slug, as_attachment=False)

    @app.route("/page/<slug>/tts/download", methods=["GET"])
    @login_required
    @rate_limit(60, 60, exempt_html_nav=False)
    def tts_download(slug):
        """Send the cached audio with ``Content-Disposition: attachment``.

        Honours an optional ``?speed=`` query parameter so users can save
        the audio at a faster / slower tempo.  Anything other than one of
        :data:`helpers._tts.TTS_SPEED_PRESETS` is rejected (and treated as
        1.0).  When ffmpeg is missing on the server the cached 1.0x file is
        sent regardless of the requested speed.
        """
        return _send_tts_file(slug, as_attachment=True)

    def _resolve_requested_speed():
        """Return the speed factor requested by the current request.

        Pulled from the ``speed`` query parameter; falls back to 1.0 when
        absent so existing direct links (the cached-audio download button,
        old bookmarks) keep producing the same file they always did.
        """
        raw = request.args.get("speed")
        if raw is None or str(raw).strip() == "":
            return 1.0
        return normalize_tts_speed(raw, default=1.0)

    def _send_tts_file(slug, *, as_attachment):
        """Shared helper used by both audio + download endpoints."""
        _abort_if_tts_plugin_disabled()
        page = _resolve_page_or_404(slug)
        user = get_current_user()
        settings = db.get_site_settings() or {}
        _abort_if_tts_not_public(user, settings)
        _abort_if_hidden(page, user)
        row = db.get_tts_generation(page["id"])
        if not row or row["status"] != "completed" or not row["filename"]:
            abort(404)

        folder = _get_tts_folder()
        # Defence in depth: ``filename`` came from our own code but make
        # sure it is just a filename and resolves inside the folder.
        safe_name = os.path.basename(row["filename"])
        full_path = os.path.realpath(os.path.join(folder, safe_name))
        folder_root = os.path.realpath(folder)
        if (
            safe_name != row["filename"]
            or not full_path.startswith(folder_root + os.sep)
            or not os.path.isfile(full_path)
        ):
            abort(404)

        speed = _resolve_requested_speed()

        # Playback (inline) leaves the speed adjustment to the client via
        # ``HTMLMediaElement.playbackRate``: sending the raw cached file
        # keeps the audio endpoint cheap and lets the player switch speed
        # instantly without a server round-trip.  Downloads at non-unit
        # speeds get re-encoded server-side so the saved MP3 plays at the
        # requested tempo in any external player.
        needs_speed_adjustment = (
            as_attachment
            and abs(speed - 1.0) > 1e-6
            and ffmpeg_speed_available()
        )

        download_name = _download_filename(
            page,
            row,
            speed,
            as_attachment,
            force_mp3=needs_speed_adjustment,
        )
        mimetype = tts_mimetype_for_filename(safe_name)

        if needs_speed_adjustment:
            return _send_speed_adjusted(
                full_path, download_name, speed,
                as_attachment=as_attachment,
            )

        try:
            return send_from_directory(
                folder,
                safe_name,
                as_attachment=as_attachment,
                download_name=download_name,
                mimetype=mimetype,
                max_age=0,
            )
        except NotFound:
            abort(404)

    def _download_filename(page, row, speed, as_attachment, *, force_mp3=False):
        """Build the ``Content-Disposition`` filename for a download.

        Returns ``None`` for inline playback.  For downloads, the language
        code is always included; a non-unit speed gets appended.  The
        extension follows the cached file unless ffmpeg is returning an
        adjusted MP3.
        """
        if not as_attachment:
            return None
        base = f"{page['slug']}-{row['language']}"
        if abs(speed - 1.0) > 1e-6:
            speed_tag = (f"{speed:.2f}").rstrip("0").rstrip(".")
            base = f"{base}-{speed_tag}x"
        ext = "mp3" if force_mp3 else os.path.splitext(row["filename"] or "")[1].lower().lstrip(".")
        if ext not in {"mp3", "wav"}:
            ext = "mp3"
        return f"{base}.{ext}"

    def _send_speed_adjusted(src_path, download_name, speed, *, as_attachment):
        """Render *src_path* at *speed* via ffmpeg and stream it back.

        The temp file is removed after the response is flushed so we do
        not accumulate per-request artefacts on disk.  If ffmpeg fails for
        any reason we fall back to serving the original cached MP3 (at
        1.0x) so the user still gets a working file rather than a 500.
        """
        folder = _get_tts_folder()
        try:
            fd, tmp_path = tempfile.mkstemp(
                prefix="tts-speed-", suffix=".mp3", dir=folder,
            )
            os.close(fd)
        except OSError:
            tmp_path = None

        if tmp_path:
            try:
                write_speed_adjusted_mp3(src_path, tmp_path, speed)
            except RuntimeError:
                # Best-effort cleanup, then fall back to the original.
                try:
                    if os.path.isfile(tmp_path):
                        os.remove(tmp_path)
                except OSError:
                    pass
                tmp_path = None

        if not tmp_path:
            return send_file(
                src_path,
                mimetype=tts_mimetype_for_filename(src_path),
                as_attachment=as_attachment,
                download_name=download_name,
                max_age=0,
            )

        @after_this_request
        def _cleanup(response):  # noqa: ANN001 (Flask hook signature)
            """Delete the per-request temp file after the response is sent."""
            try:
                if tmp_path and os.path.isfile(tmp_path):
                    os.remove(tmp_path)
            except OSError:
                pass
            return response

        return send_file(
            tmp_path,
            mimetype="audio/mpeg",
            as_attachment=as_attachment,
            download_name=download_name,
            max_age=0,
        )

    @app.route("/admin/tts-status", methods=["GET"])
    @login_required
    @admin_required
    def admin_tts_status():
        """Render the TTS status dashboard for site admins.

        Lists each cached generation with its page, language, status, file
        size and timing.  Useful for verifying that the
        ``tts_auto_generate_enabled`` toggle is actually producing audio.
        """
        _abort_if_tts_plugin_disabled()
        settings = db.get_site_settings() or {}
        if (
            settings.get("tts_auto_generate_enabled")
            and inline_tts_worker_enabled()
        ):
            try:
                recover_orphaned_pending_generations(_get_tts_folder())
            except Exception:
                current_app.logger.exception(
                    "Admin TTS status pending recovery failed"
                )
        rows = db.list_tts_generations_with_pages()
        counts = db.count_tts_generations_by_status()
        backfill_candidates = db.count_tts_backfill_candidates()

        total = sum(counts.values())
        completed = counts.get("completed", 0)
        completion_pct = int(round(completed * 100 / total)) if total else 0
        total_bytes = sum((r["file_size"] or 0) for r in rows)

        return render_template(
            "admin/tts_status.html",
            rows=rows,
            counts=counts,
            total=total,
            completion_pct=completion_pct,
            total_bytes=total_bytes,
            auto_enabled=bool(settings.get("tts_auto_generate_enabled")),
            panel_enabled=bool(settings.get("tts_page_panel_enabled")),
            backfill_candidates=backfill_candidates,
            language_labels=TTS_LANGUAGE_LABELS,
        )

    @app.route("/admin/tts-status/backfill", methods=["POST"])
    @login_required
    @admin_required
    @rate_limit(2, 60)
    def admin_tts_status_backfill():
        """Queue missing TTS jobs for every active page.

        This is intentionally a queueing action only. The request never
        performs synthesis and never fans out per-page threads; production
        instances should run ``scripts/tts_worker.py`` to drain the queue.
        """
        _abort_if_tts_plugin_disabled()
        from flask import flash
        from helpers._translations import t as _t

        if tts_generation_disabled_by_host():
            # The worker would never pick these rows up, so do not create them.
            flash(
                _t(
                    "admin.tts.backfill_disabled_by_hosting",
                    default=(
                        "Audio generation is switched off for this wiki by the "
                        "hosting provider, so no audio was queued."
                    ),
                ),
                "warning",
            )
            return redirect(url_for("admin_tts_status"))

        user = get_current_user()
        stats = _queue_missing_tts_pages_for_backfill(
            requested_by=(user["id"] if user else None),
        )
        log_action(
            "tts_backfill_queue",
            request,
            user=user,
            queued=stats["queued"],
            considered=stats["considered"],
            empty=stats["empty"],
            errors=stats["errors"],
        )

        if stats["queued"]:
            flash(
                _t(
                    "admin.tts.backfill_queued",
                    count=stats["queued"],
                    empty=stats["empty"],
                    errors=stats["errors"],
                ),
                "success",
            )
        else:
            flash(_t("admin.tts.backfill_none"), "info")
        return redirect(url_for("admin_tts_status"))

    @app.route("/admin/tts-status/clear", methods=["POST"])
    @login_required
    @admin_required
    @rate_limit(5, 60)
    def admin_tts_status_clear():
        """Drop every cached generation row + on-disk audio.

        Workers in flight will hit their supersede check and exit cleanly.
        New audio is generated only when a user/admin explicitly requests it
        or when a later page save triggers the opt-in auto-generation hook.
        """
        _abort_if_tts_plugin_disabled()
        filenames = db.clear_all_tts_generations()
        if filenames:
            remove_tts_files(_get_tts_folder(), filenames)
        log_action("tts_cache_clear_all", request, user=get_current_user(),
                   count=len(filenames))

        from flask import flash
        from helpers._translations import t as _t
        flash(_t("admin.tts.cache_cleared"), "success")
        return redirect(url_for("admin_tts_status"))

    # Expose the helper module-level too so the plugin's hooks can call it
    # without re-deriving the folder path.
    app.config.setdefault("TTS_FOLDER", _get_tts_folder())
