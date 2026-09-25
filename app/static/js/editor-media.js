// Image upload via drag & drop or file input
function initImageUpload(contentEl) {
    if (!contentEl) return;

    var dropZone = document.getElementById('drop-zone');

    function uploadFile(file) {
        var fd = new FormData();
        fd.append('file', file);
        fd.append('csrf_token', getCsrfToken());
        return fetch('/api/upload', { method: 'POST', body: fd })
            .then(function(r) { return r.json().then(function(data) { return { ok: r.ok, data: data }; }); })
            .then(function(result) {
                if (!result.ok || result.data.error) {
                    bwShowFlash(_t('js.upload.failed_prefix') + ' ' + (result.data.error || _t('js.common.please_try_again')), 'error');
                    return result.data;
                }
                if (result.data.url) {
                    openImageOptionsModal(result.data.url, file.name, contentEl);
                }
                return result.data;
            })
            .catch(function(err) {
                console.error('Upload error:', err);
                bwShowFlash(_t('js.upload.server_unreachable'), 'error');
            });
    }

    // Drop zone
    if (dropZone) {
        dropZone.addEventListener('dragover', function(e) {
            if (!bwHasImageFiles(e.dataTransfer)) return;
            e.preventDefault();
            dropZone.classList.add('drag-over');
        });
        dropZone.addEventListener('dragleave', function() {
            dropZone.classList.remove('drag-over');
        });
        dropZone.addEventListener('drop', function(e) {
            var imgs = bwExtractImageFiles(e.dataTransfer);
            if (!imgs.length) return;
            e.preventDefault();
            dropZone.classList.remove('drag-over');
            imgs.forEach(uploadFile);
        });
    }

    // Also handle drop on textarea: accept image files dropped directly
    // onto the editor area as well as onto the dedicated drop zone.
    contentEl.addEventListener('dragover', function(e) {
        if (bwHasImageFiles(e.dataTransfer)) e.preventDefault();
    });
    contentEl.addEventListener('drop', function(e) {
        var imgs = bwExtractImageFiles(e.dataTransfer);
        if (!imgs.length) return;
        e.preventDefault();
        imgs.forEach(uploadFile);
    });

    // Paste image from clipboard (e.g. screenshot) into the editor.
    contentEl.addEventListener('paste', function(e) {
        var imgs = bwExtractImageFiles(e.clipboardData);
        if (!imgs.length) return;
        e.preventDefault();
        imgs.forEach(uploadFile);
    });

    // Attach button
    var attachBtn = document.getElementById('attach-btn');
    var fileInput = document.getElementById('file-input');
    if (attachBtn && fileInput) {
        attachBtn.addEventListener('click', function() { fileInput.click(); });
        fileInput.addEventListener('change', function() {
            Array.from(fileInput.files).forEach(uploadFile);
            fileInput.value = '';
        });
    }
}

// Shared clipboard / drag helpers: return image File objects from a
// DataTransfer or DataTransferList.  Used by drop and paste handlers in
// the wiki editor, canvas, kanban description / comment editors, and
// other surfaces where users can attach images.
function bwExtractImageFiles(dataTransfer) {
    if (!dataTransfer) return [];
    var out = [];
    var seen = {};
    var pushFile = function(f) {
        if (!f || !f.type || f.type.indexOf('image/') !== 0) return;
        // De-duplicate (Chrome surfaces the same image via both .items and
        // .files when pasting screenshots).
        var key = (f.name || '') + '|' + f.size + '|' + f.type;
        if (seen[key]) return;
        seen[key] = true;
        out.push(f);
    };
    if (dataTransfer.items && dataTransfer.items.length) {
        for (var i = 0; i < dataTransfer.items.length; i++) {
            var it = dataTransfer.items[i];
            if (it && it.kind === 'file') pushFile(it.getAsFile());
        }
    }
    if (dataTransfer.files && dataTransfer.files.length) {
        for (var j = 0; j < dataTransfer.files.length; j++) {
            pushFile(dataTransfer.files[j]);
        }
    }
    return out;
}

function bwHasImageFiles(dataTransfer) {
    if (!dataTransfer) return false;
    if (dataTransfer.types && dataTransfer.types.length) {
        for (var i = 0; i < dataTransfer.types.length; i++) {
            if (dataTransfer.types[i] === 'Files') return true;
        }
    }
    if (dataTransfer.items && dataTransfer.items.length) {
        for (var k = 0; k < dataTransfer.items.length; k++) {
            var it = dataTransfer.items[k];
            if (it && it.kind === 'file' && it.type && it.type.indexOf('image/') === 0) return true;
        }
    }
    return false;
}

// Expose so inline page scripts (canvas / kanban / etc.) can reuse them.
window.bwExtractImageFiles = bwExtractImageFiles;
window.bwHasImageFiles = bwHasImageFiles;

// Image options modal
var _imgModalUrl = '', _imgModalInsertPos = 0, _imgModalEl = null;
// 'insert' = new image, 'edit' = update existing image properties
var _imgModalMode = 'insert', _imgModalOrigSrc = '';
var _videoModalEl = null, _videoModalMode = 'insert', _videoModalOrigUrl = '';
// Keep this in sync with helpers/_markdown.py:_VIDEO_PADDING_BY_RATIO.
var VIDEO_RATIO_PADDING_MAP = { '16:9': 56.25, '4:3': 75, '1:1': 100 };

function escapeRegexChars(str) {
    return String(str || '').replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

function extractMediaPadding(styleStr) {
    var padding = '', mt = '', mb = '';
    var paddingVarMatch = (styleStr || '').match(/--media-padding:\s*(\d+)px/);
    var paddingMatch = (styleStr || '').match(/(?:^|;)\s*padding:\s*(\d+)px/);
    var mtMatch = (styleStr || '').match(/--media-margin-top:\s*(\d+)px/);
    var mbMatch = (styleStr || '').match(/--media-margin-bottom:\s*(\d+)px/);
    if (paddingVarMatch) padding = paddingVarMatch[1];
    else if (paddingMatch) padding = paddingMatch[1];
    if (mtMatch) mt = mtMatch[1];
    if (mbMatch) mb = mbMatch[1];
    return padding || mt || mb || '';
}

function setActiveOptionButton(buttonSelector, attrName, value, fallbackValue) {
    var match = null;
    document.querySelectorAll(buttonSelector).forEach(function(btn) {
        var isActive = btn.dataset[attrName] === value;
        btn.classList.toggle('active', isActive);
        btn.classList.toggle('btn-outline', !isActive);
        if (isActive) match = btn;
    });
    if (!match && fallbackValue !== undefined) {
        document.querySelectorAll(buttonSelector).forEach(function(btn) {
            var isActive = btn.dataset[attrName] === fallbackValue;
            btn.classList.toggle('active', isActive);
            btn.classList.toggle('btn-outline', !isActive);
        });
    }
}

function bindHorizontalResize(handle, getStartWidth, onMoveWidth, onCommit, hooks) {
    if (!handle) return;
    handle.style.touchAction = 'none';
    handle.addEventListener('pointerdown', function(e) {
        e.preventDefault();
        e.stopPropagation();
        // Capture the pointer on the handle so subsequent move/up events
        // are delivered here even when the cursor enters a child iframe
        // (e.g. a YouTube embed), which would otherwise swallow them.
        try { handle.setPointerCapture(e.pointerId); } catch (_) {}
        var startX = e.clientX;
        var startW = getStartWidth();
        document.body.style.userSelect = 'none';
        document.body.style.cursor = 'ew-resize';
        if (hooks && typeof hooks.onStart === 'function') hooks.onStart();

        function onMove(ev) {
            onMoveWidth(Math.round(startW + (ev.clientX - startX)));
        }

        function onUp(ev) {
            handle.removeEventListener('pointermove', onMove);
            handle.removeEventListener('pointerup', onUp);
            handle.removeEventListener('pointercancel', onUp);
            document.removeEventListener('pointermove', onMove);
            document.removeEventListener('pointerup', onUp);
            document.removeEventListener('pointercancel', onUp);
            document.body.style.userSelect = '';
            document.body.style.cursor = '';
            try { handle.releasePointerCapture(ev && ev.pointerId); } catch (_) {}
            if (hooks && typeof hooks.onEnd === 'function') hooks.onEnd();
            if (onCommit) onCommit();
        }

        // Listen on both the handle (which receives captured events) and
        // the document (fallback for browsers without pointer capture).
        handle.addEventListener('pointermove', onMove);
        handle.addEventListener('pointerup', onUp);
        handle.addEventListener('pointercancel', onUp);
        document.addEventListener('pointermove', onMove);
        document.addEventListener('pointerup', onUp);
        document.addEventListener('pointercancel', onUp);
    });
}

function resetImageOptionsModal() {
    var widthInput = document.getElementById('img-width-input');
    var paddingInput = document.getElementById('img-padding-input');
    if (widthInput) widthInput.value = '';
    if (paddingInput) paddingInput.value = '';
    setActiveOptionButton('.img-align-btn', 'align', 'none', 'none');
}

function openImageOptionsModal(url, filename, contentEl) {
    _imgModalUrl = url;
    _imgModalOrigSrc = '';
    _imgModalMode = 'insert';
    _imgModalEl = contentEl;
    _imgModalInsertPos = contentEl ? (contentEl.selectionStart || contentEl.value.length) : 0;
    var preview = document.getElementById('img-preview');
    if (preview) { preview.src = url; preview.style.display = ''; }
    var altInput = document.getElementById('img-alt-input');
    if (altInput) altInput.value = filename ? filename.replace(/\.[^.]+$/, '') : '';
    resetImageOptionsModal();
    var modal = document.getElementById('image-options-modal');
    if (modal) {
        var h3 = modal.querySelector('h3');
        if (h3) h3.textContent = _t('js.image.insert_title');
        var insertBtn = modal.querySelector('#img-insert-btn');
        if (insertBtn) insertBtn.textContent = _t('js.image.insert_button');
        modal.style.display = 'flex';
    }
}

function openEditImageModal(src, alt, width, align, textareaEl, padding) {
    _imgModalUrl = src;
    _imgModalOrigSrc = src;
    _imgModalMode = 'edit';
    _imgModalEl = textareaEl;
    _imgModalInsertPos = 0;
    var preview = document.getElementById('img-preview');
    if (preview) { preview.src = src; preview.style.display = ''; }
    var altInput = document.getElementById('img-alt-input');
    if (altInput) altInput.value = alt || '';
    var widthInput = document.getElementById('img-width-input');
    if (widthInput) widthInput.value = width || '';
    var paddingInput = document.getElementById('img-padding-input');
    if (paddingInput) paddingInput.value = padding || '';
    setActiveOptionButton('.img-align-btn', 'align', align || 'none', 'none');
    var modal = document.getElementById('image-options-modal');
    if (modal) {
        var h3 = modal.querySelector('h3');
        if (h3) h3.textContent = _t('js.image.edit_title');
        var insertBtn = modal.querySelector('#img-insert-btn');
        if (insertBtn) insertBtn.textContent = _t('js.image.edit_button');
        modal.style.display = 'flex';
    }
}

function closeImageOptionsModal() {
    var modal = document.getElementById('image-options-modal');
    if (modal) modal.style.display = 'none';
}

function resetVideoOptionsModal() {
    var urlInput = document.getElementById('video-url-input');
    var widthInput = document.getElementById('video-width-input');
    var marginInput = document.getElementById('video-margin-input');
    var autoplayInput = document.getElementById('video-autoplay-input');
    var controlsInput = document.getElementById('video-controls-input');
    var loopInput = document.getElementById('video-loop-input');
    if (urlInput && _videoModalMode !== 'edit') urlInput.value = '';
    if (widthInput) widthInput.value = '';
    if (marginInput) marginInput.value = '';
    if (autoplayInput) autoplayInput.checked = false;
    if (controlsInput) controlsInput.checked = true;
    if (loopInput) loopInput.checked = false;
    setActiveOptionButton('.video-align-btn', 'align', 'center', 'center');
    setActiveOptionButton('.video-ratio-btn', 'ratio', '16:9', '16:9');
    setActiveOptionButton('.video-preload-btn', 'preload', 'metadata', 'metadata');
}

function openVideoOptionsModal(url, width, align, ratio, textareaEl, margin, autoplay, loop, controls, preload) {
    _videoModalEl = textareaEl || document.getElementById('edit-content');
    _videoModalOrigUrl = url || '';
    _videoModalMode = url ? 'edit' : 'insert';
    var modal = document.getElementById('video-embed-modal');
    var title = modal ? modal.querySelector('h3') : null;
    var insertBtn = document.getElementById('video-insert-btn');
    resetVideoOptionsModal();
    var urlInput = document.getElementById('video-url-input');
    var widthInput = document.getElementById('video-width-input');
    var marginInput = document.getElementById('video-margin-input');
    var autoplayInput = document.getElementById('video-autoplay-input');
    var controlsInput = document.getElementById('video-controls-input');
    var loopInput = document.getElementById('video-loop-input');
    if (urlInput) urlInput.value = url || '';
    if (widthInput) widthInput.value = width || '';
    if (marginInput) marginInput.value = margin || '';
    if (autoplayInput) autoplayInput.checked = autoplay === 'true' || autoplay === true;
    if (controlsInput) controlsInput.checked = controls !== 'false' && controls !== false;
    if (loopInput) loopInput.checked = loop === 'true' || loop === true;
    setActiveOptionButton('.video-align-btn', 'align', align || 'center', 'center');
    setActiveOptionButton('.video-ratio-btn', 'ratio', ratio || '16:9', '16:9');
    setActiveOptionButton('.video-preload-btn', 'preload', preload || 'metadata', 'metadata');
    if (title) title.textContent = window._t ? window._t(_videoModalMode === 'edit' ? 'js.video_modal.edit_title' : 'js.video_modal.embed_title') : (_videoModalMode === 'edit' ? 'Edit Video' : 'Embed Video');
    if (insertBtn) insertBtn.textContent = window._t ? window._t(_videoModalMode === 'edit' ? 'js.video_modal.update' : 'js.video_modal.insert') : (_videoModalMode === 'edit' ? 'Update' : 'Insert');
    if (modal) modal.style.display = 'flex';
}

function closeVideoOptionsModal() {
    var modal = document.getElementById('video-embed-modal');
    if (modal) modal.style.display = 'none';
}

function buildVideoShortcode(url, width, align, ratio, margin, autoplay, loop, controls, preload) {
    var cleanUrl = (url || '').trim();
    if (!cleanUrl) return '';
    var parts = ['[[video url="' + escapeHtml(cleanUrl) + '"'];
    if (width) parts.push('width="' + escapeHtml(width) + '"');
    if (align && align !== 'center') parts.push('align="' + escapeHtml(align) + '"');
    if (ratio && ratio !== '16:9') parts.push('ratio="' + escapeHtml(ratio) + '"');
    if (margin) parts.push('margin="' + escapeHtml(margin) + '"');
    if (autoplay) parts.push('autoplay="true"');
    if (loop) parts.push('loop="true"');
    if (controls === false || controls === 'false') parts.push('controls="false"');
    if (preload && preload !== 'metadata') parts.push('preload="' + escapeHtml(preload) + '"');
    return parts.join(' ') + ']]';
}

function confirmVideoInsert() {
    var urlInput = document.getElementById('video-url-input');
    var widthInput = document.getElementById('video-width-input');
    var marginInput = document.getElementById('video-margin-input');
    var autoplayInput = document.getElementById('video-autoplay-input');
    var controlsInput = document.getElementById('video-controls-input');
    var loopInput = document.getElementById('video-loop-input');
    var activeAlign = document.querySelector('.video-align-btn.active');
    var activeRatio = document.querySelector('.video-ratio-btn.active');
    var activePreload = document.querySelector('.video-preload-btn.active');
    var url = urlInput ? urlInput.value.trim() : '';
    var width = widthInput ? widthInput.value.trim() : '';
    var align = activeAlign ? activeAlign.dataset.align : 'center';
    var ratio = activeRatio ? activeRatio.dataset.ratio : '16:9';
    var margin = marginInput ? marginInput.value.trim() : '';
    var autoplay = autoplayInput ? autoplayInput.checked : false;
    var loop = loopInput ? loopInput.checked : false;
    var controls = controlsInput ? controlsInput.checked : true;
    var preload = activePreload ? activePreload.dataset.preload : 'metadata';
    var shortcode = buildVideoShortcode(url, width, align, ratio, margin, autoplay, loop, controls, preload);
    var ta = _videoModalEl || document.getElementById('edit-content');
    if (!ta || !shortcode) return;
    if (_videoModalMode === 'edit' && _videoModalOrigUrl) {
        updateVideoInEditor(ta, _videoModalOrigUrl, shortcode);
    } else {
        var pos = ta.selectionStart || ta.value.length;
        var before = ta.value.substring(0, pos);
        var after = ta.value.substring(pos);
        var prefix = (before.length && before[before.length - 1] !== '\n') ? '\n' : '';
        var suffix = (after.length && after[0] !== '\n') ? '\n' : '';
        ta.value = before + prefix + shortcode + suffix + after;
        ta.selectionStart = ta.selectionEnd = pos + prefix.length + shortcode.length + suffix.length;
    }
    ta.focus();
    ta.dispatchEvent(new Event('input'));
    closeVideoOptionsModal();
}

function confirmImageInsert() {
    var ta = _imgModalEl;
    if (!ta) return;
    var altEl = document.getElementById('img-alt-input');
    var widthEl = document.getElementById('img-width-input');
    var paddingEl = document.getElementById('img-padding-input');
    var alt = (altEl ? altEl.value || '' : '').trim();
    var width = (widthEl ? widthEl.value || '' : '').trim();
    var padding = (paddingEl ? paddingEl.value || '' : '').trim();
    var activeBtn = document.querySelector('.img-align-btn.active');
    var alignVal = activeBtn ? activeBtn.dataset.align : 'none';
    var url = escapeHtml(_imgModalUrl);
    var md;
    var hasPadding = !!padding;
    if (alignVal === 'none' && !width && !hasPadding) {
        md = '\n![' + alt + '](' + _imgModalUrl + ')\n';
    } else if (alignVal === 'none') {
        var styleParts = [];
        if (width) styleParts.push('--media-max-width:' + escapeHtml(width) + 'px');
        if (padding) {
            styleParts.push('--media-padding:' + escapeHtml(padding) + 'px');
            styleParts.push('padding:' + escapeHtml(padding) + 'px');
        }
        var styleAttr = styleParts.length ? ' style="' + styleParts.join(';') + '"' : '';
        md = '\n<img src="' + url + '" alt="' + escapeHtml(alt) + '"' + (width ? ' width="' + escapeHtml(width) + '"' : '') + ' class="media-responsive"' + styleAttr + '>\n';
    } else {
        var cls = 'wiki-img-' + alignVal + ' media-figure media-' + alignVal;
        var figStyle = '';
        if (hasPadding) {
            var figStyleParts = [];
            figStyleParts.push('--media-padding:' + escapeHtml(padding) + 'px');
            figStyleParts.push('padding:' + escapeHtml(padding) + 'px');
            figStyle = ' style="' + figStyleParts.join(';') + '"';
        }
        var imgStyle = width ? ' style="--media-max-width:' + escapeHtml(width) + 'px"' : '';
        var imgTag = '<img src="' + url + '" alt="' + escapeHtml(alt) + '"' + (width ? ' width="' + escapeHtml(width) + '"' : '') + ' class="media-responsive"' + imgStyle + '>';
        var caption = alt ? '<figcaption>' + escapeHtml(alt) + '</figcaption>' : '';
        md = '\n<figure class="' + cls + '"' + figStyle + '>' + imgTag + caption + '</figure>\n';
    }
    if (_imgModalMode === 'edit' && _imgModalOrigSrc) {
        updateImageInEditor(ta, _imgModalOrigSrc, md.trim());
    } else {
        ta.value = ta.value.substring(0, _imgModalInsertPos) + md + ta.value.substring(_imgModalInsertPos);
    }
    ta.dispatchEvent(new Event('input'));
    ta.focus();
    closeImageOptionsModal();
}

document.addEventListener('DOMContentLoaded', function() {
    var insertBtn = document.getElementById('img-insert-btn');
    var cancelBtn = document.getElementById('img-cancel-btn');
    var resetBtn = document.getElementById('img-reset-btn');
    var modal = document.getElementById('image-options-modal');
    if (insertBtn) insertBtn.addEventListener('click', confirmImageInsert);
    if (cancelBtn) cancelBtn.addEventListener('click', closeImageOptionsModal);
    if (resetBtn) resetBtn.addEventListener('click', resetImageOptionsModal);
    if (modal) {
        modal.addEventListener('click', function(e) { if (e.target === this) closeImageOptionsModal(); });
    }
    document.querySelectorAll('.img-align-btn').forEach(function(btn) {
        btn.addEventListener('click', function() {
            setActiveOptionButton('.img-align-btn', 'align', btn.dataset.align, 'none');
        });
    });
    document.querySelectorAll('.video-align-btn').forEach(function(btn) {
        btn.addEventListener('click', function() {
            setActiveOptionButton('.video-align-btn', 'align', btn.dataset.align, 'center');
        });
    });
    document.querySelectorAll('.video-ratio-btn').forEach(function(btn) {
        btn.addEventListener('click', function() {
            setActiveOptionButton('.video-ratio-btn', 'ratio', btn.dataset.ratio, '16:9');
        });
    });
    document.querySelectorAll('.video-preload-btn').forEach(function(btn) {
        btn.addEventListener('click', function() {
            setActiveOptionButton('.video-preload-btn', 'preload', btn.dataset.preload, 'metadata');
        });
    });
});
