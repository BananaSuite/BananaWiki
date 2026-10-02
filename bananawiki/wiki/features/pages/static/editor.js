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

  // Text helpers ---------------------------------------------------------------
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
    image: function () { if (fileInput) fileInput.click(); }
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

  // Live preview and counters --------------------------------------------------
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
        preview.replaceChildren(holder.content);
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

  // Images ---------------------------------------------------------------------
  function upload(files) {
    if (!config.upload) return;
    Array.prototype.forEach.call(files, function (file) {
      if (!/^image\//.test(file.type)) return;
      var body = new FormData();
      body.append("file", file);
      uploadStatus.textContent = BW.t("pages.uploading", { name: file.name });
      BW.fetchJSON(config.upload, { method: "POST", body: body }).then(function (data) {
        var alt = file.name.replace(/\.[^.]+$/, "").replace(/[\[\]]/g, "");
        insertBlock("![" + alt + "](" + data.url + ")");
        uploadStatus.textContent = BW.t("pages.uploaded", { name: file.name });
      }).catch(function (error) {
        uploadStatus.textContent = "";
        BW.toast(error.message || BW.t("error"), "error");
      });
    });
  }
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

  // Dialogs: link, table, video --------------------------------------------------
  function openDialog(id) {
    var dialog = document.getElementById(id);
    if (id === "dlg-link") prepareLinkDialog();
    if (dialog && dialog.showModal) dialog.showModal();
  }
  var linkText = document.querySelector("[data-link-text]");
  var linkUrl = document.querySelector("[data-link-url]");
  var linkSearch = document.querySelector("[data-link-search]");
  var linkResults = document.querySelector("[data-link-results]");
  var linkDialog = document.getElementById("dlg-link");
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

  // Presence and leaving ---------------------------------------------------------
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
