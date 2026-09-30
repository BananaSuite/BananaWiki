/* Portal behaviours: mobile menu, theme label, remembered sections, dismissible banners and resumable archive uploads. No inline handlers. */
(function () {
  "use strict";
  var BW = window.BW;

  BW.onReady(function () {
    // Small screens: the top bar links fold into a menu.
    var navToggle = document.querySelector("[data-nav-toggle]");
    var nav = navToggle && document.getElementById(navToggle.getAttribute("aria-controls"));
    if (nav) {
      navToggle.addEventListener("click", function () {
        var open = nav.classList.toggle("is-open");
        navToggle.setAttribute("aria-expanded", open ? "true" : "false");
      });
      document.addEventListener("keydown", function (event) {
        if (event.key === "Escape" && nav.classList.contains("is-open")) {
          nav.classList.remove("is-open");
          navToggle.setAttribute("aria-expanded", "false");
        }
      });
    }

    // Signed-out theme button: bananawiki.js flips the theme, this keeps the label in step.
    var themeButton = document.querySelector("[data-theme-toggle][data-label-light]");
    if (themeButton) {
      themeButton.addEventListener("click", function () {
        var light = document.documentElement.getAttribute("data-theme") === "light";
        themeButton.textContent = themeButton.getAttribute(light ? "data-label-dark" : "data-label-light");
      });
    }

    // Collapsible sections with data-remember keep their open state across saves.
    document.querySelectorAll("details[data-remember][id]").forEach(function (details) {
      var key = "bwh-open-" + location.pathname + "-" + details.id;
      try {
        var saved = sessionStorage.getItem(key);
        if (saved === "1") details.open = true;
        else if (saved === "0" && location.hash !== "#" + details.id) details.open = false;
      } catch (e) { /* storage off */ }
      details.addEventListener("toggle", function () {
        try { sessionStorage.setItem(key, details.open ? "1" : "0"); } catch (e) { /* storage off */ }
      });
    });

    document.querySelectorAll("[data-banner-id]").forEach(function (banner) {
      var key = "bwh-banner-" + banner.getAttribute("data-banner-id") + "-" + banner.getAttribute("data-banner-revision");
      try { if (localStorage.getItem(key) === "1") { banner.remove(); return; } } catch (e) { /* storage off */ }
      var close = banner.querySelector("[data-banner-dismiss]");
      if (close) {
        close.addEventListener("click", function () {
          try { localStorage.setItem(key, "1"); } catch (e) { /* storage off */ }
          banner.remove();
        });
      }
    });

    document.querySelectorAll("form[data-chunked-upload]").forEach(function (form) {
      form.addEventListener("submit", function (event) {
        var input = form.querySelector("input[type=file]");
        if (!input || !input.files || !input.files.length || !window.fetch) return;
        event.preventDefault();
        upload(form, input.files[0]);
      });
    });
  });

  function post(url, data) {
    return BW.fetchJSON(url, { method: "POST", body: data });
  }

  function upload(form, file) {
    var status = form.querySelector("[data-upload-status]");
    var base = form.getAttribute("data-chunked-upload");
    var start = new FormData(form);
    start.delete(form.querySelector("input[type=file]").name);
    start.set("filename", file.name);
    start.set("size", String(file.size));
    function say(text) { if (status) status.textContent = text; }
    post(base + "/start", start).then(function (answer) {
      var id = answer.upload_id, size = answer.chunk_size, offset = 0;
      function next() {
        if (offset >= file.size) return post(base + "/" + id + "/complete", new FormData());
        var data = new FormData();
        data.set("offset", String(offset));
        data.set("chunk", file.slice(offset, offset + size), "chunk");
        return post(base + "/" + id, data).then(function (reply) {
          offset = reply.received;
          say(BW.t("upload_progress", { percent: Math.floor(offset * 100 / file.size) }));
          return next();
        });
      }
      return next();
    }).then(function (done) {
      say(done.message || "");
      if (done.redirect) window.location.assign(done.redirect);
    }).catch(function (error) {
      say(error.message || BW.t("upload_failed"));
    });
  }
})();
