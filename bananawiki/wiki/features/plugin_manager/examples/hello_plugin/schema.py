"""The plugin's own table. Runs at every start, so it must be idempotent.

Plugins may only create and change objects named ``hello_plugin__…``.
"""


def upgrade(conn):
    conn.execute(
        "CREATE TABLE IF NOT EXISTS hello_plugin__counts ("
        "name TEXT PRIMARY KEY, value INTEGER NOT NULL DEFAULT 0)"
    )
