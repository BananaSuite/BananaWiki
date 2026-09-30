"""Outgoing webhooks (``admin`` scope, administrators): configure, test, inspect deliveries.

See :mod:`..webhooks` for the delivery format, signing and retries. The
secret is returned only when a webhook is created or its secret rotated.
"""

from __future__ import annotations

from typing import Any

from .. import webhooks
from ..errors import ApiError, flag, json_body, page_window, window_fields
from . import bp, caller, ok, require_admin, requires, secret_response


def _webhook(webhook_id: int) -> dict[str, Any]:
    require_admin()
    hook = webhooks.get(webhook_id)
    if hook is None:
        raise ApiError(404, "webhook_not_found")
    return hook


def _flags(data: dict[str, Any]) -> dict[str, Any]:
    return {name: flag(data[name], name) for name in ("active", "allow_private_network") if name in data}


@bp.get("/admin/webhooks")
@requires("admin")
def list_webhooks():
    require_admin()
    return ok(webhooks=[webhooks.public_view(hook) for hook in webhooks.all_webhooks()],
              events=sorted(webhooks.EVENTS))


@bp.post("/admin/webhooks")
@requires("admin", write=True)
@secret_response
def create_webhook():
    """``{url, events, description?, active?, allow_private_network?}`` → the webhook and its ``secret``."""
    require_admin()
    data = json_body()
    options = _flags(data)
    hook, secret = webhooks.create(url=data.get("url"), events=data.get("events"),
                                   description=data.get("description"), active=options.get("active", True),
                                   allow_private=options.get("allow_private_network", False),
                                   created_by=caller()["id"])
    return ok(201, webhook=webhooks.public_view(hook), secret=secret)


@bp.get("/admin/webhooks/<int:webhook_id>")
@requires("admin")
def get_webhook(webhook_id: int):
    return ok(webhook=webhooks.public_view(_webhook(webhook_id)))


@bp.put("/admin/webhooks/<int:webhook_id>")
@requires("admin", write=True)
def update_webhook(webhook_id: int):
    hook = _webhook(webhook_id)
    data = json_body()
    changes = {name: data[name] for name in ("url", "events", "description") if name in data}
    changes.update(_flags(data))
    return ok(webhook=webhooks.public_view(webhooks.update(hook, changes)))


@bp.delete("/admin/webhooks/<int:webhook_id>")
@requires("admin", write=True)
def delete_webhook(webhook_id: int):
    webhooks.delete(_webhook(webhook_id))
    return ok(deleted=True, id=webhook_id)


@bp.post("/admin/webhooks/<int:webhook_id>/rotate-secret")
@requires("admin", write=True)
@secret_response
def rotate_webhook_secret(webhook_id: int):
    return ok(secret=webhooks.rotate_secret(_webhook(webhook_id)))


@bp.post("/admin/webhooks/<int:webhook_id>/ping")
@requires("admin", write=True)
def ping_webhook(webhook_id: int):
    """Queue a ``ping`` delivery; it is sent by the next run of the delivery job."""
    delivery_id = webhooks.ping(_webhook(webhook_id), actor_id=caller()["id"])
    return ok(202, delivery=webhooks.delivery_view(webhooks.get_delivery(webhook_id, delivery_id)))


@bp.get("/admin/webhooks/<int:webhook_id>/deliveries")
@requires("admin")
def list_deliveries(webhook_id: int):
    hook = _webhook(webhook_id)
    limit, offset = page_window()
    rows = webhooks.deliveries(hook["id"], limit=limit + 1, offset=offset)
    return ok(deliveries=[webhooks.delivery_view(row) for row in rows[:limit]],
              **window_fields(limit, offset, len(rows)))


@bp.get("/admin/webhooks/<int:webhook_id>/deliveries/<int:delivery_id>")
@requires("admin")
def get_delivery(webhook_id: int, delivery_id: int):
    hook = _webhook(webhook_id)
    row = webhooks.get_delivery(hook["id"], delivery_id)
    if row is None:
        raise ApiError(404, "delivery_not_found")
    return ok(delivery=webhooks.delivery_view(row, with_payload=True))


@bp.post("/admin/webhooks/<int:webhook_id>/deliveries/<int:delivery_id>/redeliver")
@requires("admin", write=True)
def redeliver(webhook_id: int, delivery_id: int):
    hook = _webhook(webhook_id)
    row = webhooks.get_delivery(hook["id"], delivery_id)
    if row is None:
        raise ApiError(404, "delivery_not_found")
    if row["state"] == "pending":
        raise ApiError(409, "delivery_pending")
    webhooks.redeliver(row)
    return ok(202, delivery=webhooks.delivery_view(webhooks.get_delivery(hook["id"], delivery_id)))
