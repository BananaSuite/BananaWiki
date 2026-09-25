# Scripts

None of this is needed to start a wiki by hand. Two of them are entry points a
deployment runs on your behalf; the rest are tools that grew out of operating
the thing.

## Run by something else

- `hosted_container_entrypoint.py`: the entry point inside a tenant container.
  It runs the web process and that tenant's durable TTS worker together.
- `tts_worker.py`: the durable text to speech worker for a standalone
  installation, where the application does not run it in process.
- `sync_backups.py` and `sync_lifecycle.py`: keep the source shared with
  BananaChat and BananaVibe identical. CI runs both with `--check`. See
  [contributing](../CONTRIBUTING.md) before using `--record` or `--write`.

## Checks

- `check_navigation_browser.py`: drives Chromium over a disposable instance to
  check sidebar paging, reordering and category edits. CI runs it.
- `smoke_test_easy_deployment.py`: unpacks a built portable launcher and checks
  it starts. CI runs it after the build.
- `production_readiness.py`: a fail closed gate for the hosting platform. It
  refuses to pass on a configuration that is not ready to face the public.
- `validate_runtime_baseline.py`: compares a running deployment against the
  hardened defaults it should have.
- `io_health_check.py`: watches the database directory for the write stalls
  that show up as random slow requests.

## Operating an installation

- `manage_pending_deletions.py`: lists and processes accounts and content
  waiting out their deletion delay.
- `observability_snapshot.py`: prints the in-process counters as JSON, for
  feeding somewhere that keeps history.
- `build_incident_timeline.py`: reconstructs what happened from the logs after
  an incident.
- `hostingctl.py`: a shorter way to call the hosting administration CLI.
- `export_deploy_bundle.py`: exports a bundle in the old pre-`banana` format.
  New installations use `bananawiki backup`.

## Building and content

- `build_easy_deployment.py`: builds the portable launcher with PyInstaller.
- `seed_dev_db.py`: fills a development database with demo content, which is
  what the screenshots are taken against.
- `seed_badges.py`: inserts the default badge types.
- `sync_user_guide_docs.py`: writes the user guide Markdown files back out from
  the database copies.
- `obsidian_sync.py`: the experimental Obsidian vault sync described in
  [OBSIDIAN_SETUP.md](../docs/OBSIDIAN_SETUP.md).
- `generate_favicons.py` and `regen_favicons.py`: redraw the favicon set. The
  second one is the newer of the two.

## One-off tools

- `migrate_corsi_to_bananawiki.py`: builds hosting import archives from the
  legacy site this project replaced. Useful only to whoever has that data.
- `rename_project.py`: rewrites the product name through the tree. It exists
  because the name changed once and may change again.
