var bwCategoryModalRequests = new Set();

function openCatModal(id, url) {
    var modal = document.getElementById(id);
    if (!modal && url) {
        if (bwCategoryModalRequests.has(id)) return;
        bwCategoryModalRequests.add(id);
        fetch(url, { credentials: 'same-origin' })
            .then(function(response) { return bwParseJsonResponse(response, _t('js.nav.load_failed')); })
            .then(function(data) {
                var holder = document.createElement('div');
                holder.innerHTML = data.html;
                var loaded = holder.firstElementChild;
                if (!loaded || loaded.id !== id) throw new Error(_t('js.nav.load_failed'));
                document.body.appendChild(loaded);
                openCatModal(id);
            })
            .catch(function(error) { bwShowFlash(error.message || _t('js.nav.load_failed'), 'error'); })
            .finally(function() { bwCategoryModalRequests.delete(id); });
        return;
    }
    if (!modal) return;
    if (!modal._bwOrigParent) modal._bwOrigParent = modal.parentNode;
    document.body.appendChild(modal);
    modal.style.display = 'flex';
}

// Close category manage modal and return it to its original location
function closeCatModal(btn) {
    var modal = btn.closest('.modal');
    if (!modal) return;
    modal.style.display = 'none';
    if (modal._bwOrigParent) modal._bwOrigParent.appendChild(modal);
}

// Category delete confirmation
function confirmCatDelete(form, pageCount, catName) {
    var action = form.querySelector('.cat-page-action').value;
    var msg = window._t ? window._t('js.category.delete_confirm', { name: catName }) : ('Delete category "' + catName + '"?');
    if (pageCount > 0) {
        if (action === 'delete') {
            msg += '\n\nThis will PERMANENTLY DELETE ' + pageCount + ' page(s) in this category!';
        } else if (action === 'move') {
            var sel = form.querySelector('.cat-move-target');
            var targetName = sel && sel.value ? sel.options[sel.selectedIndex].text : '';
            if (!sel || !sel.value) {
                bwShowFlash(_t('js.move.select_target'), 'error');
                return false;
            }
            msg += '\n\n' + pageCount + ' page(s) will be moved to "' + targetName + '".';
        } else {
            msg += '\n\n' + pageCount + ' page(s) will become uncategorized.';
        }
    }
    bwConfirm(msg, function() { form.submit(); });
    return false;
}


//  CSP-compliant category / folder event delegation

(function() {
    document.addEventListener('click', function(e) {
        // Open create-category modal
        if (e.target.closest('#open-create-cat-btn')) {
            var m = document.getElementById('createCatModal');
            if (m) m.style.display = 'flex';
            return;
        }
        // Toggle category collapsed state
        var toggle = e.target.closest('.cat-toggle');
        if (toggle) {
            var sec = toggle.closest('.nav-section');
            if (sec) sec.classList.toggle('collapsed');
            return;
        }
        // Open category manage modal
        var manage = e.target.closest('[data-open-cat-modal]');
        if (manage) {
            openCatModal(manage.dataset.openCatModal, manage.dataset.modalUrl);
            return;
        }
        // Close category manage modal
        if (e.target.closest('[data-close-cat-modal]')) {
            closeCatModal(e.target.closest('[data-close-cat-modal]'));
            return;
        }
        // Generic close-modal
        if (e.target.closest('[data-close-modal]')) {
            var modal = e.target.closest('.modal');
            if (modal) modal.style.display = 'none';
            return;
        }
    });

    // Category delete form confirmation (replaces onsubmit handler)
    document.addEventListener('submit', function(e) {
        var form = e.target.closest('[data-cat-delete-confirm]');
        if (form) {
            e.preventDefault();
            confirmCatDelete(form, parseInt(form.dataset.catPageCount || '0', 10), form.dataset.catName || '');
        }
    });

    // Category management change handlers
    document.addEventListener('change', function(e) {
        // Sequential nav checkbox
        if (e.target.classList.contains('seq-nav-checkbox')) {
            var hidden = e.target.form.querySelector('input[type=hidden][name=sequential_nav]');
            if (hidden) hidden.disabled = e.target.checked;
            return;
        }
        // Category page-action radio buttons
        if (e.target.name === 'page_action_ui') {
            var f = e.target.closest('form');
            if (!f) return;
            var pa = f.querySelector('.cat-page-action');
            if (pa) pa.value = e.target.value;
            var mt = f.querySelector('.cat-move-target');
            if (mt) mt.style.display = (e.target.value === 'move') ? 'block' : 'none';
            return;
        }
        // Category move-target select
        if (e.target.classList.contains('cat-move-target')) {
            var tf = e.target.closest('form');
            if (tf) {
                var ti = tf.querySelector('.cat-target-id');
                if (ti) ti.value = e.target.value;
            }
        }
    });
})();
