/* Read-only preview of a canvas document embedded in the page as JSON
 * (history revisions): <div data-canvas-static data-source="json-script-id">. */
(function () {
  "use strict";

  BW.onReady(function () {
    document.querySelectorAll("[data-canvas-static]").forEach(function (stage) {
      // A script element: a heading of the page with that id must not stand in for the snapshot.
      var sourceId = stage.getAttribute("data-source");
      var source = sourceId && document.querySelector("script#" + CSS.escape(sourceId));
      var snapshot = {};
      try { snapshot = JSON.parse(source ? source.textContent : "{}"); } catch (e) { snapshot = {}; }
      // Like page embeds: the wheel scrolls the page, Ctrl/Cmd + wheel zooms the preview.
      var scene = new BW.Canvas.Scene(stage, { editable: false, wheelNeedsModifier: true });
      stage.tabIndex = 0;
      scene.load(snapshot.data || {}, snapshot);
      scene.fit();
      scene.trimHeight();
    });
  });
})();
