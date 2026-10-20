# Features

BananaWiki is built from features. Most can be switched on and off; turning
one off hides its pages (they answer 404), removes its menu entries, stops its
background jobs and takes away its permissions. **It never deletes data or
changes the feature's settings**: switch it back on and everything is where
it was.

How a feature is switched:

* **Plugin switch**: **Admin → Plugins** (`/admin/plugins`). The state is the
  feature's row in the `plugins` table.
* **Setting**: a site setting on the feature's own admin page or
  **Admin → Site settings**.
* **Always on**: part of the core.
* **Operator**: an environment variable (see [configuration](configuration.md)).

With `BW_EASY_WIKI=1` (the hosting platform's "easy wiki") the features marked
*advanced* below are hidden and stay off. `BW_MANAGED_PLUGIN_DENYLIST` removes
features entirely.

| Feature | Id | Switch | Default | Advanced |
|---|---|---|---|---|
| [Pages and categories](#pages-and-categories) | `pages` | always on | on | |
| [Page history](#page-history) | `page_history` | plugin | on | |
| [Drafts](#drafts) | `drafts` | plugin | on | |
| [Attachments](#attachments) | `attachments` | plugin | on | |
| [Difficulty tags](#difficulty-tags) | `difficulty_tags` | plugin | on | |
| [Page export](#page-export) | `page_export` | always on (settings per format) | on | |
| [Page builder](#page-builder) | `page_builder` | setting `page_builder_enabled` | off | yes |
| [Page governance](#page-governance) | `page_governance` | plugin | off | yes |
| [Contribution approval](#contribution-approval) | `contributions` | setting `contribution_approval_enabled` | off | yes |
| [Deletion slowdown](#deletion-slowdown) | `deletion_slowdown` | plugin | off | |
| [Temporary items](#temporary-items) | `temporary_accounts` | plugin | off | |
| [Announcements](#announcements) | `announcements` | plugin | on | |
| [Assessments](#assessments) | `assessments` | plugin | on | yes |
| [Kanban boards](#kanban-boards) | `kanban` | plugin | on | yes |
| [Canvases](#canvases) | `canvas` | plugin | on | yes |
| [Chats and groups](#chats-and-groups) | `chat` | plugin | on | yes |
| [Read aloud](#read-aloud) | `tts` | plugin | on | yes |
| [People and profiles](#people-and-profiles) | `users`, `user_profiles` | always on / plugin | on | |
| [Badges](#badges) | `badges` | plugin | on | |
| [Leaderboard](#leaderboard) | `leaderboard` | setting `contributor_leaderboard_enabled` | off | |
| [Mascot](#mascot) | `mascot` | plugin | on | |
| [Personal data export](#personal-data-export) | `user_data_export` | plugin | on | |
| [Custom pages](#custom-pages) | `custom_pages` | plugin | off | yes |
| [REST API](#rest-api) | `api_service` | plugin + setting `api_service_enabled` | off | |
| [Federation](#federation) | `federation` | operator `BW_FEDERATION_ENABLED` | off | yes |
| [Audit log](#audit-log) | `audit` | plugin | on | |
| [Administration](#administration) | `admin`, `site_admin`, `bulk_manage`, `plugin_manager` | always on | on | |
| [Needs your attention and notifications](#needs-your-attention-and-notifications) | `attention` | always on (emails off by default) | on | |
| [Sign-in and onboarding](#sign-in-and-onboarding) | `auth` | always on | on | |

## Pages and categories

The wiki itself: Markdown pages in a tree of categories.

* **Reading** at `/page/<slug>`, the home page at `/`, categories at
  `/category/<id>`, the full tree at `/navigation`. Long pages get a table of
  contents; categories can have **sequential navigation** (previous/next
  links in sidebar order).
* **Editing** at `/page/<slug>/edit` and **creating** at `/create`: a Markdown
  editor with a formatting toolbar, live preview, image upload by drag and
  drop or paste, image options (alternative text, width, alignment with text
  flowing beside, spacing, crop to a new upload) from a click in the preview,
  tables, video embeds, links to other pages, and full screen. Two people saving the same page are told about the conflict
  instead of overwriting each other. Other editors currently on the page are
  shown.
* **Markdown dialect**: tables, fenced code with highlighting, a table of
  contents, line breaks kept, forgiving lists, plus `[[video url="…"]]`,
  `[[canvas slug="…"]]` and `[[kanban board="…"]]` on their own line, bare
  YouTube/Vimeo links on their own line, and `@username` mentions.
* **Page actions** menu: rename, change address (links in other pages
  are rewritten), move to another category, make home page, hide from
  navigation and search, print, delete.
* **Search** at `/search` over a full-text index: all words must match,
  `"exact phrase"`, `-excluded`, and the fields `title:`, `content:`,
  `category:`, `slug:`, `type:page|category`; sort by relevance, title or
  recent edits. The sidebar search filters titles instantly.
* **Categories**: create, rename, move into another category, reorder (drag
  and drop on **All pages**), sequential navigation, delete (its pages are
  moved, left without category, or deleted; subcategories move up).
* **Images** are stored once in the uploads folder; unused images are removed
  by a job after 24 hours.

Settings: per-user daily upload quotas and the upload rules (**Admin → Site
settings → Uploads**). Permissions: `page.*`, `category.*`, `search.*` (see
[permissions](permissions.md)).

## Page history

Every save, rename or revert stores the full page in `page_history`.
`/page/<slug>/history` lists versions; each version can be viewed rendered,
as Markdown, or as a difference to the previous one, and restored (the
current text stays in the history). Differences are computed within a fixed
work budget: when two versions are too different (or too repetitive) to
compare in detail, the part that could not be compared shows as removed and
re-added as a whole, with a note. Holders of the permissions can delete
entries, clear a page's history, credit entries to another account or remove
the author.

Permissions: `history.view`, `history.revert`, `history.delete`,
`history.transfer` (the last three also need edit access to the page).

## Drafts

While someone edits a page, the editor autosaves their text as a private
draft. Reopening the editor offers to restore or discard it; other editors'
open drafts are listed. Saving the page clears the drafts on it and credits
their authors in the edit summary. **My drafts** (`/drafts`) lists your
drafts; **Take over draft** in the editor makes another editor's draft yours.

Setting: `draft_expiration_hours` (**Admin → Site settings → Content and
exports**; 0 keeps drafts forever). Permissions: `draft.create`, `draft.view_own`,
`draft.delete_own`, `draft.transfer`.

## Attachments

Files attached to a page, listed under it and in its editor, downloadable one
by one or as a ZIP. Downloads always check that the reader may see the page.

Settings (**Admin → Site settings → Uploads**): `upload_mode` (`allow_all`,
`whitelist` or `blacklist`) with the extension lists, `upload_max_size_mb`
(owned by the host on managed hosting), per-user daily upload quotas.
Operator limit: `BW_MAX_ATTACHMENT_SIZE_BYTES`. Permissions:
`attachment.upload`, `attachment.view`, `attachment.delete_own`,
`attachment.delete_any`.

## Difficulty tags

A level shown on a page (beginner, easy, intermediate, expert, extra) or a
custom label with a colour. Permissions: `tag.edit_difficulty`,
`tag.edit_custom` (plus edit rights on the page).

## Page export

* **PDF** (`/page/<slug>/export-pdf`, and for old versions from the history):
  generated on the server with fpdf2 from the same sanitised HTML as the page.
  Images are included only when they were uploaded to this wiki; nothing is
  fetched from the network. Install DejaVu fonts (`fonts-dejavu-core`) for
  characters outside Latin-1. Needs `pdf_export_enabled` and the
  `page.export_pdf` permission.
* **Markdown** (`/page/<slug>/export-md`) and **import into a page**
  (`/page/<slug>/import-md`). Needs `markdown_export_enabled`.
* **Bulk Markdown** (**Admin → Bulk Markdown**, administrators): export every
  page as `<category folders>/<slug>.md` with front matter, embedded images
  and attachments in one ZIP; import `.md` files or ZIPs of them (folders
  become categories). Built on disk, never in memory; archives are checked for
  size, entry count, compression ratio and unsafe paths.

## Page builder

A visual editor (`/page/<slug>/builder`) that builds a page from blocks. The
document is stored in `pages.builder_json` and a Markdown version in the page
content, so search, history and exports keep working.

* Blocks: heading, text (Markdown or plain), list, quote, callout, code
  (highlighted), table, image, gallery, YouTube, banner, columns (2–4), cards,
  button, questions (expandable FAQ), divider, spacer, a **page list** (chosen
  pages, a category or recently updated pages) and a **canvas or Kanban
  embed**. Page lists and embeds are resolved for each reader with their own
  permissions, so they never show a page, canvas or board the reader cannot
  open. The page lists of one page show at most 48 pages together; they are
  left out of the Markdown version.
* Every block has layout options from a fixed list: alignment, a background
  from the theme palette and spacing. Layouts follow the width the page really
  gets, so columns and cards stack on phones.
* Starter layouts (landing page, guide, questions and answers), undo/redo,
  duplicate, collapse, drag and drop with a drop marker or the keyboard (arrow
  keys, Home and End on a block's handle), and a preview at desktop, tablet and
  phone widths.
* Drafts are saved automatically and refused once the page has changed since
  the draft started; the editor then says so instead of losing work silently.
* Checks: images need alternative text (a single image may be marked
  decorative), and the editor points out skipped heading levels, extra main
  headings, tables without a header row, empty blocks and embeds of switched-off
  features. Builder headings get anchors, so the page's contents list works.
* The server validates every document against an allow-list (block types, keys,
  lengths, links, uploaded images only, fixed layout values) and escapes or
  sanitises everything it renders. Documents are version 2; version 1 documents
  from older releases are upgraded when read and keep rendering unchanged.
* The embed block offers a searchable picker (type part of a name, arrow keys
  and Enter) of the canvases and Kanban boards you can open, listed with those
  features' own access rules; the preview shows each embed live, exactly as
  readers with your permissions will see it. The address can still be typed by
  hand.
* **Copy and paste**: a block's copy button puts it on the clipboard; paste it
  on this or any other builder page with *Paste blocks* or Ctrl/Cmd + V outside
  text fields. Pasted blocks are validated by the server like any document.
* **Saved sections**: administrators save a run of blocks (from block *n* to
  *m*) under a name; everyone who may use the builder can insert it on any page.
  Inserting copies the blocks, so deleting a section never changes pages.
* Image uploads show their progress, and refused files (wrong type, too large)
  say why next to the image field.

Who may use it: the feature on, `page_builder_access` reached (administrators,
editors or everyone), and the right to edit that page (so plain users cannot
edit pages through it). Publishing goes through the normal page service
(history, conflicts, protection). Settings on **Admin → Page builder**.
Builder pages are hidden from anonymous visitors unless marked public by an
administrator, and always under `BW_FORBID_PUBLIC_BUILDER_PAGES`.
`BW_FORBID_PAGE_BUILDER` switches it off.

## Page governance

Two tools for teams where pages have owners:

* **Protection** (`page_protection_enabled`): an editor protects a page and
  becomes its controller; nobody else, administrators included, may edit or
  delete it. An administrator who needs it files an unlock request and may
  remove the protection 72 hours later; the controller can release it
  earlier. Never applies to the home page.
* **Reservations / check-outs** (`page_reservations_enabled`): an editor
  reserves a page for `page_reservation_duration_hours`; others cannot edit or
  delete it meanwhile (administrators can, with a warning). Releasing starts a
  cooldown (`page_reservation_cooldown_hours`). Each editor has a quota
  (`default_reserved_pages_quota`) and can request a bigger one; requests up to
  `reservation_quota_auto_approve_max` are approved automatically.

Pages: **Reservations** (`/reservations`), **Reservation quota**
(`/settings/reservation-quota`), **Admin → Check-outs** and
**Admin → Page governance**.

## Contribution approval

People who may read a page but not edit it (`contribution.propose`) propose an
edit with a reason from the page (`/page/<slug>/propose-edit`). Reviewers
(`contribution.review` plus edit rights on the page, and administrators)
approve or deny it at `/admin/contributions`; approving applies the edit
credited to the proposer. An approval applies exactly the version the reviewer
opened: if the proposal or the page changed in the meantime it is refused and
the review shows the current state. The review lists at most 5000 lines of the
diff and counts the changed lines it leaves out; approving still applies the
whole proposal, not only what was shown. Each user has a quota of waiting
proposals (`default_contribution_quota`, requests up to
`contribution_quota_auto_approve_max` approved automatically). Old proposals
expire. **My contributions** at `/my-contributions`.

## Deletion slowdown

Deleting a page hides it for 48 hours instead; **Admin → Pending deletions**
restores or purges it. Holders of `page.delete` still see waiting pages. With
`docs_bypass_deletion_slowdown`, pages of the built-in documentation category
are deleted at once. While the feature is off, deletions are immediate and
nothing is purged; waiting pages stay waiting until it is switched on again.

## Temporary items

**Admin → Temporary items** schedules, for a date and time:

* deleting a page;
* hiding or showing a page now and switching it back later;
* deleting an account;
* returning an account to its previous role (a temporary promotion).

A scheduled page deletion can show a countdown on the page. The last
administrator and owner are never deleted or demoted. While the feature is
off every schedule pauses.

## Announcements

Site-wide banners (**Admin → Announcements**): text with colour and size,
shown to signed-in visitors, anonymous visitors or both, optionally only to
(or to everyone except) chosen accounts, with an optional expiry and
countdown. Visitors can dismiss a banner; editing it shows it again. Each has
its own page at `/announcements/<id>`.

## Assessments

A quiz or poll attached to a page (`/page/<slug>/assessment`): single choice,
multiple choice and free-text questions, points for right and wrong answers,
one or several attempts, and people or roles that may not take it. Scoring is
automatic; a free-text question without accepted answers is an open question
that scores nothing. Managers see results and can reset a person's attempts.
The answer key is shown only to managers, and to takers who cannot try again.
**My quiz points** (`/settings/assessment-points`) lists a member's results.

Permissions: `assessment.view`, `assessment.manage`. Settings on
**Admin → Assessments** (including a badge for points).

## Kanban boards

Boards (`/kanban`) with columns and tickets: descriptions in Markdown, labels,
priorities, due dates, colours, several assignees, comments (up to 300 per
ticket), attachments and description history. A title shorthand fills fields
as you type: `Fix login @alice +backend !high color:red due:tomorrow`.

* Changes appear for everyone viewing the board within seconds (the page
  fetches only what changed, and catches up at once after a lost connection).
* **Planning**: checklists (subtasks, up to 100 per ticket) with progress on
  the card; optional work-in-progress limits per column (a warning, never a
  block); due-soon (within 2 days) and overdue highlighting. A column can be
  marked as a **done column** (the check-mark button among the column tools,
  *Done column*): its tickets keep their due dates but are never overdue or
  due soon, on the board, in the due-date filters and on *My tickets*.
* **Filter bar**: search text, `@person`/*Me*/unassigned, label, priority and
  due date range, kept in the address (`?q=&who=&label=&priority=&due=`);
  `/` focuses the search. Keyboard moves skip hidden cards. The board state
  (`/api/kanban/<id>/state`) and *My tickets* accept the same parameters and
  apply the same rules on the server (invalid values answer 400).
* **Swimlanes** (`?lanes=assignee|priority|label`): rows per assignee,
  priority or label. Dragging a card to another row (or moving it past the
  row's first/last card with the keyboard) changes its assignee or priority;
  label rows only group.
* **Archive**: archiving a ticket (from its dialog, in bulk, or a whole
  column at once) takes it off the board but keeps it with its comments and
  files; the board's *Archived* panel opens, restores (at the end of its
  column) or deletes them for good. Archived tickets cannot be moved and are
  left out of *My tickets*. The owner can archive a board: it leaves the board
  list (*Show archived boards* brings it back into view) and is read-only
  until restored.
* **My tickets** (`/kanban/mine`, JSON at `/api/kanban/my-tickets`): tickets
  assigned to you on every board you can open, grouped by due date (tickets
  in done columns apart, under *In done columns*), with a filter form.
* **History**: every change stores the board's structure (including WIP
  limits, checklists and archived tickets); each version lists what changed
  since the previous one. Restoring a version never deletes tickets, comments
  or attachments (tickets created later stay on the board) and brings back
  the archived state of the tickets it knew. The newest 200 entries are kept,
  within 64 MB per board (a large board keeps fewer versions, always the
  latest one). Moves of a ticket, reorderings and checklist changes of a
  ticket by the same person within two minutes share one entry. Each ticket
  keeps its newest 100 description changes, within 1 MB.
* **Sharing**: private, shared or public boards; a role or a person gets view
  or write access. Board settings, sharing and deletion belong to the creator
  and administrators.
* **Deleted accounts**: deleting an account (by its holder, by an
  administrator, in a merge, or from the hosting portal) keeps what it added.
  Its boards pass to the administrator who deleted it (otherwise to the
  longest-standing owner or administrator, active ones first), its tickets and
  comments to the owner of the board they are on (each comment headed by a
  note, in the site's language, naming the deleted author), and it leaves
  assignee lists and shares. Each board that changed records the hand-over in
  its activity (and in its history when its state changed), except when the
  hosting portal removes the user. This also happens while Kanban is switched
  off. With no other owner or administrator left, the account's own boards are
  deleted with it.
* Export and import as JSON (or ZIP with attachments), the 1.4 format plus
  column limits, checklists and archived state. A ZIP holds at most 4,999
  attachments and 1 GB; a board with more can still be exported as JSON
  without its attachments (**More → Export without attachments**, also
  offered for any board). An import unpacks only the files its
  tickets refer to, streamed into storage, and skips those that no longer fit
  in the storage quota; its `board.json` is held to the 20 MB of a JSON
  import. Imports refuse archives with more than 5,000 files, encrypted or
  unusually compressed members, or members that cannot be read.
* **Events** for other features and webhooks:
  `kanban.board.created|updated|deleted`,
  `kanban.ticket.created|updated|moved|deleted` (bulk changes: one per ticket)
  and `kanban.comment.created`, emitted after the change is saved.
* Embed a read-only board in a page with `[[kanban board="<id>"]]`.

Settings (**Kanban → settings** for administrators): `kanban_access` and
`kanban_write_access` (weakest role with global view/write access:
administrators, editors or everyone), `kanban_open_access`,
`kanban_public_access_enabled` (anonymous read of public boards in public
mode). Permissions: `kanban.view`, `kanban.create`.

## Canvases

Free-form visual boards (`/canvas`): notes, shapes, images, videos, code and
links to wiki pages, connected by edges.

* **Editing together:** several people can edit at once. Small operations
  are merged on the server; a whole-document save made on an outdated copy
  is refused instead of overwriting others.
* **History** with restore. Changes by one person within 15 minutes, or by
  anyone within a minute of the latest entry, share one entry, which keeps
  the name of the person who started it.
* **Sharing** per person or role: view, edit, or an explicit "none".
* **ZIP export and import** with images, and embeds in pages with
  `[[canvas slug="…"]]`.
* **Wiki-page nodes** store only the id of their page. The title, address
  and excerpt are read when the canvas is shown, so renaming or deleting the
  page updates the node. Readers who may not open the page see neither its
  title nor whether it still exists (only administrators see "deleted");
  exports, the REST API and the personal data export follow the same rule.
* Notes and code are rendered on the server and cached per process. Code
  without a language is shown unhighlighted.

Limits:

* 2000 nodes and 5000 edges per canvas. A document listing more, counting
  invalid and repeated entries, is refused from an import, a whole-document
  save or the REST API.
* History keeps the newest 200 entries, and only as many as fit in 32 MB of
  snapshots per canvas.
* One view renders about half a million characters of notes and code; the
  remaining notes are shown as plain text.
* An import reads at most 7 MB of canvas data and one image per node.
* An export holds at most 2000 files and 512 MB of them; a canvas showing
  more is exported with a note in place of its files.

New canvases can start from a template (flowchart, mind map, retrospective
board, SWOT analysis). The editor has:

* a grid with snapping (G), alignment and distribution of the selection,
  groups that move together (Ctrl+G) and locks (L);
* alignment guides while dragging (edges and centres of nearby elements;
  Alt places freely) and an overview map;
* connections drawn curved, straight or at right angles, attached to the
  facing sides or to a chosen side (`from_side` / `to_side`: top, right,
  bottom, left; absent means automatic).

Everyone who can open a canvas can download it as a PNG or SVG image, made in
the browser. Images from other sites are loaded from their site with CORS,
never through the wiki; when that is refused the PNG shows a labelled
placeholder and the SVG keeps a link. The **text outline**
(`/canvas/<slug>/outline`, also as Markdown with `?format=md`) lists every
element in reading order with its connections, for screen readers and quick
reviews.

**Locks are enforced by the server.** An operation that moves, resizes,
edits or deletes a locked element is refused unless an earlier operation in
the same batch unlocks it. A single operation that unlocks and changes an
element counts as a change and is refused. Locking, unlocking, restacking
(`layer`) and grouping stay allowed for every editor.

* The editor skips refused operations, reports them (`rejected` in the
  `/ops` answer) and reloads.
* The REST API and whole-document saves refuse the whole request with the
  error code `locked`.
* History restores are not blocked.
* A locked wiki-page node keeps its page, whatever page an operation or save
  names for it. A wiki-page node sent without a page keeps the stored one.

Events (after commit): `canvas.created`, `canvas.updated` (once per
operation batch, save, restore, change of title, visibility, sharing or
owner, or update of a node's page link) and `canvas.deleted`, with `canvas`
(the layout row) and `actor_id`. The web interface and the REST API emit the
same events.

Settings (**Admin → Canvas**): `canvas_access`, `canvas_write_access`,
`canvas_open_access`, `canvas_public_access_enabled`. Archived canvases are
visible only to their creator and administrators. Permissions:
`canvas.view`, `canvas.create`.

## Chats and groups

* **Direct messages** (`/chats`) between two members and **group chats**
  (`/groups`) with invite codes, a global room, owner/moderator/member roles,
  timeouts, bans, ownership transfer, and administrator takeover.
* File attachments (with a size limit and a daily count), plain-text or ZIP
  export, clearing a conversation. **Clearing a direct conversation hides it
  for you only**: the other participant keeps their copy, and messages both
  have cleared are erased. Group moderators clear a group for everyone.
* **Reporting**: participants and group members report another person's
  message to the administrators, who see a copy of it even if it is deleted
  later.
* **Blocking**: a member blocks another from a conversation, a group's member
  list or **Settings → Blocked people**, where they also unblock. Messages
  from the blocked member are withheld: they look sent but are never
  delivered, not even after an unblock. The blocked member cannot start a
  conversation with the blocker or add them to a group, and the blocker
  cannot message the blocked member until they unblock. In shared groups the
  blocked member's messages are hidden from the blocker. Nobody is notified,
  blocks are not in the audit log, and only the blocker sees the list (also
  in their data export). The administration pages show withheld messages,
  marked as such.
* Pages load the latest messages, then fetch only new ones, and poll less
  while the tab is hidden.
* **Deleting a message erases its text and attachments for everyone**; a
  "deleted" marker keeps the conversation readable. In a direct conversation
  the sender can do so for 15 minutes after sending (to take back a
  mistake); later the message belongs to the other participant's copy too.
  Deleting an account is the exception: it removes the account's direct
  conversations for both participants (a reported message keeps its copy in
  the report).
* Members may show their groups on their profile
  (`profile_group_badges_enabled`).

Administrators monitor conversations and groups, review reported messages,
clear a direct conversation for both participants when moderating, and
configure everything at **Admin → Chats** (`/admin/chats`). Opening a private
conversation or a group they are not in, downloading its attachments,
exporting, clearing or taking it over, and resolving a report are recorded in
the audit log. Settings: message length, attachments, direct
messages and groups on/off, whether members may start them, and separate
retention policies for messages and attachments of direct messages and groups
(applied daily at `chat_cleanup_hour`, every `chat_cleanup_frequency_days`).
**Admin → Users** can switch chat off for one account. Permissions:
`chat.dm`, `chat.group`, `chat.create_group`, `chat.upload`.

## Read aloud

An audio player under each page: readers press **Generate audio**, a worker
synthesises the page with a neural voice (Piper) on the server or on a GPU
server, and the audio is kept until the page changes. Speeds from 0.75× to
2×, download as audio file, automatic generation after edits (optional),
anonymous listening in public mode (optional), and a choice of languages the
wiki may speak. **Admin → Read aloud** shows the queue and dependencies.

See [read aloud](tts.md) for installation and configuration.

## People and profiles

* **Settings** (`/settings`): profile (real name, bio, birth date, avatar),
  user name, password, language, display preferences (theme, text size,
  line and letter spacing, content and sidebar width, contrast, colours,
  background picture, dyslexia-friendly font, reduced motion, the
  [mascot](#mascot)'s sunglasses), sessions (list, end one,
  sign out everywhere, clear history), two-step verification
  (`/settings/two-step`: authenticator app and recovery codes), API tokens,
  personal data export, account merges, and deleting the account.
* **Renames:** renaming or deleting an account leaves pages and drafts as
  they are.
  * A former name stays reserved for its account, and mentions of it
    (`/users/<old name>`) lead to the renamed account.
  * An administrator can release a former name on the account's **History**
    page (**Admin → Users**). The release is recorded in the audit log, and
    old mentions stop leading to the account.
  * Members can rename themselves at most 3 times a day.
* **People** (`/users`) lists members; `/users/<username>` shows a profile
  with contributions, badges and custom fields when the owner published it.
* **User profiles** feature (`user_profiles`): published profile pages,
  administrator-defined profile fields (**Admin → Profile fields**) and custom
  tags. While it is off, profiles are visible only to their owners and
  administrators. Permissions: `profile.view`, `profile.edit_own`. Setting:
  `profile_contribution_chart_enabled`.
* **Account merges**: a member asks to merge another account into theirs
  (both confirm with their passwords), an administrator approves
  (**Admin → Account merges**), and everything the source account owns moves
  to the target. Administrators can also merge directly
  (`/admin/users/merge`). The source is then deleted, or kept locked: renamed
  `merged_<name>`, made a plain user with an unknown password, signed out,
  its API tokens revoked and suspended so that only an administrator can
  reactivate it. A merge rewrites the source's `@mentions`, with a history
  entry per page. The last owner and the last active administrator cannot
  be merged away.

## Badges

Awards shown on profiles. Administrators create badge types
(**Admin → Badges**) and award or revoke them by hand, or let them be earned
automatically: first edit, number of edits, categories edited, pages created,
days of membership, minutes spent reading. Automatic badges are checked by a
job and when the author saves or signs in. Members are notified of new badges.
A revoked badge is not awarded again automatically; deleting the award instead
lets its trigger award it again.

## Leaderboard

`/leaderboard` ranks members by contributions over a period (score, edits,
characters added and removed, streaks), counting only pages the viewer may
see, with CSV export. Revision sizes are computed once in the background.

## Mascot

A pixel-art banana in the top bar, in place of the logo, for every signed-in
person; visitors who are not signed in see the logo. It bobs, blinks and hops
when clicked (Enter and Space too), and stands still with reduced motion.
Eleven clicks on the same page put sunglasses on it for that account, and
eleven more take them off. **Customise** takes them off too;
**Admin → Appearance** puts them on or takes them off for every account, and
records it in the audit log. Switch the plugin off (or deny it with
`BW_MANAGED_PLUGIN_DENYLIST`) to show the logo for everyone. While it is on, the
Konami code sends a short shower of small bananas, and the mascot peeks into
the "page not found" card.
The hosting portal's top bar shows the same mascot to everyone; there the
sunglasses are kept in the browser.

## Personal data export

**Settings → Your data → Download my data** (`/settings/export`) downloads a
ZIP of everything the account owns: account, profile, preferences, its own
records (edits, drafts, proposals, messages, boards, canvases, sessions,
tokens …) and its uploaded files. It never contains password hashes, tokens or
other secrets (two-step verification appears only as whether it is on, since
when and how many recovery codes are left), what administrators keep to
themselves (hidden suspension reasons and end times, impersonation records,
internal tags, who changed a role), or content the account can no longer read:
rows of pages, boards, canvases and groups it has lost access to keep only
their ids and times. Administrators can export any account
(`/admin/users/<id>/export`).

## Custom pages

Administrator-made pages at any free address of the wiki (for example
`/imprint` or `/download`), public by design even on a private wiki when
published. Types: redirect, HTML, styled HTML, HTML with scripts, Markdown,
wiki page, plain text, JSON, XML, image, image page, YouTube video, hosted
video, file download, file listing, iframe, page embed, link list, code
snippet, and **visual builder page**. Author HTML, CSS and JavaScript run in a
sandboxed document with no access to the wiki's cookies. A custom page can
never take over an address the wiki uses. **Admin → Custom pages**; permission
`custom_page.manage` (administrators only); setting
`custom_pages_max_video_size_mb`.

A host can forbid that: under `BW_FORBID_PUBLIC_MODE` published custom pages,
their files, redirects and sandboxed documents are for signed-in members only,
and under `BW_FORBID_PUBLIC_BUILDER_PAGES` so are visual builder pages.
Anonymous visitors are sent to sign in, as on the rest of the wiki.

A visual builder page is edited in the page builder (**Edit in the visual
builder**, available while the page builder is on) with the same blocks,
checks and safe renderer as builder wiki pages. Custom pages have no drafts:
changes stay in the editor until you save, and a save is refused when the page
changed since the editor was opened. The page is stored as a Markdown wiki
page plus its document (`custom_pages.builder_json`), so while the page builder
is off, or on a release that does not know the builder, it shows its Markdown
version. Choosing the type for an existing Markdown page turns its text into
blocks; choosing another type ends the builder page and keeps the Markdown.

## REST API

A JSON API under `/api/v1` with personal tokens, scopes, an audit log and
"userbot" automation accounts. See [API](api.md).

## Federation

Pair with other BananaWiki installations and share single pages read only.
Operator switch `BW_FEDERATION_ENABLED=1`. See [federation](federation.md).

## Audit log

**Admin → Audit log** (`/admin/audit`) lists security-relevant actions with
filters; owners and superusers set the retention in days and are the only
ones who can switch the log off. See [security](security.md#audit-log).

## Administration

Everything under **Admin** (`/admin/dashboard`), for administrators:

* **Dashboard**: accounts, pending approvals, suspensions, traffic.
* **Users**: create, search and filter accounts; roles, custom roles,
  permissions, category access; suspend, reset passwords, end sessions,
  impersonate, edit attributions, per-account audit (where a former user
  name reserved for the account can be released), whether the account uses
  two-step verification (owners and superusers reset it for an account that
  lost its app and recovery codes). Pending sign-ups are approved or denied
  here.
* **Custom roles**, **Invite codes** (with a number of uses, an expiry and an
  assigned role or custom role; editors granted the `invite.*` permissions
  manage their own codes and can only hand out a plain member account or what
  they hold themselves: no permission or category beyond their own),
  **Sessions** (every active session, end one, sign everyone out, daily
  automatic sign-out of users and editors at an hour of the site time zone).
  The account hierarchy applies throughout: a plain administrator neither sees
  the addresses of nor signs out owners, superusers and other administrators,
  and the automatic sign-out, which any administrator may set, leaves all
  three signed in.
* **Site settings** (`/admin/settings`): name, time zone, public mode (with
  end date and message), open sign-up (with end date), approval of sign-ups
  (and how long pending/denied sign-ups are kept), maintenance mode and
  message, one session per account, bot protection, whether suspended users
  may delete their account, two-step verification required of administrators
  (owners and superusers only), the app chooser after sign-in, the introduction
  and tour for new members, upload rules and quotas, draft expiry, PDF and
  Markdown export, page builder, profile contribution chart.
* **Appearance**: dark and light colour themes, default theme, favicon
  (the mascot's banana in one of eight colours, or uploaded), theme export
  and import, and the [mascot](#mascot)'s sunglasses for every account.
* **Languages**: the interface ships in English, Italian and German, all
  three switched on; upload further languages as JSON files (or one for a
  built-in language, which overrides its strings until removed), switch them
  on and off, choose the default.
* **Documentation**: add the built-in user guide (full or simplified,
  English, Italian or German) as a category of pages, or download it as
  Markdown.
* **Site migration** (owners and superusers): export the whole wiki (database
  and files) as one ZIP, or import one, which **replaces everything**,
  accounts included. Both ask for your password. Imports accept 1.4 archives,
  never touch code or the secret key, check the database first, drop triggers
  and views this release would not create, keep the live audit log (without
  doubling the entries the archive already holds), keep a copy of the current
  site in `<instance>/backups/pre-import-<time>/` and restore it if anything
  fails. An archive with encrypted files, or with files compressed other than
  stored or deflated (the only methods exports use), is refused unread.
  Two-step verification set up on another wiki cannot be checked without that
  wiki's secret key, so the import turns it off for those accounts (named in a
  notice and in the audit log); they sign in with their password and can set
  it up again. Needs `BW_ALLOW_SITE_IMPORT` (on by default except under
  managed hosting).
* **Server**: restart and error log.
* **Plugins** (`/admin/plugins`): switch features, install third-party plugins
  (owner and superusers only, and only with `BW_ALLOW_EXTERNAL_PLUGINS=1`; see
  [plugins](plugins/README.md)), order the sidebar apps.
* **Bulk delete** (`/admin/bulk`): delete many categories, pages, canvases or
  boards at once, with the same checks and events as single deletions. The
  server asks for a typed confirmation word (`DELETE`, or the interface
  language's own, such as `ELIMINA` in Italian) and every bulk deletion is
  recorded in the audit log.

## Needs your attention and notifications

Everything that waits for someone's decision is collected in one place:

| Queue | Who acts on it | Where |
|---|---|---|
| Sign-ups waiting for approval | administrators | Admin → Users (pending) |
| Reservation quota requests | administrators | Admin → Check-outs |
| Proposed edits to review | reviewers (`contribution.review` and edit rights on the page) and administrators | Contributions review list |
| Contribution quota requests | administrators | Contributions review list |
| Pages waiting to be deleted | administrators | Admin → Pending deletions |
| Account merges to approve | administrators | Admin → Account merges |
| Plugin changes waiting for a restart | administrators | Admin → Plugins |

* A **red dot with the number** of waiting requests sits on the account
  avatar, and the account menu starts with a **Needs your attention** section
  listing each non-empty queue. Screen readers hear the count in the menu
  button's label. People only see queues they may act on (a reviewer sees the
  proposals on pages they may edit, never sign-ups).
* The **first page after signing in** shows a banner ("3 requests need your
  review") when something is waiting; it can be closed and does not return
  until the next sign-in.
* **`/attention`** lists every queue with its count, how long the oldest item
  has been waiting, and a link to review it. The admin dashboard shows the
  same list at the top.
* People whose **sign-up, quota request or proposed edit** was decided see a
  notice at the top of the page (and in `/attention`) until they dismiss it.

**Email notifications** (off by default) are set up in **Admin →
Notifications** (`/admin/notifications`):

* who: every administrator and reviewer who has an email address in their
  account settings and did not turn the emails off; everyone can opt out in
  **Settings → Email and notifications** or with the link in each email;
* when: **immediately** for each new request, a **digest** when new requests
  arrive but at most every N minutes, or a **daily summary** at a chosen hour
  (site time zone) while anything is waiting;
* decisions: optionally, people are emailed when their own request is
  approved or denied (a person waiting for approval can leave an address on
  the "account pending" page);
* the mail server (SMTP with STARTTLS or SSL/TLS, Brevo or Resend) and the
  wiki's public address for links, unless the host set them with
  [environment variables](configuration.md#email); a **Send a test email**
  button (five per hour) checks the setup.

Emails are sent by the `attention.notify` background job, never while a page
loads. They are written in each recipient's interface language, contain counts
and links only (never page content) and are throttled: each new request causes
at most one email, and what each person was last told is kept in
`attention_recipients`.

## Sign-in and onboarding

`/login` (with "remember me"), `/login/two-step` (the authenticator or
recovery code of accounts with two-step verification), `/signup` (invite
codes, open sign-up, approval, bot protection), `/setup` for the first
account, account status pages for suspended, pending and denied accounts,
forced password change, the administrator's first-run wizard, an introduction
and a guided tour for new members (the tour illustrates what each role can do;
it never changes what the account can see), and sign-in with the hosting
portal on hosted wikis.
