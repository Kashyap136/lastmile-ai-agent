"""Tests for the recipient-notification honesty contract."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from lastmile.adapters.notify import NotifyResult, notify_recipient


def _plan_dict():
    now = datetime.now(timezone.utc)
    return {
        "plan_id": "plan_deadbeef",
        "donation_id": "don_001",
        "recipient_id": "rcp_01",
        "recipient_name": "Riverside Community Kitchen (demo)",
        "window": {
            "window_id": "w1",
            "start": (now + timedelta(hours=2)).isoformat(),
            "end": (now + timedelta(hours=4)).isoformat(),
            "fits_deadline": True,
            "label": "test window",
        },
        "travel": {"distance_km": 2.0, "duration_minutes": 6.0,
                   "data_source": "calculated_estimate", "note": "estimate"},
        "route_summary": "approx. 2 km, 6 min",
        "handling_instructions": "keep upright",
        "data_source": "demo",
        "proposed_at": now.isoformat(),
    }


class _Response:
    def __init__(self, status_code):
        self.status_code = status_code
        self.headers = {}


def test_no_webhook_configured_never_claims_success():
    result = notify_recipient(_plan_dict(), webhook_url="")
    assert result.delivered is False
    assert result.data_source.value in ("demo",)
    assert "have NOT been contacted" in result.detail or "no recipient notification" in result.detail


def test_webhook_2xx_is_success():
    import lastmile.adapters.notify as notify_mod

    calls = []

    def fake_post(url, json, timeout):
        calls.append(url)
        return _Response(202)

    notify_mod.requests.post = fake_post  # monkeypatching via direct attribute
    result = notify_recipient(_plan_dict(), webhook_url="https://hook.example.test/notify")
    assert result.delivered is True
    assert "HTTP 202" in result.detail
    assert calls == ["https://hook.example.test/notify"]


def test_webhook_5xx_reports_failure_not_success():
    import lastmile.adapters.notify as notify_mod

    def fake_post(url, json, timeout):
        return _Response(502)

    notify_mod.requests.post = fake_post
    result = notify_recipient(_plan_dict(), webhook_url="https://hook.example.test/notify")
    assert result.delivered is False
    assert "HTTP 502" in result.detail
    assert "awaiting_recipient_confirmation" in result.detail


def test_webhook_timeout_reports_failure():
    import requests as requests_mod
    import lastmile.adapters.notify as notify_mod

    def fake_post(url, json, timeout):
        raise requests_mod.Timeout("boom")

    notify_mod.requests.post = fake_post
    result = notify_recipient(_plan_dict(), webhook_url="https://hook.example.test/notify")
    assert result.delivered is False
    assert "delivery failed" in result.detail


def test_engine_default_notify_is_honest():
    from lastmile.workflow.engine import _default_notify

    result = _default_notify(_plan_dict())
    assert isinstance(result, NotifyResult)
    assert result.delivered is False
