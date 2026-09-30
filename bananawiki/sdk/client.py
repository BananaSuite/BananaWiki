"""A small standard-library client for the BananaWiki REST API (``/api/v1``).

It has no dependencies, so it can be copied into a script on another machine::

    from bananawiki.sdk.client import WikiClient

    wiki = WikiClient("https://wiki.example.org", "YOUR_TOKEN")
    page = wiki.page("home")
    wiki.update_page("home", content=page["content"] + "\\n\\nHello", revision=page["revision"])
    for page in wiki.iter_pages():
        print(page["slug"])

Every endpoint is reachable with :meth:`WikiClient.request`; the helpers
cover the common ones. Errors raise :class:`ApiError` with the API's stable
``code``. Redirects are never followed and environment proxies are ignored,
so the token is only ever sent to the address you gave.

:func:`verify_webhook` checks the signature of a webhook delivery.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid
from collections.abc import Iterator
from typing import Any
from urllib import error as _error
from urllib import request as _request
from urllib.parse import quote, urlencode, urlparse

MAX_RESPONSE = 8 * 1024 * 1024
WEBHOOK_TOLERANCE_SECONDS = 300
_KEEP = object()


class ApiError(Exception):
    """A refused call: ``status`` is the HTTP status, ``code`` the API's error code, ``data`` the whole answer."""

    def __init__(self, status: int, code: str, message: str, data: dict[str, Any] | None = None):
        super().__init__(f"{status} {code}: {message}")
        self.status = status
        self.code = code
        self.message = message
        self.data = data or {}


class _NoRedirect(_request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, D401
        return None


def _segment(value: Any) -> str:
    return quote(str(value), safe="")


class WikiClient:
    def __init__(self, base_url: str, token: str, *, timeout: float = 15.0):
        parsed = urlparse(base_url)
        if (parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password
                or parsed.query or parsed.fragment):
            raise ValueError("base_url must be an absolute http(s) URL")
        self.base_url = base_url.rstrip("/")
        self.api_key = token
        self.timeout = timeout
        self.last_headers: dict[str, str] = {}
        self._opener = _request.build_opener(_request.ProxyHandler({}), _NoRedirect())

    # ── Transport ─────────────────────────────────────────────────────────────

    def request(self, method: str, path: str, payload: Any = None, *, query: dict[str, Any] | None = None,
                idempotency_key: str | None = None, if_match: str | None = None) -> dict[str, Any]:
        """Call ``/api/v1<path>`` and return the decoded JSON answer.

        *idempotency_key* makes a POST safe to retry; *if_match* (an ETag such
        as ``'"r3"'``) refuses an update when the object changed (412).
        The answer's headers (rate limit, ETag) are kept in :attr:`last_headers`.
        """
        headers = {"Authorization": f"Bearer {self.api_key}", "Accept": "application/json"}
        body = None
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        if if_match is not None:
            headers["If-Match"] = if_match
        url = f"{self.base_url}/api/v1{path}"
        if query:
            url += "?" + urlencode({key: value for key, value in query.items() if value is not None})
        req = _request.Request(url, method=method, headers=headers, data=body)  # noqa: S310 - scheme checked
        try:
            with self._opener.open(req, timeout=self.timeout) as response:  # noqa: S310 - checked scheme
                self.last_headers = {name.lower(): value for name, value in response.headers.items()}
                return self._decode(response.read(MAX_RESPONSE + 1))
        except _error.HTTPError as failure:
            self.last_headers = {name.lower(): value for name, value in (failure.headers or {}).items()}
            data = self._decode(failure.read(MAX_RESPONSE + 1), strict=False)
            raise ApiError(failure.code, str(data.get("code", "http_error")),
                           str(data.get("error", failure.reason)), data) from None

    @staticmethod
    def _decode(raw: bytes, *, strict: bool = True) -> dict[str, Any]:
        if len(raw) > MAX_RESPONSE:
            raise ValueError("The response exceeds 8 MiB")
        try:
            data = json.loads(raw.decode("utf-8"))
        except ValueError:
            if strict:
                raise
            return {}
        return data if isinstance(data, dict) else {}

    def iter_all(self, path: str, key: str, *, page_size: int = 100, **query: Any) -> Iterator[dict[str, Any]]:
        """Every item of a paginated listing, following ``next_offset``."""
        offset: int | None = 0
        while offset is not None:
            answer = self.request("GET", path, query={**query, "limit": page_size, "offset": offset})
            yield from answer.get(key, [])
            offset = answer.get("next_offset")

    # ── Pages ─────────────────────────────────────────────────────────────────

    def page(self, slug: str) -> dict[str, Any]:
        """A page the account may read (needs the ``pages`` scope)."""
        return self.request("GET", "/pages/" + _segment(slug))["page"]

    def iter_pages(self, **query: Any) -> Iterator[dict[str, Any]]:
        return self.iter_all("/pages", "pages", **query)

    def search(self, text: str, *, titles_only: bool = False, limit: int = 20) -> list[dict[str, Any]]:
        return self.request("GET", "/search", query={"q": text, "titles_only": "1" if titles_only else None,
                                                     "limit": limit})["results"]

    def create_page(self, title: str, content: str = "", *, slug: str | None = None,
                    category_id: int | None = None, idempotency_key: str | None = None) -> dict[str, Any]:
        """Create a page; a retry with the same *idempotency_key* (one is made up if omitted) never duplicates it."""
        payload: dict[str, Any] = {"title": title, "content": content}
        if slug is not None:
            payload["slug"] = slug
        if category_id is not None:
            payload["category_id"] = category_id
        return self.request("POST", "/pages", payload, idempotency_key=idempotency_key or str(uuid.uuid4()))["page"]

    def update_page(self, slug: str, *, title: str | None = None, content: str | None = None,
                    revision: int | None = None, edit_message: str | None = None, **fields: Any) -> dict[str, Any]:
        """Change a page; with *revision* the save is refused (412) if someone edited it since."""
        payload = {name: value for name, value in (("title", title), ("content", content),
                                                   ("edit_message", edit_message)) if value is not None}
        payload.update(fields)
        return self.request("PUT", "/pages/" + _segment(slug), payload,
                            if_match=None if revision is None else f'"r{int(revision)}"')["page"]

    def delete_page(self, slug: str) -> dict[str, Any]:
        return self.request("DELETE", "/pages/" + _segment(slug))

    # ── Kanban ────────────────────────────────────────────────────────────────

    def boards(self, *, archived: bool = False) -> list[dict[str, Any]]:
        """Active boards, or with *archived* only the archived ones."""
        return list(self.iter_all("/kanban/boards", "boards", archived=1 if archived else 0))

    def board(self, board_id: int, **filters: str) -> dict[str, Any]:
        """The board (with ``columns``) and its active ``tickets``; *filters*: ``q``, ``who``, ``label``,
        ``priority``, ``due``."""
        return self.request("GET", f"/kanban/boards/{int(board_id)}", query=filters or None)

    def archive_board(self, board_id: int) -> dict[str, Any]:
        return self.request("POST", f"/kanban/boards/{int(board_id)}/archive")["board"]

    def restore_board(self, board_id: int) -> dict[str, Any]:
        return self.request("POST", f"/kanban/boards/{int(board_id)}/restore")["board"]

    def archived_tickets(self, board_id: int) -> list[dict[str, Any]]:
        return list(self.iter_all(f"/kanban/boards/{int(board_id)}/archived-tickets", "tickets"))

    def archive_ticket(self, ticket_id: int) -> dict[str, Any]:
        return self.request("POST", f"/kanban/tickets/{int(ticket_id)}/archive")["ticket"]

    def restore_ticket(self, ticket_id: int) -> dict[str, Any]:
        """Put an archived ticket back at the end of its column."""
        return self.request("POST", f"/kanban/tickets/{int(ticket_id)}/restore")["ticket"]

    def archive_tickets(self, board_id: int, ticket_ids: list[int]) -> int:
        return self.request("POST", f"/kanban/boards/{int(board_id)}/tickets/archive",
                            {"ids": [int(item) for item in ticket_ids]})["archived"]

    def restore_tickets(self, board_id: int, ticket_ids: list[int]) -> int:
        return self.request("POST", f"/kanban/boards/{int(board_id)}/tickets/restore",
                            {"ids": [int(item) for item in ticket_ids]})["restored"]

    def archive_column(self, column_id: int) -> int:
        """Archive every active ticket of the column; returns how many."""
        return self.request("POST", f"/kanban/columns/{int(column_id)}/archive")["archived"]

    def create_ticket(self, column_id: int, title: str, **fields: Any) -> dict[str, Any]:
        return self.request("POST", f"/kanban/columns/{int(column_id)}/tickets", {"title": title, **fields},
                            idempotency_key=str(uuid.uuid4()))["ticket"]

    def update_ticket(self, ticket_id: int, **fields: Any) -> dict[str, Any]:
        return self.request("PUT", f"/kanban/tickets/{int(ticket_id)}", fields)["ticket"]

    def move_ticket(self, ticket_id: int, column_id: int, position: int | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"column_id": column_id}
        if position is not None:
            payload["position"] = position
        return self.request("POST", f"/kanban/tickets/{int(ticket_id)}/move", payload)["ticket"]

    def comment_ticket(self, ticket_id: int, content: str) -> dict[str, Any]:
        return self.request("POST", f"/kanban/tickets/{int(ticket_id)}/comments", {"content": content},
                            idempotency_key=str(uuid.uuid4()))["comment"]

    def my_tickets(self, **filters: str) -> list[dict[str, Any]]:
        """Tickets assigned to you (with ``board_title`` and ``column_title``), earliest due date first;
        *filters*: ``q``, ``label``, ``priority``, ``due``."""
        return list(self.iter_all("/kanban/my-tickets", "tickets", **filters))

    def update_column(self, column_id: int, *, title: str | None = None, wip_limit: Any = _KEEP) -> dict[str, Any]:
        """Rename a column and/or set its WIP limit (``None`` removes the limit; leave it out to keep it)."""
        payload: dict[str, Any] = {}
        if title is not None:
            payload["title"] = title
        if wip_limit is not _KEEP:
            payload["wip_limit"] = wip_limit
        return self.request("PUT", f"/kanban/columns/{int(column_id)}", payload)["column"]

    def checklist(self, ticket_id: int) -> list[dict[str, Any]]:
        return self.request("GET", f"/kanban/tickets/{int(ticket_id)}/checklist")["checklist"]

    def add_checklist_item(self, ticket_id: int, text: str) -> list[dict[str, Any]]:
        """Append an item; returns the whole checklist."""
        return self.request("POST", f"/kanban/tickets/{int(ticket_id)}/checklist", {"text": text},
                            idempotency_key=str(uuid.uuid4()))["checklist"]

    def update_checklist_item(self, item_id: int, *, text: str | None = None,
                              done: bool | None = None) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {}
        if text is not None:
            payload["text"] = text
        if done is not None:
            payload["done"] = done
        return self.request("PUT", f"/kanban/checklist/{int(item_id)}", payload)["checklist"]

    def delete_checklist_item(self, item_id: int) -> list[dict[str, Any]]:
        return self.request("DELETE", f"/kanban/checklist/{int(item_id)}")["checklist"]

    def reorder_checklist(self, ticket_id: int, order: list[int]) -> list[dict[str, Any]]:
        return self.request("POST", f"/kanban/tickets/{int(ticket_id)}/checklist/reorder",
                            {"order": [int(item) for item in order]})["checklist"]

    # ── Webhooks (administrators) ─────────────────────────────────────────────

    def webhooks(self) -> list[dict[str, Any]]:
        """Configured webhooks; ``disabled_reason`` tells whether the wiki switched one off after failures."""
        return self.request("GET", "/admin/webhooks")["webhooks"]

    def update_webhook(self, webhook_id: int, **fields: Any) -> dict[str, Any]:
        """Change ``url``, ``events``, ``description``, ``active``…; ``active=True`` also resets the failure count."""
        return self.request("PUT", f"/admin/webhooks/{int(webhook_id)}", fields)["webhook"]

    # ── Canvas ────────────────────────────────────────────────────────────────

    def canvases(self) -> list[dict[str, Any]]:
        return list(self.iter_all("/canvas", "canvases"))

    def canvas(self, slug: str) -> dict[str, Any]:
        """The canvas (``canvas``, with its ``version``) and its document (``data``)."""
        return self.request("GET", "/canvas/" + _segment(slug))

    def save_canvas(self, slug: str, data: dict[str, Any], *, version: int | None = None) -> dict[str, Any]:
        """Replace the document; with *version* the save is refused (412) if the canvas changed since."""
        return self.request("PUT", f"/canvas/{_segment(slug)}/document", {"data": data},
                            if_match=None if version is None else f'"v{int(version)}"')["canvas"]


def verify_webhook(secret: str, body: bytes, timestamp: str, signature: str, *,
                   tolerance: int = WEBHOOK_TOLERANCE_SECONDS, now: float | None = None) -> bool:
    """Whether a webhook delivery is authentic and recent.

    Pass the raw request body and the ``X-BananaWiki-Timestamp`` and
    ``X-BananaWiki-Signature`` headers.
    """
    try:
        sent = int(timestamp)
    except (TypeError, ValueError):
        return False
    if abs((time.time() if now is None else now) - sent) > tolerance:
        return False
    expected = "sha256=" + hmac.new(secret.encode("utf-8"), timestamp.encode("ascii", "replace") + b"." + body,
                                    hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature or "")
