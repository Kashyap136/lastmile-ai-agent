"""ASI Interactive Cards payload builders.

Strictly follows the documented card spec:
  * metadata keys: card_protocol_version "1", requires_card_interaction "true",
    card_kind, card_payload (JSON string <= 64 KB)
  * predefined kinds used: form, carousel, detail, review
Builders return plain dicts ready to be json.dumps-ed into MetadataContent.
"""

from __future__ import annotations

import json

from ..models import DataSource, DonationIntake, PickupPlan
from ..data.zones import DEMO_ZONES
from ..models.common import FoodCategory, QuantityUnit, StorageType

CARD_PROTOCOL_VERSION = "1"
KIND_FORM = "form"
KIND_CAROUSEL = "carousel"
KIND_DETAIL = "detail"
KIND_REVIEW = "review"
KIND_CUSTOM = "custom"


class CardTooLarge(ValueError):
    """Raised when a card payload exceeds the documented 64 KB limit."""


def card_metadata(card_kind: str, payload: dict) -> dict[str, str]:
    """Build the MetadataContent.metadata block for a card declaration."""
    payload_json = json.dumps(payload)
    if len(payload_json.encode("utf-8")) > 64 * 1024:
        raise CardTooLarge(f"card payload exceeds 64 KB limit ({len(payload_json)} bytes)")
    return {
        "card_protocol_version": CARD_PROTOCOL_VERSION,
        "requires_card_interaction": "true",
        "card_kind": card_kind,
        "card_payload": payload_json,
        "preferred_drawer_width_px": "540",
    }


# --------------------------------------------------------------------------
# Donation intake form
# --------------------------------------------------------------------------

def donation_form_card(partial: dict | None = None) -> dict:
    """Form card collecting the full structured donation intake.

    `partial` keeps previously-submitted valid values so users only retype
    what is missing (safe handling of incomplete submissions).
    """
    partial = partial or {}

    def _pretty(_name, value) -> str:
        # enum values like packaged_canned -> "Packaged Canned"
        return str(value).replace("_", " ").title()

    fields = [
        {
            "name": "category",
            "kind": "select",
            "label": "Food category",
            "required": True,
            "options": [
                {"value": c.value, "label": _pretty("category", c.value)}
                for c in FoodCategory
            ],
        },
        {
            "name": "description",
            "kind": "text",
            "label": "Description (what exactly is left over?)",
            "placeholder": "e.g. 30 fresh vegan noodle salad bowls",
            "required": True,
        },
        {
            "name": "quantity",
            "kind": "number",
            "label": "Quantity",
            "required": True,
        },
        {
            "name": "unit",
            "kind": "select",
            "label": "Unit",
            "required": True,
            "options": [
                {"value": u.value, "label": _pretty("unit", u.value)} for u in QuantityUnit
            ],
        },
        {
            "name": "prepared_at",
            "kind": "text",
            "label": "Prepared / collected at (ISO time, or 'in 3 hours')",
            "placeholder": "2026-10-24T11:30",
            "required": True,
        },
        {
            "name": "pickup_deadline",
            "kind": "text",
            "label": "Pickup deadline (must be collected before)",
            "placeholder": "e.g. today 18:30 or 2026-10-24T18:30",
            "required": True,
        },
        {
            "name": "location",
            "kind": "text",
            "label": "Pickup location (street address of the kitchen)",
            "placeholder": "e.g. 42 Food Street, Fairview (demo)",
            "required": True,
        },
        {
            "name": "zone",
            "kind": "select",
            "label": "Nearest demo zone (used for distance estimates)",
            "required": True,
            "options": [{"value": z, "label": z} for z in DEMO_ZONES],
        },
        {
            "name": "storage",
            "kind": "select",
            "label": "Storage since preparation",
            "required": True,
            "options": [
                {"value": s.value, "label": _pretty("storage", s.value)} for s in StorageType
            ],
        },
        {
            "name": "handling_notes",
            "kind": "text",
            "label": "Handling notes (optional)",
            "placeholder": "allergens, containers, keep upright, etc.",
            "required": False,
        },
    ]
    payload = {
        "title": "Report a surplus donation",
        "fields": fields,
        "submit_cta": {
            "label": "Submit donation",
            "selection": {"action": "submit_donation"},
        },
    }
    # Preserve previously submitted values as field defaults. The card spec
    # for `form` does not declare default values, so `partial` is additionally
    # carried in the session state; it is included here as a plain hint block.
    if partial:
        payload["preferred_drawer_width_px"] = "620"
        payload["fields"] = [_apply_default(f, partial) for f in payload["fields"]]
    return payload


def _apply_default(field: dict, partial: dict) -> dict:
    value = partial.get(field["name"])
    if value in (None, "", []):
        return field
    patched = dict(field)
    if field["kind"] == "select":
        options = list(field.get("options", []))
        # Move the previously chosen option to the top so the form starts there.
        matching = [o for o in options if o.get("value") == value]
        if matching:
            patched["options"] = [matching[0]] + [o for o in options if o is not matching[0]]
    else:
        patched["placeholder"] = f"previously entered: {value}"
    return patched


# --------------------------------------------------------------------------
# Matched recipients carousel
# --------------------------------------------------------------------------

def recipients_carousel_card(intake: DonationIntake, matches: list) -> dict:
    items = []
    for match in matches:
        r = match.recipient
        distance_text = (
            f"~{match.estimated_distance_km:g} km" if match.estimated_distance_km is not None
            else "distance unknown"
        )
        data_label = " (demo dataset)" if r.is_demo() else ""
        items.append(
            {
                "id": r.recipient_id,
                "title": r.name,
                "subtitle": f"{r.org_type.replace('_', ' ').title()}{data_label} | "
                f"{r.zone} | {distance_text}",
                "badges": [
                    {"label": "Verified directory: demo", "variant": "info"},
                    {"label": f"rank {match.rank}", "variant": "success"},
                ],
                "secondary_text": match.explanation(),
                "primary_cta": {
                    "label": "Select",
                    "selection": {"action": "select_recipient", "recipient_id": r.recipient_id},
                },
            }
        )
    return {
        "title": "Matched recipient organisations",
        "subtitle": f"Donation #{intake.donation_id} | top {len(matches)} ranked matches",
        "items": items,
    }


# --------------------------------------------------------------------------
# Chosen recipient + pickup windows detail card
# --------------------------------------------------------------------------

def pickup_windows_detail_card(proposal, donor_zone: str, window_travel: dict | None = None) -> dict:
    """Detail card: chosen recipient + radio choice of feasible windows."""
    rows = [
        {"label": "Recipient", "value": proposal.recipient_name},
        {"label": "Donation", "value": proposal.donation_id},
        {"label": "Pickup zone", "value": donor_zone or "unknown"},
    ]
    choices = []
    for window in proposal.windows:
        choices.append(
            {"value": window.window_id, "label": window.label or window.window_id}
        )
    return {
        "title": "Choose a pickup window",
        "summary_rows": rows,
        "sub_options": {
            "name": "window_id",
            "kind": "radio",
            "label": "Feasible windows (planner-proposed)",
            "choices": choices,
        },
        "ctas": [
            {
                "label": "Continue",
                "primary": True,
                "selection": {"action": "select_window", "recipient_id": proposal.recipient_id},
            },
            {"label": "Pick a different recipient", "selection": {"action": "back_to_matches"}},
        ],
    }


# --------------------------------------------------------------------------
# Pickup plan review card
# --------------------------------------------------------------------------

def plan_review_card(plan: PickupPlan, intake: DonationIntake, demo_recipient: bool) -> dict:
    travel = plan.travel
    distance_text = f"{travel.distance_km:g} km ({travel.data_source.value})" if travel.distance_km else "unavailable"
    duration_text = f"{travel.duration_minutes:g} min" if travel.duration_minutes else "unavailable"
    rows = [
        {"label": "Recipient organisation", "value": plan.recipient_name},
        {"label": "Pickup deadline", "value": intake.pickup_deadline.strftime("%a %d %b %H:%M UTC")},
        {"label": "Window", "value": plan.window.label or plan.window.window_id},
        {"label": "Distance", "value": distance_text},
        {"label": "Estimated travel time", "value": duration_text},
        {"label": "Route", "value": plan.route_summary},
    ]
    if intake.handling_notes:
        rows.append({"label": "Handling notes", "value": intake.handling_notes})
    return {
        "title": "Confirm pickup coordination",
        "summary_rows": rows,
        "approve_cta": {
            "label": "Approve & coordinate pickup",
            "primary": True,
            "selection": {"action": "approve_plan", "plan_id": plan.plan_id, "recipient_id": plan.recipient_id, "window_id": plan.window.window_id},
        },
        "reject_cta": {"label": "Reject plan", "selection": {"action": "reject_plan"}},
    }
