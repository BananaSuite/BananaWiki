"""BananaWiki Easy Deployment App.

This module has two modes:

* GUI mode, used by non-technical users to start/stop a local BananaWiki.
* Server-child mode, used by the GUI subprocess to serve the wiki.

The server-child mode intentionally launches a standalone BananaWiki instance,
not the multi-tenant hosting platform. All mutable paths are redirected into a
portable "BananaWiki Files" folder next to the executable.
"""

from __future__ import annotations

import argparse
import ipaddress
import os
import platform
import re
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable


if not __package__ and not getattr(sys, "frozen", False):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from easy_deployment_app.data import (
    ensure_data_layout, wiki_database_path, create_data_archive,
    extract_data_archive, clear_all_data, data_lock,
)


APP_TITLE = "BananaWiki Easy Deployment"
DATA_DIR_NAME = "bananawiki"
DEFAULT_PORT = 80
LOCAL_HOST = "127.0.0.1"
LAN_BIND_HOST = "0.0.0.0"
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
UI_BG = "#1a1b2e"
UI_CARD = "#252740"
UI_TEXT = "#ffffff"
UI_MUTED = "#9ca3af"
UI_BORDER = "#374151"
UI_ACCENT = "#f4c542"
UI_ACCENT_ACTIVE = "#e6b72f"
UI_DANGER = "#ef4444"
LANGUAGE_CHOICES = {
    "en": {"flag": "\U0001F1EC\U0001F1E7", "name": "English"},
    "it": {"flag": "\U0001F1EE\U0001F1F9", "name": "Italiano"},
}
_FLAG_CACHE: dict[str, object] = {}





def _generate_flag_image(code: str, size: tuple[int, int] = (36, 24)) -> object:
    """Generate a small flag image for tkinter display.

    Pillow draws simple but recognisable flags so we don't need to
    rely on emoji rendering (which fails in tkinter on many platforms).
    """
    from PIL import Image, ImageDraw, ImageTk

    w, h = size
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    if code == "it":
        draw.rectangle([0, 0, w // 3 - 1, h], fill=(0, 146, 70))
        draw.rectangle([w // 3, 0, 2 * w // 3 - 1, h], fill=(255, 255, 255))
        draw.rectangle([2 * w // 3, 0, w - 1, h], fill=(206, 43, 55))
    else:
        draw.rectangle([0, 0, w - 1, h - 1], fill=(255, 255, 255))
        draw.line([(w // 2, 0), (w // 2, h)], fill=(204, 0, 0), width=max(2, w // 12))
        draw.line([(0, h // 2), (w, h // 2)], fill=(204, 0, 0), width=max(2, h // 12))
        draw.line([(0, 0), (w - 1, h - 1)], fill=(204, 0, 0), width=1)
        draw.line([(w - 1, 0), (0, h - 1)], fill=(204, 0, 0), width=1)

    border_draw = ImageDraw.Draw(img)
    border_draw.rectangle([0, 0, w - 1, h - 1], outline=(100, 100, 100), width=1)

    photo = ImageTk.PhotoImage(img)
    _FLAG_CACHE[code] = photo
    return photo
LAUNCHER_TEXT = {
    "en": {
        "title": "BananaWiki",
        "subtitle": "Start a local wiki for this computer or classroom.",
        "choose_language": "Choose a language",
        "change_language": "Change language",
        "status_ready": "Ready to start",
        "status_starting": "Starting BananaWiki...",
        "status_running": "BananaWiki is running",
        "status_failed": "BananaWiki could not start",
        "status_stopped": "BananaWiki is stopped",
        "share_label": "Share on this local network",
        "start": "Start",
        "stop": "Stop",
        "open": "Open",
        "copy": "Copy address",
        "folder": "Files",
        "starting_address": "Preparing the local server...",
        "stopped_address": "Start BananaWiki to get an address.",
        "qr_waiting": "Start with network sharing to show a QR code.",
        "qr_none": "Computer-only mode does not need a QR code.",
        "qr_missing": "QR support is missing in this build. Copy the address instead.",
        "qr_ready": "Scan from another device on the same local network.",
        "copied": "Copied",
        "teacher": "This computer",
        "classroom": "Other devices",
        "cannot_start": "Cannot start BananaWiki",
        "check_logs": "Open the Files folder and check logs/easy-deployment.log.",
        "no_internet": "No internet connection detected. Cloud features may be unavailable.",
        "stop_for_data": "Stop the server first to manage data.",
        "data_management": "Data Management",
        "export_data": "Export All",
        "import_data": "Import",
        "delete_all": "Delete All",
        "export_success": "Data exported successfully.",
        "import_confirm": "Importing replaces current data after validation and keeps the previous data in a sibling folder. Continue?",
        "delete_confirm": "This will DELETE ALL data. Are you sure?",
        "data_deleted": "All data deleted.",
        "import_complete": "Data imported. Previous data folder:",
        "import_failed": "Import failed. The archive may be invalid.",
        "export_failed": "Export failed.",
        "delete_failed": "Failed to delete data.",
    },
    "it": {
        "title": "BananaWiki",
        "subtitle": "Avvia una wiki locale per questo computer o per la classe.",
        "choose_language": "Scegli la lingua",
        "change_language": "Cambia lingua",
        "status_ready": "Pronto per l'avvio",
        "status_starting": "Avvio di BananaWiki...",
        "status_running": "BananaWiki è in esecuzione",
        "status_failed": "BananaWiki non si è avviato",
        "status_stopped": "BananaWiki è fermo",
        "share_label": "Condividi su questa rete locale",
        "start": "Avvia",
        "stop": "Ferma",
        "open": "Apri",
        "copy": "Copia indirizzo",
        "folder": "File",
        "starting_address": "Preparazione del server locale...",
        "stopped_address": "Avvia BananaWiki per ottenere un indirizzo.",
        "qr_waiting": "Avvia con condivisione rete per mostrare il QR code.",
        "qr_none": "La modalità solo computer non richiede un QR code.",
        "qr_missing": "Il supporto QR manca in questa build. Copia l'indirizzo.",
        "qr_ready": "Scansiona da un altro dispositivo sulla stessa rete locale.",
        "copied": "Copiato",
        "teacher": "Questo computer",
        "classroom": "Altri dispositivi",
        "cannot_start": "Impossibile avviare BananaWiki",
        "check_logs": "Apri la cartella File e controlla logs/easy-deployment.log.",
        "no_internet": "Nessuna connessione Internet rilevata. Le funzionalità cloud potrebbero non essere disponibili.",
        "stop_for_data": "Ferma il server prima di gestire i dati.",
        "data_management": "Gestione Dati",
        "export_data": "Esporta Tutto",
        "import_data": "Importa",
        "delete_all": "Elimina Tutto",
        "export_success": "Dati esportati con successo.",
        "import_confirm": "L’importazione sostituisce i dati dopo la verifica e conserva quelli precedenti in una cartella accanto. Continuare?",
        "delete_confirm": "Questo ELIMINERÀ tutti i dati. Sei sicuro?",
        "data_deleted": "Tutti i dati eliminati.",
        "import_complete": "Dati importati. Cartella dei dati precedenti:",
        "import_failed": "Importazione fallita. L'archivio potrebbe non essere valido.",
        "export_failed": "Esportazione fallita.",
        "delete_failed": "Eliminazione dati fallita.",
    },
}


@dataclass(frozen=True)
class NetworkStatus:
    connected: bool
    label: str
    ip_address: str | None = None
    ssid: str | None = None


@dataclass(frozen=True)
class AddressInfo:
    local_url: str
    classroom_url: str | None
    message: str


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def bundle_root() -> Path:
    if is_frozen() and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS).resolve()  # type: ignore[attr-defined]
    return Path(__file__).resolve().parents[1]


def executable_base_dir() -> Path:
    """Return the user-visible folder beside the executable.

    On macOS app bundles, ``sys.executable`` lives inside
    ``App.app/Contents/MacOS``. Put the portable data folder beside the app
    bundle instead of burying it inside the bundle internals.
    """
    if not is_frozen():
        return Path.cwd().resolve()

    exe = Path(sys.executable).resolve()
    parts = exe.parts
    for index, part in enumerate(parts):
        if part.endswith(".app") and index + 2 < len(parts):
            if parts[index + 1] == "Contents" and parts[index + 2] == "MacOS":
                return Path(*parts[: index + 1]).parent
    return exe.parent


def data_dir(base_dir: Path | None = None) -> Path:
    return (base_dir or executable_base_dir()) / DATA_DIR_NAME






def has_previous_session(root: Path) -> bool:
    db_path = wiki_database_path(root)
    if not db_path.exists():
        return False
    try:
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT setup_done FROM site_settings WHERE id = 1"
            ).fetchone()
        return bool(row and row[0])
    except sqlite3.Error:
        return True


def normalize_launcher_language(language: str | None) -> str:
    value = str(language or "").strip().lower()
    return value if value in LANGUAGE_CHOICES else "en"


def portable_environment(
    root: Path,
    *,
    host: str,
    port: int,
    wiki_language: str | None = None,
) -> dict[str, str]:
    """Return environment variables for the portable standalone wiki."""
    ensure_data_layout(root)
    env = os.environ.copy()
    # The portable data folder must own the session-signing key. A global
    # SECRET_KEY in the host shell would make sessions depend on that machine
    # environment instead of BananaWiki Files/instance/.secret_key.
    env.pop("SECRET_KEY", None)
    env.update(
        {
            "BW_HOST": host,
            "BW_PORT": str(port),
            "BW_PROXY_MODE": "0",
            # Local portable data folders.
            "BW_INSTANCE_DIR": str(root / "instance"),
            "BW_DATABASE_PATH": str(wiki_database_path(root)),
            "BW_UPLOAD_FOLDER": str(root / "uploads"),
            "BW_ATTACHMENT_FOLDER": str(root / "attachments"),
            "BW_CHAT_ATTACHMENT_FOLDER": str(root / "chat_attachments"),
            "BW_KANBAN_ATTACHMENT_FOLDER": str(root / "kanban_attachments"),
            "BW_CUSTOM_PAGE_FILES_FOLDER": str(root / "custom_page_files"),
            "BW_FAVICON_UPLOAD_FOLDER": str(root / "favicons"),
            "BW_TTS_FOLDER": str(root / "tts"),
            "BW_SITE_EXPORT_TEMP_DIR": str(root / "tmp_exports"),
            "BW_LOG_FILE": str(root / "logs" / "bananawiki.log"),
            "BW_LOGGING_LEVEL": env.get("BW_LOGGING_LEVEL", "medium"),
            "BW_EASY_DEPLOYMENT": "1",
            # Enable in-process TTS workers (no separate tts_worker.py needed).
            "BW_TTS_INLINE_WORKER": "1",
            # Default TTS to efficient mode on classroom / portable hardware.
            # The wiki admin can still switch to balanced (higher quality) via
            # Settings → TTS → Performance mode.
            "BW_TTS_PERFORMANCE_MODE": "fast",
            # Portable desktop installs can live on filesystems where POSIX
            # secret-key permissions are not meaningful, especially Windows.
            "BW_ENV": env.get("BW_ENV", "development"),
            "PYTHONUNBUFFERED": "1",
        }
    )
    if wiki_language is not None:
        env["BW_DEFAULT_INTERFACE_LANGUAGE"] = normalize_launcher_language(
            wiki_language
        )
    return env


def _is_usable_lan_ip(value: str) -> bool:
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return False
    if ip.version != 4:
        return False
    if ip.is_loopback or ip.is_multicast or ip.is_unspecified:
        return False
    return bool(ip.is_private or ip.is_link_local)


def _rank_lan_ip(value: str) -> int:
    ip = ipaddress.ip_address(value)
    if ip.is_private:
        return 0
    if ip.is_link_local:
        return 1
    return 2


def _candidate_ipv4_addresses_from_text(text: str) -> list[str]:
    candidates = []
    skip_markers = (
        "default gateway",
        "dhcp server",
        "dns server",
        "gateway",
        "subnet mask",
    )
    likely_address_markers = (
        " inet ",
        "ip address",
        "ipv4 address",
    )
    for raw_line in text.splitlines():
        line = raw_line.strip()
        lowered = f" {line.lower()} "
        if any(marker in lowered for marker in skip_markers):
            continue
        if not any(marker in lowered for marker in likely_address_markers):
            continue
        matches = re.findall(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", line)
        if not matches:
            continue
        match = matches[0]
        if _is_usable_lan_ip(match) and match not in candidates:
            candidates.append(match)
    return candidates


def _candidate_ipv4_addresses_from_commands() -> list[str]:
    system = platform.system().lower()
    commands: list[list[str]]
    if system == "windows":
        commands = [["ipconfig"]]
    elif system == "darwin":
        commands = [["ifconfig"]]
    else:
        commands = [["ip", "-4", "addr", "show"], ["ifconfig"]]

    candidates: list[str] = []
    for command in commands:
        for ip in _candidate_ipv4_addresses_from_text(_run_command(command)):
            if ip not in candidates:
                candidates.append(ip)
    return candidates


def local_ip_address() -> str | None:
    """Best-effort LAN IP detection without requiring internet access."""
    candidates: list[str] = []
    for target in [("1.1.1.1", 53), ("8.8.8.8", 53), ("208.67.222.222", 53)]:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.settimeout(0.2)
                sock.connect(target)
                ip = sock.getsockname()[0]
                if ip and _is_usable_lan_ip(ip):
                    candidates.append(ip)
                    break
        except OSError:
            continue

    try:
        hostname = socket.gethostname()
        for item in socket.getaddrinfo(hostname, None, socket.AF_INET):
            ip = item[4][0]
            if ip and _is_usable_lan_ip(ip) and ip not in candidates:
                candidates.append(ip)
    except OSError:
        pass

    for ip in _candidate_ipv4_addresses_from_commands():
        if ip not in candidates:
            candidates.append(ip)

    if not candidates:
        return None
    return sorted(candidates, key=_rank_lan_ip)[0]


def _run_command(args: Iterable[str], timeout: float = 5.0) -> str:
    try:
        completed = subprocess.run(
            list(args),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            creationflags=_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return (completed.stdout or "").strip()


def _try_import_qr_modules():
    try:
        import qrcode  # type: ignore
        from PIL import ImageTk  # type: ignore
    except Exception:
        return None, None
    return qrcode, ImageTk


def wifi_network_name() -> str | None:
    system = platform.system().lower()
    if system == "windows":
        output = _run_command(["netsh", "wlan", "show", "interfaces"])
        for raw_line in output.splitlines():
            line = raw_line.strip()
            if line.lower().startswith("ssid") and "bssid" not in line.lower():
                _, _, value = line.partition(":")
                value = value.strip()
                if value:
                    return value
    elif system == "darwin":
        iface = None
        ports = _run_command(["networksetup", "-listallhardwareports"])
        hw_section = ""
        for line in ports.splitlines():
            if line.startswith("Hardware Port:"):
                hw_section = line.split(":", 1)[1].strip()
            wifi_names = {"wi-fi", "airport", "wifi", "wireless", "wlan"}
            if hw_section.lower() in wifi_names:
                if line.strip().startswith("Device:"):
                    iface = line.split(":", 1)[1].strip()
                    break
        if not iface:
            iface = "en0"
        output = _run_command(["networksetup", "-getairportnetwork", iface])
        if ":" in output and "not associated" not in output.lower():
            return output.split(":", 1)[1].strip() or None
        # Fallback: try common Wi-Fi interface names
        for candidate in ("en0", "en1"):
            if candidate == iface:
                continue
            output = _run_command(["networksetup", "-getairportnetwork", candidate])
            if ":" in output and "not associated" not in output.lower():
                return output.split(":", 1)[1].strip() or None
    else:
        output = _run_command(["nmcli", "-t", "-f", "active,ssid", "dev", "wifi"])
        for line in output.splitlines():
            if line.startswith("yes:"):
                return line.split(":", 1)[1].strip() or None
        output = _run_command(["iwgetid", "-r"])
        if output:
            return output.strip()
    return None


def detect_network() -> NetworkStatus:
    ip = local_ip_address()
    if not ip:
        return NetworkStatus(False, "No network detected")
    ssid = wifi_network_name()
    if ssid:
        return NetworkStatus(True, f'I am connected to network "{ssid}"', ip, ssid)
    return NetworkStatus(True, f"I am connected to this network ({ip})", ip, None)


def is_port_available(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


def choose_port(host: str = LOCAL_HOST, preferred: int = DEFAULT_PORT) -> int:
    if is_port_available(host, preferred):
        return preferred
    fallback_start = 1234 if preferred < 1024 else preferred
    for port in range(fallback_start, fallback_start + 200):
        if is_port_available(host, port):
            return port
    raise RuntimeError(f"No free local port found between {fallback_start} and {fallback_start + 199}.")


def check_internet(timeout: float = 2.0) -> bool:
    for host in ("1.1.1.1", "8.8.8.8", "208.67.222.222"):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.settimeout(timeout)
                sock.connect((host, 53))
            return True
        except OSError:
            continue
    return False


def server_command(
    *,
    root: Path | None = None,
    host: str | None = None,
    port: int | None = None,
    wiki_language: str | None = None,
) -> list[str]:
    if is_frozen():
        cmd = [sys.executable, "--server-child"]
    else:
        cmd = [sys.executable, str(Path(__file__).resolve()), "--server-child"]
    if root is not None:
        cmd.extend(["--data-dir", str(root)])
    if host is not None:
        cmd.extend(["--host", host])
    if port is not None:
        cmd.extend(["--port", str(port)])
    if wiki_language is not None:
        cmd.extend([
            "--wiki-language",
            normalize_launcher_language(wiki_language),
        ])
    return cmd


def health_url(port: int) -> str:
    return f"http://{LOCAL_HOST}:{port}/healthz"


def visit_url(port: int) -> str:
    return f"http://{LOCAL_HOST}:{port}/"


def administrator_visit_url(port: int, root: Path) -> str:
    """Authorize first setup only in the browser opened on the operator's computer."""
    if has_previous_session(root):
        return visit_url(port)
    import hashlib
    import hmac
    from urllib.parse import urlencode
    token = os.environ.get("BW_SETUP_TOKEN", "").strip()
    if not token:
        try:
            key = (root / "instance" / ".secret_key").read_text().strip()
        except OSError:
            return visit_url(port)
        token = hmac.new(key.encode("utf-8"), b"initial-admin-setup", hashlib.sha256).hexdigest()
    return f"http://{LOCAL_HOST}:{port}/setup?" + urlencode({"setup_token": token})


def lan_visit_url(port: int, ip_address: str | None) -> str | None:
    if not ip_address:
        return None
    return f"http://{ip_address}:{port}/"


def address_message(
    *,
    port: int,
    shared: bool,
    ip_address: str | None,
    labels: dict[str, str] | None = None,
) -> str:
    return address_info(port=port, shared=shared, ip_address=ip_address, labels=labels).message


def address_info(
    *,
    port: int,
    shared: bool,
    ip_address: str | None,
    labels: dict[str, str] | None = None,
) -> AddressInfo:
    labels = labels or {"teacher": "Teacher computer", "classroom": "Kids' devices"}
    local = visit_url(port)
    if not shared:
        return AddressInfo(
            local_url=local,
            classroom_url=None,
            message=f"{labels['teacher']}: {local}",
        )

    lan = lan_visit_url(port, ip_address)
    if not lan:
        return AddressInfo(
            local_url=local,
            classroom_url=None,
            message=f"{labels['teacher']}: {local}",
        )
    return AddressInfo(
        local_url=local,
        classroom_url=lan,
        message=f"{labels['teacher']}: {local}\n{labels['classroom']}: {lan}",
    )


def _tcp_port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        result = sock.connect_ex((host, port))
        return result == 0


def _check_http_ready(port: int, timeout: float = 3.0) -> bool:
    try:
        with urllib.request.urlopen(health_url(port), timeout=timeout) as response:
            return response.status < 500
    except (OSError, urllib.error.URLError):
        pass
    try:
        with urllib.request.urlopen(
            f"http://{LOCAL_HOST}:{port}/setup", timeout=timeout
        ) as response:
            return response.status < 500
    except (OSError, urllib.error.URLError):
        pass
    return False


def wait_for_server(port: int, timeout: float = 60.0) -> bool:
    deadline = time.time() + timeout
    delay = 0.1
    while time.time() < deadline:
        if _tcp_port_open(LOCAL_HOST, port):
            if _check_http_ready(port):
                return True
        delay = min(delay * 2, 2.0)
        time.sleep(delay)
    return False


def _log_error(message: str) -> None:
    """Write a message to the easy-deployment log file, best-effort."""
    try:
        log_path = executable_base_dir() / DATA_DIR_NAME / "logs" / "easy-deployment.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(f"\n--- ERROR {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
            fh.write(f"{message}\n")
    except OSError:
        pass


def run_server_child(root: Path, host: str, port: int, wiki_language: str | None = None) -> None:
    """Hold exclusive data ownership while the portable server is running."""
    with data_lock(root):
        return _run_server_child(root, host, port, wiki_language)


def _run_server_child(
    root: Path,
    host: str,
    port: int,
    wiki_language: str | None = None,
) -> None:
    ensure_data_layout(root)
    print("[smoke] STEP 1: ensure_data_layout done", flush=True)
    os.environ.update(
        portable_environment(
            root,
            host=host,
            port=port,
            wiki_language=wiki_language,
        )
    )
    print("[smoke] STEP 2: portable_environment done", flush=True)

    # Redirect stdout/stderr to a file inside the data directory.
    # On Windows, windowed PyInstaller builds (console=False) have no
    # real stdout/stderr: subprocess.Popen captures nothing.  Writing
    # to a file in the data dir lets the smoke test read it.
    stdout_log = root / "logs" / "server-stdout.log"
    stdout_log.parent.mkdir(parents=True, exist_ok=True)
    _stdout_fh = open(stdout_log, "w", encoding="utf-8")

    class _FlushingWriter:
        """Write to a file and flush after every write so logs survive kill."""

        def __init__(self, fh):
            self._fh = fh

        def write(self, s):
            self._fh.write(s)
            self._fh.flush()

        def flush(self):
            self._fh.flush()

        def fileno(self):
            return self._fh.fileno()

    sys.stdout = _FlushingWriter(_stdout_fh)
    sys.stderr = _FlushingWriter(_stdout_fh)
    print("[smoke] STEP 3: stdout/stderr redirected to file", flush=True)

    repo_root = bundle_root()
    print(f"[smoke] STEP 4: bundle_root={repo_root}", flush=True)
    os.chdir(repo_root)
    sys.path.insert(0, str(repo_root))
    print("[smoke] STEP 5: chdir and sys.path done", flush=True)

    try:
        from app import app  # noqa: WPS433
        print("[smoke] STEP 6: 'from app import app' succeeded", flush=True)
    except ImportError as err:
        import importlib.util

        app_path = repo_root / "app.py"
        if not app_path.is_file():
            _log_error(f"Cannot find app.py at {app_path}")
            raise ImportError(f"Cannot find app.py at {app_path}") from err
        spec = importlib.util.spec_from_file_location("_app_module", str(app_path))
        _app_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_app_module)  # type: ignore[union-attr]
        app = _app_module.app
        print("[smoke] STEP 6: app imported via dynamic import", flush=True)
    except Exception as exc:
        _log_error(f"Failed to import app: {exc}")
        raise

    try:
        from waitress import serve  # type: ignore
        print("[smoke] STEP 7: waitress imported, starting serve()", flush=True)
    except Exception as exc:
        print(f"[smoke] STEP 7: waitress import failed ({exc}), using app.run()", flush=True)
        app.run(host=host, port=port, debug=False, threaded=True)
    else:
        serve(app, host=host, port=port, threads=8)
    finally:
        try:
            _stdout_fh.close()
        except Exception:
            pass








class EasyDeploymentApp:
    def __init__(self, root_dir: Path) -> None:
        import tkinter as tk
        from tkinter import messagebox, ttk

        self.tk = tk
        self.ttk = ttk
        self.messagebox = messagebox
        self.root_dir = root_dir
        self.process: subprocess.Popen[str] | None = None
        self.port: int | None = None
        self.server_host = LOCAL_HOST
        self.server_ip_address: str | None = None
        self.server_shared = False
        self.current_student_url: str | None = None
        self.selected_language: str | None = None
        self.log_handle = None
        self.qr_photo = None

        self.window = tk.Tk()
        self.window.title(APP_TITLE)
        self.window.geometry("640x640")
        self.window.minsize(580, 500)
        self.window.configure(bg=UI_BG)
        self.window.protocol("WM_DELETE_WINDOW", self.on_close)

        # Enable HiDPI awareness per platform
        system = platform.system().lower()
        if system == "windows":
            try:
                import ctypes
                ctypes.windll.shcore.SetProcessDpiAwareness(1)
            except Exception:
                pass
        elif system == "darwin":
            try:
                self.window.tk.call("tk", "scaling", 2.0)
            except Exception:
                pass
        else:
            try:
                dpi = self.window.winfo_fpixels("1i")
                scaling = max(dpi / 72.0, 1.0)
                self.window.tk.call("tk", "scaling", scaling)
            except Exception:
                pass

        self.style = ttk.Style(self.window)
        self._configure_styles()

        self.status_var = tk.StringVar(value="")
        self.address_var = tk.StringVar(value="")
        self.qr_status_var = tk.StringVar(value="")
        self.share_network_var = tk.BooleanVar(value=True)

        self._build_language_ui()
        self._poll_process()

    def tr(self, key: str) -> str:
        language = normalize_launcher_language(self.selected_language)
        return LAUNCHER_TEXT[language].get(key, LAUNCHER_TEXT["en"][key])

    def _clear_window(self) -> None:
        for child in self.window.winfo_children():
            child.destroy()

    def _configure_styles(self) -> None:
        try:
            if "clam" in self.style.theme_names():
                self.style.theme_use("clam")
        except Exception:
            pass
        self.style.configure(".", font=("", 11), background=UI_BG, foreground=UI_TEXT)
        self.style.configure("App.TFrame", background=UI_BG)
        self.style.configure("Card.TFrame", background=UI_CARD, borderwidth=1, relief="solid")
        self.style.configure("Card.TLabel", background=UI_CARD, foreground=UI_TEXT)
        self.style.configure("Muted.Card.TLabel", background=UI_CARD, foreground=UI_MUTED)
        self.style.configure("Title.Card.TLabel", background=UI_CARD, foreground=UI_TEXT, font=("", 25, "bold"))
        self.style.configure("Subtitle.Card.TLabel", background=UI_CARD, foreground=UI_MUTED)
        self.style.configure("Status.Card.TLabel", background=UI_CARD, foreground=UI_TEXT, font=("", 13, "bold"))
        self.style.configure("Tiny.Card.TLabel", background=UI_CARD, foreground=UI_MUTED, font=("", 9))
        self.style.configure("Accent.TButton", background=UI_ACCENT, foreground="#1a1b2e", font=("", 12, "bold"), padding=(14, 9), borderwidth=0)
        self.style.map("Accent.TButton", background=[("active", UI_ACCENT_ACTIVE), ("disabled", "#374151")], foreground=[("disabled", "#6b7280")])
        self.style.configure("Plain.TButton", background="#374151", foreground=UI_TEXT, padding=(12, 8), borderwidth=0)
        self.style.map("Plain.TButton", background=[("active", "#4b5563"), ("disabled", "#1f2937")], foreground=[("disabled", "#6b7280")])
        self.style.configure("Danger.TButton", background=UI_DANGER, foreground="#ffffff", padding=(12, 8), borderwidth=0)
        self.style.map("Danger.TButton", background=[("active", "#dc2626"), ("disabled", "#374151")], foreground=[("disabled", "#6b7280")])
        self.style.configure("Flag.TButton", background=UI_CARD, foreground=UI_TEXT, font=("", 13, "bold"), padding=(22, 18), borderwidth=1, relief="solid")
        self.style.map("Flag.TButton", background=[("active", "#374151")], relief=[("pressed", "sunken")])
        self.style.configure("App.TCheckbutton", background=UI_CARD, foreground=UI_TEXT, font=("", 11))

    def _build_language_ui(self) -> None:
        ttk = self.ttk
        self._clear_window()

        self.window.geometry("640x500")
        outer = ttk.Frame(self.window, padding=24, style="App.TFrame")
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(0, weight=1)

        card = ttk.Frame(outer, padding=28, style="Card.TFrame")
        card.grid(row=0, column=0, sticky="nsew")
        card.columnconfigure(0, weight=1)

        ttk.Label(card, text="BananaWiki", style="Title.Card.TLabel").grid(row=0, column=0, pady=(4, 8))
        ttk.Label(card, text=LAUNCHER_TEXT["en"]["choose_language"], style="Status.Card.TLabel").grid(row=1, column=0)
        ttk.Label(card, text=LAUNCHER_TEXT["it"]["choose_language"], style="Muted.Card.TLabel").grid(row=2, column=0, pady=(3, 24))

        choices = ttk.Frame(card, style="Card.TFrame")
        choices.grid(row=3, column=0, sticky="ew")
        choices.columnconfigure(0, weight=1)
        choices.columnconfigure(1, weight=1)

        for column, (code, meta) in enumerate(LANGUAGE_CHOICES.items()):
            flag_img = _generate_flag_image(code)
            button = ttk.Button(
                choices,
                text=f"  {meta['name']}",
                image=flag_img,
                compound="left",
                command=lambda value=code: self.choose_language(value),
                style="Flag.TButton",
            )
            button.grid(row=0, column=column, padx=8, sticky="nsew")

        ttk.Label(card, text="Pick a language to continue / Scegli una lingua per continuare.", style="Tiny.Card.TLabel").grid(
            row=4,
            column=0,
            pady=(24, 0),
        )

    def choose_language(self, language: str) -> None:
        self.selected_language = normalize_launcher_language(language)
        self._build_ui()
        self.refresh_network()

    def _build_ui(self) -> None:
        ttk = self.ttk
        self._clear_window()

        self.window.geometry("640x680")
        outer = ttk.Frame(self.window, padding=18, style="App.TFrame")
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)

        header = ttk.Frame(outer, padding=(16, 14), style="Card.TFrame")
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text=self.tr("title"), style="Title.Card.TLabel").grid(row=0, column=0, sticky="w")
        lang_code = normalize_launcher_language(self.selected_language)
        lang_flag = _generate_flag_image(lang_code, (24, 16))
        ttk.Label(header, text=f"  {LANGUAGE_CHOICES[lang_code]['name']}", image=lang_flag, compound="left", style="Muted.Card.TLabel").grid(row=0, column=1, padx=(12, 8), sticky="e")
        ttk.Button(header, text=self.tr("change_language"), command=self._build_language_ui, style="Plain.TButton").grid(row=0, column=2, sticky="e")
        ttk.Label(header, text=self.tr("subtitle"), wraplength=520, style="Subtitle.Card.TLabel").grid(row=1, column=0, columnspan=3, sticky="w", pady=(8, 0))

        self.internet_warning = ttk.Label(
            outer,
            text=f"⚠ {self.tr('no_internet')}",
            background="#78350f",
            foreground="#fbbf24",
            padding=8,
            anchor="center",
        )
        self.internet_warning.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        has_internet = check_internet()
        if has_internet:
            self.internet_warning.grid_remove()

        controls = ttk.Frame(outer, padding=16, style="Card.TFrame")
        controls.grid(row=2, column=0, sticky="ew", pady=(14, 0))
        for column in range(3):
            controls.columnconfigure(column, weight=1)

        ttk.Label(controls, textvariable=self.status_var, style="Status.Card.TLabel").grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 12))
        self.start_button = ttk.Button(controls, text=self.tr("start"), command=self.start_server, style="Accent.TButton")
        self.start_button.grid(row=1, column=0, padx=(0, 8), sticky="ew")
        self.open_button = ttk.Button(controls, text=self.tr("open"), command=self.open_wiki, state="disabled", style="Plain.TButton")
        self.open_button.grid(row=1, column=1, padx=(0, 8), sticky="ew")
        self.stop_button = ttk.Button(controls, text=self.tr("stop"), command=self.stop_server, state="disabled", style="Danger.TButton")
        self.stop_button.grid(row=1, column=2, sticky="ew")

        self.share_checkbox = ttk.Checkbutton(
            controls,
            text=self.tr("share_label"),
            variable=self.share_network_var,
            style="App.TCheckbutton",
        )
        self.share_checkbox.grid(row=2, column=0, columnspan=3, sticky="w", pady=(14, 0))

        server = ttk.Frame(outer, padding=16, style="Card.TFrame")
        server.grid(row=3, column=0, sticky="nsew", pady=(14, 0))
        server.columnconfigure(0, weight=1)
        ttk.Label(server, textvariable=self.address_var, wraplength=520, style="Card.TLabel").grid(row=0, column=0, sticky="w")

        secondary = ttk.Frame(server, style="Card.TFrame")
        secondary.grid(row=1, column=0, sticky="ew", pady=(12, 0))
        secondary.columnconfigure(0, weight=1)
        secondary.columnconfigure(1, weight=1)
        self.copy_button = ttk.Button(secondary, text=self.tr("copy"), command=self.copy_address, state="disabled", style="Plain.TButton")
        self.copy_button.grid(row=0, column=0, padx=(0, 8), sticky="ew")
        ttk.Button(secondary, text=self.tr("folder"), command=self.open_folder, style="Plain.TButton").grid(row=0, column=1, sticky="ew")

        qr_frame = ttk.Frame(outer, padding=(16, 0), style="App.TFrame")
        qr_frame.grid(row=4, column=0, sticky="ew", pady=(14, 0))
        qr_frame.columnconfigure(1, weight=1)
        self.qr_label = ttk.Label(qr_frame, text=self.tr("qr_waiting"), background=UI_BG, foreground=UI_MUTED)
        self.qr_label.grid(row=0, column=0, sticky="w")
        ttk.Label(
            qr_frame,
            textvariable=self.qr_status_var,
            wraplength=380,
            background=UI_BG,
            foreground=UI_MUTED,
        ).grid(row=0, column=1, sticky="w", padx=(16, 0))

        data_frame = ttk.Frame(outer, padding=12, style="Card.TFrame")
        data_frame.grid(row=5, column=0, sticky="ew", pady=(14, 0))
        data_frame.columnconfigure(0, weight=1)
        data_frame.columnconfigure(1, weight=1)
        data_frame.columnconfigure(2, weight=1)
        ttk.Label(data_frame, text=self.tr("data_management"), style="Muted.Card.TLabel").grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 8),
        )
        self.import_button = ttk.Button(
            data_frame, text=self.tr("import_data"), command=self.import_data, style="Plain.TButton",
        )
        self.import_button.grid(row=1, column=0, padx=(0, 6), sticky="ew")
        self.export_button = ttk.Button(
            data_frame, text=self.tr("export_data"), command=self.export_data, style="Plain.TButton",
        )
        self.export_button.grid(row=1, column=1, padx=(0, 6), sticky="ew")
        self.delete_button = ttk.Button(
            data_frame, text=self.tr("delete_all"), command=self.delete_all_data, style="Danger.TButton",
        )
        self.delete_button.grid(row=1, column=2, sticky="ew")

        self.status_var.set(self.tr("status_ready"))
        self.address_var.set(self.tr("stopped_address"))

    def refresh_network(self) -> None:
        status = detect_network()
        if status.connected:
            self.share_checkbox.configure(state="normal")
        else:
            self.share_checkbox.configure(state="disabled")

    def _easy_log_path(self) -> Path:
        return self.root_dir / "logs" / "easy-deployment.log"

    def _close_log_handle(self) -> None:
        if self.log_handle:
            try:
                self.log_handle.close()
            except OSError:
                pass
        self.log_handle = None

    def _latest_log_excerpt(self, line_count: int = 8) -> str:
        try:
            lines = self._easy_log_path().read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return ""
        return "\n".join(lines[-line_count:])

    def start_server(self) -> None:
        if self.process and self.process.poll() is None:
            return

        network = detect_network()
        share_on_network = bool(network.connected and self.share_network_var.get())
        self.server_host = LAN_BIND_HOST if share_on_network else LOCAL_HOST
        self.server_ip_address = network.ip_address if share_on_network else None
        self.server_shared = share_on_network

        try:
            self.port = choose_port(self.server_host)
        except RuntimeError as exc:
            self.messagebox.showerror(self.tr("cannot_start"), str(exc))
            return

        self.status_var.set(self.tr("status_starting"))
        self.address_var.set(self.tr("starting_address"))
        self.start_button.configure(state="disabled")

        try:
            ensure_data_layout(self.root_dir)
            env = portable_environment(
                self.root_dir,
                host=self.server_host,
                port=self.port,
                wiki_language=self.selected_language,
            )
            self._close_log_handle()
            self.log_handle = self._easy_log_path().open("a", encoding="utf-8")
            self.log_handle.write(f"\n--- BananaWiki start {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
            self.log_handle.flush()
            popen_kwargs: dict[str, object] = dict(
                cwd=str(bundle_root()),
                env=env,
                stdout=self.log_handle,
                stderr=subprocess.STDOUT,
                text=True,
                creationflags=_NO_WINDOW,
            )
            if platform.system().lower() != "windows":
                popen_kwargs["start_new_session"] = True
            self.process = subprocess.Popen(
                server_command(
                    root=self.root_dir,
                    host=self.server_host,
                    port=self.port,
                    wiki_language=self.selected_language,
                ),
                **popen_kwargs,
            )
        except OSError as exc:
            self._close_log_handle()
            self.status_var.set(self.tr("status_stopped"))
            self.start_button.configure(state="normal")
            self.messagebox.showerror(self.tr("cannot_start"), str(exc))
            return

        threading.Thread(target=self._finish_startup, daemon=True).start()

    def _finish_startup(self) -> None:
        assert self.port is not None
        ok = wait_for_server(self.port)
        self.window.after(0, lambda: self._set_started(ok))

    def _retry_startup(self) -> None:
        """Retry the health check when the process is alive but initial check timed out."""
        assert self.port is not None
        ok = wait_for_server(self.port, timeout=30.0)
        self.window.after(0, lambda: self._set_started(ok))

    def _set_started(self, ok: bool) -> None:
        if ok and self.process and self.process.poll() is None and self.port is not None:
            url = administrator_visit_url(self.port, self.root_dir)
            info = address_info(
                port=self.port,
                shared=self.server_shared,
                ip_address=self.server_ip_address,
                labels=LAUNCHER_TEXT[normalize_launcher_language(self.selected_language)],
            )
            self.current_student_url = info.classroom_url or info.local_url
            self.status_var.set(self.tr("status_running"))
            self.address_var.set(info.message)
            self.stop_button.configure(state="normal")
            self.open_button.configure(state="normal")
            self.copy_button.configure(state="normal")
            self.update_qr_code(info.classroom_url)
            webbrowser.open(url)
        else:
            process_alive = self.process is not None and self.process.poll() is None
            if process_alive and self.port is not None:
                # Server process is still running but health check timed out.
                # Give it a chance: show a "starting" state instead of failure.
                self.status_var.set(self.tr("status_starting"))
                self.address_var.set(self.tr("starting_address"))
                threading.Thread(target=self._retry_startup, daemon=True).start()
                return
            excerpt = self._latest_log_excerpt()
            self.status_var.set(self.tr("status_failed"))
            self.address_var.set(self.tr("check_logs") + (f"\n\n{excerpt}" if excerpt else ""))
            self.start_button.configure(state="normal")
            self.stop_button.configure(state="disabled")
            self.open_button.configure(state="disabled")
            self.copy_button.configure(state="disabled")
            self.clear_qr_code(self.tr("qr_waiting"))

    def stop_server(self) -> None:
        if not self.process:
            return
        if self.process.poll() is None:
            self.status_var.set(self.tr("status_stopped"))
            self.process.terminate()
            try:
                self.process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        self.process = None
        self._close_log_handle()
        self.server_host = LOCAL_HOST
        self.server_ip_address = None
        self.server_shared = False
        self.current_student_url = None
        self.status_var.set(self.tr("status_stopped"))
        self.address_var.set(self.tr("stopped_address"))
        self.start_button.configure(state="normal")
        self.stop_button.configure(state="disabled")
        self.open_button.configure(state="disabled")
        self.copy_button.configure(state="disabled")
        self.clear_qr_code(self.tr("qr_waiting"))

    def open_wiki(self) -> None:
        if self.port:
            webbrowser.open(administrator_visit_url(self.port, self.root_dir))

    def copy_address(self) -> None:
        if not self.current_student_url:
            return
        self.window.clipboard_clear()
        self.window.clipboard_append(self.current_student_url)
        self.qr_status_var.set(f"{self.tr('copied')}: {self.current_student_url}")

    def clear_qr_code(self, message: str) -> None:
        self.qr_photo = None
        self.qr_label.configure(image="", text=message)
        self.qr_status_var.set("")

    def update_qr_code(self, classroom_url: str | None) -> None:
        if not classroom_url:
            self.clear_qr_code(self.tr("qr_none"))
            return

        qrcode, ImageTk = _try_import_qr_modules()
        if not qrcode or not ImageTk:
            self.clear_qr_code(self.tr("qr_missing"))
            return

        qr = qrcode.QRCode(border=2, box_size=6)
        qr.add_data(classroom_url)
        qr.make(fit=True)
        image = qr.make_image(fill_color="black", back_color="white").convert("RGB")
        self.qr_photo = ImageTk.PhotoImage(image)
        self.qr_label.configure(image=self.qr_photo, text="")
        self.qr_status_var.set(self.tr("qr_ready"))

    def open_folder(self) -> None:
        ensure_data_layout(self.root_dir)
        if platform.system().lower() == "windows":
            os.startfile(self.root_dir)  # type: ignore[attr-defined]
        elif platform.system().lower() == "darwin":
            subprocess.Popen(["open", str(self.root_dir)])
        else:
            subprocess.Popen(["xdg-open", str(self.root_dir)])

    def _poll_process(self) -> None:
        if self.process and self.process.poll() is not None:
            self.process = None
            self._close_log_handle()
            self.current_student_url = None
            if self.selected_language:
                self.status_var.set(self.tr("status_stopped"))
                self.address_var.set(self.tr("stopped_address"))
                self.start_button.configure(state="normal")
                self.stop_button.configure(state="disabled")
                self.open_button.configure(state="disabled")
                self.copy_button.configure(state="disabled")
                self.clear_qr_code(self.tr("qr_waiting"))
        self.window.after(1000, self._poll_process)

    def _server_running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def export_data(self) -> None:
        if self._server_running():
            self.messagebox.showwarning(self.tr("data_management"), self.tr("stop_for_data"))
            return
        from tkinter import filedialog
        default_name = f"bananawiki_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"
        path = filedialog.asksaveasfilename(
            defaultextension=".zip",
            filetypes=[("ZIP files", "*.zip")],
            initialfile=default_name,
        )
        if not path:
            return
        try:
            create_data_archive(self.root_dir, Path(path))
            self.qr_status_var.set(self.tr("export_success"))
        except Exception as exc:
            self.messagebox.showerror(self.tr("data_management"), f"{self.tr('export_failed')}\n{exc}")

    def import_data(self) -> None:
        if self._server_running():
            self.messagebox.showwarning(self.tr("data_management"), self.tr("stop_for_data"))
            return
        if not self.messagebox.askyesno(self.tr("data_management"), self.tr("import_confirm")):
            return
        from tkinter import filedialog
        path = filedialog.askopenfilename(filetypes=[("ZIP files", "*.zip"), ("All files", "*.*")])
        if not path:
            return
        self.status_var.set(self.tr("status_stopped"))
        self.window.update()
        try:
            previous = extract_data_archive(Path(path), self.root_dir)
            self.qr_status_var.set(self.tr("import_complete") + " " + str(previous))
        except Exception as exc:
            self.messagebox.showerror(self.tr("data_management"), f"{self.tr('import_failed')}\n{exc}")

    def delete_all_data(self) -> None:
        if self._server_running():
            self.messagebox.showwarning(self.tr("data_management"), self.tr("stop_for_data"))
            return
        if not self.messagebox.askyesno(self.tr("data_management"), self.tr("delete_confirm")):
            return
        try:
            clear_all_data(self.root_dir)
            self.qr_status_var.set(self.tr("data_deleted"))
        except Exception as exc:
            self.messagebox.showerror(self.tr("data_management"), f"{self.tr('delete_failed')}\n{exc}")

    def on_close(self) -> None:
        self.stop_server()
        self.window.destroy()

    def run(self) -> None:
        self.window.mainloop()


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=APP_TITLE)
    parser.add_argument("--server-child", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--data-dir", help=argparse.SUPPRESS)
    parser.add_argument("--host", default=LOCAL_HOST, help=argparse.SUPPRESS)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=argparse.SUPPRESS)
    parser.add_argument("--language", dest="launcher_language", help=argparse.SUPPRESS)
    parser.add_argument("--wiki-language", dest="wiki_language", help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:

    try:
        _diag = data_dir() / "_startup_marker.txt"
        _diag.parent.mkdir(parents=True, exist_ok=True)
        _diag.write_text(f"main() reached at {time.strftime('%H:%M:%S')}\n",
                         encoding="utf-8")
    except Exception:
        pass

    args = parse_args(argv or sys.argv[1:])

    # Write diagnostic marker into the data dir so the smoke test can see it.
    try:
        data = Path(args.data_dir).resolve() if args.data_dir else data_dir()
        _diag2 = data / "logs" / "_args_parsed.txt"
        _diag2.parent.mkdir(parents=True, exist_ok=True)
        _diag2.write_text(
            f"args parsed: server_child={args.server_child} host={args.host} "
            f"port={args.port} data_dir={args.data_dir}\n",
            encoding="utf-8",
        )
    except Exception:
        pass

    root = Path(args.data_dir).resolve() if args.data_dir else data_dir()

    if args.server_child:
        run_server_child(root, args.host, args.port, args.wiki_language)
        return 0

    try:
        app = EasyDeploymentApp(root)
        app.run()
    except Exception as exc:
        _log_error(f"GUI failed to start: {exc}")
        try:
            import tkinter as tk
            from tkinter import messagebox

            _root = tk.Tk()
            _root.withdraw()
            messagebox.showerror(
                APP_TITLE,
                f"BananaWiki could not start:\n\n{exc}",
            )
            _root.destroy()
        except Exception:
            pass
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
