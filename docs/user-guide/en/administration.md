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

Open **Administration** from the account menu. It opens on the dashboard, and the sidebar lists
every administration page by group (People, Wiki, Content, System); **Back to the wiki** returns to
the page tree.

* **Dashboard**: accounts waiting for approval, suspended accounts, traffic.
* **Users**: create accounts, approve or deny sign-ups, change roles, assign
  custom roles, change one person's permissions and category access on top of
  their role (the account page sums up what they can do), suspend,
  reset passwords, end sessions, impersonate (to see the wiki as someone
  else; logged). An account's **History** page lists its former user names,
  which nobody else can take; release one there when another account needs
  it.
* **Access requests**: tick **Let people ask for edit access** so members
  who can read a page but not edit it can ask; approve or decline here (see
  [permissions](../../permissions.md#asking-for-edit-access)).
* **Invite codes**: codes for the sign-up page, with a number of uses, an
  expiry and a role.
* **Sessions**: every signed-in session; sign everyone out.
* **Site settings**: name, time zone, public mode, sign-up and approval,
  maintenance mode, single session, the daily sign-out, bot protection,
  uploads, drafts, PDF export.
* **BM mode**: the easy mode. Turn it on to keep the wiki simple: the
  features meant for bigger wikis are hidden (switch one on under **Plugins**
  to keep it) and screens get simpler (a shorter editor toolbar, fewer page
  actions, pages without history links or difficulty tags, a sidebar with
  only pages, boards and canvases, shorter settings, a short admin menu,
  simpler boards and canvases). With it off
  for the wiki, single accounts can still use it: decide whether people may
  switch it themselves and whether new accounts start in it, put the accounts
  of the roles you tick (users, editors, administrators) in or out at once,
  or switch one account (and whether it may change it) on its page under
  **Users**.
* **Appearance** and **Languages**: colours, favicon, interface languages, and
  the mascot's easter eggs.
* **Plugins**: switch features on and off (the page builder too) and order the
  Apps menu. Switching a feature off hides it but keeps its data and settings.
* **Documentation**: add the built-in user guide (full or simplified,
  English, Italian or German) as a category of pages.
* **Site migration**: export the whole wiki, or import one (this replaces
  everything, accounts included).
* **Roles**: what everyone with the role User or Editor may do (members
  create pages, boards and canvases and work on their own; editors also edit
  everyone's), and custom roles for groups that need something else.
* **Bulk delete**, **Bulk Markdown**, **Audit log**, **Server** and the pages
  of individual features (announcements, badges, chats, read aloud …).

## A good routine

* Review new accounts and the list of administrators regularly.
* Look at the audit log for unusual actions.
* Make sure backups run and try a restore now and then.
* Switch off the features your wiki does not use.
* Mark outdated pages clearly, or hide them.
