# Administering the wiki

A short tour for new administrators. The full reference is in the
[documentation](../../README.md), in particular [features](../../features.md),
[permissions](../../permissions.md) and [security](../../security.md).

## Trust

Installing third-party plugins and importing a whole site each give complete
control of the wiki, so only owners and superusers can do them. Other
administrators still manage every other account and all content: only give the
role to people you trust with that. Owners and superusers are protected from
other administrators: only owners and superusers can change an administrator.

## The Admin area

Open **Administration** from the account menu.

* **Dashboard**: accounts waiting for approval, suspended accounts, traffic.
* **Users**: create accounts, approve or deny sign-ups, change roles, assign
  custom roles, set individual permissions and category access, suspend,
  reset passwords, end sessions, impersonate (to see the wiki as someone
  else; logged). An account's **History** page lists its former user names,
  which nobody else can take; release one there when another account needs
  it.
* **Custom roles**: named sets of permissions and category restrictions for
  groups of people.
* **Invite codes**: codes for the sign-up page, with a number of uses, an
  expiry and a role.
* **Sessions**: every signed-in session; sign everyone out; daily automatic
  sign-out.
* **Site settings**: name, time zone, public mode, sign-up and approval,
  maintenance mode, single session, bot protection, uploads, drafts, exports.
* **BM mode**: the easy mode. Turn it on to keep the wiki simple, then choose
  which features stay and what gets simpler (a shorter editor toolbar, fewer
  page actions, pages without history links or difficulty tags, a sidebar
  with only pages, boards and canvases, shorter settings, a short admin menu,
  simpler boards and canvases). With it off
  for the wiki, single accounts can still use it: decide whether people may
  switch it themselves and whether new accounts start in it, put everyone in
  or out at once, or switch one account (and whether it may change it) on
  its page under **Users**.
* **Appearance** and **Languages**: colours, favicon, interface languages, and
  the mascot's easter eggs.
* **Plugins**: switch features on and off (the page builder too) and order the
  Apps menu. Switching a feature off hides it but keeps its data and settings.
* **Documentation**: add the built-in user guide (full or simplified,
  English, Italian or German) as a category of pages.
* **Site migration**: export the whole wiki, or import one (this replaces
  everything, accounts included).
* **Canvas**: who may use canvases and who may create or import them (only
  administrators, by default). To let one person create canvases without
  opening creation to their whole role, tick **Create canvases** in their
  custom role or their individual permissions; the editors' default alone
  does not count.
* **Bulk delete**, **Bulk Markdown**, **Audit log**, **Server** and the pages
  of individual features (announcements, badges, chats, read aloud …).

## A good routine

* Review new accounts and the list of administrators regularly.
* Look at the audit log for unusual actions.
* Make sure backups run and try a restore now and then.
* Switch off the features your wiki does not use.
* Mark outdated pages clearly, or hide them.
