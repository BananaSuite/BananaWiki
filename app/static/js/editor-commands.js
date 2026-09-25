//  Smart editor additions: table context toolbar, / command menu, auto-pair,
//  clear formatting, fullscreen, word count, sub/superscript.


// ── Table context toolbar

function initTableContextToolbar(textarea) {
    var toolbar = document.getElementById('table-ctx-toolbar');
    if (!toolbar || !textarea) return;

    var TABLE_LINE_RE = /^\|.+\|$/;

    function isCursorInTable() {
        var val = textarea.value;
        var pos = textarea.selectionStart;
        var lineStart = val.lastIndexOf('\n', pos - 1) + 1;
        var line = val.substring(lineStart, val.indexOf('\n', pos) !== -1 ? val.indexOf('\n', pos) : val.length);
        return TABLE_LINE_RE.test(line.trim());
    }

    function getTableBounds(val, pos) {
        var lines = val.split('\n');
        var cursorLine = val.substring(0, pos).split('\n').length - 1;
        var tableStart = -1, tableEnd = -1;
        for (var i = cursorLine; i >= 0; i--) {
            if (TABLE_LINE_RE.test(lines[i].trim())) { tableStart = i; }
            else if (lines[i].trim() === '' && tableStart !== -1) { break; }
            else if (tableStart === -1 && !TABLE_LINE_RE.test(lines[i].trim()) && lines[i].trim() !== '') { break; }
        }
        if (tableStart === -1) return null;
        for (var j = cursorLine; j < lines.length; j++) {
            if (TABLE_LINE_RE.test(lines[j].trim())) { tableEnd = j + 1; }
            else { break; }
        }
        // Count char offsets
        var startOffset = 0;
        for (var k = 0; k < tableStart; k++) startOffset += lines[k].length + 1;
        var endOffset = startOffset;
        for (var k = tableStart; k < tableEnd; k++) endOffset += lines[k].length + 1;
        return { startLine: tableStart, endLine: tableEnd, startOffset: startOffset, endOffset: endOffset, lines: lines.slice(tableStart, tableEnd) };
    }

    function insertRowAbove() {
        var bounds = getTableBounds(textarea.value, textarea.selectionStart);
        if (!bounds) return;
        var val = textarea.value;
        var cursorLineIdx = val.substring(0, textarea.selectionStart).split('\n').length - 1;
        var relLine = cursorLineIdx - bounds.startLine;
        if (relLine < 0 || relLine >= bounds.lines.length) return;
        // Skip separator row
        if (bounds.lines[relLine].match(/^\|[\s\-:]+\|$/)) relLine = Math.max(0, relLine - 1);
        var cols = bounds.lines[relLine].split('|').length - 2;
        var newRow = '| ' + Array(cols).fill('Cell').join(' | ') + ' |\n';
        var insertIdx = val.indexOf('\n', bounds.startOffset) !== -1
            ? val.indexOf('\n', bounds.startOffset) + 1
            : bounds.startOffset;
        // Find the correct insertion point (before the row at relLine)
        var offset = bounds.startOffset;
        for (var i = 0; i < relLine; i++) {
            offset = val.indexOf('\n', offset) + 1;
        }
        if (relLine === 0) offset = bounds.startOffset;
        textarea.value = val.substring(0, offset) + newRow + val.substring(offset);
        textarea.selectionStart = textarea.selectionEnd = offset + newRow.length;
        textarea.focus();
        textarea.dispatchEvent(new Event('input'));
    }

    function insertRowBelow() {
        var bounds = getTableBounds(textarea.value, textarea.selectionStart);
        if (!bounds) return;
        var val = textarea.value;
        var cursorLineIdx = val.substring(0, textarea.selectionStart).split('\n').length - 1;
        var relLine = cursorLineIdx - bounds.startLine;
        if (relLine < 0 || relLine >= bounds.lines.length) return;
        if (bounds.lines[relLine].match(/^\|[\s\-:]+\|$/)) relLine = Math.min(bounds.lines.length - 1, relLine + 1);
        var cols = bounds.lines[relLine].split('|').length - 2;
        var newRow = '| ' + Array(cols).fill('Cell').join(' | ') + ' |\n';
        var offset = bounds.startOffset;
        for (var i = 0; i <= relLine; i++) {
            offset = val.indexOf('\n', offset);
            if (offset === -1) offset = val.length;
            else offset = offset + 1;
        }
        textarea.value = val.substring(0, offset) + newRow + val.substring(offset);
        textarea.selectionStart = textarea.selectionEnd = offset + newRow.length;
        textarea.focus();
        textarea.dispatchEvent(new Event('input'));
    }

    function insertCol(dir) {
        var bounds = getTableBounds(textarea.value, textarea.selectionStart);
        if (!bounds) return null;
        var val = textarea.value;
        var cursorLineIdx = val.substring(0, textarea.selectionStart).split('\n').length - 1;
        var relLine = cursorLineIdx - bounds.startLine;
        if (relLine < 0 || relLine >= bounds.lines.length) return;
        if (bounds.lines[relLine].match(/^\|[\s\-:]+\|$/)) {
            relLine = relLine > 0 ? relLine - 1 : (relLine + 1 < bounds.lines.length ? relLine + 1 : null);
            if (relLine === null) return;
        }
        // Find column to insert left/right of based on cursor position within the line
        var line = bounds.lines[relLine];
        var lineStartGlobal = bounds.startOffset;
        for (var i = 0; i < relLine; i++) {
            lineStartGlobal = val.indexOf('\n', lineStartGlobal) + 1;
        }
        var cursorInLine = textarea.selectionStart - lineStartGlobal;
        var cells = line.split('|');
        // Remove empty first/last from leading/trailing |
        if (cells[0].trim() === '') cells.shift();
        if (cells[cells.length - 1].trim() === '') cells.pop();
        // Find which cell we're in based on cursor position
        var cumPos = 0;
        var cellIdx = 0;
        for (var ci = 0; ci < cells.length; ci++) {
            cumPos += 1; // for the |
            if (cursorInLine < cumPos + cells[ci].length + 1) { cellIdx = ci; break; }
            cumPos += cells[ci].length;
            if (ci === cells.length - 1) cellIdx = ci;
        }
        if (dir === 'left') {
            // Insert before cellIdx
        } else {
            cellIdx = cellIdx + 1; // Insert after
        }
        var newLines = bounds.lines.map(function(ln, idx) {
            var parts = ln.split('|');
            if (parts[0].trim() === '') parts.shift();
            if (parts[parts.length - 1].trim() === '') parts.pop();
            var isSep = ln.match(/^\|[\s\-:]+\|$/);
            var newCell = isSep ? ' --- ' : ' Cell ';
            var insertPos = Math.min(cellIdx, parts.length);
            parts.splice(insertPos, 0, newCell);
            return '| ' + parts.join(' | ') + ' |';
        });
        textarea.value = val.substring(0, bounds.startOffset) + newLines.join('\n') + val.substring(bounds.endOffset);
        textarea.focus();
        textarea.dispatchEvent(new Event('input'));
    }

    function deleteRow() {
        var bounds = getTableBounds(textarea.value, textarea.selectionStart);
        if (!bounds) return;
        var val = textarea.value;
        var cursorLineIdx = val.substring(0, textarea.selectionStart).split('\n').length - 1;
        var relLine = cursorLineIdx - bounds.startLine;
        if (relLine < 0 || relLine >= bounds.lines.length) return;
        if (bounds.lines[relLine].match(/^\|[\s\-:]+\|$/)) {
            // If deleting separator row, just skip
            return;
        }
        // Remove the line
        var offset = bounds.startOffset;
        for (var i = 0; i < relLine; i++) {
            offset = val.indexOf('\n', offset) + 1;
        }
        var lineEnd = val.indexOf('\n', offset);
        var before = val.substring(0, offset);
        var after = lineEnd !== -1 ? val.substring(lineEnd + 1) : '';
        textarea.value = before + after;
        textarea.selectionStart = textarea.selectionEnd = Math.min(offset, textarea.value.length);
        textarea.focus();
        textarea.dispatchEvent(new Event('input'));
    }

    function deleteCol() {
        var bounds = getTableBounds(textarea.value, textarea.selectionStart);
        if (!bounds) return null;
        var val = textarea.value;
        var cursorLineIdx = val.substring(0, textarea.selectionStart).split('\n').length - 1;
        var relLine = cursorLineIdx - bounds.startLine;
        if (relLine < 0 || relLine >= bounds.lines.length) return;
        if (bounds.lines[relLine].match(/^\|[\s\-:]+\|$/)) {
            relLine = relLine > 0 ? relLine - 1 : (relLine + 1 < bounds.lines.length ? relLine + 1 : null);
            if (relLine === null) return;
        }
        var line = bounds.lines[relLine];
        var lineStartGlobal = bounds.startOffset;
        for (var i = 0; i < relLine; i++) {
            lineStartGlobal = val.indexOf('\n', lineStartGlobal) + 1;
        }
        var cursorInLine = textarea.selectionStart - lineStartGlobal;
        var cells = line.split('|');
        if (cells[0].trim() === '') cells.shift();
        if (cells[cells.length - 1].trim() === '') cells.pop();
        var cumPos = 0;
        var cellIdx = 0;
        for (var ci = 0; ci < cells.length; ci++) {
            cumPos += 1;
            if (cursorInLine < cumPos + cells[ci].length + 1) { cellIdx = ci; break; }
            cumPos += cells[ci].length;
            if (ci === cells.length - 1) cellIdx = ci;
        }
        if (cells.length <= 1) return; // Can't delete last column
        var newLines = bounds.lines.map(function(ln) {
            var parts = ln.split('|');
            if (parts[0].trim() === '') parts.shift();
            if (parts[parts.length - 1].trim() === '') parts.pop();
            if (cellIdx < parts.length) parts.splice(cellIdx, 1);
            return '| ' + parts.join(' | ') + ' |';
        });
        textarea.value = val.substring(0, bounds.startOffset) + newLines.join('\n') + val.substring(bounds.endOffset);
        textarea.focus();
        textarea.dispatchEvent(new Event('input'));
    }

    function deleteTable() {
        var bounds = getTableBounds(textarea.value, textarea.selectionStart);
        if (!bounds) return;
        var val = textarea.value;
        var before = val.substring(0, bounds.startOffset);
        var after = val.substring(bounds.endOffset);
        // Remove leading blank line if present
        if (before.endsWith('\n\n')) before = before.slice(0, -1);
        else if (before.endsWith('\n')) before = before.slice(0, -1);
        textarea.value = before + after;
        textarea.selectionStart = textarea.selectionEnd = before.length;
        textarea.focus();
        textarea.dispatchEvent(new Event('input'));
    }

    var ACTION_MAP = {
        'insert-row-above': insertRowAbove,
        'insert-row-below': insertRowBelow,
        'insert-col-left': function() { insertCol('left'); },
        'insert-col-right': function() { insertCol('right'); },
        'delete-row': deleteRow,
        'delete-col': deleteCol,
        'delete-table': deleteTable,
    };

    toolbar.querySelectorAll('[data-table-action]').forEach(function(btn) {
        btn.addEventListener('click', function() {
            var action = ACTION_MAP[this.dataset.tableAction];
            if (action) action();
            hideTableToolbar();
        });
    });

    function showTableToolbar() {
        if (!toolbar.classList.contains('u-d-none')) return;
        var rect = textarea.getBoundingClientRect();
        var cursorPos = textarea.selectionStart;
        var val = textarea.value;
        var textBefore = val.substring(0, cursorPos);
        var linesBefore = textBefore.split('\n');
        var lineY = (linesBefore.length - 1) * 20; // approximate line height
        toolbar.style.left = Math.max(8, rect.left + 10) + 'px';
        toolbar.style.top = Math.max(rect.top + 10, rect.top + lineY - 40 + window.scrollY) + 'px';
        toolbar.classList.remove('u-d-none');
    }

    function hideTableToolbar() {
        toolbar.classList.add('u-d-none');
    }

    // Show/hide on cursor move or input
    textarea.addEventListener('click', function() {
        if (isCursorInTable()) showTableToolbar();
        else hideTableToolbar();
    });

    textarea.addEventListener('keyup', function() {
        if (isCursorInTable()) showTableToolbar();
        else hideTableToolbar();
    });

    textarea.addEventListener('input', function() {
        if (isCursorInTable()) showTableToolbar();
        else hideTableToolbar();
    });

    textarea.addEventListener('scroll', hideTableToolbar);

    // Hide when clicking outside
    document.addEventListener('click', function(e) {
        if (!toolbar.contains(e.target) && e.target !== textarea) {
            hideTableToolbar();
        }
    });

    // Tab navigation within tables (stopImmediatePropagation to prevent
    // the indent handler in initEditorKeyboardShortcuts from also firing).
    textarea.addEventListener('keydown', function(e) {
        if (e.key === 'Tab' && isCursorInTable()) {
            e.preventDefault();
            e.stopImmediatePropagation();
            var val = textarea.value;
            var pos = textarea.selectionStart;
            var lineStart = val.lastIndexOf('\n', pos - 1) + 1;
            var line = val.substring(lineStart, val.indexOf('\n', pos) !== -1 ? val.indexOf('\n', pos) : val.length);
            var cells = line.split('|');
            // Navigate to next/prev cell
            var cursorInLine = pos - lineStart;
            var cumPos = 0;
            var cellIdx = 0;
            for (var ci = 0; ci < cells.length; ci++) {
                cumPos += 1;
                if (cursorInLine < cumPos + (cells[ci] ? cells[ci].length : 0)) { cellIdx = ci; break; }
                cumPos += (cells[ci] ? cells[ci].length : 0);
                if (ci === cells.length - 1) cellIdx = ci;
            }
            if (e.shiftKey) {
                cellIdx = Math.max(0, cellIdx - 1);
            } else {
                cellIdx = Math.min(cells.length - 1, cellIdx + 1);
            }
            // Jump to next cell
            var newCumPos = 0;
            for (var ci2 = 0; ci2 <= cellIdx; ci2++) {
                newCumPos += 1;
                if (ci2 < cellIdx) newCumPos += (cells[ci2] ? cells[ci2].length : 0);
            }
            newCumPos = Math.min(newCumPos, line.length);
            textarea.selectionStart = textarea.selectionEnd = lineStart + newCumPos;
            textarea.focus();
        }
    });
}

// ── / command menu

function initCommandMenu(textarea) {
    var menu = document.getElementById('cmd-menu');
    if (!menu || !textarea) return;

    var active = false;
    var slashPos = -1;
    var filterText = '';

    function getLineFromPos(val, pos) {
        var lineStart = val.lastIndexOf('\n', pos - 1) + 1;
        var lineEnd = val.indexOf('\n', pos);
        if (lineEnd === -1) lineEnd = val.length;
        return { text: val.substring(lineStart, pos), lineStart: lineStart, lineEnd: lineEnd };
    }

    function showMenu() {
        var rect = textarea.getBoundingClientRect();
        var cursorPos = textarea.selectionStart;
        var val = textarea.value;
        var textBefore = val.substring(0, cursorPos);
        var linesBefore = textBefore.split('\n');
        var lineY = (linesBefore.length - 1) * 20;

        menu.style.left = Math.max(8, rect.left + 20) + 'px';
        menu.style.top = Math.max(rect.top + 20, rect.top + lineY - 120 + window.scrollY) + 'px';
        menu.classList.remove('u-d-none');
        active = true;
    }

    function hideMenu() {
        menu.classList.add('u-d-none');
        active = false;
        slashPos = -1;
        filterText = '';
        // Reset highlighting
        menu.querySelectorAll('.cmd-menu-item').forEach(function(item) {
            item.style.display = '';
            item.classList.remove('highlighted');
        });
    }

    function filterMenu(text) {
        var lower = text.toLowerCase();
        var hasMatch = false;
        var first = null;
        menu.querySelectorAll('.cmd-menu-item').forEach(function(item) {
            var label = item.textContent.trim().toLowerCase();
            if (label.indexOf(lower) !== -1) {
                item.style.display = '';
                item.classList.remove('highlighted');
                if (!first) { first = item; item.classList.add('highlighted'); }
                hasMatch = true;
            } else {
                item.style.display = 'none';
            }
        });
        return hasMatch;
    }

    function executeCommand(cmd) {
        hideMenu();
        var ta = textarea;

        // Remove the / and any filter text that was typed
        if (slashPos >= 0) {
            var afterSlash = ta.value.substring(slashPos, ta.selectionStart);
            ta.value = ta.value.substring(0, slashPos) + ta.value.substring(ta.selectionStart);
            ta.selectionStart = ta.selectionEnd = slashPos;
        }

        switch (cmd) {
            case 'table': {
                var table = '\n\n| Header | Header |\n| --- | --- |\n| Cell | Cell |\n\n';
                var pos = ta.selectionStart;
                ta.value = ta.value.substring(0, pos) + table + ta.value.substring(pos);
                ta.selectionStart = ta.selectionEnd = pos + table.length;
                break;
            }
            case 'h1': insertLine('# '); break;
            case 'h2': insertLine('## '); break;
            case 'h3': insertLine('### '); break;
            case 'code': {
                var code = '\n```\n\n```\n';
                var p = ta.selectionStart;
                ta.value = ta.value.substring(0, p) + code + ta.value.substring(p);
                ta.selectionStart = ta.selectionEnd = p + 5;
                break;
            }
            case 'image': document.getElementById('attach-btn').click(); break;
            case 'video': document.getElementById('embed-video-btn').click(); break;
            case 'link': insertLink(); break;
            case 'bullet': insertLine('- '); break;
            case 'numbered': insertLine('1. '); break;
            case 'task': insertTaskList(); break;
            case 'quote': insertLine('> '); break;
            case 'hr': insertLine('---'); break;
        }
        ta.focus();
        ta.dispatchEvent(new Event('input'));
    }

    textarea.addEventListener('keydown', function(e) {
        if (e.key === '/' && !e.ctrlKey && !e.metaKey && !e.altKey) {
            var val = textarea.value;
            var pos = textarea.selectionStart;
            var line = getLineFromPos(val, pos);
            // Only trigger at start of line or after whitespace at start of line
            var textBeforeCursor = line.text;
            if (textBeforeCursor === '/' || textBeforeCursor.match(/^\s+\/$/)) {
                // Let the / be inserted, then show menu after a brief delay
                return;
            }
        }
        if (active) {
            if (e.key === 'ArrowDown') {
                e.preventDefault();
                var items = Array.from(menu.querySelectorAll('.cmd-menu-item:not([style*="display: none"])'));
                var idx = items.findIndex(function(i) { return i.classList.contains('highlighted'); });
                if (idx < items.length - 1) {
                    items[idx].classList.remove('highlighted');
                    items[idx + 1].classList.add('highlighted');
                }
                return;
            }
            if (e.key === 'ArrowUp') {
                e.preventDefault();
                var items2 = Array.from(menu.querySelectorAll('.cmd-menu-item:not([style*="display: none"])'));
                var idx2 = items2.findIndex(function(i) { return i.classList.contains('highlighted'); });
                if (idx2 > 0) {
                    items2[idx2].classList.remove('highlighted');
                    items2[idx2 - 1].classList.add('highlighted');
                }
                return;
            }
            if (e.key === 'Enter' || e.key === 'Tab') {
                e.preventDefault();
                var highlighted = menu.querySelector('.cmd-menu-item.highlighted');
                if (highlighted) {
                    executeCommand(highlighted.dataset.cmd);
                }
                return;
            }
            if (e.key === 'Escape') {
                e.preventDefault();
                hideMenu();
                return;
            }
        }
    });

    textarea.addEventListener('input', function() {
        var val = textarea.value;
        var pos = textarea.selectionStart;
        var line = getLineFromPos(val, pos);

        if (!active) {
            // Check if we just typed /
            var text = line.text;
            if (text === '/' || text.match(/^\s+\/$/)) {
                slashPos = line.lineStart + text.length - 1;
                filterText = '';
                showMenu();
                filterMenu('');
                return;
            }
        } else {
            // We're in the menu - check if we're still on the same slash line
            if (slashPos >= 0) {
                var afterSlash = val.substring(slashPos, pos);
                if (afterSlash.indexOf('\n') !== -1) {
                    hideMenu();
                    return;
                }
                // Filter
                if (!filterMenu(afterSlash)) {
                    // No matches, hide
                    hideMenu();
                }
                return;
            }
        }
        // If typing anything else, hide menu
        if (active) hideMenu();
    });

    // Click on menu items
    menu.querySelectorAll('.cmd-menu-item').forEach(function(item) {
        item.addEventListener('click', function() {
            executeCommand(this.dataset.cmd);
        });
    });

    // Hide on click outside
    document.addEventListener('click', function(e) {
        if (!menu.contains(e.target) && e.target !== textarea) {
            hideMenu();
        }
    });
}

// ── Auto-pair brackets, quotes, and markdown delimiters

function initAutoPairing(textarea) {
    if (!textarea) return;

    var PAIRS = {
        '(': ')',
        '[': ']',
        '{': '}',
        '"': '"',
        "'": "'",
        '`': '`',
        '*': '*',
        '_': '_',
        '~': '~',
    };

    textarea.addEventListener('keydown', function(e) {
        if (e.ctrlKey || e.metaKey || e.altKey) return;
        var key = e.key;
        var close = PAIRS[key];
        if (!close) return;

        var val = textarea.value;
        var pos = textarea.selectionStart;
        var start = textarea.selectionStart;
        var end = textarea.selectionEnd;

        // If text is selected, wrap it with the pair
        if (start !== end) {
            e.preventDefault();
            var sel = val.substring(start, end);
            textarea.value = val.substring(0, start) + key + sel + close + val.substring(end);
            textarea.selectionStart = start + 1;
            textarea.selectionEnd = start + 1 + sel.length;
            textarea.focus();
            textarea.dispatchEvent(new Event('input'));
            return;
        }

        // Don't auto-close if the next char is alphanumeric (for `, *, _)
        if (key === '`' || key === '*' || key === '_' || key === '~') {
            var nextChar = val.charAt(pos);
            if (nextChar && /\w/.test(nextChar)) return;
            // Don't auto-close if we're inside a word
            var prevChar = val.charAt(pos - 1);
            if (prevChar && /\w/.test(prevChar)) return;
        }

        // Don't double-close
        if (val.charAt(pos) === close && key !== close) {
            e.preventDefault();
            textarea.selectionStart = textarea.selectionEnd = pos + 1;
            textarea.focus();
            return;
        }

        // Don't auto-close quotes inside words (e.g. "don't")
        if (key === "'" || key === '"' || key === '`') {
            var prevCh = val.charAt(pos - 1);
            var nextCh = val.charAt(pos);
            if (prevCh && /\w/.test(prevCh) && nextCh && /\w/.test(nextCh)) {
                return; // Inside a word, let the quote be regular
            }
        }

        e.preventDefault();
        textarea.value = val.substring(0, pos) + key + close + val.substring(pos);
        textarea.selectionStart = textarea.selectionEnd = pos + 1;
        textarea.focus();
    });
}

// ── Clear formatting

function clearFormatting() {
    var ta = document.getElementById('edit-content');
    if (!ta) return;
    var start = ta.selectionStart, end = ta.selectionEnd;
    if (start === end) {
        // Select current line
        var val = ta.value;
        start = val.lastIndexOf('\n', start - 1) + 1;
        end = val.indexOf('\n', start);
        if (end === -1) end = val.length;
    }
    var sel = ta.value.substring(start, end);
    // Remove common markdown formatting
    var cleaned = sel
        .replace(/\*\*(.+?)\*\*/g, '$1')
        .replace(/\*(.+?)\*/g, '$1')
        .replace(/~~(.+?)~~/g, '$1')
        .replace(/`(.+?)`/g, '$1')
        .replace(/__(.+?)__/g, '$1')
        .replace(/_(.+?)_/g, '$1')
        .replace(/^[#]+\s+/gm, '')
        .replace(/^[-*+]\s+/gm, '')
        .replace(/^\d+[.)]\s+/gm, '')
        .replace(/^>\s+/gm, '')
        .replace(/!\[([^\]]*)\]\([^)]+\)/g, '$1')
        .replace(/\[([^\]]*)\]\([^)]+\)/g, '$1');
    ta.value = ta.value.substring(0, start) + cleaned + ta.value.substring(end);
    ta.selectionStart = start;
    ta.selectionEnd = start + cleaned.length;
    ta.focus();
    ta.dispatchEvent(new Event('input'));
}

// ── Toggle fullscreen editor

function initFullscreenToggle() {
    var btn = document.querySelector('[data-action="fullscreen"]');
    if (!btn) return;
    btn.addEventListener('click', function() {
        document.body.classList.toggle('editor-fullscreen');
        var isFS = document.body.classList.contains('editor-fullscreen');
        this.textContent = isFS ? '⛶' : '⛶';
        this.title = isFS ? 'Exit full screen' : 'Toggle full screen';
    });
}

// ── Subscript / superscript

function insertSubSuper(tag) {
    var ta = document.getElementById('edit-content');
    if (!ta) return;
    var start = ta.selectionStart, end = ta.selectionEnd;
    var sel = ta.value.substring(start, end) || 'text';
    var md = '<' + tag + '>' + sel + '</' + tag + '>';
    ta.value = ta.value.substring(0, start) + md + ta.value.substring(end);
    ta.selectionStart = start;
    ta.selectionEnd = start + md.length;
    ta.focus();
    ta.dispatchEvent(new Event('input'));
}

// ── Word & character count

function initEditorStats(textarea) {
    if (!textarea) return;
    var wordEl = document.getElementById('editor-word-count');
    var charEl = document.getElementById('editor-char-count');
    if (!wordEl && !charEl) return;

    function update() {
        var val = textarea.value;
        var chars = val.length;
        var words = val.trim() ? val.trim().split(/\s+/).length : 0;
        if (wordEl) wordEl.textContent = words + ' ' + (window._t ? window._t('js.wiki_edit.words') : 'words');
        if (charEl) charEl.textContent = chars + ' ' + (window._t ? window._t('js.wiki_edit.chars') : 'chars');
    }

    textarea.addEventListener('input', update);
    update();
}
