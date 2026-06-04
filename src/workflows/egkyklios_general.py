"""Γενική Εγκύκλιος Ενημέρωσης workflow.

Step order (10 steps):
  1.  gather_sources          — resolve period, validate sources exist, idempotency guard
  2.  extract_briefing_texts  — read each briefing PDF via extract_pdf_text()
  3.  extract_meeting_summaries — pull minutes text from workflow_state rows
  4.  draft_circular          — LLM call → Markdown saved to disk + DB row created
  5.  render_pdf              — Markdown → branded ReportLab PDF
  6.  notify_board_for_review — M365 email to board + director with PDF attachment
  7.  await_approval          — halt until SecGen approves (requires_approval=True)
  8.  archive_to_sharepoint   — upload PDF + append protocol row
  9.  send_brevo_campaign     — create & send Brevo campaign to members
  10. publish_event           — emit EVENT_EGKYKLIOS_PUBLISHED on event bus
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from src.config import settings
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

logger = logging.getLogger(__name__)

_BOARD_EMAIL = "board@amnesty.org.gr"
_DIRECTOR_EMAIL = "director@amnesty.org.gr"

# Greek month names in nominative (upper) for the title
_GREEK_MONTHS_TITLE = {
    1: "ΙΑΝΟΥΑΡΙΟΣ", 2: "ΦΕΒΡΟΥΑΡΙΟΣ", 3: "ΜΑΡΤΙΟΣ", 4: "ΑΠΡΙΛΙΟΣ",
    5: "ΜΑΪΟΣ", 6: "ΙΟΥΝΙΟΣ", 7: "ΙΟΥΛΙΟΣ", 8: "ΑΥΓΟΥΣΤΟΣ",
    9: "ΣΕΠΤΕΜΒΡΙΟΣ", 10: "ΟΚΤΩΒΡΙΟΣ", 11: "ΝΟΕΜΒΡΙΟΣ", 12: "ΔΕΚΕΜΒΡΙΟΣ",
}

# Greek month names in genitive (for prose references)
_GREEK_MONTHS_GEN = {
    1: "Ιανουαρίου", 2: "Φεβρουαρίου", 3: "Μαρτίου", 4: "Απριλίου",
    5: "Μαΐου", 6: "Ιουνίου", 7: "Ιουλίου", 8: "Αυγούστου",
    9: "Σεπτεμβρίου", 10: "Οκτωβρίου", 11: "Νοεμβρίου", 12: "Δεκεμβρίου",
}


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
        return f"{period_start} – {period_end}"


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


class EgkykliosGeneralWorkflow(BaseWorkflow):
    """Γενική Εγκύκλιος Ενημέρωσης — full 10-step workflow."""

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
        may fail if a non-cancelled draft already overlaps this period — pass
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
                        f"Υπάρχει ήδη εγκύκλιος για την περίοδο {row['period_start']} – {row['period_end']} "
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
                    f"Δεν βρέθηκαν πηγές για την περίοδο {period_start} – {period_end}. "
                    "Απαιτείται τουλάχιστον ένα εισηγητικό/ενημερωτικό Διευθυντή ή "
                    "ένα σύνολο πρακτικών συνεδρίασης."
                ),
            )

        logger.info(
            "[%s] gather_sources: %d briefing(s), %d minutes row(s) in %s – %s",
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
                text, meta = extract_pdf_text(p, max_chars=8000)
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
                text = f"[Πρακτικά συνεδρίασης {workflow_id} — δεν βρέθηκε κείμενο στο context]"

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
        period_start: str = ctx.get("period_start", "")
        period_end: str = ctx.get("period_end", "")
        title: str = ctx.get("title", _period_title(period_start, period_end))
        briefing_texts: list[dict] = ctx.get("briefing_texts", [])
        meeting_summaries: list[dict] = ctx.get("meeting_summaries", [])

        # Build source bundles for the prompt
        briefings_text = ""
        for bt in briefing_texts:
            scan_note = " [ΣΚΑΝΑΡΙΣΜΕΝΟ — περιορισμένη ανάγνωση]" if bt.get("is_scan") else ""
            briefings_text += (
                f"\n--- {bt['kind']} / Συνεδρίαση {bt['meeting_ref']} "
                f"({bt.get('archived_at', '')[:10]}){scan_note} ---\n"
                f"{bt['text']}\n"
            )

        minutes_text = ""
        for ms in meeting_summaries:
            minutes_text += (
                f"\n--- Πρακτικά Συνεδρίασης {ms.get('meeting_ref') or ms.get('workflow_id', '')} "
                f"({ms.get('meeting_date', '')}) ---\n"
                f"{ms['text']}\n"
            )

        if not briefings_text.strip():
            briefings_text = "[Δεν υπάρχουν εισηγητικά για αυτή την περίοδο]"
        if not minutes_text.strip():
            minutes_text = "[Δεν υπάρχουν πρακτικά συνεδριάσεων για αυτή την περίοδο]"

        # Load prompt template
        try:
            client = ClaudeClient()
            system_prompt = client.load_prompt("egkyklios_general")
        except FileNotFoundError as e:
            return StepResult(success=False, message=f"Δεν βρέθηκε prompt: {e}")

        # Derive prose month names for intro paragraph substitution
        try:
            ds = date.fromisoformat(period_start)
            de = date.fromisoformat(period_end)
            month_start = _GREEK_MONTHS_GEN[ds.month]
            month_end = _GREEK_MONTHS_GEN[de.month]
            year = str(de.year)
        except Exception:
            month_start = period_start
            month_end = period_end
            year = ""

        # Fill prompt placeholders in the system prompt
        system_prompt = system_prompt.replace("{period_start}", period_start)
        system_prompt = system_prompt.replace("{period_end}", period_end)
        system_prompt = system_prompt.replace("{title}", title)
        system_prompt = system_prompt.replace("{month_start}", month_start)
        system_prompt = system_prompt.replace("{month_end}", month_end)
        system_prompt = system_prompt.replace("{year}", year)

        user_prompt = (
            f"## Εισηγητικά / Ενημερωτικά Διευθυντή\n\n{briefings_text}\n\n"
            f"## Πρακτικά Συνεδριάσεων ΔΣ\n\n{minutes_text}"
        )

        try:
            raw_md = client.generate(
                user_prompt=user_prompt,
                system_prompt=system_prompt,
                workflow=self.name,
                max_tokens=8000,
            )
        except Exception as e:
            return StepResult(success=False, message=f"Αποτυχία LLM κλήσης: {e}")

        # Save Markdown to disk
        drafts_dir = Path("data/egkyklios/drafts")
        drafts_dir.mkdir(parents=True, exist_ok=True)
        md_filename = f"{period_start}_{period_end}_draft.md"
        md_path = drafts_dir / md_filename
        md_path.write_text(raw_md, encoding="utf-8")

        # Create DB row
        draft_id = create_egkyklios_draft(
            kind="general",
            period_start=period_start,
            period_end=period_end,
            title=title,
            workflow_id=self.workflow_id,
        )
        update_egkyklios_draft(draft_id, draft_md_path=str(md_path))

        log_action(
            workflow=self.name,
            action="circular_drafted",
            actor=self.actor,
            target=str(md_path),
            details={
                "draft_id": draft_id,
                "title": title,
                "md_chars": len(raw_md),
            },
        )

        return StepResult(
            success=True,
            data={
                "draft_md_path": str(md_path),
                "draft_markdown": raw_md,
                "egkyklios_draft_id": draft_id,
            },
            message=f"Πρόχειρο εγκυκλίου δημιουργήθηκε: {md_path} ({len(raw_md)} χαρακτήρες)",
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
                    message="Δεν βρέθηκε πρόχειρο Markdown — εκτελέστε ξανά το βήμα draft_circular.",
                )

        pdf_filename = f"{period_start}_{period_end}_draft.pdf"
        drafts_dir = Path("data/egkyklios/drafts")
        drafts_dir.mkdir(parents=True, exist_ok=True)
        pdf_path = drafts_dir / pdf_filename

        try:
            render_egkyklios_pdf(
                markdown_text=draft_markdown,
                output_path=pdf_path,
                title=title,
                period_start=period_start,
                period_end=period_end,
                protocol_number="",  # not yet assigned
                workflow=self.name,
            )
        except Exception as e:
            return StepResult(success=False, message=f"Αποτυχία απόδοσης PDF: {e}")

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
                message="Ειδοποίηση παρελήφθη — M365 δεν έχει ρυθμιστεί",
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
                message="[TEST] Ειδοποίηση παρελήφθη — testing.test_email δεν έχει οριστεί",
            )

        subject = f"Πρόχειρο Γενικής Εγκυκλίου: {title}"
        body_html = (
            f"<p>Επισυνάπτεται το πρόχειρο της <strong>Γενικής Εγκυκλίου Ενημέρωσης "
            f"— {title}</strong> για έγκριση.</p>"
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
            message="Εγκυκλίος εγκρίθηκε — συνέχεια εκτέλεσης",
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
                message="Αρχειοθέτηση παρελήφθη — OneDrive δεν έχει ρυθμιστεί",
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
            protocol_number = await self.onedrive.get_next_protocol_number(int(year_str))
        except Exception as e:
            logger.warning("Αποτυχία ανάκτησης αριθμού πρωτοκόλλου: %s", e)
            protocol_number = f"{date.today().year}_000"

        filename = f"[{protocol_number}] Γενική Εγκύκλιος Ενημέρωσης — {title}.pdf"
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
                title=f"Γενική Εγκύκλιος Ενημέρωσης — {title}",
                main_points="",
                tags="Εγκύκλιοι, Ενημέρωση Μελών",
            )
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
                message="Brevo παρελήφθη — brevo.newsletter_template_id δεν έχει οριστεί",
            )

        list_ids: list[int] = (
            ctx.get("brevo_list_ids")
            or settings.brevo.newsletter_list_ids
            or []
        )
        fallback_list = settings.brevo.master_list_id
        effective_list_ids = list_ids if list_ids else ([fallback_list] if fallback_list else [])
        if not effective_list_ids:
            return StepResult(
                success=True,
                data={"brevo_skipped": True},
                message="Brevo παρελήφθη — δεν υπάρχουν list IDs",
            )

        # Load email template and fill placeholders
        template_path = Path("assets/email_templates/egkyklios_cover.html")
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

        subject = f"Γενική Εγκύκλιος Ενημέρωσης — {title}"
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
                subject=subject,
                params=params,
                campaign_name=campaign_name,
                test_emails=[test_addr] if (test_mode and test_addr) else None,
                workflow=self.name,
            )
            campaign_id = result.get("campaign_id")

            if not test_mode and list_ids:
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
                success=True,  # non-fatal — circular is already archived
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
            message=f"EVENT_EGKYKLIOS_PUBLISHED εκδόθηκε — {title}",
        )
