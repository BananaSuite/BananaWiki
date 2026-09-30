/* Kanban board list: reorder boards (drag, or the arrow buttons) and follow the
 * shared order when open access is on. */
(function () {
  "use strict";

  var BW = window.BW;
  var list = document.getElementById("kanban-board-list");
  if (!list || !BW) return;

  var version = list.dataset.orderVersion;

  function save(focusId) {
    var ids = Array.prototype.map.call(list.querySelectorAll("[data-board-id]"), function (item) {
      return Number(item.dataset.boardId);
    });
    BW.fetchJSON("/api/kanban/board-order", { method: "POST", body: { board_ids: ids } }).then(function (result) {
      version = result.list_order_version;
    }).catch(function (error) { BW.toast(error.message, "error"); });
    if (focusId) {
      var again = list.querySelector("[data-board-id='" + focusId + "'] [data-move]");
      if (again) again.focus();
    }
  }

  if (list.dataset.reorderable === "true") {
    list.addEventListener("click", function (event) {
      var button = event.target.closest("[data-move]");
      if (!button) return;
      var item = button.closest("[data-board-id]");
      var sibling = button.dataset.move === "-1" ? item.previousElementSibling : item.nextElementSibling;
      if (!sibling) return;
      list.insertBefore(item, button.dataset.move === "-1" ? sibling : sibling.nextElementSibling);
      button.focus();
      save();
    });

    var dragged = null;
    list.querySelectorAll("[data-board-id]").forEach(function (item) {
      item.draggable = true;
      item.addEventListener("dragstart", function (event) {
        dragged = item;
        item.classList.add("is-dragging");
        event.dataTransfer.effectAllowed = "move";
        event.dataTransfer.setData("text/plain", item.dataset.boardId);
      });
      item.addEventListener("dragend", function () {
        item.classList.remove("is-dragging");
        list.querySelectorAll(".is-drop-target").forEach(function (other) { other.classList.remove("is-drop-target"); });
      });
      item.addEventListener("dragover", function (event) {
        if (!dragged || dragged === item) return;
        event.preventDefault();
        item.classList.add("is-drop-target");
      });
      item.addEventListener("dragleave", function () { item.classList.remove("is-drop-target"); });
      item.addEventListener("drop", function (event) {
        event.preventDefault();
        item.classList.remove("is-drop-target");
        if (!dragged || dragged === item) return;
        var items = Array.prototype.slice.call(list.children);
        list.insertBefore(dragged, items.indexOf(dragged) < items.indexOf(item) ? item.nextSibling : item);
        dragged = null;
        save();
      });
    });
  }

  // With open access everyone shares one order: reload when someone changes it.
  function checkVersion() {
    if (document.hidden) return;
    BW.fetchJSON("/api/kanban/list-order-version").then(function (result) {
      if (result.list_order_version !== version) location.reload();
    }).catch(function () { /* try again later */ });
  }

  if (list.dataset.openAccess === "true") {
    setInterval(checkVersion, 30000);
    document.addEventListener("visibilitychange", checkVersion);
  }
})();
