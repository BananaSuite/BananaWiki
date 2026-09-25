//  Accessibility panel


// Current in-memory preferences (set via initAccessibility)
var _a11yPrefs = {};
var _a11ySaveTimer = null;

function _postA11yPrefs(options) {
    options = options || {};
    return fetch('/api/accessibility', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-CSRFToken': getCsrfToken() },
        body: JSON.stringify(_a11yPrefs),
        keepalive: !!options.keepalive
    }).then(function(r) {
        return bwParseJsonResponse(r, _t('js.customize.save_failed'));
    }).catch(function(err) {
        if (!options.silent) {
            bwShowFlash(err.message || _t('js.customize.save_failed'), 'error');
        }
    });
}

function getThemeConfig() {
    var fallback = {
        default_mode: 'dark',
        palettes: {
            dark: {
                primary: '#8fa0d4',
                secondary: '#1e1e2c',
                accent: '#7e9ada',
                text: '#c8ccd8',
                sidebar: '#1a1a24',
                bg: '#16161f'
            },
            light: {
                primary: '#4b63b6',
                secondary: '#ffffff',
                accent: '#3553c7',
                text: '#202534',
                sidebar: '#e9edf5',
                bg: '#f6f7fb'
            }
        }
    };
    if (!window.BW_THEME_CONFIG || !window.BW_THEME_CONFIG.palettes) return fallback;
    return window.BW_THEME_CONFIG;
}

function getEffectiveThemeMode(prefs) {
    var themeConfig = getThemeConfig();
    var mode = prefs && prefs.theme_mode ? String(prefs.theme_mode).toLowerCase() : 'default';
    if (mode === 'dark' || mode === 'light') return mode;
    return themeConfig.default_mode === 'light' ? 'light' : 'dark';
}

function _getBackgroundImageUrl(value) {
    value = String(value || '').trim();
    if (!value) return '';
    if (/^backgrounds\/[0-9a-f]{32}\.jpe?g$/.test(value)) {
        return '/static/uploads/' + value;
    }
    if (/^\/static\/uploads\/backgrounds\/[0-9a-f]{32}\.jpe?g$/.test(value)) {
        return value;
    }
    return '';
}

function _cssUrl(value) {
    return 'url("' + String(value || '').replace(/\\/g, '\\\\').replace(/"/g, '\\"') + '")';
}

function saveA11ySetting(key, value) {
    _a11yPrefs[key] = value;
    if (_a11ySaveTimer) clearTimeout(_a11ySaveTimer);
    var options = arguments.length > 2 ? (arguments[2] || {}) : {};
    if (options.immediate) {
        return _postA11yPrefs({ keepalive: true });
    }
    return new Promise(function(resolve) {
        _a11ySaveTimer = setTimeout(function() {
            _a11ySaveTimer = null;
            resolve(_postA11yPrefs());
        }, 600);
    });
}

function _getSupportedInterfaceLanguages() {
    var root = document.documentElement;
    var raw = String(root.getAttribute('data-interface-languages') || 'en,it');
    var codes = raw.split(',').map(function(code) {
        return String(code || '').trim().toLowerCase();
    }).filter(function(code) {
        return !!code;
    });
    return codes.length ? codes : ['en', 'it'];
}

function _switchInterfaceLanguage(lang) {
    // Fetch translations for the new language and update all [data-i18n]
    // elements in place, without a page reload.
    fetch('/api/translations/' + encodeURIComponent(lang), {
        headers: { 'Accept': 'application/json' }
    }).then(function(r) {
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.json();
    }).then(function(data) {
        var translations = data.translations || {};
        // Merge into the global i18n dictionary
        window.__bw_i18n = window.__bw_i18n || {};
        Object.keys(translations).forEach(function(k) {
            window.__bw_i18n[k] = translations[k];
        });
        // Update all elements with data-i18n attribute
        document.querySelectorAll('[data-i18n]').forEach(function(el) {
            var key = el.getAttribute('data-i18n');
            if (key && translations[key]) {
                el.textContent = translations[key];
            }
        });
        // Update placeholder attributes for inputs with data-i18n-placeholder
        document.querySelectorAll('[data-i18n-placeholder]').forEach(function(el) {
            var key = el.getAttribute('data-i18n-placeholder');
            if (key && translations[key]) {
                el.placeholder = translations[key];
            }
        });
        // Update html lang attribute
        document.documentElement.lang = lang;
    }).catch(function() {
        // Silently ignore: server-rendered translations remain on next reload
    });
}

function applyA11yPrefs(prefs) {
    var root = document.documentElement;
    var themeConfig = getThemeConfig();
    var effectiveThemeMode = getEffectiveThemeMode(prefs);
    var palette = themeConfig.palettes[effectiveThemeMode] || themeConfig.palettes.dark;

    root.dataset.theme = effectiveThemeMode;
    root.style.setProperty('color-scheme', effectiveThemeMode);
    root.style.setProperty('--primary', palette.primary);
    root.style.setProperty('--secondary', palette.secondary);
    root.style.setProperty('--accent', palette.accent);
    root.style.setProperty('--text', palette.text);
    root.style.setProperty('--sidebar', palette.sidebar);
    root.style.setProperty('--bg', palette.bg);
    var colorSchemeMeta = document.querySelector('meta[name="color-scheme"]');
    if (colorSchemeMeta) colorSchemeMeta.setAttribute('content', effectiveThemeMode);

    // Font scale
    var scale = prefs.font_scale || 1.0;
    root.style.setProperty('--a11y-font-scale', scale);

    // Contrast: remove old classes and set new one
    for (var i = 0; i <= 5; i++) {
        document.body.classList.remove('a11y-contrast-' + i);
    }
    if (prefs.contrast > 0) {
        document.body.classList.add('a11y-contrast-' + prefs.contrast);
    }

    // Sidebar width
    var sidebar = document.getElementById('sidebar');
    if (sidebar && prefs.sidebar_width && prefs.sidebar_width >= 180 && prefs.sidebar_width <= 500) {
        sidebar.style.width = prefs.sidebar_width + 'px';
        sidebar.style.minWidth = prefs.sidebar_width + 'px';
    }

    // Content max width
    if (prefs.content_max_width && prefs.content_max_width > 0) {
        root.style.setProperty('--content-max-width', prefs.content_max_width + 'px');
    } else {
        root.style.removeProperty('--content-max-width');
    }

    // Editor pane horizontal split
    var editorPane = document.querySelector('.editor-pane');
    var previewPane = document.querySelector('.preview-pane');
    if (editorPane && previewPane) {
        if (prefs.editor_pane_width > 0) {
            editorPane.style.flex = 'none';
            editorPane.style.width = prefs.editor_pane_width + '%';
            previewPane.style.flex = 'none';
            previewPane.style.width = (100 - prefs.editor_pane_width) + '%';
        } else {
            editorPane.style.flex = '';
            editorPane.style.width = '';
            previewPane.style.flex = '';
            previewPane.style.width = '';
        }
    }

    // Editor container height
    var editorContainer = document.querySelector('.editor-container');
    if (editorContainer) {
        if (prefs.editor_height > 0) {
            editorContainer.style.minHeight = prefs.editor_height + 'px';
            editorContainer.style.height = prefs.editor_height + 'px';
        } else {
            editorContainer.style.minHeight = '';
            editorContainer.style.height = '';
        }
    }

    // Custom CSS colors
    if (prefs.custom_bg) {
        root.style.setProperty('--bg', prefs.custom_bg);
    } else {
        root.style.setProperty('--bg', palette.bg);
    }
    if (prefs.custom_text) {
        root.style.setProperty('--text', prefs.custom_text);
    } else {
        root.style.setProperty('--text', palette.text);
    }
    if (prefs.custom_primary) {
        root.style.setProperty('--primary', prefs.custom_primary);
    } else {
        root.style.setProperty('--primary', palette.primary);
    }
    if (prefs.custom_secondary) {
        root.style.setProperty('--secondary', prefs.custom_secondary);
    } else {
        root.style.setProperty('--secondary', palette.secondary);
    }
    if (prefs.custom_accent) {
        root.style.setProperty('--accent', prefs.custom_accent);
    } else {
        root.style.setProperty('--accent', palette.accent);
    }
    if (prefs.custom_sidebar) {
        root.style.setProperty('--sidebar', prefs.custom_sidebar);
    } else {
        root.style.setProperty('--sidebar', palette.sidebar);
    }
    var bgImageUrl = _getBackgroundImageUrl(prefs.background_image);
    root.style.setProperty('--bg-image', bgImageUrl ? _cssUrl(bgImageUrl) : 'none');

    // Line height: multiplier applied to body & .wiki-content so the
    // setting is visible site-wide instead of only inside wiki articles.
    var lineHeightMap = ['1', '1.222', '1.444'];
    var lhIdx = prefs.line_height || 0;
    root.style.setProperty('--a11y-line-height-mul', lineHeightMap[lhIdx] || '1');

    // Letter spacing
    var letterSpacingMap = ['normal', '0.04em', '0.08em'];
    var lsIdx = prefs.letter_spacing || 0;
    root.style.setProperty('--a11y-letter-spacing', letterSpacingMap[lsIdx] || 'normal');

    // Reduce motion
    if (prefs.reduce_motion) {
        document.body.classList.add('a11y-reduce-motion');
    } else {
        document.body.classList.remove('a11y-reduce-motion');
    }

    // Semantic Highlighting: toggle body classes such as a11y-sem-bold-2
    var semKeys = ['bold', 'italic', 'code', 'link', 'heading'];
    semKeys.forEach(function(key) {
        var lvl = parseInt(prefs['semantic_' + key], 10);
        if (lvl !== 1 && lvl !== 2) lvl = 0;
        document.body.classList.remove('a11y-sem-' + key + '-1');
        document.body.classList.remove('a11y-sem-' + key + '-2');
        if (lvl > 0) {
            document.body.classList.add('a11y-sem-' + key + '-' + lvl);
        }
    });

    // Interface language
    var supportedLangs = _getSupportedInterfaceLanguages();
    var siteLang = String(root.getAttribute('data-site-lang') || 'en').toLowerCase();
    if (supportedLangs.indexOf(siteLang) === -1) siteLang = 'en';
    var langPref = String((prefs && prefs.interface_language) || 'default').toLowerCase();
    var effectiveLang = (supportedLangs.indexOf(langPref) !== -1) ? langPref : siteLang;
    root.setAttribute('lang', effectiveLang);
}

function _rgbToHex(color) {
    // Convert computed color (rgb or hex) to #rrggbb for color input.
    // Returns null when the value cannot be parsed so callers can skip
    // setting the input value and avoid accidentally persisting a fallback
    // black (#000000) that would turn the entire site black.
    if (!color) return null;
    if (color.charAt(0) === '#') {
        var hex = color.toLowerCase();
        if (/^#[0-9a-f]{6}$/.test(hex)) return hex;
        if (/^#[0-9a-f]{3}$/.test(hex)) {
            return '#' + hex.slice(1).split('').map(function(ch) { return ch + ch; }).join('');
        }
        if (/^#[0-9a-f]{8}$/.test(hex)) return hex.slice(0, 7);
        return null;
    }
    var m = color.match(/^rgb\((\d+),\s*(\d+),\s*(\d+)\)$/);
    if (!m) return null;
    return '#' + [m[1], m[2], m[3]].map(function(n) {
        return ('0' + parseInt(n, 10).toString(16)).slice(-2);
    }).join('');
}

// Rebuild the server-rendered <style id="a11y-style"> element to reflect the
// current _a11yPrefs custom-color state.  This must be called whenever a
// custom color is cleared so that the stale server-rendered CSS rule (e.g.
// --bg:#000) no longer overrides the site's default, which would otherwise
// keep the site black even after the user clicks the ✕ clear button.
function _syncA11yStyleBlock() {
    var el = document.getElementById('a11y-style');
    if (!el) return;
    var rules = [];
    if (_a11yPrefs.custom_bg)        rules.push('--bg:'        + _a11yPrefs.custom_bg);
    if (_a11yPrefs.custom_text)      rules.push('--text:'      + _a11yPrefs.custom_text);
    if (_a11yPrefs.custom_primary)   rules.push('--primary:'   + _a11yPrefs.custom_primary);
    if (_a11yPrefs.custom_secondary) rules.push('--secondary:' + _a11yPrefs.custom_secondary);
    if (_a11yPrefs.custom_accent)    rules.push('--accent:'    + _a11yPrefs.custom_accent);
    if (_a11yPrefs.custom_sidebar)   rules.push('--sidebar:'   + _a11yPrefs.custom_sidebar);
    var bgImageUrl = _getBackgroundImageUrl(_a11yPrefs.background_image);
    if (bgImageUrl) rules.push('--bg-image:' + _cssUrl(bgImageUrl));
    el.textContent = rules.length ? ':root{' + rules.join(';') + '}' : '';
}

function initAccessibility(prefs) {
    _a11yPrefs = prefs || {};

    // Apply stored prefs immediately
    applyA11yPrefs(_a11yPrefs);

    var panel = document.getElementById('a11y-panel');
    var overlay = document.getElementById('a11y-overlay');
    var toggleBtn = document.getElementById('a11y-toggle-btn');
    var closeBtn = document.getElementById('a11y-close-btn');
    if (!panel || !toggleBtn) return;

    function openPanel() {
        panel.classList.add('a11y-panel-open');
        if (overlay) overlay.classList.add('active');
        // Sync UI controls to current prefs
        syncPanelUI();
    }

    function closePanel() {
        panel.classList.remove('a11y-panel-open');
        if (overlay) overlay.classList.remove('active');
    }

    toggleBtn.addEventListener('click', function() {
        if (panel.classList.contains('a11y-panel-open')) {
            closePanel();
        } else {
            openPanel();
        }
    });

    // Mobile account menu customize button
    var mobileToggleBtn = document.getElementById('mobile-a11y-toggle-btn');
    if (mobileToggleBtn) {
        mobileToggleBtn.addEventListener('click', function() {
            var menu = document.getElementById('mobile-account-menu');
            var menuToggle = document.getElementById('mobile-account-menu-toggle');
            if (menu) menu.hidden = true;
            if (menuToggle) menuToggle.setAttribute('aria-expanded', 'false');
            openPanel();
        });
    }

    if (closeBtn) closeBtn.addEventListener('click', closePanel);
    if (overlay) overlay.addEventListener('click', closePanel);

    // Close on Escape
    document.addEventListener('keydown', function(e) {
        if (e.key === 'Escape' && panel.classList.contains('a11y-panel-open')) {
            closePanel();
        }
    });

    // Theme mode buttons
    panel.querySelectorAll('.a11y-theme-btn').forEach(function(btn) {
        btn.addEventListener('click', function() {
            var mode = btn.dataset.themeMode || 'default';
            _a11yPrefs.theme_mode = mode;
            // Clear custom colors so the new theme palette takes effect
            // instead of being overridden by colors from the previous theme.
            var colorKeys = ['custom_bg', 'custom_text', 'custom_primary',
                             'custom_secondary', 'custom_accent', 'custom_sidebar'];
            colorKeys.forEach(function(key) { _a11yPrefs[key] = ''; });
            applyA11yPrefs(_a11yPrefs);
            _syncA11yStyleBlock();
            syncThemeBtns();
            syncColorInputs();
            // Clear active preset highlight
            if (presetGrid) presetGrid.querySelectorAll('.a11y-preset-swatch').forEach(function(b) { b.classList.remove('active'); });
            // Save theme mode and cleared colors in one request
            if (_a11ySaveTimer) clearTimeout(_a11ySaveTimer);
            _a11ySaveTimer = setTimeout(function() {
                fetch('/api/accessibility', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json', 'X-CSRFToken': getCsrfToken() },
                    body: JSON.stringify(_a11yPrefs)
                }).then(function(r) {
                    return bwParseJsonResponse(r, _t('js.customize.save_failed'));
                }).catch(function(err) {
                    bwShowFlash(err.message || _t('js.customize.save_failed'), 'error');
                });
            }, 300);
        });
    });

    // Interface language selector: real-time update without page reload.
    // Fetches translations from the server and updates all [data-i18n] elements.
    var languageSelect = document.getElementById('a11y-language-select');
    if (languageSelect) {
        languageSelect.addEventListener('change', function() {
            var supportedLangs = _getSupportedInterfaceLanguages();
            var value = String(languageSelect.value || 'default').toLowerCase();
            if (value !== 'default' && supportedLangs.indexOf(value) === -1) value = 'default';
            var previous = _a11yPrefs.interface_language || 'default';
            _a11yPrefs.interface_language = value;
            applyA11yPrefs(_a11yPrefs);
            var saveResult = saveA11ySetting('interface_language', value, { immediate: true });
            if (previous !== value) {
                _switchInterfaceLanguage(value === 'default' ? (document.documentElement.dataset.siteLang || 'en') : value);
            }
        });
    }

    // Font size buttons
    panel.querySelectorAll('.a11y-font-btn').forEach(function(btn) {
        btn.addEventListener('click', function() {
            var scale = parseFloat(btn.dataset.scale);
            _a11yPrefs.font_scale = scale;
            applyA11yPrefs(_a11yPrefs);
            saveA11ySetting('font_scale', scale);
            syncFontBtns();
        });
    });

    // Contrast buttons
    panel.querySelectorAll('.a11y-contrast-btn').forEach(function(btn) {
        btn.addEventListener('click', function() {
            var level = parseInt(btn.dataset.contrast, 10);
            _a11yPrefs.contrast = level;
            applyA11yPrefs(_a11yPrefs);
            saveA11ySetting('contrast', level);
            syncContrastBtns();
        });
    });

    // Line height buttons
    panel.querySelectorAll('.a11y-line-btn').forEach(function(btn) {
        btn.addEventListener('click', function() {
            var idx = parseInt(btn.dataset.lineHeight, 10);
            _a11yPrefs.line_height = idx;
            applyA11yPrefs(_a11yPrefs);
            saveA11ySetting('line_height', idx);
            syncLineBtns();
        });
    });

    // Letter spacing buttons
    panel.querySelectorAll('.a11y-spacing-btn').forEach(function(btn) {
        btn.addEventListener('click', function() {
            var idx = parseInt(btn.dataset.spacing, 10);
            _a11yPrefs.letter_spacing = idx;
            applyA11yPrefs(_a11yPrefs);
            saveA11ySetting('letter_spacing', idx);
            syncSpacingBtns();
        });
    });

    // Reduce motion toggle
    var motionToggle = document.getElementById('a11y-motion-toggle');
    if (motionToggle) {
        motionToggle.addEventListener('change', function() {
            var val = motionToggle.checked ? 1 : 0;
            _a11yPrefs.reduce_motion = val;
            applyA11yPrefs(_a11yPrefs);
            saveA11ySetting('reduce_motion', val);
        });
    }

    // Semantic Highlighting buttons (Bold / Italic / Code / Links / Headings)
    panel.querySelectorAll('.a11y-semantic-btn').forEach(function(btn) {
        btn.addEventListener('click', function() {
            var key = btn.dataset.semantic;
            var level = parseInt(btn.dataset.level, 10);
            if (level !== 1 && level !== 2) level = 0;
            var prefKey = 'semantic_' + key;
            _a11yPrefs[prefKey] = level;
            applyA11yPrefs(_a11yPrefs);
            saveA11ySetting(prefKey, level);
            syncSemanticBtns();
        });
    });

    // Color inputs
    var colorMap = {
        'bg': { input: document.getElementById('a11y-color-bg'), prop: 'custom_bg', cssVar: '--bg' },
        'text': { input: document.getElementById('a11y-color-text'), prop: 'custom_text', cssVar: '--text' },
        'primary': { input: document.getElementById('a11y-color-primary'), prop: 'custom_primary', cssVar: '--primary' },
        'secondary': { input: document.getElementById('a11y-color-secondary'), prop: 'custom_secondary', cssVar: '--secondary' },
        'accent': { input: document.getElementById('a11y-color-accent'), prop: 'custom_accent', cssVar: '--accent' },
        'sidebar': { input: document.getElementById('a11y-color-sidebar'), prop: 'custom_sidebar', cssVar: '--sidebar' },
    };

    // Sync color inputs immediately so they reflect the actual theme colours
    // instead of the browser default #000000.  This MUST happen before event
    // listeners are attached to prevent Firefox session-restore from firing
    // "input" events with a stale #000000 value that would turn the site black.
    syncColorInputs();

    Object.keys(colorMap).forEach(function(key) {
        var entry = colorMap[key];
        if (!entry.input) return;
        entry.input.addEventListener('input', function() {
            _a11yPrefs[entry.prop] = entry.input.value;
            applyA11yPrefs(_a11yPrefs);
            saveA11ySetting(entry.prop, entry.input.value);
        });
    });

    // Clear buttons
    panel.querySelectorAll('.a11y-color-clear').forEach(function(btn) {
        btn.addEventListener('click', function() {
            var target = btn.dataset.target;
            var prop = 'custom_' + target;
            _a11yPrefs[prop] = '';
            applyA11yPrefs(_a11yPrefs);
            saveA11ySetting(prop, '');
            // Rebuild the server-rendered <style id="a11y-style"> so the stale
            // CSS rule is removed immediately and the site colour reverts to the
            // site default instead of remaining black until the next page load.
            _syncA11yStyleBlock();
            syncColorInputs();
        });
    });

    var bgImageInput = document.getElementById('a11y-bg-image-input');
    var bgImageUploadBtn = document.getElementById('a11y-bg-image-upload');
    var bgImageClearBtn = document.getElementById('a11y-bg-image-clear');

    if (bgImageUploadBtn && bgImageInput) {
        bgImageUploadBtn.addEventListener('click', function() {
            bgImageInput.click();
        });
        bgImageInput.addEventListener('change', function() {
            var file = bgImageInput.files && bgImageInput.files[0];
            if (!file) return;
            var formData = new FormData();
            formData.append('file', file);
            bgImageUploadBtn.disabled = true;
            fetch('/api/accessibility/background', {
                method: 'POST',
                headers: { 'X-CSRFToken': getCsrfToken() },
                body: formData
            }).then(function(r) {
                return bwParseJsonResponse(r, _t('js.customize.background_upload_failed'));
            }).then(function(d) {
                _a11yPrefs.background_image = d.background_image || '';
                applyA11yPrefs(_a11yPrefs);
                _syncA11yStyleBlock();
                syncBackgroundImageControls();
                bwShowFlash(_t('js.customize.background_upload_success'), 'success');
            }).catch(function(err) {
                bwShowFlash(err.message || _t('js.customize.background_upload_failed'), 'error');
            }).finally(function() {
                bgImageInput.value = '';
                bgImageUploadBtn.disabled = false;
            });
        });
    }

    if (bgImageClearBtn) {
        bgImageClearBtn.addEventListener('click', function() {
            fetch('/api/accessibility/background', {
                method: 'DELETE',
                headers: { 'X-CSRFToken': getCsrfToken() }
            }).then(function(r) {
                return bwParseJsonResponse(r, _t('js.customize.background_clear_failed'));
            }).then(function() {
                _a11yPrefs.background_image = '';
                applyA11yPrefs(_a11yPrefs);
                _syncA11yStyleBlock();
                syncBackgroundImageControls();
                bwShowFlash(_t('js.customize.background_clear_success'), 'success');
            }).catch(function(err) {
                bwShowFlash(err.message || _t('js.customize.background_clear_failed'), 'error');
            });
        });
    }

    // Color presets
    var _colorPresets = {
        ocean: {
            dark:  { custom_bg: '#0b1a2e', custom_text: '#c8ddf0', custom_primary: '#5b9bd5', custom_secondary: '#112640', custom_accent: '#a3c4f3', custom_sidebar: '#091526' },
            light: { custom_bg: '#f3f8fd', custom_text: '#17324a', custom_primary: '#256fa8', custom_secondary: '#ffffff', custom_accent: '#4f90c7', custom_sidebar: '#dbeaf7' }
        },
        forest: {
            dark:  { custom_bg: '#0f1e12', custom_text: '#c8dcc8', custom_primary: '#4caf50', custom_secondary: '#162a19', custom_accent: '#8fbf9f', custom_sidebar: '#0b180e' },
            light: { custom_bg: '#f2f8f1', custom_text: '#1f3a24', custom_primary: '#2f7d34', custom_secondary: '#ffffff', custom_accent: '#5c9a68', custom_sidebar: '#dcebdd' }
        },
        sunset: {
            dark:  { custom_bg: '#1f1017', custom_text: '#f0ddd0', custom_primary: '#e76f51', custom_secondary: '#2a1520', custom_accent: '#f4a261', custom_sidebar: '#1a0d14' },
            light: { custom_bg: '#fff4ed', custom_text: '#513026', custom_primary: '#c45136', custom_secondary: '#ffffff', custom_accent: '#de7d3c', custom_sidebar: '#f5dfd4' }
        },
        lavender: {
            dark:  { custom_bg: '#1a1428', custom_text: '#d8d0e8', custom_primary: '#9b7fd4', custom_secondary: '#221a34', custom_accent: '#c4b5e0', custom_sidebar: '#151020' },
            light: { custom_bg: '#f7f3fc', custom_text: '#32274a', custom_primary: '#7657b8', custom_secondary: '#ffffff', custom_accent: '#9a7acb', custom_sidebar: '#e7def4' }
        },
        midnight: {
            dark:  { custom_bg: '#0a0a12', custom_text: '#e0e0e8', custom_primary: '#00d4ff', custom_secondary: '#10101c', custom_accent: '#7ee8ff', custom_sidebar: '#08080e' },
            light: { custom_bg: '#f2f6fb', custom_text: '#202535', custom_primary: '#157ea3', custom_secondary: '#ffffff', custom_accent: '#2ca7c9', custom_sidebar: '#dce5ef' }
        },
        copper: {
            dark:  { custom_bg: '#1c1410', custom_text: '#e8dcd0', custom_primary: '#b87333', custom_secondary: '#241c16', custom_accent: '#d4a574', custom_sidebar: '#16100c' },
            light: { custom_bg: '#fbf3ed', custom_text: '#453028', custom_primary: '#9c5f29', custom_secondary: '#ffffff', custom_accent: '#bd814c', custom_sidebar: '#ecded4' }
        },
        rose: {
            dark:  { custom_bg: '#1a0e14', custom_text: '#f0d8e0', custom_primary: '#e91e8c', custom_secondary: '#240a18', custom_accent: '#f48fb1', custom_sidebar: '#140a10' },
            light: { custom_bg: '#fff1f6', custom_text: '#4c2636', custom_primary: '#bf2f77', custom_secondary: '#ffffff', custom_accent: '#df6796', custom_sidebar: '#f4d9e4' }
        },
        slate: {
            dark:  { custom_bg: '#0e1220', custom_text: '#d0d8f0', custom_primary: '#7c8fc4', custom_secondary: '#141826', custom_accent: '#a8b8e0', custom_sidebar: '#0a0e1a' },
            light: { custom_bg: '#f4f6fb', custom_text: '#253044', custom_primary: '#596b98', custom_secondary: '#ffffff', custom_accent: '#7688b4', custom_sidebar: '#e0e5f0' }
        },
        ember: {
            dark:  { custom_bg: '#1a0e08', custom_text: '#f0e0d0', custom_primary: '#ff6b2b', custom_secondary: '#221208', custom_accent: '#ffad7a', custom_sidebar: '#140a04' },
            light: { custom_bg: '#fff3ea', custom_text: '#4f2d20', custom_primary: '#c94f1d', custom_secondary: '#ffffff', custom_accent: '#e8793f', custom_sidebar: '#f3dfd0' }
        }
    };
    var presetGrid = document.getElementById('a11y-preset-grid');
    if (presetGrid) {
        presetGrid.querySelectorAll('.a11y-preset-swatch').forEach(function(btn) {
            btn.addEventListener('click', function() {
                var name = btn.dataset.preset;
                var presetSet = _colorPresets[name];
                var mode = getEffectiveThemeMode(_a11yPrefs);
                var preset = presetSet && (presetSet[mode] || presetSet.dark);
                if (!preset) return;
                Object.keys(preset).forEach(function(key) {
                    _a11yPrefs[key] = preset[key];
                });
                applyA11yPrefs(_a11yPrefs);
                _syncA11yStyleBlock();
                syncColorInputs();
                // Save all preset colors in one request
                if (_a11ySaveTimer) clearTimeout(_a11ySaveTimer);
                _a11ySaveTimer = setTimeout(function() {
                    fetch('/api/accessibility', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json', 'X-CSRFToken': getCsrfToken() },
                        body: JSON.stringify(_a11yPrefs)
                    }).then(function(r) {
                        return bwParseJsonResponse(r, _t('js.customize.save_failed'));
                    }).catch(function(err) {
                        bwShowFlash(err.message || _t('js.customize.save_failed'), 'error');
                    });
                }, 300);
                // Highlight active preset
                presetGrid.querySelectorAll('.a11y-preset-swatch').forEach(function(b) { b.classList.remove('active'); });
                btn.classList.add('active');
            });
        });
    }

    // Reset all button
    var resetBtn = document.getElementById('a11y-reset-btn');
    if (resetBtn) {
        resetBtn.addEventListener('click', function() {
            bwConfirm(_t('js.customize.reset_confirm'), function() {
            fetch('/api/accessibility/reset', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json', 'X-CSRFToken': getCsrfToken() },
                body: '{}'
            }).then(function(r) {
                return bwParseJsonResponse(r, _t('js.customize.reset_failed'));
            })
            .then(function(d) {
                var langChanged = false;
                if (d.defaults) {
                    var prevLang = _a11yPrefs && _a11yPrefs.interface_language;
                    var newLang = d.defaults.interface_language;
                    if (prevLang && newLang && prevLang !== newLang) {
                        langChanged = true;
                    }
                    _a11yPrefs = d.defaults;
                    applyA11yPrefs(_a11yPrefs);
                    syncPanelUI();
                    // Clear active preset highlight
                    if (presetGrid) presetGrid.querySelectorAll('.a11y-preset-swatch').forEach(function(b) { b.classList.remove('active'); });
                    // Also remove inline styles that were set by server-side rendering
                    var a11yStyle = document.getElementById('a11y-style');
                    if (a11yStyle) a11yStyle.remove();
                }
                bwShowFlash(d.message || _t('js.customize.reset_success'), 'success');
                if (langChanged) {
                    // Reload so server-rendered translations match the new language.
                    setTimeout(function() {
                        try { window.location.reload(); } catch (e) { /* ignore */ }
                    }, 250);
                }
            }).catch(function(err) {
                bwShowFlash(err.message || _t('js.customize.reset_failed'), 'error');
            });
            });
        });
    }

    window.addEventListener('beforeunload', function() {
        if (_a11ySaveTimer) {
            clearTimeout(_a11ySaveTimer);
            _a11ySaveTimer = null;
            _postA11yPrefs({ keepalive: true, silent: true });
        }
    });

    function syncFontBtns() {
        var scale = _a11yPrefs.font_scale || 1.0;
        panel.querySelectorAll('.a11y-font-btn').forEach(function(btn) {
            btn.classList.toggle('active', parseFloat(btn.dataset.scale) === scale);
        });
    }

    function syncThemeBtns() {
        var mode = (_a11yPrefs.theme_mode || 'default').toLowerCase();
        panel.querySelectorAll('.a11y-theme-btn').forEach(function(btn) {
            btn.classList.toggle('active', (btn.dataset.themeMode || 'default') === mode);
        });
        var hint = document.getElementById('a11y-theme-hint');
        if (hint) {
            var effective = getEffectiveThemeMode(_a11yPrefs);
            hint.textContent = _t('a11y.theme_hint', { mode: effective.charAt(0).toUpperCase() + effective.slice(1) });
        }
    }

    function syncContrastBtns() {
        var level = _a11yPrefs.contrast || 0;
        panel.querySelectorAll('.a11y-contrast-btn').forEach(function(btn) {
            btn.classList.toggle('active', parseInt(btn.dataset.contrast, 10) === level);
        });
    }

    function syncLineBtns() {
        var idx = _a11yPrefs.line_height || 0;
        panel.querySelectorAll('.a11y-line-btn').forEach(function(btn) {
            btn.classList.toggle('active', parseInt(btn.dataset.lineHeight, 10) === idx);
        });
    }

    function syncSpacingBtns() {
        var idx = _a11yPrefs.letter_spacing || 0;
        panel.querySelectorAll('.a11y-spacing-btn').forEach(function(btn) {
            btn.classList.toggle('active', parseInt(btn.dataset.spacing, 10) === idx);
        });
    }

    function syncMotionToggle() {
        var motionToggle = document.getElementById('a11y-motion-toggle');
        if (motionToggle) motionToggle.checked = !!(_a11yPrefs.reduce_motion);
    }

    function syncSemanticBtns() {
        panel.querySelectorAll('.a11y-semantic-btn').forEach(function(btn) {
            var key = btn.dataset.semantic;
            var level = parseInt(btn.dataset.level, 10);
            var current = parseInt(_a11yPrefs['semantic_' + key], 10);
            if (current !== 1 && current !== 2) current = 0;
            btn.classList.toggle('active', current === level);
        });
    }

    function syncLanguageSelect() {
        var languageSelect = document.getElementById('a11y-language-select');
        if (!languageSelect) return;
        var supportedLangs = _getSupportedInterfaceLanguages();
        var value = String(_a11yPrefs.interface_language || 'default').toLowerCase();
        if (value !== 'default' && supportedLangs.indexOf(value) === -1) value = 'default';
        languageSelect.value = value;
    }

    function syncColorInputs() {
        var root = document.documentElement;
        var computed = getComputedStyle(root);
        Object.keys(colorMap).forEach(function(key) {
            var entry = colorMap[key];
            if (!entry.input) return;
            var stored = _a11yPrefs[entry.prop];

            // If user has explicitly saved a color preference, use it
            if (stored) {
                var hex = _rgbToHex(stored);
                if (hex) {
                    entry.input.value = hex;
                    return;
                }
            }

            // Otherwise, try to read the current computed color from CSS
            var color = computed.getPropertyValue(entry.cssVar).trim();
            var hex = _rgbToHex(color);

            // Always initialize the input value to prevent stale browser defaults.
            // If we can't determine the color (hex is null), use a safe default
            // based on the color type to prevent accidentally saving inappropriate
            // colors that would make the site unreadable.
            if (hex) {
                entry.input.value = hex;
            } else {
                // Safe defaults: background=white, text=black, others=primary color
                var safeDefaults = {
                    'bg': '#ffffff',
                    'text': '#2d3748',
                    'primary': '#4299e1',
                    'secondary': '#4a5568',
                    'accent': '#ed8936',
                    'sidebar': '#f7fafc'
                };
                entry.input.value = safeDefaults[key] || '#4299e1';
            }
        });
    }

    function syncBackgroundImageControls() {
        var preview = document.getElementById('a11y-bg-image-preview');
        var clearBtn = document.getElementById('a11y-bg-image-clear');
        var bgImageUrl = _getBackgroundImageUrl(_a11yPrefs.background_image);
        if (preview) {
            preview.style.backgroundImage = bgImageUrl ? _cssUrl(bgImageUrl) : '';
            preview.classList.toggle('is-empty', !bgImageUrl);
        }
        if (clearBtn) {
            clearBtn.disabled = !bgImageUrl;
        }
    }

    function syncPanelUI() {
        syncThemeBtns();
        syncFontBtns();
        syncContrastBtns();
        syncLineBtns();
        syncSpacingBtns();
        syncMotionToggle();
        syncSemanticBtns();
        syncLanguageSelect();
        syncColorInputs();
        syncBackgroundImageControls();
    }
}
