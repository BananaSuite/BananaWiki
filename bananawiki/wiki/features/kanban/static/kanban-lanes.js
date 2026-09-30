/* Kanban swimlanes: group the board into rows by assignee, priority or label.
 *
 * Grouping happens in the browser on the board state (kanban-board.js draws
 * the rows from K.lanes). The choice is kept in the address (?lanes=…) like
 * the filter. Dragging a card to another row, or moving it past the first or
 * last card of its row with the keyboard, changes the ticket's assignee or
 * priority; label rows only show the grouping (a ticket with several labels
 * appears in each of their rows). Depends on kanban-board.js (window.BWKanban). */
(function () {
  "use strict";

  var BW = window.BW;
  var K = window.BWKanban;
  var select = document.getElementById("kanban-lanes");
  if (!K || !select) return;

  var PARAM = "lanes";
  var PRIORITY_ORDER = ["critical", "high", "medium", "low"];
  var byName = function (a, b) { return a.label.localeCompare(b.label, undefined, { sensitivity: "base" }); };

  function tickets() {
    return Object.keys(K.state.tickets).map(function (id) { return K.state.tickets[id]; });
  }

  // Lane ids carry a prefix so a label or user id can never be mistaken for "nobody"/"no label".
  var MODES = {
    assignee: {
      draggable: true,
      list: function () {
        var people = {};
        tickets().forEach(function (ticket) {
          ticket.assignees.forEach(function (person) { people[person.id] = person.username; });
        });
        var lanes = Object.keys(people).map(function (id) { return { id: "u:" + id, label: "@" + people[id] }; }).sort(byName);
        return lanes.concat([{ id: "u:", label: BW.t("kanban.lane_unassigned") }]);
      },
      of: function (ticket) {
        return ticket.assignees.length ? ticket.assignees.map(function (person) { return "u:" + person.id; }) : ["u:"];
      },
      body: function (ticket, from, to) {
        var ids = ticket.assignees.map(function (person) { return person.id; })
          .filter(function (id) { return "u:" + id !== from; });
        var added = to.slice(2);
        if (added && ids.indexOf(added) === -1) ids.push(added);
        return { assignees: ids };
      },
      create: function (lane) { return lane.slice(2) ? { assignees: [lane.slice(2)] } : {}; }
    },
    priority: {
      draggable: true,
      list: function () {
        return PRIORITY_ORDER.map(function (name) { return { id: "p:" + name, label: BW.t("kanban.priority." + name) }; });
      },
      of: function (ticket) { return ["p:" + ticket.priority]; },
      body: function (ticket, from, to) { return { priority: to.slice(2) }; },
      create: function (lane) { return { priority: lane.slice(2) }; }
    },
    label: {
      draggable: false,
      list: function () {
        var labels = {};
        tickets().forEach(function (ticket) {
          ticket.labels.forEach(function (label) { labels[label.toLowerCase()] = labels[label.toLowerCase()] || label; });
        });
        var lanes = Object.keys(labels).map(function (key) { return { id: "l:" + key, label: "+" + labels[key] }; }).sort(byName);
        return lanes.concat([{ id: "l:", label: BW.t("kanban.lane_no_label") }]);
      },
      of: function (ticket) {
        return ticket.labels.length ? ticket.labels.map(function (label) { return "l:" + label.toLowerCase(); }) : ["l:"];
      },
      create: function (lane) { return lane.slice(2) ? { labels: [laneLabel(MODES.label, lane).slice(1)] } : {}; }
    }
  };

  function laneLabel(mode, id) {
    var found = mode.list().filter(function (lane) { return lane.id === id; })[0];
    return found ? found.label : "";
  }

  function lanesFor(name) {
    var mode = MODES[name];
    if (!mode) return null;
    return {
      name: name,
      draggable: mode.draggable && K.canWrite,
      list: mode.list,
      of: mode.of,
      create: mode.create,
      change: function (ticketId, from, to) {
        var ticket = K.ticket(ticketId);
        if (!ticket || !mode.body || from === to) return Promise.resolve();
        return K.api("/tickets/" + ticketId, "PUT", mode.body(ticket, from, to)).then(function (detail) {
          K.upsertTicket(K.cardFrom(detail));
          K.refresh();
          K.announce(BW.t("kanban.lane_moved", { title: detail.title, lane: laneLabel(mode, to) }));
        }).catch(function (error) { K.fail(error); K.reload(); });
      }
    };
  }

  function writeAddress(name) {
    var params = new URLSearchParams(location.search);
    if (name) params.set(PARAM, name); else params.delete(PARAM);
    var query = params.toString();
    history.replaceState(history.state, "", location.pathname + (query ? "?" + query : "") + location.hash);
  }

  function use(name, announce) {
    K.lanes = lanesFor(name);
    select.value = K.lanes ? name : "";
    if (announce) K.announce(K.lanes ? BW.t("kanban.lanes_on", { mode: select.options[select.selectedIndex].text }) : BW.t("kanban.lanes_off"));
  }

  select.addEventListener("change", function () {
    use(select.value, true);
    writeAddress(K.lanes ? K.lanes.name : "");
    K.refresh();
  });

  // Start from the address (kanban-board.js draws the board once every script has loaded).
  use(new URLSearchParams(location.search).get(PARAM) || "", false);
})();
