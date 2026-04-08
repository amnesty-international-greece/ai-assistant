"""Board meeting minutes workflow — Phase 2 full implementation."""

from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any

from src.config import settings
from src.core.claude import ClaudeClient
from src.core.workflow import BaseWorkflow, WorkflowStep, StepResult
from src.documents.pdf_generator import embed_signatures
from src.integrations.google_drive import GoogleClient
from src.integrations.zoom import ZoomClient

logger = logging.getLogger(__name__)


def _strip_json_fences(text: str) -> str:
    """Strip markdown code fences that LLMs sometimes add around JSON."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
    if text.endswith("```"):
        text = text.rsplit("```", 1)[0]
    return text.strip()


def _parse_meeting_ref(meeting_ref: str) -> tuple[int, int]:
    """Parse a meeting reference like 'ΔΣ03-2026' into (number=3, year=2026).

    Returns:
        (meeting_number, meeting_year) as integers.

    Raises:
        ValueError: if the reference cannot be parsed.
    """
    match = re.search(r"(\d+)-(\d{4})", meeting_ref)
    if not match:
        raise ValueError(f"Cannot parse meeting reference: {meeting_ref!r}")
    return int(match.group(1)), int(match.group(2))


def _format_draft_as_text(draft_json: dict[str, Any]) -> str:
    """Convert a draft_json dict (Claude's output) to readable plain text."""
    lines: list[str] = []

    title = draft_json.get("title", "Πρακτικά Συνεδρίασης")
    lines.append(title)
    lines.append("=" * len(title))
    lines.append("")

    meta = draft_json.get("metadata", {})
    if meta:
        for key, val in meta.items():
            lines.append(f"{key}: {val}")
        lines.append("")

    for section in draft_json.get("sections", []):
        heading = section.get("heading", "")
        body = section.get("body", "")
        if heading:
            lines.append(f"\n{heading}")
            lines.append("-" * len(heading))
        if body:
            lines.append(body)

    decisions = draft_json.get("decisions", [])
    if decisions:
        lines.append("\n\nΑΠΟΦΑΣΕΙΣ")
        lines.append("----------")
        for d in decisions:
            num = d.get("number", "")
            text = d.get("text", "")
            vote = d.get("vote", "")
            vote_str = f" ({vote})" if vote else ""
            lines.append(f"{num}. {text}{vote_str}")

    return "\n".join(lines)


class BoardMeetingMinutesWorkflow(BaseWorkflow):
    """Minutes workflow: SecGen notes + Zoom transcript → Claude drafts →
    Google Doc → [APPROVAL] → Share via Gmail → Finalize & Archive →
    Extract decisions → Βιβλίο Αποφάσεων
    """

    def __init__(self, actor: str = "secgen"):
        self._google = GoogleClient()
        self._zoom = ZoomClient()
        self._gmail = None
        self._onedrive = None
        super().__init__(actor=actor)

    @property
    def gmail(self):
        if self._gmail is None:
            from src.integrations.gmail import GmailClient
            self._google._ensure_authenticated()
            self._gmail = GmailClient(self._google._creds)
        return self._gmail

    @property
    def onedrive(self):
        if self._onedrive is None:
            from src.integrations.onedrive import OneDriveClient
            self._onedrive = OneDriveClient()
        return self._onedrive

    @property
    def name(self) -> str:
        return "board_meeting_minutes"

    def define_steps(self) -> list[WorkflowStep]:
        return [
            WorkflowStep("select_sources", "Select Google Doc and Zoom recording"),
            WorkflowStep("draft_minutes", "Draft minutes with Claude"),
            WorkflowStep("write_draft_to_doc", "Write draft back to Google Doc"),
            WorkflowStep("approval_and_share", "Review, approve and share draft", requires_approval=True),
            WorkflowStep("finalize", "Generate signed PDF and archive"),
            WorkflowStep("extract_decisions", "Write decisions to Βιβλίο Αποφάσεων"),
        ]

    async def execute_step(self, step: WorkflowStep, context: dict[str, Any]) -> StepResult:
        """Route to the appropriate step handler."""
        handler = getattr(self, f"_step_{step.name}", None)
        if not handler:
            return StepResult(success=False, message=f"No handler for step: {step.name}")
        return await handler(context)

    # ── Step 1: Select sources ────────────────────────────────────────────────

    async def _step_select_sources(self, ctx: dict[str, Any]) -> StepResult:
        """List Google Docs in the drafts folder and recordings from Zoom.

        Uses ctx['meeting_ref'] (e.g. 'ΔΣ03-2026') to auto-match.
        ctx['source_doc_index'] (0-based) picks which doc.
        ctx.get('recording_index') picks which recording (None = auto-match).
        """
        meeting_ref: str = ctx.get("meeting_ref", "")
        if not meeting_ref:
            return StepResult(success=False, message="meeting_ref is required in context")

        try:
            meeting_number, meeting_year = _parse_meeting_ref(meeting_ref)
        except ValueError as e:
            return StepResult(success=False, message=str(e))

        folder_id = settings.google.minutes_drafts_folder_id
        if not folder_id:
            return StepResult(
                success=False,
                message="google.minutes_drafts_folder_id not configured",
            )

        # List docs in drafts folder
        docs = self._google.list_docs_in_folder(folder_id)
        if not docs:
            return StepResult(success=False, message="No Google Docs found in minutes_drafts_folder")

        # Select source doc
        source_doc_index: int = ctx.get("source_doc_index", 0)

        # Auto-match by meeting_ref in doc name if index not explicitly set
        if "source_doc_index" not in ctx:
            for i, doc in enumerate(docs):
                if meeting_ref in doc.get("name", ""):
                    source_doc_index = i
                    break

        if source_doc_index >= len(docs):
            return StepResult(
                success=False,
                message=f"source_doc_index {source_doc_index} out of range (found {len(docs)} docs)",
            )

        source_doc = docs[source_doc_index]
        source_doc_id: str = source_doc["id"]

        # Read SecGen notes from the selected doc
        secgen_notes: str = self._google.read_doc_content(source_doc_id)

        # List Zoom recordings and get transcript
        zoom_transcript = ""
        recording_index = ctx.get("recording_index")

        try:
            recordings = await self._zoom.list_recordings()
        except Exception as e:
            logger.warning("Could not fetch Zoom recordings: %s", e)
            recordings = []

        if recordings:
            selected_recording = None

            if recording_index is not None:
                if recording_index < len(recordings):
                    selected_recording = recordings[recording_index]
            else:
                # Auto-match by meeting_ref in topic
                for rec in recordings:
                    topic = rec.get("topic", "")
                    if meeting_ref in topic:
                        selected_recording = rec
                        break
                # Fallback: take the most recent recording
                if selected_recording is None and recordings:
                    selected_recording = recordings[0]

            if selected_recording:
                meeting_id = str(selected_recording["id"])
                try:
                    transcript = await self._zoom.get_transcript(meeting_id)
                    zoom_transcript = transcript or ""
                except Exception as e:
                    logger.warning("Could not fetch transcript for meeting %s: %s", meeting_id, e)

        return StepResult(
            success=True,
            message=f"Sources selected: doc '{source_doc['name']}', transcript length={len(zoom_transcript)}",
            data={
                "secgen_notes": secgen_notes,
                "zoom_transcript": zoom_transcript,
                "source_doc_id": source_doc_id,
                "meeting_ref": meeting_ref,
                "meeting_number": meeting_number,
                "meeting_year": meeting_year,
            },
        )

    # ── Step 2: Draft minutes ─────────────────────────────────────────────────

    async def _step_draft_minutes(self, ctx: dict[str, Any]) -> StepResult:
        """Call Claude to merge SecGen notes and Zoom transcript into draft minutes."""
        secgen_notes: str = ctx.get("secgen_notes", "")
        zoom_transcript: str = ctx.get("zoom_transcript", "")
        meeting_ref: str = ctx.get("meeting_ref", "")

        # Load system prompt
        prompt_path = Path(settings.storage.prompts_dir) / "board_minutes.md"
        if not prompt_path.exists():
            return StepResult(success=False, message=f"System prompt not found: {prompt_path}")
        system_prompt = prompt_path.read_text(encoding="utf-8")

        user_prompt = (
            f"## Secretary General's Notes\n\n{secgen_notes}\n\n"
            f"## Zoom Transcript\n\n{zoom_transcript}\n\n"
            f"## Meeting Reference: {meeting_ref}"
        )

        try:
            client = ClaudeClient()
            raw_response = client.generate(
                user_prompt=user_prompt,
                system_prompt=system_prompt,
                workflow=self.name,
            )
        except Exception as e:
            return StepResult(success=False, message=f"LLM call failed: {e}")

        try:
            draft_json = json.loads(_strip_json_fences(raw_response))
        except json.JSONDecodeError as e:
            return StepResult(success=False, message=f"Failed to parse LLM JSON response: {e}")

        return StepResult(
            success=True,
            message="Draft minutes generated by Claude",
            data={"draft_json": draft_json},
        )

    # ── Step 3: Write draft to Google Doc ─────────────────────────────────────

    async def _step_write_draft_to_doc(self, ctx: dict[str, Any]) -> StepResult:
        """Format the draft JSON as plain text and write it back to the Google Doc."""
        draft_json: dict = ctx.get("draft_json", {})
        source_doc_id: str = ctx.get("source_doc_id", "")
        meeting_ref: str = ctx.get("meeting_ref", "")

        if not source_doc_id:
            return StepResult(success=False, message="source_doc_id missing from context")

        formatted_text = _format_draft_as_text(draft_json)

        self._google.clear_and_write_doc(source_doc_id, formatted_text)

        new_title = f"[Πρόχειρο] Πρακτικά - Συνεδρίαση {meeting_ref}"
        self._google.rename_file(source_doc_id, new_title)

        draft_doc_url = f"https://docs.google.com/document/d/{source_doc_id}/edit"

        return StepResult(
            success=True,
            message=f"Draft written to Google Doc and renamed: {new_title}",
            data={
                "draft_doc_id": source_doc_id,
                "draft_doc_url": draft_doc_url,
            },
        )

    # ── Step 4: Approval gate + share ─────────────────────────────────────────

    async def _step_approval_and_share(self, ctx: dict[str, Any]) -> StepResult:
        """After approval gate, email the draft to board members."""
        meeting_ref: str = ctx.get("meeting_ref", "")
        draft_doc_url: str = ctx.get("draft_doc_url", "")
        test_mode: bool = ctx.get("test_mode", False)

        board_members = settings.workflows.board_meeting.board_members
        share_message = settings.workflows.board_meeting.minutes_share_message

        recipients = [m.email for m in board_members]

        if test_mode:
            dry_run_email = settings.testing.dry_run_email
            if dry_run_email:
                recipients = [dry_run_email]
            else:
                logger.info("test_mode=True and no dry_run_email set — skipping email")
                return StepResult(
                    success=True,
                    message="Email skipped (test_mode, no dry_run_email configured)",
                    data={"shared": False, "shared_at": datetime.utcnow().isoformat()},
                )

        if recipients:
            subject = f"Πρόχειρα Πρακτικά - Συνεδρίαση {meeting_ref}"
            body_html = (
                f"<p>{share_message}</p>"
                f'<p><a href="{draft_doc_url}">Άνοιγμα εγγράφου</a></p>'
            )
            self.gmail.send_email(
                to=recipients,
                subject=subject,
                body_html=body_html,
                workflow=self.name,
            )
            logger.info("Draft minutes shared with %d board member(s)", len(recipients))
        else:
            logger.info("No board members configured — skipping email share")

        return StepResult(
            success=True,
            message=f"Draft shared with {len(recipients)} recipient(s)",
            data={
                "shared": True,
                "shared_at": datetime.utcnow().isoformat(),
            },
        )

    # ── Step 5: Finalize ──────────────────────────────────────────────────────

    async def _step_finalize(self, ctx: dict[str, Any]) -> StepResult:
        """Export Google Doc as PDF, embed signatures, archive, register protocol.

        In test_mode: generates PDF and signs it, but skips OneDrive archive,
        Πρωτόκολλο registration, and Google Doc rename — so nothing permanent
        is written outside the local filesystem.
        """
        test_mode: bool = ctx.get("test_mode", False)
        draft_doc_id: str = ctx.get("draft_doc_id", "") or ctx.get("source_doc_id", "")
        meeting_ref: str = ctx.get("meeting_ref", "")
        meeting_year: int = ctx.get("meeting_year", date.today().year)
        draft_json: dict = ctx.get("draft_json", {})

        if not draft_doc_id:
            return StepResult(success=False, message="draft_doc_id missing from context")

        # Export Google Doc as PDF
        pdf_dir = Path("data/minutes")
        pdf_dir.mkdir(parents=True, exist_ok=True)
        pdf_path = pdf_dir / f"minutes_{meeting_ref}.pdf"

        self._google.export_doc_as_pdf(draft_doc_id, pdf_path)

        # Embed signatures (skip if signature files don't exist)
        signed_pdf_path = pdf_dir / f"minutes_{meeting_ref}_signed.pdf"
        sig_dir = Path("brand/Signatures")
        sig1 = sig_dir / "president.png"
        sig2 = sig_dir / "secgen.png"

        if sig1.exists() or sig2.exists():
            signatures = []
            if sig1.exists():
                signatures.append({
                    "image_path": str(sig1),
                    "x": 100, "y": 80, "width": 120, "height": 50,
                    "label": "Ο Πρόεδρος",
                })
            if sig2.exists():
                signatures.append({
                    "image_path": str(sig2),
                    "x": 350, "y": 80, "width": 120, "height": 50,
                    "label": "Ο Γενικός Γραμματέας",
                })
            try:
                embed_signatures(pdf_path, signed_pdf_path, signatures, workflow=self.name)
                final_pdf_path = signed_pdf_path
            except Exception as e:
                logger.warning("Could not embed signatures: %s — using unsigned PDF", e)
                final_pdf_path = pdf_path
        else:
            logger.info("No signature files found in %s — skipping signing", sig_dir)
            final_pdf_path = pdf_path

        # Read protocol number from Πρωτόκολλο sheet
        protocol_number = ""
        protokollo_id = settings.google.protokollo_sheet_id
        if protokollo_id:
            try:
                last_entry = self._google.get_last_row_value(protokollo_id, "A:A")
                if last_entry:
                    # Parse format like "2026_014" and increment
                    parts = last_entry.split("_")
                    if len(parts) == 2:
                        seq = int(parts[1]) + 1
                    else:
                        seq = 1
                else:
                    seq = 1
                protocol_number = f"{meeting_year}_{seq:03d}"
            except Exception as e:
                logger.warning("Could not read Πρωτόκολλο sheet: %s", e)
                protocol_number = f"{meeting_year}_001"
        else:
            protocol_number = f"{meeting_year}_001"

        # ── Persistent side-effects (skipped in test_mode) ─────────────────
        archive_info: dict[str, Any] = {}

        if test_mode:
            logger.info("test_mode — skipping OneDrive archive, Πρωτόκολλο write, and Doc rename")
            archive_info = {"status": "skipped", "reason": "test_mode"}
        else:
            # Upload to OneDrive (skip if MS creds not configured)
            if settings.ms_client_id and settings.ms_tenant_id:
                try:
                    result = await self.onedrive.upload_file(
                        local_path=final_pdf_path,
                        remote_folder=f"Minutes/{meeting_year}",
                        filename=f"minutes_{meeting_ref}.pdf",
                        workflow=self.name,
                    )
                    archive_info = {"file_id": result.get("id"), "status": "archived"}
                except Exception as e:
                    logger.warning("OneDrive upload failed: %s — skipping archive", e)
                    archive_info = {"status": "skipped", "reason": str(e)}
            else:
                logger.info("MS credentials not configured — skipping OneDrive archive")
                archive_info = {"status": "skipped", "reason": "MS credentials not configured"}

            # Register in Πρωτόκολλο sheet
            if protokollo_id:
                try:
                    today_str = date.today().isoformat()
                    key_points = "; ".join(
                        d.get("text", "")[:80] for d in draft_json.get("decisions", [])
                    )
                    self._google.write_sheet(
                        protokollo_id,
                        "A:E",
                        [[
                            protocol_number,
                            today_str,
                            f"Πρακτικά - Συνεδρίαση {meeting_ref}",
                            key_points,
                            "Διοικητικά, Πρακτικά",
                        ]],
                    )
                except Exception as e:
                    logger.warning("Could not write to Πρωτόκολλο sheet: %s", e)

            # Rename Google Doc to final title
            final_title = f"[Τελικό] Πρακτικά - Συνεδρίαση {meeting_ref}"
            self._google.rename_file(draft_doc_id, final_title)

        return StepResult(
            success=True,
            message=f"Finalized: {final_pdf_path}, protocol={protocol_number}",
            data={
                "pdf_path": str(final_pdf_path),
                "protocol_number": protocol_number,
                "archive_info": archive_info,
            },
        )

    # ── Step 6: Extract decisions ─────────────────────────────────────────────

    async def _step_extract_decisions(self, ctx: dict[str, Any]) -> StepResult:
        """Write decisions to Βιβλίο Αποφάσεων sheet.

        In test_mode: computes decision numbers but does NOT write to the sheet.
        """
        test_mode: bool = ctx.get("test_mode", False)
        draft_json: dict = ctx.get("draft_json", {})
        meeting_number: int = ctx.get("meeting_number", 0)
        meeting_year: int = ctx.get("meeting_year", date.today().year)

        decisions = draft_json.get("decisions", [])
        if not decisions:
            return StepResult(
                success=True,
                message="No decisions to write",
                data={"decisions_written": 0, "decision_numbers": []},
            )

        decisions_sheet_id = settings.google.decisions_sheet_id
        if not decisions_sheet_id:
            return StepResult(
                success=False,
                message="google.decisions_sheet_id not configured",
            )

        # Determine starting decision sequence number
        # Decision format: ΔΣ{decision_seq:02d}-{meeting_number:02d}-{year}
        # Look for last entry for this meeting
        mm = f"{meeting_number:02d}"
        yyyy = str(meeting_year)
        decision_seq = 1

        try:
            # Read all entries in the sheet to find the last decision for this year
            all_rows = self._google.read_sheet(decisions_sheet_id, "A:B")
            pattern = re.compile(rf"ΔΣ(\d{{2}})-{re.escape(mm)}-{re.escape(yyyy)}")
            last_seq = 0
            for row in all_rows:
                if not row:
                    continue
                cell = row[0] if row else ""
                match = pattern.match(cell)
                if match:
                    last_seq = max(last_seq, int(match.group(1)))
            decision_seq = last_seq + 1
        except Exception as e:
            logger.warning("Could not read Βιβλίο Αποφάσεων sheet: %s — starting at 1", e)
            decision_seq = 1

        # Build rows to write
        rows: list[list[str]] = []
        decision_numbers: list[str] = []
        for d in decisions:
            decision_number = f"ΔΣ{decision_seq:02d}-{mm}-{yyyy}"
            decision_numbers.append(decision_number)
            rows.append([decision_number, d.get("text", "")])
            decision_seq += 1

        if test_mode:
            logger.info("test_mode — skipping Βιβλίο Αποφάσεων write (would write %d decisions)", len(rows))
        else:
            try:
                self._google.write_sheet(decisions_sheet_id, "A:B", rows)
            except Exception as e:
                return StepResult(
                    success=False,
                    message=f"Failed to write decisions to sheet: {e}",
                )

        return StepResult(
            success=True,
            message=f"{'[TEST] Would write' if test_mode else 'Wrote'} {len(rows)} decision(s) to Βιβλίο Αποφάσεων",
            data={
                "decisions_written": len(rows),
                "decision_numbers": decision_numbers,
            },
        )
