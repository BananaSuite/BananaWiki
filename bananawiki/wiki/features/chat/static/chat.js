/* Chat pages: incremental updates, "load older", sending without reloads.
 *
 * The page renders the latest messages on the server. This script then asks
 * only for messages newer than the newest one it has (JSON), renders them
 * with textContent, pages backwards on demand, and polls less often when the
 * conversation is idle and much less while the tab is hidden. Without it the
 * forms still work (full page loads).
 */
(function () {
  "use strict";

  var ACTIVE_DELAY = 3000;
  var IDLE_MAX_DELAY = 15000;
  var HIDDEN_DELAY = 60000;
  var ERROR_MAX_DELAY = 120000;

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  }

  // User name suggestions for "new conversation" and "add member" -----------
  function setupSuggestions() {
    document.querySelectorAll("input[data-user-suggest]").forEach(function (input) {
      var list = document.getElementById(input.getAttribute("list"));
      var timer = null;
      var last = "";
      if (!list) return;
      input.addEventListener("input", function () {
        var query = input.value.trim();
        clearTimeout(timer);
        if (query.length < 2 || query === last) return;
        timer = setTimeout(function () {
          last = query;
          BW.fetchJSON(input.getAttribute("data-user-suggest") + "?q=" + encodeURIComponent(query))
            .then(function (data) {
              list.replaceChildren();
              (data.users || []).forEach(function (name) {
                var option = document.createElement("option");
                option.value = name;
                list.appendChild(option);
              });
            })
            .catch(function () { /* suggestions are optional */ });
        }, 250);
      });
    });
  }

  // Timeout countdown ------------------------------------------------------
  function setupCountdown() {
    var node = document.querySelector("[data-chat-countdown]");
    if (!node) return;
    var until = new Date(node.getAttribute("data-chat-countdown").replace(" ", "T") + "Z").getTime();
    function tick() {
      var left = Math.max(0, Math.round((until - Date.now()) / 1000));
      if (left <= 0) { window.location.reload(); return; }
      var h = Math.floor(left / 3600), m = Math.floor((left % 3600) / 60), s = left % 60;
      node.textContent = "(" + (h ? h + "h " : "") + (h || m ? m + "m " : "") + s + "s)";
      setTimeout(tick, 1000);
    }
    tick();
  }

  // The conversation -------------------------------------------------------
  function Conversation(data) {
    this.data = data;
    this.log = document.getElementById("chat-log");
    this.empty = document.getElementById("chat-empty");
    this.jump = document.getElementById("chat-jump");
    this.latestId = data.latestId || 0;
    this.oldestId = data.oldestId || 0;
    this.cursor = data.cursor || "";
    this.delay = ACTIVE_DELAY;
    this.timer = null;
    this.busy = false;
    this.unseen = false;
  }

  Conversation.prototype.nearBottom = function () {
    var log = this.log;
    return log.scrollHeight - log.scrollTop - log.clientHeight < 80;
  };

  Conversation.prototype.scrollToBottom = function () {
    this.log.scrollTop = this.log.scrollHeight;
    this.jump.hidden = true;
  };

  Conversation.prototype.render = function (m) {
    var item = el("li", "chat-msg");
    item.setAttribute("data-id", String(m.id));
    if (m.mine) item.classList.add("chat-msg--mine");
    if (m.system) {
      item.classList.add("chat-msg--system");
      var line = el("p", "chat-msg__system", m.text + " ");
      line.appendChild(el("time", "chat-msg__time", m.time));
      item.appendChild(line);
      return item;
    }
    var meta = el("div", "chat-msg__meta");
    meta.appendChild(el("strong", null, m.sender || BW.t("chat.unknown_user")));
    meta.appendChild(document.createTextNode(" "));
    meta.appendChild(el("time", "chat-msg__time", m.time));
    item.appendChild(meta);
    if (m.deleted) {
      this.markDeleted(item);
      return item;
    }
    if (m.text) item.appendChild(el("p", "chat-msg__body", m.text));
    if (m.attachments && m.attachments.length) {
      var files = el("ul", "chat-msg__files");
      m.attachments.forEach(function (a) {
        var entry = el("li");
        var link = el("a", null, a.name);
        link.href = a.url;
        entry.appendChild(link);
        entry.appendChild(document.createTextNode(" "));
        entry.appendChild(el("span", "small muted", a.size));
        files.appendChild(entry);
      });
      item.appendChild(files);
    }
    if (m.can_delete && this.data.deleteUrl) item.appendChild(this.deleteForm(m.id));
    return item;
  };

  Conversation.prototype.deleteForm = function (id) {
    var form = el("form", "chat-msg__delete");
    form.method = "post";
    form.action = this.data.deleteUrl;
    form.setAttribute("data-confirm", BW.t("chat.delete_confirm"));
    [["csrf_token", BW.csrf()], ["message_id", String(id)]].forEach(function (pair) {
      var input = el("input");
      input.type = "hidden";
      input.name = pair[0];
      input.value = pair[1];
      form.appendChild(input);
    });
    var button = el("button", "btn btn--ghost btn--small", BW.t("chat.delete"));
    button.type = "submit";
    form.appendChild(button);
    return form;
  };

  Conversation.prototype.markDeleted = function (item) {
    item.classList.add("chat-msg--deleted");
    item.querySelectorAll(".chat-msg__body, .chat-msg__files, .chat-msg__delete").forEach(function (n) { n.remove(); });
    item.appendChild(el("p", "chat-msg__body chat-msg__body--deleted", BW.t("chat.deleted")));
  };

  Conversation.prototype.find = function (id) {
    return this.log.querySelector('li[data-id="' + Number(id) + '"]');
  };

  Conversation.prototype.append = function (messages) {
    var stick = this.nearBottom();
    var self = this;
    var fromOthers = false;
    messages.forEach(function (m) {
      if (m.id <= self.latestId || self.find(m.id)) return;
      self.log.appendChild(self.render(m));
      self.latestId = m.id;
      if (!self.oldestId) self.oldestId = m.id;
      if (!m.mine) fromOthers = true;
    });
    this.empty.hidden = this.log.children.length > 0;
    if (stick) this.scrollToBottom();
    else if (fromOthers) this.jump.hidden = false;
    return fromOthers;
  };

  Conversation.prototype.applyRemovals = function (payload) {
    var self = this;
    (payload.deleted || []).forEach(function (id) {
      var item = self.find(id);
      if (item && !item.classList.contains("chat-msg--deleted")) self.markDeleted(item);
    });
    // Everything older than the oldest remaining message was cleared.
    var first = payload.first_id;
    Array.prototype.slice.call(this.log.children).forEach(function (item) {
      var id = Number(item.getAttribute("data-id"));
      if (first === null || first === undefined || id < first) item.remove();
    });
    if (first === null || first === undefined) {
      this.oldestId = 0;
      var older = document.querySelector("[data-chat-older]");
      if (older) older.parentNode.remove();
    }
    this.empty.hidden = this.log.children.length > 0;
  };

  Conversation.prototype.markRead = function () {
    if (!this.data.markRead || document.hidden) { this.unseen = true; return; }
    this.unseen = false;
    BW.fetchJSON(this.data.readUrl, { method: "POST" }).catch(function () { /* retried on next message */ });
  };

  Conversation.prototype.schedule = function () {
    var self = this;
    clearTimeout(this.timer);
    var wait = document.hidden ? Math.max(this.delay, HIDDEN_DELAY) : this.delay;
    this.timer = setTimeout(function () { self.poll(); }, wait);
  };

  Conversation.prototype.poll = function () {
    var self = this;
    if (this.busy) return;
    this.busy = true;
    var url = this.data.pollUrl + "?after=" + this.latestId + "&since=" + encodeURIComponent(this.cursor);
    BW.fetchJSON(url).then(function (payload) {
      self.cursor = payload.cursor || self.cursor;
      self.applyRemovals(payload);
      var news = self.append(payload.messages || []);
      if (news) self.markRead();
      if (payload.more) self.delay = 0;
      else if ((payload.messages || []).length) self.delay = ACTIVE_DELAY;
      else self.delay = Math.min(IDLE_MAX_DELAY, Math.round(self.delay * 1.5));
    }).catch(function () {
      self.delay = Math.min(ERROR_MAX_DELAY, Math.max(ACTIVE_DELAY, self.delay * 2));
    }).then(function () {
      self.busy = false;
      self.schedule();
    });
  };

  Conversation.prototype.loadOlder = function (link) {
    var self = this;
    if (!this.oldestId) return;
    link.setAttribute("aria-busy", "true");
    BW.fetchJSON(this.data.pollUrl + "?before=" + this.oldestId).then(function (payload) {
      var messages = payload.messages || [];
      var height = self.log.scrollHeight;
      var anchor = self.log.firstChild;
      messages.forEach(function (m) {
        if (!self.find(m.id)) self.log.insertBefore(self.render(m), anchor);
      });
      if (messages.length) self.oldestId = messages[0].id;
      self.log.scrollTop += self.log.scrollHeight - height;
      if (!payload.has_more) link.parentNode.remove();
      else link.href = link.pathname + "?before=" + self.oldestId;
      self.empty.hidden = self.log.children.length > 0;
    }).catch(function (error) {
      BW.toast(error.message || BW.t("error"), "error");
    }).then(function () { link.removeAttribute("aria-busy"); });
  };

  Conversation.prototype.bindComposer = function () {
    var self = this;
    var form = document.getElementById("chat-composer");
    if (!form) return;
    var input = form.querySelector("textarea");
    var file = form.querySelector('input[type="file"]');
    var label = form.querySelector("[data-chat-file-label]");
    var labelText = label ? label.textContent : "";
    if (file && label) {
      file.addEventListener("change", function () {
        label.textContent = file.files.length ? file.files[0].name : labelText;
      });
    }
    input.addEventListener("keydown", function (event) {
      if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
        event.preventDefault();
        if (form.requestSubmit) form.requestSubmit(); else form.submit();
      }
    });
    form.addEventListener("submit", function (event) {
      if (event.defaultPrevented) return;
      event.preventDefault();
      var button = form.querySelector('button[type="submit"]');
      button.disabled = true;
      BW.fetchJSON(form.action, { method: "POST", body: new FormData(form) }).then(function () {
        input.value = "";
        if (file) { file.value = ""; if (label) label.textContent = labelText; }
        self.delay = ACTIVE_DELAY;
        self.scrollToBottom();
        self.poll();
      }).catch(function (error) {
        BW.toast(error.message || BW.t("error"), "error");
      }).then(function () {
        button.disabled = false;
        input.focus();
      });
    });
  };

  Conversation.prototype.bind = function () {
    var self = this;
    this.log.addEventListener("submit", function (event) {
      var form = event.target;
      if (!form.classList.contains("chat-msg__delete") || event.defaultPrevented) return;
      event.preventDefault();
      BW.fetchJSON(form.action, { method: "POST", body: new FormData(form) }).then(function () {
        var item = form.closest("li");
        if (item) self.markDeleted(item);
      }).catch(function (error) { BW.toast(error.message || BW.t("error"), "error"); });
    });
    var older = document.querySelector("[data-chat-older]");
    if (older) {
      older.addEventListener("click", function (event) {
        event.preventDefault();
        self.loadOlder(older);
      });
    }
    this.jump.addEventListener("click", function () { self.scrollToBottom(); });
    this.log.addEventListener("scroll", function () { if (self.nearBottom()) self.jump.hidden = true; });
    document.addEventListener("visibilitychange", function () {
      if (document.hidden) return;
      if (self.unseen) self.markRead();
      self.delay = ACTIVE_DELAY;
      if (!self.busy) self.poll();
    });
    this.bindComposer();
    this.scrollToBottom();
    this.schedule();
  };

  BW.onReady(function () {
    setupSuggestions();
    setupCountdown();
    var node = document.querySelector("script#chat-data");
    if (!node || !document.getElementById("chat-log")) return;
    var data;
    try { data = JSON.parse(node.textContent || "{}"); } catch (e) { return; }
    new Conversation(data).bind();
  });
})();
