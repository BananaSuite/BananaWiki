# Obsidian Vault Sync (Experimental)

> **Experimental**, CLI-only, no web routes.  This feature may change or be
> removed in future releases.

BananaWiki can export wiki pages into a local Obsidian vault and import
edited pages back.  The sync is a one-shot CLI operation.  There is no
real-time or background synchronisation.

---

## Enabling the Feature

Set the feature flag in `config.py`:

```python
EXPERIMENTAL_OBSIDIAN_SYNC = True
```

If the flag is `False` (the default), the sync script exits immediately.

---

## Authentication

The CLI authenticates directly against the BananaWiki database.  Only
users with the `editor`, `admin`, or `owner` role are allowed;
regular `user` accounts are rejected.

Supply credentials via environment variables or CLI arguments:

| Environment Variable | CLI Argument | Description |
|---|---|---|
| `BANANAWIKI_OBSIDIAN_USERNAME` | `--username` | BananaWiki username |
| `BANANAWIKI_OBSIDIAN_PASSWORD` | `--password` | BananaWiki password |
| `BANANAWIKI_OBSIDIAN_VAULT` | `--vault` | Absolute path to the Obsidian vault directory |

CLI arguments take precedence over environment variables.

---

## Pull: Export Pages to the Vault

```bash
python scripts/obsidian_sync.py \
    --vault /path/to/vault \
    --username admin \
    --password secret \
    pull
```

This exports every accessible wiki page into the vault directory.

### Filters

| Flag | Effect |
|---|---|
| `--page <slug>` | Export only the page with this slug (repeatable). |
| `--directory <category-path>` | Export only pages in this category folder (repeatable). |
| `--skip-home` | Do not export the BananaWiki home page. |

Examples:

```bash
# Single page
python scripts/obsidian_sync.py --vault /path/to/vault \
    --username admin --password secret \
    pull --page sync-guide

# Single category
python scripts/obsidian_sync.py --vault /path/to/vault \
    --username admin --password secret \
    pull --directory Guides

# Everything except the home page
python scripts/obsidian_sync.py --vault /path/to/vault \
    --username admin --password secret \
    pull --skip-home
```

### Vault Structure

After a pull the vault looks like this:

```
vault/
├── Category-Name/
│   ├── page-title.md
│   └── another-page.md
├── Uncategorized/
│   └── standalone-page.md
├── assets/
│   ├── images/
│   │   └── uploaded-photo.png
│   └── attachments/
│       └── page-slug/
│           └── report.pdf
└── .bananawiki-obsidian.json
```

- **Category folders** mirror BananaWiki categories.
- **`assets/images/`** contains image uploads referenced by page content.
- **`assets/attachments/<page-slug>/`** contains page file attachments.
- **`.bananawiki-obsidian.json`** stores sync metadata (manifest version,
  page mappings, asset hashes).

### Frontmatter

Each exported `.md` file starts with YAML frontmatter:

```yaml
---
title: Page Title
slug: page-title
category: Category-Name
bananawiki_page_id: 42
bananawiki_category_id: 7
is_home: false
---
```

Push uses the `bananawiki_page_id` field to match local files back to
their server-side pages.

---

## Editing in Obsidian

After pulling:

- Edit the Markdown body directly in Obsidian.
- Keep files inside their category folders to preserve category placement.
- Reference local assets with relative paths (e.g.
  `../assets/images/photo.png`).

### Creating New Pages

Add a new `.md` file anywhere in the vault (outside `assets/`):

- The filename becomes the default slug.
- The first `# Heading` becomes the title if no `title` frontmatter is set.
- The parent folder determines the BananaWiki category.

Admins can create missing categories during push.  Editors can only push
into categories that already exist and that they have write access to.

---

## Push: Import Pages Back to BananaWiki

```bash
python scripts/obsidian_sync.py \
    --vault /path/to/vault \
    --username admin \
    --password secret \
    push
```

### How Matching Works

1. Files with a `bananawiki_page_id` in their frontmatter (or a matching
   entry in the manifest) are matched to existing pages and **updated**.
2. Files without a page ID are treated as **new pages** and created on the
   server.
3. A SHA-256 hash comparison skips files whose content has not changed since
   the last pull.

### Filters

The same `--page` and `--directory` flags are available on push:

```bash
# Push a single page
python scripts/obsidian_sync.py --vault /path/to/vault \
    --username admin --password secret \
    push --page sync-guide

# Push one category
python scripts/obsidian_sync.py --vault /path/to/vault \
    --username admin --password secret \
    push --directory Guides
```

### Asset Handling on Push

- Local image references are rewritten back to BananaWiki upload URLs.
- New or changed local images are uploaded to the server automatically.
- Existing server-side image references are preserved when unchanged.

---

## Page History Integration

When a push updates an existing page, the change is recorded as a new
entry in BananaWiki's page history with the edit message:

```
Obsidian sync push
```

The database-backed page history is the authoritative revision trail for
synced content.

---

## Limitations

| Limitation | Detail |
|---|---|
| No real-time sync | Pull and push are manual, one-shot CLI operations. |
| No conflict resolution | Last write wins: if a page is edited on both sides, the push overwrites the server copy. |
| No attachment push | New file attachments created locally are not uploaded during push; only images are. |
| Admin/editor only | Regular `user` accounts cannot use the sync. |
| Local to the server | The script accesses the BananaWiki database directly; it is not a network API client. |
| No page deletion | Deleting a local file does not delete the corresponding BananaWiki page. |
| Experimental | The feature flag, CLI interface, and vault format may change without notice. |
