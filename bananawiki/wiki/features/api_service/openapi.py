"""OpenAPI 3.1 description of ``/api/v1``, also used by the ``/api-docs`` page.

:data:`ENDPOINTS` lists every route of the API blueprint (a test keeps the two
in step): its scope, the query parameters and JSON body it reads, what it
returns and the statuses it answers with. Shared shapes are in
:data:`SCHEMAS`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

API_VERSION = "2.2.0"

Schema = dict[str, Any]


def _ref(name: str) -> Schema:
    return {"$ref": f"#/components/schemas/{name}"}


def _list(item: Schema) -> Schema:
    return {"type": "array", "items": item}


STRING: Schema = {"type": "string"}
INTEGER: Schema = {"type": "integer"}
BOOLEAN: Schema = {"type": "boolean"}
OBJECT: Schema = {"type": "object"}
DATETIME: Schema = {"type": ["string", "null"], "format": "date-time"}
NULLABLE_ID: Schema = {"type": ["integer", "null"]}
STRINGS: Schema = _list(STRING)
IDS: Schema = _list(INTEGER)


@dataclass(frozen=True)
class Endpoint:
    method: str
    path: str
    summary: str
    scope: str | None = None  # None: no token needed
    write: bool = False
    group: str = "pages"
    params: tuple[str, ...] = ()  # query parameters
    body: dict[str, Schema] = field(default_factory=dict)  # JSON body properties
    required: tuple[str, ...] = ()  # required body properties
    responses: tuple[int, ...] = (200,)
    result: dict[str, Schema] = field(default_factory=dict)  # properties of the success answer
    feature: str | None = None  # 404 while this feature is off
    upload: bool = False  # multipart/form-data with a ``file`` field
    download: bool = False  # answers with the file itself
    if_match: str | None = None  # ETag prefix accepted in If-Match
    paginated: bool = False  # ?limit=&offset= and limit/offset/next_offset in the answer


_PAGE_BODY = {"title": STRING, "content": STRING, "slug": STRING, "category_id": NULLABLE_ID,
              "edit_message": STRING}
_TICKET_BODY = {"title": STRING, "description": STRING, "priority": {"enum": ["low", "medium", "high", "critical"]},
                "color": STRING, "due_date": {"type": ["string", "null"], "format": "date"}, "labels": STRINGS,
                "assignees": STRINGS}
_WEBHOOK_BODY = {"url": {"type": "string", "format": "uri"}, "events": STRINGS, "description": STRING,
                 "active": BOOLEAN, "allow_private_network": BOOLEAN}
_DOCUMENT = {"type": "object", "required": ["nodes", "edges"],
             "properties": {"nodes": _list(OBJECT), "edges": _list(OBJECT), "viewport": OBJECT}}
_DELETED = {"deleted": BOOLEAN, "id": INTEGER}
_FILTER_PARAMS = ("q", "who", "label", "priority", "due")
_CHECKLIST = {"checklist": _list(_ref("ChecklistItem")), "ticket": _ref("Ticket")}

ENDPOINTS: tuple[Endpoint, ...] = (
    # Service
    Endpoint("GET", "/status", "Service status (no token)", None, group="meta",
             result={"api_enabled": BOOLEAN, "version": STRING, "service": STRING}),
    Endpoint("GET", "/openapi.json", "This description (no token)", None, group="meta"),
    # Pages
    Endpoint("GET", "/pages", "Pages the caller may read", "pages", params=("category_id",), paginated=True,
             result={"pages": _list(_ref("PageSummary"))}),
    Endpoint("GET", "/pages/{slug}", "One page with its Markdown", "pages", responses=(200, 404),
             result={"page": _ref("Page")}),
    Endpoint("POST", "/pages", "Create a page", "pages", True, body=_PAGE_BODY, required=("title",),
             responses=(201, 400, 403, 409), result={"page": _ref("Page")}),
    Endpoint("PUT", "/pages/{slug}", "Change title, content or category", "pages", True,
             body={**_PAGE_BODY, "expected_revision": INTEGER}, responses=(200, 400, 403, 404, 409, 412),
             result={"page": _ref("Page")}, if_match="r"),
    Endpoint("DELETE", "/pages/{slug}", "Delete a page (202 when a grace period applies)", "pages", True,
             responses=(200, 202, 403, 404, 409), result={"deleted": BOOLEAN, "pending_deletion": BOOLEAN,
                                                          "slug": STRING}),
    Endpoint("GET", "/pages/{slug}/history", "Revisions of a page", "pages", paginated=True,
             responses=(200, 403, 404), result={"history": _list(_ref("HistoryEntry"))}),
    Endpoint("GET", "/history/{entry_id}", "One revision with its content", "pages", responses=(200, 403, 404),
             result={"entry": _ref("HistoryEntry")}),
    Endpoint("GET", "/search", "Full-text search", "pages", params=("q", "titles_only"), paginated=True,
             responses=(200, 403), result={"results": _list(_ref("SearchResult"))}),
    Endpoint("POST", "/pages/bulk", "Create up to 100 pages (administrators)", "pages", True,
             body={"pages": _list(OBJECT)}, required=("pages",), responses=(200, 201, 400, 403),
             result={"created": _list(_ref("PageSummary")), "errors": _list(_ref("ItemError"))}),
    Endpoint("POST", "/pages/bulk-edit", "Edit up to 100 pages (administrators)", "pages", True,
             body={"edits": _list(OBJECT)}, required=("edits",), responses=(200, 400, 403),
             result={"updated": INTEGER, "errors": _list(_ref("ItemError"))}),
    Endpoint("POST", "/pages/bulk-delete", "Delete up to 100 pages (administrators)", "pages", True,
             body={"slugs": STRINGS, "ids": IDS}, responses=(200, 400, 403),
             result={"deleted": INTEGER, "pending_deletion": INTEGER, "skipped": INTEGER, "not_found": INTEGER}),
    # Page attachments
    Endpoint("GET", "/pages/{slug}/attachments", "Files attached to a page", "pages", group="attachments",
             feature="attachments", responses=(200, 403, 404), result={"attachments": _list(_ref("Attachment"))}),
    Endpoint("GET", "/pages/{slug}/attachments/{attachment_id}", "Download an attachment", "pages",
             group="attachments", feature="attachments", responses=(200, 403, 404), download=True),
    Endpoint("POST", "/pages/{slug}/attachments", "Attach a file (multipart, field 'file')", "pages", True,
             group="attachments", feature="attachments", upload=True, responses=(201, 400, 403, 404, 413),
             result={"attachment": _ref("Attachment")}),
    Endpoint("DELETE", "/pages/{slug}/attachments/{attachment_id}", "Delete an attachment", "pages", True,
             group="attachments", feature="attachments", responses=(200, 403, 404), result=_DELETED),
    # Categories
    Endpoint("GET", "/categories", "Categories the caller may read", "categories", group="categories",
             paginated=True, result={"categories": _list(_ref("Category"))}),
    Endpoint("GET", "/categories/{category_id}", "One category and its pages", "categories", group="categories",
             responses=(200, 404), result={"category": _ref("Category")}),
    Endpoint("POST", "/categories", "Create a category", "categories", True, group="categories",
             body={"name": STRING, "parent_id": NULLABLE_ID}, required=("name",), responses=(201, 400, 403),
             result={"category": _ref("Category")}),
    Endpoint("PUT", "/categories/{category_id}", "Rename, move or set sequential navigation", "categories", True,
             group="categories", body={"name": STRING, "parent_id": NULLABLE_ID, "sequential_nav": BOOLEAN},
             responses=(200, 400, 403, 404), result={"category": _ref("Category")}),
    Endpoint("DELETE", "/categories/{category_id}", "Delete a category (202 when a grace period applies to its pages)",
             "categories", True, group="categories", params=("page_action", "target_id"),
             responses=(200, 202, 400, 403, 404, 409),
             result={**_DELETED, "pending_deletion": STRINGS, "kept": STRINGS}),
    # Kanban
    Endpoint("GET", "/kanban/boards", "Boards the caller can open (archived ones with archived=1)", "kanban",
             group="kanban", feature="kanban", params=("archived",), paginated=True, responses=(200, 400),
             result={"boards": _list(_ref("Board"))}),
    Endpoint("POST", "/kanban/boards", "Create a board", "kanban", True, group="kanban", feature="kanban",
             body={"title": STRING, "description": STRING, "default_columns": BOOLEAN}, required=("title",),
             responses=(201, 400, 403), result={"board": _ref("Board")}),
    Endpoint("GET", "/kanban/boards/{board_id}", "A board with its columns and active tickets (filterable)", "kanban",
             group="kanban", feature="kanban", params=_FILTER_PARAMS, responses=(200, 400, 404),
             result={"board": _ref("Board"), "tickets": _list(_ref("Ticket")), "filter": OBJECT, "shown": INTEGER,
                     "total": INTEGER}),
    Endpoint("PUT", "/kanban/boards/{board_id}", "Change a board's title, description or visibility", "kanban",
             True, group="kanban", feature="kanban",
             body={"title": STRING, "description": STRING, "visibility": {"enum": ["private", "shared", "public"]}},
             responses=(200, 400, 403, 404), result={"board": _ref("Board")}),
    Endpoint("DELETE", "/kanban/boards/{board_id}", "Delete a board (creator and administrators)", "kanban", True,
             group="kanban", feature="kanban", responses=(200, 403, 404), result=_DELETED),
    Endpoint("POST", "/kanban/boards/{board_id}/archive", "Archive a board (owner; it becomes read-only)", "kanban",
             True, group="kanban", feature="kanban", responses=(200, 403, 404), result={"board": _ref("Board")}),
    Endpoint("POST", "/kanban/boards/{board_id}/restore", "Restore an archived board (owner)", "kanban", True,
             group="kanban", feature="kanban", responses=(200, 403, 404), result={"board": _ref("Board")}),
    Endpoint("GET", "/kanban/boards/{board_id}/archived-tickets", "A board's archived tickets, newest first",
             "kanban", group="kanban", feature="kanban", paginated=True, responses=(200, 404),
             result={"tickets": _list(_ref("ArchivedTicket")), "total": INTEGER}),
    Endpoint("POST", "/kanban/boards/{board_id}/tickets/archive", "Archive tickets of the board by id", "kanban",
             True, group="kanban", feature="kanban", body={"ids": IDS}, required=("ids",),
             responses=(200, 400, 403, 404), result={"archived": INTEGER}),
    Endpoint("POST", "/kanban/boards/{board_id}/tickets/restore", "Restore archived tickets of the board by id",
             "kanban", True, group="kanban", feature="kanban", body={"ids": IDS}, required=("ids",),
             responses=(200, 400, 403, 404), result={"restored": INTEGER}),
    Endpoint("POST", "/kanban/boards/{board_id}/columns", "Add a column", "kanban", True, group="kanban",
             feature="kanban", body={"title": STRING}, required=("title",), responses=(201, 400, 403, 404),
             result={"column": _ref("Column")}),
    Endpoint("POST", "/kanban/boards/{board_id}/columns/reorder", "Reorder the columns", "kanban", True,
             group="kanban", feature="kanban", body={"order": IDS}, required=("order",),
             responses=(200, 400, 403, 404), result={"order": IDS}),
    Endpoint("PUT", "/kanban/columns/{column_id}", "Rename a column or set its WIP limit", "kanban", True,
             group="kanban", feature="kanban", body={"title": STRING, "wip_limit": NULLABLE_ID},
             responses=(200, 400, 403, 404), result={"column": _ref("Column")}),
    Endpoint("DELETE", "/kanban/columns/{column_id}", "Delete a column and its tickets", "kanban", True,
             group="kanban", feature="kanban", responses=(200, 403, 404), result=_DELETED),
    Endpoint("POST", "/kanban/columns/{column_id}/archive", "Archive every active ticket of a column", "kanban",
             True, group="kanban", feature="kanban", responses=(200, 403, 404), result={"archived": INTEGER}),
    Endpoint("POST", "/kanban/columns/{column_id}/tickets", "Create a ticket", "kanban", True, group="kanban",
             feature="kanban", body=_TICKET_BODY, required=("title",), responses=(201, 400, 403, 404),
             result={"ticket": _ref("Ticket")}),
    Endpoint("GET", "/kanban/tickets/{ticket_id}", "One ticket with its description", "kanban", group="kanban",
             feature="kanban", responses=(200, 404), result={"ticket": _ref("Ticket")}),
    Endpoint("PUT", "/kanban/tickets/{ticket_id}", "Change a ticket", "kanban", True, group="kanban",
             feature="kanban", body=_TICKET_BODY, responses=(200, 400, 403, 404), result={"ticket": _ref("Ticket")}),
    Endpoint("POST", "/kanban/tickets/{ticket_id}/move", "Move a ticket to a column and position", "kanban", True,
             group="kanban", feature="kanban", body={"column_id": INTEGER, "position": INTEGER},
             required=("column_id",), responses=(200, 400, 403, 404),
             result={"ticket": _ref("Ticket"), "columns": {"type": "object", "additionalProperties": IDS}}),
    Endpoint("DELETE", "/kanban/tickets/{ticket_id}", "Delete a ticket", "kanban", True, group="kanban",
             feature="kanban", responses=(200, 403, 404), result=_DELETED),
    Endpoint("POST", "/kanban/tickets/{ticket_id}/archive", "Archive a ticket", "kanban", True, group="kanban",
             feature="kanban", responses=(200, 403, 404), result={"ticket": _ref("Ticket"), "changed": BOOLEAN}),
    Endpoint("POST", "/kanban/tickets/{ticket_id}/restore", "Restore a ticket to the end of its column", "kanban",
             True, group="kanban", feature="kanban", responses=(200, 403, 404),
             result={"ticket": _ref("Ticket"), "changed": BOOLEAN}),
    Endpoint("GET", "/kanban/tickets/{ticket_id}/checklist", "A ticket's checklist", "kanban", group="kanban",
             feature="kanban", responses=(200, 404), result={"checklist": _list(_ref("ChecklistItem"))}),
    Endpoint("POST", "/kanban/tickets/{ticket_id}/checklist", "Add a checklist item", "kanban", True,
             group="kanban", feature="kanban", body={"text": STRING}, required=("text",),
             responses=(201, 400, 403, 404), result=_CHECKLIST),
    Endpoint("POST", "/kanban/tickets/{ticket_id}/checklist/reorder", "Reorder a ticket's checklist", "kanban",
             True, group="kanban", feature="kanban", body={"order": IDS}, required=("order",),
             responses=(200, 400, 403, 404), result=_CHECKLIST),
    Endpoint("PUT", "/kanban/checklist/{item_id}", "Change or tick a checklist item", "kanban", True,
             group="kanban", feature="kanban", body={"text": STRING, "done": BOOLEAN},
             responses=(200, 400, 403, 404), result=_CHECKLIST),
    Endpoint("DELETE", "/kanban/checklist/{item_id}", "Delete a checklist item", "kanban", True, group="kanban",
             feature="kanban", responses=(200, 403, 404), result={**_DELETED, **_CHECKLIST}),
    Endpoint("GET", "/kanban/my-tickets", "Tickets assigned to the caller, earliest due date first", "kanban",
             group="kanban", feature="kanban", params=("q", "label", "priority", "due"), paginated=True,
             responses=(200, 400, 403),
             result={"tickets": _list(_ref("AssignedTicket"))}),
    Endpoint("GET", "/kanban/tickets/{ticket_id}/comments", "Comments on a ticket", "kanban", group="kanban",
             feature="kanban", responses=(200, 404), result={"comments": _list(_ref("Comment"))}),
    Endpoint("POST", "/kanban/tickets/{ticket_id}/comments", "Comment on a ticket", "kanban", True,
             group="kanban", feature="kanban", body={"content": STRING}, required=("content",),
             responses=(201, 400, 403, 404), result={"comment": _ref("Comment")}),
    Endpoint("PUT", "/kanban/comments/{comment_id}", "Edit your comment", "kanban", True, group="kanban",
             feature="kanban", body={"content": STRING}, required=("content",), responses=(200, 400, 403, 404),
             result={"comment": _ref("Comment")}),
    Endpoint("DELETE", "/kanban/comments/{comment_id}", "Delete your comment", "kanban", True, group="kanban",
             feature="kanban", responses=(200, 403, 404), result=_DELETED),
    Endpoint("GET", "/kanban/tickets/{ticket_id}/attachments", "Files attached to a ticket", "kanban",
             group="kanban", feature="kanban", responses=(200, 404),
             result={"attachments": _list(_ref("Attachment"))}),
    Endpoint("POST", "/kanban/tickets/{ticket_id}/attachments", "Attach a file to a ticket (multipart)", "kanban",
             True, group="kanban", feature="kanban", upload=True, responses=(201, 400, 403, 404, 413),
             result={"attachment": _ref("Attachment")}),
    Endpoint("GET", "/kanban/attachments/{attachment_id}", "Download a ticket attachment", "kanban",
             group="kanban", feature="kanban", responses=(200, 404), download=True),
    Endpoint("DELETE", "/kanban/attachments/{attachment_id}", "Delete a ticket attachment", "kanban", True,
             group="kanban", feature="kanban", responses=(200, 403, 404), result=_DELETED),
    # Canvas
    Endpoint("GET", "/canvas", "Canvases the caller can open", "canvas", group="canvas", feature="canvas",
             paginated=True, result={"canvases": _list(_ref("Canvas"))}),
    Endpoint("POST", "/canvas", "Create a canvas", "canvas", True, group="canvas", feature="canvas",
             body={"title": STRING, "description": STRING, "data": _DOCUMENT}, required=("title",),
             responses=(201, 400, 403), result={"canvas": _ref("Canvas")}),
    Endpoint("GET", "/canvas/{slug}", "A canvas and its document", "canvas", group="canvas", feature="canvas",
             responses=(200, 404), result={"canvas": _ref("Canvas"), "data": _DOCUMENT}),
    Endpoint("PUT", "/canvas/{slug}", "Change a canvas's title, description or visibility", "canvas", True,
             group="canvas", feature="canvas",
             body={"title": STRING, "description": STRING, "visibility": {"enum": ["private", "shared", "public"]}},
             responses=(200, 400, 403, 404), result={"canvas": _ref("Canvas")}),
    Endpoint("PUT", "/canvas/{slug}/document", "Replace a canvas's document", "canvas", True, group="canvas",
             feature="canvas", body={"data": _DOCUMENT, "expected_version": INTEGER}, required=("data",),
             responses=(200, 400, 403, 404, 409, 412), result={"canvas": _ref("Canvas"), "seq": INTEGER},
             if_match="v"),
    Endpoint("POST", "/canvas/{slug}/ops", "Apply editing operations atomically", "canvas", True, group="canvas",
             feature="canvas", body={"ops": _list(OBJECT)}, required=("ops",), responses=(200, 400, 403, 404),
             result={"canvas": _ref("Canvas"), "applied": INTEGER, "seq": INTEGER}),
    Endpoint("GET", "/canvas/{slug}/history", "Saved versions of a canvas", "canvas", group="canvas",
             feature="canvas", paginated=True, responses=(200, 404),
             result={"history": _list(_ref("HistoryEntry"))}),
    Endpoint("DELETE", "/canvas/{slug}", "Delete a canvas (creator and administrators)", "canvas", True,
             group="canvas", feature="canvas", responses=(200, 403, 404),
             result={"deleted": BOOLEAN, "slug": STRING}),
    # Accounts
    Endpoint("GET", "/users", "All accounts (administrators)", "users", group="users", paginated=True,
             result={"users": _list(_ref("User"))}),
    Endpoint("GET", "/users/{user_id}", "One account (administrators)", "users", group="users",
             responses=(200, 404), result={"user": _ref("User")}),
    Endpoint("POST", "/users", "Create an account (administrators)", "users", True, group="users",
             body={"username": STRING, "password": STRING, "role": {"enum": ["user", "editor", "admin"]},
                   "force_password_change": BOOLEAN}, required=("username", "password"),
             responses=(201, 400, 403, 409), result={"user": _ref("User")}),
    Endpoint("POST", "/users/bulk", "Create up to 20 accounts (administrators)", "users", True, group="users",
             body={"users": _list(OBJECT)}, required=("users",), responses=(200, 201, 400, 403),
             result={"created": _list(_ref("User")), "errors": _list(_ref("ItemError"))}),
    Endpoint("PUT", "/users/{user_id}", "Change role, suspension, API access or password", "users", True,
             group="users", body={"role": {"enum": ["user", "editor", "admin"]}, "suspended": BOOLEAN,
                                  "api_access_enabled": BOOLEAN, "password": STRING},
             responses=(200, 400, 403, 404), result={"user": _ref("User"), "api_tokens_revoked": INTEGER}),
    Endpoint("DELETE", "/users/{user_id}", "Delete an account (administrators)", "users", True, group="users",
             responses=(200, 400, 403, 404), result={"deleted": BOOLEAN, "id": STRING}),
    # Settings
    Endpoint("GET", "/settings", "Site settings (administrators)", "settings", group="settings",
             result={"settings": OBJECT}),
    Endpoint("PUT", "/settings", "Change allowed site settings, all or nothing", "settings", True,
             group="settings", body={"<setting>": {}}, responses=(200, 400, 403), result={"updated": STRINGS}),
    # Tokens
    Endpoint("GET", "/tokens", "Your active tokens", "tokens", group="tokens",
             result={"tokens": _list(_ref("Token"))}),
    Endpoint("POST", "/tokens", "Issue a token no broader than the calling one", "tokens", True, group="tokens",
             body={"name": STRING, "permissions": _ref("Grant"), "expires_at": {"type": "string",
                                                                                  "format": "date-time"}},
             responses=(201, 400, 403),
             result={"token": STRING, "id": INTEGER, "permissions": _ref("Grant"), "expires_at": DATETIME}),
    Endpoint("DELETE", "/tokens/{token_id}", "Revoke one of your tokens", "tokens", True, group="tokens",
             responses=(200, 404), result={"revoked": BOOLEAN, "id": INTEGER}),
    # Administration
    Endpoint("GET", "/admin/tokens", "Every active token (administrators)", "admin", group="admin",
             result={"tokens": _list(_ref("Token"))}),
    Endpoint("POST", "/admin/tokens/{token_id}/revoke", "Revoke any token (administrators)", "admin", True,
             group="admin", responses=(200, 403, 404), result={"revoked": BOOLEAN, "id": INTEGER}),
    Endpoint("PUT", "/admin/users/{user_id}/api-access", "Allow or deny API use for an account", "admin", True,
             group="admin", body={"enabled": BOOLEAN}, required=("enabled",), responses=(200, 400, 403, 404),
             result={"api_access_enabled": BOOLEAN, "id": STRING}),
    Endpoint("GET", "/admin/audit-log", "API audit log", "admin", group="admin", paginated=True,
             result={"entries": _list(OBJECT), "total": INTEGER}),
    Endpoint("DELETE", "/admin/audit-log", "Clear old audit entries (superusers)", "admin", True, group="admin",
             params=("before_days",), result={"cleared": INTEGER, "before_days": INTEGER}),
    # Webhooks
    Endpoint("GET", "/admin/webhooks", "Outgoing webhooks and the events they can subscribe to", "admin",
             group="webhooks", result={"webhooks": _list(_ref("Webhook")), "events": STRINGS}),
    Endpoint("POST", "/admin/webhooks", "Register a webhook (the secret is shown once)", "admin", True,
             group="webhooks", body=_WEBHOOK_BODY, required=("url", "events"), responses=(201, 400, 403),
             result={"webhook": _ref("Webhook"), "secret": STRING}),
    Endpoint("GET", "/admin/webhooks/{webhook_id}", "One webhook", "admin", group="webhooks",
             responses=(200, 403, 404), result={"webhook": _ref("Webhook")}),
    Endpoint("PUT", "/admin/webhooks/{webhook_id}", "Change a webhook", "admin", True, group="webhooks",
             body=_WEBHOOK_BODY, responses=(200, 400, 403, 404), result={"webhook": _ref("Webhook")}),
    Endpoint("DELETE", "/admin/webhooks/{webhook_id}", "Delete a webhook and its delivery log", "admin", True,
             group="webhooks", responses=(200, 403, 404), result=_DELETED),
    Endpoint("POST", "/admin/webhooks/{webhook_id}/rotate-secret", "Replace a webhook's signing secret", "admin",
             True, group="webhooks", responses=(200, 403, 404), result={"secret": STRING}),
    Endpoint("POST", "/admin/webhooks/{webhook_id}/ping", "Queue a test delivery", "admin", True,
             group="webhooks", responses=(202, 403, 404), result={"delivery": _ref("Delivery")}),
    Endpoint("GET", "/admin/webhooks/{webhook_id}/deliveries", "Delivery log, newest first", "admin",
             group="webhooks", paginated=True, responses=(200, 403, 404),
             result={"deliveries": _list(_ref("Delivery"))}),
    Endpoint("GET", "/admin/webhooks/{webhook_id}/deliveries/{delivery_id}", "One delivery with its payload",
             "admin", group="webhooks", responses=(200, 403, 404), result={"delivery": _ref("Delivery")}),
    Endpoint("POST", "/admin/webhooks/{webhook_id}/deliveries/{delivery_id}/redeliver", "Send a delivery again",
             "admin", True, group="webhooks", responses=(202, 403, 404, 409), result={"delivery": _ref("Delivery")}),
    # Userbot
    Endpoint("GET", "/userbot/me", "The userbot account and profile", "userbot", group="userbot",
             result={"user": OBJECT, "profile": OBJECT, "token": OBJECT}),
    Endpoint("POST", "/userbot/profile", "Update the userbot's profile", "userbot", True, group="userbot",
             body={"real_name": STRING, "bio": STRING, "page_published": BOOLEAN}, responses=(200, 400, 403),
             result={"profile": OBJECT}),
)

SCHEMAS: dict[str, Schema] = {
    "Ok": {"type": "object", "properties": {"ok": {"const": True}}, "required": ["ok"]},
    "Error": {"type": "object", "required": ["ok", "error", "code"], "properties": {
        "ok": {"const": False}, "error": {"type": "string", "description": "Message for people; application errors are translated"},
        "code": {"type": "string", "description": "Stable code for programs"},
        "request_id": STRING,
        "field": STRING, "scope": STRING, "write": BOOLEAN, "revision": INTEGER, "version": INTEGER,
        "locked": {**STRINGS, "description": "Canvas: ids of the locked nodes a change would touch (code locked)"},
        "locked_count": INTEGER}},
    "ItemError": {"type": "object", "properties": {"error": STRING, "code": STRING, "title": STRING,
                                                   "slug": STRING, "username": STRING}},
    "Grant": {"type": "object", "properties": {"read": BOOLEAN, "write": BOOLEAN, "scopes": STRINGS}},
    "PageSummary": {"type": "object", "properties": {
        "id": INTEGER, "title": STRING, "slug": STRING, "category_id": NULLABLE_ID, "is_home": BOOLEAN,
        "revision": INTEGER, "created_at": DATETIME, "last_edited_at": DATETIME,
        "last_edited_by": {"type": ["string", "null"]}}},
    "Page": {"allOf": [_ref("PageSummary"), {"type": "object", "properties": {
        "content": STRING, "pending_deletion": BOOLEAN}}]},
    "HistoryEntry": {"type": "object", "properties": {
        "id": INTEGER, "title": STRING, "edited_by": {"type": ["string", "null"]}, "editor": {"type": ["string", "null"]},
        "edit_message": STRING, "is_revert": BOOLEAN, "created_at": DATETIME, "size": {"type": ["integer", "null"]},
        "content": STRING, "page_slug": STRING}},
    "SearchResult": {"type": "object", "properties": {
        "id": INTEGER, "title": STRING, "slug": STRING, "category_id": NULLABLE_ID, "snippet": STRING,
        "last_edited_at": DATETIME}},
    "Category": {"type": "object", "properties": {
        "id": INTEGER, "name": STRING, "parent_id": NULLABLE_ID, "sort_order": INTEGER, "sequential_nav": BOOLEAN,
        "pages": _list(_ref("PageSummary"))}},
    "User": {"type": "object", "properties": {
        "id": STRING, "username": STRING, "role": STRING, "suspended": BOOLEAN, "approval_status": STRING,
        "api_access_enabled": BOOLEAN, "userbot_enabled": BOOLEAN, "created_at": DATETIME,
        "last_login_at": DATETIME}},
    "Token": {"type": "object", "properties": {
        "id": INTEGER, "name": STRING, "permissions": _ref("Grant"), "last_used_at": DATETIME,
        "expires_at": DATETIME, "active": BOOLEAN, "created_at": DATETIME, "user_id": STRING, "username": STRING}},
    "Attachment": {"type": "object", "properties": {
        "id": INTEGER, "name": STRING, "size": INTEGER, "uploaded_by": {"type": ["string", "null"]},
        "uploader": STRING, "uploaded_at": DATETIME}},
    "Board": {"type": "object", "properties": {
        "id": INTEGER, "title": STRING, "description": STRING, "visibility": STRING, "created_by": STRING,
        "creator_username": STRING, "created_at": DATETIME, "archived_at": DATETIME, "can_write": BOOLEAN,
        "is_owner": BOOLEAN,
        "columns": _list(_ref("Column"))}},
    "Column": {"type": "object", "properties": {"id": INTEGER, "title": STRING, "wip_limit": NULLABLE_ID,
                                                "tickets": IDS}},
    "ChecklistItem": {"type": "object", "properties": {"id": INTEGER, "text": STRING, "done": BOOLEAN}},
    "ArchivedTicket": {"allOf": [_ref("Ticket"), {"type": "object", "properties": {
        "column_title": STRING, "archived_by_username": STRING}}]},
    "AssignedTicket": {"allOf": [_ref("Ticket"), {"type": "object", "properties": {
        "board_title": STRING, "column_title": STRING}}]},
    "Ticket": {"type": "object", "properties": {
        "id": INTEGER, "column_id": INTEGER, "board_id": INTEGER, "title": STRING, "description": STRING,
        "priority": STRING, "due_date": {"type": ["string", "null"], "format": "date"}, "color": STRING,
        "labels": STRINGS, "assignees": _list({"type": "object", "properties": {"id": STRING, "username": STRING}}),
        "attachment_count": INTEGER, "comment_count": INTEGER, "checklist_total": INTEGER,
        "checklist_done": INTEGER, "archived_at": DATETIME, "created_by": {"type": ["string", "null"]},
        "created_by_username": STRING, "created_at": DATETIME}},
    "Comment": {"type": "object", "properties": {
        "id": INTEGER, "ticket_id": INTEGER, "user_id": {"type": ["string", "null"]}, "author": STRING,
        "content": STRING, "created_at": DATETIME, "updated_at": DATETIME}},
    "Canvas": {"type": "object", "properties": {
        "id": INTEGER, "slug": STRING, "title": STRING, "description": STRING, "visibility": STRING,
        "is_archived": BOOLEAN, "version": INTEGER, "creator_id": {"type": ["string", "null"]},
        "creator_username": STRING, "created_at": DATETIME, "updated_at": DATETIME, "can_edit": BOOLEAN,
        "is_owner": BOOLEAN}},
    "Webhook": {"type": "object", "properties": {
        "id": INTEGER, "url": STRING, "description": STRING, "events": STRINGS, "active": BOOLEAN,
        "allow_private_network": BOOLEAN, "consecutive_failures": INTEGER, "failing_since": DATETIME,
        "last_delivery_at": DATETIME, "last_status": {"type": ["string", "null"]},
        "disabled_reason": {"type": ["string", "null"], "enum": ["failures", None],
                            "description": "Why the wiki switched the webhook off; cleared when it is enabled"},
        "disabled_at": DATETIME, "created_at": DATETIME, "updated_at": DATETIME}},
    "Delivery": {"type": "object", "properties": {
        "id": INTEGER, "delivery_id": STRING, "event": STRING,
        "state": {"enum": ["pending", "delivered", "failed"]}, "attempts": INTEGER, "next_attempt_at": DATETIME,
        "response_status": {"type": ["integer", "null"]}, "error": {"type": ["string", "null"]},
        "duration_ms": {"type": ["integer", "null"]}, "created_at": DATETIME, "finished_at": DATETIME,
        "payload": OBJECT}},
}

_STATUS_TEXT = {200: "OK", 201: "Created", 202: "Accepted (scheduled)", 400: "Invalid input",
                401: "Missing or invalid token", 403: "Not allowed", 404: "Not found", 409: "Conflict",
                412: "If-Match does not match the current version", 413: "Body too large",
                422: "Idempotency-Key reused with a different request", 429: "Rate limited",
                500: "Internal error", 503: "API disabled, maintenance or storage unavailable"}
_QUERY_TYPES = {"archived": BOOLEAN, "priority": {"enum": ["low", "medium", "high", "critical"]},
                "due": {"enum": ["overdue", "soon", "week", "none"]}, "limit": INTEGER, "offset": INTEGER, "target_id": INTEGER, "before_days": INTEGER,
                "titles_only": BOOLEAN, "page_action": {"enum": ["uncategorize", "move", "delete"]}}
_STRING_IDS = frozenset({"user_id", "slug"})
_RATE_HEADERS = {
    "X-RateLimit-Limit": {"$ref": "#/components/headers/RateLimitLimit"},
    "X-RateLimit-Remaining": {"$ref": "#/components/headers/RateLimitRemaining"},
    "X-RateLimit-Reset": {"$ref": "#/components/headers/RateLimitReset"},
}


def _parameters(endpoint: Endpoint) -> list[dict[str, Any]]:
    params: list[dict[str, Any]] = [
        {"name": name, "in": "path", "required": True,
         "schema": STRING if name in _STRING_IDS else INTEGER}
        for name in (part.strip("{}") for part in endpoint.path.split("/") if part.startswith("{"))
    ]
    query = (*endpoint.params, *(("limit", "offset") if endpoint.paginated else ()))
    params += [{"name": name, "in": "query", "required": False, "schema": _QUERY_TYPES.get(name, STRING)}
               for name in query]
    if endpoint.scope and endpoint.method == "POST":
        params.append({"$ref": "#/components/parameters/IdempotencyKey"})
    if endpoint.if_match:
        params.append({"name": "If-Match", "in": "header", "required": False, "schema": STRING,
                       "description": f'The ETag of the version you started from, e.g. "{endpoint.if_match}3".'})
    return params


def _success(endpoint: Endpoint, code: int) -> dict[str, Any]:
    if endpoint.download:
        return {"description": "The file", "content": {"application/octet-stream": {
            "schema": {"type": "string", "format": "binary"}}}}
    properties = dict(endpoint.result)
    if endpoint.paginated:
        properties.update(limit=INTEGER, offset=INTEGER, next_offset={"type": ["integer", "null"]})
    schema: Schema = _ref("Ok")
    if properties:
        schema = {"allOf": [_ref("Ok"), {"type": "object", "properties": properties}]}
    answer: dict[str, Any] = {"description": _STATUS_TEXT[code], "content": {"application/json": {"schema": schema}}}
    if endpoint.if_match:
        answer["headers"] = {"ETag": {"schema": STRING, "description": "The version, for If-Match"}}
    return answer


def _operation(endpoint: Endpoint) -> dict[str, Any]:
    codes = {*endpoint.responses, 500, 503}
    if endpoint.scope:
        codes |= {401, 403, 429, 503}
        if endpoint.feature:
            codes.add(404)
        if endpoint.method == "POST":
            codes |= {409, 422}
    responses: dict[str, Any] = {}
    for code in sorted(codes):
        if code < 400:
            responses[str(code)] = _success(endpoint, code)
        else:
            responses[str(code)] = {"description": _STATUS_TEXT[code],
                                    "content": {"application/json": {"schema": _ref("Error")}}}
        if endpoint.scope:
            responses[str(code)]["headers"] = {**responses[str(code)].get("headers", {}), **_RATE_HEADERS}
    if "429" in responses:
        responses["429"]["headers"]["Retry-After"] = {"schema": INTEGER}
    operation: dict[str, Any] = {
        "operationId": f"{endpoint.method.lower()}_{endpoint.path.strip('/').replace('/', '_')}"
                       .replace("{", "").replace("}", "").replace("-", "_").replace(".", "_"),
        "summary": endpoint.summary,
        "tags": [endpoint.group],
        "parameters": _parameters(endpoint),
        "responses": responses,
    }
    if endpoint.scope:
        operation["security"] = [{"bearer": []}]
        operation["x-scope"] = endpoint.scope
        operation["x-write"] = endpoint.write
        if endpoint.feature:
            operation["x-feature"] = endpoint.feature
    else:
        operation["security"] = []
    if endpoint.upload:
        operation["requestBody"] = {"required": True, "content": {"multipart/form-data": {"schema": {
            "type": "object", "required": ["file"],
            "properties": {"file": {"type": "string", "format": "binary"}}}}}}
    elif endpoint.body:
        body: Schema = {"type": "object", "properties": dict(endpoint.body)}
        if endpoint.required:
            body["required"] = list(endpoint.required)
        operation["requestBody"] = {"required": True, "content": {"application/json": {"schema": body}}}
    return operation


def spec(server_url: str) -> dict[str, Any]:
    paths: dict[str, dict[str, Any]] = {}
    for endpoint in ENDPOINTS:
        paths.setdefault(endpoint.path, {})[endpoint.method.lower()] = _operation(endpoint)
    return {
        "openapi": "3.1.0",
        "info": {"title": "BananaWiki API", "version": API_VERSION},
        "servers": [{"url": server_url}],
        "components": {
            "securitySchemes": {"bearer": {"type": "http", "scheme": "bearer"}},
            "schemas": SCHEMAS,
            "parameters": {"IdempotencyKey": {
                "name": "Idempotency-Key", "in": "header", "required": False,
                "schema": {"type": "string", "maxLength": 255},
                "description": "Makes a retried POST safe: the first answer is replayed for 24 hours."}},
            "headers": {
                "RateLimitLimit": {"schema": INTEGER, "description": "Requests allowed per minute"},
                "RateLimitRemaining": {"schema": INTEGER, "description": "Requests left in the current minute"},
                "RateLimitReset": {"schema": INTEGER, "description": "Seconds until a request is freed"},
            },
        },
        "paths": paths,
    }
