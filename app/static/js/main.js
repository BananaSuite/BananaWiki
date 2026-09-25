/* BananaWiki: Main JS */

var FLASH_HIDE_DURATION_MS = 180;
var LIVE_CHAT_SCROLL_STICK_THRESHOLD = 72;

// CSRF token helper
function getCsrfToken() {
    var meta = document.querySelector('meta[name="csrf-token"]');
    return meta ? meta.getAttribute('content') : '';
}

// Same-origin URL validation (prevents open redirects)
function isSameOrigin(url) {
    try { return new URL(url, location.origin).origin === location.origin; }
    catch (e) { return false; }
}

document.addEventListener('click', function(event) {
    document.querySelectorAll('details[data-action-menu][open]').forEach(function(menu) {
        if (!menu.contains(event.target) || event.target.closest('button, a')) {
            menu.open = false;
        }
    });
});

document.addEventListener('keydown', function(event) {
    if (event.key !== 'Escape') return;
    document.querySelectorAll('details[data-action-menu][open]').forEach(function(menu) {
        menu.open = false;
        menu.querySelector('summary').focus();
    });
});

// In-site confirmation dialog
var _bwConfirmCallback = null;

function bwCloseConfirm(modal) {
    if (!modal) return;
    modal.style.display = 'none';
    _bwConfirmCallback = null;
}

function bwResolveConfirmLabel(source, fallback) {
    var defaultLabel = fallback || (window._t ? window._t('js.common.confirm') : 'Confirm');
    if (!source || typeof source.getAttribute !== 'function') return defaultLabel;
    var explicit = source.getAttribute('data-confirm-label');
    if (explicit) return explicit.trim();
    if (source.tagName && source.tagName.toLowerCase() === 'form') return defaultLabel;
    var label = '';
    if (source.tagName && source.tagName.toLowerCase() === 'input') {
        label = source.value || '';
    } else {
        label = source.textContent || '';
    }
    label = label.replace(/\s+/g, ' ').trim();
    return /[A-Za-z0-9]/.test(label) ? label : defaultLabel;
}

function bwConfirm(message, callback, options) {
    var modal = document.getElementById('bw-confirm-modal');
    var msgEl = document.getElementById('bw-confirm-message');
    if (!modal || !msgEl) {
        if (callback && window.confirm(message)) callback();
        return;
    }
    msgEl.textContent = message;
    _bwConfirmCallback = callback || null;
    var okBtn = document.getElementById('bw-confirm-ok');
    var cancelBtn = document.getElementById('bw-confirm-cancel');
    if (okBtn) {
        okBtn.textContent = bwResolveConfirmLabel(options && options.labelSource, (options && options.confirmLabel) || (window._t ? window._t('js.common.confirm') : 'Confirm'));
    }
    modal.style.display = 'flex';
    if (cancelBtn) cancelBtn.focus();
}

function initConfirmModal() {
    var modal = document.getElementById('bw-confirm-modal');
    if (!modal) return;
    var okBtn = document.getElementById('bw-confirm-ok');
    var cancelBtn = document.getElementById('bw-confirm-cancel');
    if (okBtn) {
        okBtn.addEventListener('click', function() {
            var cb = _bwConfirmCallback;
            bwCloseConfirm(modal);
            if (cb) cb();
        });
    }
    if (cancelBtn) {
        cancelBtn.addEventListener('click', function() {
            bwCloseConfirm(modal);
        });
    }
    modal.addEventListener('click', function(e) {
        if (e.target === modal) {
            bwCloseConfirm(modal);
        }
    });
    document.addEventListener('keydown', function(e) {
        if (e.key === 'Escape' && modal.style.display !== 'none') {
            bwCloseConfirm(modal);
        }
    });
    // Intercept submits on forms with data-confirm attribute
    document.addEventListener('submit', function(e) {
        var form = e.target;
        if (!form || typeof form.getAttribute !== 'function') return;
        if (form.dataset && form.dataset.bwConfirmBypass === '1') {
            delete form.dataset.bwConfirmBypass;
            return;
        }
        var submitter = e.submitter || null;
        var msg = submitter && submitter.getAttribute('data-confirm');
        if (!msg) msg = form.getAttribute('data-confirm');
        if (!msg) return;
        e.preventDefault();
        bwConfirm(msg, function() {
            if (form.dataset) form.dataset.bwConfirmBypass = '1';
            if (submitter && typeof form.requestSubmit === 'function') {
                try {
                    form.requestSubmit(submitter);
                    return;
                } catch (err) {
                    // Fall back to form.submit() if requestSubmit fails (e.g. if the
                    // submitter button was detached during a live chat re-render).
                    if (typeof console !== 'undefined') {
                        console.warn('form.requestSubmit failed, falling back to form.submit():', err);
                    }
                }
            }
            form.submit();
        }, {
            confirmLabel: bwResolveConfirmLabel(submitter || form, window._t ? window._t('js.common.confirm') : 'Confirm'),
            labelSource: submitter || form
        });
    }, true);
}

// Flash message close buttons (no auto-dismiss)
function bindFlashMessage(el) {
    if (!el) return;
    var closeBtn = el.querySelector('.flash-close');
    if (closeBtn) {
        closeBtn.addEventListener('click', function() {
            el.classList.add('flash-hiding');
            window.setTimeout(function() { el.remove(); }, FLASH_HIDE_DURATION_MS);
        });
    }
}

function bwShowFlash(message, category) {
    if (!message) return null;
    var mainContent = document.getElementById('main-content') || document.querySelector('main.content') || document.body;
    var container = mainContent.querySelector('.flash-messages');
    if (!container) {
        container = document.createElement('div');
        container.className = 'flash-messages';
        if (mainContent.firstChild) {
            mainContent.insertBefore(container, mainContent.firstChild);
        } else {
            mainContent.appendChild(container);
        }
    }
    // Remove matching JS-injected flash messages so repeated actions (for
    // example, moving a page several times via the sidebar arrows) replace the
    // prior confirmation instead of stacking duplicates.
    var flashCategory = category || 'info';
    var flashMessage = String(message);
    var cls = 'flash-' + flashCategory;
    container.querySelectorAll('.flash.' + cls + '[data-js-flash]').forEach(function(el) {
        var text = el.querySelector('.flash-message');
        if (text && text.textContent === flashMessage) {
            el.remove();
        }
    });
    var flash = document.createElement('div');
    flash.className = 'flash flash-' + flashCategory;
    flash.setAttribute('role', 'alert');
    flash.setAttribute('data-js-flash', '1');
    flash.innerHTML = '<span class="flash-message"></span><button type="button" class="flash-close" aria-label="' + (window._t ? window._t('js.common.dismiss_message') : 'Dismiss message') + '">x</button>';
    flash.querySelector('.flash-message').textContent = flashMessage;
    container.appendChild(flash);
    bindFlashMessage(flash);
    return flash;
}

function bwParseJsonResponse(response, fallbackError) {
    return response.json().catch(function() {
        return {};
    }).then(function(data) {
        if (!response.ok) {
            throw new Error((data && data.error) || fallbackError || 'Request failed.');
        }
        return data || {};
    });
}

function initFlashMessages() {
    document.querySelectorAll('.flash').forEach(function(el) {
        bindFlashMessage(el);
    });
}

function initDismissibleBanners() {
    document.querySelectorAll('.js-dismissible-banner').forEach(function(banner) {
        var dismissKey = banner.getAttribute('data-dismiss-key');
        try {
            if (dismissKey && window.localStorage.getItem(dismissKey) === '1') {
                banner.remove();
                return;
            }
        } catch (err) {}
        var closeBtn = banner.querySelector('.chat-status-dismiss');
        if (!closeBtn) return;
        closeBtn.addEventListener('click', function() {
            try {
                if (dismissKey) window.localStorage.setItem(dismissKey, '1');
            } catch (err) {}
            banner.remove();
        });
    });
}

function initLiveChat() {
    var container = document.querySelector('[data-chat-poll-url]');
    if (!container) return;
    var pollUrl = container.getAttribute('data-chat-poll-url');
    if (!pollUrl) return;
    var pollIntervalMs = parseInt(container.getAttribute('data-chat-poll-interval') || '2500', 10);
    var latestMessageId = parseInt(container.getAttribute('data-latest-message-id') || '0', 10);
    var messageCount = parseInt(container.getAttribute('data-message-count') || '0', 10);
    var stateToken = container.getAttribute('data-chat-state-token') || '';
    var stopped = false;

    function scrollToBottom() {
        container.scrollTop = container.scrollHeight;
    }

    function shouldStickToBottom() {
        return (container.scrollHeight - container.scrollTop - container.clientHeight) < LIVE_CHAT_SCROLL_STICK_THRESHOLD;
    }

    function pollMessages() {
        if (stopped || document.hidden) return;
        var shouldAutoScroll = shouldStickToBottom();
        fetch(pollUrl, {
            headers: {
                'X-Requested-With': 'XMLHttpRequest'
            }
        }).then(function(resp) {
            if (resp.status === 403 || resp.status === 404) {
                stopped = true;
            }
            if (!resp.ok) throw new Error('Live chat refresh failed');
            return resp.json();
        }).then(function(data) {
            var nextLatestMessageId = parseInt(data.latest_message_id || '0', 10);
            var nextMessageCount = parseInt(data.message_count || '0', 10);
            var nextStateToken = data.state_token || '';
            var hadNewContent = nextLatestMessageId !== latestMessageId
                || nextMessageCount !== messageCount
                || nextStateToken !== stateToken;
            if (!hadNewContent) return;
            latestMessageId = nextLatestMessageId;
            messageCount = nextMessageCount;
            stateToken = nextStateToken;
            container.innerHTML = data.html || '';
            container.setAttribute('data-latest-message-id', String(nextLatestMessageId));
            container.setAttribute('data-message-count', String(nextMessageCount));
            container.setAttribute('data-chat-state-token', nextStateToken);
            if (hadNewContent && shouldAutoScroll) {
                scrollToBottom();
            }
        }).catch(function(err) {
            // Network error: the interval will retry automatically.
            if (typeof console !== 'undefined') { console.warn('Chat poll failed, will retry:', err); }
        });
    }

    scrollToBottom();
    var pollHandle = window.setInterval(pollMessages, pollIntervalMs);
    window.addEventListener('beforeunload', function() {
        window.clearInterval(pollHandle);
        stopped = true;
    }, { once: true });
}

// Page title scroll animation: shows page title in topbar when scrolling
function initPageTitleScroll() {
    var contentEl = document.querySelector('.content');
    var pageH1 = document.querySelector('.page-header h1');
    var topbarTitle = document.getElementById('topbar-page-title');
    if (!contentEl || !pageH1 || !topbarTitle) return;
    topbarTitle.textContent = pageH1.textContent;
    var topbarThreshold = 50; // approx topbar height in px
    var ticking = false;
    function onScroll() {
        if (!ticking) {
            requestAnimationFrame(function() {
                var rect = pageH1.getBoundingClientRect();
                if (rect.bottom < topbarThreshold) {
                    topbarTitle.classList.add('visible');
                } else {
                    topbarTitle.classList.remove('visible');
                }
                ticking = false;
            });
            ticking = true;
        }
    }
    // Desktop: content element scrolls independently
    contentEl.addEventListener('scroll', onScroll);
    // Mobile: body/window scrolls (content has overflow-y:visible on small screens)
    window.addEventListener('scroll', onScroll);
}

function initMobileAccountMenu() {
    var toggleBtn = document.getElementById('mobile-account-menu-toggle');
    var menu = document.getElementById('mobile-account-menu');
    if (!toggleBtn || !menu) return;

    function setOpen(open) {
        menu.hidden = !open;
        toggleBtn.setAttribute('aria-expanded', String(open));
    }

    toggleBtn.addEventListener('click', function(e) {
        e.stopPropagation();
        setOpen(menu.hidden);
    });

    menu.addEventListener('click', function(e) {
        e.stopPropagation();
    });

    document.addEventListener('click', function() {
        setOpen(false);
    });

    document.addEventListener('keydown', function(e) {
        if (e.key === 'Escape') {
            setOpen(false);
        }
    });
}

// Move modals from inside .layout to <body> so they are never trapped in
// a stacking context created by accessibility contrast filters (CSS filter
// on .layout creates a new containing block for position:fixed children,
// which clips modals and drops them behind the topbar).
function _bwHoistModals() {
    var layout = document.querySelector('.layout');
    if (!layout) return;
    var modals = layout.querySelectorAll('.modal:not(.cat-modal-src), .kanban-modal-overlay');
    for (var i = 0; i < modals.length; i++) {
        document.body.appendChild(modals[i]);
    }
}

// Delegated handler for [data-bw-action] buttons. Inline onclick="" handlers
// are blocked by the strict CSP (script-src has no 'unsafe-inline'), so any
// element that needs to fire a navigation/history action declares its intent
// via a data attribute and this listener dispatches it.
document.addEventListener('click', function(e) {
    var target = e.target.closest('[data-bw-action]');
    if (!target) return;
    var action = target.getAttribute('data-bw-action');
    if (action === 'history-back') {
        e.preventDefault();
        if (window.history.length > 1) {
            window.history.back();
        } else {
            window.location.href = '/';
        }
    }
});


//  Prevent stale pages from bfcache and double form submissions

window.addEventListener('pageshow', function(e) {
    if (e.persisted) {
        // Re-enable any buttons disabled during form submission
        document.querySelectorAll('button[data-bw-submitting]').forEach(function(btn) {
            btn.disabled = false;
            btn.removeAttribute('data-bw-submitting');
        });
        // Clear any in-flight click-debounce markers left over from the
        // previous page navigation so buttons are not stuck on bfcache
        // restore.
        document.querySelectorAll('[data-bw-click-locked]').forEach(function(el) {
            el.removeAttribute('data-bw-click-locked');
        });
    }
});

// Prevent double form submissions: disable the submit button after click.
// IMPORTANT: only disable the button when the form is actually being submitted.
// If a capture-phase listener (e.g. the data-confirm modal interceptor at the
// top of this file) called e.preventDefault() the form is NOT submitting on
// this event: disabling the button would leave it stuck if the user
// dismisses the confirm modal, which historically looked like the page had
// "hung" until the user reloaded.
document.addEventListener('submit', function(e) {
    if (e.defaultPrevented) return;
    var form = e.target;
    if (!form || form.dataset.bwNoDoubleSubmit === 'false') return;
    // Don't apply to API/fetch forms (those handle their own state)
    if (form.getAttribute('data-bw-fetch')) return;
    var btn = form.querySelector('button[type="submit"], input[type="submit"]');
    if (btn && !btn.disabled) {
        btn.setAttribute('data-bw-submitting', '1');
        setTimeout(function() {
            btn.disabled = true;
        }, 0);
    }
}, false);


//  Click debounce: prevent duplicate actions from rapid double-clicks

// Some buttons trigger AJAX/fetch flows (creating a chat message, adding a
// kanban ticket, voting on a contribution, …) rather than a form submit, so
// the submit-button protection above doesn't catch a user who rapidly
// double-clicks them.  Without this guard the second click fires before the
// first request has returned and creates a duplicate row on the server.
//
// This delegated capture-phase handler runs *before* any inner click
// listener.  When a click lands on a real action target (a <button>, an
// <input type="submit"/image"> or an element with a [data-bw-action] /
// [role="button"] hint) we tag it with [data-bw-click-locked] for a short
// cooldown: subsequent clicks during that window are intercepted before
// they reach the page-specific handler.
//
// Opt out per element with [data-bw-no-debounce="1"] (useful for rapid-fire
// controls like +/- counters, kanban autoscroll arrows, etc.).  Tune the
// cooldown per element with [data-bw-debounce-ms="N"] when needed.
var BW_CLICK_DEBOUNCE_DEFAULT_MS = 600;

function _bwIsActionTarget(el) {
    if (!el || el.nodeType !== 1) return false;
    if (el.hasAttribute('data-bw-no-debounce')) return false;
    var tag = el.tagName ? el.tagName.toLowerCase() : '';
    if (tag === 'button') return true;
    if (tag === 'input') {
        var type = (el.getAttribute('type') || '').toLowerCase();
        if (type === 'submit' || type === 'image' || type === 'button' || type === 'reset') return true;
        return false;
    }
    if (el.hasAttribute('data-bw-action')) return true;
    if (el.getAttribute('role') === 'button') return true;
    return false;
}

function _bwResolveDebounceMs(el) {
    var raw = el.getAttribute('data-bw-debounce-ms');
    if (!raw) return BW_CLICK_DEBOUNCE_DEFAULT_MS;
    var n = parseInt(raw, 10);
    if (!isFinite(n) || n < 0) return BW_CLICK_DEBOUNCE_DEFAULT_MS;
    return n;
}

document.addEventListener('click', function(e) {
    // Only respond to genuine user-initiated clicks, never to clicks
    // synthesised by other JS (e.g. modals firing the OK button when the
    // user hits Enter, code calling btn.click() to replay submission).
    if (!e.isTrusted) return;
    var target = e.target && e.target.closest && e.target.closest(
        'button, input[type="submit"], input[type="image"], ' +
        'input[type="button"], input[type="reset"], ' +
        '[data-bw-action], [role="button"]'
    );
    if (!_bwIsActionTarget(target)) return;
    if (target.disabled || target.getAttribute('aria-disabled') === 'true') return;
    if (target.hasAttribute('data-bw-click-locked')) {
        // Swallow the rapid second click before any other listener (form
        // submit, fetch handler, etc.) sees it.  ``stopImmediatePropagation``
        // also blocks listeners attached at the same target with
        // ``addEventListener`` so the click is fully neutralised.
        e.preventDefault();
        e.stopImmediatePropagation();
        e.stopPropagation();
        return;
    }
    var cooldown = _bwResolveDebounceMs(target);
    if (cooldown <= 0) return;
    target.setAttribute('data-bw-click-locked', '1');
    setTimeout(function() {
        target.removeAttribute('data-bw-click-locked');
    }, cooldown);
}, true);

document.addEventListener('DOMContentLoaded', function() {
    _bwHoistModals();
    initFlashMessages();
    initDismissibleBanners();
    initLiveChat();
    initPageTitleScroll();
    initMobileAccountMenu();
    initConfirmModal();

    // Sidebar toggle for mobile
    var toggleBtn = document.getElementById('sidebar-toggle');
    var sidebar = document.getElementById('sidebar');
    var overlay = document.getElementById('sidebar-overlay');
    var collapseBtn = document.getElementById('sidebar-collapse-btn');

    function updateTopbarHeightVar() {
        var topbar = document.querySelector('.topbar');
        if (!topbar) return;
        var height = Math.ceil(topbar.getBoundingClientRect().height);
        if (height > 0) {
            document.documentElement.style.setProperty('--bw-topbar-height', height + 'px');
        }
    }

    function updateBodyScrollOnMobileSidebar() {
        if (!isDesktopSidebarLayout() && sidebar.classList.contains('open')) {
            document.body.classList.add('sidebar-open');
        } else {
            document.body.classList.remove('sidebar-open');
        }
    }

    updateTopbarHeightVar();
    window.addEventListener('resize', updateTopbarHeightVar);
    window.addEventListener('orientationchange', function() {
        setTimeout(updateTopbarHeightVar, 100);
    });

    // Helper to get the first focusable element inside a container
    function getFirstFocusableElement(container) {
        if (!container) return null;
        return container.querySelector(
            'a[href], button:not([disabled]), textarea:not([disabled]), ' +
            'input:not([disabled]), select:not([disabled]), ' +
            '[tabindex]:not([tabindex="-1"])'
        );
    }

    var lastFocusedElementBeforeSidebar = null;

    function isDesktopSidebarLayout() {
        return window.innerWidth > 768;
    }

    function setSidebarCollapsed(collapsed) {
        document.body.classList.toggle('sidebar-collapsed', collapsed);
        if (collapseBtn) {
            collapseBtn.setAttribute('aria-expanded', String(!collapsed));
            // Swap the pill's label/title so screen readers announce the
            // current action.  The pill stays visible in both states on
            // desktop, repositioned to the left edge when collapsed, and
            // its chevron flips to point right via CSS.
            var label = collapsed
                ? (collapseBtn.dataset.labelExpand || collapseBtn.title)
                : (collapseBtn.dataset.labelCollapse || collapseBtn.title);
            if (label) {
                collapseBtn.setAttribute('aria-label', label);
                collapseBtn.setAttribute('title', label);
            }
        }
        try {
            if (collapsed) {
                localStorage.setItem('bw-sidebar-collapsed', '1');
            } else {
                localStorage.removeItem('bw-sidebar-collapsed');
            }
        } catch (e) { /* ignore storage errors (private mode, etc.) */ }
    }

    if (collapseBtn && sidebar) {
        collapseBtn.addEventListener('click', function() {
            if (!isDesktopSidebarLayout()) {
                // On mobile, the collapse button is hidden via CSS; if it
                // somehow still gets clicked, toggle the drawer.
                sidebar.classList.toggle('open');
                var isOpen = sidebar.classList.contains('open');
                if (overlay) overlay.classList.toggle('active', isOpen);
                if (toggleBtn) toggleBtn.setAttribute('aria-expanded', String(isOpen));
                updateBodyScrollOnMobileSidebar();
                return;
            }
            // Toggle in both directions.  When collapsed the pill is
            // repurposed as the "expand sidebar" affordance at the left
            // edge of the viewport (same vertical centre as the collapse
            // position), so the same button drives both transitions.
            var alreadyCollapsed =
                document.body.classList.contains('sidebar-collapsed');
            setSidebarCollapsed(!alreadyCollapsed);
            // Keep focus on the button. It stays visible after both
            // collapsing and expanding, so keyboard users don't lose
            // their place.
            if (typeof collapseBtn.focus === 'function') {
                collapseBtn.focus();
            }
        });

        // The early inline script in base.html may have already added
        // ``body.sidebar-collapsed`` from a persisted localStorage flag
        // (so the sidebar doesn't flash open on first paint).  Sync the
        // pill's aria-label / title with that initial state so screen
        // readers announce the correct action without waiting for the
        // first user interaction.
        if (document.body.classList.contains('sidebar-collapsed')) {
            setSidebarCollapsed(true);
        }
    }

    if (toggleBtn && sidebar) {
        toggleBtn.addEventListener('click', function() {
            // Desktop: topbar toggle expands a previously collapsed sidebar.
            if (isDesktopSidebarLayout() && document.body.classList.contains('sidebar-collapsed')) {
                setSidebarCollapsed(false);
                if (collapseBtn && typeof collapseBtn.focus === 'function') {
                    collapseBtn.focus();
                }
                return;
            }

            // Remember what was focused before toggling
            if (!sidebar.classList.contains('open')) {
                lastFocusedElementBeforeSidebar = document.activeElement;
            }

            sidebar.classList.toggle('open');
            var isOpen = sidebar.classList.contains('open');
            toggleBtn.setAttribute('aria-expanded', String(isOpen));
            updateBodyScrollOnMobileSidebar();

            if (overlay) {
                overlay.classList.toggle('active', isOpen);
            }

            if (isOpen) {
                // Move focus into the sidebar
                var focusTarget = getFirstFocusableElement(sidebar) || sidebar;
                if (focusTarget === sidebar && !focusTarget.hasAttribute('tabindex')) {
                    focusTarget.setAttribute('tabindex', '-1');
                }
                if (typeof focusTarget.focus === 'function') {
                    focusTarget.focus();
                }
            } else {
                // Restore focus to the element that opened the sidebar
                var toFocus = lastFocusedElementBeforeSidebar || toggleBtn;
                if (toFocus && typeof toFocus.focus === 'function') {
                    toFocus.focus();
                }
            }
        });
        if (overlay) {
            overlay.addEventListener('click', function() {
                // Close sidebar and overlay
                sidebar.classList.remove('open');
                overlay.classList.remove('active');
                toggleBtn.setAttribute('aria-expanded', 'false');
                updateBodyScrollOnMobileSidebar();
                // Return focus to the toggle button
                if (toggleBtn && typeof toggleBtn.focus === 'function') {
                    toggleBtn.focus();
                }
            });
        }

        // On mobile, auto-close the drawer when the user navigates via a
        // sidebar link.  Without this the drawer stays open over the new
        // page after navigation and forces the user to dismiss it manually
        // every time.
        sidebar.addEventListener('click', function(e) {
            if (window.innerWidth > 768) return;
            if (!sidebar.classList.contains('open')) return;
            var link = e.target.closest && e.target.closest('a[href]');
            if (!link) return;
            // Ignore clicks on action buttons rendered as <a> that don't
            // actually navigate (e.g. category toggles, "Manage" links that
            // open modals).  Modal-opening links carry data-modal attributes.
            if (link.hasAttribute('data-modal') || link.hasAttribute('data-sidebar-load')) return;
            sidebar.classList.remove('open');
            if (overlay) overlay.classList.remove('active');
            toggleBtn.setAttribute('aria-expanded', 'false');
            updateBodyScrollOnMobileSidebar();
        });
    }

    // Sidebar resize handle
    var resizeHandle = document.getElementById('sidebar-resize-handle');
    if (resizeHandle && sidebar) {
        var isResizing = false;
        resizeHandle.addEventListener('mousedown', function(e) {
            // The collapse button lives inside the resize handle so it can
            // sit at the handle's vertical centre.  A mousedown on the
            // button must not start a drag-resize, otherwise clicking the
            // button silently resizes the sidebar by a few pixels and the
            // click never registers.
            if (e.target && typeof e.target.closest === 'function'
                && e.target.closest('.sidebar-collapse-btn')) {
                return;
            }
            isResizing = true;
            resizeHandle.classList.add('resizing');
            document.body.style.cursor = 'col-resize';
            document.body.style.userSelect = 'none';
            e.preventDefault();
        });
        document.addEventListener('mousemove', function(e) {
            if (!isResizing) return;
            var newWidth = e.clientX;
            if (newWidth >= 180 && newWidth <= 500) {
                sidebar.style.width = newWidth + 'px';
                sidebar.style.minWidth = newWidth + 'px';
            }
        });
        document.addEventListener('mouseup', function() {
            if (isResizing) {
                isResizing = false;
                resizeHandle.classList.remove('resizing');
                document.body.style.cursor = '';
                document.body.style.userSelect = '';
                // Save the new sidebar width as an accessibility preference
                var w = parseInt(sidebar.style.width, 10);
                if (w >= 180 && w <= 500) {
                    saveA11ySetting('sidebar_width', w);
                }
            }
        });
    }

    // Content resize handle
    var contentResizeHandle = document.getElementById('content-resize-handle');
    var mainContent = document.getElementById('main-content');
    if (contentResizeHandle && mainContent) {
        var isContentResizing = false;
        contentResizeHandle.addEventListener('mousedown', function(e) {
            isContentResizing = true;
            contentResizeHandle.classList.add('resizing');
            document.body.style.cursor = 'col-resize';
            document.body.style.userSelect = 'none';
            e.preventDefault();
        });
        document.addEventListener('mousemove', function(e) {
            if (!isContentResizing) return;
            var layoutLeft = mainContent.getBoundingClientRect().left;
            var newWidth = e.clientX - layoutLeft;
            var minWidth = 400;
            var maxWidth = window.innerWidth - layoutLeft - 8;
            if (newWidth >= minWidth && newWidth <= maxWidth) {
                document.documentElement.style.setProperty('--content-max-width', newWidth + 'px');
            }
        });
        document.addEventListener('mouseup', function() {
            if (isContentResizing) {
                isContentResizing = false;
                contentResizeHandle.classList.remove('resizing');
                document.body.style.cursor = '';
                document.body.style.userSelect = '';
                var w = parseInt(getComputedStyle(document.documentElement).getPropertyValue('--content-max-width'), 10);
                if (w >= 400) {
                    saveA11ySetting('content_max_width', w);
                }
            }
        });
    }
});
