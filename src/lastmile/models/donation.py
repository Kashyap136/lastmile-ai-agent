"""Donation intake model and structured parsing of card-form submissions."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field

from ..validation.parsers import parse_flexible_datetime
from .common import DataSource, DonationStatus, FoodCategory, QuantityUnit, StorageType


class DonationIntake(BaseModel):
    """Structured donation details collected through the ASI:One form card."""

    donation_id: str = Field(default_factory=lambda: f"don_{uuid4().hex[:10]}")
    category: FoodCategory
    description: str = Field(min_length=3, max_length=280)
    quantity: float = Field(gt=0)
    unit: QuantityUnit
    prepared_at: datetime  # when the food was prepared/collected
    pickup_deadline: datetime  # must be collected before this time
    location: str = Field(min_length=4, max_length=200)
    zone: str  # selectable neighbourhood used for distance estimation
    storage: StorageType
    handling_notes: str = Field(default="", max_length=280)

    status: DonationStatus = DonationStatus.RECEIVED
    data_source: DataSource = DataSource.USER_INPUT
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


_REQUIRED_FIELDS = (
    "category",
    "description",
    "quantity",
    "unit",
    "prepared_at",
    "pickup_deadline",
    "location",
    "zone",
    "storage",
)


def parse_intake_submission(selection: dict[str, Any]) -> tuple[DonationIntake | None, list[str]]:
    """Build a DonationIntake from a card form submission.

    Returns (intake, errors). Never raises on bad or incomplete input:
    every problem is reported as a clear, structured field error so the
    user can correct it safely. Partial submissions are reported, never
    silently completed.
    """
    errors: list[str] = []

    missing = [f for f in _REQUIRED_FIELDS if selection.get(f) in (None, "", [])]
    if missing:
        return None, [f"missing field: {field}" for field in missing]

    parsed: dict[str, Any] = {}

    # Enum fields: fail clearly instead of guessing a value.
    for name, enum_type in (
        ("category", FoodCategory),
        ("unit", QuantityUnit),
        ("storage", StorageType),
    ):
        raw = str(selection[name]).strip().lower()
        try:
            parsed[name] = enum_type(raw)
        except ValueError:
            errors.append(
                f"invalid field: {name} must be one of "
                f"{', '.join(v.value for v in enum_type)} (got {raw!r})"
            )

    # Description / location / zone: trimmed strings with sane limits.
    description = str(selection["description"]).strip()
    if len(description) < 3:
        errors.append("invalid field: description must be at least 3 characters")
    else:
        parsed["description"] = description[:280]

    location = str(selection["location"]).strip()
    if len(location) < 4:
        errors.append("invalid field: location must be at least 4 characters")
    else:
        parsed["location"] = location[:200]
    parsed["zone"] = str(selection["zone"]).strip()
    parsed["handling_notes"] = str(selection.get("handling_notes", "") or "").strip()[:280]

    # Quantity: must coerce to a positive number.
    try:
        quantity = float(selection["quantity"])
        if quantity <= 0:
            raise ValueError
        parsed["quantity"] = quantity
    except (TypeError, ValueError):
        errors.append("invalid field: quantity must be a positive number")

    # Datetimes: ISO 8601 or a small set of human forms ("in 3 hours").
    for name in ("prepared_at", "pickup_deadline"):
        value, error = parse_flexible_datetime(str(selection[name]).strip())
        if error:
            errors.append(f"invalid field: {name} ({error})")
        else:
            parsed[name] = value

    if errors:
        return None, errors

    intake = DonationIntake(**parsed)
    return intake, []


def intake_from_json(raw: str | bytes) -> tuple[DonationIntake | None, list[str]]:
    """Parse a DonationIntake from an inter-agent JSON payload."""
    try:
        data = json.loads(raw)
    except (ValueError, TypeError) as exc:
        return None, [f"unparseable donation payload: {exc}"]
    try:
        return DonationIntake(**data), []
    except Exception as exc:
        return None, [f"invalid donation payload: {exc}"]
