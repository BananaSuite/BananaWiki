"""Speech synthesis backends.

* :class:`PiperBackend` - local Piper neural voices (``piper-tts``, optional).
  Voice models live in ``BW_TTS_PIPER_VOICE_DIR`` and are downloaded on first
  use unless ``BW_TTS_PIPER_AUTO_DOWNLOAD=0``.
* :class:`RemoteGpuBackend` - a BananaWiki GPU speech server
  (``contrib/tts-gpu-server``) reached through :mod:`bananawiki.core.http`,
  with a fallback to local Piper when it fails.
* :class:`StubBackend` - a silent clip, for development (``BW_TTS_BACKEND=stub``).

Every backend writes a temporary file into the work directory it is given
and returns it; the worker moves it into place. Heavy dependencies are
imported lazily so the wiki runs without them.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from flask import current_app

from ....core import http
from . import options

log = logging.getLogger("bananawiki.tts")

MAX_REMOTE_AUDIO_BYTES = 64 * 1024 * 1024
FFMPEG_TIMEOUT = 300
_VOICE_NAME = re.compile(r"^[\w.-]{1,120}$")

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


class SynthesisError(RuntimeError):
    """Synthesis failed. ``retryable`` failures are retried with backoff;
    ``rate_limited`` ones are postponed without using the retry budget."""

    def __init__(self, message: str, *, retryable: bool = True, rate_limited: bool = False):
        super().__init__(message)
        self.retryable = retryable
        self.rate_limited = rate_limited


@dataclass(frozen=True)
class Audio:
    path: Path
    extension: str  # "mp3" or "wav"


class Synthesizer(Protocol):
    name: str

    def problem(self) -> str | None:
        """Why the backend cannot run right now, or None when it is ready."""

    def synthesize(self, spoken: str, language: str, workdir: Path) -> Audio: ...


def _temp_path(workdir: Path, suffix: str) -> Path:
    fd, name = tempfile.mkstemp(dir=workdir, prefix=".part-", suffix=suffix)
    os.close(fd)
    return Path(name)


def looks_like_audio(data: bytes) -> str | None:
    """``"mp3"`` or ``"wav"`` when *data* starts like that audio format."""
    if data[:3] == b"ID3":
        return "mp3"
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "wav"
    if len(data) < 3 or data[0] != 0xFF or (data[1] & 0xE0) != 0xE0:
        return None
    version, layer = (data[1] >> 3) & 0x3, (data[1] >> 1) & 0x3
    bitrate, rate = data[2] >> 4, (data[2] >> 2) & 0x3
    ok = version != 0x1 and layer == 0x1 and bitrate not in (0x0, 0xF) and rate != 0x3
    return "mp3" if ok else None


# ── ffmpeg ────────────────────────────────────────────────────────────────────


def ffmpeg_binary() -> str | None:
    """``BW_TTS_FFMPEG`` (a path or a command name) or ``ffmpeg`` on PATH."""
    override = options.config().ffmpeg
    if override:
        if os.path.isabs(override) and os.path.isfile(override) and os.access(override, os.X_OK):
            return override
        found = shutil.which(override)
        if found:
            return found
    return shutil.which("ffmpeg")


def ffmpeg_available() -> bool:
    return ffmpeg_binary() is not None


def encode_mp3(source: Path, target: Path, *, speed: float = 1.0) -> None:
    """Encode *source* as MP3 into *target*, optionally changing the tempo (pitch kept)."""
    binary = ffmpeg_binary()
    if not binary:
        raise SynthesisError("ffmpeg is not available.", retryable=False)
    if not 0.5 <= speed <= 2.0:
        raise SynthesisError(f"Unsupported speed {speed!r}.", retryable=False)
    command = [binary, "-y", "-nostdin", "-loglevel", "error", "-i", str(source), "-vn"]
    if abs(speed - 1.0) > 1e-6:
        command += ["-filter:a", f"atempo={speed:.4f}"]
    command += ["-acodec", "libmp3lame", "-q:a", "4", "-f", "mp3", str(target)]
    try:
        result = subprocess.run(command, capture_output=True, timeout=FFMPEG_TIMEOUT, check=False)
    except (OSError, subprocess.SubprocessError) as error:
        raise SynthesisError(f"ffmpeg could not run: {error}") from error
    if result.returncode != 0 or not target.is_file() or target.stat().st_size == 0:
        log.warning("ffmpeg failed (%s): %s", result.returncode, result.stderr[:400].decode("utf-8", "replace"))
        raise SynthesisError("ffmpeg could not encode the audio.")


# ── Stub ──────────────────────────────────────────────────────────────────────

_SILENT_MP3 = b"\xff\xfb\x90\x44\x00" + b"\x00" * 417


class StubBackend:
    """Writes one silent MP3 frame. For development and smoke tests only."""

    name = "stub"

    def problem(self) -> str | None:
        return None

    def synthesize(self, spoken: str, language: str, workdir: Path) -> Audio:
        path = _temp_path(workdir, ".mp3")
        path.write_bytes(_SILENT_MP3)
        return Audio(path, "mp3")


# ── Piper ─────────────────────────────────────────────────────────────────────

_voice_cache: dict[tuple[str, str], Any] = {}
_voice_lock = threading.Lock()
_download_lock = threading.Lock()


def piper_installed() -> bool:
    return importlib.util.find_spec("piper") is not None


def _system_specs() -> tuple[int, int]:
    cpus = os.cpu_count() or 1
    memory_mb = 0
    try:
        with open("/proc/meminfo", encoding="ascii") as handle:
            for line in handle:
                if line.startswith("MemTotal:"):
                    memory_mb = int(line.split()[1]) // 1024
                    break
    except (OSError, ValueError):
        pass
    return cpus, memory_mb


def synthesis_scales(mode: str) -> tuple[float, float, float]:
    """(length, noise, noise_w) for a performance mode; lower noise is faster."""
    if mode == "fast":
        return 1.0, 0.333, 0.55
    if mode == "balanced":
        return 1.0, 0.667, 0.8
    cpus, memory_mb = _system_specs()
    if cpus <= 4 or (memory_mb and memory_mb < 4000):
        return 1.0, 0.333, 0.55
    if cpus <= 8 or (memory_mb and memory_mb < 8000):
        return 1.0, 0.5, 0.65
    return 1.0, 0.667, 0.8


class PiperBackend:
    name = "piper"

    def __init__(self, cfg: options.TtsConfig, performance_mode: str = "auto"):
        self.cfg = cfg
        self.voice_dir = Path(cfg.piper_voice_dir)
        self.voices = {**DEFAULT_VOICES, **cfg.piper_voice_map}
        self.performance_mode = performance_mode

    def problem(self) -> str | None:
        if not piper_installed():
            return "Piper is not installed on the server (pip install piper-tts)."
        return None

    def voice_name(self, language: str) -> str | None:
        return self.voices.get(language) or self.voices.get(language.split("-", 1)[0])

    def voice_paths(self, language: str) -> tuple[str, Path, Path]:
        voice = self.voice_name(language)
        if not voice:
            raise SynthesisError(f"No Piper voice is configured for {language!r} (BW_TTS_PIPER_VOICE_MAP).",
                                 retryable=False)
        if voice.endswith(".onnx") and os.path.isabs(voice):
            model = Path(voice)  # operator-supplied absolute path (environment only)
        elif _VOICE_NAME.fullmatch(voice) and ".." not in voice:
            model = self.voice_dir / f"{voice}.onnx"
        else:
            raise SynthesisError(f"Invalid Piper voice name {voice!r}.", retryable=False)
        return voice, model, Path(f"{model}.json")

    def voice_state(self, language: str) -> str:
        """``ready``, ``downloadable``, ``missing`` or ``unmapped`` (admin page)."""
        if not self.voice_name(language):
            return "unmapped"
        try:
            _voice, model, config_path = self.voice_paths(language)
        except SynthesisError:
            return "unmapped"
        if model.is_file() and config_path.is_file():
            return "ready"
        return "downloadable" if self.cfg.piper_auto_download and not os.path.isabs(str(_voice)) else "missing"

    def _ensure_files(self, language: str) -> tuple[Path, Path]:
        voice, model, config_path = self.voice_paths(language)
        if model.is_file() and config_path.is_file():
            return model, config_path
        if os.path.isabs(voice) or not self.cfg.piper_auto_download:
            raise SynthesisError(
                f"The Piper voice {voice!r} is missing from {self.voice_dir}. Download it with "
                f"`python -m piper.download_voices --download-dir {self.voice_dir} {voice}`.", retryable=False)
        with _download_lock:
            if not (model.is_file() and config_path.is_file()):
                try:
                    from piper.download_voices import download_voice  # type: ignore[import-not-found]
                except ImportError as error:
                    raise SynthesisError("Piper is not installed on the server.", retryable=False) from error
                self.voice_dir.mkdir(parents=True, exist_ok=True)
                try:
                    download_voice(voice, self.voice_dir.resolve())
                except OSError as error:
                    raise SynthesisError(f"Cannot download the Piper voice {voice!r}: "
                                         f"{getattr(error, 'reason', None) or error}") from error
        if not (model.is_file() and config_path.is_file()):
            raise SynthesisError(f"Downloading the Piper voice {voice!r} did not produce its files.",
                                 retryable=False)
        return model, config_path

    def _voice(self, language: str) -> Any:
        model, config_path = self._ensure_files(language)
        key = (os.path.realpath(model), os.path.realpath(config_path))
        with _voice_lock:
            if key not in _voice_cache:
                try:
                    from piper import PiperVoice  # type: ignore[import-not-found]
                except ImportError as error:
                    raise SynthesisError("Piper is not installed on the server.", retryable=False) from error
                _voice_cache[key] = PiperVoice.load(str(model), config_path=str(config_path))
                log.info("Loaded Piper voice %s", model.name)
            return _voice_cache[key]

    def _syn_config(self) -> Any:
        from piper import SynthesisConfig  # type: ignore[import-not-found]

        length, noise, noise_w = synthesis_scales(self.performance_mode)
        return SynthesisConfig(
            length_scale=self.cfg.piper_length_scale or length,
            noise_scale=self.cfg.piper_noise_scale if self.cfg.piper_noise_scale is not None else noise,
            noise_w_scale=self.cfg.piper_noise_w_scale if self.cfg.piper_noise_w_scale is not None else noise_w,
        )

    def output_extension(self) -> str:
        fmt = self.cfg.piper_output_format
        if fmt == "auto":
            return "mp3" if ffmpeg_available() else "wav"
        return fmt

    def synthesize(self, spoken: str, language: str, workdir: Path) -> Audio:
        voice = self._voice(language)
        wav_path = _temp_path(workdir, ".wav")
        try:
            with wave.open(str(wav_path), "wb") as wav_file:
                voice.synthesize_wav(spoken, wav_file, syn_config=self._syn_config())
        except SynthesisError:
            wav_path.unlink(missing_ok=True)
            raise
        except Exception as error:  # noqa: BLE001 - any engine error fails this job only
            wav_path.unlink(missing_ok=True)
            raise SynthesisError(f"Piper failed: {error}") from error
        if wav_path.stat().st_size == 0:
            wav_path.unlink(missing_ok=True)
            raise SynthesisError("Piper produced an empty file.")
        if self.output_extension() == "wav":
            return Audio(wav_path, "wav")
        mp3_path = _temp_path(workdir, ".mp3")
        try:
            encode_mp3(wav_path, mp3_path)
        except SynthesisError:
            mp3_path.unlink(missing_ok=True)
            raise
        finally:
            wav_path.unlink(missing_ok=True)
        return Audio(mp3_path, "mp3")


# ── Remote GPU server ─────────────────────────────────────────────────────────


def gpu_base_url_problem(url: str) -> str | None:
    """Why *url* cannot be used as the GPU server base address (None if fine)."""
    if not url:
        return "No GPU server URL is configured."
    if "?" in url or "#" in url:
        return "The GPU server URL must not contain a query string or fragment."
    try:
        http.check_url(url, schemes=("http", "https"))
    except http.BlockedDestination as error:
        return str(error)
    return None


class RemoteGpuBackend:
    name = "remote-gpu"

    def __init__(self, url: str, token: str, timeout: int, *, transport=None, fallback: Synthesizer | None = None):
        self.url = url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.transport = transport or http.request
        self.fallback = fallback

    def problem(self) -> str | None:
        problem = gpu_base_url_problem(self.url)
        if problem:
            return problem
        if not self.token:
            return "No GPU server token is configured."
        if any(ord(char) < 33 or ord(char) > 126 for char in self.token):
            return "The GPU server token may only contain printable ASCII characters."
        return None

    def _call(self, method: str, path: str, *, body: bytes | None = None, max_bytes: int,
              timeout: float) -> http.HttpResponse:
        headers = {"Authorization": f"Bearer {self.token}", "Accept": "audio/mpeg, audio/wav, application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        return self.transport(
            method, self.url + path, headers=headers, body=body, timeout=min(timeout, 600),
            total_timeout=min(timeout + 30, 3600), max_bytes=max_bytes, allow_private=True,
            schemes=("http", "https"),
        )

    def health(self) -> dict[str, Any]:
        """The server's authenticated health report (admin "test connection")."""
        problem = self.problem()
        if problem:
            raise SynthesisError(problem, retryable=False)
        try:
            response = self._call("GET", "/health", max_bytes=64 * 1024, timeout=10)
        except http.HttpError as error:
            raise SynthesisError(f"Cannot reach the GPU server: {error}") from error
        if not response.ok:
            raise SynthesisError(f"The GPU server answered HTTP {response.status}.")
        try:
            data = json.loads(response.body.decode("utf-8"))
        except ValueError as error:
            raise SynthesisError("The GPU server did not answer with JSON.") from error
        return data if isinstance(data, dict) else {}

    def _remote(self, spoken: str, language: str, workdir: Path) -> Audio:
        problem = self.problem()
        if problem:
            raise SynthesisError(problem, retryable=False)
        body = json.dumps({"text": spoken, "language": language}).encode("utf-8")
        try:
            response = self._call("POST", "/synthesize", body=body, max_bytes=MAX_REMOTE_AUDIO_BYTES,
                                  timeout=self.timeout)
        except http.BlockedDestination as error:
            raise SynthesisError(f"Refusing to contact the GPU server: {error}", retryable=False) from error
        except http.HttpError as error:
            raise SynthesisError(f"Cannot reach the GPU server: {error}") from error
        if response.status == 429:
            raise SynthesisError("The GPU server is rate limiting requests (HTTP 429).", rate_limited=True)
        if not response.ok:
            detail = response.body[:200].decode("utf-8", "replace")
            raise SynthesisError(f"The GPU server answered HTTP {response.status}: {detail}",
                                 retryable=response.status >= 500 or response.status == 408)
        kind = looks_like_audio(response.body)
        if kind is None or len(response.body) < 100:
            raise SynthesisError("The GPU server did not return MP3 or WAV audio.")
        path = _temp_path(workdir, f".{kind}")
        path.write_bytes(response.body)
        return Audio(path, kind)

    def synthesize(self, spoken: str, language: str, workdir: Path) -> Audio:
        try:
            return self._remote(spoken, language, workdir)
        except SynthesisError as error:
            if self.fallback is None or self.fallback.problem() is not None:
                raise
            log.warning("Remote GPU speech failed (%s); falling back to %s", error, self.fallback.name)
            return self.fallback.synthesize(spoken, language, workdir)


# ── Selection ─────────────────────────────────────────────────────────────────


def current() -> Synthesizer:
    """The backend for the current settings (tests may install their own)."""
    override = current_app.extensions.get("bananawiki.tts.synthesizer")
    if override is not None:
        return override
    cfg = options.config()
    name = options.backend_name()
    if name == "stub":
        return StubBackend()
    piper = PiperBackend(cfg, options.performance_mode())
    if name == "remote-gpu":
        url, token, timeout = options.gpu_settings()
        return RemoteGpuBackend(url, token, timeout, fallback=piper)
    return piper
