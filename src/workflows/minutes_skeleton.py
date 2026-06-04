"""Pure, deterministic assembly of a board-meeting "minutes skeleton".

This module is the model-free, side-effect-free core of a larger pipeline that
turns Zoom board-meeting audio into formal Greek minutes (praktika). Downstream
stages (small language models) will convert each grouped statement into formal
third-person Greek and synthesize prose -- NONE of that happens here.

``build_minutes_skeleton`` takes structured inputs (agenda, captured events,
transcript segments, roster) and returns a structured scaffold. It is:

* **Pure** -- same inputs always produce the same output.
* **Offline** -- no database, no network, no model calls, no file I/O.
* **Auditable** -- nothing a member said is ever dropped; off-topic stretches are
  *flagged*, not removed. This matters for a human-rights organisation that wants
  deterministic, inspectable automation.

Windowing rules (the heart of the logic)
-----------------------------------------
* **Agenda windows.** ``agenda_advance`` events are sorted by timestamp. Each one
  marks the meeting moving *into* the agenda item identified by its ``to_index``
  (1-based) -- or, if that index is out of range, by a title match. The window of
  an item is ``[its advance ts, next advance ts)``. The last advanced item's
  window ends at the ``phase:end`` timestamp if one exists, otherwise it extends
  to ``+infinity``. Every title in ``agenda_items`` appears in the output, in
  order, even if it never received an ``agenda_advance`` (such items have
  ``start == end == None`` and no segments).

* **Segment assignment.** A transcript segment is assigned to the item whose
  window contains the segment's ``start`` (half-open ``[start, end)``). Segments
  that fall before the first item's start, inside a break
  (``phase:break`` .. next ``phase:resume``), or after ``phase:end`` go to
  ``unassigned_segments``. Off-topic segments are NOT unassigned merely for being
  off-topic -- they stay in their time window and carry ``"off_topic": True``.

* **Off-topic flagging.** ``off_topic`` events arrive as ``begin``/``end`` pairs
  (matched in timestamp order). A segment whose ``start`` falls inside an
  off-topic span is flagged ``off_topic=True``; everything else is ``False``. An
  unpaired ``begin`` (no following ``end``) extends to meeting end.

* **Votes.** A ``vote`` event attaches to the item whose window contains the
  vote's ``ts``. If the vote falls in no item's window (e.g. cast during a break,
  or before the first advance), it attaches to the last item that *started*
  before the vote ts; if no item started before it, the vote is skipped. The
  vote's ``ts`` is preserved on the attached record.

* **Presence.** ``present`` is the union of (a) roster members whose *latest*
  ``presence`` event status is ``present`` or ``joined``, and (b) roster members
  who appear as the ``speaker`` of at least one segment (speaking implies
  attendance). A member with a *latest* ``presence`` status of ``absent`` or
  ``left`` is forced into ``absent`` even if they spoke or joined earlier --
  explicit latest state wins. Everyone else on the roster is ``absent``. Members
  who appear only in presence events (not on the roster) are still included in
  ``present`` with ``role == ""`` so their attendance is not lost. Roster
  matching of ``payload["member"]`` is by exact name first, then
  case-insensitive.

Times
-----
Segment ``start``/``end`` are accepted as :class:`datetime` and emitted as
``.isoformat()`` strings. Event ``ts`` values are already ISO-8601 strings and
are kept verbatim.

The module imports only the standard library; it depends on nothing in the
project, which keeps it trivially unit-testable.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass
class TranscriptSegment:
    """A single contiguous utterance from one speaker.

    ``start``/``end`` are wall-clock UTC datetimes.
    """

    speaker: str
    text: str
    start: datetime
    end: datetime


# ---------------------------------------------------------------------------
# Internal helpers (all pure)
# ---------------------------------------------------------------------------

# Sentinels for an open-ended final window. ``datetime.min``/``max`` are made
# timezone-aware so comparisons against aware event/segment datetimes never
# raise. They are only ever used for ordering, never emitted.
_NEG_INF = datetime.min.replace(tzinfo=timezone.utc)
_POS_INF = datetime.max.replace(tzinfo=timezone.utc)


def _parse_ts(ts: str) -> datetime:
    """Parse an ISO-8601 timestamp into an aware datetime.

    Accepts a trailing ``Z`` (treated as UTC). A naive result is assumed UTC so
    that it remains comparable with the aware sentinels and segment datetimes.
    """

    text = ts.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _as_aware(value: datetime) -> datetime:
    """Return ``value`` as an aware datetime (assume UTC if naive)."""

    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _events_of(events: list[dict], event_type: str) -> list[dict]:
    """All events of ``event_type``, sorted by their ``ts`` (stable)."""

    selected = [e for e in events if e.get("event_type") == event_type]
    return sorted(selected, key=lambda e: _parse_ts(e["ts"]))


def _segment_to_dict(segment: TranscriptSegment, *, off_topic: bool) -> dict:
    """Convert a :class:`TranscriptSegment` to the output dict shape."""

    return {
        "speaker": segment.speaker,
        "text": segment.text,
        "start": _as_aware(segment.start).isoformat(),
        "end": _as_aware(segment.end).isoformat(),
        "off_topic": off_topic,
    }


def _meeting_end_ts(events: list[dict]) -> datetime | None:
    """Timestamp of the first ``phase:end`` event, or ``None`` if absent."""

    for event in _events_of(events, "phase"):
        if (event.get("payload") or {}).get("phase") == "end":
            return _parse_ts(event["ts"])
    return None


def _build_break_windows(events: list[dict]) -> list[tuple[datetime, datetime]]:
    """Half-open ``[break, resume)`` windows during which the meeting paused.

    A ``phase:break`` opens a window that closes at the next ``phase:resume``.
    A ``break`` with no following ``resume`` extends to ``+infinity``.
    """

    windows: list[tuple[datetime, datetime]] = []
    open_break: datetime | None = None
    for event in _events_of(events, "phase"):
        phase = (event.get("payload") or {}).get("phase")
        ts = _parse_ts(event["ts"])
        if phase == "break" and open_break is None:
            open_break = ts
        elif phase == "resume" and open_break is not None:
            windows.append((open_break, ts))
            open_break = None
    if open_break is not None:
        windows.append((open_break, _POS_INF))
    return windows


def _build_offtopic_windows(events: list[dict]) -> list[tuple[datetime, datetime]]:
    """Half-open ``[begin, end)`` off-topic spans.

    ``begin``/``end`` are matched in timestamp order. An unmatched ``begin``
    extends to ``+infinity`` (clamped to meeting end by the caller's data, but we
    keep ``+inf`` so trailing off-topic chatter is still flagged).
    """

    windows: list[tuple[datetime, datetime]] = []
    open_begin: datetime | None = None
    for event in _events_of(events, "off_topic"):
        state = (event.get("payload") or {}).get("state")
        ts = _parse_ts(event["ts"])
        if state == "begin" and open_begin is None:
            open_begin = ts
        elif state == "end" and open_begin is not None:
            windows.append((open_begin, ts))
            open_begin = None
    if open_begin is not None:
        windows.append((open_begin, _POS_INF))
    return windows


def _in_any_window(moment: datetime, windows: list[tuple[datetime, datetime]]) -> bool:
    """True if ``moment`` lies in any half-open ``[lo, hi)`` window."""

    return any(lo <= moment < hi for lo, hi in windows)


def _build_items(
    agenda_items: list[str],
    events: list[dict],
) -> list[dict]:
    """Build the per-item scaffold with resolved ``[start, end)`` windows.

    Returns a list of dicts, one per agenda title (in order), each carrying a
    private ``_window`` tuple of aware datetimes for assignment, plus the public
    ``start``/``end`` ISO strings (or ``None``).
    """

    advances = _events_of(events, "agenda_advance")
    end_ts = _meeting_end_ts(events)

    # Map each advance to a 0-based agenda position. Prefer to_index (1-based);
    # fall back to a title match when to_index is out of range.
    n = len(agenda_items)
    title_to_pos: dict[str, int] = {}
    for pos, title in enumerate(agenda_items):
        # First occurrence of a title wins, mirroring agenda ordering.
        title_to_pos.setdefault(title, pos)

    # advance_ts_by_pos: the *earliest* advance ts resolved to each position.
    advance_ts_by_pos: dict[int, datetime] = {}
    resolved: list[tuple[int, datetime]] = []  # (pos, ts) in ts order
    for adv in advances:
        payload = adv.get("payload") or {}
        ts = _parse_ts(adv["ts"])
        to_index = payload.get("to_index")
        pos: int | None = None
        if isinstance(to_index, int) and 1 <= to_index <= n:
            pos = to_index - 1
        else:
            title = payload.get("title")
            if title in title_to_pos:
                pos = title_to_pos[title]
        if pos is None:
            continue
        resolved.append((pos, ts))

    # The boundary list drives window ends: each advance's window runs until the
    # next advance (by ts), regardless of which item it points to.
    boundaries = sorted(resolved, key=lambda pr: pr[1])
    for idx, (pos, ts) in enumerate(boundaries):
        # Keep the earliest ts if the same item is advanced into twice.
        if pos not in advance_ts_by_pos or ts < advance_ts_by_pos[pos]:
            advance_ts_by_pos[pos] = ts

    # For each resolved position, its window end is the ts of the next boundary
    # after its own start; the final boundary ends at end_ts or +inf.
    window_end_by_pos: dict[int, datetime] = {}
    for idx, (pos, ts) in enumerate(boundaries):
        if pos in window_end_by_pos and advance_ts_by_pos.get(pos) != ts:
            # Position already assigned via its earliest advance; skip later dup.
            continue
        if idx + 1 < len(boundaries):
            window_end_by_pos[pos] = boundaries[idx + 1][1]
        else:
            window_end_by_pos[pos] = end_ts if end_ts is not None else _POS_INF

    items: list[dict] = []
    for pos, title in enumerate(agenda_items):
        start_dt = advance_ts_by_pos.get(pos)
        if start_dt is None:
            window = (_POS_INF, _POS_INF)  # never active -> contains nothing
            start_iso: str | None = None
            end_iso: str | None = None
        else:
            end_dt = window_end_by_pos.get(pos, _POS_INF)
            window = (start_dt, end_dt)
            start_iso = start_dt.isoformat()
            end_iso = None if end_dt is _POS_INF else end_dt.isoformat()
        items.append(
            {
                "index": pos + 1,
                "title": title,
                "start": start_iso,
                "end": end_iso,
                "segments": [],
                "votes": [],
                "_window": window,
            }
        )
    return items


def _find_item_for_moment(items: list[dict], moment: datetime) -> int | None:
    """Index into ``items`` whose window contains ``moment``; else ``None``.

    If overlapping windows ever occur, the earliest-starting item wins (items are
    already in agenda order, and ``_build_items`` derives ends from the global
    boundary order, so overlaps are not expected).
    """

    for i, item in enumerate(items):
        lo, hi = item["_window"]
        if lo <= moment < hi:
            return i
    return None


def _last_item_started_before(items: list[dict], moment: datetime) -> int | None:
    """Index of the last item whose start is <= ``moment``; else ``None``.

    Used as the fallback target for a vote that lands in no item window.
    """

    best: int | None = None
    best_start = _NEG_INF
    for i, item in enumerate(items):
        lo, _hi = item["_window"]
        if lo is _POS_INF:
            continue  # item never advanced into
        if lo <= moment and lo >= best_start:
            best = i
            best_start = lo
    return best


def _resolve_presence(
    events: list[dict],
    segments: list[TranscriptSegment],
    roster: list[dict],
    ignore_speakers: set[str] | None = None,
) -> dict:
    """Compute the ``present``/``absent`` partition.

    See the module docstring for the precedence rules. ``ignore_speakers`` is a
    set of non-person segment labels (e.g. the unknown-speaker fallback) that
    must never count as attendees.
    """
    ignore = ignore_speakers or set()

    # Roster lookup: exact name -> entry, plus a case-insensitive fallback map.
    roster_by_name: dict[str, dict] = {}
    roster_by_lower: dict[str, dict] = {}
    ordered_roster: list[dict] = []
    for entry in roster:
        name = entry.get("name", "")
        role = entry.get("role", "")
        normalized = {"name": name, "role": role}
        ordered_roster.append(normalized)
        roster_by_name.setdefault(name, normalized)
        roster_by_lower.setdefault(name.lower(), normalized)

    def resolve_member(raw: str) -> dict | None:
        """Match a presence ``member`` string to a roster entry."""

        if raw in roster_by_name:
            return roster_by_name[raw]
        return roster_by_lower.get(raw.lower())

    # Latest presence status per roster entry (keyed by canonical roster name),
    # and latest status for non-roster members (keyed by raw member string).
    latest_status_roster: dict[str, str] = {}
    latest_status_extra: dict[str, str] = {}
    extra_members: dict[str, str] = {}  # raw name -> raw name (insertion order)

    for event in _events_of(events, "presence"):
        payload = event.get("payload") or {}
        member = payload.get("member")
        status = payload.get("status")
        if not member or not status:
            continue
        matched = resolve_member(member)
        if matched is not None:
            # Events are processed in ts order, so the last write is the latest.
            latest_status_roster[matched["name"]] = status
        else:
            latest_status_extra[member] = status
            extra_members.setdefault(member, member)

    # Roster members who spoke at least once (speaking implies attendance).
    spoke: set[str] = set()
    speaker_extra: dict[str, str] = {}
    for segment in segments:
        if segment.speaker in ignore:
            continue  # non-person label (e.g. unknown-speaker fallback)
        matched = resolve_member(segment.speaker)
        if matched is not None:
            spoke.add(matched["name"])
        elif segment.speaker:
            speaker_extra.setdefault(segment.speaker, segment.speaker)

    present: list[dict] = []
    absent: list[dict] = []
    for entry in ordered_roster:
        name = entry["name"]
        status = latest_status_roster.get(name)
        if status in ("absent", "left"):
            absent.append({"name": name, "role": entry["role"]})
            continue
        if status in ("present", "joined") or name in spoke:
            present.append({"name": name, "role": entry["role"]})
        else:
            absent.append({"name": name, "role": entry["role"]})

    # Non-roster members: include in present (role "") unless their latest
    # explicit status is absent/left. Presence-event members first, then any
    # speakers not otherwise seen, preserving first-seen order.
    seen_extra: set[str] = set()
    for raw in extra_members:
        if latest_status_extra.get(raw) in ("absent", "left"):
            continue
        present.append({"name": raw, "role": ""})
        seen_extra.add(raw)
    for raw in speaker_extra:
        if raw in seen_extra or raw in extra_members:
            continue
        present.append({"name": raw, "role": ""})
        seen_extra.add(raw)

    return {"present": present, "absent": absent}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_minutes_skeleton(
    *,
    meeting_ref: str,
    agenda_items: list[str],
    events: list[dict],
    segments: list[TranscriptSegment],
    roster: list[dict],
    ignore_speakers: set[str] | None = None,
) -> dict:
    """Assemble a deterministic, pre-LLM minutes skeleton.

    See the module docstring for the full contract and windowing rules. The
    function is pure: no I/O, no randomness, no globals mutated.

    Returns a dict with keys ``meeting_ref``, ``presence``, ``agenda``,
    ``items``, ``phases`` and ``unassigned_segments`` (see module docs for the
    exact nested shape).
    """

    agenda_items = list(agenda_items or [])
    events = list(events or [])
    segments = list(segments or [])
    roster = list(roster or [])

    items = _build_items(agenda_items, events)

    break_windows = _build_break_windows(events)
    offtopic_windows = _build_offtopic_windows(events)
    end_ts = _meeting_end_ts(events)

    # Earliest agenda start (first moment any item is "active").
    first_start: datetime | None = None
    for item in items:
        lo, _hi = item["_window"]
        if lo is not _POS_INF and (first_start is None or lo < first_start):
            first_start = lo

    unassigned: list[dict] = []

    # Assign segments by the time of their start.
    for segment in sorted(segments, key=lambda s: _as_aware(s.start)):
        moment = _as_aware(segment.start)
        off_topic = _in_any_window(moment, offtopic_windows)
        seg_dict = _segment_to_dict(segment, off_topic=off_topic)

        # Unassigned if: before the meeting/first item, during a break, or after
        # the meeting end. Off-topic segments are still time-assigned (flagged).
        before_first = first_start is None or moment < first_start
        during_break = _in_any_window(moment, break_windows)
        after_end = end_ts is not None and moment >= end_ts

        if during_break or after_end or before_first:
            unassigned.append(seg_dict)
            continue

        item_idx = _find_item_for_moment(items, moment)
        if item_idx is None:
            unassigned.append(seg_dict)
        else:
            items[item_idx]["segments"].append(seg_dict)

    # Attach votes to items by the vote ts.
    for vote in _events_of(events, "vote"):
        payload = vote.get("payload") or {}
        moment = _parse_ts(vote["ts"])
        record = {
            "label": payload.get("label"),
            "result": payload.get("result"),
            "tally": payload.get("tally"),
            "method": payload.get("method"),
            "ts": vote["ts"],
        }
        target = _find_item_for_moment(items, moment)
        if target is None:
            target = _last_item_started_before(items, moment)
        if target is None:
            # No item started before this vote -> deterministically skipped.
            continue
        items[target]["votes"].append(record)

    # Phases in ts order (public projection).
    phases = [
        {"phase": (e.get("payload") or {}).get("phase"), "ts": e["ts"]}
        for e in _events_of(events, "phase")
    ]

    presence = _resolve_presence(events, segments, roster, ignore_speakers)

    # Strip the private ``_window`` key before returning.
    public_items = []
    for item in items:
        public_items.append(
            {
                "index": item["index"],
                "title": item["title"],
                "start": item["start"],
                "end": item["end"],
                "segments": item["segments"],
                "votes": item["votes"],
            }
        )

    return {
        "meeting_ref": meeting_ref,
        "presence": presence,
        "agenda": list(agenda_items),
        "items": public_items,
        "phases": phases,
        "unassigned_segments": unassigned,
    }
