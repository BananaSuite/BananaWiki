"""Regression tests for the hosting portal and operations findings of the security audit (W5).

Each test names its finding and reproduces the attack: it failed before the
fix and must keep passing.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import zipfile
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from cryptography.hazmat.primitives.ciphers import modes

from bananawiki.hosting.db import connection_scope, open_database
from bananawiki.hosting.runtime import RuntimeFailure, crypto
from bananawiki.ops import firewall
from bananawiki.ops.manager import Manager
from bananawiki.ops.system import System
from tests.hosting.agent_fakes import make_runtime, make_spec, provision
from tests.hosting.conftest import login, make_account, make_wiki, portal, query, web  # noqa: F401
from tests.ops.ops_fakes import FakeSystem, Upstream, legacy_install


def _serving(portal, instance_id):  # noqa: F811
    with portal.test_request_context("/"), connection_scope(portal.extensions["bananawiki.hosting.database"]):
        from bananawiki.hosting import domains, instances

        return domains.may_serve(instance_id), instances.get(instance_id)


def _offer(portal, make_account, make_wiki, login, query):  # noqa: F811
    abuser, sock = make_account(), make_account()
    wiki = make_wiki(abuser)
    owner_client = portal.test_client()
    login(owner_client, abuser)
    response = owner_client.post(f"/instances/{wiki['id']}/transfer", data={"username": sock["username"]})
    assert response.status_code == 302
    transfer = query("SELECT id FROM instance_ownership_transfers WHERE instance_id = ? "
                     "AND status = 'pending'", (wiki["id"],), one=True)
    assert transfer is not None
    return abuser, sock, wiki, transfer["id"]


@pytest.mark.parametrize("moderation", ["suspend", "schedule_deletion"])
def test_w5_w1_transfer_offered_before_moderation_cannot_be_accepted(
        portal, make_account, make_wiki, login, query, moderation):  # noqa: F811
    abuser, sock, wiki, transfer_id = _offer(portal, make_account, make_wiki, login, query)
    admin = portal.test_client()
    login(admin, make_account(admin=True))
    if moderation == "suspend":  # the form's default: the account's wikis are not suspended with it
        response = admin.post(f"/admin/accounts/{abuser['id']}/suspend",
                              data={"suspend_duration": "permanent", "suspend_reason": "spam"})
    else:
        response = admin.post(f"/admin/accounts/{abuser['id']}/schedule-deletion",
                              data={"deletion_days": "1", "deletion_reason": "spam"})
    assert response.status_code == 302
    assert _serving(portal, wiki["id"])[0] is False

    sock_client = portal.test_client()
    login(sock_client, sock)
    sock_client.post(f"/transfers/{transfer_id}/accept", data={"previous_owner": "view"})

    serving, inst = _serving(portal, wiki["id"])
    assert inst["account_id"] == abuser["id"] and serving is False
    assert query("SELECT status FROM instance_ownership_transfers WHERE id = ?", (transfer_id,),
                 one=True)["status"] == "cancelled"


def test_w5_w1_accept_refuses_a_moderated_owner_even_with_the_offer_still_pending(
        portal, make_account, make_wiki, login, query):  # noqa: F811
    """The accept itself checks, also when the offer was not cancelled (a row from before the fix)."""
    abuser, sock, wiki, transfer_id = _offer(portal, make_account, make_wiki, login, query)
    query("UPDATE accounts SET suspended = 1 WHERE id = ?", (abuser["id"],))
    sock_client = portal.test_client()
    login(sock_client, sock)
    sock_client.post(f"/transfers/{transfer_id}/accept")
    assert _serving(portal, wiki["id"])[1]["account_id"] == abuser["id"]
    assert query("SELECT status FROM instance_ownership_transfers WHERE id = ?", (transfer_id,),
                 one=True)["status"] == "cancelled"


def test_w5_w1_suspending_the_wiki_cancels_its_pending_transfer(
        portal, make_account, make_wiki, login, query):  # noqa: F811
    abuser, sock, wiki, transfer_id = _offer(portal, make_account, make_wiki, login, query)
    with portal.test_request_context("/"), connection_scope(portal.extensions["bananawiki.hosting.database"]):
        from bananawiki.hosting import instances

        instances.suspend(instances.get(wiki["id"]), actor_id=None, reason="abuse")
    sock_client = portal.test_client()
    login(sock_client, sock)
    sock_client.post(f"/transfers/{transfer_id}/accept")
    assert _serving(portal, wiki["id"])[1]["account_id"] == abuser["id"]


def test_w5_w1_unrelated_transfers_survive_a_suspension(
        portal, make_account, make_wiki, login, query):  # noqa: F811
    _abuser, _sock, _wiki, kept = _offer(portal, make_account, make_wiki, login, query)
    other = make_account()
    with portal.test_request_context("/"), connection_scope(portal.extensions["bananawiki.hosting.database"]):
        from bananawiki.hosting import accounts

        accounts.suspend(other["id"], until=None, reason="", reason_visible=False, time_visible=False,
                         actor_id=other["id"], duration_label="permanent")
    status = query("SELECT status FROM instance_ownership_transfers WHERE id = ?", (kept,), one=True)["status"]
    assert status == "pending"


def test_w5_w2_owner_of_a_suspended_wiki_cannot_transfer_or_add_collaborators(
        web, portal, make_account, make_wiki, login, query):  # noqa: F811
    owner, friend, other = make_account(), make_account(), make_account()
    wiki = make_wiki(owner)
    with portal.test_request_context("/"), connection_scope(portal.extensions["bananawiki.hosting.database"]):
        from bananawiki.hosting import instances

        instances.suspend(instances.get(wiki["id"]), actor_id=None, reason="abuse")
    login(web, owner)
    web.post(f"/instances/{wiki['id']}/transfer", data={"username": friend["username"]})
    web.post(f"/instances/{wiki['id']}/collaborators/add",
             data={"username": other["username"], "role": "full_access"})
    web.post(f"/instances/{wiki['id']}/use-case",
             data={"declared_use_case": "x" * 40, "compliance_declared": "1"})

    assert not query("SELECT 1 FROM instance_ownership_transfers WHERE instance_id = ?", (wiki["id"],))
    assert not query("SELECT 1 FROM instance_collaborators WHERE instance_id = ?", (wiki["id"],))
    assert query("SELECT declared_use_case FROM instances WHERE id = ?", (wiki["id"],),
                 one=True)["declared_use_case"] != "x" * 40


def test_w5_w4_admin_viewing_a_migrated_wiki_leaves_the_owners_initial_password(
        portal, make_account, make_wiki, login, query):  # noqa: F811
    owner, admin = make_account(), make_account(admin=True)
    wiki = make_wiki(owner)
    query("UPDATE instances SET admin_password_plain = 'initial-secret' WHERE id = ?", (wiki["id"],))
    admin_client = portal.test_client()
    login(admin_client, admin)
    assert "initial-secret" not in admin_client.get(f"/instances/{wiki['id']}").get_data(as_text=True)
    admin_client.post(f"/admin/accounts/{owner['id']}/impersonate")
    assert "initial-secret" not in admin_client.get(f"/instances/{wiki['id']}").get_data(as_text=True)
    stored = query("SELECT admin_password_plain FROM instances WHERE id = ?", (wiki["id"],), one=True)
    assert stored["admin_password_plain"] == "initial-secret"

    owner_client = portal.test_client()
    login(owner_client, owner)
    assert "initial-secret" in owner_client.get(f"/instances/{wiki['id']}").get_data(as_text=True)
    assert "initial-secret" not in owner_client.get(f"/instances/{wiki['id']}").get_data(as_text=True)


VERIFIER = "v" * 64
CHALLENGE = base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).decode().rstrip("=")


def test_w5_w3_signing_out_other_devices_reaches_platform_sign_in(
        web, portal, make_account, make_wiki, login, query):  # noqa: F811
    query("UPDATE hosting_settings SET platform_oauth_enabled = 1 WHERE id = 1")
    user = make_account()
    wiki = make_wiki(make_account())
    with portal.test_request_context("/"), connection_scope(portal.extensions["bananawiki.hosting.database"]):
        from bananawiki.hosting import oauth, urls

        client_id, secret = oauth.rotate_credentials(wiki["id"])
        redirect_uri = urls.instance_url(wiki) + "/auth/platform/callback"
    thief = portal.test_client()
    login(thief, user)
    authorize = thief.post(f"/oauth/authorize?response_type=code&client_id={client_id}&redirect_uri={redirect_uri}"
                           f"&state=s&code_challenge={CHALLENGE}&code_challenge_method=S256", data={"action": "allow"})
    code = parse_qs(urlparse(authorize.headers["Location"]).query)["code"][0]
    basic = base64.b64encode(f"{client_id}:{secret}".encode()).decode()
    token = thief.post("/oauth/token", headers={"Authorization": f"Basic {basic}"}, data={
        "grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri,
        "code_verifier": VERIFIER}).get_json()["access_token"]
    assert thief.get("/oauth/userinfo", headers={"Authorization": f"Bearer {token}"}).status_code == 200

    login(web, user)
    assert web.post("/account/sessions/logout-all").status_code == 302

    assert thief.get("/dashboard").status_code == 302
    assert thief.get("/oauth/userinfo", headers={"Authorization": f"Bearer {token}"}).status_code == 401
    queued = query("SELECT instance_id FROM hosting_oauth_signouts WHERE account_id = ?", (user["id"],))
    assert [row["instance_id"] for row in queued] == [wiki["id"]]
    assert web.get("/account").status_code == 200  # the device that asked stays signed in to the portal


def test_w5_1_platform_backup_goes_past_the_single_gcm_stream_limit(tmp_path, monkeypatch):
    """One GCM nonce encrypts at most 2^36 - 32 bytes: scaled down here, as cryptography reads the limit there."""
    runtime, _agent = make_runtime(tmp_path)
    open_database(runtime._cfg()).initialize()
    base = Path(runtime._cfg().instances_dir)
    provision(runtime, make_spec("beta"))
    big = os.urandom(256 * 1024)
    (base / "beta" / "storage" / "uploads" / "big.bin").write_bytes(big)
    monkeypatch.setattr(modes.GCM, "_MAX_ENCRYPTED_BYTES", 128 * 1024)  # stands for 2**36 - 32
    monkeypatch.setattr(crypto, "SEGMENT", 64 * 1024)  # segments stay far below it, as 1 MiB is below 64 GiB
    work = tmp_path / "work"
    work.mkdir()

    path = runtime.export_platform(work)
    assert runtime.platform_backup_status()["size"] == path.stat().st_size  # for the restore-limit warning

    plain = crypto.decrypt_file(path, tmp_path / "plain.zip", runtime.backup_key())
    with zipfile.ZipFile(plain) as archive:
        assert archive.read("instances/beta/storage/uploads/big.bin") == big
        assert "instances/beta/bananawiki.db" in archive.namelist()


def _sealed(tmp_path, monkeypatch, data: bytes) -> tuple[Path, bytes]:
    monkeypatch.setattr(crypto, "SEGMENT", 1024)
    key = os.urandom(32)
    output = crypto.EncryptedOutput(tmp_path / "out.bwenc", key)
    for start in range(0, len(data), 700):
        output.write(data[start:start + 700])
    return output.finish(), key


@pytest.mark.parametrize("damage", ["cut_at_segment", "cut_inside", "swap_segments", "appended", "header"])
def test_w5_1_segmented_backups_refuse_truncation_and_reordering(tmp_path, monkeypatch, damage):
    data = os.urandom(3 * 1024)  # three full segments, then an empty final one
    path, key = _sealed(tmp_path, monkeypatch, data)
    assert crypto.decrypt_file(path, tmp_path / "plain", key).read_bytes() == data
    raw = path.read_bytes()
    head, size = len(crypto.MAGIC) + crypto.SALT_SIZE, 1024 + crypto.TAG_SIZE
    segments = [raw[head + n * size: head + (n + 1) * size] for n in range(3)]
    damaged = {
        "cut_at_segment": raw[:head + 2 * size],
        "cut_inside": raw[:-5],
        "swap_segments": raw[:head] + segments[1] + segments[0] + raw[head + 2 * size:],
        "appended": raw + raw[-crypto.TAG_SIZE:],
        "header": raw[:len(crypto.MAGIC)] + bytes(crypto.SALT_SIZE) + raw[head:],
    }[damage]
    path.write_bytes(damaged)
    with pytest.raises(RuntimeFailure) as error:
        crypto.decrypt_file(path, tmp_path / "out", key)
    assert error.value.code == "archive_invalid" and not (tmp_path / "out").exists()


def test_w5_1_settings_warn_when_the_latest_backup_is_larger_than_a_restore_accepts(
        web, portal, make_account, login):  # noqa: F811
    portal.extensions["bananawiki.hosting.runtime"].backup_status = {"size": 64 * 1024 ** 3}
    login(web, make_account(admin=True))
    page = web.get("/admin/settings").get_data(as_text=True)
    assert "HOSTING_IMPORT_MAX_BYTES" in page and "64.0 GB" in page


def test_w5_1_an_unexpected_export_error_leaves_no_work_folder(web, portal, make_account, login, monkeypatch):  # noqa: F811
    runtime = portal.extensions["bananawiki.hosting.runtime"]

    def broken(work):
        (Path(work) / "partial").write_bytes(b"x")
        raise ValueError("Exceeded maximum encrypted byte limit")

    monkeypatch.setattr(runtime, "export_platform", broken)
    login(web, make_account(admin=True))
    assert web.post("/admin/export-platform").status_code == 500
    root = Path(portal.config["HOSTING"].archives.export_temp_dir)
    assert not list(root.glob("bwh-platform-*"))


def _without_firewall(system):
    """The host answers every command, except that the firewall table is not loaded (nothing runs as root)."""
    original = system.runner

    def run(command, **kwargs):
        if command[:2] == ["nft", "list"]:
            return subprocess.CompletedProcess(command, 1, "", "Error: No such file or directory\n"
                                                             "list table inet bananawiki_tenants\n")
        return original(command, **kwargs)

    system.runner = run


def test_w5_2_status_warns_and_records_a_missing_tenant_firewall(tmp_path):
    system, upstream = FakeSystem(tmp_path / "host"), Upstream(tmp_path)
    root = tmp_path / "opt" / "bananawiki"
    legacy_install(root, system, upstream, upstream.commit("hosting 1"), mode="hosting")
    manager = Manager(root, system=system)
    assert manager.status()["tenant_firewall"] == {"loaded": True}

    _without_firewall(system)
    status = manager.status()

    assert status["tenant_firewall"]["loaded"] is False
    assert any("host firewall for tenant containers is not loaded" in item for item in status["warnings"])
    assert ["nft", "list", "table", "inet", "bananawiki_tenants"] in system.commands
    # What the runtime agent hands the portal's administrators.
    assert firewall.recorded(root / "config" / firewall.RECORD_NAME)["loaded"] is False


def test_w5_2_the_probe_reports_nft_missing_or_refusing(tmp_path):
    def missing(command, **kwargs):
        raise FileNotFoundError(command[0])

    assert System(runner=missing).tenant_firewall() == "nft is not installed"
    system = System(runner=lambda command, **kwargs: subprocess.CompletedProcess(command, 0, firewall.rules(), ""))
    assert system.tenant_firewall() is None
    _without_firewall(system)
    assert "No such file or directory" in system.tenant_firewall()


def test_w5_2_a_check_from_before_the_last_boot_counts_as_none(tmp_path):
    path = tmp_path / firewall.RECORD_NAME
    assert firewall.recorded(path, boot="b")["loaded"] is None
    path.write_text(json.dumps(firewall.record(None, boot="a")))
    assert firewall.recorded(path, boot="a")["loaded"] is True
    assert firewall.recorded(path, boot="b")["loaded"] is None


@pytest.mark.parametrize("loaded", [False, None])
def test_w5_2_the_admin_dashboard_calls_attention_to_the_firewall(web, portal, make_account, login, loaded):  # noqa: F811
    portal.extensions["bananawiki.hosting.runtime"].firewall = {"loaded": loaded, "reason": "table missing",
                                                                "checked_at": ""}
    login(web, make_account(admin=True))
    page = web.get("/admin").get_data(as_text=True)
    assert 'id="host-firewall"' in page
    assert ("is not loaded on this server" if loaded is False else "has not been checked since") in page
    portal.extensions["bananawiki.hosting.runtime"].firewall = {"loaded": True, "reason": "", "checked_at": ""}
    assert 'id="host-firewall"' not in web.get("/admin").get_data(as_text=True)


def test_w5_2_the_portal_reads_the_firewall_check_the_agent_passes_on(tmp_path, monkeypatch):
    from bananawiki.ops import runtime_agent

    root = tmp_path / "opt"
    (root / "config").mkdir(parents=True)
    (root / "config" / "installation.json").write_text('{"service": "root"}')
    assert runtime_agent.load_config(root)["firewall_record"] == str(root / "config" / firewall.RECORD_NAME)

    runtime, agent = make_runtime(tmp_path)
    assert runtime.host_firewall() == {}, "an agent that does not say"
    runtime._storage_checked = None
    answer = {"loaded": False, "reason": "nft is not installed", "checked_at": "2026-10-27T10:00:00+00:00"}
    monkeypatch.setattr(agent, "ping", lambda _args: {"protocol": 1, "docker": True, "storage": {}, "firewall": answer})
    assert runtime.host_firewall() == answer


def test_r3_3_an_empty_or_outdated_table_does_not_count_as_loaded():
    def listing(text):
        return System(runner=lambda command, **kwargs: subprocess.CompletedProcess(command, 0, text, ""))

    assert listing(firewall.rules()).tenant_firewall() is None
    assert "not this release's" in listing("table inet bananawiki_tenants {\n}\n").tenant_firewall()
    stale = firewall.rules().replace(firewall.expected_digest(), "0" * 16)
    assert "not this release's" in listing(stale).tenant_firewall()
