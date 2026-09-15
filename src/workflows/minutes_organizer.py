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
* **Context across batches.** Turns are sent in batches; each batch also sees
  the last few turns of the previous one (read-only, with where they were
  filed), so a batch boundary cannot break the thread of a discussion.
* **Islands get a second look.** A short run filed under one item while the
  turns on both sides went to another is usually a misfiling (ΔΣ05 lost the
  face-to-face cost figures that way) but is sometimes a genuine brief switch.
  Each such island is re-checked with the surrounding turns in view, and only
  moved back if the model says the discussion simply continued.
* **Fail safe.** Any malformed, missing or unrecognised answer leaves the turn
  exactly as the previous pass had it. A failed organiser degrades to
  "no change", never to a corrupted skeleton.
* **Auditable.** Relocations record ``assigned_by="llm"`` (or ``"llm_island"``
  for island corrections) alongside the ``gap_fallback`` / time-window
  provenance already carried by segments.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime

logger = logging.getLogger(__name__)

SUBSTANTIVE, PROCEDURAL, OFF_TOPIC = "substantive", "procedural", "off_topic"
_VALID_TAGS = {SUBSTANTIVE, PROCEDURAL, OFF_TOPIC}
_OPENING = "opening"
_ISLAND_MARK = "llm_island"

# Defaults (overridable via settings.minutes_pipeline).
_DEFAULT_BATCH_TURNS = 40
_DEFAULT_TEXT_LIMIT = 600      # chars of a turn shown to the organiser
_DEFAULT_MAX_TOKENS = 8000
_DEFAULT_CONTEXT_TURNS = 8     # previous turns shown read-only at the top of a batch
_CONTEXT_TEXT_LIMIT = 200      # chars of each context turn
_DEFAULT_ISLAND_MAX_TURNS = 8  # longer runs are treated as a real change of topic
_DEFAULT_ISLAND_GAP_SECONDS = 180.0
_ISLAND_CONTEXT_TURNS = 6      # turns shown before and after an island


def _norm(title: str) -> str:
    return " ".join((title or "").split()).strip().lower()


def _strip_fence(raw: str) -> str:
    text = (raw or "").strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL)
    return fence.group(1).strip() if fence else text


def _parse_response(raw: str, expected: int) -> dict[int, dict]:
    """Parse the organiser's JSON array into ``{turn index: {agenda, tag}}``.

    Tolerates code fences and surrounding prose. Entries that are malformed, out
    of range, or carry an unknown tag are dropped - the caller then leaves those
    turns untouched.
    """
    text = _strip_fence(raw)
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


def _turn_line(turn: dict, limit: int) -> str:
    speaker = (turn.get("speaker") or "").strip()
    text = " ".join((turn.get("text") or "").split())[:limit]
    return f"{speaker}: {text}"


def _render_batch(turns: list[dict], titles: list[str], text_limit: int,
                  context: list[tuple[dict, str]] | None = None) -> str:
    """Build the user prompt for one batch of turns.

    *context* is ``[(turn, filed_under)]`` from the end of the previous batch.
    It is shown read-only so the model can follow the discussion across the
    batch boundary; it carries no indices and is never classified.
    """
    lines = [
        "## Θέματα ημερήσιας διάταξης (χρησιμοποίησε ΑΚΡΙΒΩΣ αυτούς τους τίτλους)",
    ]
    lines.extend(f"- {t}" for t in titles)
    lines.append(f"- {_OPENING}  (για ο,τι δεν ανήκει σε κανένα θέμα)")
    if context:
        lines.append(
            "\n## Προηγούμενες σειρές (ΜΟΝΟ για να δεις πού βρίσκεται η συζήτηση - "
            "ΜΗΝ τις ταξινομήσεις)"
        )
        for turn, filed in context:
            lines.append(f"- {_turn_line(turn, _CONTEXT_TEXT_LIMIT)} -> {filed}")
    lines.append("\n## Σειρές προς ταξινόμηση")
    for i, turn in enumerate(turns):
        lines.append(f"[{i}] {_turn_line(turn, text_limit)}")
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
    "by_tag", "islands_reviewed", "islands_moved"}``. Never raises - on any
    failure the skeleton is left as the previous pass had it.
    """
    stats = {"considered": 0, "tagged": 0, "moved": 0, "batches": 0,
             "by_tag": {SUBSTANTIVE: 0, PROCEDURAL: 0, OFF_TOPIC: 0},
             "islands_reviewed": 0, "islands_moved": 0}

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
    context_turns = int(getattr(cfg, "organizer_context_turns", _DEFAULT_CONTEXT_TURNS)
                        or 0)

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
    context: list[tuple[dict, str]] = []

    for start in range(0, len(candidates), batch_size):
        batch = candidates[start:start + batch_size]
        turns = [t for t, _ in batch]
        try:
            raw = client.generate(
                user_prompt=_render_batch(turns, titles, text_limit, context),
                system_prompt=system_prompt,
                workflow="minutes_organizer",
                max_tokens=max_tokens,
            )
        except Exception as exc:  # noqa: BLE001 - isolate one bad batch
            logger.warning("Organiser batch %d failed: %s", stats["batches"] + 1, exc)
            context = []
            continue
        stats["batches"] += 1
        parsed = _parse_response(raw, len(turns))
        filed: list[tuple[dict, str]] = []
        for i, (turn, origin) in enumerate(batch):
            target = ""
            answer = parsed.get(i)
            if answer:
                agenda_raw = answer["agenda"]
                if agenda_raw and _norm(agenda_raw) == _OPENING:
                    target = _OPENING
                elif agenda_raw:
                    target = by_title.get(_norm(agenda_raw), "")
                decisions.append((turn, origin, target, answer["tag"]))
            filed.append((turn, target or origin))
        context = filed[-context_turns:] if context_turns > 0 else []

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

    if getattr(cfg, "organizer_island_review", True):
        _review_islands(skeleton, _final_placement(candidates, moves), titles,
                        client, cfg, stats)

    logger.info(
        "Organiser: considered %d turn(s) in %d batch(es) - tagged %d (%s), moved %d, "
        "islands reviewed %d / moved back %d",
        stats["considered"], stats["batches"], stats["tagged"],
        ", ".join(f"{k}={v}" for k, v in stats["by_tag"].items()), stats["moved"],
        stats["islands_reviewed"], stats["islands_moved"],
    )
    return stats


# ---------------------------------------------------------------------------
# Island review
# ---------------------------------------------------------------------------

def _parse_ts(value) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _gap_seconds(earlier: dict, later: dict) -> float | None:
    a, b = _parse_ts(earlier.get("start")), _parse_ts(later.get("start"))
    if a is None or b is None:
        return None
    return (b - a).total_seconds()


def _final_placement(candidates: list[tuple[dict, str]],
                     moves: list[dict]) -> list[tuple[dict, str]]:
    """``[(turn, filed_under)]`` for every candidate, in time order."""
    moved = {id(m["turn"]): m["to"] for m in moves}
    placed = [(turn, moved.get(id(turn), origin)) for turn, origin in candidates]
    placed.sort(key=lambda p: str(p[0].get("start") or ""))
    return placed


def _find_islands(placed: list[tuple[dict, str]], *, max_turns: int,
                  max_gap_seconds: float) -> list[dict]:
    """Short runs filed under one item that sit inside another item's stretch.

    *placed* is ``[(turn, filed_under)]`` in time order. An island is a maximal
    run of at most *max_turns* turns, containing at least one model decision,
    whose neighbouring runs on BOTH sides were filed under the same other item,
    with no gap wider than *max_gap_seconds* to either neighbour. Runs filed as
    ``opening`` are never islands (moving chatter into an item gains nothing).
    """
    runs: list[list] = []   # [title, first_index, last_index]
    for i, (_turn, title) in enumerate(placed):
        if runs and runs[-1][0] == title:
            runs[-1][2] = i
        else:
            runs.append([title, i, i])

    islands: list[dict] = []
    for k in range(1, len(runs) - 1):
        title, first, last = runs[k]
        left, right = runs[k - 1], runs[k + 1]
        surround = left[0]
        if title == _OPENING or right[0] != surround or surround == title:
            continue
        if last - first + 1 > max_turns:
            continue
        if not any(placed[i][0].get("assigned_by") == "llm" for i in range(first, last + 1)):
            continue
        gap_before = _gap_seconds(placed[left[2]][0], placed[first][0])
        gap_after = _gap_seconds(placed[last][0], placed[right[1]][0])
        if gap_before is None or gap_after is None:
            continue
        if gap_before > max_gap_seconds or gap_after > max_gap_seconds:
            continue
        islands.append({
            "start": first, "end": last, "title": title, "surround": surround,
            "left": (left[1], left[2]), "right": (right[1], right[2]),
        })
    return islands


def _render_island(current: str, surround: str, before: list[dict],
                   island: list[dict], after: list[dict]) -> str:
    def block(turns):
        return "\n".join(_turn_line(t, _DEFAULT_TEXT_LIMIT) for t in turns)

    return "\n".join([
        "## Υποψήφια θέματα",
        f"- {surround}  (το θέμα των σειρών πριν και μετά)",
        f"- {current}  (το θέμα όπου έχει ταξινομηθεί τώρα η νησίδα)",
        "\n## Πριν",
        block(before),
        "\n## Νησίδα (για αυτές τις σειρές αποφασίζεις)",
        block(island),
        "\n## Μετά",
        block(after),
        '\nΕπέστρεψε ΜΟΝΟ: {"agenda": "<ακριβής τίτλος από τα δύο παραπάνω>"}',
    ])


def _parse_island_choice(raw: str) -> str:
    text = _strip_fence(raw)
    for candidate in (text, (re.search(r"\{.*\}", text, re.DOTALL) or [None])[0]):
        if not candidate:
            continue
        try:
            data = json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(data, dict):
            return str(data.get("agenda") or "").strip()
    return ""


def _review_islands(skeleton: dict, placed: list[tuple[dict, str]], titles: list[str],
                    client, cfg, stats: dict) -> None:
    """Ask the model, with surrounding context, whether each island was misfiled."""
    max_turns = int(getattr(cfg, "organizer_island_max_turns", _DEFAULT_ISLAND_MAX_TURNS)
                    or _DEFAULT_ISLAND_MAX_TURNS)
    max_gap = float(getattr(cfg, "organizer_island_gap_seconds", _DEFAULT_ISLAND_GAP_SECONDS)
                    or _DEFAULT_ISLAND_GAP_SECONDS)
    islands = _find_islands(placed, max_turns=max_turns, max_gap_seconds=max_gap)
    if not islands:
        return
    try:
        system_prompt = client.load_prompt("minutes_organizer_island")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Island-review prompt missing; islands left as filed: %s", exc)
        return

    moves: list[dict] = []
    for isl in islands:
        stats["islands_reviewed"] += 1
        left_first, left_last = isl["left"]
        right_first, right_last = isl["right"]
        before = [placed[i][0] for i in range(max(left_first, left_last - _ISLAND_CONTEXT_TURNS + 1),
                                                left_last + 1)]
        island = [placed[i][0] for i in range(isl["start"], isl["end"] + 1)]
        after = [placed[i][0] for i in range(right_first,
                                               min(right_last, right_first + _ISLAND_CONTEXT_TURNS - 1) + 1)]
        try:
            raw = client.generate(
                user_prompt=_render_island(isl["title"], isl["surround"], before, island, after),
                system_prompt=system_prompt,
                workflow="minutes_organizer_island",
                max_tokens=500,
            )
        except Exception as exc:  # noqa: BLE001 - one failed check changes nothing
            logger.warning("Island review failed (%s inside %s): %s",
                           isl["title"], isl["surround"], exc)
            continue
        if _norm(_parse_island_choice(raw)) != _norm(isl["surround"]):
            continue   # confirmed as filed, unrecognised, or invented: leave it
        for turn in island:
            turn["assigned_by"] = _ISLAND_MARK
            moves.append({"turn": turn, "from": isl["title"], "to": isl["surround"]})
        stats["islands_moved"] += 1

    if moves:
        _apply_moves(skeleton, moves)
        stats["moved"] += len(moves)


def _apply_moves(skeleton: dict, moves: list[dict]) -> None:
    """Relocate turns between the opening bucket and agenda items, in place."""
    by_title = {(it.get("title") or ""): it for it in skeleton.get("items") or []}
    unassigned = skeleton.get("unassigned_segments") or []

    for move in moves:
        turn, origin, target = move["turn"], move["from"], move["to"]
        # detach from where the previous pass had put it
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
