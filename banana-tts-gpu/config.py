"""Configuration for the BananaWiki GPU TTS server.

All settings are driven by environment variables so the same code works
across dev, staging, and production without config file edits.
"""

import os

# ── Server

# Loopback by default. To serve wikis on other machines, bind the private
# interface they reach it on (for example the Tailscale address), not
# 0.0.0.0, so the service is not exposed on every network the host is on.
HOST = os.environ.get("TTS_HOST", "127.0.0.1").strip() or "127.0.0.1"
PORT = int(os.environ.get("TTS_PORT", "8787"))

# ── Authentication

# Every /synthesize request must carry this token. The server refuses to start
# without one; it used to invent a random token, which left it running with a
# secret nobody had configured.
AUTH_TOKEN = os.environ.get("TTS_AUTH_TOKEN", "").strip()
if not AUTH_TOKEN:
    raise SystemExit(
        "TTS_AUTH_TOKEN is not set. Generate one (for example with "
        "`python3 -c 'import secrets; print(secrets.token_hex(32))'`), set it "
        "here and give the same value to BananaWiki."
    )

# ── Piper / GPU

VOICE_DIR = os.environ.get(
    "PIPER_VOICE_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "voices"),
)

# Use CUDA when available.  Falls back to CPU silently if onnxruntime-gpu
# is not installed or CUDA is not detected.
USE_CUDA = os.environ.get("TTS_USE_CUDA", "1").strip().lower() in (
    "1", "true", "yes", "on",
)

# Maximum number of voice models to keep loaded in memory.
# Each model consumes ~50-150 MB of VRAM depending on the voice.
MAX_LOADED_VOICES = int(os.environ.get("TTS_MAX_LOADED_VOICES", "8"))

# ── Synthesis

# Piper synthesis parameters (passed as SynthesisConfig).
LENGTH_SCALE = float(os.environ.get("TTS_LENGTH_SCALE", "1.0"))
NOISE_SCALE = float(os.environ.get("TTS_NOISE_SCALE", "0.667"))
NOISE_W_SCALE = float(os.environ.get("TTS_NOISE_W_SCALE", "0.8"))

# Maximum input text length (characters).  Matches BananaWiki's limit.
MAX_INPUT_CHARS = int(os.environ.get("TTS_MAX_INPUT_CHARS", "20000"))

# Output format: "mp3" or "wav".  MP3 requires ffmpeg on the system.
OUTPUT_FORMAT = os.environ.get("TTS_OUTPUT_FORMAT", "mp3").strip().lower()

# ── Logging

LOG_LEVEL = os.environ.get("TTS_LOG_LEVEL", "INFO").upper()
