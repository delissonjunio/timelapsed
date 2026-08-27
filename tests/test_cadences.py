"""Rollover logic for the hourly / daily / weekly renders."""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from timelapsed.schema import CADENCES


def utc(*args) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


SAO_PAULO = ZoneInfo("America/Sao_Paulo")  # UTC-3, no DST since 2019


@pytest.mark.parametrize("name, expected_window", [
    ("hourly", timedelta(hours=1)),
    ("daily", timedelta(days=1)),
    ("weekly", timedelta(days=7)),
])
def test_each_cadence_looks_back_over_its_own_period(name, expected_window):
    assert CADENCES[name].window == expected_window


@pytest.mark.parametrize("name, last_run, now, due", [
    # hourly
    ("hourly", utc(2025, 6, 1, 12, 0), utc(2025, 6, 1, 12, 59), False),
    ("hourly", utc(2025, 6, 1, 12, 59), utc(2025, 6, 1, 13, 0), True),
    ("hourly", utc(2025, 6, 1, 23, 30), utc(2025, 6, 2, 0, 1), True),
    # a full day later at the same hour is still a rollover, not a no-op
    ("hourly", utc(2025, 6, 1, 12, 0), utc(2025, 6, 2, 12, 0), True),
    # daily
    ("daily", utc(2025, 6, 1, 0, 5), utc(2025, 6, 1, 23, 55), False),
    ("daily", utc(2025, 6, 1, 23, 55), utc(2025, 6, 2, 0, 5), True),
    ("daily", utc(2025, 12, 31, 12, 0), utc(2026, 1, 1, 12, 0), True),
    # weekly: ISO weeks roll over on Monday
    ("weekly", utc(2025, 6, 3), utc(2025, 6, 8, 23, 59), False),   # Tue -> Sun, same week
    ("weekly", utc(2025, 6, 8, 23, 59), utc(2025, 6, 9, 0, 1), True),  # Sun -> Mon
    ("weekly", utc(2025, 6, 2), utc(2025, 6, 3), False),           # Mon -> Tue, same week
])
def test_rollover_detection(name, last_run, now, due):
    assert CADENCES[name].is_due(now, last_run) is due


def test_weekly_rolls_over_across_a_year_boundary():
    """29 Dec 2025 and 1 Jan 2026 share ISO week 1 of 2026, so this must not fire."""
    weekly = CADENCES["weekly"]

    assert weekly.is_due(utc(2026, 1, 1), utc(2025, 12, 29)) is False
    assert weekly.is_due(utc(2026, 1, 5), utc(2025, 12, 29)) is True


def test_no_cadence_fires_against_its_own_timestamp():
    now = utc(2025, 6, 4, 15, 30)

    assert not any(cadence.is_due(now, now) for cadence in CADENCES.values())


def test_a_long_outage_makes_every_cadence_due():
    last_run = utc(2025, 1, 1)
    now = utc(2025, 6, 1)

    assert all(cadence.is_due(now, last_run) for cadence in CADENCES.values())


def test_rollovers_follow_the_wall_clock_they_are_given():
    """The predicates read the zone their datetimes carry, which is what lets the
    daemon close a "daily" at local midnight instead of at midnight UTC."""
    daily = CADENCES["daily"]
    last_run = utc(2025, 6, 1, 12, 0).astimezone(SAO_PAULO)  # 09:00 on the 1st, locally

    # Midnight UTC is 21:00 on the 1st in Sao Paulo: still the same local day.
    assert daily.is_due(utc(2025, 6, 2, 0, 0).astimezone(SAO_PAULO), last_run) is False
    # 03:00 UTC is the local midnight, and that is where the day turns over.
    assert daily.is_due(utc(2025, 6, 2, 3, 0).astimezone(SAO_PAULO), last_run) is True

    # The same two instants judged in UTC fire three hours earlier.
    assert daily.is_due(utc(2025, 6, 2, 0, 0), utc(2025, 6, 1, 12, 0)) is True
