"""The donation-to-coordination workflow engine.

Framework-agnostic: it consumes session state + user text and produces
ordered outputs (narration texts and optional interactive cards). The
uAgent layer is a thin adapter that persists state in ctx.storage and
sends the produced messages over the Agent Chat Protocol.

Workflow: intake -> validation -> (safety) -> recipient matching ->
pickup window proposal -> plan review -> user confirmation -> status
tracking. Every destructive or outward-facing step requires explicit
user approval, and statuses never claim unverified external outcomes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional

from ..adapters.notify import NotifyResult
from ..cards import (
    KIND_CAROUSEL,
    KIND_DETAIL,
    KIND_FORM,
    KIND_REVIEW,
    donation_form_card,
    pickup_windows_detail_card,
    plan_review_card,
    recipients_carousel_card,
)
from ..chat.selection import parse_selection
from ..models.common import DataSource, DonationStatus, WorkflowStage
from ..models.donation import DonationIntake, parse_intake_submission
from ..models.matching import MatchResult
from ..models.pickup import PickupPlan, PickupProposal
from .state import SessionState
from .tools import SpecialistError, SpecialistTools

# selection keys that are donation form fields
_FORM_FIELDS = {
    "category", "description", "quantity", "unit", "prepared_at",
    "pickup_deadline", "location", "zone", "storage", "handling_notes",
}

_INTRO_TEXT = (
    "Hi! I'm LastMile Rescue, a surplus-food rescue coordinator. I validate "
    "your donation, check safety eligibility, match it to recipient "
    "organisations and coordinate a feasible pickup - all through this chat. "
    "Everything is tracked: ask for 'status' any time. To get started, fill "
    "in this donation form (required fields are marked). Note: the recipient "
    "directory in this build is a clearly-labelled DEMO dataset."
)


def _default_notify(plan_dict: dict) -> NotifyResult:
    """Honest default: without a configured channel nothing is ever sent."""
    return NotifyResult(
        delivered=False,
        data_source=DataSource.DEMO,
        detail=(
            "no recipient notification channel is configured and the recipient "
            "is demo data - nobody was contacted; recorded as "
            "awaiting_recipient_confirmation"
        ),
    )


@dataclass
class EngineOutput:
    """One outgoing chat message: narration text + optional card."""

    text: str
    card_kind: Optional[str] = None
    card_payload: dict | None = None

    def has_card(self) -> bool:
        return bool(self.card_kind and self.card_payload is not None)


@dataclass
class EngineResult:
    state: SessionState
    outputs: list[EngineOutput] = field(default_factory=list)

    def add(self, text: str, card_kind: str | None = None, card_payload: dict | None = None) -> None:
        self.outputs.append(EngineOutput(text=text, card_kind=card_kind, card_payload=card_payload))


class WorkflowEngine:
    """Runs one user turn against the workflow state machine."""

    def __init__(
        self,
        tools: SpecialistTools,
        notify_fn: Callable[[dict], NotifyResult] | None = None,
    ) -> None:
        self._tools = tools
        self._notify_fn = notify_fn or _default_notify

    async def handle_text(self, state: SessionState, text: str) -> EngineResult:
        """Entry point for any incoming user text."""
        _expire_if_deadline_passed(state, datetime.now(timezone.utc))
        selection = parse_selection(text)
        result = EngineResult(state=state)

        # ---- global intents ------------------------------------------------
        if selection.get("action") == "check_status" or _is_status_query(text):
            return self._on_status(result, state)
        if _is_picked_up_intent(text) and state.donation:
            return self._on_picked_up(result, state)
        if "retry" in (text or "").lower() and state.pending_error:
            return await self._resume_after_failure(result, state)
        # an explicit restart command works at any stage (protects the active
        # flow from accidental words like 'donate' mid-review)
        if (text or "").strip().lower() in {"start over", "new donation"}:
            return self._start_new_donation(result, state, with_intro=False)

        # ---- stage dispatch -------------------------------------------------
        stage = state.stage
        if stage == WorkflowStage.NEW:
            return self._start_new_donation(result, state, with_intro=True)
        if stage == WorkflowStage.AWAITING_DONATION_FORM:
            return await self._stage_form(result, state, selection, text)
        if stage == WorkflowStage.AWAITING_RECIPIENT_CHOICE:
            return await self._stage_recipient_choice(result, state, selection, text)
        if stage == WorkflowStage.AWAITING_WINDOW_CHOICE:
            return await self._stage_window_choice(result, state, selection, text)
        if stage == WorkflowStage.AWAITING_PLAN_REVIEW:
            return await self._stage_plan_review(result, state, selection, text)
        return await self._stage_done(result, state, selection, text)

    # ------------------------------------------------------------------
    # stage: start / form
    # ------------------------------------------------------------------

    def _start_new_donation(
        self, result: EngineResult, state: SessionState, with_intro: bool
    ) -> EngineResult:
        self._reset_for_new_donation(state)
        if with_intro:
            result.add(_INTRO_TEXT)
        result.add(
            "Please complete the structured donation form in the panel. You can "
            "cancel at any point by typing 'cancel'.",
            KIND_FORM,
            donation_form_card(state.partial),
        )
        state.stage = WorkflowStage.AWAITING_DONATION_FORM
        state.pending_error = ""
        return result

    def _reset_for_new_donation(self, state: SessionState) -> None:
        """Archive the last donation (if closed) and clear workflow fields."""
        if state.donation is not None:
            state.last_summary = _summarize_closed(state.donation)
        state.donation = None
        state.match_result = None
        state.proposal = None
        state.plan = None
        state.partial = {}

    async def _stage_form(
        self,
        result: EngineResult,
        state: SessionState,
        selection: dict,
        text: str,
    ) -> EngineResult:
        if _is_cancel_intent(text):
            return self._on_cancel(result, state)

        looks_like_submit = (
            selection.get("action") == "submit_donation"
            or (not selection.get("action") and bool(selection.get("description")))
        )
        if looks_like_submit:
            return await self._validate_submission(result, state, selection)

        if state.partial:
            hint = (
                "Your partially completed form is kept: "
                f"{', '.join(sorted(state.partial))}. "
                "Please fill/submit the form in the panel when ready."
            )
        else:
            hint = "Please fill in the donation form in the panel to continue."
        result.add(hint, KIND_FORM, donation_form_card(state.partial))
        return result

    async def _validate_submission(
        self, result: EngineResult, state: SessionState, selection: dict
    ) -> EngineResult:
        merged = dict(state.partial)
        for key, value in selection.items():
            if key in _FORM_FIELDS and value not in (None, "", []):
                merged[key] = value

        intake, errors = parse_intake_submission(merged)
        if intake is None:
            state.partial = merged  # preserve progress for safe resubmission
            error_lines = "\n".join(f"- {e}" for e in errors)
            result.add(
                "I could not accept the donation yet - please correct these:\n"
                f"{error_lines}\n\nYour previous entries are kept in the form; "
                "resubmit when ready.",
                KIND_FORM,
                donation_form_card(merged),
            )
            return result

        state.donation = intake.model_dump()
        state.partial = {}
        state.record(
            DonationStatus.RECEIVED,
            f"intake complete: {intake.quantity:g} {intake.unit.value} of "
            f"{intake.category.value}",
        )
        result.add(
            f"Donation #{intake.donation_id} recorded: {intake.quantity:g} "
            f"{intake.unit.value} of {intake.description}. Now running the "
            "safety validation.",
        )
        return await self._run_safety(result, state, intake, keep_partial=merged)

    async def _run_safety(
        self,
        result: EngineResult,
        state: SessionState,
        intake: DonationIntake,
        keep_partial: dict | None = None,
    ) -> EngineResult:
        try:
            decision = await self._tools.check_safety(intake)
        except SpecialistError as exc:
            state.pending_error = f"failed_step:safety|detail:{exc}"
            state.record(DonationStatus.FAILED, "safety validation unavailable")
            result.add(_specialist_failure_text("safety validator", str(exc)))
            return result

        if not decision.approved:
            state.donation["status"] = DonationStatus.REJECTED_BY_SAFETY.value
            state.record(
                DonationStatus.REJECTED_BY_SAFETY,
                "; ".join(decision.reasons[:2]),
            )
            reasons = "\n".join(f"- {r}" for r in decision.reason_bullets)
            result.add(
                "The safety validator REJECTED this donation. Nothing has been "
                f"shared with anyone. Reasons:\n{reasons}\n\n"
                "Fix the reported issues in the form and resubmit, or type 'cancel'.",
                KIND_FORM,
                donation_form_card(keep_partial or state.partial),
            )
            state.stage = WorkflowStage.AWAITING_DONATION_FORM
            state.partial = keep_partial if keep_partial is not None else state.partial
            return result

        state.donation["status"] = DonationStatus.VALIDATED.value
        state.record(
            DonationStatus.VALIDATED, f"passed {decision.checked_rules} safety rules"
        )
        return await self._run_matching(result, state, intake)

    async def _run_matching(
        self, result: EngineResult, state: SessionState, intake: DonationIntake
    ) -> EngineResult:
        try:
            match_result = await self._tools.find_recipients(intake)
        except SpecialistError as exc:
            state.pending_error = f"failed_step:matcher|detail:{exc}"
            state.record(DonationStatus.FAILED, "recipient matching unavailable")
            result.add(_specialist_failure_text("recipient matcher", str(exc)))
            return result

        state.match_result = match_result.model_dump()
        if not match_result.matches:
            reasons = "\n".join(
                f"- {r}"
                for r in (
                    match_result.no_match_reasons
                    or ["no eligible recipients were found"]
                )
            )
            state.record(
                DonationStatus.NO_MATCH,
                f"0 of {match_result.considered_count} recipients eligible",
            )
            state.stage = WorkflowStage.DONE
            result.add(
                "No eligible recipient organisations matched this donation - "
                "nothing has been proposed or shared with anyone.\n"
                f"{reasons}\n\nType 'donate' to start a new donation with "
                "adjusted details."
            )
            return result

        top = match_result.matches[0]
        state.record(
            DonationStatus.MATCHED,
            f"{len(match_result.matches)} candidate(s); top rank: {top.recipient.name}",
        )
        state.stage = WorkflowStage.AWAITING_RECIPIENT_CHOICE
        result.add(
            "Safety validation passed. These are the ranked matching recipients "
            "(demo dataset - not real organisations). Select one to continue:",
            KIND_CAROUSEL,
            recipients_carousel_card(intake, match_result.matches),
        )
        return result

    # ------------------------------------------------------------------
    # stage: recipient choice
    # ------------------------------------------------------------------

    async def _stage_recipient_choice(
        self, result: EngineResult, state: SessionState, selection: dict, text: str
    ) -> EngineResult:
        if _is_cancel_intent(text):
            return self._on_cancel(result, state)
        if selection.get("action") in ("submit_donation", "approve_plan", "reject_plan"):
            # stale/foreign card interaction here: treat submission text as data
            return await self._validate_submission(result, state, selection)

        intake = DonationIntake(**state.donation)
        recipient_id = selection.get("recipient_id") or _bare_id(text, "rcp")
        if not recipient_id:
            result.add(
                "Please select one of the proposed recipient organisations in "
                "the panel (or type 'cancel').",
                KIND_CAROUSEL,
                self._recipients_card(state),
            )
            return result

        known = {m["recipient"]["recipient_id"] for m in state.match_result["matches"]}
        if recipient_id not in known:
            result.add(
                f"{recipient_id} is not one of the proposed recipients. Please "
                "select one from the panel (or type 'cancel').",
                KIND_CAROUSEL,
                self._recipients_card(state),
            )
            return result

        proposal, error = await self._propose_with_fallback(result, state, intake, recipient_id)
        if proposal is None:
            return result
        if proposal.recipient_id != recipient_id:
            result.add(
                f"The planner could not schedule {recipient_id} in time. The "
                f"next best ranked recipient was proposed instead: "
                f"{proposal.recipient_name}. Pick another organisation below "
                "if you prefer.",
            )
        state.proposal = proposal.model_dump()
        state.stage = WorkflowStage.AWAITING_WINDOW_CHOICE
        result.add(
            "The pickup planner produced feasible windows for "
            f"{proposal.recipient_name}. Travel figures carry their source "
            "labels (estimate vs external routing). Choose one:",
            KIND_DETAIL,
            pickup_windows_detail_card(proposal, intake.zone),
        )
        return result

    async def _propose_with_fallback(
        self,
        result: EngineResult,
        state: SessionState,
        intake: DonationIntake,
        recipient_id: str,
    ) -> tuple[PickupProposal | None, str]:
        """Propose for the chosen recipient; when infeasible, try the next
        ranked candidates and tell the user who was auto-switched."""
        try:
            proposal, error = await self._tools.propose_windows(intake, recipient_id)
        except SpecialistError as exc:
            state.pending_error = f"failed_step:propose|recipient:{recipient_id}|detail:{exc}"
            state.record(DonationStatus.FAILED, "planner unavailable at recipient choice")
            result.add(_specialist_failure_text("pickup planner", str(exc)))
            return None, str(exc)

        if proposal is not None:
            return proposal, error

        for alt in state.match_result["matches"]:
            alt_id = alt["recipient"]["recipient_id"]
            if alt_id == recipient_id:
                continue
            try:
                alt_proposal, _ = await self._tools.propose_windows(intake, alt_id)
            except SpecialistError as exc:
                state.pending_error = f"failed_step:propose|recipient:{alt_id}|detail:{exc}"
                result.add(_specialist_failure_text("pickup planner", str(exc)))
                return None, str(exc)
            if alt_proposal is not None:
                return alt_proposal, error

        result.add(
            f"No feasible pickup with any of the proposed recipients ({error})\n"
            "Type 'cancel', or 'donate' to adjust the donation details.",
            KIND_CAROUSEL,
            self._recipients_card(state),
        )
        return None, error

    def _recipients_card(self, state: SessionState) -> dict:
        match_result = MatchResult(**state.match_result)
        intake = DonationIntake(**state.donation)
        return recipients_carousel_card(intake, match_result.matches)

    # ------------------------------------------------------------------
    # stage: window choice
    # ------------------------------------------------------------------

    async def _stage_window_choice(
        self, result: EngineResult, state: SessionState, selection: dict, text: str
    ) -> EngineResult:
        if _is_cancel_intent(text):
            return self._on_cancel(result, state)

        intake = DonationIntake(**state.donation)
        proposal = PickupProposal(**state.proposal)

        if selection.get("action") == "back_to_matches":
            state.stage = WorkflowStage.AWAITING_RECIPIENT_CHOICE
            result.add(
                "Sure - pick a different recipient organisation:",
                KIND_CAROUSEL,
                self._recipients_card(state),
            )
            return result

        window_id = selection.get("window_id") or _bare_window_id(text)
        if not window_id:
            result.add(
                "Please choose one of the listed pickup windows in the panel "
                "(or type 'cancel').",
                KIND_DETAIL,
                pickup_windows_detail_card(proposal, intake.zone),
            )
            return result
        if proposal.window_by_id(window_id) is None:
            result.add(
                f"Window {window_id!r} is not one of the proposed options; "
                "please choose from the panel.",
                KIND_DETAIL,
                pickup_windows_detail_card(proposal, intake.zone),
            )
            return result

        try:
            plan, error = await self._tools.build_plan(intake, proposal, window_id)
        except SpecialistError as exc:
            state.pending_error = f"failed_step:plan|window:{window_id}|detail:{exc}"
            state.record(DonationStatus.FAILED, "planner unavailable at plan build")
            result.add(_specialist_failure_text("pickup planner", str(exc)))
            return result

        if plan is None:
            result.add(
                f"Could not build the pickup plan: {error}\nChoose another "
                "window, a different recipient, or type 'cancel'.",
                KIND_DETAIL,
                pickup_windows_detail_card(proposal, intake.zone),
            )
            return result

        state.plan = plan.model_dump()
        state.donation["status"] = DonationStatus.PROPOSED.value
        state.record(
            DonationStatus.PROPOSED,
            f"plan {plan.plan_id} proposed for {plan.recipient_name}",
        )
        state.stage = WorkflowStage.AWAITING_PLAN_REVIEW
        result.add(
            "Please review the proposed pickup in the panel, then approve or "
            "reject it.",
            KIND_REVIEW,
            plan_review_card(plan, intake, demo_recipient=_is_demo_name(plan.recipient_name)),
        )
        return result

    # ------------------------------------------------------------------
    # stage: plan review
    # ------------------------------------------------------------------

    async def _stage_plan_review(
        self, result: EngineResult, state: SessionState, selection: dict, text: str
    ) -> EngineResult:
        if _is_cancel_intent(text):
            return self._on_cancel(result, state)

        intake = DonationIntake(**state.donation)
        plan = PickupPlan(**state.plan)

        if selection.get("action") == "approve_plan" or _is_approve_words(text):
            return await self._on_approve(result, state, intake, plan)

        if selection.get("action") == "reject_plan" or _is_reject_words(text):
            state.donation["status"] = DonationStatus.DECLINED.value
            state.record(DonationStatus.DECLINED, "donor rejected the proposed plan")
            state.stage = WorkflowStage.AWAITING_RECIPIENT_CHOICE
            result.add(
                "Plan rejected - nothing was sent anywhere. Pick a different "
                "recipient organisation, or type 'cancel'.",
                KIND_CAROUSEL,
                self._recipients_card(state),
            )
            return result

        result.add(
            "The review panel needs your decision - approve or reject the plan "
            "(you can also type 'cancel').",
            KIND_REVIEW,
            plan_review_card(plan, intake, demo_recipient=_is_demo_name(plan.recipient_name)),
        )
        return result

    async def _on_approve(
        self,
        result: EngineResult,
        state: SessionState,
        intake: DonationIntake,
        plan: PickupPlan,
    ) -> EngineResult:
        state.donation["status"] = DonationStatus.CONFIRMED.value
        state.record(
            DonationStatus.CONFIRMED,
            f"donor approved plan {plan.plan_id} with {plan.recipient_name}",
        )

        notify_result = self._notify(plan.model_dump(mode="json"))
        if notify_result.delivered:
            state.donation["status"] = DonationStatus.DISPATCHED_TO_RECIPIENT.value
            state.record(
                DonationStatus.DISPATCHED_TO_RECIPIENT,
                f"recipient confirmed by channel: {notify_result.detail}",
            )
        else:
            state.donation["status"] = DonationStatus.AWAITING_RECIPIENT_CONFIRMATION.value
            state.record(
                DonationStatus.AWAITING_RECIPIENT_CONFIRMATION,
                notify_result.detail,
            )

        state.stage = WorkflowStage.DONE
        result.add(
            "Approved - the pickup plan is recorded.\n\n"
            f"- Donation: {intake.quantity:g} {intake.unit.value} of "
            f"{intake.description} (#{intake.donation_id})\n"
            f"- Recipient: {plan.recipient_name}\n"
            f"- Window: {plan.window.label or plan.window.window_id}\n"
            f"- Travel: {plan.route_summary}\n\n"
            f"Recipient notification: {notify_result.detail}\n"
            "Reply 'picked up' once the food has been collected, or 'status' "
            "any time to see the trail.",
        )
        return result

    def _notify(self, plan_dict: dict) -> NotifyResult:
        try:
            return self._notify_fn(plan_dict)
        except Exception as exc:  # adapter crash - keep the honest failure state
            return NotifyResult(
                delivered=False,
                data_source=DataSource.DEMO,
                detail=f"notification channel crashed ({exc!r}); plan kept as "
                "awaiting_recipient_confirmation",
            )

    # ------------------------------------------------------------------
    # global intents
    # ------------------------------------------------------------------

    def _on_cancel(self, result: EngineResult, state: SessionState) -> EngineResult:
        if state.donation is not None and state.current_status() not in _TERMINAL:
            state.donation["status"] = DonationStatus.CANCELLED.value
            state.record(DonationStatus.CANCELLED, "donation cancelled by donor")
            result.add(
                f"Donation #{state.donation_id()} is tracked as CANCELLED. "
                "Nothing was sent to anyone outside this session. Type 'donate' "
                "to start again or 'status' to see the trail.",
            )
        else:
            result.add("Closed. Type 'donate' to start a new donation.")
        state.stage = WorkflowStage.DONE
        return result

    def _on_status(self, result: EngineResult, state: SessionState) -> EngineResult:
        if state.donation is None:
            tail = f" Last session summary: {state.last_summary}" if state.last_summary else ""
            result.add(
                "No donation in this session yet. Type 'donate' to report surplus." + tail
            )
            return result

        donation = state.donation
        lines = [f"Status for donation #{donation.get('donation_id')}:",
                 f"- food: {donation.get('quantity')} {donation.get('unit')} of "
                 f"{donation.get('description')}"]
        if state.plan:
            plan = state.plan
            lines.append(f"- plan: {plan.get('plan_id')} -> {plan.get('recipient_name')}")
            window = plan.get("window") or {}
            if window.get("label"):
                lines.append(f"- window: {window['label']}")
        lines.append(f"- current status: {state.current_status().value}")
        if state.history:
            lines.append("- recent status trail (UTC):")
            for event in state.history[-6:]:
                lines.append(
                    f"    * {event.at.strftime('%Y-%m-%d %H:%M')} "
                    f"{event.status.value}: {event.detail[:110]}"
                )
        if state.pending_error:
            lines.append(f"- pending failure: {state.pending_error}")
        if donation.get("status") in [s.value for s in _CLOSEABLE]:
            lines.append("Reply 'picked up' once the food has been collected.")
        result.add("\n".join(lines))
        return result

    def _on_picked_up(self, result: EngineResult, state: SessionState) -> EngineResult:
        status = state.current_status()
        if status not in _CLOSEABLE:
            result.add(
                "A pickup can only be marked complete after it has been "
                f"approved. Current status: {status.value}.",
            )
            return result
        state.donation["status"] = DonationStatus.PICKED_UP.value
        state.record(DonationStatus.PICKED_UP, "donor reported the food was collected")
        state.stage = WorkflowStage.DONE
        result.add(
            f"Rescue complete: donation #{state.donation_id()} is tracked as "
            "PICKED UP. Type 'donate' to report another batch."
        )
        return result

    async def _stage_done(
        self, result: EngineResult, state: SessionState, selection: dict, text: str
    ) -> EngineResult:
        # A form submission arriving here starts a new donation with that data.
        looks_like_submit = (
            selection.get("action") == "submit_donation" or bool(selection.get("description"))
        )
        if looks_like_submit:
            self._reset_for_new_donation(state)
            state.stage = WorkflowStage.AWAITING_DONATION_FORM
            return await self._validate_submission(result, state, selection)

        wants_new = _is_new_donation_intent(text) or _is_greeting(text) or (
            state.donation is None and not state.last_summary
        )
        if wants_new:
            return self._start_new_donation(result, state, with_intro=not state.last_summary)
        result.add(
            "This donation is closed. Reply 'donate' to start a new one, or "
            "'status' to review it."
        )
        return result

    async def _resume_after_failure(self, result: EngineResult, state: SessionState) -> EngineResult:
        """Re-run the failed specialist step, then hand back to its stage."""
        pending = state.pending_error or ""
        failed_step = ""
        for part in pending.split("|"):
            if part.startswith("failed_step:"):
                failed_step = part.split(":", 1)[1].strip()
        context: dict[str, str] = {}
        for part in pending.split("|"):
            if ":" in part:
                key, _, value = part.partition(":")
                context[key] = value
        result.add(f"Retrying the failed step ({failed_step or 'previous'})...")
        state.pending_error = ""

        if failed_step in ("safety", "matcher") and state.donation:
            intake = DonationIntake(**state.donation)
            if failed_step == "matcher":
                return await self._run_matching(result, state, intake)
            return await self._run_safety(result, state, intake)

        if failed_step == "propose":
            recipient_id = context.get("recipient", "")
            if recipient_id and state.donation:
                intake = DonationIntake(**state.donation)
                return await self._propose_for(result, state, intake, recipient_id)
            if state.donation:
                return await self._run_matching(result, state, DonationIntake(**state.donation))
            return self._start_new_donation(result, state, with_intro=False)

        if failed_step == "plan" and state.donation and state.proposal:
            window_id = context.get("window", "")
            if window_id:
                proposal = PickupProposal(**state.proposal)
                return await self._plan_window(
                    result, state, DonationIntake(**state.donation), proposal, window_id
                )
            # no window recorded - go back to recipient choice
            state.stage = WorkflowStage.AWAITING_RECIPIENT_CHOICE
            result.add("Please pick a recipient again:", KIND_CAROUSEL, self._recipients_card(state))
            return result

        if failed_step == "notify" and state.plan and state.donation:
            intake = DonationIntake(**state.donation)
            plan = PickupPlan(**state.plan)
            return await self._on_approve(result, state, intake, plan)

        # nothing specific to resume - fall back to restarting the flow
        return self._start_new_donation(result, state, with_intro=False)

    # small step-helpers reused by the resume path -------------------------

    async def _propose_for(
        self, result: EngineResult, state: SessionState, intake: DonationIntake, recipient_id: str
    ) -> EngineResult:
        try:
            proposal, error = await self._tools.propose_windows(intake, recipient_id)
        except SpecialistError as exc:
            state.pending_error = f"failed_step:propose|recipient:{recipient_id}|detail:{exc}"
            result.add(_specialist_failure_text("pickup planner", str(exc)))
            return result
        if proposal is None:
            state.stage = WorkflowStage.AWAITING_RECIPIENT_CHOICE
            result.add(
                f"Still infeasible: {error}\nPick another organisation or type 'cancel'.",
                KIND_CAROUSEL,
                self._recipients_card(state),
            )
            return result
        state.proposal = proposal.model_dump()
        state.stage = WorkflowStage.AWAITING_WINDOW_CHOICE
        result.add(
            "The planner responded this time - choose a window:",
            KIND_DETAIL,
            pickup_windows_detail_card(proposal, intake.zone),
        )
        return result

    async def _plan_window(
        self,
        result: EngineResult,
        state: SessionState,
        intake: DonationIntake,
        proposal: PickupProposal,
        window_id: str,
    ) -> EngineResult:
        try:
            plan, error = await self._tools.build_plan(intake, proposal, window_id)
        except SpecialistError as exc:
            state.pending_error = f"failed_step:plan|window:{window_id}|detail:{exc}"
            result.add(_specialist_failure_text("pickup planner", str(exc)))
            return result
        if plan is None:
            result.add(
                f"Still failing: {error}\nChoose another window or recipient.",
                KIND_DETAIL,
                pickup_windows_detail_card(proposal, intake.zone),
            )
            return result
        state.plan = plan.model_dump()
        state.donation["status"] = DonationStatus.PROPOSED.value
        state.record(
            DonationStatus.PROPOSED, f"plan {plan.plan_id} proposed for {plan.recipient_name}"
        )
        state.stage = WorkflowStage.AWAITING_PLAN_REVIEW
        result.add(
            "Plan built - please review and approve or reject:",
            KIND_REVIEW,
            plan_review_card(plan, intake, demo_recipient=_is_demo_name(plan.recipient_name)),
        )
        return result


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------

_TERMINAL = {
    DonationStatus.CANCELLED,
    DonationStatus.DECLINED,
    DonationStatus.PICKED_UP,
    DonationStatus.EXPIRED,
    DonationStatus.NO_MATCH,
    DonationStatus.REJECTED_BY_SAFETY,
}
_CLOSEABLE = {
    DonationStatus.CONFIRMED,
    DonationStatus.AWAITING_RECIPIENT_CONFIRMATION,
    DonationStatus.DISPATCHED_TO_RECIPIENT,
}


def _summarize_closed(donation: dict) -> str:
    return (
        f"#{donation.get('donation_id', '?')} "
        f"{donation.get('description', '')} "
        f"({donation.get('quantity')} {donation.get('unit')}) - "
        f"{donation.get('status')}"
    )


def _is_demo_name(recipient_name: str) -> bool:
    return "(demo)" in (recipient_name or "").lower()


def _expire_if_deadline_passed(state: SessionState, now: datetime) -> None:
    donation = state.donation
    if not donation:
        return
    deadline = _parse_utc(donation.get("pickup_deadline"))
    status = state.current_status()
    if deadline is None or status in _TERMINAL or status in _CLOSEABLE or status == DonationStatus.PROPOSED:
        return
    # only pre-confirmation stages expire automatically
    expirable = {
        WorkflowStage.NEW,
        WorkflowStage.AWAITING_DONATION_FORM,
        WorkflowStage.AWAITING_RECIPIENT_CHOICE,
        WorkflowStage.AWAITING_WINDOW_CHOICE,
        WorkflowStage.AWAITING_PLAN_REVIEW,
    }
    if state.stage in expirable and deadline < now:
        donation["status"] = DonationStatus.EXPIRED.value
        state.record(DonationStatus.EXPIRED, "pickup deadline passed before confirmation")
        state.stage = WorkflowStage.DONE
        state.pending_error = ""


def _parse_utc(raw) -> datetime | None:
    if isinstance(raw, datetime):
        return raw
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _is_greeting(text: str) -> bool:
    return bool(text) and text.strip().lower() in {
        "hi", "hi!", "hello", "hello!", "hey", "hey!", "start", "start over",
    }


def _is_new_donation_intent(text: str) -> bool:
    lowered = (text or "").lower()
    if any(w in lowered for w in ("cancel", "status", "picked up", "picked-up")):
        return False
    return any(
        w in lowered
        for w in ("donate", "donation", "surplus", "leftover", "give away")
    )


def _is_cancel_intent(text: str) -> bool:
    lowered = (text or "").lower().strip()
    return lowered.startswith("cancel") or lowered in {"stop", "abort"}


def _is_status_query(text: str) -> bool:
    lowered = (text or "").lower().strip()
    return (
        lowered in {"status", "status?", "progress", "where is it?", "check status"}
        or lowered.startswith("what's the status")
        or lowered.startswith("whats the status")
    )


def _is_picked_up_intent(text: str) -> bool:
    lowered = (text or "").lower()
    if any(w in lowered for w in ("cancel", "not ", "wasn't")):
        return False
    return any(w in lowered for w in ("picked up", "picked-up", "collected", "delivered"))


def _is_approve_words(text: str) -> bool:
    lowered = (text or "").lower()
    if any(w in lowered for w in ("reject", "cancel", "decline", "do not approve", "don't approve")):
        return False
    return any(w in lowered for w in ("approve", "confirmed", "confirm", "accept", "yes", "go ahead"))


def _is_reject_words(text: str) -> bool:
    lowered = (text or "").lower()
    return any(
        w in lowered
        for w in ("reject", "decline", "don't approve", "do not approve", "cancel")
    )


def _bare_id(text: str, prefix: str = "rcp") -> str | None:
    match = re.search(rf"\b{re.escape(prefix)}_\d+\b", text or "")
    return match.group(0) if match else None


def _bare_window_id(text: str) -> str | None:
    match = re.search(r"\bw\d+\b", (text or ""), re.IGNORECASE)
    return match.group(0).lower() if match else None


def _specialist_failure_text(agent_label: str, detail: str) -> str:
    return (
        f"The {agent_label} agent could not complete its work: {detail}\n"
        "Nothing was lost - your data is safe. Type 'retry' to try again, or "
        "'cancel' to stop."
    )
