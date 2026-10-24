"""Session-aware workflow state.

One SessionState per chat user (keyed by sender address in ctx.storage).
The state is fully JSON-serialisable (pydantic v2 dicts) so it survives
agent restarts through the uAgents persistent storage.
"""

from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, Field

from ..models.common import DonationStatus, WorkflowStage


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class StatusEvent(BaseModel):
    at: datetime = Field(default_factory=_utcnow)
    status: DonationStatus
    detail: str = ""
    data_source_label: str = ""  # e.g. provenance label used in msgs


class SessionState(BaseModel):
    """Everything recoverable about one donation conversation."""

    user_address: str
    stage: WorkflowStage = WorkflowStage.NEW
    donation: dict | None = None  # DonationIntake.model_dump()
    match_result: dict | None = None  # MatchResult.model_dump()
    proposal: dict | None = None  # PickupProposal.model_dump()
    plan: dict | None = None  # PickupPlan.model_dump()
    pending_error: str = ""  # last transient failure, for honest reporting
    history: list[StatusEvent] = []
    partial: dict = {}  # partially-submitted form values
    last_summary: str = ""  # one-line summary of the last closed donation
    updated_at: datetime = Field(default_factory=_utcnow)

    def donation_id(self) -> str | None:
        return (self.donation or {}).get("donation_id")

    def is_empty(self) -> bool:
        return self.donation is None and not self.partial

    def record(self, status: DonationStatus, detail: str) -> StatusEvent:
        """Append a status-trail event (the single point of status writes)."""
        event = StatusEvent(status=status, detail=detail)
        self.history.append(event)
        self.updated_at = _utcnow()
        return event

    def current_status(self) -> DonationStatus:
        return self.history[-1].status if self.history else DonationStatus.RECEIVED


def cleanup_expired_sessions(store: dict[str, SessionState], ttl_seconds: int) -> list[str]:
    """Remove sessions idle beyond the TTL. Returns removed session keys."""
    now = _utcnow()
    expired: list[str] = []
    for key, state in list(store.items()):
        idle = (now - state.updated_at).total_seconds()
        if idle > ttl_seconds:
            expired.append(key)
            del store[key]
    return expired
