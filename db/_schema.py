"""Serialized, atomic schema initialization and version upgrades."""

import config
from sqlite_runtime import serialized_schema
from sqlite_migrations import apply_migrations

from ._connection import get_db_context
from .migrations.legacy import _upgrade_legacy
from .migrations.navigation import upgrade as _navigation_index
from .migrations.federation import upgrade as _federation
from .migrations.legacy_rebuilds import (
    _migrate_banana_chat_to_banana_ai as _migrate_banana_chat_to_banana_ai,
    _recreate_table_int_fk_to_text as _recreate_table_int_fk_to_text,
)


@serialized_schema(lambda: config.DATABASE_PATH)
def init_db():
    """Import legacy storage once; refuse unsupported code/database pairings."""
    with get_db_context() as conn:
        apply_migrations(conn, 0x42574B49, (_upgrade_legacy, _navigation_index, _federation))
