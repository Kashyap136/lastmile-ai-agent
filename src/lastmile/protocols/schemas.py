"""Wire models for coordinator <-> specialist agent messaging.

Carried as JSON strings so they stay compatible with the pydantic-v1
Model base used by uAgents while all rich typing remains on each side
(both ends parse back into typed v2 domain models).
"""

from __future__ import annotations

from uagents import Model


class SpecialistRequest(Model):
    kind: str  # safety | matcher | planner
    request_id: str
    payload_json: str  # SpecialistRequestPayload in JSON


class SpecialistResponse(Model):
    request_id: str
    ok: bool
    error: str = ""
    result_json: str = ""  # typed result in JSON (null means "no result")
    detail: str = ""  # human-readable note (e.g. planner infeasibility reason)
    fallback: bool = False  # specialist had to use its local fallback path


class SpecialistRequestPayload(Model):
    donation_json: str
    recipient_id: str = ""
    proposal_json: str = ""
    window_id: str = ""


class StatusRequest(Model):
    request_id: str
    note: str = ""


class PingRequest(Model):
    request_id: str


class HealthResponse(Model):
    request_id: str
    healthy: bool = True
    service: str = ""
