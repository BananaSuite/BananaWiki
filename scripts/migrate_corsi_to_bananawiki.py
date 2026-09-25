#!/usr/bin/env python3
"""Build BananaWiki hosting-import archives from the legacy corsi site."""

from __future__ import annotations

import html
import argparse
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE_ROOT = REPO_ROOT.parent / "corsi.canalescuola.it"
DEFAULT_OUT_ROOT = REPO_ROOT.parent / "migration_artifacts" / "corsi_bananawiki"
SOURCE_ROOT = DEFAULT_SOURCE_ROOT
OUT_ROOT = DEFAULT_OUT_ROOT

SKIP_HTML_NAMES = {
    "Quiz.html",
    "checklist.html",
    "r_corretta.html",
    "r_sbagliata.html",
    "AppendChildJS.html",
}

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}
AUDIO_EXTS = {".mp3", ".wav", ".ogg", ".aif", ".aiff"}
VIDEO_EXTS = {".mp4", ".webm", ".mov", ".m4v"}
INLINE_MEDIA_EXTS = IMAGE_EXTS | AUDIO_EXTS | VIDEO_EXTS
IGNORED_NAMES = {".DS_Store"}

ATTR_RE = re.compile(r"""(?P<attr>\b(?:href|src)\s*=\s*)(?P<quote>["'])(?P<url>.*?)(?P=quote)""", re.I)
IMG_TAG_RE = re.compile(r"<img\b[^>]*>", re.I)
SOURCE_TAG_RE = re.compile(r"<source\b[^>]*>", re.I)
SCRIPT_RE = re.compile(r"<script\b[^>]*>.*?</script\s*>", re.I | re.S)
STYLE_RE = re.compile(r"<style\b[^>]*>.*?</style\s*>", re.I | re.S)
ORDER_PREFIX_RE = re.compile(r"^\s*[\dA-Za-z]+[)._-]\s*")
WHITESPACE_RE = re.compile(r"\s+")
SIZE_RE = re.compile(r"\b(width|height)\s*=\s*(['\"]?)(\d{2,5})(?:px)?\2", re.I)
URL_UPLOAD_RE = re.compile(r"^/static/uploads/([^?#]+)")

INLINE_IMAGE_MAX_WIDTH = 820
COMPACT_IMAGE_MAX_WIDTH = 520
SMALL_IMAGE_MAX_WIDTH = 320
OVERSIZED_ASSET_BYTES = 25 * 1024 * 1024
DEFAULT_ENABLED_PLUGINS = {
    "attachments": ("Attachments", "Page file attachments."),
    "audit": ("Audit Log", "Tamper-evident admin and activity timeline."),
    "canvas": ("Canvas", "Visual node-link layouts for wiki content."),
    "chat": ("Chats", "Direct and group messaging."),
    "drafts": ("Drafts", "Autosaved page drafts."),
    "kanban": ("Kanban", "Boards, columns, and tickets."),
    "page_history": ("Page History", "Version history and diff viewer."),
    "tts": ("Text-to-Speech", "Generate and play page narration MP3 files."),
    "user_data_export": ("User Data Export", "Personal data export tools."),
}


def is_ignored_file(path: Path) -> bool:
    return path.name in IGNORED_NAMES


def matching_file(candidate: Path) -> Path | None:
    if candidate.exists() and candidate.is_file() and not is_ignored_file(candidate):
        return candidate
    parent = candidate.parent
    if not parent.exists() or not parent.is_dir():
        return None
    wanted = candidate.name.casefold()
    for child in parent.iterdir():
        if child.is_file() and not is_ignored_file(child) and child.name.casefold() == wanted:
            return child
    suffix = candidate.suffix.casefold()
    if not suffix:
        return None
    best: tuple[float, Path] | None = None
    for child in parent.iterdir():
        if not child.is_file() or is_ignored_file(child) or child.suffix.casefold() != suffix:
            continue
        ratio = SequenceMatcher(None, wanted, child.name.casefold()).ratio()
        if ratio >= 0.94 and (best is None or ratio > best[0]):
            best = (ratio, child)
    return best[1] if best else None


def natural_key(value: str):
    return [int(part) if part.isdigit() else part.casefold() for part in re.split(r"(\d+)", value)]


def clean_label(name: str) -> str:
    suffix = Path(name).suffix.lower()
    if suffix in {".html", ".htm", ".php", ".txt", ".md", ".markdown"} | IMAGE_EXTS | AUDIO_EXTS | VIDEO_EXTS:
        stem = name[: -len(Path(name).suffix)]
    else:
        stem = name
    text = html.unescape(stem)
    text = text.replace("_", " ").replace(".", " ")
    text = re.sub(r"^\s*(?:\d+\s+)+", "", text)
    text = ORDER_PREFIX_RE.sub("", text)
    text = WHITESPACE_RE.sub(" ", text).strip()
    return text or stem or "Untitled"


def safe_upload_name(area_slug: str, source_path: Path) -> str:
    import hashlib

    rel = str(source_path.relative_to(SOURCE_ROOT)).replace(os.sep, "/")
    digest = hashlib.sha1(rel.encode("utf-8"), usedforsecurity=False).hexdigest()[:12]
    base = source_path.stem
    base = re.sub(r"[^\w.-]+", "-", base, flags=re.UNICODE).strip(".-") or "file"
    ext = source_path.suffix or ""
    return f"{area_slug}-{digest}-{base[:70]}{ext}"


def make_slug(slugify, area_slug: str, parts: list[str]) -> str:
    base = "-".join(clean_label(p) for p in parts)
    return slugify(f"{area_slug}-{base}")


def escape_attr(value: str) -> str:
    return html.escape(value or "", quote=True)


def image_dimensions(path: Path) -> tuple[int | None, int | None]:
    if path.suffix.lower() == ".svg":
        try:
            head = path.read_text(encoding="utf-8", errors="ignore")[:2000]
        except OSError:
            return None, None
        width_match = re.search(r'\bwidth=["\']?([\d.]+)', head, re.I)
        height_match = re.search(r'\bheight=["\']?([\d.]+)', head, re.I)
        width = int(float(width_match.group(1))) if width_match else None
        height = int(float(height_match.group(1))) if height_match else None
        return width, height
    try:
        from PIL import Image

        with Image.open(path) as img:
            return img.size
    except Exception:
        return None, None


def media_width_for_image(path: Path, *, inline: bool = False) -> int:
    width, _height = image_dimensions(path)
    name = path.name.casefold()
    if width and width <= SMALL_IMAGE_MAX_WIDTH:
        return min(width, SMALL_IMAGE_MAX_WIDTH)
    if any(token in name for token in ("icon", "logo", "favicon", "badge")):
        return SMALL_IMAGE_MAX_WIDTH
    if any(token in name for token in ("screenshot", "interfaccia", "wireframe")):
        return COMPACT_IMAGE_MAX_WIDTH
    max_width = COMPACT_IMAGE_MAX_WIDTH if inline else INLINE_IMAGE_MAX_WIDTH
    return min(width or max_width, max_width)


def media_figure(url: str, label: str, width: int = INLINE_IMAGE_MAX_WIDTH) -> str:
    safe_url = escape_attr(url)
    safe_label = escape_attr(label)
    width = max(120, min(int(width), INLINE_IMAGE_MAX_WIDTH))
    return (
        f'<figure class="wiki-img-center media-figure media-center" '
        f'style="--media-max-width:{width}px">'
        f'<img src="{safe_url}" alt="{safe_label}" loading="lazy" '
        f'width="{width}" class="media-responsive" '
        f'style="--media-max-width:{width}px">'
        f"<figcaption>{safe_label}</figcaption>"
        f"</figure>"
    )


def compact_media_link(kind: str, label: str, url: str) -> str:
    return f"- [{kind}: {label}]({url})"


def attachment_link(label: str, url: str) -> str:
    return f"- [File: {label}]({url})"


def parse_attrs(tag: str) -> dict[str, str]:
    attrs: dict[str, str] = {}
    for match in re.finditer(r"""([:\w-]+)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""", tag):
        attrs[match.group(1).lower()] = html.unescape(
            match.group(2) or match.group(3) or match.group(4) or ""
        )
    return attrs


def configured_path(value: str | None, default: Path) -> Path:
    if value:
        return Path(value).expanduser().resolve()
    return default.resolve()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build BananaWiki hosting-import archives from the legacy corsi site."
    )
    parser.add_argument(
        "--source-root",
        default=os.environ.get("CORSI_SOURCE_ROOT"),
        help=(
            "Path to the legacy corsi.canalescuola.it folder. "
            f"Default: {DEFAULT_SOURCE_ROOT}"
        ),
    )
    parser.add_argument(
        "--out-root",
        default=(
            os.environ.get("CORSI_MIGRATION_OUT_ROOT")
            or os.environ.get("BANANAWIKI_MIGRATION_OUT")
        ),
        help=(
            "Directory for generated import archives, databases, uploads, and reports. "
            f"Default: {DEFAULT_OUT_ROOT}"
        ),
    )
    return parser.parse_args(argv)


def format_bytes(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{size} B"


def write_report(summary: dict, details: dict[str, dict]) -> Path:
    lines = [
        "# Corsi to BananaWiki migration report",
        "",
        f"- Generated at: `{summary['generated_at']}`",
        f"- Source: `{summary['source']}`",
        f"- Output: `{OUT_ROOT}`",
        "",
        "## Archives",
        "",
    ]
    for result in summary["results"]:
        slug = result["slug"]
        lines.extend([
            f"### {result['site_name']}",
            "",
            f"- Archive: `{result['archive']}`",
            f"- Database: `{result['database']}`",
            f"- Pages created: {result['pages']}",
            f"- Material index pages: {result['material_pages']}",
            f"- Assets copied: {result['assets']}",
            f"- Skipped quiz files: {result['skipped_quizzes']}",
            f"- Missing links: {result['missing_links']}",
            f"- Oversized assets: {result['oversized_assets']}",
            f"- Pages suggested for manual review: {result['review_pages']}",
            f"- Archive size: {format_bytes(result['archive_bytes'])}",
            "",
        ])
        area_details = details.get(slug, {})
        review_pages = area_details.get("review_pages", [])
        if review_pages:
            lines.extend(["#### Manual review candidates", ""])
            for page in review_pages[:40]:
                lines.append(
                    f"- `{page['path']}`: {page['images']} image(s), "
                    f"{page['linked_media']} linked media file(s), "
                    f"{page.get('attachments', 0)} attachment(s), "
                    f"{page['sections']} section(s)"
                )
            if len(review_pages) > 40:
                lines.append(f"- ...and {len(review_pages) - 40} more")
            lines.append("")
        oversized = area_details.get("oversized_assets", [])
        if oversized:
            lines.extend(["#### Oversized assets", ""])
            for asset in oversized[:40]:
                lines.append(f"- `{asset['source']}` ({format_bytes(int(asset['bytes']))})")
            if len(oversized) > 40:
                lines.append(f"- ...and {len(oversized) - 40} more")
            lines.append("")
        missing = area_details.get("missing_links", [])
        if missing:
            lines.extend(["#### Missing links", ""])
            for link in missing[:40]:
                lines.append(f"- `{link['url']}` from `{link['from']}`")
            if len(missing) > 40:
                lines.append(f"- ...and {len(missing) - 40} more")
            lines.append("")
    report_path = OUT_ROOT / "migration-report.md"
    report_path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")
    return report_path


class Migrator:
    def __init__(self, spec: dict, db, config, archive_format):
        self.spec = spec
        self.db = db
        self.config = config
        self.archive_format = archive_format
        self.area_slug = spec["slug"]
        self.platform_root = SOURCE_ROOT / "piattaforma" / spec["platform_dir"]
        self.includes_root = self.platform_root / "includes"
        self.materiale_root = self.platform_root / "materiale"
        self.work_dir = OUT_ROOT / self.area_slug
        self.uploads_dir = self.work_dir / "uploads"
        self.db_path = self.work_dir / "bananawiki.db"
        self.archive_path = OUT_ROOT / f"{self.area_slug}-bananawiki-import.zip"
        self.asset_map: dict[Path, str] = {}
        self.category_cache: dict[tuple[int | None, str], int] = {}
        self.lesson_slug_by_query: dict[tuple[str, str, str], str] = {}
        self.pages_created = 0
        self.assets_copied = 0
        self.material_pages = 0
        self.skipped_quizzes = 0
        self.missing_links: list[dict[str, str]] = []
        self.oversized_assets: list[dict[str, str | int]] = []
        self.review_pages: list[dict[str, str | int]] = []

    def reset(self):
        if self.work_dir.exists():
            shutil.rmtree(self.work_dir)
        self.uploads_dir.mkdir(parents=True, exist_ok=True)
        if self.archive_path.exists():
            self.archive_path.unlink()

    def init_db(self):
        self.config.DATABASE_PATH = str(self.db_path)
        self.config.UPLOAD_FOLDER = str(self.uploads_dir)
        self.db.init_db()
        try:
            from plugin_loader import discover_plugins

            plugins = discover_plugins()
            builtin_manifests = [manifest for _plugin_dir, manifest, is_builtin in plugins if is_builtin]
            self.db.seed_builtin_plugins(builtin_manifests)
        except Exception:
            # If plugin discovery is unavailable, the fallback rows below still
            # make the generated archives land with core plugins enabled.
            pass
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "UPDATE site_settings SET site_name=?, interface_language=?, "
                "interface_language_fallback=?, setup_done=0, timezone=?, "
                "tts_page_panel_enabled=1, tts_public_access_enabled=1, "
                "tts_auto_generate_enabled=0, tts_enabled_languages=? WHERE id=1",
                (self.spec["site_name"], "it", "en", "Europe/Rome", "it,en"),
            )
            conn.execute(
                "UPDATE pages SET title=?, content=? WHERE is_home=1",
                (
                    self.spec["site_name"],
                    f"# {self.spec['site_name']}\n\n"
                    "Questa wiki contiene i materiali migrati dalla piattaforma corsi.canalescuola.it.\n\n"
                    "I quiz e il tracciamento dei progressi non sono stati importati in questa prima migrazione.",
                ),
            )
            now = datetime.now(timezone.utc).isoformat()
            for plugin_id, (name, description) in DEFAULT_ENABLED_PLUGINS.items():
                conn.execute(
                    "INSERT OR IGNORE INTO plugins "
                    "(id, name, version, author, description, builtin, enabled, enabled_at) "
                    "VALUES (?, ?, ?, ?, ?, 1, 1, ?)",
                    (plugin_id, name, "builtin", "BananaWiki", description, now),
                )
                conn.execute(
                    "UPDATE plugins SET enabled=1, enabled_at=COALESCE(enabled_at, ?) "
                    "WHERE id=?",
                    (now, plugin_id),
                )
            conn.commit()

    def category(self, name: str, parent_id: int | None) -> int:
        key = (parent_id, name.casefold())
        if key in self.category_cache:
            return self.category_cache[key]
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT id FROM categories WHERE parent_id IS ? AND lower(name)=lower(?)"
                if parent_id is None
                else "SELECT id FROM categories WHERE parent_id=? AND lower(name)=lower(?)",
                (parent_id, name),
            ).fetchone()
        if row:
            cat_id = int(row["id"])
        else:
            cat_id = self.db.create_category(name, parent_id)
        self.category_cache[key] = cat_id
        return cat_id

    def category_chain(self, parts: list[str]) -> int | None:
        parent = None
        for part in [self.spec["root_category"], *parts]:
            parent = self.category(clean_label(part), parent)
        return parent

    def copy_asset(self, source_path: Path) -> str | None:
        source_path = source_path.resolve()
        if not source_path.exists() or not source_path.is_file():
            return None
        if is_ignored_file(source_path):
            return None
        if source_path in self.asset_map:
            return self.asset_map[source_path]
        upload_name = safe_upload_name(self.area_slug, source_path)
        dest = self.uploads_dir / upload_name
        if not dest.exists():
            shutil.copy2(source_path, dest)
            self.assets_copied += 1
            try:
                size = source_path.stat().st_size
            except OSError:
                size = 0
            if size >= OVERSIZED_ASSET_BYTES:
                self.oversized_assets.append({
                    "source": str(source_path),
                    "upload": upload_name,
                    "bytes": size,
                })
        url = f"/static/uploads/{upload_name}"
        self.asset_map[source_path] = url
        return url

    def upload_source_for_url(self, url: str) -> Path | None:
        match = URL_UPLOAD_RE.match(url or "")
        if not match:
            return None
        upload_name = match.group(1)
        for source_path, mapped_url in self.asset_map.items():
            if mapped_url == f"/static/uploads/{upload_name}":
                return source_path
        return None

    def resolve_old_url(self, raw_url: str, current_html: Path) -> str:
        raw_url = html.unescape(raw_url or "").strip()
        if not raw_url:
            return raw_url
        if raw_url.startswith(("./materiale/http://", "./materiale/https://", "materiale/http://", "materiale/https://")):
            return raw_url.split("materiale/", 1)[1]
        lower = raw_url.lower()
        if lower.startswith(("http://", "https://", "mailto:", "tel:", "#", "data:")):
            # Rewrite old internal lesson URLs when possible; leave real external URLs alone.
            parsed = urlsplit(raw_url)
            if parsed.path.endswith(("materiali.php", "materiali")):
                return f"/page/{self.area_slug}-materiali-indice"
            if parsed.query and "index.php" in parsed.path:
                target = self.slug_for_query(parsed.query)
                if target:
                    return f"/page/{target}"
            if parsed.netloc in {"corsi.canalescuola.it", "www.corsi.canalescuola.it"}:
                candidate = SOURCE_ROOT / unquote(parsed.path).lstrip("/")
                match = matching_file(candidate)
                if match:
                    url = self.copy_asset(match)
                    if url:
                        return url
            return raw_url

        parsed = urlsplit(raw_url)
        if parsed.query and "index.php" in parsed.path:
            target = self.slug_for_query(parsed.query)
            if target:
                return f"/page/{target}"
        if parsed.path.endswith(("materiali.php", "materiali")):
            return f"/page/{self.area_slug}-materiali-indice"

        path_part = unquote(parsed.path)
        candidates: list[Path] = []
        if path_part.startswith("/"):
            stripped = path_part.lstrip("/")
            candidates.append(SOURCE_ROOT / stripped)
            if stripped.startswith(("officina/", "metodo-di-studio/")):
                candidates.append(SOURCE_ROOT / "piattaforma" / stripped)
        else:
            if path_part.startswith("./"):
                path_part = path_part[2:]
            if path_part.startswith("materiale/") or path_part.startswith("materiali/"):
                candidates.append(self.platform_root / path_part)
                candidates.append(self.materiale_root / Path(path_part).name)
                candidates.append(current_html.parent / Path(path_part).name)
            candidates.append(current_html.parent / path_part)
            candidates.append(self.platform_root / path_part)

        for candidate in candidates:
            match = matching_file(candidate)
            if match:
                url = self.copy_asset(match)
                if url:
                    if parsed.fragment:
                        url += f"#{parsed.fragment}"
                    return url
        if path_part.startswith(("materiale/", "materiali/")):
            missing = re.sub(r"[^A-Za-z0-9_-]+", "-", Path(path_part).name).strip("-") or "file"
            self.missing_links.append({
                "from": str(current_html),
                "url": raw_url,
                "kind": "material",
            })
            return f"#missing-{missing}"
        return raw_url

    def normalise_image_tag(self, tag: str) -> str:
        attrs = parse_attrs(tag)
        src = attrs.get("src")
        if not src:
            return tag
        label = attrs.get("alt") or attrs.get("title") or clean_label(Path(urlsplit(src).path).name)
        source_path = self.upload_source_for_url(src)
        if source_path:
            width = media_width_for_image(source_path, inline=True)
        else:
            width_match = SIZE_RE.search(tag)
            width = int(width_match.group(3)) if width_match else COMPACT_IMAGE_MAX_WIDTH
            width = min(width, COMPACT_IMAGE_MAX_WIDTH)
        return media_figure(src, label, width)

    def slug_for_query(self, query: str) -> str | None:
        qs = parse_qs(query.replace("&amp;", "&"))
        corso = (qs.get("corso") or [""])[0]
        argomento = (qs.get("argomento") or [""])[0]
        lezione = (qs.get("lezione") or [""])[0]
        return self.lesson_slug_by_query.get((corso, argomento, lezione))

    def rewrite_html(self, raw: str, current_html: Path) -> str:
        text = SCRIPT_RE.sub("", raw)
        text = STYLE_RE.sub("", text)

        def repl(match: re.Match) -> str:
            url = self.resolve_old_url(match.group("url"), current_html)
            return f"{match.group('attr')}{match.group('quote')}{html.escape(url, quote=True)}{match.group('quote')}"

        text = ATTR_RE.sub(repl, text)
        text = IMG_TAG_RE.sub(lambda m: self.normalise_image_tag(m.group(0)), text)
        return text.strip()

    def source_media_links(self, raw: str, current_html: Path) -> list[str]:
        links = []
        seen = set()
        for tag in SOURCE_TAG_RE.findall(raw):
            url = ""
            for match in ATTR_RE.finditer(tag):
                if match.group("attr").lower().strip().startswith("src"):
                    url = self.resolve_old_url(match.group("url"), current_html)
            if not url or url in seen:
                continue
            seen.add(url)
            label = clean_label(Path(urlsplit(url).path).name)
            links.append(compact_media_link("Media", label, url))
        return links

    def lesson_dirs(self) -> list[Path]:
        out = []
        for directory in self.includes_root.rglob("*"):
            if not directory.is_dir():
                continue
            rel = directory.relative_to(self.includes_root)
            if len(rel.parts) < 2:
                continue
            html_files = [
                p for p in directory.iterdir()
                if p.is_file() and p.suffix.lower() == ".html" and p.name not in SKIP_HTML_NAMES
            ]
            if html_files:
                out.append(directory)
        return sorted(out, key=lambda p: natural_key(str(p.relative_to(self.includes_root))))

    def build_slug_map(self, slugify):
        seen = set()
        for directory in self.lesson_dirs():
            parts = list(directory.relative_to(self.includes_root).parts)
            slug = make_slug(slugify, self.area_slug, parts)
            original = slug
            n = 2
            while slug in seen:
                slug = f"{original}-{n}"
                n += 1
            seen.add(slug)
            if len(parts) >= 3:
                self.lesson_slug_by_query[(parts[0], parts[1], parts[2])] = slug
            directory._bw_slug = slug  # type: ignore[attr-defined]

    def lesson_content(self, directory: Path) -> str:
        parts = list(directory.relative_to(self.includes_root).parts)
        title = clean_label(parts[-1])
        body = [f"# {title}", ""]

        media_files = sorted(
            [
                p for p in directory.iterdir()
                if p.is_file()
                and p.suffix.lower() in INLINE_MEDIA_EXTS
                and not is_ignored_file(p)
            ],
            key=lambda p: natural_key(p.name),
        )
        html_files = sorted(
            [
                p for p in directory.iterdir()
                if p.is_file() and p.suffix.lower() == ".html" and p.name not in SKIP_HTML_NAMES
            ],
            key=lambda p: natural_key(p.name),
        )
        self.skipped_quizzes += sum(1 for p in directory.iterdir() if p.is_file() and p.name == "Quiz.html")

        for media in media_files:
            url = self.copy_asset(media)
            if not url:
                continue
            label = clean_label(media.name)
            ext = media.suffix.lower()
            if ext in IMAGE_EXTS:
                body.append(media_figure(url, label, media_width_for_image(media)))
            elif ext in AUDIO_EXTS:
                body.append(compact_media_link("Audio", label, url))
            elif ext in VIDEO_EXTS:
                body.append(compact_media_link("Video", label, url))
            body.append("")

        for html_file in html_files:
            section_title = clean_label(html_file.name)
            body.append(f"## {section_title}")
            body.append("")
            raw = html_file.read_text(encoding="utf-8", errors="replace")
            rewritten = self.rewrite_html(raw, html_file)
            if rewritten:
                body.append(rewritten)
            media_links = self.source_media_links(raw, html_file)
            if media_links:
                body.append("")
                body.append("**Media collegati**")
                body.extend(media_links)
            body.append("")

        other_files = sorted(
            [
                p for p in directory.iterdir()
                if p.is_file()
                and p.suffix.lower() not in INLINE_MEDIA_EXTS
                and p.suffix.lower() != ".html"
                and not is_ignored_file(p)
            ],
            key=lambda p: natural_key(p.name),
        )
        if other_files:
            body.extend(["## File allegati", ""])
            for file_path in other_files:
                url = self.copy_asset(file_path)
                if url:
                    body.append(attachment_link(file_path.name, url))
            body.append("")
        image_count = sum(1 for p in media_files if p.suffix.lower() in IMAGE_EXTS)
        linked_media_count = sum(1 for p in media_files if p.suffix.lower() in AUDIO_EXTS | VIDEO_EXTS)
        attachment_count = len(other_files)
        if image_count >= 6 or linked_media_count >= 4 or attachment_count >= 4 or len(body) >= 120:
            self.review_pages.append({
                "title": title,
                "path": str(directory.relative_to(self.includes_root)),
                "images": image_count,
                "linked_media": linked_media_count,
                "attachments": attachment_count,
                "sections": len(html_files),
            })
        return "\n".join(body).strip() + "\n"

    def create_lessons(self, slugify):
        lesson_dirs = self.lesson_dirs()
        slug_by_dir = {}
        seen = set()
        for directory in lesson_dirs:
            parts = list(directory.relative_to(self.includes_root).parts)
            slug = make_slug(slugify, self.area_slug, parts)
            original = slug
            n = 2
            while slug in seen:
                slug = f"{original}-{n}"
                n += 1
            seen.add(slug)
            slug_by_dir[directory] = slug
            if len(parts) >= 3:
                self.lesson_slug_by_query[(parts[0], parts[1], parts[2])] = slug

        for directory in lesson_dirs:
            parts = list(directory.relative_to(self.includes_root).parts)
            category_id = self.category_chain(parts[:-1])
            title = clean_label(parts[-1])
            content = self.lesson_content(directory)
            self.db.create_page(title, slug_by_dir[directory], content, category_id=category_id, user_id=None)
            self.pages_created += 1

    def create_material_pages(self, slugify):
        if not self.materiale_root.exists():
            return
        material_root_cat = self.category("Materiali scaricabili", self.category(self.spec["root_category"], None))
        dirs_with_files = []
        for directory, _dirs, files in os.walk(self.materiale_root):
            file_paths = [
                Path(directory) / f for f in files
                if not is_ignored_file(Path(f))
            ]
            if file_paths:
                dirs_with_files.append(Path(directory))
        dirs_with_files.sort(key=lambda p: natural_key(str(p.relative_to(self.materiale_root))))

        used_slugs = {row[0] for row in sqlite3.connect(self.db_path).execute("SELECT slug FROM pages").fetchall()}
        for directory in dirs_with_files:
            rel_parts = list(directory.relative_to(self.materiale_root).parts)
            parent = material_root_cat
            for part in rel_parts[:-1]:
                parent = self.category(clean_label(part), parent)
            title = clean_label(rel_parts[-1]) if rel_parts else "Indice materiali"
            if rel_parts:
                category_id = self.category(clean_label(rel_parts[-1]), parent)
            else:
                category_id = material_root_cat
            page_slug = slugify(f"{self.area_slug}-materiali-{'-'.join(rel_parts) if rel_parts else 'indice'}")
            base = page_slug
            n = 2
            while page_slug in used_slugs:
                page_slug = f"{base}-{n}"
                n += 1
            used_slugs.add(page_slug)
            body = [f"# {title}", "", "File migrati dalla cartella `materiale`.", ""]
            files = sorted(
                [p for p in directory.iterdir() if p.is_file() and not is_ignored_file(p)],
                key=lambda p: natural_key(p.name),
            )
            for file_path in files:
                url = self.copy_asset(file_path)
                if url:
                    body.append(f"- [{file_path.name}]({url})")
            self.db.create_page(title, page_slug, "\n".join(body) + "\n", category_id=category_id, user_id=None)
            self.pages_created += 1
            self.material_pages += 1

    def finalise_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("UPDATE site_settings SET setup_done=0 WHERE id=1")
            conn.commit()

    def build_archive(self):
        manifest = self.archive_format.build_manifest(
            source=self.archive_format.SOURCE_STANDALONE,
            original_subdomain=self.area_slug,
            has_raw_db=True,
            has_site_export_json=False,
            extra={
                "migration_source": "corsi.canalescuola.it",
                "content_only": True,
                "setup_done": False,
            },
        )
        snap_fd, snap_path = tempfile.mkstemp(prefix=f"{self.area_slug}-", suffix=".db")
        os.close(snap_fd)
        try:
            source = sqlite3.connect(self.db_path)
            dest = sqlite3.connect(snap_path)
            try:
                source.backup(dest)
            finally:
                dest.close()
                source.close()

            with zipfile.ZipFile(self.archive_path, "w", compression=zipfile.ZIP_STORED) as zf:
                self.archive_format.write_manifest_to_zip(zf, manifest)
                zf.write(snap_path, self.archive_format.RAW_DB_FILENAME)
                for file_path in sorted(self.uploads_dir.rglob("*")):
                    if file_path.is_file():
                        zf.write(file_path, file_path.relative_to(self.work_dir).as_posix())
        finally:
            try:
                os.unlink(snap_path)
            except OSError:
                pass

    def run(self, slugify):
        self.reset()
        self.init_db()
        self.create_lessons(slugify)
        self.create_material_pages(slugify)
        self.finalise_db()
        self.build_archive()
        return {
            "slug": self.area_slug,
            "site_name": self.spec["site_name"],
            "archive": str(self.archive_path),
            "database": str(self.db_path),
            "pages": self.pages_created,
            "material_pages": self.material_pages,
            "assets": self.assets_copied,
            "skipped_quizzes": self.skipped_quizzes,
            "missing_links": len(self.missing_links),
            "oversized_assets": len(self.oversized_assets),
            "review_pages": len(self.review_pages),
            "archive_bytes": self.archive_path.stat().st_size,
        }


def main(argv: list[str] | None = None) -> int:
    global SOURCE_ROOT, OUT_ROOT

    args = parse_args(argv)
    SOURCE_ROOT = configured_path(args.source_root, DEFAULT_SOURCE_ROOT)
    OUT_ROOT = configured_path(args.out_root, DEFAULT_OUT_ROOT)
    if not SOURCE_ROOT.exists():
        raise SystemExit(f"Legacy corsi source folder not found: {SOURCE_ROOT}")

    os.chdir(REPO_ROOT)
    sys.path.insert(0, str(REPO_ROOT))
    OUT_ROOT.mkdir(parents=True, exist_ok=True)

    import archive_format
    import config
    import db
    from helpers._text import slugify

    specs = [
        {
            "slug": "officina-tecnologica",
            "platform_dir": "officina",
            "site_name": "Officina Tecnologica",
            "root_category": "Officina Tecnologica",
        },
        {
            "slug": "metodo-di-studio",
            "platform_dir": "metodo-di-studio",
            "site_name": "Metodo di Studio",
            "root_category": "Metodo di Studio",
        },
    ]

    results = []
    details = {}
    for spec in specs:
        print(f"Building {spec['site_name']}...")
        migrator = Migrator(spec, db, config, archive_format)
        result = migrator.run(slugify)
        results.append(result)
        details[result["slug"]] = {
            "missing_links": migrator.missing_links,
            "oversized_assets": migrator.oversized_assets,
            "review_pages": migrator.review_pages,
        }

    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": str(SOURCE_ROOT),
        "output": str(OUT_ROOT),
        "results": results,
        "details": details,
    }
    summary_path = OUT_ROOT / "migration-summary.json"
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    report_path = write_report(summary, details)
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    print(f"Report written to {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
