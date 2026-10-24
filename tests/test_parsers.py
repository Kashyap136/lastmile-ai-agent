"""Tests for the flexible datetime parser."""

from __future__ import annotations

from datetime import datetime, timezone

from lastmile.validation.parsers import parse_flexible_datetime


def test_iso_with_offset():
    value, err = parse_flexible_datetime("2026-10-24T18:00+02:00")
    assert err == ""
    assert value is not None
    assert value.utcoffset() is not None
    assert value.hour == 16  # converted to UTC


def test_iso_naive_becomes_utc():
    value, err = parse_flexible_datetime("2030-01-01T09:30")
    assert err == ""
    assert value.tzinfo is not None


def test_space_instead_of_t():
    value, err = parse_flexible_datetime("2030-01-01 09:30")
    assert err == ""
    assert value.hour == 9


def test_relative_hours_and_minutes():
    now = datetime.now(timezone.utc)
    value, err = parse_flexible_datetime("in 3 hours")
    assert err == ""
    assert (value - now).total_seconds() > 170 * 60

    value, err = parse_flexible_datetime("in 20 min")
    assert err == ""
    assert (value - now).total_seconds() > 15 * 60


def test_today_and_tomorrow_words():
    value, err = parse_flexible_datetime("today 17:30")
    assert err == ""
    assert value.hour == 17 and value.minute == 30

    value, err = parse_flexible_datetime("tomorrow 9am")
    assert err == ""
    assert value.hour == 9

    value, err = parse_flexible_datetime("tomorrow 10pm")
    assert err == ""
    assert value.hour == 22


def test_bare_time_moves_to_next_day_when_passed():
    now = datetime.now(timezone.utc)
    probe = (now - __import__("datetime").timedelta(hours=2)).strftime("%H:%M")
    value, err = parse_flexible_datetime(probe)
    assert err == ""
    assert value > now


def test_invalid_input_reports_clear_error():
    value, err = parse_flexible_datetime("whenever, mate")
    assert value is None
    assert "could not parse" in err

    value, err = parse_flexible_datetime("")
    assert value is None and "required" in err


def test_hour_boundaries():
    value, err = parse_flexible_datetime("today 12am")
    assert err == "" and value.hour == 0
    value, err = parse_flexible_datetime("today 25:00")
    assert value is None and "out of range" in err
