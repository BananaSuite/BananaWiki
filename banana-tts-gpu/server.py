"""BananaWiki GPU TTS Server
===========================

A lightweight FastAPI service that runs Piper TTS with GPU acceleration.
Designed to be called by BananaWiki instances over a private Tailscale network.

Endpoints:
    GET  /health: Health check + GPU status
    POST /synthesize: Accept {text, language}, return MP3 audio

Authentication:
    Bearer token via Authorization header.  Token is set by the TTS_AUTH_TOKEN
    env var; the server refuses to start without it.
"""

from __future__ import annotations

import hmac
import io
import logging
import os
import subprocess
import threading
import wave
from collections import OrderedDict
from pathlib import Path
from typing import Optional

import config

logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("banana-tts-gpu")

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

app = FastAPI(
    title="BananaWiki GPU TTS",
    description="Piper neural TTS with GPU acceleration for BananaWiki.",
    version="1.0.0",
)


class VoiceCache:
    """LRU cache of loaded PiperVoice objects, bounded by count.

    Each cached voice holds its ONNX model in GPU VRAM.  When the cache
    is full the least-recently-used voice is evicted (its VRAM is freed
    when the Python object is garbage-collected).
    """

    def __init__(self, max_size: int = 8):
        self._max_size = max_size
        self._cache: OrderedDict[str, object] = OrderedDict()
        self._lock = threading.Lock()
        # Pre-populate the voice map from config.
        self._voice_map = _build_voice_map()

    def get_voice(self, language: str):
        """Return a loaded PiperVoice for *language*, loading it if needed."""
        lang = language.strip().lower()
        # Try family fallback: "fr-CA" -> "fr"
        candidates = [lang]
        if "-" in lang:
            candidates.append(lang.split("-", 1)[0])

        with self._lock:
            for candidate in candidates:
                if candidate in self._cache:
                    self._cache.move_to_end(candidate)
                    return self._cache[candidate]

        # Load outside the lock (model loading is slow).
        for candidate in candidates:
            voice_name = self._voice_map.get(candidate)
            if voice_name:
                voice = _load_voice(voice_name, candidate)
                with self._lock:
                    if len(self._cache) >= self._max_size:
                        self._cache.popitem(last=False)
                    self._cache[candidate] = voice
                return voice

        return None

    def loaded_languages(self) -> list[str]:
        """Return language codes currently held in cache."""
        with self._lock:
            return list(self._cache.keys())

    def preload(self, languages: list[str]):
        """Warm the cache with specific languages."""
        for lang in languages:
            try:
                self.get_voice(lang)
            except Exception:
                logger.warning("Failed to preload voice for %s", lang, exc_info=True)


def _build_voice_map() -> dict[str, str]:
    """Return {language_code: piper_voice_name} from BananaWiki's defaults."""
    # Same mapping BananaWiki uses: keeps voice names consistent.
    return {
        "ar": "ar_JO-kareem-medium",
        "bg": "bg_BG-dimitar-medium",
        "ca": "ca_ES-upc_ona-medium",
        "cs": "cs_CZ-jirka-medium",
        "cy": "cy_GB-bu_tts-medium",
        "da": "da_DK-talesyntese-medium",
        "de": "de_DE-thorsten-medium",
        "el": "el_GR-rapunzelina-medium",
        "en": "en_US-lessac-medium",
        "es": "es_ES-sharvard-medium",
        "eu": "eu_ES-maider-medium",
        "fa": "fa_IR-amir-medium",
        "fi": "fi_FI-harri-medium",
        "fr": "fr_FR-tom-medium",
        "hi": "hi_IN-pratham-medium",
        "hu": "hu_HU-anna-medium",
        "id": "id_ID-news_tts-medium",
        "is": "is_IS-salka-medium",
        "it": "it_IT-paola-medium",
        "ka": "ka_GE-natia-medium",
        "kk": "kk_KZ-issai-high",
        "ku": "ku_TR-berfin_renas-medium",
        "lb": "lb_LU-marylux-medium",
        "lv": "lv_LV-aivars-medium",
        "ml": "ml_IN-meera-medium",
        "ne": "ne_NP-google-medium",
        "nl": "nl_NL-mls-medium",
        "no": "no_NO-talesyntese-medium",
        "pl": "pl_PL-gosia-medium",
        "pt": "pt_BR-cadu-medium",
        "pt-PT": "pt_PT-tugao-medium",
        "ro": "ro_RO-mihai-medium",
        "ru": "ru_RU-irina-medium",
        "sk": "sk_SK-lili-medium",
        "sl": "sl_SI-artur-medium",
        "sq": "sq_AL-edon-medium",
        "sr": "sr_RS-serbski_institut-medium",
        "sv": "sv_SE-lisa-medium",
        "sw": "sw_CD-lanfrica-medium",
        "te": "te_IN-maya-medium",
        "tr": "tr_TR-dfki-medium",
        "uk": "uk_UA-mykyta-high",
        "ur": "ur_PK-fasih-medium",
        "vi": "vi_VN-vais1000-medium",
        "zh": "zh_CN-huayan-medium",
        "zh-CN": "zh_CN-huayan-medium",
        "zh-TW": "zh_CN-huayan-medium",
        "yue": "zh_CN-huayan-medium",
    }


def _load_voice(voice_name: str, language: str):
    """Download (if needed) and load a PiperVoice with GPU acceleration."""
    from piper import PiperVoice

    voice_dir = Path(config.VOICE_DIR)
    voice_dir.mkdir(parents=True, exist_ok=True)

    model_path = voice_dir / f"{voice_name}.onnx"
    config_path = voice_dir / f"{voice_name}.onnx.json"

    # Auto-download missing voice models.
    if not model_path.exists() or not config_path.exists():
        logger.info("Downloading voice model: %s", voice_name)
        try:
            from piper.download_voices import download_voice
            download_voice(voice_name, voice_dir)
        except ImportError as err:
            raise RuntimeError(
                f"piper.download_voices not available. Manually download "
                f"{voice_name} to {voice_dir}"
            ) from err

    if not model_path.exists():
        raise RuntimeError(
            f"Voice model not found after download: {model_path}"
        )

    logger.info(
        "Loading voice %s (cuda=%s) for language %s",
        voice_name, config.USE_CUDA, language,
    )
    voice = PiperVoice.load(
        str(model_path),
        config_path=str(config_path) if config_path.exists() else None,
        use_cuda=config.USE_CUDA,
    )
    logger.info("Voice %s loaded successfully", voice_name)
    return voice


_synthesis_config = None
_synthesis_config_lock = threading.Lock()


def _get_synthesis_config():
    global _synthesis_config
    if _synthesis_config is None:
        with _synthesis_config_lock:
            if _synthesis_config is None:
                from piper import SynthesisConfig
                _synthesis_config = SynthesisConfig(
                    length_scale=config.LENGTH_SCALE,
                    noise_scale=config.NOISE_SCALE,
                    noise_w_scale=config.NOISE_W_SCALE,
                )
    return _synthesis_config


def verify_token(authorization: Optional[str] = Header(None)):
    """Verify the Bearer token in the Authorization header."""
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing Authorization header")
    parts = authorization.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(status_code=401, detail="Invalid Authorization format")
    # Constant-time comparison, so response timing does not reveal how much of
    # a guessed token matched. Compare bytes: compare_digest only accepts
    # ASCII str values and would raise on anything else a client sends.
    presented = parts[1].encode("utf-8")
    if not hmac.compare_digest(presented, config.AUTH_TOKEN.encode("utf-8")):
        raise HTTPException(status_code=403, detail="Invalid token")


voice_cache = VoiceCache(max_size=config.MAX_LOADED_VOICES)


class SynthesizeRequest(BaseModel):
    text: str
    language: str = "en"


@app.get("/health")
async def health():
    """Health check endpoint."""
    gpu_available = False
    try:
        import onnxruntime
        providers = onnxruntime.get_available_providers()
        gpu_available = "CUDAExecutionProvider" in providers
    except Exception:
        pass

    return {
        "status": "ok",
        "gpu_available": gpu_available,
        "cuda_enabled": config.USE_CUDA,
        "loaded_voices": voice_cache.loaded_languages(),
        "voice_dir": config.VOICE_DIR,
        "output_format": config.OUTPUT_FORMAT,
    }


@app.post("/synthesize")
async def synthesize(req: SynthesizeRequest, _auth=Depends(verify_token)):
    """Synthesize text to audio.

    Returns MP3 bytes with Content-Type: audio/mpeg.
    """
    text = (req.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Text is empty")
    if len(text) > config.MAX_INPUT_CHARS:
        text = text[:config.MAX_INPUT_CHARS]

    language = req.language.strip().lower() or "en"

    voice = voice_cache.get_voice(language)
    if voice is None:
        raise HTTPException(
            status_code=400,
            detail=f"No voice model available for language '{language}'",
        )

    # Synthesize to WAV in memory.
    wav_buffer = io.BytesIO()
    try:
        with wave.open(wav_buffer, "wb") as wav_file:
            voice.synthesize_wav(
                text,
                wav_file,
                syn_config=_get_synthesis_config(),
            )
    except Exception as exc:
        logger.error("Piper synthesis failed: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=500, detail=f"Synthesis failed: {exc}"
        ) from exc

    wav_bytes = wav_buffer.getvalue()
    if len(wav_bytes) == 0:
        raise HTTPException(status_code=500, detail="Piper produced empty audio")

    # Convert to MP3 if requested.
    if config.OUTPUT_FORMAT == "mp3":
        audio_bytes = _wav_to_mp3(wav_bytes)
    else:
        audio_bytes = wav_bytes

    logger.info(
        "Synthesized %d chars in %s -> %d bytes (%s)",
        len(text), language, len(audio_bytes), config.OUTPUT_FORMAT,
    )

    content_type = "audio/mpeg" if config.OUTPUT_FORMAT == "mp3" else "audio/wav"
    return Response(content=audio_bytes, media_type=content_type)


def _wav_to_mp3(wav_bytes: bytes) -> bytes:
    """Convert WAV bytes to MP3 via ffmpeg."""
    ffmpeg = _find_ffmpeg()
    if not ffmpeg:
        raise HTTPException(
            status_code=500,
            detail="ffmpeg not found: cannot convert to MP3",
        )

    try:
        result = subprocess.run(
            [
                ffmpeg, "-y",
                "-loglevel", "error",
                "-i", "pipe:0",
                "-vn",
                "-acodec", "libmp3lame",
                "-q:a", "4",
                # A pipe has no file extension to guess the container from;
                # without -f ffmpeg refuses to open the output at all.
                "-f", "mp3",
                "pipe:1",
            ],
            input=wav_bytes,
            capture_output=True,
            timeout=60,
        )
        if result.returncode != 0:
            stderr = result.stderr.decode("utf-8", errors="replace")[:400]
            raise RuntimeError(f"ffmpeg failed: {stderr}")
        return result.stdout
    except subprocess.TimeoutExpired as err:
        raise HTTPException(status_code=500, detail="ffmpeg conversion timed out") from err
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"ffmpeg error: {exc}") from exc


def _find_ffmpeg():
    """Return path to ffmpeg binary."""
    override = os.environ.get("TTS_FFMPEG", "").strip()
    if override and os.path.isfile(override) and os.access(override, os.X_OK):
        return override
    import shutil
    return shutil.which("ffmpeg")


@app.on_event("startup")
async def startup():
    """Log server info and optionally preload common voices."""
    logger.info("BananaWiki GPU TTS Server starting")
    logger.info("  Voice dir:  %s", config.VOICE_DIR)
    logger.info("  CUDA:       %s", config.USE_CUDA)
    logger.info("  Output:     %s", config.OUTPUT_FORMAT)
    logger.info("  Auth:       token-based (TTS_AUTH_TOKEN)")

    # Preload English and Italian (the BananaWiki defaults) so the first
    # request doesn't incur a model-loading delay.
    try:
        voice_cache.preload(["en", "it"])
        logger.info("Preloaded default voices: en, it")
    except Exception:
        logger.warning("Failed to preload default voices", exc_info=True)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "server:app",
        host=config.HOST,
        port=config.PORT,
        log_level=config.LOG_LEVEL.lower(),
        workers=1,  # Single worker: Piper is not fork-safe
    )
