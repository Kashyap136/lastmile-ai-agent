"""Coordinator discovery support for multi-agent mode.

Specialist agents register in a tiny local address book (JSON file) so the
coordinator can send to them without recomputing addresses. For demo
simplicity specialists also expose their address via env var.
"""

from __future__ import annotations

import json
import os
from pathlib import Path


def address_book_path() -> Path:
    return Path(
        os.getenv(
            "LASTMILE_ADDRESS_BOOK",
            os.path.join(os.getcwd(), "agent_address_book.json"),
        )
    )


def publish_address(agent_name: str, address: str) -> None:
    path = address_book_path()
    try:
        book = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except ValueError:
        book = {}
    book[agent_name] = address
    path.write_text(json.dumps(book, indent=2, sort_keys=True), encoding="utf-8")


def read_address(agent_name: str) -> str | None:
    path = address_book_path()
    try:
        book = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except ValueError:
        return None
    return book.get(agent_name)
