/* The mascot: hop on every click; enough clicks in a row put its sunglasses on (saved on the account).
 * Where to save them and how many clicks it takes are data attributes on the button itself, which the
 * top bar renders before any page content, so nothing a page contains can stand in for them. */
(function () {
  "use strict";

  var SHADES_PATH = /\/mascot\/shades$/;

  // Only this wiki's own sunglasses endpoint; anything else leaves the mascot hopping and nothing more.
  function shadesUrl(value) {
    try {
      var target = new URL(value || "", window.location.href);
      if (target.origin === window.location.origin && SHADES_PATH.test(target.pathname)) return target.href;
    } catch (e) { /* not a URL */ }
    return null;
  }

  var button = document.querySelector("[data-mascot]");
  var sprite = button && button.querySelector(".mascot-sprite");
  if (!sprite) return;
  var url = shadesUrl(button.getAttribute("data-mascot-url"));
  var needed = Math.max(1, parseInt(button.getAttribute("data-mascot-clicks"), 10) || 0);
  var clicks = 0;
  var saving = false;
  var timers = {};

  function replay(className, ms) {
    window.clearTimeout(timers[className]); // a click mid-hop restarts it instead of cutting the next one short
    button.classList.remove(className);
    void button.offsetWidth; // restart the animation
    button.classList.add(className);
    timers[className] = window.setTimeout(function () { button.classList.remove(className); }, ms);
  }

  function hasShades() {
    return sprite.classList.contains("mascot-sprite--shades");
  }

  button.addEventListener("click", function () {
    replay("mascot--hop", 450);
    if (!url || saving || hasShades()) return;
    clicks += 1;
    if (clicks < needed) return;
    saving = true;
    BW.fetchJSON(url, { method: "POST" }).then(function () {
      sprite.classList.add("mascot-sprite--shades");
      replay("mascot--shades-drop", 650);
      var label = BW.t("mascot.label_shades");
      button.setAttribute("aria-label", label);
      button.setAttribute("title", label);
      BW.toast(BW.t("mascot.unlocked"), "success");
    }).catch(function () {
      clicks = 0;
      BW.toast(BW.t("error"), "error");
    }).then(function () {
      saving = false;
    });
  });
})();
