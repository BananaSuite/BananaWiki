/* Kanban board: rendering, columns, tickets, drag and drop, keyboard moves and live sync.
 *
 * The page embeds the board state as JSON (#kanban-data). Everything is drawn
 * from that state; changes made here or by other people (through the
 * /sync event log) update the state and redraw the affected parts.
 *
 * Exposes window.BWKanban for kanban-ticket.js and kanban-bulk.js:
 *   K.state, K.canWrite, K.api(path, method, body), K.refresh(), K.reload(),
 *   K.moveTicket(id, columnId, index), K.cardHooks (functions(li, ticket)),
 *   K.onRefresh (callbacks), K.announce(text), K.fail(error),
 *   K.isShown(ticket) (replaced by kanban-filter.js; hidden cards are skipped by keyboard moves),
 *   K.dueState(date) ("overdue", "soon" or ""),
 *   K.lanes (set by kanban-lanes.js to group the board into rows: null, or
 *     { list() -> [{id, label}], of(ticket) -> [lane ids], draggable, change(id, from, to) -> Promise,
 *       create(laneId) -> extra fields for a ticket added in that row })
 */
(function () {
  "use strict";

  var BW = window.BW;
  var dataNode = document.querySelector("script#kanban-data");
  var root = document.getElementById("kanban-board");
  if (!dataNode || !root || !BW) return;

  var data = JSON.parse(dataNode.textContent || "{}");
  var K = window.BWKanban = {
    state: data.state,
    boardId: data.state.board.id,
    seq: data.seq || 0,
    canWrite: !!data.can_write,
    canComment: !!data.can_comment,
    isAdmin: !!data.is_admin,
    userId: data.user_id,
    assignable: data.assignable || [],
    session: Math.random().toString(36).slice(2) + Date.now().toString(36),
    cardHooks: [],
    onRefresh: []
  };
  var PRIORITIES = ["low", "medium", "high", "critical"];
  var CARD_FIELDS = ["id", "column_id", "title", "priority", "due_date", "color", "labels", "assignees",
    "attachment_count", "comment_count", "checklist_total", "checklist_done", "has_description"];
  var dragging = null;
  var renderedLanes = false;

  // ── Helpers ──────────────────────────────────────────────────────────────
  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  }

  K.el = el;

  K.api = function (path, method, body) {
    return BW.fetchJSON(path.charAt(0) === "/" && path.indexOf("/api/") === 0 ? path : "/api/kanban" + path, {
      method: method || "GET",
      body: body,
      headers: { "X-Kanban-Session": K.session }
    });
  };

  K.announce = function (text) {
    var live = document.getElementById("kanban-live");
    if (live) { live.textContent = ""; setTimeout(function () { live.textContent = text; }, 30); }
  };

  K.fail = function (error) {
    BW.toast((error && error.message) || BW.t("error"), "error");
  };

  function column(id) {
    for (var i = 0; i < K.state.columns.length; i++) {
      if (K.state.columns[i].id === id) return K.state.columns[i];
    }
    return null;
  }

  function columnOfTicket(ticketId) {
    for (var i = 0; i < K.state.columns.length; i++) {
      if (K.state.columns[i].tickets.indexOf(ticketId) !== -1) return K.state.columns[i];
    }
    return null;
  }

  K.column = column;
  K.columnOfTicket = columnOfTicket;
  K.ticket = function (id) { return K.state.tickets[String(id)] || null; };
  // The card fields of a ticket detail (what the board keeps for every ticket).
  K.cardFrom = function (detail) {
    var result = {};
    CARD_FIELDS.forEach(function (name) { result[name] = detail[name]; });
    return result;
  };

  function attr(value) {
    return window.CSS && CSS.escape ? CSS.escape(String(value)) : String(value).replace(/["'\\]/g, "\\$&");
  }

  function inLane(ticket, lane) {
    return !K.lanes || !lane || K.lanes.of(ticket).indexOf(lane) !== -1;
  }

  function isoDate(moment) {
    return moment.getFullYear() + "-" + String(moment.getMonth() + 1).padStart(2, "0") + "-" + String(moment.getDate()).padStart(2, "0");
  }

  // Due dates are plain dates: "overdue" before today, "soon" from today to two days ahead
  // (the same windows as the server's filters.py).
  var SOON_DAYS = (data.filter && data.filter.soon_days) || 2;
  K.weekDays = (data.filter && data.filter.week_days) || 7;
  K.dueState = function (due) {
    if (!due) return "";
    var now = new Date();
    if (due < isoDate(now)) return "overdue";
    var soon = new Date(now.getFullYear(), now.getMonth(), now.getDate() + SOON_DAYS);
    return due <= isoDate(soon) ? "soon" : "";
  };
  K.isoDate = isoDate;
  K.isShown = function () { return true; };

  // ── Rendering ────────────────────────────────────────────────────────────
  function badge(text, extra) {
    return el("span", "badge" + (extra ? " " + extra : ""), text);
  }

  function renderCard(ticket) {
    var li = el("li", "kanban-card");
    li.dataset.ticketId = ticket.id;
    if (ticket.color) {
      var bar = el("span", "kanban-card__color");
      bar.style.background = ticket.color;
      li.appendChild(bar);
    }
    K.cardHooks.forEach(function (hook) { hook(li, ticket); });
    if (K.canWrite) {
      var grip = el("button", "kanban-grip", "⠿");
      grip.type = "button";
      grip.dataset.grip = "ticket";
      grip.setAttribute("aria-label", BW.t("kanban.move_ticket", { title: ticket.title }));
      li.appendChild(grip);
    }
    var main = el("div", "kanban-card__main");
    var open = el("button", "kanban-card__open", ticket.title);
    open.type = "button";
    open.dataset.open = "ticket";
    main.appendChild(open);
    var meta = el("div", "kanban-card__meta");
    if (ticket.priority !== "medium") meta.appendChild(badge(BW.t("kanban.priority." + ticket.priority), "kanban-priority--" + ticket.priority));
    if (ticket.due_date) {
      var due = K.dueState(ticket.due_date);
      var dueBadge = badge(BW.t("kanban.due", { date: ticket.due_date }), due ? "kanban-due--" + due : "");
      if (due) dueBadge.title = BW.t("kanban.due_" + due);
      meta.appendChild(dueBadge);
    }
    if (ticket.checklist_total) {
      var progress = badge(BW.t("kanban.checklist_count", { done: ticket.checklist_done, total: ticket.checklist_total }),
        ticket.checklist_done === ticket.checklist_total ? "kanban-checklist--complete" : "");
      progress.title = BW.t("kanban.checklist_title", { done: ticket.checklist_done, total: ticket.checklist_total });
      progress.setAttribute("aria-label", progress.title);
      meta.appendChild(progress);
    }
    ticket.labels.forEach(function (label) { meta.appendChild(badge("+" + label, "badge--primary")); });
    ticket.assignees.forEach(function (person) { meta.appendChild(badge("@" + person.username)); });
    [["attachment_count", "attachments"], ["comment_count", "comments"]].forEach(function (pair) {
      var count = ticket[pair[0]];
      if (!count) return;
      var counter = badge(BW.t("kanban." + pair[1] + "_count", { count: count }));
      counter.title = BW.t("kanban." + pair[1] + "_title", { count: count });
      counter.setAttribute("aria-label", counter.title);
      meta.appendChild(counter);
    });
    if (meta.childNodes.length) main.appendChild(meta);
    li.appendChild(main);
    return li;
  }

  function addTicketForm() {
    var foot = el("div", "kanban-column__foot");
    var form = el("form");
    form.dataset.action = "add-ticket";
    var input = el("input");
    input.type = "text";
    input.maxLength = 300;
    input.required = true;
    input.dataset.action = "new-ticket";
    input.placeholder = BW.t("kanban.add_ticket_placeholder");
    input.setAttribute("aria-label", BW.t("kanban.add_ticket"));
    var submit = el("button", "btn btn--small", BW.t("kanban.add_ticket"));
    submit.type = "submit";
    form.appendChild(input);
    form.appendChild(submit);
    foot.appendChild(form);
    return foot;
  }

  // headOnly: the column's heading row above the swimlanes (the cards live in the lanes' cells).
  function buildColumn(col, headOnly) {
    var section = el("section", "kanban-column" + (headOnly ? " kanban-column--head" : ""));
    section.dataset.columnId = col.id;
    var head = el("div", "kanban-column__head");
    if (K.canWrite) {
      var grip = el("button", "kanban-grip", "⠿");
      grip.type = "button";
      grip.dataset.grip = "column";
      head.appendChild(grip);
    }
    var title = el("h2", "kanban-column__title");
    title.id = "kanban-column-title-" + col.id;
    head.appendChild(title);
    head.appendChild(el("span", "kanban-column__count"));
    if (K.canWrite) {
      // Column tools share one row under the title instead of wrapping one by one.
      var tools = el("div", "kanban-column__tools");
      var limit = el("button", "btn btn--ghost btn--small kanban-column__limit", BW.t("kanban.wip_button"));
      limit.type = "button";
      limit.dataset.action = "wip-limit";
      tools.appendChild(limit);
      var shelve = el("button", "btn btn--ghost btn--small kanban-column__archive", BW.t("kanban.archive_column"));
      shelve.type = "button";
      shelve.dataset.action = "archive-column";
      tools.appendChild(shelve);
      var remove = el("button", "btn btn--ghost btn--icon btn--small kanban-column__delete", "×");
      remove.type = "button";
      remove.dataset.action = "delete-column";
      tools.appendChild(remove);
      head.appendChild(tools);
    }
    section.appendChild(head);
    if (headOnly) return section;
    var list = el("ul", "kanban-column__cards");
    list.dataset.columnId = col.id;
    list.setAttribute("aria-labelledby", title.id);
    section.appendChild(list);
    if (K.canWrite) section.appendChild(addTicketForm());
    return section;
  }

  function fillCards(list, col, lane) {
    list.textContent = "";
    col.tickets.forEach(function (id) {
      var ticket = K.ticket(id);
      if (ticket && inLane(ticket, lane)) list.appendChild(renderCard(ticket));
    });
  }

  function fillColumn(section, col) {
    var title = section.querySelector(".kanban-column__title");
    if (!title.querySelector("input")) {
      title.textContent = "";
      if (K.canWrite) {
        var rename = el("button", "kanban-column__rename", col.title);
        rename.type = "button";
        rename.dataset.action = "rename-column";
        rename.title = BW.t("kanban.rename_column");
        title.appendChild(rename);
      } else {
        title.textContent = col.title;
      }
    }
    var count = section.querySelector(".kanban-column__count");
    var shown = col.tickets.filter(function (id) { var t = K.ticket(id); return t && K.isShown(t); }).length;
    var total = col.tickets.length;
    var text = shown === total ? String(total) : BW.t("kanban.count_filtered", { shown: shown, total: total });
    var over = !!col.wip_limit && total > col.wip_limit;
    if (col.wip_limit) text = BW.t("kanban.wip_count", { count: text, limit: col.wip_limit });
    count.textContent = text;
    count.title = col.wip_limit ? BW.t(over ? "kanban.wip_over" : "kanban.wip_title", { count: total, limit: col.wip_limit })
      : BW.t("kanban.tickets_count", { count: total });
    count.setAttribute("aria-label", count.title);
    section.classList.toggle("is-over-limit", over);
    section.classList.toggle("is-at-limit", !!col.wip_limit && total === col.wip_limit);
    var limitButton = section.querySelector("[data-action='wip-limit']");
    if (limitButton) limitButton.setAttribute("aria-label", BW.t("kanban.wip_set", { title: col.title }));
    var grip = section.querySelector("[data-grip='column']");
    if (grip) grip.setAttribute("aria-label", BW.t("kanban.move_column", { title: col.title }));
    var remove = section.querySelector("[data-action='delete-column']");
    if (remove) remove.setAttribute("aria-label", BW.t("kanban.delete_column", { title: col.title }));
    var archive = section.querySelector("[data-action='archive-column']");
    if (archive) {
      archive.setAttribute("aria-label", BW.t("kanban.archive_column_label", { title: col.title }));
      archive.title = archive.getAttribute("aria-label");
      archive.disabled = !col.tickets.length;
    }
    var list = section.querySelector(":scope > .kanban-column__cards");
    if (list) fillCards(list, col, null);
  }

  function addColumnForm() {
    var box = el("section", "kanban-column kanban-column--add no-print");
    var form = el("form");
    form.dataset.action = "add-column";
    var input = el("input");
    input.type = "text";
    input.maxLength = 100;
    input.required = true;
    input.placeholder = BW.t("kanban.add_column");
    input.setAttribute("aria-label", BW.t("kanban.add_column"));
    var submit = el("button", "btn btn--small", BW.t("kanban.add_column"));
    submit.type = "submit";
    form.appendChild(input);
    form.appendChild(submit);
    box.appendChild(form);
    return box;
  }

  function focusSnapshot() {
    var active = document.activeElement;
    if (!active || !root.contains(active)) return null;
    var card = active.closest("[data-ticket-id]");
    var col = active.closest("[data-column-id]");
    var lane = active.closest("[data-lane]");
    return {
      ticket: card ? card.dataset.ticketId : null,
      column: col ? col.dataset.columnId : null,
      lane: lane ? lane.dataset.lane : null,
      role: active.dataset.grip || active.dataset.open || active.dataset.action || active.dataset.select || "",
      value: active.dataset.action === "new-ticket" ? active.value : null
    };
  }

  function restoreFocus(snap) {
    if (!snap || !snap.role) return;
    var laneScope = snap.lane ? "[data-lane='" + attr(snap.lane) + "'] " : "";
    var scope = null;
    if (snap.ticket) {
      var card = ".kanban-card[data-ticket-id='" + attr(snap.ticket) + "']";
      scope = (laneScope && root.querySelector(laneScope + card)) || root.querySelector(card);
    } else if (snap.column) {
      var place = "[data-column-id='" + attr(snap.column) + "']";
      scope = snap.lane ? root.querySelector(".kanban-cell" + place + "[data-lane='" + attr(snap.lane) + "']") : null;
      scope = scope || root.querySelector(".kanban-column" + place);
    }
    if (!scope) return;
    var role = attr(snap.role);
    var target = scope.querySelector("[data-grip='" + role + "'], [data-open='" + role + "'], [data-action='" + role + "'], [data-select='" + role + "']");
    if (!target) return;
    if (snap.value !== null && target.value === "") target.value = snap.value;
    target.focus();
  }

  // The element that holds the column sections: the board itself, or the heading row above the swimlanes.
  function columnContainer() {
    return root.querySelector(":scope > .kanban-lanes__head") || root;
  }

  function renderColumns(container, headOnly) {
    var sections = {};
    container.querySelectorAll(":scope > .kanban-column[data-column-id]").forEach(function (section) {
      sections[section.dataset.columnId] = section;
    });
    var adder = container.querySelector(":scope > .kanban-column--add");
    K.state.columns.forEach(function (col) {
      var section = sections[col.id] || buildColumn(col, headOnly);
      delete sections[col.id];
      fillColumn(section, col);
      container.insertBefore(section, adder);
    });
    Object.keys(sections).forEach(function (id) { sections[id].remove(); });
    if (K.canWrite && !adder) container.appendChild(addColumnForm());
  }

  // Swimlanes: one row per lane; each row has a cell per column with the lane's tickets.
  function renderLanes() {
    var drafts = {};
    root.querySelectorAll(".kanban-cell input[data-action='new-ticket']").forEach(function (input) {
      if (input.value) drafts[input.closest(".kanban-cell").dataset.lane + "|" + input.closest(".kanban-cell").dataset.columnId] = input.value;
    });
    root.querySelectorAll(":scope > .kanban-lane").forEach(function (lane) { lane.remove(); });
    K.lanes.list().forEach(function (lane, index) {
      var section = el("section", "kanban-lane");
      section.dataset.lane = lane.id;
      var heading = el("h2", "kanban-lane__title");
      heading.id = "kanban-lane-" + index;
      heading.appendChild(el("span", "", lane.label));
      var shown = 0;
      Object.keys(K.state.tickets).forEach(function (id) {
        var ticket = K.state.tickets[id];
        if (K.isShown(ticket) && inLane(ticket, lane.id)) shown += 1;
      });
      var count = el("span", "badge", String(shown));
      count.setAttribute("aria-label", BW.t("kanban.tickets_count", { count: shown }));
      heading.appendChild(count);
      section.setAttribute("aria-labelledby", heading.id);
      section.appendChild(heading);
      var row = el("div", "kanban-lane__row");
      K.state.columns.forEach(function (col) {
        var cell = el("div", "kanban-cell");
        cell.dataset.columnId = col.id;
        cell.dataset.lane = lane.id;
        var list = el("ul", "kanban-column__cards");
        list.dataset.columnId = col.id;
        list.dataset.lane = lane.id;
        list.setAttribute("aria-label", BW.t("kanban.lane_cell", { column: col.title, lane: lane.label }));
        fillCards(list, col, lane.id);
        cell.appendChild(list);
        if (K.canWrite) {
          var foot = addTicketForm();
          var draft = drafts[lane.id + "|" + col.id];
          if (draft) foot.querySelector("input").value = draft;
          cell.appendChild(foot);
        }
        row.appendChild(cell);
      });
      section.appendChild(row);
      root.appendChild(section);
    });
  }

  K.refresh = function () {
    if (dragging) return;
    var snap = focusSnapshot();
    var lanes = !!K.lanes;
    if (lanes !== renderedLanes) {
      root.textContent = "";
      root.classList.toggle("kanban-board--lanes", lanes);
      if (lanes) root.appendChild(el("div", "kanban-lanes__head"));
      renderedLanes = lanes;
    }
    renderColumns(columnContainer(), lanes);
    if (lanes) renderLanes();
    var heading = document.getElementById("kanban-board-title");
    if (heading) heading.textContent = K.state.board.title;
    restoreFocus(snap);
    K.onRefresh.forEach(function (fn) { fn(); });
  };

  K.reload = function () {
    return K.api("/" + K.boardId + "/state").then(function (state) {
      if (!!state.board.archived_at !== !!K.state.board.archived_at) { location.reload(); return; }
      K.state = { board: state.board, columns: state.columns, tickets: state.tickets, archived_count: state.archived_count };
      K.seq = state.seq;
      K.refresh();
    }).catch(K.fail);
  };

  // ── Applying changes (ours and other people's) ──────────────────────────
  function applyColumns(columns) {
    Object.keys(columns || {}).forEach(function (id) {
      var col = column(Number(id));
      if (col) col.tickets = columns[id].slice();
    });
  }

  function orderColumns(order) {
    var byId = {};
    K.state.columns.forEach(function (col) { byId[col.id] = col; });
    K.state.columns = order.filter(function (id) { return byId[id]; }).map(function (id) { return byId[id]; });
  }

  function upsertTicket(ticket, columns) {
    var previous = columnOfTicket(ticket.id);
    K.state.tickets[String(ticket.id)] = ticket;
    if (previous && previous.id !== ticket.column_id && !(columns && columns[previous.id])) {
      previous.tickets = previous.tickets.filter(function (id) { return id !== ticket.id; });
    }
    var target = column(ticket.column_id);
    if (target && target.tickets.indexOf(ticket.id) === -1 && !(columns && columns[target.id])) target.tickets.push(ticket.id);
    applyColumns(columns);
  }

  K.upsertTicket = upsertTicket;

  function removeTickets(ids) {
    ids.forEach(function (id) {
      delete K.state.tickets[String(id)];
      var col = columnOfTicket(id);
      if (col) col.tickets = col.tickets.filter(function (other) { return other !== id; });
    });
  }

  K.removeTickets = removeTickets;

  var handlers = {
    board_updated: function (p) {
      K.state.board.title = p.title;
      K.state.board.description = p.description;
    },
    column_upsert: function (p) {
      var existing = column(p.column.id);
      if (existing) {
        existing.title = p.column.title;
        existing.wip_limit = p.column.wip_limit || null;
        existing.tickets = p.column.tickets;
      } else {
        K.state.columns.push({ id: p.column.id, title: p.column.title, wip_limit: p.column.wip_limit || null, tickets: p.column.tickets || [] });
      }
      orderColumns(p.order);
    },
    column_deleted: function (p) {
      var gone = column(p.column_id);
      if (gone) gone.tickets.forEach(function (id) { delete K.state.tickets[String(id)]; });
      K.state.columns = K.state.columns.filter(function (col) { return col.id !== p.column_id; });
      orderColumns(p.order);
    },
    columns_reordered: function (p) { orderColumns(p.order); },
    ticket_upsert: function (p) {
      upsertTicket(p.ticket, p.columns);
      if (p.restored) K.state.archived_count = Math.max(0, (K.state.archived_count || 0) - 1);
    },
    ticket_deleted: function (p) {
      removeTickets(p.ticket_ids);
      applyColumns(p.columns);
      K.state.archived_count = Math.max(0, (K.state.archived_count || 0) - (p.archived || []).length);
    },
    tickets_archived: function (p) {
      removeTickets(p.ticket_ids);
      applyColumns(p.columns);
      K.state.archived_count = (K.state.archived_count || 0) + p.ticket_ids.length;
    },
    tickets_reordered: function (p) { applyColumns(p.columns); }
  };

  function applyEvents(list) {
    var needsReload = false;
    list.forEach(function (event) {
      var handler = handlers[event.op];
      if (handler) handler(event.payload || {}); else needsReload = true;
    });
    if (needsReload) return K.reload();
    K.refresh();
    document.dispatchEvent(new CustomEvent("kanban:events", { detail: list }));
    return null;
  }

  // ── Live sync ────────────────────────────────────────────────────────────
  var syncDelay = 4000;
  var syncTimer = null;

  function scheduleSync(delay) {
    clearTimeout(syncTimer);
    syncTimer = setTimeout(runSync, delay === undefined ? syncDelay : delay);
  }

  function runSync() {
    if (document.hidden || dragging) { scheduleSync(); return; }
    K.api("/" + K.boardId + "/sync?since=" + K.seq).then(function (result) {
      document.getElementById("kanban-offline").hidden = true;
      syncDelay = 4000;
      if (result.reset) return K.reload();
      K.seq = result.seq;
      if (result.events.length) return applyEvents(result.events);
      return null;
    }).catch(function () {
      document.getElementById("kanban-offline").hidden = false;
      syncDelay = Math.min(syncDelay * 2, 60000);
    }).then(function () { scheduleSync(); });
  }

  document.addEventListener("visibilitychange", function () { if (!document.hidden) scheduleSync(0); });
  window.addEventListener("online", function () { syncDelay = 4000; scheduleSync(0); });
  window.addEventListener("offline", function () { document.getElementById("kanban-offline").hidden = false; });

  // ── Actions ──────────────────────────────────────────────────────────────
  K.moveTicket = function (ticketId, columnId, index) {
    var from = columnOfTicket(ticketId);
    var to = column(columnId);
    if (!from || !to) return Promise.resolve();
    from.tickets = from.tickets.filter(function (id) { return id !== ticketId; });
    to.tickets.splice(Math.max(0, Math.min(index, to.tickets.length)), 0, ticketId);
    K.ticket(ticketId).column_id = columnId;
    K.refresh();
    var message = BW.t("kanban.moved", { title: K.ticket(ticketId).title, column: to.title, position: to.tickets.indexOf(ticketId) + 1 });
    if (from !== to && to.wip_limit && to.tickets.length > to.wip_limit) {
      var warning = BW.t("kanban.wip_exceeded", { title: to.title, count: to.tickets.length, limit: to.wip_limit });
      BW.toast(warning, "warning");
      message += ". " + warning;
    }
    K.announce(message);
    return K.api("/tickets/" + ticketId + "/move", "POST", { column_id: columnId, position: index }).then(function (result) {
      upsertTicket(result.ticket, result.columns);
      K.refresh();
    }).catch(function (error) { K.fail(error); K.reload(); });
  };

  function moveColumn(columnId, index) {
    var ids = K.state.columns.map(function (col) { return col.id; }).filter(function (id) { return id !== columnId; });
    index = Math.max(0, Math.min(index, ids.length));
    ids.splice(index, 0, columnId);
    orderColumns(ids);
    K.refresh();
    K.announce(BW.t("kanban.column_moved", { title: column(columnId).title, position: index + 1 }));
    K.api("/" + K.boardId + "/columns/reorder", "POST", { order: ids }).catch(function (error) { K.fail(error); K.reload(); });
  }

  function addTicket(form) {
    var input = form.querySelector("input");
    var columnId = Number(form.closest("[data-column-id]").dataset.columnId);
    var laneNode = form.closest("[data-lane]");
    var lane = laneNode ? laneNode.dataset.lane : null;
    var title = input.value.trim();
    if (!title) return;
    var body = Object.assign({ title: title }, lane && K.lanes ? K.lanes.create(lane) : {});
    input.disabled = true;
    K.api("/columns/" + columnId + "/tickets", "POST", body).then(function (ticket) {
      upsertTicket(ticket);
      input.value = "";
      K.refresh();
      K.announce(BW.t("kanban.ticket_added", { title: ticket.title }));
    }).catch(K.fail).then(function () {
      // Swimlanes are redrawn on every change: find the (new) input of the same cell.
      var again = lane ? root.querySelector(".kanban-cell[data-lane='" + attr(lane) + "'][data-column-id='" + columnId + "'] input[data-action='new-ticket']") : input;
      if (!again) return;
      again.disabled = false;
      again.value = "";
      again.focus();
    });
  }

  function archiveColumn(section) {
    var col = column(Number(section.dataset.columnId));
    if (!col || !col.tickets.length) return;
    if (!window.confirm(BW.t("kanban.confirm_archive_column", { title: col.title, count: col.tickets.length }))) return;
    K.api("/columns/" + col.id + "/archive", "POST").then(function (result) {
      var ids = col.tickets.slice();
      removeTickets(ids);
      K.state.archived_count = (K.state.archived_count || 0) + result.updated;
      K.refresh();
      K.announce(BW.t("kanban.archived_done", { count: result.updated }));
      document.dispatchEvent(new CustomEvent("kanban:archived"));
    }).catch(K.fail);
  }

  function addColumn(form) {
    var input = form.querySelector("input");
    var title = input.value.trim();
    if (!title) return;
    K.api("/" + K.boardId + "/columns", "POST", { title: title }).then(function (col) {
      handlers.column_upsert({ column: col, order: K.state.columns.map(function (c) { return c.id; }).concat([col.id]) });
      input.value = "";
      K.refresh();
      var added = root.querySelector(".kanban-column--add input");
      if (added) added.focus();
    }).catch(K.fail);
  }

  function startRename(button) {
    var section = button.closest(".kanban-column");
    var col = column(Number(section.dataset.columnId));
    var title = section.querySelector(".kanban-column__title");
    var input = el("input");
    input.type = "text";
    input.maxLength = 100;
    input.value = col.title;
    input.setAttribute("aria-label", BW.t("kanban.rename_column"));
    title.textContent = "";
    title.appendChild(input);
    input.focus();
    input.select();
    var done = false;
    function finish(save) {
      if (done) return;
      done = true;
      var value = input.value.trim();
      title.textContent = "";
      if (!save || !value || value === col.title) { K.refresh(); return; }
      col.title = value;
      K.refresh();
      K.api("/columns/" + col.id, "PUT", { title: value }).catch(function (error) { K.fail(error); K.reload(); });
    }
    input.addEventListener("keydown", function (event) {
      if (event.key === "Enter") { event.preventDefault(); finish(true); }
      if (event.key === "Escape") { event.preventDefault(); finish(false); }
    });
    input.addEventListener("blur", function () { finish(true); });
  }

  function startLimit(button) {
    var section = button.closest(".kanban-column");
    var col = column(Number(section.dataset.columnId));
    var input = el("input", "kanban-column__limit-input");
    input.type = "number";
    input.min = "0";
    input.max = "999";
    input.step = "1";
    input.inputMode = "numeric";
    input.value = col.wip_limit || "";
    input.placeholder = BW.t("kanban.wip_none");
    input.setAttribute("aria-label", BW.t("kanban.wip_prompt", { title: col.title }));
    button.hidden = true;
    button.after(input);
    input.focus();
    input.select();
    var done = false;
    function finish(save) {
      if (done) return;
      done = true;
      var raw = input.value.trim();
      var value = raw === "" ? null : Number(raw);
      input.remove();
      button.hidden = false;
      if (!save || value === (col.wip_limit || null) || (value === 0 && !col.wip_limit)) { button.focus(); return; }
      K.api("/columns/" + col.id, "PUT", { wip_limit: value }).then(function (result) {
        col.wip_limit = result.wip_limit || null;
        K.refresh();
        K.announce(col.wip_limit ? BW.t("kanban.wip_saved", { title: col.title, limit: col.wip_limit }) : BW.t("kanban.wip_removed", { title: col.title }));
      }).catch(K.fail).then(function () {
        var again = root.querySelector(".kanban-column[data-column-id='" + col.id + "'] [data-action='wip-limit']");
        if (again) again.focus();
      });
    }
    input.addEventListener("keydown", function (event) {
      if (event.key === "Enter") { event.preventDefault(); finish(true); }
      if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); finish(false); }
    });
    input.addEventListener("blur", function () { finish(true); });
  }

  function deleteColumn(section) {
    var col = column(Number(section.dataset.columnId));
    if (!col || !window.confirm(BW.t("kanban.confirm_delete_column", { title: col.title, count: col.tickets.length }))) return;
    K.api("/columns/" + col.id, "DELETE").then(function () {
      handlers.column_deleted({ column_id: col.id, order: K.state.columns.map(function (c) { return c.id; }).filter(function (id) { return id !== col.id; }) });
      K.refresh();
    }).catch(K.fail);
  }

  root.addEventListener("submit", function (event) {
    var form = event.target;
    if (form.dataset.action === "add-ticket") { event.preventDefault(); addTicket(form); }
    if (form.dataset.action === "add-column") { event.preventDefault(); addColumn(form); }
  });

  root.addEventListener("click", function (event) {
    var target = event.target.closest("button");
    if (!target || !root.contains(target)) return;
    if (target.dataset.open === "ticket" && K.openTicket) {
      K.openTicket(Number(target.closest("[data-ticket-id]").dataset.ticketId));
    } else if (target.dataset.action === "rename-column") {
      startRename(target);
    } else if (target.dataset.action === "wip-limit") {
      startLimit(target);
    } else if (target.dataset.action === "delete-column") {
      deleteColumn(target.closest(".kanban-column"));
    } else if (target.dataset.action === "archive-column") {
      archiveColumn(target.closest(".kanban-column"));
    }
  });

  // ── Keyboard moves ───────────────────────────────────────────────────────
  // On a ticket's handle: arrows move it (up/down within the column, left/right
  // to the neighbouring column). Alt+arrows do the same from the ticket title.
  root.addEventListener("keydown", function (event) {
    if (!K.canWrite) return;
    var keys = { ArrowUp: [0, -1], ArrowDown: [0, 1], ArrowLeft: [-1, 0], ArrowRight: [1, 0] };
    var move = keys[event.key];
    if (!move) return;
    var target = event.target;
    var onGrip = target.dataset && target.dataset.grip;
    if (!onGrip && !(event.altKey && target.dataset && target.dataset.open)) return;
    event.preventDefault();
    if (onGrip === "column") {
      if (!move[0]) return;
      var colId = Number(target.closest(".kanban-column").dataset.columnId);
      var position = K.state.columns.indexOf(column(colId)) + move[0];
      if (position >= 0 && position < K.state.columns.length) moveColumn(colId, position);
      return;
    }
    var ticketId = Number(target.closest("[data-ticket-id]").dataset.ticketId);
    var laneNode = target.closest("[data-lane]");
    var lane = laneNode ? laneNode.dataset.lane : null;
    var from = columnOfTicket(ticketId);
    var index = from.tickets.indexOf(ticketId);
    if (move[0]) {
      var next = K.state.columns[K.state.columns.indexOf(from) + move[0]];
      if (next) K.moveTicket(ticketId, next.id, Math.min(index, next.tickets.length));
      return;
    }
    // Skip tickets hidden by the filter (or in other rows) so every key press visibly moves the card.
    var position = index + move[1];
    while (position >= 0 && position < from.tickets.length) {
      var other = K.ticket(from.tickets[position]);
      if (K.isShown(other) && inLane(other, lane)) break;
      position += move[1];
    }
    if (position >= 0 && position < from.tickets.length) {
      K.moveTicket(ticketId, from.id, position);
    } else if (K.lanes && lane && K.lanes.draggable) {
      // At the edge of a row: move the ticket into the neighbouring row.
      var ids = K.lanes.list().map(function (item) { return item.id; });
      var neighbour = ids[ids.indexOf(lane) + move[1]];
      if (neighbour) K.lanes.change(ticketId, lane, neighbour);
    }
  });

  // ── Pointer drag and drop (mouse, touch and pen) ─────────────────────────
  // Tickets drag from their handle (or anywhere on the card with a mouse);
  // columns drag from their handle. The dragged element itself moves through
  // the DOM while a floating copy follows the pointer.
  var THRESHOLD = 5;

  function dropTargetAt(x, y, kind) {
    var under = document.elementFromPoint(x, y);
    if (!under || !root.contains(under)) return null;
    return kind === "ticket" ? under.closest(".kanban-cell, .kanban-column") : under.closest(".kanban-column[data-column-id]");
  }

  function autoScroll(x, y) {
    var box = root.getBoundingClientRect();
    if (x < box.left + 40) root.scrollLeft -= 14;
    else if (x > box.right - 40) root.scrollLeft += 14;
    var list = dragging.list;
    if (list) {
      var lb = list.getBoundingClientRect();
      if (y < lb.top + 30) list.scrollTop -= 10;
      else if (y > lb.bottom - 30) list.scrollTop += 10;
    }
  }

  function placeTicket(x, y) {
    var section = dropTargetAt(x, y, "ticket");
    if (!section || !section.dataset.columnId) return;
    var list = section.querySelector(":scope > .kanban-column__cards");
    if (!list) return;
    dragging.list = list;
    var before = null;
    var cards = list.querySelectorAll(".kanban-card");
    for (var i = 0; i < cards.length; i++) {
      if (cards[i] === dragging.node) continue;
      var box = cards[i].getBoundingClientRect();
      if (y < box.top + box.height / 2) { before = cards[i]; break; }
    }
    if (dragging.node.parentNode !== list || dragging.node.nextSibling !== before) list.insertBefore(dragging.node, before);
  }

  function placeColumn(x, y) {
    var container = columnContainer();
    var section = dropTargetAt(x, y, "column");
    if (!section || section === dragging.node || section.parentNode !== container) return;
    var box = section.getBoundingClientRect();
    container.insertBefore(dragging.node, x < box.left + box.width / 2 ? section : section.nextSibling);
    var adder = container.querySelector(":scope > .kanban-column--add");
    if (adder) container.appendChild(adder);
  }

  // Where a dropped card lands in its column's full order (other rows and hidden cards included).
  function dropIndex(node, col, ticketId) {
    var others = col.tickets.filter(function (id) { return id !== ticketId; });
    var next = node.nextElementSibling;
    if (next && next.dataset.ticketId) {
      var at = others.indexOf(Number(next.dataset.ticketId));
      if (at !== -1) return at;
    }
    var previous = node.previousElementSibling;
    if (previous && previous.dataset.ticketId) {
      var after = others.indexOf(Number(previous.dataset.ticketId));
      if (after !== -1) return after + 1;
    }
    return others.length;
  }

  function beginDrag(state, event) {
    dragging = state;
    var rect = state.node.getBoundingClientRect();
    state.ghost = state.node.cloneNode(true);
    state.ghost.classList.add("kanban-ghost");
    state.ghost.style.width = rect.width + "px";
    state.offsetX = event.clientX - rect.left;
    state.offsetY = event.clientY - rect.top;
    document.body.appendChild(state.ghost);
    state.node.classList.add("is-dragging");
    document.body.classList.add("kanban-dragging");
  }

  function endDrag(commit) {
    var state = dragging;
    dragging = null;
    document.body.classList.remove("kanban-dragging");
    if (!state || !state.ghost) return;
    state.ghost.remove();
    state.node.classList.remove("is-dragging");
    if (!commit) { K.refresh(); return; }
    if (state.kind === "ticket") {
      var list = state.node.parentNode;
      var from = columnOfTicket(state.id);
      var columnId = Number(list.dataset.columnId);
      var to = column(columnId);
      if (!from || !to) { K.refresh(); return; }
      var index = dropIndex(state.node, to, state.id);
      var moved = from.id !== columnId || from.tickets.indexOf(state.id) !== index;
      var lane = list.dataset.lane || null;
      var laneChange = !!(K.lanes && lane && state.lane && lane !== state.lane);
      if (laneChange && !K.lanes.draggable) {
        BW.toast(BW.t("kanban.lane_fixed"), "info");
        laneChange = false;
      }
      var done = moved ? K.moveTicket(state.id, columnId, index) : Promise.resolve();
      if (laneChange) done.then(function () { return K.lanes.change(state.id, state.lane, lane); });
      if (!moved && !laneChange) K.refresh();
    } else {
      var order = Array.prototype.map.call(columnContainer().querySelectorAll(":scope > .kanban-column[data-column-id]"), function (s) { return Number(s.dataset.columnId); });
      var position = order.indexOf(state.id);
      if (K.state.columns.indexOf(column(state.id)) !== position) moveColumn(state.id, position);
      else K.refresh();
    }
  }

  root.addEventListener("pointerdown", function (event) {
    if (!K.canWrite || event.button !== 0 || dragging) return;
    var grip = event.target.closest("[data-grip]");
    var card = event.target.closest(".kanban-card");
    var fromCardBody = !grip && card && event.pointerType === "mouse" && !event.target.closest("input, select, textarea");
    if (!grip && !fromCardBody) return;
    var kind = grip ? grip.dataset.grip : "ticket";
    var node = kind === "ticket" ? event.target.closest(".kanban-card") : event.target.closest(".kanban-column");
    var id = Number(kind === "ticket" ? node.dataset.ticketId : node.dataset.columnId);
    var laneNode = node.closest("[data-lane]");
    var pending = { kind: kind, node: node, id: id, lane: laneNode ? laneNode.dataset.lane : null,
      startX: event.clientX, startY: event.clientY, pointer: event.pointerId };
    if (grip) event.preventDefault();

    function onMove(moveEvent) {
      if (moveEvent.pointerId !== pending.pointer) return;
      if (!dragging) {
        if (Math.abs(moveEvent.clientX - pending.startX) + Math.abs(moveEvent.clientY - pending.startY) < THRESHOLD) return;
        beginDrag(pending, moveEvent);
      }
      moveEvent.preventDefault();
      pending.ghost.style.left = (moveEvent.clientX - pending.offsetX) + "px";
      pending.ghost.style.top = (moveEvent.clientY - pending.offsetY) + "px";
      if (pending.kind === "ticket") placeTicket(moveEvent.clientX, moveEvent.clientY);
      else placeColumn(moveEvent.clientX, moveEvent.clientY);
      autoScroll(moveEvent.clientX, moveEvent.clientY);
    }

    function stop(stopEvent) {
      if (stopEvent.pointerId !== pending.pointer) return;
      document.removeEventListener("pointermove", onMove);
      document.removeEventListener("pointerup", stop);
      document.removeEventListener("pointercancel", stop);
      document.removeEventListener("keydown", onEscape);
      if (dragging === pending) {
        endDrag(stopEvent.type === "pointerup");
        suppressClick();
      }
    }

    function onEscape(keyEvent) {
      if (keyEvent.key === "Escape" && dragging === pending) {
        document.removeEventListener("pointermove", onMove);
        document.removeEventListener("pointerup", stop);
        document.removeEventListener("pointercancel", stop);
        document.removeEventListener("keydown", onEscape);
        endDrag(false);
      }
    }

    document.addEventListener("pointermove", onMove, { passive: false });
    document.addEventListener("pointerup", stop);
    document.addEventListener("pointercancel", stop);
    document.addEventListener("keydown", onEscape);
  });

  function suppressClick() {
    function swallow(event) { event.stopPropagation(); event.preventDefault(); }
    document.addEventListener("click", swallow, true);
    setTimeout(function () { document.removeEventListener("click", swallow, true); }, 0);
  }

  // ── Activity panel ───────────────────────────────────────────────────────
  var activityToggle = document.getElementById("kanban-activity-toggle");
  var activityPanel = document.getElementById("kanban-activity");

  function loadActivity() {
    var list = document.getElementById("kanban-activity-list");
    K.api("/" + K.boardId + "/activity").then(function (result) {
      list.textContent = "";
      if (!result.entries.length) list.appendChild(el("li", "muted", BW.t("kanban.no_activity")));
      result.entries.forEach(function (entry) {
        var li = el("li");
        li.appendChild(el("strong", "", entry.username || BW.t("kanban.someone")));
        li.appendChild(document.createTextNode(" " + entry.details));
        var time = el("time", "", (entry.created_at || "").slice(0, 16));
        time.dateTime = entry.created_at || "";
        li.appendChild(time);
        list.appendChild(li);
      });
    }).catch(K.fail);
  }

  if (activityToggle && activityPanel) {
    activityToggle.addEventListener("click", function () {
      activityPanel.hidden = !activityPanel.hidden;
      activityToggle.setAttribute("aria-expanded", activityPanel.hidden ? "false" : "true");
      if (!activityPanel.hidden) loadActivity();
    });
    document.addEventListener("kanban:events", function () { if (!activityPanel.hidden) loadActivity(); });
  }

  K.priorities = PRIORITIES;
  // Draw once every deferred kanban script (filter, lanes, ticket, …) has set its hooks from the address:
  // DOMContentLoaded fires after deferred scripts ran (BW.onReady would run now, before them).
  var started = false;
  function start() {
    if (started) return;
    started = true;
    K.refresh();
    scheduleSync();
  }
  if (document.readyState === "complete") start();
  else {
    document.addEventListener("DOMContentLoaded", start);
    window.addEventListener("load", start);
  }
})();
