"""Board meeting minutes workflow - Phase 2 full implementation."""

from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from src.config import settings
from src.core.claude import ClaudeClient
from src.core.email_templates import render_email
from src.core.workflow import BaseWorkflow, WorkflowStep, StepResult
from src.documents.pdf_generator import embed_signatures
from src.integrations.google_drive import GoogleClient
from src.integrations.zoom import ZoomClient
from src.utils.transcript_parser import parse_transcript

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


def _format_draft_as_sections(draft_json: dict[str, Any]) -> list[dict[str, str]]:
    """Convert a draft_json dict (Claude's output) to structured sections
    suitable for ``GoogleClient.write_structured_doc``.

    Returns a list of ``{"type": "title"|"heading"|"body", "text": "..."}`` dicts.
    """
    sections: list[dict[str, str]] = []

    title = draft_json.get("title", "Πρακτικά Συνεδρίασης")
    sections.append({"type": "title", "text": title})

    meta = draft_json.get("metadata", {})
    if meta:
        meta_lines = [f"{key}: {val}" for key, val in meta.items()]
        sections.append({"type": "body", "text": "\n".join(meta_lines)})

    for section in draft_json.get("sections", []):
        heading = section.get("heading", "")
        body = section.get("body", "")
        if heading:
            sections.append({"type": "heading", "text": heading})
        if body:
            sections.append({"type": "body", "text": body})

    decisions = draft_json.get("decisions", [])
    if decisions:
        sections.append({"type": "heading", "text": "ΑΠΟΦΑΣΕΙΣ"})
        decision_lines = []
        for d in decisions:
            num = d.get("number", "")
            text = d.get("text", "")
            vote = d.get("vote", "")
            vote_str = f" ({vote})" if vote else ""
            decision_lines.append(f"{num}. {text}{vote_str}")
        sections.append({"type": "body", "text": "\n".join(decision_lines)})

    return sections


def _format_draft_as_text(draft_json: dict[str, Any]) -> str:
    """Convert a draft_json dict to plain text (used for preview/logging)."""
    sections = _format_draft_as_sections(draft_json)
    lines: list[str] = []
    for sec in sections:
        if sec["type"] == "title":
            lines.append(sec["text"])
            lines.append("=" * len(sec["text"]))
            lines.append("")
        elif sec["type"] == "heading":
            lines.append(f"\n{sec['text']}")
            lines.append("-" * len(sec["text"]))
        else:
            lines.append(sec["text"])
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

    async def rollback(self, ctx: dict[str, Any]) -> None:
        """Undo any side-effects when the user rejects the draft.

        For now this is a no-op - minutes drafting does not create external
        resources that need cleanup (unlike the invitation workflow which
        creates a Zoom meeting).  The Google Doc is only modified *after*
        approval, so rejecting before that leaves everything unchanged.
        """
        logger.info("Minutes workflow rollback - no side-effects to undo.")

    def define_steps(self) -> list[WorkflowStep]:
        return [
            WorkflowStep("select_sources", "Select Google Doc and Zoom recording"),
            WorkflowStep("draft_minutes", "Draft minutes with Claude"),
            WorkflowStep("write_draft_to_doc", "Write draft back to Google Doc"),
            WorkflowStep("approval_and_share", "Review, approve and share draft", requires_approval=True),
            WorkflowStep("finalize", "Generate signed PDF and archive"),
            WorkflowStep("extract_decisions", "Write decisions to Βιβλίο Αποφάσεων"),
        ]

    @staticmethod
    def debug_fixture() -> dict[str, Any]:
        """Canonical fake ctx for `debug run board_meeting_minutes <step>`.

        Provides every key any ``_step_*`` reads so a step can run in isolation
        without a KeyError.  The debug runner forces ``test_mode=True`` (skips
        OneDrive archive, Πρωτόκολλο write, Doc rename, and Βιβλίο Αποφάσεων
        write); it is intentionally NOT set here.
        """
        return {
            # select_sources
            "meeting_ref": "ΔΣ99-2099",                   # required by select_sources/draft/write/finalize
            "source_doc_id": "debug-doc-id",              # select_sources: skip folder listing
            "source_doc_name": "[Πρόχειρο] Πρακτικά - Συνεδρίαση ΔΣ99-2099",  # select_sources
            "source_doc_index": 0,                        # select_sources fallback index
            "recording_index": -1,                        # select_sources: skip Zoom recordings
            "transcript_path": "",                        # select_sources local transcript override
            # select_sources outputs → consumed by draft_minutes / extract_*
            "secgen_notes": "Δοκιμαστικές σημειώσεις Γενικού Γραμματέα.",
            "zoom_transcript": "Δοκιμαστικό απομαγνητοφωνημένο κείμενο.",
            "meeting_number": 99,                         # extract_decisions / finalize
            "meeting_year": 2099,                         # extract_decisions / finalize
            # draft_minutes output → consumed by write/finalize/extract_decisions
            "draft_json": {
                "title": "Πρακτικά - Συνεδρίαση ΔΣ99-2099",
                "metadata": {"Ημερομηνία": "2099-06-15"},
                "sections": [
                    {"heading": "Θέμα 1", "body": "Δοκιμαστικό σώμα θέματος 1."},
                ],
                "decisions": [
                    {"number": 1, "text": "Δοκιμαστική απόφαση.", "vote": "Ομόφωνα"},
                ],
            },
            # write_draft_to_doc outputs → consumed by finalize
            "draft_doc_id": "debug-doc-id",
            "draft_doc_url": "https://example.invalid/doc/debug",
        }

    async def execute_step(self, step: WorkflowStep, context: dict[str, Any]) -> StepResult:
        """Route to the appropriate step handler."""
        handler = getattr(self, f"_step_{step.name}", None)
        if not handler:
            return StepResult(success=False, message=f"No handler for step: {step.name}")
        return await handler(context)

    # ── Step 1: Select sources ────────────────────────────────────────────────

    async def _step_select_sources(self, ctx: dict[str, Any]) -> StepResult:
        """Resolve the Google Doc and transcript for this meeting.

        Accepted context keys
        ---------------------
        meeting_ref : str (required)
            e.g. 'ΔΣ03-2026'
        source_doc_id : str (preferred)
            The Drive file ID chosen by the CLI.  When provided the workflow
            skips folder listing entirely - no index mismatch possible.
        source_doc_index : int (fallback)
            0-based index into the drafts folder listing.  Only used when
            *source_doc_id* is **not** provided.
        recording_index : int | None
            Which Zoom recording to use (-1 = skip, None = auto-match).
        transcript_path : str | None
            Local file path to a transcript (.vtt / .txt / .docx / .doc).
            Takes priority over Zoom recordings when provided.
        """
        meeting_ref: str = ctx.get("meeting_ref", "")
        if not meeting_ref:
            return StepResult(success=False, message="meeting_ref is required in context")

        try:
            meeting_number, meeting_year = _parse_meeting_ref(meeting_ref)
        except ValueError as e:
            return StepResult(success=False, message=str(e))

        # ── Resolve source doc ────────────────────────────────────────────
        source_doc_id: str = ctx.get("source_doc_id", "")
        source_doc_name: str = ""

        if source_doc_id:
            # CLI already resolved the doc - just read its content
            source_doc_name = ctx.get("source_doc_name", source_doc_id)
        else:
            # Fallback: list docs and pick by index or auto-match
            folder_id = settings.google.minutes_drafts_folder_id
            if not folder_id:
                return StepResult(
                    success=False,
                    message="google.minutes_drafts_folder_id not configured",
                )

            docs = self._google.list_docs_in_folder(folder_id)
            if not docs:
                return StepResult(success=False, message="No Google Docs found in minutes_drafts_folder")

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
            source_doc_id = source_doc["id"]
            source_doc_name = source_doc.get("name", source_doc_id)

        # Read SecGen notes from the selected doc
        secgen_notes: str = self._google.read_doc_content(source_doc_id)

        # ── Resolve transcript ────────────────────────────────────────────
        transcript_text = ""

        # Option A: local transcript file takes priority
        transcript_path: str = ctx.get("transcript_path", "")
        if transcript_path:
            try:
                transcript_text = parse_transcript(transcript_path)
                logger.info("Parsed local transcript: %s (%d chars)", transcript_path, len(transcript_text))
            except Exception as e:
                logger.warning("Could not parse transcript %s: %s", transcript_path, e)

        # Option B: Zoom recordings (only if no local transcript)
        if not transcript_text:
            recording_index = ctx.get("recording_index")

            # Skip Zoom entirely if recording_index == -1
            if recording_index != -1:
                try:
                    recordings = await self._zoom.list_recordings()
                except Exception as e:
                    logger.warning("Could not fetch Zoom recordings: %s", e)
                    recordings = []

                if recordings:
                    selected_recording = None

                    if recording_index is not None and recording_index >= 0:
                        if recording_index < len(recordings):
                            selected_recording = recordings[recording_index]
                    else:
                        # Auto-match by meeting_ref in topic
                        for rec in recordings:
                            topic = rec.get("topic", "")
                            if meeting_ref in topic:
                                selected_recording = rec
                                break
                        if selected_recording is None and recordings:
                            selected_recording = recordings[0]

                    if selected_recording:
                        meeting_id = str(selected_recording["id"])
                        try:
                            transcript = await self._zoom.get_transcript(meeting_id)
                            transcript_text = transcript or ""
                        except Exception as e:
                            logger.warning("Could not fetch transcript for meeting %s: %s", meeting_id, e)

        return StepResult(
            success=True,
            message=f"Sources selected: doc '{source_doc_name}', transcript length={len(transcript_text)}",
            data={
                "secgen_notes": secgen_notes,
                "zoom_transcript": transcript_text,
                "source_doc_id": source_doc_id,
                "source_doc_name": source_doc_name,
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
        """Write the drafted minutes back to the source Google Doc with formatting."""
        draft_json: dict = ctx.get("draft_json", {})
        source_doc_id: str = ctx.get("source_doc_id", "")
        meeting_ref: str = ctx.get("meeting_ref", "")

        if not source_doc_id:
            return StepResult(success=False, message="source_doc_id missing from context")

        # Build structured sections (title, headings, body) and write with styles
        sections = _format_draft_as_sections(draft_json)
        self._google.write_structured_doc(source_doc_id, sections)

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
        recipients = [m.email for m in board_members]

        if test_mode:
            test_email = settings.testing.test_email
            if test_email:
                recipients = [test_email]
            else:
                logger.info("test_mode=True and no test_email set - skipping email")
                return StepResult(
                    success=True,
                    message="Email skipped (test_mode, no test_email configured)",
                    data={"shared": False, "shared_at": datetime.utcnow().isoformat()},
                )

        if recipients:
            subject = f"Πρόχειρα Πρακτικά - Συνεδρίαση {meeting_ref}"
            body_html = render_email(
                "minutes_share",
                kicker=f"Πρακτικά - Συνεδρίαση {meeting_ref}",
                title="Πρόχειρα πρακτικά<br/>για σχόλια.",
                header_ref="ΔΣ - ΠΡΑΚΤΙΚΑ",
                footer_note="Εσωτερική επικοινωνία - Διοικητικό Συμβούλιο",
                draft_doc_url=draft_doc_url,
            )
            self.gmail.send_email(
                to=recipients,
                subject=subject,
                body_html=body_html,
                workflow=self.name,
            )
            logger.info("Draft minutes shared with %d board member(s)", len(recipients))
        else:
            logger.info("No board members configured - skipping email share")

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
        Πρωτόκολλο registration, and Google Doc rename - so nothing permanent
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
                logger.warning("Could not embed signatures: %s - using unsigned PDF", e)
                final_pdf_path = pdf_path
        else:
            logger.info("No signature files found in %s - skipping signing", sig_dir)
            final_pdf_path = pdf_path

        # Fetch next protocol number from the SharePoint Excel registry
        protocol_number = ""
        if settings.ms_client_id and settings.ms_tenant_id:
            try:
                protocol_number = await self.onedrive.get_next_protocol_number(meeting_year)
            except Exception as e:
                logger.warning("Could not fetch next protocol number from OneDrive: %s", e)
                protocol_number = f"{meeting_year}_001"
        else:
            protocol_number = f"{meeting_year}_001"

        # ── Persistent side-effects (skipped in test_mode) ─────────────────
        archive_info: dict[str, Any] = {}

        if test_mode:
            logger.info("test_mode - skipping OneDrive archive, Πρωτόκολλο write, and Doc rename")
            archive_info = {"status": "skipped", "reason": "test_mode"}
        else:
            # Upload to SharePoint (skip if MS creds not configured)
            if settings.ms_client_id and settings.ms_tenant_id:
                try:
                    remote_folder = f"{settings.onedrive.yearly_subfolder}/{meeting_year}"
                    result = await self.onedrive.upload_file(
                        local_path=final_pdf_path,
                        remote_folder=remote_folder,
                        filename=f"[{protocol_number}] Πρακτικά - Συνεδρίαση {meeting_ref}.pdf",
                        workflow=self.name,
                    )
                    archive_info = {"file_id": result.get("id"), "status": "archived"}
                except Exception as e:
                    logger.warning("OneDrive upload failed: %s - skipping archive", e)
                    archive_info = {"status": "skipped", "reason": str(e)}
            else:
                logger.info("MS credentials not configured - skipping OneDrive archive")
                archive_info = {"status": "skipped", "reason": "MS credentials not configured"}

            # Register in the Excel πρωτόκολλο registry on SharePoint
            if settings.ms_client_id and settings.ms_tenant_id:
                try:
                    today_str = date.today().isoformat()
                    key_points = "; ".join(
                        d.get("text", "")[:80] for d in draft_json.get("decisions", [])
                    )
                    await self.onedrive.append_protocol_row(
                        protocol_id=protocol_number,
                        date_str=today_str,
                        title=f"Πρακτικά - Συνεδρίαση {meeting_ref}",
                        main_points=key_points,
                        tags="Διοικητικά, Πρακτικά",
                    )
                except Exception as e:
                    logger.warning("Could not append row to protocol registry: %s", e)

            # Rename Google Doc to final title
            final_title = f"[Τελικό] Πρακτικά - Συνεδρίαση {meeting_ref}"
            self._google.rename_file(draft_doc_id, final_title)

        # Publish board.minutes.shared event so Discord can post the link
        # in the agenda thread. Use a best-effort Drive URL: prefer the
        # OneDrive share link (if archiving succeeded), else skip gracefully.
        drive_url = archive_info.get("share_link") or archive_info.get("file_id") or ""
        if not drive_url and not test_mode:
            # Nothing useful to post, but we still publish so the platform
            # bridge can at least record that minutes were finalized.
            drive_url = f"minutes_{meeting_ref}.pdf (see OneDrive)"

        await _publish_board_minutes_shared(
            meeting_ref=meeting_ref,
            drive_url=drive_url,
            doc_id=draft_doc_id,
        )

        # ── Post-cycle housekeeping: reset the agenda sheet (Model A) ────────
        # This frees the next cycle: bumps D5 to the next meeting_ref, clears
        # the agenda items, unchecks the three approval boxes (D16/D17/D18),
        # clears Z1 (the Apps Script idempotency cell), and removes the
        # script-owned protection.  Non-fatal - minutes is the official
        # artifact; reset failure should not fail the workflow.
        agenda_sheet_id = settings.google.agenda_sheet_id
        if agenda_sheet_id:
            try:
                reset_info = self._google.reset_agenda_sheet(agenda_sheet_id)
                logger.info(
                    "Agenda sheet reset: %s → %s",
                    reset_info.get("old_meeting_ref"),
                    reset_info.get("new_meeting_ref"),
                )
            except Exception as reset_err:
                logger.warning(
                    "Agenda sheet reset failed (non-fatal): %s",
                    reset_err,
                )
        else:
            logger.info(
                "No google.agenda_sheet_id configured - skipping agenda reset"
            )

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
            logger.warning("Could not read Βιβλίο Αποφάσεων sheet: %s - starting at 1", e)
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
            logger.info("test_mode - skipping Βιβλίο Αποφάσεων write (would write %d decisions)", len(rows))
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


# ── Module-level helpers ──────────────────────────────────────────────────────

async def _publish_board_minutes_shared(
    *,
    meeting_ref: str,
    drive_url: str,
    doc_id: str,
) -> None:
    """Publish EVENT_BOARD_MINUTES_SHARED to the event bus (non-fatal).

    The meeting_id uses the same convention as the invitation workflow:
    ``board_meeting:ΔΣXX-YYYY``.  Built directly from meeting_ref so the
    Discord platform_bridge handler keys on the same id the invitation
    workflow published when scheduling the meeting.
    """
    try:
        from src.core.event_bus import bus
        from src.core.events import EVENT_BOARD_MINUTES_SHARED, BoardMinutesSharedPayload

        meeting_id = _meeting_id_from_ref(meeting_ref)

        await bus.publish(
            EVENT_BOARD_MINUTES_SHARED,
            BoardMinutesSharedPayload(
                meeting_id=meeting_id,
                drive_url=drive_url or "",
                doc_id=doc_id or "",
            ),
        )
        logger.info("_publish_board_minutes_shared: published %s", meeting_id)
    except Exception as exc:
        logger.warning("Bus publish board.minutes.shared failed (non-fatal): %s", exc)


def _meeting_id_from_ref(meeting_ref: str) -> str:
    """Convert a meeting_ref like 'ΔΣ03-2026' to a meeting_id string.

    Aligned with the board_meeting_invitation workflow (2026-05-24 refactor):
    both workflows now key on ``board_meeting:ΔΣXX-YYYY``, so platform_bridge
    handlers and pending_actions rows match across invitation + minutes.
    """
    return f"board_meeting:{meeting_ref}"
