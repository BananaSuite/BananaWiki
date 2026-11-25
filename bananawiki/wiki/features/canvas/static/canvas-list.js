/* Canvas list: reorder with the ↑/↓ buttons (keyboard and touch) or by
 * dragging a card, and save the order (each person has their own). */
(function () {
  "use strict";

  BW.onReady(function () {
    var list = document.querySelector("ul#canvas-list");
    if (!list || !list.dataset.orderUrl) return;
    var dragged = null;
    BW.markMoveEnds(list, "[data-layout-id]");

    function save() {
      var ids = Array.prototype.map.call(list.children, function (item) { return Number(item.dataset.layoutId); });
      BW.fetchJSON(list.dataset.orderUrl, { method: "POST", body: { layout_ids: ids } }).then(function () {
        BW.toast(BW.t("canvas.list.order_saved"), "success");
      }).catch(function (error) { BW.toast(error.message || BW.t("error"), "error"); });
    }

    list.addEventListener("click", function (event) {
      var button = event.target.closest("[data-move]");
      if (!button) return;
      var item = button.closest("[data-layout-id]");
      var up = button.getAttribute("data-move") === "up";
      var sibling = up ? item.previousElementSibling : item.nextElementSibling;
      if (!sibling) return;
      list.insertBefore(item, up ? sibling : sibling.nextElementSibling);
      button.focus();
      BW.markMoveEnds(list, "[data-layout-id]");
      save();
    });

    Array.prototype.forEach.call(list.children, function (item) {
      item.draggable = true;
      item.addEventListener("dragstart", function (event) {
        dragged = item;
        item.classList.add("is-dragging");
        event.dataTransfer.effectAllowed = "move";
        event.dataTransfer.setData("text/plain", item.dataset.layoutId);
      });
      item.addEventListener("dragend", function () {
        item.classList.remove("is-dragging");
        BW.markMoveEnds(list, "[data-layout-id]");
        if (dragged) save();
        dragged = null;
      });
      item.addEventListener("dragover", function (event) {
        if (!dragged || dragged === item) return;
        event.preventDefault();
        var rect = item.getBoundingClientRect();
        var after = event.clientY > rect.top + rect.height / 2;
        list.insertBefore(dragged, after ? item.nextElementSibling : item);
      });
    });
  });
})();
