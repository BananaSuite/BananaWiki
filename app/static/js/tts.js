/* Text-to-speech plugin client.
 *
 * Lives in app/static/js/tts.js and is referenced from the page.below_content
 * template slot rendered by plugins/builtin/tts/__init__.py.
 *
 * Responsibilities:
 *  - Talk to /page/<slug>/tts/{status,generate,cancel,audio,download}.
 *  - Reflect generation state in the in-page player widget.
 *  - Poll while processing; stop polling once we hit a terminal state.
 *  - Survive multiple panels on a single page (re-runs are idempotent).
 */
(function () {
    'use strict';

    var POLL_INTERVAL_MS = 1500;
    var MAX_POLL_DURATION_MS = 5 * 60 * 1000;

    // Discrete set of speed factors the server accepts.  Keep in sync with
    // ``TTS_SPEED_PRESETS`` in ``helpers/_tts.py``.  Used to snap a stored
    // preference back onto a valid option when the user reloads the page.
    var SPEED_PRESETS = [0.75, 1.0, 1.25, 1.5, 1.75, 2.0];
    var DEFAULT_SPEED = 1.0;
    var SPEED_STORAGE_KEY = 'bwTtsSpeed';

    function $(panel, sel) {
        return panel.querySelector(sel);
    }

    function readStoredSpeed() {
        // Wrapped in try/catch because some browsers throw on storage
        // access in private-browsing contexts.  A missing / invalid value
        // just means we use the panel default.
        try {
            var raw = window.localStorage.getItem(SPEED_STORAGE_KEY);
            if (raw === null || raw === '') return null;
            var num = parseFloat(raw);
            if (!isFinite(num)) return null;
            for (var i = 0; i < SPEED_PRESETS.length; i++) {
                if (Math.abs(SPEED_PRESETS[i] - num) < 0.05) {
                    return SPEED_PRESETS[i];
                }
            }
        } catch (e) { /* ignore */ }
        return null;
    }

    function writeStoredSpeed(speed) {
        try {
            window.localStorage.setItem(SPEED_STORAGE_KEY, String(speed));
        } catch (e) { /* ignore */ }
    }

    function panelDefaultSpeed(panel) {
        var raw = panel.getAttribute('data-tts-default-speed');
        if (!raw) return DEFAULT_SPEED;
        var num = parseFloat(raw);
        return isFinite(num) ? num : DEFAULT_SPEED;
    }

    function currentSpeed(panel) {
        var sel = $(panel, '.tts-speed-select');
        if (sel) {
            var num = parseFloat(sel.value);
            if (isFinite(num)) return num;
        }
        return panelDefaultSpeed(panel);
    }

    function applyPlaybackRate(panel) {
        var audio = $(panel, '.tts-audio');
        if (!audio) return;
        var rate = currentSpeed(panel);
        try {
            audio.playbackRate = rate;
            audio.defaultPlaybackRate = rate;
        } catch (e) { /* some old browsers reject out-of-range rates */ }
    }

    function currentThemeMode() {
        var mode = document.documentElement.getAttribute('data-theme') || '';
        return mode.toLowerCase() === 'light' ? 'light' : 'dark';
    }

    function applyAudioTheme(panel) {
        var mode = currentThemeMode();
        var audio = $(panel, '.tts-audio');
        if (audio) audio.style.colorScheme = mode;
        // Keep the panel's own color-scheme in sync so native controls
        // (<select> dropdown, scrollbars) inside the panel match the theme.
        panel.style.colorScheme = mode;
    }

    function syncAllAudioThemes() {
        document.querySelectorAll('[data-tts-panel]').forEach(applyAudioTheme);
    }

    function applyDownloadHref(panel) {
        var slug = panel.getAttribute('data-slug');
        if (!slug) return;
        var completed = panel.getAttribute('data-tts-completed-at') || '';
        var bust = completed ? encodeURIComponent(completed) : Date.now().toString();
        var speed = currentSpeed(panel);
        var params = 'v=' + bust;
        if (Math.abs(speed - 1.0) > 0.001) {
            params += '&speed=' + encodeURIComponent(speed.toFixed(2));
        }
        var href = '/page/' + encodeURIComponent(slug) + '/tts/download?' + params;
        var download = $(panel, '.tts-download');
        if (download) download.href = href;
    }

    function syncSpeedSelector(panel) {
        var sel = $(panel, '.tts-speed-select');
        if (!sel) return;
        var stored = readStoredSpeed();
        if (stored === null) {
            // No stored preference yet: keep whatever option was marked
            // ``selected`` server-side (the configured default).  Still
            // write the current value back to storage so subsequent panels
            // on this device pick it up automatically.
            var num = parseFloat(sel.value);
            if (isFinite(num)) writeStoredSpeed(num);
            return;
        }
        // Snap the <select> to the stored value if it exists as an option.
        var matched = false;
        for (var i = 0; i < sel.options.length; i++) {
            var opt = sel.options[i];
            if (Math.abs(parseFloat(opt.value) - stored) < 0.05) {
                sel.selectedIndex = i;
                matched = true;
                break;
            }
        }
        if (!matched) {
            // Stored value no longer in the option list: clear it so we
            // fall back to the default the next time around.
            writeStoredSpeed(panelDefaultSpeed(panel));
        }
    }

    function getCsrf() {
        if (typeof window.getCsrfToken === 'function') {
            return window.getCsrfToken();
        }
        var meta = document.querySelector('meta[name="csrf-token"]');
        return meta ? meta.getAttribute('content') : '';
    }

    function fetchJson(url, opts) {
        opts = opts || {};
        opts.headers = opts.headers || {};
        if (!opts.headers['X-CSRFToken']) {
            opts.headers['X-CSRFToken'] = getCsrf();
        }
        opts.credentials = 'same-origin';
        return fetch(url, opts).then(function (resp) {
            return resp.json().then(function (data) {
                return { ok: resp.ok, status: resp.status, body: data || {} };
            }).catch(function () {
                return { ok: resp.ok, status: resp.status, body: {} };
            });
        });
    }

    function setStatus(panel, message, kind) {
        var el = $(panel, '.tts-status');
        if (!el) return;
        el.textContent = message || '';
        if (kind) {
            el.setAttribute('data-status', kind);
        } else {
            el.removeAttribute('data-status');
        }
    }

    function hideGenerateButton(panel) {
        var btn = $(panel, '.tts-generate-btn');
        if (!btn) return;
        btn.classList.add('tts-hidden');
        btn.disabled = true;
        btn.setAttribute('aria-hidden', 'true');
    }

    function setBusy(panel, busy) {
        var btn = $(panel, '.tts-generate-btn');
        if (btn) {
            btn.disabled = !!busy;
            btn.setAttribute('aria-busy', busy ? 'true' : 'false');
            if (busy) hideGenerateButton(panel);
        }
        var langSelect = $(panel, '.tts-language-select');
        if (langSelect) langSelect.disabled = !!busy;
    }

    function setGenerateButtonLabel(panel, hasAudio) {
        var btn = $(panel, '.tts-generate-btn');
        if (!btn) return;
        if (hasAudio) {
            btn.classList.add('tts-hidden');
            btn.disabled = true;
            btn.setAttribute('aria-hidden', 'true');
            return;
        }
        btn.classList.remove('tts-hidden');
        btn.disabled = false;
        btn.removeAttribute('aria-hidden');
        btn.textContent = panel.getAttribute('data-msg-generate') || 'Generate audio';
    }

    function showPlayer(panel, generation) {
        var slug = panel.getAttribute('data-slug');
        var player = $(panel, '.tts-player');
        var audio = $(panel, '.tts-audio');
        var download = $(panel, '.tts-download');
        var meta = $(panel, '.tts-player .tts-meta');
        var cancelBtn = $(panel, '.tts-cancel-btn');
        if (!player || !audio || !download) return;
        var completed = generation && generation.completed_at
            ? generation.completed_at
            : '';
        if (completed) {
            panel.setAttribute('data-tts-completed-at', completed);
        } else {
            panel.removeAttribute('data-tts-completed-at');
        }
        var bust = '?v=' + (completed
            ? encodeURIComponent(completed)
            : Date.now().toString());
        var nextSrc = '/page/' + encodeURIComponent(slug) + '/tts/audio' + bust;
        var hadSource = audio.getAttribute('src') === nextSrc;
        if (!hadSource) {
            audio.src = nextSrc;
            try { audio.load(); } catch (e) { /* ignore */ }
        }
        applyDownloadHref(panel);
        applyPlaybackRate(panel);
        applyAudioTheme(panel);
        player.classList.remove('tts-hidden');
        if (cancelBtn) cancelBtn.classList.remove('tts-hidden');
        if (meta && generation) {
            var label = generation.language_label || generation.language || '';
            var parts = [];
            if (label) parts.push(label);
            if (generation.has_file && typeof generation.file_size === 'number' && generation.file_size > 0) {
                parts.push(formatBytes(generation.file_size));
            }
            meta.textContent = parts.join(' \u00b7 ');
            meta.setAttribute('data-tts-meta-base', meta.textContent);
        }
    }

    function hidePlayer(panel) {
        var player = $(panel, '.tts-player');
        var audio = $(panel, '.tts-audio');
        var meta = $(panel, '.tts-player .tts-meta');
        var cancelBtn = $(panel, '.tts-cancel-btn');
        if (player) player.classList.add('tts-hidden');
        if (audio) {
            try { audio.pause(); } catch (e) {}
            audio.removeAttribute('src');
            audio.load();
        }
        if (meta) meta.textContent = '';
        if (cancelBtn) cancelBtn.classList.add('tts-hidden');
    }

    function formatBytes(n) {
        if (!n || n <= 0) return '';
        if (n < 1024) return n + ' B';
        if (n < 1024 * 1024) return Math.round(n / 1024) + ' KB';
        return (n / (1024 * 1024)).toFixed(1) + ' MB';
    }

    function formatDuration(seconds) {
        if (!seconds || !isFinite(seconds) || seconds <= 0) return '';
        var m = Math.floor(seconds / 60);
        var s = Math.floor(seconds % 60);
        return m + ':' + (s < 10 ? '0' : '') + s;
    }

    function transcriptSegments() {
        // Prefer segments in the actual page content (marked by the plugin's
        // initialisation script via data-tts-content / data-tts-segment).
        // This highlights the real text above the player rather than
        // duplicating it in a "Follow along" panel below.
        var contentEl = document.querySelector('[data-tts-content]');
        if (contentEl) {
            var segs = Array.prototype.slice.call(
                contentEl.querySelectorAll('[data-tts-segment]')
            );
            if (segs.length) return segs;
        }
        return [];
    }

    function clearTranscript() {
        transcriptSegments().forEach(function (segment) {
            segment.classList.remove('is-current');
            segment.removeAttribute('aria-current');
        });
        if (window.__bwTtsTranscriptIndex !== undefined) {
            window.__bwTtsTranscriptIndex = -1;
        }
    }

    function isElementInViewport(el) {
        var rect = el.getBoundingClientRect();
        return (
            rect.top >= 0
            && rect.bottom <= (window.innerHeight || document.documentElement.clientHeight)
        );
    }

    function updateTranscript(panel) {
        var audio = $(panel, '.tts-audio');
        var segments = transcriptSegments();
        if (!audio || !segments.length || !isFinite(audio.duration)
                || audio.duration <= 0) return;
        var totalWeight = segments.reduce(function (total, segment) {
            return total + Math.max(1, parseInt(
                segment.getAttribute('data-tts-weight'), 10
            ) || segment.textContent.length);
        }, 0);
        var progress = Math.max(0, Math.min(1, audio.currentTime / audio.duration));
        var target = progress * totalWeight;
        var elapsed = 0;
        var nextIndex = segments.length - 1;
        for (var i = 0; i < segments.length; i++) {
            elapsed += Math.max(1, parseInt(
                segments[i].getAttribute('data-tts-weight'), 10
            ) || segments[i].textContent.length);
            if (target <= elapsed) {
                nextIndex = i;
                break;
            }
        }
        if (window.__bwTtsTranscriptIndex === nextIndex) return;
        clearTranscript();
        var current = segments[nextIndex];
        current.classList.add('is-current');
        current.setAttribute('aria-current', 'true');
        window.__bwTtsTranscriptIndex = nextIndex;

        // Scroll the highlighted element into view only when it is outside
        // the visible viewport: avoids jarring jumps on every timeupdate.
        // Use 'center' so the sentence is easy to read, not clipped at the edge.
        if (!isElementInViewport(current)) {
            try {
                current.scrollIntoView({ behavior: 'smooth', block: 'center' });
            } catch (e) {
                current.scrollIntoView(false);
            }
        }
    }

    function renderState(panel, body) {
        var generation = body && body.generation;
        if (!generation) {
            panel.__bwTtsGenerateInFlight = false;
            if (body && body.can_generate === false
                    && body.generate_blocked_message) {
                hideGenerateButton(panel);
                setStatus(panel, body.generate_blocked_message, 'pending');
                setBusy(panel, false);
                hidePlayer(panel);
                return true;
            }
            setGenerateButtonLabel(panel, false);
            setStatus(panel,
                panel.getAttribute('data-msg-empty')
                || 'Audio has not been generated yet.',
                null);
            setBusy(panel, false);
            hidePlayer(panel);
            return false;
        }
        switch (generation.status) {
            case 'pending':
                panel.__bwTtsGenerateInFlight = true;
                setGenerateButtonLabel(panel, false);
                setStatus(panel,
                    panel.getAttribute('data-msg-pending')
                    || 'Audio queued, starting shortly\u2026',
                    'pending');
                setBusy(panel, true);
                hidePlayer(panel);
                return true;
            case 'processing':
                panel.__bwTtsGenerateInFlight = true;
                setGenerateButtonLabel(panel, false);
                setStatus(panel,
                    (panel.getAttribute('data-msg-processing')
                        || 'Generating audio in') + ' '
                        + (generation.language_label || generation.language)
                        + '\u2026',
                    'processing');
                setBusy(panel, true);
                hidePlayer(panel);
                return true;
            case 'completed':
                panel.__bwTtsGenerateInFlight = false;
                setGenerateButtonLabel(panel, true);
                setStatus(panel,
                    panel.getAttribute('data-msg-completed') || 'Audio ready.',
                    'completed');
                setBusy(panel, false);
                showPlayer(panel, generation);
                return false;
            case 'failed':
                panel.__bwTtsGenerateInFlight = false;
                setGenerateButtonLabel(panel, false);
                var err = generation.error_message
                    || panel.getAttribute('data-msg-failed')
                    || 'Generation failed.';
                setStatus(panel, err, 'failed');
                setBusy(panel, false);
                hidePlayer(panel);
                return false;
            default:
                panel.__bwTtsGenerateInFlight = false;
                setStatus(panel, '', null);
                setBusy(panel, false);
                hidePlayer(panel);
                return false;
        }
    }

    function poll(panel, deadline) {
        var slug = panel.getAttribute('data-slug');
        if (Date.now() > deadline) {
            panel.__bwTtsGenerateInFlight = false;
            setGenerateButtonLabel(panel, false);
            setStatus(panel,
                panel.getAttribute('data-msg-timeout')
                || 'Audio is taking longer than expected. Please refresh the page.',
                'failed');
            setBusy(panel, false);
            return;
        }
        fetchJson('/page/' + encodeURIComponent(slug) + '/tts/status')
            .then(function (resp) {
                if (!resp.ok) {
                    panel.__bwTtsGenerateInFlight = false;
                    setGenerateButtonLabel(panel, false);
                    setStatus(panel,
                        panel.getAttribute('data-msg-network')
                        || 'Could not contact the server. Please retry.',
                        'failed');
                    setBusy(panel, false);
                    return;
                }
                var keepPolling = renderState(panel, resp.body);
                if (keepPolling) {
                    setTimeout(function () { poll(panel, deadline); }, POLL_INTERVAL_MS);
                }
            })
            .catch(function () {
                panel.__bwTtsGenerateInFlight = false;
                setGenerateButtonLabel(panel, false);
                setStatus(panel,
                    panel.getAttribute('data-msg-network')
                    || 'Could not contact the server. Please retry.',
                    'failed');
                setBusy(panel, false);
            });
    }

    function generate(panel) {
        if (panel.__bwTtsGenerateInFlight) return;
        panel.__bwTtsGenerateInFlight = true;
        var slug = panel.getAttribute('data-slug');
        var language = 'auto';
        setBusy(panel, true);
        setStatus(panel,
            panel.getAttribute('data-msg-starting')
            || 'Starting audio generation\u2026',
            'pending');
        fetchJson('/page/' + encodeURIComponent(slug) + '/tts/generate', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ language: language }),
        }).then(function (resp) {
            if (resp.status === 409) {
                setStatus(panel,
                    (resp.body && resp.body.message)
                    || 'Audio is already being generated for this page. Please wait\u2026',
                    'processing');
                setBusy(panel, true);
                if (renderState(panel, resp.body)) {
                    poll(panel, Date.now() + MAX_POLL_DURATION_MS);
                }
                return;
            }
        if (resp.status === 503 && resp.body
                && resp.body.error === 'tts_disabled_by_hosting') {
            panel.__bwTtsGenerateInFlight = false;
            hideGenerateButton(panel);
            setStatus(panel,
                (resp.body && resp.body.message)
                || 'Audio generation has been temporarily disabled by the server administrator.',
                'failed');
            setBusy(panel, false);
            return;
        }
        if (
            resp.status === 503
            || (
                resp.status === 429
                && resp.body
                && (
                    resp.body.error === 'tts_queue_full'
                    || resp.body.error === 'tts_user_queue_full'
                )
            )
        ) {
            hideGenerateButton(panel);
            setStatus(panel,
                (resp.body && resp.body.message)
                || 'The TTS generator is busy. Please try again shortly.',
                'pending');
            setBusy(panel, false);
            poll(panel, Date.now() + MAX_POLL_DURATION_MS);
            return;
        }
            if (resp.status === 429) {
                panel.__bwTtsGenerateInFlight = false;
                setGenerateButtonLabel(panel, false);
                setStatus(panel,
                    panel.getAttribute('data-msg-rate-limited')
                    || 'You\u2019re generating audio too quickly. Try again in a moment.',
                    'failed');
                setBusy(panel, false);
                return;
            }
            if (!resp.ok) {
                panel.__bwTtsGenerateInFlight = false;
                setGenerateButtonLabel(panel, false);
                setStatus(panel,
                    (resp.body && resp.body.message)
                    || panel.getAttribute('data-msg-failed')
                    || 'Could not start generation.',
                    'failed');
                setBusy(panel, false);
                return;
            }
            if (renderState(panel, resp.body)) {
                poll(panel, Date.now() + MAX_POLL_DURATION_MS);
            }
        }).catch(function () {
            panel.__bwTtsGenerateInFlight = false;
            setGenerateButtonLabel(panel, false);
            setStatus(panel,
                panel.getAttribute('data-msg-network')
                || 'Could not contact the server. Please retry.',
                'failed');
            setBusy(panel, false);
        });
    }

    function cancel(panel) {
        var slug = panel.getAttribute('data-slug');
        setBusy(panel, true);
        fetchJson('/page/' + encodeURIComponent(slug) + '/tts/cancel', {
            method: 'POST',
        }).then(function () {
            panel.__bwTtsGenerateInFlight = false;
            setGenerateButtonLabel(panel, false);
            setStatus(panel, '', null);
            setBusy(panel, false);
            hidePlayer(panel);
        }).catch(function () {
            panel.__bwTtsGenerateInFlight = false;
            setBusy(panel, false);
        });
    }

    function bindPanel(panel) {
        if (panel.__bwTtsBound) return;
        panel.__bwTtsBound = true;

        var generateBtn = $(panel, '.tts-generate-btn');
        var cancelBtn = $(panel, '.tts-cancel-btn');
        if (generateBtn) {
            generateBtn.addEventListener('click', function (ev) {
                ev.preventDefault();
                generate(panel);
            });
        }
        if (cancelBtn) {
            cancelBtn.addEventListener('click', function (ev) {
                ev.preventDefault();
                cancel(panel);
            });
        }

        // Restore the user's last-used speed (if any) and wire up the speed
        // selector so it (a) re-points the playing <audio> at the new rate
        // and (b) refreshes the download URL with the matching query param.
        syncSpeedSelector(panel);
        applyPlaybackRate(panel);
        applyDownloadHref(panel);
        var speedSel = $(panel, '.tts-speed-select');
        if (speedSel) {
            speedSel.addEventListener('change', function () {
                var num = parseFloat(speedSel.value);
                if (isFinite(num)) writeStoredSpeed(num);
                applyPlaybackRate(panel);
                applyDownloadHref(panel);
            });
        }
        var audio = $(panel, '.tts-audio');
        if (audio) {
            applyAudioTheme(panel);
            panel.__bwTtsPlaybackStarted = false;
            audio.addEventListener('loadedmetadata', function () {
                applyPlaybackRate(panel);
                applyAudioTheme(panel);
                var meta = $(panel, '.tts-player .tts-meta');
                if (meta) {
                    var base = meta.getAttribute('data-tts-meta-base') || '';
                    var dur = formatDuration(audio.duration);
                    if (dur) {
                        meta.textContent = base ? base + ' \u00b7 ' + dur : dur;
                    }
                }
            });
            audio.addEventListener('play', function () {
                applyPlaybackRate(panel);
                applyAudioTheme(panel);
                if (audio.networkState === audio.NETWORK_NO_SOURCE && audio.src) {
                    try { audio.load(); } catch (e) { /* ignore */ }
                }
                panel.__bwTtsPlaybackStarted = true;
                updateTranscript(panel);
            });
            audio.addEventListener('timeupdate', function () {
                if (panel.__bwTtsPlaybackStarted) updateTranscript(panel);
            });
            audio.addEventListener('seeking', function () {
                if (panel.__bwTtsPlaybackStarted) updateTranscript(panel);
            });
        }

        // Initial state pulled from the server so that two users opening the
        // same page see the in-flight generation initiated by either one.
        var slug = panel.getAttribute('data-slug');
        fetchJson('/page/' + encodeURIComponent(slug) + '/tts/status')
            .then(function (resp) {
                if (!resp.ok) return;
                var body = resp.body;
                var gen = body.generation;

                // Detect stale generation: the page was edited after the
                // generation completed, so the cached audio is outdated.
                if (gen && gen.status === 'completed' && gen.completed_at
                    && body.page_last_edited_at
                    && body.page_last_edited_at > gen.completed_at) {
                    setStatus(panel, '', null);
                    hidePlayer(panel);
                    return;
                }

                var keepPolling = renderState(panel, body);
                if (keepPolling) {
                    poll(panel, Date.now() + MAX_POLL_DURATION_MS);
                }
            })
            .catch(function () { /* ignore: will retry on click */ });
    }

    function init() {
        var panels = document.querySelectorAll('[data-tts-panel]');
        panels.forEach(function(p) { delete p.__bwTtsBound; });
        panels.forEach(bindPanel);
    }

    // Re-init on bfcache restore (the old `__bwTtsBound` markers are stale)
    window.addEventListener('pageshow', function(e) {
        if (e.persisted) init();
    });

    if (window.MutationObserver) {
        var themeObserver = new MutationObserver(syncAllAudioThemes);
        themeObserver.observe(document.documentElement, {
            attributes: true,
            attributeFilter: ['data-theme', 'style', 'class'],
        });
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
}());
