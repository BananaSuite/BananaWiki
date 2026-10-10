# For Admins

Admins shape how the wiki behaves. Most controls live in **Admin -> Site Settings**, **Admin -> Users**, and **Admin -> Plugins**.

## First checks

- confirm the site name, theme, timezone, and language
- decide whether signup needs invite codes
- create or invite the first users
- choose which plugins are useful now
- make a backup before major imports or migrations

## Users and permissions

Use roles for broad access and category restrictions for where people can work. Use custom roles when several people need the same special access. Keep protected admin accounts limited.

## Built-in documentation

The **Wiki Documentation** section can spawn this guide into the wiki. Choose full or simplified docs, and English, Italian or German. You can also download the Markdown ZIP, edit it, and import the customized pages.

## Backups

Use the migration export or deployment backups. Managed installations can also use encrypted backups in a private GitHub or Forgejo repository; see `docs/backups.md` in the application source. Telegram backup delivery is retired. Test restore before you rely on a backup strategy.
