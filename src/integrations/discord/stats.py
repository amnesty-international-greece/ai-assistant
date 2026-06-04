"""Per-message stats — replaces legacy stats.json.

Backed by the ``discord_stats`` table added in Task A.
One module-level asyncio.Lock serialises writes; reads run lock-free.
All datetimes are stored and returned in UTC.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from src.core.audit import _get_connection

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_dt(value: str | None) -> datetime | None:
    """Parse an ISO-8601 string (with optional trailing Z) into a UTC datetime."""
    if value is None:
        return None
    cleaned = value.rstrip("Z")
    dt = datetime.fromisoformat(cleaned)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class StatsSummary:
    """Aggregate counts for a time window."""

    total: int
    inbound_email: int
    outbound_email: int
    discord_posts: int
    avg_confidence: float | None


@dataclass(slots=True)
class ChannelStats:
    """Message count per channel for a time window."""

    channel_id: str
    count: int


@dataclass(slots=True)
class ClassificationStats:
    """Message count and average confidence per classification label."""

    classification: str
    count: int
    avg_confidence: float | None


# ---------------------------------------------------------------------------
# StatsStore
# ---------------------------------------------------------------------------

class StatsStore:
    """Read/write access to the ``discord_stats`` table.

    Inserts are *not* audited (too noisy); query methods are all lock-free.
    """

    _lock: asyncio.Lock

    def __init__(self) -> None:
        self._lock = asyncio.Lock()

    # -- internal ------------------------------------------------------------

    def _build_time_filter(
        self,
        since: datetime | None,
        until: datetime | None,
        test_mode: bool | None,
    ) -> tuple[str, list[Any]]:
        """Return a ``WHERE …`` clause string and matching parameters list.

        Passing ``since=None`` means "no lower bound" — used by the "All
        time" branch of the /stats dashboard.  Without this the dashboard
        crashed with ``AttributeError: 'NoneType' object has no attribute
        'isoformat'`` (observed in production 2026-05-27).
        """
        # Always start with ``WHERE 1=1`` so callers can safely append
        # extra ``AND foo IS NOT NULL`` clauses regardless of whether any
        # time/test filters were supplied (the "All time" branch passes
        # since=None, leaving the time filter empty).
        clauses: list[str] = ["1=1"]
        params: list[Any] = []

        if since is not None:
            clauses.append("timestamp >= ?")
            params.append(since.isoformat())

        if until is not None:
            clauses.append("timestamp < ?")
            params.append(until.isoformat())

        if test_mode is not None:
            clauses.append("test_mode = ?")
            params.append(1 if test_mode else 0)

        where = "WHERE " + " AND ".join(clauses)
        return where, params

    # -- public API ----------------------------------------------------------

    async def record(
        self,
        *,
        channel_id: str | None,
        thread_id: str | None,
        direction: str,
        classification: str | None,
        confidence: float | None,
        test_mode: bool,
    ) -> None:
        """Insert one stats row.

        Parameters
        ----------
        channel_id:
            Discord channel snowflake, or ``None`` for DM-originated events.
        thread_id:
            Discord thread snowflake, or ``None`` when not applicable.
        direction:
            One of ``'inbound_email'``, ``'outbound_email'``, ``'discord_post'``.
        classification:
            Classifier label, or ``None`` if classification was not attempted.
        confidence:
            Classifier confidence in ``[0.0, 1.0]``, or ``None``.
        test_mode:
            ``True`` if this event occurred while test mode was active.
        """
        now = _now_iso()
        async with self._lock:
            conn = _get_connection()
            conn.execute(
                """INSERT INTO discord_stats
                       (timestamp, channel_id, thread_id, direction,
                        classification, confidence, test_mode)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    now,
                    channel_id,
                    thread_id,
                    direction,
                    classification,
                    confidence,
                    1 if test_mode else 0,
                ),
            )
            conn.commit()
        logger.debug(
            "StatsStore.record direction=%s channel=%s test_mode=%s",
            direction, channel_id, test_mode,
        )

    async def summary(
        self,
        *,
        since: datetime | None,
        until: datetime | None = None,
        test_mode: bool | None = None,
    ) -> StatsSummary:
        """Return aggregate counts for the given time window.

        Parameters
        ----------
        since:
            Inclusive lower bound (UTC).
        until:
            Exclusive upper bound (UTC).  ``None`` means up to now.
        test_mode:
            ``True``/``False`` filters to that mode; ``None`` returns both.
        """
        where, params = self._build_time_filter(since, until, test_mode)
        conn = _get_connection()
        row = conn.execute(
            f"""SELECT
                    COUNT(*) AS total,
                    SUM(direction = 'inbound_email') AS inbound_email,
                    SUM(direction = 'outbound_email') AS outbound_email,
                    SUM(direction = 'discord_post') AS discord_posts,
                    AVG(confidence) AS avg_confidence
               FROM discord_stats
               {where}""",
            params,
        ).fetchone()

        return StatsSummary(
            total=row["total"] or 0,
            inbound_email=row["inbound_email"] or 0,
            outbound_email=row["outbound_email"] or 0,
            discord_posts=row["discord_posts"] or 0,
            avg_confidence=row["avg_confidence"],
        )

    async def per_channel(
        self,
        *,
        since: datetime | None,
        until: datetime | None = None,
        test_mode: bool | None = None,
    ) -> list[ChannelStats]:
        """Return per-channel message counts, descending by count.

        Rows with ``NULL`` channel_id are excluded.
        """
        where, params = self._build_time_filter(since, until, test_mode)
        conn = _get_connection()
        rows = conn.execute(
            f"""SELECT channel_id, COUNT(*) AS cnt
               FROM discord_stats
               {where}
               AND channel_id IS NOT NULL
               GROUP BY channel_id
               ORDER BY cnt DESC""",
            params,
        ).fetchall()
        return [ChannelStats(channel_id=r["channel_id"], count=r["cnt"]) for r in rows]

    async def per_classification(
        self,
        *,
        since: datetime | None,
        until: datetime | None = None,
        test_mode: bool | None = None,
    ) -> list[ClassificationStats]:
        """Return per-classification counts and average confidence, descending by count.

        Rows with ``NULL`` classification are excluded.
        """
        where, params = self._build_time_filter(since, until, test_mode)
        conn = _get_connection()
        rows = conn.execute(
            f"""SELECT classification, COUNT(*) AS cnt, AVG(confidence) AS avg_conf
               FROM discord_stats
               {where}
               AND classification IS NOT NULL
               GROUP BY classification
               ORDER BY cnt DESC""",
            params,
        ).fetchall()
        return [
            ClassificationStats(
                classification=r["classification"],
                count=r["cnt"],
                avg_confidence=r["avg_conf"],
            )
            for r in rows
        ]
