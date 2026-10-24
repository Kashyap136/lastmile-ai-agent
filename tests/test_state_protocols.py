"""Tests for session state TTL cleanup and specialist wire schemas."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from lastmile.models.common import DonationStatus
from lastmile.protocols import SpecialistRequest, SpecialistRequestPayload, SpecialistResponse
from lastmile.workflow import SessionState, cleanup_expired_sessions


def test_state_round_trip_is_jsonable():
    state = SessionState(user_address="user1")
    state.record(DonationStatus.RECEIVED, "intake complete")
    dumped = state.model_dump_json()
    restored = SessionState.model_validate(json.loads(dumped))
    assert restored.user_address == state.user_address
    assert restored.history[0].status == state.history[0].status


def test_session_ttl_cleanup():
    fresh = SessionState(user_address="fresh")
    stale = SessionState(user_address="stale")
    stale.updated_at = datetime.now(timezone.utc) - timedelta(hours=7)
    store = {"fresh": fresh, "stale": stale}
    removed = cleanup_expired_sessions(store, ttl_seconds=6 * 60 * 60)
    assert removed == ["stale"]
    assert "fresh" in store and "stale" not in store


def test_specialist_wire_models_round_trip():
    payload = SpecialistRequestPayload(
        donation_json=json.dumps({"donation_id": "don_1"}),
        recipient_id="rcp_01",
    )
    request = SpecialistRequest(kind="planner", request_id="abc", payload_json=payload.model_dump_json())
    response = SpecialistResponse(request_id=request.request_id, ok=True, result_json="null", detail="no windows")

    assert SpecialistRequest.parse_raw(request.model_dump_json()).request_id == "abc"
    assert SpecialistResponse.parse_raw(response.model_dump_json()).detail == "no windows"
