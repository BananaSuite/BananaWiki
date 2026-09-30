"""Endpoints that need no token: service status and the OpenAPI description."""

from __future__ import annotations

from flask import jsonify, request

from .. import openapi, tokens
from . import bp, open_endpoint


@bp.get("/status")
@open_endpoint
def status():
    return jsonify({"ok": True, "api_enabled": tokens.service_settings()["enabled"],
                    "version": openapi.API_VERSION, "service": "BananaWiki API"})


@bp.get("/openapi.json")
@open_endpoint
def openapi_description():
    return jsonify(openapi.spec(request.url_root.rstrip("/") + "/api/v1"))
