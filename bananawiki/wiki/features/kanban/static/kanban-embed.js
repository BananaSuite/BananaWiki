/* Fills [[kanban board="<id>"]] embeds in wiki pages with a read-only board
 * and keeps it current. The server applies the board's read check. */
(function () {
  "use strict";

  var BW = window.BW;
  var embeds = document.querySelectorAll(".bw-embed-kanban[data-embed-ref]");
  if (!embeds.length || !BW) return;

  var script = document.currentScript || document.querySelector("script[data-css][src*='kanban-embed']");
  if (script && script.dataset.css && !document.querySelector("link[data-kanban-css]")) {
    var css = document.createElement("link");
    css.rel = "stylesheet";
    css.href = script.dataset.css;
    css.setAttribute("data-kanban-css", "");
    document.head.appendChild(css);
  }

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function message(box, key) {
    box.textContent = "";
    box.appendChild(el("p", "bw-embed-kanban__message", BW.t(key)));
  }

  function draw(box, state) {
    box.textContent = "";
    var head = el("div", "bw-embed-kanban__head");
    head.appendChild(el("span", "badge", BW.t("kanban.embed_label")));
    var link = el("a", "", state.board.title);
    link.href = state.url;
    head.appendChild(link);
    box.appendChild(head);
    var body = el("div", "bw-embed-kanban__body");
    if (box.dataset.height) body.style.maxHeight = box.dataset.height;
    state.columns.forEach(function (column) {
      var section = el("section", "bw-embed-kanban__column");
      section.appendChild(el("h3", "", column.title + " (" + column.tickets.length + ")"));
      var list = el("ul");
      column.tickets.forEach(function (id) {
        var ticket = state.tickets[String(id)];
        if (!ticket) return;
        var item = el("li", "", ticket.title);
        if (ticket.color) {
          var bar = el("span", "kanban-card__color");
          bar.style.background = ticket.color;
          item.appendChild(bar);
        }
        if (ticket.priority !== "medium") {
          item.appendChild(document.createTextNode(" "));
          item.appendChild(el("span", "badge kanban-priority--" + ticket.priority, BW.t("kanban.priority." + ticket.priority)));
        }
        list.appendChild(item);
      });
      section.appendChild(list);
      body.appendChild(section);
    });
    if (!state.columns.length) body.appendChild(el("p", "bw-embed-kanban__message", BW.t("kanban.embed_empty")));
    box.appendChild(body);
  }

  function load(box) {
    var ref = encodeURIComponent(box.dataset.embedRef);
    return BW.fetchJSON("/api/embed/kanban/" + ref).then(function (state) {
      box.dataset.seq = state.seq;
      draw(box, state);
      return true;
    }).catch(function (error) {
      message(box, error.status === 404 || error.status === 403 || error.status === 401 ? "kanban.embed_unavailable" : "kanban.embed_failed");
      return false;
    });
  }

  function poll(box) {
    setTimeout(function () {
      if (document.hidden) { poll(box); return; }
      BW.fetchJSON("/api/embed/kanban/" + encodeURIComponent(box.dataset.embedRef) + "/sync?since=" + (box.dataset.seq || 0))
        .then(function (result) {
          if (result.reset || result.events.length) return load(box);
          box.dataset.seq = result.seq;
          return true;
        })
        .catch(function () { return true; })
        .then(function (keep) { if (keep !== false) poll(box); });
    }, 15000);
  }

  embeds.forEach(function (box) {
    message(box, "kanban.embed_loading");
    load(box).then(function (ok) { if (ok) poll(box); });
  });
})();
