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


_SAMPLE_RATE = 16000            # faster-whisper's native rate
_CUT_SEARCH_SECONDS = 30.0      # look this far either side of a boundary for silence
_CUT_WINDOW_SECONDS = 0.5       # energy window used to find the quietest cut point
_DECODE_BLOCK_SAMPLES = 500000  # PyAV frame grouping (same as faster-whisper)


def _quiet_cut(audio, *, target: int, lo: int, hi: int, window: int) -> int:
    """Index of the quietest point in ``audio[lo:hi]`` (window-energy minimum).

    Falls back to *target* when the range is narrower than one window.
    """
    import numpy as np

    k = (hi - lo) // window if window > 0 else 0
    if k < 1:
        return target
    frames = np.asarray(audio[lo:lo + k * window], dtype=np.float32).reshape(k, window)
    return lo + int(np.argmin(np.square(frames).mean(axis=1))) * window + window // 2


def _stream_chunks(blocks, *, chunk_samples: int, search_samples: int,
                   window_samples: int):
    """Re-cut a stream of audio blocks into roughly chunk-sized pieces.

    Yields ``(start_sample, piece)``; the pieces are contiguous and together
    cover the whole stream. Each cut moves to the quietest window within
    *search_samples* of the nominal boundary so a word is not split. Only about
    one piece plus the search margin is held in memory, however long the track.
    """
    import numpy as np

    window = max(1, window_samples)
    need = chunk_samples + search_samples + window
    buf: list = []
    buffered = 0
    emitted = 0

    for block in blocks:
        if block is None or len(block) == 0:
            continue
        buf.append(np.asarray(block, dtype=np.float32))
        buffered += len(block)
        while chunk_samples > 0 and buffered >= need:
            audio = np.concatenate(buf) if len(buf) > 1 else buf[0]
            lo = max(window, chunk_samples - search_samples)
            hi = min(len(audio) - window, chunk_samples + search_samples)
            cut = _quiet_cut(audio, target=chunk_samples, lo=lo, hi=hi, window=window)
            yield emitted, audio[:cut]
            emitted += cut
            rest = np.array(audio[cut:], dtype=np.float32)  # copy, so the piece can be freed
            buf, buffered = [rest], len(rest)

    if buffered:
        yield emitted, (np.concatenate(buf) if len(buf) > 1 else buf[0])


def _decode_blocks(audio_path: str):
    """Yield 16 kHz mono float32 blocks from *audio_path* as they are decoded.

    Same PyAV pipeline as ``faster_whisper.decode_audio``, which instead builds
    the whole track in memory (~1.7 GB peak for a 5-hour track).
    """
    import gc

    try:
        import av
        import numpy as np
        from faster_whisper.audio import (
            _group_frames,
            _ignore_invalid_frames,
            _resample_frames,
        )
    except ImportError:  # pragma: no cover - private helpers moved upstream
        from faster_whisper import decode_audio

        yield decode_audio(audio_path, sampling_rate=_SAMPLE_RATE)
        return

    resampler = av.audio.resampler.AudioResampler(
        format="s16", layout="mono", rate=_SAMPLE_RATE
    )
    try:
        with av.open(audio_path, mode="r", metadata_errors="ignore") as container:
            frames = container.decode(audio=0)
            frames = _ignore_invalid_frames(frames)
            frames = _group_frames(frames, _DECODE_BLOCK_SAMPLES)
            frames = _resample_frames(frames, resampler)
            for frame in frames:
                yield frame.to_ndarray().reshape(-1).astype(np.float32) / 32768.0
    finally:
        # PyAV resampler objects are not freed without an explicit collection
        # (faster-whisper issue #390).
        del resampler
        gc.collect()


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

_OOM_MARKERS = ("malloc", "bad alloc", "failed to allocate", "out of memory")


def _is_out_of_memory(exc: BaseException) -> bool:
    """True for the allocation failures CTranslate2 / ONNX raise when RAM runs out."""
    if isinstance(exc, MemoryError):
        return True
    msg = str(exc).lower()
    return any(marker in msg for marker in _OOM_MARKERS)


def _read_piece_cache(path: str | None, n_samples: int):
    """Cached result for one audio piece, or ``None`` if absent, stale or corrupt."""
    import json
    import os

    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        if data.get("samples") != n_samples:
            return None
        return [(str(t), float(s), float(e)) for t, s, e in data["pieces"]]
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _write_piece_cache(path: str | None, n_samples: int, pieces) -> None:
    """Atomically save one transcribed piece. Never fails the run."""
    import json
    import os

    if not path:
        return
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"samples": n_samples, "pieces": [list(p) for p in pieces]},
                      fh, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError as exc:
        logger.warning("Could not cache transcribed piece %s: %s", path, exc)


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
        cache_dir: str | None = None,
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
        # tracks are decoded as a stream and transcribed in pieces of this many
        # seconds, cut at the quietest moment near each boundary; only about one
        # piece is held in memory at a time. 0 disables splitting.
        self.chunk_seconds = chunk_seconds
        # Each finished piece is saved here (text + absolute timestamps), keyed by
        # the track and the settings that shape the text, so an interrupted or
        # crashed multi-hour run resumes where it stopped. None disables it.
        self.cache_dir = cache_dir
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

        # Decode and transcribe piece by piece: only about one piece of audio
        # is in memory at a time, however long the (padded) track is.
        chunk = int(self.chunk_seconds * _SAMPLE_RATE)
        cache_prefix = self._cache_prefix(audio_path, kwargs) if self.cache_dir else None
        pieces: list[tuple[str, float, float]] = []
        skipped_seconds = 0.0
        for start, audio in _stream_chunks(
            self._audio_blocks(audio_path),
            chunk_samples=chunk,
            search_samples=min(int(_CUT_SEARCH_SECONDS * _SAMPLE_RATE), chunk // 2),
            window_samples=int(_CUT_WINDOW_SECONDS * _SAMPLE_RATE),
        ):
            offset = start / _SAMPLE_RATE
            cache_file = (
                f"{self.cache_dir}/{cache_prefix}_{start}.json" if cache_prefix else None
            )
            cached = _read_piece_cache(cache_file, len(audio))
            if cached is not None:
                pieces.extend(cached)
                continue
            got = self._transcribe_piece(model, audio, kwargs, offset)
            if got is None:
                skipped_seconds += len(audio) / _SAMPLE_RATE
                continue
            pieces.extend(got)
            _write_piece_cache(cache_file, len(audio), got)
        if skipped_seconds:
            logger.error(
                "%s: %.1f min of audio could not be transcribed (out of memory) and "
                "is MISSING from this track. Rerun the same command to retry only "
                "the missing pieces.",
                audio_path, skipped_seconds / 60,
            )
        return pieces

    def _transcribe_piece(self, model, audio, kwargs: dict, offset: float):
        """Transcribe one piece, retrying with beam 1 on an out-of-memory error.

        Returns ``None`` when the piece still cannot be transcribed, so the
        caller skips only this piece instead of losing the whole track. Any
        other error propagates as before.
        """
        import gc

        attempts = [kwargs]
        if (kwargs.get("beam_size") or 1) > 1:
            attempts.append({**kwargs, "beam_size": 1})
        for n, attempt in enumerate(attempts):
            try:
                segments, _info = model.transcribe(audio, **attempt)
                # consume the lazy generator while this piece is in scope
                return [
                    (seg.text.strip(), seg.start + offset, seg.end + offset)
                    for seg in segments
                ]
            except Exception as exc:  # noqa: BLE001 - only memory errors are handled
                if not _is_out_of_memory(exc):
                    raise
                gc.collect()
                last = n + 1 == len(attempts)
                logger.warning(
                    "Out of memory at %.1f min (beam %s): %s - %s",
                    offset / 60, attempt.get("beam_size"), exc,
                    "skipping this piece" if last else "retrying with beam 1",
                )
        return None

    def _cache_prefix(self, audio_path: str, kwargs: dict) -> str:
        """Identify a track plus the settings that shape its text.

        Beam size and thread count are deliberately excluded: a piece that
        needed the beam-1 fallback is still a valid result.
        """
        import hashlib
        import json
        import os

        st = os.stat(audio_path)
        ident = json.dumps(
            {
                "file": os.path.basename(audio_path),
                "size": st.st_size,
                "mtime": st.st_mtime_ns,
                "model": self.model_size,
                "language": kwargs.get("language"),
                "prompt": kwargs.get("initial_prompt"),
                "vad": self.vad_filter,
                "vad_silence_ms": self.vad_min_silence_ms,
                "chunk_seconds": self.chunk_seconds,
            },
            sort_keys=True,
            ensure_ascii=False,
        )
        return hashlib.sha1(ident.encode("utf-8")).hexdigest()[:16]

    def _audio_blocks(self, audio_path: str):
        """Stream decoded 16 kHz mono audio blocks (overridable in tests)."""
        return _decode_blocks(audio_path)
