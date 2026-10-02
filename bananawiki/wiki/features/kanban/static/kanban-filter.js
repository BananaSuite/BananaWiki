/* Kanban filter bar: narrow the board to the tickets that match a search text,
 * an assignee, a label, a priority and a due date range.
 *
 * Filtering happens in the browser on the board state; the choice is kept in
 * the address (?q=…&who=…&label=…&priority=…&due=…) so a filtered board can be
 * bookmarked or shared. The server applies the same rules to the same
 * parameters (filters.py: board state endpoint and "My tickets"). "/" focuses the search field; Escape in it clears it.
 * Depends on kanban-board.js (window.BWKanban). */
(function () {
  "use strict";

  var BW = window.BW;
  var K = window.BWKanban;
  var form = document.getElementById("kanban-filter");
  var tools = document.getElementById("kanban-tools");
  function openTools() { if (tools) tools.open = true; }
  if (!K || !form) return;

  var FIELDS = ["q", "who", "label", "priority", "due"];
  var WEEK_DAYS = K.weekDays || 7;
  var controls = {};
  FIELDS.forEach(function (name) { controls[name] = form.elements[name]; });
  var status = document.getElementById("kanban-filter-status");
  var clear = document.getElementById("kanban-filter-clear");
  var filter = {};

  function read() {
    FIELDS.forEach(function (name) { filter[name] = (controls[name].value || "").trim(); });
  }

  function active() {
    return FIELDS.some(function (name) { return !!filter[name]; });
  }

  function weekEnd() {
    var now = new Date();
    return K.isoDate(new Date(now.getFullYear(), now.getMonth(), now.getDate() + WEEK_DAYS));
  }

  function haystack(ticket) {
    return [ticket.title, "#" + ticket.id]
      .concat(ticket.labels.map(function (label) { return "+" + label; }))
      .concat(ticket.assignees.map(function (person) { return "@" + person.username; }))
      .join(" ").toLowerCase();
  }

  function matchesDue(ticket) {
    var due = ticket.due_date;
    switch (filter.due) {
      case "overdue": return K.dueState(due) === "overdue";
      case "soon": return !!due && K.dueState(due) !== "";
      case "week": return !!due && due >= K.isoDate(new Date()) && due <= weekEnd();
      case "none": return !due;
      default: return true;
    }
  }

  function matchesWho(ticket) {
    if (!filter.who) return true;
    if (filter.who === "none") return !ticket.assignees.length;
    var wanted = filter.who === "me" ? K.userId : filter.who;
    return ticket.assignees.some(function (person) { return person.id === wanted; });
  }

  K.isShown = function (ticket) {
    if (!ticket) return false;
    if (filter.priority && ticket.priority !== filter.priority) return false;
    if (filter.label) {
      var wanted = filter.label.toLowerCase();
      if (!ticket.labels.some(function (label) { return label.toLowerCase() === wanted; })) return false;
    }
    if (!matchesWho(ticket) || !matchesDue(ticket)) return false;
    if (filter.q) {
      var text = haystack(ticket);
      return filter.q.toLowerCase().split(/\s+/).every(function (word) { return text.indexOf(word) !== -1; });
    }
    return true;
  };

  K.cardHooks.push(function (li, ticket) {
    li.hidden = !K.isShown(ticket);
  });

  // ── Choices that come from the board (people and labels) ─────────────────
  function option(value, text) {
    var node = K.el("option", "", text);
    node.value = value;
    return node;
  }

  function refill(select, fixed, entries) {
    // Rebuild only when the choices changed, so a live update does not close an open list.
    var signature = JSON.stringify(entries);
    if (select.dataset.signature === signature) return;
    select.dataset.signature = signature;
    var chosen = select.value;
    select.textContent = "";
    fixed.concat(entries).forEach(function (pair) { select.appendChild(option(pair[0], pair[1])); });
    // Keep a choice from the address even when nothing on the board matches it (yet).
    if (chosen && !Array.prototype.some.call(select.options, function (o) { return o.value === chosen; })) {
      select.appendChild(option(chosen, chosen));
    }
    select.value = chosen;
  }

  function fillChoices() {
    var people = {};
    var labels = {};
    K.assignable.forEach(function (person) { people[person.id] = person.username; });
    Object.keys(K.state.tickets).forEach(function (id) {
      var ticket = K.state.tickets[id];
      ticket.assignees.forEach(function (person) { people[person.id] = person.username; });
      ticket.labels.forEach(function (label) { labels[label.toLowerCase()] = labels[label.toLowerCase()] || label; });
    });
    var byName = function (a, b) { return a[1].localeCompare(b[1], undefined, { sensitivity: "base" }); };
    var fixedPeople = [["", BW.t("kanban.filter_anyone")]];
    if (K.userId) fixedPeople.push(["me", BW.t("kanban.filter_me")]);
    fixedPeople.push(["none", BW.t("kanban.filter_unassigned")]);
    refill(controls.who, fixedPeople, Object.keys(people).filter(function (id) { return id !== K.userId; })
      .map(function (id) { return [id, "@" + people[id]]; }).sort(byName));
    refill(controls.label, [["", BW.t("kanban.filter_any_label")]], Object.keys(labels)
      .map(function (key) { return [labels[key], "+" + labels[key]]; }).sort(byName));
  }

  // ── Summary, address and applying ────────────────────────────────────────
  function counts() {
    var total = 0;
    var shown = 0;
    K.state.columns.forEach(function (col) {
      col.tickets.forEach(function (id) {
        var ticket = K.ticket(id);
        if (!ticket) return;
        total += 1;
        if (K.isShown(ticket)) shown += 1;
      });
    });
    return { shown: shown, total: total };
  }

  var lastSummary = "";
  function summarise(announce) {
    var on = active();
    clear.hidden = !on;
    form.classList.toggle("is-active", on);
    if (on) openTools();
    var numbers = counts();
    var text = on ? BW.t("kanban.filter_result", numbers) : "";
    status.textContent = text;
    if (announce && text !== lastSummary) K.announce(text || BW.t("kanban.filter_cleared"));
    lastSummary = text;
  }

  function writeAddress() {
    var params = new URLSearchParams(location.search);
    FIELDS.forEach(function (name) { if (filter[name]) params.set(name, filter[name]); else params.delete(name); });
    var query = params.toString();
    history.replaceState(history.state, "", location.pathname + (query ? "?" + query : "") + location.hash);
  }

  function apply() {
    read();
    writeAddress();
    K.refresh();
    summarise(true);
  }

  var typing = null;
  form.addEventListener("input", function (event) {
    if (event.target !== controls.q) return;
    clearTimeout(typing);
    typing = setTimeout(apply, 200);
  });
  form.addEventListener("change", function (event) {
    if (event.target !== controls.q) apply();
  });
  form.addEventListener("submit", function (event) {
    event.preventDefault();
    clearTimeout(typing);
    apply();
  });
  clear.addEventListener("click", function () {
    FIELDS.forEach(function (name) { controls[name].value = ""; });
    apply();
    controls.q.focus();
  });

  controls.q.addEventListener("keydown", function (event) {
    if (event.key === "Escape" && controls.q.value) {
      event.preventDefault();
      controls.q.value = "";
      apply();
    }
  });

  // "/" jumps to the search field from anywhere on the board page.
  document.addEventListener("keydown", function (event) {
    if (event.key !== "/" || event.ctrlKey || event.metaKey || event.altKey) return;
    var target = event.target;
    if (target.closest && target.closest("input, textarea, select, [contenteditable], dialog[open]")) return;
    if (document.querySelector("dialog[open]")) return;
    event.preventDefault();
    openTools();
    controls.q.focus();
    controls.q.select();
  });

  K.onRefresh.push(function () {
    fillChoices();
    summarise(false);
  });

  // Start from the address.
  var params = new URLSearchParams(location.search);
  if (params.get("lanes")) openTools();
  FIELDS.forEach(function (name) {
    var value = params.get(name) || "";
    if (value && controls[name].tagName === "SELECT" && !Array.prototype.some.call(controls[name].options, function (o) { return o.value === value; })) {
      controls[name].appendChild(option(value, value));
    }
    controls[name].value = value;
  });
  read();
})();
