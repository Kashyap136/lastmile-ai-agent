"""Ranked recipient-match models with explainable scoring."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from .common import DataSource
from .recipient import RecipientProfile


class ScoreBreakdown(BaseModel):
    """Named score components so ranking is explainable, not a black box."""

    eligibility: float = 0.0  # 0..1 category / storage / notice constraints
    capacity_fit: float = 0.0  # 0..1 how well the quantity fits daily capacity
    food_suitability: float = 0.0  # 0..1 specialisation bonus
    proximity: float = 0.0  # 0..1 closer is better
    deadline_feasibility: float = 0.0  # 0..1 rows can take it in time

    @property
    def total(self) -> float:
        return round(
            0.35 * self.eligibility
            + 0.20 * self.capacity_fit
            + 0.15 * self.food_suitability
            + 0.15 * self.proximity
            + 0.15 * self.deadline_feasibility,
            4,
        )


class RecipientMatch(BaseModel):
    recipient: RecipientProfile
    score: ScoreBreakdown
    rank: int = 0
    reasons: list[str] = []
    estimated_distance_km: float | None = None
    distance_source: DataSource = DataSource.CALCULATED_ESTIMATE

    def explanation(self) -> str:
        parts = [
            f"eligibility={self.score.eligibility:.2f}",
            f"capacity={self.score.capacity_fit:.2f}",
            f"food_fit={self.score.food_suitability:.2f}",
            f"proximity={self.score.proximity:.2f}",
            f"deadline_ok={self.score.deadline_feasibility:.2f}",
        ]
        return f"score {self.score.total:.2f} ({', '.join(parts)})"


class MatchResult(BaseModel):
    """Full result of a matching run."""

    donation_id: str
    matches: list[RecipientMatch] = []
    considered_count: int = 0
    no_match_reasons: list[str] = []
    data_source: DataSource = DataSource.DEMO


class MatchRequest(BaseModel):
    donation_id: str
    payload: dict[str, Any]


class MatchResponse(BaseModel):
    request_id: str
    ok: bool
    error: str = ""
    result: MatchResult | None = None
