"""Pickup-plan models with explicit provenance labels.

Every distance/duration value carries a DataSource so the user always
sees whether it came from a real routing API or a clearly-labelled
calculated estimate.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field

from .common import DataSource


class TravelEstimate(BaseModel):
    """Distance/duration estimate with provenance."""

    distance_km: float
    duration_minutes: float
    data_source: DataSource = DataSource.CALCULATED_ESTIMATE
    note: str = ""


class PickupWindow(BaseModel):
    """One feasible pickup window proposed by the planner."""

    window_id: str = Field(default_factory=lambda: f"w{uuid4().hex[:6]}")
    start: datetime
    end: datetime
    fits_deadline: bool = True
    label: str = ""


class PickupProposal(BaseModel):
    """Result of the Pickup Planner: ranked feasible windows for one recipient."""

    donation_id: str
    recipient_id: str
    recipient_name: str
    windows: list[PickupWindow] = []

    def window_by_id(self, window_id: str) -> PickupWindow | None:
        return next((w for w in self.windows if w.window_id == window_id), None)


class PickupPlan(BaseModel):
    """A concrete proposed pickup for user approval."""

    plan_id: str = Field(default_factory=lambda: f"plan_{uuid4().hex[:8]}")
    donation_id: str
    recipient_id: str
    recipient_name: str
    window: PickupWindow
    travel: TravelEstimate
    route_summary: str
    handling_instructions: str = ""
    data_source: DataSource = DataSource.DEMO
    proposed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class PickupRequest(BaseModel):
    donation_id: str
    payload: dict[str, Any]


class PickupResponse(BaseModel):
    request_id: str
    ok: bool
    error: str = ""
    proposal: PickupProposal | None = None
