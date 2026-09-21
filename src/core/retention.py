"""Retention: recordings and transcripts do not live here forever.

`FOUNDATION.md` §3.1 says Zoom recordings and transcripts are kept "until the
minutes are finalised, then deleted". They are the most sensitive data the
platform holds - hours of board members speaking freely - and nothing was
enforcing that, so a laptop folder kept growing.

This plans deletions and reports them; deleting is a separate, explicit step.
Two rules:

* once a meeting's minutes are finalised, its recording and transcript go
  after ``retention.grace_days`` (a short window for a correction);
* nothing survives ``retention.max_age_days``, even if minutes never got
  finalised, because "we forgot" is not a lawful basis for keeping it.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from src.config import settings
from src.core.audit import _get_connection, log_action

logger = logging.getLogger(__name__)

_MEETING_REF_RE = re.compile(r"ΔΣ\d{1,2}-\d{4}")
# Kept even when the media goes: they are the written record's raw source and
# are tiny, so they stay under the register's own retention, not this one.
_KEEP_SUFFIXES = {".json"}


@dataclass
class RetentionItem:
    """One folder considered for deletion, and why."""
    path: Path
    kind: str                      # "recording" | "transcript"
    meeting_ref: str
    started: datetime | None
    bytes: int
    delete: bool
    reason: str
    files: list[Path] = field(default_factory=list)

    @property
    def age_days(self) -> int:
        if not self.started:
            return -1
        return (datetime.now(timezone.utc) - self.started).days


def _folder_size(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def _parse_ts(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def finalised_meetings() -> set[str]:
    """Meeting refs whose minutes workflow ran to completion."""
    done: set[str] = set()
    try:
        conn = _get_connection()
        rows = conn.execute(
            "SELECT data FROM workflow_state "
            "WHERE workflow_name = 'board_meeting_minutes' AND state = 'completed'"
        ).fetchall()
    except Exception as exc:
        logger.warning("Could not read minutes workflows (%s); treating none as finalised", exc)
        return done
    for row in rows:
        try:
            context = (json.loads(row["data"] or "{}")).get("context") or {}
        except (ValueError, TypeError):
            continue
        ref = (context.get("meeting_ref") or context.get("raw_meeting_id") or "").strip()
        if ref and not context.get("test_mode"):
            done.add(ref)
    return done


def _meeting_ref_of(folder: Path, kind: str) -> tuple[str, datetime | None]:
    """The meeting a folder belongs to, from its manifest or its name."""
    if kind == "transcript":
        match = _MEETING_REF_RE.search(folder.name)
        return (match.group(0) if match else ""), None
    manifest = folder / "manifest.json"
    if manifest.exists():
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
            match = _MEETING_REF_RE.search(str(data.get("topic", "")))
            return (match.group(0) if match else ""), _parse_ts(data.get("start_time"))
        except (OSError, ValueError) as exc:
            logger.warning("Unreadable manifest in %s: %s", folder, exc)
    match = _MEETING_REF_RE.search(folder.name)
    return (match.group(0) if match else ""), None


def _mtime(folder: Path) -> datetime | None:
    try:
        newest = max((f.stat().st_mtime for f in folder.rglob("*") if f.is_file()), default=None)
    except OSError:
        return None
    return datetime.fromtimestamp(newest, tz=timezone.utc) if newest else None


def plan(now: datetime | None = None) -> list[RetentionItem]:
    """What would be deleted today, and what is kept and why."""
    now = now or datetime.now(timezone.utc)
    rules = settings.retention
    finalised = finalised_meetings()
    items: list[RetentionItem] = []

    for kind, root in (
        ("recording", Path(settings.minutes_pipeline.recordings_dir)),
        ("transcript", Path(settings.minutes_pipeline.transcripts_dir)),
    ):
        if not root.exists():
            continue
        for folder in sorted(p for p in root.iterdir() if p.is_dir()):
            if folder.name.startswith("_"):      # caches, probes
                continue
            ref, started = _meeting_ref_of(folder, kind)
            started = started or _mtime(folder)
            age = (now - started).days if started else -1
            size = _folder_size(folder)
            files = [f for f in folder.rglob("*")
                     if f.is_file() and f.suffix.lower() not in _KEEP_SUFFIXES]

            if age < 0:
                delete, reason = False, "age unknown - left alone"
            elif age >= rules.max_age_days:
                delete, reason = True, f"older than the {rules.max_age_days}-day maximum"
            elif ref and ref in finalised and age >= rules.grace_days:
                delete, reason = True, f"minutes finalised, {age} days old"
            elif ref and ref in finalised:
                delete, reason = False, f"minutes finalised {age} days ago - inside the grace period"
            elif ref:
                delete, reason = False, "minutes not finalised yet"
            else:
                delete, reason = False, "no meeting reference - left alone"

            items.append(RetentionItem(
                path=folder, kind=kind, meeting_ref=ref, started=started,
                bytes=size, delete=delete, reason=reason, files=files,
            ))
    return items


def apply(items: list[RetentionItem], *, actor: str = "system") -> dict[str, Any]:
    """Delete the media of every item marked for deletion.

    The manifest, timeline and other small JSON stay: they carry no speech,
    and the minutes reference them. Returns a summary.
    """
    freed = 0
    deleted: list[str] = []
    failed: list[str] = []
    for item in items:
        if not item.delete:
            continue
        for path in item.files:
            try:
                size = path.stat().st_size
                path.unlink()
                freed += size
            except OSError as exc:
                logger.warning("Could not delete %s: %s", path, exc)
                failed.append(str(path))
        # An emptied folder (no JSON left) is removed entirely.
        try:
            if not any(item.path.iterdir()):
                shutil.rmtree(item.path)
        except OSError:
            pass
        deleted.append(str(item.path))
        log_action(
            workflow="retention",
            action="media_deleted",
            actor=actor,
            target=item.meeting_ref or item.path.name,
            details={"path": str(item.path), "reason": item.reason, "bytes": item.bytes},
        )
    return {"folders": deleted, "failed": failed, "bytes_freed": freed}


def purge_state_transcripts(threshold_bytes: int = 64 * 1024) -> dict[str, Any]:
    """Drop bulky derived text from saved workflow contexts.

    Transcripts and drafts were persisted inside ``workflow_state`` on every
    step. They are derived data - the transcript file is the source - so they
    are removed rather than offloaded.
    """
    keys = ("transcript", "transcript_text", "raw_transcript", "segments",
            "captions", "draft_markdown")
    conn = _get_connection()
    rows = conn.execute("SELECT workflow_id, data FROM workflow_state").fetchall()
    cleaned, freed = [], 0
    for row in rows:
        raw = row["data"] or ""
        if not raw or not any(k in raw for k in keys):
            continue
        try:
            data = json.loads(raw)
        except ValueError:
            continue
        context = data.get("context")
        if not isinstance(context, dict):
            continue
        changed = False
        for key in keys:
            value = context.get(key)
            if isinstance(value, str) and len(value.encode("utf-8")) > threshold_bytes:
                freed += len(value.encode("utf-8"))
                context[key] = f"[purged by retention: {len(value)} chars]"
                changed = True
            elif isinstance(value, dict) and "__blob__" in value:
                # Too big to keep inline, so it was written beside the DB; the
                # file is the copy that has to go.
                blob = Path(str(value["__blob__"]))
                try:
                    freed += blob.stat().st_size
                    blob.unlink()
                except OSError as exc:
                    logger.warning("Could not delete offloaded %s: %s", blob, exc)
                context[key] = "[purged by retention]"
                changed = True
        if changed:
            conn.execute(
                "UPDATE workflow_state SET data = ? WHERE workflow_id = ?",
                (json.dumps(data, ensure_ascii=False), row["workflow_id"]),
            )
            cleaned.append(row["workflow_id"])
    if cleaned:
        conn.commit()
    return {"workflows": cleaned, "bytes_freed": freed}


def human_bytes(value: int) -> str:
    step = float(value)
    for unit in ("B", "KB", "MB", "GB"):
        if step < 1024 or unit == "GB":
            return f"{step:.0f} {unit}" if unit in ("B", "KB") else f"{step:.1f} {unit}"
        step /= 1024
    return f"{step:.1f} GB"
