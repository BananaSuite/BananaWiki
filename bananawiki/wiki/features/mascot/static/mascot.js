/* The mascot: hop on every click; enough clicks in a row put its sunglasses on (saved on the account). */
(function () {
  "use strict";

  var button = document.querySelector("[data-mascot]");
  var configEl = document.getElementById("mascot-config");
  if (!button || !configEl) return;
  var config = JSON.parse(configEl.textContent);
  var sprite = button.querySelector(".mascot-sprite");
  var clicks = 0;
  var saving = false;

  function replay(className, ms) {
    button.classList.remove(className);
    void button.offsetWidth; // restart the animation
    button.classList.add(className);
    window.setTimeout(function () { button.classList.remove(className); }, ms);
  }

  function hasShades() {
    return sprite.classList.contains("mascot-sprite--shades");
  }

  button.addEventListener("click", function () {
    replay("mascot--hop", 450);
    if (saving || hasShades()) return;
    clicks += 1;
    if (clicks < config.clicks) return;
    saving = true;
    BW.fetchJSON(config.url, { method: "POST" }).then(function () {
      sprite.classList.add("mascot-sprite--shades");
      replay("mascot--shades-drop", 650);
      var label = BW.t("mascot.label_shades");
      button.setAttribute("aria-label", label);
      button.setAttribute("title", label);
      BW.toast(BW.t("mascot.unlocked"), "success");
    }).catch(function () {
      clicks = 0;
    }).then(function () {
      saving = false;
    });
  });
})();
