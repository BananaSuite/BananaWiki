/* Kanban ticket dialog: details, description, checklist, comments, attachments and description history.
 * Depends on kanban-board.js (window.BWKanban). */
(function () {
  "use strict";

  var BW = window.BW;
  var K = window.BWKanban;
  var dialog = document.getElementById("kanban-ticket-dialog");
  if (!K || !dialog) return;

  var el = K.el;
  var current = null;   // ticket detail from the server
  var color = "";       // "" means no colour
  function $(id) { return document.getElementById(id); }

  // ── Tabs ─────────────────────────────────────────────────────────────────
  var loaders = { comments: loadComments, attachments: loadAttachments, history: loadHistory };

  function showTab(name) {
    dialog.querySelectorAll("[data-tab]").forEach(function (tab) {
      if (tab.dataset.tab === name) tab.setAttribute("aria-current", "page"); else tab.removeAttribute("aria-current");
      tab.setAttribute("aria-selected", tab.dataset.tab === name ? "true" : "false");
    });
    dialog.querySelectorAll("[data-panel]").forEach(function (panel) { panel.hidden = panel.dataset.panel !== name; });
    if (loaders[name]) loaders[name]();
  }

  dialog.querySelector(".tabs").addEventListener("click", function (event) {
    var tab = event.target.closest("[data-tab]");
    if (!tab) return;
    event.preventDefault();
    showTab(tab.dataset.tab);
  });

  // ── Details ──────────────────────────────────────────────────────────────
  function setDescription(html) {
    var box = $("kanban-ticket-description");
    // Server-rendered, sanitised Markdown (markdown.render).
    box.innerHTML = html || "";
    if (!html) box.appendChild(el("p", "muted", BW.t("kanban.no_description")));
  }

  function fillColumns(selected) {
    var select = $("kanban-ticket-column");
    select.textContent = "";
    K.state.columns.forEach(function (col) {
      var option = el("option", "", col.title);
      option.value = col.id;
      option.selected = col.id === selected;
      select.appendChild(option);
    });
  }

  function fillAssignees(ticket, editable) {
    var box = $("kanban-ticket-assignees");
    box.textContent = "";
    var chosen = ticket.assignees.map(function (person) { return person.id; });
    if (!editable) {
      box.appendChild(el("p", "small", ticket.assignees.map(function (p) { return "@" + p.username; }).join(", ") || BW.t("kanban.nobody")));
      return;
    }
    var people = K.assignable.slice();
    ticket.assignees.forEach(function (person) {
      if (!people.some(function (p) { return p.id === person.id; })) people.push(person);
    });
    if (!people.length) box.appendChild(el("p", "small muted", BW.t("kanban.nobody")));
    people.forEach(function (person) {
      var label = el("label", "check");
      var box2 = el("input");
      box2.type = "checkbox";
      box2.value = person.id;
      box2.checked = chosen.indexOf(person.id) !== -1;
      label.appendChild(box2);
      label.appendChild(el("span", "", person.username));
      box.appendChild(label);
    });
  }

  function setColor(value) {
    color = value || "";
    $("kanban-ticket-color").value = color || "#4caf50";
    $("kanban-ticket-color-clear").disabled = !color;
  }

  function fill(ticket) {
    current = ticket;
    var editable = !!ticket.can_write;
    var title = $("kanban-ticket-title");
    title.value = ticket.title;
    title.readOnly = !editable;
    setDescription(ticket.description_html);
    $("kanban-ticket-description-edit").hidden = !editable;
    $("kanban-ticket-description-editor").hidden = true;
    fillColumns(ticket.column_id);
    $("kanban-ticket-priority").value = ticket.priority;
    $("kanban-ticket-due").value = ticket.due_date || "";
    $("kanban-ticket-labels").value = ticket.labels.map(function (l) { return "+" + l; }).join(" ");
    setColor(ticket.color);
    fillAssignees(ticket, editable);
    renderChecklist(ticket.checklist || []);
    $("kanban-checklist-form").hidden = !editable;
    ["kanban-ticket-column", "kanban-ticket-priority", "kanban-ticket-due", "kanban-ticket-labels", "kanban-ticket-color"].forEach(function (id) {
      $(id).disabled = !editable;
    });
    $("kanban-ticket-color-clear").hidden = !editable;
    $("kanban-ticket-save").hidden = !editable;
    $("kanban-ticket-delete").hidden = !editable;
    // An archived ticket stays editable but is off the board: it cannot change column until restored.
    var archived = !!ticket.archived_at;
    var notice = $("kanban-ticket-archived");
    notice.hidden = !archived;
    notice.textContent = archived ? BW.t("kanban.ticket_archived_notice", {
      date: ticket.archived_at.slice(0, 10), username: ticket.archived_by_username || BW.t("kanban.someone")
    }) : "";
    $("kanban-ticket-column").disabled = !editable || archived;
    $("kanban-ticket-archive").hidden = !editable || archived;
    $("kanban-ticket-restore").hidden = !editable || !archived;
    $("kanban-comment-form").hidden = !ticket.can_comment;
    $("kanban-upload").hidden = !editable;
    $("kanban-ticket-created").textContent = BW.t("kanban.created_by", {
      username: ticket.created_by_username || BW.t("kanban.someone"), date: (ticket.created_at || "").slice(0, 10)
    });
  }

  K.openTicket = function (ticketId) {
    return K.api("/tickets/" + ticketId).then(function (ticket) {
      fill(ticket);
      showTab("description");
      if (!dialog.open) dialog.showModal();
      history.replaceState(null, "", "#ticket-" + ticketId);
    }).catch(K.fail);
  };

  dialog.addEventListener("close", function () {
    var id = current && current.id;
    current = null;
    if (location.hash.indexOf("#ticket-") === 0) history.replaceState(null, "", location.pathname + location.search);
    var card = id && document.querySelector(".kanban-card[data-ticket-id='" + id + "'] [data-open]");
    if (card) card.focus();
  });

  function afterSave(ticket) {
    if (!ticket.archived_at) {
      K.upsertTicket(K.cardFrom(ticket));
      K.refresh();
    }
    fill(Object.assign({}, current, ticket));
  }

  function save() {
    if (!current) return;
    var assignees = Array.prototype.map.call(
      $("kanban-ticket-assignees").querySelectorAll("input:checked"), function (box) { return box.value; });
    var body = {
      title: $("kanban-ticket-title").value,
      priority: $("kanban-ticket-priority").value,
      due_date: $("kanban-ticket-due").value,
      labels: $("kanban-ticket-labels").value,
      color: color,
      assignees: assignees
    };
    var targetColumn = Number($("kanban-ticket-column").value);
    var ticketId = current.id;
    K.api("/tickets/" + ticketId, "PUT", body).then(function (ticket) {
      afterSave(ticket);
      if (targetColumn !== ticket.column_id) {
        var target = K.column(targetColumn);
        return K.moveTicket(ticketId, targetColumn, target ? target.tickets.length : 0).then(function () {
          current.column_id = targetColumn;
        });
      }
      return null;
    }).then(function () { BW.toast(BW.t("saved"), "success"); }).catch(K.fail);
  }

  $("kanban-ticket-save").addEventListener("click", save);
  $("kanban-ticket-title").addEventListener("keydown", function (event) {
    if (event.key === "Enter") { event.preventDefault(); save(); }
  });
  $("kanban-ticket-color").addEventListener("input", function (event) { setColor(event.target.value); });
  $("kanban-ticket-color-clear").addEventListener("click", function () { setColor(""); });

  $("kanban-ticket-delete").addEventListener("click", function () {
    if (!current || !window.confirm(BW.t("kanban.confirm_delete_ticket", { title: current.title }))) return;
    var ticketId = current.id;
    var archived = !!current.archived_at;
    K.api("/tickets/" + ticketId, "DELETE").then(function () {
      K.removeTickets([ticketId]);
      if (archived) K.state.archived_count = Math.max(0, (K.state.archived_count || 0) - 1);
      dialog.close();
      K.refresh();
      K.announce(BW.t("kanban.ticket_deleted"));
      if (archived) document.dispatchEvent(new CustomEvent("kanban:archived"));
    }).catch(K.fail);
  });

  $("kanban-ticket-archive").addEventListener("click", function () {
    if (!current) return;
    var ticketId = current.id;
    var title = current.title;
    K.api("/tickets/" + ticketId + "/archive", "POST").then(function (result) {
      K.removeTickets([ticketId]);
      K.state.archived_count = (K.state.archived_count || 0) + result.updated;
      K.refresh();
      K.announce(BW.t("kanban.ticket_archived", { title: title }));
      document.dispatchEvent(new CustomEvent("kanban:archived"));
      return K.openTicket(ticketId);
    }).then(function () { $("kanban-ticket-restore").focus(); }).catch(K.fail);
  });

  $("kanban-ticket-restore").addEventListener("click", function () {
    if (!current) return;
    var ticketId = current.id;
    var title = current.title;
    K.api("/tickets/" + ticketId + "/restore", "POST").then(function (result) {
      if (result.ticket) K.upsertTicket(result.ticket, null);
      K.state.archived_count = Math.max(0, (K.state.archived_count || 0) - result.updated);
      K.refresh();
      K.announce(BW.t("kanban.ticket_restored", { title: title }));
      document.dispatchEvent(new CustomEvent("kanban:archived"));
      return K.openTicket(ticketId);
    }).then(function () { $("kanban-ticket-archive").focus(); }).catch(K.fail);
  });

  // ── Description ──────────────────────────────────────────────────────────
  $("kanban-ticket-description-edit").addEventListener("click", function () {
    $("kanban-ticket-description-input").value = current.description;
    $("kanban-ticket-description-editor").hidden = false;
    $("kanban-ticket-description-edit").hidden = true;
    $("kanban-ticket-description-input").focus();
  });
  $("kanban-ticket-description-cancel").addEventListener("click", function () {
    $("kanban-ticket-description-editor").hidden = true;
    $("kanban-ticket-description-edit").hidden = false;
  });
  $("kanban-ticket-description-save").addEventListener("click", function () {
    var text = $("kanban-ticket-description-input").value;
    K.api("/tickets/" + current.id, "PUT", { description: text }).then(afterSave).catch(K.fail);
  });

  // Pasting an image into the description uploads it and inserts a link to it.
  $("kanban-ticket-description-input").addEventListener("paste", function (event) {
    var items = (event.clipboardData && event.clipboardData.files) || [];
    var image = Array.prototype.find.call(items, function (file) { return /^image\//.test(file.type); });
    if (!image || !current) return;
    event.preventDefault();
    var area = event.target;
    upload(image).then(function (stored) {
      var markdown = "![" + stored.original_name + "](/api/kanban/attachments/" + stored.id + "/download?inline=1)";
      area.setRangeText(markdown, area.selectionStart, area.selectionEnd, "end");
    });
  });

  // ── Checklist ────────────────────────────────────────────────────────────
  function smallButton(text, label, onClick) {
    var button = el("button", "btn btn--small btn--ghost", text);
    button.type = "button";
    button.setAttribute("aria-label", label);
    button.title = label;
    button.addEventListener("click", onClick);
    return button;
  }

  function checklistProgress(items) {
    var done = items.filter(function (item) { return item.done; }).length;
    var bar = $("kanban-checklist-bar");
    bar.hidden = !items.length;
    bar.max = Math.max(1, items.length);
    bar.value = done;
    $("kanban-checklist-progress").textContent = items.length ? BW.t("kanban.checklist_count", { done: done, total: items.length }) : "";
  }

  function renderChecklist(items, focus) {
    var list = $("kanban-ticket-checklist");
    var editable = !!(current && current.can_write);
    current.checklist = items;
    list.textContent = "";
    checklistProgress(items);
    if (!items.length) list.appendChild(el("li", "muted small", BW.t("kanban.checklist_empty")));
    items.forEach(function (item, index) {
      var li = el("li", "kanban-checklist__item" + (item.done ? " is-done" : ""));
      li.dataset.itemId = item.id;
      var label = el("label", "check");
      var box = el("input");
      box.type = "checkbox";
      box.checked = item.done;
      box.disabled = !editable;
      box.dataset.role = "toggle";
      box.addEventListener("change", function () { changeItem(item, { done: box.checked }, "toggle"); });
      label.appendChild(box);
      label.appendChild(el("span", "kanban-checklist__text", item.text));
      li.appendChild(label);
      if (editable && items.length > 1) {
        var grip = el("button", "kanban-grip kanban-checklist__grip", "⠿");
        grip.type = "button";
        grip.dataset.role = "grip";
        grip.setAttribute("aria-label", BW.t("kanban.checklist_grip", { text: item.text }));
        grip.title = grip.getAttribute("aria-label");
        li.insertBefore(grip, label);
      }
      if (editable) {
        var tools = el("span", "kanban-checklist__tools");
        var edit = smallButton(BW.t("kanban.edit"), BW.t("kanban.checklist_edit", { text: item.text }), function () { editItem(li, item); });
        edit.dataset.role = "edit";
        tools.appendChild(edit);
        if (index > 0) {
          var up = smallButton("↑", BW.t("kanban.checklist_up", { text: item.text }), function () { moveItem(item, -1); });
          up.dataset.role = "up";
          tools.appendChild(up);
        }
        if (index < items.length - 1) {
          var down = smallButton("↓", BW.t("kanban.checklist_down", { text: item.text }), function () { moveItem(item, 1); });
          down.dataset.role = "down";
          tools.appendChild(down);
        }
        var remove = smallButton("×", BW.t("kanban.checklist_delete", { text: item.text }), function () { deleteItem(item, index); });
        remove.dataset.role = "delete";
        tools.appendChild(remove);
        li.appendChild(tools);
      }
      list.appendChild(li);
    });
    if (focus) {
      var target = list.querySelector("[data-item-id='" + focus.id + "'] [data-role='" + focus.role + "']") ||
        list.querySelector("[data-item-id='" + focus.id + "'] [data-role='toggle']");
      if (target) target.focus(); else $("kanban-checklist-input").focus();
    }
  }

  function checklistSaved(result, focus) {
    if (K.ticket(result.ticket.id)) {
      K.upsertTicket(result.ticket);
      K.refresh();
    }
    if (current && current.id === result.ticket.id) renderChecklist(result.checklist, focus);
  }

  function loadChecklist() {
    if (!current) return;
    var ticketId = current.id;
    K.api("/tickets/" + ticketId + "/checklist").then(function (result) {
      if (current && current.id === ticketId) renderChecklist(result.checklist);
    }).catch(K.fail);
  }

  function changeItem(item, body, role) {
    K.api("/checklist/" + item.id, "PUT", body).then(function (result) {
      checklistSaved(result, { id: item.id, role: role });
      if ("done" in body) {
        var done = result.checklist.filter(function (other) { return other.done; }).length;
        K.announce(BW.t("kanban.checklist_count", { done: done, total: result.checklist.length }));
      }
    }).catch(function (error) { K.fail(error); loadChecklist(); });
  }

  function editItem(li, item) {
    var form = el("form", "kanban-checklist__edit");
    var input = el("input");
    input.type = "text";
    input.maxLength = 300;
    input.required = true;
    input.value = item.text;
    input.setAttribute("aria-label", BW.t("kanban.checklist_edit", { text: item.text }));
    var ok = el("button", "btn btn--primary btn--small", BW.t("kanban.save"));
    ok.type = "submit";
    var cancel = el("button", "btn btn--small", BW.t("kanban.cancel"));
    cancel.type = "button";
    function stop() { renderChecklist(current.checklist, { id: item.id, role: "edit" }); }
    cancel.addEventListener("click", stop);
    input.addEventListener("keydown", function (event) {
      if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); stop(); }
    });
    form.addEventListener("submit", function (event) {
      event.preventDefault();
      var text = input.value.trim();
      if (!text || text === item.text) { stop(); return; }
      changeItem(item, { text: text }, "edit");
    });
    form.appendChild(input);
    form.appendChild(ok);
    form.appendChild(cancel);
    li.replaceChildren(form);
    input.focus();
    input.select();
  }

  function saveOrder(ids, item, role) {
    var position = ids.indexOf(item.id);
    return K.api("/tickets/" + current.id + "/checklist/reorder", "POST", { order: ids }).then(function (result) {
      checklistSaved(result, { id: item.id, role: role });
      K.announce(BW.t("kanban.checklist_moved", { text: item.text, position: position + 1, total: ids.length }));
    }).catch(function (error) { K.fail(error); loadChecklist(); });
  }

  function moveItem(item, delta, keepRole) {
    var ids = current.checklist.map(function (other) { return other.id; });
    var index = ids.indexOf(item.id);
    var target = index + delta;
    if (index === -1 || target < 0 || target >= ids.length) return;
    ids.splice(index, 1);
    ids.splice(target, 0, item.id);
    var role = keepRole || (delta < 0 ? (target > 0 ? "up" : "down") : (target < ids.length - 1 ? "down" : "up"));
    saveOrder(ids, item, role);
  }

  // Dragging items by their handle (mouse, touch and pen); on the handle the arrow keys move the item too.
  var checklistNode = $("kanban-ticket-checklist");

  function itemOf(node) {
    var id = Number(node.closest("[data-item-id]").dataset.itemId);
    return current.checklist.filter(function (item) { return item.id === id; })[0] || null;
  }

  function renderedOrder() {
    return Array.prototype.map.call(checklistNode.querySelectorAll("[data-item-id]"), function (li) { return Number(li.dataset.itemId); });
  }

  checklistNode.addEventListener("keydown", function (event) {
    var grip = event.target.closest && event.target.closest("[data-role='grip']");
    if (!grip || !current || (event.key !== "ArrowUp" && event.key !== "ArrowDown")) return;
    event.preventDefault();
    var item = itemOf(grip);
    if (item) moveItem(item, event.key === "ArrowUp" ? -1 : 1, "grip");
  });

  checklistNode.addEventListener("pointerdown", function (event) {
    var grip = event.target.closest("[data-role='grip']");
    if (!grip || event.button !== 0 || !current || !current.can_write) return;
    event.preventDefault();
    var li = grip.closest("[data-item-id]");
    var item = itemOf(grip);
    var before = renderedOrder().join(",");
    var pointer = event.pointerId;
    var startY = event.clientY;
    var started = false;

    function onMove(moveEvent) {
      if (moveEvent.pointerId !== pointer) return;
      if (!started) {
        if (Math.abs(moveEvent.clientY - startY) < 4) return;
        started = true;
        li.classList.add("is-dragging");
      }
      moveEvent.preventDefault();
      var next = null;
      var others = checklistNode.querySelectorAll("[data-item-id]");
      for (var i = 0; i < others.length; i++) {
        if (others[i] === li) continue;
        var box = others[i].getBoundingClientRect();
        if (moveEvent.clientY < box.top + box.height / 2) { next = others[i]; break; }
      }
      if (li.nextElementSibling !== next) checklistNode.insertBefore(li, next);
    }

    function finish(commit) {
      document.removeEventListener("pointermove", onMove);
      document.removeEventListener("pointerup", onUp);
      document.removeEventListener("pointercancel", onCancel);
      document.removeEventListener("keydown", onEscape, true);
      li.classList.remove("is-dragging");
      if (!started) return;
      var order = renderedOrder();
      if (!commit || !item) { renderChecklist(current.checklist, { id: item ? item.id : 0, role: "grip" }); return; }
      if (order.join(",") !== before) saveOrder(order, item, "grip");
      else grip.focus();
    }

    function onUp(upEvent) { if (upEvent.pointerId === pointer) finish(true); }
    function onCancel(cancelEvent) { if (cancelEvent.pointerId === pointer) finish(false); }
    function onEscape(keyEvent) { if (keyEvent.key === "Escape" && started) { keyEvent.preventDefault(); keyEvent.stopPropagation(); finish(false); } }

    document.addEventListener("pointermove", onMove, { passive: false });
    document.addEventListener("pointerup", onUp);
    document.addEventListener("pointercancel", onCancel);
    document.addEventListener("keydown", onEscape, true);
  });

  function deleteItem(item, index) {
    K.api("/checklist/" + item.id, "DELETE").then(function (result) {
      var next = result.checklist[Math.min(index, result.checklist.length - 1)];
      checklistSaved(result, next ? { id: next.id, role: "delete" } : { id: 0, role: "" });
      K.announce(BW.t("kanban.checklist_deleted", { text: item.text }));
    }).catch(K.fail);
  }

  $("kanban-checklist-form").addEventListener("submit", function (event) {
    event.preventDefault();
    var input = $("kanban-checklist-input");
    var text = input.value.trim();
    if (!text || !current) return;
    input.disabled = true;
    K.api("/tickets/" + current.id + "/checklist", "POST", { text: text }).then(function (result) {
      input.value = "";
      checklistSaved(result);
      K.announce(BW.t("kanban.checklist_added", { text: text }));
    }).catch(K.fail).then(function () { input.disabled = false; input.focus(); });
  });

  // ── Comments ─────────────────────────────────────────────────────────────
  function renderComment(comment) {
    var li = el("li", "kanban-comment");
    li.dataset.commentId = comment.id;
    var head = el("div", "kanban-comment__head");
    head.appendChild(el("strong", "", comment.author_name || BW.t("kanban.someone")));
    head.appendChild(el("span", "", (comment.created_at || "").slice(0, 16) + (comment.edited ? " · " + BW.t("kanban.edited") : "")));
    var actions = el("span", "kanban-comment__actions");
    if (comment.can_edit) {
      var edit = el("button", "btn btn--small btn--ghost", BW.t("kanban.edit"));
      edit.type = "button";
      edit.addEventListener("click", function () { editComment(li, comment); });
      actions.appendChild(edit);
    }
    if (comment.can_delete) {
      var remove = el("button", "btn btn--small btn--ghost", BW.t("kanban.delete"));
      remove.type = "button";
      remove.addEventListener("click", function () { deleteComment(comment); });
      actions.appendChild(remove);
    }
    head.appendChild(actions);
    li.appendChild(head);
    var body = el("div", "wiki-content");
    body.innerHTML = comment.content_html; // sanitised by markdown.render on the server
    li.appendChild(body);
    return li;
  }

  function loadComments() {
    if (!current) return;
    var list = $("kanban-ticket-comments");
    K.api("/tickets/" + current.id + "/comments").then(function (comments) {
      list.textContent = "";
      if (!comments.length) list.appendChild(el("li", "muted", BW.t("kanban.no_comments")));
      comments.forEach(function (comment) { list.appendChild(renderComment(comment)); });
    }).catch(K.fail);
  }

  function editComment(li, comment) {
    var form = el("form", "form");
    var area = el("textarea");
    area.rows = 3;
    area.maxLength = 2000;
    area.value = comment.content;
    area.setAttribute("aria-label", BW.t("kanban.edit"));
    var row = el("div", "btn-row");
    var ok = el("button", "btn btn--primary btn--small", BW.t("kanban.save"));
    ok.type = "submit";
    var cancel = el("button", "btn btn--small", BW.t("kanban.cancel"));
    cancel.type = "button";
    cancel.addEventListener("click", loadComments);
    row.appendChild(ok);
    row.appendChild(cancel);
    form.appendChild(area);
    form.appendChild(row);
    form.addEventListener("submit", function (event) {
      event.preventDefault();
      K.api("/comments/" + comment.id, "PUT", { content: area.value }).then(loadComments).catch(K.fail);
    });
    li.replaceChildren(form);
    area.focus();
  }

  function deleteComment(comment) {
    if (!window.confirm(BW.t("kanban.confirm_delete_comment"))) return;
    K.api("/comments/" + comment.id, "DELETE").then(function () {
      loadComments();
      bumpCount("comment_count", -1);
    }).catch(K.fail);
  }

  function bumpCount(field, delta) {
    var ticket = current && K.ticket(current.id);
    if (!ticket) return;
    ticket[field] = Math.max(0, (ticket[field] || 0) + delta);
    K.refresh();
  }

  $("kanban-comment-form").addEventListener("submit", function (event) {
    event.preventDefault();
    var area = $("kanban-comment-input");
    K.api("/tickets/" + current.id + "/comments", "POST", { content: area.value }).then(function () {
      area.value = "";
      loadComments();
      bumpCount("comment_count", 1);
    }).catch(K.fail);
  });

  // ── Attachments ──────────────────────────────────────────────────────────
  function upload(file) {
    var form = new FormData();
    form.append("file", file);
    return K.api("/tickets/" + current.id + "/attachments", "POST", form).then(function (stored) {
      bumpCount("attachment_count", 1);
      return stored;
    }).catch(function (error) { K.fail(error); throw error; });
  }

  function loadAttachments() {
    if (!current) return;
    var list = $("kanban-ticket-attachments");
    K.api("/tickets/" + current.id + "/attachments").then(function (files) {
      list.textContent = "";
      if (!files.length) list.appendChild(el("li", "muted", BW.t("kanban.no_attachments")));
      files.forEach(function (file) {
        var li = el("li");
        var link = el("a", "", file.original_name);
        link.href = "/api/kanban/attachments/" + file.id + "/download";
        li.appendChild(link);
        li.appendChild(el("span", "small muted", Math.max(1, Math.round(file.file_size / 1024)) + " KB · " + (file.uploader_name || "")));
        if (current.can_write) {
          var remove = el("button", "btn btn--small btn--ghost", BW.t("kanban.delete"));
          remove.type = "button";
          remove.setAttribute("aria-label", BW.t("kanban.delete_file", { name: file.original_name }));
          remove.addEventListener("click", function () {
            if (!window.confirm(BW.t("kanban.confirm_delete_file", { name: file.original_name }))) return;
            K.api("/attachments/" + file.id, "DELETE").then(function () {
              loadAttachments();
              bumpCount("attachment_count", -1);
            }).catch(K.fail);
          });
          li.appendChild(remove);
        }
        list.appendChild(li);
      });
    }).catch(K.fail);
  }

  $("kanban-upload-input").addEventListener("change", function (event) {
    var file = event.target.files[0];
    if (!file) return;
    upload(file).then(loadAttachments, function () { /* reported by upload() */ });
    event.target.value = "";
  });

  // ── Description history ──────────────────────────────────────────────────
  function loadHistory() {
    if (!current) return;
    var list = $("kanban-ticket-history");
    var diff = $("kanban-ticket-diff");
    diff.hidden = true;
    K.api("/tickets/" + current.id + "/history").then(function (entries) {
      list.textContent = "";
      if (!entries.length) list.appendChild(el("li", "muted", BW.t("kanban.no_history")));
      entries.forEach(function (entry) {
        var li = el("li");
        li.appendChild(el("span", "", (entry.created_at || "").slice(0, 16) + " · " + (entry.editor_name || BW.t("kanban.someone"))));
        var show = el("button", "btn btn--small", BW.t("kanban.show_changes"));
        show.type = "button";
        show.addEventListener("click", function () {
          K.api("/history/" + entry.id).then(function (detail) {
            diff.innerHTML = detail.diff_html; // every line escaped by the server
            diff.hidden = false;
          }).catch(K.fail);
        });
        li.appendChild(show);
        list.appendChild(li);
      });
    }).catch(K.fail);
  }

  // Keep an open ticket current when someone else changes it.
  document.addEventListener("kanban:events", function (event) {
    if (!current || !dialog.open) return;
    var id = current.id;
    function names(item) { return item.payload && item.payload.ticket_ids && item.payload.ticket_ids.indexOf(id) !== -1; }
    var touched = event.detail.some(function (item) {
      return names(item) || (item.payload && item.payload.ticket && item.payload.ticket.id === id);
    });
    if (!touched) return;
    if (event.detail.some(function (item) { return item.op === "ticket_deleted" && names(item); })) {
      dialog.close();
      BW.toast(BW.t("kanban.ticket_removed"), "info");
      return;
    }
    // Archived or restored (here or elsewhere): show the ticket's new state.
    if (current.archived_at || !K.ticket(id)) { K.openTicket(id); return; }
    var panel = dialog.querySelector("[data-panel]:not([hidden])");
    if (panel && loaders[panel.dataset.panel]) loaders[panel.dataset.panel]();
    if (panel && panel.dataset.panel === "description" && !dialog.querySelector(".kanban-checklist__edit")) loadChecklist();
  });

  var match = /^#ticket-(\d+)$/.exec(location.hash);
  if (match) BW.onReady(function () { K.openTicket(Number(match[1])); });
})();
