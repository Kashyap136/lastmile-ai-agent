"""Safety Validator service - thin, testable service over the rule engine."""

from __future__ import annotations

from datetime import datetime, timezone

from ..models.donation import DonationIntake
from ..models.safety import SafetyDecision
from ..validation.rules import SafetyRules, check_safety


class SafetyValidatorService:
    """Runs the structured rule set and returns a structured decision."""

    def __init__(self, rules: SafetyRules | None = None) -> None:
        self._rules = rules or SafetyRules()

    def validate(self, intake: DonationIntake, now: datetime | None = None) -> SafetyDecision:
        return check_safety(intake, rules=self._rules, now=now or datetime.now(timezone.utc))
