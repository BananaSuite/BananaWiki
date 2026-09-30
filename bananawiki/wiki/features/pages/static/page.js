/* Page view: notice when someone saves the page, and who is editing it. */
(function () {
  "use strict";
  var stateNode = document.querySelector("script#page-state");
  if (!stateNode) return;
  var state = JSON.parse(stateNode.textContent || "{}");
  var banner = document.querySelector("[data-page-updated]");
  var presence = document.querySelector("[data-presence]");
  var SYNC_MS = 30000, PRESENCE_MS = 30000;

  function visible() { return document.visibilityState !== "hidden"; }

  function sync() {
    if (!state.sync || !visible()) return;
    BW.fetchJSON(state.sync + "?since=" + encodeURIComponent(state.revision)).then(function (data) {
      if (data.changed && banner) { banner.hidden = false; state.sync = ""; }
    }).catch(function () {});
  }

  function checkPresence() {
    if (!presence || !visible()) return;
    BW.fetchJSON(presence.getAttribute("data-url")).then(function (data) {
      var names = data.editors || [];
      presence.hidden = names.length === 0;
      presence.textContent = names.length ? BW.t("pages.presence", { names: names.join(", ") }) : "";
    }).catch(function () {});
  }

  if (state.sync) setInterval(sync, SYNC_MS);
  if (presence) { checkPresence(); setInterval(checkPresence, PRESENCE_MS); }
})();
