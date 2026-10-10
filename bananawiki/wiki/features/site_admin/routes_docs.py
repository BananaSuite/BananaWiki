"""The built-in user guide: create it as a category, download it, and its settings.

Spawning reuses ``features/auth/docs.py`` (also used by the onboarding wizard).
"""

from __future__ import annotations

import io
import re
import zipfile

from flask import redirect, render_template, request, send_file, url_for

from ... import auth, registry, settings
from ...i18n import BUILTIN_LANGUAGES
from ..audit import record
from ..auth import docs
from ..pages import categories
from .blueprint import bp


def _choice(values) -> tuple[str, str]:
    """(variant, language) from a form or query string; 1.4 sent ``simplified=1``."""
    variant = values.get("variant") or ("simplified" if values.get("simplified") == "1" else "full")
    language = values.get("docs_language") or settings.get("interface_language_fallback") or "en"
    return (variant if variant in docs.VARIANTS else "full"), (language if language in docs.LANGUAGES else "en")


@bp.get("/admin/documentation")
@auth.admin_required
def docs_page():
    return render_template(
        "site_admin/docs.html", category=categories.get(settings.get("docs_category_id")),
        variants=docs.VARIANTS, languages=docs.LANGUAGES, language_names=BUILTIN_LANGUAGES,
        default_language=_choice({})[1],
        slowdown=registry.is_enabled("deletion_slowdown"),
        bypass=bool(settings.get("docs_bypass_deletion_slowdown")),
    )


@bp.post("/admin/spawn-docs")
@auth.admin_required
def spawn_docs():
    variant, language = _choice(request.form)
    user = auth.current_user()
    category = docs.spawn(variant=variant, language=language, actor_id=user["id"])
    record("docs.spawned", target_type="category", target_id=category["id"],
           details={"variant": variant, "language": language})
    auth.flash_t("site_admin.docs.spawned", "success")
    return redirect(url_for("site_admin.docs_page"))


@bp.get("/admin/download-docs")
@auth.admin_required
def download_docs():
    variant, language = _choice(request.args)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for position, (_title, slug, content) in enumerate(docs.pages(variant, language)):
            safe = re.sub(r"[^A-Za-z0-9_-]+", "-", slug).strip("-") or "page"
            archive.writestr(f"{docs.CATEGORY_NAME}/{position:02d}-{safe}.md", content)
    buffer.seek(0)
    return send_file(buffer, mimetype="application/zip", as_attachment=True,
                     download_name=f"bananawiki-docs-{variant}-{language}.zip")


@bp.post("/admin/docs-settings")
@auth.admin_required
def docs_settings():
    if not registry.is_enabled("deletion_slowdown"):
        auth.flash_t("site_admin.docs.slowdown_off", "error")
        return redirect(url_for("site_admin.docs_page"))
    bypass = 1 if request.form.get("docs_bypass_deletion_slowdown") else 0
    settings.update({"docs_bypass_deletion_slowdown": bypass})
    record("docs.settings_changed", details={"bypass_deletion_slowdown": bypass})
    auth.flash_t("common.saved", "success")
    return redirect(url_for("site_admin.docs_page"))
