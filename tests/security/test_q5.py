"""Regression tests for the q5 findings of the full re-audit pass 11 (portal email budget, tenant tasks, speech server)."""

from __future__ import annotations

import socket
import threading

import pytest

from bananawiki.core.timeutil import now_sql
from bananawiki.hosting import email
from bananawiki.hosting.db import db
from tests.features.test_tts_gpu import FakeEngine, load_server
from tests.hosting.conftest import login, make_account, make_wiki, portal, query, runtime, web  # noqa: F401

# QW5-1: the GPU speech server answers a Content-Length int() cannot read instead of dropping the connection


def _post(port: int, token: str, length: bytes) -> bytes:
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        sock.sendall(b"POST /synthesize HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer " + token.encode()
                     + b"\r\nContent-Type: application/json\r\nContent-Length: " + length + b"\r\n\r\n{}")
        answer = b""
        while chunk := sock.recv(4096):  # the server closes the connection after an error
            answer += chunk
        return answer


@pytest.mark.parametrize("length", [b"\xb2", b"\xb9\xb2", b"1\xb2"])
def test_qw5_1_speech_server_refuses_a_content_length_int_cannot_read(length, capsys):
    module = load_server()
    speech = module.make_server(module.Settings(token="x" * 32, port=0), FakeEngine())
    threading.Thread(target=speech.serve_forever, daemon=True).start()
    try:
        port = speech.server_address[1]
        token = module.tenant_token("x" * 32, "wiki1")
        assert _post(port, token, length).startswith(b"HTTP/1.1 411")
        assert _post(port, token, b"9" * 5000).startswith(b"HTTP/1.1 413")  # too long for int(), still too large
        assert _post(port, token, b"0" * 5000 + b"2").startswith(b"HTTP/1.1 400")  # read as 2: "{}" has no text
    finally:
        speech.shutdown()
        speech.server_close()
    assert "Traceback" not in capsys.readouterr().err


# QW4-1: other accounts cannot use up an address's daily email, and nothing holds back its recovery mail


def test_qw4_1_collaborator_notices_cannot_hold_back_a_password_reset(portal, make_account, make_wiki, login,  # noqa: F811
                                                                       query):  # noqa: F811
    victim = make_account("victim", email="victim@example.org", email_verified_at="2026-01-01 00:00:00")
    with email.capture_outbox() as outbox:
        for number, name in enumerate(("mallory1", "mallory2")):
            attacker = make_account(name)
            wiki = make_wiki(attacker)
            client = portal.test_client()
            login(client, attacker)
            for cycle in range(8):
                network = {"REMOTE_ADDR": f"198.51.{number}.{cycle + 1}"}
                client.post(f"/instances/{wiki['id']}/collaborators/add", environ_base=network,
                            data={"username": "victim", "role": "custom", "permissions": ["view"]})
                client.post(f"/instances/{wiki['id']}/collaborators/{victim['id']}/remove", environ_base=network)
        notices = [message for message in outbox if message.to == "victim@example.org"]
        portal.test_client().post("/forgot-password", data={"username": "victim", "email": "victim@example.org"},
                                  environ_base={"REMOTE_ADDR": "192.0.2.77"})
    assert len(notices) == 5  # FROM_OTHERS_PER_RECIPIENT_DAILY_LIMIT, of the 16 additions
    assert outbox[-1].to == "victim@example.org" and "/reset-password?token=" in outbox[-1].action_url
    assert query("SELECT COUNT(*) AS n FROM instance_collaborators")[0]["n"] == 0


def test_qw4_1_a_full_inbox_still_gets_recovery_and_credential_notices(portal):  # noqa: F811
    with portal.app_context():
        for _ in range(email.PER_RECIPIENT_DAILY_LIMIT):
            db.insert("hosting_email_log", {"sent_at": now_sql(), "recipient": "victim@example.org",
                                            "account_id": None, "kind": "instance_created"})
        settings = portal.config["HOSTING"].email
        assert email.refusal(settings, "Victim@example.org", "instance_created", None) == "recipient_limit"
        assert email.refusal(settings, "victim@example.org", "collaborator_added", "a1") == "recipient_limit"
        for kind in ("reset", "username", "password_changed", "email_changed"):
            assert email.refusal(settings, "victim@example.org", kind, None) is None, kind
        # Each still has its own cap.
        for _ in range(email.CREDENTIAL_PER_RECIPIENT_DAILY_LIMIT):
            db.insert("hosting_email_log", {"sent_at": now_sql(), "recipient": "victim@example.org",
                                            "account_id": None, "kind": "password_changed"})
        assert email.refusal(settings, "victim@example.org", "password_changed", None) == "recipient_limit"
        assert email.refusal(settings, "victim@example.org", "reset", None) is None



def test_qw4_1_hosting_docs_describe_the_per_recipient_budgets():
    """The 'Email budgets' table in docs/hosting.md matches the caps refusal() applies."""
    from pathlib import Path
    text = (Path(__file__).resolve().parents[2] / "docs" / "hosting.md").read_text(encoding="utf-8")
    row = next(line for line in text.splitlines() if line.startswith("| Per recipient |"))
    assert f"{email.PER_RECIPIENT_DAILY_LIMIT}, at most {email.FROM_OTHERS_PER_RECIPIENT_DAILY_LIMIT} of them notices another account caused" in row
    assert f"Password resets ({email.RECOVERY_PER_RECIPIENT_DAILY_LIMIT})" in row
    assert f"user-name reminders ({email.RECOVERY_PER_RECIPIENT_DAILY_LIMIT})" in row
    assert f"({email.CREDENTIAL_PER_RECIPIENT_DAILY_LIMIT} each) are counted apart" in row
    assert "of which at most" not in row

# QW4-2: switching Easy Wiki on a paused wiki and resetting its administrator password have per-wiki budgets


def _calls(fake, name: str, since: int) -> int:
    return [call for call, _ in fake.calls[since:]].count(name)


def test_qw4_2_easy_wiki_switches_of_a_paused_wiki_are_counted(portal, runtime, make_account, make_wiki,  # noqa: F811
                                                                login):  # noqa: F811
    owner = make_account()
    wiki = make_wiki(owner, "paused")
    client = portal.test_client()
    login(client, owner)
    client.post(f"/instances/{wiki['id']}/stop", environ_base={"REMOTE_ADDR": "192.0.2.200"})
    before = len(runtime.calls)
    for number in range(12):  # each from its own network, so the per-network limit never applies
        client.post(f"/instances/{wiki['id']}/toggle-easy-wiki", data={"enable": "1" if number % 2 == 0 else "0"},
                    environ_base={"REMOTE_ADDR": f"10.{number}.0.1"})
    # The stop used one of the wiki's 6 changes a minute.
    assert _calls(runtime, "apply_limits", before) == 5


def test_qw4_2_administrator_password_resets_have_a_per_wiki_budget(portal, runtime, make_account, make_wiki,  # noqa: F811
                                                                     login, query):  # noqa: F811
    owner = make_account()
    wiki = make_wiki(owner, "resets")
    client = portal.test_client()
    login(client, owner)
    client.post(f"/instances/{wiki['id']}/stop", environ_base={"REMOTE_ADDR": "192.0.2.200"})
    before = len(runtime.calls)
    pages = [client.post(f"/instances/{wiki['id']}/reset-password", environ_base={"REMOTE_ADDR": f"10.{n}.1.1"})
             for n in range(8)]
    assert _calls(runtime, "reset_admin_password", before) == 3
    assert [page.status_code for page in pages] == [200] * 3 + [302] * 5
    assert "reset too often" in client.get(pages[-1].headers["Location"]).get_data(as_text=True)
    assert query("SELECT COUNT(*) AS n FROM hosting_events WHERE action = 'instance.admin_password_reset'")[0]["n"] == 3
    # Pausing and resuming does not use up the resets, nor resets the pauses.
    for number in range(2):
        client.post(f"/instances/{wiki['id']}/start", environ_base={"REMOTE_ADDR": f"10.9{number}.0.1"})
        client.post(f"/instances/{wiki['id']}/stop", environ_base={"REMOTE_ADDR": f"10.9{number}.0.2"})
    assert query("SELECT status FROM instances WHERE id = ?", (wiki["id"],))[0]["status"] == "stopped"


def _age_resets() -> None:
    db.execute("UPDATE hosting_rate_limit_hits SET hit_at = datetime(hit_at, '-2 minutes') "
               "WHERE bucket = 'hosting_wiki_admin_reset'")


def test_qw4_2_a_collaborator_cannot_spend_the_owners_password_resets(portal, runtime, make_account,  # noqa: F811
                                                                       make_wiki, login):  # noqa: F811
    owner = make_account("resetowner")
    other = make_account("resetcollab")
    wiki = make_wiki(owner, "sharedresets")
    owner_client = portal.test_client()
    login(owner_client, owner)
    owner_client.post(f"/instances/{wiki['id']}/collaborators/add", environ_base={"REMOTE_ADDR": "192.0.2.1"},
                      data={"username": "resetcollab", "role": "custom", "permissions": ["view", "reset_password"]})
    other_client = portal.test_client()
    login(other_client, other)
    before = len(runtime.calls)
    for number in range(22):  # the minute windows aged away, so only the day's budget applies
        other_client.post(f"/instances/{wiki['id']}/reset-password", environ_base={"REMOTE_ADDR": f"10.{number}.1.1"})
        with portal.app_context():
            _age_resets()
    assert _calls(runtime, "reset_admin_password", before) == 20
    owner_client.post(f"/instances/{wiki['id']}/collaborators/{other['id']}/remove",
                      environ_base={"REMOTE_ADDR": "192.0.2.2"})
    before = len(runtime.calls)
    page = owner_client.post(f"/instances/{wiki['id']}/reset-password", environ_base={"REMOTE_ADDR": "192.0.2.3"})
    assert page.status_code == 200
    assert _calls(runtime, "reset_admin_password", before) == 1


def test_qw4_2_a_previous_owner_cannot_spend_the_new_owners_password_resets(portal, runtime, make_account,  # noqa: F811
                                                                             make_wiki):  # noqa: F811
    from bananawiki.hosting import instances
    old_owner = make_account("resetold")
    new_owner = make_account("resetnew")
    wiki = make_wiki(old_owner, "givenreset")
    with portal.test_request_context(environ_base={"REMOTE_ADDR": "192.0.2.9"}):
        inst = dict(db.one("SELECT * FROM instances WHERE id = ?", (wiki["id"],)))
        for _ in range(20):
            instances.reset_admin_password(inst, actor_id=old_owner["id"], charge=old_owner)
            _age_resets()
        with pytest.raises(instances.ServiceError):
            instances.reset_admin_password(inst, actor_id=old_owner["id"], charge=old_owner)
        # The transfer is accepted: the wiki's new owner can still lock the previous one out.
        db.execute("UPDATE instances SET account_id = ? WHERE id = ?", (new_owner["id"], wiki["id"]))
        inst = dict(db.one("SELECT * FROM instances WHERE id = ?", (wiki["id"],)))
        assert instances.reset_admin_password(inst, actor_id=new_owner["id"], charge=new_owner)
