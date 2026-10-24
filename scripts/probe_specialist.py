"""Manual probe: send one SpecialistRequest to a live matcher agent.

Isolates where agent-to-agent message flow breaks:
1. does the specialist's ASGI layer accept/verify the request envelope?
2. does the handler dispatch (the agent logs "matched *")?
3. does the signed SpecialistResponse envelope come back to the sender?

Usage:
    $env:PYTHONPATH="src"
    python -W ignore scripts/probe_specialist.py
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from uuid import uuid4

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "src"))

PROBE_PORT = int(os.getenv("PROBE_PORT", "8062"))
MATCHER_PORT = PROBE_PORT + 1
BOOK = os.path.join(tempfile.gettempdir(), "lastmile_probe_address_book.json")

DONATION = {
    "donation_id": "probe-d1",
    "category": "prepared_meal",
    "description": "40 fresh vegan noodle soup bowls",
    "quantity": 40.0,
    "unit": "meals",
    "prepared_at": None,   # set below
    "pickup_deadline": None,
    "location": "42 Food Street, Fairview (demo)",
    "zone": "Riverside",
    "storage": "refrigerated",
    "handling_notes": "sealed containers",
}


async def main() -> int:
    from uagents import Agent
    from uagents.communication import send_message_raw
    from uagents.protocol import Model
    from uagents_core.identity import Identity
    from lastmile.models.donation import DonationIntake
    from lastmile.protocols import (
        SpecialistRequest,
        SpecialistRequestPayload,
        SpecialistResponse,
    )

    request_digest = Model.build_schema_digest(SpecialistRequest)
    probe_seed = os.getenv("PROBE_SEED", "lastmile-probe-sender")

    if os.path.exists(BOOK):
        try:
            os.remove(BOOK)
        except OSError:
            pass

    env = dict(os.environ)
    env["PYTHONPATH"] = os.path.join(REPO_ROOT, "src")
    env["PORT"] = str(PROBE_PORT)  # matcher uses PORT+1 internally
    env["LASTMILE_ADDRESS_BOOK"] = BOOK

    matcher_proc = subprocess.Popen(
        [sys.executable, os.path.join(REPO_ROOT, "src", "lastmile", "agents", "matcher_agent.py")],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=env,
        text=True,
    )

    try:
        # wait for the address book entry written at module import
        deadline = asyncio.get_event_loop().time() + 20
        matcher_address = None
        while matcher_address is None and asyncio.get_event_loop().time() < deadline:
            if os.path.exists(BOOK):
                try:
                    book = json.loads(open(BOOK, encoding="utf-8").read())
                    matcher_address = book.get("matcher_agent")
                except (ValueError, OSError):
                    pass
            if matcher_address is None:
                await asyncio.sleep(0.5)
        if matcher_address is None:
            print("PROBE FAILED: no matcher_agent address in book; logs:")
            print(await asyncio.to_thread(lambda: matcher_proc.stdout.read()))
            return 1
        print(f"[probe] matcher address: {matcher_address}")

        def pump():
            for line in iter(matcher_proc.stdout.readline, ""):
                print(f"[matcher] {line.strip()[:160]}")

        asyncio.get_event_loop().run_in_executor(None, pump)

        # probe agent registers on Almanac REST so the matcher can reply to it
        probe = Agent(
            name="specialist_probe",
            seed=probe_seed,
            port=PROBE_PORT,
            endpoint=[f"http://127.0.0.1:{PROBE_PORT}/submit"],
        )
        gauge: dict[str, object] = {"response": None}

        @probe.on_message(model=SpecialistResponse)
        async def got_response(ctx, sender: str, msg: SpecialistResponse):
            gauge["response"] = msg
            print(f"[probe] GOT SpecialistResponse: ok={msg.ok} "
                  f"detail={msg.detail!r} fallback={msg.fallback}")

        probe_task = asyncio.create_task(probe.run_async())
        await asyncio.sleep(6)  # let its almanac REST registration complete

        now = datetime.now(timezone.utc)
        intake = DonationIntake(
            donation_id="probe-d1",
            category="prepared_meal",
            description="40 fresh vegan noodle soup bowls",
            quantity=40.0,
            unit="meals",
            prepared_at=now - timedelta(minutes=45),
            pickup_deadline=now + timedelta(hours=6),
            location="42 Food Street, Fairview (demo)",
            zone="Riverside",
            storage="refrigerated",
            handling_notes="sealed containers",
        )
        payload = SpecialistRequestPayload(donation_json=intake.model_dump_json())
        request = SpecialistRequest(
            kind="matcher",
            request_id=uuid4().hex,
            payload_json=payload.model_dump_json(),
        )
        print(f"[probe] request digest: {request_digest}")

        identity = Identity.from_seed(probe_seed, 0)
        status = await send_message_raw(
            matcher_address,
            request_digest,
            request.model_dump_json(),
            sender=identity,
        )
        print(f"[probe] delivery status: {getattr(status, 'status', status)} "
              f"detail={getattr(status, 'detail', '')!r}")

        await asyncio.sleep(10)
        if gauge["response"] is None:
            print("PROBE INCONCLUSIVE: no response within 10s "
                  "(check whether [matcher] logged a request outcome above)")
            return 2
        print("PROBE OK: round-trip SpecialistRequest -> SpecialistResponse works")
        return 0
    finally:
        matcher_proc.terminate()


if __name__ == "__main__":
    code = asyncio.run(main())
    os._exit(code)
