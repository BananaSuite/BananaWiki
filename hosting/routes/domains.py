"""Owner domain setup, administrator permission, and certificate authorization."""

from flask import abort, flash, redirect, render_template, request, session, url_for

from .. import config, domains
from ..db import get_account_by_id, get_instance
from ..db._events import list_events
from .auth import hosting_admin_required, hosting_login_required, hosting_rate_limit


def _authorized_instance(instance_id):
    instance = get_instance(instance_id)
    account = get_account_by_id(session["hosting_account_id"])
    if not instance or not account or (instance["account_id"] != account["id"] and not account["is_admin"]):
        abort(404)
    return instance, account


def register_domain_routes(app):
    @app.get("/instances/<instance_id>/domain")
    @hosting_login_required
    def hosting_instance_domain(instance_id):
        instance, account = _authorized_instance(instance_id)
        binding = domains.get_domain(instance_id)
        return render_template(
            "instance_domain.html", instance=instance, binding=binding,
            domain_active=bool(binding and domains.resolve_domain(binding["domain"])),
            domain_target=config.HOSTING_CUSTOM_DOMAIN_TARGET,
            domain_ips=config.HOSTING_CUSTOM_DOMAIN_IPS, domain_admin=bool(account["is_admin"]),
        )

    @app.post("/instances/<instance_id>/domain")
    @hosting_login_required
    @hosting_rate_limit(max_requests=6, window=60)
    def hosting_save_instance_domain(instance_id):
        instance, account = _authorized_instance(instance_id)
        try:
            action = request.form.get("action", "claim")
            if action == "remove":
                domains.remove_domain(instance_id, account["id"])
                flash("Custom domain removed.", "success")
            elif action == "verify":
                domains.verify_domain(instance_id, account["id"])
                flash("DNS verified. Your domain is ready for HTTPS.", "success")
            elif action == "claim":
                domains.claim_domain(instance_id, request.form.get("domain"), account["id"])
                flash("Add the DNS records below, then verify the domain.", "success")
            else:
                abort(400)
        except ValueError as exc:
            flash(str(exc), "error")
        return redirect(url_for("hosting_instance_domain", instance_id=instance_id))

    @app.post("/admin/instances/<instance_id>/domain-permission")
    @hosting_admin_required
    @hosting_rate_limit(max_requests=20, window=60)
    def hosting_domain_permission(instance_id):
        _authorized_instance(instance_id)
        domains.set_permission(instance_id, request.form.get("allowed") == "1", session["hosting_account_id"])
        flash("Custom domain permission updated.", "success")
        return redirect(url_for("hosting_instance_domain", instance_id=instance_id))

    @app.get("/internal/domains/authorize")
    def hosting_authorize_certificate():
        if domains.certificate_allowed(request.args.get("domain", "")):
            return "", 200, {"Cache-Control": "no-store"}
        return "", 403, {"Cache-Control": "no-store"}

    @app.get("/admin/moderation")
    @hosting_admin_required
    def hosting_moderation_history():
        return render_template("moderation_history.html", events=list_events(
            subject_type=request.args.get("type"), subject_id=request.args.get("id"),
        ))
