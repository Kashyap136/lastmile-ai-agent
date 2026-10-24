"""End-to-end workflow engine tests (single-agent logic, no network)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from lastmile.models.common import DonationStatus, WorkflowStage
from lastmile.workflow import SessionState
from lastmile.workflow.engine import EngineOutput

from .conftest import valid_selection


async def run(engine, state, text):
    return await engine.handle_text(state, text)


def last_card(result: EngineOutput):
    cards = [o for o in result.outputs if o.has_card()]
    return cards[-1] if cards else None


async def test_greeting_shows_form_card(engine):
    state = SessionState(user_address="user1")
    result = await run(engine, state, "hi")
    assert state.stage == WorkflowStage.AWAITING_DONATION_FORM
    card = last_card(result)
    assert card is not None and card.card_kind == "form"
    assert result.outputs[0].text


async def test_incomplete_submission_preserves_partial_and_explains(engine):
    state = SessionState(user_address="user1")
    await run(engine, state, "hi")
    selection = valid_selection()
    del selection["pickup_deadline"]
    selection["quantity"] = 40
    result = await run(engine, state, json.dumps(selection))
    state2 = result.state
    assert state2.stage == WorkflowStage.AWAITING_DONATION_FORM
    assert "pickup_deadline" not in state2.partial  # it was never provided
    assert "category" in state2.partial  # everything else is kept
    text = "\n".join(o.text for o in result.outputs)
    assert "missing field: pickup_deadline" in text
    card = last_card(result)
    assert card.card_kind == "form"


async def test_invalid_submission_reports_field_errors(engine):
    state = SessionState(user_address="user1")
    await run(engine, state, "hi")
    selection = valid_selection()
    selection["category"] = "cold fusion soup"
    selection["quantity"] = "-3"
    result = await run(engine, state, json.dumps(selection))
    text = "\n".join(o.text for o in result.outputs)
    assert "invalid field: category" in text
    assert "invalid field: quantity" in text
    assert result.state.stage == WorkflowStage.AWAITING_DONATION_FORM


async def test_full_happy_flow(engine):
    state = SessionState(user_address="user1")
    await run(engine, state, "hi")

    # submit the donation form
    result = await run(engine, state, json.dumps(valid_selection()))
    assert state.stage == WorkflowStage.AWAITING_RECIPIENT_CHOICE
    assert state.current_status() == DonationStatus.MATCHED
    card = last_card(result)
    assert card.card_kind == "carousel"
    items = card.card_payload["items"]
    assert items, "expected at least one ranked match"

    # select the top-ranked recipient
    chosen = items[0]["primary_cta"]["selection"]["recipient_id"]
    result = await run(engine, state, json.dumps({"action": "select_recipient", "recipient_id": chosen}))
    assert state.stage == WorkflowStage.AWAITING_WINDOW_CHOICE
    card = last_card(result)
    assert card.card_kind == "detail"
    choices = card.card_payload["sub_options"]["choices"]
    assert choices, "planner should propose feasible windows"
    window_id = choices[0]["value"]

    # choose the window -> review card
    result = await run(engine, state, json.dumps({"action": "select_window", "window_id": window_id}))
    assert state.stage == WorkflowStage.AWAITING_PLAN_REVIEW
    assert state.current_status() == DonationStatus.PROPOSED
    card = last_card(result)
    assert card.card_kind == "review"

    # approve -> confirmation + honest demo notification status
    result = await run(engine, state, json.dumps({"action": "approve_plan"}))
    assert state.stage == WorkflowStage.DONE
    assert state.current_status() == DonationStatus.AWAITING_RECIPIENT_CONFIRMATION
    receipts = "\n".join(o.text for o in result.outputs)
    assert "awaiting_recipient_confirmation" in receipts or "nobody was contacted" in receipts

    # picked up closes it
    result = await run(engine, state, "picked up")
    assert state.current_status() == DonationStatus.PICKED_UP


async def test_picked_up_before_approval_is_refused(engine):
    state = SessionState(user_address="user1")
    await run(engine, state, "hi")
    await run(engine, state, json.dumps(valid_selection()))
    result = await run(engine, state, "picked up")
    text = "\n".join(o.text for o in result.outputs)
    assert "can only be marked complete" in text
    assert state.current_status() == DonationStatus.MATCHED


async def test_no_match_reports_honestly(engine):
    # 70 crates of beverages, deadline in one hour: every candidate is either
    # over capacity or outside the required scheduling notice.
    state = SessionState(user_address="user1")
    await run(engine, state, "hi")
    selection = valid_selection()
    selection["category"] = "beverages"
    selection["unit"] = "crates"
    selection["quantity"] = "70"
    selection["storage"] = "ambient"
    now = datetime.now(timezone.utc)
    selection["prepared_at"] = (now - timedelta(minutes=90)).isoformat()
    selection["pickup_deadline"] = (now + timedelta(minutes=60)).isoformat()
    selection["description"] = "70 sealed beverage crates"
    result = await run(engine, state, json.dumps(selection))
    assert state.stage == WorkflowStage.DONE
    assert state.current_status() == DonationStatus.NO_MATCH
    text = "\n".join(o.text for o in result.outputs)
    assert "No eligible recipient" in text or "no eligible" in text.lower()
    assert "checked" in text  # considered-count explanation


async def test_unsafe_donation_is_rejected_with_reasons(engine):
    state = SessionState(user_address="user1")
    await run(engine, state, "hi")
    selection = valid_selection()
    selection["category"] = "raw_meat_or_fish"
    selection["storage"] = "frozen"
    selection["description"] = "40 kg of fresh tuna"
    selection["unit"] = "kg"
    selection["quantity"] = "40"
    result = await run(engine, state, json.dumps(selection))
    text = "\n".join(o.text for o in result.outputs)
    assert "REJECTED" in text
    assert "R01_category_policy" in text
    assert state.current_status() == DonationStatus.REJECTED_BY_SAFETY
    assert state.stage == WorkflowStage.AWAITING_DONATION_FORM
    # nothing was matched or shared
    assert state.match_result is None


async def test_cancel_mid_flow(engine):
    state = SessionState(user_address="user1")
    await run(engine, state, "hi")
    await run(engine, state, json.dumps(valid_selection()))
    result = await run(engine, state, "cancel")
    assert state.current_status() == DonationStatus.CANCELLED
    assert state.stage == WorkflowStage.DONE
    text = "\n".join(o.text for o in result.outputs)
    assert "CANCELLED" in text


async def test_status_query_shows_trail(engine):
    state = SessionState(user_address="user1")
    await run(engine, state, "hi")
    await run(engine, state, json.dumps(valid_selection()))
    result = await run(engine, state, "status")
    text = "\n".join(o.text for o in result.outputs)
    assert "#don_" in text and "status trail" in text
    assert "matched" in text


async def test_deadline_expiry(engine):
    state = SessionState(user_address="user1")
    await run(engine, state, "hi")
    result = await run(engine, state, json.dumps(valid_selection()))
    assert state.stage == WorkflowStage.AWAITING_RECIPIENT_CHOICE
    # simulate the deadline passing while the user is away
    state.donation["pickup_deadline"] = (
        datetime.now(timezone.utc) - timedelta(seconds=5)
    ).isoformat()
    result = await run(engine, state, "status")
    assert state.current_status() == DonationStatus.EXPIRED
    assert state.stage == WorkflowStage.DONE


async def test_reject_plan_returns_to_recipient_choice(engine):
    state = SessionState(user_address="user1")
    await run(engine, state, "hi")
    await run(engine, state, json.dumps(valid_selection()))
    card_result = await run(engine, state, json.dumps({"action": "select_recipient", "recipient_id": "rcp_01"}))
    card = last_card(card_result)
    window_id = card.card_payload["sub_options"]["choices"][0]["value"]
    await run(engine, state, json.dumps({"action": "select_window", "window_id": window_id}))
    result = await run(engine, state, json.dumps({"action": "reject_plan"}))
    assert state.current_status() == DonationStatus.DECLINED
    assert state.stage == WorkflowStage.AWAITING_RECIPIENT_CHOICE


async def test_unknown_recipient_rejected(engine):
    state = SessionState(user_address="user1")
    await run(engine, state, "hi")
    await run(engine, state, json.dumps(valid_selection()))
    result = await run(engine, state, json.dumps({"action": "select_recipient", "recipient_id": "rcp_999"}))
    text = "\n".join(o.text for o in result.outputs)
    assert "not one of the proposed recipients" in text


async def test_new_donation_after_completion(engine):
    state = SessionState(user_address="user1")
    await run(engine, state, "hi")
    await run(engine, state, json.dumps(valid_selection()))
    # explicit mid-flow restart command
    result = await run(engine, state, "start over")
    assert state.donation is None
    assert state.stage == WorkflowStage.AWAITING_DONATION_FORM
    assert state.last_summary  # archived
    assert "don_" in state.last_summary

