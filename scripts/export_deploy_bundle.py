#!/usr/bin/env python3
"""Export a legacy deployment bundle. New managed installations use banana backup."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import zipfile


ROOT = Path(__file__).resolve().parents[1]


def _iter_files(path: Path):
    if not path.exists():
        return
    if path.is_file():
        yield path, path.name
        return
    for p in sorted(path.rglob("*")):
        if p.is_file() and not p.is_symlink():
            yield p, str(p.relative_to(path)).replace(os.sep, "/")


def _sqlite_snapshot(source: Path, destination: Path) -> None:
    """Create a consistent SQLite snapshot while the service remains online."""
    source_conn = sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True, timeout=30)
    target_conn = sqlite3.connect(destination)
    try:
        source_conn.backup(target_conn)
    finally:
        target_conn.close()
        source_conn.close()


def _write_tree(zf: zipfile.ZipFile, source: Path, archive_root: str, temp: Path) -> None:
    """Write a data tree, snapshotting live SQLite files and skipping journals."""
    for absolute, relative in _iter_files(source):
        if relative.endswith(("-wal", "-shm")):
            continue
        archive_name = f"{archive_root}/{relative}"
        if absolute.suffix.lower() == ".db":
            snapshot = temp / f"snapshot-{len(list(temp.iterdir()))}.db"
            try:
                _sqlite_snapshot(absolute, snapshot)
                zf.write(snapshot, archive_name)
                continue
            except sqlite3.Error:
                # Non-SQLite files occasionally use a .db suffix. Preserve the
                # bytes rather than making the whole migration impossible.
                pass
        zf.write(absolute, archive_name)


def main() -> int:
    default_out = ROOT / "backups" / f"deploy_bundle_{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.zip"
    parser = argparse.ArgumentParser(description="Export BananaWiki deploy bundle zip")
    parser.add_argument("--output", default=str(default_out), help="Output zip path")
    parser.add_argument("--root", default=str(ROOT), help="Repository/app root path")
    parser.add_argument("--instances-dir", help="Hosted instance data directory")
    parser.add_argument("--landing-dir", help="Operator static website directory (default: /srv/bananawiki-site)")
    parser.add_argument("--deploy-config", default="/etc/bananawiki/deploy.conf")
    parser.add_argument("--sites-config", default="/etc/bananawiki/sites.conf")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    out_path = Path(args.output).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    wiki_db = root / "instance" / "bananawiki.db"
    wiki_uploads = root / "app" / "static" / "uploads"
    wiki_secret = root / "instance" / ".secret_key"
    hosting_db = root / "hosting" / "data" / "hosting.db"
    hosting_secret = root / "hosting" / "data" / ".secret_key"
    hosting_backup_key = root / "hosting" / "data" / ".backup_encryption_key"
    instances = Path(args.instances_dir).resolve() if args.instances_dir else root / "instances"
    landing = Path(args.landing_dir).resolve() if args.landing_dir else Path(os.environ.get("STATIC_SITE_DIR", "/srv/bananawiki-site"))
    deploy_config = Path(args.deploy_config)
    sites_config = Path(args.sites_config)
    hosting_env = root / "hosting" / ".env"
    sites_data = root / "sites"

    manifest = {
        "bundle_version": 3,
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "paths": {
            "wiki_db": str(wiki_db),
            "wiki_uploads": str(wiki_uploads),
            "wiki_secret_key": str(wiki_secret),
            "hosting_db": str(hosting_db),
            "hosting_secret_key": str(hosting_secret),
            "hosting_backup_key": str(hosting_backup_key),
            "instances": str(instances),
            "landing": str(landing),
            "deploy_config": str(deploy_config),
            "sites_config": str(sites_config),
        },
    }

    with tempfile.TemporaryDirectory(prefix="bananawiki-export-") as temp_dir:
        temp = Path(temp_dir)
        with zipfile.ZipFile(out_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("manifest.json", json.dumps(manifest, indent=2))

            if wiki_db.exists():
                snapshot = temp / "wiki.db"
                _sqlite_snapshot(wiki_db, snapshot)
                zf.write(snapshot, "wiki/bananawiki.db")

            if wiki_secret.exists():
                zf.write(wiki_secret, "wiki/.secret_key")

            if hosting_db.exists():
                snapshot = temp / "hosting.db"
                _sqlite_snapshot(hosting_db, snapshot)
                zf.write(snapshot, "hosting/hosting.db")

            if hosting_secret.exists():
                zf.write(hosting_secret, "hosting/.secret_key")
            if hosting_backup_key.exists():
                zf.write(hosting_backup_key, "hosting/.backup_encryption_key")
            if hosting_env.exists():
                zf.write(hosting_env, "configuration/hosting.env")
            if deploy_config.is_file():
                zf.write(deploy_config, "configuration/deploy.conf")
            if sites_config.is_file():
                zf.write(sites_config, "configuration/sites.conf")

            _write_tree(zf, wiki_uploads, "wiki/uploads", temp)
            _write_tree(zf, instances, "hosting/instances", temp)
            _write_tree(zf, landing, "landing", temp)
            _write_tree(zf, sites_data, "sites-data", temp)

    os.chmod(out_path, 0o600)

    print(str(out_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
