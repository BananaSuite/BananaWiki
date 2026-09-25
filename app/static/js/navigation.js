// Collapse all sidebar categories by default; remember expanded ones

(function initCategoryCollapse() {
    var STORAGE_KEY = 'bw_expanded_cats';

    function getExpanded() {
        try {
            var raw = localStorage.getItem(STORAGE_KEY);
            return raw ? JSON.parse(raw) : [];
        } catch(e) { return []; }
    }

    function saveExpanded(ids) {
        try { localStorage.setItem(STORAGE_KEY, JSON.stringify(ids)); } catch(e) {}
    }

    document.addEventListener('DOMContentLoaded', function() {
        function getCategorySections() {
            return Array.prototype.slice.call(
                document.querySelectorAll('.nav-section[data-cat-id]')
            );
        }

        var expanded = getExpanded();
        getCategorySections().forEach(function(section) {
            var catId = section.dataset.catId;
            // If the category contains the currently active page, expand it
            var hasActive = section.querySelector('.nav-item.active');
            if (!hasActive && expanded.indexOf(catId) === -1) {
                section.classList.add('collapsed');
            }
        });

        // Scroll the sidebar so the active page is visible without the user
        // having to scroll manually after a page load or reload.
        var activeItem = document.querySelector('.sidebar-nav .nav-item.active');
        if (activeItem) {
            // Use requestAnimationFrame so the browser has finished layout
            requestAnimationFrame(function() {
                activeItem.scrollIntoView({ block: 'center', behavior: 'instant' });
            });
        }

        // Listen for toggle clicks and persist state
        document.addEventListener('click', function(e) {
            if (e.target.closest('#sidebar-expand-categories-btn')) {
                getCategorySections().forEach(function(section) {
                    section.classList.remove('collapsed');
                });
                persistState();
                return;
            }
            if (e.target.closest('#sidebar-collapse-categories-btn')) {
                getCategorySections().forEach(function(section) {
                    section.classList.add('collapsed');
                });
                persistState();
                return;
            }

            // Skip clicks on admin action buttons (reorder, settings)
            if (e.target.closest('.cat-actions')) return;

            var toggle = e.target.closest('.cat-toggle');
            if (toggle) {
                // The toggle button already toggles via onclick in HTML;
                // just persist state after the click
                setTimeout(persistState, 0);
                return;
            }

            // Allow clicking anywhere on the section header to toggle
            var header = e.target.closest('.nav-section-header');
            if (header) {
                var section = header.closest('.nav-section');
                if (section && section.dataset.catId) {
                    section.classList.toggle('collapsed');
                    persistState();
                }
            }
        });

        function persistState() {
            var ids = [];
            getCategorySections().forEach(function(s) {
                if (!s.classList.contains('collapsed')) {
                    ids.push(s.dataset.catId);
                }
            });
            saveExpanded(ids);
        }
    });
}());


// Sidebar reorder (up/down arrows for pages and categories)

(function initReorder() {
    function postReorder(url, ids) {
        return fetch(url, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-CSRFToken': getCsrfToken() },
            credentials: 'same-origin',
            body: JSON.stringify({ ids: ids }),
        }).then(function(r) {
            return bwParseJsonResponse(r, _t('js.order.save_failed'));
        });
    }

    function siblingsOf(el, selector) {
        return Array.from(el.parentNode.children).filter(function(c) {
            return c.matches(selector);
        });
    }

    function moveItem(item, direction, type) {
        var container = item.parentNode;
        if (container.dataset.reorderPending || container.querySelector('[data-sidebar-load][data-loading]')) return;
        var selector = type === 'page' ? '.nav-item-row' : '.nav-section[data-cat-id]';
        var siblings = siblingsOf(item, selector);
        var index = siblings.indexOf(item);
        var other = siblings[direction === 'up' ? index - 1 : index + 1];
        if (!other) return;
        var originalNext = item.nextSibling;
        container.dataset.reorderPending = '1';
        container.setAttribute('aria-busy', 'true');
        container.insertBefore(item, direction === 'up' ? other : other.nextSibling);
        var ordered = siblingsOf(item, selector);
        var ids = ordered.map(function(row) {
            return parseInt(type === 'page' ? row.dataset.pageId : row.dataset.catId, 10);
        });
        postReorder('/api/reorder/' + (type === 'page' ? 'pages' : 'categories'), ids)
            .then(function(data) {
                // The last loaded row may have moved. Continue paging after
                // the new boundary so the next batch neither repeats nor skips it.
                if (type === 'page') {
                    var more = container.querySelector(':scope > [data-sidebar-load]');
                    if (more) {
                        var api = new URL(more.dataset.sidebarLoad, window.location.href);
                        var fallback = new URL(more.href);
                        api.searchParams.set('after', ids[ids.length - 1]);
                        fallback.searchParams.set('after', ids[ids.length - 1]);
                        more.dataset.sidebarLoad = api.href;
                        more.href = fallback.href;
                    }
                }
                bwShowFlash(data.message || _t('js.order.' + type + '_saved'), 'success');
            })
            .catch(function(error) {
                container.insertBefore(item, originalNext);
                bwShowFlash(error.message || _t('js.order.' + type + '_save_failed'), 'error');
            })
            .finally(function() {
                delete container.dataset.reorderPending;
                container.removeAttribute('aria-busy');
            });
    }

    document.addEventListener('click', function(e) {
        var btn = e.target.closest('.reorder-btn');
        if (!btn) return;
        e.preventDefault();

        var dir = btn.dataset.dir;
        var type = btn.dataset.type;
        if (type === 'page') {
            var row = btn.closest('.nav-item-row');
            if (!row) return;
            var siblings = siblingsOf(row, '.nav-item-row');
            var idx = siblings.indexOf(row);
            var swapIdx = dir === 'up' ? idx - 1 : idx + 1;
            if (swapIdx < 0 || swapIdx >= siblings.length) return;
            var pageTitleEl = row.querySelector('.nav-item');
            var pageTitle = pageTitleEl ? pageTitleEl.textContent.trim() : 'this page';
            bwConfirm(_t('js.move.confirm_page', { title: pageTitle, dir: dir }), function() {
                moveItem(row, dir, 'page');
            });

        } else if (type === 'category') {
            var section = btn.closest('.nav-section');
            if (!section) return;
            var siblings = siblingsOf(section, '.nav-section[data-cat-id]');
            var idx = siblings.indexOf(section);
            var swapIdx = dir === 'up' ? idx - 1 : idx + 1;
            if (swapIdx < 0 || swapIdx >= siblings.length) return;
            var catNameEl = section.querySelector('.nav-section-title');
            var catName = catNameEl ? catNameEl.textContent.trim() : 'this category';
            bwConfirm(_t('js.move.confirm_category', { name: catName, dir: dir }), function() {
                moveItem(section, dir, 'category');
            });
        }
    });
}());
