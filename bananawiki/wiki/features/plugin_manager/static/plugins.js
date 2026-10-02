/* Feature search and sidebar order: enhancements to the server-rendered lists.
 * App order supports drag and drop, and up/down buttons without a page reload.
 * Without JavaScript the up/down buttons submit the form and the server moves the item. */
(function () {
  "use strict";
  var search = document.querySelector("[data-feature-search]");
  if (search) {
    var filter = search.querySelector("input");
    var status = search.querySelector("[data-feature-search-status]");
    var rows = Array.prototype.slice.call(document.querySelectorAll("[data-feature-entry]"));
    var core = document.querySelector("[data-core-features]");
    var coreWasOpen = false;
    var searching = false;
    search.hidden = false;
    filter.addEventListener("input", function () {
      var query = filter.value.trim().toLocaleLowerCase();
      if (query && !searching && core) coreWasOpen = core.open;
      var count = 0;
      var coreMatches = 0;
      rows.forEach(function (row) {
        row.hidden = row.getAttribute("data-feature-search-text").toLocaleLowerCase().indexOf(query) === -1;
        if (!row.hidden) {
          count += 1;
          if (core && core.contains(row)) coreMatches += 1;
        }
      });
      if (core) {
        core.hidden = !!query && !coreMatches;
        if (query) core.open = !!coreMatches;
        else if (searching) core.open = coreWasOpen;
      }
      searching = !!query;
      status.hidden = !query;
      status.textContent = BW.t("plugin_manager.filter.matches", {count: count});
    });
  }
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
