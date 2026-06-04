"""Minutes pipeline orchestrator — one configurable entry point.

This module is the thin, configurable seam that wires the *already-built*
minutes components into a single flow:

    Zoom recording manifest  ─┐
                              ├─► segments ─► build_minutes_skeleton ─► (optional) LLM draft
    plain/VTT transcript file ┘

The heavy lifting lives elsewhere and is NOT reimplemented here:

* :mod:`src.workflows.minutes_skeleton` — the pure, deterministic skeleton core.
* :mod:`src.workflows.minutes_transcription` — manifest→segments orchestration,
  the ``Transcriber`` protocol, and the real ``FasterWhisperTranscriber``.
* :mod:`src.core.meeting_events` — the captured-events store.

Design rules honoured here:

* Importing this module never requires faster-whisper or any LLM SDK. The real
  ASR transcriber is constructed lazily by the factory only when selected, and
  the LLM client is lazy-imported inside :func:`draft_from_skeleton`.
* :func:`assemble_minutes` NEVER crashes on a missing/failed LLM — drafting
  degrades to ``draft=None`` with a logged warning.
* All datetimes used for transcript-file alignment are timezone-aware UTC.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.core.meeting_events import MeetingEventsStore
from src.workflows.minutes_skeleton import (
    TranscriptSegment,
    build_minutes_skeleton,
)
from src.workflows.minutes_transcription import (
    FasterWhisperTranscriber,
    Transcriber,
    _build_initial_prompt,
    build_minutes_from_recording,
    manifest_to_segments,
)
from src.workflows.timeline_speakers import (
    UNKNOWN_SPEAKER,
    attribute_segments,
    parse_timeline,
)


def _remap_speakers(segments, aliases: dict) -> list:
    """Rewrite segment speaker labels through a display-name → canonical alias
    map (e.g. Zoom's "Giorgos Athanasias" → roster "Γεώργιος Αθανασιάς"). Exact,
    case-insensitive match; unmapped speakers pass through unchanged."""
    if not aliases:
        return segments
    lower = {str(k).lower(): v for k, v in aliases.items()}
    for s in segments:
        mapped = aliases.get(s.speaker) or lower.get((s.speaker or "").lower())
        if mapped:
            s.speaker = mapped
    return segments

logger = logging.getLogger(__name__)

_ORG_NAMES = ["Διεθνής Αμνηστία", "Αμνηστία"]


# ---------------------------------------------------------------------------
# Roster / glossary builders
# ---------------------------------------------------------------------------

def build_glossary(settings) -> list[str]:
    """Names + org terms used to prime ASR and (later) drafting.

    Board-member full names (``"first last"``) followed by the organisation
    names. De-duplicated while preserving first-seen order.
    """

    members = settings.workflows.board_meeting.board_members or []
    terms: list[str] = [f"{m.first_name} {m.last_name}" for m in members]
    terms.extend(_ORG_NAMES)

    seen: set[str] = set()
    glossary: list[str] = []
    for term in terms:
        term = (term or "").strip()
        if term and term not in seen:
            seen.add(term)
            glossary.append(term)
    return glossary


def build_roster(settings) -> list[dict]:
    """Roster of ``{"name", "role"}`` dicts from board members.

    ``role`` is taken from a ``role`` attribute if the member config grows one;
    today it defaults to ``""`` (the current ``BoardMemberConfig`` has no role).
    """

    members = settings.workflows.board_meeting.board_members or []
    return [
        {
            "name": f"{m.first_name} {m.last_name}",
            "role": getattr(m, "role", "") or "",
        }
        for m in members
    ]


# ---------------------------------------------------------------------------
# Transcriber factory + fake (no-ASR) transcriber
# ---------------------------------------------------------------------------

class FakeTranscriber:
    """A no-ASR :class:`Transcriber` for testing and dry wiring.

    For an audio file at ``<path>`` it looks for a sidecar ``<path>.txt``:

    * present → returns the whole file as ONE piece ``[(text, 0.0, 60.0)]``.
    * absent  → returns a single placeholder piece
      ``[("[fake transcript of <basename>]", 0.0, 5.0)]``.

    This lets the manifest→segments→skeleton wiring be exercised with zero real
    speech recognition: drop a ``.txt`` next to a dummy audio path in a manifest.
    """

    def transcribe(
        self,
        audio_path: str,
        *,
        language: str = "el",
        initial_prompt: str = "",
    ) -> list[tuple[str, float, float]]:
        sidecar = Path(f"{audio_path}.txt")
        if sidecar.exists():
            text = sidecar.read_text(encoding="utf-8").strip()
            return [(text, 0.0, 60.0)]
        basename = Path(audio_path).name
        return [(f"[fake transcript of {basename}]", 0.0, 5.0)]


def get_transcriber(settings) -> Transcriber:
    """Construct the configured :class:`Transcriber`.

    Selected by ``settings.minutes_pipeline.transcriber``:
      * ``"faster_whisper"`` → :class:`FasterWhisperTranscriber` (lazy heavy dep).
      * ``"fake"`` → :class:`FakeTranscriber`.
      * anything else → :class:`ValueError` listing valid values.
    """

    cfg = settings.minutes_pipeline
    choice = (cfg.transcriber or "").strip()
    if choice == "faster_whisper":
        return FasterWhisperTranscriber(
            model_size=cfg.whisper_model,
            device=cfg.whisper_device,
            compute_type=cfg.whisper_compute_type,
        )
    if choice == "fake":
        return FakeTranscriber()
    raise ValueError(
        f"Unknown transcriber {choice!r}; valid values are: faster_whisper, fake"
    )


# ---------------------------------------------------------------------------
# Transcript-file parsing (the no-ASR test path)
# ---------------------------------------------------------------------------

# A WebVTT cue timestamp line, e.g. "00:01:23.450 --> 00:01:27.000".
_VTT_TIME_RE = re.compile(
    r"(\d{1,2}:\d{2}:\d{2}[.,]\d{1,3})\s*-->\s*(\d{1,2}:\d{2}:\d{2}[.,]\d{1,3})"
)
# A leading WebVTT voice tag, e.g. "<v Ελένη Κοντού>text".
_VTT_VOICE_RE = re.compile(r"^<v\s+([^>]+)>\s*(.*)$", re.DOTALL)
# A Zoom-copy offset line, e.g. "00:12:05" (HH:MM:SS, no millis).
_HHMMSS_RE = re.compile(r"^(\d{1,2}):([0-5]?\d):([0-5]?\d)$")


def _vtt_offset_to_seconds(stamp: str) -> float:
    """Convert an ``HH:MM:SS.mmm`` (or ``,mmm``) cue stamp to seconds."""

    stamp = stamp.replace(",", ".")
    hh, mm, rest = stamp.split(":")
    ss = float(rest)
    return int(hh) * 3600 + int(mm) * 60 + ss


def _strip_speaker_prefix(text: str) -> tuple[str, str]:
    """Split a ``"Name: utterance"`` line into ``(speaker, text)``.

    Returns ``("", text)`` if there is no plausible ``Name:`` prefix. The
    speaker part must be short-ish and not look like a sentence (heuristic:
    at most a few words before the first colon).
    """

    if ":" in text:
        head, _, tail = text.partition(":")
        head = head.strip()
        # A speaker label is short and has no terminal punctuation.
        if head and len(head) <= 60 and len(head.split()) <= 5:
            return head, tail.strip()
    return "", text.strip()


def _parse_vtt(lines: list[str], *, base: datetime) -> list[TranscriptSegment]:
    """Parse WebVTT cue blocks into wall-clock segments."""

    segments: list[TranscriptSegment] = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i].strip()
        match = _VTT_TIME_RE.search(line)
        if not match:
            i += 1
            continue
        try:
            start_off = _vtt_offset_to_seconds(match.group(1))
            end_off = _vtt_offset_to_seconds(match.group(2))
        except (ValueError, IndexError):
            logger.warning("Skipping malformed VTT cue timing: %r", line)
            i += 1
            continue

        # Collect the cue text lines until a blank line.
        i += 1
        text_lines: list[str] = []
        while i < n and lines[i].strip():
            text_lines.append(lines[i].strip())
            i += 1
        raw_text = " ".join(text_lines).strip()
        if not raw_text:
            continue

        speaker = ""
        voice = _VTT_VOICE_RE.match(raw_text)
        if voice:
            speaker = voice.group(1).strip()
            raw_text = voice.group(2).strip()
        else:
            speaker, raw_text = _strip_speaker_prefix(raw_text)

        segments.append(
            TranscriptSegment(
                speaker=speaker,
                text=raw_text,
                start=base + timedelta(seconds=start_off),
                end=base + timedelta(seconds=end_off),
            )
        )
    return segments


def _parse_zoom_copy(lines: list[str], *, base: datetime) -> list[TranscriptSegment]:
    """Parse Zoom's "copy transcript" plain blocks into wall-clock segments.

    Each turn is:

        SpeakerName
        HH:MM:SS
        text line(s)...

    Real Zoom "copy transcript" output is **contiguous** (no blank lines between
    turns), so we delimit turns by the ``HH:MM:SS`` line itself: the line
    immediately before a timestamp is the speaker, and the text runs from after
    the timestamp up to the line before the next turn's timestamp. Blank lines,
    if present, are tolerated (ignored).

    The ``HH:MM:SS`` is an offset (seconds since meeting start). Wall-clock
    start = ``base + offset``; ``end`` is the next turn's start, or +30s for the
    final turn (no end information is available in this format).
    """

    # Keep only non-empty lines; the format carries no semantic blank lines.
    rows = [ln.strip() for ln in lines if ln.strip()]

    # Indices of timestamp-only lines mark each turn.
    ts_idx = [i for i, ln in enumerate(rows) if _HHMMSS_RE.fullmatch(ln)]
    if not ts_idx:
        logger.warning("Zoom-copy transcript has no HH:MM:SS lines; nothing parsed")
        return []

    parsed: list[tuple[str, float, str]] = []  # (speaker, offset, text)
    for n, i in enumerate(ts_idx):
        speaker = rows[i - 1] if i > 0 else "Άγνωστος"
        # Text = lines after this timestamp up to (but excluding) the next
        # turn's speaker line, i.e. up to the line before the next timestamp.
        next_ts = ts_idx[n + 1] if n + 1 < len(ts_idx) else len(rows)
        text_end = (next_ts - 1) if n + 1 < len(ts_idx) else next_ts
        text = " ".join(rows[i + 1:text_end]).strip()
        if not text:
            continue
        hh, mm, ss = (int(g) for g in _HHMMSS_RE.fullmatch(rows[i]).groups())
        offset = float(hh * 3600 + mm * 60 + ss)
        parsed.append((speaker, offset, text))

    parsed.sort(key=lambda p: p[1])
    segments: list[TranscriptSegment] = []
    for idx, (speaker, offset, text) in enumerate(parsed):
        if idx + 1 < len(parsed):
            end_off = parsed[idx + 1][1]
            if end_off <= offset:
                end_off = offset + 30.0
        else:
            end_off = offset + 30.0
        segments.append(
            TranscriptSegment(
                speaker=speaker,
                text=text,
                start=base + timedelta(seconds=offset),
                end=base + timedelta(seconds=end_off),
            )
        )
    return segments


def parse_transcript_file(path, *, base: datetime) -> list[TranscriptSegment]:
    """Parse a meeting transcript text file into wall-clock segments.

    Two formats are supported and auto-detected:

    1. **WebVTT** — the file ends in ``.vtt`` or its content starts with
       ``WEBVTT``. Cue blocks ``HH:MM:SS.mmm --> HH:MM:SS.mmm`` then text; the
       speaker comes from a leading ``<v Name>`` tag or a ``"Name: text"``
       prefix. Cue offsets are added to ``base`` to get wall-clock time.
    2. **Zoom copy / plain** — the format produced by Zoom's "copy transcript"
       button: repeating blocks of a ``SpeakerName`` line, an ``HH:MM:SS`` line,
       then one or more text lines, separated by blank lines. The ``HH:MM:SS`` is
       an offset (seconds since meeting start); wall-clock = ``base + offset``.

    ``base`` MUST be a timezone-aware datetime (UTC); offsets are added to it.
    Malformed cues/blocks are skipped with a logged warning. Returned segments
    are sorted by ``start``.
    """

    file_path = Path(path)
    text = file_path.read_text(encoding="utf-8")
    is_vtt = (
        file_path.suffix.lower() == ".vtt"
        or text.lstrip().upper().startswith("WEBVTT")
    )
    lines = text.splitlines()

    if is_vtt:
        segments = _parse_vtt(lines, base=base)
    else:
        segments = _parse_zoom_copy(lines, base=base)

    segments.sort(key=lambda s: s.start)
    return segments


# ---------------------------------------------------------------------------
# Optional LLM draft
# ---------------------------------------------------------------------------

def _parse_llm_json(raw: str) -> dict:
    """Parse an LLM JSON reply defensively.

    Models often wrap JSON in ```json … ``` fences or add prose around it. Strip
    fences, then fall back to the first ``{…}`` block. Returns ``{"raw": text}``
    only when no JSON object can be recovered.
    """
    import re as _re
    text = raw.strip()
    # Strip a leading ```json / ``` fence and trailing ```.
    fence = _re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, _re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else {"raw": parsed}
    except (json.JSONDecodeError, TypeError):
        pass
    # Last resort: first balanced-looking {...} block.
    m = _re.search(r"\{.*\}", text, _re.DOTALL)
    if m:
        try:
            parsed = json.loads(m.group(0))
            if isinstance(parsed, dict):
                return parsed
        except (json.JSONDecodeError, TypeError):
            pass
    return {"raw": raw}


def draft_from_skeleton(skeleton: dict, glossary: list[str], settings) -> dict | None:
    """Turn a minutes skeleton into a πρακτικά draft via the LLM.

    Serialises the skeleton to JSON, loads the ``board_minutes`` system prompt,
    and calls :meth:`ClaudeClient.generate` (same pattern as
    ``egkyklios_general``). The model response is parsed defensively: if it is
    valid JSON it is returned as a dict; otherwise the raw text is wrapped as
    ``{"raw": text}``. Returns ``None`` on any failure (logged), so a missing or
    failing LLM never breaks :func:`assemble_minutes`. ClaudeClient is imported
    lazily so this module imports without an LLM SDK installed.
    """

    try:
        from src.core.claude import ClaudeClient

        client = ClaudeClient()
        system_prompt = client.load_prompt("board_minutes")
        glossary_line = ", ".join(glossary) if glossary else ""
        user_prompt = (
            "Παρακάτω δίνεται το δομημένο σκελετός πρακτικών (minutes skeleton) "
            "σε JSON. Συντάξτε ολοκληρωμένα πρακτικά με βάση αυτό.\n\n"
            f"Ονόματα/όροι: {glossary_line}\n\n"
            "## Minutes skeleton (JSON)\n\n"
            f"{json.dumps(skeleton, ensure_ascii=False, indent=2)}"
        )
        raw = client.generate(
            user_prompt=user_prompt,
            system_prompt=system_prompt,
            workflow="minutes_pipeline",
            max_tokens=8000,
        )
        if not raw:
            return None
        return _parse_llm_json(raw)
    except Exception as exc:  # noqa: BLE001 — drafting must never crash assemble
        logger.warning("Minutes drafting failed; continuing without draft: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Helpers for assemble_minutes
# ---------------------------------------------------------------------------

def _parse_iso_utc(value: str) -> datetime | None:
    """Parse an ISO-8601 string into an aware UTC datetime, or ``None``."""

    if not value:
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except (ValueError, TypeError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _agenda_items_from_events(events: list[dict]) -> list[str]:
    """Agenda titles from ``agenda_advance`` events, ordered by ``to_index``.

    Ties / missing indices keep first-seen order. Titles are de-duplicated.
    """

    advances = [e for e in events if e.get("event_type") == "agenda_advance"]
    entries: list[tuple[int, int, str]] = []  # (to_index, seen_order, title)
    for order, ev in enumerate(advances):
        payload = ev.get("payload") or {}
        title = (payload.get("title") or "").strip()
        if not title:
            continue
        to_index = payload.get("to_index")
        if not isinstance(to_index, int):
            to_index = 10 ** 6 + order  # push index-less items to the end, stable
        entries.append((to_index, order, title))

    entries.sort(key=lambda t: (t[0], t[1]))
    items: list[str] = []
    seen: set[str] = set()
    for _idx, _order, title in entries:
        if title not in seen:
            seen.add(title)
            items.append(title)
    return items


def _derive_base(
    meeting_ref: str,
    events: list[dict],
    meeting_start: datetime | None,
) -> datetime:
    """Resolve the wall-clock origin for transcript-file offsets.

    Precedence: explicit ``meeting_start`` → earliest ``agenda_advance`` ts →
    ``<year>-01-01`` where the year is parsed from ``meeting_ref`` digits (else
    the current year). Always returned timezone-aware (UTC).
    """

    if meeting_start is not None:
        if meeting_start.tzinfo is None:
            return meeting_start.replace(tzinfo=timezone.utc)
        return meeting_start

    advance_ts: list[datetime] = []
    for ev in events:
        if ev.get("event_type") == "agenda_advance":
            parsed = _parse_iso_utc(ev.get("ts") or "")
            if parsed is not None:
                advance_ts.append(parsed)
    if advance_ts:
        return min(advance_ts)

    year_match = re.search(r"(\d{4})", meeting_ref or "")
    year = int(year_match.group(1)) if year_match else datetime.now(timezone.utc).year
    return datetime(year, 1, 1, tzinfo=timezone.utc)


def _safe_ref(meeting_ref: str) -> str:
    """Filesystem-safe slug for a meeting_ref (keeps Greek letters)."""

    slug = re.sub(r"[^\w\-.]+", "_", meeting_ref or "", flags=re.UNICODE).strip("_")
    return slug or "meeting"


# ---------------------------------------------------------------------------
# Manifest -> segments (timeline-attributed, with per-participant fallback)
# ---------------------------------------------------------------------------

def _find_timeline_file(manifest: dict) -> dict | None:
    """Return the manifest entry for the timeline JSON, if present."""

    for entry in manifest.get("files") or []:
        if (entry.get("recording_type") or "").strip().lower() == "timeline":
            return entry
    return None


def _find_mixed_audio_file(manifest: dict) -> dict | None:
    """Return the whole-meeting mixed audio entry from the manifest.

    The mixed audio is an ``audio_only`` file under ``recording_files`` (NOT a
    per-participant track). When several qualify we prefer the largest by
    ``file_size`` — the mixed track captures every speaker, so it is the biggest.
    """

    candidates = [
        entry
        for entry in (manifest.get("files") or [])
        if (entry.get("source") or "") == "recording_files"
        and (entry.get("recording_type") or "").strip().lower() == "audio_only"
    ]
    if not candidates:
        return None

    def _size(entry: dict) -> int:
        try:
            return int(entry.get("file_size") or 0)
        except (TypeError, ValueError):
            return 0

    return max(candidates, key=_size)


def segments_from_manifest(
    *,
    manifest: dict,
    transcriber: Transcriber,
    base: datetime,
    language: str,
    glossary: list[str] | None = None,
) -> list[TranscriptSegment]:
    """Turn a recording manifest into wall-clock transcript segments.

    Preferred path (timeline attribution): if the manifest contains BOTH a
    ``timeline`` JSON file and a mixed ``audio_only`` file (under
    ``recording_files``), transcribe the single mixed audio once and label each
    piece with the dominant active speaker from the timeline (see
    :mod:`src.workflows.timeline_speakers`). Timeline usernames are Zoom display
    names (often Latin) used verbatim — Latin->Greek roster matching is out of
    scope.

    Fallback path: otherwise, defer to the per-participant
    :func:`manifest_to_segments` (one audio file per speaker, roster-resolved).
    """

    timeline_entry = _find_timeline_file(manifest)
    mixed_entry = _find_mixed_audio_file(manifest)

    if timeline_entry and mixed_entry:
        prompt = _build_initial_prompt(glossary)
        raw = transcriber.transcribe(
            mixed_entry.get("local_path") or "",
            language=language,
            initial_prompt=prompt,
        )
        intervals = parse_timeline(timeline_entry.get("local_path") or "")
        return attribute_segments(raw, intervals, base=base)

    # No timeline/mixed pair → per-participant attribution. ``manifest_to_segments``
    # resolves speakers from each file's ``participant`` field; roster matching is
    # applied downstream in ``build_minutes_skeleton``, so passing roster=None here
    # is fine (anonymous tracks get stable "Ομιλητής N" labels).
    return manifest_to_segments(
        manifest,
        transcriber,
        roster=None,
        language=language,
        glossary=glossary,
    )


# ---------------------------------------------------------------------------
# Transcript cache (so ASR runs once; re-drafts/re-processing are instant)
# ---------------------------------------------------------------------------

def _segments_to_json(segments: list[TranscriptSegment]) -> list[dict]:
    return [
        {"speaker": s.speaker, "text": s.text,
         "start": s.start.isoformat(), "end": s.end.isoformat()}
        for s in segments
    ]


def _segments_from_json(data: list[dict]) -> list[TranscriptSegment]:
    out: list[TranscriptSegment] = []
    for d in data or []:
        start = _parse_iso_utc(d.get("start") or "") or datetime(1970, 1, 1, tzinfo=timezone.utc)
        end = _parse_iso_utc(d.get("end") or "") or start
        out.append(TranscriptSegment(
            speaker=d.get("speaker", ""), text=d.get("text", ""), start=start, end=end,
        ))
    return out


def _transcript_cache_path(settings, meeting_ref: str) -> Path:
    return Path(settings.minutes_pipeline.transcripts_dir) / _safe_ref(meeting_ref) / "transcript.json"


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def assemble_minutes(
    *,
    settings,
    meeting_ref: str,
    manifest_path=None,
    transcript_path=None,
    reuse_transcript: bool = False,
    meeting_start: datetime | None = None,
    draft: bool = False,
    events_store=None,
    transcriber: Transcriber | None = None,
) -> dict:
    """Assemble a minutes skeleton (and optional draft) from one source.

    Exactly one of ``transcript_path`` or ``manifest_path`` must be provided
    (transcript wins if both are given). Events come from the meeting-events
    store; agenda items are derived from ``agenda_advance`` events.

    Returns a dict with keys: ``meeting_ref``, ``skeleton``, ``segment_count``,
    ``source`` (``"manifest"`` | ``"transcript"``), ``skeleton_path``, and —
    when ``draft=True`` — ``draft`` (dict or ``None``) and ``draft_path``.

    Outputs are written under
    ``settings.minutes_pipeline.transcripts_dir/<safe meeting_ref>/``:
    ``skeleton.json`` always, ``draft.json`` when a draft was produced.
    """

    roster = build_roster(settings)
    glossary = build_glossary(settings)
    aliases = getattr(settings.minutes_pipeline, "speaker_aliases", {}) or {}

    store = events_store or MeetingEventsStore()
    events = store.list_events(meeting_ref)
    agenda_items = _agenda_items_from_events(events)

    if reuse_transcript:
        # Load the cached raw transcript (no ASR) → instant re-process / re-draft.
        source = "cache"
        cache = _transcript_cache_path(settings, meeting_ref)
        if not cache.exists():
            raise ValueError(
                f"no cached transcript at {cache} — run a --manifest build first"
            )
        segments = _remap_speakers(
            _segments_from_json(json.loads(cache.read_text(encoding="utf-8"))), aliases
        )
        skeleton = build_minutes_skeleton(
            meeting_ref=meeting_ref,
            agenda_items=agenda_items,
            events=events,
            segments=segments,
            roster=roster,
            ignore_speakers={UNKNOWN_SPEAKER},
        )
        segment_count = len(segments)
    elif transcript_path:
        source = "transcript"
        base = _derive_base(meeting_ref, events, meeting_start)
        segments = _remap_speakers(parse_transcript_file(transcript_path, base=base), aliases)
        skeleton = build_minutes_skeleton(
            meeting_ref=meeting_ref,
            agenda_items=agenda_items,
            events=events,
            segments=segments,
            roster=roster,
            ignore_speakers={UNKNOWN_SPEAKER},
        )
        segment_count = len(segments)
    elif manifest_path:
        source = "manifest"
        manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
        transcriber = transcriber or get_transcriber(settings)
        language = getattr(settings.minutes_pipeline, "language", "el") or "el"

        # base = mixed file's recording_start (aware UTC) → manifest start_time → epoch.
        mixed_entry = _find_mixed_audio_file(manifest)
        base = None
        if mixed_entry is not None:
            base = _parse_iso_utc(mixed_entry.get("recording_start") or "")
        if base is None:
            base = _parse_iso_utc(manifest.get("start_time") or "")
        if base is None:
            base = datetime(1970, 1, 1, tzinfo=timezone.utc)

        segments = _remap_speakers(
            segments_from_manifest(
                manifest=manifest,
                transcriber=transcriber,
                base=base,
                language=language,
                glossary=glossary,
            ),
            aliases,
        )
        skeleton = build_minutes_skeleton(
            meeting_ref=meeting_ref,
            agenda_items=agenda_items,
            events=events,
            segments=segments,
            roster=roster,
            ignore_speakers={UNKNOWN_SPEAKER},
        )
        segment_count = sum(len(item.get("segments", [])) for item in skeleton["items"])
        segment_count += len(skeleton.get("unassigned_segments", []))
        # Cache the raw transcript so future runs (re-draft, re-process) skip the
        # expensive ASR — load it back with reuse_transcript=True.
        cache = _transcript_cache_path(settings, meeting_ref)
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(_segments_to_json(segments), ensure_ascii=False, indent=2), encoding="utf-8")
    else:
        raise ValueError("provide manifest_path, transcript_path, or reuse_transcript")

    result: dict = {
        "meeting_ref": meeting_ref,
        "skeleton": skeleton,
        "segment_count": segment_count,
        "source": source,
    }

    # Write outputs.
    out_dir = Path(settings.minutes_pipeline.transcripts_dir) / _safe_ref(meeting_ref)
    out_dir.mkdir(parents=True, exist_ok=True)
    skeleton_path = out_dir / "skeleton.json"
    skeleton_path.write_text(
        json.dumps(skeleton, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    result["skeleton_path"] = str(skeleton_path)

    if draft:
        draft_obj = draft_from_skeleton(skeleton, glossary, settings)
        result["draft"] = draft_obj
        if draft_obj is not None:
            draft_path = out_dir / "draft.json"
            draft_path.write_text(
                json.dumps(draft_obj, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            result["draft_path"] = str(draft_path)

    return result
