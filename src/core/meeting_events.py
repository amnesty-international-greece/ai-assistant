"""Board-meeting events store - deterministic capture backbone for minutes.

Records discrete MEETING EVENTS captured during a board meeting to the
``meeting_events`` SQLite table (defined in :mod:`src.core.audit`).  This is the
model-free, deterministic layer of the future minutes pipeline: a separate
component consumes these rows later to draft formal Greek minutes.  Nothing in
this module touches an LLM, Zoom, or Discord.

Synchronous on purpose: called from the CLI today and possibly the Discord bot
later.  It opens its connection via :func:`src.core.audit._get_connection`,
exactly like the audit module, so the whole platform shares one SQLite handle.

Canonical payload shape PER event_type
---------------------------------------
- ``agenda_advance``: ``{"index": int (0-based), "item": str}`` -- the item
  just moved INTO. (A ``to_index``/``title`` spelling was specified early on
  but nothing ever wrote it; the sidebar is the only writer.)
- ``vote``: ``{"label": str, "result": "passed"|"failed"|"tied",
  "tally": {"υπέρ": int, "κατά": int, "αποχή": int},
  "method": "unanimous"|"majority"}``
- ``phase``: ``{"phase": "start"|"break"|"resume"|"end"}`` (the sidebar also
  attaches ``source`` / ``zoom_ts``, which are ignored downstream)
- ``presence``: ``{"member": str, "status": "present"|"absent"|"left"|"joined"}``
- ``decision``: ``{"ref": str, "seq": int, "decision_text": str,
  "outcome": str, "considerations": [str], "agenda_index": int,
  "agenda_item": str}`` -- written live by the Zoom sidebar
  (``POST /zoom-app/decision``). ``ref`` is the canonical
  ``ΔΣNN-MM-YYYY`` computed from ``seq``. NOTE: these are rendered
  VERBATIM into the minutes and are never paraphrased by a model, so
  this payload is the highest-stakes one to get right.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from src.core.audit import _get_connection

VALID_EVENT_TYPES = {
    "agenda_advance",
    "vote",
    "phase",
    "presence",
    "decision",   # captured live from the Zoom sidebar: ref + text + outcome
}


# Required payload fields per event_type. Each entry is a tuple of ALTERNATIVE
# key groups: the payload must carry at least one key from every group. Aliases
# are listed because the original spec and the live Zoom sidebar drifted apart;
# accepting both means validation can never reject a real in-meeting capture.
# Extra keys are always allowed. Unlisted event types are not payload-checked.
_REQUIRED_PAYLOAD_KEYS: dict[str, tuple[tuple[str, ...], ...]] = {
    "agenda_advance": (("index",), ("item",)),
    "vote":           (("label",), ("result",)),
    "phase":          (("phase",),),
    "presence":       (("member",), ("status",)),
    "decision":       (("decision_text",),),
}


def _validate_payload(event_type: str, payload: dict) -> None:
    """Raise ValueError if *payload* lacks a field its event_type needs.

    The payload column is free-form JSON, so without this a wiring bug (a
    renamed key, a typo) is stored happily and only shows up much later as
    minutes with content under the wrong agenda item. Checking at capture time
    turns that silent corruption into an immediate, obvious error.

    Deliberately shallow: presence of required keys only - no type or
    enum checking, and extra keys are fine. The goal is to catch wiring
    mistakes, not to police callers.
    """
    groups = _REQUIRED_PAYLOAD_KEYS.get(event_type)
    if not groups:
        return
    missing = ["/".join(g) for g in groups if not any(k in payload for k in g)]
    if missing:
        raise ValueError(
            f"payload for event_type {event_type!r} is missing required "
            f"field(s): {', '.join(missing)}. Got keys: "
            f"{sorted(payload) or '(empty)'}. See the module docstring for the "
            f"canonical payload shape of each event type."
        )


class MeetingEventsStore:
    """Synchronous SQLite-backed store for board-meeting events."""

    def record_event(
        self,
        *,
        meeting_ref: str,
        event_type: str,
        payload: dict,
        ts: datetime | None = None,
        confidence: str = "confirmed",
    ) -> int:
        """Insert one meeting event; return the new row id.

        Args:
            meeting_ref: Meeting identifier, e.g. ``"ΔΣ05-2026"``.
            event_type:  One of :data:`VALID_EVENT_TYPES`.
            payload:     Event-type-specific dict (see module docstring).
            ts:          When the event occurred on the meeting clock.
                         Defaults to ``datetime.now(timezone.utc)``.
            confidence:  ``"confirmed"`` (default) or ``"low"`` (auto-proposed,
                         unconfirmed).

        Raises:
            ValueError: if *event_type* is not in :data:`VALID_EVENT_TYPES`.
        """
        if event_type not in VALID_EVENT_TYPES:
            valid = ", ".join(sorted(VALID_EVENT_TYPES))
            raise ValueError(
                f"Invalid event_type {event_type!r}; valid types are: {valid}"
            )
        _validate_payload(event_type, payload or {})
        if ts is None:
            ts = datetime.now(timezone.utc)
        created_at = datetime.now(timezone.utc).isoformat()
        conn = _get_connection()
        cur = conn.execute(
            """INSERT INTO meeting_events
                   (meeting_ref, event_type, ts, payload, confidence, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                meeting_ref,
                event_type,
                ts.isoformat(),
                json.dumps(payload, ensure_ascii=False),
                confidence,
                created_at,
            ),
        )
        conn.commit()
        return int(cur.lastrowid)

    def list_events(
        self,
        meeting_ref: str,
        *,
        event_type: str | None = None,
    ) -> list[dict]:
        """Return events for *meeting_ref*, ordered by ``ts`` ascending.

        Each row is a dict with keys ``id, meeting_ref, event_type, ts,
        payload, confidence, created_at``.  ``payload`` is JSON-decoded back
        to a dict.  Pass *event_type* to filter to a single type.
        """
        conn = _get_connection()
        sql = "SELECT * FROM meeting_events WHERE meeting_ref = ?"
        params: list = [meeting_ref]
        if event_type is not None:
            sql += " AND event_type = ?"
            params.append(event_type)
        sql += " ORDER BY ts ASC"
        rows = conn.execute(sql, params).fetchall()
        result: list[dict] = []
        for row in rows:
            data = dict(row)
            raw = data.get("payload")
            data["payload"] = json.loads(raw) if raw else {}
            result.append(data)
        return result

    def delete_event(self, event_id: int) -> bool:
        """Delete the event with *event_id*; return True if a row was removed.

        Used to correct mistaken captures.
        """
        conn = _get_connection()
        cur = conn.execute(
            "DELETE FROM meeting_events WHERE id = ?",
            (event_id,),
        )
        conn.commit()
        return cur.rowcount > 0
