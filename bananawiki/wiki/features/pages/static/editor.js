/* Page editor: Markdown toolbar, live preview, image upload, link/table/video
 * helpers, editing presence and an unsaved-changes guard. */
(function () {
  "use strict";
  var form = document.querySelector("[data-page-editor]");
  var editor = document.querySelector("[data-editor]");
  var configNode = document.querySelector("script#editor-config");
  if (!form || !editor || !configNode) return;
  var config = JSON.parse(configNode.textContent || "{}");
  var area = editor.querySelector("#editor-content");
  var preview = editor.querySelector("[data-preview]");
  var words = editor.querySelector("[data-word-count]");
  var uploadStatus = editor.querySelector("[data-upload-status]");
  var fileInput = editor.querySelector("[data-image-input]");
  function draftState() {
    // Include metadata and feature fields, not just the Markdown textarea.
    var values = [];
    new FormData(form).forEach(function (value, name) {
      if (typeof value === "string" && name !== "csrf_token") values.push([name, value]);
    });
    return JSON.stringify(values);
  }
  var initial = draftState();
  var submitting = false;

  // Text helpers
  function replaceSelection(before, after, placeholder) {
    var start = area.selectionStart, end = area.selectionEnd;
    var selected = area.value.slice(start, end) || placeholder || "";
    var text = before + selected + (after || "");
    area.setRangeText(text, start, end, "end");
    area.selectionStart = start + before.length;
    area.selectionEnd = start + before.length + selected.length;
    area.focus();
    changed();
  }

  function insertBlock(text) {
    var start = area.selectionStart;
    var prefix = start > 0 && area.value[start - 1] !== "\n" ? "\n\n" : "";
    area.setRangeText(prefix + text + "\n", start, area.selectionEnd, "end");
    area.focus();
    changed();
  }

  function eachLine(transform) {
    var value = area.value;
    var start = value.lastIndexOf("\n", area.selectionStart - 1) + 1;
    var endIndex = value.indexOf("\n", area.selectionEnd);
    var end = endIndex === -1 ? value.length : endIndex;
    var lines = value.slice(start, end).split("\n").map(transform);
    area.setRangeText(lines.join("\n"), start, end, "select");
    area.focus();
    changed();
  }

  function prefixLines(prefix, numbered) {
    var n = 0;
    eachLine(function (line) {
      n += 1;
      var marker = numbered ? n + ". " : prefix;
      return line.indexOf(marker) === 0 ? line.slice(marker.length) : marker + line;
    });
  }

  var actions = {
    bold: function () { replaceSelection("**", "**", BW.t("pages.editor_text")); },
    italic: function () { replaceSelection("*", "*", BW.t("pages.editor_text")); },
    strike: function () { replaceSelection("~~", "~~", BW.t("pages.editor_text")); },
    code: function () { replaceSelection("`", "`", "code"); },
    codeblock: function () { replaceSelection("\n```\n", "\n```\n", ""); },
    h2: function () { prefixLines("## "); },
    h3: function () { prefixLines("### "); },
    ul: function () { prefixLines("- "); },
    ol: function () { prefixLines("", true); },
    task: function () { prefixLines("- [ ] "); },
    quote: function () { prefixLines("> "); },
    indent: function () { eachLine(function (line) { return "  " + line; }); },
    outdent: function () { eachLine(function (line) { return line.replace(/^( {1,2}|\t)/, ""); }); },
    hr: function () { insertBlock("---"); },
    image: function () {
      var token = imageAtCaret();
      if (token) openImageDialog(token);
      else if (fileInput) fileInput.click();
      else BW.toast(BW.t("pages.image_pick"), "info");
    }
  };

  editor.addEventListener("click", function (event) {
    var button = event.target.closest("[data-md]");
    if (button && actions[button.getAttribute("data-md")]) {
      event.preventDefault();
      actions[button.getAttribute("data-md")]();
      var menu = button.closest("details.menu");
      if (menu) menu.removeAttribute("open");
    }
  });

  area.addEventListener("keydown", function (event) {
    if (!(event.ctrlKey || event.metaKey)) return;
    var key = event.key.toLowerCase();
    if (key === "b") { event.preventDefault(); actions.bold(); }
    else if (key === "i") { event.preventDefault(); actions.italic(); }
    else if (key === "k") { event.preventDefault(); openDialog("dlg-link"); }
    else if (key === "s") {
      event.preventDefault();
      if (form.requestSubmit) form.requestSubmit();
      else if (form.checkValidity()) { submitting = true; form.submit(); }
      else if (form.reportValidity) form.reportValidity();
    }
  });

  // Live preview and counters
  var previewTimer = null, previewController = null, previewVersion = 0;
  function cancelPreview() {
    clearTimeout(previewTimer);
    if (previewController) previewController.abort();
    previewVersion += 1;
  }
  function renderPreview() {
    cancelPreview();
    var version = previewVersion;
    previewController = window.AbortController ? new AbortController() : null;
    BW.fetchJSON(config.preview, { method: "POST", body: { content: area.value },
                                   signal: previewController ? previewController.signal : undefined })
      .then(function (data) {
        if (version !== previewVersion) return;
        var holder = document.createElement("template");
        holder.innerHTML = data.html;  // sanitised by the server
        holder.content.querySelectorAll("img").forEach(function (img) {
          if (editableImage(img) && !img.hasAttribute("title")) img.title = BW.t("pages.image_click");
        });
        preview.replaceChildren(holder.content);
        // Code blocks and tables that scroll sideways get a tab stop, as on the page itself.
        if (BW.focusScrollingBlocks) window.requestAnimationFrame(function () { BW.focusScrollingBlocks(preview); });
      }).catch(function (error) {
        if (version === previewVersion && error.name !== "AbortError") preview.textContent = error.message || BW.t("error");
      });
  }
  function count() {
    var text = area.value.trim();
    words.textContent = BW.t("pages.editor_counts", { words: text ? text.split(/\s+/).length : 0, chars: area.value.length });
  }
  function changed() {
    count();
    cancelPreview();
    previewTimer = setTimeout(renderPreview, 500);
  }
  area.addEventListener("input", changed);

  var tabs = Array.from(editor.querySelectorAll("[data-editor-tab]"));
  function selectTab(tab) {
    var showPreview = tab.getAttribute("data-editor-tab") === "preview";
    editor.classList.toggle("show-preview", showPreview);
    if (showPreview && BW.focusScrollingBlocks) window.requestAnimationFrame(function () { BW.focusScrollingBlocks(preview); });
    tabs.forEach(function (other) {
      var active = other === tab;
      other.setAttribute("aria-selected", active ? "true" : "false");
      other.setAttribute("tabindex", active ? "0" : "-1");
      other.classList.toggle("btn--ghost", !active);
    });
    if (showPreview) renderPreview();
  }
  tabs.forEach(function (tab, index) {
    tab.addEventListener("click", function () { selectTab(tab); });
    tab.addEventListener("keydown", function (event) {
      var next;
      if (event.key === "ArrowRight") next = tabs[(index + 1) % tabs.length];
      else if (event.key === "ArrowLeft") next = tabs[(index + tabs.length - 1) % tabs.length];
      else if (event.key === "Home") next = tabs[0];
      else if (event.key === "End") next = tabs[tabs.length - 1];
      else return;
      event.preventDefault();
      selectTab(next);
      next.focus();
    });
  });

  var fullscreen = editor.querySelector("[data-editor-fullscreen]");
  if (fullscreen) {
    fullscreen.addEventListener("click", function () {
      var on = editor.classList.toggle("is-fullscreen");
      fullscreen.setAttribute("aria-pressed", on ? "true" : "false");
      var menu = fullscreen.closest("details.menu");
      if (menu) menu.removeAttribute("open");
      area.focus();
    });
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && editor.classList.contains("is-fullscreen")) fullscreen.click();
    });
  }

  // Images: one at a time, each with a progress bar under the editor; a picture the server would refuse
  // (type, size) is named at once instead of after its upload.
  var progressHost = editor.querySelector("[data-upload-progress-host]");
  var imageRules = config.uploadRules || {};
  function sendImage(file) {
    var body = new FormData();
    body.append("file", file);
    var row = BW.uploadRow(progressHost || editor, file.name);
    var problem = BW.uploadProblem(file, imageRules);
    if (problem) {
      row.fail(problem);
      var refused = new Error(problem);
      refused.shown = true;
      return Promise.reject(refused);
    }
    uploadStatus.textContent = "";
    return BW.upload(config.upload, body, { onProgress: row.progress, onRetry: row.retrying, signal: row.signal })
      .then(function (data) {
        row.done();
        uploadStatus.textContent = BW.t("pages.uploaded", { name: file.name });
        return data;
      }, function (error) {
        row.fail(error.message, error.aborted);
        error.shown = true;
        throw error;
      });
  }
  var imageQueue = Promise.resolve();
  function upload(files) {
    if (!config.upload) return;
    var others = Array.prototype.filter.call(files, function (file) { return !/^image\//.test(file.type); });
    if (others.length) {
      // Other files become attachments of the page when it has an attachment list to take them.
      var handed = new CustomEvent("bw:editor-files", { detail: { files: others }, cancelable: true });
      if (editor.dispatchEvent(handed)) {
        others.forEach(function (file) { BW.toast(BW.t("pages.not_an_image", { name: file.name }), "error"); });
      }
    }
    Array.prototype.forEach.call(files, function (file) {
      if (!/^image\//.test(file.type)) return;
      imageQueue = imageQueue.then(function () {
        return sendImage(file).then(function (data) {
          var alt = file.name.replace(/\.[^.]+$/, "").replace(/[\[\]]/g, "");
          insertBlock("![" + alt + "](" + data.url + ")");
        });
      }).catch(function (error) {
        if (!error.shown) BW.toast(error.message || BW.t("error"), "error");
      });
    });
  }
  // Other parts of the editor page (attachments) add text at the cursor through this event.
  editor.addEventListener("bw:editor-insert", function (event) {
    var detail = event.detail || {};
    if (typeof detail.text !== "string" || !detail.text) return;
    if (!detail.inline) { insertBlock(detail.text); return; }
    var end = area.selectionEnd;
    var gap = end > 0 && !/\s/.test(area.value[end - 1]) ? " " : "";
    area.setRangeText(gap + detail.text, end, end, "end");
    area.focus();
    changed();
  });
  if (fileInput) fileInput.addEventListener("change", function () { upload(fileInput.files); fileInput.value = ""; });
  area.addEventListener("dragover", function (e) {
    if (config.upload && e.dataTransfer && Array.prototype.indexOf.call(e.dataTransfer.types, "Files") !== -1) {
      e.preventDefault();
      area.classList.add("is-over");
    }
  });
  area.addEventListener("dragleave", function () { area.classList.remove("is-over"); });
  area.addEventListener("drop", function (e) {
    area.classList.remove("is-over");
    if (config.upload && e.dataTransfer && e.dataTransfer.files.length) {
      e.preventDefault();
      upload(e.dataTransfer.files);
    }
  });
  area.addEventListener("paste", function (e) {
    var files = e.clipboardData && e.clipboardData.files;
    if (config.upload && files && files.length) {
      e.preventDefault();
      upload(files);
    }
  });

  // Image options: alt text, width, alignment, spacing and crop
  // A picture is changed in the Markdown, one token at a time. The preview only
  // says which picture: it is rebuilt as you type, so nothing is resized in it.
  var MD_IMAGE = /!\[([^\]\n]*)\]\(\s*<?([^\s<>()]+)>?(?:\s+"([^"\n]*)")?\s*\)/g;
  var HTML_IMAGE = /<img\b[^<>\n]*>/gi;
  var HTML_ATTR = /([^\s"'<>\/=]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'=<>`]+)))?/g;
  var ALIGN_CLASS = /^media-(left|right|center|full)$/;
  var ALIGNS = ["none", "left", "right", "center", "full"];
  // Style properties the dialog writes; the tag's other declarations are kept.
  var MANAGED_STYLES = ["padding", "--media-padding", "width", "max-width", "--media-max-width"];
  var CROP_TYPES = { "image/png": "png", "image/jpeg": "jpg", "image/webp": "webp" };
  var MIN_CROP = 0.05;
  var decoder = document.createElement("textarea");

  function decodeEntities(value) { decoder.innerHTML = value; return decoder.value; }
  function escapeAttr(value) {
    return String(value).replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }
  function sameSrc(a, b) {
    try { return new URL(a, location.href).href === new URL(b, location.href).href; } catch (e) { return a === b; }
  }
  function styleName(declaration) { return declaration.split(":")[0].trim().toLowerCase(); }
  function styleValue(styles, names) {
    var found = styles.filter(function (s) { return names.indexOf(styleName(s)) !== -1; })[0];
    return found ? found.slice(found.indexOf(":") + 1).trim() : "";
  }

  function parseHtmlImage(raw) {
    var token = { kind: "html", src: "", alt: "", title: "", width: "", classes: [], styles: [], attrs: [] };
    var inner = raw.slice(4, -1).replace(/\/\s*$/, ""), match;
    HTML_ATTR.lastIndex = 0;
    while ((match = HTML_ATTR.exec(inner))) {
      var name = match[1].toLowerCase();
      var bare = match[2] === undefined && match[3] === undefined && match[4] === undefined;
      var value = bare ? "" : decodeEntities(match[2] !== undefined ? match[2] : match[3] !== undefined ? match[3] : match[4]);
      if (name === "src" || name === "alt" || name === "title" || name === "width") token[name] = value;
      else if (name === "class") token.classes = value.split(/\s+/).filter(Boolean);
      else if (name === "style") token.styles = value.split(";").map(function (s) { return s.trim(); }).filter(Boolean);
      else token.attrs.push([name, bare ? null : value]);
    }
    return token;
  }

  // Pictures of the source in order, with their position; code is skipped as the
  // renderer skips it, so the n-th picture here is the n-th one in the preview.
  function imageTokens(text) {
    var tokens = [], offset = 0, fence = null;
    text.split("\n").forEach(function (line) {
      var marker = /^\s*(`{3,}|~{3,})/.exec(line), match;
      if (fence) {
        if (marker && marker[1].charAt(0) === fence.charAt(0) && marker[1].length >= fence.length) fence = null;
      } else if (marker) {
        fence = marker[1];
      } else {
        var visible = line.replace(/(`+)[^`]*?\1/g, function (span) { return span.replace(/./g, " "); });
        MD_IMAGE.lastIndex = 0;
        while ((match = MD_IMAGE.exec(visible))) {
          tokens.push({ kind: "md", start: offset + match.index, raw: match[0], src: match[2], alt: match[1],
                        title: match[3] || "", width: "", classes: [], styles: [], attrs: [] });
        }
        HTML_IMAGE.lastIndex = 0;
        while ((match = HTML_IMAGE.exec(visible))) {
          var token = parseHtmlImage(match[0]);
          token.start = offset + match.index;
          token.raw = match[0];
          tokens.push(token);
        }
      }
      offset += line.length + 1;
    });
    tokens.forEach(function (t) { t.end = t.start + t.raw.length; });
    return tokens.sort(function (a, b) { return a.start - b.start; });
  }

  function imageAtCaret() {
    var start = area.selectionStart, end = area.selectionEnd;
    return imageTokens(area.value).filter(function (t) { return t.start <= start && end <= t.end; })[0] || null;
  }

  function imageOptions(token) {
    var align = "none";
    token.classes.forEach(function (c) { var m = ALIGN_CLASS.exec(c); if (m) align = m[1]; });
    var width = /^(\d{1,4})(px|%)?$/.exec((token.width || styleValue(token.styles, ["width", "max-width", "--media-max-width"]))
      .replace(/\s+/g, ""));
    var padding = /^(\d{1,4})(px)?$/.exec(styleValue(token.styles, ["padding", "--media-padding"]).split(/\s+/)[0]);
    return { alt: token.alt, width: width ? width[1] : "", unit: width && width[2] === "%" ? "%" : "px",
             align: align, padding: padding && +padding[1] > 0 ? padding[1] : "" };
  }

  // Markdown while the picture needs no options, an <img> tag (which the
  // renderer's allow-list keeps) once it has a width, alignment or spacing.
  function imageMarkup(token, options, src) {
    var classes = token.classes.filter(function (c) { return !ALIGN_CLASS.test(c); });
    if (options.align !== "none") classes.push("media-" + options.align);
    var styles = token.styles.filter(function (s) { return MANAGED_STYLES.indexOf(styleName(s)) === -1; });
    if (options.padding) styles.unshift("padding:" + options.padding + "px");
    var width = options.width ? options.width + (options.unit === "%" ? "%" : "") : "";
    var alt = options.alt.replace(/\s+/g, " ").trim();
    if (!width && !classes.length && !styles.length && !token.attrs.length) {
      var title = token.title.replace(/"/g, "");
      return "![" + alt.replace(/[\[\]]/g, "") + "](" + src.replace(/[()\s]/g, encodeURIComponent) +
        (title ? ' "' + title + '"' : "") + ")";
    }
    var html = '<img src="' + escapeAttr(src) + '" alt="' + escapeAttr(alt) + '"';
    if (token.title) html += ' title="' + escapeAttr(token.title) + '"';
    if (width) html += ' width="' + width + '"';
    if (classes.length) html += ' class="' + escapeAttr(classes.join(" ")) + '"';
    if (styles.length) html += ' style="' + escapeAttr(styles.join(";")) + '"';
    token.attrs.forEach(function (attr) {
      html += " " + attr[0] + (attr[1] === null ? "" : '="' + escapeAttr(attr[1]) + '"');
    });
    return html + ">";
  }

  var imageDialog = document.querySelector("dialog#dlg-image");
  var imageToken = null, crop = null;
  function imageField(name) { return imageDialog.querySelector("[data-" + name + "]"); }
  var fields = imageDialog ? {
    alt: imageField("image-alt"), width: imageField("image-width"), unit: imageField("image-unit"),
    align: imageField("image-align"), padding: imageField("image-padding"), apply: imageField("image-apply"),
    stage: imageField("crop-stage"), img: imageField("crop-img"), box: imageField("crop-box"),
    hint: imageField("crop-hint"), reset: imageField("crop-reset")
  } : null;

  function cropRefusal(src) {
    if (!config.upload) return "pages.crop_no_upload";
    var url;
    try { url = new URL(src, location.href); } catch (e) { return "pages.crop_unavailable"; }
    // Another site's picture would taint the canvas; a GIF would lose its animation.
    if (url.origin !== location.origin) return "pages.crop_external";
    if (/\.gif$/i.test(url.pathname)) return "pages.crop_gif";
    return "";
  }
  function isCropped() {
    return !!crop && !fields.box.hidden && (crop.w < 0.999 || crop.h < 0.999);
  }
  function drawCrop() {
    fields.box.style.left = crop.x * 100 + "%";
    fields.box.style.top = crop.y * 100 + "%";
    fields.box.style.width = crop.w * 100 + "%";
    fields.box.style.height = crop.h * 100 + "%";
    fields.reset.disabled = !isCropped();
  }
  function showCropImage(src) {
    var refusal = cropRefusal(src);
    crop = { x: 0, y: 0, w: 1, h: 1 };
    fields.box.hidden = true;
    fields.reset.hidden = true;
    fields.stage.hidden = false;
    fields.hint.textContent = "";
    fields.img.onload = function () {
      if (refusal) { fields.hint.textContent = BW.t(refusal); return; }
      fields.box.hidden = false;
      fields.reset.hidden = false;
      fields.hint.textContent = BW.t("pages.crop_hint");
      drawCrop();
    };
    fields.img.onerror = function () {
      fields.stage.hidden = true;
      fields.hint.textContent = BW.t("pages.crop_unavailable");
    };
    fields.img.removeAttribute("src");
    fields.img.src = src;
  }

  function clamp(value, low, high) { return Math.min(high, Math.max(low, value)); }
  function moveCrop(from, mode, dx, dy) {
    if (mode === "move") {
      return { x: clamp(from.x + dx, 0, 1 - from.w), y: clamp(from.y + dy, 0, 1 - from.h), w: from.w, h: from.h };
    }
    var left = from.x, top = from.y, right = from.x + from.w, bottom = from.y + from.h;
    if (mode.charAt(1) === "w") left = clamp(left + dx, 0, right - MIN_CROP);
    if (mode.charAt(1) === "e") right = clamp(right + dx, left + MIN_CROP, 1);
    if (mode.charAt(0) === "n") top = clamp(top + dy, 0, bottom - MIN_CROP);
    if (mode.charAt(0) === "s") bottom = clamp(bottom + dy, top + MIN_CROP, 1);
    return { x: left, y: top, w: right - left, h: bottom - top };
  }
  function cropMode(target) {
    var handle = target.closest("[data-crop-handle]");
    return handle ? handle.getAttribute("data-crop-handle") : "move";
  }

  function croppedFile(name) {
    return new Promise(function (resolve, reject) {
      var image = fields.img, width = image.naturalWidth, height = image.naturalHeight;
      var sx = Math.round(crop.x * width), sy = Math.round(crop.y * height);
      var sw = Math.max(1, Math.min(width - sx, Math.round(crop.w * width)));
      var sh = Math.max(1, Math.min(height - sy, Math.round(crop.h * height)));
      var canvas = document.createElement("canvas");
      canvas.width = sw;
      canvas.height = sh;
      canvas.getContext("2d").drawImage(image, sx, sy, sw, sh, 0, 0, sw, sh);
      var path = new URL(image.src).pathname;
      var wanted = /\.jpe?g$/i.test(path) ? "image/jpeg" : /\.webp$/i.test(path) ? "image/webp" : "image/png";
      // A browser without a WebP encoder answers with a PNG: name the file after what it gave.
      canvas.toBlob(function (blob) {
        if (blob && CROP_TYPES[blob.type]) resolve(new File([blob], name + "." + CROP_TYPES[blob.type], { type: blob.type }));
        else reject(new Error(BW.t("pages.crop_failed")));
      }, wanted, 0.92);
    });
  }

  function openImageDialog(token) {
    if (!fields || !imageDialog.showModal) return;
    var options = imageOptions(token);
    imageToken = token;
    fields.alt.value = options.alt;
    fields.width.value = options.width;
    fields.unit.value = options.unit;
    fields.align.value = options.align;
    fields.padding.value = options.padding;
    fields.apply.disabled = false;
    showCropImage(token.src);
    imageDialog.showModal();
  }

  function readImageOptions() {
    var unit = fields.unit.value === "%" ? "%" : "px";
    var width = parseInt(fields.width.value, 10), padding = parseInt(fields.padding.value, 10);
    return {
      alt: fields.alt.value, unit: unit, align: ALIGNS.indexOf(fields.align.value) !== -1 ? fields.align.value : "none",
      width: width > 0 ? String(Math.min(width, unit === "%" ? 100 : 4000)) : "",
      padding: padding > 0 ? String(Math.min(padding, 200)) : ""
    };
  }

  function applyImage() {
    var token = imageToken;
    if (!token) return;
    var options = readImageOptions();
    function stillThere() { return imageToken === token && area.value.slice(token.start, token.end) === token.raw; }
    if (!stillThere()) { imageDialog.close(); BW.toast(BW.t("pages.image_changed"), "error"); return; }
    // A crop is a new upload: the original stays, as other pages and old versions may show it.
    var name = options.alt.replace(/[^\w\- ]+/g, "").trim().slice(0, 60) || "image";
    var source = isCropped()
      ? croppedFile(name).then(sendImage).then(function (data) { return data.url; })
      : Promise.resolve(token.src);
    fields.apply.disabled = true;
    source.then(function (src) {
      if (!stillThere()) return;
      area.setRangeText(imageMarkup(token, options, src), token.start, token.end, "end");
      imageDialog.close();
      area.focus();
      changed();
    }).catch(function (error) {
      if (!error.shown) BW.toast(error.message || BW.t("error"), "error");
    }).then(function () { fields.apply.disabled = false; });
  }

  if (fields) {
    fields.apply.addEventListener("click", applyImage);
    imageDialog.addEventListener("close", function () { imageToken = null; });
    // The dialog sits inside the page form: Enter in a field applies it instead of saving the page.
    imageDialog.addEventListener("keydown", function (event) {
      if (event.key === "Enter" && event.target.tagName === "INPUT") { event.preventDefault(); applyImage(); }
    });
    fields.reset.addEventListener("click", function () { crop = { x: 0, y: 0, w: 1, h: 1 }; drawCrop(); });
    fields.box.addEventListener("pointerdown", function (event) {
      if (event.button !== 0) return;
      event.preventDefault();
      var target = event.target, mode = cropMode(target), from = crop;
      var rect = fields.stage.getBoundingClientRect(), x0 = event.clientX, y0 = event.clientY;
      try { target.setPointerCapture(event.pointerId); } catch (e) { /* the drag still follows the box */ }
      function move(e) {
        crop = moveCrop(from, mode, (e.clientX - x0) / rect.width, (e.clientY - y0) / rect.height);
        drawCrop();
      }
      function stop() {
        target.removeEventListener("pointermove", move);
        target.removeEventListener("pointerup", stop);
        target.removeEventListener("pointercancel", stop);
      }
      target.addEventListener("pointermove", move);
      target.addEventListener("pointerup", stop);
      target.addEventListener("pointercancel", stop);
    });
    fields.box.addEventListener("keydown", function (event) {
      var step = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] }[event.key];
      if (!step) return;
      event.preventDefault();
      var size = event.shiftKey ? 0.1 : 0.01;
      crop = moveCrop(crop, cropMode(event.target), step[0] * size, step[1] * size);
      drawCrop();
    });
  }

  function editableImage(img) { return !img.closest(".bw-embed, .bw-video"); }
  preview.addEventListener("click", function (event) {
    var img = event.target.closest ? event.target.closest("img") : null;
    if (!img || !editableImage(img)) return;
    event.preventDefault();
    var src = img.getAttribute("src") || "";
    var shown = Array.prototype.filter.call(preview.querySelectorAll("img"), function (other) {
      return editableImage(other) && sameSrc(other.getAttribute("src") || "", src);
    });
    var tokens = imageTokens(area.value).filter(function (t) { return sameSrc(t.src, src); });
    // The same picture can appear twice: the n-th copy in the preview is the n-th in the source.
    var token = tokens.length === shown.length ? tokens[shown.indexOf(img)] : null;
    if (token) openImageDialog(token);
    else BW.toast(BW.t("pages.image_not_found"), "error");
  });

  // Dialogs: link, table, video
  function openDialog(id) {
    // By tag: the preview, before the dialogs, keeps the ids of its headings.
    var dialog = document.querySelector("dialog#" + CSS.escape(id));
    if (id === "dlg-link") prepareLinkDialog();
    if (dialog && dialog.showModal) dialog.showModal();
  }
  var linkText = document.querySelector("[data-link-text]");
  var linkUrl = document.querySelector("[data-link-url]");
  var linkSearch = document.querySelector("[data-link-search]");
  var linkResults = document.querySelector("[data-link-results]");
  var linkDialog = document.querySelector("dialog#dlg-link");
  var searchTimer = null, searchController = null, searchVersion = 0;
  function cancelLinkSearch() {
    clearTimeout(searchTimer);
    if (searchController) searchController.abort();
    searchVersion += 1;
  }
  function prepareLinkDialog() {
    cancelLinkSearch();
    linkText.value = area.value.slice(area.selectionStart, area.selectionEnd);
    linkUrl.value = "";
    linkSearch.value = "";
    linkResults.textContent = "";
  }
  document.querySelectorAll("[data-md-dialog='link']").forEach(function (b) {
    b.addEventListener("click", prepareLinkDialog);
  });
  if (linkDialog) linkDialog.addEventListener("close", cancelLinkSearch);
  if (linkSearch) {
    linkSearch.addEventListener("input", function () {
      cancelLinkSearch();
      linkResults.textContent = "";
      var q = linkSearch.value.trim();
      if (!q) return;
      var version = searchVersion;
      searchTimer = setTimeout(function () {
        searchController = window.AbortController ? new AbortController() : null;
        BW.fetchJSON(config.pageSearch + "?include_home=1&q=" + encodeURIComponent(q), {
          signal: searchController ? searchController.signal : undefined
        }).then(function (pages) {
          if (version !== searchVersion || (linkDialog && !linkDialog.open)) return;
          pages.forEach(function (p) {
            var li = document.createElement("li");
            var b = document.createElement("button");
            b.type = "button";
            b.textContent = p.category_name ? p.title + " · " + p.category_name : p.title;
            b.addEventListener("click", function () {
              linkUrl.value = p.is_home ? "/" : "/page/" + p.slug;
              if (!linkText.value) linkText.value = p.title;
            });
            li.appendChild(b);
            linkResults.appendChild(li);
          });
        }).catch(function (error) {
          if (version !== searchVersion || error.name === "AbortError" || (linkDialog && !linkDialog.open)) return;
          var message = document.createElement("li");
          message.textContent = error.message || BW.t("error");
          linkResults.appendChild(message);
        });
      }, 250);
    });
  }

  function closeDialog(button) { var d = button.closest("dialog"); if (d) d.close(); }

  var inserters = {
    link: function () {
      var url = linkUrl.value.trim();
      if (!url) return false;
      var text = (linkText.value.trim() || url).replace(/[\[\]]/g, "");
      area.setRangeText("[" + text + "](" + url.replace(/[()\s]/g, encodeURIComponent) + ")",
                        area.selectionStart, area.selectionEnd, "end");
      changed();
      return true;
    },
    table: function () {
      var rows = Math.min(50, Math.max(1, parseInt(document.querySelector("[data-table-rows]").value, 10) || 3));
      var cols = Math.min(12, Math.max(1, parseInt(document.querySelector("[data-table-cols]").value, 10) || 3));
      var head = [], line = [], body = [];
      for (var c = 1; c <= cols; c++) { head.push(BW.t("pages.editor_column", { n: c })); line.push("---"); }
      for (var r = 0; r < rows; r++) body.push("| " + new Array(cols).fill("   ").join(" | ") + " |");
      insertBlock(["| " + head.join(" | ") + " |", "| " + line.join(" | ") + " |"].concat(body).join("\n"));
      return true;
    },
    video: function () {
      var url = document.querySelector("[data-video-url]").value.trim();
      if (!/^https?:\/\//i.test(url) || /["\]]/.test(url)) { BW.toast(BW.t("pages.video_invalid"), "error"); return false; }
      var attrs = ['url="' + url + '"', 'align="' + document.querySelector("[data-video-align]").value + '"',
                   'ratio="' + document.querySelector("[data-video-ratio]").value + '"'];
      var width = document.querySelector("[data-video-width]").value;
      if (width) attrs.push('width="' + width + '"');
      insertBlock("[[video " + attrs.join(" ") + "]]");
      return true;
    }
  };
  document.querySelectorAll("[data-insert]").forEach(function (button) {
    button.addEventListener("click", function () {
      if (inserters[button.getAttribute("data-insert")]()) { closeDialog(button); area.focus(); }
    });
  });

  // Presence and leaving
  var othersBadge = document.querySelector("[data-presence-editor]");
  function heartbeat() {
    if (!config.heartbeat || document.visibilityState === "hidden") return;
    BW.fetchJSON(config.heartbeat, { method: "POST", body: {} }).then(function (data) {
      if (!othersBadge) return;
      var names = data.editors || [];
      othersBadge.hidden = names.length === 0;
      othersBadge.textContent = names.length ? BW.t("pages.presence", { names: names.join(", ") }) : "";
    }).catch(function () {});
  }
  if (config.heartbeat) { heartbeat(); setInterval(heartbeat, 20000); }

  form.addEventListener("submit", function (event) {
    if (!event.defaultPrevented) submitting = true;
  });
  window.addEventListener("beforeunload", function (e) {
    if (config.stop && navigator.sendBeacon) {
      var data = new FormData();
      data.append("csrf_token", BW.csrf());
      navigator.sendBeacon(config.stop, data);
    }
    if (!submitting && draftState() !== initial) { e.preventDefault(); e.returnValue = ""; }
  });

  count();
  renderPreview();
})();
