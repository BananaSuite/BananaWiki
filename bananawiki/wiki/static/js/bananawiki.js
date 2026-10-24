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

  // File sizes as people read them, like the server's filesize filter: "900 B", "2.3 KB", "5 MB".
  BW.formatBytes = function (value) {
    var size = Math.max(0, Number(value) || 0);
    var units = ["B", "KB", "MB", "GB"];
    var unit = 0;
    while (size >= 1024 && unit < units.length - 1) { size /= 1024; unit += 1; }
    if (unit === 0) return Math.round(size) + " B";
    var text = size.toFixed(1).replace(/\.0$/, "");
    var lang = (document.documentElement.getAttribute("lang") || "en").split("-")[0];
    if (["it", "de", "fr", "es", "pt", "nl"].indexOf(lang) !== -1) text = text.replace(".", ",");
    return text + " " + units[unit];
  };

  // Uploads: XMLHttpRequest, because fetch() reports no upload progress.
  //   BW.upload(url, formData, {onProgress(loaded, total), signal, retries, method}) -> Promise of the
  //   JSON answer (like fetchJSON). A failure carries .status, .data, .aborted (cancelled) or .network.
  // A request that lost its connection before the whole body was sent cannot have been stored, so it
  // is sent again (twice at most); so is one the server refused as busy (503) before keeping anything.
  var activeUploads = 0;
  BW.uploadsActive = function () { return activeUploads > 0; };
  function cancelledError() {
    var error = new Error(BW.t("upload.cancelled_short"));
    error.aborted = true;
    error.status = 0;
    error.sent = false;
    return error;
  }
  function uploadOnce(url, body, options) {
    return new Promise(function (resolve, reject) {
      // Cancelled before this attempt began (during the pause before a retry): an XMLHttpRequest
      // aborted before send() fires no event, so settle here instead of waiting for one forever.
      if (options.signal && options.signal.aborted) { reject(cancelledError()); return; }
      var xhr = new XMLHttpRequest();
      var sent = false;
      xhr.open(options.method || "POST", url, true);
      xhr.setRequestHeader("Accept", options.accept || "application/json");
      xhr.setRequestHeader("X-CSRF-Token", BW.csrf());
      xhr.setRequestHeader("X-BW-Upload", "1");
      Object.keys(options.headers || {}).forEach(function (name) { xhr.setRequestHeader(name, options.headers[name]); });
      if (xhr.upload) {
        xhr.upload.addEventListener("progress", function (event) {
          if (options.onProgress) options.onProgress(event.loaded, event.lengthComputable ? event.total : 0);
        });
        xhr.upload.addEventListener("load", function () { sent = true; });
      }
      var fail = function (props) {
        var error = new Error(props.message || BW.t("upload.network"));
        Object.keys(props).forEach(function (key) { error[key] = props[key]; });
        error.sent = sent;
        reject(error);
      };
      xhr.addEventListener("load", function () {
        var type = xhr.getResponseHeader("Content-Type") || "";
        var data = xhr.responseText;
        if (type.indexOf("application/json") !== -1) {
          try { data = JSON.parse(xhr.responseText); } catch (e) { data = null; }
        }
        if (xhr.status >= 200 && xhr.status < 300) { resolve(data); return; }
        var message = (data && typeof data === "object" && data.error) || describeErrorPage(data) ||
          (xhr.status === 413 ? BW.t("upload.too_large_server") : BW.t("error"));
        fail({ status: xhr.status, data: data, message: message });
      });
      xhr.addEventListener("error", function () { fail({ network: true, status: 0 }); });
      xhr.addEventListener("timeout", function () { fail({ network: true, status: 0 }); });
      xhr.addEventListener("abort", function () { fail({ aborted: true, status: 0, message: BW.t("upload.cancelled_short") }); });
      if (options.signal) options.signal.addEventListener("abort", function () { xhr.abort(); });
      xhr.send(body);
    });
  }
  // The heading of an HTML error page ("Too many requests"), for a message next to the upload.
  function describeErrorPage(html) {
    if (typeof html !== "string" || html.indexOf("<") === -1 || !window.DOMParser) return "";
    try {
      var doc = new DOMParser().parseFromString(html, "text/html");
      var node = doc.querySelector(".error-page h1 + p, .error-page h1, main h1, h1");
      return node ? node.textContent.replace(/\s+/g, " ").trim().slice(0, 300) : "";
    } catch (e) { return ""; }
  }
  BW.upload = function (url, body, options) {
    options = options || {};
    var retries = options.retries === undefined ? 2 : options.retries;
    var attempt = 0;
    activeUploads += 1;
    var settle = function () { activeUploads = Math.max(0, activeUploads - 1); };
    var run = function () {
      return uploadOnce(url, body, options).catch(function (error) {
        var again = !error.aborted && attempt < retries && !(options.signal && options.signal.aborted) &&
          ((error.network && !error.sent) || error.status === 503);
        if (!again) throw error;
        attempt += 1;
        if (options.onRetry) options.onRetry(attempt, retries);
        // The pause before the next attempt ends early when the upload is cancelled meanwhile.
        return new Promise(function (resolve, reject) {
          var timer = setTimeout(resolve, 1000 * attempt * attempt);
          if (options.signal) {
            options.signal.addEventListener("abort", function () { clearTimeout(timer); reject(cancelledError()); });
          }
        }).then(run);
      });
    };
    return run().then(function (data) { settle(); return data; }, function (error) { settle(); throw error; });
  };
  // A leaving page drops its uploads: ask first.
  window.addEventListener("beforeunload", function (event) {
    if (!activeUploads) return undefined;
    event.preventDefault();
    event.returnValue = BW.t("upload.leave");
    return event.returnValue;
  });

  // A progress row for one upload: BW.uploadRow(container, name) -> {progress(loaded, total), retrying(n, max),
  // done(), fail(message), signal}. Its Cancel button aborts the request through .signal.
  // The bar and its numbers are for sight; a polite live region says when an upload starts and ends.
  BW.uploadRow = function (container, name) {
    var row = document.createElement("div");
    row.className = "upload-progress";
    row.setAttribute("data-upload-row", "");
    var head = document.createElement("div");
    head.className = "upload-progress__head";
    var label = document.createElement("span");
    label.className = "upload-progress__name";
    label.textContent = name;
    var numbers = document.createElement("span");
    numbers.className = "upload-progress__numbers";
    head.appendChild(label);
    head.appendChild(numbers);
    var bar = document.createElement("progress");
    bar.className = "progress";
    bar.max = 100;
    bar.setAttribute("aria-label", BW.t("upload.uploading", { name: name }));
    var cancel = document.createElement("button");
    cancel.type = "button";
    cancel.className = "btn btn--small btn--ghost upload-progress__cancel";
    cancel.textContent = BW.t("upload.cancel");
    cancel.setAttribute("aria-label", BW.t("upload.cancel_name", { name: name }));
    var live = document.createElement("span");
    live.className = "visually-hidden";
    live.setAttribute("role", "status");
    live.setAttribute("aria-live", "polite");
    row.appendChild(head);
    row.appendChild(bar);
    row.appendChild(cancel);
    row.appendChild(live);
    container.appendChild(row);
    var controller = window.AbortController ? new AbortController() : null;
    cancel.addEventListener("click", function () { if (controller) controller.abort(); });
    var last = 0;
    window.setTimeout(function () { live.textContent = BW.t("upload.uploading", { name: name }); }, 50);
    var finish = function () {
      cancel.remove();
      bar.remove();
    };
    return {
      element: row,
      signal: controller ? controller.signal : undefined,
      progress: function (loaded, total) {
        var now = Date.now();
        if (now - last < 100 && loaded < total) return;
        last = now;
        if (total) {
          var percent = Math.min(100, Math.floor(loaded * 100 / total));
          bar.value = percent;
          numbers.textContent = BW.t("upload.progress", {
            loaded: BW.formatBytes(loaded), total: BW.formatBytes(total), percent: percent });
          if (loaded >= total) numbers.textContent = BW.t("upload.processing");
        } else {
          bar.removeAttribute("value");
          numbers.textContent = BW.formatBytes(loaded);
        }
      },
      retrying: function (attempt, max) {
        numbers.textContent = BW.t("upload.retrying", { attempt: attempt, max: max });
      },
      done: function (text) {
        finish();
        row.classList.add("is-done");
        numbers.textContent = text || BW.t("upload.done_short");
        live.textContent = BW.t("upload.done", { name: name });
        window.setTimeout(function () { row.remove(); }, 2500);
      },
      fail: function (message, aborted) {
        finish();
        row.classList.add(aborted ? "is-cancelled" : "is-failed");
        numbers.textContent = "";
        var text = aborted ? BW.t("upload.cancelled", { name: name }) : BW.t("upload.failed", { name: name, reason: message || BW.t("error") });
        var note = document.createElement("span");
        note.className = "upload-progress__error";
        note.textContent = text;
        row.insertBefore(note, live);
        live.textContent = text;
        var dismiss = document.createElement("button");
        dismiss.type = "button";
        dismiss.className = "btn btn--small btn--ghost";
        dismiss.textContent = BW.t("upload.dismiss");
        dismiss.addEventListener("click", function () { row.remove(); });
        row.insertBefore(dismiss, live);
        if (aborted) window.setTimeout(function () { row.remove(); }, 4000);
      }
    };
  };

  // Checks a file against what the server accepts before sending it: {maxBytes, allow: [ext], deny: [ext]}.
  // Returns the reason it would be refused (without its name), or "" when the file may go.
  BW.uploadProblem = function (file, rules) {
    rules = rules || {};
    var name = file.name || "";
    var dot = name.lastIndexOf(".");
    var ext = dot > 0 ? name.slice(dot + 1).toLowerCase() : "";
    if (!file.size) return BW.t("upload.empty");
    if (rules.maxBytes && file.size > rules.maxBytes) return BW.t("upload.too_large", { limit: BW.formatBytes(rules.maxBytes) });
    if ((rules.allow || rules.deny) &&
        (!ext || (rules.deny && rules.deny.indexOf(ext) !== -1) || (rules.allow && rules.allow.indexOf(ext) === -1))) {
      return BW.t("upload.type", { ext: ext || "?" });
    }
    return "";
  };
  BW.uploadRules = function (element) {
    var raw = element && element.getAttribute("data-upload-rules");
    if (!raw) return {};
    try { return JSON.parse(raw) || {}; } catch (e) { return {}; }
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

  // The Clipboard API exists only in secure contexts: over plain HTTP the text is copied from a selection.
  function copyText(text) {
    var copySelection = function () {
      var focused = document.activeElement;
      var area = document.createElement("textarea");
      area.value = text;
      area.setAttribute("readonly", "");
      area.style.position = "fixed";
      area.style.top = "0";
      area.style.opacity = "0";
      document.body.appendChild(area);
      area.focus();
      area.select();
      var copied = false;
      try { copied = document.execCommand("copy"); } catch (e) { copied = false; }
      area.remove();
      if (focused && focused.focus) focused.focus();
      return copied;
    };
    var report = function (copied) {
      BW.toast(BW.t(copied ? "copied" : "copy_failed"), copied ? "success" : "error");
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(function () { report(true); }, function () { report(copySelection()); });
    } else {
      report(copySelection());
    }
  }

  // Confirmation prompts
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

  // A form that sends files (enctype multipart/form-data; data-upload-progress="off" opts out) sends them
  // with a progress bar and a Cancel button. The server answers such a request's redirect with
  // {"redirect": url} (X-BW-Upload), and the page goes there as after a plain submit, so the flash
  // messages show; a page it renders instead (a form with its errors) replaces this one. Files the
  // server would refuse (size, type: data-upload-rules on the input or form, data-max-body on the
  // form) are named before anything is sent.
  function showPage(html) {
    // The page's scripts and styles carry the nonce of the answer's own policy; this document keeps its own.
    var own = document.querySelector("style[nonce], script[nonce]");
    var nonce = own ? own.nonce || own.getAttribute("nonce") : "";
    if (nonce) html = html.replace(/\snonce="[^"]*"/g, ' nonce="' + nonce + '"');
    document.open();
    document.write(html);
    document.close();
  }
  // Going to this very page with another #fragment only scrolls: load it again so its messages show.
  function goTo(url) {
    var target = new URL(url, window.location.href);
    var here = new URL(window.location.href);
    target.hash = here.hash = "";
    if (target.href === here.href) {
      window.location.replace(url);
      window.location.reload();
    } else {
      window.location.assign(url);
    }
  }
  function isPage(text) {
    return typeof text === "string" && /<html[\s>]/i.test(text) && text.indexOf("error-page") === -1;
  }
  document.addEventListener("submit", function (event) {
    var form = event.target;
    if (event.defaultPrevented || !form || form.tagName !== "FORM") return;
    if ((form.getAttribute("enctype") || "").toLowerCase() !== "multipart/form-data") return;
    if (form.getAttribute("data-upload-progress") === "off" || form.getAttribute("target")) return;
    if (!window.FormData || !window.XMLHttpRequest || (form.method || "").toLowerCase() !== "post") return;
    if (form.dataset.uploading === "1") { event.preventDefault(); return; }
    var files = [];
    var problems = [];
    var formRules = BW.uploadRules(form);
    form.querySelectorAll('input[type="file"]').forEach(function (input) {
      if (input.disabled) return;
      var rules = input.hasAttribute("data-upload-rules") ? BW.uploadRules(input) : formRules;
      Array.prototype.forEach.call(input.files || [], function (file) {
        files.push(file);
        var problem = BW.uploadProblem(file, rules);
        if (problem) problems.push({ name: file.name, reason: problem });
      });
    });
    if (!files.length) return;
    var total = files.reduce(function (sum, file) { return sum + file.size; }, 0);
    var limitMeta = document.querySelector('meta[name="bw-max-body"]');
    var maxBody = parseInt(form.getAttribute("data-max-body") || (limitMeta && limitMeta.getAttribute("content")) || "0", 10);
    if (maxBody && total > maxBody) {
      problems.push({ name: "", reason: BW.t("upload.total_too_large", { size: BW.formatBytes(total), limit: BW.formatBytes(maxBody) }) });
    }
    var host = form.querySelector("[data-upload-progress-host]");
    if (!host) {
      host = document.createElement("div");
      host.setAttribute("data-upload-progress-host", "");
      host.className = "upload-progress-host";
      form.appendChild(host);
    }
    event.preventDefault();
    if (problems.length) {
      form.dataset.submitting = "";
      var refused = BW.uploadRow(host, files.length === 1 ? files[0].name : BW.tn("upload.files", files.length));
      refused.fail(problems.map(function (problem) {
        return files.length > 1 && problem.name ? problem.name + ": " + problem.reason : problem.reason;
      }).join(" "));
      return;
    }
    var submitter = event.submitter;
    var data = new FormData(form);
    if (submitter && submitter.name && !data.has(submitter.name)) data.append(submitter.name, submitter.value);
    var buttons = Array.prototype.filter.call(
      form.querySelectorAll('button[type="submit"], button:not([type]), input[type="submit"]'),
      function (button) { return !button.disabled; });
    buttons.forEach(function (button) { button.disabled = true; });
    form.dataset.uploading = "1";
    var row = BW.uploadRow(host, files.length === 1 ? files[0].name : BW.tn("upload.files", files.length));
    BW.upload(form.getAttribute("action") || window.location.href, data, {
      accept: "text/html,application/xhtml+xml", onProgress: row.progress, onRetry: row.retrying, signal: row.signal
    }).then(function (answer) {
      row.done();
      if (answer && typeof answer === "object" && typeof answer.redirect === "string") {
        goTo(answer.redirect);
      } else if (isPage(answer)) {
        showPage(answer);
      } else {
        window.location.reload();
      }
    }, function (error) {
      form.dataset.uploading = "";
      form.dataset.submitting = "";
      buttons.forEach(function (button) { button.disabled = false; });
      if (!error.aborted && isPage(error.data)) { showPage(error.data); return; }
      row.fail(error.message, error.aborted);
    });
  });

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
      copyText(copy.getAttribute("data-copy"));
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

  // Sidebar (mobile drawer) and theme
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
    // screens; the wide-screen choice is remembered.
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
