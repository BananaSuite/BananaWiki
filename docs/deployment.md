# Deployment

Four ways to put BananaWiki on a server, from the most automated to the most
manual. All of them run the same application: Gunicorn serving
`wsgi:app`, with an HTTPS reverse proxy in front.

| | Managed (`banana`) | Docker Compose | Manual | Hosting platform |
|---|---|---|---|---|
| Runs | systemd units | two containers | what you set up | portal + one container per wiki |
| HTTPS | Caddy config generated | Caddy container | yours | Caddy config generated |
| Updates | `bananawiki update`, backup and rollback included | rebuild the image | `git pull`, restart | `bananawiki update` |
| Backups | packages, optional encrypted Git backups | database copy command, volume backup | yours | packages, Git backups, Google Drive |

Whatever you choose, read [security](security.md) and set up
[backups](operations.md#backups) before people rely on the wiki.

## Managed server (`banana`)

The `banana` command installs BananaWiki as systemd services under one
directory, updates it from a Git source with a backup and automatic rollback,
backs it up, restores it and removes it. It is the recommended way to run a
wiki or a hosting platform on a Linux server.

### Requirements

* Linux with systemd (tested on Debian and Ubuntu), root access.
* `python3` 3.11 or newer with the `venv` module (`apt install python3-venv`),
  `git`, and `runuser` (part of util-linux).
* Network access to GitHub (or your source) and to PyPI while installing and
  updating. Dependencies are installed from binary wheels only; nothing is
  compiled on the server.
* For HTTPS: [Caddy 2](https://caddyserver.com/docs/install) (2.7 or newer
  for visitors' real addresses behind Cloudflare; older versions still work
  without that) and a DNS name pointing at the server, proxied by Cloudflare
  or not ([behind Cloudflare](#behind-cloudflare)).
* For the hosting platform: Docker.
* For encrypted Git backups: `age` (`apt install age`).
* For read aloud with MP3 output: `ffmpeg`.

### Install

```sh
git clone https://github.com/OverloadedTech/BananaWiki.git
cd BananaWiki
sudo ./banana install --mode wiki --domain wiki.example.org
```

`install` refuses a checkout with uncommitted changes: what it installs is
exactly the commit you reviewed. It then

1. creates the system user `bananawiki` (without a login shell),
2. copies the commit into `/opt/bananawiki/releases/<commit>/`, builds its
   virtual environment and seals the tree read-only,
3. writes `/opt/bananawiki/config/app.env` (see
   [configuration](configuration.md#managed-servers)),
4. installs the units `bananawiki.service` (Gunicorn) and
   `bananawiki-tts.service` (the read-aloud worker, which idles while read
   aloud is off), plus the command `/usr/local/bin/bananawiki`,
5. starts everything and waits until `/health` answers.

Options:

| Option | Meaning |
|---|---|
| `--mode wiki` or `--mode hosting` | What to install. The mode is remembered. |
| `--domain NAME` | Public host name. Without it the wiki listens on `127.0.0.1` only (for a private network or a proxy you manage yourself). |
| `--portal-domain NAME` | Hosting only: portal host name (default `portal.<domain>`). |
| `--port N` | Local port (default 5001 for a wiki, 5099 for hosting; 1024–65535). |
| `--name NAME` | Service and command name (default `bananawiki`). Several installations need different names and `--root` directories. |
| `--root DIR` | Installation directory (default `/opt/bananawiki`); give it to every later command too. |
| `--repo URL`, `--branch NAME`, `--token-file F`, `--ssh-key F --known-hosts F`, `--require-signatures FILE` | The update source; see [updates](operations.md#updates). |
| `--restore PACKAGE` | Install from a backup package instead (moving to a new server). |
| `--reuse-data` | Reinstall over the configuration and data left by `uninstall` or an interrupted installation. |

When the command finishes, it tells you what to do next:

```sh
sudo bananawiki proxy                          # show the Caddy configuration
sudo bananawiki proxy --install --email you@example.org
sudo bananawiki setup-token                    # the token for /setup
```

`proxy --install` validates the file with `caddy validate`, installs it as
Caddy's configuration and reloads Caddy. It refuses to replace a Caddyfile it
did not write unless you add `--replace` (the old file is kept under
`/opt/bananawiki/backups/`). Then open `https://wiki.example.org` and follow
[first sign-in](getting-started.md#first-sign-in).

`proxy` options (all remembered in `config/proxy.json`, so updates re-render
the same configuration):

| Option | Meaning |
|---|---|
| `--email ADDRESS` | ACME account e-mail for certificate notices. |
| `--cloudflare` / `--no-cloudflare` | The DNS records are proxied by Cloudflare: disable the TLS-ALPN-01 challenge, which cannot pass Cloudflare's proxy (certificates then come from Let's Encrypt only, without Caddy's ZeroSSL fallback; also for custom domains in the two modes below). See [behind Cloudflare](#behind-cloudflare). |
| `--tls acme` | Default: Let's Encrypt certificates per host name (on demand for hosted wikis). |
| `--tls cloudflare-dns --cloudflare-token-file FILE` | One wildcard certificate `*.<domain>` (plus `<domain>`) through the DNS-01 challenge. Needs Caddy 2.10 or newer with the `caddy-dns/cloudflare` module and an API token with *Zone → DNS → Edit* for the zone; the token is stored in `/etc/caddy/bananawiki-cloudflare.env` (root only, loaded by a `caddy.service.d` drop-in), never in the Caddyfile. |
| `--tls origin-cert --origin-cert PEM --origin-key PEM` | A Cloudflare Origin CA certificate for `<domain>` and `*.<domain>`, copied to `/etc/caddy/bananawiki-origin.{crt,key}`. Only for proxied records with SSL/TLS mode *Full (strict)*: browsers do not trust it. |

The printed output (without `--install`) ends with warnings on standard error
when a host name is too deep for Cloudflare's free certificate or the installed
Caddy is too old to pass visitors' real addresses (2.7 or newer is needed).

Automatic updates stay **off** until you run `sudo bananawiki updates enable`.

### Layout

```
/opt/bananawiki/
  config/        installation.json, source.json, updates.json, app.env,
                 repository credentials, operation history (private)
  repository.git bare cache of the update source
  releases/      one read-only tree per deployed commit, each with .venv/
  current ->     the running release
  data/          everything the application writes (database, uploads, logs,
                 secret key); the only writable path of the services
  backups/       portable packages (manual-*, before-update-*, auto-*)
  site/          hosting: the static apex website
  staging/       scratch space
```

The services run as the `bananawiki` user with a strict systemd sandbox
(read-only system, private `/tmp`, no new privileges, no capabilities,
`ReadWritePaths` limited to `data/`). Change settings in `config/app.env` and
run `sudo bananawiki restart`.

Day-to-day commands (`status`, `update`, `backup`, `restore`, `rollback`,
logs) are in [operations](operations.md).

## Docker and Docker Compose

The repository has a `Dockerfile` for a single wiki and a `compose.yaml` that
puts it behind Caddy with automatic HTTPS.

```sh
git clone https://github.com/OverloadedTech/BananaWiki.git
cd BananaWiki
WIKI_DOMAIN=wiki.example.org ACME_EMAIL=you@example.org docker compose up -d
docker compose exec wiki python -m bananawiki.cli setup-token
```

Open `https://wiki.example.org` and use the token at `/setup`.

How the image is built:

* Python 3.12 (Debian bookworm), dependencies from wheels only, plus ffmpeg,
  and DejaVu fonts for PDF export.
* Runs as UID 10001, works with a read-only root file system; everything is
  written to the `/data` volume (`BW_INSTANCE_DIR=/data`).
* Listens on port 5001; a health check calls `/health`.
* Compose runs it read-only, with all capabilities dropped and
  `no-new-privileges`, and sets `BW_PROXY_MODE=1` and
  `BW_PREFERRED_URL_SCHEME=https`. Caddy (`deploy/Caddyfile.compose`) adds
  HSTS and strips `X-Forwarded-Prefix`.

Any variable from [configuration](configuration.md) can be added under
`environment:` in `compose.yaml`.

Without Compose:

```sh
docker build -t bananawiki .
docker run -d --name wiki -p 127.0.0.1:5001:5001 -v bananawiki-data:/data \
  --read-only --tmpfs /tmp --cap-drop ALL --security-opt no-new-privileges bananawiki
```

Put your own HTTPS proxy in front and add `-e BW_PROXY_MODE=1`.

**Backups.** `docker compose exec wiki python -m bananawiki.cli db backup`
writes a consistent copy of the database to `/data/backups/manual-<time>.db`.
Back up the whole `wiki-data` volume (it also holds the uploads and the
secret key), for example with
`docker compose exec wiki python -m bananawiki.cli export --output /tmp/wiki.tar.gz`
followed by `docker compose cp wiki:/tmp/wiki.tar.gz .` (the export must be
written outside `/data`; see [operations](operations.md#moving-a-wiki-without-banana)).

**Updates.** `git pull && docker compose build && docker compose up -d`. The
database is upgraded when the new container starts, after an automatic copy
to `/data/backups/`.

Read aloud inside the container: set `BW_TTS_INLINE_WORKER=1`, or run a
second container from the same image with the command
`python -m bananawiki.ops.tts_worker` and the same volume. The image does not
contain Piper voices; see [read aloud](tts.md).

## Manual installation

For people who manage their servers themselves. The example uses
`/srv/bananawiki` and a `bananawiki` system user.

```sh
sudo useradd --system --user-group --home-dir /srv/bananawiki/data --shell /usr/sbin/nologin bananawiki
sudo mkdir -p /srv/bananawiki && sudo chown bananawiki: /srv/bananawiki
sudo -u bananawiki git clone https://github.com/OverloadedTech/BananaWiki.git /srv/bananawiki/app
cd /srv/bananawiki/app
sudo -u bananawiki python3 -m venv .venv
sudo -u bananawiki .venv/bin/python -m pip install --only-binary=:all: -r requirements.txt
```

Environment file `/srv/bananawiki/app.env` (mode 0600, owned by root):

```sh
BW_ENV=production
BW_INSTANCE_DIR=/srv/bananawiki/data
BW_HOST=127.0.0.1
BW_PORT=5001
BW_PROXY_MODE=1
BW_PREFERRED_URL_SCHEME=https
BW_SOURCE_URL=https://github.com/OverloadedTech/BananaWiki
```

`/etc/systemd/system/bananawiki.service`:

```ini
[Unit]
Description=BananaWiki
After=network-online.target
Wants=network-online.target

[Service]
User=bananawiki
Group=bananawiki
WorkingDirectory=/srv/bananawiki/app
EnvironmentFile=/srv/bananawiki/app.env
ExecStart=/srv/bananawiki/app/.venv/bin/gunicorn -c gunicorn.conf.py wsgi:app
Restart=always
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/srv/bananawiki/data

[Install]
WantedBy=multi-user.target
```

For read aloud add a second unit with
`ExecStart=/srv/bananawiki/app/.venv/bin/python scripts/tts_worker.py` (or set
`BW_TTS_INLINE_WORKER=1`). The managed units in `deploy/systemd/` show the full
sandbox the `banana` controller uses.

```sh
sudo systemctl daemon-reload
sudo systemctl enable --now bananawiki
```

Administration commands need the same environment as the service. For
example, the setup token:

```sh
cd /srv/bananawiki/app
sudo sh -c 'set -a; . /srv/bananawiki/app.env; exec runuser -u bananawiki -- .venv/bin/python -m bananawiki.cli setup-token'
```

### Reverse proxy

Caddy: `deploy/Caddyfile.wiki` (set `WIKI_DOMAIN`, `ACME_EMAIL` and, if the
port is not 5001, `BW_PORT` in Caddy's environment). It enables HSTS, sets
request timeouts, disables response buffering and removes
`X-Forwarded-Prefix`.

Any other proxy works if it

* terminates HTTPS and forwards to `127.0.0.1:5001`,
* **overwrites** `X-Forwarded-For`, `X-Forwarded-Proto` and
  `X-Forwarded-Host` (with `BW_PROXY_MODE=1` the wiki trusts one hop of each),
  and removes `X-Forwarded-Prefix`,
* allows request bodies as large as your biggest upload (100 MiB attachments
  by default, 500 MiB for whole-site imports).

For nginx that means, inside the `location`:

```nginx
proxy_pass http://127.0.0.1:5001;
proxy_set_header Host $host;
proxy_set_header X-Forwarded-For $remote_addr;
proxy_set_header X-Forwarded-Proto $scheme;
proxy_set_header X-Forwarded-Host $host;
proxy_set_header X-Forwarded-Prefix "";
proxy_buffering off;
client_max_body_size 500m;
```

Behind Cloudflare, let nginx take the visitor's address from Cloudflare (and
only from Cloudflare) so that `$remote_addr` above is the visitor, not the
Cloudflare edge; at the `http` or `server` level:

```nginx
# https://www.cloudflare.com/ips/ (the list BananaWiki ships is in bananawiki/core/cloudflare.py)
set_real_ip_from 173.245.48.0/20;
set_real_ip_from 103.21.244.0/22;
set_real_ip_from 103.22.200.0/22;
set_real_ip_from 103.31.4.0/22;
set_real_ip_from 141.101.64.0/18;
set_real_ip_from 108.162.192.0/18;
set_real_ip_from 190.93.240.0/20;
set_real_ip_from 188.114.96.0/20;
set_real_ip_from 197.234.240.0/22;
set_real_ip_from 198.41.128.0/17;
set_real_ip_from 162.158.0.0/15;
set_real_ip_from 104.16.0.0/13;
set_real_ip_from 104.24.0.0/14;
set_real_ip_from 172.64.0.0/13;
set_real_ip_from 131.0.72.0/22;
set_real_ip_from 2400:cb00::/32;
set_real_ip_from 2606:4700::/32;
set_real_ip_from 2803:f800::/32;
set_real_ip_from 2405:b500::/32;
set_real_ip_from 2405:8100::/32;
set_real_ip_from 2a06:98c0::/29;
set_real_ip_from 2c0f:f248::/32;
real_ip_header CF-Connecting-IP;
```

With Cloudflare's *Flexible* mode nginx receives plain HTTP: do not redirect
it to HTTPS (an endless loop), or better, use *Full (strict)*.

Never set `BW_PROXY_MODE=1` when the wiki's port is reachable directly: a
client could then choose its own address and scheme.

### Updating a manual installation

```sh
cd /srv/bananawiki/app
sudo -u bananawiki git pull
sudo -u bananawiki .venv/bin/python -m pip install --only-binary=:all: -r requirements.txt
sudo systemctl restart bananawiki
```

The database is copied to `<instance>/backups/` and upgraded on the first
start of a release with a newer schema.

## Hosting platform

`sudo ./banana install --mode hosting --domain example.com` installs the
portal (`bananawiki.service`), its maintenance service
(`bananawiki-maintenance.service`) and the privileged runtime agent
(`bananawiki-agent.service`), and builds the tenant image
`bananawiki-tenant:<commit>` from `Dockerfile.tenant`. Docker must be
installed first. `sudo bananawiki proxy --install` then installs a Caddy
configuration that serves the static site on the base domain (and redirects
`www.<domain>` to it), the portal on the portal domain and every wiki through
on-demand TLS, or through one wildcard certificate with `--tls cloudflare-dns`
or `--tls origin-cert` (see [behind Cloudflare](#behind-cloudflare)).

Keep the portal and the wikis one level below the DNS zone: the default
`--domain example.com` gives `portal.example.com` and
`<name>-hosting.example.com`. With `--domain hosting.example.com` they become
`portal.hosting.example.com` and `<name>-hosting.hosting.example.com`, which
Cloudflare's free certificate does not cover; `install` and `proxy` warn about
this. Use `--domain example.com --portal-domain hosting.example.com` instead
(the static site then answers on `example.com`).

The first account on the portal is the platform administrator. Its sign-up
needs `HOSTING_BOOTSTRAP_TOKEN` from `config/app.env`:

```sh
sudo grep HOSTING_BOOTSTRAP_TOKEN /opt/bananawiki/config/app.env
```

Everything else about the platform is in [hosting](hosting.md).

## Behind Cloudflare

BananaWiki works with proxied (orange-cloud) Cloudflare DNS records, both for a
single wiki and for the hosting platform (portal, static site, every hosted
wiki). What the generated Caddy configuration does for it:

* It trusts `CF-Connecting-IP` from Cloudflare's published address ranges only
  and passes that address to the application, so sign-in limits, rate limits
  and logs see the visitor instead of a shared Cloudflare address (Caddy 2.7
  or newer).
* Every site also answers plain HTTP. ACME HTTP-01 challenges are answered
  first; other requests are redirected to HTTPS, except requests that
  Cloudflare already received over HTTPS (*Flexible* mode), which are served
  instead of redirected in an endless loop (`ERR_TOO_MANY_REDIRECTS`).
* Idle connections stay open longer than Cloudflare reuses them (occasional
  HTTP 520 otherwise).
* A wiki whose container is starting or unreachable shows its "starting"
  page (HTTP 503, refreshing by itself) instead of an empty 502 that Cloudflare
  replaces with its own "Bad gateway" page.

Recommended Cloudflare settings:

1. **SSL/TLS mode *Full (strict)*.** *Flexible* works but leaves the
   Cloudflare-to-server leg unencrypted; *Full* skips certificate checks.
   Avoid *Automatic* while certificates are still being obtained: it can settle
   on *Flexible*.
2. **Certificates on the server:**
   * `--tls cloudflare-dns` (recommended with Cloudflare): one wildcard
     certificate through the DNS API, nothing depends on port 80, no
     per-wiki certificates and no Let's Encrypt rate limits (50 new
     certificates per week per domain).
   * `--tls origin-cert`: a free Cloudflare Origin CA certificate (SSL/TLS →
     Origin Server) for `example.com` and `*.example.com`.
   * `--tls acme --cloudflare` (the default mode): Let's Encrypt must reach
     `http://<host>/.well-known/acme-challenge/` on port 80. Turn *Always Use
     HTTPS* off, or add a Configuration Rule that turns it off (and any WAF or
     Bot Fight challenge) for URI paths starting with
     `/.well-known/acme-challenge/`. With a redirect to HTTPS the first
     certificate of every new wiki fails (Cloudflare error 525).
3. **One level of subdomain.** Cloudflare's free Universal SSL certificate
   covers `example.com` and `*.example.com` only. A proxied
   `portal.hosting.example.com` fails in the browser with
   `ERR_SSL_VERSION_OR_CIPHER_MISMATCH` before it reaches the server; see the
   domain layout in [hosting platform](#hosting-platform).
4. **DNS records:** proxied A/AAAA records for the base domain, `www`, the
   portal and `*` (or each wiki) pointing at the server. Keep the custom
   domain target (`HOSTING_CUSTOM_DOMAIN_TARGET`) **DNS-only** (grey cloud):
   customers' CNAMEs to a proxied name of another Cloudflare account fail
   with error 1014.
5. **Limits:** Cloudflare's Free and Pro plans refuse request bodies above
   100 MB (HTTP 413; the wiki allows 100 MiB attachments and 500 MiB site
   imports by default, so lower `BW_MAX_ATTACHMENT_SIZE_BYTES` or upload big
   imports without the proxy) and end requests after about 125 seconds
   (HTTP 524).
6. **Port mode** (`HOSTING_MODE=port`, wikis on ports 6001–7000) cannot be
   proxied: Cloudflare only proxies a few fixed ports. Use subdomain mode.

Optionally, allow ports 80 and 443 only from
[Cloudflare's addresses](https://www.cloudflare.com/ips/) in the firewall (not
with `--tls acme` for custom domains whose records are not proxied).

## Moving to another server

* Managed: `sudo bananawiki backup --output /root/wiki.tar.gz` on the old
  server, then on the new one `sudo ./banana install --restore /root/wiki.tar.gz`
  from a checkout (add `--domain` or `--port` to change them). See
  [operations](operations.md#backups).
* Any installation: **Admin → Site migration** exports the whole wiki as one
  ZIP and imports it on the new one (see [features](features.md#administration)).
* Command line: `bananawiki export` / `bananawiki import`, see
  [operations](operations.md#moving-a-wiki-without-banana).

## Sizing

SQLite handles one writer at a time and any number of readers; every request
uses one connection, and request statistics are buffered in memory and
written every few seconds instead of on every request. Start with the defaults
(2 Gunicorn workers × 4 threads, `BW_WORKERS` and `BW_THREADS`), add threads
before workers, and keep the database on a local disk, never on a network
file system. Local read aloud (Piper) is the most CPU- and memory-hungry part;
limit it with `BW_TTS_WORKER_COUNT`, or move synthesis to a
[GPU server](tts.md#the-gpu-speech-server).
