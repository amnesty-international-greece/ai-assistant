"""Audit logging system - records every platform action to SQLite."""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.config import settings

logger = logging.getLogger(__name__)

_DB_PATH: Path | None = None
_CONNECTION: sqlite3.Connection | None = None

_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL DEFAULT (datetime('now')),
    workflow TEXT NOT NULL,
    action TEXT NOT NULL,
    actor TEXT NOT NULL,
    target TEXT,
    details TEXT,
    status TEXT NOT NULL DEFAULT 'success'
);

CREATE INDEX IF NOT EXISTS idx_audit_workflow ON audit_log(workflow);
CREATE INDEX IF NOT EXISTS idx_audit_timestamp ON audit_log(timestamp);
CREATE INDEX IF NOT EXISTS idx_audit_actor ON audit_log(actor);

CREATE TABLE IF NOT EXISTS workflow_state (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_name TEXT NOT NULL,
    workflow_id TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL DEFAULT 'pending',
    data TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_wf_state ON workflow_state(state);

CREATE TABLE IF NOT EXISTS oauth_tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    service TEXT NOT NULL UNIQUE,
    token_data BLOB NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ── Discord integration tables ──────────────────────────────────────────────

-- Bot global toggles & test-mode flags (single-row key/value store)
CREATE TABLE IF NOT EXISTS discord_bot_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Channels routed by the email gateway (enabled forum channels)
CREATE TABLE IF NOT EXISTS discord_enabled_channels (
    channel_id TEXT NOT NULL,
    test_mode INTEGER NOT NULL DEFAULT 0,   -- 0=production, 1=test
    label TEXT,
    classifier_keywords TEXT,               -- JSON list
    forum_tag_ids TEXT,                     -- JSON list of Discord forum tag snowflakes
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (channel_id, test_mode)
);

-- Users opted-in to weekly stats DM
CREATE TABLE IF NOT EXISTS discord_notification_users (
    user_id TEXT PRIMARY KEY,
    frequency TEXT NOT NULL DEFAULT 'week',  -- 'day' | 'week' | 'month'
    last_sent TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Per-message stats entries (one row per processed message)
CREATE TABLE IF NOT EXISTS discord_stats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL DEFAULT (datetime('now')),
    channel_id TEXT,
    thread_id TEXT,
    direction TEXT,           -- 'inbound_email' | 'outbound_email' | 'discord_post'
    classification TEXT,
    confidence REAL,
    test_mode INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_discord_stats_ts ON discord_stats(timestamp);
CREATE INDEX IF NOT EXISTS idx_discord_stats_channel ON discord_stats(channel_id);
CREATE INDEX IF NOT EXISTS idx_discord_stats_direction_classification
    ON discord_stats(direction, classification);

-- Maps email Message-ID / thread headers ↔ Discord thread IDs for reply correlation
CREATE TABLE IF NOT EXISTS discord_email_threads (
    message_id TEXT PRIMARY KEY,    -- RFC822 Message-ID (or References root)
    discord_thread_id TEXT NOT NULL,
    discord_channel_id TEXT NOT NULL,
    subject TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_discord_threads_thread ON discord_email_threads(discord_thread_id);

-- ── Director's briefings index ──────────────────────────────────────────────
--
-- One row per briefing attachment archived from the Director's reply to a
-- board scheduling email.  The ``meeting_ref`` lets the future Γενική
-- Εγκύκλιος workflow gather every briefing relevant to its reporting
-- window.  The ``local_path`` survives ``/board cancel`` so reusable
-- content is never lost.
CREATE TABLE IF NOT EXISTS director_briefings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    meeting_ref TEXT NOT NULL,
    kind TEXT NOT NULL,                 -- 'ΕΙΣΗΓΗΤΙΚΟ' | 'ΕΝΗΜΕΡΩΤΙΚΟ'
    protocol_number TEXT,               -- assigned by ArchiveWorkflow; nullable until upload succeeds
    local_path TEXT NOT NULL,
    sharepoint_url TEXT,                -- web URL of the SharePoint copy
    archived_at TEXT NOT NULL DEFAULT (datetime('now')),
    source_message_id TEXT,             -- Graph internetMessageId (audit / dedup)
    workflow_id TEXT                    -- ArchiveWorkflow id (lookup / manual rollback)
);
CREATE INDEX IF NOT EXISTS idx_director_briefings_meeting_ref
    ON director_briefings(meeting_ref);

-- ── Agenda-sheet mirror ─────────────────────────────────────────────────────
--
-- Single-row key/value mirror of the agenda Google Sheet's "source of truth"
-- cells.  Today only ``meeting_ref`` is mirrored (the value of D5).  The
-- mirror is refreshed on every successful read AND on every successful write
-- by ``GoogleClient.read_meeting_ref`` / ``reset_agenda_sheet``, so a Sheets
-- API outage doesn't block the workflow - we fall back to the last known
-- good value here.
CREATE TABLE IF NOT EXISTS agenda_sheet_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ── Phase B: foundation tables ──────────────────────────────────────────────

-- Persistent queue for time-deferred actions (reminders, unpins, etc.)
CREATE TABLE IF NOT EXISTS discord_pending_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action_type TEXT NOT NULL,           -- 'reminder' | 'unpin' | 'close_poll' | ...
    payload TEXT NOT NULL,                -- JSON
    due_at TEXT NOT NULL,                 -- ISO-8601 UTC
    status TEXT NOT NULL DEFAULT 'pending',  -- 'pending' | 'done' | 'failed'
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    executed_at TEXT,
    error TEXT
);

CREATE INDEX IF NOT EXISTS idx_pending_due ON discord_pending_actions(due_at, status);

-- Maps a workflow run to the Discord resources it spawned (events, threads, messages).
-- Lets cancellation paths find "what did we create for X" and clean up.
CREATE TABLE IF NOT EXISTS discord_workflow_resources (
    workflow_id TEXT NOT NULL,            -- e.g. 'board_meeting:2026-05-21'
    resource_type TEXT NOT NULL,           -- 'event' | 'thread' | 'message' | 'poll'
    discord_id TEXT NOT NULL,              -- snowflake
    channel_id TEXT,                       -- for thread/message resources
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (workflow_id, resource_type, discord_id)
);

CREATE INDEX IF NOT EXISTS idx_wfres_wfid ON discord_workflow_resources(workflow_id);

-- ── Phase C: team management ────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS discord_teams (
    team_role_id TEXT PRIMARY KEY,        -- Discord role snowflake
    team_name TEXT NOT NULL,               -- display name, e.g. "Επιτροπή Πολιτικής"
    category_id TEXT,                      -- optional: Discord category whose channels this team gates
    coordinator_role_id TEXT,              -- usually the universal Συντονιστής role; nullable for edge cases
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ── Phase 1 archive: protocol-number reservations ───────────────────────────
-- Solves the race condition between concurrent workflows (e.g. invitation +
-- archive) both calling get_next_protocol_number and getting the same value.
-- A reservation is created (committed=0) before the xlsx is written, and
-- flipped to committed=1 once append_protocol_row succeeds.  Released on
-- rollback.
CREATE TABLE IF NOT EXISTS protocol_reservations (
    year INTEGER NOT NULL,
    seq INTEGER NOT NULL,
    workflow_id TEXT NOT NULL,
    reserved_at TEXT NOT NULL,
    committed INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (year, seq)
);

CREATE INDEX IF NOT EXISTS idx_proto_workflow ON protocol_reservations(workflow_id);

-- ── Email-intake dedup (Phase 3: M365 inbox → archive workflow) ──────────────
-- Keyed on the RFC 5322 internetMessageId so the webhook delivery + safety
-- poll cannot double-process the same message.  Persists indefinitely; rows
-- are tiny.  workflow_id may be NULL when a message was seen but rejected
-- (sender not in allow-list, subject didn't match, no PDF attachment, etc.)
CREATE TABLE IF NOT EXISTS email_intake_seen (
    internet_message_id TEXT PRIMARY KEY,
    workflow_id TEXT,
    seen_at TEXT NOT NULL DEFAULT (datetime('now')),
    outcome TEXT,        -- 'archived' | 'rejected_sender' | 'rejected_subject' | 'no_pdf' | 'failed'
    notes TEXT
);

CREATE INDEX IF NOT EXISTS idx_email_intake_workflow ON email_intake_seen(workflow_id);

-- ── Graph webhook subscriptions registry ─────────────────────────────────────
-- Tracks the currently-active subscription so we can renew it before expiry
-- and refuse stale notification deliveries (clientState mismatch).
CREATE TABLE IF NOT EXISTS graph_subscriptions (
    subscription_id TEXT PRIMARY KEY,
    resource TEXT NOT NULL,
    client_state TEXT NOT NULL,
    expiration_date_time TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ── Egkyklios drafts ────────────────────────────────────────────────────────
--
-- One row per Γενική Εγκύκλιος Ενημέρωσης draft.  Created at step 4
-- (draft_circular) and updated through each subsequent step.
CREATE TABLE IF NOT EXISTS egkyklios_drafts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,                    -- 'general' for now; future: 'special'
    period_start TEXT NOT NULL,            -- ISO date  e.g. 2026-01-01
    period_end   TEXT NOT NULL,            -- ISO date  e.g. 2026-03-31
    title TEXT NOT NULL,                   -- e.g. "ΙΑΝΟΥΑΡΙΟΣ - ΜΑΡΤΙΟΣ 2026"
    status TEXT NOT NULL DEFAULT 'drafting',  -- drafting | awaiting_approval | approved | sent | cancelled
    draft_md_path TEXT,                    -- data/egkyklios/drafts/...md (LLM output)
    draft_pdf_path TEXT,                   -- data/egkyklios/drafts/...pdf
    protocol_number TEXT,                  -- assigned at archive step
    sharepoint_url TEXT,
    brevo_campaign_id INTEGER,
    workflow_id TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_egkyklios_drafts_status
    ON egkyklios_drafts(status);

-- ── RSS feeds (replacing MonitoRSS) ──────────────────────────────────────────
-- One row per feed source; the per-feed last_seen_guid drives dedup so we
-- only post NEW items on each poll cycle.  rss_routes holds the fan-out
-- rules: one feed can post to multiple channels with different URL/title
-- pattern filters and different forum tags.
CREATE TABLE IF NOT EXISTS rss_feeds (
    feed_url TEXT PRIMARY KEY,
    label TEXT,                                  -- human-readable name shown in CLI / slash output
    last_seen_guid TEXT,                         -- dedup cursor; updated after a successful poll
    last_polled_at TEXT,                         -- ISO8601 UTC of the last poll attempt
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS rss_routes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    feed_url TEXT NOT NULL,                      -- FK to rss_feeds.feed_url (loose; no FK constraint to keep migrations simple)
    channel_id TEXT NOT NULL,                    -- target Discord channel (text or forum)
    forum_tag_id TEXT,                           -- snowflake of the tag to apply (forum channels only); empty = no tag
    forum_tag_name TEXT,                         -- optional name fallback resolved against ForumChannel.available_tags at post time
    url_pattern TEXT,                            -- only post items whose item.link contains this substring
    title_pattern TEXT,                          -- only post items whose item.title matches this regex
    label TEXT,                                  -- human-readable description
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_rss_routes_feed ON rss_routes(feed_url);

-- ── Board-meeting events (minutes pipeline backbone) ─────────────────────────
--
-- Discrete events captured DURING a board meeting (agenda changes, votes,
-- presence, breaks, free-form notes).  Deterministic, model-free: a separate
-- component consumes these later to draft formal Greek minutes.  See
-- ``src/core/meeting_events.py`` for the canonical per-event_type payload spec.
CREATE TABLE IF NOT EXISTS meeting_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    meeting_ref TEXT NOT NULL,          -- e.g. "ΔΣ05-2026"
    event_type TEXT NOT NULL,           -- agenda_advance | vote | phase | presence | off_topic | note
    ts TEXT NOT NULL,                   -- ISO-8601 UTC: when the event occurred (meeting clock)
    payload TEXT NOT NULL DEFAULT '{}', -- JSON, event-type-specific
    confidence TEXT NOT NULL DEFAULT 'confirmed',  -- 'confirmed' | 'low' (low = auto-proposed, unconfirmed)
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_meeting_events_ref ON meeting_events(meeting_ref, ts);
"""


def _get_db_path() -> Path:
    global _DB_PATH
    if _DB_PATH is None:
        _DB_PATH = Path(settings.storage.database_path)
        _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    return _DB_PATH


def _get_connection() -> sqlite3.Connection:
    global _CONNECTION
    if _CONNECTION is None:
        # check_same_thread=False is needed because FastAPI background tasks
        # and APScheduler jobs may touch the DB from threads other than the
        # one that opened the connection.  Safe with WAL + serialized writes
        # (sqlite3's GIL serializes accesses through one Connection object).
        _CONNECTION = sqlite3.connect(str(_get_db_path()), check_same_thread=False)
        _CONNECTION.row_factory = sqlite3.Row
        _CONNECTION.execute("PRAGMA journal_mode=WAL")
        _CONNECTION.execute("PRAGMA foreign_keys=ON")
    return _CONNECTION


def init_db() -> None:
    """Initialize the database schema."""
    conn = _get_connection()
    conn.executescript(_SCHEMA)
    conn.commit()
    # Additive migration: add forum_tag_ids if it doesn't exist yet
    # (CREATE TABLE IF NOT EXISTS won't alter existing tables)
    try:
        conn.execute("ALTER TABLE discord_enabled_channels ADD COLUMN forum_tag_ids TEXT")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # column already exists
    logger.info("Database initialized at %s", _get_db_path())


def log_action(
    workflow: str,
    action: str,
    actor: str = "system",
    target: str | None = None,
    details: dict[str, Any] | None = None,
    status: str = "success",
) -> int:
    """Record an action to the audit log. Returns the log entry ID."""
    conn = _get_connection()
    cursor = conn.execute(
        """INSERT INTO audit_log (workflow, action, actor, target, details, status)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (
            workflow,
            action,
            actor,
            target,
            json.dumps(details, ensure_ascii=False) if details else None,
            status,
        ),
    )
    conn.commit()
    entry_id = cursor.lastrowid
    logger.info(
        "AUDIT | %s | %s | %s | %s | %s",
        workflow, action, actor, target or "-", status,
    )
    return entry_id


def get_audit_log(
    workflow: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """Query the audit log with optional filtering."""
    conn = _get_connection()
    query = "SELECT * FROM audit_log"
    params: list[Any] = []
    if workflow:
        query += " WHERE workflow = ?"
        params.append(workflow)
    query += " ORDER BY timestamp DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    rows = conn.execute(query, params).fetchall()
    return [dict(row) for row in rows]


def save_workflow_state(
    workflow_name: str,
    workflow_id: str,
    state: str,
    data: dict[str, Any] | None = None,
) -> None:
    """Save or update workflow state."""
    conn = _get_connection()
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT INTO workflow_state (workflow_name, workflow_id, state, data, updated_at)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(workflow_id) DO UPDATE SET
               state = excluded.state,
               data = excluded.data,
               updated_at = excluded.updated_at""",
        (
            workflow_name,
            workflow_id,
            state,
            json.dumps(data, ensure_ascii=False) if data else None,
            now,
        ),
    )
    conn.commit()


def get_workflow_state(workflow_id: str) -> dict[str, Any] | None:
    """Retrieve workflow state by ID."""
    conn = _get_connection()
    row = conn.execute(
        "SELECT * FROM workflow_state WHERE workflow_id = ?", (workflow_id,)
    ).fetchone()
    return dict(row) if row else None


# ── Protocol-number reservations ─────────────────────────────────────────────
#
# These helpers solve the race condition between concurrent workflows
# (invitation + archive, or two archive workflows) both calling
# ``OneDriveClient.get_next_protocol_number`` and getting back the same value.
#
# Lifecycle:
#   1. ``reserve_next_protocol_number(year, workflow_id)`` picks MAX+1 of
#      (existing xlsx rows ∪ in-memory uncommitted reservations) and inserts a
#      row with ``committed=0``.  Returns ``"YYYY_NNN"``.
#   2. After the workflow successfully appends to the xlsx, call
#      ``commit_protocol_reservation(workflow_id)`` to flip ``committed=1``.
#   3. On rollback (or cancellation), ``release_protocol_reservation(workflow_id)``
#      deletes ALL uncommitted rows for that workflow.  Committed rows stay so
#      the audit trail of which workflow grabbed which number is preserved.


def _max_seq_for_year(conn: sqlite3.Connection, year: int) -> int:
    """Highest seq currently reserved (committed OR not) for *year*.  0 if none."""
    row = conn.execute(
        "SELECT COALESCE(MAX(seq), 0) AS m FROM protocol_reservations WHERE year = ?",
        (year,),
    ).fetchone()
    return int(row["m"] or 0)


def reserve_next_protocol_number(
    year: int,
    workflow_id: str,
    *,
    xlsx_max_seq: int = 0,
) -> str:
    """Reserve the next protocol number for *year*.

    Args:
        year:           Calendar year (e.g. ``2026``).
        workflow_id:    Workflow that owns the reservation.
        xlsx_max_seq:   Highest sequence already present in the live xlsx for
                        this year.  Pass 0 (the default) if you've already
                        determined there's no xlsx entry for this year, or if
                        you trust the in-memory table to be the only source of
                        truth.  Otherwise compute it from
                        ``OneDriveClient.get_next_protocol_number`` and subtract
                        one (since that helper returns the NEXT number, not the
                        last existing one).

    Returns:
        ``"YYYY_NNN"`` string (zero-padded to 3 digits).
    """
    conn = _get_connection()
    db_max = _max_seq_for_year(conn, year)
    next_seq = max(db_max, xlsx_max_seq) + 1
    now = datetime.now(timezone.utc).isoformat()
    # Loop-on-conflict: if some other concurrent reservation grabs the same
    # seq between our SELECT and INSERT, retry with the next one.
    while True:
        try:
            conn.execute(
                """INSERT INTO protocol_reservations (year, seq, workflow_id, reserved_at, committed)
                   VALUES (?, ?, ?, ?, 0)""",
                (year, next_seq, workflow_id, now),
            )
            conn.commit()
            break
        except sqlite3.IntegrityError:
            next_seq += 1
    return f"{year}_{next_seq:03d}"


def commit_protocol_reservation(workflow_id: str) -> int:
    """Flip ``committed=1`` for every reservation owned by *workflow_id*.

    Returns the number of rows updated.
    """
    conn = _get_connection()
    cursor = conn.execute(
        "UPDATE protocol_reservations SET committed = 1 WHERE workflow_id = ?",
        (workflow_id,),
    )
    conn.commit()
    return cursor.rowcount


def release_protocol_reservation(workflow_id: str) -> int:
    """Delete all UNCOMMITTED reservations owned by *workflow_id*.

    Used by rollback paths.  Committed rows are preserved so we can audit
    after-the-fact which workflow grabbed which number.

    Returns the number of rows deleted.
    """
    conn = _get_connection()
    cursor = conn.execute(
        "DELETE FROM protocol_reservations WHERE workflow_id = ? AND committed = 0",
        (workflow_id,),
    )
    conn.commit()
    return cursor.rowcount


def get_reservations_for_year(year: int) -> list[dict[str, Any]]:
    """Return all reservations (committed or not) for *year*.  Useful for tests."""
    conn = _get_connection()
    rows = conn.execute(
        "SELECT * FROM protocol_reservations WHERE year = ? ORDER BY seq",
        (year,),
    ).fetchall()
    return [dict(r) for r in rows]


# ── Email-intake dedup (Phase 3) ────────────────────────────────────────────


def has_seen_email(internet_message_id: str) -> bool:
    """True if we've previously processed an email with this RFC 5322 id."""
    if not internet_message_id:
        return False
    conn = _get_connection()
    row = conn.execute(
        "SELECT 1 FROM email_intake_seen WHERE internet_message_id = ?",
        (internet_message_id,),
    ).fetchone()
    return row is not None


def mark_email_seen(
    internet_message_id: str,
    *,
    workflow_id: str | None = None,
    outcome: str = "archived",
    notes: str | None = None,
) -> None:
    """Record that we've processed this email.  Idempotent (INSERT OR IGNORE)."""
    if not internet_message_id:
        return
    conn = _get_connection()
    conn.execute(
        """INSERT OR REPLACE INTO email_intake_seen
              (internet_message_id, workflow_id, outcome, notes, seen_at)
           VALUES (?, ?, ?, ?, datetime('now'))""",
        (internet_message_id, workflow_id, outcome, notes),
    )
    conn.commit()


# ── Graph webhook subscriptions registry (Phase 3) ──────────────────────────


def upsert_graph_subscription(
    subscription_id: str,
    *,
    resource: str,
    client_state: str,
    expiration_date_time: str,
) -> None:
    """Insert or update a Graph subscription record."""
    conn = _get_connection()
    conn.execute(
        """INSERT INTO graph_subscriptions
              (subscription_id, resource, client_state, expiration_date_time, updated_at)
           VALUES (?, ?, ?, ?, datetime('now'))
           ON CONFLICT(subscription_id) DO UPDATE SET
              resource = excluded.resource,
              client_state = excluded.client_state,
              expiration_date_time = excluded.expiration_date_time,
              updated_at = excluded.updated_at""",
        (subscription_id, resource, client_state, expiration_date_time),
    )
    conn.commit()


def get_active_graph_subscriptions() -> list[dict[str, Any]]:
    """Return all subscriptions whose expiration_date_time is in the future."""
    conn = _get_connection()
    now = datetime.now(timezone.utc).isoformat()
    rows = conn.execute(
        "SELECT * FROM graph_subscriptions WHERE expiration_date_time > ? ORDER BY updated_at DESC",
        (now,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_graph_subscription(subscription_id: str) -> dict[str, Any] | None:
    """Look up one subscription by id (returns None if missing)."""
    conn = _get_connection()
    row = conn.execute(
        "SELECT * FROM graph_subscriptions WHERE subscription_id = ?",
        (subscription_id,),
    ).fetchone()
    return dict(row) if row else None


def delete_graph_subscription(subscription_id: str) -> None:
    """Remove a subscription record (after Graph DELETE succeeds)."""
    conn = _get_connection()
    conn.execute(
        "DELETE FROM graph_subscriptions WHERE subscription_id = ?",
        (subscription_id,),
    )
    conn.commit()


# ── Agenda-sheet mirror ─────────────────────────────────────────────────────
#
# The agenda Google Sheet is the universal source of truth; this is a thin
# read-through cache so the workflow can still resolve ``meeting_ref`` when
# the Sheets API is unreachable.  See ``GoogleClient.read_meeting_ref`` for
# the policy: refresh on every successful read, fall back when D5 fails.

_MEETING_REF_CACHE_KEY = "meeting_ref"


def set_meeting_ref_cache(ref: str) -> None:
    """Persist the most recently observed valid meeting_ref from D5.

    Idempotent - repeated writes of the same value just bump ``updated_at``.
    Best-effort by convention: callers should wrap in ``try/except`` and log
    rather than fail the workflow if the cache write fails (the Sheet read
    already succeeded; the cache is only a fallback for *future* reads).
    """
    conn = _get_connection()
    conn.execute(
        """INSERT INTO agenda_sheet_state (key, value, updated_at)
           VALUES (?, ?, datetime('now'))
           ON CONFLICT(key) DO UPDATE SET
              value = excluded.value,
              updated_at = excluded.updated_at""",
        (_MEETING_REF_CACHE_KEY, ref),
    )
    conn.commit()


def get_meeting_ref_cache() -> str | None:
    """Return the most recently cached meeting_ref, or ``None`` if never set.

    Used as a fallback when the Sheets API is unreachable; the workflow
    should prefer a fresh D5 read whenever possible.
    """
    conn = _get_connection()
    row = conn.execute(
        "SELECT value FROM agenda_sheet_state WHERE key = ?",
        (_MEETING_REF_CACHE_KEY,),
    ).fetchone()
    return row["value"] if row else None


# ── Director's briefings index ──────────────────────────────────────────────


def record_director_briefing(
    *,
    meeting_ref: str,
    kind: str,
    local_path: str,
    source_message_id: str = "",
    workflow_id: str = "",
) -> int:
    """Insert a new ``director_briefings`` row, return its ``id``.

    Called by the email-intake flow the moment a briefing attachment hits
    disk - well before the archive workflow assigns a protocol number.
    Later, ``update_director_briefing_archive_result`` fills in the
    SharePoint URL + protocol number once the upload completes.
    """
    conn = _get_connection()
    cur = conn.execute(
        """INSERT INTO director_briefings
              (meeting_ref, kind, local_path, source_message_id, workflow_id)
           VALUES (?, ?, ?, ?, ?)""",
        (meeting_ref, kind, local_path, source_message_id, workflow_id),
    )
    conn.commit()
    return int(cur.lastrowid or 0)


def update_director_briefing_archive_result(
    briefing_id: int,
    *,
    protocol_number: str | None = None,
    sharepoint_url: str | None = None,
) -> None:
    """Patch a briefing row with the protocol number + SharePoint URL
    returned by ``ArchiveWorkflow``.

    Either field may be ``None`` (no-op for that column).  Used after a
    successful archive run; on failure the row stays with NULLs which is
    a useful diagnostic signal.
    """
    if protocol_number is None and sharepoint_url is None:
        return
    conn = _get_connection()
    if protocol_number is not None and sharepoint_url is not None:
        conn.execute(
            "UPDATE director_briefings SET protocol_number = ?, sharepoint_url = ? "
            "WHERE id = ?",
            (protocol_number, sharepoint_url, briefing_id),
        )
    elif protocol_number is not None:
        conn.execute(
            "UPDATE director_briefings SET protocol_number = ? WHERE id = ?",
            (protocol_number, briefing_id),
        )
    else:
        conn.execute(
            "UPDATE director_briefings SET sharepoint_url = ? WHERE id = ?",
            (sharepoint_url, briefing_id),
        )
    conn.commit()


def list_director_briefings_for_meeting(meeting_ref: str) -> list[dict[str, Any]]:
    """Return every briefing archived for ``meeting_ref`` (newest first)."""
    conn = _get_connection()
    rows = conn.execute(
        """SELECT id, meeting_ref, kind, protocol_number, local_path,
                  sharepoint_url, archived_at, source_message_id, workflow_id
           FROM director_briefings
           WHERE meeting_ref = ?
           ORDER BY archived_at DESC""",
        (meeting_ref,),
    ).fetchall()
    return [dict(r) for r in rows]


# ── RSS feeds (replaces MonitoRSS) ──────────────────────────────────────────


def list_rss_feeds(*, enabled_only: bool = False) -> list[dict[str, Any]]:
    """Return all configured RSS feeds (newest configured first)."""
    conn = _get_connection()
    sql = "SELECT * FROM rss_feeds"
    if enabled_only:
        sql += " WHERE enabled = 1"
    sql += " ORDER BY created_at DESC"
    return [dict(r) for r in conn.execute(sql).fetchall()]


def upsert_rss_feed(
    feed_url: str,
    *,
    label: str | None = None,
    enabled: bool = True,
) -> None:
    """Insert a new feed or update an existing one's label/enabled flag.

    last_seen_guid is NOT touched here - that's owned by
    :func:`update_rss_feed_cursor` so we never accidentally lose dedup state
    while editing a feed's display name.
    """
    conn = _get_connection()
    conn.execute(
        """INSERT INTO rss_feeds (feed_url, label, enabled)
           VALUES (?, ?, ?)
           ON CONFLICT(feed_url) DO UPDATE SET
              label = excluded.label,
              enabled = excluded.enabled""",
        (feed_url, label, 1 if enabled else 0),
    )
    conn.commit()


def delete_rss_feed(feed_url: str) -> None:
    """Remove a feed and ALL its routes (cascade by hand - no FK constraint)."""
    conn = _get_connection()
    conn.execute("DELETE FROM rss_routes WHERE feed_url = ?", (feed_url,))
    conn.execute("DELETE FROM rss_feeds WHERE feed_url = ?", (feed_url,))
    conn.commit()


def update_rss_feed_cursor(feed_url: str, last_seen_guid: str | None) -> None:
    """Advance the dedup cursor after a successful poll cycle."""
    conn = _get_connection()
    conn.execute(
        "UPDATE rss_feeds SET last_seen_guid = ?, last_polled_at = datetime('now') "
        "WHERE feed_url = ?",
        (last_seen_guid, feed_url),
    )
    conn.commit()


def list_rss_routes(feed_url: str | None = None) -> list[dict[str, Any]]:
    """Return routes for one feed, or for ALL feeds if feed_url is None."""
    conn = _get_connection()
    if feed_url is None:
        rows = conn.execute("SELECT * FROM rss_routes ORDER BY id").fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM rss_routes WHERE feed_url = ? ORDER BY id",
            (feed_url,),
        ).fetchall()
    return [dict(r) for r in rows]


def add_rss_route(
    feed_url: str,
    *,
    channel_id: str,
    forum_tag_id: str | None = None,
    forum_tag_name: str | None = None,
    url_pattern: str | None = None,
    title_pattern: str | None = None,
    label: str | None = None,
) -> int:
    """Create a fan-out rule.  Returns the new route id."""
    conn = _get_connection()
    cursor = conn.execute(
        """INSERT INTO rss_routes
              (feed_url, channel_id, forum_tag_id, forum_tag_name,
               url_pattern, title_pattern, label)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (feed_url, channel_id, forum_tag_id, forum_tag_name,
         url_pattern, title_pattern, label),
    )
    conn.commit()
    return int(cursor.lastrowid)


def delete_rss_route(route_id: int) -> None:
    conn = _get_connection()
    conn.execute("DELETE FROM rss_routes WHERE id = ?", (route_id,))
    conn.commit()


# ── Egkyklios drafts ─────────────────────────────────────────────────────────


def create_egkyklios_draft(
    *,
    kind: str,
    period_start: str,
    period_end: str,
    title: str,
    workflow_id: str | None = None,
) -> int:
    """Insert a new egkyklios_drafts row and return its id."""
    conn = _get_connection()
    cur = conn.execute(
        """INSERT INTO egkyklios_drafts
              (kind, period_start, period_end, title, status, workflow_id)
           VALUES (?, ?, ?, ?, 'drafting', ?)""",
        (kind, period_start, period_end, title, workflow_id),
    )
    conn.commit()
    return int(cur.lastrowid or 0)


def get_egkyklios_draft(draft_id: int) -> dict[str, Any] | None:
    """Retrieve a single egkyklios_drafts row by id."""
    conn = _get_connection()
    row = conn.execute(
        "SELECT * FROM egkyklios_drafts WHERE id = ?", (draft_id,)
    ).fetchone()
    return dict(row) if row else None


def update_egkyklios_draft(draft_id: int, **kwargs: Any) -> None:
    """Partial-update an egkyklios_drafts row.

    Pass keyword arguments matching column names.  ``updated_at`` is
    automatically bumped to the current UTC time.

    Example::
        update_egkyklios_draft(42, status="approved", protocol_number="2026_017")
    """
    if not kwargs:
        return
    allowed = {
        "status", "draft_md_path", "draft_pdf_path",
        "protocol_number", "sharepoint_url", "brevo_campaign_id", "workflow_id",
        "title",
    }
    invalid = set(kwargs) - allowed
    if invalid:
        raise ValueError(f"Unknown egkyklios_drafts columns: {invalid}")
    conn = _get_connection()
    sets = ", ".join(f"{k} = ?" for k in kwargs)
    values = list(kwargs.values()) + [draft_id]
    conn.execute(
        f"UPDATE egkyklios_drafts SET {sets}, updated_at = datetime('now') WHERE id = ?",
        values,
    )
    conn.commit()


def list_egkyklios_drafts(
    *,
    kind: str | None = None,
    status: str | None = None,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Return egkyklios drafts filtered by kind and/or status (newest first)."""
    conn = _get_connection()
    clauses: list[str] = []
    params: list[Any] = []
    if kind is not None:
        clauses.append("kind = ?")
        params.append(kind)
    if status is not None:
        clauses.append("status = ?")
        params.append(status)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(limit)
    rows = conn.execute(
        f"SELECT * FROM egkyklios_drafts {where} ORDER BY created_at DESC LIMIT ?",
        params,
    ).fetchall()
    return [dict(r) for r in rows]


def list_director_briefings_in_window(
    period_start: str,
    period_end: str,
) -> list[dict[str, Any]]:
    """Return all director briefings archived within [period_start, period_end]."""
    conn = _get_connection()
    rows = conn.execute(
        """SELECT id, meeting_ref, kind, protocol_number, local_path,
                  sharepoint_url, archived_at, source_message_id, workflow_id
           FROM director_briefings
           WHERE archived_at >= ? AND archived_at <= ?
           ORDER BY archived_at ASC""",
        (period_start, period_end + "T23:59:59"),
    ).fetchall()
    return [dict(r) for r in rows]


def list_completed_minutes_in_window(
    period_start: str,
    period_end: str,
) -> list[dict[str, Any]]:
    """Return workflow_state rows for completed board minutes in the window.

    Looks at the ``data`` JSON column's ``context`` sub-object to find
    the meeting date; falls back to ``updated_at`` for rows that don't
    carry a meeting date in context.
    """
    conn = _get_connection()
    rows = conn.execute(
        """SELECT workflow_id, state, data, created_at, updated_at
           FROM workflow_state
           WHERE workflow_name = 'board_meeting_minutes'
             AND state IN ('completed', 'approved')
             AND updated_at >= ? AND updated_at <= ?
           ORDER BY updated_at ASC""",
        (period_start, period_end + "T23:59:59"),
    ).fetchall()
    return [dict(r) for r in rows]
