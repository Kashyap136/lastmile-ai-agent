"""Tests for donation intake parsing and safety rules."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from lastmile.models.common import DonationStatus, FoodCategory, StorageType
from lastmile.models.donation import DonationIntake, parse_intake_submission
from lastmile.validation.rules import SafetyRules, check_safety

from .conftest import valid_selection


def test_valid_submission_builds_intake():
    intake, errors = parse_intake_submission(valid_selection())
    assert errors == []
    assert intake.quantity == 40.0
    assert intake.category == FoodCategory.PREPARED_MEAL
    assert intake.storage == StorageType.REFRIGERATED
    assert intake.donation_id.startswith("don_")


def test_missing_fields_reported_individually():
    selection = valid_selection()
    for field in ("category", "quantity", "pickup_deadline", "location"):
        selection.pop(field)
    intake, errors = parse_intake_submission(selection)
    assert intake is None
    assert len(errors) == 4
    assert any("missing field: category" in e for e in errors)


def test_invalid_enum_never_guessed():
    selection = valid_selection()
    selection["category"] = "alien_nuggets"
    selection["storage"] = "warm-ish"
    intake, errors = parse_intake_submission(selection)
    assert intake is None
    assert any("invalid field: category" in e for e in errors)
    assert any("invalid field: storage" in e for e in errors)


def test_bad_datetime_and_quantity():
    selection = valid_selection()
    selection["quantity"] = "lots"
    selection["pickup_deadline"] = "sometime next week"
    intake, errors = parse_intake_submission(selection)
    assert intake is None
    assert any("invalid field: quantity" in e for e in errors)
    assert any("invalid field: pickup_deadline" in e for e in errors)


def _intake(**overrides):
    selection = valid_selection()
    selection.update(overrides)
    intake, errors = parse_intake_submission(selection)
    assert not errors, errors
    return intake


def test_safety_approves_fresh_refrigerated_meals():
    now = datetime.now(timezone.utc)
    decision = check_safety(_intake(), now=now)
    assert decision.approved, decision.reasons
    assert decision.checked_rules == 8


def test_safety_rejects_raw_meat():
    now = datetime.now(timezone.utc)
    intake = _intake(category="raw_meat_or_fish", storage="frozen", description="fresh tuna loins")
    decision = check_safety(intake, now=now)
    assert not decision.approved
    assert "R01_category_policy" in decision.rule_ids


def test_safety_rejects_ambient_prepared_food():
    now = datetime.now(timezone.utc)
    intake = _intake(storage="ambient")
    decision = check_safety(intake, now=now)
    assert not decision.approved
    assert "R07_cold_chain_required" in decision.rule_ids


def test_safety_rejects_deadline_too_soon():
    now = datetime.now(timezone.utc)
    intake = _intake(pickup_deadline=(now + timedelta(minutes=10)).isoformat())
    decision = check_safety(intake, now=now)
    assert not decision.approved
    assert "R03_deadline_too_soon" in decision.rule_ids


def test_safety_rejects_ancient_food():
    now = datetime.now(timezone.utc)
    old = _intake(prepared_at=(now - timedelta(hours=70)).isoformat())
    decision = check_safety(old, now=now)
    assert not decision.approved
    assert "R06_food_age" in decision.rule_ids


def test_safety_flags_spoiled_notes():
    now = datetime.now(timezone.utc)
    intake = _intake(handling_notes="some containers smell off")
    decision = check_safety(intake, now=now)
    assert not decision.approved
    assert "R09_handling_notes" in decision.rule_ids


def test_safety_rejects_future_preparation():
    now = datetime.now(timezone.utc)
    intake = _intake(prepared_at=(now + timedelta(hours=2)).isoformat())
    decision = check_safety(intake, now=now)
    assert not decision.approved
    assert "R05_prepared_in_future" in decision.rule_ids


def test_status_defaults():
    intake = _intake()
    assert intake.status == DonationStatus.RECEIVED
