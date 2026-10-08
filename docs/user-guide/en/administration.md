# Administering the wiki

A short tour for new administrators. The full reference is in the
[documentation](../../README.md), in particular [features](../../features.md),
[permissions](../../permissions.md) and [security](../../security.md).

## Trust

Administrators can do everything, including installing plugins and importing
a whole site, each of which gives complete control of the wiki. Only give the
role to people you trust with everything. Owners and superusers are protected
from other administrators: only owners and superusers can change an
administrator.

## The Admin area

Open **Admin** from the account menu.

* **Dashboard**: accounts waiting for approval, suspended accounts, traffic.
* **Users**: create accounts, approve or deny sign-ups, change roles, assign
  custom roles, set individual permissions and category access, suspend,
  reset passwords, end sessions, impersonate (to see the wiki as someone
  else; logged). An account's **Audit** page lists its former user names,
  which nobody else can take; release one there when another account needs
  it.
* **Custom roles**: named sets of permissions and category restrictions for
  groups of people.
* **Invite codes**: codes for the sign-up page, with a number of uses, an
  expiry and a role.
* **Sessions**: every signed-in session; sign everyone out; daily automatic
  sign-out.
* **Site settings**: name, time zone, public mode, sign-up and approval,
  maintenance mode, single session, bot protection, uploads, drafts, exports,
  page builder.
* **Appearance** and **Languages**: colours, favicon, interface languages.
* **Plugins**: switch features on and off and order the Apps menu.
  Switching a feature off hides it but keeps its data and settings.
* **Documentation**: add this guide to the wiki as a category of pages.
* **Site migration**: export the whole wiki, or import one (this replaces
  everything, accounts included).
* **Bulk delete**, **Bulk Markdown**, **Audit log**, **Server** and the pages
  of individual features (announcements, badges, chats, read aloud …).

## A good routine

* Review new accounts and the list of administrators regularly.
* Look at the audit log for unusual actions.
* Make sure backups run and try a restore now and then.
* Switch off the features your wiki does not use.
* Mark outdated pages clearly, or hide them.
