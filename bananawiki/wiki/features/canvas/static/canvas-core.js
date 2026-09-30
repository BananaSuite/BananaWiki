/* Canvas core: draws a canvas document and lets the viewer pan and zoom.
 *
 * BW.Canvas.Scene(stage, options) renders nodes as HTML boxes inside a
 * transformed "world" element and edges as SVG paths. It is shared by the
 * editor, the read-only history preview and the embeds in wiki pages.
 *
 * Safety: user-written fields are only ever set with textContent or as
 * validated attributes. The only HTML inserted is what the server rendered
 * for text and code nodes (Markdown and highlighting, sanitised there) and
 * received in the "rendered" map of a response.
 */
(function () {
  "use strict";

  var BW = window.BW = window.BW || {};
  if (BW.Canvas) return;

  var SVG_NS = "http://www.w3.org/2000/svg";
  var MIN_ZOOM = 0.15;
  var MAX_ZOOM = 3;
  var DEFAULT_SIZES = {
    text: [240, 120], code: [320, 200], image: [320, 240], video: [360, 280],
    wiki_page: [280, 200], external_link: [240, 100]
  };
  var TYPE_KEYS = {
    text: "node.text", code: "node.code", image: "node.image", video: "node.video",
    wiki_page: "node.wiki_page", external_link: "node.external_link"
  };
  var COLOR = /^#[0-9a-fA-F]{6}$/;
  /* Outlines of the angular shapes as fractions of the node box (drawn with SVG so the border follows them). */
  var POLYGONS = {
    diamond: [[0.5, 0], [1, 0.5], [0.5, 1], [0, 0.5]],
    parallelogram: [[0.15, 0], [1, 0], [0.85, 1], [0, 1]],
    hexagon: [[0.12, 0], [0.88, 0], [1, 0.5], [0.88, 1], [0.12, 1], [0, 0.5]]
  };
  var sceneCounter = 0;

  function clamp(value, low, high) { return Math.min(high, Math.max(low, value)); }

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = text;
    return node;
  }

  function svg(tag, attrs) {
    var node = document.createElementNS(SVG_NS, tag);
    Object.keys(attrs || {}).forEach(function (name) { node.setAttribute(name, attrs[name]); });
    return node;
  }

  /* A link target, or "" for schemes other than http(s)/mailto (control characters removed first). */
  function safeHref(value) {
    var raw = String(value == null ? "" : value).trim();
    var compact = raw.replace(/[\u0000- \u007f]/g, "");
    if (!compact) return "";
    if (compact.charAt(0) === "/" && compact.charAt(1) !== "/") return raw;
    var scheme = /^([a-z][a-z0-9+.\-]*):/i.exec(compact);
    return scheme && /^(https?|mailto)$/i.test(scheme[1]) ? raw : "";
  }

  function safeImage(value) {
    var url = String(value || "").trim();
    if (/^\/static\/uploads\/[A-Za-z0-9_\-.]+$/.test(url)) return url;
    return /^https?:\/\//i.test(url) ? url : "";
  }

  function sourceKey(node) {
    if (node.type === "text") return node.content || "";
    if (node.type === "code") return (node.language || "") + "\n" + (node.content || "");
    if (node.type === "video") return node.url || "";
    return null;
  }

  function sizeOf(node) {
    var defaults = DEFAULT_SIZES[node.type] || DEFAULT_SIZES.text;
    return { w: Number(node.width) || defaults[0], h: Number(node.height) || defaults[1] };
  }

  function byLayer(a, b) {
    return (a.layer || 0) - (b.layer || 0) || String(a.id).localeCompare(String(b.id));
  }

  /* ── Scene ──────────────────────────────────────────────────────────────── */

  function Scene(stage, options) {
    this.options = options || {};
    this.stage = stage;
    this.id = "cv" + (++sceneCounter);
    this.nodes = [];
    this.edges = [];
    this.pages = {};
    this.cache = {};
    this.selected = {};
    this.selectedEdge = null;
    this.connectFrom = null;
    this.viewport = { x: 0, y: 0, zoom: 1 };
    this.elements = {};
    this.edgeEls = {};
    this.previewEl = null;
    this.signatures = {};
    this.pointers = {};
    this.build();
    this.bindViewport();
  }

  Scene.prototype.build = function () {
    this.stage.classList.add("cv-stage");
    this.world = el("div", "cv-world");
    this.edgeLayer = svg("svg", { "class": "cv-edges", "aria-hidden": this.options.editable ? "false" : "true" });
    var defs = svg("defs");
    ["end", "start"].forEach(function (kind) {
      var marker = svg("marker", {
        id: this.id + "-arrow-" + kind, viewBox: "0 0 10 10", refX: kind === "end" ? "9" : "1", refY: "5",
        markerWidth: "8", markerHeight: "8", orient: "auto-start-reverse", markerUnits: "strokeWidth"
      });
      marker.appendChild(svg("path", { d: "M0,0 L10,5 L0,10 z", "class": "cv-arrowhead" }));
      defs.appendChild(marker);
    }, this);
    this.edgeLayer.appendChild(defs);
    this.edgeGroup = svg("g");
    this.edgeLayer.appendChild(this.edgeGroup);
    this.nodeLayer = el("div", "cv-nodes");
    this.world.appendChild(this.edgeLayer);
    this.world.appendChild(this.nodeLayer);
    this.stage.appendChild(this.world);
  };

  /* Replace the whole document (and remember server renderings / page previews). */
  Scene.prototype.load = function (data, extra) {
    this.nodes = (data && data.nodes || []).slice();
    this.edges = (data && data.edges || []).slice();
    this.absorb(extra || {});
    this.render();
  };

  Scene.prototype.absorb = function (extra) {
    var cache = this.cache;
    Object.keys(extra.rendered || {}).forEach(function (id) {
      var entry = extra.rendered[id];
      if (entry && entry.type) cache[entry.type + "\u0000" + entry["for"]] = entry;
    });
    var pages = this.pages;
    Object.keys(extra.pages || {}).forEach(function (id) { pages[id] = extra.pages[id]; });
  };

  Scene.prototype.node = function (id) {
    for (var i = 0; i < this.nodes.length; i++) if (this.nodes[i].id === id) return this.nodes[i];
    return null;
  };

  Scene.prototype.edge = function (id) {
    for (var i = 0; i < this.edges.length; i++) if (this.edges[i].id === id) return this.edges[i];
    return null;
  };

  Scene.prototype.renderingFor = function (node) {
    var key = sourceKey(node);
    return key === null ? null : this.cache[node.type + "\u0000" + key] || null;
  };

  /* ── Nodes ──────────────────────────────────────────────────────────────── */

  Scene.prototype.render = function () {
    var seen = {};
    var ordered = this.nodes.slice().sort(byLayer);
    ordered.forEach(function (node, index) {
      seen[node.id] = true;
      var element = this.elements[node.id];
      var rendering = this.renderingFor(node);
      var signature = JSON.stringify(node) + "|" + (rendering ? "r" : "") + "|" +
        (node.page_id && this.pages[node.page_id] ? JSON.stringify(this.pages[node.page_id]) : "");
      if (!element) {
        element = el("div");
        element.dataset.id = node.id;
        this.elements[node.id] = element;
      }
      if (this.signatures[node.id] !== signature) {
        this.fillNode(element, node, rendering);
        this.signatures[node.id] = signature;
      }
      this.placeNode(element, node);
      element.style.zIndex = String(index + 1);
      element.classList.toggle("is-selected", !!this.selected[node.id]);
      element.classList.toggle("is-connect-source", this.connectFrom === node.id);
      element.setAttribute("aria-selected", this.selected[node.id] ? "true" : "false");
      if (element.parentNode !== this.nodeLayer || this.nodeLayer.children[index] !== element) {
        this.nodeLayer.insertBefore(element, this.nodeLayer.children[index] || null);
      }
    }, this);
    Object.keys(this.elements).forEach(function (id) {
      if (!seen[id]) {
        this.elements[id].remove();
        delete this.elements[id];
        delete this.signatures[id];
      }
    }, this);
    this.renderEdges();
    this.applyViewport();
    if (this.onRender) this.onRender();
  };

  Scene.prototype.placeNode = function (element, node) {
    var size = sizeOf(node);
    element.style.left = (Number(node.x) || 0) + "px";
    element.style.top = (Number(node.y) || 0) + "px";
    element.style.width = size.w + "px";
    element.style.height = size.h + "px";
  };

  Scene.prototype.moveNode = function (node) {
    var element = this.elements[node.id];
    if (element) this.placeNode(element, node);
  };

  Scene.prototype.nodeTitle = function (node) {
    if (node.display_text) return node.display_text;
    if (node.type === "wiki_page") {
      var page = node.page_id && this.pages[node.page_id];
      return page ? page.title : "";
    }
    if (node.type === "text" || node.type === "code") return "";
    return node.label || "";
  };

  Scene.prototype.fillNode = function (element, node, rendering) {
    var t = BW.t;
    element.className = "cv-node cv-node--" + node.type + (node.shape ? " cv-shape--" + node.shape : "") +
      (node.locked ? " is-locked" : "");
    element.textContent = "";
    element.style.removeProperty("--cv-color");
    if (COLOR.test(node.color || "")) {
      element.style.setProperty("--cv-color", node.color);
      element.classList.add("cv-node--colored");
    }
    element.style.setProperty("--cv-font", (Number(node.text_size) || 13) + "px");
    element.style.opacity = node.opacity ? String(node.opacity) : "";
    var title = this.nodeTitle(node);
    var label = t(TYPE_KEYS[node.type] || "node.text") + (title ? ": " + title : "") +
      (node.locked ? " (" + t("canvas.locked") + ")" : "");
    element.setAttribute("role", "group");
    element.setAttribute("aria-label", label);
    if (this.options.editable) element.tabIndex = 0;
    var outline = node.type === "text" && POLYGONS[node.shape];
    if (outline) {
      var shape = svg("svg", { "class": "cv-node__shape", viewBox: "0 0 100 100", preserveAspectRatio: "none",
                               "aria-hidden": "true", focusable: "false" });
      shape.appendChild(svg("polygon", {
        points: outline.map(function (p) { return p[0] * 100 + "," + p[1] * 100; }).join(" "),
        "vector-effect": "non-scaling-stroke"
      }));
      element.appendChild(shape);
    }
    if (title && node.type !== "text" || (node.type === "text" && node.display_text)) {
      element.appendChild(el("div", "cv-node__title", title));
    }
    var body = el("div", "cv-node__body");
    this["body_" + (node.type in DEFAULT_SIZES ? node.type : "text")](body, node, rendering);
    element.appendChild(body);
    if (this.options.editable && !node.locked) {
      var handle = el("span", "cv-node__resize");
      handle.setAttribute("aria-hidden", "true");
      element.appendChild(handle);
    }
  };

  Scene.prototype.body_text = function (body, node, rendering) {
    if (rendering && rendering.html !== undefined) {
      body.classList.add("content", "cv-markdown");
      body.innerHTML = rendering.html;  /* sanitised server rendering of this exact content */
    } else {
      body.classList.add("cv-plain");
      body.textContent = node.content || "";
    }
  };

  Scene.prototype.body_code = function (body, node, rendering) {
    if (rendering && rendering.html) {
      body.classList.add("cv-code");
      body.innerHTML = rendering.html;  /* sanitised server highlighting */
    } else {
      var pre = el("pre", "cv-code");
      pre.appendChild(el("code", null, node.content || ""));
      body.appendChild(pre);
    }
  };

  Scene.prototype.body_image = function (body, node) {
    var src = safeImage(node.url || node.image_url);
    if (!src) {
      body.appendChild(el("p", "cv-note", BW.t("canvas.no_image")));
      return;
    }
    var img = el("img", "cv-image");
    img.alt = node.alt || node.display_text || "";
    img.loading = "lazy";
    img.decoding = "async";
    img.referrerPolicy = "no-referrer";
    img.draggable = false;
    img.addEventListener("error", function () {
      body.textContent = "";
      body.appendChild(el("p", "cv-note", BW.t("canvas.image_failed")));
    });
    img.src = src;
    body.appendChild(img);
  };

  Scene.prototype.body_video = function (body, node, rendering) {
    if (rendering && rendering.embed) {
      var frame = el("iframe", "cv-video");
      frame.src = rendering.embed;
      frame.title = node.display_text || BW.t("node.video");
      frame.loading = "lazy";
      frame.setAttribute("allow", "fullscreen; picture-in-picture");
      frame.setAttribute("allowfullscreen", "");
      frame.referrerPolicy = "strict-origin-when-cross-origin";
      body.appendChild(frame);
    } else {
      body.appendChild(el("p", "cv-note", node.url ? BW.t("canvas.video_unsupported") : BW.t("canvas.no_url")));
    }
  };

  Scene.prototype.body_wiki_page = function (body, node) {
    var page = node.page_id && this.pages[node.page_id];
    if (page) {
      var link = el("a", "cv-page-link", page.title);
      link.href = "/page/" + encodeURIComponent(page.slug);
      body.appendChild(link);
      if (page.excerpt) body.appendChild(el("p", "cv-excerpt", page.excerpt));
    } else if (node.deleted) {
      body.appendChild(el("p", "cv-note cv-note--warning", BW.t("canvas.page_deleted")));
    } else if (node.restricted) {
      body.appendChild(el("p", "cv-note", BW.t("canvas.page_restricted")));
    } else {
      body.appendChild(el("p", "cv-note", BW.t("canvas.page_missing")));
    }
  };

  Scene.prototype.body_external_link = function (body, node) {
    var href = safeHref(node.url);
    var text = node.label || node.url || BW.t("canvas.no_url");
    if (href) {
      var link = el("a", "cv-link", text);
      link.href = href;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      body.appendChild(link);
      if (node.label && node.url) body.appendChild(el("p", "cv-url", node.url));
    } else {
      body.appendChild(el("p", "cv-note", text));
    }
  };

  /* ── Edges ──────────────────────────────────────────────────────────────── */

  /* Outward direction of each side of a node box. */
  var SIDE_DIRECTIONS = { top: [0, -1], right: [1, 0], bottom: [0, 1], left: [-1, 0] };

  /* The middle of *side* of *node*. */
  function sidePoint(node, side) {
    var size = sizeOf(node);
    var d = SIDE_DIRECTIONS[side];
    return [node.x + size.w / 2 + d[0] * size.w / 2, node.y + size.h / 2 + d[1] * size.h / 2];
  }

  /* Where an edge leaves *from* and enters *to*: the sides facing each other, unless the edge
   * names a side ("from_side" / "to_side"). c1 / c2 point away from the node at each end. */
  function anchors(from, to, edge) {
    var a = sizeOf(from), b = sizeOf(to);
    var ax = from.x + a.w / 2, ay = from.y + a.h / 2, bx = to.x + b.w / 2, by = to.y + b.h / 2;
    var dx = bx - ax, dy = by - ay;
    var p;
    if (Math.abs(dx) * (a.h + b.h) > Math.abs(dy) * (a.w + b.w)) {
      var right = dx > 0;
      p = { x1: right ? from.x + a.w : from.x, y1: ay, x2: right ? to.x : to.x + b.w, y2: by,
            c1: [right ? 1 : -1, 0], c2: [right ? -1 : 1, 0] };
    } else {
      var down = dy > 0;
      p = { x1: ax, y1: down ? from.y + a.h : from.y, x2: bx, y2: down ? to.y : to.y + b.h,
            c1: [0, down ? 1 : -1], c2: [0, down ? -1 : 1] };
    }
    var start = edge && SIDE_DIRECTIONS[edge.from_side] ? edge.from_side : null;
    var end = edge && SIDE_DIRECTIONS[edge.to_side] ? edge.to_side : null;
    if (start) {
      var s = sidePoint(from, start);
      p.x1 = s[0]; p.y1 = s[1]; p.c1 = SIDE_DIRECTIONS[start];
    }
    if (end) {
      var e = sidePoint(to, end);
      p.x2 = e[0]; p.y2 = e[1]; p.c2 = SIDE_DIRECTIONS[end];
    }
    p.stub = start || end ? 20 : 0;
    return p;
  }

  /* Right-angled path: out of each side by p.stub, then one or two bends. */
  function elbowPath(p) {
    var sx = p.x1 + p.c1[0] * p.stub, sy = p.y1 + p.c1[1] * p.stub;
    var ex = p.x2 + p.c2[0] * p.stub, ey = p.y2 + p.c2[1] * p.stub;
    var startH = p.c1[0] !== 0, endH = p.c2[0] !== 0;
    var points = [[p.x1, p.y1], [sx, sy]];
    if (startH && endH) {
      var mx = (sx + ex) / 2;
      points.push([mx, sy], [mx, ey]);
    } else if (!startH && !endH) {
      var my = (sy + ey) / 2;
      points.push([sx, my], [ex, my]);
    } else if (startH) {
      points.push([ex, sy]);
    } else {
      points.push([sx, ey]);
    }
    points.push([ex, ey], [p.x2, p.y2]);
    /* Repeated points would give the arrowheads no direction. */
    points = points.filter(function (point, index) {
      var before = points[index - 1];
      return !before || before[0] !== point[0] || before[1] !== point[1];
    });
    return "M" + points.map(function (point) { return point[0] + "," + point[1]; }).join(" L");
  }

  /* The path of an edge between two nodes and where its label sits; ends stay on the node sides. */
  function edgeGeometry(edge, from, to) {
    var p = anchors(from, to, edge);
    var route = edge.route || "curved";
    var d;
    if (route === "straight") {
      d = "M" + p.x1 + "," + p.y1 + " L" + p.x2 + "," + p.y2;
    } else if (route === "elbow") {
      d = elbowPath(p);
    } else {
      var bend = Math.max(Math.min(Math.hypot(p.x2 - p.x1, p.y2 - p.y1) * 0.35, 90), p.stub * 2);
      d = "M" + p.x1 + "," + p.y1 + " C" + (p.x1 + p.c1[0] * bend) + "," + (p.y1 + p.c1[1] * bend) + " " +
        (p.x2 + p.c2[0] * bend) + "," + (p.y2 + p.c2[1] * bend) + " " + p.x2 + "," + p.y2;
    }
    return { d: d, x1: p.x1, y1: p.y1, x2: p.x2, y2: p.y2, lx: (p.x1 + p.x2) / 2, ly: (p.y1 + p.y2) / 2 };
  }

  Scene.prototype.nodeIndex = function () {
    var byId = {};
    this.nodes.forEach(function (node) { byId[node.id] = node; });
    return byId;
  };

  Scene.prototype.nodeName = function (node) {
    return this.nodeTitle(node) || BW.t(TYPE_KEYS[node.type] || "node.text");
  };

  Scene.prototype.edgeElement = function (edge) {
    var parts = this.edgeEls[edge.id];
    if (parts) return parts;
    var g = svg("g", { "data-edge-id": edge.id });
    if (this.options.editable) {
      g.setAttribute("tabindex", "0");
      g.setAttribute("role", "button");
    }
    parts = { g: g, hit: svg("path", { "class": "cv-edge__hit" }), line: svg("path"), label: null };
    g.appendChild(parts.hit);
    g.appendChild(parts.line);
    this.edgeEls[edge.id] = parts;
    return parts;
  };

  Scene.prototype.placeEdge = function (parts, edge, from, to) {
    var geometry = edgeGeometry(edge, from, to);
    parts.hit.setAttribute("d", geometry.d);
    parts.line.setAttribute("d", geometry.d);
    if (parts.label) {
      parts.label.setAttribute("x", String(geometry.lx));
      parts.label.setAttribute("y", String(geometry.ly));
    }
  };

  Scene.prototype.drawEdge = function (edge, from, to) {
    var parts = this.edgeElement(edge);
    parts.g.setAttribute("class", "cv-edge" + (this.selectedEdge === edge.id ? " is-selected" : ""));
    if (this.options.editable) {
      parts.g.setAttribute("aria-label", BW.t("canvas.edge_label", { from: this.nodeName(from), to: this.nodeName(to) }) +
        (edge.label ? " (" + edge.label + ")" : ""));
    }
    var line = parts.line;
    line.setAttribute("class", "cv-edge__line cv-edge--" + (edge.style || "solid"));
    line.style.stroke = COLOR.test(edge.color || "") ? edge.color : "";
    var arrow = edge.arrow || "end";
    if (arrow === "end" || arrow === "both") line.setAttribute("marker-end", "url(#" + this.id + "-arrow-end)");
    else line.removeAttribute("marker-end");
    if (arrow === "start" || arrow === "both") line.setAttribute("marker-start", "url(#" + this.id + "-arrow-start)");
    else line.removeAttribute("marker-start");
    if (edge.label) {
      if (!parts.label) {
        parts.label = svg("text", { "class": "cv-edge__label" });
        parts.g.appendChild(parts.label);
      }
      parts.label.setAttribute("font-size", String(Number(edge.text_size) || 14));
      parts.label.textContent = edge.label;
    } else if (parts.label) {
      parts.label.remove();
      parts.label = null;
    }
    this.placeEdge(parts, edge, from, to);
    if (parts.g.parentNode !== this.edgeGroup) this.edgeGroup.appendChild(parts.g);
  };

  /* Draw every edge, or with *movedIds* ({id: true}) only move the edges of those nodes (while dragging). */
  Scene.prototype.renderEdges = function (movedIds) {
    var byId = this.nodeIndex();
    if (movedIds) {
      this.edges.forEach(function (edge) {
        var parts = this.edgeEls[edge.id];
        if (parts && (movedIds[edge.from] || movedIds[edge.to]) && byId[edge.from] && byId[edge.to]) {
          this.placeEdge(parts, edge, byId[edge.from], byId[edge.to]);
        }
      }, this);
      return;
    }
    var seen = {};
    this.edges.forEach(function (edge) {
      var from = byId[edge.from], to = byId[edge.to];
      if (!from || !to) return;
      seen[edge.id] = true;
      this.drawEdge(edge, from, to);
    }, this);
    Object.keys(this.edgeEls).forEach(function (id) {
      if (!seen[id]) {
        this.edgeEls[id].g.remove();
        delete this.edgeEls[id];
      }
    }, this);
    this.renderPreview();
  };

  /* The dashed line that follows the pointer while a connection is being made. */
  Scene.prototype.renderPreview = function () {
    if (!this.preview) {
      if (this.previewEl) { this.previewEl.remove(); this.previewEl = null; }
      return;
    }
    if (!this.previewEl) this.previewEl = svg("line", { "class": "cv-edge__preview" });
    var p = this.preview;
    this.previewEl.setAttribute("x1", p.x1);
    this.previewEl.setAttribute("y1", p.y1);
    this.previewEl.setAttribute("x2", p.x2);
    this.previewEl.setAttribute("y2", p.y2);
    this.edgeGroup.appendChild(this.previewEl);
  };

  /* ── Viewport ───────────────────────────────────────────────────────────── */

  Scene.prototype.applyViewport = function () {
    var v = this.viewport;
    this.world.style.transform = "translate(" + v.x + "px," + v.y + "px) scale(" + v.zoom + ")";
    this.stage.style.setProperty("--cv-zoom", String(v.zoom));
    if (this.options.onViewport) this.options.onViewport(v);
    if (this.onViewportChange) this.onViewportChange(v);
  };

  Scene.prototype.toWorld = function (clientX, clientY) {
    var rect = this.stage.getBoundingClientRect();
    return { x: (clientX - rect.left - this.viewport.x) / this.viewport.zoom,
             y: (clientY - rect.top - this.viewport.y) / this.viewport.zoom };
  };

  Scene.prototype.center = function () {
    var rect = this.stage.getBoundingClientRect();
    return this.toWorld(rect.left + rect.width / 2, rect.top + rect.height / 2);
  };

  Scene.prototype.zoomAt = function (localX, localY, zoom) {
    var v = this.viewport;
    var next = clamp(zoom, MIN_ZOOM, MAX_ZOOM);
    var wx = (localX - v.x) / v.zoom, wy = (localY - v.y) / v.zoom;
    v.zoom = next;
    v.x = localX - wx * next;
    v.y = localY - wy * next;
    this.applyViewport();
  };

  Scene.prototype.zoomBy = function (factor) {
    var rect = this.stage.getBoundingClientRect();
    this.zoomAt(rect.width / 2, rect.height / 2, this.viewport.zoom * factor);
  };

  Scene.prototype.panBy = function (dx, dy) {
    this.viewport.x += dx;
    this.viewport.y += dy;
    this.applyViewport();
  };

  /* Show every node, or only *nodes* when given (zoom to selection). */
  Scene.prototype.fit = function (nodes) {
    nodes = nodes && nodes.length ? nodes : this.nodes;
    var rect = this.stage.getBoundingClientRect();
    if (!nodes.length || !rect.width) {
      this.viewport = { x: rect.width / 2 || 0, y: rect.height / 2 || 0, zoom: 1 };
      this.applyViewport();
      return;
    }
    var box = bounds(nodes);
    var minX = box.minX, minY = box.minY, maxX = box.maxX, maxY = box.maxY;
    var pad = 32;
    var zoom = clamp(Math.min((rect.width - pad * 2) / (maxX - minX), (rect.height - pad * 2) / (maxY - minY)),
                     MIN_ZOOM, 1.25);
    this.viewport = { zoom: zoom, x: (rect.width - (maxX - minX) * zoom) / 2 - minX * zoom,
                      y: (rect.height - (maxY - minY) * zoom) / 2 - minY * zoom };
    this.applyViewport();
  };

  Scene.prototype.isBackground = function (target) {
    return target === this.stage || target === this.world || target === this.nodeLayer ||
      target === this.edgeLayer || target === this.edgeGroup;
  };

  /* Drag the background to pan, pinch with two fingers, wheel to zoom. */
  Scene.prototype.bindViewport = function () {
    var scene = this;
    var stage = this.stage;
    var pan = null;
    var pinch = null;

    stage.addEventListener("pointerdown", function (event) {
      scene.pointers[event.pointerId] = { x: event.clientX, y: event.clientY };
      var ids = Object.keys(scene.pointers);
      if (ids.length === 2) {
        var a = scene.pointers[ids[0]], b = scene.pointers[ids[1]];
        pan = null;
        pinch = { dist: Math.hypot(a.x - b.x, a.y - b.y) || 1, zoom: scene.viewport.zoom };
        if (scene.options.onGestureStart) scene.options.onGestureStart();
        return;
      }
      if (event.button !== 0 || !scene.isBackground(event.target)) return;
      if (scene.options.onBackgroundDown && scene.options.onBackgroundDown(event)) return;
      pan = { x: event.clientX, y: event.clientY, moved: false };
      stage.setPointerCapture(event.pointerId);
      stage.classList.add("is-panning");
    });

    stage.addEventListener("pointermove", function (event) {
      if (!scene.pointers[event.pointerId]) return;
      scene.pointers[event.pointerId] = { x: event.clientX, y: event.clientY };
      if (pinch) {
        var ids = Object.keys(scene.pointers);
        if (ids.length < 2) return;
        var a = scene.pointers[ids[0]], b = scene.pointers[ids[1]];
        var rect = stage.getBoundingClientRect();
        scene.zoomAt((a.x + b.x) / 2 - rect.left, (a.y + b.y) / 2 - rect.top,
                     pinch.zoom * Math.hypot(a.x - b.x, a.y - b.y) / pinch.dist);
        return;
      }
      if (!pan) return;
      scene.panBy(event.clientX - pan.x, event.clientY - pan.y);
      if (Math.abs(event.clientX - pan.x) + Math.abs(event.clientY - pan.y) > 0) pan.moved = true;
      pan.x = event.clientX;
      pan.y = event.clientY;
    });

    function end(event) {
      delete scene.pointers[event.pointerId];
      if (Object.keys(scene.pointers).length < 2) pinch = null;
      if (pan) {
        var moved = pan.moved;
        pan = null;
        stage.classList.remove("is-panning");
        if (!moved && scene.options.onBackgroundClick) scene.options.onBackgroundClick(event);
        if (moved && scene.options.onViewportDone) scene.options.onViewportDone();
      }
    }
    stage.addEventListener("pointerup", end);
    stage.addEventListener("pointercancel", end);

    stage.addEventListener("wheel", function (event) {
      if (scene.options.wheelNeedsModifier && !event.ctrlKey && !event.metaKey) return;
      event.preventDefault();
      var rect = stage.getBoundingClientRect();
      if (event.ctrlKey || event.metaKey || !scene.options.wheelPans) {
        scene.zoomAt(event.clientX - rect.left, event.clientY - rect.top,
                     scene.viewport.zoom * Math.exp(-event.deltaY * 0.0015));
      } else {
        scene.panBy(-event.deltaX, -event.deltaY);
      }
      if (scene.options.onViewportDone) scene.options.onViewportDone();
    }, { passive: false });

    stage.addEventListener("keydown", function (event) {
      if (event.target !== stage) return;
      if (scene.handleViewKey(event)) event.preventDefault();
    });
  };

  /* Keys that move the view; returns true when handled. */
  Scene.prototype.handleViewKey = function (event) {
    var step = event.shiftKey ? 200 : 60;
    switch (event.key) {
      case "ArrowLeft": this.panBy(step, 0); break;
      case "ArrowRight": this.panBy(-step, 0); break;
      case "ArrowUp": this.panBy(0, step); break;
      case "ArrowDown": this.panBy(0, -step); break;
      case "+": case "=": this.zoomBy(1.2); break;
      case "-": case "_": this.zoomBy(1 / 1.2); break;
      case "0": this.fit(); break;
      default: return false;
    }
    if (this.options.onViewportDone) this.options.onViewportDone();
    return true;
  };

  /* The box around *nodes* ({minX, minY, maxX, maxY}), or null when there are none. */
  function bounds(nodes) {
    if (!nodes.length) return null;
    var box = { minX: Infinity, minY: Infinity, maxX: -Infinity, maxY: -Infinity };
    nodes.forEach(function (node) {
      var size = sizeOf(node);
      box.minX = Math.min(box.minX, node.x); box.minY = Math.min(box.minY, node.y);
      box.maxX = Math.max(box.maxX, node.x + size.w); box.maxY = Math.max(box.maxY, node.y + size.h);
    });
    return box;
  }

  BW.Canvas = {
    Scene: Scene,
    bounds: bounds,
    edgeGeometry: edgeGeometry,
    SIDES: Object.keys(SIDE_DIRECTIONS),
    byLayer: byLayer,
    svg: svg,
    COLOR: COLOR,
    POLYGONS: POLYGONS,
    DEFAULT_SIZES: DEFAULT_SIZES,
    safeHref: safeHref,
    safeImage: safeImage,
    sizeOf: sizeOf,
    sourceKey: sourceKey,
    clamp: clamp,
    el: el
  };
})();
