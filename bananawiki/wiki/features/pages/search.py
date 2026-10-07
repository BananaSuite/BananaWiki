"""The search page: a small query language over the full-text index.

Syntax (as in 1.4): plain words must all match; ``"exact phrase"``;
``title:``, ``content:``, ``category:``, ``slug:`` and ``type:page|category``
fields; ``-word`` excludes. Results are filtered by visibility in SQL
(:func:`service.visible_filter`), so pages the reader cannot open are never
loaded, and snippets are built by SQLite.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from typing import Any

from markupsafe import Markup, escape

from ... import auth
from ...db import db
from . import categories, service

FIELDS = ("title", "content", "category", "slug", "type")
SCOPES = ("all", "title", "content", "category")
TYPES = ("all", "page", "category")
SORTS = ("relevance", "title", "recent")
PER_PAGE = 20
MAX_TERMS = 12
_WORD = re.compile(r"\w+", re.UNICODE)


@dataclass
class Query:
    terms: list[str] = field(default_factory=list)
    excluded: list[str] = field(default_factory=list)
    fields: dict[str, list[str]] = field(default_factory=lambda: {name: [] for name in FIELDS})

    @property
    def empty(self) -> bool:
        return not self.terms and not any(self.fields[name] for name in FIELDS if name != "type")

    @property
    def highlight(self) -> list[str]:
        return [*self.terms, *self.fields["title"], *self.fields["content"]]


def parse(raw: str) -> Query:
    query = Query()
    raw = (raw or "")[:300]
    try:
        tokens = shlex.split(raw)
    except ValueError:
        tokens = raw.split()
    for token in tokens[: MAX_TERMS * 2]:
        token = token.strip()
        negative = token.startswith("-") and len(token) > 1
        if negative:
            token = token[1:]
        name, _, value = token.partition(":")
        if value.strip() and name.lower() in FIELDS and not negative:
            query.fields[name.lower()].append(value.strip())
        elif negative:
            query.excluded.append(token)
        elif token:
            query.terms.append(token)
    return query


def _phrase(term: str) -> str | None:
    words = _WORD.findall(term)
    return '"' + " ".join(words) + '"*' if words else None


def _like(term: str) -> str:
    return "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _requested_types(query: Query, result_type: str) -> set[str]:
    if result_type != "all":
        return {result_type}
    wanted = set()
    for value in query.fields["type"]:
        value = value.casefold()
        if value in ("page", "pages"):
            wanted.add("page")
        elif value in ("category", "categories", "folder", "folders"):
            wanted.add("category")
    return wanted or {"page", "category"}


def _readable_paths(user: dict[str, Any] | None) -> dict[int, str]:
    readable = {c["id"] for c in categories.all_categories() if auth.can_read_category(c["id"], user)}
    return {cid: path for cid, path in categories.paths(lambda cid: cid in readable).items() if cid in readable}


def _category_ids(paths: dict[int, str], terms: list[str]) -> list[int]:
    return [cid for cid, path in paths.items() if all(term.casefold() in path.casefold() for term in terms)]


def search_pages(query: Query, *, user: dict[str, Any] | None, scope: str = "all", sort: str = "relevance",
                 page: int = 1, paths: dict[int, str] | None = None) -> tuple[list[dict[str, Any]], int]:
    """Matching visible pages for one result page, and the total count."""
    paths = _readable_paths(user) if paths is None else paths
    where, params = service.visible_filter(user)
    clauses, values = [f"({where})"], list(params)
    category_terms = list(query.fields["category"])
    general = list(query.terms)
    if scope == "category":
        category_terms += general
        general = []
    if category_terms:
        ids = _category_ids(paths, category_terms)
        if not ids:
            return [], 0
        clauses.append(f"p.category_id IN ({','.join('?' for _ in ids)})")
        values.extend(ids)
    for term in query.fields["slug"]:
        clauses.append("p.slug LIKE ? ESCAPE '\\'")
        values.append(_like(term))

    general_columns = {"all": "{title content}", "title": "{title}", "content": "{content}"}.get(scope, "{title content}")
    matches = [(general_columns, t) for t in general]
    matches += [("{title}", t) for t in query.fields["title"]] + [("{content}", t) for t in query.fields["content"]]
    fts = service.fts_available() and any(_phrase(t) for _c, t in matches)
    join, score, snippet = "", "", "substr(p.content, 1, 240)"
    if fts:
        positive = [f"{columns} : {_phrase(term)}" for columns, term in matches[:MAX_TERMS] if _phrase(term)]
        expression = " AND ".join(positive)
        for term in query.excluded[:MAX_TERMS]:
            if _phrase(term):
                expression = f"({expression}) NOT {{title content}} : {_phrase(term)}"
        join = "JOIN pages_fts ON pages_fts.rowid = p.id"
        clauses.append("pages_fts MATCH ?")
        values.append(expression)
        score = "bm25(pages_fts, 8.0, 1.0)"
        snippet = "snippet(pages_fts, 1, char(2), char(3), ' … ', 16)"
    else:
        column_sql = {"{title content}": "(p.title || ' ' || p.content)", "{title}": "p.title", "{content}": "p.content"}
        for columns, term in matches:
            clauses.append(f"{column_sql[columns]} LIKE ? ESCAPE '\\'")
            values.append(_like(term))
        for term in query.excluded:
            clauses.append("(p.title || ' ' || p.content) NOT LIKE ? ESCAPE '\\'")
            values.append(_like(term))
    condition = " AND ".join(clauses)
    total = int(db.scalar(f"SELECT COUNT(*) FROM pages p {join} WHERE {condition}", values, default=0))
    order = {
        "title": "lower(p.title), p.id",
        "recent": "COALESCE(p.last_edited_at, p.created_at) DESC, p.id DESC",
    }.get(sort, f"{score}, lower(p.title), p.id" if fts else "lower(p.title), p.id")
    rows = db.all(
        f"SELECT p.id, p.title, p.slug, p.category_id, p.is_home, p.is_deindexed, p.last_edited_at, "
        f"{snippet} AS snippet FROM pages p {join} WHERE {condition} ORDER BY {order} LIMIT ? OFFSET ?",
        [*values, PER_PAGE, (max(1, page) - 1) * PER_PAGE],
    )
    for row in rows:
        row["category_path"] = paths.get(row["category_id"], "") if row["category_id"] else ""
        row["snippet_html"] = snippet_html(row["snippet"] or "", query.highlight, marked=fts)
    return rows, total


def search_categories(query: Query, *, user: dict[str, Any] | None,
                      paths: dict[int, str] | None = None) -> list[dict[str, Any]]:
    if any(query.fields[name] for name in ("title", "content", "slug")):
        return []
    terms = [*query.terms, *query.fields["category"]]
    if not terms:
        return []
    paths = _readable_paths(user) if paths is None else paths
    excluded = [term.casefold() for term in query.excluded]
    found = []
    for cid in _category_ids(paths, terms):
        path = paths[cid]
        if any(term in path.casefold() for term in excluded) or not categories.listed(cid, user):
            continue
        found.append({"id": cid, "name": path.rsplit(" / ", 1)[-1], "path": path,
                      "path_html": highlight(path, terms)})
    return sorted(found, key=lambda item: item["path"].casefold())[:50]


def run(raw: str, *, user: dict[str, Any] | None, scope: str, result_type: str, sort: str,
        page: int) -> dict[str, Any]:
    query = parse(raw)
    if query.empty:
        return {"pages": [], "categories": [], "total": 0, "query": query}
    wanted = _requested_types(query, result_type)
    paths = _readable_paths(user)
    pages, total = search_pages(query, user=user, scope=scope, sort=sort, page=page, paths=paths) \
        if "page" in wanted else ([], 0)
    found_categories = search_categories(query, user=user, paths=paths) if "category" in wanted and page == 1 else []
    return {"pages": pages, "categories": found_categories, "total": total, "query": query}


def highlight(text: str, terms: list[str]) -> Markup:
    """Escape *text* and wrap occurrences of *terms* in ``<mark>``."""
    usable = sorted({t for t in terms if t.strip()}, key=len, reverse=True)
    if not usable:
        return Markup(escape(text))
    pattern = re.compile("|".join(re.escape(t) for t in usable), re.IGNORECASE)
    parts, last = [], 0
    for match in pattern.finditer(text):
        parts.append(str(escape(text[last:match.start()])))
        parts.append(f"<mark>{escape(match.group(0))}</mark>")
        last = match.end()
    parts.append(str(escape(text[last:])))
    return Markup("".join(parts))


_MARKDOWN_NOISE = re.compile(r"[#*_`>|~]+|!?\[([^\]]*)\]\([^)]*\)")


def snippet_html(snippet: str, terms: list[str], *, marked: bool) -> Markup:
    """Safe HTML for a result snippet.

    SQLite marks FTS matches with the control characters \\x02 and \\x03; the
    text is escaped first and only then are the markers turned into ``<mark>``.
    """
    snippet = _MARKDOWN_NOISE.sub(lambda m: m.group(1) or " ", snippet.replace("\r", ""))
    snippet = " ".join(snippet.split())
    if not marked:
        return highlight(snippet, terms)
    escaped = str(escape(snippet))
    opened = 0
    out = []
    for char in escaped:
        if char == "\x02" and not opened:
            out.append("<mark>")
            opened = 1
        elif char == "\x03" and opened:
            out.append("</mark>")
            opened = 0
        elif char not in "\x02\x03":
            out.append(char)
    if opened:
        out.append("</mark>")
    return Markup("".join(out))
