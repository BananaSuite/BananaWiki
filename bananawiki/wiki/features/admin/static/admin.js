/* Administration forms: show only the fields that apply to the current choice.
 *
 *   <div data-reveal-for="name" data-reveal-values="a b">  shown only while the form field
 *     "name" (select, radio group or checkbox; an unchecked box counts as "") has one of the values.
 *   <select name="base_role" data-base-role>  hides [data-editor-only] items unless "editor".
 */
(function () {
  "use strict";

  function valueOf(form, name) {
    var field = form.elements[name];
    if (!field) return "";
    if (field.type === "checkbox") return field.checked ? field.value : "";
    if (field.length !== undefined && field.tagName !== "SELECT") {
      for (var i = 0; i < field.length; i++) if (field[i].checked) return field[i].value;
      return "";
    }
    return field.value;
  }

  function refresh(form) {
    form.querySelectorAll("[data-reveal-for]").forEach(function (el) {
      var values = (el.getAttribute("data-reveal-values") || "").split(" ");
      el.hidden = values.indexOf(valueOf(form, el.getAttribute("data-reveal-for"))) === -1;
    });
    var base = form.querySelector("[data-base-role]");
    if (base) {
      form.querySelectorAll("[data-editor-only]").forEach(function (el) {
        el.hidden = base.value !== "editor";
      });
    }
  }

  BW.onReady(function () {
    var jump = document.querySelector("[data-admin-jump]");
    if (jump) {
      jump.addEventListener("change", function () {
        if (jump.value) window.location.href = jump.value;
      });
    }
    var find = document.querySelector("[data-admin-find]");
    if (find) {
      find.addEventListener("input", function () {
        var needle = find.value.trim().toLowerCase();
        var shown = 0;
        document.querySelectorAll("[data-admin-group]").forEach(function (group) {
          var inGroup = 0;
          group.querySelectorAll("[data-admin-link]").forEach(function (link) {
            var match = !needle || link.textContent.toLowerCase().indexOf(needle) !== -1;
            link.hidden = !match;
            if (match) inGroup++;
          });
          group.hidden = !inGroup;
          shown += inGroup;
        });
        var none = document.querySelector("[data-admin-find-none]");
        if (none) none.hidden = !!shown;
      });
    }
    document.querySelectorAll("form").forEach(function (form) {
      if (!form.querySelector("[data-reveal-for], [data-base-role]")) return;
      form.addEventListener("change", function () { refresh(form); });
      refresh(form);
    });
  });
})();
