
(function initSidebarSearch() {
    var input = document.getElementById('sidebar-search-input');
    var results = document.getElementById('sidebar-search-results');
    var scopeBtn = document.getElementById('sidebar-search-scope-btn');
    if (!input || !results) return;

    var searchContent = false;
    var debounceTimer = null;

    if (scopeBtn) {
        scopeBtn.addEventListener('click', function() {
            searchContent = !searchContent;
            scopeBtn.classList.toggle('active', searchContent);
            scopeBtn.title = window._t ? window._t(searchContent ? 'js.search.scope_content_active' : 'js.search.scope_title_only_active') : (searchContent ? 'Searching in title + content (click to search title only)' : 'Search in content too');
            if (input.value.trim()) doSearch(input.value.trim());
        });
    }

    input.addEventListener('input', function() {
        clearTimeout(debounceTimer);
        var q = input.value.trim();
        if (!q) { results.hidden = true; results.innerHTML = ''; return; }
        debounceTimer = setTimeout(function() { doSearch(q); }, 220);
    });

    input.addEventListener('keydown', function(e) {
        if (e.key === 'Enter') {
            e.preventDefault();
            var q = input.value.trim();
            var params = new URLSearchParams();
            if (q) params.set('q', q);
            params.set('scope', 'all');
            window.location.href = '/search' + (params.toString() ? '?' + params.toString() : '');
            return;
        }
        if (e.key === 'Escape') { results.hidden = true; input.value = ''; }
    });

    document.addEventListener('click', function(e) {
        if (!input.contains(e.target) && !results.contains(e.target) && (!scopeBtn || !scopeBtn.contains(e.target))) {
            results.hidden = true;
        }
    });

    function doSearch(q) {
        var scope = searchContent ? 'content' : 'title';
        fetch('/api/sidebar/search?q=' + encodeURIComponent(q) + '&scope=' + scope, { credentials: 'same-origin' })
            .then(function(r) { return r.ok ? r.json() : null; })
            .then(function(data) {
                if (!data) return;
                renderResults(data);
            })
            .catch(function(err) {
                // Network error: the next keystroke will retry automatically.
                if (typeof console !== 'undefined') { console.warn('Sidebar search failed:', err); }
            });
    }

    function renderResults(data) {
        var cats = data.categories || [];
        var pages = data.pages || [];
        if (cats.length === 0 && pages.length === 0) {
            results.innerHTML = '<div class="sidebar-search-empty">No results found.</div>';
            results.hidden = false;
            return;
        }
        var html = '';
        cats.forEach(function(c) {
            html += '<a class="sidebar-search-result js-scroll-to-category" href="#" data-cat-id="' + Number(c.id) + '">'
                  + '<span class="sidebar-search-result-type">folder</span>'
                  + escHtml(c.name)
                  + '</a>';
        });
        pages.forEach(function(p) {
            var reservationLabel = p.reservation_label || '';
            var cooldownLabel = p.cooldown_label || '';
            var statusIcons = '';
            if (p.is_reserved) {
                statusIcons += '<span class="sidebar-reservation-indicator' + (p.reserved_by_current_user ? ' sidebar-reservation-indicator-self' : '') + '" title="' + escHtml(reservationLabel) + '">'
                    + '<svg class="sidebar-reservation-indicator-icon" aria-hidden="true" viewBox="0 0 16 16" focusable="false">'
                    + '<path fill="currentColor" d="M4 7V5.5a4 4 0 1 1 8 0V7a1.5 1.5 0 0 1 1.5 1.5v5A1.5 1.5 0 0 1 12 15H4a1.5 1.5 0 0 1-1.5-1.5v-5A1.5 1.5 0 0 1 4 7m1 0h6V5.5a3 3 0 1 0-6 0zm-1 1a.5.5 0 0 0-.5.5v5A.5.5 0 0 0 4 14h8a.5.5 0 0 0 .5-.5v-5A.5.5 0 0 0 12 8z"></path>'
                    + '</svg>'
                    + '<span class="sr-only">' + escHtml(reservationLabel) + '</span>'
                  + '</span>';
            }
            if (p.user_in_cooldown) {
                statusIcons += '<span class="sidebar-reservation-indicator sidebar-reservation-indicator-cooldown" title="' + escHtml(cooldownLabel) + '">'
                    + '<svg class="sidebar-reservation-indicator-icon" aria-hidden="true" viewBox="0 0 16 16" focusable="false">'
                    + '<path fill="currentColor" d="M8 1.25a.75.75 0 0 1 .75.75v2.19l1.55-1.56a.75.75 0 1 1 1.06 1.06L9.81 5.25H12a.75.75 0 0 1 0 1.5H9.81l1.56 1.55a.75.75 0 0 1-1.06 1.06L8.75 7.81V10a.75.75 0 0 1-1.5 0V7.81L5.7 9.37A.75.75 0 0 1 4.64 8.3L6.19 6.75H4a.75.75 0 0 1 0-1.5h2.19L4.64 3.7A.75.75 0 0 1 5.7 2.64l1.55 1.55V2A.75.75 0 0 1 8 1.25m0 9.5a.75.75 0 0 1 .75.75v.69a.75.75 0 0 1-1.5 0v-.69A.75.75 0 0 1 8 10.75m3.18.57a.75.75 0 0 1 1.03.27l.35.6a.75.75 0 1 1-1.3.75l-.35-.6a.75.75 0 0 1 .27-1.02m-6.36 0a.75.75 0 0 1 .27 1.02l-.35.6a.75.75 0 1 1-1.3-.75l.35-.6a.75.75 0 0 1 1.03-.27"></path>'
                    + '</svg>'
                    + '<span class="sr-only">' + escHtml(cooldownLabel) + '</span>'
                  + '</span>';
            }
            var reservationHtml = statusIcons
                ? '<span class="sidebar-page-status-icons">' + statusIcons + '</span>'
                : '';
            html += '<a class="sidebar-search-result" href="' + escHtml('/page/' + p.slug) + '">'
                  + '<span class="sidebar-search-result-type">page</span>'
                  + reservationHtml
                  + escHtml(p.title)
                  + '</a>';
        });
        results.innerHTML = html;
        results.hidden = false;
        results.addEventListener('click', function(e) {
            var link = e.target.closest('.js-scroll-to-category');
            if (!link) return;
            e.preventDefault();
            sidebarSearchScrollToCategory(Number(link.getAttribute('data-cat-id')));
        });
    }

    function escHtml(s) {
        return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');
    }
}());

function sidebarSearchScrollToCategory(catId) {
    var el = document.querySelector('.nav-section[data-cat-id="' + catId + '"]');
    if (!el) return;
    el.scrollIntoView({ behavior: 'smooth', block: 'start' });
    el.style.outline = '2px solid var(--primary)';
    setTimeout(function() { el.style.outline = ''; }, 1500);
    var results = document.getElementById('sidebar-search-results');
    if (results) results.hidden = true;
}
