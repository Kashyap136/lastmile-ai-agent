"""Tests for interactive card builders and selection parsing."""

from __future__ import annotations

import json

from lastmile.cards import (
    CardTooLarge,
    KIND_CAROUSEL,
    KIND_DETAIL,
    KIND_FORM,
    KIND_REVIEW,
    card_metadata,
    donation_form_card,
    pickup_windows_detail_card,
    plan_review_card,
    recipients_carousel_card,
)
from lastmile.chat.selection import parse_selection
from lastmile.models.donation import DonationIntake
from lastmile.models.pickup import PickupPlan, PickupWindow, TravelEstimate

from .conftest import valid_selection

_INTAKE = DonationIntake(**{k: v for k, v in valid_selection().items() if k != "action"})


def test_card_metadata_contains_all_required_keys():
    meta = card_metadata(KIND_FORM, donation_form_card())
    assert meta["card_protocol_version"] == "1"
    assert meta["requires_card_interaction"] == "true"
    assert meta["card_kind"] == KIND_FORM
    assert isinstance(json.loads(meta["card_payload"]), dict)
    assert meta["card_payload"].encode().__len__() < 64 * 1024


def test_card_payload_size_guard():
    big_payload = {"x": "A" * (200000)}
    try:
        card_metadata(KIND_FORM, big_payload)
    except CardTooLarge:
        pass
    else:
        raise AssertionError("expected CardTooLarge for 200KB payload")


def test_form_card_lists_all_intake_fields():
    payload = donation_form_card()
    names = [f["name"] for f in payload["fields"]]
    for expected in (
        "category", "description", "quantity", "unit", "prepared_at",
        "pickup_deadline", "location", "zone", "storage", "handling_notes",
    ):
        assert expected in names
    kinds = {f["name"]: f["kind"] for f in payload["fields"]}
    assert kinds["category"] == "select"
    assert kinds["quantity"] == "number"


def test_carousel_card_carries_demo_labels_and_selection():
    from lastmile.models.matching import RecipientMatch, ScoreBreakdown
    from lastmile.models.recipient import RecipientProfile

    recipient = RecipientProfile(
        recipient_id="rcp_test",
        name="Test Kitchen (demo)",
        org_type="soup_kitchen",
        address_text="somewhere",
        zone="Riverside",
        latitude=40.7,
        longitude=-74.0,
        capacity_units=100,
        accepts_categories=["prepared_meal"],
        min_notice_minutes=30,
    )
    match = RecipientMatch(recipient=recipient, score=ScoreBreakdown(eligibility=1.0), reasons=[])
    payload = recipients_carousel_card(_INTAKE, [match])
    item = payload["items"][0]
    assert item["primary_cta"]["selection"] == {
        "action": "select_recipient",
        "recipient_id": "rcp_test",
    }
    assert "(demo" in item["subtitle"].lower() or "demo" in " ".join(
        b["label"].lower() for b in item["badges"]
    )


def test_detail_card_sub_options_use_window_ids():
    from datetime import datetime, timedelta, timezone

    from lastmile.models.pickup import PickupProposal, PickupWindow

    soon = datetime.now(timezone.utc)
    proposal = PickupProposal(
        donation_id=_INTAKE.donation_id,
        recipient_id="rcp_01",
        recipient_name="Riverside Community Kitchen (demo)",
        windows=[
            PickupWindow(
                window_id="w1",
                start=soon + timedelta(hours=2),
                end=soon + timedelta(hours=5),
                label="Wed 01 Jan 10:30 - 13:00 UTC",
            ),
            PickupWindow(
                window_id="w2",
                start=soon + timedelta(hours=8),
                end=soon + timedelta(hours=11),
                label="Wed 01 Jan 16:30 - 19:30 UTC",
            ),
        ],
    )

    payload = pickup_windows_detail_card(proposal, _INTAKE.zone)
    assert payload["sub_options"]["name"] == "window_id"
    values = [c["value"] for c in payload["sub_options"]["choices"]]
    assert values == ["w1", "w2"]


def test_review_card_has_approve_and_reject():
    plan = PickupPlan(
        donation_id=_INTAKE.donation_id,
        recipient_id="rcp_01",
        recipient_name="Riverside Community Kitchen (demo)",
        window=PickupWindow(window_id="w1", **{"start": _INTAKE.pickup_deadline, "end": _INTAKE.pickup_deadline}),
        travel=TravelEstimate(distance_km=2.5, duration_minutes=8.0),
        route_summary="approx. 2.5 km, 8 min (source: calculated_estimate)",
    )
    payload = plan_review_card(plan, _INTAKE, demo_recipient=True)
    assert payload["approve_cta"]["primary"] is True
    assert payload["approve_cta"]["selection"]["action"] == "approve_plan"
    assert payload["reject_cta"]["selection"]["action"] == "reject_plan"
    labels = [r["label"] for r in payload["summary_rows"]]
    assert "Distance" in labels and "Route" in labels
    distance_row = next(r for r in payload["summary_rows"] if r["label"] == "Distance")
    assert "calculated_estimate" in distance_row["value"] or "external_api" in distance_row["value"]


# --------------------------------------------------------------------------
# selection parsing
# --------------------------------------------------------------------------


def test_json_direct_selection():
    text = json.dumps({"action": "select_recipient", "recipient_id": "rcp_03"})
    assert parse_selection(text) == {"action": "select_recipient", "recipient_id": "rcp_03"}


def test_form_submission_numbers_normalised():
    text = json.dumps({"action": "submit_donation", **valid_selection(), "quantity": 12})
    parsed = parse_selection(text)
    assert parsed["action"] == "submit_donation"
    assert parsed["quantity"] == 12.0
    assert isinstance(parsed["description"], str)


def test_json_embedded_in_prose():
    text = 'The user selected the recipient {"action": "select_recipient", "recipient_id": "rcp_05"} via the card.'
    parsed = parse_selection(text)
    assert parsed.get("action") == "select_recipient"
    assert parsed.get("recipient_id") == "rcp_05"


def test_planner_prose_with_ids():
    text = "The user picked recipient rcp_02 and window w3 and approved the plan."
    parsed = parse_selection(text)
    assert parsed["recipient_id"] == "rcp_02"
    assert parsed["window_id"] == "w3"
    assert parsed["action"] == "approve_plan"


def test_prose_negation_of_approve():
    text = "The user chose to not approve the offer."
    parsed = parse_selection(text)
    assert parsed.get("action") == "reject_plan"


def test_unknown_text_yields_empty():
    assert parse_selection("tell me a joke, robot") == {}


def test_prose_status_and_donation():
    assert parse_selection("what's the status of my donation?")["action"] == "check_status"
    assert parse_selection("The user submitted the donation form.")["action"] == "submit_donation"
