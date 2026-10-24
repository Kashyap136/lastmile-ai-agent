"""Recipient notification adapter.

Offline/demo deployments have no real recipient channel. The adapter
therefore NEVER reports success without an actual HTTP 2xx from a
configured webhook - otherwise callers must keep the donation in the
honest 'awaiting_recipient_confirmation' demo state.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

import requests

from ..models.common import DataSource

logger = logging.getLogger("lastmile.notify")


@dataclass
class NotifyResult:
    delivered: bool
    data_source: DataSource
    detail: str  # human-readable explanation used in chat replies


def notify_recipient(plan_dict: dict, webhook_url: str, timeout_seconds: float = 8.0) -> NotifyResult:
    """POST the confirmed plan to the configured recipient webhook.

    Returns delivered=True only on HTTP 2xx. On no-webhook or failure the
    result is explicitly not delivered with an explainable detail message.
    """
    if not webhook_url:
        return NotifyResult(
            delivered=False,
            data_source=DataSource.DEMO,
            detail=(
                "no recipient notification channel is configured - the recipient is a "
                "demo dataset entry and has NOT been contacted. The plan is recorded as "
                "'awaiting_recipient_confirmation'."
            ),
        )

    try:
        response = requests.post(
            webhook_url,
            json={"type": "pickup_plan_confirmed", "plan": plan_dict},
            timeout=timeout_seconds,
        )
    except requests.RequestException as exc:
        logger.warning("recipient notify failed: %s", exc)
        return NotifyResult(
            delivered=False,
            data_source=DataSource.EXTERNAL_API,
            detail=f"notification channel configured but delivery failed ({exc}); "
            "plan kept as 'awaiting_recipient_confirmation' - please retry later",
        )

    if 200 <= response.status_code < 300:
        return NotifyResult(
            delivered=True,
            data_source=DataSource.EXTERNAL_API,
            detail=f"plan dispatched to recipient webhook (HTTP {response.status_code})",
        )

    return NotifyResult(
        delivered=False,
        data_source=DataSource.EXTERNAL_API,
        detail=f"notification endpoint returned HTTP {response.status_code}; "
        "plan kept as 'awaiting_recipient_confirmation' - please retry later",
    )


def plan_to_webhook_json(plan_model) -> dict:
    return json.loads(plan_model.model_dump_json(mode="json"))
