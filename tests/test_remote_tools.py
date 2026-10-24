"""Tests for remote specialist tools: timeouts, bounded retries, explicit failures."""

from __future__ import annotations

import asyncio
import json

import pytest

from lastmile.models.common import FoodCategory, QuantityUnit, StorageType, DataSource
from lastmile.models.donation import DonationIntake
from lastmile.models.safety import SafetyDecision
from lastmile.workflow import LocalSpecialistTools, RemoteSpecialistTools, SpecialistError
from lastmile.services import SafetyValidatorService
from lastmile.validation.rules import SafetyRules


def _intake():
    now = datetime_now()
    return DonationIntake(
        donation_id="don_test",
        category=FoodCategory.PREPARED_MEAL,
        description="40 fresh vegan noodle soup bowls",
        quantity=40.0,
        unit=QuantityUnit.MEALS,
        prepared_at=now - timedelta(minutes=45),
        pickup_deadline=now + timedelta(hours=4),
        location="42 Food Street, Fairview (demo)",
        zone="Riverside",
        storage=StorageType.REFRIGERATED,
    )


def datetime_now():
    from datetime import datetime, timedelta, timezone

    return datetime.now(timezone.utc)


from datetime import timedelta  # noqa: E402


def _ok_response(kind: str) -> dict:
    decision = SafetyDecision(approved=True, reasons=[], checked_rules=8)
    return {"ok": True, "result_json": decision.model_dump_json(), "error": ""}


class FlakyBus:
    """Record calls and drop the first N requests (simulated timeouts)."""

    def __init__(self, fail_first=0, responses=None):
        self.calls = 0
        self.fail_first = fail_first
        self.responses = responses or {}

    async def __call__(self, specialist: str, payload_json: str):
        self.calls += 1
        if self.calls <= self.fail_first:
            return None  # transport timeout
        return self.responses.get(specialist, _ok_response(specialist))


async def test_remote_success():
    bus = FlakyBus()
    tools = RemoteSpecialistTools(bus, timeout_seconds=1, retries=2)
    decision = await tools.check_safety(_intake())
    assert decision.approved
    assert bus.calls == 1


async def test_bounded_retries_then_explicit_failure():
    bus = FlakyBus(fail_first=99)
    tools = RemoteSpecialistTools(bus, timeout_seconds=0.01, retries=2, fallback=None)
    with pytest.raises(SpecialistError) as err:
        await tools.check_safety(_intake())
    assert bus.calls == 3  # 1 + retries
    assert "did not respond" in str(err.value)


async def test_recovers_after_transient_timeouts():
    bus = FlakyBus(fail_first=1)
    tools = RemoteSpecialistTools(bus, timeout_seconds=1, retries=2)
    decision = await tools.check_safety(_intake())
    assert decision.approved and bus.calls == 2


async def test_explicit_agent_error_raises_without_retry():
    bus = FlakyBus(responses={"safety": {"ok": False, "error": "rule engine exploded", "result_json": ""}})
    tools = RemoteSpecialistTools(bus, timeout_seconds=1, retries=2)
    with pytest.raises(SpecialistError) as err:
        await tools.check_safety(_intake())
    assert "rule engine exploded" in str(err.value)
    assert bus.calls == 1  # no retry on explicit errors


async def test_labelled_local_fallback_when_unreachable():
    bus = FlakyBus(fail_first=99)
    fallback = LocalSpecialistTools(
        SafetyValidatorService(rules=SafetyRules()), None, None
    )
    tools = RemoteSpecialistTools(bus, timeout_seconds=0.01, retries=1, fallback=fallback)
    decision = await tools.check_safety(_intake())
    assert decision.approved
    assert any("[fallback]" in r for r in decision.reasons)


# matcher + planner behaviour -------------------------------------------------


async def test_matcher_remote_success():
    from lastmile.models.matching import MatchResult

    bus = FlakyBus(responses={
        "matcher": {
            "ok": True,
            "result_json": MatchResult(donation_id="don_test", matches=[]).model_dump_json(),
        }
    })
    tools = RemoteSpecialistTools(bus, timeout_seconds=1, retries=2)
    result = await tools.find_recipients(_intake())
    assert result.donation_id == "don_test"
    assert result.matches == []


async def test_planner_infeasible_response_carries_reason():
    bus = FlakyBus(responses={
        "planner": {
            "ok": True,
            "result_json": "null",
            "detail": "recipient has no service window before your deadline and after the required scheduling notice",
        }
    })
    tools = RemoteSpecialistTools(bus, timeout_seconds=1, retries=2)
    proposal, error = await tools.propose_windows(_intake(), "rcp_01")
    assert proposal is None
    assert "no service window" in error


# wire contract ----------------------------------------------------------------


async def test_outbound_payload_matches_specialist_schema():
    """The JSON shape RemoteSpecialistTools sends must parse as the
    SpecialistRequestPayload schema the specialist agents expect."""
    from lastmile.protocols import SpecialistRequestPayload
    from lastmile.models.donation import intake_from_json

    captured: dict[str, str | None] = {}

    async def capture(specialist: str, payload_json: str):
        captured["specialist"] = specialist
        captured["payload_json"] = payload_json
        return None  # send the flow to the explicit-failure branch fast

    tools = RemoteSpecialistTools(capture, timeout_seconds=0.01, retries=0, fallback=None)
    intake = _intake()

    # safety + matcher raise SpecialistError when all attempts fail
    for expect, method_call in (
        ("safety", tools.check_safety),
        ("matcher", tools.find_recipients),
    ):
        captured.clear()
        with pytest.raises(SpecialistError):
            await method_call(intake)
        assert captured["specialist"] == expect
        payload = SpecialistRequestPayload.parse_raw(captured["payload_json"])
        assert payload.donation_json
        # the donation JSON must survive a full round-trip into the
        # same parser the specialist agents use
        rebuilt, errors = intake_from_json(payload.donation_json)
        assert rebuilt is not None and not errors
        assert rebuilt.description == intake.description
        assert rebuilt.category == intake.category


def _plan_stub():
    from lastmile.models.pickup import PickupPlan, PickupWindow, TravelEstimate

    return PickupPlan(
        plan_id="plan_stub",
        donation_id="don_test",
        recipient_id="rcp_02",
        recipient_name="Stub Recipient (demo)",
        window=PickupWindow(window_id="w3", start=datetime_now(), end=datetime_now() + timedelta(minutes=40)),
        travel=TravelEstimate(distance_km=1.0, duration_minutes=4.0, data_source=DataSource.CALCULATED_ESTIMATE),
        route_summary="calculated_estimate: 1 km, ~4 min",
        route_source="calculated_estimate",
    )


async def test_planner_payload_carries_window_and_proposal():
    from lastmile.protocols import SpecialistRequestPayload
    from lastmile.models.pickup import PickupProposal

    captured: dict[str, str | None] = {}

    async def capture(specialist: str, payload_json: str):
        captured["payload_json"] = payload_json
        return None

    tools = RemoteSpecialistTools(capture, timeout_seconds=0.01, retries=0, fallback=None)
    plan, error = await tools.build_plan(_intake(), _plan_stub(), "w3")
    assert plan is None and "did not respond" in error
    payload = SpecialistRequestPayload.parse_raw(captured["payload_json"])
    assert payload.window_id == "w3"
    assert payload.recipient_id == "rcp_02"
    proposal_payload = PickupProposal.parse_raw(payload.proposal_json)
    assert proposal_payload.recipient_id == "rcp_02"


async def test_matcher_payload_carries_recipient_hint():
    from lastmile.protocols import SpecialistRequestPayload

    captured: dict[str, str | None] = {}

    async def capture(specialist: str, payload_json: str):
        captured["payload_json"] = payload_json
        return None

    tools = RemoteSpecialistTools(capture, timeout_seconds=0.01, retries=0, fallback=None)
    proposal, error = await tools.propose_windows(_intake(), "rcp_07")
    assert proposal is None and "did not respond" in error
    payload = SpecialistRequestPayload.parse_raw(captured["payload_json"])
    assert payload.recipient_id == "rcp_07"
