"""Federation pages: the signed snapshot endpoint, pairing administration, sharing and received copies.

Everything answers 404 unless the operator set ``BW_FEDERATION_ENABLED=1``,
so a wiki that has not opted in looks like one without the feature.
"""

from __future__ import annotations

from flask import Blueprint, abort, jsonify, make_response, redirect, render_template, request, url_for

from ... import auth
from ...db import db
from ..pages import categories, service
from . import protocol, store, sync
from .protocol import ProtocolError

bp = Blueprint("federation", __name__, template_folder="templates")


@bp.before_request
def _only_when_enabled():
    if not sync.active():
        if request.path == protocol.PATH:
            return jsonify({"error": "Not found"}), 404
        abort(404)
    return None


@bp.after_request
def _private(response):
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@bp.get(protocol.PATH)
@auth.public
@auth.exempt("maintenance", "account_steps", "approval")
def snapshot():
    """A signed snapshot of the pages shared with the calling peer (no cookies involved)."""
    if request.query_string:
        return jsonify({"error": "Unexpected query."}), 400
    try:
        body, signed = store.snapshot(request.headers)
    except ProtocolError:
        return jsonify({"error": "Invalid federation authentication."}), 401
    except store.PeerBusy:
        return jsonify({"error": "Try again later."}), 429, {"Retry-After": str(store.SERVE_INTERVAL)}
    response = make_response(body)
    response.headers["Content-Type"] = "application/json"
    response.headers["BW-Signature"] = signed
    return response


# ── Administration ────────────────────────────────────────────────────────────


@bp.route("/admin/federation", methods=["GET", "POST"])
@auth.admin_required
def admin():
    if request.method == "POST":
        raw_category = request.form.get("category", "").strip()
        try:
            store.pair(request.form.get("wiki_id", "").strip(), request.form.get("name", ""),
                       request.form.get("base_url", "").strip(), request.form.get("secret", "").strip(),
                       category=int(raw_category) if raw_category.isdigit() else None,
                       allow_private=bool(request.form.get("allow_private_network")))
        except ProtocolError as error:
            auth.flash_t(error.key, "error")
        else:
            auth.flash_t("federation.flash.paired", "success")
        return redirect(url_for("federation.admin"))
    return render_template("federation/admin.html", wiki_id=store.identity(), peers=store.peers(),
                           categories=categories.all_categories(), new_secret=protocol.new_secret())


@bp.post("/admin/federation/<remote_id>/delete")
@auth.admin_required
def disconnect(remote_id: str):
    store.disconnect(remote_id)
    auth.flash_t("federation.flash.disconnected", "success")
    return redirect(url_for("federation.admin"))


@bp.post("/admin/federation/<remote_id>/sync")
@auth.admin_required
def sync_now(remote_id: str):
    if sync.sync_peer(remote_id, force=True):
        auth.flash_t("federation.flash.synced", "success")
    else:
        auth.flash_t("federation.flash.not_synced", "info")
    return redirect(url_for("federation.admin"))


# ── Sharing ───────────────────────────────────────────────────────────────────


@bp.route("/federation/sharing", methods=["GET", "POST"])
@auth.editor_required
def sharing():
    user = auth.current_user()
    if request.method == "POST":
        peer_id = request.form.get("peer_id", "")
        if request.form.get("action") == "unshare":
            page_id = request.form.get("page_id", "")
            if page_id.isdigit() and store.unshare(peer_id, int(page_id), user):
                auth.flash_t("federation.flash.unshared", "success")
            else:
                auth.flash_t("federation.error.share_missing", "error")
        else:
            page = service.get_by_slug(request.form.get("slug", "").strip())
            try:
                if page is None or not service.can_view(page, user):
                    raise ProtocolError("federation.error.page_missing")
                store.share(peer_id, page, user)
            except ProtocolError as error:
                auth.flash_t(error.key, "error")
            else:
                auth.flash_t("federation.flash.shared", "success")
        return redirect(url_for("federation.sharing"))
    options = [(peer["wiki_id"], peer["name"]) for peer in store.peers()]
    return render_template("federation/sharing.html", peer_options=options, grants=store.shares(user))


# ── Received copies ───────────────────────────────────────────────────────────


@bp.get("/federation")
def index():
    user = auth.current_user()
    return render_template("federation/index.html", copies=[row for row in store.copies() if store.can_read(user, row)])


def _readable_copy(remote_id: str, page_id: str):
    item = store.copy(remote_id, page_id)
    if not store.can_read(auth.current_user(), item):
        abort(404)
    return item


@bp.get("/federation/copies/<remote_id>/<page_id>")
def copy(remote_id: str, page_id: str):
    return render_template("federation/copy.html", copy=_readable_copy(remote_id, page_id))


@bp.post("/federation/copies/<remote_id>/<page_id>/fork")
@auth.admin_required
def fork(remote_id: str, page_id: str):
    item = _readable_copy(remote_id, page_id)
    try:
        page = store.fork(item, auth.current_user())
    except (ProtocolError, service.PageError) as error:
        auth.flash_t(error.key, "error")
        return redirect(url_for("federation.copy", remote_id=remote_id, page_id=page_id))
    return redirect(url_for("pages.view", slug=page["slug"]))


def nav_visible(user) -> bool:
    return bool(user) and sync.active() and bool(db.scalar("SELECT 1 FROM federation_peers LIMIT 1"))
