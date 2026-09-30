/* Canvas list: reorder with the ▲/▼ buttons (keyboard and touch) or by
 * dragging a card, save the order, and reload when someone else changes the
 * shared order (open access). */
(function () {
  "use strict";

  var POLL_MS = 15000;

  BW.onReady(function () {
    var list = document.getElementById("canvas-list");
    if (!list || !list.dataset.orderUrl) return;
    var version = list.dataset.orderVersion;
    var dragged = null;

    function save() {
      var ids = Array.prototype.map.call(list.children, function (item) { return Number(item.dataset.layoutId); });
      BW.fetchJSON(list.dataset.orderUrl, { method: "POST", body: { layout_ids: ids } }).then(function (res) {
        version = String(res.list_order_version);
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

    if (list.hasAttribute("data-shared-order")) {
      window.setInterval(function () {
        if (document.hidden || dragged) return;
        BW.fetchJSON(list.dataset.versionUrl).then(function (res) {
          if (String(res.list_order_version) !== version) window.location.reload();
        }).catch(function () { /* try again later */ });
      }, POLL_MS);
    }
  });
})();
