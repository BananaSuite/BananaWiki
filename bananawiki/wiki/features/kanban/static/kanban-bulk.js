/* Kanban bulk selection: pick tickets (and columns) and change them together.
 * Depends on kanban-board.js (window.BWKanban). */
(function () {
  "use strict";

  var BW = window.BW;
  var K = window.BWKanban;
  var toggle = document.getElementById("kanban-bulk-toggle");
  var panel = document.getElementById("kanban-bulk");
  if (!K || !toggle || !panel) return;

  var active = false;
  var tickets = new Set();
  var columns = new Set();
  function $(id) { return document.getElementById(id); }

  function checkbox(kind, id, label, checked) {
    var box = K.el("input", "kanban-card__check");
    box.type = "checkbox";
    box.checked = checked;
    box.dataset.select = kind;
    box.dataset.id = id;
    box.setAttribute("aria-label", label);
    return box;
  }

  K.cardHooks.push(function (li, ticket) {
    if (!active) return;
    li.classList.toggle("is-selected", tickets.has(ticket.id));
    li.appendChild(checkbox("ticket", ticket.id, BW.t("kanban.select_ticket", { title: ticket.title }), tickets.has(ticket.id)));
  });

  function decorateColumns() {
    document.querySelectorAll(".kanban-column[data-column-id]").forEach(function (section) {
      var head = section.querySelector(".kanban-column__head");
      var old = head.querySelector("[data-select='column']");
      if (old) old.remove();
      if (!active) return;
      var id = Number(section.dataset.columnId);
      var col = K.column(id);
      head.insertBefore(checkbox("column", id, BW.t("kanban.select_column", { title: col ? col.title : "" }), columns.has(id)), head.firstChild);
    });
  }

  function fillSelects() {
    var target = $("kanban-bulk-column");
    target.textContent = "";
    K.state.columns.forEach(function (col) {
      var option = K.el("option", "", col.title);
      option.value = col.id;
      target.appendChild(option);
    });
    var people = $("kanban-bulk-assignee");
    if (!people.options.length) {
      var nobody = K.el("option", "", BW.t("kanban.nobody"));
      nobody.value = "";
      people.appendChild(nobody);
      K.assignable.forEach(function (person) {
        var option = K.el("option", "", person.username);
        option.value = person.id;
        people.appendChild(option);
      });
    }
  }

  function prune() {
    tickets.forEach(function (id) { if (!K.ticket(id)) tickets.delete(id); });
    columns.forEach(function (id) { if (!K.column(id)) columns.delete(id); });
  }

  function summary() {
    $("kanban-bulk-count").textContent = BW.t("kanban.bulk_selected", { tickets: tickets.size, columns: columns.size });
  }

  K.onRefresh.push(function () {
    if (!active) return;
    prune();
    decorateColumns();
    fillSelects();
    summary();
  });

  toggle.addEventListener("click", function () {
    active = !active;
    tickets.clear();
    columns.clear();
    panel.hidden = !active;
    toggle.setAttribute("aria-pressed", active ? "true" : "false");
    K.refresh();
    if (!active) decorateColumns();
  });

  document.getElementById("kanban-board").addEventListener("change", function (event) {
    var box = event.target;
    if (!box.dataset || !box.dataset.select) return;
    var set = box.dataset.select === "ticket" ? tickets : columns;
    var id = Number(box.dataset.id);
    if (box.checked) set.add(id); else set.delete(id);
    var card = box.closest(".kanban-card");
    if (card) card.classList.toggle("is-selected", box.checked);
    summary();
  });

  function send(path, body) {
    return K.api("/" + K.boardId + path, "POST", body).then(function (result) {
      BW.toast(BW.t("kanban.bulk_done", { count: result.updated }), "success");
      tickets.clear();
      columns.clear();
      return K.reload();
    }).then(function () {
      if (K.loadArchive) return K.loadArchive();
      return null;
    }).catch(K.fail);
  }

  panel.addEventListener("click", function (event) {
    var button = event.target.closest("[data-bulk]");
    if (!button) return;
    var action = button.dataset.bulk;
    if (action === "delete_columns") {
      if (!columns.size) { BW.toast(BW.t("kanban.bulk_none"), "warning"); return; }
      if (!window.confirm(BW.t("kanban.confirm_bulk_columns", { count: columns.size }))) return;
      send("/columns/bulk", { action: "delete", column_ids: Array.from(columns) });
      return;
    }
    if (!tickets.size) { BW.toast(BW.t("kanban.bulk_none"), "warning"); return; }
    var body = { ticket_ids: Array.from(tickets), action: action };
    if (action === "move") body.column_id = Number($("kanban-bulk-column").value);
    if (action === "priority") body.priority = $("kanban-bulk-priority").value;
    if (action === "assign") body.assignees = $("kanban-bulk-assignee").value ? [$("kanban-bulk-assignee").value] : [];
    if (action === "due_date") body.due_date = $("kanban-bulk-due").value;
    if (action === "color") body.color = $("kanban-bulk-color").value;
    if (action === "clear_color") { body.action = "color"; body.color = ""; }
    if (action === "delete" && !window.confirm(BW.t("kanban.confirm_bulk_tickets", { count: tickets.size }))) return;
    send("/tickets/bulk", body);
  });
})();
