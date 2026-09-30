/* Visual page builder. The document is plain JSON; the server validates, sanitises and renders it.
 * User text only ever reaches the DOM through textContent / input values; the only HTML inserted
 * is the preview the server rendered (every value in it escaped or sanitised by BananaWiki). */
(function () {
  "use strict";
  var root = document.getElementById("page-builder");
  if (!root || !window.BW) return;
  var BW = window.BW;
  var $ = function (id) { return document.getElementById(id); };
  var blocksEl = $("builder-blocks");
  var emptyEl = $("builder-empty");
  var stageEl = $("builder-stage");
  var previewEl = $("builder-preview");
  var previewContent = $("builder-preview-content");
  var frameEl = $("builder-device-frame");
  var editMode = $("builder-mode-edit");
  var previewMode = $("builder-mode-preview");
  var statusEl = $("builder-status");
  var titleEl = $("builder-title");
  var publishButton = $("builder-publish");
  var discardButton = $("builder-discard");
  var undoButton = $("builder-undo");
  var redoButton = $("builder-redo");
  var publicEl = $("builder-public");
  var messageEl = $("builder-edit-message");
  var checksEl = $("builder-checks");
  var checksEmpty = $("builder-checks-empty");
  var conflictEl = $("builder-conflict");
  var suggestions = $("builder-page-suggestions");
  var options = JSON.parse(document.querySelector("script#builder-options").textContent);
  var state = JSON.parse(document.querySelector("script#builder-initial-document").textContent || '{"version":2,"blocks":[]}');
  var baseRevision = Number(root.dataset.baseRevision || 0);
  // Wiki pages keep server-side drafts; custom pages have none and are saved explicitly.
  var hasDrafts = !!root.dataset.draftUrl;
  var saveTimer = null, draftRequest = null, publishing = false, dirty = false, stale = false;
  var sectionsEl = $("builder-sections");
  var sectionsEmpty = $("builder-sections-empty");
  var pasteButton = $("builder-paste");
  var embedTitles = {};
  var MAX_BLOCKS = 120;
  var ENVELOPE = "bananawiki-page-builder";
  var IMAGE_TYPES = ["image/png", "image/jpeg", "image/gif", "image/webp"];
  var selected = -1, dragged = null, dropIndex = null;
  var undoStack = [], redoStack = [], typingTimer = null;
  var collapsed = new WeakSet();
  var flagged = {};
  var MAX_HISTORY = 100;

  function tr(key, values) { return BW.t("page_builder." + key, values); }
  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }
  function clone(value) { return JSON.parse(JSON.stringify(value)); }

  // ── Block defaults ──────────────────────────────────────────────────────────
  function card(n) { return { title: tr("default.card") + " " + n, text: tr("default.card_text"), url: "", image: "", image_alt: "" }; }
  var defaults = {
    heading: function () { return { type: "heading", level: 2, text: tr("default.heading") }; },
    text: function () { return { type: "text", format: "markdown", text: tr("default.text") }; },
    list: function () { return { type: "list", ordered: false, items: [tr("default.item") + " 1", tr("default.item") + " 2"] }; },
    quote: function () { return { type: "quote", text: tr("default.quote"), cite: "" }; },
    callout: function () { return { type: "callout", title: tr("default.callout_title"), text: tr("default.callout_text"), tone: "info" }; },
    code: function () { return { type: "code", language: "", code: "" }; },
    table: function () {
      return { type: "table", header: true, caption: "", rows: [[tr("default.column") + " 1", tr("default.column") + " 2"], ["", ""]] };
    },
    image: function () { return { type: "image", url: "", alt: "", decorative: false, caption: "", size: "full" }; },
    gallery: function () { return { type: "gallery", columns: 3, images: [] }; },
    youtube: function () { return { type: "youtube", url: "", caption: "" }; },
    hero: function () {
      return { type: "hero", title: tr("default.hero_title"), text: tr("default.hero_text"), button_label: tr("default.button"),
        button_url: "/", image: "", image_alt: "", layout: { align: "center" } };
    },
    columns: function () { return { type: "columns", format: "markdown", columns: [tr("default.column") + " 1", tr("default.column") + " 2"] }; },
    cards: function () { return { type: "cards", columns: 3, items: [card(1), card(2), card(3)] }; },
    button: function () { return { type: "button", label: tr("default.button"), url: "/", style: "primary" }; },
    faq: function () { return { type: "faq", items: [{ question: tr("default.question"), answer: tr("default.answer") }] }; },
    divider: function () { return { type: "divider" }; },
    spacer: function () { return { type: "spacer" }; },
    pages: function () { return { type: "pages", source: "recent", slugs: [], category_id: null, limit: 6, display: "cards", excerpt: true }; },
    embed: function () { return { type: "embed", kind: options.embeds[0] || "canvas", ref: "" }; }
  };
  var itemDefaults = {
    images: function () { return { url: "", alt: "", caption: "" }; },
    items: function (block) {
      return block.type === "faq" ? { question: "", answer: "" } : { title: "", text: "", url: "", image: "", image_alt: "" };
    }
  };

  // ── Status, history ─────────────────────────────────────────────────────────
  function setStatus(text, kind) {
    statusEl.textContent = text;
    statusEl.className = "builder-status small" + (kind ? " builder-status--" + kind : " muted");
  }

  function updateHistoryButtons() {
    undoButton.disabled = !undoStack.length;
    redoButton.disabled = !redoStack.length;
  }

  // Call before changing `state`. Typing in one field is recorded as a single step.
  function remember(typing) {
    if (typing && typingTimer) {
      window.clearTimeout(typingTimer);
      typingTimer = window.setTimeout(function () { typingTimer = null; }, 1000);
      return;
    }
    undoStack.push(JSON.stringify(state));
    if (undoStack.length > MAX_HISTORY) undoStack.shift();
    redoStack = [];
    typingTimer = typing ? window.setTimeout(function () { typingTimer = null; }, 1000) : null;
    updateHistoryButtons();
  }

  function travel(from, to, message) {
    if (!from.length) return;
    to.push(JSON.stringify(state));
    state = JSON.parse(from.pop());
    typingTimer = null;
    selected = Math.min(selected, state.blocks.length - 1);
    render();
    changed();
    updateHistoryButtons();
    setStatus(message);
  }
  function undo() { travel(undoStack, redoStack, tr("status.undone")); }
  function redo() { travel(redoStack, undoStack, tr("status.redone")); }

  // ── Fields ──────────────────────────────────────────────────────────────────
  var fieldCounter = 0;
  function nextId() { return "builder-field-" + (++fieldCounter); }

  function get(block, path) {
    return path.split(".").reduce(function (value, key) { return value == null ? value : value[key]; }, block);
  }
  function set(block, path, value) {
    var keys = path.split(".");
    var target = keys.slice(0, -1).reduce(function (value, key) { return value[key]; }, block);
    target[keys[keys.length - 1]] = value;
  }

  // options: {choices, multiline, type ("text"|"url"|"number"|"checkbox"), maxLength, placeholder, hint, cast, rerender, code}
  function field(block, label, path, opts) {
    opts = opts || {};
    var value = get(block, path);
    var wrapper = el(opts.type === "checkbox" ? "label" : "div", opts.type === "checkbox" ? "check" : "field");
    var id = nextId();
    var input;
    if (opts.choices) {
      input = el("select");
      opts.choices.forEach(function (choice) {
        var option = el("option", "", choice[1]);
        option.value = String(choice[0]);
        option.selected = String(value) === String(choice[0]);
        input.appendChild(option);
      });
    } else if (opts.multiline) {
      input = el("textarea", opts.code ? "builder-code-input" : "");
      input.rows = opts.rows || 4;
      input.value = value == null ? "" : value;
      if (opts.code) input.spellcheck = false;
    } else {
      input = el("input");
      input.type = opts.type || "text";
      if (opts.type === "checkbox") input.checked = !!value;
      else input.value = value == null ? "" : value;
      if (opts.type === "number") { input.min = opts.min; input.max = opts.max; }
    }
    if (opts.maxLength) input.maxLength = opts.maxLength;
    if (opts.placeholder) input.placeholder = opts.placeholder;
    if (opts.list) input.setAttribute("list", opts.list);
    input.id = id;
    input.dataset.path = path;
    if (opts.cast) input.dataset.cast = opts.cast;
    if (opts.rerender) input.dataset.rerender = "1";
    if (opts.type === "checkbox") {
      wrapper.appendChild(input);
      wrapper.appendChild(el("span", "", label));
    } else {
      var labelEl = el("label", "", label);
      labelEl.htmlFor = id;
      wrapper.appendChild(labelEl);
      wrapper.appendChild(input);
    }
    if (opts.hint) {
      var hint = el("p", "hint", opts.hint);
      hint.id = id + "-hint";
      input.setAttribute("aria-describedby", hint.id);
      wrapper.appendChild(hint);
    }
    return wrapper;
  }

  function choices(prefix, values) {
    return values.map(function (value) { return [value, tr(prefix + "." + value)]; });
  }

  function smallButton(action, label, text, extra) {
    var button = el("button", "btn btn--ghost btn--small" + (text.length < 3 ? " btn--icon" : ""), text);
    button.type = "button";
    button.dataset.action = action;
    if (text !== label) {
      button.title = label;
      button.setAttribute("aria-label", label);
    }
    Object.keys(extra || {}).forEach(function (key) { button.dataset[key] = extra[key]; });
    return button;
  }

  function imageField(block, path, altPath, label) {
    var box = el("div", "builder-image-upload");
    var url = get(block, path);
    if (url) {
      var img = el("img");
      img.src = url;
      img.alt = "";
      box.appendChild(img);
    }
    var fileId = nextId();
    var fileLabel = el("label", "btn btn--small", url ? tr("field.replace_image") : tr("field.image"));
    fileLabel.htmlFor = fileId;
    var file = el("input", "visually-hidden");
    file.type = "file";
    file.id = fileId;
    file.accept = IMAGE_TYPES.join(",");
    file.dataset.upload = path;
    box.appendChild(fileLabel);
    box.appendChild(file);
    if (url) box.appendChild(smallButton("clear-image", tr("field.remove_image"), tr("field.remove_image"), { path: path }));
    var group = el("div", "builder-fields");
    group.appendChild(el("span", "builder-field-label", label));
    group.appendChild(box);
    if (altPath) {
      group.appendChild(field(block, tr("field.alt"), altPath, { maxLength: 300, hint: tr("hint.alt") }));
    }
    return group;
  }

  function itemList(block, container, listKey, labelKey, renderItem) {
    var list = block[listKey];
    list.forEach(function (item, i) {
      var fieldset = el("fieldset", "builder-item");
      fieldset.appendChild(el("legend", "", tr(labelKey) + " " + (i + 1)));
      renderItem(fieldset, listKey + "." + i, item);
      var actions = el("div", "cluster builder-item-actions");
      actions.appendChild(smallButton("item-up", tr("action.up"), "↑", { list: listKey, item: String(i) }));
      actions.appendChild(smallButton("item-down", tr("action.down"), "↓", { list: listKey, item: String(i) }));
      actions.appendChild(smallButton("item-remove", tr("action.remove"), "×", { list: listKey, item: String(i) }));
      fieldset.appendChild(actions);
      container.appendChild(fieldset);
    });
    container.appendChild(smallButton("item-add", tr("action.add_" + labelKey.split(".")[1]), tr("action.add_" + labelKey.split(".")[1]),
      { list: listKey }));
  }

  var formatChoices = function () { return choices("format", ["markdown", "plain"]); };
  var columnChoices = [[2, "2"], [3, "3"], [4, "4"]];

  function renderFields(container, block) {
    switch (block.type) {
      case "heading":
        container.appendChild(field(block, tr("field.level"), "level",
          { choices: [[1, tr("level.1")], [2, tr("level.2")], [3, tr("level.3")]], cast: "number" }));
        container.appendChild(field(block, tr("field.text"), "text", { maxLength: 300 }));
        break;
      case "text":
        container.appendChild(field(block, tr("field.format"), "format", { choices: formatChoices() }));
        container.appendChild(field(block, tr("field.text"), "text",
          { multiline: true, rows: 6, hint: block.format === "markdown" ? tr("hint.markdown") : "" }));
        break;
      case "list":
        container.appendChild(field(block, tr("field.list_style"), "ordered",
          { choices: [["false", tr("list.bullet")], ["true", tr("list.ordered")]], cast: "bool" }));
        container.appendChild(field(block, tr("field.items"), "items", { multiline: true, cast: "lines" }));
        break;
      case "quote":
        container.appendChild(field(block, tr("field.quote"), "text", { multiline: true, maxLength: 2000 }));
        container.appendChild(field(block, tr("field.cite"), "cite", { maxLength: 200 }));
        break;
      case "callout":
        container.appendChild(field(block, tr("field.title"), "title", { maxLength: 200 }));
        container.appendChild(field(block, tr("field.text"), "text", { multiline: true, maxLength: 3000 }));
        container.appendChild(field(block, tr("field.tone"), "tone", { choices: choices("tone", ["info", "success", "warning", "danger"]) }));
        break;
      case "code":
        container.appendChild(field(block, tr("field.language"), "language", { maxLength: 30, placeholder: "python, bash, json…" }));
        container.appendChild(field(block, tr("field.code"), "code", { multiline: true, rows: 8, code: true }));
        break;
      case "table":
        renderTable(container, block);
        break;
      case "image":
        container.appendChild(imageField(block, "url", null, tr("field.image_file")));
        container.appendChild(field(block, tr("field.decorative"), "decorative", { type: "checkbox", rerender: true }));
        if (!block.decorative) container.appendChild(field(block, tr("field.alt"), "alt", { maxLength: 300, hint: tr("hint.alt") }));
        container.appendChild(field(block, tr("field.caption"), "caption", { maxLength: 500 }));
        container.appendChild(field(block, tr("field.size"), "size", { choices: choices("size", ["full", "medium", "small"]) }));
        break;
      case "gallery":
        container.appendChild(field(block, tr("field.column_count"), "columns", { choices: columnChoices, cast: "number" }));
        itemList(block, container, "images", "item.image", function (set, path) {
          set.appendChild(imageField(block, path + ".url", path + ".alt", tr("field.image_file")));
          set.appendChild(field(block, tr("field.caption"), path + ".caption", { maxLength: 300 }));
        });
        break;
      case "youtube":
        container.appendChild(field(block, tr("field.youtube"), "url", { type: "url", placeholder: "https://www.youtube.com/watch?v=…" }));
        container.appendChild(field(block, tr("field.caption"), "caption", { maxLength: 500 }));
        break;
      case "hero":
        container.appendChild(field(block, tr("field.title"), "title", { maxLength: 200 }));
        container.appendChild(field(block, tr("field.text"), "text", { multiline: true, maxLength: 1000 }));
        container.appendChild(field(block, tr("field.label"), "button_label", { maxLength: 100 }));
        container.appendChild(field(block, tr("field.url"), "button_url", { placeholder: "/page/… https://…" }));
        container.appendChild(imageField(block, "image", block.image ? "image_alt" : null, tr("field.image_optional")));
        break;
      case "columns":
        container.appendChild(field(block, tr("field.column_count"), "columns.length", { choices: columnChoices, cast: "columns" }));
        container.appendChild(field(block, tr("field.format"), "format", { choices: formatChoices() }));
        block.columns.forEach(function (column, i) {
          container.appendChild(field(block, tr("field.column") + " " + (i + 1), "columns." + i, { multiline: true, maxLength: 5000 }));
        });
        break;
      case "cards":
        container.appendChild(field(block, tr("field.column_count"), "columns", { choices: columnChoices, cast: "number" }));
        itemList(block, container, "items", "item.card", function (set, path, item) {
          set.appendChild(field(block, tr("field.title"), path + ".title", { maxLength: 200 }));
          set.appendChild(field(block, tr("field.text"), path + ".text", { multiline: true, rows: 3, maxLength: 1000 }));
          set.appendChild(field(block, tr("field.link"), path + ".url", { placeholder: "/page/… https://…" }));
          set.appendChild(imageField(block, path + ".image", item.image ? path + ".image_alt" : null, tr("field.image_optional")));
        });
        break;
      case "button":
        container.appendChild(field(block, tr("field.label"), "label", { maxLength: 100 }));
        container.appendChild(field(block, tr("field.style"), "style", { choices: choices("style", ["primary", "outline"]) }));
        container.appendChild(field(block, tr("field.url"), "url", { placeholder: "/page/… https://…" }));
        break;
      case "faq":
        itemList(block, container, "items", "item.question", function (set, path) {
          set.appendChild(field(block, tr("field.question"), path + ".question", { maxLength: 300 }));
          set.appendChild(field(block, tr("field.answer"), path + ".answer", { multiline: true, rows: 3, maxLength: 5000, hint: tr("hint.markdown") }));
        });
        break;
      case "pages":
        renderPages(container, block);
        break;
      case "embed":
        renderEmbed(container, block);
        break;
      default:
        container.appendChild(el("p", "hint", tr("hint." + block.type)));
    }
  }

  function renderTable(container, block) {
    container.appendChild(field(block, tr("field.header_row"), "header", { type: "checkbox" }));
    container.appendChild(field(block, tr("field.caption"), "caption", { maxLength: 300 }));
    var wrap = el("div", "builder-table-editor");
    var table = el("table");
    block.rows.forEach(function (row, r) {
      var tr_ = el("tr");
      row.forEach(function (cell, c) {
        var td = el("td");
        var input = el("input");
        input.type = "text";
        input.value = cell;
        input.maxLength = 500;
        input.dataset.path = "rows." + r + "." + c;
        input.setAttribute("aria-label", tr("field.cell", { row: r + 1, column: c + 1 }));
        td.appendChild(input);
        tr_.appendChild(td);
      });
      table.appendChild(tr_);
    });
    wrap.appendChild(table);
    container.appendChild(wrap);
    var actions = el("div", "cluster");
    actions.appendChild(smallButton("row-add", tr("action.add_row"), tr("action.add_row")));
    actions.appendChild(smallButton("row-remove", tr("action.remove_row"), tr("action.remove_row")));
    actions.appendChild(smallButton("col-add", tr("action.add_column"), tr("action.add_column")));
    actions.appendChild(smallButton("col-remove", tr("action.remove_column"), tr("action.remove_column")));
    container.appendChild(actions);
  }

  function renderPages(container, block) {
    container.appendChild(field(block, tr("field.source"), "source",
      { choices: choices("source", ["recent", "category", "selected"]), rerender: true }));
    if (block.source === "selected") {
      container.appendChild(field(block, tr("field.slugs"), "slugs", { multiline: true, cast: "lines", hint: tr("hint.slugs") }));
      var finder = field(block, tr("field.find_page"), "", { list: "builder-page-suggestions", placeholder: tr("field.find_placeholder") });
      finder.querySelector("input").dataset.finder = "1";
      finder.querySelector("input").removeAttribute("data-path");
      container.appendChild(finder);
    } else {
      if (block.source === "category") {
        var categoryChoices = [["", tr("field.choose")]].concat(options.categories);
        container.appendChild(field(block, tr("field.category"), "category_id", { choices: categoryChoices, cast: "id" }));
      }
      container.appendChild(field(block, tr("field.limit"), "limit", { type: "number", min: 1, max: 24, cast: "number" }));
    }
    container.appendChild(field(block, tr("field.display"), "display", { choices: choices("display", ["cards", "list"]) }));
    container.appendChild(field(block, tr("field.excerpt"), "excerpt", { type: "checkbox" }));
  }

  // The embed picker: a combobox listing the canvases and boards this editor may open.
  function renderEmbed(container, block) {
    if (!options.embeds.length) {
      container.appendChild(el("p", "hint", tr("hint.embed_none")));
    } else {
      var current = el("p", "builder-embed-current");
      if (block.ref) {
        var known = embedTitles[block.kind + ":" + block.ref];
        current.appendChild(el("span", "badge", tr("embed." + block.kind)));
        current.appendChild(document.createTextNode(" " + (known || block.ref)));
      } else {
        current.textContent = tr("embed_picker.nothing");
      }
      container.appendChild(current);
      var id = nextId();
      var listId = id + "-list";
      var wrapper = el("div", "field builder-embed-picker");
      var label = el("label", "", tr("embed_picker.label"));
      label.htmlFor = id;
      var input = el("input");
      input.type = "search";
      input.id = id;
      input.maxLength = 100;
      input.autocomplete = "off";
      input.placeholder = tr("embed_picker.placeholder");
      input.dataset.embedSearch = "1";
      input.setAttribute("role", "combobox");
      input.setAttribute("aria-autocomplete", "list");
      input.setAttribute("aria-expanded", "false");
      input.setAttribute("aria-controls", listId);
      var list = el("ul", "builder-embed-options");
      list.id = listId;
      list.hidden = true;
      list.setAttribute("role", "listbox");
      list.setAttribute("aria-label", tr("embed_picker.results"));
      wrapper.appendChild(label);
      wrapper.appendChild(input);
      wrapper.appendChild(list);
      container.appendChild(wrapper);
    }
    var manual = el("details", "builder-embed-manual");
    manual.appendChild(el("summary", "", tr("embed_picker.manual")));
    manual.appendChild(field(block, tr("field.embed_kind"), "kind", { choices: choices("embed", ["canvas", "kanban"]) }));
    manual.appendChild(field(block, tr("field.embed_ref"), "ref", { maxLength: 200, hint: tr("hint.embed_" + block.kind) }));
    container.appendChild(manual);
  }

  function renderLayout(container, block) {
    var details = el("details", "builder-layout");
    details.appendChild(el("summary", "", tr("layout.title")));
    var grid = el("div", "builder-layout-grid");
    var layout = block.layout || {};
    Object.keys(options.layout).forEach(function (key) {
      var values = options.layout[key];
      var id = nextId();
      var wrapper = el("div", "field");
      var label = el("label", "", tr("layout." + key));
      label.htmlFor = id;
      var select = el("select");
      select.id = id;
      select.dataset.layout = key;
      values.forEach(function (value) {
        var option = el("option", "", tr("layout." + key + "." + value));
        option.value = value;
        option.selected = (layout[key] || values[0]) === value;
        select.appendChild(option);
      });
      wrapper.appendChild(label);
      wrapper.appendChild(select);
      grid.appendChild(wrapper);
    });
    details.appendChild(grid);
    container.appendChild(details);
  }

  function summary(block) {
    var text = block.text || block.title || block.label || block.ref || block.url || block.code ||
      (block.items && block.items[0] && (block.items[0].title || block.items[0].question || block.items[0])) || "";
    return typeof text === "string" ? text.split("\n")[0].slice(0, 80) : "";
  }

  // ── Rendering the stage ─────────────────────────────────────────────────────
  function render() {
    var focused = document.activeElement;
    var focusKey = focused && blocksEl.contains(focused) ? focusSignature(focused) : null;
    blocksEl.textContent = "";
    emptyEl.hidden = state.blocks.length > 0;
    var total = state.blocks.length;
    state.blocks.forEach(function (block, index) {
      var cardEl = el("article", "builder-block" + (index === selected ? " is-selected" : "") + (flagged[index] ? " is-flagged" : ""));
      cardEl.dataset.index = String(index);
      var titleId = "builder-block-title-" + index;
      cardEl.setAttribute("aria-labelledby", titleId);
      var header = el("div", "builder-block-header");
      var handle = smallButton("handle", tr("action.handle", { position: index + 1, total: total, type: tr("block." + block.type) }), "⠿");
      handle.classList.add("builder-handle");
      handle.setAttribute("aria-keyshortcuts", "ArrowUp ArrowDown Home End");
      var name = el("strong", "", tr("block." + block.type));
      name.id = titleId;
      var info = el("div", "builder-block-title");
      info.appendChild(handle);
      info.appendChild(name);
      if (flagged[index]) info.appendChild(el("span", "badge badge--warning", tr("check_badge")));
      var isCollapsed = collapsed.has(block);
      if (isCollapsed) info.appendChild(el("span", "builder-block-summary muted", summary(block)));
      var actions = el("div", "cluster builder-block-actions");
      var toggle = smallButton("collapse", tr(isCollapsed ? "action.expand" : "action.collapse"), isCollapsed ? "▸" : "▾");
      toggle.setAttribute("aria-expanded", String(!isCollapsed));
      actions.appendChild(toggle);
      actions.appendChild(smallButton("duplicate", tr("action.duplicate"), "⧉"));
      actions.appendChild(smallButton("copy", tr("action.copy"), "⎘"));
      actions.appendChild(smallButton("up", tr("action.up"), "↑"));
      actions.appendChild(smallButton("down", tr("action.down"), "↓"));
      actions.appendChild(smallButton("remove", tr("action.remove"), "×"));
      header.appendChild(info);
      header.appendChild(actions);
      cardEl.appendChild(header);
      if (!isCollapsed) {
        var fields = el("div", "builder-fields");
        renderFields(fields, block);
        cardEl.appendChild(fields);
        renderLayout(cardEl, block);
      }
      blocksEl.appendChild(cardEl);
    });
    if (focusKey) restoreFocus(focusKey);
  }

  function focusSignature(node) {
    var cardEl = node.closest(".builder-block");
    if (!cardEl) return null;
    return { index: cardEl.dataset.index, path: node.dataset.path, action: node.dataset.action,
      item: node.dataset.item, list: node.dataset.list, layout: node.dataset.layout };
  }

  function restoreFocus(signature) {
    var cardEl = blocksEl.querySelector('[data-index="' + signature.index + '"]');
    if (!cardEl) return;
    var candidates = cardEl.querySelectorAll("input, textarea, select, button");
    for (var i = 0; i < candidates.length; i++) {
      var node = candidates[i];
      if ((signature.path && node.dataset.path === signature.path) ||
          (signature.layout && node.dataset.layout === signature.layout) ||
          (signature.action && node.dataset.action === signature.action && node.dataset.item === signature.item &&
           node.dataset.list === signature.list)) {
        node.focus();
        return;
      }
    }
  }

  function focusBlock(index, selector) {
    var cardEl = blocksEl.querySelector('[data-index="' + index + '"]');
    if (!cardEl) return;
    var target = cardEl.querySelector(selector || "input, textarea, select") || cardEl.querySelector(".builder-handle");
    if (target) target.focus();
    cardEl.scrollIntoView({ block: "nearest" });
  }

  // ── Changes, drafts, preview ───────────────────────────────────────────────
  function changed() {
    dirty = true;
    setStatus(tr("status.unsaved"));
    window.clearTimeout(saveTimer);
    saveTimer = window.setTimeout(hasDrafts ? saveDraft : refreshPreview, 900);
  }

  function showConflict() {
    stale = true;
    conflictEl.hidden = false;
    publishButton.disabled = true;
  }

  function saveDraft() {
    if (publishing || stale) return;
    if (draftRequest) draftRequest.abort();
    draftRequest = new AbortController();
    setStatus(BW.t("saving"));
    BW.fetchJSON(root.dataset.draftUrl, {
      body: { document: state, base_revision: baseRevision }, signal: draftRequest.signal
    }).then(function () {
      dirty = false;
      setStatus(tr("status.draft_saved"), "success");
      refreshPreview();
    }).catch(function (error) {
      if (error.name === "AbortError") return;
      if (error.status === 409) showConflict();
      setStatus(error.message, "error");
    });
  }

  function refreshPreview() {
    var previewing = !previewEl.hidden;
    if (previewing) setStatus(tr("status.rendering"));
    BW.fetchJSON(root.dataset.previewUrl, { body: { document: state } })
      .then(function (result) {
        // Server-rendered markup: every value in it is escaped or sanitised by BananaWiki.
        previewContent.innerHTML = result.html;
        showEmbeds(!previewEl.hidden);
        showChecks(result.checks || []);
        if (previewing) setStatus(tr("status.preview_updated"));
      })
      .catch(function (error) {
        if (previewing) previewContent.textContent = error.message;
        else setStatus(error.message, "error");
      });
  }

  // Canvas and board placeholders become live previews (the published page itself, for this
  // user, in a same-origin frame) while the preview is shown, and inert labels otherwise.
  function showEmbeds(live) {
    previewContent.querySelectorAll(".bw-embed[data-embed-type][data-embed-ref]").forEach(function (placeholder) {
      var label = placeholder.getAttribute("data-label") || "";
      var box = el("div", "builder-embed-live");
      if (!live) {
        box.appendChild(el("p", "muted", label));
      } else {
        var frame = el("iframe", "builder-embed-frame");
        frame.title = tr("embed_picker.live", { label: label });
        frame.loading = "lazy";
        frame.src = root.dataset.embedFrameUrl + "?kind=" + encodeURIComponent(placeholder.getAttribute("data-embed-type")) +
          "&ref=" + encodeURIComponent(placeholder.getAttribute("data-embed-ref"));
        frame.addEventListener("load", function () { fitFrame(frame); });
        box.appendChild(frame);
      }
      placeholder.replaceWith(box);
    });
  }

  function fitFrame(frame) {
    var doc;
    try { doc = frame.contentDocument; } catch (e) { return; }
    var content = doc && doc.querySelector("main");
    if (!content) return;
    var fit = function () {
      frame.style.height = Math.min(Math.max(Math.ceil(content.getBoundingClientRect().height) + 16, 120), 1600) + "px";
    };
    fit();
    if (window.ResizeObserver) new ResizeObserver(fit).observe(content);
  }

  function showChecks(checks) {
    checksEl.textContent = "";
    checksEmpty.hidden = checks.length > 0;
    var next = {};
    checks.forEach(function (check) {
      next[check.block] = true;
      var item = el("li");
      var button = el("button", "builder-check", tr("check_block", { n: check.block + 1 }));
      button.type = "button";
      button.dataset.block = String(check.block);
      item.appendChild(button);
      item.appendChild(el("span", "", " " + check.message));
      checksEl.appendChild(item);
    });
    if (JSON.stringify(next) !== JSON.stringify(flagged)) {
      flagged = next;
      blocksEl.querySelectorAll(".builder-block").forEach(function (cardEl) {
        cardEl.classList.toggle("is-flagged", !!flagged[cardEl.dataset.index]);
        var title = cardEl.querySelector(".builder-block-title");
        var badge = title.querySelector(".badge");
        if (flagged[cardEl.dataset.index] && !badge) title.insertBefore(el("span", "badge badge--warning", tr("check_badge")), title.children[2] || null);
        if (!flagged[cardEl.dataset.index] && badge) badge.remove();
      });
    }
  }

  checksEl.addEventListener("click", function (event) {
    var button = event.target.closest("button[data-block]");
    if (!button) return;
    setMode(false);
    var index = Number(button.dataset.block);
    if (state.blocks[index] && collapsed.has(state.blocks[index])) {
      collapsed.delete(state.blocks[index]);
      render();
    }
    focusBlock(index);
  });

  // ── Structure changes ───────────────────────────────────────────────────────
  function insertAt() { return selected >= 0 && selected < state.blocks.length ? selected + 1 : state.blocks.length; }

  function addBlock(type, index) {
    if (!defaults[type]) return;
    remember();
    var at = typeof index === "number" ? index : insertAt();
    state.blocks.splice(at, 0, defaults[type]());
    selected = at;
    setMode(false);
    render();
    changed();
    focusBlock(at);
    setStatus(tr("status.added", { type: tr("block." + type), position: at + 1 }));
  }

  function move(from, to, keepHandleFocus) {
    if (to < 0 || to >= state.blocks.length || from === to) return;
    remember();
    state.blocks.splice(to, 0, state.blocks.splice(from, 1)[0]);
    selected = to;
    render();
    changed();
    if (keepHandleFocus) focusBlock(to, ".builder-handle");
    setStatus(tr("status.moved", { position: to + 1, total: state.blocks.length }));
  }

  function applyStarter(id) {
    var starter = options.starters.filter(function (entry) { return entry.id === id; })[0];
    if (!starter) return;
    if (state.blocks.length && !window.confirm(tr("starter_confirm"))) return;
    remember();
    state = clone(starter.document);
    selected = -1;
    setMode(false);
    render();
    changed();
    focusBlock(0);
  }

  function listAction(block, action, listKey, item, path) {
    var list = listKey ? block[listKey] : null;
    var width = (block.rows && block.rows[0] || []).length;
    switch (action) {
      case "item-add": return function () { list.push(itemDefaults[listKey](block)); };
      case "item-remove": return function () { list.splice(item, 1); };
      case "item-up": return item > 0 && function () { list.splice(item - 1, 0, list.splice(item, 1)[0]); };
      case "item-down": return item < list.length - 1 && function () { list.splice(item + 1, 0, list.splice(item, 1)[0]); };
      case "row-add": return block.rows.length < 50 && function () { block.rows.push(new Array(Math.max(width, 1)).fill("")); };
      case "row-remove": return block.rows.length > 1 && function () { block.rows.pop(); };
      case "col-add": return width < 8 && function () { block.rows.forEach(function (row) { row.push(""); }); };
      case "col-remove": return width > 1 && function () { block.rows.forEach(function (row) { row.pop(); }); };
      case "clear-image": return function () { set(block, path, ""); };
      default: return null;
    }
  }

  function blockAction(button, index) {
    var block = state.blocks[index];
    var action = button.dataset.action;
    if (action === "collapse") {
      if (collapsed.has(block)) collapsed.delete(block); else collapsed.add(block);
      render();
      focusBlock(index, '[data-action="collapse"]');
    } else if (action === "up" || action === "down") {
      move(index, action === "up" ? index - 1 : index + 1, false);
      focusBlock(selected, '[data-action="' + action + '"]');
    } else if (action === "remove") {
      remember();
      state.blocks.splice(index, 1);
      selected = Math.min(index, state.blocks.length - 1);
      render();
      changed();
      setStatus(tr("status.removed"));
      if (state.blocks.length) focusBlock(selected, ".builder-handle");
      else document.querySelector(".builder-palette-item").focus();
    } else if (action === "copy") {
      copyBlocks([block]);
    } else if (action === "duplicate") {
      remember();
      state.blocks.splice(index + 1, 0, clone(block));
      selected = index + 1;
      render();
      changed();
      focusBlock(index + 1, ".builder-handle");
      setStatus(tr("status.duplicated"));
    } else {
      var apply = listAction(block, action, button.dataset.list, Number(button.dataset.item), button.dataset.path);
      if (!apply) return;
      remember();
      apply();
      render();
      changed();
      if (action === "item-add") {
        var sets = blocksEl.querySelectorAll('[data-index="' + index + '"] fieldset');
        var last = sets[sets.length - 1];
        var input = last && last.querySelector("input, textarea");
        if (input) input.focus();
      }
    }
  }

  // ── Events ──────────────────────────────────────────────────────────────────
  function cast(input, block) {
    var how = input.dataset.cast;
    if (input.type === "checkbox") return input.checked;
    if (how === "number") return Number(input.value);
    if (how === "bool") return input.value === "true";
    if (how === "id") return input.value ? Number(input.value) : null;
    if (how === "lines") return input.value.split("\n").map(function (line) { return line.trim(); }).filter(Boolean);
    return input.value;
  }

  function onEdit(event) {
    var input = event.target;
    var cardEl = input.closest(".builder-block");
    if (!cardEl) return;
    var block = state.blocks[Number(cardEl.dataset.index)];
    if (input.dataset.layout) {
      remember();
      var layout = block.layout || {};
      var key = input.dataset.layout;
      if (input.value === options.layout[key][0]) delete layout[key]; else layout[key] = input.value;
      if (Object.keys(layout).length) block.layout = layout; else delete block.layout;
      return changed();
    }
    var path = input.dataset.path;
    if (!path) return;
    var typing = input.tagName !== "SELECT" && input.type !== "checkbox";
    remember(typing);
    if (input.dataset.cast === "columns") {
      var count = Number(input.value);
      while (block.columns.length < count) block.columns.push("");
      block.columns.length = count;
      render();
    } else {
      set(block, path, cast(input, block));
      if (input.dataset.rerender || (block.type === "text" && path === "format") || (block.type === "embed" && path === "kind")) render();
    }
    changed();
  }

  blocksEl.addEventListener("input", function (event) {
    if (event.target.tagName === "SELECT" || event.target.type === "checkbox" || event.target.type === "file") return;
    if (event.target.dataset.finder) return findPages(event.target);
    if (event.target.dataset.embedSearch) return searchEmbeds(event.target);
    onEdit(event);
  });
  blocksEl.addEventListener("change", function (event) {
    var target = event.target;
    if (target.dataset.upload) return upload(target);
    if (target.dataset.finder) return pickPage(target);
    if (target.tagName === "SELECT" || target.type === "checkbox") onEdit(event);
  });
  blocksEl.addEventListener("focusin", function (event) {
    if (event.target.dataset.embedSearch && !embedQuiet && event.target.getAttribute("aria-expanded") !== "true") {
      searchEmbeds(event.target);
    }
    var cardEl = event.target.closest(".builder-block");
    if (!cardEl || Number(cardEl.dataset.index) === selected) return;
    var previous = blocksEl.querySelector(".is-selected");
    if (previous) previous.classList.remove("is-selected");
    selected = Number(cardEl.dataset.index);
    cardEl.classList.add("is-selected");
  });
  blocksEl.addEventListener("click", function (event) {
    var option = event.target.closest("[data-embed-ref]");
    if (option && option.closest(".builder-embed-options")) {
      var picker = option.closest(".builder-embed-picker").querySelector("[data-embed-search]");
      return pickEmbed(picker, option);
    }
    var button = event.target.closest("button[data-action]");
    var cardEl = event.target.closest(".builder-block");
    if (button && cardEl) blockAction(button, Number(cardEl.dataset.index));
  });
  blocksEl.addEventListener("mousedown", function (event) {
    // Keep the focus in the picker's search box while an option is clicked.
    if (event.target.closest(".builder-embed-options")) event.preventDefault();
  });
  blocksEl.addEventListener("focusout", function (event) {
    if (event.target.dataset.embedSearch) closeEmbedOptions(event.target);
  });
  blocksEl.addEventListener("keydown", function (event) {
    if (event.target.dataset.embedSearch) return embedKeys(event);
    var handle = event.target.closest(".builder-handle");
    if (!handle) return;
    var index = Number(handle.closest(".builder-block").dataset.index);
    var to = { ArrowUp: index - 1, ArrowDown: index + 1, Home: 0, End: state.blocks.length - 1 }[event.key];
    if (to === undefined) return;
    event.preventDefault();
    move(index, to, true);
  });

  // Uploads report their progress and errors next to the image field and in the status line.
  function sendFile(url, file, onProgress) {
    return new Promise(function (resolve, reject) {
      var xhr = new XMLHttpRequest();
      xhr.open("POST", url);
      xhr.setRequestHeader("Accept", "application/json");
      xhr.setRequestHeader("X-CSRF-Token", BW.csrf());
      xhr.upload.addEventListener("progress", function (event) {
        if (event.lengthComputable) onProgress(Math.round(event.loaded * 100 / event.total));
      });
      xhr.addEventListener("load", function () {
        var data = null;
        try { data = JSON.parse(xhr.responseText); } catch (e) { data = null; }
        if (xhr.status >= 200 && xhr.status < 300 && data && data.ok) return resolve(data);
        var error = new Error((data && data.error) || (xhr.status === 413 ? tr("status.upload_too_large") : tr("status.upload_failed")));
        error.status = xhr.status;
        reject(error);
      });
      xhr.addEventListener("error", function () { reject(new Error(tr("status.upload_failed"))); });
      var form = new FormData();
      form.append("file", file);
      xhr.send(form);
    });
  }

  function uploadBox(block, path) {
    var cardEl = blocksEl.querySelector('[data-index="' + state.blocks.indexOf(block) + '"]');
    var input = cardEl && cardEl.querySelector('[data-upload="' + path + '"]');
    return input ? input.closest(".builder-image-upload") : null;
  }

  function uploadNote(block, path, node) {
    var box = uploadBox(block, path);
    if (!box) return;
    var old = box.querySelector(".builder-upload-note");
    if (old) old.remove();
    if (node) {
      node.classList.add("builder-upload-note");
      box.appendChild(node);
    }
  }

  function upload(input) {
    var file = input.files[0];
    if (!file) return;
    var cardEl = input.closest(".builder-block");
    var block = state.blocks[Number(cardEl.dataset.index)];
    var path = input.dataset.upload;
    var fail = function (message) {
      var note = el("p", "builder-upload-error", message);
      note.setAttribute("role", "alert");
      uploadNote(block, path, note);
      setStatus(message, "error");
    };
    input.value = "";
    if (IMAGE_TYPES.indexOf(file.type) === -1) return fail(tr("status.upload_type"));
    var bar = el("progress");
    bar.max = 100;
    bar.value = 0;
    bar.setAttribute("aria-label", tr("status.uploading"));
    uploadNote(block, path, bar);
    setStatus(tr("status.uploading"));
    sendFile(root.dataset.uploadUrl, file, function (percent) {
      bar.value = percent;
      setStatus(tr("status.upload_progress", { percent: percent }));
    }).then(function (result) {
      if (state.blocks.indexOf(block) === -1) return;
      remember();
      set(block, path, result.url);
      render();
      changed();
      setStatus(tr("status.uploaded"), "success");
      var altPath = path.replace(/url$/, "alt").replace(/image$/, "image_alt");
      var alt = blocksEl.querySelector('[data-index="' + state.blocks.indexOf(block) + '"] [data-path="' + altPath + '"]');
      if (alt) alt.focus();
    }).catch(function (error) { fail(error.message); });
  }

  // ── Embed picker ─────────────────────────────────────────────────────────────
  var embedTimer = null, embedRequest = null, embedQuiet = false;
  function searchEmbeds(input) {
    window.clearTimeout(embedTimer);
    embedTimer = window.setTimeout(function () {
      if (embedRequest) embedRequest.abort();
      embedRequest = new AbortController();
      BW.fetchJSON(root.dataset.embeddablesUrl + "?q=" + encodeURIComponent(input.value.trim()), { signal: embedRequest.signal })
        .then(function (result) { showEmbedOptions(input, result.items || []); })
        .catch(function (error) { if (error.name !== "AbortError") setStatus(error.message, "error"); });
    }, 200);
  }

  function embedList(input) { return document.getElementById(input.getAttribute("aria-controls")); }

  function showEmbedOptions(input, items) {
    var list = embedList(input);
    if (!list || document.activeElement !== input) return;
    list.textContent = "";
    items.forEach(function (item, i) {
      var option = el("li", "builder-embed-option");
      option.id = list.id + "-" + i;
      option.setAttribute("role", "option");
      option.setAttribute("aria-selected", "false");
      option.dataset.embedKind = item.kind;
      option.dataset.embedRef = item.ref;
      option.dataset.embedTitle = item.title;
      option.appendChild(el("span", "badge", tr("embed." + item.kind)));
      option.appendChild(el("span", "builder-embed-option__title", " " + item.title));
      if (item.description) option.appendChild(el("small", "muted", item.description));
      list.appendChild(option);
    });
    if (!items.length) {
      var none = el("li", "builder-embed-option muted", tr("embed_picker.none"));
      none.setAttribute("aria-disabled", "true");
      list.appendChild(none);
    }
    list.hidden = false;
    input.setAttribute("aria-expanded", "true");
    input.removeAttribute("aria-activedescendant");
    setStatus(tr("embed_picker.count", { count: items.length }));
  }

  function closeEmbedOptions(input) {
    var list = embedList(input);
    if (list) list.hidden = true;
    input.setAttribute("aria-expanded", "false");
    input.removeAttribute("aria-activedescendant");
  }

  function embedKeys(event) {
    var input = event.target;
    var list = embedList(input);
    var options_ = list ? Array.prototype.slice.call(list.querySelectorAll("[data-embed-ref]")) : [];
    var active = document.getElementById(input.getAttribute("aria-activedescendant") || "");
    var index = options_.indexOf(active);
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      if (list.hidden) return searchEmbeds(input);
      if (!options_.length) return;
      index = event.key === "ArrowDown" ? (index + 1) % options_.length : (index <= 0 ? options_.length - 1 : index - 1);
      options_.forEach(function (option, i) { option.setAttribute("aria-selected", String(i === index)); });
      input.setAttribute("aria-activedescendant", options_[index].id);
      options_[index].scrollIntoView({ block: "nearest" });
    } else if (event.key === "Enter" && active) {
      event.preventDefault();
      pickEmbed(input, active);
    } else if (event.key === "Escape" && !list.hidden) {
      event.preventDefault();
      closeEmbedOptions(input);
    }
  }

  function pickEmbed(input, option) {
    var index = Number(input.closest(".builder-block").dataset.index);
    var block = state.blocks[index];
    remember();
    block.kind = option.dataset.embedKind;
    block.ref = option.dataset.embedRef;
    embedTitles[block.kind + ":" + block.ref] = option.dataset.embedTitle;
    render();
    changed();
    embedQuiet = true;
    focusBlock(index, "[data-embed-search]");
    embedQuiet = false;
    setStatus(tr("embed_picker.chosen", { title: option.dataset.embedTitle }));
  }

  // ── Clipboard ────────────────────────────────────────────────────────────────
  function copyBlocks(blocks) {
    var text = JSON.stringify({ format: ENVELOPE, version: 2, blocks: clone(blocks) });
    if (!navigator.clipboard || !navigator.clipboard.writeText) return setStatus(tr("status.copy_failed"), "error");
    navigator.clipboard.writeText(text)
      .then(function () { setStatus(tr("status.copied")); })
      .catch(function () { setStatus(tr("status.copy_failed"), "error"); });
  }

  function clipboardDocument(text) {
    var data;
    try { data = JSON.parse(text); } catch (e) { return null; }
    if (!data || typeof data !== "object" || !Array.isArray(data.blocks)) return null;
    if (data.format !== undefined && data.format !== ENVELOPE) return null;
    return { version: data.version, blocks: data.blocks };
  }

  function insertBlocks(blocks, message) {
    if (state.blocks.length + blocks.length > MAX_BLOCKS) return setStatus(tr("status.too_many"), "error");
    remember();
    var at = insertAt();
    Array.prototype.splice.apply(state.blocks, [at, 0].concat(clone(blocks)));
    selected = at;
    setMode(false);
    render();
    changed();
    focusBlock(at, ".builder-handle");
    setStatus(message);
  }

  function pasteText(text) {
    var doc = clipboardDocument(text);
    if (!doc) return setStatus(tr("status.paste_invalid"), "error");
    BW.fetchJSON(root.dataset.clipboardUrl, { body: { document: doc } })
      .then(function (result) { insertBlocks(result.blocks, tr("status.pasted", { count: result.blocks.length })); })
      .catch(function (error) { setStatus(error.message, "error"); });
  }

  pasteButton.addEventListener("click", function () {
    if (!navigator.clipboard || !navigator.clipboard.readText) return setStatus(tr("status.paste_keyboard"));
    navigator.clipboard.readText().then(pasteText, function () { setStatus(tr("status.paste_keyboard")); });
  });

  document.addEventListener("paste", function (event) {
    var target = event.target;
    if (target.closest && target.closest("input, textarea, select, [contenteditable]")) return;
    var text = event.clipboardData && event.clipboardData.getData("text/plain");
    if (!text || !clipboardDocument(text)) return;
    event.preventDefault();
    pasteText(text);
  });

  // ── Saved sections ──────────────────────────────────────────────────────────
  function renderSections() {
    sectionsEl.textContent = "";
    sectionsEmpty.hidden = options.sections.length > 0;
    options.sections.forEach(function (section) {
      var item = el("li", "builder-section-item");
      var insert = el("button", "builder-starter", "");
      insert.type = "button";
      insert.dataset.section = String(section.id);
      insert.appendChild(el("strong", "", section.name));
      insert.appendChild(el("small", "", tr("sections.count", { count: section.document.blocks.length })));
      item.appendChild(insert);
      if (options.manage_sections) {
        var remove = el("button", "btn btn--ghost btn--small btn--icon", "×");
        remove.type = "button";
        remove.dataset.deleteSection = String(section.id);
        remove.setAttribute("aria-label", tr("sections.delete", { name: section.name }));
        remove.title = remove.getAttribute("aria-label");
        item.appendChild(remove);
      }
      sectionsEl.appendChild(item);
    });
  }

  function sectionById(id) {
    return options.sections.filter(function (section) { return String(section.id) === id; })[0];
  }

  sectionsEl.addEventListener("click", function (event) {
    var insert = event.target.closest("[data-section]");
    var remove = event.target.closest("[data-delete-section]");
    if (insert) {
      var section = sectionById(insert.dataset.section);
      if (section) insertBlocks(section.document.blocks, tr("sections.inserted", { name: section.name }));
    } else if (remove) {
      var doomed = sectionById(remove.dataset.deleteSection);
      if (!doomed || !window.confirm(tr("sections.delete_confirm", { name: doomed.name }))) return;
      BW.fetchJSON(doomed.delete_url, { method: "DELETE" }).then(function () {
        options.sections = options.sections.filter(function (section) { return section !== doomed; });
        renderSections();
        (sectionsEl.querySelector("button") || pasteButton).focus();
        setStatus(tr("sections.deleted", { name: doomed.name }));
      }).catch(function (error) { setStatus(error.message, "error"); });
    }
  });

  var sectionSave = $("builder-section-save");
  if (sectionSave) {
    var sectionName = $("builder-section-name"), sectionFrom = $("builder-section-from"), sectionTo = $("builder-section-to");
    // While the form is closed, it follows the selected block.
    blocksEl.addEventListener("focusin", function () {
      if (selected < 0 || sectionSave.closest("details").open) return;
      sectionFrom.value = sectionTo.value = String(selected + 1);
    });
    sectionSave.addEventListener("click", function () {
      var from = Number(sectionFrom.value), to = Number(sectionTo.value);
      if (!sectionName.value.trim()) { sectionName.focus(); return setStatus(tr("sections.name_needed"), "error"); }
      if (!(from >= 1 && to >= from && to <= state.blocks.length)) {
        sectionFrom.focus();
        return setStatus(tr("sections.range", { total: state.blocks.length }), "error");
      }
      BW.fetchJSON(root.dataset.sectionsUrl, {
        body: { name: sectionName.value, document: { version: 2, blocks: state.blocks.slice(from - 1, to) } }
      }).then(function (result) {
        options.sections.push(Object.assign({}, result.section, { delete_url: result.delete_url }));
        options.sections.sort(function (a, b) { return a.name.localeCompare(b.name); });
        renderSections();
        sectionName.value = "";
        setStatus(tr("sections.saved", { name: result.section.name }), "success");
      }).catch(function (error) { setStatus(error.message, "error"); });
    });
  }

  var searchTimer = null;
  function findPages(input) {
    window.clearTimeout(searchTimer);
    var query = input.value.trim();
    if (!query) return;
    searchTimer = window.setTimeout(function () {
      BW.fetchJSON(root.dataset.searchUrl + "?include_home=1&q=" + encodeURIComponent(query))
        .then(function (rows) {
          suggestions.textContent = "";
          rows.forEach(function (row) {
            var option = el("option");
            option.value = row.slug;
            option.label = row.title + (row.category_name ? " — " + row.category_name : "");
            suggestions.appendChild(option);
          });
        })
        .catch(function () { suggestions.textContent = ""; });
    }, 250);
  }

  function pickPage(input) {
    var slug = input.value.trim();
    var known = Array.prototype.some.call(suggestions.options, function (option) { return option.value === slug; });
    if (!known) return;
    var block = state.blocks[Number(input.closest(".builder-block").dataset.index)];
    if (block.slugs.indexOf(slug) !== -1 || block.slugs.length >= 24) { input.value = ""; return; }
    remember();
    block.slugs.push(slug);
    render();
    changed();
    var index = state.blocks.indexOf(block);
    focusBlock(index, "[data-finder]");
  }

  document.querySelectorAll(".builder-palette-item").forEach(function (button) {
    button.addEventListener("click", function () { addBlock(button.dataset.blockType); });
    button.addEventListener("dragstart", function (event) {
      event.dataTransfer.setData("application/x-bananawiki-block", button.dataset.blockType);
      event.dataTransfer.effectAllowed = "copy";
    });
  });
  document.querySelectorAll(".builder-starter").forEach(function (button) {
    button.addEventListener("click", function () { applyStarter(button.dataset.starter); });
  });

  // Dragging starts from a block's handle only, so text in its fields stays selectable.
  blocksEl.addEventListener("pointerdown", function (event) {
    var handle = event.target.closest(".builder-handle");
    if (handle) handle.closest(".builder-block").draggable = true;
  });
  blocksEl.addEventListener("dragstart", function (event) {
    var cardEl = event.target.closest(".builder-block");
    if (!cardEl || !cardEl.draggable) return;
    dragged = Number(cardEl.dataset.index);
    cardEl.classList.add("is-dragging");
    event.dataTransfer.effectAllowed = "move";
    event.dataTransfer.setData("text/plain", String(dragged));
  });
  blocksEl.addEventListener("dragend", function () {
    dragged = null;
    clearDropMarker();
    blocksEl.querySelectorAll(".builder-block").forEach(function (cardEl) {
      cardEl.classList.remove("is-dragging");
      cardEl.draggable = false;
    });
  });
  function clearDropMarker() {
    stageEl.classList.remove("is-drag-over");
    blocksEl.querySelectorAll(".is-drop-before, .is-drop-after").forEach(function (node) {
      node.classList.remove("is-drop-before", "is-drop-after");
    });
  }
  stageEl.addEventListener("dragover", function (event) {
    event.preventDefault();
    clearDropMarker();
    stageEl.classList.add("is-drag-over");
    var target = event.target.closest(".builder-block");
    if (!target) { dropIndex = state.blocks.length; return; }
    var box = target.getBoundingClientRect();
    var after = event.clientY > box.top + box.height / 2;
    target.classList.add(after ? "is-drop-after" : "is-drop-before");
    dropIndex = Number(target.dataset.index) + (after ? 1 : 0);
  });
  stageEl.addEventListener("dragleave", function (event) {
    if (!stageEl.contains(event.relatedTarget)) clearDropMarker();
  });
  stageEl.addEventListener("drop", function (event) {
    event.preventDefault();
    clearDropMarker();
    var index = dropIndex === null ? state.blocks.length : dropIndex;
    dropIndex = null;
    var paletteType = event.dataTransfer.getData("application/x-bananawiki-block");
    if (paletteType) return addBlock(paletteType, index);
    if (dragged === null) return;
    move(dragged, dragged < index ? index - 1 : index, true);
  });

  // ── Modes and devices ───────────────────────────────────────────────────────
  function setMode(preview) {
    previewEl.hidden = !preview;
    stageEl.hidden = preview;
    editMode.setAttribute("aria-pressed", String(!preview));
    previewMode.setAttribute("aria-pressed", String(preview));
    if (preview) refreshPreview();
    else previewContent.querySelectorAll(".builder-embed-frame").forEach(function (frame) { frame.remove(); });
  }
  editMode.addEventListener("click", function () { setMode(false); });
  previewMode.addEventListener("click", function () { setMode(true); });
  document.querySelectorAll("button[data-device]").forEach(function (button) {
    button.addEventListener("click", function () {
      frameEl.dataset.device = button.dataset.device;
      document.querySelectorAll("button[data-device]").forEach(function (other) {
        other.setAttribute("aria-pressed", String(other === button));
      });
    });
  });

  titleEl.addEventListener("input", function () { dirty = true; setStatus(tr("status.unsaved")); });
  undoButton.addEventListener("click", undo);
  redoButton.addEventListener("click", redo);

  if (discardButton) {
    discardButton.addEventListener("click", function () {
      if (!window.confirm(tr("discard_confirm"))) return;
      BW.fetchJSON(root.dataset.draftUrl, { method: "DELETE" })
        .then(function () { dirty = false; window.location.reload(); })
        .catch(function (error) { setStatus(error.message, "error"); });
    });
  }

  publishButton.addEventListener("click", function () {
    publishing = true;
    window.clearTimeout(saveTimer);
    if (draftRequest) draftRequest.abort();
    publishButton.disabled = true;
    setStatus(tr("status.publishing"));
    BW.fetchJSON(root.dataset.publishUrl, {
      body: {
        document: state, title: titleEl.value, edit_message: messageEl ? messageEl.value : "",
        public: publicEl ? publicEl.checked : false, base_revision: baseRevision, base_token: root.dataset.baseToken
      }
    }).then(function (result) {
      dirty = false;
      setStatus(tr(hasDrafts ? "status.published" : "status.saved"), "success");
      window.location.assign(result.redirect || root.dataset.viewUrl);
    }).catch(function (error) {
      publishing = false;
      publishButton.disabled = false;
      if (error.status === 409) showConflict();
      setStatus(error.message, "error");
    });
  });

  window.addEventListener("beforeunload", function (event) {
    if (!dirty || publishing) return;
    event.preventDefault();
    event.returnValue = "";
  });

  document.addEventListener("keydown", function (event) {
    if (!(event.ctrlKey || event.metaKey)) return;
    var key = event.key.toLowerCase();
    if (key === "s") {
      event.preventDefault();
      window.clearTimeout(saveTimer);
      if (hasDrafts) saveDraft(); else if (!publishButton.disabled) publishButton.click();
      return;
    }
    // Text fields keep their own undo; elsewhere the shortcuts undo whole editing steps.
    var target = event.target;
    if (target.tagName === "TEXTAREA" || (target.tagName === "INPUT" && target.type !== "checkbox")) return;
    if (key === "z" && !event.shiftKey) { event.preventDefault(); undo(); }
    else if ((key === "z" && event.shiftKey) || key === "y") { event.preventDefault(); redo(); }
  });

  // Name the canvases and boards already embedded (the picker shows titles, not addresses).
  function nameEmbeds() {
    var used = state.blocks.some(function (block) { return block.type === "embed" && block.ref; });
    if (!used || !options.embeds.length) return;
    BW.fetchJSON(root.dataset.embeddablesUrl).then(function (result) {
      (result.items || []).forEach(function (item) { embedTitles[item.kind + ":" + item.ref] = item.title; });
      render();
    }).catch(function () { /* the addresses stay visible */ });
  }

  render();
  renderSections();
  refreshPreview();
  nameEmbeds();
}());
