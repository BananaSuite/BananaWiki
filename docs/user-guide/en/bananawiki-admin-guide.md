# Admin Guide

Admins manage site settings, users, the built-in documentation, content operations, and plugins.

## Site basics

In **Admin -> Site Settings** you can change the site name, theme colors, default theme, favicon, timezone, interface language, signup behavior, maintenance mode, and many feature defaults. Most changes apply immediately.

## Users and access

Use **Admin -> Users** to create accounts, reset passwords, change roles, suspend users, assign custom roles, and tune per-user permissions. Invite codes can limit who signs up. Session limits can prevent one account from being used in multiple places at the same time.

## Documentation lifecycle

The built-in documentation can be spawned as a BananaWiki category. You can choose full or simplified docs, and English or Italian. Downloading the docs as a ZIP gives you Markdown files that can be edited locally and re-imported with Bulk Markdown Import.

## Content operations

Admins can manage categories, restore or permanently remove slowed deletions, run bulk Markdown imports, export pages, generate PDFs, and use migration tools for full-site backup and restore.

A full-site export contains every account's password hash, and a full-site import replaces every account. Both ask for your password again and are recorded in the log.

## Owners and trust

Owner status stops other admins from demoting or deleting an account through the normal admin pages. It does not stop an admin who imports a full-site backup or installs a plugin: either one gives complete control of the wiki, including every account. Give the admin role only to people you trust with everything.

## Plugins

Plugins control optional features. Enable only what your wiki will use, then revisit the list as the team grows. Disabling a plugin hides or stops its feature without necessarily deleting stored data.

## Recommended routine

- review new users and admin accounts
- check recent audit entries for unusual actions
- keep backups current and occasionally test restore
- prune old pages or mark outdated content clearly
- update this documentation when your local workflow differs from the default
