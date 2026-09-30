# Writing pages

You need the **editor** role (or a custom role that allows it) and write
access to the category. Users who may only read can often **propose an
edit** instead (see [organising](organising.md#proposing-an-edit)).

## Creating a page

Click **New page** in the sidebar (or open `/create`). Enter a title, choose a
category (or none), write the text and save. The address of the page
(`/page/<address>`) is made from the title; you can change it later.

## Editing

Open the page and click **Edit**. The editor has two tabs, **Write** and
**Preview**, a formatting toolbar (headings, bold, italic, lists, quotes,
code, links, tables, horizontal lines, images and videos) and a full-screen mode. **Ctrl+B** and **Ctrl+I** work as
usual. Click **Save changes** when done and add a short summary of what you
changed; it appears in the history.

If other people are editing the same page, the editor says so. If someone
saves the page while you are editing, saving tells you and lets you compare
instead of silently overwriting their work.

## Markdown in a nutshell

| Write | Result |
|---|---|
| `## Section` / `### Subsection` | headings (they appear in the contents list) |
| `**bold**`, `*italic*` | emphasis |
| `- item` or `1. item` | lists (indent by two spaces to nest) |
| `[text](https://example.org)` | link |
| `[text](/page/other-page)` | link to another page |
| `` `code` `` and blocks between ```` ``` ```` lines | code (with highlighting when you name the language) |
| `> quote` | quotation |
| `| a | b |` rows | table |
| `@name` | a link to that person's profile |

On a line of their own:

* a YouTube or Vimeo address becomes a video player; `[[video url="…"]]`
  lets you set width, alignment and ratio (the toolbar's video button writes it
  for you);
* `[[kanban board="12"]]` shows a kanban board, `[[canvas slug="plan"]]` a
  canvas.

HTML that could be dangerous is removed, so pages always look like the rest
of the wiki.

## Images and files

Drop or paste images into the editor to upload them; they are stored in the
wiki and inserted where the cursor is. Images from other websites work too,
but uploading is kinder to readers' privacy.

If attachments are enabled, the editor and the page have an **Attachments**
section for other files (PDF, documents, archives, audio, video). The wiki may
limit file types, sizes and how much you can upload per day.

## Drafts

While you type, the editor saves a private **draft**. If you close the tab by
mistake, reopening the editor offers to restore it. Drafts of other editors
on the same page are listed, so you know someone has unsaved work. Saving the
page clears the drafts and credits their authors. **My drafts** in the
account menu lists yours; you can discard a draft or hand it to someone else.
Old drafts may expire.

## History

**More → History** lists every saved version: who, when, the summary and the
size of the change. Open a version to see it as it was, its Markdown, or
what changed compared with the version before. With the right permission you
can **Restore this version** (the current text stays in the history), delete
entries, or credit an entry to someone else.

## The page builder

Some wikis let you build a page from visual blocks (headings, text, images,
buttons, columns, videos) instead of writing Markdown. If your wiki has it,
the page offers the builder next to **Edit**. The page keeps a Markdown copy,
so search and history still work.
