"""Specialist tool interfaces used by the workflow engine.

Two implementations, both returning structured results:

* LocalSpecialistTools - runs SafetyValidatorService / RecipientMatcher /
  PickupPlanner in-process (single-agent topology).

* RemoteSpecialistTools - delegates to the Layer-3 specialist agents via
  an injected request/response transport with timeouts and bounded
  retries. Failures are surfaced as SpecialistError (never silently
  swallowed); an optional local fallback is used only when explicitly
  enabled and is always labelled in the result.
"""

from __future__ import annotations

import json
import logging
from typing import Awaitable, Callable, Protocol

from ..models.donation import DonationIntake
from ..models.matching import MatchResult
from ..models.pickup import PickupPlan, PickupProposal
from ..models.safety import SafetyDecision

logger = logging.getLogger("lastmile.tools")


class SpecialistError(Exception):
    """A specialist could not complete after its bounded retries/timeouts."""


class SpecialistTools(Protocol):
    """The workflow engine talks to specialists only through this surface."""

    async def check_safety(self, intake: DonationIntake) -> SafetyDecision: ...

    async def find_recipients(self, intake: DonationIntake) -> MatchResult: ...

    async def propose_windows(
        self, intake: DonationIntake, recipient_id: str
    ) -> tuple[PickupProposal | None, str]: ...

    async def build_plan(
        self, intake: DonationIntake, proposal: PickupProposal, window_id: str
    ) -> tuple[PickupPlan | None, str]: ...


class LocalSpecialistTools:
    """In-process implementations - the single-agent topology."""

    def __init__(
        self,
        validator,
        matcher,
        planner,
        donor_zone_centers: dict[str, tuple[float, float]] | None = None,
    ) -> None:
        self._validator = validator
        self._matcher = matcher
        self._planner = planner

    async def check_safety(self, intake: DonationIntake) -> SafetyDecision:
        return self._validator.validate(intake)

    async def find_recipients(self, intake: DonationIntake) -> MatchResult:
        return self._matcher.match(intake)

    async def propose_windows(
        self, intake: DonationIntake, recipient_id: str
    ) -> tuple[PickupProposal | None, str]:
        return self._planner.propose(intake, recipient_id)

    async def build_plan(
        self, intake: DonationIntake, proposal: PickupProposal, window_id: str
    ) -> tuple[PickupPlan | None, str]:
        return self._planner.build_plan(intake, proposal, window_id)


# --------------------------------------------------------------------------
# Remote (multi-agent) tools
# --------------------------------------------------------------------------

Transport = Callable[[str, str], Awaitable[dict | None]]


class RemoteSpecialistTools:
    """Delegates specialist work to Layer-3 agents.

    request_fn: async (specialist_name, payload_json) -> response-dict | None
    The transport is owned by the coordinator agent (uAgents ctx + futures);
    here we only add timeout policy, bounded retries and explicit failures.
    """

    def __init__(
        self,
        request_fn: Transport,
        timeout_seconds: float = 20.0,
        retries: int = 2,
        fallback: SpecialistTools | None = None,
    ) -> None:
        self._request_fn = request_fn
        self._timeout = timeout_seconds
        self._retries = retries
        self._fallback = fallback

    async def check_safety(self, intake: DonationIntake) -> SafetyDecision:
        payload = {"donation_id": intake.donation_id, "payload": intake.model_dump(mode="json")}
        response = await self._request_with_retries("safety", "safety agent", payload)
        if response is not None:
            if response.get("ok"):
                decision = SafetyDecision(**json.loads(response["result_json"]))
                return _apply_fallback_note(decision, response)
            raise SpecialistError(f"safety agent error: {response['error']}")
        if self._fallback is not None:
            decision = await self._fallback.check_safety(intake)
            decision.reasons = list(decision.reasons) + [
                "[fallback] safety specialist agent was unreachable - rules evaluated locally"
            ]
            return decision
        raise SpecialistError(_attempt_message("safety validator"))

    async def find_recipients(self, intake: DonationIntake) -> MatchResult:
        payload = {"donation_id": intake.donation_id, "payload": intake.model_dump(mode="json")}
        response = await self._request_with_retries("matcher", "matcher agent", payload)
        if response is not None:
            if response.get("ok"):
                result = MatchResult(**json.loads(response["result_json"]))
                return _apply_fallback_note(result, response)
            raise SpecialistError(f"matcher agent error: {response.get('error', 'unknown')}")
        if self._fallback is not None:
            result = await self._fallback.find_recipients(intake)
            result.no_match_reasons = list(result.no_match_reasons) + [
                "[fallback] recipient matcher agent was unreachable - dataset matched locally"
            ]
            return result
        raise SpecialistError(_attempt_message("recipient matcher"))

    async def propose_windows(
        self, intake: DonationIntake, recipient_id: str
    ) -> tuple[PickupProposal | None, str]:
        payload = {
            "donation_id": intake.donation_id,
            "payload": intake.model_dump(mode="json"),
            "recipient_id": recipient_id,
        }
        response = await self._request_with_retries("planner", "planner agent", payload)
        if response is not None:
            if response.get("ok"):
                result = json.loads(response.get("result_json") or "null")
                if result is None:
                    return None, response.get("detail", "no feasible windows for this recipient")
                proposal = PickupProposal(**result)
                return _apply_fallback_note(proposal, response), ""
            raise SpecialistError(f"planner agent error: {response.get('error', 'unknown')}")
        if self._fallback is not None:
            return await self._fallback.propose_windows(intake, recipient_id)
        return None, self._attempt_message("pickup planner")

    async def build_plan(
        self, intake: DonationIntake, proposal: PickupProposal, window_id: str
    ) -> tuple[PickupPlan | None, str]:
        payload = {
            "donation_id": intake.donation_id,
            "payload": intake.model_dump(mode="json"),
            "proposal": proposal.model_dump(mode="json"),
            "window_id": window_id,
        }
        response = await self._request_with_retries("planner", "planner agent", payload, "plan")
        if response is not None:
            if response.get("ok"):
                result = json.loads(response.get("result_json") or "null")
                if result is None:
                    return None, response.get("detail", "could not build the plan")
                plan = PickupPlan(**result)
                return _apply_fallback_note(plan, response), ""
            raise SpecialistError(f"planner agent error: {response.get('error', 'unknown')}")
        if self._fallback is not None:
            return await self._fallback.build_plan(intake, proposal, window_id)
        return None, self._attempt_message("plan builder")

    # ---- internals ----------------------------------------------------------

    async def _request_with_retries(
        self, specialist: str, label: str, payload: dict, variant: str = ""
    ) -> dict | None:
        """Run one remote call with bounded retries on transport failures.

        Returns the response dict, or None when all attempts timed out.
        Explicit agent errors raise SpecialistError immediately - retrying
        errors is a policy decision, not a transport recovery.
        """
        attempt = 0
        while attempt <= self._retries:
            response = await self._request_fn(specialist, json.dumps(payload))
            if response is None:
                logger.warning(
                    "%s%s timed out, attempt %d/%d",
                    label,
                    f" ({variant})" if variant else "",
                    attempt + 1,
                    self._retries + 1,
                )
                attempt += 1
                continue
            return response
        return None


def _attempt_message(label: str) -> str:
    return (
        f"{label} agent did not respond within the per-call timeout "
        "(bounded retries exhausted) - no result could be produced"
    )


def _apply_fallback_note(result, response: dict):
    """Annotate results the remote agent flagged as locally-computed fallbacks."""
    if response.get("fallback"):
        note = "[fallback] specialist agent could not complete this request"
        if hasattr(result, "reasons") and result.reasons is not None:
            result.reasons = list(result.reasons) + [note]
        if hasattr(result, "no_match_reasons") and result.no_match_reasons is not None:
            result.no_match_reasons = list(result.no_match_reasons) + [note]
    return result

