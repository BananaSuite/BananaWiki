//  Announcements bar

function initAnnouncements() {
    document.querySelectorAll('[data-announcement-background]').forEach(function(el) {
        var background = el.dataset.announcementBackground || '';
        var text = el.dataset.announcementText || '';
        if (/^#[0-9a-f]{6}$/i.test(background) && /^#[0-9a-f]{6}$/i.test(text)) {
            el.style.background = background;
            el.style.color = text;
        }
    });

    var bar = document.getElementById('announcements-bar');
    if (!bar) return;

    var allSlides = Array.from(bar.querySelectorAll('.announcement-slide'));
    if (!allSlides.length) return;

    // Filter out previously dismissed announcements (only for removable ones)
    var dismissed = [];
    var dismissalStorageKey = 'dismissed_announcements:' + (bar.dataset.viewerId || 'guest');
    try {
        dismissed = JSON.parse(localStorage.getItem(dismissalStorageKey) || '[]');
        if (!Array.isArray(dismissed)) dismissed = [];
    } catch (e) { dismissed = []; }

    function isDismissed(s) {
        var id = parseInt(s.dataset.annId, 10);
        var revision = parseInt(s.dataset.annRevision || '1', 10);
        return dismissed.indexOf(id + ':' + revision) !== -1 ||
            (revision === 1 && dismissed.indexOf(id) !== -1);
    }

    var slides = allSlides.filter(function(s) {
        if (s.dataset.notRemovable === '1') return true;
        return !isDismissed(s);
    });

    if (!slides.length) return;

    bar.style.display = 'block';
    var current = 0;

    function showSlide(idx) {
        slides.forEach(function(s) { s.style.display = 'none'; });
        if (!slides.length) { bar.style.display = 'none'; return; }
        slides[idx].style.display = 'block';
        // Update nav visibility and counter in the active slide
        var slide = slides[idx];
        var prev = slide.querySelector('.ann-prev');
        var next = slide.querySelector('.ann-next');
        var counter = slide.querySelector('.ann-counter');
        var cur = slide.querySelector('.ann-current');
        var hasMany = slides.length > 1;
        if (prev) prev.style.display = hasMany ? '' : 'none';
        if (next) next.style.display = hasMany ? '' : 'none';
        if (counter) counter.style.display = hasMany ? '' : 'none';
        if (cur) cur.textContent = (idx + 1) + ' / ' + slides.length;
    }

    showSlide(0);

    // Countdown timer logic
    function updateCountdowns() {
        var anyExpired = false;
        slides.forEach(function(s) {
            var cdEl = s.querySelector('.ann-countdown');
            var expiresAt = s.dataset.expiresAt;
            if (!expiresAt) {
                if (s.dataset.showCountdown === '1' && cdEl) {
                    cdEl.style.display = 'inline';
                    cdEl.textContent = 'No expiry set';
                }
                return;
            }
            // Strip any existing timezone offset (e.g. +00:00) or Z suffix so we can
            // re-append 'Z' to force UTC interpretation by the Date constructor.
            var normalized = expiresAt.replace(/[+-]\d{2}:\d{2}$/, '').replace(/Z$/, '');
            var expDate = new Date(normalized + 'Z');
            if (isNaN(expDate.getTime())) {
                if (s.dataset.showCountdown === '1' && cdEl) {
                    cdEl.style.display = 'inline';
                    cdEl.textContent = 'Invalid date';
                }
                return;
            }
            var now = new Date();
            var diff = expDate.getTime() - now.getTime();
            if (diff <= 0) {
                // Timer expired: hide the announcement
                s.style.display = 'none';
                anyExpired = true;
                return;
            }
            if (s.dataset.showCountdown !== '1' || !cdEl) return;
            var totalSec = Math.floor(diff / 1000);
            var days = Math.floor(totalSec / 86400);
            var hours = Math.floor((totalSec % 86400) / 3600);
            var minutes = Math.floor((totalSec % 3600) / 60);
            var seconds = totalSec % 60;
            var parts = [];
            if (days > 0) parts.push(days + 'd');
            if (hours > 0) parts.push(hours + 'h');
            parts.push(minutes + 'm');
            parts.push(seconds + 's');
            cdEl.style.display = 'inline';
            cdEl.textContent = parts.join(' ');
        });

        if (anyExpired) {
            // Rebuild slides list excluding expired and dismissed ones
            slides = Array.from(bar.querySelectorAll('.announcement-slide')).filter(function(s) {
                if (s.dataset.showCountdown === '1' && s.dataset.expiresAt) {
                    var norm = s.dataset.expiresAt.replace(/[+-]\d{2}:\d{2}$/, '').replace(/Z$/, '');
                    var expDate = new Date(norm + 'Z');
                    if (!isNaN(expDate.getTime()) && expDate.getTime() <= Date.now()) return false;
                }
                if (s.dataset.notRemovable === '1') return true;
                return !isDismissed(s);
            });
            if (!slides.length) { bar.style.display = 'none'; return; }
            if (current >= slides.length) current = slides.length - 1;
            showSlide(current);
        }
    }

    updateCountdowns();
    setInterval(updateCountdowns, 1000);

    bar.addEventListener('click', function(e) {
        var target = e.target;
        if (target.classList.contains('ann-prev')) {
            current = (current - 1 + slides.length) % slides.length;
            showSlide(current);
        } else if (target.classList.contains('ann-next')) {
            current = (current + 1) % slides.length;
            showSlide(current);
        } else if (target.classList.contains('ann-close')) {
            var id = parseInt(target.dataset.annId, 10);
            var slide = target.closest('.announcement-slide');
            var revision = parseInt((slide && slide.dataset.annRevision) || '1', 10);
            dismissed.push(id + ':' + revision);
            try { localStorage.setItem(dismissalStorageKey, JSON.stringify(dismissed)); } catch (e) {}
            slides = slides.filter(function(s) {
                return parseInt(s.dataset.annId, 10) !== id;
            });
            if (!slides.length) { bar.style.display = 'none'; return; }
            if (current >= slides.length) current = slides.length - 1;
            showSlide(current);
        }
    });
}


// Keep long navigation trees usable without rendering every page link up front.
document.addEventListener('click', function(event) {
    var more = event.target.closest('[data-sidebar-load]');
    if (!more || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    if (more.dataset.loading || more.parentElement.dataset.reorderPending) return;
    var label = more.textContent;
    more.dataset.loading = '1';
    more.setAttribute('aria-busy', 'true');
    more.textContent = _t('js.nav.loading');
    fetch(more.dataset.sidebarLoad, { credentials: 'same-origin' })
        .then(function(response) { return bwParseJsonResponse(response, _t('js.nav.load_failed')); })
        .then(function(data) {
            more.insertAdjacentHTML('beforebegin', data.html);
            var current = document.querySelector('.sidebar-current-page');
            if (current) {
                var matches = more.parentElement.querySelectorAll('.nav-item-row a.nav-item');
                matches.forEach(function(link) {
                    if (link.href === current.href) {
                        link.classList.add('active');
                        current.remove();
                    }
                });
            }
            more.remove();
        })
        .catch(function(error) {
            more.textContent = label;
            more.removeAttribute('aria-busy');
            delete more.dataset.loading;
            bwShowFlash(error.message || _t('js.nav.load_failed'), 'error');
        });
});

document.addEventListener('DOMContentLoaded', initAnnouncements);
