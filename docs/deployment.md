# Deploy and maintain BananaWiki

Use one command, `banana`, for a managed Linux installation. It remembers whether this server runs a single wiki or a hosting platform. After installation the same command is available as `bananawiki` from any directory. Development and the portable desktop app remain separate ways to run locally.

**Automatic repository updates are optional and disabled by default.** A normal installation never starts pulling future changes without an explicit `updates enable` command. Updates may change or remove features; choose manual updates or a branch/fork you control if that suits your wiki.

## Requirements

Use Linux with systemd, Python 3.12+, Python venv support, Git, and enough local disk for the application, a prepared release, and complete data backups. Debian 13 and Ubuntu 24.04 are suitable starting points. Install Caddy for the documented HTTPS setup, Docker Engine for hosting, and FFmpeg for audio/video features. Install operating-system packages through their official distribution channels.

Start from a clean, reviewed Git checkout. The installer uses that exact local commit and does not need to fetch a future repository during installation. Source changes must be committed first. The default update source is `https://github.com/BananaSuite/BananaWiki.git`, branch `main`; replace it with your own repository or fork as needed.

## Single wiki

```sh
git clone https://github.com/BananaSuite/BananaWiki.git
cd BananaWiki
sudo ./banana install --mode wiki --domain wiki.example.org
sudo bananawiki proxy
sudo bananawiki proxy --install
```

Replace the hostname, point its DNS records at the server, and allow ports 80 and 443. `proxy` prints the matching Caddy configuration. `proxy --install` validates and installs it; an existing unrelated Caddyfile is preserved unless you explicitly request `--replace`. For an existing multi-site proxy, merge the printed configuration yourself.

The application binds to loopback. The installer generates a private setup token in `/opt/bananawiki/config/app.env`. Read `BW_SETUP_TOKEN` there as the server administrator, open the HTTPS address, and create the first administrator. Keep the token private. You can remove it after setup and run `sudo bananawiki restart`.

Omit `--domain` for loopback-only access, for example through an SSH tunnel. Proxy trust and secure URL settings are enabled when a domain is supplied. Keep the application port private behind the trusted proxy. `--port` selects another application port at installation.

## Hosting platform

```sh
sudo ./banana install --mode hosting --domain example.org --portal-domain portal.example.org
sudo bananawiki proxy
sudo bananawiki proxy --install
```

The installer builds the tenant image from the selected source commit and runs the portal and its background maintenance process as supervised services. It remembers the hosting mode for every update, backup, restore, and uninstall. Docker tenant isolation, account approvals, moderation, storage limits, expiry, and custom-domain verification remain available.

With domain-based hosting, tenant containers use separate internal networks by default. The portal reaches their private bridge addresses; plugins cannot initiate connections to the Internet, other tenant networks or cloud metadata services. For hosted integrations that need remote models, voice downloads or LAN cameras, the host operator can set `HOSTING_TENANT_NETWORK=outbound` in `config/app.env`, then run `sudo bananawiki restart`. That explicitly grants tenant code external network access. Use a dedicated host, keep private host services bound to loopback, and restrict cloud metadata and sensitive LAN destinations in the host/provider firewall when granting outbound access. Solo Wiki deployments keep their normal network access.

Port-based and onion hosting reach tenants through published ports, and Docker does not publish ports from internal networks, so these modes always use outbound networking. Their loopback tenant ports must be exposed through an operator-managed proxy, SSH tunnel or onion service. Choose domain-based hosting for isolated public tenants. An explicit `isolated` setting with port or onion mode is rejected with configuration guidance. In onion mode, a tenant's clearnet requests can reveal the server's public address, so send tenant traffic through Tor or block clearnet traffic from the tenant networks.

Whenever tenants have outbound access, the portal logs a warning at startup. Tenant plugin code can then reach the Internet, your LAN and any host service listening on the bridge gateway, so add host firewall rules. Docker checks the `DOCKER-USER` chain before its own rules for traffic that leaves a container, and traffic from a container to the host itself goes through `INPUT`. Give tenant networks a known address pool in `/etc/docker/daemon.json`, for example `{"default-address-pools": [{"base": "172.30.0.0/16", "size": 24}]}`, restart Docker, and then block new connections from that pool to the host, to private ranges and to metadata addresses:

```sh
T=172.30.0.0/16
iptables -I INPUT -s "$T" -m conntrack --ctstate NEW -j DROP
for net in 10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 100.64.0.0/10 169.254.0.0/16; do
  iptables -I DOCKER-USER -s "$T" -d "$net" -m conntrack --ctstate NEW -j DROP
done
```

Replies to connections the portal or a visitor opened are still allowed. If the host's DNS server has a private address, allow it after the loop, so the rule lands above the drops: `iptables -I DOCKER-USER -s "$T" -d 192.168.1.1 -p udp --dport 53 -j ACCEPT`, with your server's address. Add matching `ip6tables` rules if Docker gives tenants IPv6, and make the rules persistent with your distribution's firewall tooling. These rules are also worth having with isolated networks, as a second layer.

Point the apex, portal, and tenant DNS names at this server. With the defaults, a tenant named `notes` uses `notes-hosting.example.org`; a wildcard DNS record can cover those names. Caddy obtains individual certificates and uses the portal's domain-authorization endpoint before serving a new tenant/custom domain. See [custom domains](custom-domains.md) for ownership and permission requirements.

Use `HOSTING_BOOTSTRAP_TOKEN` from `/opt/bananawiki/config/app.env` to create the first platform administrator. Review registration, approval, and resource policies in the portal before opening it to users.

The apex static website lives in `/opt/bananawiki/site`. Initially it contains the editable `site/` placeholder. Deploy your own website there; application updates preserve it. The customer help centre is part of the portal itself at `/help` and needs no configuration or separate hosting.

The portal keeps a little state for each wiki outside the wiki's own data folder, where the tenant cannot change it: the plugin safety snapshots it takes and the plugin quarantine markers (see [hosting](hosting.md#plugins-and-tenant-code)). It lives in `platform_state` next to the hosting database, `/opt/bananawiki/data/platform_state` with the installer, and `HOSTING_PLATFORM_STATE_DIR` moves it. It must not be inside `INSTANCES_DIR`; the portal refuses to start if it is. `HOSTING_TENANT_PLUGIN_DENYLIST` takes a comma-separated list of plugin ids that no hosted wiki may install or enable. The wiki compares ids only, so the same code uploaded under another id is not caught: treat the list as a policy switch, not a security boundary.

Hosting backups stop the portal, background jobs, and its tenant containers briefly to capture consistent data. Updates rebuild and restart tenants from the new image and check previously running tenant endpoints before reopening traffic. These operations can take longer on platforms with many wikis.

## Daily commands

| Command | Purpose |
| --- | --- |
| `sudo bananawiki status` | Show mode, revision, update policy, source, and last operation. |
| `sudo bananawiki update` | Fetch the configured branch, back up, deploy, and check readiness. |
| `sudo bananawiki backup --output /safe/wiki.tar.gz` | Create a portable package while writes are stopped. |
| `sudo bananawiki migrate --output /safe/wiki.tar.gz` | The same portable backup, for moving servers. |
| `sudo bananawiki restore /safe/wiki.tar.gz` | Back up this installation, then restore a package. |
| `sudo bananawiki rollback` | Restore code and data saved before the last successful update. |
| `sudo bananawiki start`, `stop`, or `restart` | Manage this installation's services and tenants. |
| `sudo bananawiki recover` | Finish recovery from an interrupted maintenance operation. |
| `sudo bananawiki uninstall` | Remove this installation's services and updater, preserving its files. |

`./banana --help` and each subcommand's `--help` describe the options. Use `--root /opt/another-wiki` before the subcommand and `install --name bananawiki-other` for a separate installation. Each root needs its own service name and application port; merge its proxy configuration into your existing proxy.

## Optional automatic updates

```sh
sudo bananawiki source check
sudo bananawiki updates enable --interval 60
sudo bananawiki updates status
sudo bananawiki updates disable
```

The systemd timer checks at the chosen interval, with a small randomized delay. `source check` verifies access and reports the candidate commit without deploying it. The updater skips an intentionally stopped application. Disabling updates cancels a candidate still being prepared; a transaction already applying changes finishes or rolls back safely. It does not undo a completed update.

Select a fork and branch before enabling updates:

```sh
sudo bananawiki source set --repo https://forge.example.org/team/BananaWiki.git --branch stable --fallback-branch main
sudo bananawiki source check
sudo bananawiki updates enable
```

The updater uses a fallback only if the selected branch disappears. Without a configured fallback, updates stop and leave the deployed version running. An unrelated or rewritten history also pauses updates. Review it and use `update --allow-divergent` only for an intentional manual switch; an automatic run never permits divergence. `source set --clear-fallback` removes the fallback.

Every update prepares dependencies before the maintenance window, creates a complete pre-update package, switches source, and checks readiness. A failed deployment restores matching code and data. The failed commit is not retried automatically. Review the journal and fix the cause before running `update --retry-failed`, or wait for a corrected commit. Automatic backups retain the newest three packages by default; `updates enable --keep-backups 7` changes that retention. Manual packages are never pruned automatically. Prepared releases use the same retention count, always preserving the active and previous releases. Keep an additional copy off the server.

## What a bad commit can and cannot do

The updater assumes the source repository can go wrong, whether through a
mistake or through someone who should not have push access.

A commit that deletes the application cannot take the deployment with it.
Nothing is switched until a release has been prepared from the new revision,
and a revision that is empty or missing the managed entry point is refused at
that point, with the running version untouched. A revision that installs and
starts but fails its readiness checks is rolled back to the previous code and
its matching database. Either way the failed revision is recorded and never
retried automatically. Wiki data, uploads and configuration live outside the
source tree, so no commit can delete them.

Rewritten history is refused as well. An automatic update only moves forward
from the installed revision, so a force-push that replaces history pauses
updates instead of applying them, and switching to unrelated history takes a
deliberate `update --allow-divergent` from a maintainer.

None of that catches a well-formed commit that does exactly what it says and
is also hostile, such as code that installs cleanly, passes readiness and then
does something you did not want. If the repository you follow is
compromised, the updater will deploy what it finds there. Against that, require
signatures:

```sh
sudo bananawiki source set --require-signatures /root/allowed_signers
```

The file is an SSH allowed-signers file, one line per key you trust:

```text
you@example.org namespaces="git" ssh-ed25519 AAAAC3Nza...
```

With it in place, every revision must carry a valid SSH signature from a listed
key before anything is fetched into a release. An unsigned commit, or one
signed by a key you have not listed, stops the update and leaves the running
version alone. Sign your releases with `git commit -S` after setting
`gpg.format=ssh` and `user.signingkey`. `source set --clear-signatures` returns
to unsigned updates.

This checks SSH signatures only. An OpenPGP-signed commit is deliberately
refused, and the message says so. Git picks its verifier from the signature
itself, so an OpenPGP signature would be checked against whatever the server's
GnuPG keyring happens to trust instead of against the file you configured. If
you sign with OpenPGP today, move the signing key to SSH format for the server
you want protected.

Keep the allowed-signers file on the server, owned by the operator and mode
0600; the updater refuses to read it otherwise, and refuses to update rather
than carrying on unverified if it goes missing. Signature checks protect the
code only. A compromised token still lets someone read a private repository.

## Private repository updates

A private source works over HTTPS with a read-only repository token or SSH with a deploy key. The token/key belongs to the server updater and is independent of BananaVibe's bot credential.

```sh
sudo bananawiki source set --repo https://github.com/YOUR_TEAM/BananaWiki.git --branch main --token-file /root/banana-repo.token --username YOUR_BOT
sudo bananawiki source check
```

Create the token file privately with mode `0600`; do not put the token in the URL or command line. GitHub fine-grained tokens need repository Contents read access; Forgejo tokens need repository read access on the selected private repository. The username is the account required by your forge (GitHub App tokens commonly use `x-access-token`).

For SSH, verify the forge's host key through a trusted source and save it in a known-hosts file:

```sh
sudo bananawiki source set --repo git@forge.example.org:team/BananaWiki.git --branch main --ssh-key /root/banana-deploy-key --known-hosts /root/banana-known-hosts
sudo bananawiki source check
```

The updater stores credentials in root-private files under `config/`, uses an isolated Git credential helper, refuses credential-bearing HTTPS URLs and redirects, and enforces SSH host verification. Application processes do not receive repository credentials. Rotating access means repeating `source set` with the new file. `source set --clear-credentials` removes stored access. Changing the repository host without new credentials disables authentication rather than sending an old credential to the new host.

## The nginx and systemd wizard

`setup_wizard.py` in the repository root is an older helper that predates the
`banana` installer. It serves a one-off page on loopback, asks for a service
name, a virtual environment path, a WSGI target and a domain, then writes a
systemd unit and an nginx server block and requests a certificate with certbot.
It is generic and will deploy any WSGI application.

Prefer `banana install`. It knows about this application's data directory,
setup token, backups and updates, and the wizard does not. The wizard is kept
for servers that already run nginx and where the operator wants the unit and
the server block written for them:

```sh
python3 setup_wizard.py --host 127.0.0.1 --port 5050
```

`--non-interactive` takes the same answers as flags and skips the page, which
is what to use from a provisioning script. `python3 setup_wizard.py --help`
lists them.

It needs root for the steps that install files under `/etc`, and it makes no
attempt to preserve an existing configuration of the same name. Read what it
previews before letting it write anything.

## Data, migration, and removal

For optional encrypted off-server backups, follow [repository backups](backups.md). Install age, configure a dedicated private GitHub/Forgejo repository and recovery key, run `bananawiki backups run`, and rehearse restore before enabling `backups enable`. Both single Wiki and Hosting use the same commands; Hosting includes all managed wikis and the deployed static site. Hosting can also use Google Drive.

`/opt/bananawiki/data` contains databases, uploads, session keys, plugins, and hosted wikis. `config/app.env` holds application settings; `config/source.json` and `config/updates.json` hold source and update policy. Prepared source is under `releases/`; `current` selects the active release. Keep mutable application paths inside `data` and website files inside `site` so portable packages cover them.

Follow [server migration](server-migration.md) for install-and-restore, restore-after-install, and older deployments. Packages contain secrets and repository credentials. They have mode `0600` but are **not encrypted**; transfer them over SSH or another protected channel and store them privately.

`uninstall` disables the updater and backup schedule and removes the managed services and command, preserving data, configuration, releases, and backups. If this command installed the Caddyfile and it has not since been edited, uninstall restores its previous configuration. An operator-edited proxy is retained for manual adjustment. System accounts, shared Docker/Caddy/Ollama packages, and other applications are not removed.

Permanent removal requires the explicit installation name:

```sh
sudo bananawiki uninstall --purge --confirm bananawiki
```

That deletes the installation root, including its local backups. Copy the migration package elsewhere first. A preserved installation can be installed again from a clean checkout using the same root, mode, and name with `install --reuse-data`.

## Moving to a new domain

`legacy_redirect/` is a small optional daemon, separate from the application
and from the installer. It serves a "we moved" page on every wildcard
subdomain of a domain you have left, with a link to the same subdomain on the
new one, so `team.old.example.com` sends visitors to `team.wiki.example.net`.
It uses only the standard library and holds no data.

```sh
sudo bash legacy_redirect/manage.sh install
sudo bash legacy_redirect/manage.sh add old.example.com
sudo bash legacy_redirect/manage.sh add legacy.example.org
sudo bash legacy_redirect/manage.sh set-target wiki.example.net
sudo bash legacy_redirect/manage.sh status
```

Both settings are required and the shipped unit file carries placeholders, so
the service refuses to start until you set your own domains. A default here
would send your visitors to a domain you do not control, which is worse than
not starting. The daemon listens on `LEGACY_BIND_PORT` (8090 by default).
Point a proxy server block for the old domain and its wildcard at that port,
and keep a certificate for the old names for as long as you want the redirect
to work.

Run it in the foreground to check it before installing:

```sh
python -m legacy_redirect --old old.example.com --new wiki.example.net
```

Remove it with `sudo bash legacy_redirect/uninstall.sh`.

## License and source availability

AGPL-3.0-only permits personal and commercial use, including a paid service using this same source. Keep `BW_SOURCE_URL` pointing to the corresponding source users are entitled to obtain. A private update repository does not replace the source offer required for a modified network deployment; publish an accessible corresponding-source copy or provide authorized access. The updater preserves an explicitly customized source-offer URL.
