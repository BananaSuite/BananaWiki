"""Read-aloud configuration: ``BW_TTS_*`` environment variables and site settings.

Environment variables are read once when the application starts. Unlike the
core configuration, an invalid ``BW_TTS_*`` value only logs a warning and
falls back to its default: a typo in an optional feature must not keep the
wiki (or the worker) from starting.

Site settings (``site_settings.tts_*``) are read per request/job, so changes
made on the admin page apply without a restart.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass, field

from flask import current_app

from ....core.env import ConfigError, Env
from ... import settings
from . import text

log = logging.getLogger("bananawiki.tts")

BACKENDS = frozenset({"piper", "stub", "remote-gpu"})
PERFORMANCE_MODES = ("auto", "balanced", "fast")
OUTPUT_FORMATS = frozenset({"auto", "wav", "mp3"})
GPU_DEFAULT_TIMEOUT = 120
GPU_MAX_TIMEOUT = 3600
MAX_WORKERS = 8


@dataclass(frozen=True)
class TtsConfig:
    folder: str
    piper_voice_dir: str
    backend: str = ""  # empty: chosen from site settings
    manual_max_active_jobs: int = 1
    manual_max_active_per_user: int = 1
    worker_count: int = 1
    inline_worker: bool = False
    min_start_interval: float = 0.25
    max_auto_resume_attempts: int = 3
    resume_base_delay: float = 2.0
    resume_max_delay: float = 30.0
    rate_limit_cooldown: float = 900.0
    shutdown_grace: float = 20.0
    piper_auto_download: bool = True
    piper_output_format: str = "auto"
    piper_length_scale: float | None = None
    piper_noise_scale: float | None = None
    piper_noise_w_scale: float | None = None
    piper_voice_map: Mapping[str, str] = field(default_factory=dict)
    performance_mode: str = ""  # empty: the site setting decides
    ffmpeg: str = ""
    gpu_url: str = ""
    gpu_token: str = field(default="", repr=False)
    gpu_timeout: int | None = None
    host_disabled: bool = False  # BW_MANAGED_TTS_DISABLED
    gpu_managed_by_host: bool = False  # managed hosting: GPU settings come from the host only


class _Lenient:
    """Typed env reads that warn and use the default on invalid values."""

    def __init__(self, environ: Mapping[str, str] | None):
        self.env = Env(environ)

    def _safe(self, name: str, default, read):
        try:
            return read()
        except ConfigError as error:
            log.warning("Ignoring %s: %s", name, error)
            return default

    def str(self, name: str, default: str = "") -> str:
        return self.env.str(name, default)

    def bool(self, name: str, default: bool) -> bool:
        return self._safe(name, default, lambda: self.env.bool(name, default))

    def int(self, name: str, default: int, minimum: int, maximum: int | None = None) -> int:
        def read() -> int:
            value = self.env.int(name, default)
            return max(minimum, value if maximum is None else min(maximum, value))
        return self._safe(name, default, read)

    def float(self, name: str, default: float | None, minimum: float = 0.0) -> float | None:
        if not self.env.str(name):
            return default
        return self._safe(name, default, lambda: max(minimum, self.env.float(name, default or 0.0)))


def parse_voice_map(raw: str) -> dict[str, str]:
    """``BW_TTS_PIPER_VOICE_MAP`` as JSON or ``code=voice,code=voice``."""
    raw = (raw or "").strip()
    if not raw:
        return {}
    pairs: dict[str, object] = {}
    if raw.startswith("{"):
        try:
            loaded = json.loads(raw)
        except ValueError:
            log.warning("Ignoring BW_TTS_PIPER_VOICE_MAP: not valid JSON")
            return {}
        pairs = loaded if isinstance(loaded, dict) else {}
    else:
        for part in raw.split(","):
            key, sep, voice = part.partition("=")
            if sep:
                pairs[key.strip()] = voice.strip()
    result = {}
    for key, voice in pairs.items():
        code = text.normalize_language(key)
        if code and str(voice).strip():
            result[code] = str(voice).strip()
    return result


def gpu_timeout(value: object) -> int | None:
    try:
        seconds = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return min(seconds, GPU_MAX_TIMEOUT) if seconds > 0 else None


def load(cfg, environ: Mapping[str, str] | None = None) -> TtsConfig:
    """Build the TTS configuration from the environment (``os.environ`` by default)."""
    env = _Lenient(environ if environ is not None else os.environ)
    backend = env.str("BW_TTS_BACKEND").lower()
    backend = {"local": "piper", "offline": "piper"}.get(backend, backend)
    if backend and backend not in BACKENDS:
        log.warning("Ignoring BW_TTS_BACKEND=%r (use piper, remote-gpu or stub)", backend)
        backend = ""
    output = env.str("BW_TTS_PIPER_OUTPUT_FORMAT", "auto").lower()
    performance = env.str("BW_TTS_PERFORMANCE_MODE").lower()
    return TtsConfig(
        folder=cfg.folders.tts,
        piper_voice_dir=cfg.folders.piper_voices,
        backend=backend,
        manual_max_active_jobs=env.int("BW_TTS_MANUAL_MAX_ACTIVE_JOBS", 1, 1),
        manual_max_active_per_user=env.int("BW_TTS_MANUAL_MAX_ACTIVE_PER_USER", 1, 1),
        worker_count=env.int("BW_TTS_WORKER_COUNT", 1, 1, MAX_WORKERS),
        inline_worker=env.bool("BW_TTS_INLINE_WORKER", False),
        min_start_interval=env.float("BW_TTS_MIN_START_INTERVAL_SECONDS", 0.25) or 0.0,
        max_auto_resume_attempts=env.int("BW_TTS_MAX_AUTO_RESUME_ATTEMPTS", 3, 0, 50),
        resume_base_delay=env.float("BW_TTS_AUTO_RESUME_BASE_DELAY_SECONDS", 2.0) or 0.0,
        resume_max_delay=env.float("BW_TTS_AUTO_RESUME_MAX_DELAY_SECONDS", 30.0) or 0.0,
        rate_limit_cooldown=env.float("BW_TTS_RATE_LIMIT_COOLDOWN_SECONDS", 900.0) or 0.0,
        shutdown_grace=env.float("BW_TTS_SHUTDOWN_GRACE_SECONDS", 20.0) or 0.0,
        piper_auto_download=env.bool("BW_TTS_PIPER_AUTO_DOWNLOAD", True),
        piper_output_format=output if output in OUTPUT_FORMATS else "auto",
        piper_length_scale=env.float("BW_TTS_PIPER_LENGTH_SCALE", None, 0.1),
        piper_noise_scale=env.float("BW_TTS_PIPER_NOISE_SCALE", None),
        piper_noise_w_scale=env.float("BW_TTS_PIPER_NOISE_W_SCALE", None),
        piper_voice_map=parse_voice_map(env.str("BW_TTS_PIPER_VOICE_MAP")),
        performance_mode=performance if performance in PERFORMANCE_MODES else "",
        ffmpeg=env.str("BW_TTS_FFMPEG"),
        gpu_url=env.str("BW_TTS_REMOTE_GPU_URL").rstrip("/"),
        gpu_token=env.str("BW_TTS_REMOTE_GPU_AUTH_TOKEN"),
        gpu_timeout=gpu_timeout(env.str("BW_TTS_REMOTE_GPU_TIMEOUT")),
        host_disabled=cfg.managed_tts_disabled,
        gpu_managed_by_host=cfg.managed_hosting,
    )


def config() -> TtsConfig:
    app = current_app
    loaded = app.extensions.get("bananawiki.tts.config")
    if loaded is None:
        loaded = load(app.config["BW"])
        app.extensions["bananawiki.tts.config"] = loaded
    return loaded


# ── Site settings ─────────────────────────────────────────────────────────────


def host_disabled() -> bool:
    """The host switched generation off; existing audio stays playable."""
    return config().host_disabled


def panel_enabled() -> bool:
    return bool(settings.get("tts_page_panel_enabled"))


def public_access() -> bool:
    """Anonymous visitors may listen (public mode, panel and public TTS all on)."""
    return settings.public_mode_active() and panel_enabled() and bool(settings.get("tts_public_access_enabled", 1))


def auto_generate() -> bool:
    return bool(settings.get("tts_auto_generate_enabled"))


def enabled_languages() -> tuple[str, ...]:
    return text.parse_enabled_languages(settings.get("tts_enabled_languages"))


def performance_mode() -> str:
    mode = config().performance_mode or str(settings.get("tts_performance_mode") or "auto").lower()
    return mode if mode in PERFORMANCE_MODES else "auto"


def gpu_settings() -> tuple[str, str, int]:
    """``(url, token, timeout)`` for the remote GPU server.

    Environment variables win. On managed hosting only they count: the
    tenant's ``tts_gpu_*`` columns are ignored so a wiki admin cannot send
    the platform's token (or the wiki's requests) to a server of their choice.
    """
    cfg = config()
    url, token, timeout = cfg.gpu_url, cfg.gpu_token, cfg.gpu_timeout
    if not cfg.gpu_managed_by_host and not (url and token):
        url = url or str(settings.get("tts_gpu_url") or "").strip().rstrip("/")
        token = token or str(settings.get("tts_gpu_auth_token") or "").strip()
        timeout = timeout or gpu_timeout(settings.get("tts_gpu_timeout"))
    return url, token, timeout or GPU_DEFAULT_TIMEOUT


def backend_name() -> str:
    cfg = config()
    if cfg.backend:
        return cfg.backend
    if cfg.gpu_managed_by_host:
        url, token, _ = gpu_settings()
        return "remote-gpu" if url and token else "piper"
    if settings.get("tts_gpu_enabled") and str(settings.get("tts_gpu_url") or cfg.gpu_url).strip():
        return "remote-gpu"
    return "piper"
