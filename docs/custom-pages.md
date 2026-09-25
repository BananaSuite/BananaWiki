# Custom Pages

> **Plugin:** `custom_pages`, must be enabled in **Admin → Plugins**.

## Overview

Custom pages let administrators publish content at arbitrary URL paths on the
wiki.  When a visitor requests a path that does not match any built-in route,
BananaWiki's 404 handler calls `try_serve_custom_page()`, which looks up the
path in the `custom_pages` table and serves the matching page if one exists and
is published.

This mechanism supports 19 content types, from simple Markdown pages
and redirects to hosted videos, file downloads, embedded iframes, and raw HTML.

---

## Who Can Use Them

- **Managing** custom pages (create, edit, delete, upload and delete files)
  is limited to the admin and owner roles.  The `custom_page.manage`
  permission is admin-only: it cannot be given to editors or users through a
  custom role, and a role row that still lists it from an older version has
  no effect.  A custom page can publish anything at a path of the wiki's own
  domain and send visitors to another site, which is why this stays with the
  people who already administer the wiki.
- **Published pages are public.**  A published custom page and its files are
  served to anyone who opens the path, signed in or not, even when public
  mode is off and the rest of the wiki needs a login.  This is what makes
  custom pages useful for a landing page, an imprint or a download link on a
  private wiki.  Do not publish anything there that only members should see.
  There is no separate "view custom pages" permission.
- **Unpublished pages** and their files return 404 to everyone except admins
  and owners, who can preview them.

---

## Content Types

| Content type | Description |
|---|---|
| `redirect` | HTTP redirect (301 permanent or 302 temporary) to a path on the wiki or an `http`/`https` URL. |
| `html` | Raw HTML served as a standalone document (no wiki layout, not sanitised), inside the sandbox described under [Security](#security). |
| `html_styled` | Raw HTML plus the CSS field, served as a standalone document in the same sandbox. |
| `html_full` | Raw HTML plus the CSS and JavaScript fields, served as a standalone document in the same sandbox.  The script runs, but with a unique origin. |
| `markdown` | Markdown rendered through the standard `render_markdown()` pipeline (sanitised) plus the CSS field, served as a standalone document in the same sandbox. |
| `wiki_page` | The page's own Markdown content rendered (sanitised) inside the wiki layout.  It does not mirror an existing wiki page. |
| `plain_text` | Plain text served with `text/plain` content type. |
| `json_content` | JSON served with `application/json` content type. |
| `xml_content` | XML served with `application/xml` content type, in the sandbox (browsers render XML as a document). |
| `image` | Serves the page's first uploaded file directly.  A file whose type is not on the inline list under [Serving policy](#serving-policy) is downloaded instead of shown. |
| `image_page` | Displays the uploaded images inside the wiki layout. |
| `youtube_video` | Embeds a YouTube video player.  Supports youtube.com, youtu.be, m.youtube.com, and music.youtube.com URLs. |
| `video_hosted` | Serves a self-hosted video with an HTML5 `<video>` player.  Supports autoplay, controls, loop, and muted options. |
| `file_download` | Serves the page's first uploaded file as a download (`Content-Disposition: attachment`). |
| `file_listing` | Lists all files attached to the custom page with download links. |
| `iframe_embed` | Embeds an external URL in an `<iframe>` with configurable height, sandbox policy (default `allow-scripts allow-same-origin allow-popups`), and allow attributes. |
| `page_embed` | Embeds an external URL in an `<iframe>` like `iframe_embed`, but with no `sandbox` attribute unless one is set. |
| `link_list` | Displays a styled list of links (stored as JSON).  Links whose URL uses a scheme other than `http`, `https` or `mailto` are left out. |
| `code_snippet` | Shows the content as a plain code block with a language label. |

For `iframe_embed` and `page_embed`, the response's CSP `frame-src` is widened
to the embedded URL's origin, and only for an `https` URL.

---

## URL Path Rules

Custom page paths must begin with `/` and must **not** collide with any
built-in route prefix.  The following prefixes are reserved:

```
/admin       /login      /logout       /signup      /setup
/maintenance /lockdown   /session-conflict          /static      /api
/wiki        /chats      /groups       /kanban      /canvas
/badges      /users      /page         /settings    /global-settings
/reservations            /announcements             /_cpf
```

Paths are validated when a page is created or edited.  They are stored
without a trailing slash and matched case-sensitively.

---

## Creation Workflow

1. Navigate to **Admin → Custom Pages**.
2. Click **Create Custom Page**.
3. Enter a **path** (e.g. `/about`) and **title**.
4. Select a **content type** from the dropdown. The form then shows the
   fields for that type.
5. Fill in the content (text, URL, uploaded file, etc.).
6. Optionally add a **meta description** for search engines.
7. Toggle **Published** to control visibility.
8. Click **Save**.

Editing and deleting pages works from the same admin list view.

---

## File Storage

Content types that involve uploaded files (`image`, `image_page`,
`video_hosted`, `file_download`, `file_listing`) store their files in:

```
instance/custom_page_files/
```

Each file is saved with a UUID-based filename (preserving the original
extension) and tracked in the `custom_page_files` database table, with a copy
in the database blob store.  Files are written in streaming 8 KB chunks to
limit memory usage.

### Upload rules

Uploads follow the same extension rules as page attachments: the extensions
that are always blocked (HTML, SVG, executables and similar), the platform
blacklist on hosted wikis, and the upload mode chosen under
**Admin → Settings** (allow all, whitelist or blacklist).  A file without an
extension is refused.  When an upload is refused the page is still saved and
the error is shown.

### Serving policy

Files are served through the `/_cpf/` route:

```
GET /_cpf/<file_id>/<filename>
```

Files of a published page are public, like the page itself.

A file is shown inline only when its type is on this list:

- images: PNG, JPEG, GIF, WebP, AVIF, BMP
- video: MP4, WebM, Ogg, QuickTime, AVI, Matroska
- audio: MP3, Ogg, WAV, WebM, FLAC, AAC, M4A
- plain text

Every other file, including JavaScript, CSS, HTML, SVG, XML and PDF, is sent
as `application/octet-stream` with `Content-Disposition: attachment`, so the
browser downloads it.  Together with the global `X-Content-Type-Options:
nosniff` header this means an uploaded file can never be loaded as a script
or stylesheet by a page on the wiki's domain.  File responses also carry the
custom page sandbox, so a file opened directly in a tab does not get the
wiki's origin.  The `image` content type uses the same policy.

---

## Size Limits

| Limit | Default | Override |
|---|---|---|
| General file upload | 16 MB | `config.CUSTOM_PAGE_MAX_FILE_SIZE` |
| Video file upload | 100 MB | Site settings: `custom_pages_max_video_size_mb` (range 1–2048 MB) |

The video size limit can be changed at runtime from **Admin → Site Settings**
without a restart.

---

## Security

- **Admins only:** only admins and owners can create, edit or delete custom
  pages and their files (see [Who Can Use Them](#who-can-use-them)).  Every
  create, update, delete and file delete is recorded in the action log.
- **Sandbox, not sanitisation:** `html`, `html_styled` and `html_full` are
  served exactly as written.  Instead of being sanitised they are served with
  a `Content-Security-Policy: sandbox allow-scripts` header, so the browser
  gives the page a unique origin: its script cannot read the wiki's cookies,
  call the wiki as the signed-in viewer or reach other wiki pages.  The same
  sandbox applies to `markdown` and `xml_content` pages and to uploaded
  files.  Because the sandbox does not allow popups, a link with
  `target="_blank"` on one of these pages does not open.
- **CSS field:** `<` in the CSS field is written as the CSS escape `\3c `,
  so the field cannot close its `<style>` element and add markup to the
  page.  The CSS and JavaScript fields receive the per-request CSP nonce.
- **Redirects:** a redirect target must be a path on the wiki or an `http`
  or `https` URL.  Other schemes (`javascript:`, `data:`...) are refused
  when the page is saved, and an older row holding one is not served.
- **Path validation:** the reserved prefix check prevents shadowing built-in
  routes.
- **Uploaded files:** extension rules on upload and an inline allowlist on
  serving, as described under [File Storage](#file-storage).
- **Rate limiting:** the create, edit and delete routes are rate-limited
  (10 requests per minute each).
- **CSRF protection:** all admin POST routes require a valid CSRF token.

---

## Routes

### Admin Routes (admin and owner only, `custom_page.manage`)

| Method | Path | Description |
|---|---|---|
| GET | `/admin/custom-pages` | List all custom pages |
| GET / POST | `/admin/custom-pages/create` | Create a new custom page |
| GET / POST | `/admin/custom-pages/<page_id>/edit` | Edit an existing custom page |
| POST | `/admin/custom-pages/<page_id>/delete` | Delete a custom page and its files |
| POST | `/admin/custom-pages/files/<file_id>/delete` | Delete a single attached file |

Other signed-in users get 403.  When the plugin is disabled every route,
including the public ones below, returns 404.

### Public Routes

| Method | Path | Description |
|---|---|---|
| GET | `/_cpf/<file_id>/<filename>` | Serve a custom page file (published pages only, unless the viewer is an admin) |
| GET | `/<any_path>` | 404 fallback: serves a published custom page if the path matches |

---

## Database Tables

### `custom_pages`

| Column | Type | Description |
|---|---|---|
| `id` | INTEGER PK | Auto-increment ID |
| `path` | TEXT UNIQUE | URL path (must start with `/`) |
| `title` | TEXT | Display title |
| `content_type` | TEXT CHECK | One of the 19 content types |
| `content` | TEXT | Text content (Markdown, HTML, JSON, etc.) |
| `css` | TEXT | Optional custom CSS (`html_styled`, `html_full`, `markdown`) |
| `js` | TEXT | Optional custom JS (`html_full`) |
| `redirect_url` | TEXT | Target URL (`redirect` type) |
| `redirect_code` | INTEGER | 301 or 302 (`redirect` type) |
| `links_json` | TEXT | JSON array of links (`link_list` type) |
| `code_language` | TEXT | Language tag (`code_snippet` type) |
| `iframe_url` | TEXT | Source URL (`iframe_embed` and `page_embed` types) |
| `iframe_height` | TEXT | Height in px (`iframe_embed` type) |
| `iframe_sandbox` | TEXT | Sandbox attribute value |
| `iframe_allow` | TEXT | Allow attribute value |
| `video_url` | TEXT | YouTube URL (`youtube_video` type) |
| `video_autoplay` | INTEGER | 0 or 1 (`video_hosted` / `youtube_video`) |
| `video_controls` | INTEGER | 0 or 1 |
| `video_loop` | INTEGER | 0 or 1 |
| `video_muted` | INTEGER | 0 or 1 |
| `meta_description` | TEXT | SEO meta description |
| `is_published` | INTEGER | 1 = visible to visitors, 0 = draft |
| `created_at` | TEXT | UTC ISO timestamp |
| `updated_at` | TEXT | UTC ISO timestamp |

### `custom_page_files`

| Column | Type | Description |
|---|---|---|
| `id` | INTEGER PK | Auto-increment ID |
| `custom_page_id` | INTEGER FK | References `custom_pages.id` |
| `filename` | TEXT | UUID-based filename on disk |
| `original_name` | TEXT | User-facing original filename |
| `mime_type` | TEXT | Detected MIME type |
| `file_size` | INTEGER | Size in bytes |
| `uploaded_at` | TEXT | UTC ISO timestamp |
| `blob_id` | INTEGER | Copy of the file in the database blob store (optional) |
