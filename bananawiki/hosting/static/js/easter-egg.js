/* The portal's easter egg page for visitors who are not signed in: their finds are kept in this browser
 * (banana-eggs.js, BananaEggs.localFinds()), so this marks the eggs found here and counts them again.
 * Loaded deferred, after banana-eggs.js. Texts come from the page's data attributes; no inline script. */
(function () {
  "use strict";

  var page = document.querySelector("[data-easter-egg-page][data-local-finds]");
  if (!page || !window.BananaEggs || typeof window.BananaEggs.localFinds !== "function") return;
  var finds = window.BananaEggs.localFinds();
  var eggs = page.querySelectorAll("[data-egg-id]");
  var found = 0;

  function fill(template, values) {
    return Object.keys(values).reduce(function (text, key) {
      return text.split("{" + key + "}").join(String(values[key]));
    }, template || "");
  }

  Array.prototype.forEach.call(eggs, function (egg) {
    var id = egg.getAttribute("data-egg-id");
    if (!Object.prototype.hasOwnProperty.call(finds, id)) return;
    found += 1;
    var mark = egg.querySelector("[data-egg-mark]");
    var text = egg.querySelector("[data-egg-text]");
    if (mark) mark.textContent = egg.getAttribute("data-egg-sprite") || "?";
    if (text) {
      text.textContent = fill(page.getAttribute("data-found-template"), { name: id });
      var when = new Date(finds[id]);
      if (!isNaN(when.getTime())) {
        var date = document.createElement("span");
        date.className = "muted small";
        date.textContent = " " + when.toLocaleDateString();
        text.appendChild(date);
      }
    }
  });
  var count = page.querySelector("[data-easter-egg-count]");
  if (count) count.textContent = fill(page.getAttribute("data-count-template"), { found: found, total: eggs.length });
})();
