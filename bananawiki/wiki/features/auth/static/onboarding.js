/* Onboarding wizard: the feature list only applies to "Choose the features", and the guide
 * options only when the guide is created. Without scripts the hints say the same. */
(function () {
  "use strict";
  var form = document.querySelector("form[data-onboarding]");
  if (!form) return;

  var list = form.querySelector("[data-feature-list]");
  var picked = {};
  function syncFeatures() {
    if (!list) return;
    var mode = form.querySelector("input[name='mode']:checked");
    var recommended = mode && mode.value === "easy";
    list.querySelectorAll("input[type='checkbox'][data-default]").forEach(function (box) {
      if (recommended) {
        if (!list.disabled) picked[box.value] = box.checked;
        box.checked = box.getAttribute("data-default") === "1";
      } else if (Object.prototype.hasOwnProperty.call(picked, box.value)) {
        box.checked = picked[box.value];
      }
    });
    list.disabled = !!recommended;
  }

  var docs = form.querySelector("[data-docs-options]");
  var spawn = form.querySelector("input[name='spawn_docs']");
  function syncDocs() {
    if (docs && spawn) docs.disabled = !spawn.checked;
  }

  form.addEventListener("change", function (event) {
    if (event.target.name === "mode") syncFeatures();
    if (event.target === spawn) syncDocs();
  });
  syncFeatures();
  syncDocs();
})();
