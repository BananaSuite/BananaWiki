# For administrators

Most controls are in **Administration → Site settings**, **Administration → Users** and **Administration → Plugins**.

## First checks

- set the site name, time zone and language
- decide whether sign-up needs invite codes
- create the first accounts
- switch on the plugins you need now
- make a backup before large imports

## Users and permissions

Roles set what people can do; category restrictions set where. Use a custom role when several people need the same special access. Keep the number of owners and superusers small.

## Built-in documentation

**Administration → Documentation** adds this guide to the wiki, full or simplified, in English, Italian or German. You can also download it as a Markdown ZIP, edit it and import the pages again.

## Backups

Use the full-site export or your server's backups. Servers can also keep encrypted backups in a private GitHub or Forgejo repository; see `docs/operations.md` in the source. Try a restore before you rely on a backup.
