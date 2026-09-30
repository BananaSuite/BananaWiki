/* Question editor for assessments: add, reorder and remove questions,
 * and hide the options field for free-text questions. The form works
 * without this script (the "Add question" button then reloads the page). */
(function () {
  "use strict";

  var form = document.querySelector("[data-assessment-editor]");
  if (!form) return;
  var list = form.querySelector("[data-question-list]");
  var template = form.querySelector("template[data-question-template]");
  var addButton = form.querySelector("[data-question-add]");

  function nextIndex() {
    var highest = -1;
    list.querySelectorAll("[name^='questions-']").forEach(function (field) {
      var match = /^questions-(\d+)-/.exec(field.name);
      if (match) highest = Math.max(highest, parseInt(match[1], 10));
    });
    return highest + 1;
  }

  function renumber() {
    list.querySelectorAll("[data-question]").forEach(function (question, position, all) {
      var number = question.querySelector("[data-question-number]");
      if (number) number.textContent = String(position + 1);
      question.querySelectorAll("[data-question-move]").forEach(function (button) {
        var step = parseInt(button.getAttribute("data-question-move"), 10);
        button.hidden = false;
        button.disabled = (step < 0 && position === 0) || (step > 0 && position === all.length - 1);
      });
    });
  }

  function syncType(question) {
    var select = question.querySelector("[data-question-type]");
    var options = question.querySelector("[data-options-field]");
    if (select && options) options.hidden = select.value === "free_text";
  }

  function addQuestion() {
    var index = String(nextIndex());
    var html = template.innerHTML.split("__INDEX__").join(index);
    var holder = document.createElement("div");
    holder.innerHTML = html;
    var question = holder.querySelector("[data-question]");
    list.appendChild(question);
    syncType(question);
    renumber();
    var prompt = question.querySelector("textarea");
    if (prompt) prompt.focus();
  }

  if (addButton && template) {
    addButton.addEventListener("click", function (event) {
      event.preventDefault();
      addQuestion();
    });
  }

  form.addEventListener("change", function (event) {
    var question = event.target.closest("[data-question]");
    if (!question) return;
    if (event.target.matches("[data-question-type]")) syncType(question);
    if (event.target.matches("[data-question-remove]")) {
      if (event.target.checked) question.setAttribute("data-removed", "");
      else question.removeAttribute("data-removed");
    }
  });

  form.addEventListener("click", function (event) {
    var button = event.target.closest("[data-question-move]");
    if (!button) return;
    event.preventDefault();
    var question = button.closest("[data-question]");
    var step = parseInt(button.getAttribute("data-question-move"), 10);
    var sibling = step < 0 ? question.previousElementSibling : question.nextElementSibling;
    if (!sibling) return;
    if (step < 0) list.insertBefore(question, sibling);
    else list.insertBefore(sibling, question);
    renumber();
    button.focus();
  });

  list.querySelectorAll("[data-question]").forEach(syncType);
  renumber();
})();
