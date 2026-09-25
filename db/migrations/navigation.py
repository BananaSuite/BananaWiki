"""Version 2: indexed page navigation without materializing all page bodies."""


def upgrade(connection):
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_pages_navigation "
        "ON pages(is_home, category_id, sort_order, title, id)"
    )
