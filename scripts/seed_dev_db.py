"""
Seed a BananaWiki development database with demo content for screenshots.
"""
import os, sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import config

os.makedirs(os.path.join(os.path.dirname(__file__), "..", "instance"), exist_ok=True)

config.DATABASE_PATH = os.path.join(
    os.path.dirname(__file__), "..", "instance", "bananawiki.db"
)
config.LOGGING_LEVEL = "off"

from werkzeug.security import generate_password_hash as _orig_generate_password_hash
try:
    import hashlib; hashlib.scrypt(b"t", salt=b"t", n=2, r=1, p=1, maxmem=132)
    generate_password_hash = _orig_generate_password_hash
except (AttributeError, TypeError, ValueError):
    def generate_password_hash(password, method="scrypt", salt_length=16):
        if method == "scrypt":
            method = "pbkdf2:sha256"
        return _orig_generate_password_hash(password, method=method, salt_length=salt_length)
import db as db_mod

db_mod.init_db()

for _dir, manifest, is_builtin in __import__("plugin_loader").discover_plugins():
    if is_builtin:
        db_mod.register_plugin(
            manifest["id"],
            name=manifest.get("name", manifest["id"]),
            version=manifest.get("version", "0.0.0"),
            author=manifest.get("author", "BananaWiki"),
            description=manifest.get("description", ""),
            builtin=True,
            enabled=True,
        )

admin_uid = db_mod.create_user("admin", generate_password_hash("admin123"), role="admin")
db_mod.update_site_settings(setup_done=1)

from db._categories import create_category
from db._kanban import create_board, create_column, create_ticket
from db._canvas import create_layout, save_layout_data
from db._groups import create_group_chat

cat_id = create_category("Getting Started")
cat2_id = create_category("Documentation")
cat3_id = create_category("Projects")

def make_page(title, slug, md, cat_id):
    db_mod.create_page(title=title, slug=slug, content=md, category_id=cat_id, user_id=admin_uid)

make_page("Wiki Home", "wiki-home", """# Welcome to BananaWiki

This is the **wiki home page** for your team. BananaWiki is a self-hostable knowledge portal built on Flask 3.1 + SQLite.

## Quick Links

- [Getting Started Guide](/welcome)
- [Canvas Diagrams](/canvas)
- [Kanban Boards](/kanban)
- [Chat with Team](/chats)

## Features

- Markdown editing with live preview
- Full version history and revert
- Hierarchical categories
- Canvas visual diagrams
- Kanban task management
- Built-in chat
- 19 built-in plugins
- Text-to-Speech
- And much more!
""", cat_id)

make_page("Editor Guide", "editor", """# Editor Guide

## Smart Editor Features

The BananaWiki editor includes a **smart layer** with:

| Feature | Description |
|---------|-------------|
| Table toolbar | Insert/delete rows and columns with context-aware toolbar |
| / Command Menu | Quick insertion of headings, tables, code blocks, images |
| Auto-pairing | Automatic bracket and quote pairing |
| Clear Formatting | Remove all formatting with one click |
| Fullscreen | Distraction-free editing mode |
| Word/Character Count | Real-time statistics |
| Subscript/Superscript | Scientific notation support |

## Formatting Examples

- **Bold** and *italic* text
- `inline code` and code blocks
- Lists (ordered and unordered)
- [Links to pages](/wiki-home)
- Images and video embeds
""", cat_id)

make_page("Canvas", "canvas", """# Canvas Overview

Canvas allows you to create **visual node-link diagrams** directly inside BananaWiki.

- Create nodes with text, wiki page references, external links, images, and videos
- Connect nodes with labeled edges
- Export layouts as `.canvas.json`
- Per-layout permission system

Canvas integrates closely with wiki pages and Kanban boards for a complete planning experience.
""", cat_id)

make_page("Kanban", "kanban-board", """# Kanban Boards

Kanban boards provide **visual task management** within BananaWiki.

- Drag-and-drop tickets between columns
- Markdown descriptions
- Priority, assignees, and due dates
- Attachments and threaded comments
- Per-user/role sharing

Use Kanban to plan sprints, track projects, or manage personal tasks.
""", cat2_id)

make_page("Chat", "chat", """# Team Chat

BananaWiki includes **built-in chat** for team communication.

- Direct messaging
- Group chats with roles
- File attachments
- Message moderation
- Banned-word filtering

Chat is built on the plugin system, so it uses the same accounts and permissions as the rest of the wiki.
""", cat2_id)

make_page("Text-to-Speech", "text-to-speech", """# Text-to-Speech

The **Text-to-Speech** plugin generates downloadable MP3 narrations of wiki pages.

- Italian and English voices available
- Local Piper TTS generation
- Performance mode for weaker machines
- Auto-recovery for transient failures

Generate narrations for any page from the page view.
""", cat2_id)

make_page("Setup Guide", "setup", """# Setup Guide

Complete the **setup wizard** to configure your BananaWiki instance:

1. Create the administrator account
2. Choose Easy or Advanced setup mode
3. Select starter plugins
4. Configure access defaults
5. Set interface language
6. Create first users
7. Take the guided tour
""", cat3_id)

board_id = create_board(title="Development Sprint", description="Sprint planning and task tracking", created_by=admin_uid)
col_backlog = create_column(board_id, "Backlog", sort_order=0)
col_in_progress = create_column(board_id, "In Progress", sort_order=1)
col_review = create_column(board_id, "Review", sort_order=2)
col_done = create_column(board_id, "Done", sort_order=3)

for col_id, title, desc, priority in [
    (col_backlog, "Set up CI/CD pipeline", "Configure automated testing and deployment", "high"),
    (col_in_progress, "Implement user authentication", "Add OAuth login support", "high"),
    (col_backlog, "Write API documentation", "Document all REST API endpoints", "medium"),
    (col_done, "Design landing page", "Create the public landing page layout", "medium"),
    (col_review, "Fix mobile layout", "Responsive fixes for mobile devices", "low"),
    (col_backlog, "Database optimization", "Optimize SQLite queries for performance", "medium"),
    (col_backlog, "Add dark mode support", "Implement dark/light theme toggle", "low"),
]:
    create_ticket(col_id, title=title, description=desc, created_by=admin_uid, priority=priority)

canvas_layout_id = create_layout(
    title="Project Architecture",
    creator_id=admin_uid,
    description="System architecture diagram",
)

canvas_data = {
    "nodes": [
        {"id": "n1", "type": "text", "x": 80, "y": 60, "width": 160, "height": 80, "text": "Frontend", "color": "#4a90d9"},
        {"id": "n2", "type": "text", "x": 80, "y": 220, "width": 160, "height": 80, "text": "API Layer", "color": "#50b86c"},
        {"id": "n3", "type": "text", "x": 80, "y": 380, "width": 160, "height": 80, "text": "Database", "color": "#e67e22"},
        {"id": "n4", "type": "text", "x": 380, "y": 60, "width": 160, "height": 80, "text": "Wiki Pages", "color": "#9b59b6"},
        {"id": "n5", "type": "text", "x": 380, "y": 220, "width": 160, "height": 80, "text": "Canvas", "color": "#e74c3c"},
        {"id": "n6", "type": "text", "x": 380, "y": 380, "width": 160, "height": 80, "text": "Kanban", "color": "#1abc9c"},
    ],
    "edges": [
        {"id": "e1", "from": "n1", "to": "n2", "label": "requests"},
        {"id": "e2", "from": "n2", "to": "n3", "label": "queries"},
        {"id": "e3", "from": "n2", "to": "n4", "label": "pages"},
        {"id": "e4", "from": "n2", "to": "n5", "label": "layouts"},
        {"id": "e5", "from": "n2", "to": "n6", "label": "boards"},
    ],
}
save_layout_data(canvas_layout_id, canvas_data)

create_group_chat("Team Alpha", creator_id=admin_uid)
create_group_chat("Design Team", creator_id=admin_uid)

print("Database seeded successfully!")
print(f"Database path: {config.DATABASE_PATH}")
print("Admin login: admin / admin123")
