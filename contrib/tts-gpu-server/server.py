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
refused, never silently truncated; at most ``TTS_MAX_CONCURRENT`` syntheses
run at once and further requests wait up to ``TTS_QUEUE_TIMEOUT`` seconds
before getting 503. Configuration is read from environment variables, see
README.md.
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
import subprocess
import sys
import threading
import wave
from collections import OrderedDict
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Protocol

log = logging.getLogger("bananawiki-tts-gpu")

VERSION = "1.6.0"
_LANGUAGE = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z]{2})?$")
_VOICE_NAME = re.compile(r"^[\w.-]{1,120}$")

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
    def __init__(self, status: HTTPStatus, message: str):
        super().__init__(message)
        self.status = status


class Engine(Protocol):
    def synthesize(self, text: str, language: str) -> tuple[bytes, str]: ...

    def details(self) -> dict[str, Any]: ...


# ── Piper engine ──────────────────────────────────────────────────────────────


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
        with wave.open(buffer, "wb") as wav_file:
            voice.synthesize_wav(text, wav_file, syn_config=self._config())
        wav = buffer.getvalue()
        if not wav:
            raise RuntimeError("Piper produced no audio.")
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


# ── HTTP ──────────────────────────────────────────────────────────────────────


TENANT_TOKEN = re.compile(r"bwt1\.([A-Za-z0-9]{1,64})\.([0-9a-f]{64})")


def tenant_token(master: str, tenant: str) -> str:
    """The token BananaWiki hosting gives one wiki (see ``bananawiki.hosting.instances.tenant_tts_token``)."""
    digest = hmac.new(master.encode("utf-8"), f"bananawiki-tts-tenant:{tenant}".encode(), "sha256")
    return f"bwt1.{tenant}.{digest.hexdigest()}"


def token_valid(settings: Settings, presented: str) -> bool:
    candidate = presented.encode("utf-8", "replace")
    if hmac.compare_digest(candidate, settings.token.encode("ascii")):
        return True
    match = TENANT_TOKEN.fullmatch(presented)
    if not match or match.group(1) in settings.revoked_tenants:
        return False
    return hmac.compare_digest(candidate, tenant_token(settings.token, match.group(1)).encode("ascii"))


def make_handler(settings: Settings, engine: Engine) -> type[BaseHTTPRequestHandler]:
    slots = threading.BoundedSemaphore(settings.max_concurrent)

    class Handler(BaseHTTPRequestHandler):
        server_version = f"BananaWikiTTS/{VERSION}"
        sys_version = ""
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args: Any) -> None:
            log.info("%s %s", self.address_string(), fmt % args)

        def _send(self, status: HTTPStatus, body: bytes, content_type: str, headers: dict[str, str] | None = None):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: HTTPStatus, data: dict[str, Any], headers: dict[str, str] | None = None):
            self._send(status, json.dumps(data).encode("utf-8"), "application/json", headers)

        def _authorized(self) -> bool:
            scheme, _, presented = (self.headers.get("Authorization") or "").partition(" ")
            if scheme.lower() != "bearer":
                return False
            return token_valid(settings, presented.strip())

        def do_GET(self) -> None:  # noqa: N802 - http.server API
            if self.path.split("?", 1)[0] != "/health":
                self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                return
            report: dict[str, Any] = {"status": "ok", "version": VERSION}
            if self._authorized():
                report.update(engine.details())
            self._json(HTTPStatus.OK, report)

        def _read_request(self) -> tuple[str, str]:
            if not self._authorized():
                status = HTTPStatus.UNAUTHORIZED if "Authorization" not in self.headers else HTTPStatus.FORBIDDEN
                raise RequestError(status, "A valid bearer token is required.")
            length = self.headers.get("Content-Length")
            if length is None or not length.isdigit():
                raise RequestError(HTTPStatus.LENGTH_REQUIRED, "Content-Length is required.")
            if int(length) > settings.max_body_bytes:
                raise RequestError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "The request is too large.")
            if (self.headers.get("Content-Type") or "").split(";")[0].strip().lower() != "application/json":
                raise RequestError(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "Send application/json.")
            try:
                payload = json.loads(self.rfile.read(int(length)).decode("utf-8"))
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
            except RequestError as error:
                self.close_connection = True
                self._json(error.status, {"error": str(error)})
                return
            if not slots.acquire(timeout=settings.queue_timeout):
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
            log.info("Synthesized %d characters in %s -> %d bytes", len(text), language, len(audio))
            self._send(HTTPStatus.OK, audio, content_type)

        def do_PUT(self) -> None:  # noqa: N802
            self._json(HTTPStatus.METHOD_NOT_ALLOWED, {"error": "method not allowed"})

        do_DELETE = do_PATCH = do_PUT  # noqa: N815

    return Handler


def make_server(settings: Settings, engine: Engine) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((settings.host, settings.port), make_handler(settings, engine))
    server.daemon_threads = True
    return server


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
