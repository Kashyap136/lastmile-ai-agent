"""Shared test fixtures and helpers (no network, no credentials)."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from lastmile.data.zones import DEMO_ZONES
from lastmile.models.recipient import load_demo_recipients
from lastmile.services import PickupPlanner, RecipientMatcher, SafetyValidatorService
from lastmile.workflow import LocalSpecialistTools, WorkflowEngine


@pytest.fixture
def recipients():
    return load_demo_recipients()


@pytest.fixture
def service_tools(recipients):
    return LocalSpecialistTools(
        SafetyValidatorService(),
        RecipientMatcher(recipients, donor_zone_centers=DEMO_ZONES),
        PickupPlanner(recipients, donor_zone_centers=DEMO_ZONES),
    )


@pytest.fixture
def engine(service_tools):
    return WorkflowEngine(service_tools)


def valid_selection(now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    return {
        "action": "submit_donation",
        "category": "prepared_meal",
        "description": "40 fresh vegan noodle soup bowls",
        "quantity": "40",
        "unit": "meals",
        "prepared_at": (now - timedelta(minutes=45)).isoformat(),
        "pickup_deadline": (now + timedelta(hours=4)).isoformat(),
        "location": "42 Food Street, Fairview (demo)",
        "zone": "Riverside",
        "storage": "refrigerated",
        "handling_notes": "sealed containers, keep upright",
    }


def submit_selection(engine, state, selection: dict | str):
    text = selection if isinstance(selection, str) else json.dumps(selection)
    return state, engine.handle_text(state, text)
