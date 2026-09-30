/* Replace the editor text with an uploaded Markdown file (the page is saved only when the form is). */
(function () {
  "use strict";
  var box = document.querySelector("[data-md-import]");
  var input = box && box.querySelector("[data-md-import-input]");
  var content = document.getElementById("editor-content");
  var title = document.getElementById("editor-title");
  if (!box || !input || !content || !window.BW) return;
  box.hidden = false;
  input.addEventListener("change", function () {
    var file = input.files && input.files[0];
    if (!file) return;
    if (content.value.trim() && !window.confirm(BW.t("page_export.replace_confirm"))) { input.value = ""; return; }
    var data = new FormData();
    data.append("import_file", file);
    BW.fetchJSON(box.getAttribute("data-url"), { method: "POST", body: data }).then(function (result) {
      content.value = result.content || "";
      content.dispatchEvent(new Event("input", { bubbles: true }));
      if (result.title && title && !title.readOnly) {
        title.value = result.title;
        title.dispatchEvent(new Event("input", { bubbles: true }));
      }
      BW.toast(BW.t("page_export.imported"), "success");
    }).catch(function (error) {
      BW.toast(error.message || BW.t("error"), "error");
    }).then(function () { input.value = ""; });
  });
})();
