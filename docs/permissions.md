# Roles and permissions

Who may see and do what. Everything on this page applies equally to the web
interface and to the [REST API](api.md).

## Roles

Every account has one base role:

| Role | What it is for |
|---|---|
| `user` | Reading and taking part: creating pages, boards and canvases and working on their own, chats, quizzes, proposing changes to other people's pages. |
| `editor` | Also everyone's content: edits every page, board and canvas they can open, and manages categories. |
| `admin` | Everything: holds every permission and sees every category, manages accounts and the site. |
| `owner` | An administrator that other administrators cannot change. |

Two more protections sit on top:

* **Superuser** (`users.is_superuser`): only superusers change a superuser's
  account, grant or remove superuser status, and impersonate administrators
  and owners. The account created at `/setup` is an owner and a superuser.
* **Hierarchy of administrators:**
  * nobody changes their own role, suspends or deletes themselves in the admin
    pages (an owner may step down while another owner remains);
  * an owner is changed only by themselves; only owners and superusers make
    someone an owner;
  * an administrator is changed (role, password, suspension, deletion) only by
    an owner or a superuser;
  * the last owner can never be demoted or deleted.

Roles are changed on **Admin → Users → (account)**, which also suspends
(permanently or until a time, with an optional visible reason), resets
passwords, ends sessions, switches off chat for the account, and opens the
account's permissions and category access.

## Where a user's permissions come from

Administrators and owners hold every permission. Users and editors get
theirs in two steps:

1. **What their role gives**: their **custom role**, if they have one,
   otherwise the **defaults of their base role**: the built-in defaults (the
   tables below), as this wiki's administrators changed them on **Admin →
   Roles → User** or **Editor** (**Restore built-in defaults** there undoes
   the changes).
2. **Their own changes**, on **Admin → Users → (account) → Permissions**.
   A permission ticked or unticked there differently from the role is kept as
   that person's own change (marked *changed*); everything else keeps
   following the role, also when the role's defaults or the custom role
   change later. **Remove their own changes** puts the person back on their
   role alone. Changing someone's role or custom role removes their own
   changes too.

The account page (**Admin → Users → (account)**) sums this up under **What
<name> can do**: where the permissions come from, the categories the person
reads and writes in, what they may do with pages, boards and canvases, and
what was added or taken away for them.

Before schema 26, saving an account's **Permissions** page replaced the
role's whole set with a copy that no longer followed the role. The upgrade
keeps exactly what each such account had: it stores every permission as
that account's own change, so its access stays the same until an
administrator saves its page again or removes its own changes.

A permission that belongs to a feature that is switched off is never granted,
whatever the settings say. Granting some permissions also grants the ones they
need (see [implied permissions](#implied-permissions)); taking a permission
away from a base role also takes away the ones that need it.

**Which permissions a `user` may hold.** Everything except the powers over the
wiki's structure and over other people's records: categories, page history
(revert, delete, transfer), hiding pages, reviewing proposed edits, taking over
drafts, deleting other people's files and invite codes. Those are for
editors ("Assignable to" in the tables). Working on content, one's own or
everyone's, is open to both base roles. Custom pages are for administrators.

Every permission in the catalogue is enforced by the pages and API calls it
describes. No site setting grants or withholds what a permission decides.

## The same verbs for pages, boards and canvases

| Verb | Pages | Kanban boards | Canvases | User default | Editor default |
|---|---|---|---|---|---|
| View | `page.view_all` | `kanban.view` | `canvas.view` | yes | yes |
| Create | `page.create` | `kanban.create` | `canvas.create` | yes | yes |
| Edit own | `page.edit_own` | `kanban.edit_own` | `canvas.edit_own` | yes | yes |
| Delete own | `page.delete_own` | `kanban.delete_own` | `canvas.delete_own` | yes | yes |
| Share own | — | `kanban.share_own` | `canvas.share_own` | yes | yes |
| Edit all | `page.edit_all` | `kanban.edit_all` | `canvas.edit_all` | no | yes |
| Delete all | `page.delete_all` | `kanban.delete_all` | `canvas.delete_all` | no | no |

"Own" is what the person created (`pages.created_by`, a board's or a
canvas's creator; pages created before version 23 have no recorded creator).
"All" is everything they can open. Administrators do everything. Pages also
keep the category limits below, and changing someone else's page's title,
address or category needs `page.edit_metadata`. Someone who may not edit a
page may propose a change (`contribution.propose`, while proposed edits are
switched on).

## Boards and canvases: who sees them

Boards and canvases follow one sharing model:

* **Visibility.** *Only people it is shared with* (`private`, the default
  for new ones), *All members* (`members`: every member who holds the view
  permission) or *Everyone* (`public`: also visitors without an account
  while the wiki's public mode is on).
* **Shares.** The creator (with *share own*) and administrators share with
  one person or one base role (`user` or `editor`), at *Can view* or *Can
  edit*. The higher of a person's own share and their role's share applies.
  Administrators always have access.
* **Changing** an item needs an edit share, *edit own* for its creator, or
  *edit all*. **Deleting** (and archiving a board) needs *delete own* for
  its creator or *delete all*. **Sharing** and handing an item over need
  *share own* (creator) or administrator.

Archived boards are read-only for everyone; archived canvases are visible to
their creator and administrators only.

## Category access

Permissions say *what* someone may do; category access says *where*.

* **Read access** is either unrestricted or a list of allowed categories.
  Restricted readers do not see other categories, their pages, or pages
  without a category, anywhere: sidebar, search, links, API, exports.
* **Write access** is either unrestricted or a list, for users and editors
  alike. Writing to a listed category includes reading it; unrestricted
  writing means writing wherever one may read.
* There is no "private category" flag: a new category is visible to every
  unrestricted reader.
* Where a category is hidden but a subcategory is readable, the readable
  subcategory moves up in the navigation.

Set category access per account on **Admin → Users → (account) →
Permissions**, or for a whole group with a custom role. Both work together:
where the person's custom role does not limit reading or writing, their own
list limits it; where the custom role limits it, the categories on their own
list are added to the role's (**Also let them edit in these categories**).

Two permissions sit on top of read access:

* `page.view_all` is needed to read pages at all. Without it a user or
  editor reads no page anywhere (pages, navigation, search, previews,
  history, exports, API, pages received through federation).
* `category.view_all` is needed to see categories where they are listed:
  the navigation, subcategory lists, category search results and
  `GET /categories` of the API. Without it these show no categories (pages
  without a category stay in the navigation); a readable category still
  opens from a link or a page's breadcrumbs. Category choices in forms
  (page settings, page builder blocks) are not listings: they still offer
  the categories the user may write to or read.

Administrators always hold both; anonymous visitors in public mode need
neither. Both are in the defaults of users and editors.

## Custom roles

**Admin → Roles** lists the base roles and the custom roles. A custom role
(for example "Support editor" or "Read-only contractor") has

* a base role (`user` or `editor`),
* a set of permissions (limited to what the base role may hold),
* optional read and write category restrictions,

and is assigned to accounts on the role page or from the account page.
Invite codes can assign a base role or a custom role to the people who sign
up with them, with open sign-up too. When a custom role is deleted, its
members move to another custom role you choose (or the most similar one), or
keep their base role with its default permissions. Changing or deleting a
role changes its holders' accounts, so it is refused while one of them is an
account you may not change (for example a superuser).

Use the base roles' defaults for what everyone should do, custom roles when
several people need the same exceptions, and a person's own changes for one
person (they apply on top of a custom role too).

## Asking for edit access

Off by default. With **Let people ask for edit access** ticked on **Admin →
People → Access requests**, a signed-in member who can read a page but may
not edit it gets **Ask for edit access** where the Edit button would be, with
an optional message (at most 500 characters). What they ask for follows from
why they may not edit:

* **Editing pages in a category**, when their writing is limited to some
  categories and the page's category is not one of them;
* **Editing pages in this wiki**, when they may write in the page's category
  but their role does not let them edit other people's pages
  (`page.edit_all`).

A page without a category can only be edited by people who may write in every
category, which no request gives, so no button is offered there.

Open requests are listed on **Admin → People → Access requests** (and count in
the attention list and its emails) with what approving would change for that
person *now*. **Approve** gives exactly that as the person's own change, and
nothing else: the category is added to their own write list (on top of a
custom role's list, which stays as it is for everyone else), and
`page.edit_all` is added when they lack it, since editing the page needs it.
For someone whose writing is limited, `page.edit_all` applies in every
category they may write in. **Decline** changes nothing. Either way the person
gets a notice (and an email when the wiki sends decision emails), and the
request, the approval or the decline are in the audit log. Administrators
decide; there is no permission to manage people that others could hold.

One open request per person and scope; a declined request can be asked again
after 7 days; at most 5 requests a day per person. Requests can also be listed
and decided through the [REST API](api.md#requests-for-edit-access-users-administrators).
Hosting administrators acting as a member may ask on their behalf; deciding is
an administrator's action with the usual checks.

Edit access to a board or a canvas is given by its owner, who shares it
(see above): someone who may only view one is told who that is.

## When something is not allowed

A member who may not do something is told why, in plain words, rather than
getting a bare refusal: opening the editor of a page they may only read says
"You can read this page but not edit it." with the reason (the category is
limited to some people, pages without a category need writing everywhere,
their role edits only their own pages or none) and offers **Ask for edit
access** when that is on (otherwise: ask an administrator). Creating a page
where they may not says why too. The page's menu says it when nothing else
offers a way in. Boards and canvases they may only view say so and name the
owner who can share them for editing.

## Other controls

* Chat can be switched off for one account (**Admin → Users →
  (account)**); who may send direct messages, start conversations, take part
  in or create groups and send files are the chat permissions.
* Page governance: protected pages and reservations block editing by others.
* Temporary items: a role can be granted until a date and reverts
  automatically. Only owners and superusers can pause a revert of an
  administrator by switching the feature off; what a temporary
  administrator does meanwhile (such as creating other administrators) stays.

## Permission catalogue

The catalogue lives in `bananawiki/wiki/permissions.py`. "Feature" means the
permission is only granted while that feature is on. `page.delete` (before
version 23) is still understood as `page.delete_all`.

### Pages

| Key | What it allows | Editor default | User default | Feature | Assignable to |
|---|---|---|---|---|---|
| `page.view_all` | Read the pages in the categories you may read | yes | yes | — | users and editors |
| `page.view_deindexed` | Read pages hidden from navigation and search | yes | no | — | users and editors |
| `page.create` | Create new pages | yes | yes | — | users and editors |
| `page.edit_own` | Change the pages you created, their title, address and category too | yes | yes | — | users and editors |
| `page.delete_own` | Delete the pages you created | yes | yes | — | users and editors |
| `page.edit_all` | Change every page in the categories you may write to | yes | no | — | users and editors |
| `page.delete_all` | Delete any page you can edit | no | no | — | users and editors |
| `page.edit_metadata` | Change the title, address and category of any page you can edit | yes | no | — | users and editors |
| `page.deindex` | Hide pages from navigation and search | no | no | — | editors |
| `page.use_builder` | Lay out the pages you can edit with blocks | no | no | `page_builder` | users and editors |
| `page.export_pdf` | Download pages as PDF files | no | no | — | users and editors |

### Categories

| Key | What it allows | Editor default | User default | Feature | Assignable to |
|---|---|---|---|---|---|
| `category.view_all` | See the categories you may read in lists and search | yes | yes | — | users and editors |
| `category.create` | Create categories | yes | no | — | editors |
| `category.edit` | Rename and edit categories | yes | no | — | editors |
| `category.delete` | Delete categories | yes | no | — | editors |
| `category.reorder` | Change the order and nesting of categories | yes | no | — | editors |
| `category.manage_sequential` | Turn sequential navigation on or off | yes | no | — | editors |

### Page history

| Key | What it allows | Editor default | User default | Feature | Assignable to |
|---|---|---|---|---|---|
| `history.view` | Read earlier versions of pages | yes | yes | `page_history` | users and editors |
| `history.revert` | Restore an earlier version of a page | no | no | `page_history` | editors |
| `history.delete` | Delete entries from a page's history | no | no | `page_history` | editors |
| `history.transfer` | Credit an edit to another user | no | no | `page_history` | editors |

### Drafts

| Key | What it allows | Editor default | User default | Feature | Assignable to |
|---|---|---|---|---|---|
| `draft.create` | Save unfinished edits as drafts | yes | yes | `drafts` | users and editors |
| `draft.view_own` | List your own drafts | yes | yes | `drafts` | users and editors |
| `draft.delete_own` | Delete your own drafts | yes | yes | `drafts` | users and editors |
| `draft.transfer` | Take over another user's draft | no | no | `drafts` | editors |

### Attachments

| Key | What it allows | Editor default | User default | Feature | Assignable to |
|---|---|---|---|---|---|
| `attachment.upload` | Attach files to the pages you can edit | yes | yes | `attachments` | users and editors |
| `attachment.view` | Download page attachments | yes | yes | `attachments` | users and editors |
| `attachment.delete_own` | Delete attachments you uploaded | yes | yes | `attachments` | users and editors |
| `attachment.delete_any` | Delete anyone's attachments | no | no | `attachments` | editors |

### Page tags

| Key | What it allows | Editor default | User default | Feature | Assignable to |
|---|---|---|---|---|---|
| `tag.edit_difficulty` | Set a page's difficulty tag | yes | no | `difficulty_tags` | users and editors |
| `tag.edit_custom` | Set a page's custom tag | yes | no | `difficulty_tags` | users and editors |

### Proposed edits

| Key | What it allows | Editor default | User default | Feature | Assignable to |
|---|---|---|---|---|---|
| `contribution.propose` | Suggest changes to pages you cannot edit | no | yes | — | users and editors |
| `contribution.review` | Approve or deny suggested changes | no | no | — | editors |

### Kanban boards

| Key | What it allows | Editor default | User default | Feature | Assignable to |
|---|---|---|---|---|---|
| `kanban.view` | Open boards visible to members or shared with you | yes | yes | `kanban` | users and editors |
| `kanban.create` | Create new boards | yes | yes | `kanban` | users and editors |
| `kanban.edit_own` | Change the boards you created | yes | yes | `kanban` | users and editors |
| `kanban.delete_own` | Delete or archive the boards you created | yes | yes | `kanban` | users and editors |
| `kanban.share_own` | Choose who sees and edits the boards you created, or hand them over | yes | yes | `kanban` | users and editors |
| `kanban.edit_all` | Change every one of the boards you can open | yes | no | `kanban` | users and editors |
| `kanban.delete_all` | Delete or archive any of the boards you can open | no | no | `kanban` | users and editors |

### Canvases

| Key | What it allows | Editor default | User default | Feature | Assignable to |
|---|---|---|---|---|---|
| `canvas.view` | Open canvases visible to members or shared with you | yes | yes | `canvas` | users and editors |
| `canvas.create` | Create new canvases | yes | yes | `canvas` | users and editors |
| `canvas.edit_own` | Change the canvases you created | yes | yes | `canvas` | users and editors |
| `canvas.delete_own` | Delete or archive the canvases you created | yes | yes | `canvas` | users and editors |
| `canvas.share_own` | Choose who sees and edits the canvases you created, or hand them over | yes | yes | `canvas` | users and editors |
| `canvas.edit_all` | Change every one of the canvases you can open | yes | no | `canvas` | users and editors |
| `canvas.delete_all` | Delete or archive any of the canvases you can open | no | no | `canvas` | users and editors |

### Assessments

| Key | What it allows | Editor default | User default | Feature | Assignable to |
|---|---|---|---|---|---|
| `assessment.view` | Answer the quizzes attached to pages | yes | yes | `assessments` | users and editors |
| `assessment.manage` | Create and edit quizzes on pages | yes | no | `assessments` | users and editors |

### Profiles

| Key | What it allows | Editor default | User default | Feature | Assignable to |
|---|---|---|---|---|---|
| `profile.view` | Open other people's profile pages | yes | yes | `user_profiles` | users and editors |
| `profile.edit_own` | Edit your own profile page | yes | yes | `user_profiles` | users and editors |

### Chat

| Key | What it allows | Editor default | User default | Feature | Assignable to |
|---|---|---|---|---|---|
| `chat.dm` | Send and receive direct messages | yes | yes | `chat` | users and editors |
| `chat.start_dm` | Write to someone first | yes | yes | `chat` | users and editors |
| `chat.group` | Take part in group chats | yes | yes | `chat` | users and editors |
| `chat.create_group` | Create group chats | yes | yes | `chat` | users and editors |
| `chat.upload` | Send files in chats | yes | yes | `chat` | users and editors |

### Search

| Key | What it allows | Editor default | User default | Feature | Assignable to |
|---|---|---|---|---|---|
| `search.pages` | Search the wiki | yes | yes | — | users and editors |
| `search.users` | Search the member list | yes | yes | — | users and editors |

### Invite codes

| Key | What it allows | Editor default | User default | Feature | Assignable to |
|---|---|---|---|---|---|
| `invite.generate` | Create invite codes | no | no | — | editors |
| `invite.view` | List invite codes | no | no | — | editors |
| `invite.delete` | Delete unused invite codes | no | no | — | editors |

### Custom pages

| Key | What it allows | Editor default | User default | Feature | Assignable to |
|---|---|---|---|---|---|
| `custom_page.manage` | Create, edit and delete custom pages | no | no | `custom_pages` | administrators only |

### Implied permissions

| Granting | also grants |
|---|---|
| `page.view_deindexed` | `page.view_all` |
| `page.create` | `page.view_all` |
| `page.edit_own` | `page.view_all` |
| `page.delete_own` | `page.view_all` |
| `page.edit_all` | `page.view_all` |
| `page.delete_all` | `page.view_all` |
| `page.edit_metadata` | `page.view_all` |
| `page.deindex` | `page.view_all` |
| `page.use_builder` | `page.view_all` |
| `category.create` | `category.view_all` |
| `category.edit` | `category.view_all` |
| `category.delete` | `category.view_all` |
| `category.reorder` | `category.view_all` |
| `category.manage_sequential` | `category.view_all` |
| `contribution.propose` | `page.view_all` |
| `contribution.review` | `page.view_all` |
| `history.revert` | `history.view` |
| `chat.start_dm` | `chat.dm` |
| `chat.create_group` | `chat.group` |
| `history.delete` | `history.view` |
| `kanban.create` | `kanban.view` |
| `kanban.edit_own` | `kanban.view` |
| `kanban.delete_own` | `kanban.view` |
| `kanban.share_own` | `kanban.view` |
| `kanban.edit_all` | `kanban.view` |
| `kanban.delete_all` | `kanban.view` |
| `canvas.create` | `canvas.view` |
| `canvas.edit_own` | `canvas.view` |
| `canvas.delete_own` | `canvas.view` |
| `canvas.share_own` | `canvas.view` |
| `canvas.edit_all` | `canvas.view` |
| `canvas.delete_all` | `canvas.view` |
