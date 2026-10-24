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
   when ffmpeg is available, and stores the file in `BW_TTS_FOLDER`. The
   voice reads the title, then each heading, list item and table row as a
   sentence of its own (table cells separated by commas); web addresses are
   read as the site's name, and embeds (`[[video …]]`, `[[canvas …]]`,
   `[[kanban …]]`) and `[TOC]` are skipped. Whether audio is out of date is
   still decided on the 1.4 text, so existing audio stays valid.
3. Editing a page invalidates its audio; a page that had audio gets fresh
   audio queued. Deleting a page removes its files.
4. The `tts.sweep` job (every 6 hours) deletes files no row refers to and
   refreshes audio that fell out of date.

## Listening

While audio is ready, the word being read is highlighted and the page follows
it, waiting while you scroll on your own; untick **Highlight as it reads** to
stop both. Once you are listening, click or tap a word to read from there. The
previous/next paragraph buttons move between paragraphs, as do J and L while
the player has focus (K plays or pauses) and a phone's lock-screen and
headphone controls. While the audio plays and the player is out of view, a
small player stays at the bottom of the screen. Each page continues where you
stopped (kept in your browser for the last 30 pages), with **Start over**.
The audio reads at most the first 20,000 characters of a page; on longer
pages the player says so and the highlighting stops where the audio does.
Queued pages show how many jobs come first, and failed audio can be tried
again from the page.

## Install

* **Piper**: `piper-tts` is part of `requirements.txt` (managed servers and the
  container image have it). From source: `python -m pip install -e '.[tts]'`.
* **ffmpeg** (optional, for MP3 and speed-changed downloads):
  `apt install ffmpeg`, or point `BW_TTS_FFMPEG` at it.
  The wiki and tenant container images include FFmpeg 9.0.2 built from the
  signed upstream source with only what speech needs: WAV/MP3 input, PCM
  formats, MP3 output and pitch-preserving speed changes, with file and pipe
  protocols only. The build recipe is in `docker/media/`; sources, the
  configuration, licences and redistribution notes are in each image at
  `/usr/local/share/bananawiki/media`.
* The wiki, the desktop app, the GPU server and the containers set
  `ORT_DISABLE_TELEMETRY=1` before Piper loads, which turns off ONNX
  Runtime's diagnostic telemetry. A value you set yourself is kept.
* **Voices** are downloaded on first use into `BW_TTS_PIPER_VOICE_DIR` (default
  `<instance>/piper-voices`). On a server without internet access set
  `BW_TTS_PIPER_AUTO_DOWNLOAD=0` and download them beforehand, for example
  `python -m piper.download_voices --download-dir <voice dir> it_IT-paola-medium`.
  English uses `en_US-lessac-medium` and Italian `it_IT-paola-medium`; many
  other languages have a default voice, and `BW_TTS_PIPER_VOICE_MAP` adds or
  replaces voices.
* **Language detection**: a built-in heuristic for English and Italian is
  always available. `langdetect` improves detection for other languages; it is
  not installed by default because it is only published as a source
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
* On Linux and other POSIX systems Piper runs in a child process of the worker,
  started for each job, so the voice is loaded for every page. If it runs out
  of memory, crashes or runs longer than `BW_TTS_MAX_JOB_SECONDS` (default 3600),
  only that job fails. `BW_TTS_PIPER_MEMORY_MB` also limits its address space
  (`RLIMIT_AS`), so it fails before the server runs out of memory. The limit
  is off by default because ONNX Runtime reserves more address space than it
  uses, more on servers with many CPU cores: try a value (for example 4096)
  on your server before you rely on it. A lower `BW_MEMORY_LIMIT_MB` applies
  as well. On Windows and in the packaged desktop app Piper runs inside the
  worker.
* No job runs longer than `BW_TTS_MAX_JOB_SECONDS`: its lease stops being
  renewed and the job fails ("The job ran longer than ..."). With a GPU
  server, keep it above `BW_TTS_REMOTE_GPU_TIMEOUT`.
* A job whose worker stopped while reading it (killed, crashed) goes back to
  the queue. The third time, it fails ("The worker stopped 3 times ...")
  instead of stopping the next worker too. **Retry** on the admin page queues
  it again from scratch.
* Every worker touches `.worker-heartbeat` in `BW_TTS_FOLDER` when it starts
  and every 30 seconds. When pages are queued and no worker has checked in for
  3 minutes, **Admin → Read aloud** warns and readers are told it may take a
  while. The System table shows the last check-in. A worker run from cron
  with `--once` checks in only while it runs.

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
  is used. German pages use `de_DE-thorsten-medium` once German is ticked
  here. Without `langdetect`, German is detected only when the reader
  chooses it;
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

A small HTTP service in `contrib/tts-gpu-server/` runs Piper voices on an
NVIDIA GPU for one or more wikis; if it cannot be reached, the wiki falls
back to Piper on its own machine. It speaks the protocol of the 1.4
`banana-tts-gpu/` service, so an existing wiki configuration keeps working.
Installing it, its settings and its HTTP API are described in
[its README](../contrib/tts-gpu-server/README.md). Never expose it to the
internet: bind it to a private address and firewall the port.

### Connect a wiki

In the wiki: **Admin → Read aloud → Remote GPU server**. Enter the address
(`http://192.168.1.50:8787`) and the token, tick *Use the GPU server*, save,
and press *Test the GPU server*. Alternatively set `BW_TTS_REMOTE_GPU_URL`,
`BW_TTS_REMOTE_GPU_AUTH_TOKEN` and optionally `BW_TTS_REMOTE_GPU_TIMEOUT`
(seconds) in the wiki's environment; they take precedence, and on managed
hosting they are the only source.

An environment token is only sent to the environment address. Changing the
saved address (scheme, host or port) clears the saved token; enter the token
again with the new address.

The wiki only connects to loopback, private, shared (100.64.0.0/10) or
public addresses; link-local and metadata addresses are refused, and
redirects are never followed.
