
//  Resizable images in edit preview pane

function initResizableImages(previewEl, textareaEl) {
    if (!previewEl || !textareaEl) return;
    previewEl.querySelectorAll('img').forEach(function(img) {
        if (img.dataset.bwResizable) return;
        img.dataset.bwResizable = '1';

        var container;
        var parentFig = img.closest('figure');
        if (parentFig) {
            container = parentFig;
            container.style.position = 'relative';
        } else {
            var wrap = document.createElement('div');
            wrap.className = 'preview-img-wrap';
            img.parentNode.insertBefore(wrap, img);
            wrap.appendChild(img);
            container = wrap;
        }

        var handle = document.createElement('span');
        handle.className = 'preview-img-resize-handle';
        handle.title = window._t ? window._t('js.resize.image_handle') : 'Drag to resize image';
        container.appendChild(handle);

        var tooltip = document.createElement('span');
        tooltip.className = 'preview-img-resize-tooltip';
        tooltip.textContent = '';
        container.appendChild(tooltip);

        // Click-to-edit: clicking the image (not the resize handle) opens edit modal
        img.style.cursor = 'pointer';
        img.title = 'Click to edit image size / position';
        img.addEventListener('click', function(e) {
            if (container.classList && container.classList.contains('resizing')) return;
            e.preventDefault();
            e.stopPropagation();
            var src = img.getAttribute('src') || '';
            var alt = img.getAttribute('alt') || '';
            var widthAttr = img.getAttribute('width') || '';
            var align = 'none';
            var padding;
            var fig = img.closest('figure');
            if (fig) {
                if (fig.classList.contains('wiki-img-left')) align = 'left';
                else if (fig.classList.contains('wiki-img-right')) align = 'right';
                else if (fig.classList.contains('wiki-img-center')) align = 'center';
                padding = extractMediaPadding(fig.getAttribute('style'));
            } else {
                padding = extractMediaPadding(img.getAttribute('style'));
            }
            openEditImageModal(src, alt, widthAttr, align, textareaEl, padding);
        });

        bindHorizontalResize(
            handle,
            function() { return img.getBoundingClientRect().width; },
            function(nextWidth) {
                var newW = Math.max(50, nextWidth);
                img.style.width = newW + 'px';
                img.style.maxWidth = 'none';
                tooltip.textContent = Math.round(newW) + ' px';
                if (container.classList) container.classList.add('resizing');
            },
            function() {
                var finalW = Math.round(img.getBoundingClientRect().width);
                tooltip.textContent = '';
                if (container.classList) container.classList.remove('resizing');
                updateImageWidthInEditor(textareaEl, img.getAttribute('src'), finalW);
            }
        );
    });

    // Make embedded videos resizable in the preview pane.  The iframe stays
    // interactive (so play/pause/scrub still work in the live preview);
    // pointer events are only suspended while the user is actively dragging
    // the resize handle, since otherwise the iframe would swallow the move
    // and up events that happen over its body.
    previewEl.querySelectorAll('.video-embed').forEach(function(embed) {
        if (embed.dataset.bwResizable) return;
        embed.dataset.bwResizable = '1';

        var iframe = embed.querySelector('iframe');
        embed.style.position = 'relative';

        // Resize handle (bottom-right).
        var handle = document.createElement('span');
        handle.className = 'preview-img-resize-handle preview-video-resize-handle';
        handle.title = window._t ? window._t('js.resize.video_handle') : 'Drag to resize video';
        handle.setAttribute('aria-label', handle.title);
        embed.appendChild(handle);

        // Edit overlay (top-right).  Lets users open the size/position
        // modal without losing the iframe's own click target (play button).
        var editBtn = document.createElement('button');
        editBtn.type = 'button';
        editBtn.className = 'preview-video-edit-btn';
        editBtn.title = window._t ? window._t('js.resize.video_edit_btn') : 'Edit video size / position';
        editBtn.setAttribute('aria-label', editBtn.title);
        editBtn.textContent = '\u270E'; // ✎
        editBtn.addEventListener('click', function(e) {
            e.preventDefault();
            e.stopPropagation();
            openVideoOptionsModal(
                embed.dataset.bwSourceUrl || '',
                embed.dataset.bwWidth || '',
                embed.dataset.bwAlign || 'center',
                embed.dataset.bwRatio || '16:9',
                textareaEl,
                embed.dataset.bwMargin || '',
                embed.dataset.bwAutoplay || '',
                embed.dataset.bwLoop || '',
                embed.dataset.bwControls || '',
                embed.dataset.bwPreload || ''
            );
        });
        embed.appendChild(editBtn);

        bindHorizontalResize(
            handle,
            function() { return embed.getBoundingClientRect().width; },
            function(nextWidth) {
                var ratio = embed.dataset.bwRatio || '16:9';
                var newW = Math.max(200, nextWidth);
                embed.dataset.bwWidth = String(newW);
                embed.style.width = 'min(' + newW + 'px,100%)';
                embed.style.maxWidth = '100%';
                embed.style.paddingBottom = VIDEO_RATIO_PADDING_MAP[ratio] + '%';
            },
            function() {
                updateVideoInEditor(
                    textareaEl,
                    embed.dataset.bwSourceUrl || '',
                    buildVideoShortcode(
                        embed.dataset.bwSourceUrl || '',
                        embed.dataset.bwWidth || '',
                        embed.dataset.bwAlign || 'center',
                        embed.dataset.bwRatio || '16:9',
                        embed.dataset.bwMargin || '',
                        embed.dataset.bwAutoplay === 'true',
                        embed.dataset.bwLoop === 'true',
                        embed.dataset.bwControls !== 'false',
                        embed.dataset.bwPreload || ''
                    )
                );
            },
            {
                onStart: function() { if (iframe) iframe.style.pointerEvents = 'none'; },
                onEnd: function() { if (iframe) iframe.style.pointerEvents = ''; },
            }
        );
    });
}

function updateImageWidthInEditor(textarea, src, width) {
    if (!textarea || !src || !width) return;
    var content = textarea.value;
    var updated;

    // Update any existing HTML <img> tag whose src matches
    updated = content.replace(/<img\b([^>]*)>/gi, function(match, attrs) {
        var srcMatch = attrs.match(/\bsrc=(?:"([^"]*)"|'([^']*)')/i);
        if (!srcMatch) return match;
        var tagSrc = srcMatch[1] !== undefined ? srcMatch[1] : srcMatch[2];
        if (tagSrc !== src) return match;
        // Strip existing width attribute and --media-max-width from style
        var newAttrs = attrs
            .replace(/\s+width=(?:"[^"]*"|'[^']*'|\S+)/gi, '')
            .replace(/--media-max-width\s*:\s*[^;]+;?\s*/gi, '')
            .replace(/\s*style="\s*"/, '')
            .replace(/\s+/g, ' ').trimEnd();
        // Add updated width and --media-max-width
        if (newAttrs.match(/\bstyle="/i)) {
            newAttrs = newAttrs.replace(/\b(style="[^"]*)"/i, '$1;--media-max-width:' + width + 'px"');
        } else {
            newAttrs += ' style="--media-max-width:' + width + 'px"';
        }
        return '<img' + newAttrs + ' width="' + width + '">';
    });

    // If no HTML <img> was matched, the image may be in Markdown format: ![alt](src)
    if (updated === content) {
        var escapedSrc = escapeRegexChars(src);
        updated = content.replace(
            new RegExp('!\\[([^\\]]*)\\]\\(' + escapedSrc + '\\)', 'g'),
            function(match, alt) {
                return '<img src="' + escapeHtml(src) + '" alt="' + escapeHtml(alt) + '" width="' + width + '" class="media-responsive" style="--media-max-width:' + width + 'px">';
            }
        );
    }

    if (updated !== content) {
        textarea.value = updated;
        textarea.dispatchEvent(new Event('input'));
    }
}

// Replace an existing image/figure in the editor with new markup (for edit mode).
// Searches for the image by src and replaces the surrounding figure (if any) or
// the bare <img> / markdown ![...](src) token.
function updateImageInEditor(textarea, src, newMarkdown) {
    if (!textarea || !src) return;
    var content = textarea.value;
    var updated = content;
    var escapedSrc = escapeRegexChars(src);

    // 1. <figure ...><img src="src"...>...</figure>  (only double- or single-quoted src)
    var figRe = new RegExp('<figure[^>]*>\\s*<img[^>]+src=(?:"' + escapedSrc + '"|\''+escapedSrc+'\'\\b)[^>]*>(?:\\s*<figcaption>[^]*?</figcaption>)?\\s*</figure>', 'i');
    if (figRe.test(updated)) {
        updated = updated.replace(figRe, newMarkdown);
    } else {
        // 2. Plain <img src="src"...> (not inside a figure)
        var imgRe = new RegExp('<img\\b([^>]*)>', 'gi');
        var replaced = false;
        updated = content.replace(imgRe, function(match, attrs) {
            if (replaced) return match;
            var srcMatch = attrs.match(/\bsrc=(?:"([^"]*)"|'([^']*)')/i);
            if (!srcMatch) return match;
            var tagSrc = srcMatch[1] !== undefined ? srcMatch[1] : srcMatch[2];
            if (tagSrc !== src) return match;
            replaced = true;
            return newMarkdown;
        });
        // 3. Markdown: ![alt](src)
        if (!replaced) {
            updated = content.replace(
                new RegExp('!\\[([^\\]]*)\\]\\(' + escapedSrc + '\\)', 'g'),
                newMarkdown
            );
        }
    }

    if (updated !== content) {
        textarea.value = updated;
        textarea.dispatchEvent(new Event('input'));
    }
}

function updateVideoInEditor(textarea, sourceUrl, newMarkup) {
    if (!textarea || !sourceUrl || !newMarkup) return;
    var content = textarea.value;
    var escapedSourceUrl = escapeRegexChars(sourceUrl);
    // Keep this shortcode matcher aligned with helpers/_markdown.py:_CUSTOM_VIDEO_RE.
    var shortcodeRe = new RegExp(
        '\\[\\[video\\s+url="' + escapedSourceUrl + '"'
        + '(?:\\s+width="\\d{2,4}")?'
        + '(?:\\s+align="(?:none|left|right|center)")?'
        + '(?:\\s+ratio="(?:16:9|4:3|1:1)")?'
        + '(?:\\s+margin="\\d{1,3}")?'
        + '(?:\\s+autoplay="(?:true|false)")?'
        + '(?:\\s+loop="(?:true|false)")?'
        + '(?:\\s+controls="(?:true|false)")?'
        + '(?:\\s+preload="(?:none|metadata|auto)")?'
        + '\\s*\\]\\]',
        'gi'
    );
    var bareUrlRe = new RegExp('(^|\\n)(' + escapedSourceUrl + ')(?=\\n|$)', 'g');
    var angleUrlRe = new RegExp('<' + escapedSourceUrl + '>', 'g');
    var updated = content.replace(shortcodeRe, newMarkup);
    if (updated === content) {
        updated = content.replace(bareUrlRe, function(match, prefix) {
            return prefix + newMarkup;
        });
    }
    if (updated === content) {
        updated = content.replace(angleUrlRe, newMarkup);
    }
    if (updated !== content) {
        textarea.value = updated;
        textarea.dispatchEvent(new Event('input'));
    }
}


//  Editor textarea keyboard helpers (indent / outdent / smart Enter / shortcuts)

//
// Two-space indents match CommonMark sub-list nesting and are what GitHub,
// VS Code and most other Markdown editors use.  Pressing Enter on a list
// item carries the bullet/number to the next line; pressing Enter on an
// empty bullet ends the list (matches the behaviour of Obsidian, Notion,
// VS Code, etc.).
//
// Keyboard shortcuts:
//   Ctrl/Cmd+B: Bold (**)
//   Ctrl/Cmd+I: Italic (*)
//   Ctrl/Cmd+K: Insert link
//   Ctrl/Cmd+Shift+X: Strikethrough (~~)
//   Ctrl/Cmd+Shift+C: Inline code (`)
function initEditorKeyboardShortcuts(textarea) {
    if (!textarea) return;
    var INDENT_UNIT = '  ';

    function dispatchInput() {
        textarea.dispatchEvent(new Event('input'));
    }

    function getLineRange(value, pos) {
        var start = value.lastIndexOf('\n', pos - 1) + 1;
        var endIdx = value.indexOf('\n', pos);
        if (endIdx === -1) endIdx = value.length;
        return [start, endIdx];
    }

    function indentSelection() {
        var v = textarea.value;
        var s = textarea.selectionStart;
        var e = textarea.selectionEnd;
        if (s === e) {
            // If the cursor is at the start of a line, inside leading
            // whitespace, or right after a list marker (e.g. "- " or "1. "),
            // indent the whole line so the bullet/number nests properly.
            var lineStart = v.lastIndexOf('\n', s - 1) + 1;
            var beforeCursor = v.substring(lineStart, s);
            var atLineStart = lineStart === s;
            var inLeadingWhitespace = /^[ \t]*$/.test(beforeCursor);
            var afterListMarker = /^[ \t]*([-*+]|\d+[.)])[ \t](\[[ xX]\][ \t])?$/.test(beforeCursor);
            if (atLineStart || inLeadingWhitespace || afterListMarker) {
                textarea.value = v.substring(0, lineStart) + INDENT_UNIT + v.substring(lineStart);
                textarea.selectionStart = textarea.selectionEnd = s + INDENT_UNIT.length;
                dispatchInput();
                return;
            }
            // Otherwise behave like a standard Tab key (insert two spaces).
            textarea.value = v.substring(0, s) + INDENT_UNIT + v.substring(s);
            textarea.selectionStart = textarea.selectionEnd = s + INDENT_UNIT.length;
            dispatchInput();
            return;
        }
        var firstLineStart = getLineRange(v, s)[0];
        var endProbe = e;
        if (endProbe > firstLineStart && v.charAt(endProbe - 1) === '\n') {
            endProbe = endProbe - 1;
        }
        var lastLineEnd = getLineRange(v, endProbe)[1];
        var block = v.substring(firstLineStart, lastLineEnd);
        var indented = block.replace(/^/gm, INDENT_UNIT);
        var added = indented.length - block.length;
        var addedFirst = INDENT_UNIT.length;
        textarea.value = v.substring(0, firstLineStart) + indented + v.substring(lastLineEnd);
        // Anchor the selection start to the original line start when the
        // user selected from column 0, so the caret doesn't slip into the
        // newly-inserted indent.
        textarea.selectionStart = s === firstLineStart ? s : s + addedFirst;
        textarea.selectionEnd = e + added;
        dispatchInput();
    }

    function outdentSelection() {
        var v = textarea.value;
        var s = textarea.selectionStart;
        var e = textarea.selectionEnd;
        var firstLineStart = getLineRange(v, s)[0];
        var endProbe = e;
        if (endProbe > firstLineStart && v.charAt(endProbe - 1) === '\n') {
            endProbe = endProbe - 1;
        }
        var lastLineEnd = getLineRange(v, endProbe)[1];
        var block = v.substring(firstLineStart, lastLineEnd);
        var firstRemoved = 0;
        var totalRemoved = 0;
        var outdented = block.split('\n').map(function(line, idx) {
            var match = line.match(/^( {1,2}|\t)/);
            if (!match) return line;
            if (idx === 0) firstRemoved = match[0].length;
            totalRemoved += match[0].length;
            return line.substring(match[0].length);
        }).join('\n');
        if (outdented === block) return;
        textarea.value = v.substring(0, firstLineStart) + outdented + v.substring(lastLineEnd);
        textarea.selectionStart = Math.max(firstLineStart, s - firstRemoved);
        textarea.selectionEnd = Math.max(textarea.selectionStart, e - totalRemoved);
        dispatchInput();
    }

    // Match an unordered, ordered, or task-list line.
    //   group 1: leading whitespace
    //   group 2: the marker (e.g. "-", "*", "+", "1.", "12)")
    //   group 3: optional task-list checkbox ("[ ]", "[x]", "[X]")
    //   group 4: trailing content
    var LIST_LINE_RE = /^([ \t]*)([-*+]|\d+[.)])\s(\[[ xX]\])?\s?(.*)$/;

    function parseListLine(line) {
        var m = line.match(LIST_LINE_RE);
        if (!m) return null;
        var marker = m[2];
        var nextMarker = marker;
        var numMatch = marker.match(/^(\d+)([.)])$/);
        if (numMatch) {
            nextMarker = (parseInt(numMatch[1], 10) + 1) + numMatch[2];
        }
        return {
            indent: m[1],
            marker: marker,
            nextMarker: nextMarker,
            checkbox: m[3] || '',
            content: m[4] || '',
        };
    }

    function handleEnter(ev) {
        if (ev.shiftKey || ev.altKey || ev.ctrlKey || ev.metaKey) return false;
        var v = textarea.value;
        var s = textarea.selectionStart;
        var e = textarea.selectionEnd;
        if (s !== e) return false;
        var range = getLineRange(v, s);
        var line = v.substring(range[0], range[1]);
        var info = parseListLine(line);
        if (!info) return false;
        // Empty list item with no content: exit the list.  Replace the
        // empty marker line with a blank line so the list terminates with
        // a proper paragraph break (otherwise the next line the user types
        // becomes a continuation of the previous list item).
        if (info.content === '' && !info.checkbox) {
            ev.preventDefault();
            var afterRange = v.substring(range[1]);
            // If there's no newline after the empty marker (e.g. the list
            // was the last line of the document), insert one so the
            // cursor lands on a fresh blank line.
            var insertion = afterRange.charAt(0) === '\n' ? '' : '\n';
            textarea.value = v.substring(0, range[0]) + insertion + afterRange;
            textarea.selectionStart = textarea.selectionEnd = range[0] + insertion.length;
            dispatchInput();
            return true;
        }
        ev.preventDefault();
        var prefix = info.indent + info.nextMarker + ' ' + (info.checkbox ? '[ ] ' : '');
        var newline = '\n' + prefix;
        textarea.value = v.substring(0, s) + newline + v.substring(s);
        var caret = s + newline.length;
        textarea.selectionStart = textarea.selectionEnd = caret;
        dispatchInput();
        return true;
    }

    textarea.addEventListener('keydown', function(ev) {
        if (ev.key === 'Tab') {
            ev.preventDefault();
            if (ev.shiftKey) outdentSelection();
            else indentSelection();
            return;
        }
        if (ev.key === 'Enter') {
            if (handleEnter(ev)) return;
        }
        if ((ev.ctrlKey || ev.metaKey) && !ev.altKey) {
            var key = (ev.key || '').toLowerCase();
            if (!ev.shiftKey && key === 'b' && typeof window.insertFormat === 'function') {
                ev.preventDefault(); window.insertFormat('**', '**'); return;
            }
            if (!ev.shiftKey && key === 'i' && typeof window.insertFormat === 'function') {
                ev.preventDefault(); window.insertFormat('*', '*'); return;
            }
            if (!ev.shiftKey && key === 'k' && typeof window.insertLink === 'function') {
                ev.preventDefault(); window.insertLink(); return;
            }
            if (ev.shiftKey && key === 'x' && typeof window.insertFormat === 'function') {
                ev.preventDefault(); window.insertFormat('~~', '~~'); return;
            }
            if (ev.shiftKey && key === 'c' && typeof window.insertInlineCode === 'function') {
                ev.preventDefault(); window.insertInlineCode(); return;
            }
        }
    });
}


//  Editor pane resize (split editor/preview in edit mode)

function initEditorResize() {
    var divider = document.querySelector('.editor-divider');
    var editorPane = document.querySelector('.editor-pane');
    var previewPane = document.querySelector('.preview-pane');
    if (!divider || !editorPane || !previewPane) return;

    var isResizingEditor = false;
    var container = divider.parentElement;

    function isMobileLayout() {
        return window.innerWidth <= 768;
    }

    function applyHorizSplit(clientX) {
        var rect = container.getBoundingClientRect();
        var offsetX = clientX - rect.left;
        var totalW = rect.width;
        var pct = Math.max(15, Math.min(85, (offsetX / totalW) * 100));
        editorPane.style.flex = 'none';
        editorPane.style.width = pct + '%';
        previewPane.style.flex = 'none';
        previewPane.style.width = (100 - pct) + '%';
        return pct;
    }

    function applyVertSplit(clientY) {
        var rect = container.getBoundingClientRect();
        var offsetY = clientY - rect.top;
        var totalH = rect.height;
        var pct = Math.max(15, Math.min(85, (offsetY / totalH) * 100));
        editorPane.style.flex = 'none';
        editorPane.style.height = pct + '%';
        previewPane.style.flex = 'none';
        previewPane.style.height = (100 - pct) + '%';
        return pct;
    }

    // Mouse events: horizontal split on desktop
    divider.addEventListener('mousedown', function(e) {
        if (isMobileLayout()) return;
        isResizingEditor = true;
        divider.classList.add('resizing');
        document.body.style.userSelect = 'none';
        document.body.style.cursor = 'col-resize';
        e.preventDefault();
    });

    document.addEventListener('mousemove', function(e) {
        if (!isResizingEditor) return;
        applyHorizSplit(e.clientX);
    });

    document.addEventListener('mouseup', function() {
        if (isResizingEditor) {
            isResizingEditor = false;
            divider.classList.remove('resizing');
            document.body.style.userSelect = '';
            document.body.style.cursor = '';
            var pct = parseFloat(editorPane.style.width);
            if (pct >= 15 && pct <= 85) {
                saveA11ySetting('editor_pane_width', pct);
            }
        }
    });

    // Touch events: horizontal on desktop, vertical on mobile (stacked layout)
    divider.addEventListener('touchstart', function(e) {
        if (e.touches.length !== 1) return;
        isResizingEditor = true;
        divider.classList.add('resizing');
        e.preventDefault();
    }, { passive: false });

    divider.addEventListener('touchmove', function(e) {
        if (!isResizingEditor || e.touches.length !== 1) return;
        e.preventDefault();
        var touch = e.touches[0];
        if (isMobileLayout()) {
            applyVertSplit(touch.clientY);
        } else {
            applyHorizSplit(touch.clientX);
        }
    }, { passive: false });

    divider.addEventListener('touchend', function() {
        if (!isResizingEditor) return;
        isResizingEditor = false;
        divider.classList.remove('resizing');
        if (!isMobileLayout()) {
            var pct = parseFloat(editorPane.style.width);
            if (pct >= 15 && pct <= 85) {
                saveA11ySetting('editor_pane_width', pct);
            }
        }
    });

    // Vertical resize handle for editor container height
    var vertHandle = document.getElementById('editor-resize-handle');
    var editorContainer = document.querySelector('.editor-container');
    if (vertHandle && editorContainer) {
        var isResizingVert = false;
        var startY, startH;

        function startVertResize(clientY) {
            isResizingVert = true;
            startY = clientY;
            startH = editorContainer.offsetHeight;
            vertHandle.classList.add('resizing');
            document.body.style.userSelect = 'none';
            document.body.style.cursor = 'row-resize';
        }

        function moveVertResize(clientY) {
            var newH = startH + (clientY - startY);
            if (newH >= 300 && newH <= 2000) {
                editorContainer.style.minHeight = newH + 'px';
                editorContainer.style.height = newH + 'px';
            }
        }

        function endVertResize() {
            if (!isResizingVert) return;
            isResizingVert = false;
            vertHandle.classList.remove('resizing');
            document.body.style.userSelect = '';
            document.body.style.cursor = '';
            var h = parseInt(editorContainer.style.height, 10);
            if (h >= 300 && h <= 2000) {
                saveA11ySetting('editor_height', h);
            }
        }

        vertHandle.addEventListener('mousedown', function(e) {
            startVertResize(e.clientY);
            e.preventDefault();
        });
        document.addEventListener('mousemove', function(e) {
            if (!isResizingVert) return;
            moveVertResize(e.clientY);
        });
        document.addEventListener('mouseup', endVertResize);

        vertHandle.addEventListener('touchstart', function(e) {
            if (e.touches.length !== 1) return;
            startVertResize(e.touches[0].clientY);
            e.preventDefault();
        }, { passive: false });
        vertHandle.addEventListener('touchmove', function(e) {
            if (!isResizingVert || e.touches.length !== 1) return;
            e.preventDefault();
            moveVertResize(e.touches[0].clientY);
        }, { passive: false });
        vertHandle.addEventListener('touchend', endVertResize);
    }
}


//  Editor mobile tabs (Edit / Preview switcher on phones)

function initEditorMobileTabs() {
    var tabs = document.getElementById('editor-mobile-tabs');
    var container = document.querySelector('.editor-container');
    if (!tabs || !container) return;

    var buttons = Array.prototype.slice.call(
        tabs.querySelectorAll('.editor-mobile-tab')
    );
    if (!buttons.length) return;

    var MOBILE_BREAKPOINT = 768;

    function isMobile() {
        return window.innerWidth <= MOBILE_BREAKPOINT;
    }

    function setActive(pane) {
        container.classList.remove(
            'editor-mobile-show-edit',
            'editor-mobile-show-preview'
        );
        container.classList.add('editor-mobile-show-' + pane);
        buttons.forEach(function(btn) {
            var match = btn.getAttribute('data-mobile-pane') === pane;
            btn.setAttribute('aria-selected', match ? 'true' : 'false');
        });
    }

    function applyVisibility() {
        if (isMobile()) {
            tabs.classList.remove('u-d-none');
            if (
                !container.classList.contains('editor-mobile-show-edit') &&
                !container.classList.contains('editor-mobile-show-preview')
            ) {
                setActive('edit');
            }
        } else {
            tabs.classList.add('u-d-none');
            container.classList.remove(
                'editor-mobile-show-edit',
                'editor-mobile-show-preview'
            );
        }
    }

    buttons.forEach(function(btn) {
        btn.addEventListener('click', function() {
            var pane = btn.getAttribute('data-mobile-pane');
            if (!pane) return;
            setActive(pane);
            if (pane === 'edit') {
                var ta = document.getElementById('edit-content');
                if (ta && typeof ta.focus === 'function') {
                    setTimeout(function() { ta.focus(); }, 0);
                }
            }
        });
    });

    applyVisibility();
    window.addEventListener('resize', applyVisibility);
    window.addEventListener('orientationchange', applyVisibility);
}


//  Improved editor formatting helpers (used by edit.html toolbar)
//  These replace the inline versions previously defined in the template.
//  Toggle-aware: if the selection is already wrapped, unwrap it.

function insertFormat(before, after) {
    var ta = document.getElementById('edit-content');
    if (!ta) return;
    var start = ta.selectionStart, end = ta.selectionEnd;
    var val = ta.value;
    var sel = val.substring(start, end);
    var len = before.length;

    // Check if text immediately before/after the selection has the delimiters.
    var textBefore = val.substring(Math.max(0, start - len), start);
    var textAfter = val.substring(end, Math.min(val.length, end + len));

    if (textBefore === before && textAfter === after) {
        // Unwrap: selection was already formatted by adjacent delimiters
        ta.value = val.substring(0, start - len) + sel + val.substring(end + len);
        ta.selectionStart = start - len;
        ta.selectionEnd = start - len + sel.length;
    } else if (sel.length >= len * 2 && sel.indexOf(before) === 0 && sel.lastIndexOf(after) === sel.length - len) {
        // Unwrap: selection itself contains the delimiters at its edges
        var inner = sel.substring(len, sel.length - len);
        ta.value = val.substring(0, start) + inner + val.substring(end);
        ta.selectionStart = start;
        ta.selectionEnd = start + inner.length;
    } else {
        // Wrap: insert delimiters around selection (or placeholder)
        var placeholder = sel || (before === '`' ? 'code' : 'text');
        ta.value = val.substring(0, start) + before + placeholder + after + val.substring(end);
        ta.selectionStart = start + len;
        ta.selectionEnd = start + len + placeholder.length;
    }

    ta.focus();
    ta.dispatchEvent(new Event('input'));
}

function insertLine(prefix) {
    var ta = document.getElementById('edit-content');
    if (!ta) return;
    var val = ta.value;
    var start = ta.selectionStart;
    var lineStart = val.lastIndexOf('\n', start - 1) + 1;
    var lineEnd = val.indexOf('\n', start);
    if (lineEnd === -1) lineEnd = val.length;
    var line = val.substring(lineStart, lineEnd);

    // Horizontal rule is always insert (toggle makes no sense for it)
    if (prefix === '---') {
        var hr = '\n' + prefix + '\n';
        ta.value = val.substring(0, start) + hr + val.substring(start);
        ta.selectionStart = ta.selectionEnd = start + hr.length;
        ta.focus();
        ta.dispatchEvent(new Event('input'));
        return;
    }

    // Toggle: check if line starts with the prefix
    if (line.substring(0, prefix.length) === prefix) {
        // Remove prefix (un-toggle)
        var rest = line.substring(prefix.length);
        ta.value = val.substring(0, lineStart) + rest + val.substring(lineEnd);
        ta.selectionStart = ta.selectionEnd = Math.max(lineStart, start - prefix.length);
    } else {
        // Add prefix (toggle on)
        ta.value = val.substring(0, lineStart) + prefix + line + val.substring(lineEnd);
        ta.selectionStart = ta.selectionEnd = start + prefix.length;
    }

    ta.focus();
    ta.dispatchEvent(new Event('input'));
}

function insertInlineCode() {
    insertFormat('`', '`');
}

function insertTaskList() {
    var ta = document.getElementById('edit-content');
    if (!ta) return;
    var val = ta.value;
    var start = ta.selectionStart;
    var lineStart = val.lastIndexOf('\n', start - 1) + 1;
    var lineEnd = val.indexOf('\n', start);
    if (lineEnd === -1) lineEnd = val.length;
    var line = val.substring(lineStart, lineEnd);

    // Check if line is already a task list item
    var taskRe = /^(\s*)([-*+]\s+)(\[[ xX]\]\s+)?(.*)$/;
    var m = line.match(taskRe);
    if (m && m[3]) {
        // Has checkbox → remove it (toggle off)
        var withoutCheckbox = m[1] + m[2] + m[4];
        ta.value = val.substring(0, lineStart) + withoutCheckbox + val.substring(lineEnd);
        ta.selectionStart = ta.selectionEnd = Math.max(lineStart, start - m[3].length);
    } else if (m) {
        // Has list marker but no checkbox → add checked checkbox
        var withCheckbox = m[1] + m[2] + '[ ] ' + m[4];
        ta.value = val.substring(0, lineStart) + withCheckbox + val.substring(lineEnd);
        ta.selectionStart = ta.selectionEnd = start + 4; // '[ ] '.length
    } else {
        // Not a list item → prepend "- [ ] "
        var newLine = '- [ ] ' + line;
        ta.value = val.substring(0, lineStart) + newLine + val.substring(lineEnd);
        ta.selectionStart = ta.selectionEnd = lineStart + 6;
    }

    ta.focus();
    ta.dispatchEvent(new Event('input'));
}


//  Table grid picker: a popup that lets users choose rows × columns and
//  inserts a markdown table at the cursor.

function initTableGridPicker(textarea) {
    var btn = document.getElementById('table-btn');
    var picker = document.getElementById('table-grid-picker');
    if (!btn || !picker) return;

    var MAX_ROWS = 10, MAX_COLS = 10;
    var body = picker.querySelector('.table-grid-body');
    var sizeLabel = document.getElementById('table-grid-size');
    if (!body || !sizeLabel) return;

    var selRows = 1, selCols = 1;

    function insertTable(rows, cols) {
        if (rows < 1 || cols < 1) return;
        var colHeaders = [];
        for (var c = 0; c < cols; c++) colHeaders.push('Header');
        var header = '| ' + colHeaders.join(' | ') + ' |\n';
        var sep = '| ' + Array(cols).fill('---').join(' | ') + ' |\n';
        var bodyRows = [];
        for (var r = 0; r < rows; r++) {
            var cells = [];
            for (var c2 = 0; c2 < cols; c2++) cells.push('Cell');
            bodyRows.push('| ' + cells.join(' | ') + ' |');
        }
        var table = '\n' + header + sep + bodyRows.join('\n') + '\n';

        var pos = textarea.selectionStart || textarea.value.length;
        textarea.value = textarea.value.substring(0, pos) + table + textarea.value.substring(pos);
        textarea.selectionStart = textarea.selectionEnd = pos + table.length;
        textarea.focus();
        textarea.dispatchEvent(new Event('input'));
    }

    // Build the grid cells
    for (var r = 0; r < MAX_ROWS; r++) {
        for (var c = 0; c < MAX_COLS; c++) {
            (function(row, col) {
                var cell = document.createElement('div');
                cell.className = 'table-grid-cell';
                cell.addEventListener('mouseenter', function() {
                    selRows = row + 1;
                    selCols = col + 1;
                    sizeLabel.textContent = selCols + ' \u00D7 ' + selRows;
                    var cells = body.querySelectorAll('.table-grid-cell');
                    cells.forEach(function(el) {
                        var er = parseInt(el.dataset.row, 10);
                        var ec = parseInt(el.dataset.col, 10);
                        el.classList.toggle('selected', er <= row && ec <= col);
                    });
                });
                cell.addEventListener('click', function() {
                    insertTable(selRows, selCols);
                    hidePicker();
                });
                cell.dataset.row = r;
                cell.dataset.col = c;
                body.appendChild(cell);
            }(r, c));
        }
    }

    function hidePicker() {
        picker.classList.add('u-d-none');
    }

    function showPicker() {
        var rect = btn.getBoundingClientRect();
        picker.style.left = Math.max(8, rect.left) + 'px';
        picker.style.top = (rect.bottom + window.scrollY + 4) + 'px';
        // Reset selection
        selRows = 1; selCols = 1;
        sizeLabel.textContent = '1 \u00D7 1';
        body.querySelectorAll('.table-grid-cell').forEach(function(el) {
            el.classList.toggle('selected', parseInt(el.dataset.row, 10) === 0 && parseInt(el.dataset.col, 10) === 0);
        });
        picker.classList.remove('u-d-none');
    }

    btn.addEventListener('click', function(e) {
        e.stopPropagation();
        if (picker.classList.contains('u-d-none')) {
            showPicker();
        } else {
            hidePicker();
        }
    });

    // Close when clicking outside
    document.addEventListener('click', function(e) {
        if (!picker.contains(e.target) && e.target !== btn) {
            hidePicker();
        }
    });
}


// Sidebar search
