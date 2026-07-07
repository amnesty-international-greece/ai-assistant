"""Board meeting invitation workflow - Wave 2 refactor.

Step order (12 steps):
  1.  send_scheduling_email   - board scheduling email via M365 (anchor for thread)
  2.  await_approval          - SecGen approval gate (always halts)
  3.  read_agenda             - read single-tab agenda (no filtering, computes duration)
  4.  init_meeting_thread     - derive meeting_id (board_meeting:ΔΣXX-YYYY)
  5.  schedule_zoom           - create Zoom meeting using computed duration
  6.  draft_invitation        - fill template replacements
  7.  generate_pdf            - render PDF (no Drive upload)
  8.  approval                - PDF review gate (halts only in test_mode)
  9.  archive                 - upload PDF to SharePoint + protocol row (email PDF if upload fails)
  10. send_board_email        - threaded reply to scheduling email with final invitation
  11. send_newsletter         - Brevo: draft+test in test_mode, full live send in live mode
  12. confirm_newsletter      - confirm gate (halts only in test_mode; no-op if already sent live)
"""

from __future__ import annotations

import logging
from datetime import date as _date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

# Meeting dates/times in the agenda sheet are Athens-local wall-clock values.
_ATHENS_TZ = ZoneInfo("Europe/Athens")

from src.config import settings
from src.core.email_templates import render_email
from src.core.workflow import BaseWorkflow, WorkflowStep, StepResult
from src.integrations.google_drive import GoogleClient
from src.integrations.zoom import ZoomClient
from src.integrations.onedrive import OneDriveClient
from src.integrations.brevo import BrevoClient
from src.integrations.m365_mail import M365MailClient

logger = logging.getLogger(__name__)

_BOARD_EMAIL = "board@amnesty.org.gr"
_DIRECTOR_EMAIL = "director@amnesty.org.gr"   # BCC'd on the scheduling email only
_ARCHIVE_FALLBACK_EMAIL = "members@amnesty.org.gr"


class BoardMeetingInvitationWorkflow(BaseWorkflow):
    """Complete board meeting invitation flow (post-Wave-2)."""

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
        # NOTE: `schedule_reminder` was removed - Zoom handles reminders natively
        # (account-level setting in the Zoom web portal → Settings → Email Notification).
        return [
            WorkflowStep("send_scheduling_email", "Send scheduling email to board via M365"),
            WorkflowStep("await_approval", "Wait for SecGen approval of final agenda + date", requires_approval=True),
            WorkflowStep("read_agenda", "Read agenda from Google Sheets (single tab)"),
            WorkflowStep("init_meeting_thread", "Initialise board email thread anchor"),
            WorkflowStep("schedule_zoom", "Schedule Zoom meeting"),
            WorkflowStep("draft_invitation", "Draft invitation"),
            WorkflowStep("generate_pdf", "Generate PDF document"),
            WorkflowStep("approval", "Review and approve draft", requires_approval=True),  # halts only in test_mode
            WorkflowStep("archive", "Archive PDF to OneDrive"),
            WorkflowStep("send_board_email", "Send final invitation reply to board via M365"),
            WorkflowStep("send_newsletter", "Create campaign + (test or live) send"),
            WorkflowStep("confirm_newsletter", "Confirm live send", requires_approval=True),  # halts only in test_mode
        ]

    @staticmethod
    def debug_fixture() -> dict[str, Any]:
        """Canonical fake ctx for `debug run board_meeting_invitation <step>`.

        Provides every key any ``_step_*`` reads so a step can run in isolation
        without a KeyError.  The debug runner forces ``test_mode=True`` (emails
        redirect to test_email, Brevo stays draft, archive is skipped); it is
        intentionally NOT set here.  Steps that hit external APIs (M365, Zoom,
        Google, Brevo) behave per test_mode / config - the fixture only
        guarantees the INPUT keys exist.
        """
        return {
            # send_scheduling_email
            "poll_url": "https://example.invalid/poll/debug",      # send_scheduling_email
            "meeting_ref_override": "ΔΣ99-2099",                   # send_scheduling_email / read_agenda sandbox ref
            "agenda_sheet_id": "",                                 # read_agenda / send_scheduling_email
            "response_deadline": "2099-06-11",                     # send_scheduling_email deadline
            "crabfit_dates": [],                                   # send_scheduling_email: candidate dates (empty → no poll created)
            # read_agenda (skip live Sheets read; provide manual agenda)
            "_skip_read_agenda": True,                             # read_agenda: use provided agenda
            "_skip_approval_guard": True,                          # read_agenda: skip D16:D18 approval gate
            "agenda_items": ["Δοκιμαστικό θέμα 1", "Δοκιμαστικό θέμα 2"],  # read_agenda/draft/zoom/newsletter
            "meeting_number": "99",                                # many steps
            "meeting_date": "2099-06-15",                          # many steps
            "meeting_time": "18:00",                               # many steps
            "meeting_type": "ΤΑΚΤΙΚΗ",                              # draft_invitation / newsletter
            "location": "ΔΙΑΔΙΚΤΥΑΚΑ",                              # draft_invitation
            "raw_meeting_id": "ΔΣ99-2099",                         # meeting_id derivation / refs
            # init_meeting_thread derives meeting_id from the above
            # send_scheduling_email output → consumed by send_board_email
            "email_thread_anchor": "debug-anchor-id",              # send_board_email parent message id
            # schedule_zoom outputs → consumed by draft / board email / newsletter
            "zoom_join_url": "https://example.invalid/zoom/debug",
            "zoom_meeting_id": "9999999999",
            "zoom_passcode": "debugpass",
            "meeting_duration_minutes": 90,                        # schedule_zoom duration
            # draft_invitation / generate_pdf / archive
            "protocol_number": "2099_999",
            "invitation_replacements": {                           # generate_pdf input (normally from draft_invitation)
                "_invitation_dates_": {"issue": "6 Ιουνίου 2099", "meeting": "15 Ιουνίου 2099"},
                "[ΤΥΠΟΣ]": "ΤΑΚΤΙΚΗΣ",
                "[ΩΡΑ ΕΝΑΡΞΗΣ]": "18:00",
                "[ΤΟΠΟΘΕΣΙΑ]": "διαδικτυακά μέσω της πλατφόρμας Zoom",
                "_agenda_items_": ["Δοκιμαστικό θέμα 1", "Δοκιμαστικό θέμα 2"],
            },
            "invitation_zoom_url": "https://example.invalid/zoom/debug",  # generate_pdf
            # generate_pdf outputs → consumed by archive / send_board_email / rollback
            "pdf_path": "data/debug/invitation.pdf",
            "pdf_filename": "Πρόσκληση - Συνεδρίαση ΔΣ99-2099.pdf",
            # archive outputs → consumed by send_board_email / rollback
            "archive_file_id": "debug-archive-id",
            "archive_share_link": "https://example.invalid/share/debug",
            # newsletter steps
            "brevo_template_id": 0,                                # send_newsletter (0 → step skips gracefully)
            "brevo_list_ids": [],                                  # send_newsletter
            "meeting_location": "Zoom",                            # send_newsletter preview text
            "newsletter_campaign_id": 0,                           # confirm_newsletter / rollback
            "newsletter_sent": False,                              # confirm_newsletter branch
            "newsletter_skipped": True,                            # confirm_newsletter / rollback branch
            "bus_event_published": False,                          # rollback cancel-event guard
        }

    async def execute_step(self, step: WorkflowStep, context: dict[str, Any]) -> StepResult:
        handler = getattr(self, f"_step_{step.name}", None)
        if not handler:
            return StepResult(success=False, message=f"No handler for step: {step.name}")
        return await handler(context)

    async def rollback(self, ctx: dict[str, Any]) -> None:
        """Undo any side effects created during the workflow."""
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

        # Delete archived PDF from SharePoint (non-fatal)
        pdf_filename = ctx.get("pdf_filename") or ""
        meeting_date = ctx.get("meeting_date", "")
        year_str = meeting_date[:4] if len(meeting_date) >= 4 else ""
        if pdf_filename and year_str and ctx.get("archive_file_id"):
            try:
                remote_path = f"{settings.onedrive.yearly_subfolder}/{year_str}/{pdf_filename}"
                await self.onedrive.delete_file(remote_path, workflow=self.name)
                logger.info("Rollback: deleted archived PDF %s", remote_path)
            except Exception as e:
                logger.warning("Rollback: could not delete archived PDF (non-fatal): %s", e)

        # Delete protocol row (non-fatal)
        protocol_number = ctx.get("protocol_number") or ""
        if protocol_number:
            try:
                await self.onedrive.delete_protocol_row(protocol_number)
                logger.info("Rollback: deleted protocol row %s", protocol_number)
            except Exception as e:
                logger.warning("Rollback: could not delete protocol row (non-fatal): %s", e)

        # Delete Brevo draft campaign (test mode only - live campaigns are kept)
        campaign_id = ctx.get("newsletter_campaign_id")
        if campaign_id and ctx.get("newsletter_skipped"):
            try:
                await self.brevo.delete_campaign(campaign_id, workflow=self.name)
                logger.info("Rollback: deleted Brevo draft campaign %s", campaign_id)
            except Exception as e:
                logger.warning("Rollback: could not delete Brevo campaign %s: %s", campaign_id, e)

        # Publish bus cancel event if the schedule was already announced
        if ctx.get("bus_event_published"):
            meeting_id_str = _derive_meeting_id(ctx)
            if meeting_id_str:
                try:
                    from src.core.event_bus import bus
                    from src.core.events import (
                        EVENT_BOARD_MEETING_CANCELLED,
                        BoardMeetingCancelledPayload,
                    )
                    await bus.publish(
                        EVENT_BOARD_MEETING_CANCELLED,
                        BoardMeetingCancelledPayload(
                            meeting_id=meeting_id_str,
                            reason="Workflow rolled back",
                        ),
                    )
                except Exception as exc:
                    logger.warning("Rollback: bus publish CANCELLED failed (non-fatal): %s", exc)

    # ─────────────────────────────────────────────────────────────────────────
    # Step 1: send_scheduling_email
    # ─────────────────────────────────────────────────────────────────────────

    async def _step_send_scheduling_email(self, ctx: dict[str, Any]) -> StepResult:
        """Send the initial board scheduling email.

        Uses the EXACT Greek template from Notes.md.  In test_mode the email is
        sent to ``settings.testing.test_email`` instead of the board address
        (it is NOT skipped - verifies layout end-to-end).
        """
        poll_url = (ctx.get("poll_url") or "").strip()
        test_mode = bool(ctx.get("test_mode"))

        if not settings.ms_client_id or not settings.ms_tenant_id:
            return StepResult(
                success=True,
                data={"scheduling_email_skipped": True},
                message="Scheduling email skipped - M365 not configured (set MS_CLIENT_ID / MS_TENANT_ID)",
            )

        # ── Resolve meeting_ref from cell D5 (the source of truth) ───────────
        # D5 is password-protected on the user side; ``reset_agenda_sheet``
        # updates it at the start of each cycle, so the value is always the
        # ref for THIS cycle's meeting.  Falls back to the placeholder ref
        # only if the sheet isn't configured at all - any other read failure
        # raises (because sending an email with the wrong meeting_ref would
        # corrupt the thread anchor, subject line, and downstream artefacts).
        # Sandbox override - when ``--meeting-ref ΔΣ99-2099`` is passed (used
        # for safe end-to-end testing in parallel with a live cycle), bypass
        # D5 entirely so the test workflow's meeting_id never collides with
        # the live one's Discord threads / Zoom meeting / pending reminders.
        meeting_ref_override = (ctx.get("meeting_ref_override") or "").strip()
        sheet_id = ctx.get("agenda_sheet_id") or settings.google.agenda_sheet_id
        meeting_ref = "ΔΣXX-YYYY"
        if meeting_ref_override:
            meeting_ref = meeting_ref_override
            logger.info(
                "send_scheduling_email: using sandbox meeting_ref override %r "
                "(D5 NOT read)", meeting_ref,
            )
        elif sheet_id:
            try:
                self._google.authenticate()
                meeting_ref = self._google.read_meeting_ref(sheet_id)
            except Exception as e:
                logger.warning(
                    "Could not read meeting_ref from D5 (non-fatal): %s", e
                )

        # ── Deadline (Greek long form e.g. "1 Ιουνίου"): default today + 4,
        #    override via --response-deadline ─────────────────────────────────
        deadline_str = (ctx.get("response_deadline") or "").strip()
        try:
            if deadline_str:
                deadline_dt = _date.fromisoformat(deadline_str)
            else:
                deadline_dt = _date.today() + timedelta(days=4)
            deadline_fmt = f"{deadline_dt.day} {_GREEK_MONTHS[deadline_dt.month]}"
        except (ValueError, TypeError, IndexError):
            deadline_fmt = "-"

        # ── Crab Fit availability poll ───────────────────────────────────────
        # When candidate dates were supplied (and no explicit poll URL given),
        # create a Crab Fit event over those dates and use its grid as the poll.
        # Non-fatal: if creation fails, the email still sends (no-poll variant).
        crabfit_dates = ctx.get("crabfit_dates") or []
        crabfit_url = ""
        if not poll_url and crabfit_dates:
            try:
                from src.integrations.crabfit import CrabFitClient
                _dates = [_date.fromisoformat(s) for s in crabfit_dates]
                _event = await CrabFitClient().create_event(
                    name=f"Συνεδρίαση {meeting_ref}",
                    dates=_dates,
                    workflow=self.name,
                )
                poll_url = _event["url"]
                crabfit_url = _event["url"]
            except Exception as cf_err:
                logger.warning("Crab Fit event creation failed (non-fatal): %s", cf_err)

        # ── URLs for hyperlink substitution ──────────────────────────────────
        sheet_url = (
            f"https://docs.google.com/spreadsheets/d/{sheet_id}/"
            if sheet_id
            else "#"
        )

        # ── Deadline countdown hint (optional days-remaining suffix) ─────────
        # Compose a single placeholder so the template never sees a KeyError
        # on missing `days_remaining`.  Callers that later compute days can
        # pass it via ctx; for now it degrades gracefully to the bare date.
        try:
            _days_left = (deadline_dt - _date.today()).days
            _hint = f" (σε {_days_left} ημέρες)" if _days_left > 0 else ""
        except Exception:
            _hint = ""
        deadline_with_hint = f"{deadline_fmt}{_hint}"

        # ── Build HTML body from the email template ──────────────────────────
        # Templates live in assets/email_templates/*.html and are editable
        # without touching code.  Two variants depending on poll URL.
        template_name = "scheduling_with_poll" if poll_url else "scheduling_no_poll"
        template_vars = {
            "meeting_ref":        meeting_ref,
            "sheet_url":          sheet_url,
            "deadline":           deadline_fmt,
            "deadline_with_hint": deadline_with_hint,
        }
        if poll_url:
            template_vars["poll_url"] = poll_url
        body_html = render_email(
            template_name,
            kicker="ΠΡΟΓΡΑΜΜΑΤΙΣΜΟΣ - ΗΜΕΡΗΣΙΑ ΔΙΑΤΑΞΗ",
            title=f"ΣΥΝΕΔΡΙΑΣΗ {meeting_ref}",
            header_ref="ΔΣ - ΠΡΟΓΡΑΜΜΑΤΙΣΜΟΣ",
            # Shell already prefixes the org name on its own line; this is
            # the second line and should be the workflow-specific context.
            footer_note="Εσωτερική επικοινωνία - Διοικητικό Συμβούλιο",
            **template_vars,
        )

        subject = f"Συνεδρίαση {meeting_ref}"
        # In test_mode everything is redirected to the test inbox (no BCC).
        # In live mode the scheduling email - and ONLY this one - also BCCs
        # the Director so they're looped in for the input phase.  Subsequent
        # emails in this thread (poll URL share, invitation, minutes) do NOT
        # BCC the Director; that's only useful for the initial call-for-input.
        recipient = settings.testing.test_email if test_mode else _BOARD_EMAIL
        bcc_list: list[str] | None = None if test_mode else [_DIRECTOR_EMAIL]

        if test_mode and not recipient:
            return StepResult(
                success=True,
                data={"scheduling_email_skipped": True},
                message="[TEST] Scheduling email skipped - testing.test_email not set",
            )

        try:
            client = M365MailClient()
            anchor = await client.send_email(
                to=recipient,
                subject=subject,
                body=body_html,
                html=True,
                bcc=bcc_list,
                workflow=self.name,
            )
        except Exception as e:
            logger.warning("Scheduling email send failed (non-fatal): %s", e)
            return StepResult(
                success=True,
                data={"scheduling_email_skipped": True},
                message=f"Scheduling email failed (non-fatal): {e}",
            )

        # ── Publish bus event so the Discord side opens the private thread ───
        # ``platform_bridge`` listens for THREAD_OPENED and creates the
        # board forum thread; it also posts the email body as the first
        # message via the EMAIL_SENT companion event.  Both publications
        # are non-fatal - the workflow has done its primary job already.
        meeting_id = _derive_meeting_id({"raw_meeting_id": meeting_ref})
        try:
            from src.core.event_bus import bus
            from src.core.events import (
                EVENT_BOARD_MEETING_THREAD_OPENED,
                EVENT_BOARD_EMAIL_SENT,
                BoardMeetingThreadOpenedPayload,
                BoardEmailSentPayload,
            )
            await bus.publish(
                EVENT_BOARD_MEETING_THREAD_OPENED,
                BoardMeetingThreadOpenedPayload(
                    meeting_id=meeting_id,
                    meeting_ref=meeting_ref,
                    email_subject=subject,
                    email_body_html=body_html,
                    poll_url=poll_url or "",
                    agenda_sheet_url=sheet_url or "",
                    test_mode=test_mode,
                ),
            )
            await bus.publish(
                EVENT_BOARD_EMAIL_SENT,
                BoardEmailSentPayload(
                    meeting_id=meeting_id,
                    meeting_ref=meeting_ref,
                    kind="scheduling",
                    subject=subject,
                    body_html=body_html,
                    test_mode=test_mode,
                    poll_url=poll_url or "",
                    agenda_url=sheet_url or "",
                ),
            )
        except Exception as bus_err:
            logger.warning(
                "Could not publish board thread/email events (non-fatal): %s",
                bus_err,
            )

        # Persist to DB so the bot can open the thread even if it was offline
        # when the bus event fired above (CLI and bot run in separate processes).
        try:
            from src.integrations.discord.scheduler import PendingActionsStore
            _pa_store = PendingActionsStore()
            await _pa_store.enqueue(
                action_type="board_meeting_thread_open",
                payload={
                    "meeting_id": meeting_id,
                    "meeting_ref": meeting_ref,
                    "email_subject": subject,
                    "email_body_html": body_html,
                    "poll_url": poll_url or "",
                    "agenda_url": sheet_url or "",
                    "agenda_sheet_url": sheet_url or "",  # legacy key kept for compat
                    "test_mode": test_mode,
                },
                due_at=datetime.now(timezone.utc),
            )
            logger.info("Enqueued board_meeting_thread_open pending action for %s", meeting_ref)
        except Exception as pa_err:
            logger.warning("Could not enqueue board_meeting_thread_open (non-fatal): %s", pa_err)

        recipients_msg = recipient + (f" (bcc {_DIRECTOR_EMAIL})" if bcc_list else "")
        return StepResult(
            success=True,
            data={
                "email_thread_anchor": anchor,
                "meeting_ref": meeting_ref,
                "raw_meeting_id": meeting_ref,
                "poll_url": poll_url or "",
                "crabfit_url": crabfit_url,
            },
            message=f"Scheduling email sent to {recipients_msg} (anchor={anchor})",
        )

    # ─────────────────────────────────────────────────────────────────────────
    # Step 2: await_approval (unconditional gate)
    # ─────────────────────────────────────────────────────────────────────────

    async def _step_await_approval(self, ctx: dict[str, Any]) -> StepResult:
        """Approval gate - always halts until SecGen manually resumes."""
        return StepResult(
            success=True,
            data={"awaiting_approval": True},
            message="Waiting for SecGen approval - re-run workflow to advance",
        )

    # ─────────────────────────────────────────────────────────────────────────
    # Step 3: read_agenda (single tab, no filtering)
    # ─────────────────────────────────────────────────────────────────────────

    async def _step_read_agenda(self, ctx: dict[str, Any]) -> StepResult:
        """Read meeting agenda data from the single-tab Google Sheet.

        The agenda sheet is guaranteed to have exactly ONE tab whose name is
        the meeting reference ``ΔΣXX-YYYY``.  We read ``tabs[0]`` directly -
        no filtering, no LLM, just pure data mapping.

        Additionally reads column ``I7:I`` (numeric duration in minutes per
        agenda item) and SUMs it into ``meeting_duration_minutes``.
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

            # ── Defensive guard: refuse to read an un-approved agenda ────────
            # If all three approval checkboxes (D16/D17/D18) are FALSE the
            # board has not signed off on the sheet - bail out with a clear
            # message rather than emailing a half-baked invitation.  Skipped
            # when ANY box is TRUE (typical auto-trigger case) and when the
            # caller has already supplied agenda data via _skip_read_agenda.
            if not ctx.get("_skip_approval_guard"):
                try:
                    tabs_for_guard = self._google.list_sheet_tabs(sheet_id)
                    if tabs_for_guard:
                        guard_tab = tabs_for_guard[0]["title"]
                        approval_rows = self._google.read_sheet(
                            sheet_id,
                            f"'{guard_tab}'!D16:D18",
                            value_render_option="UNFORMATTED_VALUE",
                        )
                        # Sanity check: D16:D18 must yield ≤3 rows.  If a
                        # wider range was returned (e.g. by an over-broad
                        # test mock) we skip the guard rather than misread
                        # column A as the approval state.
                        if approval_rows and len(approval_rows) <= 3:
                            any_checked = any(
                                bool(row[0]) for row in approval_rows
                                if row and row[0] not in (None, "", "FALSE", "false")
                            )
                            if not any_checked:
                                return StepResult(
                                    success=False,
                                    message=(
                                        f"Agenda sheet has not been approved "
                                        f"(D16/D17/D18 all FALSE in tab '{guard_tab}'). "
                                        "Reset the sheet via "
                                        "`ai-assistant invite reset-sheet` "
                                        "if needed, then have the board fill in "
                                        "fresh agenda + check the boxes."
                                    ),
                                )
                except StepResult:
                    raise
                except Exception as guard_err:
                    logger.warning(
                        "Approval-guard check failed (non-fatal, continuing): %s",
                        guard_err,
                    )

            tabs = self._google.list_sheet_tabs(sheet_id)
            if not tabs:
                return StepResult(
                    success=False,
                    message="Agenda sheet has no tabs.",
                )

            tab = tabs[0]
            tab_title = tab["title"]

            rows = self._google.read_sheet(
                sheet_id,
                f"'{tab_title}'!A1:K200",
                value_render_option="UNFORMATTED_VALUE",
            )

            fields = _scan_form_labels(rows)

            raw_number = fields.get("ΑΡΙΘΜΟΣ ΣΥΝΕΔΡΙΑΣΗΣ", ("", 5))[0]
            raw_date, date_row = fields.get("ΗΜΕΡΟΜΗΝΙΑ", ("", 7))
            raw_time, _ = fields.get("ΩΡΑ ΕΝΑΡΞΗΣ", ("", 9))
            raw_type, _ = fields.get("ΤΥΠΟΣ", ("", None))
            raw_loc, _ = fields.get("ΤΟΠΟΘΕΣΙΑ", ("", None))
            _, trigger_row = fields.get("ΠΡΟΣΚΛΗΣΗ", ("", 11))

            parsed_date = _parse_sheet_date(raw_date)
            parsed_time = _parse_sheet_time(raw_time)

            import re as _re

            # Meeting number: prefer D5 (the single source of truth - see
            # GoogleClient.read_meeting_ref).  Falls back to the form's
            # "ΑΡΙΘΜΟΣ ΣΥΝΕΔΡΙΑΣΗΣ" cell if D5 is unreadable, then to whatever
            # the tab title contains.  Note D5 holds the full ref ΔΣXX-YYYY;
            # we extract just the XX (with year-month-aware mapping handled
            # by reset_agenda_sheet's roll-over logic, not here).
            # Sandbox override: --meeting-ref bypasses D5 read here too.
            meeting_number = ""
            override = (ctx.get("meeting_ref_override") or "").strip()
            if override:
                m = _re.match(r"^ΔΣ(\d{1,2})-(\d{4})$", override)
                if m:
                    meeting_number = str(int(m.group(1)))
                    logger.info(
                        "read_agenda: meeting_number derived from sandbox "
                        "override %r (D5 NOT read)", override,
                    )
            try:
                if not meeting_number:
                    d5_ref = self._google.read_meeting_ref(sheet_id, tab_title=tab_title)
                    m = _re.match(r"^ΔΣ(\d{1,2})-(\d{4})$", d5_ref)
                    if m:
                        meeting_number = str(int(m.group(1)))
            except Exception as e:
                logger.warning(
                    "D5 meeting_ref unreadable, falling back to form cell: %s", e
                )
            if not meeting_number:
                nm = _re.search(r"\d+", str(raw_number))
                if nm:
                    meeting_number = str(int(nm.group(0)))
                else:
                    num_match = _re.search(r"(\d+)-(\d{4})", tab_title)
                    meeting_number = (
                        str(int(num_match.group(1))) if num_match else str(raw_number)
                    )

            meeting_date = str(parsed_date) if parsed_date else ""
            meeting_time = parsed_time

            def cell(r: int, c: int) -> str:
                try:
                    return str(rows[r - 1][c - 1]).strip()
                except IndexError:
                    return ""

            # Agenda items: column H (8) starting row 7
            agenda_items: list[str] = []
            duration_total = 0
            for row_idx in range(7, len(rows) + 1):
                item = cell(row_idx, 8)
                if item and item.lower() not in ("none", "nan", ""):
                    agenda_items.append(item)
                    # Column I (9): duration in minutes (numeric, may be blank)
                    raw_dur = cell(row_idx, 9)
                    if raw_dur:
                        try:
                            duration_total += int(float(raw_dur))
                        except (ValueError, TypeError):
                            pass

            meeting_duration_minutes = duration_total or None

            # CLI-provided values take precedence
            final_number = ctx.get("meeting_number") or meeting_number
            final_date = ctx.get("meeting_date") or meeting_date
            final_time = ctx.get("meeting_time") or meeting_time

            # ── Interactive fallback for missing time ────────────────────────
            if not final_time:
                print(f"\n  Tab '{tab_title}' has no meeting time set.")
                raw_t = input("  Enter meeting start time (e.g. 18:00): ").strip()
                if raw_t and _re.match(r"^\d{1,2}:\d{2}$", raw_t):
                    h, m = map(int, raw_t.split(":"))
                    final_time = f"{h:02d}:{m:02d}"
                elif raw_t:
                    print(f"  Invalid format '{raw_t}' - time left blank.")

            # ── Date sanity check ────────────────────────────────────────────
            if not final_date:
                date_col = f"D{date_row}" if date_row else "the date cell"
                return StepResult(
                    success=False,
                    message=(
                        f"Tab '{tab_title}' has no meeting date set "
                        f"({date_col} currently contains: '{raw_date}'). "
                        f"Please fill in the date (e.g. 14/05/2026), "
                        f"or pass --date YYYY-MM-DD on the command line."
                    ),
                )
            today = _date.today()
            meeting_date_obj = _date.fromisoformat(final_date)
            days_until = (meeting_date_obj - today).days
            max_advance = settings.workflows.board_meeting.max_advance_days

            if days_until < 0:
                date_col = f"D{date_row}" if date_row else "the date cell"
                return StepResult(
                    success=False,
                    message=(
                        f"Meeting date {final_date} is in the past "
                        f"({abs(days_until)} days ago). "
                        f"Please update {date_col} in tab '{tab_title}'."
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
                        message=f"Cancelled - meeting date {final_date} is too far in advance.",
                    )

            return StepResult(
                success=True,
                data={
                    "meeting_number": final_number,
                    "meeting_date": final_date,
                    "meeting_time": final_time,
                    "meeting_type": raw_type,
                    "location": raw_loc,
                    "agenda_items": agenda_items,
                    "meeting_duration_minutes": meeting_duration_minutes,
                    "raw_meeting_id": raw_number,
                    "agenda_tab": tab_title,
                    "trigger_row": trigger_row,
                },
                message=(
                    f"Read agenda from tab '{tab_title}': "
                    f"Meeting #{final_number} on {final_date} at {final_time} "
                    f"({raw_type or 'ΤΑΚΤΙΚΗ'}, {raw_loc or 'ΔΙΑΔΙΚΤΥΑΚΑ'}) "
                    f"- {len(agenda_items)} items, "
                    f"{meeting_duration_minutes or '(default)'} min"
                ),
            )
        except Exception as e:
            return StepResult(success=False, message=f"Failed to read agenda: {e}")

    # ─────────────────────────────────────────────────────────────────────────
    # Step 4: init_meeting_thread
    # ─────────────────────────────────────────────────────────────────────────

    async def _step_init_meeting_thread(self, ctx: dict[str, Any]) -> StepResult:
        meeting_id = _derive_meeting_id(ctx)
        if not meeting_id:
            return StepResult(
                success=False,
                message="Cannot derive meeting_id - meeting_date / meeting_number missing from context",
            )
        return StepResult(
            success=True,
            data={"meeting_id": meeting_id},
            message=f"Meeting thread initialised: {meeting_id}",
        )

    # ─────────────────────────────────────────────────────────────────────────
    # Step 5: schedule_zoom
    # ─────────────────────────────────────────────────────────────────────────

    async def _step_schedule_zoom(self, ctx: dict[str, Any]) -> StepResult:
        try:
            meeting_date = ctx.get("meeting_date", "")
            meeting_time = ctx.get("meeting_time", "")
            meeting_number = ctx.get("meeting_number", "")

            if not meeting_date:
                return StepResult(
                    success=False,
                    message="Meeting date is required to schedule Zoom - check the agenda sheet.",
                )
            if not meeting_time:
                import re as _re
                print("\n  Meeting time not set. Required for the Zoom meeting.")
                raw_t = input("  Enter meeting start time (e.g. 18:00): ").strip()
                if raw_t and _re.match(r"^\d{1,2}:\d{2}$", raw_t):
                    h, m = map(int, raw_t.split(":"))
                    meeting_time = f"{h:02d}:{m:02d}"
                else:
                    return StepResult(
                        success=False,
                        message=f"Invalid or missing meeting time '{raw_t}' - cannot schedule Zoom.",
                    )

            start_time = f"{meeting_date}T{meeting_time}:00"

            raw_id = ctx.get("raw_meeting_id", "")
            year = meeting_date[:4] if len(meeting_date) >= 4 else "ΧΧΧΧ"
            seq_str = str(meeting_number).zfill(2) if str(meeting_number).isdigit() else str(meeting_number)
            meeting_ref = raw_id or f"ΔΣ{seq_str}-{year}"
            topic = f"Συνεδρίαση {meeting_ref}"

            agenda_items = ctx.get("agenda_items", [])
            agenda_body = "\n".join(f"{i + 1}. {item}" for i, item in enumerate(agenda_items))
            agenda_text = f"Ημερήσια Διάταξη\n{agenda_body}" if agenda_body else "Ημερήσια Διάταξη\n"

            duration = ctx.get("meeting_duration_minutes") or settings.zoom.meeting_defaults.duration

            result = await self._zoom.schedule_meeting(
                topic=topic,
                start_time=start_time,
                duration=duration,
                agenda=agenda_text,
                workflow=self.name,
            )

            meeting_id = str(result.get("id", ""))

            # Pre-register board members so each gets a personal join link.
            # SAFETY: in test_mode we register ONLY the SecGen's test inbox.
            # Zoom emails each registrant their personal join URL immediately,
            # so registering all 10 real board members during a test would
            # spam them (and the meeting gets rolled back at the end, likely
            # triggering cancellation emails too).
            if ctx.get("test_mode"):
                test_email = settings.testing.test_email
                if test_email and meeting_id:
                    try:
                        await self._zoom.add_registrants(
                            meeting_id=meeting_id,
                            registrants=[{
                                "email":      test_email,
                                "first_name": "Test",
                                "last_name":  "Run",
                            }],
                            workflow=self.name,
                        )
                    except Exception as reg_err:
                        logger.warning("Could not register test recipient: %s", reg_err)
            else:
                board_members = settings.workflows.board_meeting.board_members or []
                if board_members and meeting_id:
                    try:
                        await self._zoom.add_registrants(
                            meeting_id=meeting_id,
                            registrants=[m.model_dump() for m in board_members],
                            workflow=self.name,
                        )
                    except Exception as reg_err:
                        logger.warning("Could not pre-register board members: %s", reg_err)

            return StepResult(
                success=True,
                data={
                    "zoom_join_url": result.get("join_url", ""),
                    "zoom_meeting_id": meeting_id,
                    "zoom_passcode": result.get("password", ""),
                    "zoom_duration_minutes": duration,
                },
                message=f"Zoom meeting scheduled ({duration} min): {result.get('join_url', '')}",
            )
        except Exception as e:
            return StepResult(success=False, message=f"Failed to schedule Zoom: {e}")

    # ─────────────────────────────────────────────────────────────────────────
    # Step 6: draft_invitation
    # ─────────────────────────────────────────────────────────────────────────

    async def _step_draft_invitation(self, ctx: dict[str, Any]) -> StepResult:
        try:
            meeting_date = ctx.get("meeting_date", "")
            meeting_time = ctx.get("meeting_time", "")
            meeting_type = ctx.get("meeting_type", "ΤΑΚΤΙΚΗ")
            location = ctx.get("location", "ΔΙΑΔΙΚΤΥΑΚΑ")
            agenda_items = ctx.get("agenda_items", [])
            zoom_url = ctx.get("zoom_join_url", "")
            protocol_number = ctx.get("protocol_number", "")

            import re as _re

            _PROTO_RE = r"^\d{4}[_-]\d+$"
            if not (protocol_number and _re.match(_PROTO_RE, protocol_number.strip())):
                protocol_number = await _fetch_next_protocol_number(self.onedrive) or ""

            if not protocol_number:
                print()
                print("  Αριθμός Πρωτοκόλλου: δεν βρέθηκε στο Πρωτόκολλο.")
                raw = input("  Εισάγετε αριθμό πρωτοκόλλου (π.χ. 2026_017) ή Enter για παράλειψη: ").strip()
                if raw and _re.match(_PROTO_RE, raw):
                    protocol_number = raw
                elif raw:
                    print(f"  Μη έγκυρη μορφή '{raw}' - παράλειψη αριθμού πρωτοκόλλου.")
                    protocol_number = ""

            greek_date = _format_greek_date(meeting_date)
            type_genitive = _meeting_type_genitive(meeting_type)

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
            else:
                zoom_part = "[ZOOM_PLACEHOLDER]" if zoom_url else "Zoom"
                location_phrase = f"διαδικτυακά μέσω της πλατφόρμας {zoom_part}"

            replacements: dict = {}
            if protocol_number and _re.match(r"^\d{4}[_-]\d+$", protocol_number.strip()):
                replacements["[ΑΡΙΘΜΟΣ ΠΡΩΤΟΚΟΛΛΟΥ]"] = protocol_number.strip()
            else:
                replacements["_delete_paragraphs_"] = ["Αρ. Πρωτ.: [ΑΡΙΘΜΟΣ ΠΡΩΤΟΚΟΛΛΟΥ]"]

            # The template reuses [ΗΜΕΡΟΜΗΝΙΑ] twice: the top-right letterhead
            # (the *issue* date - always today) and the body sentence (the
            # *meeting* date).  They are filled per-occurrence, not via a single
            # replaceAllText, since the two need different values.
            replacements.update({
                "_invitation_dates_": {
                    "issue": _format_greek_date(_date.today().isoformat()),
                    "meeting": greek_date,
                },
                "[ΤΥΠΟΣ]": type_genitive,
                "[ΩΡΑ ΕΝΑΡΞΗΣ]": meeting_time or "ΩΡΑ ΤΒΔ",
                "[ΤΟΠΟΘΕΣΙΑ]": location_phrase,
                "_agenda_items_": agenda_items if agenda_items else ["(κατόπιν ανακοίνωσης)"],
            })

            proto_msg = f", Αρ. Πρωτ. {protocol_number}" if protocol_number else ", χωρίς αριθμό πρωτοκόλλου"
            return StepResult(
                success=True,
                data={
                    "invitation_replacements": replacements,
                    "invitation_zoom_url": zoom_url,
                    "protocol_number": protocol_number,
                },
                message=(
                    f"Invitation prepared for {greek_date} at {meeting_time} "
                    f"({type_genitive}){proto_msg} - {len(agenda_items)} agenda items"
                ),
            )
        except Exception as e:
            return StepResult(success=False, message=f"Failed to prepare invitation: {e}")

    # ─────────────────────────────────────────────────────────────────────────
    # Step 7: generate_pdf  (no Drive upload - newsletter no longer needs link)
    # ─────────────────────────────────────────────────────────────────────────

    async def _step_generate_pdf(self, ctx: dict[str, Any]) -> StepResult:
        try:
            replacements = ctx.get("invitation_replacements")
            if not replacements:
                return StepResult(
                    success=False,
                    message="No invitation replacements found - did draft_invitation run?",
                )

            template_id = settings.google.invitation_template_id
            if not template_id:
                return StepResult(
                    success=False,
                    message="google.invitation_template_id not set in config.yaml",
                )

            meeting_number = ctx.get("meeting_number", "XX")
            meeting_date = ctx.get("meeting_date", "unknown")
            protocol_number = ctx.get("protocol_number", "")
            zoom_url = ctx.get("invitation_zoom_url") or ctx.get("zoom_join_url", "")

            raw_id = ctx.get("raw_meeting_id", "")
            year = meeting_date[:4] if len(meeting_date) >= 4 else "ΧΧΧΧ"
            seq_str = str(meeting_number).zfill(2) if str(meeting_number).isdigit() else str(meeting_number)
            meeting_ref = raw_id or f"ΔΣ{seq_str}-{year}"

            import re as _re
            doc_base = f"Πρόσκληση - Συνεδρίαση {meeting_ref}"
            if protocol_number and _re.match(r"^\d{4}[_-]\d+$", protocol_number.strip()):
                filename = f"[{protocol_number}] {doc_base}.pdf"
            else:
                filename = f"{doc_base}.pdf"
            safe_filename = filename.replace(":", "-").replace("/", "-")

            output_path = Path("data") / "output" / safe_filename
            output_path.parent.mkdir(parents=True, exist_ok=True)

            self._google.authenticate()
            working_doc_id = self._google.copy_document(template_id, doc_base)

            try:
                self._google.fill_document_template(
                    working_doc_id, replacements, zoom_url=zoom_url
                )
                self._google.export_doc_as_pdf(working_doc_id, output_path)
            finally:
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

    # ─────────────────────────────────────────────────────────────────────────
    # Step 8: approval  (halts only in test_mode)
    # ─────────────────────────────────────────────────────────────────────────

    async def _step_approval(self, ctx: dict[str, Any]) -> StepResult:
        """PDF approval gate.

        The ``WorkflowStep`` has ``requires_approval=True`` so the base runner
        halts BEFORE running this step.  In live mode we don't want a halt -
        the CLI handler is responsible for detecting that and auto-resuming.

        When this method actually runs (because the gate was passed), we
        simply succeed.  In live mode the CLI should call
        ``approve_and_resume()`` immediately without prompting the user.
        """
        if ctx.get("test_mode"):
            return StepResult(
                success=True,
                data={"approved": True, "approved_by": self.actor},
                message="Draft approved (test_mode)",
            )
        return StepResult(
            success=True,
            data={"approved": True, "approved_by": self.actor, "auto_approved": True},
            message="Approval auto-passed (live mode)",
        )

    # ─────────────────────────────────────────────────────────────────────────
    # Step 9: archive  (with email fallback on upload failure)
    # ─────────────────────────────────────────────────────────────────────────

    async def _step_archive(self, ctx: dict[str, Any]) -> StepResult:
        if ctx.get("test_mode"):
            return StepResult(success=True, data={"archive_skipped": True}, message="[TEST] Archive skipped")
        if not settings.ms_client_id or not settings.ms_tenant_id:
            return StepResult(
                success=True,
                data={"archive_skipped": True},
                message="Archive skipped - OneDrive not configured (set MS_CLIENT_ID / MS_TENANT_ID in .env)",
            )
        try:
            pdf_path = Path(ctx.get("pdf_path", ""))
            if not pdf_path.exists():
                return StepResult(success=False, message=f"PDF not found at {pdf_path}")

            meeting_date = ctx.get("meeting_date", "")
            year_str = meeting_date[:4] if len(meeting_date) >= 4 else ""
            protocol_number = ctx.get("protocol_number", "")
            meeting_number = ctx.get("meeting_number", "XX")
            agenda_items = ctx.get("agenda_items", [])

            remote_folder = f"{settings.onedrive.yearly_subfolder}/{year_str}" if year_str else settings.onedrive.yearly_subfolder

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

            if protocol_number and year_str:
                try:
                    raw_id = ctx.get("raw_meeting_id", "")
                    year = meeting_date[:4] if len(meeting_date) >= 4 else "ΧΧΧΧ"
                    seq_str = str(meeting_number).zfill(2) if str(meeting_number).isdigit() else str(meeting_number)
                    meeting_ref = raw_id or f"ΔΣ{seq_str}-{year}"
                    title = f"Πρόσκληση - Συνεδρίαση {meeting_ref}"
                    main_pts = "\n".join(f"{i}. {item}" for i, item in enumerate(agenda_items, 1)) if agenda_items else ""
                    await self.onedrive.append_protocol_row(
                        protocol_id=protocol_number,
                        date_str=meeting_date,
                        title=title,
                        main_points=main_pts,
                        tags="Διοικητικά, Προσκλήσεις",
                    )
                except Exception as reg_err:
                    logger.warning("Protocol registry update failed (non-fatal): %s", reg_err)

            return StepResult(
                success=True,
                data={
                    "archive_file_id": file_id,
                    "archive_share_link": share_link,
                },
                message=f"PDF archived to SharePoint: {remote_folder}/{pdf_path.name}",
            )
        except Exception as e:
            logger.warning("OneDrive archive failed (non-fatal, will email PDF): %s", e)
            await self._email_archive_fallback(ctx, error=e)
            return StepResult(
                success=True,
                data={"archive_skipped": True, "archive_emailed": True},
                message=f"Archive failed - PDF emailed to {_ARCHIVE_FALLBACK_EMAIL} for manual archiving: {e}",
            )

    async def _email_archive_fallback(self, ctx: dict[str, Any], *, error: Exception) -> None:
        """Send the PDF as an email attachment when SharePoint upload fails."""
        pdf_path_str = ctx.get("pdf_path") or ""
        pdf_filename = ctx.get("pdf_filename") or ""
        if not pdf_path_str:
            logger.warning("Archive fallback skipped - no pdf_path in context")
            return
        pdf_path = Path(pdf_path_str)
        if not pdf_path.exists():
            logger.warning("Archive fallback skipped - PDF not found at %s", pdf_path)
            return
        if not settings.ms_client_id or not settings.ms_tenant_id:
            logger.warning("Archive fallback skipped - M365 not configured")
            return

        meeting_date = ctx.get("meeting_date", "")
        year_str = meeting_date[:4] if len(meeting_date) >= 4 else ""
        intended_remote_path = (
            f"{settings.onedrive.yearly_subfolder}/{year_str}/{pdf_filename}"
            if year_str and pdf_filename
            else (pdf_filename or pdf_path.name)
        )

        subject = f"[Σφάλμα αρχειοθέτησης] {pdf_filename or pdf_path.name}"
        body = (
            "Αγαπητοί συνάδελφοι,\n\n"
            "Το συνημμένο PDF δεν κατέστη δυνατό να αρχειοθετηθεί αυτόματα στο SharePoint. "
            "Παρακαλούμε αρχειοθετήστε το χειροκίνητα στη θέση:\n\n"
            f"  {intended_remote_path}\n\n"
            f"Σφάλμα: {error}\n\n"
            "Με εκτίμηση,\nAI Assistant"
        )

        try:
            client = M365MailClient()
            await client.send_email(
                to=_ARCHIVE_FALLBACK_EMAIL,
                subject=subject,
                body=body,
                html=False,
                attachments=[pdf_path],
                workflow=self.name,
            )
            logger.info("Archive fallback email sent to %s", _ARCHIVE_FALLBACK_EMAIL)
        except Exception as e:
            logger.warning("Archive fallback email failed (non-fatal): %s", e)

    # ─────────────────────────────────────────────────────────────────────────
    # Step 10: send_board_email  (threaded reply, HTML with hyperlink)
    # ─────────────────────────────────────────────────────────────────────────

    async def _step_send_board_email(self, ctx: dict[str, Any]) -> StepResult:
        """Send the final invitation as a threaded reply to the scheduling email.

        In test mode the recipient is swapped to ``settings.testing.test_email``
        (same pattern as step 1's scheduling email), so the full thread can be
        inspected in one inbox without spamming the board.
        """
        anchor = ctx.get("email_thread_anchor", "") or ""
        test_mode = bool(ctx.get("test_mode"))

        if not settings.ms_client_id or not settings.ms_tenant_id:
            return StepResult(
                success=True,
                data={"board_email_skipped": True},
                message="Final board email skipped - M365 not configured",
            )
        if not anchor:
            return StepResult(
                success=True,
                data={"board_email_skipped": True},
                message="Final board email skipped - no email_thread_anchor (scheduling email did not run)",
            )

        recipient = settings.testing.test_email if test_mode else _BOARD_EMAIL
        if test_mode and not recipient:
            return StepResult(
                success=True,
                data={"board_email_skipped": True},
                message="[TEST] Final board email skipped - testing.test_email not set",
            )

        meeting_date = ctx.get("meeting_date", "")
        meeting_time = ctx.get("meeting_time", "")
        greek_date = _format_greek_date(meeting_date)
        _email_meeting_ref = ctx.get("raw_meeting_id", "") or ""

        zoom_url = ctx.get("zoom_join_url", "") or "(δεν έχει οριστεί)"
        zoom_meeting_id = ctx.get("zoom_meeting_id", "") or "(δεν έχει οριστεί)"
        zoom_passcode = ctx.get("zoom_passcode", "") or "(δεν έχει οριστεί)"
        share_link = ctx.get("archive_share_link", "") or "#"

        body_html = render_email(
            "invitation_board",
            # Shelled-render: wraps content in the v2 brand shell
            # (black header / yellow titlebar / candle footer).
            kicker="ΠΡΟΣΚΛΗΣΗ ΔΙΟΙΚΗΤΙΚΟΥ ΣΥΜΒΟΥΛΙΟΥ",
            title=f"ΣΥΝΕΔΡΙΑΣΗ {_email_meeting_ref}",
            header_ref="ΔΣ - ΠΡΟΣΚΛΗΣΗ",
            # Shell already prefixes the org name on its own line; this is
            # the second line and should be the workflow-specific context.
            footer_note="Εσωτερική επικοινωνία - Διοικητικό Συμβούλιο",
            # Inner-template placeholders
            share_link=share_link,
            greek_date=greek_date,
            meeting_time=meeting_time,
            zoom_url=zoom_url,
            zoom_meeting_id=zoom_meeting_id,
            zoom_passcode=zoom_passcode,
        )

        try:
            client = M365MailClient()
            reply_id = await client.send_reply(
                parent_internet_message_id=anchor,
                body=body_html,
                html=True,
                to=recipient,
                workflow=self.name,
            )
        except Exception as e:
            logger.warning("Final board email send failed (non-fatal): %s", e)
            return StepResult(
                success=True,
                data={"board_email_skipped": True},
                message=f"Final board email failed (non-fatal): {e}",
            )

        # Mirror this email to the private Discord board thread (non-fatal).
        try:
            from src.core.event_bus import bus
            from src.core.events import EVENT_BOARD_EMAIL_SENT, BoardEmailSentPayload
            meeting_ref_for_mirror = ctx.get("raw_meeting_id", "") or ""
            # send_reply derives its subject from the parent - mirror the
            # synthetic reply subject the recipient will see in their inbox.
            mirror_subject = (
                f"Re: Συνεδρίαση {meeting_ref_for_mirror}"
                if meeting_ref_for_mirror
                else "Re: Συνεδρίαση"
            )
            _agenda_items = ctx.get("agenda_items") or []
            _agenda_summary = "\n".join(f"{i+1}. {item}" for i, item in enumerate(_agenda_items))
            _meeting_date = ctx.get("meeting_date", "")
            _meeting_time = ctx.get("meeting_time", "")
            _meeting_dt_str = (
                f"{_meeting_date}T{_meeting_time}" if _meeting_date and _meeting_time else _meeting_date
            )
            _sheet_id = ctx.get("agenda_sheet_id") or settings.google.agenda_sheet_id
            _sheet_url = f"https://docs.google.com/spreadsheets/d/{_sheet_id}/" if _sheet_id else ""
            _invitation_pdf_url = ctx.get("archive_share_link", "") or ""
            await bus.publish(
                EVENT_BOARD_EMAIL_SENT,
                BoardEmailSentPayload(
                    meeting_id=_derive_meeting_id(ctx),
                    meeting_ref=meeting_ref_for_mirror,
                    kind="invitation",
                    subject=mirror_subject,
                    body_html=body_html,
                    test_mode=test_mode,
                    zoom_url=ctx.get("zoom_join_url", "") or "",
                    agenda_url=_sheet_url,
                    invitation_pdf_url=_invitation_pdf_url,
                    meeting_datetime=_meeting_dt_str,
                    agenda_summary=_agenda_summary,
                ),
            )
        except Exception as bus_err:
            logger.warning(
                "Could not publish board email_sent event for invitation (non-fatal): %s",
                bus_err,
            )

        # Persist to DB so the bot posts the invitation embed into the private
        # board thread even when it was offline during the bus publish above
        # (CLI / FastAPI webhook and bot run in separate processes).
        try:
            from src.integrations.discord.scheduler import PendingActionsStore
            await PendingActionsStore().enqueue(
                action_type="board_email_invitation_mirror",
                payload={
                    "meeting_id": _derive_meeting_id(ctx),
                    "meeting_ref": meeting_ref_for_mirror,
                    "kind": "invitation",
                    "subject": mirror_subject,
                    "body_html": body_html,
                    "test_mode": test_mode,
                    "zoom_url": ctx.get("zoom_join_url", "") or "",
                    "agenda_url": _sheet_url,
                    "invitation_pdf_url": _invitation_pdf_url,
                    "meeting_datetime": _meeting_dt_str,
                    "agenda_summary": _agenda_summary,
                },
                due_at=datetime.now(timezone.utc),
            )
            logger.info("Enqueued board_email_invitation_mirror pending action for %s", meeting_ref_for_mirror)
        except Exception as pa_err:
            logger.warning("Could not enqueue board_email_invitation_mirror (non-fatal): %s", pa_err)

        return StepResult(
            success=True,
            data={"board_email_message_id": reply_id},
            message=f"Final board invitation sent in thread (reply id={reply_id}, to={recipient})",
        )

    # ─────────────────────────────────────────────────────────────────────────
    # Newsletter steps
    # ─────────────────────────────────────────────────────────────────────────

    def _build_newsletter_params(self, ctx: dict[str, Any]) -> tuple[dict, str, str, list[int], str]:
        meeting_number = ctx.get("meeting_number", "")
        meeting_date = ctx.get("meeting_date", "")
        meeting_time = ctx.get("meeting_time", "")
        meeting_type = ctx.get("meeting_type", "ΤΑΚΤΙΚΗ")
        zoom_link = ctx.get("zoom_join_url", "")
        raw_id = ctx.get("raw_meeting_id", "")
        year = meeting_date[:4] if len(meeting_date) >= 4 else "ΧΧΧΧ"
        seq_str = str(meeting_number).zfill(2) if str(meeting_number).isdigit() else str(meeting_number)
        meeting_ref = raw_id or f"ΔΣ{seq_str}-{year}"

        _GREEK_MONTHS = {
            1: "Ιανουαρίου", 2: "Φεβρουαρίου", 3: "Μαρτίου", 4: "Απριλίου",
            5: "Μαΐου", 6: "Ιουνίου", 7: "Ιουλίου", 8: "Αυγούστου",
            9: "Σεπτεμβρίου", 10: "Οκτωβρίου", 11: "Νοεμβρίου", 12: "Δεκεμβρίου",
        }
        try:
            dt = _date.fromisoformat(meeting_date)
            greek_date = f"{dt.day} {_GREEK_MONTHS[dt.month]} {dt.year}"
        except (ValueError, KeyError):
            greek_date = meeting_date

        type_lower = "έκτακτη" if "ΕΚΤΑΚΤΗ" in str(meeting_type).upper() else "τακτική"

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
            "[ΣΥΝΕΔΡΙΑΣΗ]": meeting_ref,
            "[ΤΥΠΟΣ]": type_lower,
            "[ΗΜΕΡΟΜΗΝΙΑ]": greek_date,
            "[ΩΡΑ]": meeting_time,
            "[ΗΜΕΡΗΣΙΑ_ΔΙΑΤΑΞΗ]": agenda_html,
            "[ZOOM_LINK]": zoom_link or "#",
        }
        subject = f"Πρόσκληση - Συνεδρίαση {meeting_ref}"
        campaign_name = f"Πρόσκληση {meeting_ref}"
        list_ids = ctx.get("brevo_list_ids") or settings.brevo.newsletter_list_ids or []
        location = "Zoom" if "ΔΙΑΔΙΚΤΥΑΚ" in str(meeting_type).upper() else ctx.get("meeting_location", "")
        preview_text = f"{greek_date}, {meeting_time}, {location}".strip(", ")

        return template_params, subject, campaign_name, list_ids, preview_text

    async def _step_send_newsletter(self, ctx: dict[str, Any]) -> StepResult:
        """Create the Brevo campaign.

        test_mode: save as draft + send ONE test email to test_email.  Draft
                   stays in Brevo for review.  Halts at confirm_newsletter gate.
        live:      save AND send immediately to newsletter_list_ids.  Skips the
                   confirm_newsletter gate (handler short-circuits via
                   ``newsletter_sent`` flag).  Publishes the bus event here.
        """
        template_id = ctx.get("brevo_template_id") or settings.brevo.newsletter_template_id

        if not template_id:
            return StepResult(
                success=True,
                data={"newsletter_skipped": True},
                message="Newsletter skipped - set brevo.newsletter_template_id in config.yaml",
            )

        test_addr = settings.testing.test_email
        template_params, subject, campaign_name, list_ids, preview_text = self._build_newsletter_params(ctx)

        fallback_list = settings.brevo.master_list_id
        creation_list_ids = list_ids if list_ids else ([fallback_list] if fallback_list else [])

        if not creation_list_ids:
            return StepResult(
                success=True,
                data={"newsletter_skipped": True},
                message="Newsletter skipped - no list IDs available (set brevo.master_list_id or brevo.newsletter_list_ids)",
            )

        test_mode = bool(ctx.get("test_mode"))

        async def _create_campaign(list_ids_attempt: list[int]) -> dict:
            return await self.brevo.send_campaign(
                template_id=template_id,
                list_ids=list_ids_attempt,
                subject=subject,
                params=template_params,
                campaign_name=campaign_name,
                preview_text=preview_text or None,
                test_emails=[test_addr] if test_addr else None,
                workflow=self.name,
            )

        try:
            try:
                result = await _create_campaign(creation_list_ids)
            except Exception as primary_err:
                if fallback_list and creation_list_ids != [fallback_list]:
                    logger.warning(
                        "Campaign creation failed with list %s (%s) - retrying with master list %d",
                        creation_list_ids, primary_err, fallback_list,
                    )
                    result = await _create_campaign([fallback_list])
                    result["used_fallback_list"] = True
                else:
                    raise
            campaign_id = result.get("campaign_id")

            if test_mode:
                msg = (
                    f"Test email sent to {test_addr} - draft will be deleted on cleanup"
                    if test_addr
                    else f"Campaign draft created (id={campaign_id}) - no test email configured"
                )
                return StepResult(
                    success=True,
                    data={
                        "newsletter_campaign_id": campaign_id,
                        "newsletter_test_sent": bool(test_addr),
                        "newsletter_test_addr": test_addr or "",
                        "newsletter_skipped": True,  # halt at confirm gate; no live send
                    },
                    message=msg,
                )

            # ── Live mode: send NOW, skip the confirm gate ───────────────────
            if not list_ids:
                # Live mode requires real list IDs; only draft was created.
                return StepResult(
                    success=True,
                    data={
                        "newsletter_campaign_id": campaign_id,
                        "newsletter_test_addr": test_addr or "",
                        "newsletter_list_ids": list_ids,
                        "newsletter_sent": False,
                        "newsletter_skipped": True,
                    },
                    message=f"Campaign draft created (id={campaign_id}) - newsletter_list_ids empty, not sent live",
                )

            try:
                await self.brevo.send_campaign_now(campaign_id, workflow=self.name)
            except Exception as send_err:
                logger.warning("Live newsletter send failed (non-fatal): %s", send_err)
                return StepResult(
                    success=True,
                    data={
                        "newsletter_campaign_id": campaign_id,
                        "newsletter_sent": False,
                        "newsletter_skipped": True,
                    },
                    message=f"Live send failed (campaign kept as draft): {send_err}",
                )

            # Publish bus event AFTER successful live send
            await _publish_board_meeting_scheduled(ctx)

            return StepResult(
                success=True,
                data={
                    "newsletter_campaign_id": campaign_id,
                    "newsletter_test_addr": test_addr or "",
                    "newsletter_list_ids": list_ids,
                    "newsletter_sent": True,
                    "bus_event_published": True,
                },
                message=f"Newsletter sent live (campaign {campaign_id}, lists {list_ids})",
            )
        except Exception as e:
            err_text = str(e).lower()
            hint = ""
            if "sender is invalid" in err_text or "sender is inactive" in err_text:
                hint = (
                    f" → Sender '{settings.brevo.sender_email}' is not verified in Brevo. "
                    "Verify it at https://app.brevo.com/senders/list (Senders & IPs → Senders), "
                    "or change brevo.sender_email in config.yaml to an already-verified address."
                )
            elif "list ids are not valid" in err_text or "list id" in err_text:
                hint = (
                    f" → List ID {creation_list_ids} does not exist in Brevo. "
                    "Check brevo.master_list_id in config.yaml."
                )
            elif "unauthorized" in err_text or "401" in err_text:
                hint = (
                    " → Brevo API key invalid or IP not authorised. "
                    "Whitelist your IP at https://app.brevo.com/security/authorised_ips."
                )
            logger.warning("Newsletter campaign creation/test failed (non-fatal): %s%s", e, hint)
            return StepResult(
                success=True,
                data={"newsletter_skipped": True},
                message=f"Newsletter campaign failed (non-fatal): {e}{hint}",
            )

    async def _step_confirm_newsletter(self, ctx: dict[str, Any]) -> StepResult:
        """Confirm gate - in live mode this is a no-op (newsletter already sent).

        In test_mode the runner halts BEFORE running this step because the
        ``WorkflowStep`` has ``requires_approval=True``.  The CLI handles the
        user prompt and either resumes (this method runs → no-op) or rolls back.
        """
        # Live mode: already sent during send_newsletter → just acknowledge
        if ctx.get("newsletter_sent"):
            return StepResult(
                success=True,
                data={"newsletter_sent": True},
                message="Newsletter already sent during send_newsletter (live mode)",
            )

        # Test mode (or draft kept for any other reason): no live send here.
        # When the user resumes through the test-mode confirm gate, treat that
        # as a simulated publish: fire board.meeting.scheduled with
        # test_mode=True so platform_bridge spins up the full Discord
        # choreography (public thread, scheduled event, reminder DM) against
        # sandbox channels.  Rollback at end-of-run publishes CANCELLED, which
        # tears the test artefacts back down.
        if ctx.get("newsletter_skipped"):
            if ctx.get("test_mode"):
                await _publish_board_meeting_scheduled(ctx)
                return StepResult(
                    success=True,
                    data={"newsletter_sent": False, "bus_event_published": True},
                    message="Test-mode confirm: published scheduled event (sandbox channels)",
                )
            return StepResult(
                success=True,
                data={"newsletter_sent": False},
                message="Newsletter draft retained - no live send",
            )

        # Shouldn't normally reach here.
        return StepResult(
            success=True,
            data={"newsletter_sent": False},
            message="No newsletter to send.",
        )


def _derive_meeting_id(ctx: dict[str, Any]) -> str:
    """Construct a stable meeting_id from workflow context.

    New format: ``board_meeting:ΔΣXX-YYYY``  (matches the agenda tab name).
    Falls back to building from meeting_number + meeting_date year if
    raw_meeting_id is missing.

    Returns "" if no usable data is present.
    """
    raw_id = (ctx.get("raw_meeting_id") or "").strip()
    if raw_id:
        return f"board_meeting:{raw_id}"

    meeting_number = ctx.get("meeting_number", "")
    meeting_date = ctx.get("meeting_date", "")
    if meeting_number and meeting_date and len(meeting_date) >= 4:
        seq_str = str(meeting_number).zfill(2) if str(meeting_number).isdigit() else str(meeting_number)
        year = meeting_date[:4]
        return f"board_meeting:ΔΣ{seq_str}-{year}"
    return ""


async def _publish_board_meeting_scheduled(ctx: dict[str, Any]) -> None:
    """Publish EVENT_BOARD_MEETING_SCHEDULED to the bus (non-fatal)."""
    try:
        from src.core.event_bus import bus
        from src.core.events import (
            EVENT_BOARD_MEETING_SCHEDULED,
            BoardMeetingScheduledPayload,
        )

        meeting_date = ctx.get("meeting_date", "")
        meeting_time = ctx.get("meeting_time", "")
        meeting_id = _derive_meeting_id(ctx)

        if not meeting_id:
            logger.warning("_publish_board_meeting_scheduled: no meeting_id derivable, skipping publish")
            return

        try:
            if meeting_time:
                dt_str = f"{meeting_date}T{meeting_time}:00"
                meeting_dt = datetime.fromisoformat(dt_str).replace(tzinfo=_ATHENS_TZ)
            else:
                d = _date.fromisoformat(meeting_date)
                meeting_dt = datetime(d.year, d.month, d.day, tzinfo=_ATHENS_TZ)
        except (ValueError, TypeError) as exc:
            logger.warning("_publish_board_meeting_scheduled: could not parse datetime: %s", exc)
            return

        agenda_items = ctx.get("agenda_items", [])
        if agenda_items:
            agenda_summary = "\n".join(f"{i + 1}. {item}" for i, item in enumerate(agenda_items))
        else:
            agenda_summary = "(κατόπιν ανακοίνωσης)"

        board_member_emails = [m.email for m in settings.workflows.board_meeting.board_members]

        await bus.publish(
            EVENT_BOARD_MEETING_SCHEDULED,
            BoardMeetingScheduledPayload(
                meeting_id=meeting_id,
                starts_at=meeting_dt,
                zoom_url=ctx.get("zoom_join_url", ""),
                agenda_summary=agenda_summary,
                board_member_emails=board_member_emails,
                test_mode=bool(ctx.get("test_mode")),
            ),
        )
        logger.info("_publish_board_meeting_scheduled: published %s", meeting_id)

        # Persist to DB so the bot creates the public agenda thread + Discord
        # scheduled event even when it was offline during the bus publish above
        # (CLI / FastAPI webhook and bot run in separate processes).
        try:
            from src.integrations.discord.scheduler import PendingActionsStore
            await PendingActionsStore().enqueue(
                action_type="board_meeting_scheduled",
                payload={
                    "meeting_id": meeting_id,
                    "starts_at": meeting_dt.isoformat(),
                    "zoom_url": ctx.get("zoom_join_url", "") or "",
                    "agenda_summary": agenda_summary,
                    "board_member_emails": board_member_emails,
                    "test_mode": bool(ctx.get("test_mode")),
                },
                due_at=datetime.now(timezone.utc),
            )
            logger.info("Enqueued board_meeting_scheduled pending action for %s", meeting_id)
        except Exception as pa_err:
            logger.warning("Could not enqueue board_meeting_scheduled (non-fatal): %s", pa_err)
    except Exception as exc:
        logger.warning("Bus publish board.meeting.scheduled failed (non-fatal): %s", exc)


async def _fetch_next_protocol_number(onedrive_client) -> str:
    if not settings.ms_client_id or not settings.ms_tenant_id:
        logger.debug("MS credentials not configured - skipping auto protocol fetch")
        return ""

    try:
        year = _date.today().year
        # Awaited directly - the workflow already runs inside an event loop, so
        # asyncio.run() here would raise "cannot be called from a running loop".
        return await onedrive_client.get_next_protocol_number(year)
    except Exception as e:
        logger.warning("Could not fetch protocol number from SharePoint Excel: %s", e)
        return ""


def _scan_form_labels(rows: list) -> dict:
    _KNOWN_LABELS = {
        "ΑΡΙΘΜΟΣ ΣΥΝΕΔΡΙΑΣΗΣ",
        "ΤΥΠΟΣ",
        "ΗΜΕΡΟΜΗΝΙΑ",
        "ΩΡΑ ΕΝΑΡΞΗΣ",
        "ΤΟΠΟΘΕΣΙΑ",
        "ΠΡΟΣΚΛΗΣΗ",
    }

    result: dict = {}
    try:
        result["ΑΡΙΘΜΟΣ ΣΥΝΕΔΡΙΑΣΗΣ"] = (str(rows[4][3]).strip(), 5)
    except IndexError:
        result["ΑΡΙΘΜΟΣ ΣΥΝΕΔΡΙΑΣΗΣ"] = ("", 5)

    for row_idx, row in enumerate(rows, 1):
        try:
            label = str(row[2]).strip().upper()
        except IndexError:
            continue
        if label in _KNOWN_LABELS and label != "ΑΡΙΘΜΟΣ ΣΥΝΕΔΡΙΑΣΗΣ":
            try:
                d_val = str(row[3]).strip()
            except IndexError:
                d_val = ""
            result[label] = (d_val, row_idx)
    return result


def _parse_sheet_date(raw: str) -> "date | None":
    from datetime import date as _d, timedelta as _td
    _SHEETS_EPOCH = _d(1899, 12, 30)
    _PLACEHOLDER_CUTOFF = _d(2020, 1, 1)

    if raw is None or str(raw).strip() in ("", "None", "nan"):
        return None
    raw_str = str(raw).strip()

    try:
        serial = float(raw_str)
        if serial > 0:
            result = _SHEETS_EPOCH + _td(days=int(serial))
            if result <= _PLACEHOLDER_CUTOFF:
                return None
            return result
    except (ValueError, TypeError):
        pass

    try:
        if " " in raw_str and "-" in raw_str:
            raw_str = raw_str.split(" ")[0]
        if raw_str.count("-") == 2:
            return _d.fromisoformat(raw_str)
        if "/" in raw_str:
            parts = raw_str.split("/")
            if len(parts) == 3:
                d, m, y = parts
                return _d(int(y), int(m), int(d))
    except (ValueError, TypeError):
        pass
    return None


def _parse_sheet_time(raw: str) -> str:
    if raw is None or str(raw).strip() in ("", "None", "nan"):
        return ""
    raw_str = str(raw).strip()
    try:
        frac = float(raw_str)
        if 0.0 < frac < 1.0:
            total_minutes = round(frac * 24 * 60)
            h, m = divmod(total_minutes, 60)
            time_str = f"{h:02d}:{m:02d}"
            return "" if time_str == "00:00" else time_str
    except (ValueError, TypeError):
        pass
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
    try:
        d = _date.fromisoformat(iso_date)
        return f"{d.day} {_GREEK_MONTHS[d.month]} {d.year}"
    except (ValueError, TypeError, IndexError):
        return iso_date


def _meeting_type_genitive(meeting_type: str) -> str:
    t = (meeting_type or "ΤΑΚΤΙΚΗ").strip().upper()
    if t in ("ΤΑΚΤΙΚΗ", "ΤΑΚΤΙΚΗΣ"):
        return "ΤΑΚΤΙΚΗΣ"
    if t in ("ΕΚΤΑΚΤΗ", "ΕΚΤΑΚΤΗΣ"):
        return "ΕΚΤΑΚΤΗΣ"
    return t
