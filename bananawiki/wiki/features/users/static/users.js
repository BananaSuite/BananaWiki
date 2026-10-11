/* Contribution calendar on profile pages: one cell per day of the chosen year,
 * coloured by the number of edits; choosing a day lists what was edited. */
(function () {
  "use strict";
  var dataNode = document.querySelector("script#users-calendar-data");
  var host = document.querySelector("div#users-calendar");
  var details = document.querySelector("div#users-calendar-details");
  if (!dataNode || !host) return;
  var data;
  try { data = JSON.parse(dataNode.textContent || "{}"); } catch (e) { return; }
  var counts = data.counts || {};
  var max = 0;
  Object.keys(counts).forEach(function (day) { if (counts[day] > max) max = counts[day]; });

  function pad(n) { return (n < 10 ? "0" : "") + n; }
  function key(date) { return date.getUTCFullYear() + "-" + pad(date.getUTCMonth() + 1) + "-" + pad(date.getUTCDate()); }
  function level(count) {
    if (!count) return 0;
    var ratio = count / max;
    return ratio < 0.25 ? 1 : ratio < 0.5 ? 2 : ratio < 0.75 ? 3 : 4;
  }

  function showDay(day) {
    if (!details) return;
    details.textContent = "";
    var items = (data.details || {})[day] || [];
    var head = document.createElement("p");
    head.textContent = BW.tn("users.calendar.day", items.length, { day: day, count: items.length });
    details.appendChild(head);
    if (!items.length) return;
    var list = document.createElement("ul");
    items.forEach(function (item) {
      var li = document.createElement("li");
      var time = document.createElement("span");
      time.className = "small muted";
      time.textContent = item.time + " ";
      var link = document.createElement("a");
      link.href = item.url;
      link.textContent = item.title;
      li.appendChild(time);
      li.appendChild(link);
      if (item.message) {
        var message = document.createElement("span");
        message.className = "muted";
        message.textContent = " — " + item.message;
        li.appendChild(message);
      }
      list.appendChild(li);
    });
    details.appendChild(list);
  }

  var grid = document.createElement("div");
  grid.className = "users-calendar__grid";
  grid.setAttribute("role", "group");
  var start = new Date(Date.UTC(data.year, 0, 1));
  start.setUTCDate(start.getUTCDate() - start.getUTCDay());
  var end = new Date(Date.UTC(data.year, 11, 31));
  var today = key(new Date());
  // The current year stops at this week: no empty columns of future days after it.
  if (String(data.year) === today.slice(0, 4)) end = new Date(today + "T00:00:00Z");
  var cells = [];
  for (var d = new Date(start); d <= end || d.getUTCDay() !== 0; d.setUTCDate(d.getUTCDate() + 1)) {
    var day = key(d);
    var cell = document.createElement("button");
    cell.type = "button";
    cell.className = "users-calendar__cell";
    if (d.getUTCFullYear() !== data.year || day > today) {
      cell.disabled = true;
      cell.setAttribute("aria-hidden", "true");
    } else {
      var count = counts[day] || 0;
      cell.dataset.day = day;
      cell.dataset.level = String(level(count));
      cell.setAttribute("aria-pressed", "false");
      cell.title = BW.tn("users.calendar.cell", count, { day: day, count: count });
      cell.setAttribute("aria-label", cell.title);
      cells.push(cell);
    }
    grid.appendChild(cell);
  }
  grid.setAttribute("aria-label", BW.t("users.calendar.label", { year: data.year }));
  // One Tab stop for the whole calendar (the latest day); the arrow keys move between days:
  // left/right by a week (a column), up/down by a day, Home/End to the first/last day.
  var current = cells.length - 1;
  cells.forEach(function (c, index) { c.tabIndex = index === current ? 0 : -1; });
  function rove(index, focus) {
    if (index < 0 || index >= cells.length) return;
    cells[current].tabIndex = -1;
    current = index;
    cells[current].tabIndex = 0;
    if (focus) cells[current].focus();
  }
  grid.addEventListener("keydown", function (event) {
    var index = cells.indexOf(event.target);
    if (index === -1) return;
    var step = { ArrowLeft: -7, ArrowRight: 7, ArrowUp: -1, ArrowDown: 1 }[event.key];
    if (step) rove(Math.max(0, Math.min(cells.length - 1, index + step)), true);
    else if (event.key === "Home") rove(0, true);
    else if (event.key === "End") rove(cells.length - 1, true);
    else return;
    event.preventDefault();
  });
  grid.addEventListener("click", function (event) {
    var target = event.target.closest(".users-calendar__cell");
    if (!target || target.disabled) return;
    cells.forEach(function (c) { c.setAttribute("aria-pressed", "false"); });
    target.setAttribute("aria-pressed", "true");
    rove(cells.indexOf(target), false);
    showDay(target.dataset.day);
  });
  host.appendChild(grid);
  // Narrow screens show the recent weeks first, not January.
  if (String(data.year) === today.slice(0, 4)) host.scrollLeft = host.scrollWidth;
})();
