"""Authenticator enrollment and the second step of password login."""

import time
from datetime import datetime, timezone

from flask import abort, flash, redirect, render_template, request, session, url_for

from helpers._passwords import check_password_hash
from .. import mfa
from ..db import get_account_by_id, get_hosting_db_context, check_and_record_hosting_rate_limit, revoke_all_hosting_sessions
from .auth import _complete_hosting_login, hosting_login_required, hosting_rate_limit


def register_mfa_routes(app):
    @app.route("/login/mfa", methods=["GET", "POST"])
    @hosting_rate_limit(max_requests=10, window=60)
    def hosting_login_mfa():
        account = get_account_by_id(session.get("hosting_mfa_pending_id"))
        if (not account or not account.get("totp_enabled")
                or time.time() - session.get("hosting_mfa_started_at", 0) > 300
                or session.get("hosting_mfa_session_version") != int(account.get("session_version") or 0)):
            session.clear()
            flash("Sign in again to request a new verification code.", "info")
            return redirect(url_for("hosting_login"))
        if request.method == "POST":
            if not check_and_record_hosting_rate_limit(account["id"], "mfa-login", 10, 300):
                abort(429)
            code = request.form.get("code", "")[:128]
            method = "totp" if mfa.consume_totp(account["id"], code) else ""
            if not method and mfa.consume_recovery_code(account["id"], code):
                method = "recovery_code"
            if method:
                return _complete_hosting_login(account, session.get("hosting_mfa_next"), method)
            flash("The code is invalid or has already been used.", "error")
        return render_template("login_mfa.html")

    @app.route("/account/mfa", methods=["GET", "POST"])
    @hosting_login_required
    @hosting_rate_limit(max_requests=10, window=60)
    def hosting_account_mfa():
        if session.get("hosting_impersonator_account_id"):
            abort(403)
        account = get_account_by_id(session["hosting_account_id"])
        enabled = bool(account.get("totp_enabled"))
        if not enabled and (not session.get("mfa_enrollment") or time.time() - session.get("mfa_enrollment_at", 0) > 600):
            session["mfa_enrollment"] = mfa.encrypt_secret(mfa.generate_secret())
            session["mfa_enrollment_at"] = time.time()
        secret = mfa.decrypt_secret(session.get("mfa_enrollment")) if not enabled else ""
        if request.method == "POST":
            password = request.form.get("password", "")
            code = request.form.get("code", "")[:128]
            if len(password) > 1000 or not check_password_hash(account["password"], password):
                flash("The current password is incorrect.", "error")
            elif enabled:
                if mfa.consume_totp(account["id"], code) or mfa.consume_recovery_code(account["id"], code):
                    with get_hosting_db_context() as conn:
                        conn.execute("UPDATE accounts SET totp_enabled=0,totp_secret_encrypted='',totp_recovery_hashes='[]',totp_last_counter=-1,totp_enabled_at=NULL,session_version=session_version+1 WHERE id=?", (account["id"],))
                        conn.commit()
                    revoke_all_hosting_sessions(account["id"])
                    response = _complete_hosting_login(get_account_by_id(account["id"]), url_for("hosting_account"))
                    flash("Two-step verification is disabled. Other sessions have been signed out.", "success")
                    return response
                flash("The code is invalid or has already been used.", "error")
            else:
                counter = mfa.matching_counter(secret, code)
                if counter is None:
                    flash("Enter the current code from your authenticator.", "error")
                else:
                    recovery = mfa.generate_recovery_codes()
                    with get_hosting_db_context() as conn:
                        changed = conn.execute("UPDATE accounts SET totp_enabled=1,totp_secret_encrypted=?,totp_recovery_hashes=?,totp_last_counter=?,totp_enabled_at=?,session_version=session_version+1 WHERE id=? AND totp_enabled=0", (mfa.encrypt_secret(secret), mfa.recovery_hashes(recovery), counter, datetime.now(timezone.utc).isoformat(), account["id"])).rowcount
                        conn.commit()
                    if not changed:
                        abort(409)
                    revoke_all_hosting_sessions(account["id"])
                    _complete_hosting_login(get_account_by_id(account["id"]))
                    return render_template("mfa_recovery.html", recovery_codes=recovery)
        return render_template("account_mfa.html", enabled=enabled, secret=secret,
                               qr=mfa.qr_data_uri(mfa.provisioning_uri(secret, account["username"])) if secret else "")
