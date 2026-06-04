"""Bot global toggles, enabled channels, notification users, email-thread map.

All classes are thin async-safe wrappers over the SQLite tables added in Task A:
  - discord_bot_state
  - discord_enabled_channels
  - discord_notification_users
  - discord_email_threads

One module-level asyncio.Lock per class serialises writes; reads run lock-free.
All datetimes are stored and returned in UTC.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from src.core.audit import _get_connection, log_action
from src.integrations.discord.constants import (
    WEEKLY_DIGEST_MIN_INTERVAL_SECONDS,
    WORKFLOW_NAME,
)

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


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now_utc().isoformat()


# ---------------------------------------------------------------------------
# Frequency interval map
# ---------------------------------------------------------------------------

_FREQUENCY_DELTAS: dict[str, timedelta] = {
    "day": timedelta(days=1),
    "week": timedelta(days=7),
    "month": timedelta(days=30),
}


# ---------------------------------------------------------------------------
# BotStateStore
# ---------------------------------------------------------------------------

class BotStateStore:
    """Single global toggles backed by the ``discord_bot_state`` table.

    Keys defined in constants:
      STATE_BOT_ACTIVE, STATE_WEBHOOK_ACTIVE, STATE_AUTO_CLASSIFY,
      STATE_TEST_MODE_ACTIVE, STATE_TEST_EMAIL.

    Booleans are stored as ``'1'`` / ``'0'`` text values.
    Strings are stored verbatim.
    """

    _lock: asyncio.Lock

    def __init__(self) -> None:
        self._lock = asyncio.Lock()

    # -- internal ------------------------------------------------------------

    def _raw_get(self, key: str) -> str | None:
        conn = _get_connection()
        row = conn.execute(
            "SELECT value FROM discord_bot_state WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else None

    def _raw_set(self, key: str, value: str) -> None:
        conn = _get_connection()
        now = _now_iso()
        conn.execute(
            """INSERT INTO discord_bot_state (key, value, updated_at)
               VALUES (?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                                              updated_at = excluded.updated_at""",
            (key, value, now),
        )
        conn.commit()

    # -- public API ----------------------------------------------------------

    async def get_bool(self, key: str, default: bool = False) -> bool:
        """Return the boolean stored under *key*, or *default* if absent."""
        raw = self._raw_get(key)
        if raw is None:
            return default
        return raw == "1"

    async def set_bool(self, key: str, value: bool) -> None:
        """Persist *value* as ``'1'`` or ``'0'`` under *key*."""
        async with self._lock:
            old_raw = self._raw_get(key)
            self._raw_set(key, "1" if value else "0")
        log_action(
            workflow=WORKFLOW_NAME,
            action="state_set_bool",
            target=key,
            details={"old": old_raw, "new": "1" if value else "0"},
        )
        logger.debug("BotStateStore.set_bool %s=%s", key, value)

    async def get_str(self, key: str, default: str = "") -> str:
        """Return the string stored under *key*, or *default* if absent."""
        raw = self._raw_get(key)
        return raw if raw is not None else default

    async def set_str(self, key: str, value: str) -> None:
        """Persist *value* as a string under *key*."""
        async with self._lock:
            old_raw = self._raw_get(key)
            self._raw_set(key, value)
        log_action(
            workflow=WORKFLOW_NAME,
            action="state_set_str",
            target=key,
            details={"old": old_raw, "new": value},
        )
        logger.debug("BotStateStore.set_str %s=%r", key, value)

    async def snapshot(self) -> dict[str, str]:
        """Return all rows as a ``{key: value}`` dict."""
        conn = _get_connection()
        rows = conn.execute("SELECT key, value FROM discord_bot_state").fetchall()
        return {row["key"]: row["value"] for row in rows}


# ---------------------------------------------------------------------------
# EnabledChannel dataclass
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class EnabledChannel:
    """One row from ``discord_enabled_channels``."""

    channel_id: str
    test_mode: bool
    label: str
    classifier_keywords: list[str]
    forum_tag_ids: list[str]
    created_at: datetime


# ---------------------------------------------------------------------------
# EnabledChannelsStore
# ---------------------------------------------------------------------------

class EnabledChannelsStore:
    """Channels routed by the email gateway.

    ``test_mode`` is part of the composite primary key so that the same
    channel_id can appear in both production and test rows.
    """

    _lock: asyncio.Lock

    def __init__(self) -> None:
        self._lock = asyncio.Lock()

    # -- internal ------------------------------------------------------------

    def _row_to_channel(self, row: Any) -> EnabledChannel:
        kw_raw = row["classifier_keywords"]
        keywords: list[str] = json.loads(kw_raw) if kw_raw else []
        try:
            tag_raw = row["forum_tag_ids"]
            forum_tag_ids: list[str] = json.loads(tag_raw) if tag_raw else []
        except (IndexError, KeyError):
            forum_tag_ids = []
        return EnabledChannel(
            channel_id=row["channel_id"],
            test_mode=bool(row["test_mode"]),
            label=row["label"] or "",
            classifier_keywords=keywords,
            forum_tag_ids=forum_tag_ids,
            created_at=_parse_dt(row["created_at"]) or _now_utc(),
        )

    # -- public API ----------------------------------------------------------

    async def list(self, *, test_mode: bool) -> list[EnabledChannel]:
        """Return all channels for the given *test_mode* flag."""
        conn = _get_connection()
        rows = conn.execute(
            "SELECT * FROM discord_enabled_channels WHERE test_mode = ?",
            (1 if test_mode else 0,),
        ).fetchall()
        return [self._row_to_channel(r) for r in rows]

    async def add(
        self,
        channel_id: str,
        *,
        test_mode: bool,
        label: str = "",
        classifier_keywords: list[str] | None = None,
        forum_tag_ids: list[str] | None = None,
    ) -> None:
        """Insert or replace a channel row."""
        kw_json = json.dumps(classifier_keywords or [], ensure_ascii=False)
        tag_json = json.dumps(forum_tag_ids or [], ensure_ascii=False)
        now = _now_iso()
        async with self._lock:
            conn = _get_connection()
            conn.execute(
                """INSERT INTO discord_enabled_channels
                       (channel_id, test_mode, label, classifier_keywords, forum_tag_ids, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(channel_id, test_mode) DO UPDATE SET
                       label = excluded.label,
                       classifier_keywords = excluded.classifier_keywords,
                       forum_tag_ids = excluded.forum_tag_ids""",
                (channel_id, 1 if test_mode else 0, label, kw_json, tag_json, now),
            )
            conn.commit()
        log_action(
            workflow=WORKFLOW_NAME,
            action="channel_add",
            target=channel_id,
            details={"test_mode": test_mode, "label": label, "keywords": classifier_keywords or [], "forum_tag_ids": forum_tag_ids or []},
        )
        logger.debug("EnabledChannelsStore.add %s test_mode=%s", channel_id, test_mode)

    async def remove(self, channel_id: str, *, test_mode: bool) -> bool:
        """Delete a channel row.  Returns ``True`` if a row was deleted."""
        async with self._lock:
            conn = _get_connection()
            cursor = conn.execute(
                "DELETE FROM discord_enabled_channels WHERE channel_id = ? AND test_mode = ?",
                (channel_id, 1 if test_mode else 0),
            )
            conn.commit()
            deleted = cursor.rowcount > 0
        if deleted:
            log_action(
                workflow=WORKFLOW_NAME,
                action="channel_remove",
                target=channel_id,
                details={"test_mode": test_mode},
            )
            logger.debug("EnabledChannelsStore.remove %s test_mode=%s", channel_id, test_mode)
        return deleted

    async def get(self, channel_id: str, *, test_mode: bool) -> EnabledChannel | None:
        """Return a single channel or ``None`` if not found."""
        conn = _get_connection()
        row = conn.execute(
            "SELECT * FROM discord_enabled_channels WHERE channel_id = ? AND test_mode = ?",
            (channel_id, 1 if test_mode else 0),
        ).fetchone()
        return self._row_to_channel(row) if row else None


# ---------------------------------------------------------------------------
# NotificationUser dataclass
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class NotificationUser:
    """One row from ``discord_notification_users``."""

    user_id: str
    frequency: str          # 'day' | 'week' | 'month'
    last_sent: datetime | None
    created_at: datetime


# ---------------------------------------------------------------------------
# NotificationUsersStore
# ---------------------------------------------------------------------------

class NotificationUsersStore:
    """Users opted-in to periodic digest DMs.

    ``due_now`` enforces both the per-user frequency *and* the global
    ``WEEKLY_DIGEST_MIN_INTERVAL_SECONDS`` floor from constants.
    """

    _lock: asyncio.Lock

    def __init__(self) -> None:
        self._lock = asyncio.Lock()

    # -- internal ------------------------------------------------------------

    def _row_to_user(self, row: Any) -> NotificationUser:
        return NotificationUser(
            user_id=row["user_id"],
            frequency=row["frequency"],
            last_sent=_parse_dt(row["last_sent"]),
            created_at=_parse_dt(row["created_at"]) or _now_utc(),
        )

    # -- public API ----------------------------------------------------------

    async def list(self) -> list[NotificationUser]:
        """Return all opted-in users."""
        conn = _get_connection()
        rows = conn.execute("SELECT * FROM discord_notification_users").fetchall()
        return [self._row_to_user(r) for r in rows]

    async def upsert(self, user_id: str, frequency: str = "week") -> None:
        """Register or update a user's notification preference."""
        now = _now_iso()
        async with self._lock:
            conn = _get_connection()
            conn.execute(
                """INSERT INTO discord_notification_users (user_id, frequency, created_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(user_id) DO UPDATE SET frequency = excluded.frequency""",
                (user_id, frequency, now),
            )
            conn.commit()
        log_action(
            workflow=WORKFLOW_NAME,
            action="notification_user_upsert",
            target=user_id,
            details={"frequency": frequency},
        )
        logger.debug("NotificationUsersStore.upsert %s freq=%s", user_id, frequency)

    async def remove(self, user_id: str) -> bool:
        """Opt a user out.  Returns ``True`` if a row was deleted."""
        async with self._lock:
            conn = _get_connection()
            cursor = conn.execute(
                "DELETE FROM discord_notification_users WHERE user_id = ?", (user_id,)
            )
            conn.commit()
            deleted = cursor.rowcount > 0
        if deleted:
            log_action(
                workflow=WORKFLOW_NAME,
                action="notification_user_remove",
                target=user_id,
            )
            logger.debug("NotificationUsersStore.remove %s", user_id)
        return deleted

    async def mark_sent(self, user_id: str, when: datetime | None = None) -> None:
        """Update ``last_sent`` for *user_id* to *when* (defaults to now UTC)."""
        ts = (when or _now_utc()).isoformat()
        async with self._lock:
            conn = _get_connection()
            conn.execute(
                "UPDATE discord_notification_users SET last_sent = ? WHERE user_id = ?",
                (ts, user_id),
            )
            conn.commit()
        logger.debug("NotificationUsersStore.mark_sent %s at %s", user_id, ts)

    async def due_now(self, now: datetime) -> list[NotificationUser]:
        """Return users whose digest is overdue.

        A user is due when::

            last_sent + max(frequency_delta, MIN_INTERVAL_SECONDS) < now

        Users who have *never* been sent a digest are always included.
        """
        floor = timedelta(seconds=WEEKLY_DIGEST_MIN_INTERVAL_SECONDS)
        users = await self.list()
        due: list[NotificationUser] = []
        for user in users:
            if user.last_sent is None:
                due.append(user)
                continue
            delta = _FREQUENCY_DELTAS.get(user.frequency, timedelta(days=7))
            effective_delta = max(delta, floor)
            if user.last_sent + effective_delta < now:
                due.append(user)
        return due


# ---------------------------------------------------------------------------
# EmailThreadLink dataclass
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class EmailThreadLink:
    """One row from ``discord_email_threads``."""

    message_id: str
    discord_thread_id: str
    discord_channel_id: str
    subject: str
    created_at: datetime


# ---------------------------------------------------------------------------
# EmailThreadMap
# ---------------------------------------------------------------------------

class EmailThreadMap:
    """Bidirectional map: RFC822 Message-IDs ↔ Discord thread IDs.

    Backed by ``discord_email_threads``.  Not audited per spec (write-only
    correlation map, not a configuration mutation).
    """

    _lock: asyncio.Lock

    def __init__(self) -> None:
        self._lock = asyncio.Lock()

    # -- internal ------------------------------------------------------------

    def _row_to_link(self, row: Any) -> EmailThreadLink:
        return EmailThreadLink(
            message_id=row["message_id"],
            discord_thread_id=row["discord_thread_id"],
            discord_channel_id=row["discord_channel_id"],
            subject=row["subject"] or "",
            created_at=_parse_dt(row["created_at"]) or _now_utc(),
        )

    # -- public API ----------------------------------------------------------

    async def record(
        self,
        message_id: str,
        *,
        discord_thread_id: str,
        discord_channel_id: str,
        subject: str = "",
    ) -> None:
        """Insert or replace a message_id → thread mapping."""
        now = _now_iso()
        async with self._lock:
            conn = _get_connection()
            conn.execute(
                """INSERT INTO discord_email_threads
                       (message_id, discord_thread_id, discord_channel_id, subject, created_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(message_id) DO UPDATE SET
                       discord_thread_id = excluded.discord_thread_id,
                       discord_channel_id = excluded.discord_channel_id,
                       subject = excluded.subject""",
                (message_id, discord_thread_id, discord_channel_id, subject, now),
            )
            conn.commit()
        logger.debug("EmailThreadMap.record %s → thread %s", message_id, discord_thread_id)

    async def lookup_thread(self, message_id: str) -> EmailThreadLink | None:
        """Return the link for *message_id*, or ``None``."""
        conn = _get_connection()
        row = conn.execute(
            "SELECT * FROM discord_email_threads WHERE message_id = ?", (message_id,)
        ).fetchone()
        return self._row_to_link(row) if row else None

    async def lookup_by_thread(self, discord_thread_id: str) -> list[EmailThreadLink]:
        """Return all links whose ``discord_thread_id`` matches."""
        conn = _get_connection()
        rows = conn.execute(
            "SELECT * FROM discord_email_threads WHERE discord_thread_id = ?",
            (discord_thread_id,),
        ).fetchall()
        return [self._row_to_link(r) for r in rows]


# ---------------------------------------------------------------------------
# Team dataclass
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class Team:
    """One row from ``discord_teams``."""

    team_role_id: str
    team_name: str
    category_id: str | None
    coordinator_role_id: str | None
    created_at: datetime


# ---------------------------------------------------------------------------
# TeamsStore
# ---------------------------------------------------------------------------

class TeamsStore:
    """Registered teams. Used by the /team slash commands to constrain
    coordinator authority to the intersection of (Συντονιστής, team_role)."""

    _lock: asyncio.Lock

    def __init__(self) -> None:
        self._lock = asyncio.Lock()

    def _row_to_team(self, row: Any) -> Team:
        return Team(
            team_role_id=row["team_role_id"],
            team_name=row["team_name"],
            category_id=row["category_id"],
            coordinator_role_id=row["coordinator_role_id"],
            created_at=_parse_dt(row["created_at"]) or _now_utc(),
        )

    async def list(self) -> list[Team]:
        conn = _get_connection()
        rows = conn.execute("SELECT * FROM discord_teams").fetchall()
        return [self._row_to_team(r) for r in rows]

    async def get(self, team_role_id: str) -> Team | None:
        conn = _get_connection()
        row = conn.execute(
            "SELECT * FROM discord_teams WHERE team_role_id = ?",
            (team_role_id,),
        ).fetchone()
        return self._row_to_team(row) if row else None

    async def add(
        self,
        team_role_id: str,
        *,
        team_name: str,
        category_id: str | None = None,
        coordinator_role_id: str | None = None,
    ) -> None:
        now = _now_iso()
        async with self._lock:
            conn = _get_connection()
            conn.execute(
                """INSERT INTO discord_teams
                       (team_role_id, team_name, category_id, coordinator_role_id, created_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(team_role_id) DO UPDATE SET
                       team_name = excluded.team_name,
                       category_id = excluded.category_id,
                       coordinator_role_id = excluded.coordinator_role_id""",
                (team_role_id, team_name, category_id, coordinator_role_id, now),
            )
            conn.commit()
        log_action(
            workflow=WORKFLOW_NAME,
            action="team_add",
            target=team_role_id,
            details={"name": team_name, "category": category_id, "coordinator": coordinator_role_id},
        )

    async def remove(self, team_role_id: str) -> bool:
        async with self._lock:
            conn = _get_connection()
            cur = conn.execute(
                "DELETE FROM discord_teams WHERE team_role_id = ?",
                (team_role_id,),
            )
            conn.commit()
            deleted = cur.rowcount > 0
        if deleted:
            log_action(
                workflow=WORKFLOW_NAME,
                action="team_remove",
                target=team_role_id,
            )
        return deleted
