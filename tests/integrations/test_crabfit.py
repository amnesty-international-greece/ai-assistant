"""Tests for the Crab Fit availability-poll integration."""
from __future__ import annotations

from datetime import date
from unittest.mock import patch

import pytest

from src.integrations.crabfit import build_event_times, CrabFitClient


def test_build_event_times_format_and_count():
    """One date, 09:00-23:00 → 14 hours x 4 slots = 56 strings, all HHmm-DDMMYYYY."""
    times = build_event_times([date(2026, 6, 17)], start_hour=9, end_hour=23,
                              timezone="Europe/Athens")
    assert len(times) == 14 * 4
    # Athens is UTC+3 in June → 09:00 local = 06:00 UTC, date unchanged.
    assert times[0] == "0600-17062026"
    # Last slot 22:45 local = 19:45 UTC.
    assert times[-1] == "1945-17062026"
    # Every entry matches HHmm-DDMMYYYY.
    import re
    assert all(re.fullmatch(r"\d{4}-\d{8}", t) for t in times)


def test_build_event_times_multiple_dates():
    times = build_event_times([date(2026, 6, 17), date(2026, 6, 29)])
    # 2 dates x 14 hours x 4 = 112
    assert len(times) == 112
    assert any(t.endswith("17062026") for t in times)
    assert any(t.endswith("29062026") for t in times)


def test_build_event_times_empty_dates():
    assert build_event_times([]) == []


@pytest.mark.asyncio
async def test_create_event_posts_and_returns_url():
    """create_event POSTs name/times/timezone and builds the web URL from the id."""
    captured = {}

    class FakeResp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"id": "synedriasi-ds05-2026-123456", "times": ["x"], "timezone": "Europe/Athens"}

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None):
            captured["url"] = url
            captured["json"] = json
            return FakeResp()

    with patch("src.integrations.crabfit.httpx.AsyncClient", FakeClient), \
         patch("src.integrations.crabfit.log_action"):
        client = CrabFitClient(api_base="https://api.crab.fit", web_base="https://crab.fit")
        result = await client.create_event(
            name="Συνεδρίαση ΔΣ05-2026",
            dates=[date(2026, 6, 17)],
        )

    assert captured["url"] == "https://api.crab.fit/event"
    assert captured["json"]["name"] == "Συνεδρίαση ΔΣ05-2026"
    assert captured["json"]["timezone"] == "Europe/Athens"
    assert len(captured["json"]["times"]) == 56
    assert result["id"] == "synedriasi-ds05-2026-123456"
    assert result["url"] == "https://crab.fit/synedriasi-ds05-2026-123456"


@pytest.mark.asyncio
async def test_create_event_rejects_empty_dates():
    with pytest.raises(ValueError):
        await CrabFitClient().create_event(name="x", dates=[])
