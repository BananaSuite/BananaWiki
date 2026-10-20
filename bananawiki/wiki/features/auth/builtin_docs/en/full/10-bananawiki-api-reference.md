# REST API

The **REST API** plugin adds JSON endpoints under `/api/v1/` for scripts and integrations. Requests carry a Bearer token. The full list of endpoints is at `/api-docs` on your wiki. The endpoints under `/api/` that the web interface uses are internal and can change between releases.

## Switching it on

An administrator enables the plugin in **Administration → Plugins**, then ticks *Enable the REST API* on its settings page, where request limits and the number of tokens per account are set too.

## Tokens

Each account creates tokens in **Settings → API tokens**. A token is shown once; revoke tokens you no longer use.

## Scopes

A token only reaches the endpoints of its scopes: `pages`, `categories`, `kanban`, `canvas`, `tokens`, and for administrators `users`, `settings` and `admin`. `userbot` is for the automation key.

## What a token can do

A token acts as the person who owns it and can never do more than that person could in the editor. Category read and write restrictions apply, so a page outside the owner's readable categories is reported as not found and a write outside their writable categories is refused with `403`. A page that is protected, or checked out by another editor, is refused with `409`. The list of pages only contains what the owner can read. Creating, renaming and moving categories needs the same category permissions as in the wiki.

Deleting a page through the API follows the same rules as the Delete button. When **Deletion slowdown** is on, the page enters its grace period and the request returns `202` with `"pending_deletion": true` instead of removing it.
