"""Structured food-safety eligibility rules.

These rules are configurable and every fired rule contributes a reason to
the SafetyDecision. Unknown or unverifiable cases are REJECTED - the
validator never invents safety certifications.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from ..models.common import FoodCategory, StorageType
from ..models.donation import DonationIntake
from ..models.safety import SafetyDecision


@dataclass
class SafetyRules:
    """Demonstration eligibility rules (editable, explainable defaults)."""

    # How long food may have been prepared before validation, per category.
    # (Refrigerated storage extends these windows - see the cold-chain rule.)
    max_age_hours: dict[FoodCategory, float] = field(default_factory=lambda: {
        FoodCategory.PREPARED_MEAL: 4.0,
        FoodCategory.BAKERY: 48.0,
        FoodCategory.PRODUCE: 96.0,
        FoodCategory.DAIRY: 72.0,
        FoodCategory.PACKAGED_CANNED: 24 * 60.0,  # bounded by printed dates instead
        FoodCategory.BEVERAGES: 24 * 30.0,
    })
    # Categories that require refrigeration or frozen storage.
    cold_chain_required: set[FoodCategory] = field(default_factory=lambda: {
        FoodCategory.PREPARED_MEAL,
        FoodCategory.DAIRY,
        FoodCategory.RAW_MEAT_OR_FISH,
    })
    # Categories that are never eligible for direct transfer in this demo.
    rejected_categories: set[FoodCategory] = field(default_factory=lambda: {
        FoodCategory.RAW_MEAT_OR_FISH,  # needs certified cold chain, out of scope
    })
    # Ambient storage is only acceptable for these categories.
    ambient_ok_categories: set[FoodCategory] = field(default_factory=lambda: {
        FoodCategory.BAKERY,
        FoodCategory.PRODUCE,
        FoodCategory.PACKAGED_CANNED,
        FoodCategory.BEVERAGES,
    })
    # Pickup must be requested within this horizon.
    min_deadline_minutes: int = 45
    max_deadline_hours: float = 72.0
    max_quantity: float = 1000.0
    # Words that flag clearly spoiled / unsafe food in handling notes.
    unsafe_flag_words: tuple[str, ...] = (
        "expired", "spoiled", "mould", "mold", "rancid", "rotten",
        "smell", "smells", "re-frozen", "thawed and refrozen",
    )


def check_safety(
    intake: DonationIntake,
    rules: SafetyRules | None = None,
    now: datetime | None = None,
) -> SafetyDecision:
    """Run the structured rule set; every fired rule becomes a reason.

    Returns a SafetyDecision with approved=False when ANY hard rule fails.
    Nothing in here invents certifications: unverifiable cases are refused
    with the concrete rule id and reason.
    """
    rules = rules or SafetyRules()
    now = now or datetime.now(timezone.utc)
    reasons: list[str] = []
    rule_ids: list[str] = []

    def fail(rule_id: str, reason: str) -> None:
        rule_ids.append(rule_id)
        reasons.append(f"[{rule_id}] {reason}")

    # R01 - category policy
    if intake.category in rules.rejected_categories:
        fail(
            "R01_category_policy",
            f"category {intake.category.value} is not eligible for direct transfer "
            "in this configuration (requires certified cold-chain handling)",
        )

    # R02 - quantity bounds
    if not (0 < intake.quantity <= rules.max_quantity):
        fail(
            "R02_quantity_bounds",
            f"quantity {intake.quantity:g} {intake.unit.value} is outside the "
            f"accepted range (0, {rules.max_quantity:g}]",
        )

    # R03 - pickup deadline horizon
    min_deadline = now + timedelta(minutes=rules.min_deadline_minutes)
    max_deadline = now + timedelta(hours=rules.max_deadline_hours)
    if intake.pickup_deadline < min_deadline:
        fail(
            "R03_deadline_too_soon",
            f"pickup deadline {intake.pickup_deadline.isoformat()} is sooner than "
            f"the required {rules.min_deadline_minutes}-minute notice",
        )
    elif intake.pickup_deadline > max_deadline:
        fail(
            "R04_deadline_too_far",
            f"pickup deadline {intake.pickup_deadline.isoformat()} is further than "
            f"the {rules.max_deadline_hours:g}- hour planning horizon",
        )

    # R05 - prepared_at must be in the past
    if intake.prepared_at > now:
        fail(
            "R05_prepared_in_future",
            f"prepared_at {intake.prepared_at.isoformat()} is in the future",
        )

    # R06 - food age vs category limits (refrigeration buys extra time only
    # for the age rule when the food is truly refrigerated/frozen).
    age_limit = rules.max_age_hours.get(intake.category)
    effective_age_limit = age_limit
    if (
        age_limit is not None
        and intake.category == FoodCategory.PREPARED_MEAL
        and intake.storage in (StorageType.REFRIGERATED, StorageType.FROZEN)
    ):
        effective_age_limit = age_limit * 12.0  # refrigerated prepared meals: 48h
    if effective_age_limit is not None:
        age_hours = (now - intake.prepared_at).total_seconds() / 3600.0
        if age_hours > effective_age_limit:
            fail(
                "R06_food_age",
                f"food of category {intake.category.value} appears "
                f"{age_hours:g}h old, exceeding the {effective_age_limit:g}h window",
            )

    # R07 - cold chain consistency
    if (
        intake.category in rules.cold_chain_required
        and intake.category not in rules.rejected_categories
        and intake.storage not in (StorageType.REFRIGERATED, StorageType.FROZEN)
    ):
        fail(
            "R07_cold_chain_required",
            f"category {intake.category.value} requires refrigerated or frozen "
            f"storage but declared {intake.storage.value}",
        )

    # R09 - handling notes must not describe spoiled / unsafe food
    notes = (intake.handling_notes or "").lower()
    for word in rules.unsafe_flag_words:
        if word in notes:
            fail(
                "R09_handling_notes",
                f"handling notes contain an unsafe-donation indicator ({word!r}) - "
                "food cannot be verified as safe",
            )
            break

    return SafetyDecision(
        approved=not reasons,
        reasons=reasons,
        rule_ids=rule_ids,
        checked_rules=8,
    )
