# Working together

These tools appear under **Apps** when your wiki has them switched on.

## Messages and groups

* **Messages** (`/chats`): start a conversation with one person by entering
  their user name.
* **Groups** (`/groups`): create a group, or join one with its invite code.
  Most wikis also have a global room for everyone. Owners and moderators can
  add and remove members, time out or ban someone, and change the invite code;
  the owner can hand the group over.
* You can attach files (within the size and daily limits), export a
  conversation, and delete your messages. **Deleting a message erases it for
  everyone**; a small "deleted" marker remains.
* Administrators can read conversations for moderation, and the wiki may
  delete old messages and files automatically after a set time.

Use chat for conversation and pages for knowledge: when a thread settles
something, write it down on a page.

## Kanban boards

**Kanban** (`/kanban`) holds boards made of columns and tickets.

* Add columns (for example *To do*, *Doing*, *Done*) and tickets; drag them
  to reorder or move. Everyone looking at the board sees changes within
  seconds.
* A ticket has a description (Markdown), labels, a priority, a due date, a
  colour, assignees, comments, attachments and its own history.
* Type shortcuts in a ticket title: `Fix login @alice +backend !high
  color:red due:tomorrow` assigns Alice, adds the label, sets the priority,
  colour and due date (`today`, `tomorrow`, `nextweek` or `YYYY-MM-DD`).
* A ticket can hold a **checklist** of smaller steps; the card shows how many
  are done (☑ 2/5).
* The **filter bar** above the board narrows it by text, person (or *Me*),
  label, priority and due date (overdue, due within 2 days, next 7 days, no
  date). Press `/` to jump to the search field; the filter is kept in the
  address, so you can bookmark or share it. Cards due soon are marked in
  amber, overdue ones in red.
* **Limit** in a column header sets a work-in-progress limit: the column is
  highlighted when it holds more tickets than that.
* **Rows** above the board groups it into swimlanes by assignee, priority or
  label. Drag a ticket to another row to reassign it or change its priority
  (with the keyboard: move it past the first or last ticket of its row).
* **Archive** a ticket from its window, several at once with **Select**, or
  every ticket of a column with **Archive** in the column header (handy for
  *Done*). Archived tickets leave the board but keep everything; the
  **Archived** button lists them to open, restore or delete for good.
* The board's creator can **archive the whole board**: it disappears from the
  board list (use *Show archived boards* to find it) and is read-only until
  it is restored.
* **My tickets** (`/kanban/mine`) lists what is assigned to you on every board
  you can open, grouped by due date; the form at the top filters it.
* Checklist items can be dragged by their handle (⠿) or moved with the arrow
  keys on it.
* Keyboard: focus a ticket's handle (⠿) and use the arrow keys to move it
  between positions and columns; each move is announced to screen readers.
* **History** shows earlier states of the board, with their column limits,
  checklists and archived tickets, and what changed in each; restoring one
  never deletes tickets, comments or files.
* The board's creator can share it with people or roles (view or write),
  make it public, export or delete it. **More → Export without attachments**
  exports just the board, for example when its files are too many or too
  large for one export.
* Put a board into a page with `[[kanban board="<number>"]]`.

## Canvases

**Canvases** (`/canvas`) are free-form boards: notes, shapes, images, videos,
code and links to wiki pages, connected by arrows. Several people can edit at
the same time. Canvases have a history with restore, can be shared with people
or roles (view or edit), exported and imported, and shown in a page with
`[[canvas slug="<name>"]]`.

* **Start from a template**: when creating a canvas, choose a flowchart, a
  mind map, a retrospective board or a SWOT analysis instead of an empty one.
* **Arrange**: select several elements (Shift-click or Shift-drag) and use
  **Arrange** to align or distribute them, group them (Ctrl+G) so they move
  together, or lock them (L) so they stay put. A locked element cannot be
  moved, resized, edited or deleted by anyone (the wiki refuses it, also
  through the API) until someone unlocks it. While you drag, **guide lines**
  appear when edges or centres line up with nearby elements and the element
  snaps to them; otherwise **Snap to grid** (G) lines elements up. Hold Alt
  to place them freely.
* **Connections** follow the elements when you move them; double-click one
  to choose a curved, straight or right-angled path, arrowheads, a label and
  the side of each element it attaches to (top, right, bottom, left or
  automatic).
* **Overview map** in the corner shows the whole canvas; click it to jump.
* **Image** downloads the canvas as PNG or SVG. Images from other sites are
  included when their site allows it; otherwise the PNG shows a labelled
  placeholder box (the SVG keeps a link to the image). **Text outline** lists every
  element with its connections, readable with a screen reader and
  downloadable as Markdown.

## Quizzes

A page can have a quiz (single choice, multiple choice or free-text
questions). Open it from the page, answer and submit; the score is shown at
once. Depending on the quiz you may try again. **My quiz points** in the
account menu lists your results. Editors with the right permission create and
manage quizzes and see everyone's results.

## Announcements

Banners at the top of the wiki with news from the administrators. You can
close a banner; it comes back if it is changed.

## Badges and the leaderboard

Badges on your profile recognise contributions and milestones; some are
earned automatically (first edit, number of edits, time spent reading, days
of membership), others are awarded by administrators. You are notified when
you get one. The **Leaderboard** ranks contributors over a period.

## Federated pages

If your wiki is paired with other BananaWiki installations, **Federated
pages** shows pages they shared with you (read only), and editors can share
pages with them.
