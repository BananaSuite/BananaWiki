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
 *
 * Page content keeps the ids of its headings (anchors) and can come before the interface's own
 * elements, so scripts never look an element up by its id alone: they name its tag ("dialog#…":
 * content cannot make one), use a data attribute, or look inside their own container.
 */
(function () {
  "use strict";

  // Only a BW that a script set, not an element of the page named "BW" (window's named access).
  var BW = window.BW = Object.prototype.hasOwnProperty.call(window, "BW") ? window.BW : {};
  var SCROLL_BLOCKS = "pre, .table-wrap, .prose table, .prose .table-wrapper";
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

  // Count-dependent string: "<key>_one" when count is 1 (if the catalogue has it), else "<key>".
  BW.tn = function (key, count, vars) {
    vars = vars || {};
    if (vars.count === undefined) vars.count = count;
    var one = key + "_one";
    var useOne = Number(count) === 1 && Object.prototype.hasOwnProperty.call(strings, one);
    return BW.t(useOne ? one : key, vars);
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

  // Move-up/down buttons: the first item's "up" and the last item's "down" cannot move and say so
  // (aria-disabled keeps them focusable, so focus is not lost after a move to either end).
  // Buttons carry data-move="up"/"-1" or "down"/"1"; items are the children of *list* matching *itemSelector*.
  BW.markMoveEnds = function (list, itemSelector) {
    if (!list) return;
    var items = Array.prototype.filter.call(list.children, function (child) { return child.matches(itemSelector); });
    items.forEach(function (item, index) {
      item.querySelectorAll("[data-move]").forEach(function (button) {
        if (button.closest(itemSelector) !== item) return;
        var move = button.getAttribute("data-move");
        var end = (move === "up" || move === "-1") ? index === 0 : (move === "down" || move === "1") ? index === items.length - 1 : false;
        if (end) button.setAttribute("aria-disabled", "true"); else button.removeAttribute("aria-disabled");
      });
    });
  };

  // A scroller whose children are opaque (board columns, palette groups) cannot show CSS scroll shadows:
  // data-more on it names the edges ("left right top bottom") past which content is hidden, and the CSS
  // fades those edges. data-scroll-cue="y" watches the vertical axis, anything else the horizontal one.
  BW.scrollCue = function (el) {
    if (!el || el.__bwScrollCue) return;
    el.__bwScrollCue = true;
    var vertical = el.getAttribute("data-scroll-cue") === "y";
    var pending = false;
    function update() {
      pending = false;
      var more = [];
      // A couple of pixels (sub-pixel padding, a snap point just inside the edge) are not hidden content.
      if (vertical) {
        if (el.scrollTop > 2) more.push("top");
        if (el.scrollTop + el.clientHeight < el.scrollHeight - 2) more.push("bottom");
      } else {
        var hidden = el.scrollWidth - el.clientWidth;
        var x = el.scrollLeft;  // 0 at the start; negative towards the left in right-to-left pages
        var left = x < 0 ? hidden + x : x;
        if (left > 2) more.push("left");
        if (hidden - left > 2) more.push("right");
      }
      var value = more.join(" ");
      if (el.getAttribute("data-more") !== value) el.setAttribute("data-more", value);
    }
    function later() { if (!pending) { pending = true; window.requestAnimationFrame(update); } }
    el.addEventListener("scroll", later, { passive: true });
    window.addEventListener("resize", later);
    if (window.ResizeObserver) new ResizeObserver(later).observe(el);
    if (window.MutationObserver) new MutationObserver(later).observe(el, { childList: true, subtree: true });
    update();
  };

  // A .table--stack table turns into one card per row (.table--stacked) whenever its columns do not fit
  // the room it has. Phones get the cards from the stylesheet alone; on a tablet or a narrow window the
  // admin menu or the sidebar can leave less room than a phone, and the row actions would sit past the
  // right edge. Each table is measured as a table again whenever its wrapper changes width.
  BW.fitTables = function (root) {
    (root || document).querySelectorAll("table.table--stack").forEach(function (table) {
      if (table.__bwFit) return;
      table.__bwFit = true;
      var wrap = table.closest(".table-wrap") || table.parentElement;
      var width = -1;
      var pending = false;
      function update() {
        pending = false;
        if (wrap.clientWidth === width) return;
        width = wrap.clientWidth;
        var was = table.classList.contains("table--stacked");
        table.classList.remove("table--stacked");
        var stack = width > 0 && wrap.scrollWidth > width + 1;
        if (stack) table.classList.add("table--stacked");
        // The wrapper takes keyboard focus only while it scrolls.
        if (stack !== was && BW.focusScrollingBlocks) BW.focusScrollingBlocks(wrap.parentElement);
      }
      function later() { if (!pending) { pending = true; window.requestAnimationFrame(update); } }
      if (window.ResizeObserver) new ResizeObserver(later).observe(wrap);
      else window.addEventListener("resize", later);
      update();
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
      var dialogId = opener.getAttribute("data-dialog-open");
      var dialog = dialogId && document.querySelector("dialog#" + CSS.escape(dialogId));
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

    // A tap outside the open phone drawer (on the dimmed page) closes it.
    if (document.body.classList.contains("sidebar-open") && !closest(target, ".sidebar")
        && !closest(target, "[data-sidebar-toggle]")) {
      BW.closeSidebar(false);
    }

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
      var sidebarOpen = document.body.classList.contains("sidebar-open");
      var sidebar = document.querySelector(".sidebar");
      var sidebarHadFocus = sidebar && sidebar.contains(document.activeElement);
      document.querySelectorAll("details.menu[open]").forEach(function (menu) {
        var restoreFocus = menu.contains(document.activeElement);
        menu.removeAttribute("open");
        var summary = menu.querySelector("summary");
        if (restoreFocus && summary) summary.focus();
      });
      if (sidebarOpen) BW.closeSidebar(sidebarHadFocus);
    }
  });

  // Sidebar (mobile drawer) and theme ---------------------------------------
  // The phone drawer starts at the topbar's real bottom edge: announcement and other
  // banners above the topbar push it down until the page scrolls.
  var placeDrawer = function () {
    var topbar = document.querySelector(".topbar");
    if (!topbar) return;
    var bottom = Math.max(0, Math.round(topbar.getBoundingClientRect().bottom));
    document.documentElement.style.setProperty("--drawer-top", bottom + "px");
  };
  // Kept current while scrolling (the sidebar's height follows it too), at most once per frame.
  var placing = false;
  var schedulePlace = function () {
    if (placing) return;
    placing = true;
    window.requestAnimationFrame(function () { placing = false; placeDrawer(); });
  };
  window.addEventListener("scroll", schedulePlace, { passive: true });
  window.addEventListener("resize", schedulePlace);
  BW.closeSidebar = function (restoreFocus) {
    document.body.classList.remove("sidebar-open");
    var toggle = document.querySelector("[data-sidebar-toggle]");
    if (toggle) {
      toggle.setAttribute("aria-expanded", "false");
      if (restoreFocus) toggle.focus();
    }
  };

  BW.onReady(function () {
    placeDrawer();
    // Feature actions can include a dialog. Keep modal forms outside dropdown
    // panels so menu styling and a closed menu cannot hide their contents.
    document.querySelectorAll("details.menu dialog").forEach(function (dialog) {
      document.body.appendChild(dialog);
    });
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
          placeDrawer();
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

    BW.fitTables();

    // A code block or table that scrolls sideways must be reachable with the keyboard.
    // Scripts that render content later (the editor preview) call BW.focusScrollingBlocks(root).
    BW.focusScrollingBlocks = function (root) {
      (root || document).querySelectorAll(SCROLL_BLOCKS).forEach(function (block) {
        if (block.scrollWidth > block.clientWidth + 1) block.setAttribute("tabindex", "0");
        else if (!block.hasAttribute("data-keep-tabindex")) block.removeAttribute("tabindex");
      });
    };
    BW.focusScrollingBlocks();
    window.addEventListener("resize", function () { BW.focusScrollingBlocks(); });

    document.querySelectorAll("[data-scroll-cue]").forEach(BW.scrollCue);

    // Flash messages fade out on their own.
    document.querySelectorAll(".flash-stack .alert[data-autohide]").forEach(function (alert) {
      setTimeout(function () { alert.remove(); }, 7000);
    });
  });
})();
