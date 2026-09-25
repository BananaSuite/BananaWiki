"""Serialized, atomic schema initialization and version upgrades."""

from .. import config
from sqlite_runtime import serialized_schema
from sqlite_migrations import apply_migrations

from ._connection import get_hosting_db_context
from .migrations.legacy import _upgrade_legacy
from .migrations.api_tokens import upgrade as _api_tokens
from .migrations.legacy_rebuilds import (
    _rebuild_instances_with_composite_unique as _rebuild_instances_with_composite_unique,
)


@serialized_schema(lambda: config.HOSTING_DATABASE_PATH)
def init_hosting_db():
    """Import legacy storage once; refuse unsupported code/database pairings."""
    with get_hosting_db_context() as conn:
        apply_migrations(conn, 0x42574850, (_upgrade_legacy, _api_tokens))
