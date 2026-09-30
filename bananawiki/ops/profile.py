"""What a deployment mode runs: services, environment defaults, health URLs.

The service command lines are a compatibility contract: every release must
provide ``gunicorn.conf.py`` + ``wsgi:app`` and ``scripts/tts_worker.py``
(wiki) or ``hosting/gunicorn.conf.py`` + ``hosting.wsgi:app`` and
``python -m hosting.maintenance`` (hosting) at its root, because the 1.4
updater writes exactly these commands when it installs a newer release.
"""

from __future__ import annotations

import os
import re
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from . import DEFAULT_SOURCE_URL, PRODUCT

MODES = ("wiki", "hosting")
DEFAULT_PORTS = {"wiki": 5001, "hosting": 5099}
SERVICE_NAME = re.compile(r"[a-z][a-z0-9-]{1,45}")
HOSTING_STORAGE_NAMES = ("uploads", "attachments", "chat_attachments", "kanban_attachments", "custom_page_files")
TENANT_IMAGE = "bananawiki-tenant"


@dataclass(frozen=True)
class ReleaseFeatures:
    """What a release tree supports; decides how its units are written.

    ``runtime_agent``: the release ships :mod:`bananawiki.ops.runtime_agent`,
    so hosting mode gets the privileged ``<name>-agent`` helper and the portal
    loses Docker access. ``hardened``: the release was written for the 1.6
    sandbox (1.4 releases get the exact units 1.4 wrote, so a rollback runs
    them the way they were tested).
    """

    runtime_agent: bool = False
    hardened: bool = False

    @classmethod
    def of(cls, release: Path) -> ReleaseFeatures:
        ours = (release / "bananawiki" / "ops" / "runtime_agent.py").is_file()
        return cls(runtime_agent=ours, hardened=ours)


@dataclass(frozen=True)
class Service:
    name: str
    command: list[str]
    description: str
    privileged: bool = False  # runs as root (the runtime agent)
    after: tuple[str, ...] = field(default_factory=tuple)


def modes() -> tuple[str, ...]:
    return MODES


def validate_service_name(name: str) -> str:
    if not isinstance(name, str) or not SERVICE_NAME.fullmatch(name):
        raise ValueError("Use a lowercase service name with letters, digits and hyphens (2-46 characters).")
    return name


def validate_domain(domain: str | None) -> str:
    if not domain:
        return ""
    domain = domain.encode("idna").decode().lower().rstrip(".")
    labels = domain.split(".")
    if len(domain) > 253 or len(labels) < 2 or not all(
        re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in labels
    ):
        raise ValueError("Use a DNS hostname without a scheme, port or path.")
    return domain


def validate_port(port: int) -> int:
    port = int(port)
    if not 1024 <= port <= 65535:
        raise ValueError("Choose an application port between 1024 and 65535.")
    return port


def source_link(url: str) -> str:
    """The public web page for a Git URL (AGPL ``/source`` link); never includes credentials."""
    if url.startswith("git@") and ":" in url:
        host, path = url[4:].split(":", 1)
        return "https://" + host + "/" + path.removesuffix(".git")
    if url.startswith("ssh://"):
        parsed = urlsplit(url)
        return "https://" + (parsed.hostname or "") + parsed.path.removesuffix(".git")
    if url.startswith("https://"):
        return url.removesuffix(".git")
    return DEFAULT_SOURCE_URL.removesuffix(".git")


def hosting_storage_link(path: Path, data: Path) -> bool:
    """Recognise only the relative ``instances/<t>/<name> -> storage/<name>`` aliases hosting creates."""
    try:
        relative = path.relative_to(data)
    except ValueError:
        return False
    if len(relative.parts) != 3 or relative.parts[0] != "instances" or path.name not in HOSTING_STORAGE_NAMES:
        return False
    if not path.is_symlink() or os.readlink(path) != "storage/" + path.name:
        return False
    tenant = path.parent
    return all(folder.is_dir() and not folder.is_symlink()
               for folder in (data, data / "instances", tenant, tenant / "storage", tenant / "storage" / path.name))


def inside_tenant(path: Path, data: Path) -> bool:
    """An entry inside a hosted wiki's directory (``instances/<tenant>/…``).

    The tenant's container (and the plugins it runs) can create any link or
    special file there. The controller runs as root, so it never follows or
    packages such entries, and one tenant's stray link must not block every
    backup and update of the platform: they are skipped.
    """
    try:
        relative = path.relative_to(data)
    except ValueError:
        return False
    return len(relative.parts) >= 3 and relative.parts[0] == "instances"


_DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def _mkdir_at(directory: int, name: str) -> int | None:
    """Create *name* (0700) in *directory* if missing and open it, never through a link (None otherwise)."""
    try:
        os.mkdir(name, 0o700, dir_fd=directory)
    except FileExistsError:
        pass
    try:
        return os.open(name, _DIRECTORY, dir_fd=directory)
    except OSError:
        return None


def prepare_hosting_storage(data: Path) -> None:
    """Recreate the storage aliases after restoring a (regular-files-only) package.

    Runs as root on directories tenants write to, so every step works on
    directory descriptors and never follows a link: a tenant that replaced
    ``storage`` (or one of its folders) with a link is left as it is (the
    portal repairs the layout when the wiki starts) instead of making root
    create directories wherever the link points.
    """
    instances = data / "instances"
    if instances.is_symlink():
        raise ValueError("The managed instances directory must not be a symlink.")
    instances.mkdir(mode=0o700, parents=True, exist_ok=True)
    for tenant in instances.iterdir():
        if tenant.is_symlink():
            raise ValueError("Managed tenant directories must not be symlinks.")
        if not tenant.is_dir():
            continue
        tenant_fd = os.open(tenant, _DIRECTORY)
        try:
            storage_fd = _mkdir_at(tenant_fd, "storage")
            if storage_fd is None:
                continue
            try:
                for name in HOSTING_STORAGE_NAMES:
                    folder = _mkdir_at(storage_fd, name)
                    if folder is not None:
                        os.close(folder)
                    try:
                        os.stat(name, dir_fd=tenant_fd, follow_symlinks=False)
                    except FileNotFoundError:
                        os.symlink("storage/" + name, name, target_is_directory=True, dir_fd=tenant_fd)
            finally:
                os.close(storage_fd)
        finally:
            os.close(tenant_fd)


def wiki_paths(data: Path) -> dict[str, str]:
    folders = {
        "BW_UPLOAD_FOLDER": "uploads", "BW_FAVICON_UPLOAD_FOLDER": "favicons", "BW_ATTACHMENT_FOLDER": "attachments",
        "BW_CHAT_ATTACHMENT_FOLDER": "chat_attachments", "BW_KANBAN_ATTACHMENT_FOLDER": "kanban_attachments",
        "BW_CUSTOM_PAGE_FILES_FOLDER": "custom_page_files", "BW_TTS_FOLDER": "tts",
        "BW_EXTERNAL_PLUGINS_DIR": "plugins", "BW_SITE_EXPORT_TEMP_DIR": "tmp_exports",
    }
    return {
        "BW_INSTANCE_DIR": str(data), "BW_DATABASE_PATH": str(data / "bananawiki.db"),
        "BW_LOG_FILE": str(data / "logs" / "bananawiki.log"),
        **{key: str(data / folder) for key, folder in folders.items()},
    }


def agent_socket(settings: dict[str, Any]) -> str:
    return f"/run/{settings['service']}-agent/agent.sock"


def environment(settings: dict[str, Any], previous: dict[str, str] | None = None,
                features: ReleaseFeatures | None = None) -> dict[str, str]:
    """Merge defaults into the operator's ``app.env``; existing values always win.

    Exceptions (values the controller owns): the maintenance file, bytecode
    suppression, the tenant image tag and the runtime-agent socket.
    """
    root, mode = Path(settings["root"]), settings["mode"]
    data = root / "data"
    domain = settings.get("domain") or ""
    values = dict(previous or {})
    values.update(BANANA_MAINTENANCE_FILE=str(data / ".banana-maintenance"), PYTHONDONTWRITEBYTECODE="1")
    if mode == "wiki":
        defaults = {
            "BW_HOST": "127.0.0.1", "BW_PORT": str(settings["port"]), "BW_PROXY_MODE": "1" if domain else "0",
            "BW_PREFERRED_URL_SCHEME": "https" if domain else "http", "BW_ENV": "production",
            "BW_SETUP_TOKEN": secrets.token_hex(32), "BW_SOURCE_URL": source_link(settings["source_url"]),
            "BW_SYSTEMD_SERVICE": settings["service"] + ".service", **wiki_paths(data),
        }
    else:
        defaults = {
            "HOSTING_HOST": "127.0.0.1", "HOSTING_PORT": str(settings["port"]),
            "HOSTING_PROXY_MODE": "1" if domain else "0", "HOSTING_PUBLIC_SCHEME": "https" if domain else "http",
            "HOSTING_MODE": "subdomain" if domain else "port", "HOSTING_PUBLIC_HOST": "127.0.0.1",
            "HOSTING_DATABASE_PATH": str(data / "hosting.db"), "HOSTING_SECRET_KEY_PATH": str(data / ".secret_key"),
            "HOSTING_BACKUP_KEY_PATH": str(data / ".backup_encryption_key"), "INSTANCES_DIR": str(data / "instances"),
            "STATIC_SITE_DIR": str(root / "site"), "HOSTING_IMPORT_TEMP_DIR": str(data / "imports"),
            "HOSTING_EXPORT_TEMP_DIR": str(data / "exports"), "HOSTING_BOOTSTRAP_TOKEN": secrets.token_hex(32),
            "BASE_DOMAIN": domain, "PORTAL_DOMAIN": settings.get("portal_domain", ""),
            "INSTANCE_URL_SUFFIX": "hosting", "HOSTING_INSTANCE_RUNTIME": "docker",
            "BW_SOURCE_URL": source_link(settings["source_url"]),
            # Portal helpers import the wiki configuration too; keep its files out of the release tree.
            **wiki_paths(data / "wiki-runtime"),
        }
        values["HOSTING_CONTAINER_IMAGE"] = f"{TENANT_IMAGE}:{settings['revision']}"
        if features is not None and features.runtime_agent:
            values["BW_RUNTIME_AGENT_SOCKET"] = agent_socket(settings)
        else:
            values.pop("BW_RUNTIME_AGENT_SOCKET", None)
    for key, value in defaults.items():
        values.setdefault(key, value)
    return values


def services(settings: dict[str, Any], features: ReleaseFeatures | None = None) -> list[Service]:
    """Services in start order (stop in reverse)."""
    root, name = Path(settings["root"]), settings["service"]
    current = root / "current"
    python, gunicorn = str(current / ".venv/bin/python"), str(current / ".venv/bin/gunicorn")
    if settings["mode"] == "wiki":
        return [
            Service(name, [gunicorn, "-c", "gunicorn.conf.py", "wsgi:app"], "BananaWiki"),
            Service(name + "-tts", [python, "scripts/tts_worker.py"], "BananaWiki text-to-speech worker"),
        ]
    output = []
    after: tuple[str, ...] = ()
    if features is not None and features.runtime_agent:
        agent = Service(
            name + "-agent",
            ["/usr/bin/python3", "-E", "-s", str(current / "banana"), "--root", str(root), "agent", "serve"],
            "BananaWiki hosting runtime agent",
            privileged=True,
        )
        output.append(agent)
        after = (agent.name + ".service",)
    output += [
        Service(name, [gunicorn, "-c", "hosting/gunicorn.conf.py", "--timeout", "180", "hosting.wsgi:app"],
                "BananaWiki hosting", after=after),
        Service(name + "-maintenance", [python, "-m", "hosting.maintenance", "--interval", "300"],
                "BananaWiki hosting maintenance", after=after),
    ]
    return output


def service_names(settings: dict[str, Any], features: ReleaseFeatures | None = None) -> list[str]:
    return [service.name for service in services(settings, features)]


def health_urls(settings: dict[str, Any]) -> list[str]:
    return [f"http://127.0.0.1:{int(settings['port'])}/health"]


def new_settings(root: Path, *, mode: str, service: str, domain: str, portal_domain: str, port: int,
                 source_url: str) -> dict[str, Any]:
    """``installation.json`` (schema 1; BananaChat-only keys kept empty for compatibility)."""
    return {"schema": 1, "product": PRODUCT, "mode": mode, "root": str(root), "service": service,
            "domain": domain, "portal_domain": portal_domain, "port": port, "backend_url": "",
            "ollama_binary": "", "source_url": source_url, "installed": False, "revision": ""}
