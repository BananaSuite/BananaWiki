"""Administrator pairing and explicitly shared read-only remote documentation."""

import secrets
import time
from functools import wraps

from flask import (
    abort,
    flash,
    jsonify,
    make_response,
    redirect,
    render_template,
    request,
    url_for,
)

import config
import db
from federation import protocol, store
from helpers import (
    admin_required,
    editor_required,
    get_current_user,
    login_required,
    user_can_view_category,
)


def enabled(view):
    """Decorator that returns 404 when federation is disabled."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        """Verify the peer signature before the view sees the request."""
        if not config.FEDERATION_ENABLED:
            abort(404)
        return view(*args, **kwargs)
    return wrapped


def can_read(user, copy):
    """Return True if *user* may view the federated *copy*.

    Administrators and owners always have access. Other users need read
    access to the copy's audience category. Copies older than 48 hours
    without a successful synchronization are hidden to limit stale data
    exposure.
    """
    if not user or not copy or copy["last_success"] < time.time() - 172800:
        return False
    return (user["role"] in ("admin", "owner") or
            (copy["audience_category"] is not None and user_can_view_category(user, copy["audience_category"])))


def register_federation_routes(app):
    """Register all federation route groups and error handlers on *app*."""
    @app.context_processor
    def federation_context():
        """Expose federation status to all templates."""
        return {"federation_enabled": config.FEDERATION_ENABLED}

    @app.after_request
    def federation_private_response(response):
        """Apply private caching headers to federation endpoints."""
        if request.path.startswith(("/federation", "/admin/federation")):
            response.headers["Cache-Control"] = "no-store"
            response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.get(protocol.PATH)
    @enabled
    def federation_snapshot():
        """Return a signed snapshot of pages shared with the authenticated peer."""
        if request.query_string:
            abort(400)
        try:
            body, signed = store.snapshot(request.headers)
        except store.AuthenticationError:
            return jsonify(error="Invalid federation authentication."), 401
        except store.PeerBusy:
            return jsonify(error="Try again later."), 429, {"Retry-After": "30"}
        response = make_response(body)
        response.headers["Content-Type"] = "application/json"
        response.headers["BW-Signature"] = signed
        return response

    @app.route("/admin/federation", methods=["GET", "POST"])
    @enabled
    @login_required
    @admin_required
    def admin_federation():
        """Admin panel for pairing wikis and viewing synchronization status."""
        if request.method == "POST":
            try:
                category = request.form.get("category", "")
                store.pair(request.form.get("wiki_id", ""), request.form.get("name", ""),
                           request.form.get("base_url", ""), request.form.get("secret", ""),
                           int(category) if category else None)
                flash("Wiki paired. Synchronization starts after both administrators configure the pairing.", "success")
            except ValueError as error:
                flash(str(error), "error")
            return redirect(url_for("admin_federation"))
        return render_template("federation/admin.html", wiki_id=store.identity(),
                               peers=store.peers(), categories=db.list_categories(),
                               new_secret=secrets.token_hex(32))

    @app.post("/admin/federation/<remote_id>/delete")
    @enabled
    @login_required
    @admin_required
    def federation_delete(remote_id):
        """Remove a pairing and all its local state."""
        store.disconnect(remote_id)
        flash("Pairing, outgoing grants and local remote copies deleted.", "success")
        return redirect(url_for("admin_federation"))

    @app.post("/admin/federation/<remote_id>/sync")
    @enabled
    @login_required
    @admin_required
    def federation_sync(remote_id):
        """Trigger an immediate synchronization with a paired wiki."""
        from federation.runtime import sync_peer
        success = sync_peer(remote_id)
        flash("Synchronized." if success else "No synchronization completed. Check the status or wait for the retry deadline.",
              "success" if success else "info")
        return redirect(url_for("admin_federation"))

    @app.route("/federation/sharing", methods=["GET", "POST"])
    @enabled
    @login_required
    @editor_required
    def federation_sharing():
        """Page for editors to share local pages with paired wikis."""
        user = get_current_user()
        if request.method == "POST":
            try:
                peer_id = request.form.get("peer_id", "")
                if request.form.get("action") == "unshare":
                    store.unshare(peer_id, int(request.form.get("page_id", "")), user)
                    flash("Sharing grant withdrawn.", "success")
                else:
                    page = db.get_page_by_slug(request.form.get("slug", "").strip())
                    if not page:
                        raise ValueError("Page not found.")
                    store.share(peer_id, page["id"], user["id"])
                    flash("Page shared. Future edits will be sent to this wiki until sharing is withdrawn.", "success")
            except ValueError as error:
                flash(str(error), "error")
            return redirect(url_for("federation_sharing"))
        grants = [row for row in store.shares() if user["role"] in ("admin", "owner") or row["granted_by"] == user["id"]]
        return render_template("federation/sharing.html", peers=store.peers(), grants=grants)

    @app.get("/federation")
    @enabled
    @login_required
    def federation_index():
        """List received federated pages visible to the current user."""
        user = get_current_user()
        return render_template("federation/index.html", copies=[row for row in store.copies() if can_read(user, row)])

    @app.get("/federation/copies/<remote_id>/<page_id>")
    @enabled
    @login_required
    def federation_copy(remote_id, page_id):
        """Display a single read-only federated page."""
        copy = store.copies(remote_id, page_id)
        if not can_read(get_current_user(), copy):
            abort(404)
        return render_template("federation/copy.html", copy=copy)

    @app.post("/federation/copies/<remote_id>/<page_id>/fork")
    @enabled
    @login_required
    @admin_required
    def federation_fork(remote_id, page_id):
        """Create an independent editable page from a federated copy."""
        copy = store.copies(remote_id, page_id)
        if not can_read(get_current_user(), copy):
            abort(404)
        # A random local slug avoids a conflicting remote title overwriting a page.
        slug = "federated-" + secrets.token_hex(12)
        content = ("Source: " + copy["base_url"] + copy["source_path"] + "\n"
                   "Source wiki: " + remote_id + "\nRevision: " + copy["revision"] + "\n\n" + copy["content"])
        if copy["audience_category"] is None:
            # Uncategorized pages would broaden the default admin-only audience.
            flash("Pair this wiki with a restricted audience category before creating an editable local fork.", "error")
            return redirect(url_for("federation_copy", remote_id=remote_id, page_id=page_id))
        db.create_page(copy["title"], slug, content, category_id=copy["audience_category"], user_id=get_current_user()["id"])
        return redirect(url_for("view_page", slug=slug))
