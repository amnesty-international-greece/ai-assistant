"""Crab Fit integration - create a group-availability event programmatically.

Crab Fit (https://crab.fit) is a free, open-source (GPLv3) When2Meet-style
availability grid.  Its public API has no auth: POST a list of 15-minute time
slots and it returns an event id whose human-facing grid lives at
``{web_base}/{id}``.

We use it to replace the manual scheduling-poll URL: the invitation workflow
creates an event over the candidate dates, drops the link in the scheduling
email, and the President reads the filled grid to pick the final date/time.

Slot format (from the Crab Fit frontend ``serializeTime``): ``HHmm-DDMMYYYY``,
serialized in **UTC**.  We generate each 15-minute slot in the meeting's local
timezone (Europe/Athens), convert to UTC, and format accordingly; the ``timezone``
field on the event tells the grid to render back in local time for viewers.

Base URLs are config-driven (``settings.crabfit``) so a self-hosted instance can
be used later without touching code.
"""
from __future__ import annotations

import logging
from datetime import date as _date, datetime
from zoneinfo import ZoneInfo

import httpx

from src.config import settings
from src.core.audit import log_action

logger = logging.getLogger(__name__)

_UTC = ZoneInfo("UTC")


def build_event_times(
    dates: list[_date],
    start_hour: int = 9,
    end_hour: int = 23,
    timezone: str = "Europe/Athens",
) -> list[str]:
    """Build the Crab Fit ``times`` array for the given candidate dates.

    For each date, emits one ``HHmm-DDMMYYYY`` (UTC) string per 15-minute slot
    in the local-time window ``[start_hour:00, end_hour:00)`` - i.e. the last
    slot is ``(end_hour-1):45``.  Conversion to UTC is per-slot, so any date
    rollover at the timezone boundary is handled correctly.
    """
    tz = ZoneInfo(timezone)
    times: list[str] = []
    for d in dates:
        for hour in range(start_hour, end_hour):
            for minute in (0, 15, 30, 45):
                local_dt = datetime(d.year, d.month, d.day, hour, minute, tzinfo=tz)
                u = local_dt.astimezone(_UTC)
                times.append(f"{u.hour:02d}{u.minute:02d}-{u.day:02d}{u.month:02d}{u.year}")
    return times


class CrabFitClient:
    """Thin async client for the Crab Fit event API."""

    def __init__(self, api_base: str | None = None, web_base: str | None = None) -> None:
        self._api_base = (api_base or settings.crabfit.api_base).rstrip("/")
        self._web_base = (web_base or settings.crabfit.web_base).rstrip("/")

    async def create_event(
        self,
        name: str,
        dates: list[_date],
        *,
        start_hour: int | None = None,
        end_hour: int | None = None,
        timezone: str = "Europe/Athens",
        workflow: str = "crabfit",
    ) -> dict:
        """Create an availability event and return ``{id, url, times, timezone}``.

        Raises on HTTP/network error - the caller decides whether that's fatal
        (the scheduling step treats it as non-fatal and falls back to no poll).
        """
        if not dates:
            raise ValueError("create_event requires at least one candidate date")

        start = settings.crabfit.default_start_hour if start_hour is None else start_hour
        end = settings.crabfit.default_end_hour if end_hour is None else end_hour
        times = build_event_times(dates, start, end, timezone)

        payload = {"name": name, "times": times, "timezone": timezone}
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(f"{self._api_base}/event", json=payload)
            resp.raise_for_status()
            data = resp.json()

        event_id = data["id"]
        url = f"{self._web_base}/{event_id}"
        log_action(
            workflow=workflow,
            action="crabfit_event_created",
            actor="system",
            target=event_id,
            details={"dates": [d.isoformat() for d in dates], "url": url},
        )
        logger.info("Created Crab Fit event %s (%d dates) → %s", event_id, len(dates), url)
        return {"id": event_id, "url": url, "times": data.get("times", times), "timezone": timezone}
