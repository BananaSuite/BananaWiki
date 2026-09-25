"""Obsidian vault export/import helpers."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from PIL import Image
from werkzeug.utils import secure_filename

import config
import db
from helpers._auth import editor_has_category_access, user_can_view_page
from helpers._constants import _get_dummy_hash
from helpers._passwords import check_password_hash
from helpers._text import slugify
from helpers._validation import allowed_attachment, allowed_file
from sync import notify_change, notify_file_upload

OBSIDIAN_MANIFEST = ".bananawiki-obsidian.json"
OBSIDIAN_MANIFEST_VERSION = 1
OBSIDIAN_ALLOWED_ROLES = ("editor", "admin", "owner")

_SERVER_IMAGE_RE = re.compile(r"/static/uploads/([^\s)\"']+)")
_SERVER_ATTACHMENT_RE = re.compile(r"/page/[^/]+/attachments/(\d+)/download")
_LOCAL_ASSET_RE = re.compile(r"(?P<path>(?:\.\.?/)*assets/(?:images|attachments)/[^\s)\"']+)")


def authenticate_obsidian_user(username, password):
    """Validate Obsidian sync credentials and return the BananaWiki user row."""
    _ensure_obsidian_sync_enabled()
    user = db.get_user_by_username((username or "").strip())
    if not user:
        check_password_hash(_get_dummy_hash(), password or "")
        raise PermissionError("Invalid username or password.")
    if not check_password_hash(user["password"], password or ""):
        raise PermissionError("Invalid username or password.")
    if user["suspended"]:
        raise PermissionError("Your account has been suspended.")
    if user["role"] not in OBSIDIAN_ALLOWED_ROLES:
        raise PermissionError("You do not have the required permissions to use Obsidian sync.")
    return user


def export_obsidian_vault(user, vault_dir, *, slugs=None, category_paths=None, include_home=True):
    """Export accessible pages into an Obsidian-style local vault directory."""
    _ensure_obsidian_sync_enabled()
    vault_root = _resolve_vault_root(vault_dir)
    vault_root.mkdir(parents=True, exist_ok=True)

    categories = list(db.list_categories())
    category_map, category_path_map = _build_category_maps(categories)
    selected_pages = _select_pages(
        user,
        category_map,
        category_path_map,
        slugs=slugs,
        category_paths=category_paths,
        include_home=include_home,
    )

    manifest = {
        "version": OBSIDIAN_MANIFEST_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "generated_by": user["id"],
        "pages": [],
    }

    files_written = 0
    asset_count = 0
    copied_assets = set()

    for page in selected_pages:
        entry, page_assets = _export_page_to_vault(
            page,
            vault_root,
            category_path_map,
            copied_assets,
        )
        manifest["pages"].append(entry)
        files_written += 1
        asset_count += page_assets

    _write_json(vault_root / OBSIDIAN_MANIFEST, manifest)
    return {
        "vault_dir": str(vault_root),
        "pages_exported": files_written,
        "assets_exported": asset_count,
        "manifest": str(vault_root / OBSIDIAN_MANIFEST),
    }


def import_obsidian_vault(user, vault_dir, *, slugs=None, category_paths=None):
    """Import edited markdown pages and local assets from an Obsidian vault."""
    _ensure_obsidian_sync_enabled()
    vault_root = _resolve_vault_root(vault_dir)
    manifest_path = vault_root / OBSIDIAN_MANIFEST
    manifest = _load_manifest(manifest_path)
    manifest_pages = {
        entry.get("vault_path"): dict(entry)
        for entry in manifest.get("pages", [])
        if entry.get("vault_path")
    }
    page_entries_by_slug = {
        entry.get("slug"): entry
        for entry in manifest.get("pages", [])
        if entry.get("slug")
    }

    markdown_files = _select_markdown_files(
        vault_root,
        manifest_pages,
        page_entries_by_slug,
        slugs=slugs,
        category_paths=category_paths,
    )

    categories = list(db.list_categories())
    category_map, category_path_map = _build_category_maps(categories)
    created_pages = 0
    updated_pages = 0
    uploaded_images = 0
    uploaded_attachments = 0

    for rel_path in markdown_files:
        page_path = vault_root / rel_path
        entry = manifest_pages.get(rel_path.as_posix())
        frontmatter, body = _split_frontmatter(page_path.read_text(encoding="utf-8"))

        page, created = _resolve_page_for_import(
            user,
            body,
            frontmatter,
            entry,
            rel_path,
            category_map,
            category_path_map,
        )

        asset_result = _rewrite_local_assets_for_server(
            user,
            vault_root,
            page_path,
            body,
            page,
            entry,
        )

        title = _resolve_page_title(body, frontmatter, entry, page_path)
        current_body = page["content"] if page else ""
        if created:
            if asset_result["content"] != current_body:
                db.update_page(
                    page["id"],
                    title,
                    asset_result["content"],
                    user["id"],
                    "Obsidian sync push",
                    builder_json="",
                    builder_public=False,
                )
            created_pages += 1
        else:
            if (
                title != page["title"]
                or asset_result["content"] != page["content"]
            ):
                db.update_page(
                    page["id"],
                    title,
                    asset_result["content"],
                    user["id"],
                    "Obsidian sync push",
                    builder_json="",
                    builder_public=False,
                )
                updated_pages += 1

        new_category_id = _category_id_from_rel_path(
            rel_path.parent,
            user,
            category_map,
            category_path_map,
            allow_create=user["role"] in ("admin", "owner"),
        )
        if new_category_id != page["category_id"]:
            if not editor_has_category_access(user, new_category_id):
                raise PermissionError("You do not have permission to move pages into this category.")
            db.update_page_category(page["id"], new_category_id)
            page = db.get_page(page["id"])

        attachment_result = _sync_attachment_directory(
            user,
            vault_root,
            page,
            asset_result["asset_map"],
            referenced_assets=asset_result["referenced_attachment_assets"],
        )

        uploaded_images += asset_result["uploaded_images"]
        uploaded_attachments += asset_result["uploaded_attachments"] + attachment_result["uploaded"]

        updated_entry = _build_manifest_entry_from_page(
            page,
            rel_path,
            _categories_by_page_path(page, category_path_map),
            asset_result["asset_map"],
            attachment_result["attachments"],
        )
        manifest_pages[rel_path.as_posix()] = updated_entry
        page_entries_by_slug[page["slug"]] = updated_entry
        _write_page_frontmatter(page_path, updated_entry, body)

    manifest["version"] = OBSIDIAN_MANIFEST_VERSION
    manifest["generated_at"] = datetime.now(timezone.utc).isoformat()
    manifest["generated_by"] = user["id"]
    manifest["pages"] = sorted(manifest_pages.values(), key=lambda item: item["vault_path"])
    _write_json(manifest_path, manifest)

    if created_pages or updated_pages or uploaded_images or uploaded_attachments:
        notify_change(
            "obsidian_sync_push",
            f"Obsidian sync push by {user['username']}: "
            f"{created_pages} created, {updated_pages} updated, "
            f"{uploaded_images} images, {uploaded_attachments} attachments",
        )

    return {
        "vault_dir": str(vault_root),
        "pages_created": created_pages,
        "pages_updated": updated_pages,
        "images_uploaded": uploaded_images,
        "attachments_uploaded": uploaded_attachments,
    }


def _ensure_obsidian_sync_enabled():
    """Raise when the experimental feature flag is turned off."""
    if not config.EXPERIMENTAL_OBSIDIAN_SYNC:
        raise RuntimeError("Experimental Obsidian sync is disabled in config.py.")


def _resolve_vault_root(vault_dir):
    """Return a resolved ``Path`` for the vault root."""
    return Path(vault_dir).expanduser().resolve()


def _write_json(path, data):
    """Write JSON data to ``path`` with a stable, readable layout."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")


def _build_category_maps(categories):
    """Return category metadata and vault path mappings."""
    category_map = {cat["id"]: dict(cat) for cat in categories}
    path_map = {}

    def resolve_parts(cat_id):
        """Recursively compute the sanitized vault path for ``cat_id``."""
        cat = category_map.get(cat_id)
        if not cat:
            return ()
        if "_vault_parts" in cat:
            return cat["_vault_parts"]
        parent_parts = resolve_parts(cat["parent_id"])
        parts = parent_parts + (_sanitize_vault_segment(cat["name"], f"category-{cat_id}"),)
        cat["_vault_parts"] = parts
        path_map["/".join(parts)] = cat["id"]
        return parts

    for cat_id in category_map:
        resolve_parts(cat_id)
    return category_map, path_map


def _sanitize_vault_segment(value, fallback):
    """Return a safe vault path segment."""
    cleaned = (value or "").replace("\\", "-").replace("/", "-").strip()
    cleaned = "".join(ch for ch in cleaned if ch.isprintable())
    cleaned = cleaned.strip(" .")
    return cleaned or fallback


def _flatten_pages():
    """Return all non-home pages from the category tree plus the home page."""
    roots, uncategorized = db.get_category_tree()
    pages = []

    def visit(nodes):
        """Collect pages from a nested category tree."""
        for node in nodes:
            pages.extend(node["pages"])
            visit(node["children"])

    visit(roots)
    pages.extend(uncategorized)
    home = db.get_home_page()
    if home:
        pages.append(dict(home))
    return pages


def _select_pages(user, category_map, category_path_map, *, slugs=None, category_paths=None, include_home):
    """Return the pages that should be exported for this sync run."""
    slug_filter = {slug.strip() for slug in (slugs or []) if slug and slug.strip()}
    category_filter = {
        _normalize_category_selector(path)
        for path in (category_paths or [])
        if path and _normalize_category_selector(path)
    }
    pages = []
    for page in _flatten_pages():
        if page["is_home"] and not include_home:
            continue
        if not user_can_view_page(user, page):
            continue
        if slug_filter and page["slug"] not in slug_filter:
            continue
        page_category_path = _category_path_for_page(page, category_map)
        if category_filter and not any(
            page_category_path == selector or page_category_path.startswith(f"{selector}/")
            for selector in category_filter
        ):
            continue
        pages.append(page)
    return sorted(pages, key=lambda row: (row["slug"] != "home", page_sort_key(row, category_map)))


def page_sort_key(page, category_map):
    """Return a stable sort key for page exports."""
    category_path = _category_path_for_page(page, category_map)
    return (category_path, page["slug"])


def _category_path_for_page(page, category_map):
    """Return the sanitized category path for ``page``."""
    if not page["category_id"]:
        return ""
    cat = category_map.get(page["category_id"])
    if not cat:
        return ""
    return "/".join(cat["_vault_parts"])


def _export_page_to_vault(page, vault_root, category_path_map, copied_assets):
    """Write one page into the vault and return its manifest entry."""
    rel_path = _vault_path_for_page(page, category_path_map)
    page_path = vault_root / rel_path
    page_path.parent.mkdir(parents=True, exist_ok=True)
    assets_dir = vault_root / "assets"
    image_dir = assets_dir / "images"
    attachment_dir = assets_dir / "attachments" / page["slug"]
    image_dir.mkdir(parents=True, exist_ok=True)
    attachment_dir.mkdir(parents=True, exist_ok=True)

    body = page["content"] or ""
    asset_map = {}
    asset_count = 0

    for filename in sorted(set(_SERVER_IMAGE_RE.findall(body))):
        source = Path(config.UPLOAD_FOLDER) / filename
        if not source.is_file():
            continue
        dest = image_dir / filename
        _safe_copy(source, dest)
        copied_assets.add(dest.resolve())
        asset_count += 1
        rel_from_page = _posix_relpath(dest, page_path.parent)
        server_ref = f"/static/uploads/{filename}"
        body = body.replace(server_ref, rel_from_page)
        asset_map[dest.relative_to(vault_root).as_posix()] = {
            "kind": "image",
            "server_ref": server_ref,
            "sha256": _file_sha256(dest),
        }

    attachments = []
    for attachment in db.get_page_attachments(page["id"]):
        source = Path(config.ATTACHMENT_FOLDER) / attachment["filename"]
        if not source.is_file():
            continue
        local_name = f"{attachment['id']}-{secure_filename(attachment['original_name']) or attachment['filename']}"
        dest = attachment_dir / local_name
        _safe_copy(source, dest)
        copied_assets.add(dest.resolve())
        asset_count += 1
        rel_from_page = _posix_relpath(dest, page_path.parent)
        server_ref = f"/page/{page['slug']}/attachments/{attachment['id']}/download"
        body = body.replace(server_ref, rel_from_page)
        vault_rel = dest.relative_to(vault_root).as_posix()
        asset_map[vault_rel] = {
            "kind": "attachment",
            "server_ref": server_ref,
            "sha256": _file_sha256(dest),
        }
        attachments.append({
            "attachment_id": attachment["id"],
            "original_name": attachment["original_name"],
            "vault_path": vault_rel,
            "server_ref": server_ref,
            "sha256": asset_map[vault_rel]["sha256"],
        })

    entry = _build_manifest_entry_from_page(
        page,
        rel_path,
        _categories_by_page_path(page, category_path_map),
        asset_map,
        attachments,
    )
    _write_page_frontmatter(page_path, entry, body)
    return entry, asset_count


def _categories_by_page_path(page, category_path_map):
    """Return the category path string for a page."""
    if not page["category_id"]:
        return ""
    for path, category_id in category_path_map.items():
        if category_id == page["category_id"]:
            return path
    return ""


def _vault_path_for_page(page, category_path_map):
    """Return the relative vault file path for a page."""
    category_path = _categories_by_page_path(page, category_path_map)
    base_name = "home.md" if page["is_home"] else f"{page['slug']}.md"
    if not category_path:
        return PurePosixPath(base_name)
    return PurePosixPath(category_path) / base_name


def _build_manifest_entry_from_page(page, rel_path, category_path, asset_map, attachments):
    """Return a manifest entry for a page."""
    return {
        "page_id": page["id"],
        "slug": page["slug"],
        "title": page["title"],
        "category_path": category_path or "",
        "is_home": bool(page["is_home"]),
        "vault_path": rel_path.as_posix() if hasattr(rel_path, "as_posix") else str(rel_path),
        "asset_map": asset_map,
        "attachments": attachments,
    }


def _write_page_frontmatter(page_path, entry, body):
    """Write page frontmatter followed by markdown body."""
    frontmatter = {
        "bananawiki_page_id": entry["page_id"],
        "bananawiki_slug": entry["slug"],
        "bananawiki_category": entry.get("category_path", ""),
        "bananawiki_is_home": bool(entry["is_home"]),
        "title": entry["title"],
    }
    lines = ["---"]
    for key, value in frontmatter.items():
        lines.append(f"{key}: {json.dumps(value)}")
    lines.append("---")
    lines.append(body.lstrip("\n"))
    page_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def _split_frontmatter(text):
    """Return ``(frontmatter_dict, body)`` from markdown text."""
    if not text.startswith("---\n"):
        return {}, text
    marker = text.find("\n---\n", 4)
    if marker == -1:
        return {}, text
    raw_frontmatter = text[4:marker]
    body = text[marker + 5:]
    parsed = {}
    for line in raw_frontmatter.splitlines():
        if ":" not in line:
            continue
        key, raw_value = line.split(":", 1)
        key = key.strip()
        raw_value = raw_value.strip()
        if not key:
            continue
        try:
            parsed[key] = json.loads(raw_value)
        except json.JSONDecodeError:
            parsed[key] = raw_value.strip("\"'")
    return parsed, body


def _load_manifest(path):
    """Load a manifest file if present."""
    if not path.is_file():
        return {"version": OBSIDIAN_MANIFEST_VERSION, "pages": []}
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("version") != OBSIDIAN_MANIFEST_VERSION:
        raise ValueError("Unsupported Obsidian sync manifest version.")
    data.setdefault("pages", [])
    return data


def _select_markdown_files(vault_root, manifest_pages, page_entries_by_slug, *, slugs=None, category_paths=None):
    """Return the markdown files to import from the vault."""
    selected = []
    slug_filter = {slug.strip() for slug in (slugs or []) if slug and slug.strip()}
    category_filter = {
        _normalize_category_selector(path)
        for path in (category_paths or [])
        if path and _normalize_category_selector(path)
    }

    for path in sorted(vault_root.rglob("*.md")):
        rel_path = path.relative_to(vault_root)
        if rel_path.parts and rel_path.parts[0] == "assets":
            continue
        if any(part.startswith(".") for part in rel_path.parts):
            continue
        entry = manifest_pages.get(rel_path.as_posix())
        if slug_filter:
            slug = None
            if entry:
                slug = entry["slug"]
            else:
                frontmatter, _ = _split_frontmatter(path.read_text(encoding="utf-8"))
                slug = frontmatter.get("bananawiki_slug") or slugify(path.stem)
            if slug not in slug_filter:
                continue
        if category_filter:
            rel_parent = rel_path.parent.as_posix()
            rel_parent = "" if rel_parent == "." else rel_parent
            if not any(
                rel_parent == selector or rel_parent.startswith(f"{selector}/")
                for selector in category_filter
            ):
                continue
        selected.append(rel_path)
    return selected


def _resolve_page_for_import(user, body, frontmatter, entry, rel_path, category_map, category_path_map):
    """Return ``(page, created)`` for the markdown file being imported."""
    page_id = frontmatter.get("bananawiki_page_id") or (entry or {}).get("page_id")
    slug = frontmatter.get("bananawiki_slug") or (entry or {}).get("slug") or slugify(rel_path.stem)
    page = db.get_page(int(page_id)) if page_id else None
    if not page and slug:
        page = db.get_page_by_slug(slug)
    if page:
        if not editor_has_category_access(user, page["category_id"]):
            raise PermissionError("You do not have permission to edit pages in this category.")
        return page, False

    allow_create = user["role"] in OBSIDIAN_ALLOWED_ROLES
    if not allow_create:
        raise PermissionError("You do not have the required permissions to create pages.")
    category_id = _category_id_from_rel_path(
        rel_path.parent,
        user,
        category_map,
        category_path_map,
        allow_create=user["role"] in ("admin", "owner"),
    )
    if not editor_has_category_access(user, category_id):
        raise PermissionError("You do not have permission to create pages in this category.")
    title = _resolve_page_title(body, frontmatter, entry, rel_path)
    slug = _unique_slug(slugify(slug or title or rel_path.stem))
    page_id = db.create_page(title, slug, body, category_id, user["id"])
    notify_change("page_create", f"Page '{slug}' created from Obsidian sync")
    return db.get_page(page_id), True


def _resolve_page_title(body, frontmatter, entry, page_path):
    """Return the title that should be used for an imported page."""
    title = (frontmatter.get("title") or (entry or {}).get("title") or "").strip()
    if title:
        return title
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip()
    return page_path.stem.replace("-", " ").strip() or "Untitled"


def _normalize_category_selector(path):
    """Return a normalized category selector string."""
    raw = str(path).strip().strip("/")
    if not raw or raw == ".":
        return ""
    parts = [
        _sanitize_vault_segment(segment, "category")
        for segment in raw.split("/")
        if segment and segment != "."
    ]
    return "/".join(parts)


def _category_id_from_rel_path(rel_path, user, category_map, category_path_map, *, allow_create):
    """Return or create the category for a markdown file path."""
    if not rel_path or str(rel_path) == ".":
        return None
    current_parent = None
    current_parts = []
    for segment in rel_path.parts:
        sanitized = _sanitize_vault_segment(segment, "category")
        current_parts.append(sanitized)
        path_key = "/".join(current_parts)
        existing_id = category_path_map.get(path_key)
        if existing_id:
            current_parent = existing_id
            continue
        if not allow_create:
            raise ValueError(f"Category '{path_key}' does not exist in BananaWiki.")
        category_id = db.create_category(segment, parent_id=current_parent)
        category_map[category_id] = {"id": category_id, "name": segment, "parent_id": current_parent}
        category_map[category_id]["_vault_parts"] = tuple(current_parts)
        category_path_map[path_key] = category_id
        current_parent = category_id
        notify_change("category_create", f"Category '{path_key}' created from Obsidian sync")
    return current_parent


def _rewrite_local_assets_for_server(user, vault_root, page_path, body, page, entry):
    """Upload new local assets and rewrite markdown links back to server URLs."""
    entry_asset_map = dict((entry or {}).get("asset_map") or {})
    rewritten = body
    referenced_attachment_assets = set()
    uploaded_images = 0
    uploaded_attachments = 0
    cached_server_refs = {}
    updated_asset_map = dict(entry_asset_map)

    def replace(match):
        """Replace one local asset reference with its BananaWiki server URL."""
        nonlocal uploaded_images, uploaded_attachments
        raw_ref = match.group("path")
        vault_asset = _resolve_asset_reference(vault_root, page_path.parent, raw_ref)
        if not vault_asset:
            return raw_ref
        vault_rel = vault_asset.relative_to(vault_root).as_posix()
        manifest_data = entry_asset_map.get(vault_rel)
        checksum = _file_sha256(vault_asset)
        if manifest_data and manifest_data.get("sha256") == checksum:
            cached_server_refs[vault_rel] = manifest_data["server_ref"]
            updated_asset_map[vault_rel] = dict(manifest_data)
            if manifest_data.get("kind") == "attachment":
                referenced_attachment_assets.add(vault_rel)
            return manifest_data["server_ref"]
        if vault_rel in cached_server_refs:
            return cached_server_refs[vault_rel]
        if "/assets/images/" in f"/{vault_rel}":
            server_ref = _upload_image_asset(vault_asset)
            uploaded_images += 1
            updated_asset_map[vault_rel] = {
                "kind": "image",
                "server_ref": server_ref,
                "sha256": checksum,
            }
        else:
            attachment = _upload_attachment_asset(user, page, vault_asset)
            server_ref = f"/page/{page['slug']}/attachments/{attachment['id']}/download"
            uploaded_attachments += 1
            updated_asset_map[vault_rel] = {
                "kind": "attachment",
                "server_ref": server_ref,
                "sha256": checksum,
            }
            referenced_attachment_assets.add(vault_rel)
        cached_server_refs[vault_rel] = updated_asset_map[vault_rel]["server_ref"]
        return cached_server_refs[vault_rel]

    rewritten = _LOCAL_ASSET_RE.sub(replace, rewritten)
    return {
        "content": rewritten,
        "uploaded_images": uploaded_images,
        "uploaded_attachments": uploaded_attachments,
        "asset_map": updated_asset_map,
        "referenced_attachment_assets": referenced_attachment_assets,
    }


def _resolve_asset_reference(vault_root, page_dir, raw_ref):
    """Resolve an asset reference to a safe file inside the vault."""
    candidate = (page_dir / raw_ref).resolve()
    try:
        candidate.relative_to(vault_root)
    except ValueError:
        return None
    return candidate if candidate.is_file() else None


def _upload_image_asset(asset_path):
    """Validate and copy an image asset into the BananaWiki uploads folder."""
    if not allowed_file(asset_path.name):
        raise ValueError(f"Image '{asset_path.name}' is not an allowed upload type.")
    with Image.open(asset_path) as img:
        img.verify()
    os.makedirs(config.UPLOAD_FOLDER, exist_ok=True)
    ext = asset_path.suffix.lower().lstrip(".")
    stored_name = f"{uuid.uuid4().hex}.{ext}"
    target = Path(config.UPLOAD_FOLDER).resolve() / stored_name
    _safe_copy(asset_path, target)
    notify_file_upload(stored_name, str(target))
    return f"/static/uploads/{stored_name}"


def _upload_attachment_asset(user, page, asset_path):
    """Validate and copy an attachment asset into BananaWiki storage."""
    if not allowed_attachment(asset_path.name):
        raise ValueError(f"Attachment '{asset_path.name}' is not an allowed upload type.")
    os.makedirs(config.ATTACHMENT_FOLDER, exist_ok=True)
    ext = asset_path.suffix.lower().lstrip(".")
    stored_name = f"{uuid.uuid4().hex}.{ext}" if ext else uuid.uuid4().hex
    target = Path(config.ATTACHMENT_FOLDER).resolve() / stored_name
    _safe_copy(asset_path, target)
    attachment_id = db.add_page_attachment(
        page["id"],
        stored_name,
        secure_filename(asset_path.name) or asset_path.name,
        asset_path.stat().st_size,
        user["id"],
    )
    notify_file_upload(stored_name, str(target), display_name=asset_path.name)
    return db.get_page_attachment(attachment_id)


def _sync_attachment_directory(user, vault_root, page, asset_map, *, referenced_assets):
    """Upload non-referenced files found in the page attachment directory."""
    attachment_root = vault_root / "assets" / "attachments" / page["slug"]
    attachments = []
    uploaded = 0
    asset_map = dict(asset_map or {})
    known_paths = set(referenced_assets)

    for file_path in sorted(attachment_root.glob("*")) if attachment_root.is_dir() else []:
        if not file_path.is_file():
            continue
        vault_rel = file_path.relative_to(vault_root).as_posix()
        checksum = _file_sha256(file_path)
        existing = asset_map.get(vault_rel)
        if existing and existing.get("sha256") == checksum:
            attachments.append({
                "attachment_id": _attachment_id_from_ref(existing["server_ref"]),
                "original_name": file_path.name,
                "vault_path": vault_rel,
                "server_ref": existing["server_ref"],
                "sha256": checksum,
            })
            continue
        if vault_rel in known_paths:
            attachments.append({
                "attachment_id": _attachment_id_from_ref(asset_map[vault_rel]["server_ref"]),
                "original_name": file_path.name,
                "vault_path": vault_rel,
                "server_ref": asset_map[vault_rel]["server_ref"],
                "sha256": checksum,
            })
            continue
        attachment = _upload_attachment_asset(user, page, file_path)
        server_ref = f"/page/{page['slug']}/attachments/{attachment['id']}/download"
        attachments.append({
            "attachment_id": attachment["id"],
            "original_name": attachment["original_name"],
            "vault_path": vault_rel,
            "server_ref": server_ref,
            "sha256": checksum,
        })
        uploaded += 1
    return {"uploaded": uploaded, "attachments": attachments}


def _attachment_id_from_ref(server_ref):
    """Extract an attachment ID from a server download URL."""
    match = _SERVER_ATTACHMENT_RE.search(server_ref or "")
    return int(match.group(1)) if match else None


def _unique_slug(base_slug):
    """Return a slug that does not collide with an existing page."""
    slug = base_slug or "untitled"
    counter = 2
    while db.get_page_by_slug(slug):
        slug = f"{base_slug}-{counter}"
        counter += 1
    return slug


def _safe_copy(source, dest):
    """Copy a file while ensuring the parent directory exists."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, dest)


def _file_sha256(path):
    """Return the SHA-256 digest for ``path``."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _posix_relpath(target, start):
    """Return a POSIX relative path from ``start`` to ``target``."""
    return os.path.relpath(target, start).replace(os.sep, "/")
