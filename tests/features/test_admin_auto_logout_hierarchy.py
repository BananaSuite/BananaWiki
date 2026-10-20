"""The daily automatic sign-out follows the account hierarchy.

Any administrator may switch it on and choose the hour, so it ends only the
sessions every administrator may end: those of users and editors. Administrators,
owners and superusers stay signed in, as with a plain administrator's mass
sign-out.
"""

from __future__ import annotations

from bananawiki.core.timeutil import sql_in
from bananawiki.wiki.db import connection_scope
from bananawiki.wiki.features.admin import jobs


def _run_due_sign_out(app, db) -> None:
    db.execute("UPDATE user_sessions SET created_at = ?", (sql_in(days=-3),))
    db.execute("INSERT INTO job_runs (name, last_run_at) VALUES (?, ?) ON CONFLICT(name) DO UPDATE SET "
               "last_run_at = excluded.last_run_at", (jobs.AUTO_LOGOUT_JOB, sql_in(days=-2)))
    with app.app_context(), connection_scope():
        jobs.auto_logout()


def test_a_plain_administrators_daily_sign_out_spares_the_accounts_above_editors(app, admin_client, db, make_user,
                                                                                  login):
    clients = {}
    for name, role, extra in (("member", "user", {}), ("writer", "editor", {}), ("colleague", "admin", {}),
                              ("boss", "owner", {}), ("super", "user", {"is_superuser": 1})):
        clients[name] = app.test_client()
        login(clients[name], make_user(name, role=role, **extra))
    admin_client.post("/admin/sessions/auto-logout", data={"auto_logout_enabled": "1", "auto_logout_hour": "3"})
    _run_due_sign_out(app, db)
    signed_in = {name: client.get("/_probe/private").data == b"private" for name, client in clients.items()}
    assert signed_in == {"member": False, "writer": False, "colleague": True, "boss": True, "super": True}
    assert admin_client.get("/_probe/private").data == b"private"


def test_the_settings_say_whom_it_signs_out(admin_client):
    page = admin_client.get("/admin/sessions").get_data(as_text=True)
    assert "Sign users and editors out every day" in page
    italian = admin_client.get("/admin/sessions?lang=it").get_data(as_text=True)
    assert "Disconnetti ogni giorno utenti ed editor" in italian
