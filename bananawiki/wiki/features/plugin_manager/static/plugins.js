/* Sidebar app order: drag and drop, and up/down buttons without a page reload.
 * Without JavaScript the up/down buttons submit the form and the server moves the item. */
(function () {
  "use strict";
  var form = document.querySelector("[data-sidebar-order]");
  if (!form) return;
  var list = form.querySelector("[data-order-list]");
  var dragged = null;

  function items() { return Array.prototype.slice.call(list.querySelectorAll(".pm-order__item")); }

  function refreshButtons() {
    var all = items();
    all.forEach(function (item, index) {
      item.querySelector("[data-move='up']").disabled = index === 0;
      item.querySelector("[data-move='down']").disabled = index === all.length - 1;
    });
  }

  form.addEventListener("click", function (event) {
    var button = event.target.closest("[data-move]");
    if (!button) return;
    event.preventDefault();
    var item = button.closest(".pm-order__item");
    if (button.getAttribute("data-move") === "up" && item.previousElementSibling) {
      list.insertBefore(item, item.previousElementSibling);
    } else if (button.getAttribute("data-move") === "down" && item.nextElementSibling) {
      list.insertBefore(item.nextElementSibling, item);
    }
    refreshButtons();
    var target = item.querySelector("[data-move='" + button.getAttribute("data-move") + "']");
    (target.disabled ? item.querySelector("[data-move]:not([disabled])") : target).focus();
  });

  list.addEventListener("dragstart", function (event) {
    dragged = event.target.closest(".pm-order__item");
    if (!dragged) return;
    dragged.classList.add("is-dragging");
    event.dataTransfer.effectAllowed = "move";
    event.dataTransfer.setData("text/plain", dragged.getAttribute("data-id"));
  });

  list.addEventListener("dragover", function (event) {
    var over = event.target.closest(".pm-order__item");
    if (!dragged || !over || over === dragged) return;
    event.preventDefault();
    items().forEach(function (item) { item.classList.toggle("is-over", item === over); });
    var box = over.getBoundingClientRect();
    list.insertBefore(dragged, event.clientY > box.top + box.height / 2 ? over.nextElementSibling : over);
  });

  list.addEventListener("dragend", function () {
    if (dragged) dragged.classList.remove("is-dragging");
    items().forEach(function (item) { item.classList.remove("is-over"); });
    dragged = null;
    refreshButtons();
  });
})();
