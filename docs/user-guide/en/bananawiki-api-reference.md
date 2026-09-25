# Developer API Reference

BananaWiki exposes developer surfaces for automation, plugins, and integrations. Some endpoints use the web session and CSRF token. The API Service plugin adds Bearer-token endpoints for external clients.

## Session-based endpoints

Common web endpoints include search, Markdown preview, draft management, sidebar filtering, page reservations, Kanban reordering, Canvas data, accessibility preferences, and uploads. Session endpoints require a logged-in user and mutation requests require CSRF protection.

## API Service plugin

When enabled, API Service provides `/api/v1/` endpoints authenticated with Bearer tokens. Users can create personal tokens where allowed. Admins can configure scopes, token limits, expiry behavior, audit visibility, and Banana Mode automation.

## Scopes

Token scopes limit what an integration can do. Prefer the smallest useful scope set: pages for content automation, categories for navigation automation, users for administration, settings for site configuration, and userbot for account automation workflows.

## What a token can do

A token acts as the person who owns it and can never do more than that person could in the editor. Category read and write restrictions apply, so a page outside the owner's readable categories is reported as not found and a write outside their writable categories is refused with `403`. A page that is protected, or checked out by another editor, is refused with `409`. The list of pages only contains what the owner can read. Creating, renaming and moving categories needs the same category permissions as in the wiki.

Deleting a page through the API follows the same rules as the Delete button. When Deletion Slowdown is on, the page enters its grace period and the request returns `202` with `"pending_deletion": true` instead of removing it.

## Plugin SDK

Plugin authors can use decorators for authentication, register new permissions, add template slots, call safe database helpers, and react to hooks. Treat the SDK as the stable integration layer instead of importing private internals.

## Safety checklist

- keep tokens secret and rotate them when people leave
- set expiry dates for temporary integrations
- log and review automation that changes content
- validate incoming data even when a route is admin-only
