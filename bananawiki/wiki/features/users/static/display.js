/* Saves the top-bar theme switch in the viewer's display preferences, so the
 * choice follows the account (or the visitor's cookie) instead of one browser. */
(function () {
  "use strict";
  var node = document.querySelector("script#users-display-config");
  var toggle = document.querySelector("[data-theme-toggle]");
  if (!node || !toggle || !window.BW) return;
  var config;
  try { config = JSON.parse(node.textContent || "{}"); } catch (e) { return; }
  BW.onReady(function () {
    // Register after the core's ready callback, so its theme switch runs first.
    // Serialize saves so rapid toggles cannot persist an earlier choice last.
    var saving = Promise.resolve();
    toggle.addEventListener("click", function () {
      var theme = document.documentElement.getAttribute("data-theme");
      saving = saving.then(function () {
        return BW.fetchJSON(config.url, { method: "POST", body: { theme_mode: theme } });
      }).catch(function () {
        BW.toast(BW.t("error"), "error");
      });
    });
  });
})();
