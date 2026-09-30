/* Counts the time a signed-in member spends reading a page (tab visible and
 * recently used) and reports it once a minute for the reading-time badges. */
(function () {
  "use strict";
  var node = document.querySelector("script#badges-reading-config");
  if (!node || !window.BW) return;
  var config;
  try { config = JSON.parse(node.textContent || "{}"); } catch (e) { return; }
  var IDLE_AFTER = 120000;
  var lastActivity = Date.now();
  var seconds = 0;
  ["scroll", "keydown", "mousemove", "touchstart"].forEach(function (name) {
    window.addEventListener(name, function () { lastActivity = Date.now(); }, { passive: true });
  });
  setInterval(function () {
    if (document.visibilityState === "visible" && Date.now() - lastActivity < IDLE_AFTER) seconds += 5;
  }, 5000);
  setInterval(function () {
    if (seconds < 30) return;
    var sent = Math.min(seconds, 120);
    seconds -= sent;
    BW.fetchJSON(config.url, { method: "POST", body: { seconds: sent } }).catch(function () { /* retried next minute */ });
  }, 60000);
})();
