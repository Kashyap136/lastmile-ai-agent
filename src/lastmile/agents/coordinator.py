"""The LastMile Rescue coordinator agent (Layer 1+2).

* Runs locally with mailbox access so it is reachable from Agentverse and
  usable through ASI:One.
* Implements the Agent Chat Protocol (ACP) v0.3.0 via uagents_core.
* Persists session-first workflow state in ctx.storage (recoverable across
  restarts) and delegates specialist logic through the tools layer, which
  either runs in-process (single topology) or exchanges structured
  request/response messages with the Layer-3 specialist agents (multi).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timezone
from uuid import uuid4

from uagents import Agent, Context, Model, Protocol
from uagents.communication import send_message_raw
from uagents_core.contrib.protocols.chat import (
    ChatAcknowledgement,
    ChatMessage,
    chat_protocol_spec,
)
from uagents_core.identity import Identity
from uagents_core.types import DeliveryStatus

# make `lastmile` importable when running this file directly as a script:
# the file lives at <repo>/src/lastmile/agents/coordinator.py, so the package
# parent directory (`<repo>/src`) is exactly two levels up.
sys.path.insert(
    0,
    os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")),
)

from lastmile.adapters.notify import notify_recipient  # noqa: E402
from uagents_core.config import AgentverseConfig  # noqa: E402
from lastmile.chat.messages import (  # noqa: E402
    ack_message,
    card_message,
    first_text,
    text_message,
)
from lastmile.config import configure_logging, load_settings  # noqa: E402
from lastmile.data.zones import DEMO_ZONES  # noqa: E402
from lastmile.models.recipient import load_demo_recipients  # noqa: E402
from lastmile.protocols import (  # noqa: E402
    SpecialistRequest,
    SpecialistRequestPayload,
    SpecialistResponse,
    publish_address,
    read_address,
)
from lastmile.services import PickupPlanner, RecipientMatcher, SafetyValidatorService  # noqa: E402
from lastmile.workflow import (  # noqa: E402
    LocalSpecialistTools,
    RemoteSpecialistTools,
    SessionState,
    WorkflowEngine,
)

logger = logging.getLogger("lastmile.coordinator")

# ----------------------------------------------------------------------
# wiring
# ----------------------------------------------------------------------

settings = load_settings()
configure_logging(settings.log_level)

# the Agent class binds to the current event loop at construction time; the
# identity is derived from the seed only (agent name does not affect it)
_coordinator_seed = settings.seed_phrase or "lastmile-rescue-coordinator-demo-seed"

agents_recipients = load_demo_recipients()
matcher = RecipientMatcher(agents_recipients, donor_zone_centers=DEMO_ZONES)
planner = PickupPlanner(agents_recipients, donor_zone_centers=DEMO_ZONES, routing_api_url=settings.routing_api_url)
validator = SafetyValidatorService()
local_tools = LocalSpecialistTools(validator, matcher, planner)

notify_fn = lambda plan_dict: notify_recipient(plan_dict, settings.recipient_notify_webhook)

pending_specialist_requests: dict[str, asyncio.Future] = {}

# per-message serialization of storage writes (see handle_messages_concurrently)
_chat_locks: dict[str, asyncio.Lock] = {}


def chat_lock_for(user_key: str) -> asyncio.Lock:
    return _chat_locks.setdefault(user_key, asyncio.Lock())


async def remote_request(specialist: str, payload_json: str) -> dict | None:
    """Transport used by RemoteSpecialistTools: send + await the response.

    Any transport failure (unknown address, undeliverable request, crash or
    timeout) resolves to None so the tools layer can apply its timeout and
    retry policy deterministically - exceptions never escape this shim.
    """
    address = {
        "safety": settings.safety_agent_address or read_address("safety_agent"),
        "matcher": settings.matcher_agent_address or read_address("matcher_agent"),
        "planner": settings.planner_agent_address or read_address("planner_agent"),
    }[specialist]
    if not address:
        return None
    request_id = uuid4().hex
    envelope = SpecialistRequest(
        kind=specialist,
        request_id=request_id,
        payload_json=payload_json,
    )
    loop = asyncio.get_running_loop()
    future: asyncio.Future = loop.create_future()
    pending_specialist_requests[request_id] = future
    try:
        status = await send_message_raw(
            address,
            SPECIALIST_REQUEST_DIGEST,
            envelope.model_dump_json(),
            sender=coordinator_identity,
        )
        if getattr(status, "status", None) is not DeliveryStatus.DELIVERED:
            logger.warning(
                "specialist request %s to %s not delivered: %s",
                request_id,
                specialist,
                getattr(status, "detail", status),
            )
            return None
        return await future
    except Exception as exc:
        logger.warning("specialist transport failed for %s: %r", specialist, exc)
        return None
    finally:
        pending_specialist_requests.pop(request_id, None)


if settings.topology == "multi":
    tools = RemoteSpecialistTools(
        remote_request,
        timeout_seconds=settings.remote_call_timeout_seconds,
        retries=settings.remote_call_retries,
        fallback=local_tools,  # explicit, labelled fallback keeps the demo usable
    )
else:
    tools = local_tools

engine = WorkflowEngine(tools, notify_fn=notify_fn)

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))

# ASI:One delivery goes through the Agentverse mailbox. Local smoke/test runs
# opt out with LASTMILE_USE_MAILBOX=0 so they neither run the mailbox client
# nor touch the production agent's mailbox registration.
_mailbox_enabled = os.getenv("LASTMILE_USE_MAILBOX", "1").strip().lower() not in ("0", "false", "no")
_endpoints: list[str] = [f"http://127.0.0.1:{settings.port}/submit"]
if _mailbox_enabled:
    _endpoints.append(AgentverseConfig().mailbox_endpoint)

coordinator_agent = Agent(
    name=settings.agent_name,
    seed=_coordinator_seed,
    port=settings.port,
    # two delivery paths, both registered in the almanac record:
    # - local:  same-machine agent-to-agent delivery (specialist agents, smoke client)
    # - mailbox: Agentverse-hosted delivery so ASI:One chat messages reach this agent
    # Including the mailbox URL in the endpoint list is what actually starts
    # MailboxClient (uagents checks the endpoint list, not the mailbox flag);
    # with an explicit local endpoint alone the mailbox client never runs and
    # Agentverse cannot route chat messages ("unable to route envelope").
    # Smoke runs override LASTMILE_USE_MAILBOX=0 and drop the mailbox path.
    endpoint=_endpoints,
    publish_agent_details=True,
    readme_path=os.path.join(_REPO_ROOT, "README.md"),
    # published with every boot so the public Agentverse/almanac profile stays
    # complete regardless of registration order (register_agent.py sets the
    # same values; the runtime registration previously reset them to null).
    description=(
        "Autonomous surplus-food rescue coordinator: validates donations, "
        "matches recipient organisations and coordinates feasible pickups "
        "through interactive chat cards."
    ),
    metadata={
        "display_name": "LastMile Rescue",
        "short_description": "Coordinates surplus-food rescue pickups end-to-end.",
        "long_description": (
            "LastMile Rescue validates surplus-food donations against "
            "structured safety rules, matches them with recipient "
            "organisations (clearly-labelled demo dataset), proposes "
            "feasible pickup windows with labelled travel estimates and "
            "tracks each donation's status trail."
        ),
        "recommended_use_case": (
            "Donate surplus food safely: describe the food, approve a "
            "recipient and a pickup window, and track the rescue."
        ),
    },
    # chat turns may await remote specialist responses; when the specialist
    # reply arrives (a separate message), it must be dispatched while the
    # chat turn is still waiting - storage writes stay strictly ordered per
    # user through chat_lock_for().
    handle_messages_concurrently=True,
)

# address consistency is a wire-level requirement: specialist agents reply
# to `coordinator_identity.address`, which must equal coordinator_agent.address
coordinator_identity = Identity.from_seed(_coordinator_seed, 0)
assert coordinator_identity.address == coordinator_agent.address
SPECIALIST_REQUEST_DIGEST = Model.build_schema_digest(SpecialistRequest)

chat_proto = Protocol(spec=chat_protocol_spec)


# ----------------------------------------------------------------------
# ACP handlers (ASI:One compatible)
# ----------------------------------------------------------------------


def _load_session(ctx: Context, user_key: str) -> SessionState:
    raw = ctx.storage.get(f"session:{user_key}")
    if raw:
        return SessionState.model_validate(raw)
    return SessionState(user_address=user_key)


def _save_session(ctx: Context, state: SessionState) -> None:
    ctx.storage.set(
        f"session:{state.user_address}",
        state.model_dump(mode="json"),
    )


@coordinator_agent.on_event("startup")
async def startup(ctx: Context) -> None:
    ctx.logger.info(
        f"LastMile Rescue coordinator started. address={coordinator_agent.address}"
    )
    if settings.topology == "multi":
        missing = [
            name
            for name, addr in {
                "safety": settings.safety_agent_address or read_address("safety_agent"),
                "matcher": settings.matcher_agent_address or read_address("matcher_agent"),
                "planner": settings.planner_agent_address or read_address("planner_agent"),
            }.items()
            if not addr
        ]
        if missing:
            ctx.logger.warning(
                "multi-agent topology active but missing addresses for: %s "
                "(calls will fail explicitly and offer a labelled fallback)",
                ", ".join(missing),
            )


@chat_proto.on_message(ChatMessage)
async def on_chat_message(ctx: Context, sender: str, msg: ChatMessage) -> None:
    # every incoming ChatMessage is acknowledged (protocol requirement)
    await ctx.send(sender, ack_message(msg.msg_id))

    text = first_text(msg)
    if not text.strip():
        return

    # messages process concurrently (required so specialist responses can be
    # dispatched while a chat turn awaits) - storage reads/writes stay
    # strictly ordered per user through this lock
    async with chat_lock_for(sender):
        state = _load_session(ctx, sender)
        result = await engine.handle_text(state, text)
        _save_session(ctx, result.state)

    for output in result.outputs:
        if output.has_card():
            await ctx.send(sender, card_message(output.text, output.card_kind, output.card_payload))
        else:
            await ctx.send(sender, text_message(output.text))

    ctx.logger.info(
        "handled chat turn for %s -> %d output(s), stage=%s status=%s",
        sender[:12],
        len(result.outputs),
        state.stage.value,
        state.history[-1].status.value if state.history else "-",
    )


@chat_proto.on_message(ChatAcknowledgement)
async def on_chat_ack(ctx: Context, sender: str, msg: ChatAcknowledgement) -> None:
    ctx.logger.debug("ack from %s for %s", sender[:12], msg.acknowledged_msg_id)


# ----------------------------------------------------------------------
# specialist request/response handling (multi topology)
# ----------------------------------------------------------------------


@coordinator_agent.on_message(model=SpecialistResponse)
async def on_specialist_response(ctx: Context, sender: str, msg: SpecialistResponse) -> None:
    future = pending_specialist_requests.get(msg.request_id)
    if future is not None and not future.done():
        future.set_result(
            {
                "ok": msg.ok,
                "error": msg.error,
                "result_json": msg.result_json,
                "detail": msg.detail,
                "fallback": msg.fallback,
            }
        )
    else:
        ctx.logger.warning("stale specialist response for %s", msg.request_id)


coordinator_agent.include(chat_proto, publish_manifest=True)


def main() -> None:  # pragma: no cover - entrypoint
    if __debug__ and not settings.seed_phrase:
        logger.warning(
            "AGENT_SEED_PHRASE not set - using a demo default seed so addresses "
            "stay deterministic; set a private phrase for real deployments"
        )
    coordinator_agent.run()


if __name__ == "__main__":
    main()
