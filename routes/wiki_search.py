"""Wiki search routes."""

import html
import re
import shlex
from flask import (
    render_template, request, url_for,
)
import db
from helpers import (
    login_required, get_current_user,
    user_can_view_page, rate_limit,
    user_can_view_category,
)

from .wiki_common import (
    _SEARCH_FIELDS,
    _SEARCH_SCOPES,
    _SEARCH_TYPES,
)

def _parse_advanced_search_query(raw_query):
    """Parse a small Google-like query syntax for the advanced search page."""
    parsed = {
        "terms": [],
        "negative_terms": [],
        "fields": {key: [] for key in _SEARCH_FIELDS},
    }
    try:
        tokens = shlex.split(raw_query or "")
    except ValueError:
        tokens = (raw_query or "").split()
    for token in tokens:
        token = token.strip()
        if not token:
            continue
        negative = token.startswith("-") and len(token) > 1
        if negative:
            token = token[1:]
        field = None
        value = token
        if ":" in token:
            maybe_field, maybe_value = token.split(":", 1)
            maybe_field = maybe_field.lower().strip()
            if maybe_field in _SEARCH_FIELDS and maybe_value.strip():
                field = maybe_field
                value = maybe_value.strip()
        if negative:
            parsed["negative_terms"].append(value)
        elif field:
            parsed["fields"][field].append(value)
        else:
            parsed["terms"].append(value)
    return parsed


def _search_contains(haystack, needle):
    """Return whether *needle* appears in *haystack* case-insensitively."""
    return str(needle or "").casefold() in str(haystack or "").casefold()


def _search_any(haystack, terms):
    """Return whether any search term appears in *haystack*."""
    return any(_search_contains(haystack, term) for term in terms)


def _search_all(haystack, terms):
    """Return whether all search terms appear in *haystack*."""
    return all(_search_contains(haystack, term) for term in terms)


def _highlight_text(value, terms):
    """HTML-escape text and wrap matching search terms in ``mark`` tags."""
    text = str(value or "")
    usable_terms = sorted(
        {term for term in terms if term},
        key=len,
        reverse=True,
    )
    if not usable_terms:
        return html.escape(text)
    pattern = re.compile(
        "(" + "|".join(re.escape(term) for term in usable_terms) + ")",
        re.IGNORECASE,
    )
    pieces = []
    last = 0
    for match in pattern.finditer(text):
        pieces.append(html.escape(text[last:match.start()]))
        pieces.append("<mark>" + html.escape(match.group(0)) + "</mark>")
        last = match.end()
    pieces.append(html.escape(text[last:]))
    return "".join(pieces)


def _build_content_snippet(content, terms):
    """Build a highlighted content preview around the first matching term."""
    content = re.sub(r"\s+", " ", str(content or "")).strip()
    if not content:
        return ""
    lowered = content.casefold()
    hit_at = None
    for term in terms:
        if not term:
            continue
        idx = lowered.find(term.casefold())
        if idx >= 0:
            hit_at = idx if hit_at is None else min(hit_at, idx)
    if hit_at is None:
        snippet = content[:260]
        return _highlight_text(snippet + ("..." if len(content) > 260 else ""), terms)
    start = max(0, hit_at - 90)
    end = min(len(content), hit_at + 190)
    snippet = content[start:end].strip()
    if start > 0:
        snippet = "..." + snippet
    if end < len(content):
        snippet += "..."
    return _highlight_text(snippet, terms)


def _category_path(category, category_by_id):
    """Return a slash-separated ancestor path for a category row."""
    names = []
    seen = set()
    current = category
    while current and current.get("id") not in seen:
        seen.add(current.get("id"))
        names.append(current.get("name") or "")
        parent_id = current.get("parent_id")
        current = category_by_id.get(parent_id) if parent_id is not None else None
    return " / ".join(reversed([name for name in names if name]))


def _advanced_search_results(raw_query, scope, result_type, sort):
    """Return advanced-search result dictionaries for pages and categories."""
    user = get_current_user()
    include_deindexed = bool(user and db.has_permission(user, "page.view_deindexed"))
    parsed = _parse_advanced_search_query(raw_query)
    highlight_terms = (
        parsed["terms"]
        + parsed["fields"]["title"]
        + parsed["fields"]["content"]
        + parsed["fields"]["category"]
        + parsed["fields"]["slug"]
    )
    has_query = any(parsed["terms"]) or any(parsed["fields"][field] for field in _SEARCH_FIELDS)
    if not has_query:
        return {"pages": [], "categories": [], "total": 0, "parsed": parsed, "highlight_terms": []}

    requested_types = set()
    for value in parsed["fields"]["type"]:
        value = value.casefold()
        if value in ("page", "pages"):
            requested_types.add("page")
        elif value in ("category", "categories", "folder", "folders"):
            requested_types.add("category")
    if result_type != "all":
        requested_types = {result_type}
    if not requested_types:
        requested_types = {"page", "category"}

    categories_raw = [dict(row) for row in db.list_categories()]
    category_by_id = {row["id"]: row for row in categories_raw}
    category_names = {
        row["id"]: _category_path(row, category_by_id) or row["name"]
        for row in categories_raw
    }

    page_results = []
    if "page" in requested_types:
        for row in db.list_searchable_pages(include_deindexed=include_deindexed):
            page = dict(row)
            if not user_can_view_page(user, page):
                continue
            title = page.get("title") or ""
            content = page.get("content") or ""
            slug = page.get("slug") or ""
            category_name = category_names.get(page.get("category_id"), page.get("category_name") or "")
            haystacks = {
                "title": title,
                "content": content,
                "category": category_name,
                "slug": slug,
            }
            if parsed["negative_terms"] and _search_any(" ".join(haystacks.values()), parsed["negative_terms"]):
                continue
            if parsed["fields"]["title"] and not _search_all(title, parsed["fields"]["title"]):
                continue
            if parsed["fields"]["content"] and not _search_all(content, parsed["fields"]["content"]):
                continue
            if parsed["fields"]["category"] and not _search_all(category_name, parsed["fields"]["category"]):
                continue
            if parsed["fields"]["slug"] and not _search_all(slug, parsed["fields"]["slug"]):
                continue
            if parsed["terms"]:
                if scope == "title":
                    matched_general = _search_all(title, parsed["terms"])
                elif scope == "content":
                    matched_general = _search_all(content, parsed["terms"])
                elif scope == "category":
                    matched_general = _search_all(category_name, parsed["terms"])
                else:
                    matched_general = all(
                        _search_any(" ".join(haystacks.values()), [term])
                        for term in parsed["terms"]
                    )
                if not matched_general:
                    continue
            score = 0
            for term in highlight_terms:
                if _search_contains(title, term):
                    score += 8
                if _search_contains(slug, term):
                    score += 4
                if _search_contains(category_name, term):
                    score += 3
                if _search_contains(content, term):
                    score += 1
            page_results.append({
                "kind": "page",
                "title": title,
                "slug": slug,
                "url": url_for("view_page", slug=slug),
                "category_name": category_name,
                "last_edited_at": page.get("last_edited_at"),
                "is_home": bool(page.get("is_home")),
                "is_deindexed": bool(page.get("is_deindexed")),
                "score": score,
                "title_html": _highlight_text(title, highlight_terms),
                "slug_html": _highlight_text(slug, highlight_terms),
                "category_html": _highlight_text(category_name, highlight_terms),
                "snippet_html": _build_content_snippet(content, highlight_terms),
            })

    category_results = []
    if "category" in requested_types:
        for category in categories_raw:
            if not user_can_view_category(user, category["id"]):
                continue
            path = category_names.get(category["id"], category["name"])
            haystack = path
            if parsed["negative_terms"] and _search_any(haystack, parsed["negative_terms"]):
                continue
            if parsed["fields"]["category"] and not _search_all(path, parsed["fields"]["category"]):
                continue
            if parsed["fields"]["title"] or parsed["fields"]["content"] or parsed["fields"]["slug"]:
                continue
            if parsed["terms"] and not _search_all(path, parsed["terms"]):
                continue
            score = sum(6 for term in highlight_terms if _search_contains(path, term))
            category_results.append({
                "kind": "category",
                "id": category["id"],
                "title": category["name"],
                "path": path,
                "url": url_for("home") + f"#category-{category['id']}",
                "score": score,
                "title_html": _highlight_text(category["name"], highlight_terms),
                "path_html": _highlight_text(path, highlight_terms),
            })

    if sort == "title":
        page_results.sort(key=lambda item: item["title"].casefold())
        category_results.sort(key=lambda item: item["title"].casefold())
    elif sort == "recent":
        page_results.sort(key=lambda item: item.get("last_edited_at") or "", reverse=True)
        category_results.sort(key=lambda item: item["title"].casefold())
    else:
        page_results.sort(key=lambda item: (-item["score"], item["title"].casefold()))
        category_results.sort(key=lambda item: (-item["score"], item["title"].casefold()))

    page_results = page_results[:100]
    category_results = category_results[:50]
    return {
        "pages": page_results,
        "categories": category_results,
        "total": len(page_results) + len(category_results),
        "parsed": parsed,
        "highlight_terms": highlight_terms,
    }


def register_wiki_search_routes(app):
    """Register search endpoints on the application."""

    @app.route("/search")
    @login_required
    @rate_limit(60, 60)
    def search_page():
        """Dedicated advanced wiki search page."""
        query = (request.args.get("q") or "").strip()[:300]
        scope = (request.args.get("scope") or "all").strip().lower()
        result_type = (request.args.get("type") or "all").strip().lower()
        sort = (request.args.get("sort") or "relevance").strip().lower()
        if scope not in _SEARCH_SCOPES:
            scope = "all"
        if result_type not in _SEARCH_TYPES:
            result_type = "all"
        if sort not in {"relevance", "title", "recent"}:
            sort = "relevance"
        results = _advanced_search_results(query, scope, result_type, sort)
        return render_template(
            "wiki/search.html",
            page=None,
            query=query,
            scope=scope,
            result_type=result_type,
            sort=sort,
            results=results,
        )
