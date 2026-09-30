/* Drafts in the page editor: autosave, restore, "save draft and close", and
 * the live list of other editors' drafts. Works on the stable editor ids
 * (#page-editor-form, #editor-title, #editor-content). */
(function () {
  "use strict";
  var panel = document.querySelector("[data-drafts]");
  var form = document.getElementById("page-editor-form");
  var title = document.getElementById("editor-title");
  var content = document.getElementById("editor-content");
  if (!panel || !form || !content || !window.BW) return;

  var pageId = parseInt(panel.getAttribute("data-page-id"), 10);
  var saveUrl = panel.getAttribute("data-save-url");
  var status = panel.querySelector("[data-draft-status]");
  var revisionInput = form.querySelector('input[name="revision"]');
  var timer = null;
  var inFlight = null;
  var controller = null;
  var stopped = false;
  var DELAY = 2000;

  function say(key) { if (status) status.textContent = key ? BW.t(key) : ""; }

  function payload() {
    var revision = revisionInput ? parseInt(revisionInput.value, 10) : NaN;
    var body = { page_id: pageId, content: content.value, title: title ? title.value : "" };
    if (!isNaN(revision)) body.revision = revision;
    return body;
  }

  function save() {
    if (!saveUrl || stopped) return Promise.resolve(null);
    if (timer) { clearTimeout(timer); timer = null; }
    if (inFlight) return inFlight.then(save);
    controller = window.AbortController ? new AbortController() : null;
    say("drafts.saving");
    inFlight = BW.fetchJSON(saveUrl, { method: "POST", body: payload(), signal: controller && controller.signal })
      .then(function (data) {
        say(data.status === "stale" ? "" : "drafts.saved");
        return data;
      }, function (error) {
        if (error.name !== "AbortError") { say("drafts.save_failed"); }
        throw error;
      })
      .then(function (data) { inFlight = null; return data; }, function (error) { inFlight = null; throw error; });
    return inFlight;
  }

  function schedule() {
    if (!saveUrl || stopped) return;
    if (timer) clearTimeout(timer);
    timer = setTimeout(function () { save().catch(function () {}); }, DELAY);
  }

  if (saveUrl) {
    content.addEventListener("input", schedule);
    if (title) title.addEventListener("input", schedule);
  }

  // Saving the page (or any other submit of the editor form) ends autosaving:
  // the server clears the draft and must not see a late autosave re-create it.
  form.addEventListener("submit", function () {
    stopped = true;
    if (timer) { clearTimeout(timer); timer = null; }
    if (controller) controller.abort();
  });

  var restore = panel.querySelector("[data-draft-restore]");
  if (restore) {
    restore.hidden = false;
    restore.addEventListener("click", function () {
      restore.disabled = true;
      BW.fetchJSON(panel.getAttribute("data-load-url")).then(function (draft) {
        if (draft.content === null) { BW.toast(BW.t("drafts.gone"), "warning"); return; }
        if (title && !title.readOnly && draft.title) {
          title.value = draft.title;
          title.dispatchEvent(new Event("input", { bubbles: true }));
        }
        content.value = draft.content;
        content.dispatchEvent(new Event("input", { bubbles: true }));
        restore.hidden = true;
        BW.toast(BW.t("drafts.restored"), "success");
      }).catch(function (error) {
        restore.disabled = false;
        BW.toast(error.message || BW.t("error"), "error");
      });
    });
  }

  var closeButton = panel.querySelector("[data-draft-close]");
  if (closeButton && saveUrl) {
    closeButton.hidden = false;
    closeButton.addEventListener("click", function () {
      closeButton.disabled = true;
      save().then(function (data) {
        if (!data || data.status === "stale") throw new Error(BW.t("drafts.save_failed"));
        stopped = true;
        // The text is safe in the draft: put the editor back to its loaded
        // state so the unsaved-changes guard lets us leave.
        content.value = content.defaultValue;
        if (title) title.value = title.defaultValue;
        window.location.href = panel.getAttribute("data-close-url");
      }).catch(function (error) {
        closeButton.disabled = false;
        BW.toast(error.message || BW.t("drafts.save_failed"), "error");
      });
    });
  }

  // Other editors' drafts --------------------------------------------------
  var othersBox = panel.querySelector("[data-draft-others]");
  var othersList = panel.querySelector("[data-draft-others-list]");
  var transferUrl = panel.getAttribute("data-transfer-url");

  function renderOthers(drafts) {
    othersList.textContent = "";
    drafts.forEach(function (draft) {
      var item = document.createElement("li");
      var name = document.createElement("strong");
      name.textContent = draft.username;
      item.appendChild(name);
      if (transferUrl) {
        var button = document.createElement("button");
        button.type = "submit";
        button.className = "btn btn--small btn--ghost";
        button.formAction = transferUrl;
        button.formNoValidate = true;
        button.name = "from_user_id";
        button.value = draft.user_id;
        button.setAttribute("data-confirm", BW.t("drafts.transfer_confirm", { user: draft.username }));
        button.textContent = BW.t("drafts.transfer");
        item.appendChild(document.createTextNode(" "));
        item.appendChild(button);
      }
      othersList.appendChild(item);
    });
    othersBox.hidden = drafts.length === 0;
  }

  function pollOthers() {
    if (document.visibilityState === "hidden") return;
    BW.fetchJSON(panel.getAttribute("data-others-url")).then(function (data) {
      renderOthers(data.drafts || []);
    }).catch(function () {});
  }
  if (othersBox && othersList) setInterval(pollOthers, 30000);
})();
