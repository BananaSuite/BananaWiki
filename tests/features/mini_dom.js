/* A small stand-in for the browser's DOM, enough to run BananaWiki's page scripts under node
 * (tests/features/test_content_ids_script.py). It parses the HTML the server renders, keeps elements
 * in tree order and answers the lookups the scripts make as a browser does: getElementById returns
 * the first element with the id, querySelector(All) understands the simple selectors the scripts use
 * (tag, #id, .class, [attr], [attr=value], :checked, :not(...), descendant and child combinators),
 * and window answers to the ids of the page (named access) until a script sets the name itself.
 * Simplified: no layout (sizes are 0), no default actions (a click on a label or a submit button does
 * nothing more), and a select given a value none of its options has keeps showing its first option. */
"use strict";

const VOID = new Set(["area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"]);
const RAW_TEXT = new Set(["script", "style", "textarea", "title"]);
const FORM_CONTROLS = new Set(["input", "select", "textarea", "button", "fieldset", "option"]);
const ENTITIES = { amp: "&", lt: "<", gt: ">", quot: '"', apos: "'", nbsp: " " };

function decode(text) {
  return text.replace(/&(#x[0-9a-f]+|#\d+|[a-z]+);/gi, function (whole, name) {
    if (name[0] === "#") {
      const code = name[1] === "x" || name[1] === "X" ? parseInt(name.slice(2), 16) : parseInt(name.slice(1), 10);
      return String.fromCodePoint(code);
    }
    return Object.prototype.hasOwnProperty.call(ENTITIES, name) ? ENTITIES[name] : whole;
  });
}

function camel(name) { return name.replace(/-([a-z])/g, function (m, c) { return c.toUpperCase(); }); }
function kebab(name) { return name.replace(/[A-Z]/g, function (c) { return "-" + c.toLowerCase(); }); }

class Text {
  constructor(document, data) { this.ownerDocument = document; this.nodeType = 3; this.data = data; this.parentNode = null; }
  get textContent() { return this.data; }
  set textContent(value) { this.data = String(value); }
  remove() { if (this.parentNode) this.parentNode.removeChild(this); }
}

class Event {
  constructor(type, init) {
    this.type = type;
    this.bubbles = !!(init && init.bubbles);
    this.detail = init && init.detail;
    this.defaultPrevented = false;
    this.propagationStopped = false;
    Object.assign(this, (init && init.props) || {});
  }
  preventDefault() { this.defaultPrevented = true; }
  stopPropagation() { this.propagationStopped = true; }
  stopImmediatePropagation() { this.propagationStopped = true; }
}

class EventTargetish {
  constructor() { this.listeners = {}; }
  addEventListener(type, fn, options) {
    const capture = options === true || !!(options && options.capture);
    (this.listeners[type] = this.listeners[type] || []).push({ fn: fn, capture: capture });
  }
  removeEventListener(type, fn) {
    this.listeners[type] = (this.listeners[type] || []).filter(function (entry) { return entry.fn !== fn; });
  }
  listenerCount(type) { return (this.listeners[type] || []).length; }
}

class Element extends EventTargetish {
  constructor(document, tag) {
    super();
    this.ownerDocument = document;
    this.nodeType = 1;
    this.localName = tag.toLowerCase();
    this.tagName = this.localName.toUpperCase();
    this.attrs = new Map();
    this.childNodes = [];
    this.parentNode = null;
    const self = this;
    this.dataset = new Proxy({}, {
      get(target, name) { return typeof name === "string" ? (self.getAttribute("data-" + kebab(name)) === null ? undefined : self.getAttribute("data-" + kebab(name))) : undefined; },
      set(target, name, value) { self.setAttribute("data-" + kebab(name), String(value)); return true; },
      has(target, name) { return self.hasAttribute("data-" + kebab(name)); },
      deleteProperty(target, name) { self.removeAttribute("data-" + kebab(name)); return true; },
    });
    this.classList = {
      add() { const set = self.classes(); for (const name of arguments) set.add(name); self.setClasses(set); },
      remove() { const set = self.classes(); for (const name of arguments) set.delete(name); self.setClasses(set); },
      contains(name) { return self.classes().has(name); },
      toggle(name, force) {
        const set = self.classes();
        const on = force === undefined ? !set.has(name) : !!force;
        if (on) set.add(name); else set.delete(name);
        self.setClasses(set);
        return on;
      },
    };
    if (FORM_CONTROLS.has(this.localName) || this.localName === "dialog" || this.localName === "details" ||
        this.localName === "label" || this.localName === "form") {
      installControl(this);
    }
  }

  // Attributes
  getAttribute(name) { name = name.toLowerCase(); return this.attrs.has(name) ? this.attrs.get(name) : null; }
  setAttribute(name, value) { this.attrs.set(name.toLowerCase(), String(value)); }
  removeAttribute(name) { this.attrs.delete(name.toLowerCase()); }
  hasAttribute(name) { return this.attrs.has(name.toLowerCase()); }
  toggleAttribute(name, force) {
    const on = force === undefined ? !this.hasAttribute(name) : !!force;
    if (on) this.setAttribute(name, ""); else this.removeAttribute(name);
    return on;
  }
  classes() { return new Set((this.getAttribute("class") || "").split(/\s+/).filter(Boolean)); }
  setClasses(set) { this.setAttribute("class", Array.from(set).join(" ")); }
  get id() { return this.getAttribute("id") || ""; }
  set id(value) { this.setAttribute("id", value); }
  get className() { return this.getAttribute("class") || ""; }
  set className(value) { this.setAttribute("class", value); }
  get hidden() { return this.hasAttribute("hidden"); }
  set hidden(value) { this.toggleAttribute("hidden", !!value); }
  get title() { return this.getAttribute("title") || ""; }
  set title(value) { this.setAttribute("title", value); }
  get tabIndex() { return Number(this.getAttribute("tabindex") || -1); }
  set tabIndex(value) { this.setAttribute("tabindex", value); }

  // Tree
  get children() { return this.childNodes.filter(function (node) { return node.nodeType === 1; }); }
  get firstChild() { return this.childNodes[0] || null; }
  get firstElementChild() { return this.children[0] || null; }
  get lastElementChild() { const kids = this.children; return kids[kids.length - 1] || null; }
  get parentElement() { return this.parentNode && this.parentNode.nodeType === 1 ? this.parentNode : null; }
  sibling(step, elementsOnly) {
    if (!this.parentNode) return null;
    const list = elementsOnly ? this.parentNode.children : this.parentNode.childNodes;
    return list[list.indexOf(this) + step] || null;
  }
  get nextSibling() { return this.sibling(1, false); }
  get nextElementSibling() { return this.sibling(1, true); }
  get previousElementSibling() { return this.sibling(-1, true); }
  appendChild(node) { return this.insertBefore(node, null); }
  append() { for (const node of arguments) this.appendChild(typeof node === "string" ? new Text(this.ownerDocument, node) : node); }
  prepend() {
    const first = this.firstChild;
    for (const node of arguments) this.insertBefore(typeof node === "string" ? new Text(this.ownerDocument, node) : node, first);
  }
  insertBefore(node, reference) {
    if (node.isFragment) {
      for (const child of node.childNodes.slice()) this.insertBefore(child, reference);
      return node;
    }
    if (node.parentNode) node.parentNode.removeChild(node);
    const index = reference ? this.childNodes.indexOf(reference) : -1;
    if (index < 0) this.childNodes.push(node); else this.childNodes.splice(index, 0, node);
    node.parentNode = this;
    return node;
  }
  removeChild(node) {
    const index = this.childNodes.indexOf(node);
    if (index >= 0) this.childNodes.splice(index, 1);
    node.parentNode = null;
    return node;
  }
  replaceChildren() {
    for (const child of this.childNodes.slice()) this.removeChild(child);
    for (const node of arguments) this.appendChild(node);
  }
  replaceWith(node) { if (this.parentNode) { this.parentNode.insertBefore(node, this); this.remove(); } }
  remove() { if (this.parentNode) this.parentNode.removeChild(this); }
  contains(node) { for (let n = node; n; n = n.parentNode) if (n === this) return true; return false; }
  get textContent() { return this.childNodes.map(function (node) { return node.textContent; }).join(""); }
  set textContent(value) {
    this.replaceChildren();
    if (value !== "" && value !== null && value !== undefined) this.appendChild(new Text(this.ownerDocument, String(value)));
  }
  set innerHTML(html) { this.replaceChildren(); parseInto(this, String(html)); }
  get innerHTML() { return this.childNodes.map(serialize).join(""); }
  get outerHTML() { return serialize(this); }

  // Selectors
  *descendants() {
    for (const child of this.childNodes) {
      if (child.nodeType !== 1) continue;
      yield child;
      yield* child.descendants();
    }
  }
  querySelectorAll(selector) {
    const groups = parseSelector(selector);
    const found = [];
    for (const node of this.descendants()) if (groups.some(function (g) { return matchComplex(node, g); })) found.push(node);
    return found;
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  matches(selector) { const self = this; return parseSelector(selector).some(function (g) { return matchComplex(self, g); }); }
  closest(selector) { for (let n = this; n && n.nodeType === 1; n = n.parentNode) if (n.matches(selector)) return n; return null; }

  // Events and focus
  dispatchEvent(event) {
    event.target = event.target || this;
    const path = [];
    for (let n = this; n; n = n.parentNode) path.push(n);  // up to the document, when attached
    for (const node of path.slice().reverse()) fire(node, event, true);
    for (const node of path) {
      if (event.propagationStopped) break;
      fire(node, event, false);
      if (!event.bubbles) break;
    }
    return !event.defaultPrevented;
  }
  click() { this.dispatchEvent(new Event("click", { bubbles: true })); }
  focus() { this.ownerDocument.activeElement = this; }
  blur() { if (this.ownerDocument.activeElement === this) this.ownerDocument.activeElement = this.ownerDocument.body; }
  getBoundingClientRect() { return { top: 0, left: 0, right: 0, bottom: 0, width: 0, height: 0 }; }
  scrollIntoView() {}
  get offsetWidth() { return 0; }
  get offsetHeight() { return 0; }
  get clientWidth() { return 0; }
  get clientHeight() { return 0; }
  get scrollWidth() { return 0; }
  get scrollHeight() { return 0; }
}

// What listeners threw: a browser reports it and goes on with the next listener, so does this.
const errors = [];

function fire(node, event, capture) {
  const entries = (node.listeners && node.listeners[event.type]) || [];
  for (const entry of entries.slice()) {
    if (entry.capture !== capture) continue;
    event.currentTarget = node;
    try { entry.fn.call(node, event); } catch (error) { errors.push(String((error && error.stack) || error)); }
  }
}

// Properties of form controls, dialogs and labels (only those elements have them, as in a browser:
// on a heading, `.value` or `.checked` is an ordinary property that nothing reads back).
function installControl(element) {
  const tag = element.localName;
  let value = null;
  let checked = null;
  const bool = function (name, attribute) {
    Object.defineProperty(element, name, {
      get() { return element.hasAttribute(attribute); },
      set(on) { element.toggleAttribute(attribute, !!on); },
      configurable: true,
    });
  };
  bool("disabled", "disabled");
  bool("required", "required");
  bool("readOnly", "readonly");
  bool("multiple", "multiple");
  bool("open", "open");
  Object.defineProperty(element, "type", {
    get() { return element.getAttribute("type") || (tag === "input" ? "text" : tag === "button" ? "submit" : tag); },
    set(v) { element.setAttribute("type", v); },
    configurable: true,
  });
  for (const name of ["name", "placeholder", "accept"]) {
    Object.defineProperty(element, name, { get() { return element.getAttribute(name) || ""; }, set(v) { element.setAttribute(name, v); }, configurable: true });
  }
  Object.defineProperty(element, "maxLength", { get() { return Number(element.getAttribute("maxlength") || -1); }, set(v) { element.setAttribute("maxlength", v); }, configurable: true });
  if (tag === "input" || tag === "textarea") {
    Object.defineProperty(element, "value", {
      get() {
        if (value !== null) return value;
        if (tag === "textarea") return element.textContent;
        const attr = element.getAttribute("value");
        return attr !== null ? attr : (element.type === "checkbox" || element.type === "radio" ? "on" : "");
      },
      set(v) { value = v === null || v === undefined ? "" : String(v); },
      configurable: true,
    });
  }
  if (tag === "input") {
    Object.defineProperty(element, "checked", {
      get() { return checked !== null ? checked : element.hasAttribute("checked"); },
      set(v) { checked = !!v; },
      configurable: true,
    });
    element.files = [];
    element.select = function () {};
    element.setRangeText = function () {};
  }
  if (tag === "option") {
    let selected = null;
    Object.defineProperty(element, "value", {
      get() { const attr = element.getAttribute("value"); return attr !== null ? attr : element.textContent; },
      set(v) { element.setAttribute("value", v); },
      configurable: true,
    });
    Object.defineProperty(element, "selected", {
      get() { return selected !== null ? selected : element.hasAttribute("selected"); },
      set(v) { selected = !!v; },
      configurable: true,
    });
  }
  if (tag === "select") {
    Object.defineProperty(element, "options", { get() { return element.querySelectorAll("option"); }, configurable: true });
    Object.defineProperty(element, "value", {
      get() {
        const options = element.options;
        const chosen = options.filter(function (o) { return o.selected; });
        const option = chosen[chosen.length - 1] || options[0];
        return option ? option.value : "";
      },
      set(v) { element.options.forEach(function (o) { o.selected = o.value === String(v); }); },
      configurable: true,
    });
  }
  if (tag === "dialog") {
    element.showModal = function () { element.open = true; element.ownerDocument.modal = element; };
    element.show = element.showModal;
    element.close = function () {
      if (!element.open) return;
      element.open = false;
      element.dispatchEvent(new Event("close"));
    };
  }
  if (tag === "label") {
    Object.defineProperty(element, "htmlFor", { get() { return element.getAttribute("for") || ""; }, set(v) { element.setAttribute("for", v); }, configurable: true });
    // The labelled control, by the HTML rule: with for=, the first element of the page with that id
    // (none when that one cannot be labelled); without it, the first form control inside the label.
    Object.defineProperty(element, "control", {
      get() {
        const labelable = function (node) { return node && ["input", "select", "textarea", "button"].indexOf(node.localName) >= 0; };
        if (element.hasAttribute("for")) {
          const target = element.ownerDocument.getElementById(element.getAttribute("for"));
          return labelable(target) ? target : null;
        }
        for (const node of element.descendants()) if (labelable(node)) return node;
        return null;
      },
      configurable: true,
    });
  }
  if (tag === "form") {
    Object.defineProperty(element, "elements", {
      get() {
        const named = {};
        for (const node of element.descendants()) {
          if (["input", "select", "textarea", "button"].indexOf(node.localName) < 0) continue;
          for (const key of [node.getAttribute("name"), node.getAttribute("id")]) if (key && !(key in named)) named[key] = node;
        }
        return named;
      },
      configurable: true,
    });
    element.reset = function () {};
  }
}

// Selectors

function parseSelector(text) {
  return text.split(",").map(function (part) {
    const steps = [];
    const tokens = part.trim().replace(/\s*>\s*/g, " > ").split(/\s+/);
    let combinator = " ";
    for (const token of tokens) {
      if (token === ">") { combinator = ">"; continue; }
      steps.push({ combinator: combinator, compound: parseCompound(token) });
      combinator = " ";
    }
    return steps;
  });
}

function parseCompound(text) {
  const parts = [];
  const pattern = /^(\*|[a-zA-Z][\w-]*)|#((?:\\.|[\w-])+)|\.([\w-]+)|\[([\w-]+)(?:([~|^$*]?=)(?:"([^"]*)"|'([^']*)'|([^\]]*)))?\]|:not\(([^)]*)\)|:([\w-]+)/g;
  let match;
  let consumed = 0;
  while ((match = pattern.exec(text))) {
    if (match.index !== consumed) throw new Error("mini_dom: unsupported selector " + text);
    consumed = pattern.lastIndex;
    if (match[1]) parts.push({ kind: "tag", value: match[1].toLowerCase() });
    else if (match[2]) parts.push({ kind: "id", value: match[2].replace(/\\(.)/g, "$1") });
    else if (match[3]) parts.push({ kind: "class", value: match[3] });
    else if (match[4]) parts.push({ kind: "attr", name: match[4], op: match[5], value: match[6] !== undefined ? match[6] : match[7] !== undefined ? match[7] : match[8] });
    else if (match[9] !== undefined) parts.push({ kind: "not", value: parseCompound(match[9]) });
    else if (match[10]) parts.push({ kind: "pseudo", value: match[10] });
  }
  if (consumed !== text.length) throw new Error("mini_dom: unsupported selector " + text);
  return parts;
}

function matchCompound(node, parts) {
  return parts.every(function (part) {
    switch (part.kind) {
      case "tag": return part.value === "*" || node.localName === part.value;
      case "id": return node.getAttribute("id") === part.value;
      case "class": return node.classes().has(part.value);
      case "attr": {
        const value = node.getAttribute(part.name);
        if (value === null) return false;
        if (!part.op) return true;
        if (part.op === "=") return value === part.value;
        if (part.op === "~=") return value.split(/\s+/).indexOf(part.value) >= 0;
        if (part.op === "^=") return value.indexOf(part.value) === 0;
        throw new Error("mini_dom: unsupported attribute operator " + part.op);
      }
      case "not": return !matchCompound(node, part.value);
      case "pseudo":
        if (part.value === "checked") return node.localName === "input" && node.checked;
        if (part.value === "focus-visible" || part.value === "focus") return node.ownerDocument.activeElement === node;
        throw new Error("mini_dom: unsupported pseudo-class :" + part.value);
      default: return false;
    }
  });
}

function matchComplex(node, steps) {
  let index = steps.length - 1;
  if (!matchCompound(node, steps[index].compound)) return false;
  let current = node;
  while (index > 0) {
    const combinator = steps[index].combinator;
    index -= 1;
    if (combinator === ">") {
      current = current.parentElement;
      if (!current || !matchCompound(current, steps[index].compound)) return false;
    } else {
      current = current.parentElement;
      while (current && !matchCompound(current, steps[index].compound)) current = current.parentElement;
      if (!current) return false;
    }
  }
  return true;
}

// HTML

function parseAttributes(text) {
  const attrs = [];
  const pattern = /([^\s=\/>"']+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+)))?/g;
  let match;
  while ((match = pattern.exec(text))) {
    const value = match[2] !== undefined ? match[2] : match[3] !== undefined ? match[3] : match[4] !== undefined ? match[4] : "";
    attrs.push([match[1].toLowerCase(), decode(value)]);
  }
  return attrs;
}

function parseInto(parent, html) {
  const document = parent.ownerDocument;
  const stack = [parent];
  const top = function () { return stack[stack.length - 1]; };
  let i = 0;
  while (i < html.length) {
    if (html.startsWith("<!--", i)) { const end = html.indexOf("-->", i); i = end < 0 ? html.length : end + 3; continue; }
    if (html.startsWith("<!", i)) { const end = html.indexOf(">", i); i = end < 0 ? html.length : end + 1; continue; }
    if (html[i] === "<" && html[i + 1] === "/") {
      const end = html.indexOf(">", i);
      const name = html.slice(i + 2, end).trim().toLowerCase();
      for (let k = stack.length - 1; k > 0; k--) {
        if (stack[k].localName === name) { stack.length = k; break; }
      }
      i = end + 1;
      continue;
    }
    if (html[i] === "<" && /[a-zA-Z]/.test(html[i + 1] || "")) {
      // The end of the tag: the first ">" outside a quoted attribute value.
      let j = i + 1, quote = null;
      for (; j < html.length; j++) {
        const c = html[j];
        if (quote) { if (c === quote) quote = null; } else if (c === '"' || c === "'") quote = c; else if (c === ">") break;
      }
      const inside = html.slice(i + 1, j);
      const name = /^[a-zA-Z][\w-]*/.exec(inside)[0].toLowerCase();
      const selfClosing = /\/\s*$/.test(inside);
      const element = document.createElement(name);
      for (const [key, value] of parseAttributes(inside.slice(name.length).replace(/\/\s*$/, ""))) element.attrs.set(key, value);
      top().appendChild(element);
      i = j + 1;
      if (RAW_TEXT.has(name)) {
        const close = html.toLowerCase().indexOf("</" + name, i);
        const text = html.slice(i, close < 0 ? html.length : close);
        if (text) element.appendChild(new Text(document, name === "textarea" || name === "title" ? decode(text) : text));
        i = close < 0 ? html.length : html.indexOf(">", close) + 1;
      } else if (!VOID.has(name) && !selfClosing) {
        stack.push(element);
      }
      continue;
    }
    const next = html.indexOf("<", i + 1);
    const end = next < 0 ? html.length : next;
    top().appendChild(new Text(document, decode(html.slice(i, end))));
    i = end;
  }
}

function serialize(node) {
  if (node.nodeType === 3) return node.data.replace(/&/g, "&amp;").replace(/</g, "&lt;");
  const attrs = Array.from(node.attrs).map(function ([k, v]) { return " " + k + '="' + v.replace(/"/g, "&quot;") + '"'; }).join("");
  if (VOID.has(node.localName)) return "<" + node.localName + attrs + ">";
  return "<" + node.localName + attrs + ">" + node.childNodes.map(serialize).join("") + "</" + node.localName + ">";
}

// Document and window

class Document extends EventTargetish {
  constructor(html) {
    super();
    this.nodeType = 9;
    this.readyState = "complete";
    this.hidden = false;
    this.documentElement = this.createElement("html");
    this.documentElement.parentNode = this;
    this.childNodes = [this.documentElement];
    parseInto(this.documentElement, html || "");
    // A full page brings its own <html>: use it as the root.
    const own = this.documentElement.children.filter(function (n) { return n.localName === "html"; })[0];
    if (own) { this.documentElement = own; own.parentNode = this; this.childNodes = [own]; }
    this.head = this.documentElement.querySelector("head") || this.documentElement;
    this.body = this.documentElement.querySelector("body") || this.documentElement;
    this.activeElement = this.body;
  }
  createElement(tag) { return new Element(this, tag); }
  createTextNode(text) { return new Text(this, String(text)); }
  createDocumentFragment() { const fragment = new Element(this, "fragment"); fragment.isFragment = true; return fragment; }
  getElementById(id) {
    for (const node of this.documentElement.descendants()) if (node.getAttribute("id") === id) return node;
    return null;
  }
  querySelectorAll(selector) {
    const all = [this.documentElement].concat(Array.from(this.documentElement.descendants()));
    const groups = parseSelector(selector);
    return all.filter(function (node) { return groups.some(function (g) { return matchComplex(node, g); }); });
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  dispatchEvent(event) { event.target = event.target || this; fire(this, event, true); fire(this, event, false); return !event.defaultPrevented; }
}

// A window whose prototype holds the page's ids, as a browser's does (named access): `window.someId`
// is the element until a script gives window a property of that name.
function makeWindow(document, extra) {
  const named = {};
  for (const node of document.documentElement.descendants()) {
    const id = node.getAttribute("id");
    if (id && !(id in named)) named[id] = node;
  }
  const window = Object.create(named);
  Object.assign(window, extra || {});
  return window;
}

function cssEscape(value) {
  return String(value).replace(/[^a-zA-Z0-9_-]/g, function (c) { return "\\" + c; });
}

module.exports = { Document, Element, Event, makeWindow, cssEscape, errors };
