"""Owners and collaborators: the dashboard and everything about one wiki."""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

from flask import (
    Blueprint,
    Response,
    abort,
    current_app,
    flash,
    make_response,
    redirect,
    render_template,
    request,
    url_for,
)

from ...core.timeutil import is_past
from .. import accounts, collaborators, domains, features, instances, settings, urls
from ..errors import ServiceError
from ..i18n import t
from ..limits import rate_limit
from ..runtime import RuntimeFailure
from .common import account, back, detail_url, flash_error, instance_for, owner_locked

bp = Blueprint("dashboard", __name__)


@bp.get("/dashboard")
def dashboard():
    current = account()
    items = instances.dashboard_list(current)
    stats = {status: sum(1 for i in items if i["status"] == status) for status in instances.STATUSES}
    stats["shared"] = sum(1 for i in items if i["role"] == "collaborator")
    stats["storage_mb"] = round(sum(i["storage_mb"] for i in items if i["role"] == "owner"), 1)
    return render_template("hosting/dashboard/dashboard.html", items=items, stats=stats,
                           transfers=collaborators.incoming_transfers(current["id"]),
                           max_instances=current_app.config["HOSTING"].limits.max_per_account)


def _create_context(**extra):
    cfg = current_app.config["HOSTING"]
    return dict(suffix=urls.instance_suffix(), base_domain=cfg.base_domain, duration_days=cfg.limits.duration_days,
                storage_limit_mb=cfg.limits.storage_limit_mb, form=request.form, **extra)


def _credentials_page(inst: dict, username: str, password: str, kind: str):
    response = make_response(render_template("hosting/dashboard/credentials.html", instance=inst, username=username,
                                             password=password, kind=kind, url=urls.instance_url(inst)))
    response.headers["Cache-Control"] = "no-store"
    return response


@bp.route("/instances/create", methods=["GET", "POST"])
@rate_limit(5)
def create_instance():
    current = account()
    if request.method == "GET":
        return render_template("hosting/dashboard/create.html", **_create_context())
    mode = request.form.get("domain_mode", "hosting")
    try:
        if mode not in urls.DOMAIN_MODES or (mode == "apex" and not current["is_admin"]):
            raise ServiceError("hosting.slug.apex_admin_only")
        use_case = (request.form.get("declared_use_case") or "").strip()
        if not 20 <= len(use_case) <= 2000:
            raise ServiceError("hosting.instances.use_case_length")
        if request.form.get("compliance_declared") != "1":
            raise ServiceError("hosting.instances.compliance_required")
        username = password = ""
        if request.form.get("set_admin_credentials") == "1":
            username = (request.form.get("admin_username") or "").strip()
            password = request.form.get("admin_password") or ""
            if not password:
                raise ServiceError("hosting.accounts.password_too_short")
            if password != (request.form.get("admin_confirm_password") or ""):
                raise ServiceError("hosting.accounts.password_mismatch")
        inst, username, password = instances.create(
            current, request.form.get("subdomain") or "", domain_mode=mode, admin_username=username,
            admin_password=password, easy_wiki=request.form.get("easy_wiki") == "1", use_case=use_case,
        )
    except ServiceError as error:
        flash_error(error)
        return render_template("hosting/dashboard/create.html", **_create_context()), 400
    from .. import notifications

    notifications.notify_account(current, "instance_created", slug=inst["subdomain"], url=urls.instance_url(inst))
    return _credentials_page(inst, username, password, "created")


@bp.get("/instances/<instance_id>")
def instance_detail(instance_id: str):
    inst = instance_for(instance_id)
    viewer = account()
    permissions = collaborators.permissions_of(inst, viewer)
    is_owner = inst["account_id"] == viewer["id"]
    manager = is_owner or bool(viewer["is_admin"])
    item = instances.describe(inst)
    legacy_password = instances.take_legacy_password(inst) if manager else ""
    users, users_total = [], 0
    if manager and inst["status"] != "terminated" and instances.provisioning_ready(inst):
        try:
            users, users_total = instances.runtime().list_users(instances.spec(inst, with_policy=False), limit=50)
        except (RuntimeFailure, ValueError):
            users, users_total = [], 0
    history = features.history(inst["id"]) if manager else []
    return render_template(
        "hosting/dashboard/instance.html", instance=item, permissions=permissions, is_owner=is_owner,
        manager=manager, legacy_password=legacy_password, users=users, users_total=users_total,
        collaborators=collaborators.list_for(inst["id"]) if manager else [],
        transfer=collaborators.pending_transfer(inst["id"]) if manager else None,
        incoming=[x for x in collaborators.incoming_transfers(viewer["id"]) if x["instance_id"] == inst["id"]],
        feature_history=history, pending_features={h["feature"] for h in history if h["status"] == "pending"},
        feature_restricted={f: features.restricted(f) for f in features.FEATURES},
        owner=accounts.get(inst["account_id"]),
        domain=domains.binding(inst["id"]), domains_configured=domains.configured(),
        owner_can_delete_expired=settings.flag("allow_owner_delete_expired"),
        owner_can_download=settings.flag("allow_owner_download_expired"),
        upload_policy=instances.upload_policy(inst),
    )


def _act(instance_id: str, permission: str, action, success_key: str):
    inst = instance_for(instance_id, permission)
    if owner_locked(inst):
        flash(t("hosting.instances.suspended_locked"), "error")
        return redirect(detail_url(instance_id))
    try:
        action(inst)
        flash(t(success_key, slug=inst["subdomain"]), "success")
    except ServiceError as error:
        flash_error(error)
    return back(detail_url(instance_id))


@bp.post("/instances/<instance_id>/stop")
@rate_limit(10)
def stop(instance_id: str):
    return _act(instance_id, "start_stop", lambda i: instances.stop(i, actor_id=account()["id"]),
                "hosting.instances.stopped")


@bp.post("/instances/<instance_id>/restart")
@rate_limit(10)
def restart(instance_id: str):
    def resume(inst):
        if inst["status"] == "running":
            instances.restart(inst, actor_id=account()["id"])
        else:
            instances.start(inst, actor_id=account()["id"])

    return _act(instance_id, "start_stop", resume, "hosting.instances.started")


def _expired_block(inst: dict) -> bool:
    return (not account()["is_admin"] and not settings.flag("allow_owner_delete_expired")
            and bool(inst.get("expires_at")) and is_past(inst["expires_at"]))


@bp.post("/instances/<instance_id>/terminate")
@rate_limit(10)
def terminate(instance_id: str):
    inst = instance_for(instance_id, "terminate")
    if owner_locked(inst):
        flash(t("hosting.instances.suspended_locked"), "error")
        return redirect(detail_url(instance_id))
    if _expired_block(inst):
        flash(t("hosting.instances.expired_no_delete"), "error")
        return redirect(detail_url(instance_id))
    try:
        instances.terminate(inst, actor_id=account()["id"])
        flash(t("hosting.instances.terminated_done", slug=inst["subdomain"]), "success")
    except ServiceError as error:
        flash_error(error)
        return redirect(detail_url(instance_id))
    return redirect(url_for("dashboard.dashboard"))


@bp.post("/instances/<instance_id>/toggle-easy-wiki")
@rate_limit(5)
def toggle_easy_wiki(instance_id: str):
    enable = request.form.get("enable") == "1"
    return _act(instance_id, "toggle_mode", lambda i: instances.set_easy_wiki(i, enable, actor_id=account()["id"]),
                "hosting.instances.mode_changed")


@bp.post("/instances/<instance_id>/reset-password")
@rate_limit(5)
def reset_password(instance_id: str):
    inst = instance_for(instance_id, "reset_password")
    if owner_locked(inst):
        flash(t("hosting.instances.suspended_locked"), "error")
        return redirect(detail_url(instance_id))
    try:
        username, password = instances.reset_admin_password(inst, actor_id=account()["id"])
    except ServiceError as error:
        flash_error(error)
        return redirect(detail_url(instance_id))
    return _credentials_page(inst, username, password, "password_reset")


@bp.post("/instances/<instance_id>/reset-wiki")
@rate_limit(3, 300)
def reset_wiki(instance_id: str):
    inst = instance_for(instance_id, "reset_wiki")
    if owner_locked(inst):
        flash(t("hosting.instances.suspended_locked"), "error")
        return redirect(detail_url(instance_id))
    try:
        username, password = instances.reset_content(inst, actor_id=account()["id"])
    except ServiceError as error:
        flash_error(error)
        return redirect(detail_url(instance_id))
    return _credentials_page(inst, username, password, "reset")


@bp.post("/instances/<instance_id>/use-case")
@rate_limit(10)
def update_use_case(instance_id: str):
    inst = instance_for(instance_id, "use_case")
    try:
        if request.form.get("compliance_declared") != "1":
            raise ServiceError("hosting.instances.compliance_required")
        instances.set_use_case(inst, request.form.get("declared_use_case") or "")
        flash(t("hosting.instances.use_case_saved"), "success")
    except ServiceError as error:
        flash_error(error)
    return redirect(detail_url(instance_id))


@bp.post("/instances/<instance_id>/features/<feature>/requests")
@rate_limit(10)
def request_feature(instance_id: str, feature: str):
    inst = instance_for(instance_id)
    if feature not in features.FEATURES:
        abort(404)
    try:
        created = features.request(inst, account(), feature, request.form.get("reason") or "")
        key = "hosting.features.auto_approved" if created["status"] == "approved" else "hosting.features.requested"
        flash(t(key, feature=t(f"hosting.features.{feature}")), "success")
    except ServiceError as error:
        flash_error(error)
    return redirect(detail_url(instance_id))


@bp.post("/instances/<instance_id>/feature-requests/<int:request_id>/cancel")
@rate_limit(10)
def cancel_feature(instance_id: str, request_id: int):
    instance_for(instance_id)
    try:
        features.cancel(request_id, account())
        flash(t("hosting.features.cancelled"), "success")
    except ServiceError as error:
        flash_error(error)
    return redirect(detail_url(instance_id))


@bp.get("/instances/<instance_id>/analytics")
@rate_limit(30)
def analytics(instance_id: str):
    inst = instance_for(instance_id, "analytics")
    try:
        days = max(1, min(365, int(request.args.get("days") or 30)))
    except ValueError:
        days = 30
    try:
        summary = instances.runtime().analytics(instances.spec(inst, with_policy=False), days)
    except (RuntimeFailure, ValueError):
        flash(t("hosting.analytics.unavailable"), "error")
        return redirect(detail_url(instance_id))
    peak = max([1] + [int(row.get(kind, 0) or 0) for row in summary.get("daily", [])
                      for kind in ("request", "page_view", "error")])
    return render_template("hosting/dashboard/analytics.html", instance=inst, summary=summary, days=days, peak=peak)


def stream_file(path: Path, name: str, cleanup: Path) -> Response:
    """Stream a temporary download and remove its directory afterwards."""
    size = path.stat().st_size

    def chunks():
        try:
            with path.open("rb") as handle:
                while chunk := handle.read(1024 * 1024):
                    yield chunk
        finally:
            shutil.rmtree(cleanup, ignore_errors=True)

    response = Response(chunks(), mimetype="application/octet-stream")
    response.headers.set("Content-Disposition", "attachment", filename=name)
    response.headers["Content-Length"] = str(size)
    response.headers["X-Accel-Buffering"] = "no"
    return response


def export_instance(inst: dict) -> Response:
    root = current_app.config["HOSTING"].archives.export_temp_dir
    os.makedirs(root, mode=0o700, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="bwh-archive-", dir=root))
    try:
        archive = instances.runtime().export_archive(instances.spec(inst, with_policy=False), work)
    except (RuntimeFailure, ValueError) as error:
        shutil.rmtree(work, ignore_errors=True)
        raise ServiceError(f"hosting.runtime.{getattr(error, 'code', 'failed')}") from error
    return stream_file(archive, f"{urls.original_slug(inst['subdomain']) or inst['subdomain']}.zip", work)


@bp.post("/instances/<instance_id>/download")
@rate_limit(5, 300)
def download(instance_id: str):
    inst = instance_for(instance_id, "download")
    if not account()["is_admin"] and not settings.flag("allow_owner_download_expired"):
        abort(404)
    if not instances.grace_active(inst) or inst.get("grace_period_suspended"):
        flash(t("hosting.instances.download_unavailable"), "error")
        return redirect(detail_url(instance_id))
    try:
        return export_instance(inst)
    except ServiceError as error:
        flash_error(error)
        return redirect(detail_url(instance_id))


def _bulk(action, allowed_status: tuple[str, ...], success_key: str):
    current = account()
    done = skipped = 0
    for instance_id in request.form.getlist("instance_ids")[:200]:
        inst = instances.get(instance_id)
        if inst is None or inst["account_id"] != current["id"]:
            continue
        if inst["status"] not in allowed_status:
            skipped += 1
            continue
        try:
            action(inst)
            done += 1
        except ServiceError:
            skipped += 1
    flash(t(success_key, done=done, skipped=skipped), "success")
    return redirect(url_for("dashboard.dashboard"))


@bp.post("/instances/bulk-stop")
@rate_limit(10)
def bulk_stop():
    return _bulk(lambda i: instances.stop(i, actor_id=account()["id"]), ("running",), "hosting.bulk.stopped")


@bp.post("/instances/bulk-restart")
@rate_limit(10)
def bulk_restart():
    return _bulk(lambda i: instances.start(i, actor_id=account()["id"]), ("stopped",), "hosting.bulk.started")


@bp.post("/instances/bulk-delete")
@rate_limit(5)
def bulk_delete():
    def terminate_one(inst):
        if _expired_block(inst):
            raise ServiceError("hosting.instances.expired_no_delete")
        instances.terminate(inst, actor_id=account()["id"])

    return _bulk(terminate_one, ("running", "stopped"), "hosting.bulk.terminated")


# ── Collaborators and transfers ───────────────────────────────────────────────


@bp.get("/instances/<instance_id>/collaborators")
def collaborators_page(instance_id: str):
    inst = instance_for(instance_id, "manage_collaborators")
    return render_template("hosting/dashboard/collaborators.html", instance=inst,
                           collaborators=collaborators.list_for(inst["id"]),
                           transfer=collaborators.pending_transfer(inst["id"]),
                           permissions=collaborators.PERMISSIONS,
                           can_transfer=inst["account_id"] == account()["id"] or account()["is_admin"])


def _collab_back(instance_id: str):
    return redirect(url_for("dashboard.collaborators_page", instance_id=instance_id))


@bp.post("/instances/<instance_id>/collaborators/add")
@rate_limit(15)
def add_collaborator(instance_id: str):
    inst = instance_for(instance_id, "manage_collaborators")
    try:
        collaborators.add(inst, account(), request.form.get("username") or "", request.form.get("role", "custom"),
                          request.form.getlist("permissions"))
        flash(t("hosting.collaborators.added"), "success")
    except ServiceError as error:
        flash_error(error)
    return _collab_back(instance_id)


@bp.post("/instances/<instance_id>/collaborators/<collab_account_id>/update")
@rate_limit(15)
def update_collaborator(instance_id: str, collab_account_id: str):
    inst = instance_for(instance_id, "manage_collaborators")
    try:
        collaborators.update(inst, account(), collab_account_id, request.form.get("role", "custom"),
                             request.form.getlist("permissions"))
        flash(t("hosting.collaborators.updated"), "success")
    except ServiceError as error:
        flash_error(error)
    return _collab_back(instance_id)


@bp.post("/instances/<instance_id>/collaborators/<collab_account_id>/remove")
@rate_limit(15)
def remove_collaborator(instance_id: str, collab_account_id: str):
    viewer = account()
    leaving = collab_account_id == viewer["id"]
    inst = instance_for(instance_id, "view" if leaving else "manage_collaborators")
    try:
        collaborators.remove(inst, viewer, collab_account_id)
    except ServiceError as error:
        flash_error(error)
        return _collab_back(instance_id)
    if leaving:
        flash(t("hosting.collaborators.left"), "success")
        return redirect(url_for("dashboard.dashboard"))
    flash(t("hosting.collaborators.removed"), "success")
    return _collab_back(instance_id)


@bp.post("/instances/<instance_id>/transfer")
@rate_limit(5)
def start_transfer(instance_id: str):
    viewer = account()
    inst = instance_for(instance_id)
    if inst["account_id"] != viewer["id"] and not viewer["is_admin"]:
        abort(403)
    try:
        collaborators.start_transfer(inst, viewer, request.form.get("username") or "")
        flash(t("hosting.transfers.started"), "success")
    except ServiceError as error:
        flash_error(error)
    return _collab_back(instance_id)


@bp.post("/transfers/<int:transfer_id>/accept")
@rate_limit(5)
def accept_transfer(transfer_id: int):
    try:
        instance_id = collaborators.accept_transfer(transfer_id, account())
    except ServiceError as error:
        flash_error(error)
        return redirect(url_for("dashboard.dashboard"))
    flash(t("hosting.transfers.accepted"), "success")
    return redirect(detail_url(instance_id))


@bp.post("/transfers/<int:transfer_id>/decline")
@rate_limit(5)
def decline_transfer(transfer_id: int):
    try:
        collaborators.resolve_transfer(transfer_id, account()["id"], as_sender=False)
        flash(t("hosting.transfers.declined"), "success")
    except ServiceError as error:
        flash_error(error)
    return redirect(url_for("dashboard.dashboard"))


@bp.post("/transfers/<int:transfer_id>/cancel")
@rate_limit(5)
def cancel_transfer(transfer_id: int):
    try:
        collaborators.resolve_transfer(transfer_id, account()["id"], as_sender=True)
        flash(t("hosting.transfers.cancelled"), "success")
    except ServiceError as error:
        flash_error(error)
    return redirect(url_for("dashboard.dashboard"))


# ── Custom domain ─────────────────────────────────────────────────────────────


@bp.route("/instances/<instance_id>/domain", methods=["GET", "POST"])
@rate_limit(6)
def instance_domain(instance_id: str):
    viewer = account()
    inst = instance_for(instance_id)
    if inst["account_id"] != viewer["id"] and not viewer["is_admin"]:
        abort(404)
    if request.method == "POST":
        action = request.form.get("action", "claim")
        try:
            if action == "remove":
                domains.remove(inst, viewer["id"])
                flash(t("hosting.domains.removed"), "success")
            elif action == "verify":
                verified = domains.verify(inst, viewer["id"])
                flash(t("hosting.domains.verified"), "success")
                if verified.get("proxied"):
                    flash(t("hosting.domains.verified_proxied"), "info")
            elif action == "claim":
                domains.claim(inst, request.form.get("domain") or "", viewer["id"])
                flash(t("hosting.domains.claimed"), "success")
            else:
                abort(400)
        except ServiceError as error:
            flash_error(error)
        return redirect(url_for("dashboard.instance_domain", instance_id=instance_id))
    cfg = current_app.config["HOSTING"]
    binding = domains.binding(inst["id"])
    return render_template("hosting/dashboard/domain.html", instance=inst, binding=binding,
                           active=bool(binding and domains.resolve(binding["domain"])),
                           configured=domains.configured(), target=cfg.custom_domain_target,
                           ips=cfg.custom_domain_ips, allow_proxied=cfg.custom_domain_allow_proxied)
