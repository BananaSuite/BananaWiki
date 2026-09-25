# What is BananaWiki?

> A friendly, non-technical overview of how BananaWiki works.

BananaWiki is a shared notebook that lives on your team's own server.
Anyone you invite can read it, the people you trust can edit it, and
nobody else can see it.

This guide is for anyone who wants to use a wiki, developer or not.
You do not need to know what Flask, SQLite or Markdown is to
understand any of it. Where a technical term is unavoidable we explain
it in plain English the first time it appears.

## What is a "wiki" anyway?

A *wiki* is a website made up of **pages** that the people using it can
edit themselves. Wikipedia is the most famous example, but most wikis
are small and private: for a company, a school, a friend group, or a
single project.

Each page has:

- a **title** (for example *Onboarding* or *Holiday calendar*);
- a **body** of text that anyone with edit rights can change;
- a **history**, so you can always see what was changed and roll back
  to an earlier version.

You write the body using **Markdown**, basically plain text with a few
simple symbols for things like **bold**, *italic* and bullet lists. The
editor has buttons for everything, so you do not have to memorise the
symbols.

## What makes BananaWiki different?

BananaWiki is completely self-hosted. Everything runs on a server you
control: the wiki, the search, the chat and the file uploads.

It ships with:

- **Categories** that work like folders so you can group related pages.
- **Search** that filters pages and categories as you type.
- **Chat** for direct messages and group conversations.
- **Kanban boards** for tracking work, with columns, cards and
  attachments.
- **Page reservations** so two people don't accidentally edit the same
  page at the same time.
- **Roles** so you can decide who can read, who can write, and who can
  administer the site.

You don't have to use any of these. Admins can turn features on or off
from the admin panel.

## How does a typical wiki day look?

1. You open the wiki in your browser and log in.
2. The home page shows the **categories** down the left side and the
   most recent updates in the middle.
3. You click a category to open it, and a page to read it.
4. If you spot a typo or want to add something, you click **Edit** at
   the top of the page.
5. The editor splits into two halves: you type in the left half and a
   live preview appears on the right.
6. You save your changes. The page gets a new entry in the **History**
   tab, so you (or anyone else) can always see what you changed.

## Who can do what?

BananaWiki uses four roles. They go from least to most powerful:

| Role | What they can do |
| --- | --- |
| **User** | Read pages, chat and comment, but cannot change pages. |
| **Editor** | Everything a User can do, plus create, edit and delete pages. |
| **Admin** | Everything an Editor can do, plus manage users, settings and plugins. |
| **Protected admin** | Same as Admin, but other admins cannot remove or downgrade them. Useful for a "rescue" account. |

Admins can also customise these roles per user. For example, you can
grant a single User the right to upload images without making them a
full Editor.

## The in-app user guide

Every BananaWiki ships with a built-in user guide that admins can
publish into the wiki with one click from
**Admin → Site Settings → Wiki Documentation → Spawn documentation**.
It creates a category called *BananaWiki* with pages like *Welcome*,
*Pages & editing*, *Roles & permissions*, *Chat*, *Kanban* and so on.

You can:

- **Read it directly inside the wiki**, like any other page.
- **Download it as a ZIP** from the same settings panel, edit the
  Markdown files locally, and re-upload them with **Bulk Markdown
  Import** to keep a customised version that suits your team.
- **Browse the same content on disk** in the
  [`docs/user-guide/`](user-guide/) folder of the BananaWiki source.
  The English copy is in
  [`docs/user-guide/en/`](user-guide/en/) and the Italian copy is in
  [`docs/user-guide/it/`](user-guide/it/).

The on-disk copy is not synchronised automatically. After changing the
guide in `db/_wiki_docs.py`, run `python scripts/sync_user_guide_docs.py`
by hand to bring `docs/user-guide/` back in line. See
[`docs/user-guide/en/README.md`](user-guide/en/README.md).

## Where to go next

- **Brand new to BananaWiki?** Read
  [`docs/user-guide/en/bananawiki-welcome.md`](user-guide/en/bananawiki-welcome.md).
- **Want to set up a fresh wiki?** Follow
  [`docs/getting-started.md`](getting-started.md).
- **Running it for a team?** Open
  [`docs/permissions.md`](permissions.md) and
  [`docs/operations.md`](operations.md).
- **Curious about the API?** Skim
  [`docs/api.md`](api.md).

If something here is unclear, that's a documentation bug. Please open
an issue or pull request so we can fix it for the next person.
