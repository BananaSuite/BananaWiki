"""Focused tests for managed-hosting feature requests and env gates."""

import os

import pytest

from helpers._passwords import generate_password_hash
from hosting import config as hosting_config
from hosting.app import create_hosting_app


@pytest.fixture(autouse=True)
def isolated_hosting(tmp_path, monkeypatch):
    hosting_config.HOSTING_DATABASE_PATH = str(tmp_path / "hosting.db")
    hosting_config.INSTANCES_DIR = str(tmp_path / "instances")
    os.makedirs(hosting_config.INSTANCES_DIR, exist_ok=True)
    from hosting.db import init_hosting_db

    init_hosting_db()
    monkeypatch.setattr(
        "hosting.instance_manager._get_or_create_instance_oauth_credentials",
        lambda inst, port: (None, None),
    )


@pytest.fixture
def app():
    application = create_hosting_app()
    application.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    return application


@pytest.fixture
def client(app):
    return app.test_client()


def _account(username, *, admin=False):
    from hosting.db import create_account

    return create_account(
        username, generate_password_hash("password123"), is_admin=admin
    )


def _instance(owner_id, subdomain="feature-test"):
    from hosting.db import create_instance

    return create_instance(owner_id, subdomain)


def _login(client, username):
    response = client.post(
        "/login",
        data={"username": username, "password": "password123"},
        follow_redirects=False,
    )
    assert response.status_code in {302, 303}


def _logout(client):
    with client.session_transaction() as session:
        session.clear()


def test_db_lifecycle_retains_denial_and_allows_retry():
    from hosting.db import (
        cancel_instance_feature_request,
        create_instance_feature_request,
        get_instance,
        list_instance_feature_requests,
        review_instance_feature_request,
    )

    admin_id = _account("admin", admin=True)
    owner_id = _account("owner")
    instance = _instance(owner_id)
    first = create_instance_feature_request(
        instance["id"], owner_id, "public_access", "Public readers need access to this documentation."
    )
    with pytest.raises(ValueError, match="pending_exists"):
        create_instance_feature_request(
            instance["id"], owner_id, "public_access", "A second pending request must not be accepted."
        )

    denied = review_instance_feature_request(
        first["id"], admin_id, "denied", "The access plan needs more detail."
    )
    assert denied["status"] == "denied"
    assert get_instance(instance["id"])["public_wiki_allowed"] == 0

    retry = create_instance_feature_request(
        instance["id"], owner_id, "public_access", "The public audience and moderation plan are now documented."
    )
    approved = review_instance_feature_request(
        retry["id"], admin_id, "approved", "Approved after the owner clarified moderation."
    )
    assert approved["status"] == "approved"
    assert get_instance(instance["id"])["public_wiki_allowed"] == 1

    page_request = create_instance_feature_request(
        instance["id"], owner_id, "page_builder", "Editors need the page builder for a custom landing page."
    )
    cancel_instance_feature_request(page_request["id"], owner_id)
    history = list_instance_feature_requests(instance["id"])
    assert [item["status"] for item in history] == [
        "cancelled", "approved", "denied"
    ]
    assert history[1]["review_note"].startswith("Approved after")


def test_instance_rebuild_preserves_entitlement_columns():
    from hosting.db import (
        create_instance_feature_request,
        get_hosting_db_context,
        list_instance_feature_requests,
    )
    from hosting.db._schema import _rebuild_instances_with_composite_unique

    owner_id = _account("rebuild-owner")
    instance = _instance(owner_id, "rebuild-test")
    create_instance_feature_request(
        instance["id"], owner_id, "public_access",
        "Preserve this request while rebuilding the instance table.",
    )
    with get_hosting_db_context() as conn:
        conn.execute(
            "UPDATE instances SET public_wiki_allowed=1, page_builder_allowed=1 WHERE id=?",
            (instance["id"],),
        )
        conn.commit()
        _rebuild_instances_with_composite_unique(conn)
        row = conn.execute(
            "SELECT public_wiki_allowed, page_builder_allowed FROM instances WHERE id=?",
            (instance["id"],),
        ).fetchone()
        assert tuple(row) == (1, 1)
    assert len(list_instance_feature_requests(instance["id"])) == 1


def test_owner_and_admin_route_authorization(client):
    from hosting.db import (
        get_instance,
        list_instance_feature_requests,
        update_instance_status,
    )

    admin_id = _account("route-admin", admin=True)
    owner_id = _account("route-owner")
    other_id = _account("other-owner")
    del admin_id, other_id
    instance = _instance(owner_id, "route-test")
    update_instance_status(instance["id"], "stopped")

    _login(client, "other-owner")
    response = client.post(
        f"/instances/{instance['id']}/features/public_access/requests",
        data={"reason": "I should not be able to request this for another owner."},
        follow_redirects=True,
    )
    assert b"Instance not found" in response.data
    assert list_instance_feature_requests(instance["id"]) == []

    _logout(client)
    _login(client, "route-owner")
    response = client.post(
        f"/instances/{instance['id']}/features/public_access/requests",
        data={"reason": "Readers need anonymous access to our published documentation."},
        follow_redirects=True,
    )
    assert b"request submitted for review" in response.data
    feature_request = list_instance_feature_requests(instance["id"])[0]

    response = client.post(
        f"/admin/feature-requests/{feature_request['id']}/approve",
        data={"review_note": "Owner accounts cannot review requests."},
        follow_redirects=False,
    )
    assert response.status_code in {302, 303}
    assert list_instance_feature_requests(instance["id"])[0]["status"] == "pending"

    _logout(client)
    _login(client, "route-admin")
    dashboard = client.get("/admin")
    assert dashboard.status_code == 200
    assert b"Pending feature requests" in dashboard.data
    assert b"Readers need anonymous access" in dashboard.data
    response = client.post(
        f"/admin/feature-requests/{feature_request['id']}/approve",
        data={"review_note": "The documented public use case is acceptable."},
        follow_redirects=True,
    )
    assert b"request approved and entitlement granted" in response.data
    assert get_instance(instance["id"])["public_wiki_allowed"] == 1


def test_auto_approval_settings_default_to_disabled():
    from hosting.db import (
        create_instance_feature_request,
        get_hosting_settings,
    )

    owner_id = _account("default-policy-owner")
    instance = _instance(owner_id, "default-policy")
    settings = get_hosting_settings()

    assert settings["auto_approve_public_access_requests"] == 0
    assert settings["auto_approve_page_builder_requests"] == 0
    feature_request = create_instance_feature_request(
        instance["id"],
        owner_id,
        "public_access",
        "Readers need access, but the default policy still requires review.",
    )
    assert feature_request["status"] == "pending"


@pytest.mark.parametrize(
    ("feature", "setting", "entitlement_column"),
    [
        (
            "public_access",
            "auto_approve_public_access_requests",
            "public_wiki_allowed",
        ),
        (
            "page_builder",
            "auto_approve_page_builder_requests",
            "page_builder_allowed",
        ),
    ],
)
def test_feature_request_auto_approval_is_independent_and_owner_visible(
    client, monkeypatch, feature, setting, entitlement_column
):
    from hosting.db import (
        get_hosting_settings,
        get_instance,
        list_instance_feature_requests,
        update_hosting_settings,
    )

    _account(f"{feature}-policy-admin", admin=True)
    owner_id = _account(f"{feature}-policy-owner")
    instance = _instance(owner_id, f"auto-{feature.replace('_', '-')}")
    restarted = []
    monkeypatch.setattr(
        "hosting.routes.dashboard_instances.force_restart_instance",
        lambda instance_id: (restarted.append(instance_id) or True, ""),
    )
    update_hosting_settings(**{setting: 1})

    _login(client, f"{feature}-policy-owner")
    response = client.post(
        f"/instances/{instance['id']}/features/{feature}/requests",
        data={"reason": "This managed wiki needs the feature for its documented audience."},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert b"automatically approved by policy" in response.data
    assert b"Automatic policy" in response.data
    assert b"Automatically approved because hosting policy permits" in response.data
    assert restarted == [instance["id"]]
    assert get_instance(instance["id"])[entitlement_column] == 1
    feature_request = list_instance_feature_requests(instance["id"])[0]
    assert feature_request["status"] == "approved"
    assert feature_request["reviewed_by"] is None
    assert feature_request["reviewed_at"]
    assert feature_request["review_source"] == "automatic"
    assert feature_request["review_note"].startswith("Automatically approved")

    other_setting = (
        "auto_approve_page_builder_requests"
        if feature == "public_access"
        else "auto_approve_public_access_requests"
    )
    assert get_hosting_settings()[other_setting] == 0


def test_admin_can_save_independent_auto_approval_toggles(client):
    from hosting.db import get_hosting_settings

    _account("settings-policy-admin", admin=True)
    _login(client, "settings-policy-admin")
    response = client.post(
        "/admin/settings",
        data={
            "action": "save_account_communications",
            "email_verification_cooldown_seconds": "60",
            "forbid_non_admin_public_wikis": "1",
            "forbid_non_admin_page_builder": "1",
            "auto_approve_public_access_requests": "1",
        },
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert b"Automatically approve public access requests" in response.data
    settings = get_hosting_settings()
    assert settings["auto_approve_public_access_requests"] == 1
    assert settings["auto_approve_page_builder_requests"] == 0


def test_auto_approval_reports_restart_failure_without_losing_entitlement(
    client, monkeypatch
):
    from hosting.db import (
        get_instance,
        list_instance_feature_requests,
        update_hosting_settings,
    )

    _account("auto-failure-admin", admin=True)
    owner_id = _account("auto-failure-owner")
    instance = _instance(owner_id, "auto-failure")
    update_hosting_settings(auto_approve_public_access_requests=1)
    monkeypatch.setattr(
        "hosting.routes.dashboard_instances.force_restart_instance",
        lambda instance_id: (False, "Process failed to start."),
    )

    _login(client, "auto-failure-owner")
    response = client.post(
        f"/instances/{instance['id']}/features/public_access/requests",
        data={"reason": "Public readers need this documentation without signing in."},
        follow_redirects=True,
    )

    assert b"automatically approved and the entitlement was granted" in response.data
    assert b"could not be restarted: Process failed to start" in response.data
    assert get_instance(instance["id"])["public_wiki_allowed"] == 1
    assert list_instance_feature_requests(instance["id"])[0]["status"] == "approved"


def test_direct_entitlement_reports_restart_failure(client, monkeypatch):
    from hosting.db import get_instance

    _account("restart-admin", admin=True)
    owner_id = _account("restart-owner")
    instance = _instance(owner_id, "restart-feature-test")
    monkeypatch.setattr(
        "hosting.routes.dashboard_features.force_restart_instance",
        lambda instance_id: (False, "Process failed to start."),
    )

    _login(client, "restart-admin")
    response = client.post(
        f"/admin/instances/{instance['id']}/features/page_builder/entitlement",
        data={"allowed": "1"},
        follow_redirects=True,
    )
    assert b"could not be restarted" in response.data
    assert b"Process failed to start" in response.data
    assert b"running instance was restarted" not in response.data
    assert get_instance(instance["id"])["page_builder_allowed"] == 1


def test_instance_env_defaults_and_entitlement_gates():
    from hosting.db import (
        get_instance,
        set_instance_feature_entitlement,
        update_hosting_settings,
    )
    from hosting.instance_manager import _instance_env

    owner_id = _account("env-owner")
    instance = _instance(owner_id, "env-test")

    env = _instance_env("/tmp/env-test", instance["port"], dict(instance))
    assert env["BW_FORBID_PUBLIC_MODE"] == "1"
    assert env["BW_FORBID_PAGE_BUILDER"] == "1"
    assert env["BW_FORBID_PUBLIC_BUILDER_PAGES"] == "1"

    set_instance_feature_entitlement(instance["id"], "public_access", True)
    set_instance_feature_entitlement(instance["id"], "page_builder", True)
    env = _instance_env(
        "/tmp/env-test", instance["port"], dict(get_instance(instance["id"]))
    )
    assert env["BW_FORBID_PUBLIC_MODE"] == "0"
    assert env["BW_FORBID_PAGE_BUILDER"] == "0"
    assert env["BW_FORBID_PUBLIC_BUILDER_PAGES"] == "0"

    set_instance_feature_entitlement(instance["id"], "public_access", False)
    set_instance_feature_entitlement(instance["id"], "page_builder", False)
    update_hosting_settings(
        forbid_non_admin_public_wikis=0,
        forbid_non_admin_page_builder=0,
    )
    env = _instance_env(
        "/tmp/env-test", instance["port"], dict(get_instance(instance["id"]))
    )
    assert env["BW_FORBID_PUBLIC_MODE"] == "0"
    assert env["BW_FORBID_PAGE_BUILDER"] == "0"
    assert env["BW_FORBID_PUBLIC_BUILDER_PAGES"] == "0"

    admin_id = _account("env-admin", admin=True)
    admin_instance = _instance(admin_id, "admin-env-test")
    update_hosting_settings(
        forbid_non_admin_public_wikis=1,
        forbid_non_admin_page_builder=1,
    )
    env = _instance_env(
        "/tmp/admin-env-test", admin_instance["port"], dict(admin_instance)
    )
    assert env["BW_FORBID_PUBLIC_MODE"] == "0"
    assert env["BW_FORBID_PAGE_BUILDER"] == "0"
    assert env["BW_FORBID_PUBLIC_BUILDER_PAGES"] == "0"


def test_global_policy_change_restarts_running_instances(client, monkeypatch):
    from hosting.db import get_hosting_settings

    _account("policy-admin", admin=True)
    owner_id = _account("policy-owner")
    instance = _instance(owner_id, "policy-restart")
    restarted = []
    monkeypatch.setattr(
        "hosting.routes.dashboard_settings.force_restart_instance",
        lambda instance_id: (restarted.append(instance_id) or True, ""),
    )
    _login(client, "policy-admin")
    response = client.post(
        "/global-settings",
        data={
            "action": "save_account_communications",
            "email_verification_cooldown_seconds": "60",
            # Omitted restriction checkboxes change both defaults from on to off.
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert instance["id"] in restarted
    assert get_hosting_settings()["forbid_non_admin_page_builder"] == 0


def test_admin_demotion_restarts_owned_instances(client, monkeypatch):
    _account("demotion-admin", admin=True)
    target_id = _account("demotion-target", admin=True)
    instance = _instance(target_id, "demotion-restart")
    restarted = []
    monkeypatch.setattr(
        "hosting.routes.dashboard_accounts.force_restart_instance",
        lambda instance_id: (restarted.append(instance_id) or True, ""),
    )
    _login(client, "demotion-admin")
    response = client.post(
        f"/admin/accounts/{target_id}/toggle-admin",
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert instance["id"] in restarted
