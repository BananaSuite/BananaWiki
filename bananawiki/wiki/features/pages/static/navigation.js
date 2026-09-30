/* Sidebar and navigation page: live search, "more pages" loading and reordering. */
(function () {
  "use strict";
  if (window.BWPagesNav) return;
  window.BWPagesNav = true;

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  // Live search -------------------------------------------------------------
  function initSearch(form) {
    var input = form.querySelector('input[name="q"]');
    var content = form.querySelector("[data-search-content]");
    var box = form.querySelector("[data-search-results]");
    var timer = null, controller = null;
    function hide() { box.hidden = true; box.textContent = ""; }
    function render(data) {
      box.textContent = "";
      (data.categories || []).forEach(function (c) {
        var a = el("a", "", c.path || c.name);
        a.href = "/category/" + encodeURIComponent(c.id);
        a.prepend(el("small", "", BW.t("pages.search_category")));
        box.appendChild(a);
      });
      (data.pages || []).forEach(function (p) {
        var a = el("a", "", p.title);
        a.href = "/page/" + encodeURIComponent(p.slug);
        if (p.category_name) a.appendChild(el("small", "", p.category_name));
        box.appendChild(a);
      });
      if (!box.children.length) box.appendChild(el("p", "small muted", BW.t("pages.search_none")));
      var all = el("a", "", BW.t("pages.search_all"));
      all.href = form.action + "?q=" + encodeURIComponent(input.value.trim());
      box.appendChild(all);
      box.hidden = false;
    }
    function run() {
      var q = input.value.trim();
      if (!q) { hide(); return; }
      if (controller) controller.abort();
      controller = window.AbortController ? new AbortController() : null;
      var url = form.getAttribute("data-api") + "?q=" + encodeURIComponent(q) + "&scope=" + (content && content.checked ? "content" : "title");
      BW.fetchJSON(url, { signal: controller ? controller.signal : undefined }).then(render).catch(function () {});
    }
    input.addEventListener("input", function () { clearTimeout(timer); timer = setTimeout(run, 250); });
    if (content) content.addEventListener("change", run);
    input.addEventListener("keydown", function (e) { if (e.key === "Escape") { input.value = ""; hide(); } });
    document.addEventListener("click", function (e) { if (!form.contains(e.target)) box.hidden = true; });
  }

  // "More pages" ------------------------------------------------------------
  document.addEventListener("click", function (event) {
    var link = event.target.closest && event.target.closest("a[data-nav-more]");
    if (!link) return;
    event.preventDefault();
    var item = link.closest("li");
    link.setAttribute("aria-busy", "true");
    BW.fetchJSON(link.getAttribute("data-nav-more")).then(function (data) {
      var holder = document.createElement("template");
      holder.innerHTML = data.html;  // server-rendered, escaped template
      item.replaceWith(holder.content);
    }).catch(function (error) {
      link.removeAttribute("aria-busy");
      BW.toast(error.message || BW.t("error"), "error");
    });
  });

  // Reordering (drag and drop, plus up/down buttons) -------------------------
  function items(list) {
    return Array.prototype.filter.call(list.children, function (c) { return c.hasAttribute("data-reorder-item"); });
  }
  function save(list) {
    var ids = items(list).map(function (item) { return Number(item.getAttribute("data-id")); });
    BW.fetchJSON(list.getAttribute("data-reorder-url"), { method: "POST", body: { ids: ids } })
      .then(function (data) { BW.toast(data.message || BW.t("saved"), "success"); })
      .catch(function (error) { BW.toast(error.message || BW.t("error"), "error"); });
  }
  var dragged = null;
  document.addEventListener("dragstart", function (e) {
    var item = e.target.closest && e.target.closest("[data-reorder-item][draggable='true']");
    if (!item) return;
    dragged = item;
    item.classList.add("is-dragging");
    e.dataTransfer.effectAllowed = "move";
    e.dataTransfer.setData("text/plain", item.getAttribute("data-id"));
    e.stopPropagation();
  });
  document.addEventListener("dragover", function (e) {
    if (!dragged) return;
    var target = e.target.closest && e.target.closest("[data-reorder-item]");
    if (!target || target === dragged || target.parentNode !== dragged.parentNode) return;
    e.preventDefault();
    var rect = target.getBoundingClientRect();
    var after = e.clientY > rect.top + rect.height / 2;
    target.parentNode.insertBefore(dragged, after ? target.nextSibling : target);
  });
  document.addEventListener("dragend", function () {
    if (!dragged) return;
    var list = dragged.parentNode;
    dragged.classList.remove("is-dragging");
    dragged = null;
    if (list && list.hasAttribute("data-reorder-list")) save(list);
  });
  document.addEventListener("click", function (e) {
    var button = e.target.closest && e.target.closest("[data-move]");
    if (!button) return;
    e.preventDefault();
    var item = button.closest("[data-reorder-item]");
    var list = item && item.parentNode;
    if (!list || !list.hasAttribute("data-reorder-list")) return;
    var siblings = items(list), index = siblings.indexOf(item);
    if (button.getAttribute("data-move") === "up" && index > 0) list.insertBefore(item, siblings[index - 1]);
    else if (button.getAttribute("data-move") === "down" && index < siblings.length - 1) list.insertBefore(siblings[index + 1], item);
    else return;
    button.focus();
    save(list);
  });

  BW.onReady(function () {
    document.querySelectorAll("form[data-sidebar-search]").forEach(initSearch);
    var current = document.querySelector(".bw-nav [aria-current='page']");
    if (current && current.scrollIntoView) current.scrollIntoView({ block: "nearest" });
  });
})();
