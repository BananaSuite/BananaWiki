/* Editor attachments: upload files and delete them without leaving the editor. */
(function () {
  "use strict";
  var box = document.querySelector("[data-attachments]");
  if (!box || !window.BW) return;
  var list = box.querySelector("[data-attachments-list]");
  var input = box.querySelector("[data-attachments-input]");
  var status = box.querySelector("[data-attachments-status]");
  var uploadUrl = box.getAttribute("data-upload-url");
  var maxBytes = parseInt(box.getAttribute("data-max-bytes"), 10) || 0;

  function say(text) { if (status) status.textContent = text; }

  function deleteButton(url, name) {
    var button = document.createElement("button");
    button.type = "button";
    button.className = "btn btn--small btn--ghost text-danger";
    button.setAttribute("data-attachment-delete", url);
    button.setAttribute("data-name", name);
    button.textContent = BW.t("attachments.delete");
    return button;
  }

  function addRow(item) {
    var row = document.createElement("li");
    row.className = "attachments__item";
    row.setAttribute("data-attachment", "");
    var link = document.createElement("a");
    link.href = item.download_url;
    link.setAttribute("download", "");
    link.textContent = item.name;
    var size = document.createElement("span");
    size.className = "small muted";
    size.textContent = BW.t("attachments.size_kb", { size: (item.size / 1024).toFixed(1) });
    row.appendChild(link);
    row.appendChild(size);
    row.appendChild(deleteButton(item.delete_url, item.name));
    list.appendChild(row);
  }

  function upload(files, index) {
    if (index >= files.length) { say(BW.t("attachments.done")); input.value = ""; return; }
    var file = files[index];
    if (maxBytes && file.size > maxBytes) {
      BW.toast(BW.t("attachments.too_large", { name: file.name }), "error");
      upload(files, index + 1);
      return;
    }
    say(BW.t("attachments.uploading", { name: file.name }));
    var data = new FormData();
    data.append("file", file);
    BW.fetchJSON(uploadUrl, { method: "POST", body: data }).then(function (item) {
      addRow(item);
    }).catch(function (error) {
      BW.toast(error.message || BW.t("error"), "error");
    }).then(function () { upload(files, index + 1); });
  }

  if (input && uploadUrl) {
    input.addEventListener("change", function () { upload(Array.prototype.slice.call(input.files || []), 0); });
  }

  list.addEventListener("click", function (event) {
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
