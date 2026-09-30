/* Custom page editor: show only the form sections the chosen content type uses. */
(function () {
  "use strict";
  var select = document.querySelector("[data-cp-type]");
  var data = document.getElementById("cp-type-fields");
  if (!select || !data) return;
  var fields = JSON.parse(data.textContent || "{}");
  var help = document.querySelector("[data-cp-type-help]");

  function update() {
    var used = fields[select.value] || [];
    document.querySelectorAll("[data-cp-section]").forEach(function (section) {
      section.hidden = used.indexOf(section.getAttribute("data-cp-section")) === -1;
    });
    var option = select.options[select.selectedIndex];
    if (help && option) help.textContent = option.getAttribute("data-help") || "";
  }

  select.addEventListener("change", update);
  update();
}());
