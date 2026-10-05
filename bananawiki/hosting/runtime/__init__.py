"""The boundary between the hosting portal and everything that runs tenants.

The portal (this package, minus ``runtime/``) owns ``hosting.db``: accounts,
instance rows, policies, lifecycle state and every decision about who may do
what. It never touches Docker, the reverse proxy, TLS, tenant files or tenant
databases itself. Everything that does goes through a :class:`Runtime`.

Privilege split
---------------
The web process must not hold Docker privileges (1.4 put the portal in the
``docker`` group, so any portal bug was host root). Docker is reached only
through the root runtime agent installed by ``bananawiki/ops`` (contract:
``bananawiki/ops/RUNTIME_AGENT.md``; client:
``bananawiki.ops.agent_client.connect()``, whose ``call(op, args)`` offers
``tenant.start``, ``tenant.stop``, ``tenant.status``, ``tenant.list``,
``tenant.logs``, ``tenant.task``, ``proxy.routes``, ``image.status`` and
``ping``). The production implementation of :class:`Runtime` is therefore
a module of this package (:mod:`.agent`, selected by
``HOSTING_RUNTIME_BACKEND=agent``, the default) that runs in the portal and
maintenance processes as the service account: it creates and reads tenant
directories itself (they belong to the service account), maps
:class:`TenantPolicy` to the ``env``/``limits`` arguments of
``tenant.start`` and never runs ``docker`` directly. Archives and backups
stay file-level in that module until the agent protocol grows
``tenant.archive``/``tenant.restore``. Tenant databases are created,
migrated and edited only inside the tenant's sandbox (``tenant.task`` runs
:mod:`bananawiki.ops.tenant_task` there), never host-side.

Every argument and result below is plain data (strings, ints, bools, tuples
and the frozen dataclasses defined here, convertible with :func:`to_json` /
:func:`spec_from_json`), so an implementation may also forward calls to a
separate process. Implementations must re-validate ids, directory names and
user names and never trust paths.

Secrets (the tenant's OAuth client secret, the GPU TTS token, initial admin
passwords) travel inside these objects and must reach a tenant only through
an ``--env-file`` (mode 0600, removed after start) or a mounted secret file,
never on a command line, and must never be logged.

Conventions for every method
----------------------------
* Methods are synchronous but bounded: lifecycle calls return once the action
  has been *carried out and confirmed* by Docker (container created and
  started, or confirmed removed); they do not wait for the wiki to answer
  HTTP. Readiness is reported later by :meth:`Runtime.status`. The portal's
  web workers have a 180 s timeout, so an implementation must not block a
  call for more than about 60 s; archive import/export may take longer and
  run from admin pages only.
* Failures raise :class:`RuntimeFailure` with a short ``code`` from
  :data:`FAILURE_CODES`; the portal translates the code for the user and
  logs ``detail``. Any other exception is treated as ``"failed"``.
* The portal updates ``hosting.db`` itself, before or after calling the
  runtime as documented per method. The runtime must not write to
  ``hosting.db``, with one exception: the maintenance loop (see
  :mod:`bananawiki.hosting.maintenance`) calls portal functions that do.
* Data directories live at ``INSTANCES_DIR/<TenantSpec.data_dir_name>``:
  ``<slug>`` for hosting-mode wikis and ``<slug>__apex`` for apex wikis,
  exactly as in 1.4, because the 1.4 updater checks that path for every row
  of ``instances`` with ``status='running'``. Containers must carry the
  labels ``org.bananawiki.role=tenant``, ``org.bananawiki.data-dir=<realpath>``
  and ``org.bananawiki.internal-port=<HOSTING_CONTAINER_INTERNAL_PORT>``.
"""

from __future__ import annotations

import dataclasses
import importlib
import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

log = logging.getLogger("bananawiki.hosting.runtime")

FAILURE_CODES = frozenset({
    "unavailable",      # the runtime/agent cannot be reached or Docker is down
    "not_found",        # the tenant's data directory or container does not exist
    "data_exists",      # the target data directory already exists
    "start_failed",     # the container did not start
    "stop_failed",      # the container could not be confirmed stopped/removed
    "db_missing",       # the tenant has no bananawiki.db yet
    "db_unsafe",        # the tenant replaced its database or a file with a link/special file
    "user_not_found",   # no such user in the tenant database
    "protected_user",   # the user is an owner and may not be removed
    "invalid",          # an argument failed validation inside the agent
    "archive_invalid",  # an uploaded archive is not a BananaWiki archive, or unsafe
    "too_large",        # an archive or its extracted size exceeds the configured limits
    "no_space",         # not enough free disk space
    "timeout",          # the operation did not finish in time
    "not_configured",   # e.g. Google Drive backups without credentials
    "failed",           # anything else
})


class RuntimeFailure(Exception):
    """A runtime operation failed. ``code`` is one of :data:`FAILURE_CODES`."""

    def __init__(self, code: str, detail: str = ""):
        self.code = code if code in FAILURE_CODES else "failed"
        self.detail = detail
        super().__init__(f"{self.code}: {detail}" if detail else self.code)


# ── Data passed to the runtime ────────────────────────────────────────────────


@dataclass(frozen=True)
class OAuthClient:
    """Platform sign-in credentials handed to one wiki (1.4 ``BW_PLATFORM_OAUTH_*``).

    Maps to the tenant environment as ``BW_PLATFORM_OAUTH_ENABLED=1``,
    ``..._CLIENT_ID``, ``..._CLIENT_SECRET``, ``..._PORTAL_BASE``,
    ``..._AUTHORIZE_URL`` (``{portal_base}/oauth/authorize``), ``..._TOKEN_URL``
    (``/oauth/token``), ``..._USERINFO_URL`` (``/oauth/userinfo``),
    ``..._VERIFY_URL`` (``/oauth/verify``), ``..._LINK_URL`` (``/oauth/link``),
    ``..._UNLINK_URL`` (``/oauth/unlink``), ``..._LINK_STATUS_URL``
    (``/oauth/link-status``) and ``BW_PLATFORM_INSTANCE_ID``. Without an
    ``OAuthClient`` the tenant gets ``BW_PLATFORM_OAUTH_ENABLED=0``.
    """

    client_id: str
    client_secret: str = field(repr=False)
    portal_base: str
    instance_id: str

    def urls(self) -> dict[str, str]:
        base = self.portal_base.rstrip("/")
        return {name: f"{base}/oauth/{path}" for name, path in (
            ("authorize", "authorize"), ("token", "token"), ("userinfo", "userinfo"), ("verify", "verify"),
            ("link", "link"), ("unlink", "unlink"), ("link_status", "link-status"),
        )}


@dataclass(frozen=True)
class TtsGpu:
    """The shared GPU text-to-speech server (``BW_TTS_BACKEND=remote-gpu``,
    ``BW_TTS_REMOTE_GPU_URL``, ``BW_TTS_REMOTE_GPU_AUTH_TOKEN``,
    ``BW_TTS_REMOTE_GPU_TIMEOUT``). Only given to tenants the TTS policy allows."""

    url: str
    token: str = field(repr=False)
    timeout: int


@dataclass(frozen=True)
class TenantPolicy:
    """What the platform allows one wiki, decided by the portal from ``hosting.db``.

    The runtime turns this into the tenant's ``BW_*`` environment (always
    ``BW_MANAGED_HOSTING=1``, ``BW_PROXY_MODE=1``, paths under ``/data``,
    ``BW_MAINTENANCE_FILE=/data/.banana-maintenance``, a per-tenant
    ``BW_SESSION_COOKIE_NAME``; never the portal's own secrets) and into
    container limits. Field to variable mapping, as in 1.4:

    ==========================  ================================================
    ``easy_wiki``               ``BW_EASY_WIKI`` (1/0)
    ``forbid_public_mode``      ``BW_FORBID_PUBLIC_MODE``
    ``forbid_page_builder``     ``BW_FORBID_PAGE_BUILDER``
    ``forbid_public_builder``   ``BW_FORBID_PUBLIC_BUILDER_PAGES``
    ``storage_limit_bytes``     ``BW_STORAGE_LIMIT_BYTES`` (0 = unlimited); the
                                agent should also enforce it outside the tenant
                                (filesystem quota) where possible
    ``upload_max_bytes``        ``BW_MAX_ATTACHMENT_SIZE_BYTES``; the request
                                body limit ``BW_MAX_CONTENT_LENGTH_BYTES`` is
                                ``max(max_request_bytes, upload_max_bytes)``
    ``blocked_extensions``      ``BW_PLATFORM_UPLOAD_BLACKLIST`` (comma list),
                                also synced into the tenant DB upload settings
    ``expires_at``              ``BW_INSTANCE_EXPIRES_AT`` (omit when None)
    ``federation_enabled``      ``BW_FEDERATION_ENABLED``
    ``tts_disabled``            ``BW_MANAGED_TTS_DISABLED=1`` when True
    ``tts_gpu``                 see :class:`TtsGpu`
    ``oauth``                   see :class:`OAuthClient`
    ``plugin_denylist``         ``BW_MANAGED_PLUGIN_DENYLIST``
    ``global_tour``             seeds ``onboarding_required`` for the first admin
    ``memory_mb`` etc.          ``--memory``, ``--cpus``, ``--pids-limit``,
                                ``--ulimit nofile`` and ``BW_MEMORY_LIMIT_MB`` /
                                ``BW_NOFILE_LIMIT``
    ==========================  ================================================

    External plugins are enabled in the container unless the agent's own
    host-side plugin quarantine marker is set (``BW_ALLOW_EXTERNAL_PLUGINS``,
    ``BW_MANAGED_PLUGIN_QUARANTINE``); the quarantine is runtime state.
    """

    easy_wiki: bool = False
    forbid_public_mode: bool = True
    forbid_page_builder: bool = True
    forbid_public_builder: bool = True
    storage_limit_bytes: int = 0
    upload_max_bytes: int = 100 * 1024 * 1024
    max_request_bytes: int = 16 * 1024 * 1024
    blocked_extensions: tuple[str, ...] = ()
    expires_at: str | None = None
    federation_enabled: bool = False
    tts_disabled: bool = False
    tts_gpu: TtsGpu | None = None
    oauth: OAuthClient | None = None
    plugin_denylist: tuple[str, ...] = ()
    global_tour: bool = False
    memory_mb: int = 768
    cpu_limit: str = "1.0"
    pids_limit: int = 256
    nofile_limit: int = 1024


@dataclass(frozen=True)
class TenantSpec:
    """One wiki as the runtime needs to see it.

    ``slug`` is ``instances.subdomain`` (for a terminated wiki, the archived
    name ``<slug>--terminated-<id[:8]>``). ``data_dir_name`` is always
    ``slug`` or ``slug + "__apex"`` and is validated by the portal to match
    ``[a-z0-9-]+(__apex)?``. ``hostnames`` are the public hosts that route to
    this tenant (its platform host and any verified custom domain); it is
    empty in port mode, where ``port`` is published on the host instead.
    """

    instance_id: str
    slug: str
    domain_mode: str
    data_dir_name: str
    port: int | None
    url: str
    hostnames: tuple[str, ...]
    policy: TenantPolicy


@dataclass(frozen=True)
class TenantStatus:
    """``state`` is ``running`` (container up and HTTP healthy), ``starting``
    (container up, not answering yet), ``unhealthy`` (up but failing or
    crash-looping), ``stopped`` (no container) or ``missing`` (no data dir)."""

    state: str
    detail: str = ""

    @property
    def healthy(self) -> bool:
        return self.state == "running"


@dataclass(frozen=True)
class LogTail:
    content: str
    size_bytes: int
    truncated: bool


@dataclass(frozen=True)
class WikiUser:
    username: str
    role: str
    created_at: str


@dataclass(frozen=True)
class DomainCheck:
    """Result of the DNS checks for a custom domain claim.

    ``ownership`` is True when a TXT record at ``_bananawiki-challenge.<domain>``
    equals the claim's verification token. ``routing`` is True when the domain
    has a CNAME to ``HOSTING_CUSTOM_DOMAIN_TARGET`` or all its A/AAAA records
    are within ``HOSTING_CUSTOM_DOMAIN_IPS`` (or the target's own addresses).
    ``dns_error`` is set when resolution failed (records not propagated yet).
    ``proxied`` is set when routing was accepted because every address of the
    domain belongs to Cloudflare's proxy (``HOSTING_CUSTOM_DOMAIN_ALLOW_PROXIED``).
    """

    ownership: bool
    routing: bool
    dns_error: bool = False
    proxied: bool = False


@runtime_checkable
class Runtime(Protocol):
    """Operations on tenants. See the module docstring for the conventions."""

    # ── Lifecycle ────────────────────────────────────────────────────────

    def provision(self, spec: TenantSpec, *, admin_username: str, admin_password: str,
                  force_password_change: bool) -> None:
        """Create a new wiki and start it.

        Creates ``INSTANCES_DIR/<data_dir_name>`` (mode 0700, owned by the
        tenant's UID) with the 1.4 layout (``storage/<name>`` asset folders
        for uploads, attachments, chat_attachments, kanban_attachments and
        custom_page_files, plus relative ``<name> -> storage/<name>`` links),
        writes the tenant ``.secret_key`` once, creates and migrates the wiki
        database *inside the tenant image* (never host-side), creates the
        first administrator with *admin_username*/*admin_password* (role
        ``owner``, ``force_password_change`` as given, onboarding when
        ``policy.global_tour``), applies the upload policy to the tenant DB,
        then starts the container like :meth:`start`.

        The portal inserts the ``instances`` row (status ``running``) first
        and, when this raises, terminates the row and calls :meth:`destroy`
        (not after ``data_exists``: that directory was never this wiki's).
        The password must not be stored or logged anywhere.
        Raises ``data_exists`` when the directory is already there (never
        overwrite), ``start_failed``/``unavailable`` otherwise.
        1.4: ``instance_manager.provision_instance`` + ``instance_database._seed_instance_db``.
        """

    def start(self, spec: TenantSpec) -> None:
        """Make sure the tenant runs with exactly this spec.

        Idempotent: an up-to-date running container is left alone; a stopped,
        stale or differently configured one is (re)created with the current
        policy (so a policy change takes effect on the next ``start``). Clears
        stale state (PID files, ``.starting`` locks) from earlier crashes.
        Called after the portal sets the row to ``running``; on failure the
        portal sets the row back to ``stopped``.
        1.4: ``restart_instance`` / ``_start_process`` / ``container_runtime.start_container``.
        """

    def stop(self, spec: TenantSpec) -> None:
        """Stop and remove the tenant's container (and its private network).

        Idempotent: succeeds when nothing runs. Must only return once Docker
        confirms the container is gone (raise ``stop_failed`` otherwise). Data
        stays on disk. Used for pause, suspension, rename and before
        termination. 1.4: ``_stop_process`` / ``container_runtime.stop_container``.
        """

    def restart(self, spec: TenantSpec, *, force: bool = False) -> None:
        """Stop then start. With *force*, also discard every cached health and
        process state first (1.4 ``force_restart_instance``, the fix for a
        wiki stuck on "Starting up" after a host reboot)."""

    def recover(self, specs: Sequence[TenantSpec]) -> int:
        """Bring back every tenant the database says is running.

        Called by the maintenance service at start (the 1.4 updater removes
        all tenant containers and waits for them to come back, see audit C10)
        with every ``status='running'`` row. Starts the missing or unhealthy
        ones in bounded parallel waves and returns how many were started.
        Tenants whose data directory is missing are logged and skipped.
        1.4: ``recover_running_instances``.
        """

    def relocate(self, instance_id: str, old_dir_name: str, new_dir_name: str) -> None:
        """Move a stopped tenant's data directory within ``INSTANCES_DIR``.

        Used for renames, apex/hosting mode changes, termination with a grace
        period (``slug`` -> ``slug--terminated-<id8>``) and restores. Refuses
        (``data_exists``) when the target exists and differs from the source;
        a missing source raises ``not_found`` (checked first, so data an
        interrupted move already took away reads as gone). Must not follow
        links planted by the tenant. 1.4: ``_move_dir_safely``.
        """

    def destroy(self, spec: TenantSpec) -> None:
        """Delete the tenant's data directory and its host-owned state
        (``HOSTING_PLATFORM_STATE_DIR/<instance_id>``: plugin snapshots and the
        quarantine marker). Stops the container first. Idempotent. Used when a
        grace period ends, when an admin hard-deletes a terminated wiki and to
        clean up a failed provisioning. 1.4: ``purge_expired_grace_periods``,
        ``hard_delete_terminated_instance``."""

    # ── Observation (cheap, cached; safe to call on every dashboard view) ─

    def status(self, spec: TenantSpec) -> TenantStatus:
        """Current state of the tenant. Must answer in well under a second
        (cache health for ~10 s, negative results for ~3 s like 1.4)."""

    def upstream(self, spec: TenantSpec) -> tuple[str, int] | None:
        """``(address, port)`` where the running tenant answers HTTP, or None.

        The portal proxies a wiki's traffic itself only when a request for a
        wiki host reaches it anyway (the reverse proxy has no route for it
        yet, or still sends every host to the portal as 1.4 did)."""

    def usage(self, spec: TenantSpec) -> int:
        """Bytes used by the tenant's data directory (cached ~5 s; a bounded
        walk that does not follow links). 0 when the directory is missing."""

    def logs(self, spec: TenantSpec, name: str, max_bytes: int = 256 * 1024) -> LogTail:
        """Tail of ``error.log`` or ``access.log`` (any other *name* raises
        ``invalid``); opened without following links. A missing log is an
        empty tail. 1.4: ``read_instance_log``."""

    def analytics(self, spec: TenantSpec, days: int) -> dict[str, Any]:
        """Daily traffic roll-up read from the tenant DB (read-only):
        ``{"window_days": n, "totals": {"request": int, "page_view": int,
        "error": int}, "daily": [{"day": "YYYY-MM-DD", "request": int,
        "page_view": int, "error": int}, ...]}`` oldest first. Raises
        ``db_missing``/``db_unsafe``. 1.4: ``read_instance_analytics``."""

    # ── Tenant database operations (recovery tools) ──────────────────────

    def list_users(self, spec: TenantSpec, *, limit: int = 200, offset: int = 0) -> tuple[list[WikiUser], int]:
        """One page of the wiki's users (oldest first, fields capped at 200
        chars) and the total count. 1.4: ``list_instance_users_page``."""

    def set_user_password(self, spec: TenantSpec, username: str, password: str, role: str) -> None:
        """Create the user, or set its password and role; revokes the user's
        wiki API tokens when it existed. *role* is user/editor/admin/owner.
        1.4: ``set_instance_user_password``."""

    def remove_user(self, spec: TenantSpec, username: str) -> None:
        """Delete a wiki user; raises ``user_not_found`` or ``protected_user``
        (owners). 1.4: ``remove_instance_user``."""

    def reset_admin_password(self, spec: TenantSpec, admin_username: str, new_password: str) -> str:
        """Set *new_password* (with ``force_password_change=1``) on the user
        named *admin_username*; when it no longer exists, on every admin and
        owner. Revokes those users' wiki API tokens. Returns the user name the
        caller should show. Works whether the tenant runs or not.
        1.4: ``reset_instance_password``."""

    def reset_content(self, spec: TenantSpec, *, admin_username: str, admin_password: str) -> None:
        """Factory reset of a *stopped* tenant: delete the database and the
        asset folders (real folders only; links planted by the tenant are
        removed as links), then seed a fresh database as in :meth:`provision`.
        The plugin quarantine survives. 1.4: ``reset_wiki``."""

    def apply_limits(self, spec: TenantSpec) -> None:
        """Write the policy that lives inside the tenant DB (upload size and
        blocked extensions) and any agent-side quota. The portal restarts a
        running tenant afterwards so its environment matches.
        1.4: ``apply_upload_policy_to_instance``."""

    # ── Copies and archives ──────────────────────────────────────────────

    def export_archive(self, spec: TenantSpec, destination_dir: Path) -> Path:
        """Write the tenant's portable ZIP (1.4 ``build_instance_archive``
        format, importable by :meth:`import_archive` and by a self-hosted
        wiki) into *destination_dir* (created by the portal under
        ``HOSTING_EXPORT_TEMP_DIR``) and return its path. Consistent DB
        snapshot; links and special files skipped. The portal streams and
        deletes the file."""

    def import_archive(self, spec: TenantSpec, archive: Path) -> None:
        """Provision a new tenant whose data comes from *archive* (a ZIP
        uploaded by an admin), validate members (no traversal, links or
        oversized content per ``HOSTING_IMPORT_*``), migrate the database
        inside the tenant image, then start it. Raises ``archive_invalid``,
        ``too_large``, ``no_space``, and ``data_exists`` (without touching
        anything) when the directory is already there. On any other failure
        the portal terminates the row and calls :meth:`destroy`.
        1.4: ``provision_instance_from_archive``."""

    def duplicate(self, source: TenantSpec, target: TenantSpec) -> None:
        """Copy *source*'s database snapshot and asset folders into a new
        tenant *target*, then start it. Raises ``data_exists`` when
        *target*'s directory is already there, checked first and without
        touching anything (even when *source* is missing too), and
        ``not_found`` when *source*'s is missing. On any other failure the
        portal terminates the row and calls :meth:`destroy` on *target*.
        1.4: ``duplicate_instance``."""

    # ── Plugin safety (host-side state, out of the tenant's reach) ───────

    def plugins_quarantined(self, spec: TenantSpec) -> bool:
        """Whether the operator's plugin quarantine is in force."""

    def quarantine_plugins(self, spec: TenantSpec) -> str:
        """Kill switch: set the quarantine marker, disable every external
        plugin in the tenant DB and restart a running tenant. Returns a
        human-readable summary for the event log."""

    def lift_plugin_quarantine(self, spec: TenantSpec) -> str:
        """Clear the marker and restart a running tenant."""

    def list_plugin_snapshots(self, spec: TenantSpec) -> list[dict[str, Any]]:
        """Host-stored DB snapshots, newest first: ``{"name", "label",
        "created_at", "size_bytes"}``."""

    def capture_plugin_snapshot(self, spec: TenantSpec) -> str:
        """Store a trusted DB snapshot now; returns its name."""

    def restore_plugin_snapshot(self, spec: TenantSpec, name: str | None = None) -> str:
        """Roll the tenant DB back to snapshot *name* (newest when None) and
        restart a running tenant; returns a summary."""

    # ── Routing and custom domains ───────────────────────────────────────

    def check_domain(self, domain: str, token: str) -> DomainCheck:
        """DNS checks for a custom domain claim (see :class:`DomainCheck`).
        1.4: ``domains.verify_domain`` (dnspython, 4 s lifetime)."""

    def sync_routes(self, specs: Sequence[TenantSpec]) -> None:
        """Publish the complete routing table (hostname -> tenant) to the
        reverse proxy. The portal calls it after anything that changes
        routing (create, rename, stop/start, suspension, termination, domain
        verification); the maintenance loop calls it periodically. Only
        tenants that may serve are passed (see ``domains.may_serve``);
        requests for other hosts reach the portal, which shows a status
        page (or proxies the wiki itself, see :mod:`..wikihosts`).
        1.4: the in-portal ``SubdomainProxyMiddleware``, which 1.6 replaces
        with direct proxy routes (audit section 3)."""

    # ── Platform backups ─────────────────────────────────────────────────

    def export_platform(self, destination_dir: Path) -> Path:
        """Write an encrypted full backup (consistent ``hosting.db`` snapshot,
        the portal secret key, every tenant directory) into *destination_dir*
        and return the ``.bwenc`` file. Must never write a plaintext copy
        outside *destination_dir*. 1.4: ``backups.build_full_backup`` +
        ``backup_crypto.encrypt_backup``."""

    def restore_platform(self, archives: Sequence[Path]) -> None:
        """Replace the platform with the uploaded backup parts (``.zip`` or
        ``.bwenc``). Stops every tenant first. The portal restarts afterwards.
        1.4: ``db.restore_hosting_from_backup_zips``."""

    def backup_key(self) -> bytes:
        """The 32-byte key that decrypts platform backups
        (``HOSTING_BACKUP_ENCRYPTION_KEY`` or ``HOSTING_BACKUP_KEY_PATH``)."""

    def gdrive_test(self) -> str:
        """Check the Google Drive configuration; returns a summary, raises
        ``not_configured`` or ``failed``."""

    def gdrive_backup_now(self) -> str:
        """Upload an encrypted platform backup to Google Drive now."""

    def store_gdrive_credentials(self, content: bytes) -> str:
        """Store a service-account JSON (mode 0600) and return its path."""


def to_json(value: Any) -> str:
    """Serialise a spec or result for the agent channel."""
    return json.dumps(dataclasses.asdict(value) if dataclasses.is_dataclass(value) else value)


def spec_from_json(text: str) -> TenantSpec:
    raw = json.loads(text)
    policy = dict(raw["policy"])
    if policy.get("tts_gpu"):
        policy["tts_gpu"] = TtsGpu(**policy["tts_gpu"])
    if policy.get("oauth"):
        policy["oauth"] = OAuthClient(**policy["oauth"])
    for key in ("blocked_extensions", "plugin_denylist"):
        policy[key] = tuple(policy.get(key) or ())
    raw["policy"] = TenantPolicy(**policy)
    raw["hostnames"] = tuple(raw.get("hostnames") or ())
    return TenantSpec(**raw)


class UnavailableRuntime:
    """Used until a runtime backend is installed: observation answers
    "unknown", every operation raises ``unavailable``. Keeps the portal
    (accounts, settings, the dashboard) usable."""

    def __getattr__(self, name: str) -> Any:
        if name == "status":
            return lambda spec: TenantStatus("unknown", "no runtime backend")
        if name == "usage":
            return lambda spec: 0
        if name == "plugins_quarantined":
            return lambda spec: False
        if name in ("list_plugin_snapshots",):
            return lambda spec: []
        if name == "sync_routes":
            return lambda specs: None
        if name == "upstream":
            return lambda spec: None
        if name.startswith("_"):
            raise AttributeError(name)

        def unavailable(*_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeFailure("unavailable", "no hosting runtime backend is installed")

        return unavailable


def load_runtime(backend: str | None = None) -> Runtime:
    """Load ``bananawiki.hosting.runtime.<backend>.create_runtime()``.

    The backend name comes from ``HOSTING_RUNTIME_BACKEND`` (default
    ``agent``: the client that talks to the privileged agent). When the
    module does not exist yet, an :class:`UnavailableRuntime` is returned.
    """
    import os

    name = (backend or os.environ.get("HOSTING_RUNTIME_BACKEND") or "agent").strip()
    if not name.isidentifier():
        raise ValueError("HOSTING_RUNTIME_BACKEND must be a module name.")
    try:
        module = importlib.import_module(f"{__name__}.{name}")
    except ModuleNotFoundError as error:
        if error.name != f"{__name__}.{name}":
            raise
        log.warning("Hosting runtime backend %r is not installed; tenant operations are unavailable.", name)
        return UnavailableRuntime()  # type: ignore[return-value]
    return module.create_runtime()


__all__ = [
    "DomainCheck", "FAILURE_CODES", "LogTail", "OAuthClient", "Runtime", "RuntimeFailure", "TenantPolicy",
    "TenantSpec", "TenantStatus", "TtsGpu", "UnavailableRuntime", "WikiUser", "load_runtime",
    "spec_from_json", "to_json",
]

