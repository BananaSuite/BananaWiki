# BananaWiki GPU speech server

A small HTTP service that runs [Piper](https://github.com/OHF-Voice/piper1-gpl) voices on an
NVIDIA GPU for one or more BananaWiki wikis. The wiki sends a page's text, the server answers
with MP3 (or WAV) audio. If the server cannot be reached, the wiki falls back to Piper on its
own machine.

It replaces the 1.4 `banana-tts-gpu/` service and speaks the same protocol, so an existing
wiki configuration keeps working.

## Install

On the GPU machine (Ubuntu/Debian with the NVIDIA driver, CUDA and cuDNN installed):

```sh
sudo ./install.sh                  # listens on the Tailscale address, or 127.0.0.1
sudo ./install.sh --host 10.0.0.5  # a specific private address
sudo ./install.sh --cpu            # no GPU: CPU onnxruntime
sudo ./install.sh --uninstall
```

The installer creates the `bananawiki-tts` user, a virtualenv in `/opt/bananawiki-tts-gpu`,
the root-only environment file `/etc/bananawiki-tts-gpu.env` with a random `TTS_AUTH_TOKEN`,
and the systemd unit `bananawiki-tts-gpu`.

Manual run: `pip install -r requirements.txt`, then
`TTS_AUTH_TOKEN=... TTS_HOST=100.64.0.2 python server.py`.

Never expose the server to the internet: bind it to a private address (a LAN, a VPN such as
Tailscale or WireGuard) and firewall the port.

## Connect a wiki

In the wiki: **Admin → Read aloud → Remote GPU server**. Enter the address
(`http://100.64.0.2:8787`) and the token, tick *Use the GPU server*, save, and press
*Test the GPU server*. Alternatively set `BW_TTS_REMOTE_GPU_URL`,
`BW_TTS_REMOTE_GPU_AUTH_TOKEN` and optionally `BW_TTS_REMOTE_GPU_TIMEOUT` (seconds) in the
wiki's environment; they take precedence (and on managed hosting they are the only source).

The wiki only connects to loopback, private, and shared (100.64.0.0/10) or public addresses;
link-local and metadata addresses are refused, and redirects are never followed.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `TTS_AUTH_TOKEN` | (required, ≥ 16 characters) | Master bearer token. A BananaWiki hosting platform never hands it to its wikis: each hosted wiki gets its own token `bwt1.<wiki id>.<hex>` derived from it, which this server checks too. |
| `TTS_REVOKED_TENANTS` | | Hosted wiki ids (comma-separated) whose derived tokens are refused |
| `TTS_HOST` / `TTS_PORT` | `127.0.0.1` / `8787` | Listen address |
| `PIPER_VOICE_DIR` | `./voices` | Voice models (`<voice>.onnx` + `.onnx.json`) |
| `TTS_AUTO_DOWNLOAD` | `1` | Download missing voices on first use |
| `TTS_VOICE_MAP` | | Extra voices: `de=de_DE-thorsten-high,pt=pt_BR-faber-medium` |
| `TTS_PRELOAD` | `en,it` | Voices loaded at start |
| `TTS_USE_CUDA` | `1` | Run on the GPU |
| `TTS_MAX_LOADED_VOICES` | `8` | Voices kept in (GPU) memory |
| `TTS_MAX_CONCURRENT` | `1` | Simultaneous syntheses |
| `TTS_QUEUE_TIMEOUT` | `30` | Seconds a request waits for a free slot before `503` |
| `TTS_MAX_INPUT_CHARS` | `20000` | Longer text is refused with `413` (the wiki sends at most 20000) |
| `TTS_MAX_BODY_BYTES` | `262144` | Larger request bodies are refused with `413` |
| `TTS_OUTPUT_FORMAT` | `mp3` | `mp3` (needs ffmpeg) or `wav` |
| `TTS_FFMPEG` | | Path to ffmpeg if it is not on `PATH` |
| `TTS_LENGTH_SCALE`, `TTS_NOISE_SCALE`, `TTS_NOISE_W_SCALE` | `1.0`, `0.667`, `0.8` | Piper voice parameters |
| `TTS_LOG_LEVEL` | `INFO` | Logging |

## API

* `GET /health` → `{"status": "ok", "version": "1.6.0"}`. With the bearer token it also
  reports `gpu_available`, `cuda_enabled`, `loaded_voices` and `output_format`.
* `POST /synthesize` with `Authorization: Bearer <token>`, `Content-Type: application/json`
  and `{"text": "...", "language": "en"}` → `200` with `audio/mpeg` (or `audio/wav`).
  Errors: `401`/`403` token, `400` bad input or no voice for the language, `411` missing
  length, `413` too large, `415` not JSON, `503` busy (with `Retry-After`), `500` synthesis
  failed. The wiki retries `5xx` answers with backoff and postpones on `429`.

## Changes from 1.4

* Standard-library HTTP server (no FastAPI/uvicorn); synthesis runs in request threads with a
  concurrency limit instead of blocking an event loop, so `/health` always answers.
* Over-long text is refused instead of silently cut.
* `/health` without a token no longer discloses the voice directory or loaded voices.
