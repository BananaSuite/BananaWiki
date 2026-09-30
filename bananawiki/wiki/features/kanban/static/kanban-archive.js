/* Kanban archive panel: the board's archived tickets, to open, restore or delete for good.
 * Depends on kanban-board.js (window.BWKanban); kanban-ticket.js opens a ticket. */
(function () {
  "use strict";

  var BW = window.BW;
  var K = window.BWKanban;
  var toggle = document.getElementById("kanban-archive-toggle");
  var panel = document.getElementById("kanban-archive");
  var list = document.getElementById("kanban-archive-list");
  var badge = document.getElementById("kanban-archive-count");
  if (!K || !toggle || !panel || !list) return;

  var el = K.el;

  function showCount() {
    if (badge) badge.textContent = String(K.state.archived_count || 0);
  }

  function button(text, label, className) {
    var node = el("button", "btn btn--small" + (className ? " " + className : ""), text);
    node.type = "button";
    node.setAttribute("aria-label", label);
    return node;
  }

  function render(tickets, total) {
    list.textContent = "";
    K.state.archived_count = total;
    showCount();
    if (!tickets.length) {
      list.appendChild(el("li", "muted", BW.t("kanban.archived_empty")));
      return;
    }
    tickets.forEach(function (ticket) {
      var li = el("li", "kanban-archive__item");
      li.dataset.ticketId = ticket.id;
      var main = el("div", "kanban-archive__main");
      var open = el("button", "kanban-card__open", ticket.title);
      open.type = "button";
      open.dataset.archived = "open";
      main.appendChild(open);
      main.appendChild(el("p", "small muted", BW.t("kanban.archived_meta", {
        column: ticket.column_title, date: (ticket.archived_at || "").slice(0, 10),
        username: ticket.archived_by_username || BW.t("kanban.someone")
      })));
      li.appendChild(main);
      if (K.canWrite) {
        var tools = el("div", "kanban-archive__tools");
        var restore = button(BW.t("kanban.restore"), BW.t("kanban.restore_ticket", { title: ticket.title }));
        restore.dataset.archived = "restore";
        var remove = button(BW.t("kanban.delete_forever"), BW.t("kanban.delete_forever_ticket", { title: ticket.title }), "btn--danger");
        remove.dataset.archived = "delete";
        tools.appendChild(restore);
        tools.appendChild(remove);
        li.appendChild(tools);
      }
      list.appendChild(li);
    });
  }

  function load() {
    return K.api("/" + K.boardId + "/archived").then(function (result) { render(result.tickets, result.total); }).catch(K.fail);
  }

  K.loadArchive = function () { return panel.hidden ? Promise.resolve() : load(); };

  // After the list is redrawn, focus the row that took the place of the one just handled.
  function focusRow(index) {
    var rows = list.querySelectorAll("[data-ticket-id]");
    var row = rows[Math.min(index, rows.length - 1)];
    var target = row && (row.querySelector("[data-archived='restore']") || row.querySelector("button"));
    (target || toggle).focus();
  }

  list.addEventListener("click", function (event) {
    var target = event.target.closest("[data-archived]");
    if (!target) return;
    var li = target.closest("[data-ticket-id]");
    var ticketId = Number(li.dataset.ticketId);
    var title = li.querySelector("[data-archived='open']").textContent;
    var index = Array.prototype.indexOf.call(list.children, li);
    if (target.dataset.archived === "open") {
      if (K.openTicket) K.openTicket(ticketId);
    } else if (target.dataset.archived === "restore") {
      K.api("/tickets/" + ticketId + "/restore", "POST").then(function (result) {
        if (result.ticket) K.upsertTicket(result.ticket, null);
        K.refresh();
        K.announce(BW.t("kanban.ticket_restored", { title: title }));
        return load();
      }).then(function () { focusRow(index); }).catch(K.fail);
    } else if (target.dataset.archived === "delete") {
      if (!window.confirm(BW.t("kanban.confirm_delete_forever", { title: title }))) return;
      K.api("/tickets/" + ticketId, "DELETE").then(function () {
        K.announce(BW.t("kanban.ticket_deleted"));
        return load();
      }).then(function () { focusRow(index); }).catch(K.fail);
    }
  });

  toggle.addEventListener("click", function () {
    panel.hidden = !panel.hidden;
    toggle.setAttribute("aria-expanded", panel.hidden ? "false" : "true");
    if (!panel.hidden) load();
  });

  K.onRefresh.push(showCount);
  document.addEventListener("kanban:archived", function () { K.loadArchive(); });
  document.addEventListener("kanban:events", function (event) {
    var relevant = event.detail.some(function (item) {
      return item.op === "tickets_archived" || item.op === "ticket_deleted" || (item.payload && item.payload.restored);
    });
    if (relevant) K.loadArchive();
  });
})();
