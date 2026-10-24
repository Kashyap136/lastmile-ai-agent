"""Structured safety-decision models.

Safety decisions are explicit and explainable: reasons list every rule
that fired. The validator never invents certifications - unknown or
unverifiable cases are rejected with a reason.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from .common import DataSource


class SafetyDecision(BaseModel):
    """Result of the Safety Validator."""

    approved: bool
    reasons: list[str]
    rule_ids: list[str] = []
    checked_rules: int = 0
    data_source: DataSource = DataSource.CALCULATED_ESTIMATE

    @property
    def reason_bullets(self) -> list[str]:
        return self.reasons or ["no rules triggered (no reason recorded)"]


class SafetyCheckRequest(BaseModel):
    donation_id: str
    payload: dict[str, Any]


class SafetyCheckResponse(BaseModel):
    request_id: str
    ok: bool  # transport-level success of the specialist call
    error: str = ""
    decision: SafetyDecision | None = None
