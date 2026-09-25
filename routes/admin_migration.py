"""Administration: migration."""

from flask import (
    render_template,
    request,
    redirect,
    url_for,
    session,
    flash,
    send_file,
    Response,
    stream_with_context,
    current_app,
)
import os, io, json, zipfile, re, collections, sqlite3, tempfile, shutil
from datetime import datetime, timezone
import db
import config
from helpers import (
    MAX_PASSWORD_LENGTH,
    check_password_hash,
    login_required,
    admin_required,
    get_current_user,
    rate_limit,
    t,
)
from wiki_logger import log_action, get_logger
from sync import notify_change
from .admin_common import (
    favicon_upload_folder,
)


def _iter_migration_asset_files(root_dir, *, include_file=None):
    """Yield (absolute_path, relative_path) pairs for migration asset export."""
    if not root_dir or not os.path.isdir(root_dir):
        return

    root_dir = os.path.abspath(root_dir)
    for dirpath, _, filenames in os.walk(root_dir):
        for filename in sorted(filenames):
            rel_path = os.path.relpath(os.path.join(dirpath, filename), root_dir)
            rel_path = rel_path.replace(os.sep, "/")
            if include_file and not include_file(rel_path):
                continue
            yield os.path.join(dirpath, filename), rel_path


def _get_migration_asset_roots():
    """Return archive-prefix/root-dir pairs for site migration assets.

    The archive prefixes are flat directory names at the archive root
    (no ``assets/`` wrapper), to match the unified BananaWiki archive
    format shared with the hosting platform.  Legacy archives that
    still use ``assets/<kind>/`` paths are accepted on import via
    :func:`_get_legacy_migration_asset_aliases`.
    """
    return (
        ("uploads", config.UPLOAD_FOLDER, None),
        ("attachments", config.ATTACHMENT_FOLDER, None),
        ("chat_attachments", config.CHAT_ATTACHMENT_FOLDER, None),
        ("kanban_attachments", config.KANBAN_ATTACHMENT_FOLDER, None),
        ("custom_page_files", config.CUSTOM_PAGE_FILES_FOLDER, None),
        # Logs excluded from export: they contain runtime data, not user content.
        (
            "favicons",
            favicon_upload_folder(),
            lambda rel_path: os.path.basename(rel_path).startswith("custom_"),
        ),
    )


def _get_legacy_migration_asset_aliases():
    """Map legacy ``assets/<kind>`` prefixes to their unified equivalents.

    Used when importing archives produced by older BananaWiki releases.
    """
    return {
        "assets/uploads": "uploads",
        "assets/attachments": "attachments",
        "assets/chat_attachments": "chat_attachments",
        "assets/kanban_attachments": "kanban_attachments",
        "assets/custom_page_files": "custom_page_files",
        "assets/favicons": "favicons",
    }


def _normalise_legacy_member_name(name, *, hosting_slug_prefix=None):
    """Translate a legacy archive member name to its unified equivalent.

    Handles two legacy prefixes:

    * Legacy standalone archives nest assets under ``assets/<kind>/``;
      we strip the ``assets/`` wrapper.
    * Legacy hosting archives nest everything under ``<slug>/``;
      callers detecting that layout can pass *hosting_slug_prefix* to
      strip that wrapper here.

    Returns the name unchanged when no legacy prefix matches.
    """
    if hosting_slug_prefix and name.startswith(hosting_slug_prefix + "/"):
        name = name[len(hosting_slug_prefix) + 1 :]
    for legacy_prefix, unified_prefix in _get_legacy_migration_asset_aliases().items():
        if name.startswith(legacy_prefix + "/"):
            return unified_prefix + name[len(legacy_prefix) :]
    return name


def _get_migration_standalone_files(*, include_config=True):
    """Return (archive_path, filesystem_path) pairs for standalone files.

    The raw secret key file (.secret_key) is intentionally excluded from
    exports.  Including it in a portable ZIP would expose the Flask session
    signing secret to anyone who obtains a copy of the archive.  Admins who
    need to preserve session continuity across a full server migration must
    copy the secret key file manually out-of-band.

    When *include_config* is ``False`` (used during **import**), ``config.py``
    is excluded from the returned list.  Importing arbitrary Python from a ZIP
    would allow Remote Code Execution on the next worker restart, so this file
    is export-only.
    """
    files = []
    if include_config:
        # config.py: site-specific configuration (no raw secrets).
        config_path = os.path.join(config.BASE_DIR, "config.py")
        if os.path.isfile(config_path):
            files.append(("instance/config.py", config_path))
    return files


_SITE_EXPORT_TEMP_MAX_AGE_SECONDS = 24 * 60 * 60

_SITE_EXPORT_STREAM_CHUNK_SIZE = 1024 * 1024


def _site_export_temp_root():
    """Return the directory used for temporary site export archives."""
    root = getattr(
        config,
        "SITE_EXPORT_TEMP_DIR",
        os.path.join(config.INSTANCE_DIR, "tmp_exports"),
    )
    os.makedirs(root, exist_ok=True)
    return root


def _cleanup_stale_site_exports():
    """Remove old temporary export directories left by interrupted downloads."""
    root = _site_export_temp_root()
    cutoff = datetime.now(timezone.utc).timestamp() - _SITE_EXPORT_TEMP_MAX_AGE_SECONDS
    try:
        entries = os.listdir(root)
    except OSError:
        return
    for entry in entries:
        if not entry.startswith("bw-site-export-"):
            continue
        path = os.path.join(root, entry)
        try:
            if os.path.getmtime(path) < cutoff:
                if os.path.isdir(path):
                    shutil.rmtree(path, ignore_errors=True)
                else:
                    os.unlink(path)
        except OSError:
            pass


def _site_export_compress_level():
    """Return the configured ZIP compression level for site exports."""
    try:
        level = int(getattr(config, "SITE_EXPORT_COMPRESS_LEVEL", 1))
    except (TypeError, ValueError):
        level = 1
    return max(0, min(level, 9))


def _site_export_store_threshold_bytes():
    """Return the file-size threshold above which export entries are stored."""
    try:
        threshold = int(
            getattr(config, "SITE_EXPORT_STORE_FILE_BYTES", 64 * 1024 * 1024)
        )
    except (TypeError, ValueError):
        threshold = 64 * 1024 * 1024
    return max(0, threshold)


def _site_export_store_extensions():
    """Return lowercase filename extensions that should be stored in exports."""
    raw = getattr(config, "SITE_EXPORT_STORE_EXTENSIONS", "")
    if not raw:
        return frozenset()
    return frozenset(
        ext.strip().lower()
        for ext in str(raw).split(",")
        if ext.strip().startswith(".")
    )


def _site_export_compress_type(path):
    """Return the ZIP compression method to use for *path*."""
    try:
        size = os.path.getsize(path)
    except OSError:
        size = 0
    ext = os.path.splitext(path)[1].lower()
    if ext in _site_export_store_extensions():
        return zipfile.ZIP_STORED
    threshold = _site_export_store_threshold_bytes()
    if threshold > 0 and size >= threshold:
        return zipfile.ZIP_STORED
    return zipfile.ZIP_DEFLATED


def _site_export_write_file(zf, fs_path, archive_path):
    """Write one file to a site export using the configured compression policy."""
    compress_type = _site_export_compress_type(fs_path)
    compresslevel = (
        None if compress_type == zipfile.ZIP_STORED else _site_export_compress_level()
    )
    zf.write(
        fs_path,
        archive_path,
        compress_type=compress_type,
        compresslevel=compresslevel,
    )


def _stream_site_export_file(path, download_name, cleanup_dir):
    """Return a ZIP download response and clean up its temporary directory."""
    try:
        size = os.path.getsize(path)
    except OSError:
        size = 0
    if current_app.config.get("TESTING"):
        with open(path, "rb") as fh:
            payload = fh.read()
        shutil.rmtree(cleanup_dir, ignore_errors=True)
        response = Response(payload, mimetype="application/zip")
        response.headers.set(
            "Content-Disposition", "attachment", filename=download_name
        )
        response.headers["Content-Length"] = str(len(payload))
        return response

    @stream_with_context
    def _iter_file():
        """Yield the export archive in chunks."""
        try:
            with open(path, "rb") as fh:
                while True:
                    chunk = fh.read(_SITE_EXPORT_STREAM_CHUNK_SIZE)
                    if not chunk:
                        break
                    yield chunk
        finally:
            shutil.rmtree(cleanup_dir, ignore_errors=True)

    response = Response(_iter_file(), mimetype="application/zip")
    response.headers.set("Content-Disposition", "attachment", filename=download_name)
    response.headers["Content-Length"] = str(size)
    response.headers["X-Accel-Buffering"] = "no"
    return response


def _drop_site_export_tts_cache_rows(db_path):
    """Remove disposable TTS cache metadata from an export DB snapshot."""
    if not db_path or not os.path.isfile(db_path):
        return
    conn = sqlite3.connect(db_path, timeout=20)
    try:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if "tts_generations" in tables:
            conn.execute("DELETE FROM tts_generations")
            conn.commit()
    finally:
        conn.close()


def _write_site_migration_assets(zf):
    """Add runtime asset files to a site migration zip."""
    for archive_root, fs_root, include_file in _get_migration_asset_roots():
        for abs_path, rel_path in _iter_migration_asset_files(
            fs_root, include_file=include_file
        ):
            _site_export_write_file(zf, abs_path, f"{archive_root}/{rel_path}")
    for archive_path, fs_path in _get_migration_standalone_files():
        _site_export_write_file(zf, fs_path, archive_path)


def _clear_site_migration_assets():
    """Remove runtime asset files before a full replacement import."""
    for _, fs_root, include_file in _get_migration_asset_roots():
        for abs_path, _ in _iter_migration_asset_files(
            fs_root, include_file=include_file
        ):
            os.remove(abs_path)


def _resolve_migration_asset_path(root_dir, rel_path):
    """Resolve an archive relative path under *root_dir* safely."""
    rel_path = rel_path.replace("\\", "/")
    parts = [part for part in rel_path.split("/") if part not in ("", ".")]
    if any(part == ".." for part in parts):
        raise ValueError("Migration archive contains an invalid asset path.")
    root_dir = os.path.abspath(root_dir)
    dest_path = os.path.abspath(os.path.join(root_dir, *parts))
    if os.path.commonpath([root_dir, dest_path]) != root_dir:
        raise ValueError("Migration archive contains an invalid asset path.")
    return dest_path


def _import_site_migration_assets(
    zfs,
    mode,
    *,
    include_system_files=False,
    hosting_slug_prefixes=None,
):
    """Restore runtime asset files from one or more site migration zips.

    Supports reassembling chunked files (e.g. from multi-part Telegram backups).

    *hosting_slug_prefixes* is an optional dict mapping each ``zf`` to a
    legacy-hosting slug prefix to strip from member names before
    matching.  Used so legacy hosting archives (where everything is
    nested under ``<slug>/``) are accepted by the standalone wiki's
    migration import.
    """
    hosting_slug_prefixes = hosting_slug_prefixes or {}
    if mode == "delete_all":
        _clear_site_migration_assets()

    # Build a lookup for standalone files that may appear in the archive.
    standalone_map = {
        archive_path: fs_path
        for archive_path, fs_path in _get_migration_standalone_files(
            include_config=False
        )
    }

    if include_system_files:
        # We also include system files that might be in a Telegram backup.
        standalone_map.update(
            {
                "instance/.secret_key": config.SECRET_KEY_FILE,
                "instance/config.py": os.path.join(config.BASE_DIR, "config.py"),
                "database/bananawiki.db": config.DATABASE_PATH,
            }
        )

    # First pass: collect all members across all ZIPs and identify chunks
    chunks = collections.defaultdict(
        list
    )  # original_filename -> list of (chunk_member, zf)
    for zf in zfs:
        for member in zf.infolist():
            if member.is_dir():
                continue

            # Normalise legacy paths (``assets/<kind>/`` or legacy
            # hosting ``<slug>/<kind>/``) to the unified flat
            # ``<kind>/`` layout before matching.
            normalised_name = _normalise_legacy_member_name(
                member.filename,
                hosting_slug_prefix=hosting_slug_prefixes.get(id(zf)),
            )

            # Check if this is a chunk (filename.chunkNNN)
            match = re.search(r"^(.*)\.chunk(\d+)$", normalised_name)
            if match:
                chunks[match.group(1)].append((member, zf))
                continue

            # Non-chunked files: check standalone map (uses the raw
            # archive name; standalone keys are explicit paths like
            # ``instance/config.py``).
            if member.filename in standalone_map:
                dest = standalone_map[member.filename]
                if mode == "keep" and os.path.exists(dest):
                    continue
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with zf.open(member) as src, open(dest, "wb") as dst:
                    dst.write(src.read())
                continue

            # Asset directories
            for archive_root, fs_root, _ in _get_migration_asset_roots():
                prefix = f"{archive_root}/"
                if not normalised_name.startswith(prefix):
                    continue
                if not fs_root:
                    break
                rel_path = normalised_name[len(prefix) :]
                if not rel_path:
                    continue
                dest_path = _resolve_migration_asset_path(fs_root, rel_path)
                if mode == "keep" and os.path.exists(dest_path):
                    break
                os.makedirs(os.path.dirname(dest_path), exist_ok=True)
                with zf.open(member) as src, open(dest_path, "wb") as dst:
                    dst.write(src.read())
                break

    # Second pass: reassemble chunks
    for original_filename, chunk_list in chunks.items():
        chunk_list.sort(
            key=lambda x: int(re.search(r"\.chunk(\d+)$", x[0].filename).group(1))
        )

        dest_path = None
        # Determine destination path.  ``original_filename`` here is the
        # chunk's reassembled key, which we already normalised in the
        # first pass before stashing it in ``chunks``.
        if original_filename in standalone_map:
            dest_path = standalone_map[original_filename]
        else:
            for archive_root, fs_root, _ in _get_migration_asset_roots():
                prefix = f"{archive_root}/"
                if original_filename.startswith(prefix):
                    rel_path = original_filename[len(prefix) :]
                    if rel_path and fs_root:
                        dest_path = _resolve_migration_asset_path(fs_root, rel_path)
                        break

        if not dest_path:
            get_logger().warning(
                "site import: could not resolve path for chunks of %s",
                original_filename,
            )
            continue

        if mode == "keep" and os.path.exists(dest_path):
            continue

        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
        try:
            with open(dest_path, "wb") as dst:
                for member, zf in chunk_list:
                    with zf.open(member) as src:
                        dst.write(src.read())
        except Exception as exc:
            get_logger().error(
                "site import, failed to reassemble %s: %s", original_filename, exc
            )


def _confirm_site_migration_password(user, action):
    """Return True when the request carries the acting admin's password.

    A full-site export hands over every account's password hash and a full
    copy of the database, and an import can replace every account, the
    owner's included. Both therefore ask for the password again even inside
    a signed-in admin session, so a hijacked or unattended session is not
    enough. A refusal is logged under ``<action>_refused`` and flashed; the
    caller only has to redirect.
    """
    if session.get("impersonator_id"):
        # The password would belong to the impersonated account, which the
        # person at the keyboard should not know. Make them act as themselves.
        log_action(f"{action}_refused", request, user=user, reason="impersonating")
        flash(
            t(
                "flash.stop_impersonating_before_site_migration",
                default="Stop impersonating before exporting or importing the whole wiki.",
            ),
            "error",
        )
        return False
    password = request.form.get("password", "")
    if (
        not password
        or len(password) > MAX_PASSWORD_LENGTH
        or not check_password_hash(user["password"], password)
    ):
        log_action(f"{action}_refused", request, user=user, reason="wrong_password")
        flash(t("flash.incorrect_password"), "error")
        return False
    return True


def register_admin_migration_routes(app):
    """Register administration routes for migration."""

    @app.route("/admin/migration")
    @login_required
    @admin_required
    def admin_migration():
        """Render the site migration page."""
        user = get_current_user()
        log_action("view_migration", request, user=user)
        return render_template("admin/migration.html")

    @app.route("/admin/migration/export", methods=["POST"])
    @login_required
    @admin_required
    @rate_limit(5, 60)
    def admin_migration_export():
        """Export the entire site as a unified BananaWiki archive.

        The archive follows the shared format defined in
        :mod:`archive_format` so it can be imported by either the
        standalone wiki (Settings → Migration → Import) or the hosting
        platform (Admin → Import wiki ZIP).

        It holds the whole database, password hashes included, so the
        admin has to confirm their password first.
        """
        import archive_format

        user = get_current_user()
        if not _confirm_site_migration_password(user, "export_site"):
            return redirect(url_for("admin_migration"))
        _cleanup_stale_site_exports()
        tmp_dir = tempfile.mkdtemp(
            prefix="bw-site-export-",
            dir=_site_export_temp_root(),
        )
        filename = (
            f"site_export_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.zip"
        )
        archive_path = os.path.join(tmp_dir, filename)

        # Take a consistent snapshot of the live SQLite DB so the
        # archive also includes a raw ``bananawiki.db`` ready for the
        # hosting platform's import path (which prefers raw DB).
        snap_path = None
        site_export_bytes = None
        has_site_export_json = False
        try:
            from db._connection import get_db_context  # noqa: WPS433

            with get_db_context() as conn:
                fd, snap_path = tempfile.mkstemp(
                    prefix="bw-export-",
                    suffix=".db",
                    dir=tmp_dir,
                )
                os.close(fd)
                dest = sqlite3.connect(snap_path)
                try:
                    conn.backup(dest)
                finally:
                    dest.close()
            _drop_site_export_tts_cache_rows(snap_path)
        except Exception:
            get_logger().exception(
                "site export: failed to snapshot SQLite DB; archive will "
                "still contain site_export.json"
            )
            if snap_path:
                try:
                    os.unlink(snap_path)
                except OSError:
                    pass
            snap_path = None

        try:
            snapshot_for_json = (
                snap_path if snap_path and os.path.isfile(snap_path) else None
            )
            if snapshot_for_json:
                snapshot_size = os.path.getsize(snapshot_for_json)
                limit = int(
                    getattr(
                        config,
                        "SITE_EXPORT_JSON_DB_SIZE_LIMIT_BYTES",
                        128 * 1024 * 1024,
                    )
                )
                if snapshot_size <= limit:
                    data = db.export_site_data(db_path=snapshot_for_json)
                    site_export_bytes = json.dumps(
                        data,
                        separators=(",", ":"),
                    ).encode("utf-8")
                    has_site_export_json = True
                else:
                    get_logger().info(
                        "site export: skipping site_export.json because DB "
                        "snapshot is %.1f MiB; raw SQLite DB remains included",
                        snapshot_size / (1024 * 1024),
                    )
            else:
                data = db.export_site_data()
                site_export_bytes = json.dumps(
                    data,
                    separators=(",", ":"),
                ).encode("utf-8")
                has_site_export_json = True

            manifest = archive_format.build_manifest(
                source=archive_format.SOURCE_STANDALONE,
                has_raw_db=bool(snap_path and os.path.isfile(snap_path)),
                has_site_export_json=has_site_export_json,
            )

            with zipfile.ZipFile(
                archive_path,
                "w",
                zipfile.ZIP_DEFLATED,
                compresslevel=_site_export_compress_level(),
                allowZip64=True,
            ) as zf:
                archive_format.write_manifest_to_zip(zf, manifest)
                if site_export_bytes is not None:
                    zf.writestr(
                        archive_format.SITE_EXPORT_FILENAME,
                        site_export_bytes,
                        compress_type=zipfile.ZIP_DEFLATED,
                        compresslevel=_site_export_compress_level(),
                    )
                if snap_path and os.path.isfile(snap_path):
                    _site_export_write_file(
                        zf,
                        snap_path,
                        archive_format.RAW_DB_FILENAME,
                    )
                _write_site_migration_assets(zf)
        except Exception:
            get_logger().exception("site export: failed to build archive")
            shutil.rmtree(tmp_dir, ignore_errors=True)
            flash("Failed to build the site export archive.", "error")
            return redirect(url_for("admin_migration"))

        log_action(
            "export_site",
            request,
            user=user,
            raw_db=bool(snap_path and os.path.isfile(snap_path)),
            site_export_json=has_site_export_json,
        )
        notify_change("site_export", "Full site exported")
        return _stream_site_export_file(archive_path, filename, tmp_dir)

    @app.route("/admin/migration/import", methods=["POST"])
    @login_required
    @admin_required
    @rate_limit(5, 60)
    def admin_migration_import():
        """Import a previously exported site ZIP, including runtime assets.

        Accepts the unified BananaWiki archive format (manifest.json +
        bananawiki.db + site_export.json + flat asset dirs) as well as
        the two legacy shapes: legacy standalone (``site_export.json``
        + ``assets/<kind>/`` paths) and legacy hosting
        (``<slug>/bananawiki.db`` + ``<slug>/<kind>/`` paths).  Hosting
        archives without a ``site_export.json`` get one derived from the
        raw SQLite file on the fly so the same three import modes
        (``delete_all`` / ``override`` / ``keep``) work for both.

        Every mode can bring in accounts with any role, and the first two
        can replace existing ones, so the admin has to confirm their
        password before anything in the upload is read.
        """
        import archive_format

        user = get_current_user()
        if not _confirm_site_migration_password(user, "import_site"):
            return redirect(url_for("admin_migration"))
        mode = request.form.get("import_mode", "")
        if mode not in ("delete_all", "override", "keep"):
            flash(t("flash.invalid_import_mode_selected"), "error")
            return redirect(url_for("admin_migration"))

        files = request.files.getlist("import_file")
        if not files or all(not f.filename for f in files):
            flash(t("flash.no_file_selected"), "error")
            return redirect(url_for("admin_migration"))

        zfs = []
        hosting_slug_prefixes = {}
        data = None
        derived_db_path = None
        try:
            for f in files:
                if not f.filename or not f.filename.lower().endswith(".zip"):
                    continue
                raw = f.read()
                archive_buf = io.BytesIO(raw)
                zf = zipfile.ZipFile(archive_buf)
                zfs.append(zf)

                # -- ZIP bomb protection: enforce file-count limit
                infolist = zf.infolist()
                max_files = getattr(config, "MAX_IMPORT_FILE_COUNT", 50_000)
                if len(infolist) > max_files:
                    flash(
                        t(
                            "flash.archive_filename_contains_infolist_entries_which_exceeds_the",
                            filename=f.filename,
                            infolist=len(infolist),
                            max_files=max_files,
                        ),
                        "error",
                    )
                    return redirect(url_for("admin_migration"))
                # -- ZIP bomb protection: enforce decompressed-size limits --
                total_uncompressed = 0
                for member in infolist:
                    if member.file_size > config.MAX_IMPORT_MEMBER_SIZE:
                        member_mb = round(member.file_size / (1024 * 1024), 2)
                        limit_mb = round(
                            config.MAX_IMPORT_MEMBER_SIZE / (1024 * 1024), 2
                        )
                        flash(
                            t(
                                "flash.archive_member_filename_in_filename2_is_too_large",
                                filename=member.filename,
                                filename_2=f.filename,
                                member_mb=member_mb,
                                limit_mb=limit_mb,
                            ),
                            "error",
                        )
                        return redirect(url_for("admin_migration"))
                    total_uncompressed += member.file_size
                if total_uncompressed > config.MAX_IMPORT_UNCOMPRESSED_SIZE:
                    total_mb = round(total_uncompressed / (1024 * 1024), 2)
                    limit_mb = round(
                        config.MAX_IMPORT_UNCOMPRESSED_SIZE / (1024 * 1024), 2
                    )
                    flash(
                        t(
                            "flash.archive_filename_total_uncompressed_size_totalmb_mb_exceeds",
                            filename=f.filename,
                            total_mb=total_mb,
                            limit_mb=limit_mb,
                        ),
                        "error",
                    )
                    return redirect(url_for("admin_migration"))

                names = zf.namelist()
                layout = archive_format.detect_layout(names)
                # Remember the legacy-hosting slug prefix (if any) so
                # the asset extractor can strip it from member names.
                if layout["layout"] == "legacy_hosting":
                    hosting_slug_prefixes[id(zf)] = layout["root_prefix"].rstrip("/")

                if data is None:
                    # Validate manifest if one is present.  An invalid
                    # manifest aborts the import: it indicates the
                    # archive was produced by a newer BananaWiki we do
                    # not yet understand.
                    if layout.get("has_manifest"):
                        try:
                            with zf.open(archive_format.MANIFEST_FILENAME) as mh:
                                archive_format.parse_manifest(mh.read())
                        except ValueError as exc:
                            flash(
                                t("flash.import_failed_exc", exc=str(exc)),
                                "error",
                            )
                            return redirect(url_for("admin_migration"))

                    # Try a JSON dump first; that's the preferred path.
                    json_candidates = [
                        archive_format.SITE_EXPORT_FILENAME,
                        f"{layout.get('root_prefix', '')}{archive_format.SITE_EXPORT_FILENAME}",
                    ]
                    json_name = next(
                        (n for n in json_candidates if n and n in names),
                        None,
                    )
                    if json_name is None:
                        # Fall back to *any* .json at the root (older standalone exports
                        # may name the dump differently).
                        root_json_names = [
                            n
                            for n in names
                            if n.endswith(".json")
                            and "/" not in n
                            and n != archive_format.MANIFEST_FILENAME
                        ]
                        if root_json_names:
                            json_name = root_json_names[0]

                    if json_name is not None:
                        with zf.open(json_name) as jf:
                            data = json.load(jf)
                    else:
                        # No JSON dump.  If the archive has a raw
                        # ``bananawiki.db`` (unified or legacy hosting),
                        # extract it to a temp file and derive a JSON
                        # dump from it.
                        db_candidates = [
                            archive_format.RAW_DB_FILENAME,
                            f"{layout.get('root_prefix', '')}{archive_format.RAW_DB_FILENAME}",
                        ]
                        db_name = next(
                            (n for n in db_candidates if n and n in names),
                            None,
                        )
                        if db_name is not None:
                            fd, derived_db_path = tempfile.mkstemp(
                                prefix="bw-import-",
                                suffix=".db",
                            )
                            os.close(fd)
                            with (
                                zf.open(db_name) as src,
                                open(derived_db_path, "wb") as dst,
                            ):
                                dst.write(src.read())
                            data = db.export_site_data(db_path=derived_db_path)

            if data is None:
                flash(
                    t("flash.the_uploaded_archives_contain_no_json_data_file"), "error"
                )
                return redirect(url_for("admin_migration"))

        except zipfile.BadZipFile:
            flash(t("flash.one_of_the_uploaded_files_is_not_a"), "error")
            return redirect(url_for("admin_migration"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            flash(t("flash.the_data_file_inside_the_archive_is_not"), "error")
            return redirect(url_for("admin_migration"))
        except Exception as exc:
            get_logger().warning("site import: failed to read archive: %s", exc)
            flash(t("flash.failed_to_read_the_uploaded_archive"), "error")
            return redirect(url_for("admin_migration"))
        finally:
            if derived_db_path:
                try:
                    os.unlink(derived_db_path)
                except OSError:
                    pass

        try:
            skipped = db.import_site_data(data, mode) or {}
        except ValueError as exc:
            flash(t("flash.import_failed_exc", exc=exc), "error")
            return redirect(url_for("admin_migration"))
        except Exception as exc:
            get_logger().exception(
                "site import, DB import error (mode=%s): %s", mode, exc
            )
            flash(
                t("flash.an_unexpected_error_occurred_during_import_the_database"),
                "error",
            )
            return redirect(url_for("admin_migration"))

        asset_warning = None
        try:
            include_system = request.form.get("restore_system_files") == "1"
            _import_site_migration_assets(
                zfs,
                mode,
                include_system_files=include_system,
                hosting_slug_prefixes=hosting_slug_prefixes,
            )
        except Exception as exc:
            get_logger().exception(
                "site import, asset extraction error (mode=%s): %s", mode, exc
            )
            asset_warning = str(exc)
        finally:
            for zf in zfs:
                zf.close()

        mode_labels = {
            "delete_all": "all previous data deleted, file restored",
            "override": "previous data kept, conflicts overridden",
            "keep": "previous data kept, conflicts left as-is",
        }
        file_users = data.get("users") if isinstance(data, dict) else None
        existing_accounts_kept = skipped.get("users", 0)
        account_rows_skipped = sum(
            count for table, count in skipped.items() if table != "users"
        )
        log_action(
            "import_site",
            request,
            user=user,
            mode=mode,
            accounts_in_file=len(file_users) if isinstance(file_users, list) else 0,
            existing_accounts_kept=existing_accounts_kept,
            account_rows_skipped=account_rows_skipped,
        )
        notify_change("site_import", f"Full site imported (mode={mode})")
        # Imports revoke login records. Clear this cookie before queuing the
        # result so the next request cannot erase the completion message.
        session.clear()
        flash(
            t("flash.site_data_imported_successfully_mode", mode=mode_labels[mode]),
            "success",
        )
        if account_rows_skipped:
            flash(
                t(
                    "flash.site_import_skipped_existing_account_rows",
                    default=(
                        "{count} record(s) in the file would have changed accounts "
                        "that already exist here (a role or deletion schedule, a "
                        "sign-in token or an account merge request). They were "
                        "left out."
                    ),
                    count=account_rows_skipped,
                ),
                "info",
            )
        if asset_warning:
            flash(
                t(
                    "flash.database_was_imported_but_some_asset_files_could",
                    asset_warning=asset_warning,
                ),
                "warning",
            )
        return redirect(url_for("login"))

    @app.route("/admin/bulk-markdown", methods=["GET"])
    @login_required
    @admin_required
    def admin_bulk_markdown():
        """Render the bulk-markdown import form."""
        return render_template("admin/bulk_markdown.html")

    @app.route("/admin/bulk-markdown/export", methods=["POST"])
    @login_required
    @admin_required
    @rate_limit(5, 60)
    def admin_bulk_markdown_export():
        """Export wiki pages as a Markdown tree with page-facing assets."""
        from helpers._bulk_markdown_export import build_bulk_markdown_export

        user = get_current_user()
        result = build_bulk_markdown_export()
        log_action(
            "bulk_markdown_export",
            request,
            user=user,
            pages_exported=result.page_count,
            uploads_exported=result.upload_count,
            attachments_exported=result.attachment_count,
            missing_assets=len(result.missing_assets),
        )
        notify_change(
            "bulk_markdown_export",
            f"{result.page_count} page(s) exported as markdown bundle",
        )
        return send_file(
            result.buffer,
            mimetype="application/zip",
            as_attachment=True,
            download_name=result.filename,
        )

    @app.route("/admin/bulk-markdown", methods=["POST"])
    @login_required
    @admin_required
    @rate_limit(5, 60)
    def admin_bulk_markdown_import():
        """Import one or more markdown files (.md or .zip with .md files)."""
        from helpers._bulk_markdown import (
            extract_markdown_files_from_zip,
            import_markdown_bundle,
        )

        user = get_current_user()
        uploads = request.files.getlist("import_file")
        if not uploads or all(not f.filename for f in uploads):
            flash(t("flash.no_file_selected"), "error")
            return redirect(url_for("admin_bulk_markdown"))

        skip_existing = request.form.get("skip_existing", "1") == "1"
        author_id = (
            user["id"]
            if request.form.get("attribute_to_me") == "1"
            else db.SYSTEM_USER_ID
        )

        files: list[tuple[str, str]] = []
        warnings: list[str] = []

        for upload in uploads:
            name = upload.filename or ""
            ext = os.path.splitext(name)[1].lower()
            try:
                raw = upload.read()
            except Exception as exc:  # pragma: no cover (defensive)
                warnings.append(f"{name}: could not read upload ({exc})")
                continue

            if ext == ".zip":
                zip_files, zip_errors = extract_markdown_files_from_zip(
                    raw,
                    max_uncompressed=config.MAX_IMPORT_UNCOMPRESSED_SIZE,
                    max_member=config.MAX_IMPORT_MEMBER_SIZE,
                    max_entries=config.MAX_IMPORT_FILE_COUNT,
                )
                files.extend(zip_files)
                warnings.extend(zip_errors)
            elif ext in (".md", ".markdown"):
                if len(raw) > config.MAX_IMPORT_MEMBER_SIZE:
                    warnings.append(f"{name}: file exceeds the per-file size limit.")
                    continue
                try:
                    text = raw.decode("utf-8")
                except UnicodeDecodeError:
                    text = raw.decode("latin-1", errors="replace")
                files.append((name, text))
            else:
                warnings.append(f"{name}: unsupported file type (use .md or .zip).")

        if not files:
            for w in warnings:
                flash(w, "error")
            if not warnings:
                flash(t("flash.no_markdown_files_were_found_in_the_upload"), "error")
            return redirect(url_for("admin_bulk_markdown"))

        result = import_markdown_bundle(files, author_id, skip_existing=skip_existing)
        log_action(
            "bulk_markdown_import",
            request,
            user=user,
            pages_created=len(result.created_pages),
            pages_skipped=len(result.skipped_pages),
            attributed_to="user" if author_id == user["id"] else "system",
        )
        if result.created_pages:
            notify_change(
                "bulk_markdown_import",
                f"{len(result.created_pages)} page(s) imported from markdown bundle",
            )

        for w in warnings + result.errors:
            flash(w, "warning")
        if result.created_pages:
            flash(
                t(
                    "flash.bulk_markdown_imported",
                    created=len(result.created_pages),
                    categories=len(set(result.created_categories)),
                ),
                "success",
            )
        elif not warnings and not result.errors:
            flash(t("flash.no_pages_were_created_during_the_import"), "info")
        return redirect(url_for("admin_bulk_markdown"))
