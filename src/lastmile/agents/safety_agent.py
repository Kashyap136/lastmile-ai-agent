"""Safety Validator agent (Layer 3).

Returns strictly structured SafetyDecisions with reasons. It rejects
unsafe or unverifiable cases instead of inventing safety certifications.
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timezone

from uagents import Agent, Context

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "src"))

from lastmile.config import configure_logging, load_settings  # noqa: E402
from lastmile.models.donation import intake_from_json  # noqa: E402
from lastmile.protocols import SpecialistRequest, SpecialistRequestPayload, SpecialistResponse, publish_address  # noqa: E402
from lastmile.services import SafetyValidatorService  # noqa: E402

logger = logging.getLogger("lastmile.safety")
settings = load_settings()
configure_logging(settings.log_level)

service = SafetyValidatorService()

safety_agent = Agent(
    name="lastmile_safety_agent",
    seed=os.getenv("SAFETY_AGENT_SEED", "lastmile-safety-agent-demo-seed"),
    port=settings.port + 1,
    endpoint=[f"http://127.0.0.1:{settings.port + 1}/submit"],
    mailbox=True,
    publish_agent_details=False,
)


@safety_agent.on_event("startup")
async def startup(ctx: Context) -> None:
    publish_address("safety_agent", safety_agent.address)
    ctx.logger.info(f"safety agent ready at {safety_agent.address}")


@safety_agent.on_message(model=SpecialistRequest, replies=SpecialistResponse)
async def handle_request(ctx: Context, sender: str, request: SpecialistRequest) -> None:
    try:
        payload = SpecialistRequestPayload.parse_raw(request.payload_json)
    except ValueError as exc:
        await ctx.send(sender, SpecialistResponse(request_id=request.request_id, ok=False, error=f"bad payload: {exc}"))
        return

    intake, errors = intake_from_json(payload.donation_json)
    if intake is None or errors:
        await ctx.send(
            sender,
            SpecialistResponse(
                request_id=request.request_id,
                ok=False,
                error=f"invalid donation: {'; '.join(errors or ['unknown'])}",
            ),
        )
        return

    decision = service.validate(intake, now=datetime.now(timezone.utc))
    await ctx.send(
        sender,
        SpecialistResponse(
            request_id=request.request_id,
            ok=True,
            result_json=decision.model_dump_json(),
        ),
    )
    ctx.logger.info(
        "checked %s -> approved=%s (%d reasons)",
        intake.donation_id,
        decision.approved,
        len(decision.reasons),
    )


if __name__ == "__main__":
    safety_agent.run()
