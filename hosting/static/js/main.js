/* BananaWiki Hosting Portal: main.js */
(function () {
    'use strict';

    function initHostingBanners() {
        var bar = document.getElementById('hosting-banners-bar');
        if (!bar) return;
        var allSlides = Array.from(bar.querySelectorAll('.hosting-banner'));
        var dismissed = [];
        var storageKey = 'dismissed_hosting_banners:' + (bar.dataset.viewerId || 'guest');
        try {
            dismissed = JSON.parse(localStorage.getItem(storageKey) || '[]');
            if (!Array.isArray(dismissed)) dismissed = [];
        } catch (e) { dismissed = []; }

        allSlides.forEach(function (slide) {
            var background = slide.dataset.bannerBackground || '';
            var text = slide.dataset.bannerText || '';
            if (/^#[0-9a-f]{6}$/i.test(background) && /^#[0-9a-f]{6}$/i.test(text)) {
                slide.style.background = background;
                slide.style.color = text;
            }
        });

        function token(slide) {
            return slide.dataset.bannerId + ':' + (slide.dataset.bannerRevision || '1');
        }
        function isDismissed(slide) {
            return slide.dataset.notRemovable !== '1' && dismissed.indexOf(token(slide)) !== -1;
        }

        var slides = allSlides.filter(function (slide) { return !isDismissed(slide); });
        if (!slides.length) return;
        var current = 0;
        bar.style.display = 'block';

        function showSlide(index) {
            allSlides.forEach(function (slide) { slide.style.display = 'none'; });
            if (!slides.length) { bar.style.display = 'none'; return; }
            current = Math.max(0, Math.min(index, slides.length - 1));
            var slide = slides[current];
            slide.style.display = 'block';
            var hasMany = slides.length > 1;
            slide.querySelectorAll('.hosting-banner-nav').forEach(function (button) { button.hidden = !hasMany; });
            var position = slide.querySelector('.hosting-banner-position');
            if (position) {
                position.hidden = !hasMany;
                position.textContent = (current + 1) + ' / ' + slides.length;
            }
        }

        function updateCountdowns() {
            var expired = [];
            slides.forEach(function (slide) {
                var rawExpiry = slide.dataset.expiresAt;
                if (!rawExpiry) return;
                var expiry = new Date(rawExpiry);
                if (isNaN(expiry.getTime())) return;
                var difference = expiry.getTime() - Date.now();
                if (difference <= 0) {
                    expired.push(slide);
                    return;
                }
                if (slide.dataset.showCountdown !== '1') return;
                var countdown = slide.querySelector('.hosting-banner-countdown');
                if (!countdown) return;
                var secondsTotal = Math.floor(difference / 1000);
                var days = Math.floor(secondsTotal / 86400);
                var hours = Math.floor((secondsTotal % 86400) / 3600);
                var minutes = Math.floor((secondsTotal % 3600) / 60);
                var seconds = secondsTotal % 60;
                var parts = [];
                if (days) parts.push(days + 'd');
                if (hours || days) parts.push(hours + 'h');
                parts.push(minutes + 'm');
                parts.push(seconds + 's');
                countdown.textContent = parts.join(' ');
                countdown.hidden = false;
            });
            if (expired.length) {
                slides = slides.filter(function (slide) { return expired.indexOf(slide) === -1; });
                expired.forEach(function (slide) { slide.remove(); });
                if (current >= slides.length) current = slides.length - 1;
                showSlide(current);
            }
        }

        bar.addEventListener('click', function (event) {
            var button = event.target.closest('button');
            if (!button) return;
            if (button.classList.contains('hosting-banner-prev')) {
                showSlide((current - 1 + slides.length) % slides.length);
            } else if (button.classList.contains('hosting-banner-next')) {
                showSlide((current + 1) % slides.length);
            } else if (button.classList.contains('hosting-banner-close')) {
                var slide = button.closest('.hosting-banner');
                dismissed.push(token(slide));
                try { localStorage.setItem(storageKey, JSON.stringify(dismissed)); } catch (e) {}
                slides = slides.filter(function (candidate) { return candidate !== slide; });
                slide.remove();
                if (current >= slides.length) current = slides.length - 1;
                showSlide(current);
            }
        });

        showSlide(0);
        updateCountdowns();
        window.setInterval(updateCountdowns, 1000);
    }

    document.addEventListener('DOMContentLoaded', function () {
        initHostingBanners();
        /* ── Flash message dismiss ── */
        document.querySelectorAll('.flash-close').forEach(function (btn) {
            btn.addEventListener('click', function () {
                btn.closest('.flash').remove();
            });
        });

        /* ── Language selector auto-submit ── */
        document.querySelectorAll('.js-language-switcher').forEach(function (sel) {
            sel.addEventListener('change', function () {
                if (sel.form) {
                    sel.form.submit();
                }
            });
        });

        /* ── Confirm dialog for dangerous actions ── */
        document.querySelectorAll('.js-confirm-form').forEach(function (form) {
            form.addEventListener('submit', function (e) {
                var message = form.getAttribute('data-confirm');
                if (message && !confirm(message)) {
                    e.preventDefault();
                }
            });
        });

        /* ── Loading overlay for delicate operations ── */
        var overlay = document.getElementById('hosting-loading');
        var loadingText = document.getElementById('hosting-loading-text');

        document.querySelectorAll('.js-loading-form').forEach(function (form) {
            form.addEventListener('submit', function (e) {
                if (e.defaultPrevented) return;
                var msg = form.getAttribute('data-loading-text') || 'Please wait\u2026';
                if (loadingText) loadingText.textContent = msg;
                if (overlay) overlay.style.display = 'flex';

                /* Download forms: use fetch() instead of iframe navigation.
                 *
                 * The old approach targeted a hidden iframe and relied on its
                 * "load" event to dismiss the overlay.  That breaks for file
                 * downloads (Content-Disposition: attachment) because browsers
                 * do NOT fire "load" on the iframe. The overlay stayed
                 * visible forever.  It also swallowed server-side flash
                 * messages on error, since the redirected HTML page was
                 * rendered inside the hidden iframe where the user could
                 * never see it.
                 *
                 * fetch() gives us full control: we can read the response
                 * headers, trigger the download programmatically on success,
                 * and reload the page on error so the user sees the flash
                 * messages from the server. */
                if (form.hasAttribute('data-download')) {
                    e.preventDefault();
                    var formData = new FormData(form);
                    fetch(form.action, { method: 'POST', body: formData, credentials: 'same-origin' })
                        .then(function (resp) {
                            /* Successful file download: Content-Type should be
                             * application/zip (or another non-HTML type). */
                            var contentType = resp.headers.get('Content-Type') || '';
                            if (resp.ok && contentType.indexOf('text/html') === -1) {
                                var disposition = resp.headers.get('Content-Disposition') || '';
                                var filename = 'download.zip';
                                var match = disposition.match(/filename\*?=(?:UTF-8'')?"?([^";\n]+)"?/i);
                                if (match) filename = decodeURIComponent(match[1]);
                                return resp.blob().then(function (blob) {
                                    var url = URL.createObjectURL(blob);
                                    var a = document.createElement('a');
                                    a.href = url;
                                    a.download = filename;
                                    document.body.appendChild(a);
                                    a.click();
                                    a.remove();
                                    URL.revokeObjectURL(url);
                                });
                            }
                            /* Error or HTML redirect: reload so the user
                             * sees the server's flash messages. */
                            window.location.reload();
                        })
                        .catch(function () {
                            /* Network error: reload as fallback. */
                            window.location.reload();
                        })
                        .finally(function () {
                            if (overlay) overlay.style.display = 'none';
                        });
                }
            });
        });
    });

    /* ── Hamburger menu toggle for mobile navigation ── */
    (function () {
        var toggle = document.getElementById('nav-toggle');
        var menu = document.getElementById('nav-menu');
        var overlay = document.getElementById('nav-overlay');
        if (toggle && menu) {
            function setOpen(open) {
                menu.classList.toggle('open', open);
                toggle.classList.toggle('open', open);
                toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
                if (overlay) {
                    overlay.classList.toggle('active', open);
                    overlay.hidden = !open;
                }
                document.body.classList.toggle('hosting-nav-open', open);
            }
            toggle.addEventListener('click', function (e) {
                e.stopPropagation();
                setOpen(!menu.classList.contains('open'));
            });
            document.addEventListener('click', function (e) {
                if (
                    menu.classList.contains('open') &&
                    !menu.contains(e.target) &&
                    !toggle.contains(e.target)
                ) {
                    setOpen(false);
                }
            });
            if (overlay) {
                overlay.addEventListener('click', function () {
                    setOpen(false);
                });
            }
            document.addEventListener('keydown', function (e) {
                if (e.key === 'Escape' && menu.classList.contains('open')) {
                    setOpen(false);
                }
            });
        }
    })();

    /* ── Always hide loading overlay on pageshow (bfcache restore) ── */
    window.addEventListener('pageshow', function () {
        var overlay = document.getElementById('hosting-loading');
        if (overlay) overlay.style.display = 'none';
    });
})();
