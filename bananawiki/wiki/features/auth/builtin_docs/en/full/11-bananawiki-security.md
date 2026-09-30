# Security & Backups

BananaWiki is built for private knowledge, so it protects accounts and content, logs admin and security events, and supports full-site backup and restore.

## Account protection

Passwords are stored as hashes, login attempts are rate-limited, CSRF tokens protect forms and AJAX mutations, and admins can enforce session limits. Suspensions, forced password changes, invite codes, and maintenance mode provide additional controls.

## Content protection

Markdown is sanitized before display. Uploads are validated and dangerous file types are blocked where appropriate. Attachments are served through authenticated routes rather than as public files. Category restrictions and permissions keep sensitive pages away from users who should not see them.

## Operational safety

Audit logs record important administrative and security events. Deletion slowdown can add a grace period before destructive page removal. Page history and backups give admins a path back when content changes unexpectedly.

## Backups

Use full-site exports or deployment backups. A backup is only useful if restore works, so test restoration before an emergency. Keep copies outside the server that hosts the live wiki.

A full-site export holds every password hash, so store it like a password. Export and import both ask for the admin's password again and are logged. Importing a backup gives complete control of the wiki, owner accounts included, because the backup replaces every account. Installing a plugin does the same, since its code runs with the wiki's own rights.

## Production recommendations

Run behind HTTPS, keep the application updated, limit admin accounts, review plugins before enabling them, monitor disk space, and document your local recovery procedure in the wiki itself.
