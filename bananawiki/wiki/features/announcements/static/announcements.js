/* Announcement bar: remembers dismissed banners per revision (in this
 * browser), shows several announcements as a carousel, and counts down to
 * expiry. Loaded right after the bar, before the core script. */
(function () {
  "use strict";

  var bar = document.getElementById("announcements-bar");
  if (!bar) return;
  var storageKey = "dismissed_announcements:" + (bar.getAttribute("data-viewer-id") || "guest");
  var all = Array.prototype.slice.call(bar.querySelectorAll("[data-announcement-id]"));
  var dismissed = loadDismissed();
  var current = 0;

  function loadDismissed() {
    try {
      var stored = JSON.parse(window.localStorage.getItem(storageKey) || "[]");
      return Array.isArray(stored) ? stored : [];
    } catch (e) {
      return [];
    }
  }

  function saveDismissed() {
    try { window.localStorage.setItem(storageKey, JSON.stringify(dismissed.slice(-200))); } catch (e) { /* storage off */ }
  }

  function idOf(item) { return parseInt(item.getAttribute("data-announcement-id"), 10); }
  function revisionOf(item) { return parseInt(item.getAttribute("data-revision") || "1", 10); }
  function expiryOf(item) {
    var value = item.getAttribute("data-expires-at");
    var time = value ? Date.parse(value) : NaN;
    return isNaN(time) ? null : time;
  }

  function isDismissed(item) {
    if (item.getAttribute("data-removable") !== "1") return false;
    var id = idOf(item), revision = revisionOf(item);
    // 1.4 stored plain ids for first revisions.
    return dismissed.indexOf(id + ":" + revision) !== -1 || (revision === 1 && dismissed.indexOf(id) !== -1);
  }

  function isExpired(item) {
    var expiry = expiryOf(item);
    return expiry !== null && expiry <= Date.now();
  }

  function live() {
    return all.filter(function (item) { return !isDismissed(item) && !isExpired(item); });
  }

  function render() {
    var items = live();
    all.forEach(function (item) { item.hidden = true; });
    if (!items.length) { bar.hidden = true; return; }
    bar.hidden = false;
    if (current >= items.length) current = items.length - 1;
    var many = items.length > 1;
    if (many) bar.setAttribute("data-carousel", ""); else bar.removeAttribute("data-carousel");
    var shown = items[current];
    shown.hidden = false;
    shown.querySelectorAll("[data-announcement-prev], [data-announcement-next]").forEach(function (button) {
      button.hidden = !many;
    });
    var counter = shown.querySelector(".announcement__counter");
    if (counter) {
      counter.hidden = !many;
      counter.textContent = (bar.getAttribute("data-counter") || "{n} / {total}")
        .replace("{n}", String(current + 1)).replace("{total}", String(items.length));
    }
    updateCountdown(shown);
  }

  function updateCountdown(item) {
    var label = item.querySelector(".announcement__countdown");
    var expiry = expiryOf(item);
    if (!label || expiry === null || item.getAttribute("data-countdown") !== "1") return;
    var total = Math.max(0, Math.floor((expiry - Date.now()) / 1000));
    var days = Math.floor(total / 86400), hours = Math.floor((total % 86400) / 3600);
    var minutes = Math.floor((total % 3600) / 60), seconds = total % 60;
    var parts = [];
    if (days) parts.push(days + bar.getAttribute("data-unit-days"));
    if (days || hours) parts.push(hours + bar.getAttribute("data-unit-hours"));
    parts.push(minutes + bar.getAttribute("data-unit-minutes"));
    parts.push(seconds + bar.getAttribute("data-unit-seconds"));
    label.textContent = parts.join(" ");
    label.hidden = false;
  }

  bar.addEventListener("click", function (event) {
    var button = event.target.closest("button");
    if (!button || !bar.contains(button)) return;
    var count = live().length;
    if (button.hasAttribute("data-announcement-prev")) {
      current = (current - 1 + count) % count;
    } else if (button.hasAttribute("data-announcement-next")) {
      current = (current + 1) % count;
    } else if (button.hasAttribute("data-announcement-dismiss")) {
      var item = button.closest("[data-announcement-id]");
      dismissed.push(idOf(item) + ":" + revisionOf(item));
      saveDismissed();
    } else {
      return;
    }
    render();
    var visible = bar.querySelector("[data-announcement-id]:not([hidden]) button:not([hidden])");
    if (visible && button.hasAttribute("data-announcement-dismiss")) visible.focus();
  });

  render();
  if (all.some(function (item) { return expiryOf(item) !== null; })) {
    window.setInterval(render, 1000);
  }
})();
