"""BananaWiki GPU speech server.

A small standalone HTTP service that runs Piper neural voices on a GPU
(onnxruntime-gpu) for BananaWiki wikis on the same private network. It uses
only the Python standard library plus Piper.

    GET  /health       {"status": "ok"}; with a valid token also GPU and voice details
    POST /synthesize   {"text": "...", "language": "en"} -> audio/mpeg (or audio/wav)

Every /synthesize request needs ``Authorization: Bearer <token>`` (compared in
constant time): either ``TTS_AUTH_TOKEN`` itself or, for wikis on a BananaWiki
hosting platform, that wiki's own token ``bwt1.<wiki id>.<hex>``, an
HMAC-SHA256 of the wiki id under ``TTS_AUTH_TOKEN``. Hosted wikis can run
plugins, so they never get the master token; a single wiki is shut out with
``TTS_REVOKED_TENANTS``. Request bodies and text length are capped and
refused, never silently truncated; the text is synthesised in pieces of at
most 500 characters, as the wiki's own Piper does, so one request without a
full stop cannot exhaust the GPU memory. At most ``TTS_MAX_CONCURRENT``
syntheses run at once and further requests wait up to ``TTS_QUEUE_TIMEOUT``
seconds before getting 503. A client that stays silent for
``TTS_SOCKET_TIMEOUT`` seconds while sending a request (or between keep-alive
requests), or that has not sent a whole request (line, headers and body)
``TTS_REQUEST_TIMEOUT`` seconds after it began waiting for one, is
disconnected. Every answer to a request without a valid token (``/health``
included) closes the connection: only authenticated clients keep connections
alive. Connections beyond ``TTS_MAX_CONNECTIONS`` are closed at once. One
address keeps at most ``TTS_MAX_CONNECTIONS_PER_CLIENT`` connections, of
which at most ``TTS_MAX_PENDING_PER_CLIENT`` have not yet presented a valid
token; a new connection over these caps closes that address's oldest
connection not busy with an authenticated request (or is itself closed when
there is none), so slow or idle clients cannot tie up every connection or
thread, nor shut out the next request from the address they share with
other wikis. A hosted wiki (one tenant token) has at most
``TTS_MAX_BUSY_PER_TENANT`` requests in progress at once, over all addresses;
a further one is answered 429 and its connection closed, so one wiki cannot
keep every connection of the shared address busy; such a connection is the
first to make room for a new one and never counts as pending, so a wiki
reconnecting after each 429 cannot push another wiki's new connection out. A free synthesis slot goes
to the waiting client served longest ago (each tenant, and the master token,
counts as one), so one wiki's queue cannot keep the others waiting. Every
request is logged with its tenant (``tenant=<wiki id>``, ``operator`` for
the master token), the id that ``TTS_REVOKED_TENANTS`` takes.
Configuration is read from environment variables; see README.md.
"""

from __future__ import annotations

import hmac
import io
import json
import logging
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import wave
from collections import OrderedDict
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Protocol

log = logging.getLogger("bananawiki-tts-gpu")

VERSION = "1.8.0"  # a test checks it matches bananawiki.__version__
_LANGUAGE = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z]{2})?$")
_CONTENT_LENGTH = re.compile(r"[0-9]+")
_VOICE_NAME = re.compile(r"^[\w.-]{1,120}$")
# What the logs show of client input (a request line) is escaped as repr() would: every control character (C0,
# DEL, C1), the Unicode line separators and the bidirectional overrides. A terminal acts on them (OSC 52 writes the
# clipboard, others retitle the window or rewrite lines), and a line break would forge further log lines. Python's
# own BaseHTTPRequestHandler.log_message escapes C0 and C1 too (3.12 and newer); this server overrides it.
_LOG_ESCAPES = {code: f"\\x{code:02x}" for code in (*range(0x20), 0x7F, *range(0x80, 0xA0))}
_LOG_ESCAPES.update({code: f"\\u{code:04x}"
                     for code in (0x2028, 0x2029, *range(0x202A, 0x202F), *range(0x2066, 0x206A))})

# Same voices as BananaWiki's local Piper defaults, so both sound alike.
DEFAULT_VOICES: dict[str, str] = {
    "ar": "ar_JO-kareem-medium", "bg": "bg_BG-dimitar-medium", "ca": "ca_ES-upc_ona-medium",
    "cs": "cs_CZ-jirka-medium", "cy": "cy_GB-bu_tts-medium", "da": "da_DK-talesyntese-medium",
    "de": "de_DE-thorsten-medium", "el": "el_GR-rapunzelina-medium", "en": "en_US-lessac-medium",
    "es": "es_ES-sharvard-medium", "eu": "eu_ES-maider-medium", "fa": "fa_IR-amir-medium",
    "fi": "fi_FI-harri-medium", "fr": "fr_FR-tom-medium", "fr-CA": "fr_FR-tom-medium",
    "hi": "hi_IN-pratham-medium", "hu": "hu_HU-anna-medium", "id": "id_ID-news_tts-medium",
    "is": "is_IS-salka-medium", "it": "it_IT-paola-medium", "ka": "ka_GE-natia-medium",
    "kk": "kk_KZ-issai-high", "ku": "ku_TR-berfin_renas-medium", "lb": "lb_LU-marylux-medium",
    "lv": "lv_LV-aivars-medium", "ml": "ml_IN-meera-medium", "ne": "ne_NP-google-medium",
    "nl": "nl_NL-mls-medium", "no": "no_NO-talesyntese-medium", "pl": "pl_PL-gosia-medium",
    "pt": "pt_BR-cadu-medium", "pt-PT": "pt_PT-tugão-medium", "ro": "ro_RO-mihai-medium",
    "ru": "ru_RU-irina-medium", "sk": "sk_SK-lili-medium", "sl": "sl_SI-artur-medium",
    "sq": "sq_AL-edon-medium", "sr": "sr_RS-serbski_institut-medium", "sv": "sv_SE-lisa-medium",
    "sw": "sw_CD-lanfrica-medium", "te": "te_IN-maya-medium", "tr": "tr_TR-dfki-medium",
    "uk": "uk_UA-mykyta-high", "ur": "ur_PK-fasih-medium", "vi": "vi_VN-vais1000-medium",
    "zh": "zh_CN-huayan-medium", "zh-CN": "zh_CN-huayan-medium", "zh-TW": "zh_CN-huayan-medium",
    "yue": "zh_CN-huayan-medium",
}


class ConfigError(SystemExit):
    pass


def _env_int(environ, name: str, default: int, minimum: int = 1) -> int:
    raw = environ.get(name, "").strip()
    try:
        value = int(raw) if raw else default
    except ValueError:
        raise ConfigError(f"{name} must be an integer.") from None
    if value < minimum:
        raise ConfigError(f"{name} must be at least {minimum}.")
    return value


def _env_float(environ, name: str, default: float) -> float:
    raw = environ.get(name, "").strip()
    try:
        return float(raw) if raw else default
    except ValueError:
        raise ConfigError(f"{name} must be a number.") from None


def _env_positive_float(environ, name: str, default: float) -> float:
    value = _env_float(environ, name, default)
    if value <= 0:
        raise ConfigError(f"{name} must be greater than 0.")
    return value


@dataclass(frozen=True)
class Settings:
    token: str = field(repr=False)
    revoked_tenants: frozenset[str] = frozenset()
    host: str = "127.0.0.1"
    port: int = 8787
    voice_dir: str = "voices"
    use_cuda: bool = True
    max_loaded_voices: int = 8
    max_input_chars: int = 20_000
    max_body_bytes: int = 256 * 1024
    max_concurrent: int = 1
    queue_timeout: float = 30.0
    socket_timeout: float = 30.0
    request_timeout: float = 10.0
    max_connections: int = 64
    max_connections_per_client: int = 8
    max_pending_per_client: int = 4
    max_busy_per_tenant: int = 2
    output_format: str = "mp3"
    auto_download: bool = True
    preload: tuple[str, ...] = ("en", "it")
    ffmpeg: str = ""
    length_scale: float = 1.0
    noise_scale: float = 0.667
    noise_w_scale: float = 0.8
    voices: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_VOICES))

    @classmethod
    def from_env(cls, environ=None) -> Settings:
        env = os.environ if environ is None else environ
        token = env.get("TTS_AUTH_TOKEN", "").strip()
        if len(token) < 16 or any(ord(char) < 33 or ord(char) > 126 for char in token):
            raise ConfigError(
                "TTS_AUTH_TOKEN must be set to at least 16 printable characters, for example "
                "`python3 -c 'import secrets; print(secrets.token_hex(32))'`. Give the same value to BananaWiki."
            )
        output = env.get("TTS_OUTPUT_FORMAT", "mp3").strip().lower()
        if output not in ("mp3", "wav"):
            raise ConfigError("TTS_OUTPUT_FORMAT must be mp3 or wav.")
        voices = dict(DEFAULT_VOICES)
        for part in env.get("TTS_VOICE_MAP", "").split(","):
            code, sep, voice = part.partition("=")
            if sep and _LANGUAGE.match(code.strip()) and _VOICE_NAME.match(voice.strip()):
                voices[code.strip()] = voice.strip()
        return cls(
            token=token,
            revoked_tenants=frozenset(part.strip() for part in env.get("TTS_REVOKED_TENANTS", "").split(",")
                                      if part.strip()),
            host=env.get("TTS_HOST", "127.0.0.1").strip() or "127.0.0.1",
            port=_env_int(env, "TTS_PORT", 8787),
            voice_dir=env.get("PIPER_VOICE_DIR", "").strip() or str(Path(__file__).resolve().parent / "voices"),
            use_cuda=env.get("TTS_USE_CUDA", "1").strip().lower() in ("1", "true", "yes", "on"),
            max_loaded_voices=_env_int(env, "TTS_MAX_LOADED_VOICES", 8),
            max_input_chars=_env_int(env, "TTS_MAX_INPUT_CHARS", 20_000),
            max_body_bytes=_env_int(env, "TTS_MAX_BODY_BYTES", 256 * 1024, 1024),
            max_concurrent=_env_int(env, "TTS_MAX_CONCURRENT", 1),
            queue_timeout=_env_float(env, "TTS_QUEUE_TIMEOUT", 30.0),
            socket_timeout=_env_positive_float(env, "TTS_SOCKET_TIMEOUT", 30.0),
            request_timeout=_env_positive_float(env, "TTS_REQUEST_TIMEOUT", 10.0),
            max_connections=_env_int(env, "TTS_MAX_CONNECTIONS", 64),
            max_connections_per_client=_env_int(env, "TTS_MAX_CONNECTIONS_PER_CLIENT", 8),
            max_pending_per_client=_env_int(env, "TTS_MAX_PENDING_PER_CLIENT", 4),
            max_busy_per_tenant=_env_int(env, "TTS_MAX_BUSY_PER_TENANT", 2),
            output_format=output,
            auto_download=env.get("TTS_AUTO_DOWNLOAD", "1").strip().lower() in ("1", "true", "yes", "on"),
            preload=tuple(c.strip() for c in env.get("TTS_PRELOAD", "en,it").split(",") if c.strip()),
            ffmpeg=env.get("TTS_FFMPEG", "").strip(),
            length_scale=_env_float(env, "TTS_LENGTH_SCALE", 1.0),
            noise_scale=_env_float(env, "TTS_NOISE_SCALE", 0.667),
            noise_w_scale=_env_float(env, "TTS_NOISE_W_SCALE", 0.8),
            voices=voices,
        )


class RequestError(Exception):
    def __init__(self, status: HTTPStatus, message: str, headers: dict[str, str] | None = None):
        super().__init__(message)
        self.status = status
        self.headers = headers


class Engine(Protocol):
    def synthesize(self, text: str, language: str) -> tuple[bytes, str]: ...

    def details(self) -> dict[str, Any]: ...


# Text in pieces
# The same cuts as BananaWiki's local Piper (bananawiki/wiki/features/tts/text.py
# and piper_child.py; tests check they agree), copied so the server needs no wiki.

CHUNK_CHARS = 500
_CHUNK_BREAKS = (
    (". ", "! ", "? ", "… ", "。", "！", "？"),
    ("; ", ": ", ", ", "；", "：", "，", "、"),
    (" ",),
)


def speech_chunks(text: str, limit: int = CHUNK_CHARS) -> list[str]:
    """*text* (single spaces) in pieces of at most *limit* characters, read one after another.

    Piper synthesises a sentence at once and its memory grows faster than
    the sentence, so a long text without a full stop could exhaust the GPU.
    A piece ends at the last sentence end in the second half of its window,
    else at a clause mark or a space, else at *limit*.
    """
    limit = max(2, limit)
    pieces: list[str] = []
    start = 0
    while len(text) - start > limit:
        low, high = start + limit // 2, start + limit
        cut = high
        for marks in _CHUNK_BREAKS:
            ends = [found + len(mark.rstrip()) for mark in marks if (found := text.rfind(mark, low, high)) >= 0]
            if ends:
                cut = max(ends)
                break
        pieces.append(text[start:cut].strip())
        start = cut
    pieces.append(text[start:].strip())
    return [piece for piece in pieces if piece]


def _placeholder_format(wav: wave.Wave_write) -> None:
    """A format for a file Piper may leave without audio; Piper sets the real one first."""
    wav.setnchannels(1)
    wav.setsampwidth(2)
    wav.setframerate(22_050)


def write_wav(voice: Any, chunks: list[str], syn_config: Any, output: Any) -> int:
    """Synthesise *chunks* one after another into one WAV; returns the number of frames."""
    frames = 0
    shape: tuple[int, int, int] | None = None
    with wave.open(output, "wb") as target:
        _placeholder_format(target)
        for chunk in chunks:
            buffer = io.BytesIO()
            with wave.open(buffer, "wb") as part:
                _placeholder_format(part)
                voice.synthesize_wav(chunk, part, syn_config=syn_config)
            buffer.seek(0)
            with wave.open(buffer, "rb") as part:
                count = part.getnframes()
                if not count:
                    continue
                current = (part.getnchannels(), part.getsampwidth(), part.getframerate())
                if shape is None:
                    shape = current
                    target.setnchannels(current[0])
                    target.setsampwidth(current[1])
                    target.setframerate(current[2])
                elif current != shape:
                    raise ValueError("the voice changed its audio format between two pieces")
                target.writeframes(part.readframes(count))
            frames += count
    return frames


# Piper engine


class PiperEngine:
    """Piper voices kept in an LRU cache (each loaded voice holds GPU memory)."""

    def __init__(self, settings: Settings):
        os.environ.setdefault("ORT_DISABLE_TELEMETRY", "1")
        self.settings = settings
        self._voices: OrderedDict[str, Any] = OrderedDict()
        self._lock = threading.Lock()
        self._download_lock = threading.Lock()
        self._syn_config = None

    def _voice_name(self, language: str) -> str | None:
        voices = self.settings.voices
        return voices.get(language) or voices.get(language.split("-", 1)[0])

    def _load(self, name: str) -> Any:
        from piper import PiperVoice  # type: ignore[import-not-found]

        voice_dir = Path(self.settings.voice_dir)
        model, config = voice_dir / f"{name}.onnx", voice_dir / f"{name}.onnx.json"
        if not (model.is_file() and config.is_file()):
            if not self.settings.auto_download:
                raise RequestError(HTTPStatus.BAD_REQUEST, f"Voice {name} is not installed on this server.")
            with self._download_lock:
                if not (model.is_file() and config.is_file()):
                    from piper.download_voices import download_voice  # type: ignore[import-not-found]

                    voice_dir.mkdir(parents=True, exist_ok=True)
                    log.info("Downloading voice %s", name)
                    download_voice(name, voice_dir)
        log.info("Loading voice %s (cuda=%s)", name, self.settings.use_cuda)
        return PiperVoice.load(str(model), config_path=str(config), use_cuda=self.settings.use_cuda)

    def voice(self, language: str) -> Any:
        name = self._voice_name(language)
        if not name:
            raise RequestError(HTTPStatus.BAD_REQUEST, f"No voice for language {language!r}.")
        with self._lock:
            if name in self._voices:
                self._voices.move_to_end(name)
                return self._voices[name]
        loaded = self._load(name)
        with self._lock:
            self._voices[name] = loaded
            while len(self._voices) > self.settings.max_loaded_voices:
                self._voices.popitem(last=False)
        return loaded

    def _config(self) -> Any:
        if self._syn_config is None:
            from piper import SynthesisConfig  # type: ignore[import-not-found]

            s = self.settings
            self._syn_config = SynthesisConfig(length_scale=s.length_scale, noise_scale=s.noise_scale,
                                               noise_w_scale=s.noise_w_scale)
        return self._syn_config

    def synthesize(self, text: str, language: str) -> tuple[bytes, str]:
        voice = self.voice(language)
        buffer = io.BytesIO()
        if not write_wav(voice, speech_chunks(" ".join(text.split())), self._config(), buffer):
            raise RuntimeError("Piper produced no audio.")
        wav = buffer.getvalue()
        if self.settings.output_format == "wav":
            return wav, "audio/wav"
        return wav_to_mp3(wav, self.settings.ffmpeg), "audio/mpeg"

    def details(self) -> dict[str, Any]:
        gpu = False
        try:
            import onnxruntime  # type: ignore[import-not-found]

            gpu = "CUDAExecutionProvider" in onnxruntime.get_available_providers()
        except ImportError:
            pass
        with self._lock:
            loaded = list(self._voices)
        return {"gpu_available": gpu, "cuda_enabled": self.settings.use_cuda, "loaded_voices": loaded,
                "output_format": self.settings.output_format}

    def preload(self) -> None:
        for language in self.settings.preload:
            try:
                self.voice(language)
            except Exception:  # noqa: BLE001 - a missing voice must not stop the server
                log.warning("Could not preload the voice for %s", language, exc_info=True)


def wav_to_mp3(wav: bytes, override: str = "") -> bytes:
    binary = override if override and os.path.isfile(override) and os.access(override, os.X_OK) else None
    binary = binary or shutil.which("ffmpeg")
    if not binary:
        raise RuntimeError("ffmpeg is not installed; set TTS_OUTPUT_FORMAT=wav or install ffmpeg.")
    result = subprocess.run(
        [binary, "-nostdin", "-loglevel", "error", "-i", "pipe:0", "-vn", "-acodec", "libmp3lame",
         "-q:a", "4", "-f", "mp3", "pipe:1"],
        input=wav, capture_output=True, timeout=120, check=False,
    )
    if result.returncode != 0 or not result.stdout:
        raise RuntimeError(f"ffmpeg failed: {result.stderr[:300].decode('utf-8', 'replace')}")
    return result.stdout


# HTTP


TENANT_TOKEN = re.compile(r"bwt1\.([A-Za-z0-9]{1,64})\.([0-9a-f]{64})")


def tenant_token(master: str, tenant: str) -> str:
    """The token BananaWiki hosting gives one wiki (see ``bananawiki.hosting.instances.tenant_tts_token``)."""
    digest = hmac.new(master.encode("utf-8"), f"bananawiki-tts-tenant:{tenant}".encode(), "sha256")
    return f"bwt1.{tenant}.{digest.hexdigest()}"


def tenant_of(presented: str) -> str | None:
    """The wiki id of a (valid) tenant token; None for the master token."""
    match = TENANT_TOKEN.fullmatch(presented)
    return match.group(1) if match else None


def token_valid(settings: Settings, presented: str) -> bool:
    candidate = presented.encode("utf-8", "replace")
    if hmac.compare_digest(candidate, settings.token.encode("ascii")):
        return True
    match = TENANT_TOKEN.fullmatch(presented)
    if not match or match.group(1) in settings.revoked_tenants:
        return False
    return hmac.compare_digest(candidate, tenant_token(settings.token, match.group(1)).encode("ascii"))


class _FairSlots:
    """At most *size* syntheses at once. A freed slot goes to the waiting client served longest ago (a tenant,
    or the master token), its oldest request first, so one wiki's queued requests cannot keep the others
    waiting for the GPU (a plain semaphore wakes whoever asked first, and a busy wiki always asks first)."""

    def __init__(self, size: int):
        self._free = size
        self._ready = threading.Condition()
        self._waiting: list[tuple[str, object]] = []  # (client, ticket), in order of arrival
        self._served: dict[str, int] = {}  # client -> turn it last got a slot; one entry per tenant at most
        self._turn = 0

    def _next(self) -> object:
        return min(self._waiting, key=lambda item: self._served.get(item[0], 0))[1]

    def acquire(self, client: str, timeout: float) -> bool:
        ticket = (client, object())
        deadline = time.monotonic() + timeout
        with self._ready:
            self._waiting.append(ticket)
            try:
                while not (self._free > 0 and self._next() is ticket[1]):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return False
                    self._ready.wait(remaining)
                self._free -= 1
                self._turn += 1
                self._served[client] = self._turn
                return True
            finally:
                self._waiting.remove(ticket)
                self._ready.notify_all()  # the next in line may be someone else now

    def release(self) -> None:
        with self._ready:
            self._free += 1
            self._ready.notify_all()


# Connection states (SpeechServer.mark): no valid token yet, handling an authenticated request, waiting for
# the next request after an authenticated one (keep-alive), and refused for its tenant's busy cap (answered 429,
# then closed).
_PENDING, _BUSY, _IDLE, _CROWDED = "pending", "busy", "idle", "crowded"


class _ClientTimeout(Exception):
    """The client stopped sending the request body (timeout or closed connection)."""


class _RequestReader:
    """The reading side of a connection: each receive waits at most the idle timeout, and a whole request
    must arrive before the deadline set by :meth:`start` (a client trickling a byte now and then would
    otherwise keep its connection forever). Raises TimeoutError, as the socket does."""

    def __init__(self, sock: Any, idle_timeout: float):
        self._sock, self._idle = sock, idle_timeout
        self._buffer = bytearray()
        self._deadline: float | None = None

    def start(self, seconds: float) -> None:
        self._deadline = time.monotonic() + seconds

    def _fill(self) -> bool:
        wait = self._idle if self._deadline is None else min(self._idle, self._deadline - time.monotonic())
        if wait <= 0:
            raise TimeoutError("the request took too long")
        self._sock.settimeout(wait)
        try:
            chunk = self._sock.recv(65536)
        finally:
            self._sock.settimeout(self._idle)  # the answer is written with the idle timeout
        self._buffer += chunk
        return bool(chunk)

    def _take(self, size: int) -> bytes:
        data = bytes(self._buffer[:size])
        del self._buffer[:size]
        return data

    def readline(self, limit: int = -1) -> bytes:
        while True:
            end = self._buffer.find(b"\n")
            if end >= 0 and (limit < 0 or end < limit):
                return self._take(end + 1)
            if 0 <= limit <= len(self._buffer) or not self._fill():
                return self._take(len(self._buffer) if limit < 0 else limit)

    def read(self, size: int = -1) -> bytes:
        while (size < 0 or len(self._buffer) < size) and self._fill():
            pass
        return self._take(len(self._buffer) if size < 0 else size)

    def close(self) -> None:
        self._buffer.clear()


def make_handler(settings: Settings, engine: Engine) -> type[BaseHTTPRequestHandler]:
    slots = _FairSlots(settings.max_concurrent)

    class Handler(BaseHTTPRequestHandler):
        server_version = f"BananaWikiTTS/{VERSION}"
        sys_version = ""
        protocol_version = "HTTP/1.1"
        # Applied to the socket: a client silent this long while sending is disconnected.
        timeout = settings.socket_timeout

        def setup(self) -> None:
            super().setup()
            self.rfile = _RequestReader(self.connection, settings.socket_timeout)  # type: ignore[assignment]
            self.tenant: str | None = None  # of the current request's valid token ("" for the master token)
            self.crowded = False

        def handle_one_request(self) -> None:
            self.tenant, self.crowded = None, False
            # The deadline counts from when the server starts waiting for the request line.
            self.rfile.start(settings.request_timeout)  # type: ignore[attr-defined]
            try:
                super().handle_one_request()
            except ConnectionError as error:
                # A client that reset or closed its connection went away: the stdlib catches only TimeoutError,
                # and socketserver would print a traceback for each one, outside the log format.
                self.close_connection = True
                log.debug("%s %s connection lost: %s", self.address_string(), self._who(),
                          type(error).__name__)
            finally:
                self._mark(_IDLE)

        def _mark(self, state: str) -> None:
            mark = getattr(self.server, "mark", None)
            if mark is not None:
                mark(self.client_address[0], self.connection, state)

        def _who(self) -> str:
            """The tenant for the logs: ``tenant=<wiki id>`` (what TTS_REVOKED_TENANTS takes), ``operator`` for
            the master token, ``-`` without a valid token."""
            if self.tenant is None:
                return "-"
            return f"tenant={self.tenant}" if self.tenant else "operator"

        def log_message(self, fmt: str, *args: Any) -> None:
            # The request line is the client's, byte for byte (see _LOG_ESCAPES).
            log.info("%s %s %s", self.address_string(), self._who(), (fmt % args).translate(_LOG_ESCAPES))

        def _send(self, status: HTTPStatus, body: bytes, content_type: str, headers: dict[str, str] | None = None):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            if self.close_connection:
                self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: HTTPStatus, data: dict[str, Any], headers: dict[str, str] | None = None):
            self._send(status, json.dumps(data).encode("utf-8"), "application/json", headers)

        def _authorized(self) -> bool:
            """Whether the request carries a valid token; the connection then counts as busy, not pending,
            and may stay open for the next request. Without one it closes after the answer. A tenant with
            ``max_busy_per_tenant`` other requests in progress is *crowded*: its connection stays evictable and
            closes after the answer."""
            scheme, _, presented = (self.headers.get("Authorization") or "").partition(" ")
            if scheme.lower() == "bearer" and token_valid(settings, presented.strip()):
                tenant = tenant_of(presented.strip())
                self.tenant = tenant or ""
                busy = getattr(self.server, "busy", None)
                if busy is not None and not busy(self.client_address[0], self.connection, tenant,
                                                 settings.max_busy_per_tenant):
                    self.crowded = True
                    self.close_connection = True
                return True
            self.close_connection = True
            return False

        def do_GET(self) -> None:  # noqa: N802 - http.server API
            authorized = self._authorized()
            if self.path.split("?", 1)[0] != "/health":
                self.close_connection = True
                self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                return
            report: dict[str, Any] = {"status": "ok", "version": VERSION}
            if authorized:
                report.update(engine.details())
            self._json(HTTPStatus.OK, report)

        def _read_request(self) -> tuple[str, str]:
            if not self._authorized():
                status = HTTPStatus.UNAUTHORIZED if "Authorization" not in self.headers else HTTPStatus.FORBIDDEN
                raise RequestError(status, "A valid bearer token is required.")
            if self.crowded:
                log.warning("Refused a request of tenant %s from %s: %d requests in progress "
                            "(TTS_MAX_BUSY_PER_TENANT)", self.tenant, self.address_string(),
                            settings.max_busy_per_tenant)
                raise RequestError(HTTPStatus.TOO_MANY_REQUESTS, "Too many requests in progress for this wiki.",
                                   {"Retry-After": "5"})
            length = self.headers.get("Content-Length") or ""
            # ASCII digits only (isdigit() also takes "\u00b2", headers being read as Latin-1), and no
            # int() of thousands of digits: either would end the request thread with a traceback.
            if not _CONTENT_LENGTH.fullmatch(length):
                raise RequestError(HTTPStatus.LENGTH_REQUIRED, "Content-Length is required.")
            digits = length.lstrip("0") or "0"
            if len(digits) > 12 or int(digits) > settings.max_body_bytes:
                raise RequestError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "The request is too large.")
            size = int(digits)
            if (self.headers.get("Content-Type") or "").split(";")[0].strip().lower() != "application/json":
                raise RequestError(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "Send application/json.")
            try:
                raw = self.rfile.read(size)
            except TimeoutError:
                raise _ClientTimeout from None
            if len(raw) != size:
                raise _ClientTimeout
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                raise RequestError(HTTPStatus.BAD_REQUEST, "The body is not valid JSON.") from None
            if not isinstance(payload, dict):
                raise RequestError(HTTPStatus.BAD_REQUEST, "The body must be a JSON object.")
            text, language = payload.get("text"), payload.get("language", "en")
            if not isinstance(text, str) or not text.strip():
                raise RequestError(HTTPStatus.BAD_REQUEST, "text is required.")
            if len(text) > settings.max_input_chars:
                raise RequestError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                                   f"text is longer than {settings.max_input_chars} characters.")
            if not isinstance(language, str) or not _LANGUAGE.match(language):
                raise RequestError(HTTPStatus.BAD_REQUEST, "language must be a code such as en or pt-PT.")
            return text.strip(), language

        def do_POST(self) -> None:  # noqa: N802 - http.server API
            if self.path.split("?", 1)[0] != "/synthesize":
                self.close_connection = True
                self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                return
            try:
                text, language = self._read_request()
            except _ClientTimeout:
                self.close_connection = True
                log.info("%s sent an incomplete request body; connection closed", self.address_string())
                return
            except RequestError as error:
                self.close_connection = True
                self._json(error.status, {"error": str(error)}, error.headers)
                return
            if not slots.acquire(self.tenant or "", settings.queue_timeout):
                self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "The server is busy."}, {"Retry-After": "30"})
                return
            try:
                audio, content_type = engine.synthesize(text, language)
            except RequestError as error:
                self._json(error.status, {"error": str(error)})
                return
            except Exception:  # noqa: BLE001 - never leak internals to the client
                log.exception("Synthesis failed (%s, %d characters)", language, len(text))
                self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "Synthesis failed."})
                return
            finally:
                slots.release()
            log.info("Synthesized %d characters in %s -> %d bytes for %s", len(text), language, len(audio),
                     self._who())
            self._send(HTTPStatus.OK, audio, content_type)

        def do_PUT(self) -> None:  # noqa: N802
            self.close_connection = True
            self._json(HTTPStatus.METHOD_NOT_ALLOWED, {"error": "method not allowed"})

        do_DELETE = do_PATCH = do_PUT  # noqa: N815

    return Handler


class SpeechServer(ThreadingHTTPServer):
    """One thread per connection, but never more than ``max_connections`` at once, nor more than
    ``max_per_client`` from one address, of which at most ``max_pending_per_client`` without a valid token.

    A connection over the global cap is closed straight away instead of
    queueing a thread, so a burst of idle connections cannot exhaust threads
    or memory, and one address cannot take every connection. Over an
    address's caps, the newcomer takes the place of that address's oldest
    connection that is not busy with an authenticated request (one that
    never presented a token first): hosted wikis share one address, so
    connections held open without a token must not shut out the next wiki's
    request. A connection answered 429 for its tenant's busy cap (*crowded*)
    goes first and is not counted as pending. Only when every connection of
    the address is busy is the newcomer closed. Each accepted connection is bounded by the handler's
    socket timeout and request deadline.
    """

    daemon_threads = True
    # socketserver listens with a backlog of 5: a client opening connections quickly (one whose requests are
    # refused reconnects at once) would overflow it, and the kernel then drops another wiki's connection attempt,
    # which its client retries only after a second. Connections are closed (not queued) once accepted, so a
    # longer backlog costs nothing.
    request_queue_size = 128

    def __init__(self, address: tuple[str, int], handler: type[BaseHTTPRequestHandler], max_connections: int,
                 max_per_client: int | None = None, max_pending_per_client: int | None = None):
        self._connections = threading.BoundedSemaphore(max_connections)
        self.max_connections = max_connections
        self.max_per_client = max_per_client or max_connections
        self.max_pending_per_client = min(max_pending_per_client or self.max_per_client, self.max_per_client)
        self._per_client: dict[str, dict[Any, str]] = {}  # address -> {socket: state}, oldest first
        self._tenants: dict[Any, str | None] = {}  # socket -> tenant of its latest authenticated request
        self._per_client_lock = threading.Lock()
        super().__init__(address, handler)

    def _claim(self, client: str, request: Any) -> tuple[bool, Any]:
        """Whether *request* may be served, and the connection of the same address to close for it."""
        with self._per_client_lock:
            held = self._per_client.get(client, {})
            pending = [sock for sock, state in held.items() if state == _PENDING]
            # A crowded connection is about to close: it goes first, and it never counts as pending, so a wiki
            # whose requests are refused for its busy cap cannot push another wiki's new connection out.
            crowded = next((sock for sock, state in held.items() if state == _CROWDED), None)
            evict = None
            if len(pending) >= self.max_pending_per_client:
                evict = pending[0]
            elif len(held) >= self.max_per_client:
                evict = crowded or (pending[0] if pending else
                                    next((sock for sock, state in held.items() if state == _IDLE), None))
                if evict is None:
                    return False, None
            if not self._connections.acquire(blocking=False):
                return False, None
            if evict is not None:
                del held[evict]  # its thread still holds a global slot until it ends
            held[request] = _PENDING
            self._per_client[client] = held
            return True, evict

    def mark(self, client: str, request: Any, state: str) -> None:
        """Record that a connection presented a valid token (busy) or finished an authenticated request
        (idle); one that never presented a token stays pending."""
        with self._per_client_lock:
            held = self._per_client.get(client, {})
            if request in held and not (state == _IDLE and held[request] in (_PENDING, _CROWDED)):
                held[request] = state

    def busy(self, client: str, request: Any, tenant: str | None, limit: int) -> bool:
        """Mark *request* busy with an authenticated request of *tenant* (None: the master token, never
        limited); False, leaving it as it was, when the tenant already has *limit* busy connections from
        any address."""
        with self._per_client_lock:
            held = self._per_client.get(client, {})
            if tenant is not None:
                others = sum(1 for connections in self._per_client.values()
                             for sock, state in connections.items()
                             if state == _BUSY and sock is not request and self._tenants.get(sock) == tenant)
                if others >= limit:
                    if request in held:
                        held[request] = _CROWDED
                    return False
            if request in held:
                held[request] = _BUSY
                self._tenants[request] = tenant
            return True

    def _release(self, client: str, request: Any) -> None:
        with self._per_client_lock:
            self._tenants.pop(request, None)
            held = self._per_client.get(client, {})
            held.pop(request, None)
            if not held:
                self._per_client.pop(client, None)
            self._connections.release()

    def process_request(self, request: Any, client_address: Any) -> None:
        claimed, evict = self._claim(client_address[0], request)
        if not claimed:
            log.warning("Refused a connection from %s: too many open connections (at most %d, %d per address)",
                        client_address[0], self.max_connections, self.max_per_client)
            self.shutdown_request(request)
            return
        if evict is not None:
            log.info("Closed an idle connection from %s to make room for a new one", client_address[0])
            try:
                evict.shutdown(socket.SHUT_RDWR)  # its handler sees the end of the stream and finishes
            except OSError:
                pass
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._release(client_address[0], request)
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._release(client_address[0], request)


def make_server(settings: Settings, engine: Engine) -> ThreadingHTTPServer:
    return SpeechServer((settings.host, settings.port), make_handler(settings, engine), settings.max_connections,
                        settings.max_connections_per_client, settings.max_pending_per_client)


def main() -> int:
    logging.basicConfig(level=os.environ.get("TTS_LOG_LEVEL", "INFO").upper(),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = Settings.from_env()
    engine = PiperEngine(settings)
    engine.preload()
    server = make_server(settings, engine)
    signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=server.shutdown, daemon=True).start())
    log.info("BananaWiki GPU speech server %s listening on %s:%s (cuda=%s, output=%s)", VERSION,
             settings.host, settings.port, settings.use_cuda, settings.output_format)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
