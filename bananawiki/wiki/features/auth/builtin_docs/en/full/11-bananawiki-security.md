# Security and backups

How the wiki protects accounts and content, and how to back it up.

## Account protection

Passwords are stored as hashes, sign-in attempts are rate-limited, and forms are protected against cross-site requests. Administrators can suspend accounts, force a password change, require invite codes, allow one session per account and put the wiki in maintenance mode.

Anyone can turn on **two-step verification** in **Settings → Two-step verification**: after the password, signing in also asks for a code from an authenticator app on their phone, or one of ten single-use recovery codes shown when it is turned on. Owners and superusers can require it for every administrator in **Administration → Site settings → Security**, and reset it in **Administration → Users** for someone who lost both their phone and their recovery codes.

## Content protection

Markdown is cleaned of unsafe HTML before it is shown. Uploads are checked against the size limits and the allowed and blocked file types, and still images are re-encoded, which removes location data. Attachments are only served to people who can read the page.

## Undoing changes

The audit log records sign-ins, account and role changes, deletions and settings changes. Page history restores earlier versions, and deletion slowdown keeps deleted pages for 48 hours.

## Backups

Back up with the full-site export or the server's own backups, keep copies on another machine, and try a restore before you need one.

A full-site export holds every password hash, so store it like a password. Only owners and superusers can export or import the whole site; both ask for the password again and are logged. Importing a backup gives complete control of the wiki, owner accounts included, because the backup replaces every account. Installing a plugin does the same, since its code runs with the wiki's own rights.
