"""Hosting dashboard: archives."""

import json
import logging
import os
import secrets
import shutil
import tempfile
import threading
import time
import zipfile
from io import BytesIO
from flask import (
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
    flash,
)
from helpers._passwords import check_password_hash
from .. import config
from .auth import hosting_admin_required, hosting_rate_limit, get_current_account
from ..instance_manager import (
    provision_instance_from_archive,
)
from ..db import (
    get_instance_by_subdomain,
    get_account_by_id,
)
from ..subdomain import validate_subdomain

logger = logging.getLogger(__name__)
from .dashboard_common import (
    _cleanup_download_artifacts,
    _stream_download_file,
)


_IMPORT_UPLOAD_MAX_AGE_SECONDS = 24 * 60 * 60


def _chunked_import_root():
    root = config.HOSTING_IMPORT_TEMP_DIR
    os.makedirs(root, exist_ok=True)
    return root


def _is_safe_upload_id(upload_id):
    return (
        isinstance(upload_id, str)
        and 16 <= len(upload_id) <= 96
        and all(ch.isalnum() or ch in "-_" for ch in upload_id)
    )


def _chunked_import_dir(upload_id):
    if not _is_safe_upload_id(upload_id):
        return None
    return os.path.join(_chunked_import_root(), upload_id)


def _chunked_import_meta_path(upload_dir):
    return _safe_path_within(upload_dir, "metadata.json")


def _chunked_import_status_path(upload_dir):
    return _safe_path_within(upload_dir, "status.json")


def _safe_path_within(base_dir, *parts):
    """Resolve *parts* inside *base_dir* and reject path traversal."""
    base = os.path.realpath(base_dir)
    candidate = os.path.realpath(os.path.join(base, *parts))
    if candidate != base and not candidate.startswith(base + os.sep):
        raise ValueError("Unsafe path outside upload directory.")
    return candidate


def _write_chunked_import_meta(upload_dir, meta):
    tmp_path = _chunked_import_meta_path(upload_dir) + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as fh:
        json.dump(meta, fh, sort_keys=True)
    os.replace(tmp_path, _chunked_import_meta_path(upload_dir))


def _write_chunked_import_status(upload_dir, status):
    tmp_path = _chunked_import_status_path(upload_dir) + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as fh:
        json.dump(status, fh, sort_keys=True)
    os.replace(tmp_path, _chunked_import_status_path(upload_dir))


def _read_chunked_import_status(upload_dir):
    try:
        with open(_chunked_import_status_path(upload_dir), "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {"state": "pending", "message": "Waiting for import status."}


def _read_chunked_import_meta(upload_id, account_id, *, expected_kind=None):
    upload_dir = _chunked_import_dir(upload_id)
    if not upload_dir:
        return None, None
    meta_path = _chunked_import_meta_path(upload_dir)
    try:
        with open(meta_path, "r", encoding="utf-8") as fh:
            meta = json.load(fh)
    except (OSError, ValueError):
        return None, None
    if str(meta.get("account_id")) != str(account_id):
        return None, None
    kind = meta.get("kind") or "instance_import"
    if expected_kind and kind != expected_kind:
        return None, None
    return upload_dir, meta


def _cleanup_chunked_import_payload(upload_dir):
    for name in ("chunks", "archive.zip"):
        try:
            path = _safe_path_within(upload_dir, name)
        except ValueError:
            continue
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
        else:
            try:
                os.unlink(path)
            except OSError:
                pass


def _expected_chunk_size(meta, chunk_index):
    total_size = int(meta["size"])
    chunk_size = int(meta["chunk_size"])
    total_chunks = int(meta["total_chunks"])
    if chunk_index == total_chunks - 1:
        return total_size - (chunk_size * chunk_index)
    return chunk_size


def _validate_chunked_import_parts(upload_dir, meta):
    chunks_dir = _safe_path_within(upload_dir, "chunks")
    total_chunks = int(meta["total_chunks"])
    missing = []
    invalid = []
    total_bytes = 0
    for index in range(total_chunks):
        part_path = _safe_path_within(chunks_dir, f"{index:08d}.part")
        if not os.path.isfile(part_path):
            missing.append(index)
            continue
        try:
            size = os.path.getsize(part_path)
        except OSError:
            invalid.append(index)
            continue
        total_bytes += size
        if size != _expected_chunk_size(meta, index):
            invalid.append(index)
    if total_bytes > int(meta["size"]):
        invalid.append("total")
    return missing, invalid


def _cleanup_stale_chunked_imports():
    root = _chunked_import_root()
    cutoff = time.time() - _IMPORT_UPLOAD_MAX_AGE_SECONDS
    try:
        entries = os.listdir(root)
    except OSError:
        return
    for entry in entries:
        upload_dir = os.path.join(root, entry)
        try:
            if os.path.getmtime(upload_dir) < cutoff:
                if os.path.isdir(upload_dir):
                    shutil.rmtree(upload_dir, ignore_errors=True)
                else:
                    os.unlink(upload_dir)
        except OSError:
            pass


def _json_import_error(message, status=400):
    return jsonify({"ok": False, "error": message}), status


def _save_uploaded_zip_files(files, work_dir):
    saved = []
    seen = set()
    for upload in files:
        filename = os.path.basename((upload.filename or "").strip())
        if not filename or not filename.lower().endswith((".zip", ".bwenc")):
            continue
        stem, ext = os.path.splitext(filename)
        candidate = filename
        suffix = 1
        while candidate in seen:
            candidate = f"{stem}_{suffix}{ext}"
            suffix += 1
        seen.add(candidate)
        path = os.path.join(work_dir, candidate)
        upload.save(path)
        try:
            size = os.path.getsize(path)
        except OSError:
            size = 0
        if size <= 0:
            raise RuntimeError("Uploaded ZIP file is empty.")
        if size > config.HOSTING_IMPORT_MAX_BYTES:
            max_gb = config.HOSTING_IMPORT_MAX_BYTES / (1024 * 1024 * 1024)
            raise RuntimeError(
                f"A ZIP file is larger than the {max_gb:.1f} GB restore limit."
            )
        saved.append(path)
    return saved


def _zip_has_platform_backup_markers(zip_path):
    try:
        with zipfile.ZipFile(zip_path) as zf:
            for name in zf.namelist():
                normalized = str(name or "").strip("/")
                if not normalized:
                    continue
                if (
                    normalized in {"hosting.db", "hosting/hosting.db"}
                    or normalized.startswith("hosting/instances/")
                    or normalized.startswith("hosting_db/")
                    or normalized.startswith("hosting_config/")
                    or normalized.startswith("instances/")
                    or normalized.startswith("landing/")
                ):
                    return True
    except Exception:
        return False
    return False


def _expand_platform_restore_envelopes(zip_paths, work_dir):
    expanded = []
    extracted_paths = []
    for path in zip_paths:
        if _zip_has_platform_backup_markers(path):
            expanded.append(path)
            continue
        nested = []
        with zipfile.ZipFile(path) as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                member_name = os.path.basename(info.filename or "")
                if not member_name.lower().endswith(".zip"):
                    continue
                nested.append((info, member_name))
            if not nested:
                expanded.append(path)
                continue
            for info, member_name in nested:
                out_name = f"nested_{secrets.token_hex(4)}_{member_name}"
                out_path = os.path.join(work_dir, out_name)
                with zf.open(info, "r") as src, open(out_path, "wb") as dst:
                    shutil.copyfileobj(src, dst, length=1024 * 1024)
                extracted_paths.append(out_path)
                expanded.append(out_path)
    return expanded, extracted_paths


def _restore_platform_from_uploaded_archives(zip_paths, work_dir):
    from ..db import init_hosting_db, restore_hosting_from_backup_zips
    from ..backup_crypto import is_encrypted_backup, decrypt_backup

    decrypted = []
    normalized_paths = []
    for index, path in enumerate(zip_paths):
        if is_encrypted_backup(path):
            output = os.path.join(work_dir, f"decrypted-{index:04d}.zip")
            decrypt_backup(path, output)
            decrypted.append(output)
            normalized_paths.append(output)
        else:
            normalized_paths.append(path)

    restore_inputs, extracted = _expand_platform_restore_envelopes(
        normalized_paths, work_dir
    )
    extracted.extend(decrypted)
    if not restore_inputs:
        raise RuntimeError("No valid ZIP files uploaded.")
    zfs = []
    try:
        for path in restore_inputs:
            zfs.append(zipfile.ZipFile(path))
        restore_hosting_from_backup_zips(zfs)
        init_hosting_db()
    finally:
        for zf in zfs:
            try:
                zf.close()
            except Exception:
                pass
        for path in extracted:
            try:
                os.unlink(path)
            except OSError:
                pass


def _run_chunked_import_job(upload_dir, meta, account_id, account_is_admin, upload_id):
    chunks_dir = _safe_path_within(upload_dir, "chunks")
    archive_path = _safe_path_within(upload_dir, "archive.zip")
    total_chunks = int(meta["total_chunks"])
    try:
        _write_chunked_import_status(
            upload_dir,
            {
                "state": "assembling",
                "message": "Assembling uploaded archive.",
            },
        )
        with open(archive_path, "wb") as out:
            for index in range(total_chunks):
                part_path = _safe_path_within(chunks_dir, f"{index:08d}.part")
                if os.path.getsize(part_path) != _expected_chunk_size(meta, index):
                    raise RuntimeError(
                        "The uploaded archive chunks are incomplete or corrupted."
                    )
                with open(part_path, "rb") as part:
                    shutil.copyfileobj(part, out, length=1024 * 1024)
        if os.path.getsize(archive_path) != int(meta["size"]):
            raise RuntimeError(
                "The assembled archive size does not match the uploaded file."
            )

        _write_chunked_import_status(
            upload_dir,
            {
                "state": "importing",
                "message": "Importing wiki archive.",
            },
        )
        inst, error = provision_instance_from_archive(
            account_id,
            meta.get("subdomain", ""),
            archive_path,
            domain_mode=meta.get("domain_mode", "hosting"),
            account_is_admin=account_is_admin,
        )
        if error:
            _write_chunked_import_status(
                upload_dir,
                {
                    "state": "error",
                    "error": error,
                },
            )
            return

        _write_chunked_import_status(
            upload_dir,
            {
                "state": "complete",
                "message": "Import complete.",
                "url": inst["url"],
            },
        )
    except Exception as exc:
        logger.exception("Failed to complete chunked import %s", upload_id)
        _write_chunked_import_status(
            upload_dir,
            {
                "state": "error",
                "error": str(exc) or "Failed to import the uploaded archive.",
            },
        )
    finally:
        _cleanup_chunked_import_payload(upload_dir)


def _run_chunked_restore_platform_job(upload_dir, meta, upload_id):
    from ..backups import notify_change

    chunks_dir = _safe_path_within(upload_dir, "chunks")
    archive_path = _safe_path_within(upload_dir, "archive.zip")
    restore_dir = _safe_path_within(upload_dir, "restore_parts")
    total_chunks = int(meta["total_chunks"])
    os.makedirs(restore_dir, exist_ok=True)
    try:
        _write_chunked_import_status(
            upload_dir,
            {
                "state": "assembling",
                "message": "Assembling uploaded backup archive.",
            },
        )
        with open(archive_path, "wb") as out:
            for index in range(total_chunks):
                part_path = _safe_path_within(chunks_dir, f"{index:08d}.part")
                if os.path.getsize(part_path) != _expected_chunk_size(meta, index):
                    raise RuntimeError(
                        "The uploaded backup chunks are incomplete or corrupted."
                    )
                with open(part_path, "rb") as part:
                    shutil.copyfileobj(part, out, length=1024 * 1024)
        if os.path.getsize(archive_path) != int(meta["size"]):
            raise RuntimeError(
                "The assembled backup size does not match the uploaded file."
            )

        _write_chunked_import_status(
            upload_dir,
            {
                "state": "importing",
                "message": "Restoring hosting platform backup.",
            },
        )
        _restore_platform_from_uploaded_archives([archive_path], restore_dir)
        notify_change(
            "platform_migration_import", "Full platform restoration performed"
        )
        _write_chunked_import_status(
            upload_dir,
            {
                "state": "complete",
                "message": "Platform restoration completed successfully.",
            },
        )
    except Exception as exc:
        logger.exception("Failed to complete chunked platform restore %s", upload_id)
        _write_chunked_import_status(
            upload_dir,
            {
                "state": "error",
                "error": str(exc) or "Failed to restore the uploaded platform backup.",
            },
        )
    finally:
        _cleanup_chunked_import_payload(upload_dir)
        shutil.rmtree(restore_dir, ignore_errors=True)


def register_hosting_archives_routes(app):
    """Register hosting routes for archives."""

    @app.route("/admin/instances/import", methods=["GET", "POST"])
    @hosting_admin_required
    @hosting_rate_limit(max_requests=5, window=300)
    def hosting_admin_import_instance():
        """Admin: provision a new instance by uploading a wiki ZIP archive.

        The ZIP must use the same structure produced by the "Download
        archive" admin action.  Admins can optionally claim an apex
        subdomain (``wiki.example.com``) provided it is not already in
        use.
        """
        account_id = session["hosting_account_id"]
        account = get_account_by_id(account_id)
        account_is_admin = bool(account and account["is_admin"])
        selected_domain_mode = "hosting"

        if request.method == "GET":
            return render_template(
                "admin_import_instance.html",
                base_domain=config.BASE_DOMAIN,
                instance_url_suffix=config.INSTANCE_URL_SUFFIX,
                hosting_mode=config.HOSTING_MODE,
                account_is_admin=account_is_admin,
                selected_domain_mode=selected_domain_mode,
            )

        upload = request.files.get("archive")
        if upload is None or not upload.filename:
            flash("Choose a ZIP archive to import.", "error")
            return redirect(url_for("hosting_admin_import_instance"))
        if not os.path.basename(upload.filename).lower().endswith(".zip"):
            flash("Choose a ZIP archive to import.", "error")
            return redirect(url_for("hosting_admin_import_instance"))

        subdomain = (request.form.get("subdomain") or "").strip().lower()
        requested_mode = (request.form.get("domain_mode") or "hosting").strip().lower()
        if requested_mode not in ("hosting", "apex"):
            requested_mode = "hosting"

        # Persist to a temp file before handing to the importer so the
        # path is stable across the long-running operation.
        tmp_fd, tmp_path = tempfile.mkstemp(
            prefix="bwh-import-",
            suffix=".zip",
            dir=_chunked_import_root(),
        )
        os.close(tmp_fd)
        try:
            upload.save(tmp_path)
            try:
                _tmp_size = os.path.getsize(tmp_path)
            except OSError:
                _tmp_size = 0
            if _tmp_size > config.HOSTING_IMPORT_MAX_BYTES:
                max_gb = config.HOSTING_IMPORT_MAX_BYTES / (1024 * 1024 * 1024)
                flash(
                    f"The archive is larger than the {max_gb:.1f} GB import limit.",
                    "error",
                )
                return redirect(url_for("hosting_admin_import_instance"))
            inst, error = provision_instance_from_archive(
                account_id,
                subdomain,
                tmp_path,
                domain_mode=requested_mode,
                account_is_admin=account_is_admin,
            )
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

        if error:
            flash(error, "error")
            return redirect(url_for("hosting_admin_import_instance"))

        flash(f"Imported wiki created at {inst['url']}.", "success")
        return redirect(url_for("hosting_admin"))

    @app.route("/admin/instances/import/chunk/start", methods=["POST"])
    @hosting_admin_required
    @hosting_rate_limit(max_requests=20, window=300)
    def hosting_admin_import_chunk_start():
        """Start a resumable admin import upload.

        The browser uses this for ZIPs that are too large for a single
        Cloudflare/proxy request.  Each upload is scoped to the current admin
        account and cleaned up after completion or after a stale timeout.
        """
        account_id = session["hosting_account_id"]
        account = get_account_by_id(account_id)
        account_is_admin = bool(account and account["is_admin"])
        filename = os.path.basename((request.form.get("filename") or "").strip())
        subdomain = (request.form.get("subdomain") or "").strip().lower()
        requested_mode = (request.form.get("domain_mode") or "hosting").strip().lower()
        if requested_mode not in ("hosting", "apex"):
            requested_mode = "hosting"

        try:
            total_size = int(request.form.get("size") or "0")
        except (TypeError, ValueError):
            return _json_import_error("Invalid archive size.")

        if not filename.lower().endswith(".zip"):
            return _json_import_error("Choose a ZIP archive to import.")
        if total_size <= 0:
            return _json_import_error("The uploaded file is empty.")
        if total_size > config.HOSTING_IMPORT_MAX_BYTES:
            max_gb = config.HOSTING_IMPORT_MAX_BYTES / (1024 * 1024 * 1024)
            return _json_import_error(
                f"The archive is larger than the {max_gb:.1f} GB import limit."
            )

        ok, reason = validate_subdomain(
            subdomain,
            account_is_admin=account_is_admin,
            domain_mode=requested_mode,
        )
        if not ok:
            return _json_import_error(reason)
        if get_instance_by_subdomain(subdomain, domain_mode=requested_mode):
            return _json_import_error("That name is already in use.")

        chunk_size = max(1024 * 1024, int(config.HOSTING_IMPORT_CHUNK_BYTES))
        total_chunks = (total_size + chunk_size - 1) // chunk_size
        upload_id = secrets.token_urlsafe(24)
        upload_dir = os.path.join(_chunked_import_root(), upload_id)
        os.makedirs(os.path.join(upload_dir, "chunks"), exist_ok=True)
        _write_chunked_import_meta(
            upload_dir,
            {
                "kind": "instance_import",
                "account_id": account_id,
                "created_at": time.time(),
                "domain_mode": requested_mode,
                "filename": filename,
                "size": total_size,
                "subdomain": subdomain,
                "chunk_size": chunk_size,
                "total_chunks": total_chunks,
            },
        )
        _cleanup_stale_chunked_imports()
        return jsonify(
            {
                "ok": True,
                "upload_id": upload_id,
                "chunk_size": chunk_size,
                "total_chunks": total_chunks,
            }
        )

    @app.route("/admin/instances/import/chunk/<upload_id>", methods=["POST"])
    @hosting_admin_required
    @hosting_rate_limit(max_requests=1000, window=3600)
    def hosting_admin_import_chunk(upload_id):
        """Receive one ZIP chunk for a resumable admin import."""
        account_id = session["hosting_account_id"]
        upload_dir, meta = _read_chunked_import_meta(
            upload_id,
            account_id,
            expected_kind="instance_import",
        )
        if not meta:
            return _json_import_error("Import upload session was not found.", 404)

        try:
            chunk_index = int(request.form.get("chunk_index") or "-1")
        except (TypeError, ValueError):
            return _json_import_error("Invalid chunk index.")
        total_chunks = int(meta["total_chunks"])
        if chunk_index < 0 or chunk_index >= total_chunks:
            return _json_import_error("Chunk index is out of range.")

        chunk = request.files.get("chunk")
        if chunk is None or not chunk.filename:
            return _json_import_error("No chunk was uploaded.")

        expected_size = _expected_chunk_size(meta, chunk_index)
        try:
            chunk_path = _safe_path_within(
                upload_dir, "chunks", f"{chunk_index:08d}.part"
            )
        except ValueError:
            return _json_import_error("Import upload session is invalid.", 400)
        tmp_chunk_path = f"{chunk_path}.uploading"
        try:
            chunk.save(tmp_chunk_path)
            chunk_bytes = os.path.getsize(tmp_chunk_path)
        except OSError:
            try:
                os.unlink(tmp_chunk_path)
            except OSError:
                pass
            return _json_import_error("Failed to save uploaded chunk.", 500)
        if chunk_bytes != expected_size:
            try:
                os.unlink(tmp_chunk_path)
            except OSError:
                pass
            return _json_import_error("Uploaded chunk has an invalid size.")
        os.replace(tmp_chunk_path, chunk_path)

        received = 0
        chunks_dir = _safe_path_within(upload_dir, "chunks")
        try:
            received = len([n for n in os.listdir(chunks_dir) if n.endswith(".part")])
        except OSError:
            pass
        return jsonify(
            {
                "ok": True,
                "received_chunks": received,
                "total_chunks": total_chunks,
            }
        )

    @app.route("/admin/instances/import/chunk/<upload_id>/complete", methods=["POST"])
    @hosting_admin_required
    @hosting_rate_limit(max_requests=10, window=3600)
    def hosting_admin_import_chunk_complete(upload_id):
        """Start assembling/provisioning a resumable import upload."""
        account_id = session["hosting_account_id"]
        account = get_account_by_id(account_id)
        account_is_admin = bool(account and account["is_admin"])
        upload_dir, meta = _read_chunked_import_meta(
            upload_id,
            account_id,
            expected_kind="instance_import",
        )
        if not meta:
            return _json_import_error("Import upload session was not found.", 404)

        missing, invalid = _validate_chunked_import_parts(upload_dir, meta)
        if missing:
            return _json_import_error("The import upload is incomplete.")
        if invalid:
            return _json_import_error("The import upload contains corrupted chunks.")

        current_status = _read_chunked_import_status(upload_dir)
        if current_status.get("state") in ("assembling", "importing"):
            return jsonify(
                {
                    "ok": True,
                    "status_url": url_for(
                        "hosting_admin_import_chunk_status", upload_id=upload_id
                    ),
                }
            )

        _write_chunked_import_status(
            upload_dir,
            {
                "state": "queued",
                "message": "Import queued.",
            },
        )
        worker = threading.Thread(
            target=_run_chunked_import_job,
            args=(
                upload_dir,
                meta,
                account_id,
                account_is_admin,
                upload_id,
            ),
            daemon=True,
        )
        worker.start()
        return jsonify(
            {
                "ok": True,
                "status_url": url_for(
                    "hosting_admin_import_chunk_status", upload_id=upload_id
                ),
            }
        )

    @app.route("/admin/instances/import/chunk/<upload_id>/status", methods=["GET"])
    @hosting_admin_required
    @hosting_rate_limit(max_requests=1000, window=3600)
    def hosting_admin_import_chunk_status(upload_id):
        """Return status for a background chunked import job."""
        account_id = session["hosting_account_id"]
        upload_dir, meta = _read_chunked_import_meta(
            upload_id,
            account_id,
            expected_kind="instance_import",
        )
        if not meta:
            return _json_import_error("Import upload session was not found.", 404)

        status = _read_chunked_import_status(upload_dir)
        if status.get("state") == "complete" and status.get("url"):
            flash(f"Imported wiki created at {status['url']}.", "success")
        return jsonify(
            {
                "ok": True,
                "state": status.get("state", "pending"),
                "message": status.get("message", ""),
                "error": status.get("error", ""),
                "redirect": url_for("hosting_admin")
                if status.get("state") == "complete"
                else "",
                "url": status.get("url", ""),
            }
        )

    @app.route("/admin/export-platform", methods=["POST"])
    @hosting_admin_required
    @hosting_rate_limit(max_requests=5, window=60)
    def hosting_admin_export_platform():
        """Admin: download a complete encrypted hosting backup.

        Includes consistent SQLite snapshots, instance data, and session keys.
        When the backup needs multiple parts (large instances), they are
        wrapped into one outer ZIP so the admin always gets a single
        file to upload to ``/admin/restore-platform`` on the target
        host.
        """
        import zipfile
        from datetime import datetime, timezone
        from ..backups import build_full_backup, notify_change, logger as sync_logger
        from ..backup_crypto import encrypt_backup

        try:
            parts = build_full_backup(reason="manual_export")
        except Exception:
            sync_logger.exception("Manual hosting backup failed to build")
            flash("Failed to build the hosting backup. Check the logs.", "error")
            return redirect(url_for("hosting_admin_settings"))

        if not parts:
            flash("There is nothing to back up yet.", "error")
            return redirect(url_for("hosting_admin_settings"))

        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

        envelope_dir = None
        envelope_path = None
        if len(parts) == 1:
            try:
                # Single part: stream the ZIP directly so it can be
                # restored by upload without re-extraction.
                src = parts[0]
                encrypted = encrypt_backup(src)
                download_name = f"bananawiki_hosting_backup_{timestamp}.zip.bwenc"
                try:
                    notify_change(
                        "platform_migration_export",
                        "Full hosting backup downloaded by admin (single part)",
                    )
                except Exception:
                    sync_logger.exception(
                        "Failed to record manual hosting backup export",
                    )
                return _stream_download_file(
                    encrypted,
                    download_name,
                    mimetype="application/octet-stream",
                    cleanup_paths=tuple(parts) + (encrypted,),
                )
            except Exception:
                sync_logger.exception(
                    "Manual hosting backup failed to prepare download"
                )
                _cleanup_download_artifacts(parts)
                flash(
                    "Failed to prepare the hosting backup download. Check the logs.",
                    "error",
                )
                return redirect(url_for("hosting_admin_settings"))

        try:
            # Multi-part: wrap into one outer ZIP so the admin only
            # needs to manage a single file.  Restore on the target
            # host accepts a list of ZIPs in any order, so we can ship
            # them as siblings inside an envelope.
            envelope_dir = tempfile.mkdtemp(prefix="bwh-platform-export-")
            archive_name = f"bananawiki_hosting_backup_{timestamp}_envelope.zip"
            envelope_path = os.path.join(envelope_dir, archive_name)
            with zipfile.ZipFile(
                envelope_path, "w", zipfile.ZIP_STORED, allowZip64=True
            ) as outer:
                for src in parts:
                    outer.write(src, os.path.basename(src))
            try:
                notify_change(
                    "platform_migration_export",
                    f"Full hosting backup downloaded by admin ({len(parts)} parts)",
                )
            except Exception:
                sync_logger.exception(
                    "Failed to record manual hosting backup export",
                )
            encrypted_envelope = encrypt_backup(envelope_path)
            return _stream_download_file(
                encrypted_envelope,
                archive_name + ".bwenc",
                mimetype="application/octet-stream",
                cleanup_paths=tuple(parts) + (envelope_path, encrypted_envelope),
                cleanup_dirs=(envelope_dir,),
            )
        except Exception:
            sync_logger.exception("Manual hosting backup failed to prepare download")
            cleanup_paths = tuple(parts)
            if envelope_path:
                cleanup_paths += (envelope_path,)
            _cleanup_download_artifacts(
                cleanup_paths, (envelope_dir,) if envelope_dir else ()
            )
            flash(
                "Failed to prepare the hosting backup download. Check the logs.",
                "error",
            )
            return redirect(url_for("hosting_admin_settings"))

    @app.route("/admin/export-backup-key", methods=["POST"])
    @hosting_admin_required
    @hosting_rate_limit(max_requests=3, window=300)
    def hosting_admin_export_backup_key():
        """Download the separate key required to restore encrypted backups."""
        admin = get_current_account()
        password = request.form.get("current_password") or ""
        if not admin or not check_password_hash(admin["password"], password):
            flash("Enter your current password to download the backup key.", "error")
            return redirect(url_for("hosting_admin_settings"))
        key = config.load_or_generate_backup_key()
        return send_file(
            BytesIO(key),
            as_attachment=True,
            download_name="bananawiki-backup-encryption.key",
            mimetype="application/octet-stream",
        )

    @app.route("/admin/restore-platform", methods=["POST"])
    @hosting_admin_required
    @hosting_rate_limit(max_requests=5, window=60)
    def hosting_admin_restore_platform():
        """Admin: restore entire hosting platform from uploaded backup ZIPs."""
        from ..backups import notify_change

        files = request.files.getlist("backup_files")
        if not files or all(not f.filename for f in files):
            flash("No files selected.", "error")
            return redirect(url_for("hosting_admin_settings"))

        work_dir = tempfile.mkdtemp(
            prefix="bwh-platform-restore-", dir=_chunked_import_root()
        )
        try:
            uploaded_paths = _save_uploaded_zip_files(files, work_dir)
            if not uploaded_paths:
                flash("No valid ZIP files uploaded.", "error")
                return redirect(url_for("hosting_admin_settings"))
            _restore_platform_from_uploaded_archives(uploaded_paths, work_dir)
            notify_change(
                "platform_migration_import", "Full platform restoration performed"
            )
            flash(
                "Platform restored. Restart the portal service to load its session key, then sign in with an account from the backup.",
                "success",
            )
        except Exception as exc:
            from ..backups import logger

            logger.exception("Hosting platform restoration failed: %s", exc)
            flash(f"Restoration failed: {exc}", "error")
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

        return redirect(url_for("hosting_admin_settings"))

    @app.route("/admin/restore-platform/chunk/start", methods=["POST"])
    @hosting_admin_required
    @hosting_rate_limit(max_requests=20, window=300)
    def hosting_admin_restore_platform_chunk_start():
        """Start a resumable platform-restore upload session."""
        account_id = session["hosting_account_id"]
        filename = os.path.basename((request.form.get("filename") or "").strip())
        try:
            total_size = int(request.form.get("size") or "0")
        except (TypeError, ValueError):
            return _json_import_error("Invalid archive size.")

        if not filename.lower().endswith((".zip", ".bwenc")):
            return _json_import_error(
                "Choose a ZIP or encrypted .bwenc backup archive."
            )
        if total_size <= 0:
            return _json_import_error("The uploaded file is empty.")
        if total_size > config.HOSTING_IMPORT_MAX_BYTES:
            max_gb = config.HOSTING_IMPORT_MAX_BYTES / (1024 * 1024 * 1024)
            return _json_import_error(
                f"The archive is larger than the {max_gb:.1f} GB restore limit."
            )

        chunk_size = max(1024 * 1024, int(config.HOSTING_IMPORT_CHUNK_BYTES))
        total_chunks = (total_size + chunk_size - 1) // chunk_size
        upload_id = secrets.token_urlsafe(24)
        upload_dir = os.path.join(_chunked_import_root(), upload_id)
        os.makedirs(os.path.join(upload_dir, "chunks"), exist_ok=True)
        _write_chunked_import_meta(
            upload_dir,
            {
                "kind": "platform_restore",
                "account_id": account_id,
                "created_at": time.time(),
                "filename": filename,
                "size": total_size,
                "chunk_size": chunk_size,
                "total_chunks": total_chunks,
            },
        )
        _cleanup_stale_chunked_imports()
        return jsonify(
            {
                "ok": True,
                "upload_id": upload_id,
                "chunk_size": chunk_size,
                "total_chunks": total_chunks,
            }
        )

    @app.route("/admin/restore-platform/chunk/<upload_id>", methods=["POST"])
    @hosting_admin_required
    @hosting_rate_limit(max_requests=1000, window=3600)
    def hosting_admin_restore_platform_chunk(upload_id):
        """Receive one ZIP chunk for a resumable platform restore."""
        account_id = session["hosting_account_id"]
        upload_dir, meta = _read_chunked_import_meta(
            upload_id,
            account_id,
            expected_kind="platform_restore",
        )
        if not meta:
            return _json_import_error("Restore upload session was not found.", 404)

        try:
            chunk_index = int(request.form.get("chunk_index") or "-1")
        except (TypeError, ValueError):
            return _json_import_error("Invalid chunk index.")
        total_chunks = int(meta["total_chunks"])
        if chunk_index < 0 or chunk_index >= total_chunks:
            return _json_import_error("Chunk index is out of range.")

        chunk = request.files.get("chunk")
        if chunk is None or not chunk.filename:
            return _json_import_error("No chunk was uploaded.")

        expected_size = _expected_chunk_size(meta, chunk_index)
        try:
            chunk_path = _safe_path_within(
                upload_dir, "chunks", f"{chunk_index:08d}.part"
            )
        except ValueError:
            return _json_import_error("Restore upload session is invalid.", 400)
        tmp_chunk_path = f"{chunk_path}.uploading"
        try:
            chunk.save(tmp_chunk_path)
            chunk_bytes = os.path.getsize(tmp_chunk_path)
        except OSError:
            try:
                os.unlink(tmp_chunk_path)
            except OSError:
                pass
            return _json_import_error("Failed to save uploaded chunk.", 500)
        if chunk_bytes != expected_size:
            try:
                os.unlink(tmp_chunk_path)
            except OSError:
                pass
            return _json_import_error("Uploaded chunk has an invalid size.")
        os.replace(tmp_chunk_path, chunk_path)

        return jsonify(
            {
                "ok": True,
                "chunk_index": chunk_index,
                "total_chunks": total_chunks,
            }
        )

    @app.route("/admin/restore-platform/chunk/<upload_id>/complete", methods=["POST"])
    @hosting_admin_required
    @hosting_rate_limit(max_requests=10, window=3600)
    def hosting_admin_restore_platform_chunk_complete(upload_id):
        """Start assembling/restoring a chunked platform backup upload."""
        account_id = session["hosting_account_id"]
        upload_dir, meta = _read_chunked_import_meta(
            upload_id,
            account_id,
            expected_kind="platform_restore",
        )
        if not meta:
            return _json_import_error("Restore upload session was not found.", 404)

        missing, invalid = _validate_chunked_import_parts(upload_dir, meta)
        if missing:
            return _json_import_error("The restore upload is incomplete.")
        if invalid:
            return _json_import_error("The restore upload contains corrupted chunks.")

        current_status = _read_chunked_import_status(upload_dir)
        if current_status.get("state") in ("assembling", "importing"):
            return jsonify(
                {
                    "ok": True,
                    "status_url": url_for(
                        "hosting_admin_restore_platform_chunk_status",
                        upload_id=upload_id,
                    ),
                }
            )

        _write_chunked_import_status(
            upload_dir,
            {
                "state": "queued",
                "message": "Restore queued.",
            },
        )
        worker = threading.Thread(
            target=_run_chunked_restore_platform_job,
            args=(upload_dir, meta, upload_id),
            daemon=True,
        )
        worker.start()
        return jsonify(
            {
                "ok": True,
                "status_url": url_for(
                    "hosting_admin_restore_platform_chunk_status", upload_id=upload_id
                ),
            }
        )

    @app.route("/admin/restore-platform/chunk/<upload_id>/status", methods=["GET"])
    @hosting_admin_required
    @hosting_rate_limit(max_requests=1000, window=3600)
    def hosting_admin_restore_platform_chunk_status(upload_id):
        """Return status for a background chunked platform restore."""
        account_id = session["hosting_account_id"]
        upload_dir, meta = _read_chunked_import_meta(
            upload_id,
            account_id,
            expected_kind="platform_restore",
        )
        if not meta:
            return _json_import_error("Restore upload session was not found.", 404)

        status = _read_chunked_import_status(upload_dir)
        if status.get("state") == "complete":
            flash(
                "Platform restored. Restart the portal service to load its session key, then sign in with an account from the backup.",
                "success",
            )
        return jsonify(
            {
                "ok": True,
                "state": status.get("state", "pending"),
                "message": status.get("message", ""),
                "error": status.get("error", ""),
                "redirect": url_for("hosting_admin_settings")
                if status.get("state") == "complete"
                else "",
            }
        )
