/* Read-aloud player (slot page.below_content).
 *
 * Reads its configuration from the <script type="application/json" data-tts-config>
 * block inside the panel, talks to the /page/<slug>/tts/* endpoints, polls while
 * audio is being generated, and highlights the word being read in the page text.
 * The page content is #page-content (or [data-page-content], or ".prose" inside <main>).
 *
 * The audio reads the page title, then the text of the page (text.speech_text on the server):
 * code blocks, the contents list and embeds are not read, image descriptions are. Highlighting
 * follows the same order, placing each word by its length plus a pause after each sentence and
 * block, which is how long the voice takes over it; a long page is read up to state.cut_at
 * characters. The same model moves between paragraphs (buttons, J and L, the phone's lock-screen
 * controls) and starts reading at a word that is clicked. Following scrolls the page to the word
 * being read, but waits while the reader scrolls on their own. While the audio plays and the panel
 * is out of view, a small player stays at the bottom of the screen. Each page's audio continues
 * where the reader stopped (remembered in this browser only).
 */
(function () {
  "use strict";

  var POLL_MS = 3000;
  var BUSY_POLL_MS = 10000;  // while the queue is full: until there is room to ask again
  var SLOW_POLL_MS = 60000;  // after MAX_POLL_MS: the job goes on, the panel asks less often
  var WAITING = ["queue_full", "user_queue_full"];
  var MAX_POLL_MS = 15 * 60 * 1000;
  var SPEED_KEY = "bwTtsSpeed";
  var POSITIONS_KEY = "bwTtsPositions";
  var MAX_POSITIONS = 30;
  var MIN_RESUME_SECONDS = 5;  // closer to the start or the end, a page starts from the beginning
  var SAVE_EVERY_MS = 5000;
  var RESTART_PARAGRAPH_SECONDS = 2;  // "previous" first goes back to the start of the paragraph
  var SEEK_SECONDS = 10;  // the lock screen's skip buttons
  // Elements whose text the voice does not read (or that are not page text at all).
  var SKIP = "pre, script, style, template, noscript, button, select, textarea, svg, iframe, video, audio, " +
    "object, canvas, .toc, .bw-embed, [data-tts-panel], [aria-hidden='true'], [hidden]";
  // Elements after which the voice pauses (each ends like a sentence).
  var BLOCK = "p, li, h1, h2, h3, h4, h5, h6, blockquote, td, th, tr, dt, dd, figcaption, caption, summary, div, " +
    "section, article, details, figure, table, ul, ol, dl";
  // Clicking these keeps its usual meaning instead of starting to read there.
  var INTERACTIVE = "a, button, input, label, select, textarea, summary, [contenteditable], [role='button']";
  var SENTENCE_PAUSE = 4;  // in characters of speech
  var CLAUSE_PAUSE = 2;
  var USER_SCROLL_MS = 8000;  // following waits this long after the reader scrolled

  function t(key, vars) { return window.BW ? BW.t("tts." + key, vars) : key; }
  function tn(key, count, vars) { return window.BW && BW.tn ? BW.tn("tts." + key, count, vars) : key; }

  function load(key) {
    try { return window.localStorage.getItem(key); } catch (e) { return null; }
  }

  function save(key, value) {
    try { window.localStorage.setItem(key, value); } catch (e) { /* private mode, storage full */ }
  }

  function formatBytes(n) {
    if (!n) return "";
    if (n < 1024 * 1024) return Math.max(1, Math.round(n / 1024)) + " KB";
    return (n / (1024 * 1024)).toFixed(1) + " MB";
  }

  function clock(seconds) {
    seconds = isFinite(seconds) && seconds > 0 ? Math.floor(seconds) : 0;
    var s = seconds % 60;
    return Math.floor(seconds / 60) + ":" + (s < 10 ? "0" : "") + s;
  }

  function formatTime(seconds) {
    return isFinite(seconds) && seconds > 0 ? clock(seconds) : "";
  }

  function reducedMotion() {
    if (document.documentElement.getAttribute("data-reduce-motion") === "true") return true;
    return !!(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);
  }

  /* Positions: where the reader stopped in each page's audio, by audio address (which changes with
     new audio). The most recent MAX_POSITIONS pages are kept. */

  function positions() {
    try {
      var stored = JSON.parse(load(POSITIONS_KEY) || "{}");
      if (!stored || typeof stored !== "object" || Array.isArray(stored)) return {};
      // Only [seconds, saved at] pairs: anything else (an older format, a hand edit) is dropped.
      var clean = {};
      Object.keys(stored).forEach(function (key) {
        var entry = stored[key];
        if (Array.isArray(entry) && isFinite(entry[0]) && isFinite(entry[1])) clean[key] = entry;
      });
      return clean;
    } catch (e) { return {}; }
  }

  function savedPosition(src) {
    var entry = src && positions()[src];
    return entry && isFinite(entry[0]) ? Number(entry[0]) : 0;
  }

  function rememberPosition(src, seconds) {
    if (!src) return;
    var all = positions();
    if (seconds > 0) all[src] = [Math.floor(seconds), Date.now()];
    else delete all[src];
    var keys = Object.keys(all).sort(function (a, b) { return (all[b][1] || 0) - (all[a][1] || 0); });
    keys.slice(MAX_POSITIONS).forEach(function (key) { delete all[key]; });
    save(POSITIONS_KEY, JSON.stringify(all));
  }

  /* The words read aloud */

  function contentRoot() {
    return document.querySelector("article#page-content") || document.querySelector("[data-page-content]") ||
      document.querySelector("main .prose");
  }

  // The page title: the voice reads it first.
  function titleRoot(content) {
    var main = content && content.closest(".wiki-page__main, main");
    var title = main && main.querySelector(".wiki-page__title h1, .page-header h1");
    return title && !(content && content.contains(title)) ? title : null;
  }

  function pauseAfter(word) {
    var last = word.charAt(word.length - 1);
    if (".!?…。！？".indexOf(last) !== -1) return SENTENCE_PAUSE;
    if (",;:，；：、".indexOf(last) !== -1) return CLAUSE_PAUSE;
    return 0;
  }

  function closestBlock(node, root) {
    var el = node.nodeType === 1 ? node : node.parentNode;
    var block = el && el.closest(BLOCK);
    return block && root.contains(block) ? block : root;
  }

  /* Wraps every word the voice reads in <span data-tts-word> (images with a description take part
     as one word) once, in reading order. Each word gets the point where it starts and ends, and the
     number of its paragraph (a block: a paragraph, heading, list item, cell...). */
  function wrapWords(roots) {
    var words = [];
    var paragraph = -1;
    roots.forEach(function (root) {
      if (!root) return;
      root.setAttribute("data-tts-content", "");
      var walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT | NodeFilter.SHOW_ELEMENT, {
        acceptNode: function (node) {
          if (node.nodeType === 1) {
            if (node.matches(SKIP)) return NodeFilter.FILTER_REJECT;  // and everything inside
            return node.tagName === "IMG" ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_SKIP;
          }
          return /\S/.test(node.nodeValue) ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_SKIP;
        }
      });
      var nodes = [];
      while (walker.nextNode()) nodes.push(walker.currentNode);
      var lastBlock = null;
      nodes.forEach(function (node) {
        var block = closestBlock(node, root);
        if (block !== lastBlock) {
          if (words.length) {
            var previous = words[words.length - 1];
            previous.weight = Math.max(previous.weight, previous.length + SENTENCE_PAUSE);
          }
          paragraph += 1;
        }
        lastBlock = block;
        if (node.nodeType === 1) {
          var alt = (node.getAttribute("alt") || "").trim();
          if (!alt) return;
          node.setAttribute("data-tts-word", "");
          words.push({ el: node, length: alt.length, weight: alt.length + SENTENCE_PAUSE, paragraph: paragraph });
          return;
        }
        var fragment = document.createDocumentFragment();
        node.nodeValue.split(/(\s+)/).forEach(function (part) {
          if (!part) return;
          if (/^\s+$/.test(part)) { fragment.appendChild(document.createTextNode(part)); return; }
          var span = document.createElement("span");
          span.setAttribute("data-tts-word", "");
          span.textContent = part;
          fragment.appendChild(span);
          words.push({ el: span, length: part.length, weight: part.length + 1 + pauseAfter(part), paragraph: paragraph });
        });
        node.parentNode.replaceChild(fragment, node);
      });
      if (words.length) {
        var end = words[words.length - 1];
        end.weight = Math.max(end.weight, end.length + SENTENCE_PAUSE);
      }
    });
    var total = 0;
    words.forEach(function (w, index) { w.index = index; w.el.__ttsIndex = index; w.start = total; w.end = (total += w.weight); });
    return words;
  }

  /* The words of *roots* the audio reads: all of them, or those within the first *limit* characters
     (a long page). Wrapped once; *cache* keeps them. */
  function collectWords(roots, cache, limit) {
    if (!cache.all) cache.all = wrapWords(roots);
    limit = limit > 0 ? limit : 0;
    if (cache.words && cache.limit === limit) return cache.words;
    var count = cache.all.length;
    if (limit) {
      var chars = 0;
      for (count = 0; count < cache.all.length; count += 1) {
        chars += cache.all[count].length + 1;
        if (chars > limit) break;
      }
    }
    cache.words = cache.all.slice(0, count);
    cache.limit = limit;
    cache.total = count ? cache.words[count - 1].end : 0;
    cache.paragraphs = [];
    cache.words.forEach(function (w, index) {
      if (!index || w.paragraph !== cache.words[index - 1].paragraph) cache.paragraphs.push(index);
    });
    return cache.words;
  }

  function Highlighter() {
    this.root = contentRoot();
    this.title = titleRoot(this.root);
    this.current = null;
    this.cache = {};
    this.limit = 0;
    this.bottomInset = function () { return 0; };
    this.userScrolledAt = 0;
    this.autoScrollUntil = 0;
    var self = this;
    var reading = function () { self.userScrolledAt = Date.now(); };
    // The reader's own scrolling: the wheel, a finger, the keyboard, or any scroll (the scroll bar)
    // that is not the one following started.
    window.addEventListener("wheel", reading, { passive: true });
    window.addEventListener("touchmove", reading, { passive: true });
    window.addEventListener("scroll", function () {
      if (Date.now() > self.autoScrollUntil) reading();
    }, { passive: true });
    window.addEventListener("keydown", function (event) {
      if (/^(PageUp|PageDown|Home|End|ArrowUp|ArrowDown| |Spacebar)$/.test(event.key) &&
          !(event.target && event.target.closest && event.target.closest("input, textarea, select, audio, [contenteditable]"))) {
        reading();
      }
    });
  }

  Highlighter.prototype.roots = function () { return [this.title, this.root].filter(Boolean); };

  Highlighter.prototype.words = function () {
    return this.root ? collectWords([this.title, this.root], this.cache, this.limit) : [];
  };

  // The index of the word read at *fraction* (0 to 1) of the audio, or -1 when there are no words.
  Highlighter.prototype.indexAt = function (fraction) {
    var words = this.words();
    if (!words.length) return -1;
    var target = Math.max(0, Math.min(1, fraction)) * this.cache.total;
    var lo = 0, hi = words.length - 1;
    while (lo < hi) {
      var mid = (lo + hi) >> 1;
      if (words[mid].end <= target) lo = mid + 1; else hi = mid;
    }
    return lo;
  };

  // Where (0 to 1) the word at *index* starts in the audio.
  Highlighter.prototype.fractionOf = function (index) {
    var words = this.words();
    return words.length && this.cache.total ? words[index].start / this.cache.total : 0;
  };

  // The word element's index, or -1 when it is not one the audio reads.
  Highlighter.prototype.indexOf = function (el) {
    var words = this.words();
    var index = el && typeof el.__ttsIndex === "number" ? el.__ttsIndex : -1;
    return index >= 0 && index < words.length && words[index].el === el ? index : -1;
  };

  /* Where (0 to 1) to go for the previous (*direction* -1) or next (+1) paragraph from *fraction*, or
     null when there is none. "Previous" first returns to the start of the paragraph being read,
     unless that started less than RESTART_PARAGRAPH_SECONDS ago. */
  Highlighter.prototype.paragraphFrom = function (fraction, direction, duration) {
    var index = this.indexAt(fraction);
    if (index < 0) return null;
    var starts = this.cache.paragraphs;
    var current = 0;
    while (current + 1 < starts.length && starts[current + 1] <= index) current += 1;
    if (direction > 0) return current + 1 < starts.length ? this.fractionOf(starts[current + 1]) : null;
    var into = (fraction - this.fractionOf(starts[current])) * (duration || 0);
    if (into > RESTART_PARAGRAPH_SECONDS || current === 0) return this.fractionOf(starts[current]);
    return this.fractionOf(starts[current - 1]);
  };

  Highlighter.prototype.update = function (audio, follow) {
    if (!this.root || !isFinite(audio.duration) || audio.duration <= 0) return;
    var index = this.indexAt(audio.currentTime / audio.duration);
    if (index < 0) return;
    var word = this.words()[index].el;
    if (word === this.current) return;
    this.clear();
    word.classList.add("is-current");
    this.current = word;
    if (follow && Date.now() - this.userScrolledAt > USER_SCROLL_MS) {
      var rect = word.getBoundingClientRect();
      var bar = document.querySelector(".topbar");  // sticky: a word under it is out of sight
      var top = bar ? Math.max(0, bar.getBoundingClientRect().bottom) : 0;
      if (rect.top < top + 12 || rect.bottom > window.innerHeight - this.bottomInset() - 20) {
        var smooth = !reducedMotion();
        this.autoScrollUntil = Date.now() + (smooth ? 1500 : 200);
        word.scrollIntoView({ block: "center", behavior: smooth ? "smooth" : "auto" });
      }
    }
  };

  // Follow again at once (playback started, or the reader turned following back on).
  Highlighter.prototype.resume = function () {
    this.userScrolledAt = 0;
    this.clear();
  };

  Highlighter.prototype.clear = function () {
    if (this.current) this.current.classList.remove("is-current");
    this.current = null;
  };

  /* Panel */

  function Panel(root) {
    var config = JSON.parse(root.querySelector("[data-tts-config]").textContent || "{}");
    var all = function (selector) { return Array.prototype.slice.call(root.querySelectorAll(selector)); };
    this.root = root;
    this.urls = config.urls;
    this.title = config.title || "";
    this.speedDownload = !!config.speedDownload;
    this.status = root.querySelector("[data-tts-status]");
    this.generateBtn = root.querySelector("[data-tts-generate]");
    this.generateLabel = this.generateBtn.textContent;
    this.cancelBtn = root.querySelector("[data-tts-cancel]");
    this.player = root.querySelector("[data-tts-player]");
    this.audio = root.querySelector("[data-tts-audio]");
    this.speed = root.querySelector("[data-tts-speed]");
    this.follow = root.querySelector("[data-tts-follow]");
    this.download = root.querySelector("[data-tts-download]");
    this.meta = root.querySelector("[data-tts-meta]");
    this.resumeNote = root.querySelector("[data-tts-resume]");
    this.resumeText = root.querySelector("[data-tts-resume-text]");
    this.restartBtn = root.querySelector("[data-tts-restart]");
    this.cutNote = root.querySelector("[data-tts-cut]");
    this.mini = root.querySelector("[data-tts-mini]");
    this.toggleBtn = root.querySelector("[data-tts-toggle]");
    this.time = root.querySelector("[data-tts-time]");
    this.showBtn = root.querySelector("[data-tts-show]");
    this.previousBtns = all("[data-tts-previous]");
    this.nextBtns = all("[data-tts-next]");
    this.highlighter = new Highlighter();
    this.started = false;  // played in this page view: only then does the page follow the audio
    this.panelVisible = true;
    this.lastSaved = 0;
    this.pollUntil = 0;
    this.pollTimer = 0;
    var self = this;
    this.highlighter.bottomInset = function () { return self.mini && !self.mini.hidden ? self.mini.offsetHeight + 16 : 0; };
    this.bind();
    this.startPolling(this.render(config.state));
  }

  Panel.prototype.bind = function () {
    var self = this;
    var saved = load(SPEED_KEY);
    if (saved && this.speed.options) {
      Array.prototype.forEach.call(this.speed.options, function (option) {
        if (option.value === saved) self.speed.value = saved;
      });
    }
    this.speed.addEventListener("change", function () {
      save(SPEED_KEY, self.speed.value);
      self.applySpeed();
    });
    this.generateBtn.addEventListener("click", function () { self.generate(); });
    this.cancelBtn.addEventListener("click", function () { self.cancel(); });
    var tick = function () {
      self.showTime();
      if (!self.started) return;
      if (self.follow && !self.follow.checked) { self.highlighter.clear(); return; }
      self.highlighter.update(self.audio, true);
    };
    this.audio.addEventListener("loadedmetadata", function () {
      self.applySpeed();
      self.showMeta();
      self.restorePosition();
      self.setSkipping(true);
      self.markSeekable();
      self.showTime();
    });
    this.audio.addEventListener("timeupdate", function () {
      tick();
      if (Date.now() - self.lastSaved > SAVE_EVERY_MS) self.savePosition();
    });
    this.audio.addEventListener("seeked", tick);
    this.audio.addEventListener("play", function () {
      self.started = true;
      self.resumeNote.hidden = true;
      self.highlighter.resume();
      self.setupMediaSession();
      self.showPlaying();
      tick();
    });
    this.audio.addEventListener("pause", function () { self.savePosition(); self.showPlaying(); });
    this.audio.addEventListener("ended", function () {
      self.highlighter.clear();
      rememberPosition(self.audio.getAttribute("src"), 0);
      self.started = false;
      self.showPlaying();
    });
    this.follow.addEventListener("change", function () { self.highlighter.resume(); self.markSeekable(); tick(); });
    this.restartBtn.addEventListener("click", function () {
      rememberPosition(self.audio.getAttribute("src"), 0);
      self.resumeNote.hidden = true;
      if (isFinite(self.audio.duration)) self.audio.currentTime = 0;
      self.audio.focus();
    });
    this.previousBtns.forEach(function (button) { button.addEventListener("click", function () { self.paragraph(-1); }); });
    this.nextBtns.forEach(function (button) { button.addEventListener("click", function () { self.paragraph(1); }); });
    if (this.toggleBtn) this.toggleBtn.addEventListener("click", function () { self.toggle(); });
    if (this.showBtn) {
      this.showBtn.addEventListener("click", function () {
        self.root.scrollIntoView({ block: "center", behavior: reducedMotion() ? "auto" : "smooth" });
        self.audio.focus({ preventScroll: true });
      });
    }
    // J, K and L while the player (or the small player) has focus: WCAG 2.1.4 keeps one-key
    // shortcuts to the component that has focus.
    this.root.addEventListener("keydown", function (event) {
      if (self.player.hidden || event.defaultPrevented || event.altKey || event.ctrlKey || event.metaKey) return;
      var target = event.target;
      if (target && target.closest && target.closest("input, select, textarea, [contenteditable]")) return;
      var key = (event.key || "").toLowerCase();
      if (key === "k") self.toggle();
      else if (key === "j") self.paragraph(-1);
      else if (key === "l") self.paragraph(1);
      else return;
      event.preventDefault();
    });
    // Clicking a word in the page reads from there.
    this.highlighter.roots().forEach(function (root) {
      root.addEventListener("click", function (event) { self.readFrom(event); });
    });
    if (window.IntersectionObserver && this.mini) {
      new window.IntersectionObserver(function (entries) {
        self.panelVisible = entries[entries.length - 1].isIntersecting;
        self.showPlaying();
      }).observe(this.root);
    }
    window.addEventListener("pagehide", function () { self.savePosition(); });
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

  Panel.prototype.ready = function () {
    return !this.player.hidden && isFinite(this.audio.duration) && this.audio.duration > 0;
  };

  Panel.prototype.setSkipping = function (on) {
    this.previousBtns.concat(this.nextBtns).forEach(function (button) { button.disabled = !on; });
  };

  // Words can be clicked to read from there while the audio is ready and highlighting is on.
  Panel.prototype.markSeekable = function () {
    var on = this.ready() && (!this.follow || this.follow.checked);
    if (on && !this.highlighter.cache.all) {
      // Wrap the words once the page is idle, so the first click finds them.
      var highlighter = this.highlighter;
      var wrap = function () { highlighter.words(); };
      if (window.requestIdleCallback) window.requestIdleCallback(wrap, { timeout: 2000 }); else window.setTimeout(wrap, 200);
    }
    this.highlighter.roots().forEach(function (root) {
      if (on) root.classList.add("tts-seekable"); else root.classList.remove("tts-seekable");
    });
  };

  Panel.prototype.seek = function (fraction) {
    if (!this.ready() || fraction === null) return;
    this.audio.currentTime = Math.max(0, Math.min(1, fraction)) * this.audio.duration;
  };

  Panel.prototype.paragraph = function (direction) {
    if (!this.ready()) return;
    var target = this.highlighter.paragraphFrom(this.audio.currentTime / this.audio.duration, direction,
                                                this.audio.duration);
    if (target === null) return;
    // Moving between paragraphs is not listening yet: only pressing play starts following and read-from-here.
    this.highlighter.resume();
    this.seek(target);
  };

  Panel.prototype.readFrom = function (event) {
    // Only once the reader has started listening in this page view: before that, a tap on the text (to
    // scroll, to select, out of habit) must never start the audio by surprise.
    if (!this.ready() || !this.started || (this.follow && !this.follow.checked)) return;
    if (event.defaultPrevented || event.button || event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return;
    var target = event.target;
    var word = target && target.closest ? target.closest("[data-tts-word]") : null;
    if (!word || target.closest(INTERACTIVE)) return;
    var selection = window.getSelection ? window.getSelection() : null;
    if (selection && !selection.isCollapsed) return;  // selecting text, not asking to read
    var index = this.highlighter.indexOf(word);
    if (index < 0) return;
    this.started = true;
    this.highlighter.resume();
    this.highlighter.userScrolledAt = Date.now();  // the word is where the reader is looking
    this.seek(this.highlighter.fractionOf(index));
    var playing = this.audio.play();
    if (playing && playing.catch) playing.catch(function () { /* blocked autoplay: the player shows it */ });
  };

  Panel.prototype.toggle = function () {
    if (this.player.hidden) return;
    if (this.audio.paused) {
      var playing = this.audio.play();
      if (playing && playing.catch) playing.catch(function () {});
    } else {
      this.audio.pause();
    }
  };

  // The small player's state: shown while the audio has been started, is not over and the panel is
  // out of view; its button says what it does next.
  Panel.prototype.showPlaying = function () {
    if (!this.mini) return;
    this.mini.hidden = !(this.started && !this.player.hidden && !this.audio.ended && !this.panelVisible);
    var paused = this.audio.paused;
    var label = this.toggleBtn.getAttribute(paused ? "data-label-play" : "data-label-pause");
    if (label) { this.toggleBtn.setAttribute("aria-label", label); this.toggleBtn.setAttribute("title", label); }
    var glyphs = this.toggleBtn.querySelectorAll("[data-tts-glyph]");
    Array.prototype.forEach.call(glyphs, function (glyph) {
      glyph.hidden = glyph.getAttribute("data-tts-glyph") !== (paused ? "play" : "pause");
    });
  };

  Panel.prototype.showTime = function () {
    if (this.time && isFinite(this.audio.duration)) {
      this.time.textContent = clock(this.audio.currentTime) + " / " + clock(this.audio.duration);
    }
  };

  Panel.prototype.savePosition = function () {
    this.lastSaved = Date.now();
    var src = this.audio.getAttribute("src");
    if (!src || !this.started || !isFinite(this.audio.duration)) return;
    var at = this.audio.currentTime;
    rememberPosition(src, at >= MIN_RESUME_SECONDS && at < this.audio.duration - MIN_RESUME_SECONDS ? at : 0);
  };

  // Shown as soon as the audio is: "Continuing at 3:12. Start over".
  Panel.prototype.showResume = function () {
    var at = savedPosition(this.audio.getAttribute("src"));
    this.resumeNote.hidden = !(at >= MIN_RESUME_SECONDS) || this.started;
    if (!this.resumeNote.hidden) this.resumeText.textContent = t("resume", { time: clock(at) });
  };

  Panel.prototype.restorePosition = function () {
    if (this.started) return;
    var at = savedPosition(this.audio.getAttribute("src"));
    if (at >= MIN_RESUME_SECONDS && at < this.audio.duration - MIN_RESUME_SECONDS) {
      this.audio.currentTime = at;
    } else {
      this.resumeNote.hidden = true;
    }
  };

  // The phone's lock screen and headphone buttons: title, play, pause, skip and paragraphs.
  Panel.prototype.setupMediaSession = function () {
    if (this.mediaSession || typeof navigator === "undefined" || !navigator.mediaSession ||
        typeof window.MediaMetadata !== "function") return;
    this.mediaSession = true;
    var self = this;
    var brand = document.querySelector(".topbar__brand");
    try {
      navigator.mediaSession.metadata = new window.MediaMetadata({
        title: this.title || document.title, artist: brand ? brand.textContent.trim() : ""
      });
    } catch (e) { /* not supported here */ }
    var skip = function (seconds) {
      return function (details) {
        var step = details && details.seekOffset ? details.seekOffset : seconds;
        self.audio.currentTime = Math.max(0, Math.min(self.audio.duration || 0, self.audio.currentTime + step));
      };
    };
    [["play", function () { self.audio.play(); }], ["pause", function () { self.audio.pause(); }],
     ["previoustrack", function () { self.paragraph(-1); }], ["nexttrack", function () { self.paragraph(1); }],
     ["seekbackward", skip(-SEEK_SECONDS)], ["seekforward", skip(SEEK_SECONDS)],
     ["seekto", function (details) { if (details && isFinite(details.seekTime)) self.audio.currentTime = details.seekTime; }]
    ].forEach(function (pair) {
      try { navigator.mediaSession.setActionHandler(pair[0], pair[1]); } catch (e) { /* an action it does not know */ }
    });
  };

  /* Shows *state* (the status endpoint's answer); returns how long to wait before asking again
     (0: nothing will change on its own). */
  Panel.prototype.render = function (state) {
    var gen = state.generation;
    this.generation = gen;
    this.cancelBtn.hidden = !state.can_cancel;
    this.generateBtn.hidden = !state.can_generate;
    this.generateBtn.disabled = false;
    var retry = !!(gen && gen.status === "failed" && state.can_generate);
    this.generateBtn.textContent = retry ? (this.generateBtn.getAttribute("data-label-retry") || this.generateLabel)
      : this.generateLabel;
    if (state.usable) {
      this.player.hidden = false;
      var src = this.urls.audio + "?v=" + encodeURIComponent(gen.id + "-" + (gen.completed_at || ""));
      if (this.audio.getAttribute("src") !== src) {
        // Only the audio of a page someone was listening to loads its length at once (to offer to continue);
        // every other page view fetches nothing until play is pressed.
        this.audio.setAttribute("preload", savedPosition(src) > 0 ? "metadata" : "none");
        this.audio.setAttribute("src", src);
      }
      this.highlighter.limit = state.cut_at || 0;
      this.cutNote.hidden = !state.cut_at;
      this.setStatus(t("ready"), null);
      this.applySpeed();
      this.showMeta();
      this.showResume();
      this.markSeekable();
      this.showPlaying();
      return 0;
    }
    this.player.hidden = true;
    if (this.audio.getAttribute("src")) {
      // Removing the source alone would let the audio play on, hidden and out of reach.
      this.audio.pause();
      this.audio.removeAttribute("src");
      this.audio.load();
    }
    this.started = false;
    this.highlighter.clear();
    this.setSkipping(false);
    this.markSeekable();
    this.showPlaying();
    if (state.in_flight) {
      var message;
      if (gen.status === "processing") message = t("processing", { language: gen.language_label });
      else if (!state.worker_running) message = t("no_worker");
      else if (state.queue_ahead > 0) message = tn("queued_ahead", state.queue_ahead);
      else message = t("pending");
      this.setStatus(message, "busy");
      return POLL_MS;
    }
    if (retry) {
      this.setStatus(t("failed_retry"), "failed");
    } else if (gen && gen.status === "failed") {
      this.setStatus(t("failed"), "failed");
    } else if (state.can_generate) {
      this.setStatus(t("empty"), null);
    } else {
      this.setStatus(state.blocked_message || t("empty"), null);
      // A full queue empties by itself: the button comes back when there is room.
      if (WAITING.indexOf(state.blocked_reason) !== -1) return BUSY_POLL_MS;
    }
    return 0;
  };

  Panel.prototype.schedule = function (delay) {
    var self = this;
    window.clearTimeout(this.pollTimer);
    this.pollTimer = delay ? window.setTimeout(function () { self.poll(); }, delay) : 0;
  };

  Panel.prototype.poll = function () {
    var self = this;
    BW.fetchJSON(this.urls.status).then(function (state) {
      var delay = self.render(state);
      if (delay && Date.now() > self.pollUntil) {
        // Long pages on a slow server take a while: say so, and keep asking now and then.
        self.setStatus(t("timeout"), "busy");
        delay = SLOW_POLL_MS;
      }
      self.schedule(delay);
    }).catch(function () {
      self.schedule(POLL_MS * 3);
    });
  };

  /* Asks for the state again after *delay* ms (default: the usual interval); after MAX_POLL_MS it
     asks once a minute. */
  Panel.prototype.startPolling = function (delay) {
    if (delay === 0) { this.schedule(0); return; }
    this.pollUntil = Date.now() + MAX_POLL_MS;
    this.schedule(delay || POLL_MS);
  };

  Panel.prototype.generate = function () {
    var self = this;
    this.generateBtn.disabled = true;
    this.setStatus(t("starting"), "busy");
    BW.fetchJSON(this.urls.generate, { method: "POST", body: { language: "auto" } }).then(function () {
      self.generateBtn.hidden = true;
      self.status.focus();  // the button is gone: keep the keyboard where the news comes
      self.startPolling();
    }).catch(function (error) {
      self.generateBtn.disabled = false;
      // Refusals carry "message"; the rate limit only "error"; no answer at all is a network problem.
      var data = error.data && typeof error.data === "object" ? error.data : {};
      var message = data.message || data.error || t("network_error");
      self.setStatus(message, error.status === 409 ? "busy" : "failed");
      if (error.status === 409) { self.generateBtn.hidden = true; self.status.focus(); self.startPolling(); }
    });
  };

  Panel.prototype.cancel = function () {
    var self = this;
    if (!window.confirm(t("remove_confirm"))) return;
    var src = this.audio.getAttribute("src");
    BW.fetchJSON(this.urls.cancel, { method: "POST", body: {} }).then(function () {
      rememberPosition(src, 0);
      return BW.fetchJSON(self.urls.status);
    }).then(function (state) {
      self.startPolling(self.render(state));
      if (!self.generateBtn.hidden) self.generateBtn.focus(); else self.status.focus();
    }).catch(function () {
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
