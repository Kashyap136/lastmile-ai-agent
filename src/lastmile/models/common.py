"""Core domain enums and small value types shared across layers."""

from __future__ import annotations

from enum import StrEnum


class FoodCategory(StrEnum):
    PREPARED_MEAL = "prepared_meal"
    BAKERY = "bakery"
    PRODUCE = "produce"
    DAIRY = "dairy"
    PACKAGED_CANNED = "packaged_canned"
    BEVERAGES = "beverages"
    RAW_MEAT_OR_FISH = "raw_meat_or_fish"


class StorageType(StrEnum):
    AMBIENT = "ambient"
    REFRIGERATED = "refrigerated"
    FROZEN = "frozen"
    HOT_HELD = "hot_held"


class QuantityUnit(StrEnum):
    MEALS = "meals"
    KG = "kg"
    BOXES = "boxes"
    CRATES = "crates"
    ITEM = "items"


class DonationStatus(StrEnum):
    """Lifecycle statuses we can honestly report to the user.

    We never mark a status that implies an external action succeeded
    unless the related service confirmed it (e.g. dispatch webhook 2xx).
    """

    RECEIVED = "received"  # intake complete, not yet validated
    VALIDATED = "validated"  # passed safety validation
    REJECTED_BY_SAFETY = "rejected_by_safety"
    MATCHED = "matched"  # ranked recipients proposed for choice
    NO_MATCH = "no_match"  # no eligible recipient found
    PROPOSED = "proposed"  # pickup plan proposed, awaiting approval
    CONFIRMED = "confirmed"  # donor approved; demo recipient not yet notified
    AWAITING_RECIPIENT_CONFIRMATION = "awaiting_recipient_confirmation"
    DISPATCHED_TO_RECIPIENT = "dispatched_to_recipient"  # webhook 2xx only
    PICKED_UP = "picked_up"  # donor reported the pickup happened
    DECLINED = "declined"  # donor rejected the proposed plan
    CANCELLED = "cancelled"  # donor cancelled the donation
    EXPIRED = "expired"  # pickup deadline passed before confirmation
    FAILED = "failed"  # transient internal failure, retriable


class WorkflowStage(StrEnum):
    """Session-aware workflow state, persisted per chat session."""

    NEW = "new"
    AWAITING_DONATION_FORM = "awaiting_donation_form"
    AWAITING_RECIPIENT_CHOICE = "awaiting_recipient_choice"
    AWAITING_WINDOW_CHOICE = "awaiting_window_choice"
    AWAITING_PLAN_REVIEW = "awaiting_plan_review"
    DONE = "done"


class DataSource(StrEnum):
    """Explicit provenance labels - required by the honesty rules."""

    DEMO = "demo"  # from the clearly-labelled demonstration dataset
    CALCULATED_ESTIMATE = "calculated_estimate"  # our own math, labelled
    EXTERNAL_API = "external_api"  # confirmed by a real external service
    USER_INPUT = "user_input"  # provided by the donor in chat
