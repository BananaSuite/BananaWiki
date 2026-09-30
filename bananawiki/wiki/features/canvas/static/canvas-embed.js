/* Canvases embedded in wiki pages with [[canvas slug="…"]].
 *
 * The Markdown renderer leaves <div class="bw-embed bw-embed-canvas"
 * data-embed-ref="slug">; this script loads the canvas through
 * /api/embed/canvas/<slug> (which applies the reader's permissions), draws it
 * read-only and reloads it when the canvas changes. */
(function () {
  "use strict";

  var POLL_MS = 8000;

  function session() {
    try { if (window.crypto && crypto.randomUUID) return crypto.randomUUID(); } catch (e) { /* fall through */ }
    return "e" + Math.random().toString(36).slice(2);
  }

  function mount(container, sessionId) {
    var slug = container.getAttribute("data-embed-ref");
    if (!slug || container.dataset.canvasMounted) return;
    container.dataset.canvasMounted = "1";
    var base = "/api/embed/canvas/" + encodeURIComponent(slug);
    var el = BW.Canvas.el;

    var header = el("div", "bw-embed__header");
    header.appendChild(el("span", "badge", BW.t("node.canvas")));
    var title = el("span", "bw-embed__title", BW.t("canvas.loading"));
    header.appendChild(title);
    var controls = el("span", "bw-embed__controls");
    header.appendChild(controls);
    var stage = el("div", "canvas-frame canvas-frame--embed");
    stage.tabIndex = 0;
    var height = parseInt(container.getAttribute("data-height"), 10);
    if (height >= 120 && height <= 2000) stage.style.height = height + "px";
    var width = parseInt(container.getAttribute("data-width"), 10);
    if (width >= 120 && width <= 4000) container.style.maxWidth = width + "px";
    container.appendChild(header);
    container.appendChild(stage);

    var scene = new BW.Canvas.Scene(stage, { editable: false, wheelNeedsModifier: true });
    stage.setAttribute("aria-label", BW.t("canvas.embed_label"));
    [["−", 1 / 1.2, "canvas.zoom_out"], ["+", 1.2, "canvas.zoom_in"], ["⤢", 0, "canvas.fit"]].forEach(function (spec) {
      var button = el("button", "btn btn--ghost btn--small", spec[0]);
      button.type = "button";
      button.setAttribute("aria-label", BW.t(spec[2]));
      button.addEventListener("click", function () { if (spec[1]) scene.zoomBy(spec[1]); else scene.fit(); });
      controls.appendChild(button);
    });

    var seq = 0;
    var version = null;
    function load() {
      return BW.fetchJSON(base).then(function (data) {
        title.textContent = "";
        var link = el("a", null, data.title);
        link.href = data.url;
        title.appendChild(link);
        seq = data.seq || 0;
        version = data.version;
        scene.load(data.data, data);
        scene.fit();
      }).catch(function (error) {
        title.textContent = error.status === 404 || error.status === 403 || error.status === 401
          ? BW.t("canvas.embed_unavailable") : BW.t("canvas.embed_failed");
        stage.remove();
        return Promise.reject(error);
      });
    }

    function poll() {
      if (document.hidden) return;
      BW.fetchJSON(base + "/sync?since=" + seq, { headers: { "X-Canvas-Session": sessionId } }).then(function (res) {
        seq = res.seq || seq;
        if (res.reset || (res.events && res.events.length) || (version !== null && res.version !== version)) {
          var viewport = { x: scene.viewport.x, y: scene.viewport.y, zoom: scene.viewport.zoom };
          load().then(function () { scene.viewport = viewport; scene.applyViewport(); });
        }
      }).catch(function () { /* try again at the next poll */ });
    }

    load().then(function () { window.setInterval(poll, POLL_MS); }, function () { /* message shown */ });
  }

  BW.onReady(function () {
    var embeds = document.querySelectorAll(".bw-embed-canvas[data-embed-ref]");
    if (!embeds.length || !BW.Canvas) return;
    var sessionId = session();
    embeds.forEach(function (container) { mount(container, sessionId); });
  });
})();
