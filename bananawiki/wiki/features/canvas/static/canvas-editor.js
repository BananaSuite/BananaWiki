/* Canvas editor and viewer (the /canvas/<slug> page).
 *
 * Saving: the editor keeps the last state the server confirmed ("synced")
 * and, a moment after each change, sends the difference as operations to
 * /ops. Other sessions' operations arrive through /sync and are merged node
 * by node; a node changed locally and remotely keeps the local version (the
 * next save wins). A "snapshot" event (history restore, whole-document save)
 * reloads the canvas.
 *
 * Input: mouse, touch and pen through pointer events; every action is also
 * reachable from the toolbar and the keyboard (see the "Keyboard" help on the
 * page). User text is only ever inserted with textContent.
 *
 * Arranging: while dragging, guide lines show where the edges or centres of
 * the moved elements line up with nearby elements, and the move snaps to
 * them; otherwise moves and resizes snap to the background grid while "Snap"
 * is on (hold Alt to place freely). Nodes sharing a "group" id are selected
 * and moved together; "locked" nodes are not moved, resized, aligned, edited
 * or deleted until they are unlocked. The server enforces locks as well: a
 * lock change is saved at once, in its own request, so a later edit never
 * reaches the server before the unlock does, and a change the server refuses
 * (someone else locked the node meanwhile) reloads the canvas.
 */
(function () {
  "use strict";

  var SAVE_DELAY_MS = 700;
  var SYNC_MS = 3000;
  var MAX_UNDO = 100;
  var PASTE_OFFSET = 24;
  var CLIPBOARD_MARK = "bananawiki-canvas";
  var MAX_OPS = 500;
  var GRID = 24;
  var GUIDE_PX = 6;  /* how close, in screen pixels, an edge must come to a guide line to snap to it */
  var Canvas = BW.Canvas;

  function readSetting(key, fallback) {
    try {
      var value = localStorage.getItem(key);
      return value === null ? fallback : value === "1";
    } catch (e) { return fallback; }
  }

  function writeSetting(key, on) {
    try { localStorage.setItem(key, on ? "1" : "0"); } catch (e) { /* storage off */ }
  }

  function snapValue(value) { return Math.round(value / GRID) * GRID; }

  function randomId(prefix) {
    var bytes = new Uint8Array(8);
    (window.crypto || window.msCrypto).getRandomValues(bytes);
    return prefix + "_" + Array.prototype.map.call(bytes, function (b) { return b.toString(16).padStart(2, "0"); }).join("");
  }

  function clone(value) { return JSON.parse(JSON.stringify(value)); }

  function isTyping(target) {
    return target && (target.closest("input, textarea, select, [contenteditable='true']") || target.closest("dialog"));
  }

  /* The first index of *list* (sorted by .v) whose value is at least *value*. */
  function lowerBound(list, value) {
    var low = 0, high = list.length;
    while (low < high) {
      var mid = (low + high) >> 1;
      if (list[mid].v < value) low = mid + 1; else high = mid;
    }
    return low;
  }

  /* The ids of the locked nodes of a snapshot, to notice lock changes. */
  function lockedIds(nodes) {
    return nodes.filter(function (node) { return node.locked; }).map(function (node) { return node.id; }).sort().join(",");
  }

  /* ── Alignment guides ───────────────────────────────────────────────────── */

  /* Edge and centre positions of the nodes around the view (not *skip*), collected once when a drag
   * starts and sorted, so each pointer move only needs two binary searches however large the canvas. */
  function Guides(scene, skip) {
    var rect = scene.stage.getBoundingClientRect();
    var v = scene.viewport;
    var margin = Math.max(rect.width, rect.height) / v.zoom / 2;
    var view = { minX: -v.x / v.zoom - margin, minY: -v.y / v.zoom - margin,
                 maxX: (rect.width - v.x) / v.zoom + margin, maxY: (rect.height - v.y) / v.zoom + margin };
    var xs = this.xs = [], ys = this.ys = [];
    scene.nodes.forEach(function (node) {
      if (skip[node.id]) return;
      var size = Canvas.sizeOf(node);
      if (node.x > view.maxX || node.y > view.maxY || node.x + size.w < view.minX || node.y + size.h < view.minY) return;
      [node.x, node.x + size.w / 2, node.x + size.w].forEach(function (value) {
        xs.push({ v: value, from: node.y, to: node.y + size.h });
      });
      [node.y, node.y + size.h / 2, node.y + size.h].forEach(function (value) {
        ys.push({ v: value, from: node.x, to: node.x + size.w });
      });
    });
    var byValue = function (a, b) { return a.v - b.v; };
    xs.sort(byValue);
    ys.sort(byValue);
    this.reach = GUIDE_PX / v.zoom;
    this.world = scene.world;
    this.lines = {};
  }

  /* The closest guide to any of *values* within reach, as {delta, guide}, or null. */
  Guides.prototype.nearest = function (list, values) {
    var best = null;
    values.forEach(function (value) {
      var index = lowerBound(list, value);
      [list[index - 1], list[index]].forEach(function (guide) {
        if (!guide) return;
        var delta = guide.v - value;
        if (Math.abs(delta) <= this.reach && (!best || Math.abs(delta) < Math.abs(best.delta))) {
          best = { delta: delta, guide: guide };
        }
      }, this);
    }, this);
    return best;
  };

  /* Draw the matched vertical (*x*) and horizontal (*y*) guides across *box*, the moved elements. */
  Guides.prototype.show = function (x, y, box) {
    this.line("v", x && { left: x.guide.v, top: Math.min(x.guide.from, box.minY),
                          height: Math.max(x.guide.to, box.maxY) - Math.min(x.guide.from, box.minY) });
    this.line("h", y && { left: Math.min(y.guide.from, box.minX), top: y.guide.v,
                          width: Math.max(y.guide.to, box.maxX) - Math.min(y.guide.from, box.minX) });
  };

  Guides.prototype.line = function (kind, place) {
    var line = this.lines[kind];
    if (!place) { if (line) line.hidden = true; return; }
    if (!line) {
      line = this.lines[kind] = Canvas.el("div", "cv-guide cv-guide--" + kind);
      line.setAttribute("aria-hidden", "true");
      this.world.appendChild(line);
    }
    line.hidden = false;
    Object.keys(place).forEach(function (key) { line.style[key] = place[key] + "px"; });
  };

  Guides.prototype.remove = function () {
    Object.keys(this.lines).forEach(function (kind) { this.lines[kind].remove(); }, this);
    this.lines = {};
  };

  function Editor(app) {
    this.app = app;
    this.canEdit = app.dataset.canEdit === "true";
    this.urls = {
      data: app.dataset.dataUrl, sync: app.dataset.syncUrl, ops: app.dataset.opsUrl,
      upload: app.dataset.uploadUrl, pages: app.dataset.pagesUrl
    };
    this.storageKey = app.dataset.storageKey;
    this.statusEl = app.querySelector("[data-status]");
    this.stage = app.querySelector("[data-canvas-stage]");
    this.session = randomId("s");
    this.synced = { nodes: {}, edges: {} };
    this.seq = 0;
    this.version = 0;
    this.undoStack = [];
    this.redoStack = [];
    this.current = null;
    this.saveTimer = null;
    this.saving = false;
    this.failed = false;
    this.interaction = null;
    this.clipboard = null;
    this.snap = readSetting("canvas-snap", true);
    var editor = this;
    this.scene = new Canvas.Scene(this.stage, {
      editable: this.canEdit,
      wheelPans: true,
      onBackgroundDown: function (event) { return editor.backgroundDown(event); },
      onBackgroundClick: function () { editor.clearSelection(); },
      onViewportDone: function () { editor.rememberViewport(); },
      onGestureStart: function () { editor.cancelInteraction(); }
    });
    this.minimap = new Minimap(this, app.querySelector("[data-minimap]"));
    this.bind();
    this.refreshToggles();
    this.load(true);
  }

  /* ── Minimap ────────────────────────────────────────────────────────────── */

  /* An overview of the whole canvas with the visible area outlined; click or drag on it to move the view.
   * It is hidden from assistive technology: the text outline page describes the canvas instead. */
  function Minimap(editor, canvas) {
    this.editor = editor;
    this.canvas = canvas;
    this.visible = !!canvas && readSetting("canvas-minimap", true);
    this.frame = 0;
    this.transform = null;
    this.dragging = null;
    if (!canvas) return;
    canvas.hidden = !this.visible;
    var minimap = this;
    editor.scene.onRender = editor.scene.onViewportChange = function () { minimap.schedule(); };
    canvas.addEventListener("pointerdown", function (event) {
      event.stopPropagation();
      event.preventDefault();
      minimap.dragging = event.pointerId;
      canvas.setPointerCapture(event.pointerId);
      minimap.jump(event);
    });
    canvas.addEventListener("pointermove", function (event) {
      if (minimap.dragging === event.pointerId) minimap.jump(event);
    });
    function end(event) {
      if (minimap.dragging !== event.pointerId) return;
      minimap.dragging = null;
      minimap.schedule();
      editor.rememberViewport();
    }
    canvas.addEventListener("pointerup", end);
    canvas.addEventListener("pointercancel", end);
    canvas.addEventListener("wheel", function (event) { event.stopPropagation(); }, { passive: true });
  }

  Minimap.prototype.toggle = function () {
    if (!this.canvas) return;
    this.visible = !this.visible;
    this.canvas.hidden = !this.visible;
    writeSetting("canvas-minimap", this.visible);
    this.schedule();
  };

  Minimap.prototype.schedule = function () {
    if (!this.visible || this.frame) return;
    var minimap = this;
    this.frame = window.requestAnimationFrame(function () { minimap.draw(); });
  };

  Minimap.prototype.draw = function () {
    this.frame = 0;
    var canvas = this.canvas;
    var scene = this.editor.scene;
    var width = canvas.clientWidth, height = canvas.clientHeight;
    if (!width || !height) return;
    var ratio = window.devicePixelRatio || 1;
    canvas.width = Math.round(width * ratio);
    canvas.height = Math.round(height * ratio);
    var ctx = canvas.getContext("2d");
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    ctx.clearRect(0, 0, width, height);
    var stage = scene.stage.getBoundingClientRect();
    var v = scene.viewport;
    var view = { minX: -v.x / v.zoom, minY: -v.y / v.zoom,
                 maxX: (stage.width - v.x) / v.zoom, maxY: (stage.height - v.y) / v.zoom };
    var box = Canvas.bounds(scene.nodes) || view;
    box = { minX: Math.min(box.minX, view.minX), minY: Math.min(box.minY, view.minY),
            maxX: Math.max(box.maxX, view.maxX), maxY: Math.max(box.maxY, view.maxY) };
    var pad = 6;
    var t = this.dragging !== null && this.transform;  /* keep the map still while it is being dragged */
    var scale = Math.min((width - pad * 2) / (box.maxX - box.minX || 1), (height - pad * 2) / (box.maxY - box.minY || 1));
    var offX = pad + ((width - pad * 2) - (box.maxX - box.minX) * scale) / 2 - box.minX * scale;
    var offY = pad + ((height - pad * 2) - (box.maxY - box.minY) * scale) / 2 - box.minY * scale;
    if (t) { scale = t.scale; offX = t.offX; offY = t.offY; }
    this.transform = { scale: scale, offX: offX, offY: offY };
    var style = getComputedStyle(canvas);
    var selected = scene.selected;
    scene.nodes.forEach(function (node) {
      var size = Canvas.sizeOf(node);
      ctx.fillStyle = selected[node.id] ? style.outlineColor : Canvas.COLOR.test(node.color || "") ? node.color : style.color;
      ctx.globalAlpha = selected[node.id] ? 0.9 : 0.55;
      ctx.fillRect(offX + node.x * scale, offY + node.y * scale, Math.max(1, size.w * scale), Math.max(1, size.h * scale));
    });
    ctx.globalAlpha = 1;
    ctx.strokeStyle = style.outlineColor;
    ctx.lineWidth = 1.5;
    ctx.strokeRect(offX + view.minX * scale, offY + view.minY * scale,
                   (view.maxX - view.minX) * scale, (view.maxY - view.minY) * scale);
  };

  /* Centre the view on the canvas point under the pointer. */
  Minimap.prototype.jump = function (event) {
    var t = this.transform;
    if (!t) return;
    var rect = this.canvas.getBoundingClientRect();
    var scene = this.editor.scene;
    var stage = scene.stage.getBoundingClientRect();
    var wx = (event.clientX - rect.left - t.offX) / t.scale, wy = (event.clientY - rect.top - t.offY) / t.scale;
    scene.viewport.x = stage.width / 2 - wx * scene.viewport.zoom;
    scene.viewport.y = stage.height / 2 - wy * scene.viewport.zoom;
    scene.applyViewport();
  };

  /* ── Status ─────────────────────────────────────────────────────────────── */

  Editor.prototype.status = function (key, vars) {
    if (this.statusEl) this.statusEl.textContent = key ? BW.t(key, vars) : "";
  };

  Editor.prototype.rememberViewport = function () {
    try { localStorage.setItem(this.storageKey, JSON.stringify(this.scene.viewport)); } catch (e) { /* storage off */ }
  };

  Editor.prototype.restoreViewport = function () {
    try {
      var saved = JSON.parse(localStorage.getItem(this.storageKey) || "null");
      if (saved && isFinite(saved.x) && isFinite(saved.y) && isFinite(saved.zoom)) {
        this.scene.viewport = { x: saved.x, y: saved.y, zoom: Canvas.clamp(saved.zoom, 0.15, 3) };
        this.scene.applyViewport();
        return;
      }
    } catch (e) { /* ignore */ }
    this.scene.fit();
  };

  /* ── Loading and syncing ────────────────────────────────────────────────── */

  Editor.prototype.load = function (first) {
    var editor = this;
    this.status("canvas.status.loading");
    return BW.fetchJSON(this.urls.data).then(function (res) {
      editor.version = res.version;
      editor.seq = res.seq || 0;
      editor.scene.selected = {};
      editor.scene.selectedEdge = null;
      editor.scene.load(res.data, res);
      editor.markSynced();
      editor.current = editor.snapshot();
      if (first) editor.restoreViewport();
      editor.refreshToolbar();
      editor.status("canvas.status.loaded");
      if (first) editor.scheduleSync();
    }).catch(function () { editor.status("canvas.status.load_failed"); });
  };

  Editor.prototype.markSynced = function () {
    var synced = { nodes: {}, edges: {} };
    this.scene.nodes.forEach(function (node) { synced.nodes[node.id] = JSON.stringify(node); });
    this.scene.edges.forEach(function (edge) { synced.edges[edge.id] = JSON.stringify(edge); });
    this.synced = synced;
  };

  Editor.prototype.scheduleSync = function () {
    var editor = this;
    window.clearTimeout(this.syncTimer);
    this.syncTimer = window.setTimeout(function () { editor.sync(); }, SYNC_MS);
  };

  Editor.prototype.sync = function () {
    var editor = this;
    if (document.hidden || this.interaction || this.saving) { this.scheduleSync(); return; }
    BW.fetchJSON(this.urls.sync + "?since=" + this.seq, { headers: { "X-Canvas-Session": this.session } })
      .then(function (res) {
        if (res.reset || res.events.some(function (event) { return event.op_type === "snapshot"; })) {
          editor.status("canvas.status.reloaded");
          return editor.flush().then(function () { return editor.load(false); });
        }
        editor.seq = res.seq;
        editor.version = res.version;
        if (res.events.length) {
          editor.scene.absorb(res);
          res.events.forEach(function (event) { editor.applyRemote(event); });
          editor.scene.render();
          editor.current = editor.snapshot();
          editor.refreshToolbar();
        }
      })
      .catch(function () { /* offline: try again later */ })
      .then(function () { editor.scheduleSync(); });
  };

  Editor.prototype.localChanged = function (kind, id) {
    var item = kind === "nodes" ? this.scene.node(id) : this.scene.edge(id);
    return item ? JSON.stringify(item) !== this.synced[kind][id] : this.synced[kind][id] !== undefined;
  };

  Editor.prototype.applyRemote = function (event) {
    var payload = event.payload || {};
    var scene = this.scene;
    var kind = /_node$/.test(event.op_type) ? "nodes" : "edges";
    var list = scene[kind];
    if (event.op_type === "upsert_node" || event.op_type === "upsert_edge") {
      var item = payload.node || payload.edge;
      var dirty = this.localChanged(kind, item.id);
      this.synced[kind][item.id] = JSON.stringify(item);
      if (dirty) return;
      var index = list.findIndex(function (existing) { return existing.id === item.id; });
      if (index >= 0) list[index] = item; else list.push(item);
    } else if (event.op_type === "delete_node" || event.op_type === "delete_edge") {
      delete this.synced[kind][payload.id];
      scene[kind] = list.filter(function (existing) { return existing.id !== payload.id; });
      delete scene.selected[payload.id];
      if (kind === "nodes") {
        var synced = this.synced.edges;
        scene.edges = scene.edges.filter(function (edge) {
          var keep = edge.from !== payload.id && edge.to !== payload.id;
          if (!keep) delete synced[edge.id];
          return keep;
        });
      } else if (scene.selectedEdge === payload.id) {
        scene.selectedEdge = null;
      }
    }
  };

  /* ── Saving ─────────────────────────────────────────────────────────────── */

  Editor.prototype.snapshot = function () {
    return JSON.stringify({ nodes: this.scene.nodes, edges: this.scene.edges });
  };

  /* Record a finished change: undo point, redraw, save soon. */
  Editor.prototype.commit = function () {
    var next = this.snapshot();
    if (next === this.current) { this.scene.render(); return; }
    this.undoStack.push(this.current);
    if (this.undoStack.length > MAX_UNDO) this.undoStack.shift();
    this.redoStack = [];
    this.current = next;
    this.scene.render();
    this.refreshToolbar();
    this.scheduleSave();
  };

  Editor.prototype.scheduleSave = function () {
    var editor = this;
    this.status("canvas.status.unsaved");
    window.clearTimeout(this.saveTimer);
    this.saveTimer = window.setTimeout(function () { editor.save(); }, SAVE_DELAY_MS);
  };

  Editor.prototype.diff = function () {
    var ops = [];
    var synced = this.synced;
    var nodeIds = {}, edgeIds = {};
    this.scene.nodes.forEach(function (node) {
      nodeIds[node.id] = true;
      if (synced.nodes[node.id] !== JSON.stringify(node)) ops.push({ op: "upsert_node", node: node });
    });
    Object.keys(synced.nodes).forEach(function (id) {
      if (!nodeIds[id]) ops.push({ op: "delete_node", id: id });
    });
    this.scene.edges.forEach(function (edge) {
      edgeIds[edge.id] = true;
      if (synced.edges[edge.id] !== JSON.stringify(edge)) ops.push({ op: "upsert_edge", edge: edge });
    });
    Object.keys(synced.edges).forEach(function (id) {
      if (!edgeIds[id] && nodeIds[JSON.parse(synced.edges[id]).from] && nodeIds[JSON.parse(synced.edges[id]).to]) {
        ops.push({ op: "delete_edge", id: id });
      } else if (!edgeIds[id]) {
        delete synced.edges[id];  /* removed with its node on the server */
      }
    });
    return ops.slice(0, MAX_OPS);
  };

  Editor.prototype.save = function () {
    var editor = this;
    window.clearTimeout(this.saveTimer);
    if (!this.canEdit) return Promise.resolve();
    if (this.saving) { this.saveAgain = true; return this.saving; }
    var ops = this.diff();
    if (!ops.length) { this.status(this.failed ? "canvas.status.save_failed" : "canvas.status.saved"); return Promise.resolve(); }
    var sent = {};
    ops.forEach(function (op) { if (op.op === "upsert_node") sent[op.node.id] = JSON.stringify(op.node); });
    this.status("canvas.status.saving");
    this.saving = BW.fetchJSON(this.urls.ops, {
      method: "POST", body: { ops: ops }, headers: { "X-Canvas-Session": this.session }
    }).then(function (res) {
      editor.failed = false;
      editor.version = res.version;
      if (res.rejected && res.rejected.length) {
        BW.toast(res.error || BW.t("canvas.status.locked"), "error");
        editor.reloadNeeded = true;
      }
      editor.scene.absorb(res);
      ops.forEach(function (op) {
        if (op.op === "delete_node") {
          delete editor.synced.nodes[op.id];
          Object.keys(editor.synced.edges).forEach(function (edgeId) {
            var edge = JSON.parse(editor.synced.edges[edgeId]);
            if (edge.from === op.id || edge.to === op.id) delete editor.synced.edges[edgeId];
          });
        } else if (op.op === "delete_edge") {
          delete editor.synced.edges[op.id];
        } else if (op.op === "upsert_edge") {
          editor.synced.edges[op.edge.id] = JSON.stringify(op.edge);
        } else if (op.op === "upsert_node") {
          editor.synced.nodes[op.node.id] = sent[op.node.id];
        }
      });
      res.applied.forEach(function (op) {
        if (op.op === "upsert_edge") editor.synced.edges[op.edge.id] = JSON.stringify(op.edge);
        if (op.op !== "upsert_node") return;
        editor.synced.nodes[op.node.id] = JSON.stringify(op.node);
        var index = editor.scene.nodes.findIndex(function (node) { return node.id === op.node.id; });
        if (index >= 0 && JSON.stringify(editor.scene.nodes[index]) === sent[op.node.id]) {
          editor.scene.nodes[index] = op.node;
        }
      });
      editor.current = editor.snapshot();
      editor.scene.render();
      if (ops.length >= MAX_OPS) editor.saveAgain = true;  /* the rest of a large change */
      else editor.status("canvas.status.saved");
    }).catch(function (error) {
      editor.failed = true;
      editor.status(error.status ? "canvas.status.save_failed" : "canvas.status.offline");
      if (error.data && error.data.error) BW.toast(error.data.error, "error");
      if (!error.status || error.status >= 500 || error.status === 429) {
        window.setTimeout(function () { editor.save(); }, 4000);
      }
    }).then(function () {
      editor.saving = false;
      if (editor.saveAgain) { editor.saveAgain = false; return editor.save(); }
      if (editor.reloadNeeded) { editor.reloadNeeded = false; return editor.load(false); }
      return undefined;
    });
    return this.saving;
  };

  Editor.prototype.flush = function () {
    return this.canEdit ? this.save() : Promise.resolve();
  };

  Editor.prototype.hasUnsaved = function () {
    return this.canEdit && (this.saving || this.diff().length > 0);
  };

  /* ── Undo ───────────────────────────────────────────────────────────────── */

  Editor.prototype.restore = function (from, to) {
    if (!from.length) return;
    to.push(this.current);
    var locks = lockedIds(JSON.parse(this.current).nodes);
    this.current = from.pop();
    var state = JSON.parse(this.current);
    this.scene.nodes = state.nodes;
    this.scene.edges = state.edges;
    var present = {};
    state.nodes.forEach(function (node) { present[node.id] = true; });
    Object.keys(this.scene.selected).forEach(function (id) { if (!present[id]) delete this.scene.selected[id]; }, this);
    this.scene.selectedEdge = null;
    this.scene.render();
    this.refreshToolbar();
    if (lockedIds(state.nodes) !== locks) this.save(); else this.scheduleSave();
  };

  Editor.prototype.undo = function () { this.restore(this.undoStack, this.redoStack); };
  Editor.prototype.redo = function () { this.restore(this.redoStack, this.undoStack); };

  /* ── Selection ──────────────────────────────────────────────────────────── */

  Editor.prototype.selectedNodes = function () {
    var selected = this.scene.selected;
    return this.scene.nodes.filter(function (node) { return selected[node.id]; });
  };

  /* *ids* plus the other members of their groups. */
  Editor.prototype.withGroups = function (ids) {
    var groups = {};
    ids.forEach(function (id) {
      var node = this.scene.node(id);
      if (node && node.group) groups[node.group] = true;
    }, this);
    var all = ids.slice();
    this.scene.nodes.forEach(function (node) {
      if (node.group && groups[node.group] && all.indexOf(node.id) < 0) all.push(node.id);
    });
    return all;
  };

  Editor.prototype.select = function (ids, additive) {
    if (!additive) this.scene.selected = {};
    ids.forEach(function (id) { this.scene.selected[id] = true; }, this);
    this.scene.selectedEdge = null;
    this.scene.render();
    this.refreshToolbar();
  };

  Editor.prototype.clearSelection = function () {
    if (this.scene.connectFrom) { this.cancelConnect(); return; }
    this.scene.selected = {};
    this.scene.selectedEdge = null;
    this.scene.render();
    this.refreshToolbar();
  };

  Editor.prototype.selectEdge = function (id) {
    this.scene.selected = {};
    this.scene.selectedEdge = id;
    this.scene.render();
    this.refreshToolbar();
  };

  /* Buttons inside [data-needs-selection] need a selected node (or edge, for edit and delete);
   * data-min="n" asks for at least n selected nodes. */
  Editor.prototype.refreshToolbar = function () {
    var count = this.selectedNodes().length;
    var hasSelection = count > 0 || !!this.scene.selectedEdge;
    this.app.querySelectorAll("[data-needs-selection] [data-action]").forEach(function (button) {
      var action = button.getAttribute("data-action");
      var nodeOnly = action !== "delete" && action !== "edit";
      var min = parseInt(button.getAttribute("data-min") || "1", 10);
      button.disabled = !hasSelection || (nodeOnly && count < min);
    }, this);
    var menu = this.app.querySelector("[data-arrange-menu]");
    if (menu) menu.classList.toggle("is-disabled", !count);
    var undo = this.app.querySelector("[data-action='undo']");
    var redo = this.app.querySelector("[data-action='redo']");
    if (undo) undo.disabled = !this.undoStack.length;
    if (redo) redo.disabled = !this.redoStack.length;
  };

  /* ── Pointer interaction ────────────────────────────────────────────────── */

  Editor.prototype.bindPointer = function () {
    var editor = this;
    var stage = this.stage;

    stage.addEventListener("pointerdown", function (event) {
      if (event.button !== 0 || Object.keys(editor.scene.pointers).length > 1) return;
      var edgeEl = event.target.closest(".cv-edge");
      if (edgeEl) {
        editor.selectEdge(edgeEl.getAttribute("data-edge-id"));
        editor.focusQuietly(edgeEl);
        return;
      }
      var nodeEl = event.target.closest(".cv-node");
      if (!nodeEl || event.target.closest("a, iframe, button, input, textarea, select")) return;
      var id = nodeEl.dataset.id;
      /* Keyboard shortcuts keep working after a click, without the focus handler changing the selection. */
      event.preventDefault();
      editor.focusQuietly(nodeEl);
      if (editor.scene.connectFrom) { editor.finishConnect(id); return; }
      var members = event.altKey ? [id] : editor.withGroups([id]);
      if (event.shiftKey || event.ctrlKey || event.metaKey) {
        var adding = !editor.scene.selected[id];
        members.forEach(function (member) {
          if (adding) editor.scene.selected[member] = true; else delete editor.scene.selected[member];
        });
        editor.scene.selectedEdge = null;
        editor.scene.render();
        editor.refreshToolbar();
        return;
      }
      if (!editor.scene.selected[id]) editor.select(members, false);
      var start = editor.scene.toWorld(event.clientX, event.clientY);
      var resize = event.target.classList.contains("cv-node__resize");
      var grabbed = editor.scene.node(id);
      var targets = resize ? [grabbed] : editor.movable(editor.selectedNodes());
      if (!targets.length) return;
      /* The grabbed node comes first: snapping lines it up and moves the others by as much. */
      targets.sort(function (a, b) { return (b === grabbed) - (a === grabbed); });
      var skip = {};
      targets.forEach(function (node) { skip[node.id] = true; });
      editor.interaction = {
        kind: resize ? "resize" : "move", start: start, moved: false, pointerId: event.pointerId,
        origin: targets.map(function (node) {
          var size = Canvas.sizeOf(node);
          return { node: node, x: node.x, y: node.y, w: size.w, h: size.h };
        }),
        box: Canvas.bounds(targets),
        guides: new Guides(editor.scene, skip)
      };
      stage.setPointerCapture(event.pointerId);
    });

    stage.addEventListener("pointermove", function (event) {
      var action = editor.interaction;
      if (editor.scene.connectFrom && !action) {
        var from = editor.scene.node(editor.scene.connectFrom);
        if (from) {
          var size = Canvas.sizeOf(from);
          var to = editor.scene.toWorld(event.clientX, event.clientY);
          editor.scene.preview = { x1: from.x + size.w / 2, y1: from.y + size.h / 2, x2: to.x, y2: to.y };
          editor.scene.renderPreview();
        }
        return;
      }
      if (!action || action.pointerId !== event.pointerId) return;
      var point = editor.scene.toWorld(event.clientX, event.clientY);
      if (action.kind === "marquee") { editor.updateMarquee(point); return; }
      var dx = point.x - action.start.x, dy = point.y - action.start.y;
      if (Math.abs(dx) + Math.abs(dy) > 2) action.moved = true;
      var snapped = editor.snapDrag(action, dx, dy, event.altKey);
      dx = snapped.dx;
      dy = snapped.dy;
      var moved = {};
      action.origin.forEach(function (item) {
        if (action.kind === "move") {
          item.node.x = Math.round(item.x + dx);
          item.node.y = Math.round(item.y + dy);
        } else {
          item.node.width = Math.round(Canvas.clamp(item.w + dx, 40, 4000));
          item.node.height = Math.round(Canvas.clamp(item.h + dy, 30, 4000));
        }
        moved[item.node.id] = true;
        editor.scene.moveNode(item.node);
      });
      editor.scene.renderEdges(moved);
      editor.minimap.schedule();
    });

    function end(event) {
      var action = editor.interaction;
      if (!action || action.pointerId !== event.pointerId) return;
      editor.interaction = null;
      if (action.kind === "marquee") { editor.finishMarquee(); return; }
      action.guides.remove();
      if (action.moved) editor.commit();
    }
    stage.addEventListener("pointerup", end);
    stage.addEventListener("pointercancel", function (event) {
      if (editor.interaction && editor.interaction.pointerId === event.pointerId) editor.cancelInteraction();
    });

    stage.addEventListener("dblclick", function (event) {
      var edgeEl = event.target.closest(".cv-edge");
      if (edgeEl) { editor.openEdgeDialog(edgeEl.getAttribute("data-edge-id")); return; }
      var nodeEl = event.target.closest(".cv-node");
      if (nodeEl && !event.target.closest("a, iframe")) editor.openNodeDialog(nodeEl.dataset.id);
      else if (!nodeEl && editor.scene.isBackground(event.target)) {
        editor.openNodeDialog(null, "text", editor.scene.toWorld(event.clientX, event.clientY));
      }
    });
  };

  /* The pointer offset of a move or resize after snapping: to an alignment guide within reach, else to
   * the grid while snapping is on; Alt places freely. Shows the guides that were used. */
  Editor.prototype.snapDrag = function (action, dx, dy, free) {
    var first = action.origin[0];
    var guides = action.guides;
    var gx = null, gy = null;
    if (!free && action.kind === "move") {
      var box = action.box, w = box.maxX - box.minX, h = box.maxY - box.minY;
      var left = box.minX + dx, top = box.minY + dy;
      gx = guides.nearest(guides.xs, [left, left + w / 2, left + w]);
      gy = guides.nearest(guides.ys, [top, top + h / 2, top + h]);
      if (gx) dx += gx.delta; else if (this.snap) dx = snapValue(first.x + dx) - first.x;
      if (gy) dy += gy.delta; else if (this.snap) dy = snapValue(first.y + dy) - first.y;
    } else if (!free) {
      gx = guides.nearest(guides.xs, [first.x + first.w + dx]);
      gy = guides.nearest(guides.ys, [first.y + first.h + dy]);
      if (gx) dx += gx.delta; else if (this.snap) dx = snapValue(first.x + first.w + dx) - first.x - first.w;
      if (gy) dy += gy.delta; else if (this.snap) dy = snapValue(first.y + first.h + dy) - first.y - first.h;
    }
    var moved = action.kind === "move"
      ? { minX: action.box.minX + dx, minY: action.box.minY + dy, maxX: action.box.maxX + dx, maxY: action.box.maxY + dy }
      : { minX: first.x, minY: first.y, maxX: first.x + first.w + dx, maxY: first.y + first.h + dy };
    guides.show(gx, gy, moved);
    return { dx: dx, dy: dy };
  };

  Editor.prototype.cancelInteraction = function () {
    var action = this.interaction;
    this.interaction = null;
    if (!action) return;
    if (action.kind === "marquee") { this.removeMarquee(); return; }
    action.guides.remove();
    action.origin.forEach(function (item) {
      item.node.x = item.x; item.node.y = item.y;
      if (action.kind === "resize") { item.node.width = item.w; item.node.height = item.h; }
    });
    this.scene.render();
  };

  /* Shift-drag on the background draws a selection rectangle. */
  Editor.prototype.backgroundDown = function (event) {
    if (!this.canEdit || !event.shiftKey) return false;
    var start = this.scene.toWorld(event.clientX, event.clientY);
    this.marquee = Canvas.el("div", "cv-marquee");
    this.scene.world.appendChild(this.marquee);
    this.interaction = { kind: "marquee", start: start, end: start, pointerId: event.pointerId };
    this.stage.setPointerCapture(event.pointerId);
    return true;
  };

  Editor.prototype.updateMarquee = function (point) {
    var action = this.interaction;
    action.end = point;
    var style = this.marquee.style;
    style.left = Math.min(action.start.x, point.x) + "px";
    style.top = Math.min(action.start.y, point.y) + "px";
    style.width = Math.abs(point.x - action.start.x) + "px";
    style.height = Math.abs(point.y - action.start.y) + "px";
  };

  Editor.prototype.removeMarquee = function () {
    if (this.marquee) this.marquee.remove();
    this.marquee = null;
  };

  Editor.prototype.finishMarquee = function () {
    var box = this.marquee ? this.marquee.style : null;
    this.removeMarquee();
    if (!box || !box.width) return;
    var left = parseFloat(box.left), top = parseFloat(box.top);
    var right = left + parseFloat(box.width), bottom = top + parseFloat(box.height);
    var ids = this.scene.nodes.filter(function (node) {
      var size = Canvas.sizeOf(node);
      return node.x >= left && node.y >= top && node.x + size.w <= right && node.y + size.h <= bottom;
    }).map(function (node) { return node.id; });
    this.select(ids, true);
  };

  /* ── Node and edge actions ──────────────────────────────────────────────── */

  Editor.prototype.topLayer = function () {
    return this.scene.nodes.reduce(function (top, node) { return Math.max(top, node.layer || 0); }, 0);
  };

  Editor.prototype.addNode = function (fields, at) {
    var size = Canvas.DEFAULT_SIZES[fields.type] || Canvas.DEFAULT_SIZES.text;
    var width = fields.width || size[0], height = fields.height || size[1];
    var point = at || this.scene.center();
    var x = Math.round(point.x - width / 2), y = Math.round(point.y - height / 2);
    while (!at && this.scene.nodes.some(function (other) { return other.x === x && other.y === y; })) {
      x += PASTE_OFFSET;
      y += PASTE_OFFSET;
    }
    var node = Object.assign({ id: randomId("n"), x: x, y: y, width: width, height: height,
                               layer: this.topLayer() + 1, text_size: 13 }, fields);
    this.scene.nodes.push(node);
    this.scene.selected = {};
    this.scene.selected[node.id] = true;
    this.commit();
    this.focusNode(node.id);
    return node;
  };

  Editor.prototype.focusQuietly = function (element) {
    this.quietFocus = true;
    try { element.focus({ preventScroll: true }); } finally { this.quietFocus = false; }
  };

  Editor.prototype.focusNode = function (id) {
    var element = this.scene.elements[id];
    if (element) element.focus({ preventScroll: true });
  };

  Editor.prototype.deleteSelection = function () {
    var edgeId = this.scene.selectedEdge;
    if (edgeId) {
      this.scene.edges = this.scene.edges.filter(function (edge) { return edge.id !== edgeId; });
      this.scene.selectedEdge = null;
    } else {
      var nodes = this.selectedNodes();
      if (!nodes.length) return;
      var removed = {};
      this.movable(nodes).forEach(function (node) { removed[node.id] = true; });
      if (Object.keys(removed).length < nodes.length) this.status("canvas.status.locked_kept");
      this.scene.nodes = this.scene.nodes.filter(function (node) { return !removed[node.id]; });
      this.scene.edges = this.scene.edges.filter(function (edge) { return !removed[edge.from] && !removed[edge.to]; });
      this.scene.selected = {};
    }
    this.commit();
    this.stage.focus({ preventScroll: true });
  };

  /* The nodes of *nodes* that are not locked. */
  Editor.prototype.movable = function (nodes) {
    return nodes.filter(function (node) { return !node.locked; });
  };

  Editor.prototype.moveSelection = function (dx, dy, resize) {
    var selected = this.selectedNodes();
    if (!selected.length) return false;
    var nodes = this.movable(selected);
    if (!nodes.length) { this.status("canvas.status.locked"); return true; }
    nodes.forEach(function (node) {
      if (resize) {
        var size = Canvas.sizeOf(node);
        node.width = Canvas.clamp(size.w + dx, 40, 4000);
        node.height = Canvas.clamp(size.h + dy, 30, 4000);
      } else {
        node.x += dx;
        node.y += dy;
      }
    });
    this.commit();
    var node = nodes[0], size = Canvas.sizeOf(node);
    this.status(resize ? "canvas.status.resized" : "canvas.status.moved",
                { x: Math.round(node.x), y: Math.round(node.y), width: size.w, height: size.h });
    return true;
  };

  /* Line up the selected nodes: edge is left, center, right, top, middle or bottom. */
  Editor.prototype.align = function (edge) {
    var nodes = this.movable(this.selectedNodes());
    var box = Canvas.bounds(this.selectedNodes());
    if (nodes.length < 1 || this.selectedNodes().length < 2) return;
    nodes.forEach(function (node) {
      var size = Canvas.sizeOf(node);
      switch (edge) {
        case "left": node.x = box.minX; break;
        case "center": node.x = Math.round((box.minX + box.maxX - size.w) / 2); break;
        case "right": node.x = box.maxX - size.w; break;
        case "top": node.y = box.minY; break;
        case "middle": node.y = Math.round((box.minY + box.maxY - size.h) / 2); break;
        case "bottom": node.y = box.maxY - size.h; break;
      }
    });
    this.commit();
    this.status("canvas.status.aligned");
  };

  /* Spread three or more selected nodes so the gaps between them are equal. */
  Editor.prototype.distribute = function (horizontal) {
    var nodes = this.selectedNodes();
    if (nodes.length < 3) return;
    var pos = horizontal ? "x" : "y";
    var sizeKey = horizontal ? "w" : "h";
    nodes.sort(function (a, b) { return a[pos] - b[pos]; });
    var total = 0;
    nodes.forEach(function (node) { total += Canvas.sizeOf(node)[sizeKey]; });
    var firstNode = nodes[0], lastNode = nodes[nodes.length - 1];
    var span = lastNode[pos] + Canvas.sizeOf(lastNode)[sizeKey] - firstNode[pos];
    var gap = (span - total) / (nodes.length - 1);
    var cursor = firstNode[pos];
    nodes.forEach(function (node) {
      if (!node.locked) node[pos] = Math.round(cursor);
      cursor += Canvas.sizeOf(node)[sizeKey] + gap;
    });
    this.commit();
    this.status("canvas.status.distributed");
  };

  Editor.prototype.group = function () {
    var nodes = this.selectedNodes();
    if (nodes.length < 2) return;
    var id = randomId("g");
    nodes.forEach(function (node) { node.group = id; });
    this.commit();
    this.status("canvas.status.grouped", { count: nodes.length });
  };

  Editor.prototype.ungroup = function () {
    var nodes = this.selectedNodes().filter(function (node) { return node.group; });
    if (!nodes.length) return;
    nodes.forEach(function (node) { delete node.group; });
    this.commit();
    this.status("canvas.status.ungrouped");
  };

  /* Lock the selection, or unlock it when every selected node is locked already. */
  Editor.prototype.toggleLock = function () {
    var nodes = this.selectedNodes();
    if (!nodes.length) return;
    var lock = nodes.some(function (node) { return !node.locked; });
    nodes.forEach(function (node) { if (lock) node.locked = true; else delete node.locked; });
    this.commit();
    this.save();  /* on its own, before any edit that needs the unlock */
    this.status(lock ? "canvas.status.locked_now" : "canvas.status.unlocked");
  };

  Editor.prototype.toggleSnap = function () {
    this.snap = !this.snap;
    writeSetting("canvas-snap", this.snap);
    this.refreshToggles();
    this.status(this.snap ? "canvas.status.snap_on" : "canvas.status.snap_off");
  };

  Editor.prototype.refreshToggles = function () {
    var snap = this.app.querySelector("[data-action='snap']");
    if (snap) snap.setAttribute("aria-pressed", this.snap ? "true" : "false");
    var minimap = this.app.querySelector("[data-action='minimap']");
    if (minimap) minimap.setAttribute("aria-pressed", this.minimap.visible ? "true" : "false");
  };

  Editor.prototype.exportImage = function (format) {
    var editor = this;
    this.status("canvas.status.exporting");
    Canvas.exportImage(this.scene, format, this.app.dataset.filename || "canvas").then(function (placeholders) {
      if (placeholders) editor.status("canvas.status.exported_placeholders", { count: placeholders });
      else editor.status("canvas.status.exported");
    }).catch(function () {
      editor.status("");
      BW.toast(BW.t("canvas.export_failed"), "error");
    });
  };

  Editor.prototype.restack = function (toFront) {
    var nodes = this.selectedNodes();
    if (!nodes.length) return;
    var others = this.scene.nodes.filter(function (node) { return nodes.indexOf(node) < 0; }).sort(function (a, b) {
      return (a.layer || 0) - (b.layer || 0);
    });
    var ordered = toFront ? others.concat(nodes) : nodes.concat(others);
    ordered.forEach(function (node, index) { node.layer = index; });
    this.commit();
  };

  Editor.prototype.copySelection = function () {
    var nodes = this.selectedNodes();
    if (!nodes.length) return null;
    var ids = {};
    nodes.forEach(function (node) { ids[node.id] = true; });
    var edges = this.scene.edges.filter(function (edge) { return ids[edge.from] && ids[edge.to]; });
    this.clipboard = clone({ nodes: nodes, edges: edges });
    var payload = {};
    payload[CLIPBOARD_MARK] = 1;
    payload.nodes = this.clipboard.nodes;
    payload.edges = this.clipboard.edges;
    return JSON.stringify(payload);
  };

  Editor.prototype.pasteItems = function (items, at) {
    if (!items || !items.nodes || !items.nodes.length) return;
    var mapping = {};
    var minX = Infinity, minY = Infinity;
    items.nodes.forEach(function (node) { minX = Math.min(minX, node.x || 0); minY = Math.min(minY, node.y || 0); });
    var dx = at ? at.x - minX : PASTE_OFFSET, dy = at ? at.y - minY : PASTE_OFFSET;
    var layer = this.topLayer();
    var groups = {};
    var created = items.nodes.slice(0, MAX_OPS).map(function (node) {
      var copy = clone(node);
      mapping[node.id] = copy.id = randomId("n");
      if (copy.group) copy.group = groups[copy.group] || (groups[copy.group] = randomId("g"));
      copy.x = Math.round((copy.x || 0) + dx);
      copy.y = Math.round((copy.y || 0) + dy);
      copy.layer = ++layer;
      delete copy.restricted;
      return copy;
    });
    (items.edges || []).forEach(function (edge) {
      if (mapping[edge.from] && mapping[edge.to]) {
        this.scene.edges.push(Object.assign(clone(edge), { id: randomId("e"), from: mapping[edge.from], to: mapping[edge.to] }));
      }
    }, this);
    this.scene.nodes = this.scene.nodes.concat(created);
    this.scene.selected = {};
    created.forEach(function (node) { this.scene.selected[node.id] = true; }, this);
    this.clipboard = { nodes: created, edges: [] };
    this.commit();
  };

  Editor.prototype.duplicate = function () {
    if (this.copySelection()) this.pasteItems(this.clipboard, null);
  };

  Editor.prototype.startConnect = function () {
    var nodes = this.selectedNodes();
    if (nodes.length !== 1) { this.status("canvas.status.connect_select"); return; }
    this.scene.connectFrom = nodes[0].id;
    this.scene.render();
    this.status("canvas.status.connect_hint");
  };

  Editor.prototype.cancelConnect = function () {
    this.scene.connectFrom = null;
    this.scene.preview = null;
    this.scene.render();
    this.status("");
  };

  Editor.prototype.finishConnect = function (targetId) {
    var from = this.scene.connectFrom;
    this.scene.connectFrom = null;
    this.scene.preview = null;
    if (from && targetId && from !== targetId) {
      var exists = this.scene.edges.some(function (edge) { return edge.from === from && edge.to === targetId; });
      if (!exists) this.scene.edges.push({ id: randomId("e"), from: from, to: targetId, label: "", text_size: 14 });
      this.status("canvas.status.connected");
    } else {
      this.status("");
    }
    this.commit();
  };

  /* ── Dialogs ────────────────────────────────────────────────────────────── */

  var FIELDS = {
    text: ["display_text", "content", "markdown_hint", "shape"],
    code: ["display_text", "content", "language"],
    image: ["display_text", "url", "upload", "alt"],
    video: ["display_text", "url"],
    wiki_page: ["display_text", "page"],
    external_link: ["display_text", "url"]
  };

  Editor.prototype.openNodeDialog = function (nodeId, type, at) {
    var node = nodeId ? this.scene.node(nodeId) : null;
    if (nodeId && !node) return;
    if (node && node.locked) { this.status("canvas.status.locked"); return; }
    type = node ? node.type : type;
    var dialog = document.getElementById("canvas-node-dialog");
    var form = dialog.querySelector("[data-node-form]");
    var visible = FIELDS[type] || FIELDS.text;
    form.querySelectorAll("[data-field]").forEach(function (field) {
      field.hidden = visible.indexOf(field.getAttribute("data-field")) < 0;
    });
    var values = node || { type: type, text_size: 13, shape: type === "text" && this.pendingShape ? this.pendingShape : "" };
    var size = Canvas.sizeOf(values.width ? values : { type: type });
    form.elements.display_text.value = values.display_text || "";
    form.elements.content.value = values.content || "";
    form.elements.language.value = values.language || "";
    form.elements.url.value = values.url || "";
    form.elements.alt.value = values.alt || "";
    form.elements.shape.value = values.shape || "rectangle";
    form.elements.color.value = values.color || "";
    form.elements.text_size.value = values.text_size || 13;
    form.elements.width.value = size.w;
    form.elements.height.value = size.h;
    form.elements.file.value = "";
    var contentLabel = form.querySelector("[data-content-label]");
    contentLabel.textContent = contentLabel.getAttribute(type === "code" ? "data-code-label" : "data-text-label");
    var urlLabel = form.querySelector("[data-url-label]");
    urlLabel.textContent = urlLabel.getAttribute(type === "image" ? "data-image-label" :
      type === "video" ? "data-video-label" : "data-link-label");
    form.querySelector("[data-upload-status]").textContent = "";
    form.querySelector("[data-form-error]").textContent = "";
    this.pageChoice = type === "wiki_page" && node && node.page_id ? { id: node.page_id } : null;
    this.resetPagePicker(form, node);
    dialog.querySelector("[data-dialog-title]").textContent =
      BW.t(node ? "canvas.dialog.edit" : "canvas.dialog.add", { type: BW.t("node." + type) });
    this.dialogState = { nodeId: nodeId, type: type, at: at || null };
    dialog.showModal();
    var first = form.querySelector("[data-field]:not([hidden]) input, [data-field]:not([hidden]) textarea");
    var focusTarget = type === "wiki_page" ? form.querySelector("#cn-page-search") :
      (type === "text" || type === "code") ? form.elements.content : first;
    if (focusTarget) focusTarget.focus();
  };

  Editor.prototype.resetPagePicker = function (form, node) {
    var results = form.querySelector("#cn-page-results");
    results.textContent = "";
    form.querySelector("#cn-page-search").value = "";
    var chosen = form.querySelector("[data-page-selected]");
    var page = node && node.page_id && this.scene.pages[node.page_id];
    chosen.textContent = page ? BW.t("canvas.dialog.page_selected", { title: page.title }) : "";
  };

  Editor.prototype.searchPages = function (query) {
    var editor = this;
    var results = document.getElementById("cn-page-results");
    window.clearTimeout(this.pageTimer);
    if (!query.trim()) { results.textContent = ""; return; }
    this.pageTimer = window.setTimeout(function () {
      BW.fetchJSON(editor.urls.pages + "?q=" + encodeURIComponent(query)).then(function (res) {
        results.textContent = "";
        if (!res.pages.length) {
          results.appendChild(Canvas.el("li", "muted", BW.t("canvas.dialog.no_pages")));
          return;
        }
        res.pages.forEach(function (page) {
          var item = Canvas.el("li");
          var button = Canvas.el("button", "canvas-page-results__item", page.title);
          button.type = "button";
          button.setAttribute("role", "option");
          button.addEventListener("click", function () {
            editor.pageChoice = page;
            var form = document.querySelector("[data-node-form]");
            if (!form.elements.display_text.value) form.elements.display_text.placeholder = page.title;
            form.querySelector("[data-page-selected]").textContent = BW.t("canvas.dialog.page_selected", { title: page.title });
            results.textContent = "";
          });
          item.appendChild(button);
          results.appendChild(item);
        });
      }).catch(function () { results.textContent = ""; });
    }, 250);
  };

  Editor.prototype.upload = function (file) {
    var body = new FormData();
    body.append("file", file);
    return BW.fetchJSON(this.urls.upload, { method: "POST", body: body }).then(function (res) { return res.url; });
  };

  Editor.prototype.saveNodeDialog = function (form) {
    var state = this.dialogState;
    var type = state.type;
    var error = form.querySelector("[data-form-error]");
    var number = function (name, low, high, fallback) {
      var value = parseInt(form.elements[name].value, 10);
      return isFinite(value) ? Canvas.clamp(value, low, high) : fallback;
    };
    var size = Canvas.DEFAULT_SIZES[type];
    var fields = {
      type: type,
      display_text: form.elements.display_text.value.trim(),
      text_size: number("text_size", 8, 96, 13),
      width: number("width", 40, 4000, size[0]),
      height: number("height", 30, 4000, size[1])
    };
    var color = form.elements.color.value;
    if (color) fields.color = color;
    if (type === "text" || type === "code") fields.content = form.elements.content.value;
    if (type === "code" && form.elements.language.value.trim()) fields.language = form.elements.language.value.trim().toLowerCase();
    if (type === "text" && form.elements.shape.value !== "rectangle") fields.shape = form.elements.shape.value;
    if (type === "image" || type === "video" || type === "external_link") {
      fields.url = form.elements.url.value.trim();
      var valid = type === "image" ? Canvas.safeImage(fields.url) : Canvas.safeHref(fields.url);
      if (!valid) { error.textContent = BW.t("canvas.dialog.url_required"); return false; }
      fields.label = fields.display_text || fields.url.slice(0, 80);
    }
    if (type === "image" && form.elements.alt.value.trim()) fields.alt = form.elements.alt.value.trim();
    if (type === "wiki_page") {
      if (!this.pageChoice) { error.textContent = BW.t("canvas.dialog.page_required"); return false; }
      fields.page_id = this.pageChoice.id;
      if (this.pageChoice.slug) { fields.page_slug = this.pageChoice.slug; fields.label = this.pageChoice.title; }
    }
    if (state.nodeId) {
      var node = this.scene.node(state.nodeId);
      if (!node) return true;
      ["display_text", "content", "language", "url", "alt", "shape", "color", "label", "page_id", "page_slug"].forEach(function (key) {
        if (!(key in fields)) delete node[key];
      });
      if (type === "wiki_page" && !this.pageChoice.slug) fields.page_slug = node.page_slug;
      Object.keys(fields).forEach(function (key) { if (fields[key] !== undefined && fields[key] !== "") node[key] = fields[key]; });
      if (!fields.display_text) delete node.display_text;
      delete node.deleted;
      delete node.restricted;
      this.commit();
      this.focusNode(node.id);
    } else {
      if (!fields.display_text) delete fields.display_text;
      this.addNode(fields, state.at);
    }
    return true;
  };

  Editor.prototype.openEdgeDialog = function (edgeId) {
    var edge = this.scene.edge(edgeId);
    if (!edge) return;
    this.selectEdge(edgeId);
    var dialog = document.getElementById("canvas-edge-dialog");
    var form = dialog.querySelector("[data-edge-form]");
    form.elements.label.value = edge.label || "";
    form.elements.arrow.value = edge.arrow || "end";
    form.elements.style.value = edge.style || "solid";
    form.elements.route.value = edge.route || "curved";
    form.elements.from_side.value = edge.from_side || "auto";
    form.elements.to_side.value = edge.to_side || "auto";
    form.elements.text_size.value = edge.text_size || 14;
    this.edgeDialogId = edgeId;
    dialog.showModal();
    form.elements.label.focus();
  };

  Editor.prototype.saveEdgeDialog = function (form) {
    var edge = this.scene.edge(this.edgeDialogId);
    if (!edge) return;
    edge.label = form.elements.label.value.trim().slice(0, 200);
    edge.arrow = form.elements.arrow.value;
    edge.style = form.elements.style.value;
    if (form.elements.route.value === "curved") delete edge.route; else edge.route = form.elements.route.value;
    ["from_side", "to_side"].forEach(function (key) {
      var side = form.elements[key].value;
      if (Canvas.SIDES.indexOf(side) >= 0) edge[key] = side; else delete edge[key];
    });
    var size = parseInt(form.elements.text_size.value, 10);
    edge.text_size = isFinite(size) ? Canvas.clamp(size, 8, 96) : 14;
    this.commit();
  };

  Editor.prototype.bindDialogs = function () {
    var editor = this;
    var nodeDialog = document.getElementById("canvas-node-dialog");
    var nodeForm = nodeDialog.querySelector("[data-node-form]");
    nodeForm.addEventListener("submit", function (event) {
      event.preventDefault();
      if (editor.saveNodeDialog(nodeForm)) nodeDialog.close();
    });
    nodeForm.elements.file.addEventListener("change", function () {
      var file = nodeForm.elements.file.files[0];
      var status = nodeForm.querySelector("[data-upload-status]");
      if (!file) return;
      status.textContent = BW.t("canvas.dialog.uploading");
      editor.upload(file).then(function (url) {
        nodeForm.elements.url.value = url;
        status.textContent = BW.t("canvas.dialog.uploaded");
      }).catch(function (error) {
        status.textContent = (error.data && error.data.error) || BW.t("canvas.dialog.upload_failed");
      });
    });
    nodeForm.querySelector("#cn-page-search").addEventListener("input", function (event) {
      editor.searchPages(event.target.value);
    });
    var edgeDialog = document.getElementById("canvas-edge-dialog");
    var edgeForm = edgeDialog.querySelector("[data-edge-form]");
    edgeForm.addEventListener("submit", function (event) {
      event.preventDefault();
      editor.saveEdgeDialog(edgeForm);
      edgeDialog.close();
    });
    edgeDialog.querySelector("[data-edge-delete]").addEventListener("click", function () {
      edgeDialog.close();
      editor.deleteSelection();
    });
    [nodeDialog, edgeDialog].forEach(function (dialog) {
      dialog.addEventListener("close", function () { editor.stage.focus({ preventScroll: true }); });
    });
  };

  /* ── Toolbar, keyboard, clipboard, drops ────────────────────────────────── */

  Editor.prototype.run = function (action) {
    var scene = this.scene;
    switch (action) {
      case "zoom-in": scene.zoomBy(1.2); this.rememberViewport(); return;
      case "zoom-out": scene.zoomBy(1 / 1.2); this.rememberViewport(); return;
      case "fit": scene.fit(); this.rememberViewport(); return;
      case "fit-selection": scene.fit(this.selectedNodes()); this.rememberViewport(); return;
      case "minimap": this.minimap.toggle(); this.refreshToggles(); return;
      case "export-svg": this.exportImage("svg"); return;
      case "export-png": this.exportImage("png"); return;
    }
    if (!this.canEdit) return;
    if (action.indexOf("add-") === 0) {
      var type = action.slice(4);
      this.pendingShape = type === "shape" ? "rounded" : "";
      this.openNodeDialog(null, type === "shape" ? "text" : type);
      return;
    }
    switch (action) {
      case "edit":
        if (scene.selectedEdge) this.openEdgeDialog(scene.selectedEdge);
        else if (this.selectedNodes().length) this.openNodeDialog(this.selectedNodes()[0].id);
        break;
      case "connect": this.startConnect(); break;
      case "duplicate": this.duplicate(); break;
      case "front": this.restack(true); break;
      case "back": this.restack(false); break;
      case "delete": this.deleteSelection(); break;
      case "undo": this.undo(); break;
      case "redo": this.redo(); break;
      case "snap": this.toggleSnap(); break;
      case "group": this.group(); break;
      case "ungroup": this.ungroup(); break;
      case "lock": this.toggleLock(); break;
      case "distribute-h": this.distribute(true); break;
      case "distribute-v": this.distribute(false); break;
      default:
        if (action.indexOf("align-") === 0) this.align(action.slice(6));
    }
  };

  Editor.prototype.handleKey = function (event) {
    var mod = event.ctrlKey || event.metaKey;
    var key = event.key.length === 1 ? event.key.toLowerCase() : event.key;
    var focusedNode = event.target.closest && event.target.closest(".cv-node");
    var focusedEdge = event.target.closest && event.target.closest(".cv-edge");
    if (!this.canEdit) return false;
    if (mod && key === "z") { if (event.shiftKey) this.redo(); else this.undo(); return true; }
    if (mod && key === "y") { this.redo(); return true; }
    if (mod && key === "a") { this.select(this.scene.nodes.map(function (n) { return n.id; }), false); return true; }
    if (mod && key === "d") { this.duplicate(); return true; }
    if (mod && key === "s") { this.save(); return true; }
    if (mod && key === "g") { if (event.shiftKey) this.ungroup(); else this.group(); return true; }
    if (mod) return false;
    if (key === "g") { this.toggleSnap(); return true; }
    if (key === "l" && this.selectedNodes().length) { this.toggleLock(); return true; }
    if (key === "Escape") { this.clearSelection(); this.stage.focus({ preventScroll: true }); return true; }
    if (focusedEdge && (key === "Enter" || key === " ")) { this.openEdgeDialog(focusedEdge.getAttribute("data-edge-id")); return true; }
    if (focusedEdge && (key === "Delete" || key === "Backspace")) {
      this.scene.selectedEdge = focusedEdge.getAttribute("data-edge-id");
      this.deleteSelection();
      return true;
    }
    if (key === "Delete" || key === "Backspace") { this.deleteSelection(); return true; }
    if (focusedNode && key === "Enter") {
      if (this.scene.connectFrom) this.finishConnect(focusedNode.dataset.id);
      else this.openNodeDialog(focusedNode.dataset.id);
      return true;
    }
    if (focusedNode && key === " ") {
      var id = focusedNode.dataset.id;
      if (this.scene.selected[id] && event.shiftKey) delete this.scene.selected[id]; else this.select([id], event.shiftKey);
      this.scene.render();
      return true;
    }
    if (key === "c") { this.startConnect(); return true; }
    var arrows = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] };
    if (arrows[key] && this.selectedNodes().length && event.target !== this.stage) {
      var step = this.snap ? (event.shiftKey ? GRID * 4 : GRID) : (event.shiftKey ? 50 : 10);
      return this.moveSelection(arrows[key][0] * step, arrows[key][1] * step, event.altKey);
    }
    return false;
  };

  Editor.prototype.urlNode = function (url) {
    var video = /(youtube\.com|youtu\.be|vimeo\.com)\//i.test(url);
    var image = /\.(png|jpe?g|gif|webp|avif)(\?.*)?$/i.test(url);
    if (video) return { type: "video", url: url };
    if (image) return { type: "image", url: url };
    return { type: "external_link", url: url, label: url.slice(0, 80) };
  };

  Editor.prototype.addImages = function (files, at) {
    var editor = this;
    files.filter(function (file) { return /^image\//.test(file.type); }).slice(0, 10).forEach(function (file, index) {
      editor.status("canvas.status.uploading");
      editor.upload(file).then(function (url) {
        var point = at ? { x: at.x + index * PASTE_OFFSET, y: at.y + index * PASTE_OFFSET } : null;
        editor.addNode({ type: "image", url: url, alt: file.name.slice(0, 300) }, point);
      }).catch(function (error) {
        BW.toast((error.data && error.data.error) || BW.t("canvas.dialog.upload_failed"), "error");
        editor.status("");
      });
    });
  };

  Editor.prototype.paste = function (event) {
    var data = event.clipboardData;
    if (!data) return;
    var files = Array.prototype.slice.call(data.files || []);
    if (files.length) { event.preventDefault(); this.addImages(files, null); return; }
    var text = data.getData("text/plain");
    if (!text) return;
    event.preventDefault();
    try {
      var parsed = JSON.parse(text);
      if (parsed && parsed[CLIPBOARD_MARK]) { this.pasteItems(parsed, null); return; }
    } catch (e) { /* plain text */ }
    var trimmed = text.trim();
    if (/^https?:\/\/\S+$/i.test(trimmed)) { this.addNode(this.urlNode(trimmed)); return; }
    this.addNode({ type: "text", content: text.slice(0, 20000) });
  };

  Editor.prototype.bind = function () {
    var editor = this;
    var app = this.app;
    app.addEventListener("click", function (event) {
      var button = event.target.closest("[data-action]");
      if (!button || !app.contains(button)) return;
      var menu = button.closest("details");
      if (menu) {
        menu.open = false;
        menu.querySelector("summary").focus();  /* the item is hidden now; keep focus in the editor */
      }
      editor.run(button.getAttribute("data-action"));
    });
    if (this.canEdit) {
      this.bindPointer();
      this.bindDialogs();
      app.addEventListener("keydown", function (event) {
        if (isTyping(event.target)) return;
        if (editor.handleKey(event)) event.preventDefault();
      });
      this.stage.addEventListener("focusin", function (event) {
        var nodeEl = event.target.closest(".cv-node");
        if (nodeEl && !editor.quietFocus && !editor.scene.selected[nodeEl.dataset.id] && !editor.interaction) {
          editor.scene.selected = {};
          editor.scene.selected[nodeEl.dataset.id] = true;
          editor.scene.selectedEdge = null;
          editor.scene.render();
          editor.refreshToolbar();
        }
      });
      document.addEventListener("copy", function (event) {
        if (isTyping(event.target) || !app.contains(document.activeElement)) return;
        var text = editor.copySelection();
        if (text) { event.clipboardData.setData("text/plain", text); event.preventDefault(); }
      });
      document.addEventListener("cut", function (event) {
        if (isTyping(event.target) || !app.contains(document.activeElement)) return;
        var text = editor.copySelection();
        if (text) { event.clipboardData.setData("text/plain", text); event.preventDefault(); editor.deleteSelection(); }
      });
      document.addEventListener("paste", function (event) {
        if (isTyping(event.target) || !app.contains(document.activeElement)) return;
        editor.paste(event);
      });
      this.stage.addEventListener("dragover", function (event) {
        if (event.dataTransfer && Array.prototype.indexOf.call(event.dataTransfer.types, "Files") >= 0) {
          event.preventDefault();
          editor.stage.classList.add("is-drop-target");
        }
      });
      this.stage.addEventListener("dragleave", function () { editor.stage.classList.remove("is-drop-target"); });
      this.stage.addEventListener("drop", function (event) {
        editor.stage.classList.remove("is-drop-target");
        var files = Array.prototype.slice.call(event.dataTransfer ? event.dataTransfer.files : []);
        if (!files.length) return;
        event.preventDefault();
        editor.addImages(files, editor.scene.toWorld(event.clientX, event.clientY));
      });
      window.addEventListener("beforeunload", function (event) {
        if (editor.hasUnsaved()) { editor.save(); event.preventDefault(); event.returnValue = ""; }
      });
    }
    document.addEventListener("visibilitychange", function () {
      if (document.hidden) { editor.flush(); return; }
      window.clearTimeout(editor.syncTimer);
      editor.syncTimer = window.setTimeout(function () { editor.sync(); }, 200);
    });
  };

  BW.onReady(function () {
    var app = document.getElementById("canvas-app");
    if (app && BW.Canvas) BW.canvasEditor = new Editor(app);
  });
})();
