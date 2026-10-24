"""Central configuration loaded from environment variables.

Secrets and optional service credentials always come from the environment
(see .env.example). Nothing here hardcodes credentials.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

try:  # optional, only needed when running agents from a .env file
    from dotenv import load_dotenv

    load_dotenv()  # no-op if .env does not exist
except ImportError:  # pragma: no cover - dotenv is in requirements.txt
    pass


@dataclass
class Settings:
    """Runtime configuration for all agents."""

    # Agent identity
    agent_name: str = "lastmile_rescue_coordinator"
    seed_phrase: str = ""
    api_key: str = ""

    # Local server
    port: int = 8000
    log_level: str = "INFO"

    # Topology: "single" keeps everything in the coordinator process;
    # "multi" delegates to the specialist agents via agent messaging.
    topology: str = "single"
    safety_agent_address: str = ""
    matcher_agent_address: str = ""
    planner_agent_address: str = ""

    # Optional external services
    routing_api_url: str = ""
    recipient_notify_webhook: str = ""

    # Behavioural knobs (documented, no secrets)
    remote_call_timeout_seconds: float = 20.0
    remote_call_retries: int = 2
    session_ttl_seconds: int = 6 * 60 * 60  # stale sessions expire after 6h
    max_card_payload_bytes: int = 64 * 1024

    def __post_init__(self) -> None:
        self.seed_phrase = self.seed_phrase or os.getenv("AGENT_SEED_PHRASE", "")
        self.api_key = self.api_key or os.getenv("AGENTVERSE_KEY", "")
        self.port = int(os.getenv("PORT", self.port))
        self.log_level = os.getenv("LOG_LEVEL", self.log_level).upper()
        self.topology = os.getenv("AGENT_TOPOLOGY", self.topology).lower()
        self.safety_agent_address = os.getenv("SAFETY_AGENT_ADDRESS", "")
        self.matcher_agent_address = os.getenv("MATCHER_AGENT_ADDRESS", "")
        self.planner_agent_address = os.getenv("PLANNER_AGENT_ADDRESS", "")
        self.routing_api_url = os.getenv("ROUTING_API_URL", "").rstrip("/")
        self.recipient_notify_webhook = os.getenv("RECIPIENT_NOTIFY_WEBHOOK", "")

        if self.topology not in ("single", "multi"):
            raise ValueError(
                f"AGENT_TOPOLOGY must be 'single' or 'multi', got {self.topology!r}"
            )

    @property
    def registration_configured(self) -> bool:
        """True when an Agentverse API key + seed are present for registration."""
        return bool(self.api_key) and bool(self.seed_phrase)


def load_settings() -> Settings:
    return Settings()


def configure_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
