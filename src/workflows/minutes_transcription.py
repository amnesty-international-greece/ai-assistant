"""Orchestration layer: Zoom recording manifest -> transcript segments -> skeleton.

This is the connective tissue between the Zoom fetch stage
(``ZoomClient.download_recording_assets``, which produces a *manifest* describing
downloaded audio files) and the pure, deterministic
``build_minutes_skeleton`` core in :mod:`src.workflows.minutes_skeleton`.

It is a PURE ORCHESTRATION layer. It owns no network and no model: the actual
ASR (automatic speech recognition) work is injected as a ``Transcriber`` so the
windowing/alignment logic can be unit-tested with a fake. The concrete
faster-whisper implementation (:class:`FasterWhisperTranscriber`) imports its
heavy dependency LAZILY inside ``transcribe`` so that importing this module never
requires faster-whisper to be installed.

What this module does:

* Selects which manifest files are per-participant audio worth transcribing.
* Runs the injected transcriber over each, producing offset-based pieces.
* Aligns each piece to wall-clock UTC using the file's own ``recording_start``
  as the origin (spike-robust: correct whether Zoom pads files to meeting start
  or starts them at the participant's join time).
* Resolves a speaker name per file (participant field, roster match, or a stable
  fallback label).
* Feeds the resulting :class:`TranscriptSegment` list into
  ``build_minutes_skeleton``.

NOTE ON COVERAGE: :class:`FasterWhisperTranscriber` is exercised only with the
real dependency installed and against real audio (a post-spike step). Everything
above it -- file selection, wall-clock alignment, speaker resolution, the
end-to-end wiring -- is what the unit tests cover via a fake transcriber.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Protocol

from src.workflows.minutes_skeleton import (
    TranscriptSegment,
    build_minutes_skeleton,
)

logger = logging.getLogger(__name__)

# Audio file extensions we are willing to transcribe.
_AUDIO_EXTENSIONS = {"m4a", "mp3", "wav", "m4p"}


class Transcriber(Protocol):
    """Anything that can turn one audio file into offset-tagged text pieces."""

    def transcribe(
        self,
        audio_path: str,
        *,
        language: str = "el",
        initial_prompt: str = "",
    ) -> list[tuple[str, float, float]]:
        """Return ``[(text, start_offset_seconds, end_offset_seconds), ...]``.

        Offsets are measured from the start of *this* audio file.
        """
        ...


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _is_audio_file(entry: dict) -> bool:
    """True if a manifest file entry looks like an audio asset.

    Decided by file extension first (the most reliable signal), then by
    ``file_type`` / ``recording_type`` containing the substring "audio".
    """

    ext = (entry.get("file_extension") or "").lower().lstrip(".")
    if ext in _AUDIO_EXTENSIONS:
        return True
    file_type = (entry.get("file_type") or "").lower()
    recording_type = (entry.get("recording_type") or "").lower()
    return "audio" in file_type or "audio" in recording_type


def _select_audio_files(manifest: dict) -> list[dict]:
    """Choose which manifest files to transcribe.

    Selection rule:

    * Prefer entries whose ``source == "participant_audio_files"`` (Zoom's
      dedicated per-participant audio array) -- these give us one clean track
      per speaker, which is exactly what we want for speaker attribution.
    * If there are NONE of those, fall back to per-participant-looking entries
      inside ``recording_files``: those with ``recording_type == "audio_only"``.
    * In both cases, keep only entries that actually look like audio
      (see :func:`_is_audio_file`), so transcripts, video, and chat ``.txt``
      files are never sent to the transcriber.
    """

    files = manifest.get("files") or []

    participant = [
        f for f in files
        if f.get("source") == "participant_audio_files" and _is_audio_file(f)
    ]
    if participant:
        return participant

    fallback = [
        f for f in files
        if (f.get("recording_type") or "").lower() == "audio_only"
        and _is_audio_file(f)
    ]
    return fallback


def _parse_iso_utc(value: str) -> datetime | None:
    """Parse an ISO-8601 string into an aware UTC datetime, or ``None``.

    Accepts a trailing ``Z``. Naive results are assumed to be UTC. Returns
    ``None`` (rather than raising) on anything unparseable so callers can skip
    the offending file with a warning.
    """

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


def _build_initial_prompt(glossary: list[str] | None) -> str:
    """Build a Greek-friendly priming string from a glossary of names/terms."""

    if not glossary:
        return ""
    terms = [t for t in glossary if t]
    if not terms:
        return ""
    return "Ονόματα και όροι: " + ", ".join(terms)


def _build_roster_matcher(roster: list[dict] | None):
    """Return a function mapping a raw participant string to a roster name.

    Matching is, in order: exact (case-insensitive) equality, then a
    case-insensitive substring relationship in either direction. Returns the
    canonical roster ``name`` on a hit, else ``None``.
    """

    entries: list[tuple[str, str]] = []  # (lower_name, canonical_name)
    for entry in roster or []:
        name = (entry.get("name") or "").strip()
        if name:
            entries.append((name.lower(), name))

    def match(raw: str) -> str | None:
        if not raw:
            return None
        candidate = raw.strip().lower()
        if not candidate:
            return None
        # Exact (case-insensitive) first.
        for lower_name, canonical in entries:
            if candidate == lower_name:
                return canonical
        # Then loose substring (either direction).
        for lower_name, canonical in entries:
            if candidate in lower_name or lower_name in candidate:
                return canonical
        return None

    return match


_SAMPLE_RATE = 16000          # faster-whisper's native rate
_CUT_SEARCH_SECONDS = 30.0    # look this far either side of a boundary for silence
_CUT_WINDOW_SECONDS = 0.5     # energy window used to find the quietest cut point


def _chunk_bounds(audio, *, chunk_samples: int, search_samples: int,
                  window_samples: int) -> list[tuple[int, int]]:
    """Split *audio* into contiguous ``(start, end)`` sample ranges.

    Each piece is about *chunk_samples* long. Rather than cutting blindly (which
    could split a word), each cut moves to the lowest-energy window within
    *search_samples* of the nominal boundary. The ranges are contiguous and
    together cover the whole array.
    """
    n = len(audio)
    if chunk_samples <= 0 or n <= chunk_samples:
        return [(0, n)]

    import numpy as np

    window = max(1, window_samples)
    bounds: list[tuple[int, int]] = []
    start = 0
    while n - start > chunk_samples:
        target = start + chunk_samples
        lo = max(start + window, target - search_samples)
        hi = min(n - window, target + search_samples)
        k = (hi - lo) // window
        if k >= 1:
            frames = np.asarray(audio[lo:lo + k * window], dtype=np.float32).reshape(k, window)
            quietest = int(np.argmin(np.square(frames).mean(axis=1)))
            cut = lo + quietest * window + window // 2
        else:
            cut = target
        bounds.append((start, cut))
        start = cut
    bounds.append((start, n))
    return bounds


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def manifest_to_segments(
    manifest: dict,
    transcriber: Transcriber,
    *,
    roster: list[dict] | None = None,
    language: str = "el",
    glossary: list[str] | None = None,
) -> list[TranscriptSegment]:
    """Transcribe the selected audio files and align them to wall-clock UTC.

    See :func:`_select_audio_files` for which files are chosen. For each file,
    the transcriber returns offset-based pieces; each piece is anchored to the
    file's own ``recording_start`` so::

        segment.start = recording_start + timedelta(seconds=offset_start)

    This holds whether Zoom pads files to the meeting start or starts them at
    the participant's join time, because every file's own ``recording_start`` is
    the origin for its own offsets.

    Speaker resolution per file (first match wins):

    1. If the file's ``participant`` matches a roster entry (case-insensitive,
       exact or loose substring), use the canonical roster ``name``.
    2. Else if ``participant`` is non-empty, use it verbatim.
    3. Else assign a stable ``"Ομιλητής N"`` label, where ``N`` increments per
       distinct file that needs one (so each anonymous track is one speaker).

    Robust to unparseable ``recording_start`` (file skipped with a warning) and
    to empty transcriber output. Returned segments are sorted by ``start``.
    """

    match_roster = _build_roster_matcher(roster)
    prompt = _build_initial_prompt(glossary)

    selected = _select_audio_files(manifest)
    segments: list[TranscriptSegment] = []
    anon_counter = 0
    attempted = 0
    failures = 0

    for entry in selected:
        base = _parse_iso_utc(entry.get("recording_start") or "")
        if base is None:
            logger.warning(
                "Skipping audio file with unparseable recording_start: %r (path=%r)",
                entry.get("recording_start"),
                entry.get("local_path"),
            )
            continue

        local_path = entry.get("local_path") or ""

        # Resolve speaker for this file.
        participant = (entry.get("participant") or "").strip()
        matched = match_roster(participant) if participant else None
        if matched:
            speaker = matched
        elif participant:
            speaker = participant
        else:
            anon_counter += 1
            speaker = f"Ομιλητής {anon_counter}"

        attempted += 1
        try:
            pieces = transcriber.transcribe(
                local_path, language=language, initial_prompt=prompt
            )
        except Exception as exc:  # noqa: BLE001 - isolate one bad file
            logger.warning(
                "Transcriber failed on %r: %s", local_path, exc
            )
            failures += 1
            continue

        for text, off_start, off_end in pieces or []:
            segments.append(
                TranscriptSegment(
                    speaker=speaker,
                    text=text,
                    start=base + timedelta(seconds=float(off_start)),
                    end=base + timedelta(seconds=float(off_end)),
                )
            )

    if attempted and failures == attempted:
        # Every track failed: returning [] would silently yield an empty
        # transcript after a long run. Fail loudly instead.
        raise RuntimeError(
            f"Transcription failed on all {attempted} audio file(s); "
            "see the warnings above for the cause."
        )
    segments.sort(key=lambda s: s.start)
    return segments


def build_minutes_from_recording(
    *,
    manifest: dict,
    events: list[dict],
    agenda_items: list[str],
    roster: list[dict],
    transcriber: Transcriber,
    glossary: list[str] | None = None,
    meeting_ref: str = "",
) -> dict:
    """Wire Zoom fetch -> transcription -> minutes skeleton in one call.

    The manifest carries no ``meeting_ref``, so it is accepted as an explicit
    parameter (defaulting to ``""``). All other knobs flow through to
    :func:`manifest_to_segments` and then to ``build_minutes_skeleton``.
    """

    segments = manifest_to_segments(
        manifest, transcriber, roster=roster, glossary=glossary
    )
    return build_minutes_skeleton(
        meeting_ref=meeting_ref,
        agenda_items=agenda_items,
        events=events,
        segments=segments,
        roster=roster,
    )


# ---------------------------------------------------------------------------
# Concrete transcriber (lazy heavy dependency; not unit-tested for real ASR)
# ---------------------------------------------------------------------------

class FasterWhisperTranscriber:
    """A :class:`Transcriber` backed by faster-whisper.

    The faster-whisper import and model construction are LAZY: nothing heavy is
    touched until :meth:`transcribe` is first called, so importing this module
    never requires the dependency. This concrete impl is exercised only with the
    dependency installed against real audio (a post-spike step); the
    orchestration around it is what the unit tests cover.
    """

    def __init__(
        self,
        model_size: str = "large-v3",
        device: str = "cpu",
        compute_type: str = "int8",
        *,
        vad_filter: bool = True,
        beam_size: int = 5,
        condition_on_previous_text: bool = False,
        vad_min_silence_ms: int = 1000,
        cpu_threads: int = 0,
        chunk_seconds: int = 1800,
    ) -> None:
        self.model_size = model_size
        self.device = device
        self.compute_type = compute_type
        # 0 = let CTranslate2 choose. Set explicitly to use all cores on a
        # multi-core box (transcribing a board meeting is CPU-bound).
        self.cpu_threads = cpu_threads
        # Voice-activity detection: per-participant tracks are mostly silence
        # (one speaker, whole-meeting duration), so VAD both skips that silence
        # -- a large speedup -- and prevents Whisper hallucinating text over it.
        self.vad_filter = vad_filter
        self.beam_size = beam_size
        # Whisper's repetition loops come mainly from conditioning each window on
        # the previous window's text; disabling it is the standard mitigation and
        # matters most on long, silence-heavy recordings.
        self.condition_on_previous_text = condition_on_previous_text
        self.vad_min_silence_ms = vad_min_silence_ms
        # Zoom pads every per-participant track to the full meeting length, and
        # faster-whisper's Silero VAD runs its LSTM over the WHOLE array in one
        # pass. A 5-hour track exhausts memory ("bad allocation"), so long
        # tracks are split into pieces of this many seconds, cut at the quietest
        # moment near each boundary. 0 disables splitting.
        self.chunk_seconds = chunk_seconds
        self._model = None  # lazily constructed on first transcribe()

    def _ensure_model(self):
        if self._model is not None:
            return self._model
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:  # pragma: no cover - depends on optional dep
            raise RuntimeError(
                "faster-whisper not installed; pip install faster-whisper"
            ) from exc
        kwargs: dict = {"device": self.device, "compute_type": self.compute_type}
        if self.cpu_threads:
            kwargs["cpu_threads"] = self.cpu_threads
        self._model = WhisperModel(self.model_size, **kwargs)
        return self._model

    def transcribe(
        self,
        audio_path: str,
        *,
        language: str = "el",
        initial_prompt: str = "",
    ) -> list[tuple[str, float, float]]:
        model = self._ensure_model()
        kwargs: dict = {
            "language": language,
            "initial_prompt": initial_prompt or None,
            "beam_size": self.beam_size,
            "condition_on_previous_text": self.condition_on_previous_text,
            "vad_filter": self.vad_filter,
        }
        if self.vad_filter:
            kwargs["vad_parameters"] = {
                "min_silence_duration_ms": self.vad_min_silence_ms
            }
        if not self.chunk_seconds:
            segments, _info = model.transcribe(audio_path, **kwargs)
            return [(seg.text.strip(), seg.start, seg.end) for seg in segments]

        audio = self._load_audio(audio_path)
        pieces: list[tuple[str, float, float]] = []
        for start, end in _chunk_bounds(
            audio,
            chunk_samples=int(self.chunk_seconds * _SAMPLE_RATE),
            search_samples=int(_CUT_SEARCH_SECONDS * _SAMPLE_RATE),
            window_samples=int(_CUT_WINDOW_SECONDS * _SAMPLE_RATE),
        ):
            offset = start / _SAMPLE_RATE
            segments, _info = model.transcribe(audio[start:end], **kwargs)
            # consume the lazy generator while this piece is in scope
            pieces.extend(
                (seg.text.strip(), seg.start + offset, seg.end + offset)
                for seg in segments
            )
        return pieces

    def _load_audio(self, audio_path: str):
        """Decode *audio_path* to 16 kHz mono float32 (overridable in tests)."""
        from faster_whisper import decode_audio

        return decode_audio(audio_path, sampling_rate=_SAMPLE_RATE)
