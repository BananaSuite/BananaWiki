/* Show the custom label and colour fields only while "Custom" is selected. */
(function () {
  "use strict";
  function sync(box) {
    var select = box.querySelector("[data-tag-select]");
    var custom = box.querySelector("[data-tag-custom]");
    if (select && custom) custom.hidden = select.value !== "custom";
  }
  document.querySelectorAll("[data-tag-fields]").forEach(function (box) {
    if (box.hasAttribute("data-tag-ready")) return;
    box.setAttribute("data-tag-ready", "");
    sync(box);
    var select = box.querySelector("[data-tag-select]");
    if (select) select.addEventListener("change", function () { sync(box); });
  });
})();
