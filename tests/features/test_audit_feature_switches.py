"""Every feature switch is recorded, and only owners and superusers switch the audit log off, whatever page
switches it (second audit of the 1.7 round: the onboarding wizard went around both rules)."""

from __future__ import annotations

import pytest

from bananawiki.wiki import registry
from bananawiki.wiki.db import connection_scope
from bananawiki.wiki.features.auth import onboarding


def _enabled(app, feature_id: str) -> bool:
    with app.test_request_context(), connection_scope():
        return registry.is_enabled(feature_id)


def _switchable(app) -> list[str]:
    with app.test_request_context(), connection_scope():
        return [feature.id for feature in onboarding.switchable_features()]


def _wizard(client, app, *, without: tuple[str, ...]):
    with app.test_request_context(), connection_scope():
        keep = [f.id for f in onboarding.switchable_features() if registry.is_enabled(f.id) and f.id not in without]
    return client.post("/onboarding", data={"site_name": "Wiki", "mode": "advanced", "features": keep})


def _entries(db, since: int) -> list[tuple[str, str]]:
    return [(row["action"], row["target_id"]) for row in
            db.all("SELECT action, target_id FROM audit_log WHERE id > ? ORDER BY id", (since,))]


def _last_id(db) -> int:
    return int(db.scalar("SELECT COALESCE(MAX(id), 0) FROM audit_log"))


@pytest.fixture
def owner_client(app, make_user, login):
    client = app.test_client()
    login(client, make_user("site_owner", role="owner"))
    return client


def test_a_plain_administrator_cannot_switch_the_audit_log_off_with_the_onboarding_wizard(app, admin_client, db):
    assert "audit" not in _switchable_for(app, admin_client)
    since = _last_id(db)
    response = _wizard(admin_client, app, without=("audit",))
    assert response.status_code == 302
    assert _enabled(app, "audit"), "the audit log stays on"
    assert ("plugin.disabled", "audit") not in _entries(db, since)


def _switchable_for(app, client) -> list[str]:
    """The features the wizard offers to the administrator signed in with *client*."""
    html = client.get("/onboarding").get_data(as_text=True)
    return [feature_id for feature_id in _switchable(app) if f'value="{feature_id}"' in html]


def test_owners_switch_the_audit_log_off_with_the_wizard_and_it_is_recorded(app, owner_client, db):
    assert "audit" in _switchable_for(app, owner_client)
    since = _last_id(db)
    assert _wizard(owner_client, app, without=("audit",)).status_code == 302
    assert not _enabled(app, "audit")
    assert _entries(db, since)[-1] == ("plugin.disabled", "audit"), "its last entry says it was switched off"


def test_features_switched_by_the_wizard_are_recorded(app, admin_client, db):
    since = _last_id(db)
    assert _wizard(admin_client, app, without=("kanban",)).status_code == 302
    assert not _enabled(app, "kanban")
    assert ("plugin.disabled", "kanban") in _entries(db, since)


def test_the_plugin_manager_records_each_switch_once(app, admin_client, db):
    since = _last_id(db)
    admin_client.post("/admin/plugins/kanban/disable")
    admin_client.post("/admin/plugins/kanban/enable")
    assert _entries(db, since) == [("plugin.disabled", "kanban"), ("plugin.enabled", "kanban")]


def test_the_registry_refuses_a_plain_administrator_whoever_calls_it(app, admin, db):
    from flask import g

    with app.test_request_context(), connection_scope():
        g.user = g.real_user = admin
        with pytest.raises(registry.SwitchRefused) as refused:
            registry.set_enabled("audit", False)
        assert refused.value.key == "audit.disable.owner_only"
        assert registry.is_enabled("audit")
        registry.set_enabled("kanban", False)  # every other feature: any administrator
        assert not registry.is_enabled("kanban")
