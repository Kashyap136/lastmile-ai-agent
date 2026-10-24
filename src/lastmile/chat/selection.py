"""Interactive-card selection parsing.

ASI:One returns card interactions in two shapes (per the documented spec):
  1. Direct @mention: JSON object serialised to a string.
  2. Planner-mediated: natural-language prose mentioning every identifier
     from the CTA's selection payload.
The parser must understand both without inventing data.
"""

from __future__ import annotations

import json
import re

_ACTION_KEYWORDS = {
    "approve_plan": ("approve", "confirm", "approve_plan", "accept"),
    "reject_plan": ("reject", "cancel", "decline", "cancel_pickup", "no"),
    "reject_plan_confirm": ("cancel",),
    "submit_donation": ("submit", "submit_donation", "donate", "submitting", "submitted"),
    "back_to_matches": ("back", "different", "other", "rematch"),
    "check_status": ("status", "where", "progress", "how"),
    "resubmit": ("fix", "resubmit", "correct", "edit"),
}

_RECIPIENT_ID = re.compile(r"\brcp_\d+\b")
_WINDOW_ID = re.compile(r"\bw\d+\b", re.IGNORECASE)
_PLAN_ID = re.compile(r"\bplan_[0-9a-f]{8}\b")


def parse_selection(text: str) -> dict:
    """Parse a card interaction reply into an action selection dict.

    Returns {} only when nothing interpretable is present - callers then
    treat the input as free text.
    """
    if not text:
        return {}

    # 1) Direct @mention: verbatim JSON.
    try:
        data = json.loads(text)
        if isinstance(data, dict) and data:
            return _normalise(data)
    except (ValueError, TypeError):
        pass

    # Also accept JSON embedded around narration.
    embedded = re.search(r"\{.*\}", text, re.DOTALL)
    if embedded:
        try:
            data = json.loads(embedded.group(0))
            if isinstance(data, dict) and data:
                return _normalise(data)
        except (ValueError, TypeError):
            pass

    # 2) Planner-mediated prose.
    selection: dict = {}
    lowered = text.lower()

    ids = _extract_ids(text)
    selection.update(ids)

    # Approve vs reject must be disambiguated in prose. "Not approve"-style
    # negations flip the keyword.
    negated = bool(re.search(r"\b(?:not|don'?t|no)\b.{0,25}\b(?:approve|confirm|accept)\b", lowered))
    approve_kw = any(k in lowered for k in _ACTION_KEYWORDS["approve_plan"])
    if approve_kw and not negated:
        selection["action"] = "approve_plan"
    elif any(k in lowered for k in _ACTION_KEYWORDS["reject_plan"]) or negated:
        selection["action"] = "reject_plan"
    else:
        for action, keywords in _ACTION_KEYWORDS.items():
            if action in ("approve_plan", "reject_plan", "reject_plan_confirm"):
                continue
            for keyword in keywords:
                if keyword in lowered:
                    selection.setdefault("action", action)
                    break
            if "action" in selection:
                break
    return selection


def _extract_ids(text: str) -> dict:
    found: dict = {}
    recipient = _RECIPIENT_ID.search(text)
    if recipient:
        found["recipient_id"] = recipient.group(0)
    window = _WINDOW_ID.search(text)
    if window:
        found["window_id"] = window.group(0)
    plan = _PLAN_ID.search(text)
    if plan:
        found["plan_id"] = plan.group(0)
    return found


# Fields that may legitimately arrive as text from the form card.
_FORM_TEXT_FIELDS = {
    "description",
    "location",
    "zone",
    "prepared_at",
    "pickup_deadline",
    "handling_notes",
    "category",
    "unit",
    "storage",
}


def _normalise(data: dict) -> dict:
    """Coerce the types ASI:One may deliver (numbers as strings, etc.)."""
    normalised: dict = {}
    for key, value in data.items():
        if key in ("quantity",):
            try:
                normalised[key] = float(value)
                continue
            except (TypeError, ValueError):
                pass
        if key in _FORM_TEXT_FIELDS and not isinstance(value, str):
            normalised[key] = str(value)
        else:
            normalised[key] = value
    return normalised
