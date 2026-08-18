"""LLM organiser: file ambiguous turns under the right agenda item and tag them.

The deterministic skeleton assigns turns by time window, which is exact only
when the sidebar's agenda marks are. In practice the chair advances late, or the
Board circles back to a previous topic - so a slice of every meeting lands in the
generic opening bucket or under the wrong item.

This pass fixes that slice with a narrowly-scoped model call. It does NOT write
prose and it does NOT decide anything: for each turn it proposes an agenda item
and a kind (``substantive`` / ``procedural`` / ``off_topic``).

Design rules, all load-bearing:

* **Flag, never delete.** Every turn stays in the skeleton with its tag. Only
  the DRAFT input is filtered, so nothing a member said can vanish from the
  record - the ethical spine of the whole pipeline.
* **Hybrid, not model-first.** Only genuinely ambiguous turns are sent (the
  unassigned/opening bucket, plus turns the skeleton itself placed by inference
  rather than by a real agenda mark). Turns filed by a reliable agenda mark are
  left alone, so the deterministic core keeps its authority.
* **Fail safe.** Any malformed, missing or unrecognised answer leaves the turn
  exactly as the deterministic pass had it. A failed organiser degrades to
  "no change", never to a corrupted skeleton.
* **Auditable.** Every relocation records ``assigned_by="llm"`` alongside the
  ``gap_fallback`` / time-window provenance already carried by segments.
"""

from __future__ import annotations

import json
import logging
import re

logger = logging.getLogger(__name__)

SUBSTANTIVE, PROCEDURAL, OFF_TOPIC = "substantive", "procedural", "off_topic"
_VALID_TAGS = {SUBSTANTIVE, PROCEDURAL, OFF_TOPIC}
_OPENING = "opening"

# Defaults (overridable via settings.minutes_pipeline).
_DEFAULT_BATCH_TURNS = 40
_DEFAULT_TEXT_LIMIT = 600      # chars of a turn shown to the organiser
_DEFAULT_MAX_TOKENS = 8000


def _norm(title: str) -> str:
    return " ".join((title or "").split()).strip().lower()


def _parse_response(raw: str, expected: int) -> dict[int, dict]:
    """Parse the organiser's JSON array into ``{turn index: {agenda, tag}}``.

    Tolerates code fences and surrounding prose. Entries that are malformed, out
    of range, or carry an unknown tag are dropped - the caller then leaves those
    turns untouched.
    """
    text = (raw or "").strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        match = re.search(r"\[.*\]", text, re.DOTALL)
        if not match:
            return {}
        try:
            data = json.loads(match.group(0))
        except (json.JSONDecodeError, TypeError):
            return {}
    if not isinstance(data, list):
        return {}

    out: dict[int, dict] = {}
    for entry in data:
        if not isinstance(entry, dict):
            continue
        idx = entry.get("i")
        if not isinstance(idx, int) or not (0 <= idx < expected):
            continue
        tag = str(entry.get("tag") or "").strip().lower()
        out[idx] = {
            "agenda": str(entry.get("agenda") or "").strip(),
            "tag": tag if tag in _VALID_TAGS else "",
        }
    return out


def _render_batch(turns: list[dict], titles: list[str], text_limit: int) -> str:
    """Build the user prompt for one batch of turns."""
    lines = [
        "## Θέματα ημερήσιας διάταξης (χρησιμοποίησε ΑΚΡΙΒΩΣ αυτούς τους τίτλους)",
    ]
    lines.extend(f"- {t}" for t in titles)
    lines.append(f"- {_OPENING}  (για ο,τι δεν ανήκει σε κανένα θέμα)")
    lines.append("\n## Σειρές προς ταξινόμηση")
    for i, turn in enumerate(turns):
        speaker = (turn.get("speaker") or "").strip()
        text = " ".join((turn.get("text") or "").split())[:text_limit]
        lines.append(f"[{i}] {speaker}: {text}")
    lines.append(
        "\nΕπέστρεψε ΜΟΝΟ τον JSON πίνακα, μία εγγραφή για κάθε σειρά, με τα ίδια i."
    )
    return "\n".join(lines)


def _candidates(skeleton: dict) -> list[tuple[dict, str]]:
    """Turns worth sending, as ``(turn, origin)``.

    Ambiguous means: the opening/unassigned bucket (the chair had not marked an
    item yet), and turns the skeleton itself placed by inference rather than by
    a real agenda mark (``assigned_by`` set, e.g. ``gap_fallback``).
    """
    out: list[tuple[dict, str]] = []
    for turn in skeleton.get("unassigned_segments") or []:
        out.append((turn, _OPENING))
    for item in skeleton.get("items") or []:
        title = item.get("title") or ""
        for turn in item.get("segments") or []:
            if turn.get("assigned_by"):        # inferred, not marked - re-check it
                out.append((turn, title))
    return out


def organize_skeleton(skeleton: dict, settings, *, client=None) -> dict:
    """Tag and re-file the skeleton's ambiguous turns. Mutates *skeleton*.

    Returns a stats dict: ``{"considered", "tagged", "moved", "batches",
    "by_tag"}``. Never raises - on any failure the skeleton is left unchanged.
    """
    stats = {"considered": 0, "tagged": 0, "moved": 0, "batches": 0,
             "by_tag": {SUBSTANTIVE: 0, PROCEDURAL: 0, OFF_TOPIC: 0}}

    titles = [it.get("title") or "" for it in skeleton.get("items") or []]
    titles = [t for t in titles if t]
    candidates = _candidates(skeleton)
    if not titles or not candidates:
        return stats

    cfg = getattr(settings, "minutes_pipeline", None)
    batch_size = int(getattr(cfg, "organizer_batch_turns", _DEFAULT_BATCH_TURNS)
                     or _DEFAULT_BATCH_TURNS)
    text_limit = int(getattr(cfg, "organizer_text_limit", _DEFAULT_TEXT_LIMIT)
                     or _DEFAULT_TEXT_LIMIT)
    max_tokens = int(getattr(cfg, "organizer_max_tokens", _DEFAULT_MAX_TOKENS)
                     or _DEFAULT_MAX_TOKENS)

    if client is None:
        try:
            from src.core.claude import ClaudeClient

            client = ClaudeClient()
        except Exception as exc:  # noqa: BLE001 - organiser is optional
            logger.warning("Organiser unavailable; leaving skeleton as-is: %s", exc)
            return stats
    try:
        system_prompt = client.load_prompt("minutes_organizer")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Organiser prompt missing; leaving skeleton as-is: %s", exc)
        return stats

    by_title = {_norm(t): t for t in titles}
    decisions: list[tuple[dict, str, str, str]] = []   # turn, origin, target, tag

    for start in range(0, len(candidates), batch_size):
        batch = candidates[start:start + batch_size]
        turns = [t for t, _ in batch]
        try:
            raw = client.generate(
                user_prompt=_render_batch(turns, titles, text_limit),
                system_prompt=system_prompt,
                workflow="minutes_organizer",
                max_tokens=max_tokens,
            )
        except Exception as exc:  # noqa: BLE001 - isolate one bad batch
            logger.warning("Organiser batch %d failed: %s", stats["batches"] + 1, exc)
            continue
        stats["batches"] += 1
        parsed = _parse_response(raw, len(turns))
        for i, (turn, origin) in enumerate(batch):
            answer = parsed.get(i)
            if not answer:
                continue
            agenda_raw = answer["agenda"]
            if agenda_raw and _norm(agenda_raw) == _OPENING:
                target = _OPENING
            else:
                target = by_title.get(_norm(agenda_raw), "") if agenda_raw else ""
            decisions.append((turn, origin, target, answer["tag"]))

    stats["considered"] = len(candidates)

    # Apply deterministically: tag in place, then move only real relocations.
    moves: list[dict] = []
    for turn, origin, target, tag in decisions:
        if tag:
            turn["tag"] = tag
            stats["tagged"] += 1
            stats["by_tag"][tag] += 1
        if not target or target == origin:
            continue
        turn["assigned_by"] = "llm"
        moves.append({"turn": turn, "from": origin, "to": target})

    if moves:
        _apply_moves(skeleton, moves)
        stats["moved"] = len(moves)

    logger.info(
        "Organiser: considered %d turn(s) in %d batch(es) - tagged %d (%s), moved %d",
        stats["considered"], stats["batches"], stats["tagged"],
        ", ".join(f"{k}={v}" for k, v in stats["by_tag"].items()), stats["moved"],
    )
    return stats


def _apply_moves(skeleton: dict, moves: list[dict]) -> None:
    """Relocate turns between the opening bucket and agenda items, in place."""
    by_title = {(it.get("title") or ""): it for it in skeleton.get("items") or []}
    unassigned = skeleton.get("unassigned_segments") or []

    for move in moves:
        turn, origin, target = move["turn"], move["from"], move["to"]
        # detach from where the deterministic pass had put it
        if origin == _OPENING:
            if turn in unassigned:
                unassigned.remove(turn)
        else:
            src = by_title.get(origin)
            if src and turn in (src.get("segments") or []):
                src["segments"].remove(turn)
        # attach to the proposed home (unknown target falls back to the bucket)
        if target == _OPENING:
            unassigned.append(turn)
        else:
            dst = by_title.get(target)
            if dst is not None:
                dst.setdefault("segments", []).append(turn)
            else:
                unassigned.append(turn)

    skeleton["unassigned_segments"] = sorted(
        unassigned, key=lambda s: str(s.get("start") or "")
    )
    for item in skeleton.get("items") or []:
        item["segments"] = sorted(
            item.get("segments") or [], key=lambda s: str(s.get("start") or "")
        )


def drafting_turns(segments: list[dict]) -> list[dict]:
    """Turns the drafter should render: substantive or untagged.

    Procedural and off-topic turns stay in the skeleton (and therefore in the
    record) but are left out of the drafted prose.
    """
    return [
        s for s in segments or []
        if (s.get("tag") or SUBSTANTIVE) == SUBSTANTIVE
    ]
