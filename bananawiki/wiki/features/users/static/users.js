/* Contribution calendar on profile pages: one cell per day of the chosen year,
 * coloured by the number of edits; choosing a day lists what was edited. */
(function () {
  "use strict";
  var dataNode = document.getElementById("users-calendar-data");
  var host = document.getElementById("users-calendar");
  var details = document.getElementById("users-calendar-details");
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
    head.textContent = BW.t("users.calendar.day", { day: day, count: items.length });
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
      cell.title = BW.t("users.calendar.cell", { day: day, count: count });
      cell.setAttribute("aria-label", cell.title);
      cells.push(cell);
    }
    grid.appendChild(cell);
  }
  grid.addEventListener("click", function (event) {
    var target = event.target.closest(".users-calendar__cell");
    if (!target || target.disabled) return;
    cells.forEach(function (c) { c.setAttribute("aria-pressed", "false"); });
    target.setAttribute("aria-pressed", "true");
    showDay(target.dataset.day);
  });
  host.appendChild(grid);
})();
