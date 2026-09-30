/* Read-only preview of a canvas document embedded in the page as JSON
 * (history revisions): <div data-canvas-static data-source="json-script-id">. */
(function () {
  "use strict";

  BW.onReady(function () {
    document.querySelectorAll("[data-canvas-static]").forEach(function (stage) {
      var source = document.getElementById(stage.getAttribute("data-source"));
      var snapshot = {};
      try { snapshot = JSON.parse(source ? source.textContent : "{}"); } catch (e) { snapshot = {}; }
      var scene = new BW.Canvas.Scene(stage, { editable: false });
      stage.tabIndex = 0;
      scene.load(snapshot.data || {}, snapshot);
      scene.fit();
    });
  });
})();
