"""Active-speaker attribution from a Zoom recording timeline.

Zoom per-participant audio files carry ``participant_name = null`` (useless for
naming). The recording's *timeline* JSON, however, records the active-speaker
set over time WITH display names. Each timeline entry marks the moment the
active-speaker set changes; between ``ts[i]`` and ``ts[i+1]`` the active speaker
is ``entry[i].users[0].username`` (an empty ``users`` list means silence).

This module is PURE: it turns that timeline into half-open
:class:`SpeakerInterval`s and attributes transcript segments (from the mixed
audio) to whoever was the dominant active speaker during each segment.

NOTE ON NAMES: timeline usernames are Zoom *display* names (often Latin, e.g.
"Giorgos Athanasias") while the board roster is in Greek. We deliberately do NOT
force-map Latin->Greek here: the timeline username is used verbatim, which is
the accurate label. Roster matching is out of scope for this module.

stdlib only (json + open for the optional path-reading in :func:`parse_timeline`).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from src.workflows.minutes_transcription import TranscriptSegment

# Label for segments the timeline can't attribute (silence / overlapping speech).
# Treated as a non-person by presence resolution (see build_minutes_skeleton's
# ``ignore_speakers``), so it never inflates attendance.
UNKNOWN_SPEAKER = "Άγνωστος ομιλητής"


@dataclass
class SpeakerInterval:
    """A half-open ``[start, end)`` window (seconds) with one active speaker."""

    start: float  # seconds from recording start
    end: float
    username: str


def _hhmmss_ms_to_seconds(ts: str) -> float:
    """Convert an ``HH:MM:SS.mmm`` offset string to seconds.

    Example: ``"00:04:19.410"`` -> ``259.41``. The fractional part is optional.
    """

    text = (ts or "").strip()
    hh, mm, rest = text.split(":")
    ss = float(rest)
    return int(hh) * 3600 + int(mm) * 60 + ss


def parse_timeline(timeline: dict | list | str) -> list[SpeakerInterval]:
    """Build active-speaker intervals from a Zoom recording timeline.

    Accepts the timeline dict (with a ``"timeline"`` key), its bare list, or a
    path (``str``) to the JSON file. Builds half-open intervals
    ``[ts_i, ts_{i+1})`` with ``username = entry.users[0].username`` when
    ``users`` is non-empty; empty-``users`` gaps (silence) are skipped. Zero- or
    negative-length intervals are dropped. The result is sorted by ``start``.
    """

    if isinstance(timeline, str):
        data = json.loads(Path(timeline).read_text(encoding="utf-8"))
    else:
        data = timeline

    if isinstance(data, dict):
        entries = data.get("timeline") or []
    else:
        entries = data or []

    # Each entry's ts is the moment the active-speaker set changes. Sort by ts
    # so consecutive entries delimit each interval, even if the JSON is unsorted.
    parsed: list[tuple[float, list]] = []
    for entry in entries:
        ts_raw = entry.get("ts") if isinstance(entry, dict) else None
        if ts_raw is None:
            continue
        try:
            ts = _hhmmss_ms_to_seconds(ts_raw)
        except (ValueError, AttributeError):
            continue
        users = entry.get("users") or []
        parsed.append((ts, users))

    parsed.sort(key=lambda p: p[0])

    intervals: list[SpeakerInterval] = []
    for i, (ts, users) in enumerate(parsed):
        if i + 1 >= len(parsed):
            # The final entry has no following boundary; nothing to close it.
            break
        end = parsed[i + 1][0]
        if end <= ts:
            continue  # zero/negative-length window
        if not users:
            continue  # silence gap
        first = users[0] or {}
        username = (first.get("username") or "").strip()
        if not username:
            continue
        intervals.append(SpeakerInterval(start=ts, end=end, username=username))

    intervals.sort(key=lambda iv: iv.start)
    return intervals


def dominant_speaker(
    seg_start: float,
    seg_end: float,
    intervals: list[SpeakerInterval],
) -> str | None:
    """Return the username with the greatest total overlap with the segment.

    Overlap is measured against the half-open segment ``[seg_start, seg_end)``.
    Returns ``None`` if no interval overlaps the segment.
    """

    totals: dict[str, float] = {}
    order: list[str] = []  # preserve first-seen order for stable tie-breaking
    for iv in intervals:
        overlap = min(seg_end, iv.end) - max(seg_start, iv.start)
        if overlap <= 0:
            continue
        if iv.username not in totals:
            totals[iv.username] = 0.0
            order.append(iv.username)
        totals[iv.username] += overlap

    if not totals:
        return None
    # Max overlap; ties broken by first-seen order (stable).
    return max(order, key=lambda name: totals[name])


def attribute_segments(
    raw: list[tuple[str, float, float]],
    intervals: list[SpeakerInterval],
    *,
    base: datetime,
    fallback: str = UNKNOWN_SPEAKER,
) -> list[TranscriptSegment]:
    """Label transcriber output with the dominant active speaker per piece.

    ``raw`` is the transcriber output ``[(text, start_offset, end_offset), ...]``
    where offsets are seconds from the recording start. For each piece the
    speaker is :func:`dominant_speaker` over the timeline intervals, or the
    :data:`UNKNOWN_SPEAKER` fallback when nothing overlaps (timeline silence /
    overlapping speech). Wall-clock ``start``/``end`` are ``base + offset``.
    Returned segments are sorted by ``start``.
    """

    fallback_label = fallback
    segments: list[TranscriptSegment] = []
    for text, off_start, off_end in raw or []:
        start_off = float(off_start)
        end_off = float(off_end)
        speaker = dominant_speaker(start_off, end_off, intervals) or fallback_label
        segments.append(
            TranscriptSegment(
                speaker=speaker,
                text=text,
                start=base + timedelta(seconds=start_off),
                end=base + timedelta(seconds=end_off),
            )
        )

    segments.sort(key=lambda s: s.start)
    return segments
