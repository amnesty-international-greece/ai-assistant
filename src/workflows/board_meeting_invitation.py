"""Board meeting invitation workflow — full Phase 1 implementation."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from src.config import settings
from src.core.workflow import BaseWorkflow, WorkflowStep, StepResult
from src.integrations.google_drive import GoogleClient
from src.integrations.zoom import ZoomClient
from src.integrations.onedrive import OneDriveClient
from src.integrations.brevo import BrevoClient

logger = logging.getLogger(__name__)


class BoardMeetingInvitationWorkflow(BaseWorkflow):
    """Complete board meeting invitation flow:
    Read agenda → Schedule Zoom → Claude drafts → Generate PDF →
    [APPROVAL] → Archive → Send newsletter → Schedule reminder
    """

    def __init__(self, actor: str = "secgen"):
        self._google = GoogleClient()
        self._zoom = ZoomClient()
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
        return "board_meeting_invitation"

    def define_steps(self) -> list[WorkflowStep]:
        return [
            WorkflowStep("read_agenda", "Read agenda from Google Sheets"),
            WorkflowStep("schedule_zoom", "Schedule Zoom meeting"),
            WorkflowStep("draft_invitation", "Draft invitation with Claude"),
            WorkflowStep("generate_pdf", "Generate PDF document"),
            WorkflowStep("approval", "Review and approve draft", requires_approval=True),
            WorkflowStep("archive", "Archive PDF to OneDrive"),
            WorkflowStep("send_newsletter_test", "Create campaign and send test email"),
            WorkflowStep("confirm_newsletter", "Confirm and send live newsletter", requires_approval=True),
            WorkflowStep("schedule_reminder", "Meeting reminders (Zoom-native)"),
        ]

    async def execute_step(self, step: WorkflowStep, context: dict[str, Any]) -> StepResult:
        """Route to the appropriate step handler."""
        handler = getattr(self, f"_step_{step.name}", None)
        if not handler:
            return StepResult(success=False, message=f"No handler for step: {step.name}")
        return await handler(context)

    async def rollback(self, ctx: dict[str, Any]) -> None:
        """Undo any side effects created during the workflow.

        Called when the user rejects the draft or explicitly requests cleanup.
        Cancels the Zoom meeting and deletes the local PDF if they exist.
        """
        # Cancel Zoom meeting
        meeting_id = ctx.get("zoom_meeting_id")
        if meeting_id:
            try:
                await self._zoom.delete_meeting(meeting_id, workflow=self.name)
                logger.info("Rollback: Zoom meeting %s cancelled", meeting_id)
            except Exception as e:
                logger.warning("Rollback: could not cancel Zoom meeting %s: %s", meeting_id, e)

        # Delete local PDF
        pdf_path = ctx.get("pdf_path")
        if pdf_path:
            try:
                p = Path(pdf_path)
                if p.exists():
                    p.unlink()
                    logger.info("Rollback: deleted PDF %s", pdf_path)
            except Exception as e:
                logger.warning("Rollback: could not delete PDF %s: %s", pdf_path, e)

    async def _step_read_agenda(self, ctx: dict[str, Any]) -> StepResult:
        """Read meeting agenda data from Google Sheets.

        Layout is detected dynamically by scanning column C for Greek labels,
        so both the old (D7=date, D9=time, D11=trigger) and new template
        (D7=type, D9=date, D11=time, D13=location, D15=trigger) are handled
        transparently.

        Fixed columns regardless of layout version:
          D5  — ΑΡΙΘΜΟΣ ΣΥΝΕΔΡΙΑΣΗΣ  (e.g. "ΔΣ04-2026")
          H7: — ΘΕΜΑ                  (agenda items, open-ended rows)
        """
        if ctx.get("_skip_read_agenda") and ctx.get("agenda_items"):
            return StepResult(
                success=True,
                data={},
                message=f"Using manually provided agenda ({len(ctx['agenda_items'])} items)",
            )
        try:
            self._google.authenticate()
            sheet_id = ctx.get("agenda_sheet_id") or settings.google.agenda_sheet_id
            if not sheet_id:
                return StepResult(
                    success=False,
                    message="No agenda sheet ID configured. Set google.agenda_sheet_id in config.yaml.",
                )

            # ── 1. List all tabs ──────────────────────────────────────────────
            tabs = self._google.list_sheet_tabs(sheet_id)

            # ── 2. Find meeting tabs by name pattern ΔΣxx-YYYY ──────────────
            import re
            today = datetime.now().date()
            candidates = []

            for tab in tabs:
                tab_title = tab["title"]
                m = re.search(r"(\d+)-(\d{4})$", tab_title)
                if not m:
                    continue   # template, notes tab, etc.

                seq  = int(m.group(1))
                year = int(m.group(2))

                if year < today.year:
                    continue

                rows = self._google.read_sheet(
                    sheet_id,
                    f"'{tab_title}'!A1:K200",
                    value_render_option="UNFORMATTED_VALUE",
                )

                fields = _scan_form_labels(rows)

                raw_number = fields.get("ΑΡΙΘΜΟΣ ΣΥΝΕΔΡΙΑΣΗΣ", ("", 5))[0]
                raw_date,  date_row  = fields.get("ΗΜΕΡΟΜΗΝΙΑ",         ("", 7))
                raw_time,  time_row  = fields.get("ΩΡΑ ΕΝΑΡΞΗΣ",        ("", 9))
                raw_type,  _         = fields.get("ΤΥΠΟΣ",               ("", None))
                raw_loc,   _         = fields.get("ΤΟΠΟΘΕΣΙΑ",           ("", None))
                _,         trigger_row = fields.get("ΠΡΟΣΚΛΗΣΗ",         ("", 11))

                parsed_date = _parse_sheet_date(raw_date)
                if parsed_date is not None and parsed_date < today:
                    continue

                parsed_time = _parse_sheet_time(raw_time)

                candidates.append({
                    "tab":          tab_title,
                    "seq":          seq,
                    "year":         year,
                    "raw_number":   raw_number,
                    "raw_date":     raw_date,
                    "date_row":     date_row,
                    "parsed_date":  parsed_date,
                    "parsed_time":  parsed_time,
                    "meeting_type": raw_type,
                    "location":     raw_loc,
                    "trigger_row":  trigger_row,
                    "rows":         rows,
                })

            if not candidates:
                return StepResult(
                    success=False,
                    message="No board meeting tabs found for the current or future year. "
                            "Tabs must be named like 'ΔΣ04-2026'.",
                )

            # ── 3. Sort: real future dates first (nearest), then highest seq ──
            candidates.sort(key=lambda c: (
                c["parsed_date"] or datetime(c["year"], 12, 31).date(),
                -c["seq"] if c["parsed_date"] is None else c["seq"],
            ))
            chosen = candidates[0]

            # ── 4. Confirm with user (unless tab was pre-specified) ───────────
            forced_tab = ctx.get("agenda_tab")
            if forced_tab:
                match = next((c for c in candidates if c["tab"] == forced_tab), None)
                if match:
                    chosen = match
                else:
                    return StepResult(
                        success=False,
                        message=f"Tab '{forced_tab}' not found or has no future date.",
                    )
            elif len(candidates) > 1:
                print("\n  Found multiple upcoming meeting tabs:")
                for i, c in enumerate(candidates):
                    marker = " [auto-selected]" if i == 0 else ""
                    date_str = str(c["parsed_date"]) if c["parsed_date"] else "no date set"
                    time_str = f" {c['parsed_time']}" if c["parsed_time"] else ""
                    type_str = f" [{c['meeting_type']}]" if c["meeting_type"] else ""
                    print(f"    [{i + 1}] {c['tab']} —{date_str}{time_str}{type_str}{marker}")
                print()
                choice = input("  Use auto-selected tab? [Enter to confirm / type number to switch]: ").strip()
                if choice.isdigit():
                    idx = int(choice) - 1
                    if 0 <= idx < len(candidates):
                        chosen = candidates[idx]
                    else:
                        return StepResult(success=False, message="Invalid tab selection.")
            else:
                date_str = str(chosen["parsed_date"]) if chosen["parsed_date"] else "no date set"
                time_str = f" {chosen['parsed_time']}" if chosen["parsed_time"] else ""
                print(f"\n  Using tab: '{chosen['tab']}' ({date_str}{time_str})")

            # ── 5. Extract data from chosen tab ───────────────────────────────
            rows = chosen["rows"]

            def cell(r: int, c: int) -> str:
                try:
                    return str(rows[r - 1][c - 1]).strip()
                except IndexError:
                    return ""

            raw_number = chosen["raw_number"]

            # Meeting number: prefer tab name digits (most reliable)
            num_match = re.search(r"(\d+)-(\d{4})", chosen["tab"])
            if num_match:
                meeting_number = str(int(num_match.group(1)))
            else:
                nm = re.search(r"\d+", raw_number)
                meeting_number = str(int(nm.group(0))) if nm else raw_number

            meeting_date = str(chosen["parsed_date"]) if chosen["parsed_date"] else ""
            meeting_time = chosen["parsed_time"]

            agenda_items = []
            for row_idx in range(7, len(rows) + 1):
                item = cell(row_idx, 8)   # column H is always the agenda topic
                if item and item.lower() not in ("none", "nan", ""):
                    agenda_items.append(item)

            # CLI-provided values take precedence
            final_number = ctx.get("meeting_number") or meeting_number
            final_date   = ctx.get("meeting_date")   or meeting_date
            final_time   = ctx.get("meeting_time")   or meeting_time

            # ── 6. Date sanity check ──────────────────────────────────────────
            from datetime import date as _date
            if not final_date:
                date_col = f"D{chosen['date_row']}" if chosen["date_row"] else "the date cell"
                return StepResult(
                    success=False,
                    message=(
                        f"Tab '{chosen['tab']}' has no meeting date set "
                        f"({date_col} currently contains: '{chosen['raw_date']}'). "
                        f"Please fill in the date (e.g. 14/05/2026), "
                        f"or pass --date YYYY-MM-DD on the command line."
                    ),
                )
            meeting_date_obj = _date.fromisoformat(final_date)
            days_until = (meeting_date_obj - today).days
            max_advance = settings.workflows.board_meeting.max_advance_days

            if days_until < 0:
                date_col = f"D{chosen['date_row']}" if chosen["date_row"] else "the date cell"
                return StepResult(
                    success=False,
                    message=(
                        f"Meeting date {final_date} is in the past "
                        f"({abs(days_until)} days ago). "
                        f"Please update {date_col} in tab '{chosen['tab']}'."
                    ),
                )

            if days_until > max_advance:
                print(
                    f"\n  WARNING: Meeting is {days_until} days away "
                    f"(policy maximum: {max_advance} days)."
                )
                confirm = input("  Proceed anyway? [y/n]: ").strip().lower()
                if confirm not in ("y", "yes"):
                    return StepResult(
                        success=False,
                        message=f"Cancelled — meeting date {final_date} is too far in advance.",
                    )

            return StepResult(
                success=True,
                data={
                    "meeting_number":  final_number,
                    "meeting_date":    final_date,
                    "meeting_time":    final_time,
                    "meeting_type":    chosen["meeting_type"],   # ΤΑΚΤΙΚΗ / ΕΚΤΑΚΤΗ
                    "location":        chosen["location"],       # ΔΙΑΔΙΚΤΥΑΚΑ / physical
                    "agenda_items":    agenda_items,
                    "raw_meeting_id":  raw_number,
                    "agenda_tab":      chosen["tab"],
                    "trigger_row":     chosen["trigger_row"],    # row of ΠΡΟΣΚΛΗΣΗ cell
                },
                message=(
                    f"Read agenda from tab '{chosen['tab']}': "
                    f"Meeting #{final_number} on {final_date} at {final_time} "
                    f"({chosen['meeting_type'] or 'ΤΑΚΤΙΚΗ'}, {chosen['location'] or 'ΔΙΑΔΙΚΤΥΑΚΑ'}) "
                    f"— {len(agenda_items)} items"
                ),
            )
        except Exception as e:
            return StepResult(success=False, message=f"Failed to read agenda: {e}")

    async def _step_schedule_zoom(self, ctx: dict[str, Any]) -> StepResult:
        """Schedule a Zoom meeting for the board meeting."""
        try:
            meeting_date = ctx.get("meeting_date", "")
            meeting_time = ctx.get("meeting_time", "")
            meeting_number = ctx.get("meeting_number", "")

            if not meeting_date or not meeting_time:
                return StepResult(
                    success=False,
                    message="Meeting date and time are required to schedule Zoom",
                )

            # Construct ISO datetime
            start_time = f"{meeting_date}T{meeting_time}:00"

            # Topic: use the full meeting reference (e.g. "Συνεδρίαση ΔΣ04-2026")
            raw_id  = ctx.get("raw_meeting_id", "")
            year    = meeting_date[:4] if len(meeting_date) >= 4 else "ΧΧΧΧ"
            seq_str = meeting_number.zfill(2) if meeting_number.isdigit() else meeting_number
            meeting_ref = raw_id or f"ΔΣ{seq_str}-{year}"
            topic = f"Συνεδρίαση {meeting_ref}"

            agenda_items = ctx.get("agenda_items", [])
            agenda_text = "\n".join(f"{i+1}. {item}" for i, item in enumerate(agenda_items))

            result = await self._zoom.schedule_meeting(
                topic=topic,
                start_time=start_time,
                agenda=agenda_text,
                workflow=self.name,
            )

            meeting_id = str(result.get("id", ""))

            # ── Pre-register board members ────────────────────────────────────
            # Board members are registered in advance so Zoom emails each of
            # them a personal join link — they never see the registration form.
            # Other participants (regular members, observers) use the public
            # registration URL (join_url) and are auto-approved.
            board_members = settings.workflows.board_meeting.board_members or []
            board_join_urls: dict[str, str] = {}
            if board_members and meeting_id:
                try:
                    reg_results = await self._zoom.add_registrants(
                        meeting_id=meeting_id,
                        registrants=[m.model_dump() for m in board_members],
                        workflow=self.name,
                    )
                    board_join_urls = {r["email"]: r["join_url"] for r in reg_results}
                except Exception as reg_err:
                    logger.warning("Could not pre-register board members: %s", reg_err)

            return StepResult(
                success=True,
                data={
                    "zoom_join_url":    result.get("join_url", ""),
                    "zoom_meeting_id":  meeting_id,
                    "zoom_passcode":    result.get("password", ""),
                    "board_join_urls":  board_join_urls,  # email → personal join URL
                },
                message=f"Zoom meeting scheduled: {result.get('join_url', '')}",
            )
        except Exception as e:
            return StepResult(success=False, message=f"Failed to schedule Zoom: {e}")

    async def _step_draft_invitation(self, ctx: dict[str, Any]) -> StepResult:
        """Prepare placeholder fill-data for the Google Docs invitation template.

        All formatting (letterhead, table, signatures) lives in the template;
        we only provide the variable values.

        Template placeholders (exact strings in the document):
          [ΗΜΕΡΟΜΗΝΙΑ]                   → Greek long-form date (appears twice)
          Αρ. Πρωτ.: [ΑΡΙΘΜΟΣ ΠΡΩΤΟΚΟΛΛΟΥ] → removed if no valid protocol number
          [ΤΥΠΟΣ]                         → ΤΑΚΤΙΚΗΣ / ΕΚΤΑΚΤΗΣ
          [ΩΡΑ ΕΝΑΡΞΗΣ]                   → HH:MM
          [ΤΟΠΟΘΕΣΙΑ]                     → location phrase
          [ΘΕΜΑ]  (numbered or single)    → agenda items (handled in fill step)
        """
        try:
            meeting_date   = ctx.get("meeting_date", "")
            meeting_time   = ctx.get("meeting_time", "")
            meeting_number = ctx.get("meeting_number", "")
            meeting_type   = ctx.get("meeting_type", "ΤΑΚΤΙΚΗ")
            location       = ctx.get("location", "ΔΙΑΔΙΚΤΥΑΚΑ")
            agenda_items   = ctx.get("agenda_items", [])
            zoom_url       = ctx.get("zoom_join_url", "")
            protocol_number = ctx.get("protocol_number", "")

            # ── Greek date ────────────────────────────────────────────────────
            greek_date = _format_greek_date(meeting_date)

            # ── Meeting type (genitive form for the title) ────────────────────
            type_genitive = _meeting_type_genitive(meeting_type)

            # ── Location phrase ───────────────────────────────────────────────
            _OFFICE_ADDRESS = "στη διεύθυνση Σίνα 30, 2ος όροφος"
            loc_upper = (location or "ΔΙΑΔΙΚΤΥΑΚΑ").strip().upper()

            if loc_upper == "ΔΙΑ ΖΩΣΗΣ":
                location_phrase = f"δια ζώσης στο Γραφείο του Τμήματος, {_OFFICE_ADDRESS}"
            elif loc_upper == "ΥΒΡΙΔΙΚΑ":
                zoom_part = "[ZOOM_PLACEHOLDER]" if zoom_url else "Zoom"
                location_phrase = (
                    f"υβριδικά, στο Γραφείο του Τμήματος {_OFFICE_ADDRESS}, "
                    f"και διαδικτυακά μέσω της πλατφόρμας {zoom_part}"
                )
            else:  # ΔΙΑΔΙΚΤΥΑΚΑ (default)
                zoom_part = "[ZOOM_PLACEHOLDER]" if zoom_url else "Zoom"
                location_phrase = f"διαδικτυακά μέσω της πλατφόρμας {zoom_part}"

            # ── Protocol number ───────────────────────────────────────────────
            import re as _re
            replacements: dict = {}
            if protocol_number and _re.match(r"^\d{4}-\d+$", protocol_number.strip()):
                replacements["[ΑΡΙΘΜΟΣ ΠΡΩΤΟΚΟΛΛΟΥ]"] = protocol_number.strip()
            else:
                # Delete the whole "Αρ. Πρωτ.: ..." paragraph so no blank line remains
                replacements["_delete_paragraphs_"] = ["Αρ. Πρωτ.: [ΑΡΙΘΜΟΣ ΠΡΩΤΟΚΟΛΛΟΥ]"]

            replacements.update({
                "[ΗΜΕΡΟΜΗΝΙΑ]":  greek_date,
                "[ΤΥΠΟΣ]":       type_genitive,
                "[ΩΡΑ ΕΝΑΡΞΗΣ]": meeting_time or "ΩΡΑ ΤΒΔ",
                "[ΤΟΠΟΘΕΣΙΑ]":   location_phrase,
                # _agenda_items_ is a special list key consumed by fill_document_template
                # via character-index replacement (not replaceAllText)
                "_agenda_items_": agenda_items if agenda_items else ["(κατόπιν ανακοίνωσης)"],
            })

            return StepResult(
                success=True,
                data={
                    "invitation_replacements": replacements,
                    "invitation_zoom_url": zoom_url,  # forwarded to generate_pdf
                },
                message=(
                    f"Invitation prepared for {greek_date} at {meeting_time} "
                    f"({type_genitive}) — {len(agenda_items)} agenda items"
                ),
            )
        except Exception as e:
            return StepResult(success=False, message=f"Failed to prepare invitation: {e}")

    async def _step_generate_pdf(self, ctx: dict[str, Any]) -> StepResult:
        """Copy the Google Docs invitation template, fill placeholders, export as PDF.

        Filename convention:
          With protocol number    : "[YYYY-N] Πρόσκληση - Συνεδρίαση ΔΣxx-YYYY.pdf"
          Without protocol number : "Πρόσκληση - Συνεδρίαση ΔΣxx-YYYY.pdf"

        Flow:
          1. Copy the template doc in Drive (gives us an editable working copy)
          2. Replace all placeholders via Docs API batchUpdate
          3. Export the filled doc as PDF to data/output/
          4. Trash the working copy (no longer needed once we have the PDF)
        """
        try:
            replacements = ctx.get("invitation_replacements")
            if not replacements:
                return StepResult(
                    success=False,
                    message="No invitation replacements found — did draft_invitation run?",
                )

            template_id = settings.google.invitation_template_id
            if not template_id:
                return StepResult(
                    success=False,
                    message="google.invitation_template_id not set in config.yaml",
                )

            meeting_number  = ctx.get("meeting_number", "XX")
            meeting_date    = ctx.get("meeting_date", "unknown")
            protocol_number = ctx.get("protocol_number", "")
            zoom_url        = ctx.get("invitation_zoom_url") or ctx.get("zoom_join_url", "")

            # ── Build meeting reference from raw sheet value if available ─────
            raw_id  = ctx.get("raw_meeting_id", "")   # e.g. "ΔΣ04-2026"
            year    = meeting_date[:4] if len(meeting_date) >= 4 else "ΧΧΧΧ"
            seq_str = meeting_number.zfill(2) if meeting_number.isdigit() else meeting_number
            meeting_ref = raw_id or f"ΔΣ{seq_str}-{year}"

            # ── Filename & doc title ──────────────────────────────────────────
            import re as _re
            doc_base  = f"Πρόσκληση - Συνεδρίαση {meeting_ref}"
            if protocol_number and _re.match(r"^\d{4}-\d+$", protocol_number.strip()):
                filename = f"[{protocol_number}] {doc_base}.pdf"
            else:
                filename = f"{doc_base}.pdf"
            # Sanitise for file system (remove characters invalid on Windows)
            safe_filename = filename.replace(":", "-").replace("/", "-")

            output_path = Path("data") / "output" / safe_filename
            output_path.parent.mkdir(parents=True, exist_ok=True)

            self._google.authenticate()

            # 1. Copy template
            working_doc_id = self._google.copy_document(template_id, doc_base)

            try:
                # 2. Fill placeholders (handles [ΘΕΜΑ] logic + Zoom hyperlink)
                self._google.fill_document_template(
                    working_doc_id, replacements, zoom_url=zoom_url
                )

                # 3. Export PDF
                self._google.export_doc_as_pdf(working_doc_id, output_path)

            finally:
                # 4. Trash the working copy regardless of success/failure
                try:
                    self._google.delete_file(working_doc_id, workflow=self.name)
                except Exception as cleanup_err:
                    logger.warning("Could not trash working doc %s: %s", working_doc_id, cleanup_err)

            return StepResult(
                success=True,
                data={
                    "pdf_path": str(output_path),
                    "pdf_filename": safe_filename,
                },
                message=f"PDF generated from template: {output_path}",
            )
        except Exception as e:
            return StepResult(success=False, message=f"Failed to generate PDF: {e}")

    async def _step_approval(self, ctx: dict[str, Any]) -> StepResult:
        """Approval gate — execution reaches here only after user approves."""
        return StepResult(
            success=True,
            data={"approved": True, "approved_by": self.actor},
            message="Draft approved",
        )

    async def _step_archive(self, ctx: dict[str, Any]) -> StepResult:
        """Archive the approved PDF to OneDrive."""
        if ctx.get("test_mode"):
            return StepResult(success=True, data={"archive_skipped": True}, message="[TEST] Archive skipped")
        if not settings.ms_client_id or not settings.ms_tenant_id:
            return StepResult(
                success=True,
                data={"archive_skipped": True},
                message="Archive skipped — OneDrive not configured (set MS_CLIENT_ID / MS_TENANT_ID in .env)",
            )
        try:
            pdf_path = Path(ctx.get("pdf_path", ""))
            if not pdf_path.exists():
                return StepResult(success=False, message=f"PDF not found at {pdf_path}")

            meeting_date = ctx.get("meeting_date", "unknown")
            year = meeting_date[:4] if len(meeting_date) >= 4 else "unknown"

            # Upload to OneDrive: /Archive/{year}/DS/Proskliseis/
            remote_folder = f"{year}/DS/Proskliseis"
            result = await self.onedrive.upload_file(
                local_path=pdf_path,
                remote_folder=remote_folder,
                workflow=self.name,
            )

            file_id = result.get("id", "")
            share_link = ""
            if file_id:
                try:
                    share_link = await self.onedrive.get_share_link(file_id)
                except Exception:
                    logger.warning("Could not create share link for archived file")

            return StepResult(
                success=True,
                data={
                    "archive_file_id": file_id,
                    "archive_share_link": share_link,
                },
                message=f"PDF archived to OneDrive: {remote_folder}",
            )
        except Exception as e:
            return StepResult(success=False, message=f"Failed to archive PDF: {e}")

    def _build_newsletter_params(self, ctx: dict[str, Any]) -> tuple[dict, str, str, list[int]]:
        """Shared helper: build template_params, subject, campaign_name, list_ids."""
        meeting_number = ctx.get("meeting_number", "")
        meeting_date   = ctx.get("meeting_date", "")
        meeting_time   = ctx.get("meeting_time", "")
        meeting_type   = ctx.get("meeting_type", "ΤΑΚΤΙΚΗ")
        zoom_link      = ctx.get("zoom_join_url", "")
        raw_id         = ctx.get("raw_meeting_id", "")
        year           = meeting_date[:4] if len(meeting_date) >= 4 else "ΧΧΧΧ"
        seq_str        = meeting_number.zfill(2) if meeting_number.isdigit() else meeting_number
        meeting_ref    = raw_id or f"ΔΣ{seq_str}-{year}"

        _GREEK_MONTHS = {
            1: "Ιανουαρίου", 2: "Φεβρουαρίου", 3: "Μαρτίου",    4: "Απριλίου",
            5: "Μαΐου",      6: "Ιουνίου",     7: "Ιουλίου",     8: "Αυγούστου",
            9: "Σεπτεμβρίου",10: "Οκτωβρίου", 11: "Νοεμβρίου",  12: "Δεκεμβρίου",
        }
        try:
            from datetime import date as _date
            dt = _date.fromisoformat(meeting_date)
            greek_date = f"{dt.day} {_GREEK_MONTHS[dt.month]} {dt.year}"
        except (ValueError, KeyError):
            greek_date = meeting_date

        type_lower = "έκτακτη" if "ΕΚΤΑΚΤΗ" in meeting_type.upper() else "τακτική"

        agenda_items = ctx.get("agenda_items", [])
        if agenda_items:
            items_html = "".join(
                f'<li style="margin-bottom:6px;">{item}</li>'
                for item in agenda_items
            )
            agenda_html = (
                '<ol style="margin:0;padding-left:22px;font-family:arial,helvetica,sans-serif;'
                'font-size:14px;color:#333333;line-height:1.8;">'
                f"{items_html}</ol>"
            )
        else:
            agenda_html = (
                '<p style="margin:0;font-family:arial,helvetica,sans-serif;'
                'font-size:14px;color:#666666;font-style:italic;">(κατόπιν ανακοίνωσης)</p>'
            )

        template_params = {
            "[ΣΥΝΕΔΡΙΑΣΗ]":       meeting_ref,
            "[ΤΥΠΟΣ]":            type_lower,
            "[ΗΜΕΡΟΜΗΝΙΑ]":       greek_date,
            "[ΩΡΑ]":              meeting_time,
            "[ΗΜΕΡΗΣΙΑ_ΔΙΑΤΑΞΗ]": agenda_html,
            "[ZOOM_LINK]":        zoom_link or "#",
        }
        subject       = f"Πρόσκληση — Συνεδρίαση {meeting_ref} ({meeting_date})"
        campaign_name = f"Πρόσκληση {meeting_ref}"
        list_ids      = ctx.get("brevo_list_ids") or settings.brevo.newsletter_list_ids or []

        return template_params, subject, campaign_name, list_ids

    async def _step_send_newsletter_test(self, ctx: dict[str, Any]) -> StepResult:
        """Create the Brevo campaign (saved as draft) and send a test email.

        Template placeholder mapping (template #234):
            [ΣΥΝΕΔΡΙΑΣΗ]       → meeting reference     (e.g. "ΔΣ04-2026")
            [ΤΥΠΟΣ]            → lowercase nominative  (e.g. "τακτική")
            [ΗΜΕΡΟΜΗΝΙΑ]       → Greek long-form date  (e.g. "14 Απριλίου 2026")
            [ΩΡΑ]              → meeting time           (e.g. "20:30")
            [ΗΜΕΡΗΣΙΑ_ΔΙΑΤΑΞΗ] → HTML <ol> of agenda items
            [ZOOM_LINK]        → actual zoom_join_url

        The campaign is always created first (Brevo keeps it as a draft).
        A test email is sent to testing.dry_run_email so the SecGen can review
        the rendered output before the live send is confirmed.
        """
        template_id = ctx.get("brevo_template_id") or settings.brevo.newsletter_template_id

        if not template_id:
            return StepResult(
                success=True,
                data={"newsletter_skipped": True},
                message="Newsletter skipped — set brevo.newsletter_template_id in config.yaml",
            )

        test_addr = settings.testing.dry_run_email
        template_params, subject, campaign_name, list_ids = self._build_newsletter_params(ctx)

        # Use a dummy list_id for campaign creation so the Brevo API accepts it
        # even when newsletter_list_ids is empty (testing mode).
        creation_list_ids = list_ids if list_ids else [1]

        try:
            result = await self.brevo.send_campaign(
                template_id=template_id,
                list_ids=creation_list_ids,
                subject=subject,
                params=template_params,
                campaign_name=campaign_name,
                test_emails=[test_addr] if test_addr else None,
                workflow=self.name,
            )
            campaign_id = result.get("campaign_id")
            msg = (
                f"Campaign created (id={campaign_id}) and test sent to {test_addr}"
                if test_addr
                else f"Campaign created (id={campaign_id}) — no dry_run_email set, skipping test send"
            )
            return StepResult(
                success=True,
                data={
                    "newsletter_campaign_id": campaign_id,
                    "newsletter_test_sent": bool(test_addr),
                    "newsletter_test_addr": test_addr or "",
                    "newsletter_list_ids": list_ids,
                },
                message=msg,
            )
        except Exception as e:
            logger.warning("Newsletter campaign creation/test failed (non-fatal): %s", e)
            return StepResult(
                success=True,
                data={"newsletter_skipped": True},
                message=f"Newsletter campaign failed (non-fatal): {e}",
            )

    async def _step_confirm_newsletter(self, ctx: dict[str, Any]) -> StepResult:
        """Approval gate: send the campaign live to all members.

        This step is reached only after the user confirms the test email looks
        good.  If newsletter_list_ids is empty (testing mode) the live send is
        skipped and the campaign stays as a Brevo draft.
        """
        campaign_id = ctx.get("newsletter_campaign_id")
        list_ids    = ctx.get("newsletter_list_ids") or []

        if ctx.get("newsletter_skipped"):
            return StepResult(
                success=True,
                data={"newsletter_sent": False},
                message="Newsletter was skipped in previous step — nothing to send",
            )

        if not campaign_id:
            return StepResult(
                success=True,
                data={"newsletter_sent": False},
                message="No campaign_id in context — newsletter will not be sent",
            )

        if not list_ids:
            return StepResult(
                success=True,
                data={"newsletter_sent": False},
                message="newsletter_list_ids is empty — campaign saved as draft in Brevo, not sent live",
            )

        try:
            await self.brevo.send_campaign_now(campaign_id, workflow=self.name)
            return StepResult(
                success=True,
                data={"newsletter_sent": True},
                message=f"Newsletter sent live (campaign {campaign_id}, lists {list_ids})",
            )
        except Exception as e:
            return StepResult(success=False, message=f"Failed to send newsletter live: {e}")

    async def _step_schedule_reminder(self, ctx: dict[str, Any]) -> StepResult:
        """Reminder placeholder — Zoom handles reminders natively.

        Zoom sends automatic reminder emails to registered participants
        based on account-level settings (Settings → Meeting → Email
        Notification in the Zoom web portal).  No custom reminder is needed.
        """
        if ctx.get("test_mode"):
            return StepResult(
                success=True,
                data={"reminder_skipped": True},
                message="[TEST] Reminder skipped — Zoom handles reminders natively",
            )

        return StepResult(
            success=True,
            data={"reminder_native": True},
            message="Reminder handled by Zoom — configure in Zoom Settings → Email Notification",
        )


def _scan_form_labels(rows: list) -> dict:
    """Scan column C for known Greek form labels and return {label: (D_value_str, row_number)}.

    Works for both the old template layout (date=D7, time=D9, trigger=D11) and
    the new layout (type=D7, date=D9, time=D11, location=D13, trigger=D15).
    D5 (meeting number) is always returned regardless of the C5 label.
    """
    _KNOWN_LABELS = {
        "ΑΡΙΘΜΟΣ ΣΥΝΕΔΡΙΑΣΗΣ",
        "ΤΥΠΟΣ",
        "ΗΜΕΡΟΜΗΝΙΑ",
        "ΩΡΑ ΕΝΑΡΞΗΣ",
        "ΤΟΠΟΘΕΣΙΑ",
        "ΠΡΟΣΚΛΗΣΗ",
    }

    result: dict = {}

    # D5 is always the meeting number
    try:
        result["ΑΡΙΘΜΟΣ ΣΥΝΕΔΡΙΑΣΗΣ"] = (str(rows[4][3]).strip(), 5)
    except IndexError:
        result["ΑΡΙΘΜΟΣ ΣΥΝΕΔΡΙΑΣΗΣ"] = ("", 5)

    for row_idx, row in enumerate(rows, 1):
        try:
            label = str(row[2]).strip().upper()   # column C (index 2)
        except IndexError:
            continue
        if label in _KNOWN_LABELS and label != "ΑΡΙΘΜΟΣ ΣΥΝΕΔΡΙΑΣΗΣ":
            try:
                d_val = str(row[3]).strip()       # column D (index 3)
            except IndexError:
                d_val = ""
            result[label] = (d_val, row_idx)

    return result


def _parse_sheet_date(raw: str) -> "date | None":
    """Parse a date value from Google Sheets (read with UNFORMATTED_VALUE) into a date object.

    With UNFORMATTED_VALUE, dates come back as numeric serial floats (days since
    1899-12-30).  Text cells that happen to contain ISO strings are also handled
    as a fallback.

    Treats any date on or before 2020-01-01 as a placeholder and returns None.
    """
    from datetime import date as _date, timedelta
    _SHEETS_EPOCH = _date(1899, 12, 30)
    _PLACEHOLDER_CUTOFF = _date(2020, 1, 1)

    if raw is None or str(raw).strip() in ("", "None", "nan"):
        return None

    raw_str = str(raw).strip()

    # ── Numeric serial (primary path for UNFORMATTED_VALUE reads) ────────────
    try:
        serial = float(raw_str)
        if serial > 0:
            result = _SHEETS_EPOCH + timedelta(days=int(serial))
            if result <= _PLACEHOLDER_CUTOFF:
                return None  # placeholder date (e.g. 01/01/2000)
            return result
    except (ValueError, TypeError):
        pass

    # ── String fallback (ISO or DD/MM/YYYY) ──────────────────────────────────
    try:
        if " " in raw_str and "-" in raw_str:
            raw_str = raw_str.split(" ")[0]
        if raw_str.count("-") == 2:
            return _date.fromisoformat(raw_str)
        if "/" in raw_str:
            parts = raw_str.split("/")
            if len(parts) == 3:
                d, m, y = parts
                return _date(int(y), int(m), int(d))
    except (ValueError, TypeError):
        pass

    return None


def _parse_sheet_time(raw: str) -> str:
    """Parse a time value from Google Sheets (UNFORMATTED_VALUE) into "HH:MM".

    With UNFORMATTED_VALUE, time-only cells come back as a decimal fraction of
    a day (e.g. 0.854166... = 20:30).  String values like "20:30" are also
    accepted as a fallback.

    Returns "" if the value cannot be parsed or represents midnight (00:00).
    """
    if raw is None or str(raw).strip() in ("", "None", "nan"):
        return ""

    raw_str = str(raw).strip()

    # ── Fractional day (primary path for UNFORMATTED_VALUE reads) ────────────
    try:
        frac = float(raw_str)
        if 0.0 < frac < 1.0:
            total_minutes = round(frac * 24 * 60)
            h, m = divmod(total_minutes, 60)
            time_str = f"{h:02d}:{m:02d}"
            return "" if time_str == "00:00" else time_str
    except (ValueError, TypeError):
        pass

    # ── String fallback ("HH:MM" or "HH:MM:SS") ──────────────────────────────
    if ":" in raw_str:
        parts = raw_str.split(":")
        try:
            h, m = int(parts[0]), int(parts[1])
            time_str = f"{h:02d}:{m:02d}"
            return "" if time_str == "00:00" else time_str
        except (ValueError, IndexError):
            pass

    return ""


_GREEK_MONTHS = [
    "", "Ιανουαρίου", "Φεβρουαρίου", "Μαρτίου", "Απριλίου", "Μαΐου", "Ιουνίου",
    "Ιουλίου", "Αυγούστου", "Σεπτεμβρίου", "Οκτωβρίου", "Νοεμβρίου", "Δεκεμβρίου",
]


def _format_greek_date(iso_date: str) -> str:
    """Convert 'YYYY-MM-DD' to Greek long-form date, e.g. '14 Απριλίου 2026'.

    Returns the original string unchanged if it cannot be parsed.
    """
    try:
        from datetime import date as _date
        d = _date.fromisoformat(iso_date)
        return f"{d.day} {_GREEK_MONTHS[d.month]} {d.year}"
    except (ValueError, TypeError, IndexError):
        return iso_date


def _meeting_type_genitive(meeting_type: str) -> str:
    """Return the genitive form of the meeting type for use in the title.

    ΤΑΚΤΙΚΗ  → ΤΑΚΤΙΚΗΣ
    ΕΚΤΑΚΤΗ  → ΕΚΤΑΚΤΗΣ
    Anything else is returned uppercased as-is.
    """
    t = (meeting_type or "ΤΑΚΤΙΚΗ").strip().upper()
    if t in ("ΤΑΚΤΙΚΗ", "ΤΑΚΤΙΚΗΣ"):
        return "ΤΑΚΤΙΚΗΣ"
    if t in ("ΕΚΤΑΚΤΗ", "ΕΚΤΑΚΤΗΣ"):
        return "ΕΚΤΑΚΤΗΣ"
    return t
