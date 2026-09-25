"""Text-to-speech: built-in BananaWiki plugin.

Hooks
-----
* ``after_page_create``: when ``tts_auto_generate_enabled`` is on, kicks off
  background local audio synthesis for the new page (language auto-detected).
* ``after_page_update``: drops the cached audio for the page so the audio
  matches the freshly-saved content.  Any worker thread that was synthesising
  the previous version notices the supersede and discards its output.  A fresh
  generation is queued when global auto-generation is on, or when the page
  already had usable / in-flight TTS audio before the edit.
* ``after_page_delete``: removes the cache entry and on-disk audio (the DB row
  would also be removed via the FK ``ON DELETE CASCADE`` but we still need
  to remove the file).
* Plugin disable: clears every cached row + audio file so the feature can be
  re-enabled cleanly.

The hooks never queue synthesis while the host has TTS switched off
(``BW_MANAGED_TTS_DISABLED`` or EasyWiki mode).  Hooks run for every
subscriber regardless of the per-request plugin mask that EasyWiki applies to
routes and template slots, so the check lives here and in
``helpers._tts.enqueue_auto_generation``.  Invalidating stale audio on edit
and removing it on delete still happen, since they only clean up.

Template slot
-------------
* ``page.below_content``: renders the in-page audio player widget.  All of
  the actual logic lives in the static ``app/static/js/tts.js`` client; this
  slot only emits the markup shell.
"""

from __future__ import annotations

import html
import logging
import os

from flask import request, url_for
from markupsafe import Markup

from bananawiki_sdk import Plugin, hook, template_slot

import db
import config
from helpers._auth import is_public_mode_active
from helpers._translations import t as _t
from helpers._tts import (
    TTS_SUPPORTED_LANGUAGES,
    TTS_SPEED_PRESETS, TTS_DEFAULT_PLAYBACK_SPEED,
    detect_tts_language, enqueue_auto_generation, remove_tts_files,
    tts_content_hash_for_text, tts_normalize_text,
    tts_auto_generation_suppressed, tts_generation_disabled_by_host,
)


_logger = logging.getLogger("bananawiki.tts.plugin")
plugin = Plugin("tts")


def _render_tts_content_marker_script(nonce=""):
    """Return an inline script that wraps every word in .wiki-content for TTS highlighting.

    Uses a TreeWalker to find all text nodes inside block elements
    (p, h1–h6, li), splits each on whitespace, and wraps every word in
    ``<span data-tts-segment data-tts-weight="N">``.  Inline markup
    (links, bold, code, etc.) is preserved because only the leaf text
    nodes are replaced.

    The TTS client (``app/static/js/tts.js``) queries ``[data-tts-segment]``
    elements and highlights the one currently being narrated using a
    proportional character-weight mapping onto the audio timeline.

    This runs immediately (not deferred) so the attributes are present before
    the first ``timeupdate`` event fires.
    """
    nonce_attr = f' nonce="{html.escape(nonce)}"' if nonce else ""
    return (
        f"<script{nonce_attr}>"
        "(function(){"
        "var c=document.querySelector('.wiki-content');"
        "if(!c)return;"
        "c.setAttribute('data-tts-content','');"
        "var els=c.querySelectorAll('p,h1,h2,h3,h4,h5,h6,li');"
        "els.forEach(function(el){"
            "if(!(el.textContent||'').trim())return;"
            "var w=document.createTreeWalker(el,NodeFilter.SHOW_TEXT,null,false);"
            "var ns=[];"
            "while(w.nextNode())ns.push(w.currentNode);"
            "ns.forEach(function(tn){"
                "var v=tn.nodeValue;"
                "if(!v||!/\\S/.test(v))return;"
                "var ps=v.split(/(\\s+)/);"
                "var f=document.createDocumentFragment();"
                "ps.forEach(function(p){"
                    "if(!p)return;"
                    "if(/^\\s+$/.test(p)){"
                        "f.appendChild(document.createTextNode(p));"
                    "}else{"
                        "var s=document.createElement('span');"
                        "s.setAttribute('data-tts-segment','');"
                        "s.setAttribute('data-tts-weight',String(Math.max(1,p.length)));"
                        "s.textContent=p;"
                        "f.appendChild(s);"
                    "}"
                "});"
                "tn.parentNode.replaceChild(f,tn);"
            "});"
        "});"
        "})();"
        "</script>"
    )


def _tts_folder():
    """Return the configured TTS cache folder, defaulting to ``instance/tts/``."""
    return getattr(
        config, "TTS_FOLDER",
        os.path.join(config.BASE_DIR, "instance", "tts"),
    )


@plugin.on_load
def setup(app):
    """Ensure the TTS storage folder exists when the plugin loads."""
    folder = _tts_folder()
    try:
        os.makedirs(folder, exist_ok=True)
    except OSError:
        _logger.warning("Could not create TTS folder %s", folder, exc_info=True)


@plugin.on_enable
def enable():
    """Enable the in-page TTS panel without queueing global generation work."""
    try:
        db.update_site_settings(
            tts_page_panel_enabled=1,
        )
    except Exception:  # noqa: BLE001 (enabling must never raise)
        _logger.warning(
            "Could not enable TTS page panel on plugin enable",
            exc_info=True,
        )


def _backfill_all_pages():
    """Queue missing TTS rows for every existing active wiki page.

    This legacy helper is intentionally queue-only: it creates durable
    ``pending`` rows but does not start per-page worker threads. The standalone
    TTS worker drains the queue at its configured pace.
    """
    if tts_generation_disabled_by_host():
        return 0
    queued = 0
    try:
        pages = db.list_tts_backfill_candidates()
    except Exception:  # noqa: BLE001 (keep the enable flow defensive)
        _logger.warning("Could not list TTS backfill candidates", exc_info=True)
        return 0
    for page in pages:
        try:
            spoken = tts_normalize_text(page["title"], page["content"])
            if not spoken.strip():
                continue
            language = detect_tts_language(spoken)
            _, created = db.request_tts_generation(
                page_id=page["id"],
                language=language,
                content_hash=tts_content_hash_for_text(spoken, language),
                requested_by=None,
            )
        except Exception:  # noqa: BLE001 (never abort the whole loop)
            _logger.warning(
                "TTS backfill failed for page %s", _page_id(page),
                exc_info=True,
            )
            continue
        if created:
            queued += 1
    if queued:
        _logger.info("TTS backfill queued %s pending row(s)", queued)
    return queued


@plugin.on_disable
def teardown(app=None):
    """Drop every cached audio file + DB row when the plugin is disabled."""
    try:
        filenames = db.clear_all_tts_generations()
    except Exception:  # noqa: BLE001 (disable must never raise)
        _logger.warning("Failed to clear TTS rows on disable", exc_info=True)
        return
    if filenames:
        remove_tts_files(_tts_folder(), filenames)


def _auto_generate_enabled():
    """Return ``True`` when the admin has opted in to auto-generation."""
    if tts_auto_generation_suppressed():
        return False
    try:
        settings = db.get_site_settings() or {}
    except Exception:  # noqa: BLE001 (settings load must not break hooks)
        _logger.warning("Could not load site settings for TTS auto-generate", exc_info=True)
        return False
    return bool(settings.get("tts_auto_generate_enabled"))


def _page_id(page):
    """Return ``page['id']`` defensively (page rows or dicts)."""
    if not page:
        return None
    try:
        return page["id"]
    except (KeyError, TypeError):
        return getattr(page, "id", None)


def _user_id(user):
    """Return ``user['id']`` defensively (user rows, dicts, or ``None``)."""
    if not user:
        return None
    try:
        return user["id"]
    except (KeyError, TypeError):
        return getattr(user, "id", None)


def _public_tts_panel_enabled(settings):
    """Return True when anonymous public-mode visitors may see the player."""
    return bool(
        is_public_mode_active()
        and settings.get("tts_page_panel_enabled")
        and settings.get("tts_public_access_enabled", 1)
    )


def _tts_generation_state(page_id):
    """Return the cached TTS row for *page_id*, or ``None`` on lookup errors."""
    if not page_id:
        return None
    try:
        return db.get_tts_generation(page_id)
    except Exception:  # noqa: BLE001 (fail closed; page render must survive)
        _logger.warning("Could not inspect TTS cache for page %s", page_id, exc_info=True)
        return None


def _generation_usable(row):
    """Return True when *row* represents a playable completed audio file."""
    return bool(
        row
        and row["status"] == "completed"
        and row["filename"]
        and row["file_size"] > 0
    )


def _generation_in_flight(row):
    """Return True when a worker is already producing audio for this page."""
    return bool(row and row["status"] in ("pending", "processing"))


@hook("after_page_create")
def _autogen_on_create(page, user, **kwargs):
    """Optionally kick off automatic TTS synthesis for a brand-new page."""
    if not db.is_plugin_enabled("tts"):
        return
    if tts_generation_disabled_by_host():
        return
    if not _page_id(page):
        return
    if not _auto_generate_enabled():
        return
    try:
        enqueue_auto_generation(page, _tts_folder(), requested_by=_user_id(user))
    except Exception:  # noqa: BLE001 (never break page-save)
        _logger.warning(
            "TTS auto-generation failed for new page %s", _page_id(page),
            exc_info=True,
        )


@hook("after_page_update")
def _invalidate_on_update(page, user, **kwargs):
    """Invalidate the cached audio when a page is edited.

    A fresh request is queued immediately when auto-generation is on, or when
    the page already had usable / in-flight TTS before the edit.  Any worker
    thread that was still synthesising the previous version notices the
    supersede and discards its output: see the supersede check in
    ``helpers/_tts._run_generation``.
    """
    if not db.is_plugin_enabled("tts"):
        return
    pid = _page_id(page)
    if not pid:
        return
    existing = _tts_generation_state(pid)
    filename, _ = db.delete_tts_generation(pid)
    if filename:
        remove_tts_files(_tts_folder(), [filename])
    should_refresh = (
        _auto_generate_enabled()
        or _generation_usable(existing)
        or _generation_in_flight(existing)
    )
    if not should_refresh or tts_generation_disabled_by_host():
        return
    try:
        enqueue_auto_generation(page, _tts_folder(), requested_by=_user_id(user))
    except Exception:  # noqa: BLE001 (never break page-save)
        _logger.warning(
            "TTS auto-generation failed for updated page %s", pid,
            exc_info=True,
        )


@hook("after_page_delete")
def _cleanup_on_delete(page, user, **kwargs):
    """Remove the cached audio when a page is deleted."""
    if not db.is_plugin_enabled("tts"):
        return
    pid = _page_id(page)
    if not pid:
        return
    filename, _ = db.delete_tts_generation(pid)
    if filename:
        remove_tts_files(_tts_folder(), [filename])


# The helpers below only emit the player markup; all of its behaviour lives in app/static/js/tts.js.

def _format_speed_label(value):
    """Return a compact label for a speed factor (``1.0`` -> ``1×``)."""
    text = (f"{float(value):.2f}").rstrip("0").rstrip(".")
    # ``\u00d7`` is the multiplication sign; reads better than ``x`` for the
    # speed picker and is what video / podcast players conventionally use.
    return f"{text}\u00d7"


def _build_speed_options():
    """Return the playback-speed ``<option>`` HTML for the speed picker.

    The default option is marked ``selected`` so the in-page player loads
    at :data:`TTS_DEFAULT_PLAYBACK_SPEED` without the JS needing to read
    back the configured default.  Any preset whose value matches the
    user's stored preference (set via ``localStorage`` from the client)
    will replace this default at hydration time.
    """
    parts = []
    for preset in TTS_SPEED_PRESETS:
        value = f"{float(preset):.2f}"
        label = _format_speed_label(preset)
        selected = (
            " selected"
            if abs(float(preset) - float(TTS_DEFAULT_PLAYBACK_SPEED)) < 1e-6
            else ""
        )
        parts.append(
            f'<option value="{html.escape(value)}"{selected}>'
            f'{html.escape(label)}</option>'
        )
    return "".join(parts)


def _render_cache_section(page):
    """Return the HTML for the per-page cached-audio info section.

    Removed: the download button in the player area is sufficient.
    """
    return ""


@template_slot("page.below_content")
def render_tts_panel(context):
    """Render the in-page TTS panel for the current wiki page.

    The panel is hidden for anonymous users unless public site mode and the
    TTS public playback setting are both enabled.  The Generate button appears
    only while the page has no playable / in-flight audio; once audio exists,
    nobody can manually regenerate it from the page view.

    Even when the ``tts`` plugin is enabled, this slot stays empty unless
    the admin has explicitly turned on the per-page "Listen to this page"
    panel via Site Settings → Features → TTS.  This keeps the in-page UI
    opt-in and avoids triggering automatic synthesis for every page view.

    The panel is also hidden on edit/creation/propose-edit pages so it
    only appears in read-only page view.
    """
    _EDIT_BLACKLIST = ("edit_page", "create_page", "propose_edit")
    if request and request.endpoint in _EDIT_BLACKLIST:
        return ""

    page = context.get("page") if context else None
    user = context.get("user") if context else None
    if not page:
        return ""
    try:
        if not db.is_plugin_enabled("tts"):
            return ""
    except Exception:  # noqa: BLE001 (fail closed if plugin state is unavailable)
        _logger.warning("Could not verify TTS plugin state for panel render", exc_info=True)
        return ""
    slug = page["slug"] if hasattr(page, "__getitem__") else getattr(page, "slug", None)
    if not slug:
        slug = "home"

    # Respect the admin-controlled opt-in toggle.  The in-page widget is
    # gated SOLELY on ``tts_page_panel_enabled``: when it is off the panel
    # is hidden regardless of whether auto-generation is producing audio
    # in the background.  Cached audio remains reachable through the
    # ``/page/<slug>/tts/audio`` endpoint for anyone with a direct link.
    try:
        settings = db.get_site_settings() or {}
    except Exception:  # noqa: BLE001 (never block page render on settings load)
        settings = {}
    panel_enabled = bool(settings.get("tts_page_panel_enabled"))
    if not panel_enabled:
        return ""
    if not user and not _public_tts_panel_enabled(settings):
        return ""

    generation = _tts_generation_state(_page_id(page))
    completed_audio = _generation_usable(generation)
    in_flight = _generation_in_flight(generation)
    can_request_missing_audio = bool(user) or _public_tts_panel_enabled(settings)
    if not completed_audio and not in_flight and not can_request_missing_audio:
        return ""

    try:
        css_url = url_for("static", filename="css/tts.css")
        js_url = url_for("static", filename="js/tts.js")
    except RuntimeError:
        # No app context: slot is being invoked outside a request (e.g. tests).
        css_url = "/static/css/tts.css"
        js_url = "/static/js/tts.js"

    speed_options_html = _build_speed_options()
    speed_label = html.escape(_t("tts.panel.speed"))
    default_speed_attr = html.escape(f"{float(TTS_DEFAULT_PLAYBACK_SPEED):.2f}")
    cache_html = _render_cache_section(page)
    nonce = context.get("csp_nonce", "") if context else ""
    content_marker_script = _render_tts_content_marker_script(nonce)

    listen_label = html.escape(_t("tts.panel.title"))
    generate_label = html.escape(_t("tts.panel.generate"))
    regenerate_label = html.escape(_t("tts.panel.regenerate"))
    download_label = html.escape(_t("tts.panel.download"))
    aria_listen = html.escape(_t("tts.panel.aria_label"))
    empty_label = html.escape(_t("tts.panel.empty"))
    pending_label = html.escape(_t("tts.panel.pending"))
    processing_label = html.escape(_t("tts.panel.processing"))
    completed_label = html.escape(_t("tts.panel.completed"))
    failed_label = html.escape(_t("tts.panel.failed"))
    timeout_label = html.escape(_t("tts.panel.timeout"))
    network_label = html.escape(_t("tts.panel.network_error"))
    starting_label = html.escape(_t("tts.panel.starting"))
    rate_limited_label = html.escape(_t("tts.panel.rate_limited"))

    generate_btn = ""
    if can_request_missing_audio and not completed_audio and not in_flight:
        generate_btn_label = generate_label
        generate_btn = (
            '<button type="button" class="btn btn-sm tts-generate-btn">'
            f'{generate_btn_label}</button>'
        )

    return Markup(
        f'{content_marker_script}'
        f'<link rel="stylesheet" href="{html.escape(css_url)}">'
        f'<section class="tts-panel" data-tts-panel '
        f'data-slug="{html.escape(slug)}" '
        f'data-tts-default-speed="{default_speed_attr}" '
        f'data-msg-empty="{empty_label}" '
        f'data-msg-generate="{generate_label}" '
        f'data-msg-regenerate="{regenerate_label}" '
        f'data-msg-pending="{pending_label}" '
        f'data-msg-processing="{processing_label}" '
        f'data-msg-completed="{completed_label}" '
        f'data-msg-failed="{failed_label}" '
        f'data-msg-timeout="{timeout_label}" '
        f'data-msg-network="{network_label}" '
        f'data-msg-starting="{starting_label}" '
        f'data-msg-rate-limited="{rate_limited_label}" '
        f'aria-label="{aria_listen}">'
        f'  <div class="tts-panel-header">'
        f'    <div class="tts-panel-heading">'
        f'      <h3 class="tts-panel-title">{listen_label}</h3>'
        f'      <div class="tts-status" data-tts-status role="status" aria-live="polite"></div>'
        f'    </div>'
        f'    <div class="tts-panel-controls">'
        f'      <label class="tts-meta" for="tts-speed-{html.escape(slug)}">{speed_label}</label>'
        f'      <select id="tts-speed-{html.escape(slug)}" '
        f'              class="tts-speed-select" '
        f'              data-tts-speed-select '
        f'              aria-label="{speed_label}">{speed_options_html}</select>'
        f'      {generate_btn}'
        f'    </div>'
        f'  </div>'
        f'  <div class="tts-player tts-hidden">'
        f'    <audio class="tts-audio" controls preload="auto"></audio>'
        f'    <div class="tts-actions">'
        f'      <a class="btn btn-sm btn-outline tts-download" '
        f'         href="#" download>{download_label}</a>'
        f'    </div>'
        f'    <div class="tts-meta"></div>'
        f'  </div>'
        f'  {cache_html}'
        f'</section>'
        f'<script defer src="{html.escape(js_url)}"></script>'
    )
