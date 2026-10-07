# Roles and permissions

Who may see and do what. Everything on this page applies equally to the web
interface and to the [REST API](api.md).

## Roles

Every account has one base role:

| Role | What it is for |
|---|---|
| `user` | Reading, taking part (chats, boards, quizzes, proposing edits). Cannot edit pages. |
| `editor` | Writing: pages and categories, in the categories they may write to. |
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

  In 1.4 any administrator could demote, suspend or reset the password of
  another administrator; that is no longer possible.

Roles are changed on **Admin → Users → (account)**, which also suspends
(permanently or until a time, with an optional visible reason), resets
passwords, ends sessions, switches off chat for the account, and opens the
account's permissions and category access.

## Where a user's permissions come from

Administrators and owners hold every permission. For users and editors the
first of these that applies decides:

1. their **custom role**, if they have one;
2. their **individual permissions**, if an administrator set any
   (**Admin → Users → (account) → Permissions**);
3. the **defaults of their base role** (the tables below).

A permission that belongs to a feature that is switched off is never granted,
whatever the settings say. Some permissions can only be given to editors
(anything that writes pages) or only to administrators; the admin forms only
offer what the role may hold. Granting some permissions also grants the ones
they need (see [implied permissions](#implied-permissions)).

Unlike 1.4, every permission in the catalogue is enforced by the pages and
API calls it describes.

## Category access

Permissions say *what* someone may do; category access says *where*.

* **Read access** is either unrestricted or a list of allowed categories.
  Restricted readers do not see other categories, their pages, or pages
  without a category, anywhere: sidebar, search, links, API, exports.
* **Write access** (editors only) is either unrestricted or a list. Write
  access to a category implies read access to it.
* There is no "private category" flag: a new category is visible to every
  unrestricted reader.
* Where a category is hidden but a subcategory is readable, the readable
  subcategory moves up in the navigation.

Set category access per account on **Admin → Users → (account) → Editor
access** (for editors) and **Permissions**, or for a whole group with a
custom role.

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

**Admin → Custom roles** (`/admin/roles`) defines named roles, for example
"Support editor" or "Read-only contractor". A custom role has

* a base role (`user` or `editor`),
* a set of permissions (limited to what the base role may hold),
* optional read and write category restrictions,

and is assigned to accounts on the role page or from the account page.
Invite codes can assign a base role or a custom role to the people who sign
up with them. When a custom role is deleted, its members move to another
custom role you choose (or the most similar one), or keep their base role with
its default permissions.

Use custom roles when several people need the same access; individual
permissions are for exceptions.

## Other controls

Some features also have their own site settings (see [features](features.md)):

* Kanban and canvases: the weakest role with global view access and with
  write access (`admin`, `editor` or everyone), open access, and anonymous
  access to public boards in public mode. Boards and canvases can be shared
  with a role or a person.
* Page builder: the weakest role that may use it (`page_builder_access`);
  users additionally need the right to edit the page.
* Chat: direct messages and groups can be switched off site-wide, and chat
  can be switched off for one account.
* Page governance: protected pages and check-outs block editing by others.
* Temporary items: a role can be granted until a date and reverts
  automatically.

## Permission catalogue

The catalogue lives in `bananawiki/wiki/permissions.py`. "Feature" means the
permission is only granted while that feature is on.

### Pages

| Key | What it allows | Editor default | User default | Feature | Assignable to |
|---|---|---|---|---|---|
| `page.view_all` | Read every page in readable categories | yes | yes | — | users and editors |
| `page.view_deindexed` | Read pages hidden from navigation and search | yes | no | — | users and editors |
| `page.create` | Create new pages | yes | no | — | editors |
| `page.edit_all` | Edit any page in writable categories | yes | no | — | editors |
| `page.delete` | Delete pages | no | no | — | editors |
| `page.edit_metadata` | Change a page's title, address and category | yes | no | — | editors |
| `page.deindex` | Hide pages from navigation and search | no | no | — | editors |
| `page.export_pdf` | Download pages as PDF files | no | no | — | users and editors |

### Categories

| Key | What it allows | Editor default | User default | Feature | Assignable to |
|---|---|---|---|---|---|
| `category.view_all` | See readable categories in the navigation, lists and search | yes | yes | — | users and editors |
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
| `draft.create` | Save unfinished edits as drafts | yes | no | `drafts` | editors |
| `draft.view_own` | List your own drafts | yes | no | `drafts` | editors |
| `draft.delete_own` | Delete your own drafts | yes | no | `drafts` | editors |
| `draft.transfer` | Hand a draft to another user | no | no | `drafts` | editors |

### Attachments

| Key | What it allows | Editor default | User default | Feature | Assignable to |
|---|---|---|---|---|---|
| `attachment.upload` | Attach files to pages | yes | no | `attachments` | editors |
| `attachment.view` | Download page attachments | yes | yes | `attachments` | users and editors |
| `attachment.delete_own` | Delete attachments you uploaded | yes | no | `attachments` | editors |
| `attachment.delete_any` | Delete anyone's attachments | no | no | `attachments` | editors |

### Page tags

| Key | What it allows | Editor default | User default | Feature | Assignable to |
|---|---|---|---|---|---|
| `tag.edit_difficulty` | Set a page's difficulty tag | yes | no | `difficulty_tags` | editors |
| `tag.edit_custom` | Set a page's custom tag | yes | no | `difficulty_tags` | editors |

### Profiles

| Key | What it allows | Editor default | User default | Feature | Assignable to |
|---|---|---|---|---|---|
| `profile.view` | Open other people's profile pages | yes | yes | `user_profiles` | users and editors |
| `profile.edit_own` | Edit your own profile page | yes | yes | `user_profiles` | users and editors |

### Chat

| Key | What it allows | Editor default | User default | Feature | Assignable to |
|---|---|---|---|---|---|
| `chat.dm` | Send and receive direct messages | yes | yes | `chat` | users and editors |
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

### Kanban boards

| Key | What it allows | Editor default | User default | Feature | Assignable to |
|---|---|---|---|---|---|
| `kanban.view` | Open boards shared with you | yes | yes | `kanban` | users and editors |
| `kanban.create` | Create and manage your own boards | yes | no | `kanban` | users and editors |

### Canvases

| Key | What it allows | Editor default | User default | Feature | Assignable to |
|---|---|---|---|---|---|
| `canvas.view` | Open canvases shared with you | yes | yes | `canvas` | users and editors |
| `canvas.create` | Create and manage your own canvases | yes | no | `canvas` | users and editors |

### Assessments

| Key | What it allows | Editor default | User default | Feature | Assignable to |
|---|---|---|---|---|---|
| `assessment.view` | Answer the quizzes attached to pages | yes | yes | `assessments` | users and editors |
| `assessment.manage` | Create and edit quizzes on pages | yes | no | `assessments` | users and editors |

### Custom pages

| Key | What it allows | Editor default | User default | Feature | Assignable to |
|---|---|---|---|---|---|
| `custom_page.manage` | Create, edit and delete custom pages | no | no | `custom_pages` | administrators only |

### Contributions

| Key | What it allows | Editor default | User default | Feature | Assignable to |
|---|---|---|---|---|---|
| `contribution.propose` | Suggest edits for review | no | yes | — | users and editors |
| `contribution.review` | Approve or deny suggested edits | no | no | — | editors |

### Implied permissions

| Granting | also grants |
|---|---|
| `page.view_deindexed` | `page.view_all` |
| `page.create` | `page.view_all` |
| `page.edit_all` | `page.view_all` |
| `page.delete` | `page.view_all` |
| `page.edit_metadata` | `page.view_all` |
| `page.deindex` | `page.view_all` |
| `category.create` | `category.view_all` |
| `category.edit` | `category.view_all` |
| `category.delete` | `category.view_all` |
| `category.reorder` | `category.view_all` |
| `category.manage_sequential` | `category.view_all` |
| `contribution.propose` | `page.view_all` |
| `contribution.review` | `page.view_all` |
| `history.revert` | `history.view` |
| `history.delete` | `history.view` |
