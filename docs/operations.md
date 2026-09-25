# BananaWiki operations

Managed servers use `bananawiki` for both standalone and hosting deployments. See [deployment](deployment.md) for installation, optional automatic updates, private repository authentication, and removal; see [server migration](server-migration.md) for portable packages and legacy imports.

## Inspect and maintain

```sh
sudo bananawiki status
sudo journalctl -u bananawiki -n 100
sudo bananawiki update
sudo bananawiki backup --output /safe/bananawiki.tar.gz
```

Use your chosen service name if installation used `--name`. A standalone wiki also runs a `-tts` service. Hosting runs a `-maintenance` service and separately isolated tenant containers. `bananawiki start`, `stop`, and `restart` handle the remembered set together. The web administrator's restart control refreshes its application; source updates remain an operator action or an explicitly enabled update policy.

Configuration and operation records are under `/opt/bananawiki/config`. `status.json` records the latest operation and `history.jsonl` records prior maintenance results. `app.env` contains private settings; edit it as the operator and restart to apply a setting. Keep runtime data under the managed `data/` directory and website files under `site/`. Do not edit installed `current/` files: use a reviewed commit in your update source.

During maintenance, user traffic receives a temporary 503 while health endpoints remain available for readiness checks. The updater does not reopen traffic until the candidate is ready, or matching old code and data have been restored. Automatic updates skip known failed revisions and intentionally stopped services. Use `recover` after a disrupted transaction; never manually delete its journal to conceal an incomplete update.

Hosting starts tenants in bounded recovery waves (`BW_HOSTING_RECOVERY_MAX_WORKERS`, default 2). `INSTANCE_STARTUP_TIMEOUT_SECONDS` controls each tenant's startup allowance, from 30 to 600 seconds; the deployment check allows for the required waves and retries, capped at two hours. A healthy deployment proceeds immediately. If readiness fails, `config/last-readiness-failure.json` preserves the failed service/endpoint checks across rollback. On slow or emulated hosts, increase the startup allowance before updating instead of repeatedly recycling a healthy process that is still loading.

## Backups and verification

Keep off-server backups, check that they can be restored, and retain the corresponding source revision. Portable command-line packages contain secrets and are not encrypted. Platform administrator exports remain a separate encrypted format requiring their backup key. A wiki-level export is a content-migration ZIP, not an installation package.

After an update or migration, verify sign-in, page edits/history, uploads, plugin behavior, and a fresh backup. For hosting also check tenant start/stop, scheduled maintenance, and custom domains. Review release changes before manual updates; use a controlled fork or branch when you need to keep a particular feature set.

The runtime baseline helper can inspect a checkout without root:

```sh
python scripts/validate_runtime_baseline.py --repo-root .
```

The incident timeline helper accepts application, proxy, or journal export files:

```sh
python scripts/build_incident_timeline.py --log /private/incident.log --output /private/timeline.json
```

Keep logs private because application events can contain user and operational information.
