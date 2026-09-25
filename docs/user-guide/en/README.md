# BananaWiki user guide

This folder is the on-disk mirror of the BananaWiki user guide.  It is the exact text that **Spawn documentation** (Admin → Site Settings → Wiki Documentation) writes into the wiki, and it is generated automatically from the Python module `db/_wiki_docs.py` via `scripts/sync_user_guide_docs.py`.

To edit the guide:

1. Edit the Markdown files in this folder directly, or edit the strings in `db/_wiki_docs.py`.
2. Run `python scripts/sync_user_guide_docs.py` to keep both sides in sync (the Python source is the canonical reference).
3. To publish your edits inside a real wiki, use **Admin → Site Settings → Wiki Documentation → Download ZIP**, edit the Markdown files locally, then re-upload with **Bulk Markdown Import**, or re-spawn the stock documentation.

## Pages

- [Welcome to BananaWiki](bananawiki-welcome.md)
- [Pages & Editing](bananawiki-pages-editing.md)
- [Categories & Navigation](bananawiki-categories-navigation.md)
- [Roles & Permissions](bananawiki-roles-permissions.md)
- [Admin Guide](bananawiki-admin-guide.md)
- [Chat & Messaging](bananawiki-chat-messaging.md)
- [Kanban Boards](bananawiki-kanban-boards.md)
- [Canvas Layouts](bananawiki-canvas.md)
- [Badges & Achievements](bananawiki-badges.md)
- [Plugins & Extensions](bananawiki-plugins.md)
- [Developer API Reference](bananawiki-api-reference.md)
- [Security & Backups](bananawiki-security.md)
