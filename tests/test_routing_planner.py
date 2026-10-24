"""Tests for routing estimates, OSRM adapter behaviour and labelling."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from lastmile.adapters.routing import estimate_between, haversine_km
from lastmile.models.common import DataSource, FoodCategory, QuantityUnit, StorageType
from lastmile.models.donation import DonationIntake
from lastmile.models.recipient import RecipientProfile
from lastmile.services import PickupPlanner


def test_haversine_known_distance():
    # ~1.0-1.5 km inside the demo grid
    km = haversine_km(40.7220, -74.0020, 40.7300, -74.0010)
    assert 0.5 < km < 2.5


def test_estimate_without_api_is_labelled_estimate():
    result = estimate_between(40.7220, -74.0020, 40.7300, -74.0010)
    assert result.error == ""
    assert result.estimate.data_source == DataSource.CALCULATED_ESTIMATE
    assert "estimate" in result.estimate.note
    assert result.estimate.duration_minutes > 0


def test_estimate_without_donor_coords_is_explicit():
    result = estimate_between(None, None, 40.73, -74.00)
    assert result.error
    assert result.estimate.note


class _FakeResponse:
    def __init__(self, status_code=200, payload=None, raises=False):
        self.status_code = status_code
        self.payload = payload or {}
        self.raises = raises

    def json(self):
        return self.payload


def test_osrm_success(monkeypatch):
    import lastmile.adapters.routing as routing

    def fake_get(url, timeout):
        assert "/route/v1/driving/" in url
        return _FakeResponse(
            200,
            {"routes": [{"distance": 2500.0, "duration": 420.0}]},
        )

    monkeypatch.setattr(routing.requests, "get", fake_get)
    result = estimate_between(
        40.7220, -74.0020, 40.7300, -74.0010,
        routing_api_url="https://api.example-osrm.test",
    )
    assert result.estimate.data_source == DataSource.EXTERNAL_API
    assert result.estimate.distance_km == 2.5
    assert result.estimate.duration_minutes == 7.0
    assert result.error == ""


def test_osrm_failure_falls_back_with_explicit_note(monkeypatch):
    import lastmile.adapters.routing as routing

    def fake_get(url, timeout):
        return _FakeResponse(503)

    monkeypatch.setattr(routing.requests, "get", fake_get)
    result = estimate_between(
        40.7220, -74.0020, 40.7300, -74.0010,
        routing_api_url="https://api.example-osrm.test",
    )
    assert result.error
    assert "fell back" in result.error
    assert result.estimate.data_source == DataSource.CALCULATED_ESTIMATE


def test_osrm_no_route(monkeypatch):
    import lastmile.adapters.routing as routing

    monkeypatch.setattr(routing.requests, "get", lambda url, timeout: _FakeResponse(200, {"routes": []}))
    result = estimate_between(
        40.7220, -74.0020, 40.7300, -74.0010,
        routing_api_url="https://api.example-osrm.test",
    )
    assert "no route found" in result.error


def _intake(zone="Riverside", hours=4):
    now = datetime.now(timezone.utc)
    return DonationIntake(
        donation_id="don_test",
        category=FoodCategory.PREPARED_MEAL,
        description="40 fresh vegan noodle soup bowls",
        quantity=40.0,
        unit=QuantityUnit.MEALS,
        prepared_at=now - timedelta(minutes=45),
        pickup_deadline=now + timedelta(hours=hours),
        location="42 Food Street, Fairview (demo)",
        zone=zone,
        storage=StorageType.REFRIGERATED,
    )


def _recipient(rid="rcp_x", windows=(("00:00", "23:59"),)):
    return RecipientProfile(
        recipient_id=rid,
        name="Test Kitchen (demo)",
        org_type="soup_kitchen",
        address_text="12 Test Street",
        zone="Riverside",
        latitude=40.7251,
        longitude=-74.0105,
        capacity_units=120,
        accepts_categories=["prepared_meal", "bakery", "produce", "dairy"],
        min_notice_minutes=30,
        service_windows=list(windows),
    )


def test_planner_proposes_windows_with_labels():
    planner = PickupPlanner([_recipient()], donor_zone_centers={"Riverside": (40.7265, -74.0118)})
    proposal, error = planner.propose(_intake(hours=6), "rcp_x")
    assert error == ""
    assert proposal is not None and proposal.windows
    for window in proposal.windows:
        assert window.label, "window labels must travel with the cards"
    window_ids = [w.window_id for w in proposal.windows]
    assert window_ids[0] == "w1"


def test_planner_infeasible_when_deadline_too_tight():
    import datetime as dt

    planner = PickupPlanner([_recipient()])
    tight = _intake()
    tight.pickup_deadline = datetime.now(timezone.utc) + dt.timedelta(minutes=20)
    proposal, error = planner.propose(tight, "rcp_x")
    assert proposal is None
    assert error


def test_planner_builds_reviewable_plan_with_estimate_label():
    planner = PickupPlanner(
        [_recipient()],
        donor_zone_centers={"Riverside": (40.7265, -74.0118)},
    )
    proposal, _ = planner.propose(_intake(), "rcp_x")
    plan, error = planner.build_plan(_intake(), proposal, proposal.windows[0].window_id)
    assert error == "" and plan is not None
    assert plan.route_summary
    assert plan.travel.data_source == DataSource.CALCULATED_ESTIMATE
    assert "estimate" in plan.travel.note


def test_planner_unknown_recipient():
    planner = PickupPlanner([_recipient()])
    proposal, error = planner.propose(_intake(), "rcp_missing")
    assert proposal is None and "unknown recipient" in error


def test_planner_uses_real_api_when_configured(monkeypatch):
    import lastmile.adapters.routing as routing

    def fake_get(url, timeout):
        return _FakeResponse(200, {"routes": [{"distance": 1800.0, "duration": 360.0}]})

    monkeypatch.setattr(routing.requests, "get", fake_get)
    planner = PickupPlanner(
        [_recipient()],
        donor_zone_centers={"Riverside": (40.7265, -74.0118)},
        routing_api_url="https://api.example-osrm.test",
    )
    proposal, _ = planner.propose(_intake(), "rcp_x")
    assert "routing API" in proposal.windows[0].label
