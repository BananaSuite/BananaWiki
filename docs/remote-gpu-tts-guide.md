# Remote GPU TTS: Experimental Setup Guide

Route BananaWiki TTS synthesis to a separate GPU server over a private Tailscale network.

## Overview

```
┌──────────────────────┐         Tailscale (private)        ┌──────────────────────┐
│  BananaWiki VPS      │ ──────── http://100.x.x.x:8787 ──► │  GPU Server          │
│  (Hetzner, 2 CPU)    │                                     │  (Piper + CUDA)      │
│                      │ ◄────── MP3 audio response ──────── │                      │
│  TTS worker enqueues │                                     │  FastAPI + uvicorn   │
│  pending row, then   │                                     │  Voice models cached │
│  POSTs text here     │                                     │  in VRAM             │
└──────────────────────┘                                     └──────────────────────┘
```

**What happens:**
1. User clicks "Listen to this page" on BananaWiki
2. BananaWiki creates a `pending` TTS row in the DB
3. The TTS worker picks up the row and sends the text to the GPU server via HTTP
4. The GPU server synthesizes audio with Piper (CUDA-accelerated) and returns MP3 bytes
5. BananaWiki saves the audio and marks the row `completed`

**What stays local:**
- The wiki itself, the database, all other features
- If the GPU server fails, the worker synthesizes that page with local Piper instead

The remote GPU backend is optional and experimental. Without it, BananaWiki uses local Piper.

---

## Prerequisites

- **GPU Server:** Linux with NVIDIA GPU, CUDA toolkit, cuDNN
- **BananaWiki VPS:** Any Linux server (the existing Hetzner VPS)
- **Tailscale:** Both machines must be on the same Tailscale network

---

## Step 1: Tailscale Setup

### If Tailscale is NOT installed on either machine

**On the GPU server:**
```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
# Follow the auth URL to log in
# Note the Tailscale IP: tailscale ip -4
# Example output: 100.64.0.1
```

**On the BananaWiki VPS:**
```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
# Follow the auth URL to log in (same Tailscale account)
# Note the Tailscale IP: tailscale ip -4
# Example output: 100.64.0.2
```

### If Tailscale is already installed

Verify connectivity:
```bash
# On the VPS
tailscale status
# Confirm the GPU server appears and is "Active"

# Ping the GPU server
ping $(tailscale ip -4 --peer=<GPU_SERVER_HOSTNAME>)
```

Record both Tailscale IPs:
- **GPU Server:** `100.x.x.x` (the one with the NVIDIA GPU)
- **BananaWiki VPS:** `100.y.y.y` (the existing Hetzner server)

---

## Step 2: GPU Server Installation

Copy the `banana-tts-gpu/` directory to the GPU server:

```bash
# From your local machine or the VPS
scp -r banana-tts-gpu/ root@<GPU_SERVER_TS_IP>:/tmp/
```

SSH into the GPU server and run the installer:

```bash
ssh root@<GPU_SERVER_TS_IP>

cd /tmp/banana-tts-gpu
sudo ./install.sh
```

The installer will:
1. Check for NVIDIA GPU and CUDA
2. Create a `banana-tts` system user
3. Install Python venv with `piper-tts` + `onnxruntime-gpu`
4. Download default voice models (en, it)
5. Ask for the listen address. The default is the machine's Tailscale IPv4 address when Tailscale is up, otherwise `127.0.0.1`. Pass `--host <address>` to choose it without the prompt.
6. Generate an auth token and store it in `/etc/banana-tts-gpu.env`, readable only by root
7. Install a systemd service

**Save the auth token.** You'll need it for BananaWiki configuration. If you bring your own with `--auth-token`, use only letters, digits and `. _ ~ + / = -`; the installer refuses anything else, because the token is stored in a systemd environment file.

The server never listens on every interface unless you ask for `0.0.0.0`, and the installer warns if you do. A server on `127.0.0.1` is only reachable from the same machine, which suits a wiki on the GPU host itself.

The server refuses to start when `TTS_AUTH_TOKEN` is empty. Every `/synthesize` request must send `Authorization: Bearer <token>`, and the server compares it in constant time.

### Verify the GPU server is running

```bash
sudo systemctl start banana-tts-gpu
sudo journalctl -u banana-tts-gpu -f

# In another terminal, using the listen address you chose:
curl http://<GPU_SERVER_TS_IP>:8787/health
# Should return: {"status":"ok","gpu_available":true,...}

# Test synthesis:
AUTH_TOKEN="<your-token>"
curl -H "Authorization: Bearer $AUTH_TOKEN" \
     -H "Content-Type: application/json" \
     -d '{"text": "Hello from the GPU server", "language": "en"}' \
     http://<GPU_SERVER_TS_IP>:8787/synthesize -o test.mp3

# Verify from the VPS over Tailscale:
# (run on the BananaWiki VPS)
curl -H "Authorization: Bearer $AUTH_TOKEN" \
     -H "Content-Type: application/json" \
     -d '{"text": "Hello over Tailscale", "language": "en"}' \
     http://<GPU_SERVER_TS_IP>:8787/synthesize -o test.mp3
```

---

## Step 3: BananaWiki Configuration

The wiki web process and the TTS worker both need the same settings. There are two places to put them:

- **Environment variables:** `BW_TTS_BACKEND=remote-gpu`, `BW_TTS_REMOTE_GPU_URL`, `BW_TTS_REMOTE_GPU_AUTH_TOKEN` and optionally `BW_TTS_REMOTE_GPU_TIMEOUT`. When the URL and the token are both set here, they win over anything in the wiki's settings.
- **Admin Settings:** open Settings, section Text-to-Speech, then "Remote GPU (Advanced)". Tick the checkbox and fill in the server URL, auth token and timeout. This only works on a wiki that does not set `BW_INSTANCE_DIR`. The settings API does not accept these values.

A wiki installed with `banana install` sets `BW_INSTANCE_DIR`, so there the Settings form shows the GPU fields as managed elsewhere and only the environment counts (see "Hosted wikis" below).

The URL is the server's base address, such as `http://100.x.x.x:8787`. BananaWiki sends `POST <URL>/synthesize`. A URL with a user name or password, a query string (`?`) or a fragment (`#`) is refused. The token may only contain printable ASCII characters.

### Installed with `banana install`

The web service and the TTS worker (`bananawiki.service` and `bananawiki-tts.service` with the default name) both read `/opt/bananawiki/config/app.env`. Add these lines there as the server administrator:

```ini
BW_TTS_BACKEND=remote-gpu
BW_TTS_REMOTE_GPU_URL=http://<GPU_SERVER_TS_IP>:8787
BW_TTS_REMOTE_GPU_AUTH_TOKEN=<your-token>
BW_TTS_REMOTE_GPU_TIMEOUT=120
```

Then restart:

```bash
sudo bananawiki restart
```

### Other installations

Add the same variables to the service of the wiki web process and to the service that runs `scripts/tts_worker.py`, for example:

```ini
[Service]
# ... existing config ...
Environment="BW_TTS_BACKEND=remote-gpu"
Environment="BW_TTS_REMOTE_GPU_URL=http://<GPU_SERVER_TS_IP>:8787"
Environment="BW_TTS_REMOTE_GPU_AUTH_TOKEN=<your-token>"
Environment="BW_TTS_REMOTE_GPU_TIMEOUT=120"
```

Then run `sudo systemctl daemon-reload` and restart both services. If you use the Settings form instead, leave `BW_TTS_BACKEND` unset: ticking the checkbox is what selects the GPU backend there.

### Hosted wikis

On a hosted wiki (`BW_MANAGED_HOSTING=1`, or `BW_INSTANCE_DIR` set) the GPU server belongs to the platform, not to the wiki. There:

- The URL, token and timeout come only from `BW_TTS_REMOTE_GPU_URL`, `BW_TTS_REMOTE_GPU_AUTH_TOKEN` and `BW_TTS_REMOTE_GPU_TIMEOUT` in the instance environment.
- The `tts_gpu_*` values in the wiki's own settings are ignored, whoever wrote them. A wiki admin can restore those values from a full-site backup import (older versions also accepted them through the settings API), so reading them would let the wiki send the platform's token to any server.
- The remote GPU backend is used when both the URL and the token are set, unless `BW_TTS_BACKEND` says otherwise. Otherwise the wiki uses local Piper.
- The platform should inject these variables only for instances its TTS policy allows, and never write the token into a wiki's database, where the wiki admin can export it.
- A wiki that is allowed to run its own plugin code (the container runtime) can read its environment, so it can read the token too. Keep that in mind before giving such a wiki the shared token.

### When the host switches TTS off

When `BW_MANAGED_TTS_DISABLED=1` or `BW_EASY_WIKI=1` is set, the wiki creates and synthesizes no audio at all. This covers the Generate button, auto-generation on page save, the admin "Queue missing audio" action, restart recovery and the TTS worker, which starts but stays idle. Turning on auto-generation in the wiki's settings does not change this. Audio generated earlier stays playable.

---

## Step 4: Verify End-to-End

1. Open your BananaWiki in a browser
2. Navigate to any page with content
3. Click "Listen to this page"
4. Click "Generate"

**Check the TTS worker logs:**
```bash
sudo journalctl -u bananawiki-tts -f
# A failed GPU request is logged as:
#   Remote GPU TTS failed (...), falling back to local piper
```

**Check the GPU server logs:**
```bash
# On the GPU server
sudo journalctl -u banana-tts-gpu -f
# You should see:
#   Synthesized 1234 chars in en -> 45678 bytes (mp3)
```

---

## Configuration Reference

### BananaWiki env vars

| Variable | Default | Description |
|----------|---------|-------------|
| `BW_TTS_BACKEND` | `piper` | Set to `remote-gpu` to use the GPU server |
| `BW_TTS_REMOTE_GPU_URL` | *(none)* | GPU server base URL, e.g. `http://100.x.x.x:8787` |
| `BW_TTS_REMOTE_GPU_AUTH_TOKEN` | *(none)* | Shared secret token for auth |
| `BW_TTS_REMOTE_GPU_TIMEOUT` | `120` | HTTP request timeout in seconds (1 to 3600) |

### GPU Server env vars

| Variable | Default | Description |
|----------|---------|-------------|
| `TTS_PORT` | `8787` | Server listen port |
| `TTS_HOST` | `127.0.0.1` | Bind address. Use the Tailscale address to serve other machines |
| `TTS_AUTH_TOKEN` | *(required)* | Shared secret token. The server does not start without it |
| `TTS_USE_CUDA` | `1` | Enable CUDA acceleration |
| `PIPER_VOICE_DIR` | `./voices` | Directory for voice models |
| `TTS_OUTPUT_FORMAT` | `mp3` | Output format (`mp3` or `wav`) |
| `TTS_LOG_LEVEL` | `INFO` | Logging verbosity |

---

## Security Notes

- The request goes only to the configured host. BananaWiki resolves the host name once, connects only to the addresses it checked and never follows a redirect.
- Addresses a TTS server never uses are refused before anything is sent: link-local (including the `169.254.169.254` cloud metadata service), multicast, unspecified and reserved ranges. Loopback, LAN and Tailscale addresses are allowed.
- A reply that is not MP3 or WAV audio is discarded rather than saved as page audio.
- Anyone who has the token can use the GPU server, so keep it off public networks and keep the token out of wiki databases on shared hosting.

---

## Troubleshooting

### "Cannot reach remote GPU server"

- Check Tailscale is running: `tailscale status`
- Ping the GPU server: `ping <GPU_SERVER_TS_IP>`
- Check the GPU server is listening on the address the wiki uses: `ss -tlnp | grep 8787`. A server on `127.0.0.1` cannot be reached from another machine; set `TTS_HOST` in the systemd unit to the Tailscale address and restart it.
- Check firewall: the GPU server must allow port 8787 on the Tailscale interface

### "Refusing to contact remote GPU TTS server"

The configured host resolved to a link-local, multicast, unspecified or reserved address. Use the server's Tailscale, LAN or loopback address.

### "Invalid token"

- Verify the same `TTS_AUTH_TOKEN` is set on both the GPU server and BananaWiki. On the GPU server it lives in `/etc/banana-tts-gpu.env`.
- Token is case-sensitive

### The GPU server exits at startup with "TTS_AUTH_TOKEN is not set"

Set `TTS_AUTH_TOKEN` in `/etc/banana-tts-gpu.env` (or the environment you start it from) and restart the service.

### GPU not detected

- Check: `nvidia-smi`
- Verify onnxruntime-gpu: `python3 -c "import onnxruntime; print(onnxruntime.get_available_providers())"`
- If `CUDAExecutionProvider` is not listed, CUDA/cuDNN setup is incomplete

### Slow first request

The first synthesis request is slow because voice models are loaded from disk into VRAM. Subsequent requests use cached models and are fast.

### Fallback behavior

If the GPU server is unreachable, returns an error or replies with something that is not audio, the TTS worker logs a warning and synthesizes that page with local Piper. If local Piper also fails, the usual retry logic applies: up to 3 automatic retries with exponential backoff before the row is marked `failed`.

---

## Uninstalling

### GPU Server

```bash
sudo ./banana-tts-gpu/install.sh --uninstall
```

This also removes `/etc/banana-tts-gpu.env`.

### BananaWiki

Remove the `BW_TTS_*` environment variables from `config/app.env` or the systemd services, untick the Remote GPU option in Admin Settings if you used it, and restart.

---

## Architecture Notes

- The remote-gpu backend is a third branch in `synthesize_to_mp3()` in `helpers/_tts.py`, alongside piper and stub
- Self-hosted settings live in the `tts_gpu_*` columns of `site_settings`; hosted wikis read only the environment
- The existing retry/resilience/supersede logic is backend-agnostic
- Voice models on the GPU server use the same names as BananaWiki's local Piper setup
- The GPU server caches loaded voice models in VRAM (configurable max, default 8)
