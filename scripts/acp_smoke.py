"""Local end-to-end ACP smoke test (no Agentverse account required).

Runs the real coordinator agent as a subprocess (exactly like production)
plus a real chat-protocol client agent that walks the complete donation
workflow over protocol envelopes: greeting -> form card -> submission ->
carousel -> recipient selection -> window detail -> review -> approval.

The client is a plain uAgent (same seed derivation as production code):
it registers its endpoints with the Almanac REST API on startup so the
coordinator can resolve it for replies, and it delivers messages with
``uagents.communication.send_message_raw`` carrying the ChatMessage schema
digest (the same wire shape ASI:One uses).

Usage:
    $env:PYTHONPATH="src"
    python scripts\acp_smoke.py
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from uuid import uuid4

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "src"))

SMOKE_PORT = int(os.getenv("SMOKE_PORT", "8020"))
CLIENT_PORT = SMOKE_PORT + 5
CLIENT_SEED = "acp-smoke-client-seed"
ACK_TIMEOUT = 60.0  # seconds per step


# ----------------------------------------------------------------------
# client agent (constructed inside the running loop)
# ----------------------------------------------------------------------

def build_client():
    from uagents import Agent, Protocol
    from uagents_core.contrib.protocols.chat import (
        ChatAcknowledgement,
        ChatMessage,
        chat_protocol_spec,
    )

    client = Agent(
        name="acp_smoke_client",
        seed=CLIENT_SEED,
        port=CLIENT_PORT,
        endpoint=[f"http://127.0.0.1:{CLIENT_PORT}/submit"],
    )
    chat_proto = Protocol(spec=chat_protocol_spec)
    queue: asyncio.Queue = asyncio.Queue()

    @chat_proto.on_message(ChatMessage)
    async def on_message(ctx, sender: str, msg: ChatMessage):
        await queue.put(msg)

    @chat_proto.on_message(ChatAcknowledgement)
    async def on_ack(ctx, sender: str, msg: ChatAcknowledgement):
        pass  # acknowledgements carry no card/text payload; not queued

    client.include(chat_proto, publish_manifest=False)
    return client, queue


CHAT_DIGEST = None  # ChatMessage schema digest, set in main()


def extract_card(msg):
    from uagents_core.contrib.protocols.chat import MetadataContent

    for item in msg.content:
        if isinstance(item, MetadataContent) and (
            item.metadata.get("requires_card_interaction") == "true"
        ):
            return item.metadata.get("card_kind"), json.loads(
                item.metadata.get("card_payload", "{}")
            )
    return None, None


async def main() -> int:
    global CHAT_DIGEST

    from uagents.communication import send_message_raw
    from uagents_core.contrib.protocols.chat import ChatMessage, TextContent
    from uagents_core.identity import Identity
    from uagents.protocol import Model
    from uagents.resolver import GlobalResolver
    from uagents_core.types import DeliveryStatus

    CHAT_DIGEST = Model.build_schema_digest(ChatMessage)
    identity = Identity.from_seed(CLIENT_SEED, 0)
    resolver = GlobalResolver()

    async def wait_resolvable(address: str, label: str, timeout: float = 60.0):
        """Poll Almanac REST until `address` resolves to at least one endpoint."""
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            resolved_address, endpoints = await resolver.resolve(address)
            if endpoints:
                print(f"[smoke] {label} is registered ({len(endpoints)} endpoint(s))")
                return
            await asyncio.sleep(2.0)
        raise TimeoutError(f"{label} never became resolvable within {timeout}s")

    # 1) launch the real coordinator as a subprocess
    env = dict(os.environ)
    env["PYTHONPATH"] = os.path.join(REPO_ROOT, "src")
    env["PORT"] = str(SMOKE_PORT)
    proc = subprocess.Popen(
        [sys.executable, os.path.join(REPO_ROOT, "src", "lastmile", "agents", "coordinator.py")],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=env,
        text=True,
    )
    coordinator_address = None
    try:
        # read startup logs for the agent address
        deadline = asyncio.get_event_loop().time() + 40
        while coordinator_address is None and asyncio.get_event_loop().time() < deadline:
            line = await asyncio.to_thread(proc.stdout.readline)  # type: ignore[union-attr]
            if not line:
                break
            line_s = line.strip()
            print(f"[coordinator] {line_s[:150]}")
            if "Starting agent with address:" in line_s:
                coordinator_address = line_s.split("Starting agent with address:")[1].split()[0]
        if coordinator_address is None:
            print("SMOKE FAILED: coordinator did not report its address")
            return 1
        print(f"\n[smoke] coordinator address: {coordinator_address}\n")

        client, queue = build_client()
        assert client.address == identity.address, (
            f"client identity mismatch: {client.address} != {identity.address}"
        )

        client_task = asyncio.create_task(client.run_async())
        client_task.add_done_callback(
            lambda t: t.cancelled() is None and not t.cancelled() and t.exception()
            and print(f"[client] task crashed: {t.exception()!r}")
        )

        # the coordinator must be dialable, and the client must be registered
        # with Almanac REST so the coordinator can deliver replies back to it
        await wait_resolvable(coordinator_address, "coordinator")
        await wait_resolvable(client.address, "smoke client")
        await asyncio.sleep(1.0)

        async def send(text: str) -> None:
            msg = ChatMessage(
                timestamp=datetime.now(timezone.utc),
                msg_id=uuid4(),
                content=[TextContent(type="text", text=text)],
            )
            status = await send_message_raw(
                coordinator_address,
                CHAT_DIGEST,
                msg.model_dump_json(),
                sender=identity,
            )
            if getattr(status, "status", None) is DeliveryStatus.FAILED:
                raise RuntimeError(f"delivery failed: {getattr(status, 'detail', status)!r}")

        async def drain_queue() -> None:
            while not queue.empty():
                queue.get_nowait()

        async def next_card(timeout=ACK_TIMEOUT):
            deadline = asyncio.get_event_loop().time() + timeout
            while True:
                remaining = deadline - asyncio.get_event_loop().time()
                if remaining <= 0:
                    raise TimeoutError(f"no card received within {timeout}s")
                msg = await asyncio.wait_for(queue.get(), timeout=remaining)
                kind, payload = extract_card(msg)
                if kind is not None:
                    return kind, payload

        async def next_text(timeout=ACK_TIMEOUT):
            deadline = asyncio.get_event_loop().time() + timeout
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                raise TimeoutError(f"no text received within {timeout}s")
            msg = await asyncio.wait_for(queue.get(), timeout=remaining)
            return "\n".join(
                item.text for item in msg.content
                if isinstance(item, TextContent)
            )

        # 1) greeting -> form card
        await drain_queue()
        await send("hi")
        kind, _ = await next_card()
        assert kind == "form", f"expected form card, got {kind}"
        print("[smoke] greeting -> form card OK")

        # 2) donation submission (JSON direct-mention shape)
        now = datetime.now(timezone.utc)
        selection = {
            "action": "submit_donation",
            "category": "prepared_meal",
            "description": "40 fresh vegan noodle soup bowls",
            "quantity": "40",
            "unit": "meals",
            "prepared_at": (now - timedelta(minutes=45)).isoformat(),
            "pickup_deadline": (now + timedelta(hours=6)).isoformat(),
            "location": "42 Food Street, Fairview (demo)",
            "zone": "Riverside",
            "storage": "refrigerated",
            "handling_notes": "sealed containers, keep upright",
        }
        await drain_queue()
        await send(json.dumps(selection))
        kind, payload = await next_card()
        assert kind == "carousel", f"expected carousel, got {kind}"
        items = payload["items"]
        print(f"[smoke] submission -> carousel with {len(items)} match(es) OK")
        for item in items:
            print(f"        - {item['title']} ({item['subtitle']})")

        # 3) select recipient -> window detail
        chosen = items[0]["primary_cta"]["selection"]["recipient_id"]
        await drain_queue()
        await send(json.dumps({"action": "select_recipient", "recipient_id": chosen}))
        kind, payload = await next_card()
        assert kind == "detail", f"expected detail, got {kind}"
        window_id = payload["sub_options"]["choices"][0]["value"]
        print(f"[smoke] recipient {chosen} -> window detail OK ({window_id} proposed)")

        # 4) window selection -> review
        await drain_queue()
        await send(json.dumps({"action": "select_window", "window_id": window_id}))
        kind, payload = await next_card()
        assert kind == "review", f"expected review, got {kind}"
        print("[smoke] window selection -> review card OK")

        # 5) approve -> honest receipt
        await drain_queue()
        await send(json.dumps({"action": "approve_plan"}))
        text = await next_text()
        assert "Approved" in text, text
        assert "awaiting_recipient_confirmation" in text, text
        print("[smoke] approval -> honest receipt OK")

        # 6) status query
        await drain_queue()
        await send("status")
        status_text = await next_text()
        assert "current status" in status_text.lower(), status_text
        print("[smoke] status query OK")

        print("\nALL ACP SMOKE STEPS PASSED")
        return 0
    except Exception as exc:
        print(f"\nSMOKE FAILED: {exc!r}")
        return 1
    finally:
        proc.terminate()  # best effort on all platforms


if __name__ == "__main__":
    code = asyncio.run(main())
    os._exit(code)  # kill remaining uAgents loop-bound tasks cleanly
