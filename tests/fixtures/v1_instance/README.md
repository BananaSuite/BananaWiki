A small BananaWiki 1.4 instance (schema version 3) created with the 1.4 code:
an owner (`owner` / `owner-password-1`), an editor (`editor1` / `editor-password-1`),
a reader, two categories, pages with history, an inline image, a direct chat,
a kanban board, an API token and an encrypted settings value. `credentials.json`
holds a session cookie and the raw API token issued by 1.4 with `secret_key`.
`tests/test_upgrade_from_1x.py` boots BananaWiki 1.6 on a copy of it.
These credentials exist only for this fixture.
