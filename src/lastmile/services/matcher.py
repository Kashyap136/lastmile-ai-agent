"""Recipient Matcher service - explainable, deterministic matching.

Considers recipient eligibility, capacity, food suitability, distance and
pickup deadlines. Every candidate keeps an explanation; ineligible ones
keep their rejection reason so 'no match' is explainable, not silent.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ..models.common import DataSource, FoodCategory
from ..models.donation import DonationIntake
from ..models.matching import MatchResult, RecipientMatch, ScoreBreakdown
from ..models.recipient import RecipientProfile
from ..validation.rules import SafetyRules
from ..adapters.routing import haversine_km

_MAX_PROXIMITY_KM = 15.0
_FOOD_SUITABILITY: dict[tuple[FoodCategory, str], float] = {
    (FoodCategory.PREPARED_MEAL, "soup_kitchen"): 1.0,
    (FoodCategory.PREPARED_MEAL, "senior_center"): 1.0,
    (FoodCategory.PREPARED_MEAL, "shelter"): 0.8,
    (FoodCategory.PREPARED_MEAL, "food_bank"): 0.7,
    (FoodCategory.PREPARED_MEAL, "community_fridge"): 0.7,
    (FoodCategory.PRODUCE, "food_bank"): 0.9,
    (FoodCategory.PACKAGED_CANNED, "food_bank"): 0.9,
    (FoodCategory.PACKAGED_CANNED, "shelter"): 0.8,
    (FoodCategory.BAKERY, "shelter"): 0.9,
    (FoodCategory.BAKERY, "soup_kitchen"): 0.8,
    (FoodCategory.DAIRY, "senior_center"): 0.9,
    (FoodCategory.DAIRY, "soup_kitchen"): 0.8,
    (FoodCategory.BEVERAGES, "shelter"): 0.8,
    (FoodCategory.BEVERAGES, "food_bank"): 0.8,
}


class RecipientMatcher:
    """Ranks the recipient dataset for a donation."""

    def __init__(
        self,
        recipients: list[RecipientProfile],
        rules: SafetyRules | None = None,
        donor_zone_centers: dict[str, tuple[float, float]] | None = None,
    ) -> None:
        self._recipients = recipients
        self._rules = rules or SafetyRules()
        self._donor_centers = donor_zone_centers or {}

    def match(
        self,
        intake: DonationIntake,
        max_results: int = 3,
        now: datetime | None = None,
    ) -> MatchResult:
        now = now or datetime.now(timezone.utc)
        candidates: list[RecipientMatch] = []
        considered = 0
        ineligibility_reasons: dict[str, str] = {}

        donor_center = self._donor_centers.get(_zone_key(intake.zone))

        for recipient in self._recipients:
            considered += 1
            reason = self._eligibility_failure(recipient, intake, now)
            if reason:
                ineligibility_reasons[recipient.recipient_id] = reason
                continue

            distance_km = self._distance_km(donor_center, recipient)

            score = ScoreBreakdown(
                eligibility=1.0,
                capacity_fit=self._capacity_fit(recipient, intake),
                food_suitability=self._food_suitability(recipient, intake),
                proximity=self._proximity(distance_km),
                deadline_feasibility=self._deadline_feasibility(recipient, intake, now),
            )
            candidates.append(
                RecipientMatch(
                    recipient=recipient,
                    score=score,
                    reasons=self._match_reasons(recipient, intake, score, distance_km, now),
                    estimated_distance_km=distance_km,
                )
            )

        candidates.sort(key=lambda m: (-m.score.total, m.recipient.recipient_id))
        for rank, match in enumerate(candidates[:max_results], start=1):
            match.rank = rank

        no_match_reasons: list[str] = []
        if not candidates:
            no_match_reasons = _summarize_no_match(ineligibility_reasons, considered)

        return MatchResult(
            donation_id=intake.donation_id,
            matches=candidates[:max_results],
            considered_count=considered,
            no_match_reasons=no_match_reasons,
            data_source=DataSource.DEMO,
        )

    # ----- eligibility (hard filters) -------------------------------------

    def _eligibility_failure(
        self, recipient: RecipientProfile, intake: DonationIntake, now: datetime
    ) -> str:
        if intake.category not in recipient.accepts_categories:
            return (
                f"does not accept {intake.category.value} "
                f"(accepts: {', '.join(c.value for c in recipient.accepts_categories)})"
            )
        if intake.storage.value in ("ambient",) and not recipient.accepts_ambient:
            return "cannot accept ambient-stored food (refrigeration capacity only)"
        if intake.quantity > recipient.capacity_units:
            return (
                f"quantity {intake.quantity:g} exceeds capacity "
                f"{recipient.capacity_units:g} {intake.unit.value}"
            )
        deadline_notice = (intake.pickup_deadline - now).total_seconds() / 60.0
        if deadline_notice < recipient.min_notice_minutes:
            return (
                f"needs at least {recipient.min_notice_minutes} min notice; deadline "
                f"gives {deadline_notice:.0f} min"
            )
        return ""

    # ----- scoring components ----------------------------------------------

    @staticmethod
    def _capacity_fit(recipient: RecipientProfile, intake: DonationIntake) -> float:
        ratio = intake.quantity / max(recipient.capacity_units, 1.0)
        return round(max(0.0, 1.0 - ratio), 3)

    @staticmethod
    def _food_suitability(recipient: RecipientProfile, intake: DonationIntake) -> float:
        base = _FOOD_SUITABILITY.get((intake.category, recipient.org_type), 0.5)
        # slight boost when the category is the org's primary specialisation
        if recipient.accepts_categories[:1] == [intake.category]:
            base = min(1.0, base + 0.1)
        return round(base, 3)

    @staticmethod
    def _proximity(distance_km: float | None) -> float:
        if distance_km is None:
            return 0.4  # unknown distance: neutral-low, never fabricated as better
        return round(max(0.0, 1.0 - distance_km / _MAX_PROXIMITY_KM), 3)

    @staticmethod
    def _deadline_feasibility(
        recipient: RecipientProfile, intake: DonationIntake, now: datetime
    ) -> float:
        deadline_minutes = (intake.pickup_deadline - now).total_seconds() / 60.0
        if deadline_minutes < recipient.min_notice_minutes:
            return 0.0
        # comfortable when notice equals or exceeds twice their minimum
        if deadline_minutes >= 2 * recipient.min_notice_minutes:
            return 1.0
        return 0.6

    def _distance_km(
        self, donor_center: tuple[float, float] | None, recipient: RecipientProfile
    ) -> float | None:
        if donor_center is None:
            return None
        return round(
            haversine_km(donor_center[0], donor_center[1], recipient.latitude, recipient.longitude),
            2,
        )

    def _match_reasons(
        self,
        recipient: RecipientProfile,
        intake: DonationIntake,
        score: ScoreBreakdown,
        distance_km: float | None,
        now: datetime,
    ) -> list[str]:
        reasons = [
            f"accepts {intake.category.value}",
            f"capacity {recipient.capacity_units:g} {intake.unit.value} vs offer "
            f"{intake.quantity:g}",
        ]
        if distance_km is not None:
            reasons.append(f"~{distance_km:g} km from pickup zone")
        else:
            reasons.append("distance unknown (pickup zone not in demo layout)")
        slack = (intake.pickup_deadline - now).total_seconds() / 60.0
        if slack >= 2 * recipient.min_notice_minutes:
            reasons.append("deadline leaves comfortable scheduling slack")
        elif slack >= recipient.min_notice_minutes:
            reasons.append("deadline meets their minimum notice, little slack")
        return reasons


def _zone_key(zone: str) -> str:
    return (zone or "").strip()


def _summarize_no_match(ineligibility: dict[str, str], considered: int) -> list[str]:
    summary = [f"{considered} recipient organisations in the demo dataset were checked."]
    distinct = list(dict.fromkeys(ineligibility.values()))
    for reason in distinct[:5]:
        summary.append(f"- some recipients excluded: {reason}")
    return summary
