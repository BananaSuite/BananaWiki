# Read aloud (text-to-speech)

The `tts` feature adds an audio player under every page. A reader presses
**Generate**; a worker turns the page text into speech with a neural
[Piper](https://github.com/OHF-Voice/piper1-gpl) voice, on the wiki's own
server or on a separate GPU server, and the audio is kept until the page
changes. Readers can choose a speed (0.75× to 2×) and download the audio.

Nothing here is required: without Piper the wiki works normally and
**Admin → Read aloud** says what is missing.

## How it works

1. **Generate** (or an edit, when automatic generation is on) only adds a
   row to `tts_generations`.
2. A worker picks up queued rows, synthesises the text, converts it to MP3
   when ffmpeg is available, and stores the file in `BW_TTS_FOLDER`.
3. Editing a page invalidates its audio; a page that had audio gets fresh
   audio queued. Deleting a page removes its files.
4. The `tts.sweep` job (every 6 hours) deletes files no row refers to and
   refreshes audio that fell out of date.

Audio made by 1.4 stays valid: the spoken text and its hash are computed
exactly as before (text beyond the 1,000,000 characters a page can hold is
ignored), in time proportional to the length of the page.

## Install

* **Piper**: `piper-tts` is part of `requirements.txt` (managed servers and the
  container image have it). From source: `python -m pip install -e '.[tts]'`.
* **ffmpeg** (optional, for MP3 and speed-changed downloads):
  `apt install ffmpeg`, or point `BW_TTS_FFMPEG` at it.
  The Wiki and tenant container images include a source-built FFmpeg 9.0.2,
  verified with the upstream release signature and a pinned SHA256. It keeps
  WAV/MP3 speech input, all supported PCM formats, MP3 output and pitch-preserving
  speed conversion; only file and pipe protocols are compiled. Other media
  formats and FFmpeg network protocols are excluded. The build recipe is in
  `docker/media/`; matching source archives, effective configuration, licenses
  and redistribution instructions are in each image at
  `/usr/local/share/bananawiki/media`.
  Wiki/desktop speech, the GPU server and containers disable ONNX Runtime's
  optional diagnostic telemetry (`ORT_DISABLE_TELEMETRY=1`) before Piper loads.
  An explicitly supplied operator setting is preserved. Local speech synthesis
  remains available without an external diagnostic service or device probe.
* **Voices** are downloaded on first use into `BW_TTS_PIPER_VOICE_DIR` (default
  `<instance>/piper-voices`). On a server without internet access set
  `BW_TTS_PIPER_AUTO_DOWNLOAD=0` and download them beforehand, for example
  `python -m piper.download_voices --download-dir <voice dir> it_IT-paola-medium`.
  English uses `en_US-lessac-medium` and Italian `it_IT-paola-medium`; many
  other languages have a default voice, and `BW_TTS_PIPER_VOICE_MAP` adds or
  replaces voices.
* **Language detection**: a built-in heuristic for English and Italian is
  always available. `langdetect` improves detection for other languages; it is
  no longer installed by default because it is only published as a source
  package (`python -m pip install langdetect==1.0.9` where building it is
  acceptable).

## The worker

Run **one** of:

* the separate process `python scripts/tts_worker.py` (managed servers run it
  as `bananawiki-tts.service`; containers of the hosting platform run it next
  to the wiki). Flags: `--once`, `--max-jobs N`, `--poll-interval S`,
  `--threads N`, `--skip-recovery`. It idles, instead of exiting, while the
  feature is off or Piper is missing, so systemd never sees a crash loop.
* threads inside the web workers: `BW_TTS_INLINE_WORKER=1`. Simpler (Docker
  Compose, small wikis), but synthesis then competes with web requests.

Several workers can run at once: jobs are claimed in the database, and jobs of
a worker that stopped are put back in the queue. `BW_TTS_WORKER_COUNT` sets the
threads per worker.

### Limits

One page cannot stop the worker or hold the queue for good:

* Piper reads the text in pieces of at most 500 characters, cut after a
  sentence where possible. Its memory grows with the length of a sentence,
  and a page may hold 20,000 characters without a full stop.
* On Linux (any POSIX system) Piper runs in a child process of the worker,
  started for each job, so the voice is loaded for every page. If it runs out
  of memory, crashes or runs longer than `BW_TTS_MAX_JOB_SECONDS` (default 3600),
  only that job fails. `BW_TTS_PIPER_MEMORY_MB` also limits its address space
  (`RLIMIT_AS`), so it fails before the server runs out of memory. The limit
  is off by default because ONNX Runtime reserves more address space than it
  uses, more on servers with many CPU cores: try a value (for example 4096)
  on your server before you rely on it. A lower `BW_MEMORY_LIMIT_MB` applies
  as well. On Windows and in the packaged desktop app Piper runs inside the
  worker.
* No job runs longer than `BW_TTS_MAX_JOB_SECONDS`: its lease is no longer
  renewed and the job fails ("The job ran longer than ..."). With a GPU
  server, keep it above `BW_TTS_REMOTE_GPU_TIMEOUT`.
* A job whose worker stopped while reading it (killed, crashed) goes back to
  the queue. The third time, it fails ("The worker stopped 3 times ...")
  instead of stopping the next worker too. **Retry** on the admin page queues
  it again from scratch.

## Settings

**Admin → Read aloud** (`/admin/tts`):

* show the player under pages (`tts_page_panel_enabled`);
* let anonymous visitors listen while public mode is on
  (`tts_public_access_enabled`);
* when to generate: when a reader asks, or every time a page is created or
  edited (`tts_auto_generate_enabled`);
* voice quality (`tts_performance_mode`: `auto`, `balanced`, `fast`);
* the languages the wiki may speak (`tts_enabled_languages`, English and
  Italian until changed). A page is read in its detected language only if
  that language is enabled; otherwise English or another enabled language
  is used. The interface language plays no part: German
  pages are read with the German voice (`de_DE-thorsten-medium`) once German
  is ticked here, right away when the reader picks German and through
  automatic detection only with `langdetect` installed (see language
  detection above); until then they are read with the English or Italian
  voice;
* the remote GPU server (address, token, timeout, on/off) and a **Test**
  button;
* the queue: retry failed jobs, delete one page's audio, queue audio for
  every page that has none (backfill), or delete all audio.

Environment variables (`BW_TTS_*`, see
[configuration](configuration.md#read-aloud-text-to-speech)) set the worker's
limits and override the backend, performance mode and GPU server. Readers can
queue only a limited number of pages at once (`BW_TTS_MANUAL_MAX_ACTIVE_JOBS`,
`BW_TTS_MANUAL_MAX_ACTIVE_PER_USER`).

On the hosting platform the operator decides: `BW_MANAGED_TTS_DISABLED`
switches generation off (existing audio stays playable), and the GPU server is
configured by the platform only, never by the tenant.

## The GPU speech server

The server lives in `contrib/tts-gpu-server/` (`server.py`, `install.sh`,
`requirements.txt`).

A small HTTP service that runs [Piper](https://github.com/OHF-Voice/piper1-gpl) voices on an
NVIDIA GPU for one or more BananaWiki wikis. The wiki sends a page's text, the server answers
with MP3 (or WAV) audio. If the server cannot be reached, the wiki falls back to Piper on its
own machine.

It replaces the 1.4 `banana-tts-gpu/` service and speaks the same protocol, so an existing
wiki configuration keeps working.

### Install

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

### Connect a wiki

In the wiki: **Admin → Read aloud → Remote GPU server**. Enter the address
(`http://100.64.0.2:8787`) and the token, tick *Use the GPU server*, save, and press
*Test the GPU server*. Alternatively set `BW_TTS_REMOTE_GPU_URL`,
`BW_TTS_REMOTE_GPU_AUTH_TOKEN` and optionally `BW_TTS_REMOTE_GPU_TIMEOUT` (seconds) in the
wiki's environment; they take precedence (and on managed hosting they are the only source).

The wiki only connects to loopback, private, and shared (100.64.0.0/10) or public addresses;
link-local and metadata addresses are refused, and redirects are never followed.

### Configuration

| Variable | Default | Meaning |
|---|---|---|
| `TTS_AUTH_TOKEN` | (required, ≥ 16 characters) | Shared bearer token |
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

### API

* `GET /health` → `{"status": "ok", "version": "1.6.0"}`. With the bearer token it also
  reports `gpu_available`, `cuda_enabled`, `loaded_voices` and `output_format`.
* `POST /synthesize` with `Authorization: Bearer <token>`, `Content-Type: application/json`
  and `{"text": "...", "language": "en"}` → `200` with `audio/mpeg` (or `audio/wav`).
  Errors: `401`/`403` token, `400` bad input or no voice for the language, `411` missing
  length, `413` too large, `415` not JSON, `503` busy (with `Retry-After`), `500` synthesis
  failed. The wiki retries `5xx` answers with backoff and postpones on `429`.

### Changes from 1.4

* Standard-library HTTP server (no FastAPI/uvicorn); synthesis runs in request threads with a
  concurrency limit instead of blocking an event loop, so `/health` always answers.
* Over-long text is refused instead of silently cut.
* `/health` without a token no longer discloses the voice directory or loaded voices.
