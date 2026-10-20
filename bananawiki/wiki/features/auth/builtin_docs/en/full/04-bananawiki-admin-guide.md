# Administration

Administrators manage site settings, accounts, the built-in documentation, content and features.

## Site basics

In **Administration → Site settings** you set the site name, time zone, sign-up and approval, maintenance mode, single session per account, uploads and other defaults. Most changes apply immediately.

## Users and access

Use **Administration → Users** to create accounts, reset passwords, change roles, suspend users, assign custom roles and set per-user permissions. Invite codes limit who can sign up. Owners and superusers can require two-step verification for administrators and reset it for an account that lost its authenticator app and recovery codes.

## Documentation

**Administration → Documentation** adds this guide to the wiki as a category, full or simplified, in English, Italian or German. You can also download it as a ZIP of Markdown files, edit them and bring them back with the bulk Markdown import.

## Content operations

Administrators can manage categories, restore or remove pages waiting in deletion slowdown, run bulk Markdown imports, and export pages and PDFs.

Only owners and superusers can export or import the whole site; bulk deletions ask you to type DELETE.

## Owners and trust

Owner status stops other administrators from demoting or deleting an account through the normal admin pages. Importing a full-site backup or installing a third-party plugin gives complete control of the wiki, including every account, so only owners and superusers can do either. Other administrators still manage every other account and all content: give the administrator role only to people you trust with that, and make someone an owner or a superuser only if you trust them with everything.

## Plugins

**Administration → Plugins** switches features on and off. Switching one off hides it; its data and settings stay and come back when you switch it on again.

## A good routine

- review new accounts and the list of administrators
- look at the audit log for unusual actions
- check that backups run, and try a restore now and then
- switch off features your wiki does not use
