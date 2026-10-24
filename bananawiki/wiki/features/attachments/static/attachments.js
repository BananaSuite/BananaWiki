/* Editor attachments: add files (picked, dropped or from the toolbar's File button) and delete them
   without leaving the editor, with a progress bar per file. "Insert link" puts a link to a file into
   the text at the cursor (through the editor's bw:editor-insert event). */
(function () {
  "use strict";
  var box = document.querySelector("[data-attachments]");
  if (!box || !window.BW) return;
  var list = box.querySelector("[data-attachments-list]");
  var input = box.querySelector("[data-attachments-input]");
  var status = box.querySelector("[data-attachments-status]");
  var progressHost = box.querySelector("[data-attachments-progress]");
  var drop = box.querySelector("[data-attachments-drop]");
  var createButton = box.querySelector("[data-attachments-create]");
  var uploadUrl = box.getAttribute("data-upload-url");
  var rules = BW.uploadRules(box);
  if (!rules.maxBytes) rules.maxBytes = parseInt(box.getAttribute("data-max-bytes"), 10) || 0;
  var editor = document.querySelector("[data-editor]");
  var queue = [];
  var busy = false;
  var added = 0;
  var hint = status ? status.textContent : "";

  function say(text) { if (status) status.textContent = text; }

  function insertLink(name, url) {
    if (!editor) return;
    var label = name.replace(/[\[\]]/g, "");
    editor.dispatchEvent(new CustomEvent("bw:editor-insert", { detail: { text: "[" + label + "](" + url + ")", inline: true } }));
  }

  function showInsertButtons() {
    if (!editor || !list) return;
    list.querySelectorAll("[data-attachment-insert]").forEach(function (button) { button.hidden = false; });
  }

  function addRow(item) {
    var row = document.createElement("li");
    row.className = "attachments__item";
    row.setAttribute("data-attachment", "");
    var link = document.createElement("a");
    link.href = item.download_url;
    link.setAttribute("download", "");
    link.setAttribute("data-attachment-link", "");
    link.textContent = item.name;
    var size = document.createElement("span");
    size.className = "small muted";
    size.textContent = BW.formatBytes(item.size);
    var actions = document.createElement("span");
    actions.className = "attachments__actions";
    if (editor) {
      var insert = document.createElement("button");
      insert.type = "button";
      insert.className = "btn btn--small btn--ghost";
      insert.setAttribute("data-attachment-insert", "");
      insert.textContent = BW.t("attachments.insert_link");
      actions.appendChild(insert);
    }
    if (item.delete_url) {
      var remove = document.createElement("button");
      remove.type = "button";
      remove.className = "btn btn--small btn--danger-quiet";
      remove.setAttribute("data-attachment-delete", item.delete_url);
      remove.setAttribute("data-name", item.name);
      remove.textContent = BW.t("attachments.delete");
      actions.appendChild(remove);
    }
    row.appendChild(link);
    row.appendChild(size);
    row.appendChild(actions);
    list.appendChild(row);
  }

  // One file at a time: the server counts uploads per minute, and one bar moving is easier to follow.
  function next() {
    if (busy) return;
    var job = queue.shift();
    if (!job) { say(added ? BW.t("attachments.done") : hint); added = 0; return; }
    var file = job.file;
    busy = true;
    var row = BW.uploadRow(progressHost || box, file.name);
    var problem = BW.uploadProblem(file, rules);
    if (problem) {
      row.fail(problem);
      busy = false;
      next();
      return;
    }
    var data = new FormData();
    data.append("file", file);
    BW.upload(uploadUrl, data, { onProgress: row.progress, onRetry: row.retrying, signal: row.signal }).then(function (item) {
      row.done();
      added += 1;
      addRow(item);
      if (job.link) insertLink(item.name, item.download_url);
    }, function (error) {
      row.fail(error.message, error.aborted);
    }).then(function () {
      busy = false;
      next();
    });
  }

  // *link*: the files were dropped or pasted into the text, so a link to each goes there too.
  function add(files, link) {
    if (!uploadUrl || !files || !files.length) return;
    Array.prototype.forEach.call(files, function (file) { queue.push({ file: file, link: !!link }); });
    next();
  }
  if (editor && uploadUrl) {
    editor.addEventListener("bw:editor-files", function (event) {
      event.preventDefault();
      add(event.detail.files, true);
    });
  }

  if (input && uploadUrl) {
    input.addEventListener("change", function () { add(Array.prototype.slice.call(input.files || [])); input.value = ""; });
  }
  if (drop && uploadUrl) {
    var hasFiles = function (event) {
      return event.dataTransfer && Array.prototype.indexOf.call(event.dataTransfer.types || [], "Files") !== -1;
    };
    box.addEventListener("dragover", function (event) {
      if (!hasFiles(event)) return;
      event.preventDefault();
      box.classList.add("is-over");
    });
    box.addEventListener("dragleave", function (event) {
      if (!box.contains(event.relatedTarget)) box.classList.remove("is-over");
    });
    box.addEventListener("drop", function (event) {
      box.classList.remove("is-over");
      if (!hasFiles(event)) return;
      event.preventDefault();
      add(event.dataTransfer.files);
    });
  }

  // The toolbar's File button: pick files here, or (on a page not created yet) come to this panel.
  var attach = editor && editor.querySelector("[data-editor-attach]");
  if (attach && (input || createButton)) {
    attach.hidden = false;
    attach.addEventListener("click", function () {
      if (input && uploadUrl) { input.click(); return; }
      box.scrollIntoView({ block: "center" });
      if (createButton) createButton.focus();
    });
  }

  if (!list) return;
  showInsertButtons();
  list.addEventListener("click", function (event) {
    var insert = event.target.closest("[data-attachment-insert]");
    if (insert) {
      var row = insert.closest("[data-attachment]");
      var link = row && row.querySelector("[data-attachment-link]");
      if (link) insertLink(link.textContent, link.getAttribute("href"));
      return;
    }
    var button = event.target.closest("[data-attachment-delete]");
    if (!button) return;
    if (!window.confirm(BW.t("attachments.confirm_delete", { name: button.getAttribute("data-name") }))) return;
    button.disabled = true;
    BW.fetchJSON(button.getAttribute("data-attachment-delete"), { method: "DELETE" }).then(function () {
      var row = button.closest("[data-attachment]");
      if (row) row.remove();
    }).catch(function (error) {
      button.disabled = false;
      BW.toast(error.message || BW.t("error"), "error");
    });
  });
})();
