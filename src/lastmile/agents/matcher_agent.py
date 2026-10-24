"""Recipient Matcher agent (Layer 3).

Matches eligible donations against the clearly-labelled demo recipient
dataset, considering eligibility, capacity, food suitability, distance
and pickup deadlines. Ranking is always explainable.
"""

from __future__ import annotations

import logging
import os
import sys

from uagents import Agent, Context

sys.path.insert(
    0,
    os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")),
)

from lastmile.config import configure_logging, load_settings  # noqa: E402
from lastmile.data.zones import DEMO_ZONES  # noqa: E402
from lastmile.models.donation import intake_from_json  # noqa: E402
from lastmile.models.recipient import load_demo_recipients  # noqa: E402
from lastmile.protocols import SpecialistRequest, SpecialistRequestPayload, SpecialistResponse, publish_address  # noqa: E402
from lastmile.services import RecipientMatcher  # noqa: E402

logger = logging.getLogger("lastmile.matcher")
settings = load_settings()
configure_logging(settings.log_level)

matcher_service = RecipientMatcher(
    load_demo_recipients(),
    donor_zone_centers=DEMO_ZONES,
)

matcher_agent = Agent(
    name="lastmile_matcher_agent",
    seed=os.getenv("MATCHER_AGENT_SEED", "lastmile-matcher-agent-demo-seed"),
    port=settings.port + 2,
    endpoint=[f"http://127.0.0.1:{settings.port + 2}/submit"],
    mailbox=True,
    publish_agent_details=False,
)


@matcher_agent.on_event("startup")
async def startup(ctx: Context) -> None:
    publish_address("matcher_agent", matcher_agent.address)
    ctx.logger.info(f"matcher agent ready at {matcher_agent.address}")


@matcher_agent.on_message(model=SpecialistRequest, replies=SpecialistResponse)
async def handle_request(ctx: Context, sender: str, request: SpecialistRequest) -> None:
    try:
        payload = SpecialistRequestPayload.parse_raw(request.payload_json)
    except ValueError as exc:
        await ctx.send(
            sender,
            SpecialistResponse(request_id=request.request_id, ok=False, error=f"bad payload: {exc}"),
        )
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

    result = matcher_service.match(intake)
    await ctx.send(
        sender,
        SpecialistResponse(
            request_id=request.request_id,
            ok=True,
            result_json=result.model_dump_json(),
        ),
    )
    ctx.logger.info(
        "matched %s -> %d candidate(s) of %d considered",
        intake.donation_id,
        len(result.matches),
        result.considered_count,
    )


if __name__ == "__main__":
    matcher_agent.run()
