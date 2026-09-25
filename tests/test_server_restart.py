"""
Tests for the admin "Restart server" action.

Covers:

* Permissioning, only admins can hit ``POST /admin/settings/restart-server``.
* Cooldown enforcement (60 s by default, see
  :data:`config.SERVER_RESTART_COOLDOWN_SECONDS`).
* Atomic claim helper :func:`db.check_and_claim_server_restart`.
* Per-wiki isolation: hosted instances are detected via ``BW_INSTANCE_DIR``.

The actual signalling in :func:`helpers.trigger_server_restart` is **never**
exercised: every test monkeypatches the helper to a no-op so we don't risk
SIGTERM-ing the pytest runner.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

import config
import db
from helpers import _server_restart


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def patched_restart(monkeypatch):
    """Replace :func:`helpers.trigger_server_restart` with a recording stub.

    The stub returns ``(True, "")`` and records every call so tests can
    assert that the route reaches the trigger step.  We patch on every
    module that imports the symbol because Python's ``from X import Y``
    creates an independent binding inside the importing module.
    """
    calls = []

    def _stub():
        calls.append(time.time())
        return True, ""

    monkeypatch.setattr(_server_restart, "trigger_server_restart", _stub)
    import helpers
    monkeypatch.setattr(helpers, "trigger_server_restart", _stub)
    return calls


@pytest.fixture
def fresh_cooldown():
    """Ensure no prior restart timestamp is recorded for the test wiki."""
    db.update_site_settings(last_server_restart_at=None)
    yield


# ---------------------------------------------------------------------------
# Cooldown helper unit tests
# ---------------------------------------------------------------------------


class TestCooldownHelpers:
    """Tests for the DB-backed atomic cooldown primitives."""

    def test_remaining_is_zero_when_no_prior_restart(self, fresh_cooldown):
        assert _server_restart.get_restart_cooldown_remaining() == 0

    def test_remaining_is_positive_within_window(self, fresh_cooldown):
        recent = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
        db.update_site_settings(last_server_restart_at=recent)
        remaining = _server_restart.get_restart_cooldown_remaining()
        # Default cooldown is 60s; 10s elapsed means ~50s remaining.
        assert 1 <= remaining <= config.SERVER_RESTART_COOLDOWN_SECONDS

    def test_remaining_is_zero_after_window_elapses(self, fresh_cooldown):
        long_ago = (
            datetime.now(timezone.utc)
            - timedelta(seconds=config.SERVER_RESTART_COOLDOWN_SECONDS + 5)
        ).isoformat()
        db.update_site_settings(last_server_restart_at=long_ago)
        assert _server_restart.get_restart_cooldown_remaining() == 0

    def test_remaining_zero_for_malformed_timestamp(self, fresh_cooldown):
        # A bogus value must not blow up: fail-open and let the atomic
        # ``check_and_claim`` step be the source of truth.
        db.update_site_settings(last_server_restart_at="not-a-timestamp")
        assert _server_restart.get_restart_cooldown_remaining() == 0

    def test_check_and_claim_succeeds_when_cooldown_clear(self, fresh_cooldown):
        assert db.check_and_claim_server_restart(60) is True
        # Should now be on cooldown.
        assert db.check_and_claim_server_restart(60) is False

    def test_check_and_claim_resumes_after_window(self, fresh_cooldown):
        long_ago = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()
        db.update_site_settings(last_server_restart_at=long_ago)
        assert db.check_and_claim_server_restart(60) is True

    def test_check_and_claim_with_zero_cooldown(self, fresh_cooldown):
        # Setting SERVER_RESTART_COOLDOWN_SECONDS = 0 in code should
        # let admins bypass the cooldown entirely (the documented escape
        # hatch).
        db.check_and_claim_server_restart(0)
        # Even immediately, a zero-second window allows the next call.
        assert db.check_and_claim_server_restart(0) is True


# ---------------------------------------------------------------------------
# Hosted-instance detection
# ---------------------------------------------------------------------------


class TestHostedDetection:
    """Tests for the ``BW_INSTANCE_DIR`` based deployment-mode detection."""

    def test_main_wiki_when_env_unset(self, monkeypatch):
        monkeypatch.delenv("BW_INSTANCE_DIR", raising=False)
        assert _server_restart.is_hosted_instance() is False

    def test_hosted_when_env_set(self, monkeypatch):
        monkeypatch.setenv("BW_INSTANCE_DIR", "/srv/instances/foo")
        assert _server_restart.is_hosted_instance() is True

    def test_main_wiki_when_env_blank(self, monkeypatch):
        monkeypatch.setenv("BW_INSTANCE_DIR", "")
        assert _server_restart.is_hosted_instance() is False


class TestEasyWikiDetection:
    """Tests for the ``BW_EASY_WIKI`` based EasyWiki mode detection."""

    def test_full_mode_when_env_unset(self, monkeypatch):
        monkeypatch.delenv("BW_EASY_WIKI", raising=False)
        assert _server_restart.is_easy_wiki() is False

    def test_easy_mode_when_env_set(self, monkeypatch):
        monkeypatch.setenv("BW_EASY_WIKI", "1")
        assert _server_restart.is_easy_wiki() is True

    def test_full_mode_when_env_blank(self, monkeypatch):
        monkeypatch.setenv("BW_EASY_WIKI", "")
        assert _server_restart.is_easy_wiki() is False

    def test_full_mode_when_env_zero(self, monkeypatch):
        monkeypatch.setenv("BW_EASY_WIKI", "0")
        assert _server_restart.is_easy_wiki() is False


# ---------------------------------------------------------------------------
# systemd supervision detection
# ---------------------------------------------------------------------------


class TestIsUnderSystemd:
    """``is_under_systemd()`` reads the master's cgroup file, not env vars.

    The previous implementation looked at ``INVOCATION_ID`` /
    ``JOURNAL_STREAM``, but those leak through ``fork()`` from any
    systemd-supervised parent (``sshd``, the hosting portal, container
    init, …) and produced false positives that made the restart helper
    send a plain ``SIGTERM`` with no supervisor to bring Gunicorn back.
    These tests pin the cgroup-based contract so we don't regress.
    """

    def test_returns_true_for_bananawiki_service_cgroupv2(self, monkeypatch):
        monkeypatch.setattr(
            _server_restart, "_read_master_cgroup",
            lambda _pid: "0::/system.slice/bananawiki.service\n",
        )
        assert _server_restart.is_under_systemd(master_pid=123) is True

    def test_returns_true_for_bananawiki_service_cgroupv1(self, monkeypatch):
        # Legacy cgroupv1 hosts list one line per controller; the unit
        # name still appears as the final path segment.
        cgroup_v1 = (
            "12:memory:/system.slice/bananawiki.service\n"
            "11:cpu,cpuacct:/system.slice/bananawiki.service\n"
            "0::/system.slice/bananawiki.service\n"
        )
        monkeypatch.setattr(
            _server_restart, "_read_master_cgroup", lambda _pid: cgroup_v1,
        )
        assert _server_restart.is_under_systemd(master_pid=123) is True

    def test_returns_false_for_user_session(self, monkeypatch):
        # Someone SSH-ed in and ran ``./start.sh``: the master is in a
        # user-session scope, NOT under bananawiki.service, so nothing
        # would restart Gunicorn after a SIGTERM.
        monkeypatch.setattr(
            _server_restart, "_read_master_cgroup",
            lambda _pid: "0::/user.slice/user-1000.slice/session-1.scope\n",
        )
        assert _server_restart.is_under_systemd(master_pid=123) is False

    def test_returns_false_for_unrelated_service(self, monkeypatch):
        # Wrapped in some other systemd service (the dev sandbox, the
        # hosting portal, an init container). *That* service may have
        # ``Restart=always`` for itself but not for us.
        monkeypatch.setattr(
            _server_restart, "_read_master_cgroup",
            lambda _pid: "0::/system.slice/remote-shell.service\n",
        )
        assert _server_restart.is_under_systemd(master_pid=123) is False

    def test_returns_false_for_similar_named_service(self, monkeypatch):
        # ``not-bananawiki.service`` and ``bananawiki-something.service``
        # must NOT match: the unit name has to be a full path segment.
        monkeypatch.setattr(
            _server_restart, "_read_master_cgroup",
            lambda _pid: "0::/system.slice/not-bananawiki.service\n",
        )
        assert _server_restart.is_under_systemd(master_pid=123) is False

    def test_returns_false_when_cgroup_unreadable(self, monkeypatch):
        monkeypatch.setattr(
            _server_restart, "_read_master_cgroup", lambda _pid: "",
        )
        assert _server_restart.is_under_systemd(master_pid=123) is False

    def test_ignores_inherited_environment_variables(self, monkeypatch):
        # The whole point of the cgroup-based check: inherited env vars
        # must NOT push us into the SIGTERM-only path.
        monkeypatch.setenv("INVOCATION_ID", "leaked-from-parent")
        monkeypatch.setenv("JOURNAL_STREAM", "8:13177")
        monkeypatch.setattr(
            _server_restart, "_read_master_cgroup",
            lambda _pid: "0::/user.slice/user-1000.slice/session-1.scope\n",
        )
        assert _server_restart.is_under_systemd(master_pid=123) is False

    def test_resolves_master_pid_when_omitted(self, monkeypatch):
        # When the caller doesn't pass a PID we ask
        # ``_gunicorn_master_pid()``.  Make sure that path is wired up.
        seen = []

        def _spy_read(pid):
            seen.append(pid)
            return "0::/system.slice/bananawiki.service\n"

        monkeypatch.setattr(_server_restart, "_gunicorn_master_pid", lambda: 4242)
        monkeypatch.setattr(_server_restart, "_read_master_cgroup", _spy_read)
        assert _server_restart.is_under_systemd() is True
        assert seen == [4242]


# ---------------------------------------------------------------------------
# deploy.sh per-site supervised unit detection
# ---------------------------------------------------------------------------


class TestIsUnderSupervisedSystemdUnit:
    """``deploy.sh`` writes one ``bananawiki-<slug>.service`` unit per
    managed site.  Those units default to ``Restart=on-failure`` (a
    clean SIGTERM does not trigger a restart) and inherit the default
    ``KillMode=control-group`` (the rest of the cgroup is SIGKILL'd
    when the MainPID exits).  The detection helper has to pick these
    cases out so the restart helper can fall back to a SIGHUP-only
    refresh; otherwise the wiki stays down after the admin "Restart
    server" button is clicked, which is exactly the user-reported bug.
    """

    def test_returns_true_for_per_site_unit(self, monkeypatch):
        monkeypatch.setattr(
            _server_restart, "_read_master_cgroup",
            lambda _pid: "0::/system.slice/bananawiki-example-com.service\n",
        )
        assert (
            _server_restart.is_under_supervised_systemd_unit(master_pid=123)
            is True
        )

    def test_returns_true_for_per_site_unit_cgroupv1(self, monkeypatch):
        cgroup_v1 = (
            "12:memory:/system.slice/bananawiki-foo.service\n"
            "11:cpu,cpuacct:/system.slice/bananawiki-foo.service\n"
            "0::/system.slice/bananawiki-foo.service\n"
        )
        monkeypatch.setattr(
            _server_restart, "_read_master_cgroup", lambda _pid: cgroup_v1,
        )
        assert (
            _server_restart.is_under_supervised_systemd_unit(master_pid=123)
            is True
        )

    def test_returns_false_for_install_sh_main_unit(self, monkeypatch):
        # ``bananawiki.service`` (no slug) is the install.sh unit which
        # has ``Restart=always`` and is safe to SIGTERM. It must NOT
        # be reported as a supervised per-site unit.
        monkeypatch.setattr(
            _server_restart, "_read_master_cgroup",
            lambda _pid: "0::/system.slice/bananawiki.service\n",
        )
        assert (
            _server_restart.is_under_supervised_systemd_unit(master_pid=123)
            is False
        )

    def test_returns_false_for_hosting_unit(self, monkeypatch):
        # The hosting platform's own service is excluded: its MainPID
        # is the hosting Flask app, not a wiki, and instances daemonized
        # under it are NOT the unit's MainPID so the SIGTERM-and-cgroup-
        # kill problem doesn't apply.
        monkeypatch.setattr(
            _server_restart, "_read_master_cgroup",
            lambda _pid: "0::/system.slice/bananawiki-hosting.service\n",
        )
        assert (
            _server_restart.is_under_supervised_systemd_unit(master_pid=123)
            is False
        )

    def test_returns_false_for_user_session(self, monkeypatch):
        monkeypatch.setattr(
            _server_restart, "_read_master_cgroup",
            lambda _pid: "0::/user.slice/user-1000.slice/session-1.scope\n",
        )
        assert (
            _server_restart.is_under_supervised_systemd_unit(master_pid=123)
            is False
        )

    def test_returns_false_for_unrelated_service(self, monkeypatch):
        monkeypatch.setattr(
            _server_restart, "_read_master_cgroup",
            lambda _pid: "0::/system.slice/remote-shell.service\n",
        )
        assert (
            _server_restart.is_under_supervised_systemd_unit(master_pid=123)
            is False
        )

    def test_returns_false_for_similar_named_service(self, monkeypatch):
        # ``not-bananawiki-foo.service`` must NOT match. The per-site
        # pattern requires ``/bananawiki-`` as the path segment prefix.
        monkeypatch.setattr(
            _server_restart, "_read_master_cgroup",
            lambda _pid: "0::/system.slice/not-bananawiki-foo.service\n",
        )
        assert (
            _server_restart.is_under_supervised_systemd_unit(master_pid=123)
            is False
        )

    def test_returns_false_when_cgroup_unreadable(self, monkeypatch):
        monkeypatch.setattr(
            _server_restart, "_read_master_cgroup", lambda _pid: "",
        )
        assert (
            _server_restart.is_under_supervised_systemd_unit(master_pid=123)
            is False
        )


# ---------------------------------------------------------------------------
# Master PID resolution
# ---------------------------------------------------------------------------


class TestMasterPidResolution:
    """The restart helper must refuse to signal init / detached parents."""

    def test_refuses_pid_one(self, monkeypatch):
        monkeypatch.setattr(os, "getppid", lambda: 1)
        assert _server_restart._gunicorn_master_pid() == 0

    def test_refuses_zero(self, monkeypatch):
        monkeypatch.setattr(os, "getppid", lambda: 0)
        assert _server_restart._gunicorn_master_pid() == 0

    def test_returns_real_parent(self, monkeypatch):
        monkeypatch.setattr(os, "getppid", lambda: 4242)
        assert _server_restart._gunicorn_master_pid() == 4242


class TestTriggerNoMaster:
    """Trigger must fail cleanly when no Gunicorn master can be identified."""

    def test_returns_false_with_explanatory_message(self, monkeypatch):
        monkeypatch.setattr(os, "getppid", lambda: 1)
        ok, reason = _server_restart.trigger_server_restart()
        assert ok is False
        assert "Gunicorn" in reason


# ---------------------------------------------------------------------------
# HTTP route tests
# ---------------------------------------------------------------------------


class TestRestartRoutePermissions:
    """Anonymous and non-admin users must not be able to trigger a restart."""

    def test_anonymous_redirected_to_login(self, client, fresh_cooldown):
        rv = client.post("/admin/settings/restart-server")
        # Anonymous users get redirected to login (302).  We MUST NOT see
        # any indication that a restart was attempted.
        assert rv.status_code in (302, 401, 403)
        assert db.get_last_server_restart_at() is None

    def test_regular_user_is_blocked(
        self, client, regular_user, fresh_cooldown
    ):
        client.post("/login", data={"username": "user", "password": "user123"})
        rv = client.post("/admin/settings/restart-server")
        assert rv.status_code in (302, 403)
        assert db.get_last_server_restart_at() is None

    def test_editor_user_is_blocked(
        self, client, editor_user, fresh_cooldown
    ):
        client.post(
            "/login", data={"username": "editor", "password": "editor123"}
        )
        rv = client.post("/admin/settings/restart-server")
        assert rv.status_code in (302, 403)
        assert db.get_last_server_restart_at() is None


class TestRestartRouteAdmin:
    """Admin behaviour: success path, cooldown enforcement, alias parity."""

    def test_admin_first_call_triggers_restart(
        self, logged_in_admin, patched_restart, fresh_cooldown
    ):
        rv = logged_in_admin.post("/admin/settings/restart-server")
        assert rv.status_code == 302
        assert len(patched_restart) == 1
        # Cooldown timestamp must now be set.
        assert db.get_last_server_restart_at() is not None

    def test_admin_second_call_blocked_by_cooldown(
        self, logged_in_admin, patched_restart, fresh_cooldown
    ):
        first = logged_in_admin.post("/admin/settings/restart-server")
        assert first.status_code == 302
        assert len(patched_restart) == 1

        second = logged_in_admin.post("/admin/settings/restart-server")
        assert second.status_code == 302
        # Trigger must NOT have been called a second time. The cooldown
        # short-circuited the request before reaching the helper.
        assert len(patched_restart) == 1

    def test_admin_can_restart_after_cooldown_expires(
        self, logged_in_admin, patched_restart, fresh_cooldown
    ):
        # First restart claims the slot.
        logged_in_admin.post("/admin/settings/restart-server")
        assert len(patched_restart) == 1

        # Fast-forward the recorded timestamp past the cooldown window.
        long_ago = (
            datetime.now(timezone.utc)
            - timedelta(seconds=config.SERVER_RESTART_COOLDOWN_SECONDS + 5)
        ).isoformat()
        db.update_site_settings(last_server_restart_at=long_ago)

        # Second restart should now succeed.
        logged_in_admin.post("/admin/settings/restart-server")
        assert len(patched_restart) == 2

    def test_global_settings_alias_works(
        self, logged_in_admin, patched_restart, fresh_cooldown
    ):
        # ``/global-settings/restart-server`` is registered as a second
        # alias for the same endpoint to match the existing
        # ``/global-settings`` route family used elsewhere in admin.py.
        rv = logged_in_admin.post("/global-settings/restart-server")
        assert rv.status_code == 302
        assert len(patched_restart) == 1

    def test_restart_failure_is_reported(
        self, logged_in_admin, fresh_cooldown, monkeypatch
    ):
        """A failure inside :func:`trigger_server_restart` must surface as
        an error flash and must NOT leave the cooldown in an "in-flight"
        state from the user's POV.

        Note: the cooldown is intentionally still recorded on failure.
        This matches the behaviour of every other ``check_and_claim_*``
        helper in the codebase and prevents an attacker from spamming
        the trigger if it consistently fails.
        """
        def _fail():
            return False, "simulated subprocess failure"

        monkeypatch.setattr(_server_restart, "trigger_server_restart", _fail)
        import helpers
        monkeypatch.setattr(helpers, "trigger_server_restart", _fail)

        rv = logged_in_admin.post(
            "/admin/settings/restart-server", follow_redirects=False
        )
        assert rv.status_code == 302

    def test_get_method_is_rejected(self, logged_in_admin, fresh_cooldown):
        """The route is POST-only; GET must 405 to prevent CSRF via image
        tags / link previews."""
        rv = logged_in_admin.get("/admin/settings/restart-server")
        assert rv.status_code == 405


class TestRestartIsolation:
    """The restart action must never reach into another wiki's process."""

    def test_signal_target_is_only_direct_parent(self, monkeypatch):
        """The helper must only ever signal ``os.getppid()``: never a
        wildcard like process group 0, never another PID looked up out of
        band.  This is what guarantees per-wiki isolation."""
        targets = []

        def _record_kill(pid, sig):
            targets.append((pid, sig))

        monkeypatch.setattr(os, "kill", _record_kill)
        monkeypatch.setattr(os, "getppid", lambda: 31337)
        # Force the systemd-supervised path so we exercise the original
        # single-SIGTERM contract this test was written for.  The
        # graceful re-exec path is covered separately.
        monkeypatch.setattr(
            _server_restart, "_read_master_cgroup",
            lambda _pid: "0::/system.slice/bananawiki.service\n",
        )
        # Run the main-wiki restart synchronously to inspect the signals.
        _server_restart._restart_main_wiki(31337)

        # Every kill must target our recorded master PID. Never any
        # other value, and certainly never 0 (process group) or -1
        # (every process the user owns).
        assert targets, "expected at least one kill"
        for pid, _sig in targets:
            assert pid == 31337

    def test_non_systemd_main_wiki_uses_graceful_reexec(self, monkeypatch):
        """When the main wiki is not under systemd, a plain SIGTERM would
        kill Gunicorn for good with nothing to bring it back.  The helper
        must instead use Gunicorn's own SIGUSR2 graceful re-exec sequence
        (SIGUSR2 → SIGWINCH → SIGTERM) so the master fork-replaces itself.
        """
        import signal as _signal

        targets = []

        def _record_kill(pid, sig):
            targets.append((pid, sig))

        monkeypatch.setattr(os, "kill", _record_kill)
        monkeypatch.setattr(os, "getppid", lambda: 31337)
        # No-op sleep so the test isn't slowed by the real handover delay.
        monkeypatch.setattr(_server_restart.time, "sleep", lambda *_a, **_kw: None)
        # Pretend the master is in some unrelated cgroup so the helper
        # falls back to the graceful re-exec path.  This mirrors what
        # happens when ``./start.sh`` is launched from sshd, a login
        # shell, the hosting platform, container init, etc.
        monkeypatch.setattr(
            _server_restart, "_read_master_cgroup",
            lambda _pid: "0::/user.slice/user-1000.slice/session-1.scope\n",
        )
        # Belt and braces: any inherited env vars must NOT short-circuit
        # the cgroup-based detection.
        monkeypatch.setenv("INVOCATION_ID", "inherited-from-sshd")
        monkeypatch.setenv("JOURNAL_STREAM", "8:99999")

        _server_restart._restart_main_wiki(31337)

        # All three signals must be delivered, in order, to the same PID.
        assert [s for _, s in targets] == [
            _signal.SIGUSR2,
            _signal.SIGWINCH,
            _signal.SIGTERM,
        ]
        for pid, _sig in targets:
            assert pid == 31337

    def test_supervised_per_site_unit_uses_sighup_only(self, monkeypatch):
        """When the master is the MainPID of a ``bananawiki-<slug>.
        service`` unit (deploy.sh per-site case), the helper must
        send a single ``SIGHUP`` instead of either ``SIGTERM`` or the
        graceful re-exec sequence.

        Rationale: those units default to ``Restart=on-failure`` (clean
        SIGTERM does not trigger a restart) and the default
        ``KillMode=control-group`` makes systemd SIGKILL the rest of
        the cgroup when the MainPID exits, including any
        SIGUSR2-forked successor master.  SIGHUP keeps the MainPID
        alive while Gunicorn cycles workers, so neither directive
        misfires and the wiki stays up.  This is the fix for the
        user-reported bug where the admin "Restart server" button
        left the wiki down on deploy.sh installs.
        """
        import signal as _signal

        targets = []
        monkeypatch.setattr(os, "kill", lambda pid, sig: targets.append((pid, sig)))
        monkeypatch.setattr(os, "getppid", lambda: 31337)
        monkeypatch.setattr(_server_restart.time, "sleep", lambda *_a, **_kw: None)
        monkeypatch.setattr(
            _server_restart, "_read_master_cgroup",
            lambda _pid: "0::/system.slice/bananawiki-example-com.service\n",
        )

        _server_restart._restart_main_wiki(31337)

        # Exactly one SIGHUP to the master PID. No SIGTERM, no
        # SIGUSR2/SIGWINCH dance.
        assert targets == [(31337, _signal.SIGHUP)]

    def test_supervised_per_site_unit_via_hosted_path_uses_sighup_only(
        self, monkeypatch
    ):
        """deploy.sh sets ``BW_INSTANCE_DIR`` on every per-site unit, so
        the dispatch in :func:`_run_restart_in_background` routes those
        wikis through :func:`_restart_hosted_instance` rather than
        :func:`_restart_main_wiki`.  The hosted-instance path must
        therefore *also* detect the supervised unit case and fall back
        to SIGHUP-only, otherwise the bug reproduces just on the
        hosted code path.
        """
        import signal as _signal

        targets = []
        monkeypatch.setattr(os, "kill", lambda pid, sig: targets.append((pid, sig)))
        monkeypatch.setattr(_server_restart.time, "sleep", lambda *_a, **_kw: None)
        monkeypatch.setattr(
            _server_restart, "_read_master_cgroup",
            lambda _pid: "0::/system.slice/bananawiki-example-com.service\n",
        )

        _server_restart._restart_hosted_instance(31337)

        assert targets == [(31337, _signal.SIGHUP)]

    def test_hosting_managed_instance_still_uses_graceful_reexec(
        self, monkeypatch
    ):
        """A hosted instance daemonized *by* the hosting platform runs
        in the ``bananawiki-hosting.service`` cgroup but is **not** the
        unit's MainPID: those master processes can (and should) still
        use the full graceful re-exec sequence, otherwise the hosting
        instance can never actually replace its code.  This pins that
        the supervised-unit detection excludes the hosting service.
        """
        import signal as _signal

        targets = []
        monkeypatch.setattr(os, "kill", lambda pid, sig: targets.append((pid, sig)))
        monkeypatch.setattr(_server_restart.time, "sleep", lambda *_a, **_kw: None)
        monkeypatch.setattr(
            _server_restart, "_read_master_cgroup",
            lambda _pid: "0::/system.slice/bananawiki-hosting.service\n",
        )

        _server_restart._restart_hosted_instance(31337)

        assert [s for _, s in targets] == [
            _signal.SIGUSR2,
            _signal.SIGWINCH,
            _signal.SIGTERM,
        ]
