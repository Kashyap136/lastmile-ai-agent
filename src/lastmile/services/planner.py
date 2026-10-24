"""Pickup Planner service - feasible windows, travel estimates, route notes.

Uses the routing adapter (real API when configured, else a
clearly-labelled calculated estimate) and the recipient's service
windows to propose concrete pickup windows that fit the donation deadline.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from ..adapters.routing import estimate_between
from ..models.common import DataSource
from ..models.donation import DonationIntake
from ..models.pickup import PickupPlan, PickupProposal, PickupWindow, TravelEstimate
from ..models.recipient import RecipientProfile


@dataclass
class PlanOptions:
    minimum_headroom_minutes: int = 30  # donor needs at least this to leave
    max_windows: int = 3


class PickupPlanner:
    """Proposes ranked feasible pickup windows for one recipient."""

    def __init__(
        self,
        recipients: list[RecipientProfile],
        donor_zone_centers: dict[str, tuple[float, float]] | None = None,
        routing_api_url: str = "",
    ) -> None:
        self._by_id = {r.recipient_id: r for r in recipients}
        self._donor_centers = dict(donor_zone_centers or {})
        self._routing_api_url = routing_api_url

    def propose(
        self,
        intake: DonationIntake,
        recipient_id: str,
        options: PlanOptions | None = None,
        now: datetime | None = None,
    ) -> tuple[PickupProposal | None, str]:
        """Return (proposal, "") or (None, reason) when no window is feasible."""
        options = options or PlanOptions()
        now = now or datetime.now(timezone.utc)
        recipient = self._by_id.get(recipient_id)
        if recipient is None:
            return None, f"unknown recipient {recipient_id!r}"

        earliest = now + timedelta(
            minutes=max(recipient.min_notice_minutes, options.minimum_headroom_minutes)
        )
        windows = self._feasible_windows(
            recipient, earliest, intake.pickup_deadline, options.max_windows
        )
        if not windows:
            return None, (
                f"{recipient.name} has no service window that starts before your "
                f"deadline of {intake.pickup_deadline.strftime('%Y-%m-%d %H:%M UTC')} "
                "and after the required scheduling notice"
            )

        donor_center = self._donor_centers.get((intake.zone or "").strip())
        for window in windows:
            window.label = self._window_label(
                window, donor_center, recipient, intake
            )

        proposal = PickupProposal(
            donation_id=intake.donation_id,
            recipient_id=recipient.recipient_id,
            recipient_name=recipient.name,
            windows=windows,
        )
        return proposal, ""

    def build_plan(
        self,
        intake: DonationIntake,
        proposal: PickupProposal,
        window_id: str,
    ) -> tuple[PickupPlan | None, str]:
        """Turn a chosen window into a concrete reviewable plan."""
        window = proposal.window_by_id(window_id)
        if window is None:
            return None, f"unknown pickup window {window_id!r} for this proposal"
        recipient = self._by_id.get(proposal.recipient_id)
        if recipient is None:  # pragma: no cover - proposal came from this planner
            return None, "recipient dataset changed during the session"

        donor_center = self._donor_centers.get((intake.zone or "").strip())
        if donor_center is None:
            travel = TravelEstimate(
                distance_km=0.0,
                duration_minutes=0.0,
                data_source=DataSource.CALCULATED_ESTIMATE,
                note="donor zone not in demo layout - travel estimate unavailable",
            )
            route_summary = "route unavailable (pickup zone missing from demo layout)"
        else:
            result = estimate_between(
                donor_center[0],
                donor_center[1],
                recipient.latitude,
                recipient.longitude,
                routing_api_url=self._routing_api_url,
            )
            travel = result.estimate
            if result.error and "fell back" in result.error:
                travel = result.estimate  # still an explicitly-labelled estimate
                route_summary = (
                    f"approx. {travel.distance_km:g} km, {travel.duration_minutes:g} min "
                    f"({result.error})"
                )
            elif result.error:
                route_summary = "route could not be estimated"
            else:
                route_summary = (
                    f"approx. {travel.distance_km:g} km, {travel.duration_minutes:g} min "
                    f"(source: {travel.data_source.value})"
                )

        return PickupPlan(
            donation_id=intake.donation_id,
            recipient_id=recipient.recipient_id,
            recipient_name=recipient.name,
            window=window,
            travel=travel,
            route_summary=route_summary,
            handling_instructions=(
                f"Food arrives as {intake.storage.value} storage. "
                + (f"Handling notes: {intake.handling_notes}" if intake.handling_notes else "")
            ).strip(),
        ), ""

    # ---- internals ----------------------------------------------------------

    def _window_label(
        self,
        window: PickupWindow,
        donor_center: tuple[float, float] | None,
        recipient: RecipientProfile,
        intake: DonationIntake,
    ) -> str:
        base = f"{window.start.strftime('%a %d %b %H:%M')} - {window.end.strftime('%H:%M')} UTC"
        if donor_center is None:
            return base + " | travel estimate unavailable (zone missing)"
        result = estimate_between(
            donor_center[0],
            donor_center[1],
            recipient.latitude,
            recipient.longitude,
            routing_api_url=self._routing_api_url,
        )
        if result.error and not result.estimate.distance_km:
            return base + " | travel estimate unavailable"
        suffix = (
            "est." if result.estimate.data_source == DataSource.CALCULATED_ESTIMATE
            else "(routing API)"
        )
        return (
            f"{base} | {result.estimate.distance_km:g} km, "
            f"{result.estimate.duration_minutes:g} min {suffix}".strip()
        )

    @staticmethod
    def _feasible_windows(
        recipient: RecipientProfile,
        earliest: datetime,
        deadline: datetime,
        max_windows: int,
    ) -> list[PickupWindow]:
        windows: list[PickupWindow] = []
        day = earliest.date()
        guard = 0
        while day <= deadline.date() and len(windows) < max_windows and guard < 14:
            guard += 1
            for open_s, close_s in recipient.service_windows:
                start, close = _times_on(day, open_s, close_s)
                start = max(start, earliest)
                if start >= close:
                    continue  # service slot already passed
                if start >= deadline:
                    continue  # would blow the donor deadline
                end = min(close, deadline)
                if (end - start).total_seconds() < 30 * 60:
                    continue  # slot too short to be useful
                windows.append(
                    PickupWindow(
                        window_id=f"w{len(windows) + 1}",
                        start=start,
                        end=end,
                        fits_deadline=True,
                        label="",
                    )
                )
            day += timedelta(days=1)
        return windows[:max_windows]


def _times_on(day, open_hhmm: str, close_hhmm: str) -> tuple[datetime, datetime]:
    open_h, open_m = (int(x) for x in open_hhmm.split(":")[:2])
    close_h, close_m = (int(x) for x in close_hhmm.split(":")[:2])
    close_day = day + timedelta(days=1) if close_h > 23 else day
    close_hour = close_h - 24 if close_h > 23 else close_h
    start = datetime(day.year, day.month, day.day, open_h, open_m, tzinfo=timezone.utc)
    close = datetime(
        close_day.year, close_day.month, close_day.day, close_hour, close_m, tzinfo=timezone.utc
    )
    return start, close
