/* Canvas export to SVG and PNG, made in the browser from what the scene shows.
 *
 * BW.Canvas.exportImage(scene, format, filename) builds a standalone SVG from
 * the nodes and edges (colours and fonts are read from the rendered elements,
 * so the image matches the current theme), inlines images from this wiki's
 * upload folder, and downloads it as .svg or, drawn onto a <canvas>, as .png.
 * Text is taken from the rendered nodes with innerText and always escaped.
 *
 * Images hosted elsewhere are loaded straight from their site with CORS
 * (never through this wiki, which must not fetch arbitrary URLs for users).
 * When the site allows it they are inlined like uploads. When it does not,
 * the SVG keeps a link to the image (an SVG viewer loads it itself) and the
 * PNG, which cannot load anything, shows a dashed placeholder box labelled
 * with the image's description and site instead of leaving a silent gap;
 * the promise resolves with the number of such placeholders.
 */
(function () {
  "use strict";

  var Canvas = BW.Canvas;
  var PAD = 40;
  var MAX_PNG_SIDE = 8000;
  var LINE_HEIGHT = 1.4;
  var EXTERNAL_TIMEOUT_MS = 8000;
  var MAX_INLINE_SIDE = 2000;  /* external images are inlined at most this large */
  var INSETS = {  /* body padding as a share of the size, like canvas.css */
    ellipse: [0.18, 0.15], diamond: [0.25, 0.22], hexagon: [0.14, 0.08], parallelogram: [0.17, 0.08]
  };

  function esc(value) {
    return String(value).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function attrs(map) {
    return Object.keys(map).filter(function (key) { return map[key] !== "" && map[key] != null; })
      .map(function (key) { return " " + key + '="' + esc(map[key]) + '"'; }).join("");
  }

  function px(value) { return parseFloat(value) || 0; }

  function visibleColor(value, fallback) {
    return !value || value === "transparent" || /rgba\([^)]*,\s*0\)$/.test(value) ? fallback : value;
  }

  function shapeMarkup(node, x, y, w, h, style) {
    var paint = { fill: style.fill, stroke: style.stroke, "stroke-width": style.strokeWidth };
    if (node.shape === "ellipse") {
      return "<ellipse" + attrs(Object.assign({ cx: x + w / 2, cy: y + h / 2, rx: w / 2, ry: h / 2 }, paint)) + "/>";
    }
    var points = node.type === "text" && Canvas.POLYGONS[node.shape];
    if (points) {
      return "<polygon" + attrs(Object.assign({
        points: points.map(function (p) { return (x + p[0] * w) + "," + (y + p[1] * h); }).join(" ")
      }, paint)) + "/>";
    }
    var radius = node.shape === "pill" ? h / 2 : Math.min(style.radius, w / 2, h / 2);
    return "<rect" + attrs(Object.assign({ x: x, y: y, width: w, height: h, rx: radius }, paint)) + "/>";
  }

  /* An external image as a PNG data URL when its site allows cross-origin use, else "". */
  function corsImageData(url) {
    return new Promise(function (resolve) {
      var image = new Image();
      var timer = window.setTimeout(function () { image.src = ""; resolve(""); }, EXTERNAL_TIMEOUT_MS);
      image.crossOrigin = "anonymous";
      image.referrerPolicy = "no-referrer";
      image.decoding = "async";
      image.onload = function () {
        window.clearTimeout(timer);
        try {
          var scale = Math.min(1, MAX_INLINE_SIDE / Math.max(image.naturalWidth, image.naturalHeight, 1));
          var canvas = document.createElement("canvas");
          canvas.width = Math.max(1, Math.round(image.naturalWidth * scale));
          canvas.height = Math.max(1, Math.round(image.naturalHeight * scale));
          canvas.getContext("2d").drawImage(image, 0, 0, canvas.width, canvas.height);
          resolve(canvas.toDataURL("image/png"));
        } catch (error) {
          resolve("");  /* the site did not allow cross-origin use after all */
        }
      };
      image.onerror = function () { window.clearTimeout(timer); resolve(""); };
      image.src = url;
    });
  }

  function hostOf(url) {
    try { return new URL(url).host; } catch (error) { return ""; }
  }

  function Exporter(scene, format) {
    this.scene = scene;
    this.format = format;
    this.placeholders = 0;
    this.measure = document.createElement("canvas").getContext("2d");
    this.clipCount = 0;
    this.markers = {};
    this.defs = [];
  }

  /* Lines of *text* wrapped to *width* for *font* (code keeps its own lines). */
  Exporter.prototype.wrap = function (text, width, font, keepLines) {
    var ctx = this.measure;
    ctx.font = font;
    var lines = [];
    String(text).replace(/\r/g, "").split("\n").forEach(function (paragraph) {
      if (keepLines || !paragraph) { lines.push(paragraph); return; }
      var line = "";
      paragraph.split(/(\s+)/).forEach(function (word) {
        var next = line + word;
        if (line && ctx.measureText(next).width > width) {
          lines.push(line.replace(/\s+$/, ""));
          line = word.replace(/^\s+/, "");
        } else {
          line = next;
        }
      });
      lines.push(line);
    });
    return lines;
  };

  Exporter.prototype.clip = function (x, y, w, h) {
    var id = "clip" + (++this.clipCount);
    this.defs.push('<clipPath id="' + id + '"><rect' + attrs({ x: x, y: y, width: Math.max(0, w), height: Math.max(0, h) }) +
      "/></clipPath>");
    return id;
  };

  Exporter.prototype.textBlock = function (lines, x, y, w, h, style, extra) {
    var size = style.fontSize;
    var clipId = this.clip(x, y, w, h);
    var out = '<g clip-path="url(#' + clipId + ')">';
    var max = Math.ceil(h / (size * LINE_HEIGHT)) + 1;
    lines.slice(0, max).forEach(function (line, index) {
      if (!line) return;
      out += "<text" + attrs(Object.assign({
        x: extra && extra.center ? x + w / 2 : x, y: y + size + index * size * LINE_HEIGHT,
        "font-size": size, "font-family": style.font, fill: style.color,
        "text-anchor": extra && extra.center ? "middle" : "", "font-weight": extra && extra.bold ? "bold" : "",
        "xml:space": "preserve"
      })) + ">" + esc(line) + "</text>";
    });
    return out + "</g>";
  };

  /* What an <image> of the export points at: a data URL, the external URL (SVG only) or "" (not included). */
  Exporter.prototype.imageData = function (url) {
    var format = this.format;
    if (!/^\/static\/uploads\//.test(url)) {
      return corsImageData(url).then(function (data) { return data || (format === "svg" ? url : ""); });
    }
    return fetch(url, { credentials: "same-origin" }).then(function (response) {
      if (!response.ok) throw new Error("image");
      return response.blob();
    }).then(function (blob) {
      return new Promise(function (resolve) {
        var reader = new FileReader();
        reader.onload = function () { resolve(String(reader.result)); };
        reader.onerror = function () { resolve(""); };
        reader.readAsDataURL(blob);
      });
    }).catch(function () { return ""; });
  };

  Exporter.prototype.node = function (node) {
    var element = this.scene.elements[node.id];
    if (!element) return Promise.resolve("");
    var computed = getComputedStyle(element);
    var size = Canvas.sizeOf(node);
    var x = node.x, y = node.y, w = size.w, h = size.h;
    var style = {
      fill: visibleColor(computed.backgroundColor, "#ffffff"),
      stroke: visibleColor(computed.borderTopColor, "#888888"),
      strokeWidth: px(computed.borderTopWidth) || 1,
      radius: px(computed.borderTopLeftRadius),
      color: computed.color, font: computed.fontFamily, fontSize: px(computed.fontSize) || 13
    };
    var polygon = element.querySelector(".cv-node__shape polygon");
    if (polygon) {
      var outline = getComputedStyle(polygon);
      style.fill = visibleColor(outline.fill, style.fill);
      style.stroke = visibleColor(outline.stroke, style.stroke);
      style.strokeWidth = px(outline.strokeWidth) || 1;
    }
    var out = "<g" + attrs({ opacity: node.opacity && node.opacity < 1 ? node.opacity : "" }) + ">" +
      shapeMarkup(node, x, y, w, h, style);
    var top = y;
    var titleEl = element.querySelector(".cv-node__title");
    var centered = /^(pill|ellipse|diamond|hexagon|parallelogram)$/.test(node.shape || "");
    var inset = INSETS[node.shape] || [0, 0];
    var padX = Math.max(8, inset[0] * w), padY = Math.max(6, inset[1] * h);
    if (titleEl && titleEl.textContent) {
      var titleSize = style.fontSize * LINE_HEIGHT + 10;
      if (!centered) {
        var bar = getComputedStyle(titleEl).backgroundColor;
        out += "<rect" + attrs({ x: x + 1, y: y + 1, width: w - 2, height: titleSize, fill: visibleColor(bar, style.fill) }) + "/>";
        out += "<line" + attrs({ x1: x, y1: y + titleSize, x2: x + w, y2: y + titleSize, stroke: style.stroke }) + "/>";
      } else {
        top = y + padY - 6;
      }
      out += this.textBlock([titleEl.textContent], x + padX, top + 4, w - padX * 2, titleSize - 4, style,
                            { bold: true, center: centered });
      top += titleSize;
    }
    var bodyEl = element.querySelector(".cv-node__body");
    var bodyTop = Math.max(top, y + padY) + (titleEl && !centered ? 4 : 0);
    var bodyH = y + h - padY - bodyTop;
    if (node.type === "image") {
      var src = Canvas.safeImage(node.url || node.image_url);
      if (!src) return Promise.resolve(out + "</g>");
      var exporter = this;
      return this.imageData(src).then(function (href) {
        if (!href && !/^\/static\/uploads\//.test(src)) {
          return out + exporter.placeholder(node, src, x, bodyTop, w, y + h - bodyTop, style) + "</g>";
        }
        if (!href) return out + "</g>";
        return out + "<image" + attrs({ x: x + 1, y: bodyTop, width: w - 2, height: y + h - bodyTop - 1, href: href,
                                        preserveAspectRatio: "xMidYMid meet" }) + "/></g>";
      });
    }
    var code = node.type === "code";
    var text = bodyEl ? (code ? (node.content || "") : bodyEl.innerText || bodyEl.textContent || "") : "";
    if (node.type === "video" && node.url) text = (node.display_text ? "" : BW.t("node.video") + "\n") + node.url;
    var font = style.fontSize + "px " + (code ? "monospace" : style.font);
    var textStyle = code ? Object.assign({}, style, { font: "monospace", fontSize: style.fontSize * 0.92 }) : style;
    var lines = this.wrap(text.trim(), w - padX * 2, font, code);
    if (centered) {
      var height = lines.length * textStyle.fontSize * LINE_HEIGHT;
      bodyTop = Math.max(bodyTop, y + (h - height) / 2 - textStyle.fontSize * 0.2);
    }
    out += this.textBlock(lines, x + padX, bodyTop, w - padX * 2, y + h - padY - bodyTop, textStyle,
                          { center: centered });
    return Promise.resolve(out + "</g>");
  };

  /* A dashed box naming an external image that could not be copied into the export. */
  Exporter.prototype.placeholder = function (node, src, x, y, w, h, style) {
    this.placeholders += 1;
    var inset = 6;
    var lines = [BW.t("canvas.export_placeholder")];
    var label = node.alt || node.display_text || node.label || "";
    if (label) lines.push(label);
    lines.push(hostOf(src));
    var font = style.fontSize + "px " + style.font;
    var wrapped = [];
    lines.forEach(function (line) { wrapped = wrapped.concat(this.wrap(line, w - inset * 4, font)); }, this);
    return "<rect" + attrs({ x: x + inset, y: y + inset, width: Math.max(0, w - inset * 2), height: Math.max(0, h - inset * 2),
                             fill: "none", stroke: style.stroke, "stroke-width": 1, "stroke-dasharray": "6 4" }) + "/>" +
      this.textBlock(wrapped, x + inset * 2, y + inset * 2, w - inset * 4, h - inset * 4, style, { center: true });
  };

  Exporter.prototype.marker = function (color, kind) {
    var key = kind + color;
    if (!this.markers[key]) {
      var id = "arrow" + Object.keys(this.markers).length;
      this.markers[key] = id;
      this.defs.push('<marker id="' + id + '" viewBox="0 0 10 10" refX="' + (kind === "end" ? 9 : 1) +
        '" refY="5" markerWidth="8" markerHeight="8" orient="auto-start-reverse" markerUnits="strokeWidth">' +
        '<path d="M0,0 L10,5 L0,10 z"' + attrs({ fill: color }) + "/></marker>");
    }
    return "url(#" + this.markers[key] + ")";
  };

  Exporter.prototype.edges = function (background) {
    var scene = this.scene;
    var byId = scene.nodeIndex();
    var out = "";
    scene.edges.forEach(function (edge) {
      var from = byId[edge.from], to = byId[edge.to];
      var parts = scene.edgeEls[edge.id];
      if (!from || !to || !parts) return;
      var geometry = Canvas.edgeGeometry(edge, from, to);
      var computed = getComputedStyle(parts.line);
      var color = Canvas.COLOR.test(edge.color || "") ? edge.color : visibleColor(computed.stroke, "#888888");
      var arrow = edge.arrow || "end";
      out += "<path" + attrs({
        d: geometry.d, fill: "none", stroke: color, "stroke-width": 2,
        "stroke-dasharray": edge.style === "dashed" ? "8 6" : edge.style === "dotted" ? "2 5" : "",
        "stroke-linecap": edge.style === "dotted" ? "round" : "",
        "marker-end": arrow === "end" || arrow === "both" ? this.marker(color, "end") : "",
        "marker-start": arrow === "start" || arrow === "both" ? this.marker(color, "start") : ""
      }) + "/>";
      if (edge.label && parts.label) {
        var labelStyle = getComputedStyle(parts.label);
        out += "<text" + attrs({
          x: geometry.lx, y: geometry.ly, "font-size": Number(edge.text_size) || 14, "font-family": labelStyle.fontFamily,
          fill: visibleColor(labelStyle.fill, "#222222"), stroke: background, "stroke-width": 4,
          "paint-order": "stroke", "text-anchor": "middle", "dominant-baseline": "middle"
        }) + ">" + esc(edge.label) + "</text>";
      }
    }, this);
    return out;
  };

  Exporter.prototype.build = function () {
    var scene = this.scene;
    var box = Canvas.bounds(scene.nodes) || { minX: 0, minY: 0, maxX: 200, maxY: 120 };
    var width = Math.ceil(box.maxX - box.minX + PAD * 2), height = Math.ceil(box.maxY - box.minY + PAD * 2);
    var background = visibleColor(getComputedStyle(scene.stage).backgroundColor, "#ffffff");
    var ordered = scene.nodes.slice().sort(Canvas.byLayer);
    var exporter = this;
    return Promise.all(ordered.map(function (node) { return exporter.node(node); })).then(function (nodes) {
      var edges = exporter.edges(background);
      var svgText = '<?xml version="1.0" encoding="UTF-8"?>\n' +
        '<svg xmlns="http://www.w3.org/2000/svg"' + attrs({
          width: width, height: height,
          viewBox: (box.minX - PAD) + " " + (box.minY - PAD) + " " + width + " " + height
        }) + ">" + "<defs>" + exporter.defs.join("") + "</defs>" +
        "<rect" + attrs({ x: box.minX - PAD, y: box.minY - PAD, width: width, height: height, fill: background }) + "/>" +
        edges + nodes.join("") + "</svg>";
      return { svg: svgText, width: width, height: height, placeholders: exporter.placeholders };
    });
  };

  function download(blob, filename) {
    var url = URL.createObjectURL(blob);
    var link = document.createElement("a");
    link.href = url;
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    link.remove();
    window.setTimeout(function () { URL.revokeObjectURL(url); }, 10000);
  }

  function toPng(result) {
    return new Promise(function (resolve, reject) {
      var scale = Math.min(2, MAX_PNG_SIDE / Math.max(result.width, result.height));
      var url = URL.createObjectURL(new Blob([result.svg], { type: "image/svg+xml" }));
      var image = new Image();
      image.onload = function () {
        try {
          var canvas = document.createElement("canvas");
          canvas.width = Math.max(1, Math.round(result.width * scale));
          canvas.height = Math.max(1, Math.round(result.height * scale));
          var ctx = canvas.getContext("2d");
          ctx.scale(scale, scale);
          ctx.drawImage(image, 0, 0, result.width, result.height);
          canvas.toBlob(function (blob) { if (blob) resolve(blob); else reject(new Error("png")); }, "image/png");
        } catch (error) {
          reject(error);
        } finally {
          URL.revokeObjectURL(url);
        }
      };
      image.onerror = function () { URL.revokeObjectURL(url); reject(new Error("svg")); };
      image.src = url;
    });
  }

  /* Download the scene as "svg" or "png"; resolves, once the file was handed to the browser, with the
   * number of external images drawn as placeholders. */
  Canvas.exportImage = function (scene, format, filename) {
    return new Exporter(scene, format).build().then(function (result) {
      if (format === "svg") {
        download(new Blob([result.svg], { type: "image/svg+xml" }), filename + ".svg");
        return result.placeholders;
      }
      return toPng(result).then(function (blob) {
        download(blob, filename + ".png");
        return result.placeholders;
      });
    });
  };
})();
