"""Pickup Planner agent (Layer 3).

Evaluates feasible pickup windows, travel estimates and route notes.
Uses a real routing API when ROUTING_API_URL is configured, otherwise
returns clearly-labelled calculated estimates. Returns the proposed plan
for user approval - it never declares a pickup confirmed by itself.
"""

from __future__ import annotations

import logging
import os
import sys

from uagents import Agent, Context

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "src"))

from lastmile.config import configure_logging, load_settings  # noqa: E402
from lastmile.data.zones import DEMO_ZONES  # noqa: E402
from lastmile.models.donation import intake_from_json  # noqa: E402
from lastmile.models.pickup import PickupProposal  # noqa: E402
from lastmile.models.recipient import load_demo_recipients  # noqa: E402
from lastmile.protocols import SpecialistRequest, SpecialistRequestPayload, SpecialistResponse, publish_address  # noqa: E402
from lastmile.services import PickupPlanner  # noqa: E402

logger = logging.getLogger("lastmile.planner")
settings = load_settings()
configure_logging(settings.log_level)

planner_service = PickupPlanner(
    load_demo_recipients(),
    donor_zone_centers=DEMO_ZONES,
    routing_api_url=settings.routing_api_url,
)

planner_agent = Agent(
    name="lastmile_planner_agent",
    seed=os.getenv("PLANNER_AGENT_SEED", "lastmile-planner-agent-demo-seed"),
    port=settings.port + 3,
    endpoint=[f"http://127.0.0.1:{settings.port + 3}/submit"],
    mailbox=True,
    publish_agent_details=False,
)


@planner_agent.on_event("startup")
async def startup(ctx: Context) -> None:
    publish_address("planner_agent", planner_agent.address)
    ctx.logger.info(f"planner agent ready at {planner_agent.address}")


@planner_agent.on_message(model=SpecialistRequest, replies=SpecialistResponse)
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

    if not payload.recipient_id:
        await ctx.send(
            sender,
            SpecialistResponse(
                request_id=request.request_id,
                ok=False,
                error="payload missing recipient_id",
            ),
        )
        return

    if payload.proposal_json:
        # plan build for an already-chosen window
        try:
            proposal = PickupProposal.parse_raw(payload.proposal_json)
        except ValueError as exc:
            await ctx.send(
                sender,
                SpecialistResponse(
                    request_id=request.request_id,
                    ok=False,
                    error=f"invalid proposal payload: {exc}",
                ),
            )
            return
        plan, detail = planner_service.build_plan(intake, proposal, payload.window_id)
        result = plan.model_dump_json() if plan else "null"
        await ctx.send(
            sender,
            SpecialistResponse(
                request_id=request.request_id,
                ok=True,
                result_json=result,
                detail=detail,
            ),
        )
        ctx.logger.info("built plan for %s: %s", intake.donation_id, detail or "ok")
        return

    # propose windows for a recipient
    proposal, detail = planner_service.propose(intake, payload.recipient_id)
    result = proposal.model_dump_json() if proposal else "null"
    await ctx.send(
        sender,
        SpecialistResponse(
            request_id=request.request_id,
            ok=True,
            result_json=result,
            detail=detail,
        ),
    )
    ctx.logger.info(
        "proposed %s -> %s",
        intake.donation_id,
        f"{len(proposal.windows)} window(s)" if proposal else f"infeasible: {detail}",
    )


if __name__ == "__main__":
    planner_agent.run()
