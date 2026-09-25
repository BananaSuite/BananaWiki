(function () {
    'use strict';
    var root = document.getElementById('page-builder');
    if (!root) return;

    var blocksEl = document.getElementById('builder-blocks');
    var emptyEl = document.getElementById('builder-empty');
    var stageEl = document.getElementById('builder-stage');
    var previewEl = document.getElementById('builder-preview');
    var previewContent = document.getElementById('builder-preview-content');
    var previewToggle = document.getElementById('builder-preview-toggle');
    var statusEl = document.getElementById('builder-save-state');
    var titleEl = document.getElementById('builder-title');
    var publishButton = document.getElementById('builder-publish');
    var publicEl = document.getElementById('builder-public');
    var messageEl = document.getElementById('builder-edit-message');
    var initialEl = document.getElementById('builder-initial-document');
    var documentState = JSON.parse(initialEl.textContent || '{"version":1,"blocks":[]}');
    var saveTimer = null;
    var previewTimer = null;
    var draggedIndex = null;
    var publishing = false;
    var draftController = null;

    var defaults = {
        heading: function () { return { type: 'heading', level: 2, text: 'A clear heading' }; },
        text: function () { return { type: 'text', text: 'Write something useful here.' }; },
        image: function () { return { type: 'image', url: '', alt: '', caption: '' }; },
        youtube: function () { return { type: 'youtube', url: '', caption: '' }; },
        button: function () { return { type: 'button', label: 'Learn more', url: '/', style: 'primary' }; },
        callout: function () { return { type: 'callout', title: 'Good to know', text: 'Highlight an important detail.', tone: 'info' }; },
        list: function () { return { type: 'list', ordered: false, items: ['First item', 'Second item'] }; },
        columns: function () { return { type: 'columns', columns: ['Left column', 'Right column'] }; },
        divider: function () { return { type: 'divider' }; },
        spacer: function () { return { type: 'spacer' }; }
    };

    function csrfToken() {
        var meta = document.querySelector('meta[name="csrf-token"]');
        return meta ? meta.content : '';
    }

    function setStatus(text, kind) {
        statusEl.textContent = text;
        statusEl.className = 'builder-save-state' + (kind ? ' is-' + kind : '');
    }

    function input(label, value, key, options) {
        options = options || {};
        var wrapper = document.createElement('label');
        wrapper.textContent = label;
        var field = document.createElement(options.multiline ? 'textarea' : 'input');
        field.className = 'input';
        field.value = value || '';
        field.dataset.key = key;
        if (options.type) field.type = options.type;
        if (options.placeholder) field.placeholder = options.placeholder;
        wrapper.appendChild(field);
        return wrapper;
    }

    function selectField(label, value, key, values) {
        var wrapper = document.createElement('label');
        wrapper.textContent = label;
        var field = document.createElement('select');
        field.className = 'input';
        field.dataset.key = key;
        values.forEach(function (item) {
            var option = document.createElement('option');
            option.value = item[0];
            option.textContent = item[1];
            option.selected = String(value) === String(item[0]);
            field.appendChild(option);
        });
        wrapper.appendChild(field);
        return wrapper;
    }

    function renderFields(container, block, index) {
        if (block.type === 'heading') {
            container.appendChild(selectField('Level', block.level, 'level', [[1, 'Hero heading'], [2, 'Section heading'], [3, 'Small heading']]));
            container.appendChild(input('Text', block.text, 'text'));
        } else if (block.type === 'text') {
            container.appendChild(input('Text', block.text, 'text', { multiline: true }));
        } else if (block.type === 'image') {
            var upload = document.createElement('div');
            upload.className = 'builder-image-upload';
            if (block.url) {
                var image = document.createElement('img');
                image.src = block.url;
                image.alt = '';
                upload.appendChild(image);
            }
            var file = document.createElement('input');
            file.type = 'file';
            file.accept = 'image/png,image/jpeg,image/gif,image/webp';
            file.dataset.imageUpload = String(index);
            upload.appendChild(file);
            container.appendChild(upload);
            container.appendChild(input('Alternative text', block.alt, 'alt', { placeholder: 'Describe the image' }));
            container.appendChild(input('Caption', block.caption, 'caption'));
        } else if (block.type === 'youtube') {
            container.appendChild(input('YouTube URL', block.url, 'url', { type: 'url', placeholder: 'https://youtube.com/watch?v=...' }));
            container.appendChild(input('Caption', block.caption, 'caption'));
        } else if (block.type === 'button') {
            var row = document.createElement('div');
            row.className = 'builder-inline-fields';
            row.appendChild(input('Label', block.label, 'label'));
            row.appendChild(selectField('Style', block.style, 'style', [['primary', 'Primary'], ['outline', 'Outline']]));
            container.appendChild(row);
            container.appendChild(input('Destination', block.url, 'url', { placeholder: '/page/example or https://...' }));
        } else if (block.type === 'callout') {
            container.appendChild(input('Title', block.title, 'title'));
            container.appendChild(input('Message', block.text, 'text', { multiline: true }));
            container.appendChild(selectField('Tone', block.tone, 'tone', [['info', 'Information'], ['success', 'Success'], ['warning', 'Warning']]));
        } else if (block.type === 'list') {
            container.appendChild(selectField('List style', block.ordered ? 'ordered' : 'bullet', 'listStyle', [['bullet', 'Bullets'], ['ordered', 'Numbered']]));
            container.appendChild(input('Items (one per line)', (block.items || []).join('\n'), 'itemsText', { multiline: true }));
        } else if (block.type === 'columns') {
            (block.columns || []).forEach(function (column, columnIndex) {
                container.appendChild(input('Column ' + (columnIndex + 1), column, 'column:' + columnIndex, { multiline: true }));
            });
        } else {
            var note = document.createElement('small');
            note.textContent = block.type === 'divider' ? 'A subtle horizontal separator.' : 'Adds breathing room between sections.';
            container.appendChild(note);
        }
    }

    function render() {
        blocksEl.textContent = '';
        emptyEl.hidden = documentState.blocks.length > 0;
        documentState.blocks.forEach(function (block, index) {
            var card = document.createElement('article');
            card.className = 'builder-block';
            card.draggable = true;
            card.dataset.index = String(index);
            var handle = document.createElement('div');
            handle.className = 'builder-block-handle';
            handle.title = 'Drag to reorder';
            handle.innerHTML = '<span aria-hidden="true">::</span>';
            var body = document.createElement('div');
            body.className = 'builder-block-body';
            var header = document.createElement('div');
            header.className = 'builder-block-header';
            var name = document.createElement('strong');
            name.textContent = block.type;
            var actions = document.createElement('div');
            actions.className = 'builder-block-actions';
            [['up', 'Move up', '↑'], ['down', 'Move down', '↓'], ['remove', 'Remove', '×']].forEach(function (action) {
                var button = document.createElement('button');
                button.type = 'button';
                button.dataset.action = action[0];
                button.title = action[1];
                button.setAttribute('aria-label', action[1]);
                button.textContent = action[2];
                actions.appendChild(button);
            });
            header.appendChild(name);
            header.appendChild(actions);
            var fields = document.createElement('div');
            fields.className = 'builder-fields';
            renderFields(fields, block, index);
            body.appendChild(header);
            body.appendChild(fields);
            card.appendChild(handle);
            card.appendChild(body);
            blocksEl.appendChild(card);
        });
    }

    function addBlock(type, index) {
        if (!defaults[type]) return;
        var target = typeof index === 'number' ? index : documentState.blocks.length;
        documentState.blocks.splice(target, 0, defaults[type]());
        render();
        changed();
    }

    function changed() {
        setStatus('Unsaved changes');
        window.clearTimeout(saveTimer);
        saveTimer = window.setTimeout(saveDraft, 900);
        if (!previewEl.hidden) {
            window.clearTimeout(previewTimer);
            previewTimer = window.setTimeout(updatePreview, 350);
        }
    }

    function postJson(url, data) {
        return fetch(url, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken() },
            body: JSON.stringify(data)
        }).then(function (response) {
            return response.json().catch(function () { return { error: 'The server returned an invalid response.' }; })
                .then(function (body) {
                    if (!response.ok) throw new Error(body.error || 'Request failed.');
                    return body;
                });
        });
    }

    function saveDraft() {
        if (publishing) return Promise.resolve();
        if (draftController) draftController.abort();
        draftController = new AbortController();
        setStatus('Saving...');
        return fetch(root.dataset.draftUrl, {
            method: 'POST',
            signal: draftController.signal,
            headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken() },
            body: JSON.stringify({
                document: documentState,
                base_edited_at: root.dataset.baseEditedAt
            })
        }).then(function (response) {
            return response.json().then(function (body) {
                if (!response.ok) throw new Error(body.error || 'Draft save failed.');
                return body;
            });
        })
            .then(function () { setStatus('Draft saved', 'success'); })
            .catch(function (error) {
                if (error.name !== 'AbortError') setStatus(error.message, 'error');
            });
    }

    function updatePreview() {
        previewContent.innerHTML = '<p>Rendering preview...</p>';
        postJson(root.dataset.previewUrl, { document: documentState })
            .then(function (result) { previewContent.innerHTML = result.html; })
            .catch(function (error) { previewContent.textContent = error.message; });
    }

    document.querySelectorAll('.builder-palette-item').forEach(function (button) {
        button.addEventListener('click', function () { addBlock(button.dataset.blockType); });
        button.addEventListener('dragstart', function (event) {
            event.dataTransfer.setData('application/x-bananawiki-block', button.dataset.blockType);
            event.dataTransfer.effectAllowed = 'copy';
        });
    });

    blocksEl.addEventListener('input', function (event) {
        var card = event.target.closest('.builder-block');
        if (!card || !event.target.dataset.key) return;
        var block = documentState.blocks[Number(card.dataset.index)];
        var key = event.target.dataset.key;
        if (key === 'level') block.level = Number(event.target.value);
        else if (key === 'listStyle') block.ordered = event.target.value === 'ordered';
        else if (key === 'itemsText') block.items = event.target.value.split('\n').map(function (item) { return item.trim(); }).filter(Boolean);
        else if (key.indexOf('column:') === 0) block.columns[Number(key.split(':')[1])] = event.target.value;
        else block[key] = event.target.value;
        changed();
    });

    blocksEl.addEventListener('change', function (event) {
        if (!event.target.dataset.imageUpload || !event.target.files[0]) return;
        var index = Number(event.target.dataset.imageUpload);
        var targetBlock = documentState.blocks[index];
        var formData = new FormData();
        formData.append('file', event.target.files[0]);
        formData.append('csrf_token', csrfToken());
        setStatus('Uploading image...');
        fetch(root.dataset.uploadUrl, { method: 'POST', body: formData, headers: { 'X-CSRFToken': csrfToken() } })
            .then(function (response) { return response.json().then(function (body) { if (!response.ok) throw new Error(body.error || 'Upload failed.'); return body; }); })
            .then(function (body) {
                if (documentState.blocks.indexOf(targetBlock) === -1) return;
                targetBlock.url = body.url;
                render();
                changed();
            })
            .catch(function (error) { setStatus(error.message, 'error'); });
    });

    blocksEl.addEventListener('click', function (event) {
        var button = event.target.closest('button[data-action]');
        var card = event.target.closest('.builder-block');
        if (!button || !card) return;
        var index = Number(card.dataset.index);
        if (button.dataset.action === 'remove') documentState.blocks.splice(index, 1);
        if (button.dataset.action === 'up' && index > 0) documentState.blocks.splice(index - 1, 0, documentState.blocks.splice(index, 1)[0]);
        if (button.dataset.action === 'down' && index < documentState.blocks.length - 1) documentState.blocks.splice(index + 1, 0, documentState.blocks.splice(index, 1)[0]);
        render();
        changed();
    });

    blocksEl.addEventListener('dragstart', function (event) {
        var card = event.target.closest('.builder-block');
        if (!card) return;
        draggedIndex = Number(card.dataset.index);
        card.classList.add('is-dragging');
        event.dataTransfer.effectAllowed = 'move';
        event.dataTransfer.setData('text/plain', String(draggedIndex));
    });
    blocksEl.addEventListener('dragend', function () {
        draggedIndex = null;
        blocksEl.querySelectorAll('.is-dragging').forEach(function (item) { item.classList.remove('is-dragging'); });
    });
    stageEl.addEventListener('dragover', function (event) { event.preventDefault(); stageEl.classList.add('is-drag-over'); });
    stageEl.addEventListener('dragleave', function (event) { if (!stageEl.contains(event.relatedTarget)) stageEl.classList.remove('is-drag-over'); });
    stageEl.addEventListener('drop', function (event) {
        event.preventDefault();
        stageEl.classList.remove('is-drag-over');
        var targetCard = event.target.closest('.builder-block');
        var targetIndex = targetCard ? Number(targetCard.dataset.index) : documentState.blocks.length;
        var paletteType = event.dataTransfer.getData('application/x-bananawiki-block');
        if (paletteType) return addBlock(paletteType, targetIndex);
        if (draggedIndex === null || draggedIndex === targetIndex) return;
        var moved = documentState.blocks.splice(draggedIndex, 1)[0];
        if (draggedIndex < targetIndex) targetIndex -= 1;
        documentState.blocks.splice(targetIndex, 0, moved);
        render();
        changed();
    });

    previewToggle.addEventListener('click', function () {
        previewEl.hidden = !previewEl.hidden;
        previewToggle.setAttribute('aria-pressed', String(!previewEl.hidden));
        previewToggle.textContent = previewEl.hidden ? 'Show preview' : 'Hide preview';
        if (!previewEl.hidden) updatePreview();
    });
    titleEl.addEventListener('input', changed);

    publishButton.addEventListener('click', function () {
        publishing = true;
        window.clearTimeout(saveTimer);
        if (draftController) draftController.abort();
        publishButton.disabled = true;
        setStatus('Publishing...');
        postJson(root.dataset.publishUrl, {
            document: documentState,
            title: titleEl.value,
            edit_message: messageEl.value,
            public: publicEl ? publicEl.checked : false,
            base_edited_at: root.dataset.baseEditedAt
        }).then(function (result) {
            setStatus('Published', 'success');
            window.location.assign(result.redirect || root.dataset.viewUrl);
        }).catch(function (error) {
            publishing = false;
            publishButton.disabled = false;
            setStatus(error.message, 'error');
        });
    });

    document.addEventListener('keydown', function (event) {
        if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 's') {
            event.preventDefault();
            window.clearTimeout(saveTimer);
            saveDraft();
        }
    });

    render();
}());
