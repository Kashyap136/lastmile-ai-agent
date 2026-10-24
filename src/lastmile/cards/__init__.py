"""Card builders and metadata for ASI:One Interactive Cards.

Strictly follows the documented card spec:
  * metadata keys: card_protocol_version "1", requires_card_interaction "true",
    card_kind, card_payload (JSON string <= 64 KB)
  * predefined kinds used: form, carousel, detail, review
Builders return plain dicts ready to be json.dumps-ed into MetadataContent.
"""

from .builders import (
    CARD_PROTOCOL_VERSION,
    KIND_CAROUSEL,
    KIND_CUSTOM,
    KIND_DETAIL,
    KIND_FORM,
    KIND_REVIEW,
    CardTooLarge,
    card_metadata,
    donation_form_card,
    pickup_windows_detail_card,
    plan_review_card,
    recipients_carousel_card,
)

__all__ = [
    "CARD_PROTOCOL_VERSION",
    "KIND_CAROUSEL",
    "KIND_CUSTOM",
    "KIND_DETAIL",
    "KIND_FORM",
    "KIND_REVIEW",
    "CardTooLarge",
    "card_metadata",
    "donation_form_card",
    "pickup_windows_detail_card",
    "plan_review_card",
    "recipients_carousel_card",
]
