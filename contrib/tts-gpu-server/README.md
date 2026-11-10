# BananaWiki GPU speech server

A small HTTP service that runs [Piper](https://github.com/OHF-Voice/piper1-gpl) voices on an
NVIDIA GPU for one or more BananaWiki wikis. The wiki sends a page's text, the server answers
with MP3 (or WAV) audio. If the server cannot be reached, the wiki falls back to Piper on its
own machine.

It speaks the protocol of the 1.4 `banana-tts-gpu/` service, so an existing wiki configuration
keeps working.

## Install

On the GPU machine (Ubuntu/Debian with the NVIDIA driver, CUDA and cuDNN installed):

```sh
sudo ./install.sh                      # a Tailscale address if Tailscale is installed, otherwise 127.0.0.1
sudo ./install.sh --host 192.168.1.50  # a specific private address
sudo ./install.sh --cpu                # no GPU: CPU onnxruntime
sudo ./install.sh --uninstall
```

The installer creates the `bananawiki-tts` user, a virtualenv in `/opt/bananawiki-tts-gpu`,
the root-only environment file `/etc/bananawiki-tts-gpu.env` with a random `TTS_AUTH_TOKEN`,
and the systemd unit `bananawiki-tts-gpu`.

Manual run: `pip install -r requirements.txt`, then
`TTS_AUTH_TOKEN=... TTS_HOST=192.168.1.50 python server.py`.

Never expose the server to the internet: bind it to a private address (a LAN, a VPN such as
Tailscale or WireGuard) and firewall the port.

## Connect a wiki

See [Connect a wiki](../../docs/tts.md#connect-a-wiki) in the read-aloud documentation.

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
| `TTS_QUEUE_TIMEOUT` | `30` | Seconds a request waits for a free slot before `503`. A freed slot goes to the waiting wiki served longest ago (each hosted wiki, and the master token, counts as one), so one wiki's queue never keeps the others waiting |
| `TTS_MAX_INPUT_CHARS` | `20000` | Longer text is refused with `413` (the wiki sends at most 20000). Accepted text is synthesised in pieces of at most 500 characters, so a page without full stops cannot exhaust the GPU memory |
| `TTS_MAX_BODY_BYTES` | `262144` | Larger request bodies are refused with `413` |
| `TTS_SOCKET_TIMEOUT` | `30` | Seconds a client may stay silent while sending a request (or between keep-alive requests) before it is disconnected |
| `TTS_REQUEST_TIMEOUT` | `10` | Seconds a client has to send a whole request (line, headers and body), counted from when the server starts waiting for it (also between keep-alive requests); slower clients are disconnected |
| `TTS_MAX_CONNECTIONS` | `64` | Open connections at once; further ones are closed straight away |
| `TTS_MAX_CONNECTIONS_PER_CLIENT` | `8` | Open connections from one address. A further one takes the place of that address's oldest connection that is not busy with an authenticated request, and is closed straight away when every one is busy. Raise it when many wikis reach the server through one address (a hosting platform whose wikis share the host's address) |
| `TTS_MAX_PENDING_PER_CLIENT` | `4` | Connections from one address that have not yet sent a valid token (at most `TTS_MAX_CONNECTIONS_PER_CLIENT`); a further one takes the place of the oldest of them, so clients without a token never shut out the next request from the same address |
| `TTS_MAX_BUSY_PER_TENANT` | `2` | Requests one hosted wiki (one `bwt1` token, from any address) may have in progress at once, waiting or synthesising; a further one is answered `429` with `Connection: close`, so one wiki cannot keep every connection of an address its wikis share busy. Such a connection is the first to make room for a new one and does not count against `TTS_MAX_PENDING_PER_CLIENT`. The master token is not limited |
| `TTS_OUTPUT_FORMAT` | `mp3` | `mp3` (needs ffmpeg) or `wav` |
| `TTS_FFMPEG` | | Path to ffmpeg if it is not on `PATH` |
| `TTS_LENGTH_SCALE`, `TTS_NOISE_SCALE`, `TTS_NOISE_W_SCALE` | `1.0`, `0.667`, `0.8` | Piper voice parameters |
| `TTS_LOG_LEVEL` | `INFO` | Logging |

## API

* `GET /health` → `{"status": "ok", "version": "1.8.0"}`. Without a token it reports nothing
  else; with the bearer token it also reports `gpu_available`, `cuda_enabled`, `loaded_voices`
  and `output_format`.
* Every answer to a request without a valid token (`/health` included) closes the connection
  (`Connection: close`); only requests with the token keep a connection alive.
* `POST /synthesize` with `Authorization: Bearer <token>`, `Content-Type: application/json`
  and `{"text": "...", "language": "en"}` → `200` with `audio/mpeg` (or `audio/wav`).
  Errors: `401`/`403` token, `400` bad input or no voice for the language, `411` missing
  length, `413` too large, `415` not JSON, `429` the wiki has `TTS_MAX_BUSY_PER_TENANT` requests
  in progress (with `Retry-After`), `503` busy (with `Retry-After`), `500` synthesis
  failed. The wiki retries `5xx` answers with backoff and postpones on `429`.
* Every request is logged with its tenant: `tenant=<wiki id>` (the id `TTS_REVOKED_TENANTS`
  takes), `operator` for the master token, `-` without a valid token. Tokens are never logged.
  Control characters in a request line (a client sends it byte for byte) are logged escaped
  (`\x1b`), so a client cannot send escape sequences to the terminal showing the log.

The server uses only the standard library's HTTP server. Synthesis runs in request threads
under the `TTS_MAX_CONCURRENT` limit, so `/health` answers while voices are speaking.
