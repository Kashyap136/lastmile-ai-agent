"""Programmatic Agentverse registration for the coordinator agent.

Registers the locally-running coordinator agent as a chat agent so it
appears on Agentverse and can be used through ASI:One.

Usage:
    set AGENTVERSE_KEY=<key>       (from https://agentverse.ai/profile/api-keys)
    set AGENT_SEED_PHRASE=<phrase> (determines the agent address)
    python scripts/register_agent.py

The agent must also be running locally (python -m lastmile.agents.coordinator)
so it appears Active and can answer ASI:One interactions via the mailbox.
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from uagents_core.utils.registration import (  # noqa: E402
    RegistrationRequestCredentials,
    register_chat_agent,
)


def main() -> int:  # pragma: no cover - requires real credentials
    api_key = os.getenv("AGENTVERSE_KEY", "")
    seed = os.getenv("AGENT_SEED_PHRASE", "")
    name = os.getenv("AGENT_NAME", "lastmile_rescue_coordinator")
    port = int(os.getenv("PORT", "8000"))

    if not api_key or not seed:
        print(
            "ERROR: AGENTVERSE_KEY and AGENT_SEED_PHRASE must be set.\n"
            "Get an API key at https://agentverse.ai/profile/api-keys - see .env.example"
        )
        return 2

    # the mailbox submit endpoint the agent exposes locally
    endpoint = os.getenv("LOCAL_AGENT_ENDPOINT", f"http://127.0.0.1:{port}/submit")

    readme_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "README.md")
    readme = ""
    if os.path.exists(readme_path):
        with open(readme_path, "r", encoding="utf-8") as fh:
            readme = fh.read()

    description = (
        "Autonomous surplus-food rescue coordinator: validates donations, "
        "matches recipient organisations and coordinates feasible pickups "
        "through interactive chat cards."
    )
    starters = [
        "I have surplus food to donate",
        "Check my donation status",
        "What can you rescue for me?",
    ]

    ok = register_chat_agent(
        name,
        endpoint,
        active=True,
        credentials=RegistrationRequestCredentials(
            agentverse_api_key=api_key,
            agent_seed_phrase=seed,
        ),
        readme=readme or description,
        description=description,
        starter_prompts=starters,
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
    )

    if ok:
        print("Registration succeeded - agent should appear on Agentverse and ASI:One.")
        print("Keep the coordinator process running for it to stay 'Active'.")
        return 0
    print("Registration failed - see logs above.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
