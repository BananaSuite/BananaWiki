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
    var navigation = document.querySelector("[data-admin-nav]");
    if (navigation) {
      var mobile = window.matchMedia("(max-width: 760px)");
      function resizeNavigation() { navigation.open = !mobile.matches; }
      resizeNavigation();
      mobile.addEventListener("change", resizeNavigation);
    }
    document.querySelectorAll("form").forEach(function (form) {
      if (!form.querySelector("[data-reveal-for], [data-base-role]")) return;
      form.addEventListener("change", function () { refresh(form); });
      refresh(form);
    });
  });
})();
