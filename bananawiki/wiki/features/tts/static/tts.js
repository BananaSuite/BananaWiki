/* Read-aloud player (slot page.below_content).
 *
 * Reads its configuration from the <script type="application/json" data-tts-config>
 * block inside the panel, talks to the /page/<slug>/tts/* endpoints, polls while
 * audio is being generated, and highlights the word being read in the page text.
 * The page content is #page-content (or [data-page-content], or ".prose" inside <main>).
 */
(function () {
  "use strict";

  var POLL_MS = 3000;
  var MAX_POLL_MS = 15 * 60 * 1000;
  var SPEED_KEY = "bwTtsSpeed";
  var BLOCKS = "p, li, h1, h2, h3, h4, h5, h6, blockquote, td, th, dt, dd, figcaption";

  function t(key, vars) { return window.BW ? BW.t("tts." + key, vars) : key; }

  function storedSpeed() {
    try { return window.localStorage.getItem(SPEED_KEY); } catch (e) { return null; }
  }

  function storeSpeed(value) {
    try { window.localStorage.setItem(SPEED_KEY, value); } catch (e) { /* private mode */ }
  }

  function formatBytes(n) {
    if (!n) return "";
    if (n < 1024 * 1024) return Math.max(1, Math.round(n / 1024)) + " KB";
    return (n / (1024 * 1024)).toFixed(1) + " MB";
  }

  function formatTime(seconds) {
    if (!isFinite(seconds) || seconds <= 0) return "";
    var s = Math.floor(seconds % 60);
    return Math.floor(seconds / 60) + ":" + (s < 10 ? "0" : "") + s;
  }

  /* Word highlighting ------------------------------------------------------ */

  function contentRoot() {
    return document.getElementById("page-content") || document.querySelector("[data-page-content]") ||
      document.querySelector("main .prose");
  }

  function wrapWords(root) {
    if (root.hasAttribute("data-tts-content")) return root.__ttsWords || [];
    root.setAttribute("data-tts-content", "");
    var words = [];
    root.querySelectorAll(BLOCKS).forEach(function (block) {
      if (block.closest("pre, code, [data-tts-panel]") || block.querySelector(BLOCKS)) return;
      var walker = document.createTreeWalker(block, NodeFilter.SHOW_TEXT, null);
      var nodes = [];
      while (walker.nextNode()) {
        if (!walker.currentNode.parentNode.closest("pre, code")) nodes.push(walker.currentNode);
      }
      nodes.forEach(function (node) {
        if (!/\S/.test(node.nodeValue)) return;
        var fragment = document.createDocumentFragment();
        node.nodeValue.split(/(\s+)/).forEach(function (part) {
          if (!part) return;
          if (/^\s+$/.test(part)) { fragment.appendChild(document.createTextNode(part)); return; }
          var span = document.createElement("span");
          span.setAttribute("data-tts-word", "");
          span.textContent = part;
          fragment.appendChild(span);
          words.push({ el: span, weight: Math.max(1, part.length) });
        });
        node.parentNode.replaceChild(fragment, node);
      });
    });
    var total = 0;
    words.forEach(function (w) { w.end = (total += w.weight); });
    root.__ttsWords = words;
    root.__ttsTotal = total;
    return words;
  }

  function Highlighter() {
    this.root = contentRoot();
    this.current = null;
  }

  Highlighter.prototype.update = function (audio, follow) {
    if (!this.root || !isFinite(audio.duration) || audio.duration <= 0) return;
    var words = wrapWords(this.root);
    if (!words.length) return;
    var target = (audio.currentTime / audio.duration) * this.root.__ttsTotal;
    var lo = 0, hi = words.length - 1;
    while (lo < hi) {
      var mid = (lo + hi) >> 1;
      if (words[mid].end < target) lo = mid + 1; else hi = mid;
    }
    var word = words[lo].el;
    if (word === this.current) return;
    this.clear();
    word.classList.add("is-current");
    this.current = word;
    if (follow) {
      var rect = word.getBoundingClientRect();
      if (rect.top < 60 || rect.bottom > window.innerHeight - 20) {
        var smooth = document.documentElement.getAttribute("data-reduce-motion") !== "true";
        word.scrollIntoView({ block: "center", behavior: smooth ? "smooth" : "auto" });
      }
    }
  };

  Highlighter.prototype.clear = function () {
    if (this.current) this.current.classList.remove("is-current");
    this.current = null;
  };

  /* Panel ------------------------------------------------------------------ */

  function Panel(root) {
    var config = JSON.parse(root.querySelector("[data-tts-config]").textContent || "{}");
    this.root = root;
    this.urls = config.urls;
    this.speedDownload = !!config.speedDownload;
    this.status = root.querySelector("[data-tts-status]");
    this.generateBtn = root.querySelector("[data-tts-generate]");
    this.cancelBtn = root.querySelector("[data-tts-cancel]");
    this.player = root.querySelector("[data-tts-player]");
    this.audio = root.querySelector("[data-tts-audio]");
    this.speed = root.querySelector("[data-tts-speed]");
    this.follow = root.querySelector("[data-tts-follow]");
    this.download = root.querySelector("[data-tts-download]");
    this.meta = root.querySelector("[data-tts-meta]");
    this.highlighter = new Highlighter();
    this.pollUntil = 0;
    this.bind();
    if (this.render(config.state)) this.startPolling();
  }

  Panel.prototype.bind = function () {
    var self = this;
    var saved = storedSpeed();
    if (saved) {
      Array.prototype.forEach.call(this.speed.options, function (option) {
        if (option.value === saved) self.speed.value = saved;
      });
    }
    this.speed.addEventListener("change", function () {
      storeSpeed(self.speed.value);
      self.applySpeed();
    });
    this.generateBtn.addEventListener("click", function () { self.generate(); });
    this.cancelBtn.addEventListener("click", function () { self.cancel(); });
    this.audio.addEventListener("loadedmetadata", function () {
      self.applySpeed();
      self.showMeta();
    });
    var tick = function () {
      if (self.follow && !self.follow.checked) { self.highlighter.clear(); return; }
      self.highlighter.update(self.audio, true);
    };
    this.audio.addEventListener("timeupdate", tick);
    this.audio.addEventListener("seeked", tick);
    this.audio.addEventListener("ended", function () { self.highlighter.clear(); });
    this.follow.addEventListener("change", tick);
  };

  Panel.prototype.applySpeed = function () {
    var rate = parseFloat(this.speed.value) || 1;
    this.audio.playbackRate = rate;
    this.audio.defaultPlaybackRate = rate;
    var url = this.urls.download;
    if (this.speedDownload && Math.abs(rate - 1) > 0.001) url += "?speed=" + encodeURIComponent(this.speed.value);
    this.download.setAttribute("href", url);
  };

  Panel.prototype.setStatus = function (message, kind) {
    this.status.textContent = message || "";
    if (kind) this.status.setAttribute("data-kind", kind); else this.status.removeAttribute("data-kind");
  };

  Panel.prototype.showMeta = function () {
    var gen = this.generation || {};
    var parts = [gen.language_label, formatBytes(gen.file_size), formatTime(this.audio.duration)];
    this.meta.textContent = parts.filter(Boolean).join(" · ");
  };

  Panel.prototype.render = function (state) {
    var gen = state.generation;
    this.generation = gen;
    this.cancelBtn.hidden = !state.can_cancel;
    this.generateBtn.hidden = !state.can_generate;
    this.generateBtn.disabled = false;
    if (state.usable) {
      this.player.hidden = false;
      var src = this.urls.audio + "?v=" + encodeURIComponent(gen.id + "-" + (gen.completed_at || ""));
      if (this.audio.getAttribute("src") !== src) this.audio.setAttribute("src", src);
      this.setStatus(t("ready"), null);
      this.applySpeed();
      this.showMeta();
      return false;
    }
    this.player.hidden = true;
    this.audio.removeAttribute("src");
    this.highlighter.clear();
    if (state.in_flight) {
      this.setStatus(gen.status === "processing" ? t("processing", { language: gen.language_label }) : t("pending"),
                     "busy");
      return true;
    }
    if (gen && gen.status === "failed") {
      this.setStatus(t("failed"), "failed");
    } else if (state.can_generate) {
      this.setStatus(t("empty"), null);
    } else {
      this.setStatus(state.blocked_message || t("empty"), null);
    }
    return false;
  };

  Panel.prototype.poll = function () {
    var self = this;
    if (Date.now() > this.pollUntil) { this.setStatus(t("timeout"), "failed"); return; }
    BW.fetchJSON(this.urls.status).then(function (state) {
      if (self.render(state)) window.setTimeout(function () { self.poll(); }, POLL_MS);
    }).catch(function () {
      window.setTimeout(function () { self.poll(); }, POLL_MS * 3);
    });
  };

  Panel.prototype.startPolling = function () {
    this.pollUntil = Date.now() + MAX_POLL_MS;
    var self = this;
    window.setTimeout(function () { self.poll(); }, POLL_MS);
  };

  Panel.prototype.generate = function () {
    var self = this;
    this.generateBtn.disabled = true;
    this.setStatus(t("starting"), "busy");
    BW.fetchJSON(this.urls.generate, { method: "POST", body: { language: "auto" } }).then(function () {
      self.generateBtn.hidden = true;
      self.startPolling();
    }).catch(function (error) {
      self.generateBtn.disabled = false;
      var message = (error.data && error.data.message) || t("network_error");
      self.setStatus(message, error.status === 409 ? "busy" : "failed");
      if (error.status === 409) { self.generateBtn.hidden = true; self.startPolling(); }
    });
  };

  Panel.prototype.cancel = function () {
    var self = this;
    if (!window.confirm(t("remove_confirm"))) return;
    BW.fetchJSON(this.urls.cancel, { method: "POST", body: {} }).then(function () {
      return BW.fetchJSON(self.urls.status);
    }).then(function (state) { self.render(state); }).catch(function () {
      self.setStatus(t("network_error"), "failed");
    });
  };

  function init() {
    document.querySelectorAll("[data-tts-panel]").forEach(function (root) {
      if (root.__ttsPanel) return;
      root.__ttsPanel = new Panel(root);
    });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
}());
