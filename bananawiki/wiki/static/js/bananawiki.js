/* BananaWiki core behaviours. Loaded on every page; no inline handlers anywhere.
 *
 * Exposes window.BW with:
 *   BW.t(key, vars)          translated string from the js.* catalogue
 *   BW.csrf()                the CSRF token
 *   BW.fetchJSON(url, opts)  fetch with CSRF header and JSON handling
 *   BW.toast(message, kind)  transient notification
 *   BW.onReady(fn)
 *
 * Declarative hooks:
 *   data-confirm="message"        on a form or button: ask before submitting
 *   data-autosubmit               on an input/select: submit its form on change
 *   data-copy="text"              on a button: copy to clipboard
 *   data-dialog-open="id"         open <dialog id=...>; data-dialog-close closes the enclosing dialog
 *   data-dismiss                  inside .alert: remove it
 *   data-toggle-target="selector" toggle the hidden attribute of matching elements
 */
(function () {
  "use strict";

  var BW = window.BW = window.BW || {};
  var strings = {};
  try {
    var node = document.querySelector("script#bw-strings");
    if (node) strings = JSON.parse(node.textContent || "{}");
  } catch (e) { strings = {}; }

  BW.t = function (key, vars) {
    var text = Object.prototype.hasOwnProperty.call(strings, key) ? strings[key] : key;
    if (vars) {
      Object.keys(vars).forEach(function (name) {
        text = text.split("{" + name + "}").join(String(vars[name]));
      });
    }
    return text;
  };

  BW.csrf = function () {
    var meta = document.querySelector('meta[name="csrf-token"]');
    return meta ? meta.getAttribute("content") : "";
  };

  BW.fetchJSON = function (url, options) {
    options = options || {};
    var headers = new Headers(options.headers || {});
    headers.set("Accept", "application/json");
    headers.set("X-CSRF-Token", BW.csrf());
    var body = options.body;
    if (body && !(body instanceof FormData) && typeof body !== "string") {
      headers.set("Content-Type", "application/json");
      body = JSON.stringify(body);
    }
    return fetch(url, {
      method: options.method || (body ? "POST" : "GET"),
      headers: headers,
      body: body,
      credentials: "same-origin",
      signal: options.signal
    }).then(function (response) {
      var type = response.headers.get("Content-Type") || "";
      var parse = type.indexOf("application/json") !== -1 ? response.json() : response.text();
      return parse.then(function (data) {
        if (!response.ok) {
          var error = new Error((data && data.error) || response.statusText || "Request failed");
          error.status = response.status;
          error.data = data;
          throw error;
        }
        return data;
      });
    });
  };

  BW.onReady = function (fn) {
    if (document.readyState !== "loading") fn();
    else document.addEventListener("DOMContentLoaded", fn);
  };

  BW.toast = function (message, kind) {
    var stack = document.querySelector(".flash-stack");
    if (!stack) {
      stack = document.createElement("div");
      stack.className = "flash-stack";
      stack.setAttribute("role", "status");
      stack.setAttribute("aria-live", "polite");
      document.body.appendChild(stack);
    }
    var alert = document.createElement("div");
    alert.className = "alert alert--" + (kind || "info");
    var body = document.createElement("div");
    body.className = "alert__body";
    body.textContent = message;
    var close = document.createElement("button");
    close.type = "button";
    close.className = "alert__close";
    close.setAttribute("data-dismiss", "");
    close.setAttribute("aria-label", BW.t("close"));
    close.textContent = "×";
    alert.appendChild(body);
    alert.appendChild(close);
    stack.appendChild(alert);
    setTimeout(function () { alert.remove(); }, 6000);
  };

  function closest(el, selector) {
    return el && el.closest ? el.closest(selector) : null;
  }

  // Confirmation prompts ----------------------------------------------------
  document.addEventListener("submit", function (event) {
    var form = event.target;
    var submitter = event.submitter;
    var message = (submitter && submitter.getAttribute("data-confirm")) || form.getAttribute("data-confirm");
    if (message && !window.confirm(message)) {
      event.preventDefault();
      return;
    }
    // Avoid double submissions.
    if (!form.hasAttribute("data-allow-resubmit")) {
      if (form.dataset.submitting === "1") { event.preventDefault(); return; }
      form.dataset.submitting = "1";
      setTimeout(function () { form.dataset.submitting = ""; }, 4000);
    }
  }, true);

  document.addEventListener("click", function (event) {
    var target = event.target;

    var link = closest(target, "a[data-confirm]");
    if (link && !window.confirm(link.getAttribute("data-confirm"))) {
      event.preventDefault();
      return;
    }

    var dismiss = closest(target, "[data-dismiss]");
    if (dismiss) {
      var alert = closest(dismiss, ".alert, .banner");
      if (alert) alert.remove();
      return;
    }

    var copy = closest(target, "[data-copy]");
    if (copy) {
      var text = copy.getAttribute("data-copy");
      if (navigator.clipboard) {
        navigator.clipboard.writeText(text).then(function () { BW.toast(BW.t("copied"), "success"); });
      }
      return;
    }

    var opener = closest(target, "[data-dialog-open]");
    if (opener) {
      var dialog = document.getElementById(opener.getAttribute("data-dialog-open"));
      if (dialog && dialog.showModal) { event.preventDefault(); dialog.showModal(); }
      return;
    }

    var closer = closest(target, "[data-dialog-close]");
    if (closer) {
      var parentDialog = closest(closer, "dialog");
      if (parentDialog) { event.preventDefault(); parentDialog.close(); }
      return;
    }

    var toggle = closest(target, "[data-toggle-target]");
    if (toggle) {
      event.preventDefault();
      document.querySelectorAll(toggle.getAttribute("data-toggle-target")).forEach(function (el) {
        el.hidden = !el.hidden;
      });
      toggle.setAttribute("aria-expanded", toggle.getAttribute("aria-expanded") === "true" ? "false" : "true");
      return;
    }

    var print = closest(target, "[data-print]");
    if (print) { event.preventDefault(); window.print(); return; }

    // Close open <details class="menu"> when clicking elsewhere.
    document.querySelectorAll("details.menu[open]").forEach(function (menu) {
      if (!menu.contains(target)) menu.removeAttribute("open");
    });
  });

  document.addEventListener("change", function (event) {
    var el = event.target;
    if (el && el.hasAttribute && el.hasAttribute("data-autosubmit") && el.form) {
      if (el.form.requestSubmit) el.form.requestSubmit(); else el.form.submit();
    }
  });

  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape") {
      document.body.classList.remove("sidebar-open");
      document.querySelectorAll("details.menu[open]").forEach(function (menu) { menu.removeAttribute("open"); });
    }
  });

  // Sidebar (mobile drawer) and theme ---------------------------------------
  BW.onReady(function () {
    // The menu button opens the drawer on phones and hides/shows the sidebar on wide
    // screens (as the 1.4 collapse button did); the wide-screen choice is remembered.
    var toggle = document.querySelector("[data-sidebar-toggle]");
    if (toggle) {
      var narrow = window.matchMedia ? window.matchMedia("(max-width: 900px)") : { matches: false };
      var collapsed = false;
      try { collapsed = localStorage.getItem("bw-sidebar-collapsed") === "1"; } catch (e) { collapsed = false; }
      document.body.classList.toggle("sidebar-collapsed", collapsed);
      var sync = function () {
        var shown = narrow.matches ? document.body.classList.contains("sidebar-open")
          : !document.body.classList.contains("sidebar-collapsed");
        toggle.setAttribute("aria-expanded", shown ? "true" : "false");
      };
      sync();
      if (narrow.addEventListener) narrow.addEventListener("change", sync);
      toggle.addEventListener("click", function () {
        if (narrow.matches) {
          document.body.classList.toggle("sidebar-open");
        } else {
          var now = document.body.classList.toggle("sidebar-collapsed");
          try { localStorage.setItem("bw-sidebar-collapsed", now ? "1" : "0"); } catch (e) { /* storage disabled */ }
        }
        sync();
      });
    }

    var themeButton = document.querySelector("[data-theme-toggle]");
    if (themeButton) {
      themeButton.addEventListener("click", function () {
        var root = document.documentElement;
        var next = root.getAttribute("data-theme") === "light" ? "dark" : "light";
        root.setAttribute("data-theme", next);
        try { localStorage.setItem("bw-theme", next); } catch (e) { /* storage disabled */ }
        document.cookie = "bw_theme=" + next + ";path=/;max-age=31536000;samesite=lax";
      });
    }

    // Remember which sidebar tree nodes are open.
    var tree = document.querySelector("[data-nav-tree]");
    if (tree) {
      var saved = {};
      try { saved = JSON.parse(localStorage.getItem("bw-nav-open") || "{}"); } catch (e) { saved = {}; }
      tree.querySelectorAll("details[data-node]").forEach(function (details) {
        var id = details.getAttribute("data-node");
        if (saved[id] === true) details.open = true;
        if (saved[id] === false && !details.hasAttribute("data-current")) details.open = false;
        details.addEventListener("toggle", function () {
          saved[id] = details.open;
          try { localStorage.setItem("bw-nav-open", JSON.stringify(saved)); } catch (e) { /* ignore */ }
        });
      });
    }

    // Flash messages fade out on their own.
    document.querySelectorAll(".flash-stack .alert[data-autohide]").forEach(function (alert) {
      setTimeout(function () { alert.remove(); }, 7000);
    });
  });
})();
