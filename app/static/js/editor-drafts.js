// Autosave for editor
function initAutosave(pageId) {
    var titleEl = document.getElementById('edit-title');
    var contentEl = document.getElementById('edit-content');
    if (!titleEl || !contentEl) return;

    var saveTimer = null;
    var saving = false;
    var disabled = false;
    var hasShownSaveError = false;
    var saveController = null;

    function setIndicator(state) {
        var indicator = document.getElementById('save-indicator');
        if (!indicator) return;
        indicator.className = 'save-indicator save-indicator-' + state;
        if (state === 'syncing') {
            indicator.textContent = _t('js.editor.syncing');
        } else if (state === 'saved') {
            indicator.textContent = _t('js.editor.all_saved');
        } else if (state === 'error') {
            indicator.textContent = _t('js.editor.save_error');
        } else {
            indicator.textContent = '';
        }
    }

    var pendingCallback = null;

    function doSave(callback) {
        if (disabled) return;
        if (saving) {
            // If a save is already in progress, queue callback for when it finishes
            if (callback) pendingCallback = callback;
            return;
        }
        saving = true;
        setIndicator('syncing');
        if (saveController) saveController.abort();
        saveController = new AbortController();
        fetch('/api/draft/save', {
            method: 'POST',
            signal: saveController.signal,
            headers: { 'Content-Type': 'application/json', 'X-CSRFToken': getCsrfToken() },
            body: JSON.stringify({
                page_id: pageId,
                title: titleEl.value,
                content: contentEl.value
            })
        }).then(function(r) {
            return bwParseJsonResponse(r, _t('js.draft.save_failed'));
        }).then(function(d) {
            saving = false;
            hasShownSaveError = false;
            if (!disabled) setIndicator('saved');
            if (callback) callback(true);
            if (pendingCallback) { var cb = pendingCallback; pendingCallback = null; cb(true); }
        }).catch(function(err) {
            saving = false;
            if (err.name === 'AbortError') return;
            if (!disabled) setIndicator('error');
            if (!hasShownSaveError) {
                bwShowFlash(err.message || _t('js.draft.save_failed'), 'error');
                hasShownSaveError = true;
            }
            if (callback) callback(false);
            if (pendingCallback) { var cb = pendingCallback; pendingCallback = null; cb(false); }
        });
    }

    function scheduleSave() {
        if (disabled) return;
        if (saveTimer) clearTimeout(saveTimer);
        saveTimer = setTimeout(doSave, 1500);
    }

    function stopAutosave() {
        disabled = true;
        if (saveTimer) { clearTimeout(saveTimer); saveTimer = null; }
        setIndicator('');
    }

    // Save Draft & Close button
    var saveDraftCloseBtn = document.getElementById('save-draft-close');
    if (saveDraftCloseBtn) {
        saveDraftCloseBtn.addEventListener('click', function() {
            if (saveTimer) clearTimeout(saveTimer);
            doSave(function(ok) {
                if (ok) {
                    var redirectUrl = saveDraftCloseBtn.dataset.redirect || '/';
                    window.location.href = isSameOrigin(redirectUrl) ? redirectUrl : '/';
                } else {
                    bwShowFlash(_t('js.draft.save_failed'), 'error');
                }
            });
        });
    }

    titleEl.addEventListener('input', scheduleSave);
    contentEl.addEventListener('input', scheduleSave);

    // Stop autosave immediately when the edit form is submitted so the
    // draft-delete that happens server-side is not overridden by a
    // race-condition autosave that fires right before the redirect.
    // Wait for any in-flight autosave to finish first, then submit.
    // ``disabled`` is set *first* so that any keystroke the user makes
    // while the in-flight save is resolving cannot schedule a *new*
    // autosave that would re-create the draft after the server deletes it.
    var editForm = document.getElementById('editForm');
    if (editForm) {
        editForm.addEventListener('submit', function (e) {
            disabled = true;
            if (saveTimer) { clearTimeout(saveTimer); saveTimer = null; }
            if (saving) {
                e.preventDefault();
                pendingCallback = function () {
                    if (saveController) { saveController.abort(); saveController = null; }
                    editForm.submit();
                };
                return;
            }
            if (saveController) { saveController.abort(); saveController = null; }
        });
    }

    // Expose stop function for draft deletion
    window._bwStopAutosave = stopAutosave;

    // Check for other drafts and page staleness periodically
    var pageLoadedAt = new Date().toISOString();
    setInterval(function() {
        fetch('/api/draft/others/' + pageId)
            .then(function(r) {
                if (!r.ok) throw new Error('Failed to check drafts');
                return r.json();
            })
            .then(function(resp) {
                var drafts = resp.drafts || [];
                var notice = document.getElementById('other-drafts-notice');
                if (notice && drafts.length > 0) {
                    notice.innerHTML = '';
                    notice.appendChild(document.createTextNode(_t('js.editor.other_drafts_warning')));
                    drafts.forEach(function(d, idx) {
                        if (idx > 0) notice.appendChild(document.createTextNode(', '));
                        notice.appendChild(document.createTextNode(d.username));
                    });
                    if (notice.getAttribute('data-can-transfer') === '1') {
                        drafts.forEach(function(d) {
                            notice.appendChild(document.createTextNode(' '));
                            var btn = document.createElement('button');
                            btn.className = 'btn btn-sm';
                            btn.textContent = _t('js.editor.transfer_draft', { user: d.username });
                            btn.addEventListener('click', function() {
                                transferDraft(pageId, d.user_id);
                            });
                            notice.appendChild(btn);
                        });
                    }
                    notice.style.display = 'block';
                }
                // Stale draft detection: page was updated after we opened the editor
                var staleNotice = document.getElementById('stale-draft-notice');
                if (staleNotice && resp.page_last_edited_at) {
                    if (!staleNotice.dataset.shown && resp.page_last_edited_at > pageLoadedAt) {
                        staleNotice.style.display = 'block';
                        staleNotice.dataset.shown = '1';
                    }
                }
            })
            .catch(function() { /* silently ignore polling errors */ });
    }, 10000);
}

function transferDraft(pageId, fromUserId) {
    bwConfirm(_t('js.draft.transfer_confirm'), function() {
    fetch('/api/draft/transfer', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-CSRFToken': getCsrfToken() },
        body: JSON.stringify({ page_id: pageId, from_user_id: fromUserId })
    }).then(function(r) {
        return bwParseJsonResponse(r, _t('js.draft.transfer_failed'));
    }).then(function() {
        location.reload();
    }).catch(function(err) {
        bwShowFlash(err.message || _t('js.draft.transfer_failed'), 'error');
    });
    });
}

function deleteDraft(pageId) {
    // Stop autosave to prevent re-saving the draft
    if (window._bwStopAutosave) window._bwStopAutosave();
    fetch('/api/draft/delete', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-CSRFToken': getCsrfToken() },
        body: JSON.stringify({ page_id: pageId })
    }).then(function(r) {
        return bwParseJsonResponse(r, _t('js.draft.delete_failed'));
    }).then(function() {
        location.reload();
    }).catch(function(err) {
        bwShowFlash(err.message || _t('js.draft.delete_failed'), 'error');
    });
}

function discardDraftAndClose(pageId, redirectUrl) {
    bwConfirm(_t('js.draft.discard_unsaved_confirm'), function() {
    if (window._bwStopAutosave) window._bwStopAutosave();
    fetch('/api/draft/delete', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-CSRFToken': getCsrfToken() },
        body: JSON.stringify({ page_id: pageId })
    }).then(function(r) {
        return bwParseJsonResponse(r, _t('js.draft.discard_failed'));
    }).then(function() {
        window.location.href = isSameOrigin(redirectUrl) ? redirectUrl : '/';
    }).catch(function(err) {
        bwShowFlash(err.message || _t('js.draft.discard_failed'), 'error');
    });
    });
}

function initDraftManager() {
    var container = document.getElementById('draft-manager-list');
    if (!container) return;
    fetch('/api/draft/mine')
        .then(function(r) {
            if (!r.ok) throw new Error('Failed to load drafts');
            return r.json();
        })
        .then(function(drafts) {
            if (!drafts.length) {
                container.innerHTML = '<p class="u-o70 u-fs-09">No pending drafts.</p>';
                return;
            }
            var html = '<table class="draft-manager-table"><thead><tr><th>Page</th><th>Last saved</th><th>Actions</th></tr></thead><tbody>';
            drafts.forEach(function(d) {
                var editUrl = '/page/' + d.page_slug + '/edit';
                html += '<tr data-page-id="' + d.page_id + '">'
                    + '<td>' + escapeHtml(d.page_title) + '</td>'
                    + '<td class="u-ws-nowrap u-fs-085 u-o70">' + escapeHtml(d.updated_at_formatted || d.updated_at) + '</td>'
                    + '<td class="u-ws-nowrap">'
                    + '<a href="' + editUrl + '" class="btn btn-sm u-mr04">Continue editing</a>'
                    + '<button class="btn btn-sm btn-outline btn-danger-outline js-discard-draft" data-page-id="' + Number(d.page_id) + '">Discard</button>'
                    + '</td>'
                    + '</tr>';
            });
            html += '</tbody></table>';
            container.innerHTML = html;
            container.addEventListener('click', function(e) {
                var btn = e.target.closest('.js-discard-draft');
                if (!btn) return;
                var pageId = Number(btn.getAttribute('data-page-id'));
                discardDraftFromSettings(pageId, btn);
            });
        })
        .catch(function() {
            container.innerHTML = '<p class="u-o70 u-fs-09">Could not load drafts.</p>';
        });
}

function discardDraftFromSettings(pageId, btn) {
    bwConfirm(_t('js.draft.discard_undone_confirm'), function() {
    btn.disabled = true;
    fetch('/api/draft/delete', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-CSRFToken': getCsrfToken() },
        body: JSON.stringify({ page_id: pageId })
    }).then(function(r) {
        return bwParseJsonResponse(r, _t('js.draft.discard_retry'));
    }).then(function() {
        var row = btn.closest('tr');
        if (row) row.remove();
        var tbody = document.querySelector('#draft-manager-list tbody');
        if (tbody && tbody.querySelectorAll('tr').length === 0) {
            document.getElementById('draft-manager-list').innerHTML =
                '<p class="u-o70 u-fs-09">No pending drafts.</p>';
        }
        bwShowFlash(_t('js.draft.deleted_success'), 'success');
    }).catch(function(err) {
        btn.disabled = false;
        bwShowFlash(err.message || _t('js.draft.discard_retry'), 'error');
    });
    });
}

function escapeHtml(str) {
    return String(str)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}
