"""Helpers shared by the chat tests."""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

JSON = {"Accept": "application/json"}


def upload(name: str = "note.txt", data: bytes = b"hello file") -> tuple[io.BytesIO, str]:
    return io.BytesIO(data), name


def chat_folder(app) -> Path:
    return Path(app.config["BW"].folders.chat_attachments)


def set_settings(db, **values: Any) -> None:
    for key, value in values.items():
        db.execute(f'UPDATE site_settings SET "{key}" = ? WHERE id = 1', (value,))


def start_dm(client, username: str) -> int:
    response = client.post("/chats/new", data={"username": username}, headers=JSON)
    assert response.status_code == 200, response.data[:300]
    return response.json["chat_id"]


def create_group(client, name: str = "Team") -> int:
    response = client.post("/groups/new", data={"name": name}, headers=JSON)
    assert response.status_code == 200, response.data[:300]
    return response.json["group_id"]


def send(client, url: str, content: str = "hello", **files: Any):
    data: dict[str, Any] = {"content": content, **files}
    return client.post(url, data=data, headers=JSON, content_type="multipart/form-data")


def switch(client, login, user) -> None:
    client.post("/logout")
    login(client, user)
