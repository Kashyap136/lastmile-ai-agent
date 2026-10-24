"""Agent Chat Protocol (ACP) message helpers.

Uses the canonical uagents_core chat protocol models (v0.3.0) exactly as
documented. Every outgoing user-facing message is either:
  * a text ChatMessage, or
  * a text ChatMessage with ONE MetadataContent block declaring a card.
Incoming messages are always acknowledged with ChatAcknowledgement.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from uuid import uuid4

from uagents_core.contrib.protocols.chat import (
    ChatAcknowledgement,
    ChatMessage,
    MetadataContent,
    TextContent,
)

from ..cards.builders import card_metadata


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def new_msg_id():
    return uuid4()


def text_message(text: str) -> ChatMessage:
    return ChatMessage(
        timestamp=now_utc(),
        msg_id=new_msg_id(),
        content=[TextContent(type="text", text=text)],
    )


def card_message(narration: str, card_kind: str, payload: dict) -> ChatMessage:
    """Send narration + one interactive card declaration."""
    return ChatMessage(
        timestamp=now_utc(),
        msg_id=new_msg_id(),
        content=[
            TextContent(type="text", text=narration),
            MetadataContent(type="metadata", metadata=card_metadata(card_kind, payload)),
        ],
    )


def ack_message(msg_id) -> ChatAcknowledgement:
    return ChatAcknowledgement(timestamp=now_utc(), acknowledged_msg_id=msg_id)


def first_text(msg: ChatMessage) -> str:
    """The first TextContent block of an incoming ChatMessage ('' if none)."""
    for item in msg.content:
        if isinstance(item, TextContent):
            return item.text
    return ""


def outgoing_card_payload(msg: ChatMessage) -> dict | None:
    """Extract (card_kind, payload) from an outgoing ChatMessage - test helper."""
    for item in msg.content:
        if isinstance(item, MetadataContent):
            meta = item.metadata
            if meta.get("requires_card_interaction") == "true":
                return {
                    "kind": meta.get("card_kind"),
                    "payload": json.loads(meta.get("card_payload", "{}")),
                }
    return None


def contents_of(msg: ChatMessage) -> dict:
    """Summarise an outgoing/incoming ChatMessage for tests and logs."""
    text = first_text(msg)
    card = outgoing_card_payload(msg)
    return {"text": text, "card": card}
