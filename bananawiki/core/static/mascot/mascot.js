/* The mascot: hop on every click; enough clicks in a row put its sunglasses on (saved on the account), as many
 * again take them off. Where to save them and how many clicks it takes are data attributes on the button itself,
 * which the top bar renders before any page content, so nothing a page contains can stand in for them. With
 * data-mascot-store="local" (no account) the sunglasses are kept in this browser instead.
 *
 * The sunglasses can be switched off on the button itself (wiki: Admin -> Appearance; portal: settings):
 * no data-mascot-clicks, or "0", turns them off (clicks only hop, nothing is saved). Key sequences such as
 * the Konami code are the BananaSuite easter eggs' (core/static/eggs/banana-eggs.js), not this script's. */
(function () {
  "use strict";

  var SHADES_PATH = /\/mascot\/shades$/;
  var STORE_KEY = "bananawiki.mascot.shades";
  var SVG_NS = "http://www.w3.org/2000/svg";

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
  var local = button.getAttribute("data-mascot-store") === "local";
  var url = local ? null : shadesUrl(button.getAttribute("data-mascot-url"));
  var needed = parseInt(button.getAttribute("data-mascot-clicks"), 10);
  var shadesEgg = needed > 0; // false for a missing, empty or "0" count
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

  function remembered() {
    try { return window.localStorage.getItem(STORE_KEY) === "1"; } catch (e) { return false; }
  }

  function save(on) {
    if (!local) return BW.fetchJSON(url, { method: on ? "POST" : "DELETE" });
    try {
      if (on) window.localStorage.setItem(STORE_KEY, "1"); else window.localStorage.removeItem(STORE_KEY);
    } catch (e) { /* storage blocked: they stay until the page is left */ }
    return Promise.resolve();
  }

  function wear(on) {
    if (on) sprite.classList.add("mascot-sprite--shades"); else sprite.classList.remove("mascot-sprite--shades");
    var label = BW.t(on ? "mascot.label_shades" : "mascot.label");
    button.setAttribute("aria-label", label);
    button.setAttribute("title", label);
  }

  // The glint is one white art pixel wide and runs across both lenses once they have landed (mascot.css).
  function drop() {
    var shades = sprite.querySelector(".mascot__shades");
    if (shades && !shades.querySelector(".mascot__glint")) {
      var glint = document.createElementNS(SVG_NS, "rect");
      var attributes = { "class": "mascot__glint", x: 4, y: 6, width: 1, height: 2, fill: "#ffffff" };
      Object.keys(attributes).forEach(function (name) { glint.setAttribute(name, attributes[name]); });
      shades.appendChild(glint);
    }
    replay("mascot--shades-drop", 950);
  }

  if (local && shadesEgg && remembered()) wear(true);

  button.addEventListener("click", function () {
    if (!shadesEgg) {
      replay("mascot--hop", 450);
      return;
    }
    clicks += 1;
    // Past half way, the hop gets higher.
    if (clicks * 2 >= needed) button.classList.add("mascot--eager");
    replay("mascot--hop", 450);
    if ((!url && !local) || saving || clicks < needed) return;
    var on = !hasShades();
    saving = true;
    save(on).then(function () {
      wear(on);
      if (on) {
        drop();
        BW.toast(BW.t(local ? "mascot.unlocked_local" : "mascot.unlocked"), "success");
      } else {
        BW.toast(BW.t("mascot.shades_off"), "success");
      }
    }).catch(function () {
      BW.toast(BW.t("error"), "error");
    }).then(function () {
      clicks = 0;
      button.classList.remove("mascot--eager");
      saving = false;
    });
  });
})();
