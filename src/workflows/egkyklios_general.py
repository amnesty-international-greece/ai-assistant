"""Γενική Εγκύκλιος Ενημέρωσης workflow.

Step order (10 steps):
  1.  gather_sources          - resolve period, validate sources exist, idempotency guard
  2.  extract_briefing_texts  - read each briefing PDF via extract_pdf_text()
  3.  extract_meeting_summaries - pull minutes text from workflow_state rows
  4.  draft_circular          - LLM call → Markdown saved to disk + DB row created
  5.  render_pdf              - Markdown → branded ReportLab PDF
  6.  notify_board_for_review - M365 email to board + director with PDF attachment
  7.  await_approval          - halt until SecGen approves (requires_approval=True)
  8.  archive_to_sharepoint   - upload PDF + append protocol row
  9.  send_brevo_campaign     - create & send Brevo campaign to members
  10. publish_event           - emit EVENT_EGKYKLIOS_PUBLISHED on event bus
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from src.config import settings
from src.domain.locale_el import (
    MONTHS_GENITIVE,
    MONTHS_NOMINATIVE_UPPER,
    format_date,
    month_accusative,
)
from src.core.protocol import allocate_protocol_number, commit_protocol_reservation
from src.core.audit import (
    create_egkyklios_draft,
    get_egkyklios_draft,
    list_egkyklios_drafts,
    list_director_briefings_in_window,
    list_completed_minutes_in_window,
    update_egkyklios_draft,
    log_action,
)
from src.core.claude import ClaudeClient
from src.core.workflow import BaseWorkflow, WorkflowStep, StepResult
from src.integrations.onedrive import OneDriveClient
from src.integrations.brevo import BrevoClient
from src.utils.pdf_text import extract_pdf_text
from src.profile import section

logger = logging.getLogger(__name__)

_BOARD_EMAIL = settings.roles.board
_DIRECTOR_EMAIL = settings.roles.director

_GREEK_MONTHS_TITLE = MONTHS_NOMINATIVE_UPPER
_GREEK_MONTHS_GEN = MONTHS_GENITIVE


def _period_title(period_start: str, period_end: str) -> str:
    """Build the period title in Greek uppercase, e.g. 'ΙΑΝΟΥΑΡΙΟΣ - ΜΑΡΤΙΟΣ 2026'."""
    try:
        ds = date.fromisoformat(period_start)
        de = date.fromisoformat(period_end)
        m_start = _GREEK_MONTHS_TITLE[ds.month]
        m_end = _GREEK_MONTHS_TITLE[de.month]
        year = de.year
        if ds.month == de.month:
            return f"{m_start} {year}"
        return f"{m_start} - {m_end} {year}"
    except Exception:
        return f"{period_start} - {period_end}"


def _default_quarter(test_mode: bool = False) -> tuple[str, str]:
    """Return (period_start, period_end) ISO strings.

    test_mode=True → last 7 days (easy to populate in dev).
    live           → last full calendar quarter.
    """
    today = date.today()
    if test_mode:
        return (today - timedelta(days=7)).isoformat(), today.isoformat()

    # Last full quarter
    q = (today.month - 1) // 3  # 0-based quarter of current quarter
    if q == 0:
        # We're in Q1, so last quarter is Q4 of previous year
        start = date(today.year - 1, 10, 1)
        end = date(today.year - 1, 12, 31)
    else:
        start_month = (q - 1) * 3 + 1
        end_month = q * 3
        end_day = [0, 31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][end_month]
        # Feb leap year
        if end_month == 2 and today.year % 4 == 0 and (today.year % 100 != 0 or today.year % 400 == 0):
            end_day = 29
        start = date(today.year, start_month, 1)
        end = date(today.year, end_month, end_day)
    return start.isoformat(), end.isoformat()



def _bundle_briefings(briefing_texts: list[dict]) -> str:
    """All briefing text, in order, with its provenance kept visible."""
    out = ""
    for bt in briefing_texts:
        scan = " [ΣΚΑΝΑΡΙΣΜΕΝΟ]" if bt.get("is_scan") else ""
        out += (f"\n--- {bt['kind']} / Συνεδρίαση {bt['meeting_ref']} "
                f"({bt.get('archived_at', '')[:10]}){scan} ---\n{bt['text']}\n")
    return out


def _period_words(period_start: str, period_end: str) -> tuple[str, str, str]:
    """The months as the opening sentence needs them: "από τον Απρίλιο ..."."""
    try:
        ds, de = date.fromisoformat(period_start), date.fromisoformat(period_end)
        return month_accusative(ds.month), month_accusative(de.month), str(de.year)
    except Exception:
        return period_start, period_end, ""


def _meeting_heading(index: int, meeting_date: str, month_shared: bool = False) -> str:
    """"Τακτική Συνεδρίαση Ιουνίου"; with the day when two meetings share a month."""
    try:
        d = date.fromisoformat(meeting_date)
        when = _GREEK_MONTHS_GEN[d.month]
        if month_shared:
            when = f"{d.day}ης {when}"
        return f"### **1.{index}. Τακτική Συνεδρίαση {when}**"
    except Exception:
        return f"### **1.{index}. Τακτική Συνεδρίαση**"


def _previous_sections() -> str:
    """The last edition's Office sections, as a naming preference."""
    try:
        path = section.asset_path("style_reference") / "egkyklios_previous_sections.txt"
        lines = [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines()]
        return "\n".join(f"- {ln}" for ln in lines if ln and not ln.startswith("#"))
    except Exception as exc:
        logger.warning("No previous-section reference (%s)", exc)
        return "- (δεν υπάρχει προηγούμενη εγκύκλιος)"


def _parse_json(raw: str) -> dict:
    """The first JSON object in a model reply, fences or not."""
    text = (raw or "").strip()
    first, last = text.find("{"), text.rfind("}")
    if first < 0 or last <= first:
        raise ValueError("no JSON object in the reply")
    return json.loads(text[first:last + 1])


def _in_period(item: dict, period_start: str, period_end: str) -> bool:
    """Undated items stay; dated ones must fall inside the edition's window."""
    start = (item.get("date") or "").strip()
    end = (item.get("date_end") or "").strip() or start
    if not start:
        return True
    return end >= period_start and start <= period_end


def _extract_items(client, prompt: str, briefing_texts: list[dict],
                   period_start: str, period_end: str, workflow: str
                   ) -> tuple[list[dict], list[str], list[str]]:
    """Every briefing, broken into items; personal and out-of-period ones dropped.

    Returns (items, staff_names, failures). Items carry an id ("I01"...) so the
    plan can assign them and each section writer sees only its own.
    """
    items: list[dict] = []
    staff: set[str] = set()
    failures: list[str] = []
    seen: set[tuple[str, str]] = set()
    for bt in briefing_texts:
        label = f"{bt.get('kind', '')} {bt.get('meeting_ref', '')}".strip()
        data = None
        for attempt in (1, 2):
            # one malformed reply from a free model should not sink the run;
            # two in a row is a real problem and stops it
            try:
                # the provenance line carries the date the year is inferred
                # from: briefings write "13/06" and leave the year to the reader
                raw = client.generate(user_prompt=_bundle_briefings([bt]), system_prompt=prompt,
                                      workflow=workflow, max_tokens=24000)
                data = _parse_json(raw)
                break
            except Exception as exc:
                logger.warning("Item extraction from %s failed (attempt %d): %s",
                               label, attempt, exc)
                if attempt == 2:
                    failures.append(f"εξαγωγή θεμάτων από {label}: {str(exc)[:100]}")
        if data is None:
            continue
        staff.update(n.strip() for n in data.get("staff_names") or [] if n and n.strip())
        for item in data.get("items") or []:
            if item.get("personal"):
                continue
            if not _in_period(item, period_start, period_end):
                continue
            key = ((item.get("date") or "").strip(),
                   re.sub(r"\W+", "", (item.get("title") or "").lower())[:24])
            if key in seen:            # the same event in two briefings
                continue
            seen.add(key)
            item["source"] = label
            items.append(item)
    items.sort(key=lambda it: (it.get("date") or "9999", it.get("title") or ""))
    for index, item in enumerate(items, 1):
        item["id"] = f"I{index:02d}"
    return items, sorted(staff), failures


def _item_line(item: dict) -> str:
    when = item.get("date") or "χωρίς ημερομηνία"
    if item.get("date_end"):
        when += f" έως {item['date_end']}"
    return f"{item['id']} | {when} | {item.get('title', '')} | ενότητα εισηγητικού: {item.get('heading', '')}"


def _canonical_title(title: str, previous_titles: list[str]) -> str:
    """The previous edition's exact wording when the model meant the same section.

    The model inflects titles freely ("Διεθνή Γραμματεία" for "Διεθνής
    Γραμματεία"); members should see the same heading from edition to edition.
    """
    def stems(text: str) -> list[str]:
        bare = unicodedata.normalize("NFD", text.lower())
        bare = "".join(c for c in bare if not unicodedata.combining(c))
        return [w[:6] for w in re.findall(r"\w+", bare)]

    for prev in previous_titles:
        if stems(prev) == stems(title):
            return prev
    return title


def _plan_office_sections(client, plan_prompt: str, items: list[dict], previous: str,
                          period_start: str, period_end: str, workflow: str) -> list[dict]:
    """Assign items to sections; fall back to the Director's own headings."""
    prompt = (plan_prompt.replace("{period_start}", period_start)
              .replace("{period_end}", period_end)
              .replace("{previous_sections}", previous))
    known = {it["id"] for it in items}
    previous_titles = [ln[2:].split("|")[0].strip()
                       for ln in previous.splitlines() if ln.startswith("- ")]
    try:
        raw = client.generate(user_prompt="\n".join(_item_line(it) for it in items),
                              system_prompt=prompt, workflow=workflow, max_tokens=3000)
        sections = []
        taken: set[str] = set()
        for sec in _parse_json(raw).get("sections") or []:
            # one event, one entry: the same item in two sections reads to a
            # member as two events
            ids = [i for i in sec.get("items") or [] if i in known and i not in taken]
            taken.update(ids)
            title = _canonical_title((sec.get("title") or "").split("|")[0].strip(),
                                     previous_titles)
            if title and ids:
                sections.append({"title": title, "items": ids})
        placed = {i for sec in sections for i in sec["items"]}
        missing = [it["id"] for it in items if it["id"] not in placed]
        if missing and sections:
            # nothing silently disappears: an unplaced item goes to the section
            # sharing a word with the Director's heading for it, else the last
            by_id = {it["id"]: it for it in items}
            for item_id in missing:
                words = {w[:5] for w in re.findall(r"\w{5,}", by_id[item_id].get("heading", "").lower())}
                home = next((sec for sec in sections
                             if words & {w[:5] for w in re.findall(r"\w{5,}", sec["title"].lower())}),
                            sections[-1])
                home["items"].append(item_id)
                logger.warning("Plan left %s unplaced; placed in %r", item_id, home["title"])
        if sections:
            return sections
    except Exception as exc:
        logger.warning("Section planning failed (%s); grouping by the Director's headings", exc)
    grouped: dict[str, list[str]] = {}
    for it in items:
        grouped.setdefault(it.get("heading") or "Λοιπά", []).append(it["id"])
    return [{"title": title, "items": ids} for title, ids in grouped.items()]


_MONEY = re.compile(r"\d[\d.,]*\s*(?:€|ευρώ|EUR)", re.I)
_PAY = ("μισθ", "αύξηση", "αυξήσ", "αποδοχ", "αμοιβ", "καθαρά")
# what makes a mention of an employee a personal matter rather than a byline
_HR = _PAY + ("πρόσληψ", "προσλήφθ", "προσλαμβ", "αποχώρ", "παραίτ", "απόλυσ",
              "αξιολόγ", "υποψήφι", "βραχεία λίστα", "καθήκοντα", "καθηκόντων",
              "άδεια", "ασθέν")


def _prices_pay(sentence: str) -> bool:
    """A sum of money with a word about pay right next to it ("αύξηση κατά 100€").

    Proximity, not co-occurrence: "the reserve rose to 15.000 ευρώ, and inflation
    will inform the salary review" is the organisation's finances, not anyone's pay.
    """
    low = sentence.lower()
    for m in _MONEY.finditer(sentence):
        window = low[max(0, m.start() - 40):m.end() + 40]
        if any(p in window for p in _PAY):
            return True
    return False


def _redact(markdown: str, staff_names: list[str]) -> tuple[str, list[str], list[str]]:
    """Remove what puts an employee's personal matters in front of the members.

    A second line of defence behind the prompts and the extraction step, which
    dropped personal items already: a model that ignores an instruction once is
    enough to put a colleague's salary in front of every member.

    * a paragraph that names an employee *and* concerns hiring, leaving, pay or
      evaluation goes;
    * a sentence that puts a figure on pay goes;
    * any other mention of a name is kept and listed for the reviewer, since
      the list of names comes from the model and a run once counted the
      journalists hosting an interview among the staff.

    Returns (markdown, removed, to_review).
    """
    stems = set()
    for name in staff_names:
        for part in name.split():
            if len(part) >= 5:
                stems.add(part[:-1].lower())      # tolerate case endings
    named = (re.compile(r"(?<!\w)(?:" + "|".join(map(re.escape, sorted(stems))) + ")")
             if stems else None)
    removed: list[str] = []
    review: list[str] = []
    out_lines = []
    for line in markdown.split("\n"):
        if line.startswith("#") or not line.strip():
            out_lines.append(line)
            continue
        low = line.lower()
        names_someone = bool(named and named.search(low))
        if names_someone and any(k in low for k in _HR):
            removed.append(line.strip())
            continue
        kept = []
        for sentence in re.split(r"(?<=[.;·!])\s+", line):
            if _prices_pay(sentence):
                removed.append(sentence.strip())
            else:
                kept.append(sentence)
        if kept:
            out_lines.append(" ".join(kept))
            if names_someone:
                review.append(" ".join(kept).strip())

    # an entry whose whole body went must not stay behind as a bare heading
    lines = out_lines
    out_lines = []
    for i, line in enumerate(lines):
        if line.startswith("### "):
            body = []
            for nxt in lines[i + 1:]:
                if nxt.startswith("#"):
                    break
                body.append(nxt)
            if not "".join(body).strip():
                removed.append(f"(κενή εγγραφή) {line.lstrip('# ').strip()}")
                continue
        out_lines.append(line)
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(out_lines))
    return text, removed, review


def _tidy(markdown: str) -> str:
    """The house conventions the model keeps half-following, made exact.

    Only a plain hyphen, and every dated entry as ``### **\\[13 - 14 Ιουνίου
    2026\\] Title**`` - the form the circular has always used.
    """
    text = markdown.replace("–", "-").replace("—", "-")

    def date_part(raw: str) -> str:
        raw = re.sub(r"\s+έως\s+", " - ", raw.strip())
        raw = re.sub(r"\s*-\s*", " - ", raw)
        m = re.fullmatch(r"(\d{1,2}) (\S+) (\d{4}) - (\d{1,2}) (\S+) (\d{4})", raw)
        if m and m.group(3) == m.group(6):
            if m.group(2) == m.group(5):
                return f"{m.group(1)} - {m.group(4)} {m.group(2)} {m.group(3)}"
            return f"{m.group(1)} {m.group(2)} - {m.group(4)} {m.group(5)} {m.group(3)}"
        return raw

    def heading(m: re.Match) -> str:
        inner = m.group(1).replace("*", "").replace("\\", "").strip()
        d = re.match(r"\[([^\]]+)\]\s*(.*)", inner)
        if not d:
            return m.group(0)
        return f"### **\\[{date_part(d.group(1))}\\] {d.group(2).strip()}**"

    text = re.sub(r"^###\s+(.+)$", heading, text, flags=re.M)
    return re.sub(r"^(###[^\n]*)\n(?=[^\n#])", r"\1\n\n", text, flags=re.M)


def _assemble(title: str, month_start: str, month_end: str, year: str,
              part_a: list[str], part_b: list[str]) -> str:
    """Put the drafted pieces into the section's frame.

    The frame is optional and deliberately thin: the wording a section is
    obliged to repeat in every edition (here, the paragraph the Internal
    Regulations prescribe) and the names of the two parts. Everything else -
    how many meetings, which Office sections, in what order - comes out of the
    sources. A section that drops no frame in gets the structure only, so
    adopting the workflow does not start with filling in a template.
    """
    frame = ""
    try:
        frame = (section.asset_path("templates") / "egkyklios.md").read_text(encoding="utf-8")
    except Exception as exc:
        logger.info("No circular frame for this section (%s); using the bare structure", exc)

    # Comments in the frame are notes for whoever edits it, not for members.
    frame = re.sub(r"<!--.*?-->", "", frame, flags=re.S).lstrip()

    if not frame.strip():
        frame = (
            "# {{TITLE_HEADING}}\n\n## {{TITLE}}\n\n"
            "# Α. ΔΙΟΙΚΗΤΙΚΟ ΣΥΜΒΟΥΛΙΟ\n\n## 1\\. Συνεδριάσεις\n\n{{PART_A}}\n\n"
            "# Β. ΓΡΑΦΕΙΟ\n\n{{PART_B}}\n"
        )

    filled = (frame
              .replace("{{TITLE_HEADING}}", "ΓΕΝΙΚΗ ΕΓΚΥΚΛΙΟΣ ΕΝΗΜΕΡΩΣΗΣ")
              .replace("{{TITLE}}", title)
              .replace("{{MONTH_START}}", month_start)
              .replace("{{MONTH_END}}", month_end)
              .replace("{{YEAR}}", year)
              .replace("{{PART_A}}", "\n".join(part_a).strip())
              .replace("{{PART_A_EXTRA}}", "")
              .replace("{{PART_B}}", "\n".join(part_b).strip()))
    while "\n\n\n" in filled:
        filled = filled.replace("\n\n\n", "\n\n")
    return filled.rstrip() + "\n"

class EgkykliosGeneralWorkflow(BaseWorkflow):
    """Γενική Εγκύκλιος Ενημέρωσης - full 10-step workflow."""

    def __init__(self, actor: str = "secgen") -> None:
        self._onedrive: OneDriveClient | None = None
        self._brevo: BrevoClient | None = None
        super().__init__(actor=actor)

    @property
    def onedrive(self) -> OneDriveClient:
        if self._onedrive is None:
            self._onedrive = OneDriveClient()
        return self._onedrive

    @property
    def brevo(self) -> BrevoClient:
        if self._brevo is None:
            self._brevo = BrevoClient()
        return self._brevo

    @property
    def name(self) -> str:
        return "egkyklios_general"

    def define_steps(self) -> list[WorkflowStep]:
        return [
            WorkflowStep("gather_sources", "Resolve period and validate source content exists"),
            WorkflowStep("extract_briefing_texts", "Extract text from Director briefing PDFs"),
            WorkflowStep("extract_meeting_summaries", "Extract summaries from board minutes workflow state"),
            WorkflowStep("draft_circular", "Draft Γενική Εγκύκλιος via LLM (Claude)"),
            WorkflowStep("render_pdf", "Render Markdown draft to branded PDF"),
            WorkflowStep("notify_board_for_review", "Email draft PDF to board and Director for review"),
            WorkflowStep("await_approval", "Halt until SecGen approves the draft", requires_approval=True),
            WorkflowStep("archive_to_sharepoint", "Upload PDF to SharePoint and register protocol number"),
            WorkflowStep("send_brevo_campaign", "Send Γενική Εγκύκλιος to members via Brevo"),
            WorkflowStep("publish_event", "Publish EgkykliosPublished event to event bus"),
        ]

    @staticmethod
    def debug_fixture() -> dict[str, Any]:
        """Canonical fake ctx for `debug run egkyklios_general <step>`.

        Provides every key any ``_step_*`` reads so a step can run in isolation
        without a KeyError.  The debug runner forces ``test_mode=True`` (skips
        SharePoint upload; Brevo stays draft/test); it is intentionally NOT set
        here.  Note: ``gather_sources`` performs a live DB idempotency check and
        may fail if a non-cancelled draft already overlaps this period - pass
        ``--set period_start=...`` to move the window if needed.
        """
        return {
            # gather_sources
            "period_start": "2099-01-01",                 # gather_sources / draft / render / archive
            "period_end": "2099-03-31",                   # gather_sources / draft / render / archive
            "title": "ΙΑΝΟΥΑΡΙΟΣ - ΜΑΡΤΙΟΣ 2099",          # most steps
            # gather_sources outputs → consumed by extract_briefing_texts / extract_meeting_summaries
            "briefings_meta": [],                         # extract_briefing_texts (empty → no PDFs to read)
            "minutes_rows": [],                           # extract_meeting_summaries (empty → no summaries)
            # extract_* outputs → consumed by draft_circular
            "briefing_texts": [
                {
                    "meeting_ref": "ΔΣ99-2099",
                    "kind": "ΕΝΗΜΕΡΩΤΙΚΟ",
                    "archived_at": "2099-02-01T00:00:00",
                    "text": "Δοκιμαστικό κείμενο εισηγητικού.",
                    "is_scan": False,
                },
            ],
            "meeting_summaries": [
                {
                    "workflow_id": "debug123",
                    "meeting_ref": "ΔΣ99-2099",
                    "meeting_date": "2099-02-15",
                    "text": "Δοκιμαστική περίληψη πρακτικών.",
                },
            ],
            # draft_circular outputs → consumed by render_pdf / later steps
            "draft_markdown": "# Δοκιμαστική Εγκύκλιος\n\nΔοκιμαστικό περιεχόμενο.",
            "draft_md_path": "data/debug/egkyklios_draft.md",  # render_pdf reload fallback
            "egkyklios_draft_id": 0,                      # render/notify/archive/brevo DB-row id (0 → no DB update)
            # render_pdf output → consumed by notify / archive / brevo
            "draft_pdf_path": "data/debug/egkyklios_draft.pdf",
            # archive_to_sharepoint outputs → consumed by brevo / publish_event
            "sharepoint_url": "https://example.invalid/share/debug",
            "protocol_number": "2099_999",
            # send_brevo_campaign
            "brevo_template_id": 0,                       # send_brevo_campaign (0 → step skips gracefully)
            "brevo_list_ids": [],                         # send_brevo_campaign
        }

    async def execute_step(self, step: WorkflowStep, context: dict[str, Any]) -> StepResult:
        handler = getattr(self, f"_step_{step.name}", None)
        if not handler:
            return StepResult(success=False, message=f"Δεν βρέθηκε handler για βήμα: {step.name}")
        return await handler(context)

    async def rollback(self, ctx: dict[str, Any]) -> None:
        """Undo side-effects on failure/cancellation."""
        # Delete local draft files
        for key in ("draft_md_path", "draft_pdf_path"):
            p_str = ctx.get(key)
            if p_str:
                p = Path(p_str)
                if p.exists():
                    try:
                        p.unlink()
                        logger.info("Rollback: deleted %s", p)
                    except Exception as e:
                        logger.warning("Rollback: could not delete %s: %s", p, e)

        # Mark DB row as cancelled
        draft_id = ctx.get("egkyklios_draft_id")
        if draft_id:
            try:
                update_egkyklios_draft(draft_id, status="cancelled")
            except Exception as e:
                logger.warning("Rollback: could not cancel egkyklios draft %s: %s", draft_id, e)

    # ─────────────────────────────────────────────────────────────────────────
    # Step 1: gather_sources
    # ─────────────────────────────────────────────────────────────────────────

    async def _step_gather_sources(self, ctx: dict[str, Any]) -> StepResult:
        """Resolve the reporting window and validate at least 1 source exists.

        Idempotency guard: aborts if a non-cancelled draft for the same period
        already exists in egkyklios_drafts.
        """
        test_mode = bool(ctx.get("test_mode"))

        # Allow explicit overrides from CLI; fall back to quarter/test defaults
        period_start = ctx.get("period_start") or ""
        period_end = ctx.get("period_end") or ""
        if not period_start or not period_end:
            period_start, period_end = _default_quarter(test_mode)

        title = _period_title(period_start, period_end)

        # ── Idempotency guard ─────────────────────────────────────────────────
        existing = list_egkyklios_drafts(kind="general", limit=50)
        for row in existing:
            if row["status"] == "cancelled":
                continue
            # Check for overlapping period
            if row["period_start"] <= period_end and row["period_end"] >= period_start:
                return StepResult(
                    success=False,
                    message=(
                        f"Υπάρχει ήδη εγκύκλιος για την περίοδο {row['period_start']} - {row['period_end']} "
                        f"(id={row['id']}, status={row['status']}). "
                        "Ακυρώστε τη προηγούμενη πριν δημιουργήσετε νέα."
                    ),
                )

        # ── Validate sources ──────────────────────────────────────────────────
        briefings = list_director_briefings_in_window(period_start, period_end)
        minutes_rows = list_completed_minutes_in_window(period_start, period_end)

        if not briefings and not minutes_rows:
            return StepResult(
                success=False,
                message=(
                    f"Δεν βρέθηκαν πηγές για την περίοδο {period_start} - {period_end}. "
                    "Απαιτείται τουλάχιστον ένα εισηγητικό/ενημερωτικό Διευθυντή ή "
                    "ένα σύνολο πρακτικών συνεδρίασης."
                ),
            )

        logger.info(
            "[%s] gather_sources: %d briefing(s), %d minutes row(s) in %s - %s",
            self.workflow_id, len(briefings), len(minutes_rows), period_start, period_end,
        )

        return StepResult(
            success=True,
            data={
                "period_start": period_start,
                "period_end": period_end,
                "title": title,
                "briefings_meta": briefings,
                "minutes_rows": minutes_rows,
            },
            message=(
                f"Πηγές για {title}: {len(briefings)} εισηγητικά, "
                f"{len(minutes_rows)} πρακτικά συνεδριάσεων"
            ),
        )

    # ─────────────────────────────────────────────────────────────────────────
    # Step 2: extract_briefing_texts
    # ─────────────────────────────────────────────────────────────────────────

    async def _step_extract_briefing_texts(self, ctx: dict[str, Any]) -> StepResult:
        briefings_meta: list[dict] = ctx.get("briefings_meta", [])
        extracted: list[dict[str, Any]] = []

        for b in briefings_meta:
            local_path = b.get("local_path", "")
            if not local_path:
                logger.warning("Briefing id=%s has no local_path, skipping", b.get("id"))
                continue
            p = Path(local_path)
            if not p.exists():
                logger.warning("Briefing PDF not found at %s, skipping", p)
                continue
            try:
                text, meta = extract_pdf_text(p, max_chars=60000)
                extracted.append({
                    "meeting_ref": b.get("meeting_ref", ""),
                    "kind": b.get("kind", ""),
                    "archived_at": b.get("archived_at", ""),
                    "text": text,
                    "is_scan": meta.get("is_scan", False),
                })
            except Exception as e:
                logger.warning("Could not extract text from %s: %s", p, e)
                extracted.append({
                    "meeting_ref": b.get("meeting_ref", ""),
                    "kind": b.get("kind", ""),
                    "archived_at": b.get("archived_at", ""),
                    "text": f"[Αποτυχία εξαγωγής κειμένου: {e}]",
                    "is_scan": True,
                })

        if not extracted and briefings_meta:
            return StepResult(
                success=False,
                message="Δεν κατέστη δυνατή η εξαγωγή κειμένου από κανένα εισηγητικό PDF.",
            )

        return StepResult(
            success=True,
            data={"briefing_texts": extracted},
            message=f"Εξαχθηκε κείμενο από {len(extracted)} εισηγητικά",
        )

    # ─────────────────────────────────────────────────────────────────────────
    # Step 3: extract_meeting_summaries
    # ─────────────────────────────────────────────────────────────────────────

    async def _step_extract_meeting_summaries(self, ctx: dict[str, Any]) -> StepResult:
        minutes_rows: list[dict] = ctx.get("minutes_rows", [])
        summaries: list[dict[str, Any]] = []

        for row in minutes_rows:
            raw_data = row.get("data") or "{}"
            try:
                data = json.loads(raw_data) if isinstance(raw_data, str) else raw_data
            except json.JSONDecodeError:
                data = {}

            row_ctx = data.get("context", {})
            workflow_id = row.get("workflow_id", "")

            # Prefer the richest text available: minutes_markdown > draft_json > agenda_summary
            text = ""
            draft_json = row_ctx.get("draft_json", {})
            if draft_json and isinstance(draft_json, dict):
                # Build plain text from the minutes draft JSON structure
                parts: list[str] = []
                meeting_ref_str = row_ctx.get("meeting_ref", workflow_id)
                parts.append(f"Συνεδρίαση {meeting_ref_str}")
                for section in draft_json.get("sections", []):
                    heading = section.get("heading", "")
                    body = section.get("body", "")
                    if heading:
                        parts.append(f"\n{heading}")
                    if body:
                        parts.append(body)
                decisions = draft_json.get("decisions", [])
                if decisions:
                    parts.append("\nΑΠΟΦΑΣΕΙΣ:")
                    for d in decisions:
                        parts.append(f"- {d.get('text', '')}")
                text = "\n".join(parts)
            elif row_ctx.get("agenda_summary"):
                text = str(row_ctx["agenda_summary"])
            elif row_ctx.get("secgen_notes"):
                text = str(row_ctx["secgen_notes"])[:3000]

            if not text:
                text = f"[Πρακτικά συνεδρίασης {workflow_id} - δεν βρέθηκε κείμενο στο context]"

            summaries.append({
                "workflow_id": workflow_id,
                "meeting_ref": row_ctx.get("meeting_ref", ""),
                "meeting_date": row_ctx.get("meeting_date", ""),
                "text": text,
            })

        return StepResult(
            success=True,
            data={"meeting_summaries": summaries},
            message=f"Εξαχθηκαν περιλήψεις από {len(summaries)} πρακτικά συνεδριάσεων",
        )

    # ─────────────────────────────────────────────────────────────────────────
    # Step 4: draft_circular
    # ─────────────────────────────────────────────────────────────────────────

    async def _step_draft_circular(self, ctx: dict[str, Any]) -> StepResult:
        """Draft the circular one section at a time.

        A single call for a whole six-month edition produces a summary of a
        summary: the model has one output budget for forty events. So the work
        is split the way a person would split it: each meeting is written on
        its own; the briefings are broken into items (dropping what falls
        outside the period or concerns one employee), the items are assigned
        to sections, and each section is written from its own items only, so
        an event appears once. A deterministic guard then removes any sentence
        that still names an employee or prices someone's pay.
        """
        period_start: str = ctx.get("period_start", "")
        period_end: str = ctx.get("period_end", "")
        title: str = ctx.get("title", _period_title(period_start, period_end))
        briefing_texts: list[dict] = ctx.get("briefing_texts", [])
        meeting_summaries: list[dict] = ctx.get("meeting_summaries", [])

        client = ClaudeClient(model=settings.llm.drafting_model or None)
        try:
            plan_prompt = client.load_prompt("egkyklios_plan")
            board_prompt = client.load_prompt("egkyklios_board")
            office_prompt = client.load_prompt("egkyklios_office")
        except FileNotFoundError as e:
            return StepResult(success=False, message=f"Δεν βρέθηκε prompt: {e}")

        month_start, month_end, year = _period_words(period_start, period_end)

        # Any piece that fails stops the step: a circular with holes in it must
        # not reach the review gate looking like a draft. (One run with an
        # unavailable model produced eleven failure notices, emailed them for
        # review and parked them for approval.)
        failures: list[str] = []

        # ── Part A: one call per meeting ──────────────────────────────────────
        part_a: list[str] = []
        months = [(s.get("meeting_date") or "")[:7] for s in meeting_summaries]
        for index, summary in enumerate(meeting_summaries, 1):
            meeting_ref = summary.get("meeting_ref") or summary.get("workflow_id", "")
            meeting_date = summary.get("meeting_date", "")
            heading = _meeting_heading(index, meeting_date,
                                       month_shared=months.count(meeting_date[:7]) > 1)
            prompt = (
                board_prompt
                .replace("{meeting_ref}", meeting_ref)
                .replace("{meeting_date_greek}", format_date(meeting_date))
            )
            try:
                body = client.generate(
                    user_prompt=f"## Πρακτικά\n\n{summary.get('text', '')}",
                    system_prompt=prompt,
                    workflow=self.name,
                    max_tokens=4000,
                )
            except Exception as e:
                logger.warning("Draft failed for %s: %s", meeting_ref, e)
                failures.append(f"{meeting_ref}: {str(e)[:120]}")
                continue
            part_a.append(f"{heading}\n\n{body.strip()}\n")
            logger.info("[%s] drafted %s (%d chars)", self.workflow_id, meeting_ref, len(body))

        # ── Part B: items first, then the plan, then one writer per section ──
        try:
            extract_prompt = client.load_prompt("egkyklios_extract")
        except FileNotFoundError as e:
            return StepResult(success=False, message=f"Δεν βρέθηκε prompt: {e}")
        items, staff_names, extract_failures = _extract_items(
            client, extract_prompt, briefing_texts, period_start, period_end, self.name
        )
        failures.extend(extract_failures)
        by_id = {it["id"]: it for it in items}
        previous = _previous_sections()
        plan = _plan_office_sections(client, plan_prompt, items, previous,
                                     period_start, period_end, self.name)
        logger.info("[%s] %d item(s) in %d section(s)", self.workflow_id, len(items), len(plan))

        part_b: list[str] = []
        for index, sec in enumerate(plan, 1):
            section_title = sec["title"]
            assigned = [by_id[i] for i in sec["items"] if i in by_id]
            prompt = (office_prompt.replace("{section_title}", section_title)
                      .replace("{period_start}", period_start)
                      .replace("{period_end}", period_end))
            user = "\n\n".join(
                f"### {_item_line(it)}\n{it.get('text', '')}" for it in assigned
            )
            try:
                body = client.generate(user_prompt=user, system_prompt=prompt,
                                       workflow=self.name, max_tokens=6000)
            except Exception as e:
                logger.warning("Draft failed for section %s: %s", section_title, e)
                failures.append(f"{section_title}: {str(e)[:120]}")
                continue
            part_b.append(f"## {index}\\. {section_title}\n\n{body.strip()}\n")
            logger.info("[%s] drafted section %r from %d item(s)",
                        self.workflow_id, section_title, len(assigned))

        if failures:
            return StepResult(
                success=False,
                data={"draft_failures": failures},
                message=(f"Η σύνταξη απέτυχε σε {len(failures)} σημείο(α): "
                         + "; ".join(failures[:4])),
            )

        markdown = _assemble(title, month_start, month_end, year, part_a, part_b)
        markdown, redactions, privacy_review = _redact(_tidy(markdown), staff_names)
        if redactions:
            logger.warning("[%s] privacy guard removed %d passage(s)",
                           self.workflow_id, len(redactions))

        drafts_dir = Path("data/egkyklios/drafts")
        drafts_dir.mkdir(parents=True, exist_ok=True)
        md_path = drafts_dir / f"{period_start}_{period_end}_draft.md"
        md_path.write_text(markdown, encoding="utf-8")
        report_path = md_path.with_name(md_path.stem + "_redactions.md")
        report_path.write_text(
            "# Αφαιρέθηκαν από τον έλεγχο απορρήτου\n\n"
            + ("\n".join(f"- {r}" for r in redactions) if redactions else "Τίποτα.")
            + "\n\n# Να ελεγχθούν: αναφέρουν πρόσωπο που ίσως είναι εργαζόμενος\n\n"
            + ("\n".join(f"- {r}" for r in privacy_review) if privacy_review else "Τίποτα.")
            + "\n",
            encoding="utf-8",
        )

        draft_id = ctx.get("egkyklios_draft_id") or create_egkyklios_draft(
            kind="general", period_start=period_start, period_end=period_end,
            title=title, workflow_id=self.workflow_id,
        )
        update_egkyklios_draft(draft_id, draft_md_path=str(md_path), status="drafting")

        log_action(
            workflow=self.name, action="circular_drafted", actor=self.actor,
            target=str(md_path),
            details={"chars": len(markdown), "meetings": len(part_a), "sections": len(part_b)},
        )
        return StepResult(
            success=True,
            data={"draft_markdown": markdown, "draft_md_path": str(md_path),
                  "egkyklios_draft_id": draft_id,
                  "redactions": redactions,
                  "privacy_review": privacy_review,
                  "office_items": len(items),
                  "office_sections": [s.get("title") for s in plan]},
            message=(f"Συντάχθηκε προσχέδιο {len(markdown)} χαρακτήρων "
                     f"({len(part_a)} συνεδριάσεις, {len(part_b)} ενότητες Γραφείου)"),
        )

    # ─────────────────────────────────────────────────────────────────────────
    # Step 5: render_pdf
    # ─────────────────────────────────────────────────────────────────────────

    async def _step_render_pdf(self, ctx: dict[str, Any]) -> StepResult:
        from src.documents.egkyklios_pdf import render_egkyklios_pdf

        period_start: str = ctx.get("period_start", "")
        period_end: str = ctx.get("period_end", "")
        title: str = ctx.get("title", "")
        draft_markdown: str = ctx.get("draft_markdown", "")
        draft_id: int = ctx.get("egkyklios_draft_id", 0)

        if not draft_markdown:
            # Reload from disk if not in context (e.g. resumed workflow)
            md_path_str = ctx.get("draft_md_path", "")
            if md_path_str and Path(md_path_str).exists():
                draft_markdown = Path(md_path_str).read_text(encoding="utf-8")
            else:
                return StepResult(
                    success=False,
                    message="Δεν βρέθηκε πρόχειρο Markdown - εκτελέστε ξανά το βήμα draft_circular.",
                )

        pdf_filename = f"{period_start}_{period_end}_draft.pdf"
        drafts_dir = Path("data/egkyklios/drafts")
        drafts_dir.mkdir(parents=True, exist_ok=True)
        pdf_path = drafts_dir / pdf_filename

        # The published document is produced by pasting this Markdown into the
        # section's letterhead. The PDF here is only a readable proof for the
        # review gate, so a failure to render it is not fatal.
        try:
            render_egkyklios_pdf(
                markdown_text=draft_markdown,
                output_path=pdf_path,
                title=title,
                period_start=period_start,
                period_end=period_end,
                protocol_number="",  # assigned at archiving
                workflow=self.name,
            )
        except Exception as e:
            logger.warning("Proof PDF could not be rendered (%s); the Markdown stands", e)

        if draft_id:
            update_egkyklios_draft(draft_id, draft_pdf_path=str(pdf_path))

        return StepResult(
            success=True,
            data={"draft_pdf_path": str(pdf_path)},
            message=f"PDF δημιουργήθηκε: {pdf_path}",
        )

    # ─────────────────────────────────────────────────────────────────────────
    # Step 6: notify_board_for_review
    # ─────────────────────────────────────────────────────────────────────────

    async def _step_notify_board_for_review(self, ctx: dict[str, Any]) -> StepResult:
        title: str = ctx.get("title", "")
        pdf_path_str: str = ctx.get("draft_pdf_path", "")
        test_mode = bool(ctx.get("test_mode"))
        draft_id: int = ctx.get("egkyklios_draft_id", 0)

        if not settings.ms_client_id or not settings.ms_tenant_id:
            # Update DB status even when email is skipped
            if draft_id:
                update_egkyklios_draft(draft_id, status="awaiting_approval")
            return StepResult(
                success=True,
                data={"review_email_skipped": True},
                message="Ειδοποίηση παρελήφθη - M365 δεν έχει ρυθμιστεί",
            )

        pdf_path = Path(pdf_path_str) if pdf_path_str else None
        attachments = [pdf_path] if pdf_path and pdf_path.exists() else []

        recipient = settings.testing.test_email if test_mode else _BOARD_EMAIL
        if test_mode and not recipient:
            if draft_id:
                update_egkyklios_draft(draft_id, status="awaiting_approval")
            return StepResult(
                success=True,
                data={"review_email_skipped": True},
                message="[TEST] Ειδοποίηση παρελήφθη - testing.test_email δεν έχει οριστεί",
            )

        subject = f"Πρόχειρο Γενικής Εγκυκλίου: {title}"
        body_html = (
            f"<p>Επισυνάπτεται το πρόχειρο της <strong>Γενικής Εγκυκλίου Ενημέρωσης "
            f"- {title}</strong> για έγκριση.</p>"
            f"<p>Παρακαλούμε ελέγξτε το περιεχόμενο και επικοινωνήστε με τον Γενικό "
            f"Γραμματέα για τυχόν διορθώσεις ή έγκριση αποστολής.</p>"
        )

        cc_list: list[str] | None = None if test_mode else [_DIRECTOR_EMAIL]

        try:
            from src.integrations.m365_mail import M365MailClient
            mail_client = M365MailClient()
            await mail_client.send_email(
                to=recipient,
                cc=cc_list,
                subject=subject,
                body=body_html,
                html=True,
                attachments=attachments,
                workflow=self.name,
            )
        except Exception as e:
            logger.warning("Αποτυχία αποστολής email ειδοποίησης (non-fatal): %s", e)

        # Publish bus event for Discord mirror (non-fatal)
        try:
            from src.core.event_bus import bus
            from src.core.events import EVENT_BOARD_EMAIL_SENT, BoardEmailSentPayload
            await bus.publish(
                EVENT_BOARD_EMAIL_SENT,
                BoardEmailSentPayload(
                    meeting_id=f"egkyklios:{title}",
                    meeting_ref=title,
                    kind="egkyklios_review",
                    subject=subject,
                    body_html=body_html,
                    test_mode=test_mode,
                ),
            )
        except Exception as bus_err:
            logger.warning("Bus publish failed (non-fatal): %s", bus_err)

        if draft_id:
            update_egkyklios_draft(draft_id, status="awaiting_approval")

        return StepResult(
            success=True,
            data={"review_email_sent": True},
            message=f"Email ειδοποίησης στάλθηκε στο {recipient}",
        )

    # ─────────────────────────────────────────────────────────────────────────
    # Step 7: await_approval (unconditional gate)
    # ─────────────────────────────────────────────────────────────────────────

    async def _step_await_approval(self, ctx: dict[str, Any]) -> StepResult:
        """Always halts. SecGen resumes via CLI or Discord button."""
        draft_id: int = ctx.get("egkyklios_draft_id", 0)
        if draft_id:
            update_egkyklios_draft(draft_id, status="approved")
        return StepResult(
            success=True,
            data={"approved": True, "approved_by": self.actor},
            message="Εγκυκλίος εγκρίθηκε - συνέχεια εκτέλεσης",
        )

    # ─────────────────────────────────────────────────────────────────────────
    # Step 8: archive_to_sharepoint
    # ─────────────────────────────────────────────────────────────────────────

    async def _step_archive_to_sharepoint(self, ctx: dict[str, Any]) -> StepResult:
        test_mode = bool(ctx.get("test_mode"))
        if test_mode:
            return StepResult(
                success=True,
                data={"archive_skipped": True},
                message="[TEST] Αρχειοθέτηση παρελήφθη",
            )
        if not settings.ms_client_id or not settings.ms_tenant_id:
            return StepResult(
                success=True,
                data={"archive_skipped": True},
                message="Αρχειοθέτηση παρελήφθη - OneDrive δεν έχει ρυθμιστεί",
            )

        pdf_path_str: str = ctx.get("draft_pdf_path", "")
        pdf_path = Path(pdf_path_str) if pdf_path_str else None
        if not pdf_path or not pdf_path.exists():
            return StepResult(success=False, message=f"PDF δεν βρέθηκε: {pdf_path_str}")

        title: str = ctx.get("title", "")
        period_end: str = ctx.get("period_end", "")
        draft_id: int = ctx.get("egkyklios_draft_id", 0)

        try:
            year_str = period_end[:4] if len(period_end) >= 4 else str(date.today().year)
            protocol_number = await allocate_protocol_number(
                self.onedrive, int(year_str), self.workflow_id
            )
        except Exception as e:
            logger.warning("Αποτυχία δέσμευσης αριθμού πρωτοκόλλου: %s", e)
            protocol_number = f"{date.today().year}_000"

        filename = f"[{protocol_number}] Γενική Εγκύκλιος Ενημέρωσης - {title}.pdf"
        remote_folder = f"Αρχείο/Εγκύκλιοι/Γενικές/{year_str}"

        try:
            result = await self.onedrive.upload_file(
                local_path=pdf_path,
                remote_folder=remote_folder,
                filename=filename,
                workflow=self.name,
            )
            file_id = result.get("id", "")
            share_link = ""
            if file_id:
                try:
                    share_link = await self.onedrive.get_share_link(file_id)
                except Exception:
                    logger.warning("Αποτυχία δημιουργίας share link")
        except Exception as e:
            return StepResult(success=False, message=f"Αποτυχία αρχειοθέτησης στο SharePoint: {e}")

        # Register in protocol xlsx
        try:
            await self.onedrive.append_protocol_row(
                protocol_id=protocol_number,
                date_str=date.today().isoformat(),
                title=f"Γενική Εγκύκλιος Ενημέρωσης - {title}",
                main_points="",
                tags="Εγκύκλιοι, Ενημέρωση Μελών",
            )
            commit_protocol_reservation(self.workflow_id)
        except Exception as e:
            logger.warning("Αποτυχία εγγραφής στο πρωτόκολλο (non-fatal): %s", e)

        if draft_id:
            update_egkyklios_draft(
                draft_id,
                protocol_number=protocol_number,
                sharepoint_url=share_link,
            )

        return StepResult(
            success=True,
            data={
                "protocol_number": protocol_number,
                "sharepoint_url": share_link,
            },
            message=f"Αρχειοθετήθηκε: {filename}, πρωτ. {protocol_number}",
        )

    # ─────────────────────────────────────────────────────────────────────────
    # Step 9: send_brevo_campaign
    # ─────────────────────────────────────────────────────────────────────────

    async def _step_send_brevo_campaign(self, ctx: dict[str, Any]) -> StepResult:
        test_mode = bool(ctx.get("test_mode"))
        title: str = ctx.get("title", "")
        sharepoint_url: str = ctx.get("sharepoint_url", "#")
        draft_id: int = ctx.get("egkyklios_draft_id", 0)
        pdf_path_str: str = ctx.get("draft_pdf_path", "")

        template_id = ctx.get("brevo_template_id") or settings.brevo.newsletter_template_id
        if not template_id:
            return StepResult(
                success=True,
                data={"brevo_skipped": True},
                message="Brevo παρελήφθη - brevo.newsletter_template_id δεν έχει οριστεί",
            )

        list_ids: list[int] = list(
            ctx.get("brevo_list_ids") or settings.brevo.newsletter_list_ids or []
        )
        segment_ids: list[int] = list(
            ctx.get("brevo_segment_ids") or settings.brevo.newsletter_segment_ids or []
        )
        has_audience = bool(list_ids or segment_ids)
        # Without an audience the master list only makes a draft creatable; it
        # is never sent to (live send below requires a configured audience).
        fallback_list = settings.brevo.master_list_id
        effective_list_ids = list_ids if has_audience else ([fallback_list] if fallback_list else [])
        if not (has_audience or effective_list_ids):
            return StepResult(
                success=True,
                data={"brevo_skipped": True},
                message="Brevo παρελήφθη - δεν υπάρχουν list IDs",
            )

        # Load email template and fill placeholders
        template_path = section.asset_path("email_templates") / "egkyklios_cover.html"
        if template_path.exists():
            html_body = template_path.read_text(encoding="utf-8")
            html_body = html_body.replace("{title}", title)
            html_body = html_body.replace("{download_url}", sharepoint_url)
        else:
            html_body = (
                f"<p>Η Γενική Εγκύκλιος Ενημέρωσης για την περίοδο <strong>{title}</strong> "
                f"είναι διαθέσιμη.</p>"
                f"<p><a href='{sharepoint_url}'>Κατεβάστε την εγκύκλιο</a></p>"
            )

        subject = f"Γενική Εγκύκλιος Ενημέρωσης - {title}"
        campaign_name = f"Εγκύκλιος {title}"
        test_addr = settings.testing.test_email

        params = {
            "[ΤΙΤΛΟΣ]": title,
            "[DOWNLOAD_URL]": sharepoint_url,
            "{title}": title,
            "{download_url}": sharepoint_url,
        }

        try:
            result = await self.brevo.send_campaign(
                template_id=template_id,
                list_ids=effective_list_ids,
                segment_ids=segment_ids,
                subject=subject,
                params=params,
                campaign_name=campaign_name,
                test_emails=[test_addr] if (test_mode and test_addr) else None,
                workflow=self.name,
            )
            campaign_id = result.get("campaign_id")

            if not test_mode and has_audience:
                try:
                    await self.brevo.send_campaign_now(campaign_id, workflow=self.name)
                except Exception as send_err:
                    logger.warning("Αποτυχία live αποστολής Brevo (non-fatal): %s", send_err)

            if draft_id:
                update_egkyklios_draft(
                    draft_id,
                    brevo_campaign_id=campaign_id,
                    status="sent",
                )

            return StepResult(
                success=True,
                data={
                    "brevo_campaign_id": campaign_id,
                    "newsletter_sent": not test_mode,
                },
                message=f"Brevo campaign {'(test)' if test_mode else 'sent'}: id={campaign_id}",
            )
        except Exception as e:
            return StepResult(
                success=True,  # non-fatal - circular is already archived
                data={"brevo_skipped": True},
                message=f"Αποτυχία Brevo (non-fatal): {e}",
            )

    # ─────────────────────────────────────────────────────────────────────────
    # Step 10: publish_event
    # ─────────────────────────────────────────────────────────────────────────

    async def _step_publish_event(self, ctx: dict[str, Any]) -> StepResult:
        title: str = ctx.get("title", "")
        protocol_number: str = ctx.get("protocol_number", "")
        sharepoint_url: str = ctx.get("sharepoint_url", "")
        sent_at = datetime.now(timezone.utc).isoformat()

        try:
            from src.core.event_bus import bus
            from src.core.events import EVENT_EGKYKLIOS_PUBLISHED, EgkykliosPublishedPayload
            await bus.publish(
                EVENT_EGKYKLIOS_PUBLISHED,
                EgkykliosPublishedPayload(
                    kind="general",
                    title=title,
                    protocol_number=protocol_number,
                    sharepoint_url=sharepoint_url,
                    sent_at=sent_at,
                ),
            )
            logger.info("EgkykliosPublished event published: %s", title)
        except Exception as e:
            logger.warning("Bus publish EgkykliosPublished failed (non-fatal): %s", e)

        return StepResult(
            success=True,
            data={"event_published": True, "sent_at": sent_at},
            message=f"EVENT_EGKYKLIOS_PUBLISHED εκδόθηκε - {title}",
        )
