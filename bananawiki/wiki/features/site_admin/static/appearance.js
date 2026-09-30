/* Live preview of the colour sets on the appearance page. */
(function () {
  "use strict";
  function apply(input) {
    var preview = document.querySelector('[data-preview="' + input.getAttribute("data-preview-mode") + '"]');
    if (preview) preview.style.setProperty(input.getAttribute("data-preview-var"), input.value);
  }
  function init() {
    var inputs = document.querySelectorAll("[data-theme-editor] input[type=color]");
    Array.prototype.forEach.call(inputs, function (input) {
      apply(input);
      input.addEventListener("input", function () { apply(input); });
    });
  }
  if (document.readyState !== "loading") init();
  else document.addEventListener("DOMContentLoaded", init);
})();
