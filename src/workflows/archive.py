"""Archive workflow (Phase 1 + 2): file a PDF into SharePoint + πρωτόκολλο.

Six steps:
  1. intake             - validate the input file (PDF only) + extract text
  2. extract_metadata   - LLM proposes title / tags / Κύρια Σημεία; fallback
                          to the recent-entries second pass if low confidence
                          or ad-hoc category
  3. resolve_protocol   - reuse an existing protocol number if the doc already
                          has one, otherwise reserve the next available number
                          via the SQLite protocol_reservations table
  4. upload_and_register- upload PDF to ``Αρχείο/Αρχείο ανά έτος/{year}/`` and
                          append a row to ``[Πρωτόκολλο] Αρχείο ΔΣ.xlsx``
  5. notify             - Phase 1 prints a CLI summary; Phase 3 will replace
                          this with a threaded email reply
  6. revision_window    - Phase 2: park the workflow in ``revision_open`` state
                          for 72h so ``ai-assistant archive review`` can amend it

Rollback unwinds:
  - delete protocol row
  - delete uploaded SharePoint file
  - release the protocol reservation (the uncommitted ones; committed rows
    stay so we can audit who grabbed which number)
  - delete the local PDF copy (only if we made one - never the original)
"""
from __future__ import annotations

import logging
import re
import shutil
from datetime import date as _date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from src.config import settings
from src.core.audit import (
    commit_protocol_reservation,
    release_protocol_reservation,
    reserve_next_protocol_number,
)
from src.core.workflow import BaseWorkflow, StepResult, WorkflowStep
from src.integrations.onedrive import OneDriveClient
from src.utils.pdf_text import EncryptedPDFError, extract_pdf_text
from src.workflows import archive_llm

logger = logging.getLogger(__name__)


_REVISION_HOURS = 72
_CONFIDENCE_FLOOR = 0.7
# Minimum title-match confidence required to auto-fill a SecGen reservation.
# Below this we defer to SecGen via Discord DM / CLI resolve.  Matches the
# user's "if confidence > 0.7 that the file is indeed the file for which we
# reserved a protokollo number" spec on 2026-05-27.
_RESERVATION_MATCH_FLOOR = 0.7
_PROTO_RE = re.compile(r"^\d{4}[_-]\d+$")
_UPO_EXETASI = "[ΥΠΟ ΕΞΕΤΑΣΗ]"

# Matches "[YYYY_NNN] <title>.<ext>" filenames (the convention users adopt
# when they have a pre-assigned protocol number).  Extracts the title verbatim;
# used in _step_extract_metadata to override the LLM when this pattern is
# present.  Supports the common archivable extensions.
_FILENAME_TITLE_RE = re.compile(
    r"^\[(?P<proto>\d{4}[_-]\d+)\]\s+(?P<title>.+?)\.(?:pdf|docx?|odt|rtf|jpe?g|png|gif|bmp|tiff?|heic|heif)$",
    re.IGNORECASE,
)


class ArchiveWorkflow(BaseWorkflow):
    """Submit a PDF to the institutional archive (SharePoint + πρωτόκολλο)."""

    def __init__(self, actor: str = "secgen") -> None:
        self._onedrive: OneDriveClient | None = None
        super().__init__(actor=actor)

    @property
    def onedrive(self) -> OneDriveClient:
        if self._onedrive is None:
            self._onedrive = OneDriveClient()
        return self._onedrive

    @property
    def name(self) -> str:
        return "archive"

    def define_steps(self) -> list[WorkflowStep]:
        return [
            WorkflowStep("intake", "Load file + sender metadata"),
            WorkflowStep("extract_metadata", "Run LLM to extract title/tags/Σημεία"),
            WorkflowStep("resolve_protocol", "Determine protocol number"),
            # Step name preserved as "collision_check" for backwards compat
            # with in-flight workflow_state rows after the 2026-05-27 logic
            # rewrite (the step's BEHAVIOUR is now pre-existence check).
            WorkflowStep("collision_check", "Check πρωτόκολλο for an existing entry / SecGen reservation"),
            WorkflowStep("upload_and_register", "Upload to SharePoint + append protocol row"),
            WorkflowStep("notify", "Notify (CLI summary in Phase 1)"),
            WorkflowStep("revision_window", "Open 72h revision window"),
        ]

    @staticmethod
    def debug_fixture() -> dict[str, Any]:
        """Canonical fake ctx for the `debug run archive <step>` command.

        Every key any ``_step_*`` reads from ctx is present with a safe fake
        value so a step can be invoked in isolation without a KeyError.  The
        debug runner forces ``test_mode=True`` (so SharePoint/xlsx writes are
        skipped); it is intentionally NOT set here.
        """
        return {
            # intake
            "pdf_path": "data/debug/sample.pdf",          # intake reads/validates this path
            "sender_email": "debug@amnesty.org.gr",       # intake
            "sender_name": "Debug Sender",                # intake
            "email_subject": "[Debug] Δοκιμαστικό έγγραφο",  # intake / extract_metadata
            "email_body": "Δοκιμαστικό σώμα email.",      # intake / extract_metadata
            "_skip_workbook_refresh": True,               # intake: skip live xlsx snapshot
            # extract_metadata
            "_skip_llm": True,                            # extract_metadata: bypass live LLM call
            "pdf_filename_orig": "[2099_999] Δοκιμαστικός τίτλος.pdf",  # extract_metadata title source
            "pdf_text": "Δοκιμαστικό περιεχόμενο PDF.",   # extract_metadata
            "pdf_metadata": {"page_count": 1, "char_count": 30, "is_scan": False},  # intake/notify
            "llm_result": {                               # extract_metadata result + downstream consumers
                "title": "Δοκιμαστικός τίτλος",
                "labels": ["Διοικητικά", "Δοκιμή"],
                "key_points": "Δοκιμαστικά κύρια σημεία.",
                "confidence": 0.95,
                "category_matched": "Διοικητικά",
                "existing_protocol": "",
            },
            "override_title": "",                         # extract_metadata / resolve_protocol overrides
            "override_labels": [],
            # Pre-set so `debug run archive resolve_protocol` echoes this value
            # instead of reserving a fresh row from the protocol DB on every run
            # (keeps repeated debug runs side-effect-free). Use --set
            # 'override_protocol=' to exercise the live-reserve path instead.
            "override_protocol": "2099_999",              # resolve_protocol CLI override
            # resolve_protocol / collision_check / upload_and_register
            "protocol_number": "2099_999",
            "protocol_source": "cli_override",            # collision_check branch selector
            "is_filling_reservation": False,              # upload_and_register / rollback
            "reserved_row": {},                           # upload_and_register reservation-fill source
            # upload_and_register outputs (read by notify + rollback)
            "remote_filename": "[2099_999] Δοκιμαστικός τίτλος.pdf",
            "remote_folder": "Αρχείο/Αρχείο ανά έτος/2099",
            "upload_file_id": "debug-file-id",
            "share_link": "https://example.invalid/share/debug",
            "local_copy_path": "",                        # rollback: local staging copy
        }

    async def execute_step(self, step: WorkflowStep, context: dict[str, Any]) -> StepResult:
        handler = getattr(self, f"_step_{step.name}", None)
        if handler is None:
            return StepResult(success=False, message=f"No handler for step: {step.name}")
        return await handler(context)

    # ── rollback ──────────────────────────────────────────────────────────────

    async def rollback(self, ctx: dict[str, Any]) -> None:
        """Undo the workflow's side effects in reverse order.

        CRITICAL: when ``ctx['test_mode']`` is True, we MUST NOT touch
        SharePoint.  Test-mode runs never wrote there in the first place
        (``_step_upload_and_register`` short-circuits), so a rollback that
        called delete_protocol_row would catastrophically remove a PRE-EXISTING
        production row that just happens to share the protocol number.  This
        actually happened in production on 2026-05-27 - two real rows were
        deleted via a test-mode cancel before this guard was added.
        """
        test_mode = bool(ctx.get("test_mode"))
        is_filling_reservation = bool(ctx.get("is_filling_reservation"))
        protocol_number = (ctx.get("protocol_number") or "").strip()
        year_match = re.match(r"^(\d{4})", protocol_number) if protocol_number else None
        year_str = year_match.group(1) if year_match else ""

        # 1) Delete protocol row (non-fatal)
        # Skipped in two cases:
        #   • test mode - the row was never written.
        #   • reservation-fill - SecGen made the row before we touched it.
        #     Rolling back our partial work must NOT delete SecGen's row.
        skip_row_delete_reason = ""
        if test_mode:
            skip_row_delete_reason = "test mode (no row was ever written)"
        elif is_filling_reservation:
            skip_row_delete_reason = "filling SecGen reservation (row pre-existed; not ours to delete)"
        if protocol_number and not skip_row_delete_reason:
            try:
                await self.onedrive.delete_protocol_row(protocol_number)
                logger.info("Rollback: deleted protocol row %s", protocol_number)
            except Exception as exc:
                logger.warning("Rollback: delete_protocol_row failed (non-fatal): %s", exc)
        elif protocol_number:
            logger.info(
                "Rollback: skipping delete_protocol_row for %s - %s.",
                protocol_number, skip_row_delete_reason,
            )

        # 2) Delete uploaded SharePoint file (non-fatal) - test_mode skips
        remote_filename = ctx.get("remote_filename") or ""
        if remote_filename and year_str and ctx.get("upload_file_id") and not test_mode:
            try:
                remote_path = f"{settings.onedrive.yearly_subfolder}/{year_str}/{remote_filename}"
                await self.onedrive.delete_file(remote_path, workflow=self.name)
                logger.info("Rollback: deleted archived PDF %s", remote_path)
            except Exception as exc:
                logger.warning("Rollback: delete_file failed (non-fatal): %s", exc)
        elif remote_filename and test_mode:
            logger.info(
                "Rollback (TEST MODE): skipping delete_file for %s - "
                "no file was uploaded.",
                remote_filename,
            )

        # 3) Release uncommitted reservations (committed ones stay for audit)
        try:
            released = release_protocol_reservation(self.workflow_id)
            if released:
                logger.info("Rollback: released %d uncommitted reservation(s)", released)
        except Exception as exc:
            logger.warning("Rollback: release_protocol_reservation failed: %s", exc)

        # 4) Delete the local PDF copy IF we made one (never delete the user's
        # original input file - only files we copied into data/output/).
        local_copy = ctx.get("local_copy_path") or ""
        if local_copy:
            try:
                p = Path(local_copy)
                if p.exists():
                    p.unlink()
                    logger.info("Rollback: deleted local copy %s", local_copy)
            except Exception as exc:
                logger.warning("Rollback: delete local copy failed: %s", exc)

    # ── steps ─────────────────────────────────────────────────────────────────

    async def _step_intake(self, ctx: dict[str, Any]) -> StepResult:
        """Validate the input file and extract text + metadata.

        Phase 5: non-PDF inputs (DOCX, ODT, JPG, PNG, etc.) are auto-converted
        to PDF via LibreOffice headless before the rest of the pipeline runs.
        The original filename (incl. its real extension) is preserved in
        ``pdf_filename_orig`` so the LLM still sees what the sender actually
        sent - useful for title selection.
        """
        from src.utils.pdf_convert import (
            ConversionError, convert_to_pdf, is_pdf, needs_conversion,
        )

        path_str = ctx.get("pdf_path") or ""
        if not path_str:
            return StepResult(success=False, message="No pdf_path provided.")
        input_path = Path(path_str)
        if not input_path.exists():
            return StepResult(success=False, message=f"File not found: {input_path}")

        # Original filename - used downstream by the LLM and the final archive
        # name (we strip the converter's extension and append "[YYYY_NNN] <title>.pdf").
        original_filename = input_path.name
        converted_from: str | None = None

        if not is_pdf(input_path):
            if not needs_conversion(input_path):
                return StepResult(
                    success=False,
                    message=(
                        f"Unsupported file type {input_path.suffix!r}.  "
                        "Παρακαλώ στείλτε PDF, DOCX, ODT, RTF, ή εικόνα "
                        "(JPG/PNG/HEIC)."
                    ),
                )
            try:
                pdf_path = convert_to_pdf(input_path)
            except ConversionError as exc:
                return StepResult(
                    success=False,
                    message=f"Conversion to PDF failed: {exc}",
                )
            converted_from = input_path.suffix.lower()
            logger.info("Converted %s → %s", input_path.name, pdf_path.name)
        else:
            pdf_path = input_path

        try:
            pdf_text, pdf_metadata = extract_pdf_text(pdf_path)
        except EncryptedPDFError as exc:
            return StepResult(success=False, message=str(exc))
        except Exception as exc:
            return StepResult(success=False, message=f"PDF parse failed: {exc}")

        sender_email = (ctx.get("sender_email") or "secgen@amnesty.org.gr").strip()

        # Take ONE snapshot of the πρωτόκολλο xlsx for the entire workflow run.
        # All subsequent reads (taxonomy, categories, recent entries, row
        # lookups, max-seq) use this snapshot via OneDriveClient's backup
        # path - one network download per run instead of one per read.
        # Skipped in test mode (the test harness stubs read methods).
        if not ctx.get("test_mode") and not ctx.get("_skip_workbook_refresh"):
            try:
                await self.onedrive.refresh_protocol_workbook()
            except Exception as exc:
                # Non-fatal: read methods will fall back to fresh per-call
                # downloads if the snapshot is missing.
                logger.warning(
                    "Could not refresh πρωτόκολλο snapshot at workflow start "
                    "(reads will re-download per-call): %s", exc,
                )

        data: dict[str, Any] = {
            "pdf_path": str(pdf_path),
            "pdf_filename_orig": original_filename,
            "pdf_text": pdf_text,
            "pdf_metadata": pdf_metadata,
            "sender_email": sender_email,
            "sender_name": ctx.get("sender_name") or "",
            "email_subject": ctx.get("email_subject") or "",
            "email_body": ctx.get("email_body") or "",
        }
        if converted_from:
            data["converted_from"] = converted_from
            data["converted_pdf_path"] = str(pdf_path)

        convert_note = f" (converted from {converted_from})" if converted_from else ""
        return StepResult(
            success=True,
            data=data,
            message=(
                f"Intake OK: {original_filename}{convert_note} "
                f"({pdf_metadata.get('page_count', 0)} pages, "
                f"{pdf_metadata.get('char_count', 0)} chars, "
                f"scan={pdf_metadata.get('is_scan', False)})"
            ),
        )

    async def _step_extract_metadata(self, ctx: dict[str, Any]) -> StepResult:
        """Run the LLM (first pass + optional fallback) to extract metadata."""
        if ctx.get("_skip_llm"):
            # Test-mode escape hatch: caller has already filled ``llm_result``
            llm_result = ctx.get("llm_result") or {}
            return StepResult(success=True, data={"llm_result": llm_result}, message="LLM skipped")

        filename = ctx.get("pdf_filename_orig") or ""
        try:
            initial = await archive_llm.classify_document(
                filename=filename,
                sender_email=ctx.get("sender_email", ""),
                sender_name=ctx.get("sender_name", ""),
                subject=ctx.get("email_subject", ""),
                body=ctx.get("email_body", ""),
                pdf_text=ctx.get("pdf_text", ""),
                pdf_metadata=ctx.get("pdf_metadata") or {},
                workflow=self.name,
            )
        except Exception as exc:
            return StepResult(success=False, message=f"LLM classify_document failed: {exc}")

        final = initial
        fallback_used = False
        confidence = float(initial.get("confidence") or 0.0)
        cat = (initial.get("category_matched") or "").lower()
        if confidence < _CONFIDENCE_FLOOR or cat == "ad-hoc":
            try:
                recent = await self.onedrive.read_recent_entries()
            except Exception as exc:
                logger.warning("read_recent_entries failed (non-fatal): %s", exc)
                recent = []
            if recent:
                try:
                    final = await archive_llm.refine_against_recent(
                        initial_result=initial,
                        recent_entries=recent,
                        document_context={
                            "filename": filename,
                            "sender_name": ctx.get("sender_name", ""),
                            "sender_email": ctx.get("sender_email", ""),
                            "subject": ctx.get("email_subject", ""),
                        },
                        workflow=self.name,
                    )
                    fallback_used = True
                except Exception as exc:
                    logger.warning("LLM refine pass failed (non-fatal): %s", exc)

        # Filename-title fallback (deterministic, ahead of any LLM creativity).
        # If the filename matches "[YYYY_NNN] <title>.<ext>", the user clearly
        # intends <title> to be the document's title.  The LLM has been known
        # to hallucinate alternate titles (e.g. substituting the SENDER's
        # name for a candidate's name) so we prefer the filename when present.
        # Override order: CLI > filename pattern > LLM.
        if not ctx.get("override_title"):
            m = _FILENAME_TITLE_RE.match(filename)
            if m:
                extracted = m.group("title").strip()
                if extracted and extracted != final.get("title", ""):
                    logger.info(
                        "Overriding LLM title %r with filename-extracted %r",
                        final.get("title"), extracted,
                    )
                    final["title"] = extracted

        # CLI overrides win
        if ctx.get("override_title"):
            final["title"] = ctx["override_title"]
        if ctx.get("override_labels"):
            final["labels"] = ctx["override_labels"]

        # Low-confidence sentinel
        key_points = final.get("key_points") or ""
        if float(final.get("confidence") or 0.0) < _CONFIDENCE_FLOOR:
            if not key_points.startswith(_UPO_EXETASI):
                key_points = f"{_UPO_EXETASI} {key_points}".strip()
            final["key_points"] = key_points

        return StepResult(
            success=True,
            data={
                "llm_result": final,
                "llm_fallback_used": fallback_used,
            },
            message=(
                f"LLM result: title={final.get('title', '?')!r}, "
                f"labels={final.get('labels', [])}, "
                f"confidence={final.get('confidence', 0):.2f}, "
                f"category={final.get('category_matched', '?')!r}"
                + (" (fallback used)" if fallback_used else "")
            ),
        )

    async def _step_resolve_protocol(self, ctx: dict[str, Any]) -> StepResult:
        """Pick a protocol number: CLI > existing-in-doc > reserve-next."""
        # CLI override always wins
        override = (ctx.get("override_protocol") or "").strip()
        if override:
            if not _PROTO_RE.match(override):
                return StepResult(
                    success=False,
                    message=f"Invalid --proto value '{override}' (expected YYYY_NNN).",
                )
            return StepResult(
                success=True,
                data={"protocol_number": override, "protocol_source": "cli_override"},
                message=f"Protocol number set from CLI: {override}",
            )

        # LLM-detected existing protocol number on the document
        llm_result = ctx.get("llm_result") or {}
        existing = (llm_result.get("existing_protocol") or "").strip()
        if existing and _PROTO_RE.match(existing):
            # The document already carries this protocol number - trust it.
            # The pre-existence gate downstream (``_step_collision_check``)
            # will reject this if a different file is already archived under
            # the same number, so we don't need to re-check here.
            return StepResult(
                success=True,
                data={"protocol_number": existing, "protocol_source": "document"},
                message=f"Protocol number reused from document: {existing}",
            )

        # Otherwise reserve the next available number for the current year
        year = _date.today().year
        xlsx_max_seq = 0
        try:
            xlsx_max_seq = await self.onedrive.get_current_year_max_seq(year)
        except Exception as exc:
            logger.warning("get_current_year_max_seq failed (non-fatal, using 0): %s", exc)

        reserved = reserve_next_protocol_number(
            year=year,
            workflow_id=self.workflow_id,
            xlsx_max_seq=xlsx_max_seq,
        )
        return StepResult(
            success=True,
            data={"protocol_number": reserved, "protocol_source": "reserved"},
            message=f"Protocol number reserved: {reserved}",
        )

    async def _step_collision_check(self, ctx: dict[str, Any]) -> StepResult:
        """Pre-existence check (renamed from the old "collision_check" - same
        step slot, new semantics as of 2026-05-27).

        The previous collision-gate model is GONE.  The bot now NEVER
        overwrites an existing archive entry under any circumstances.  Manual
        intervention by the SecGen is required for that case.

        Three branches:

        * ``protocol_source == "reserved"`` - fresh reservation owned by THIS
          workflow.  No row exists yet by construction.  Proceed.

        * **No πρωτόκολλο row** for the claimed number - proceed.  The user
          claimed a number nobody else has used; we'll append normally.

        * **Row exists AND a file exists in SharePoint** at
          ``Αρχείο ανά έτος/{year}/[{proto}] *.pdf`` - HARD FAIL.  Refuse to
          overwrite.  This is the safety guardrail: SecGen must move/rename
          the existing file manually before we'll touch anything.

        * **Row exists, no file** - treated as a SecGen pre-reservation.
          Compare the LLM-extracted title against the row's title:
          - **Match** (substring containment, normalised) → proceed in
            "filling-reservation" mode (see ``_step_upload_and_register``
            for what changes).
          - **Mismatch** → park the workflow.  Publish event
            ``archive.reservation_confirmation_needed`` so SecGen can confirm
            via Discord DM or CLI (`ai-assistant archive resolve <id>`).

        Test mode (``ctx['test_mode']``) skips this check entirely so we
        never block test runs on prior production state.
        """
        if ctx.get("test_mode"):
            return StepResult(success=True, data={},
                              message="Pre-existence check skipped (test mode).")

        protocol_source = ctx.get("protocol_source", "")
        if protocol_source == "reserved":
            return StepResult(success=True, data={},
                              message="Fresh reservation; no pre-existing row possible.")

        proto = (ctx.get("protocol_number") or "").strip()
        if not proto:
            return StepResult(success=False, message="No protocol_number to check.")

        # Look up the row in the πρωτόκολλο xlsx
        try:
            existing_row = await self.onedrive.find_protocol_row(proto)
        except Exception as exc:
            # Best-effort: if the lookup itself fails, fail OPEN (let the
            # workflow proceed) - the upload step will surface any real
            # collision via SharePoint's own error.
            logger.warning("Pre-existence check: find_protocol_row failed: %s", exc)
            return StepResult(
                success=True,
                data={"pre_existence_check": "skipped_lookup_error"},
                message=f"Row lookup failed (non-fatal): {exc}",
            )

        if not existing_row:
            return StepResult(
                success=True,
                data={"pre_existence_check": "no_row"},
                message=f"Protocol {proto} not yet in πρωτόκολλο - claim is free.",
            )

        # Row exists - check if a file is also archived for this number.
        try:
            file_exists = await self.onedrive.file_exists_for_protocol(proto)
        except Exception as exc:
            logger.warning(
                "Pre-existence check: file_exists_for_protocol failed (treating "
                "as 'no file' to fail-open): %s", exc,
            )
            file_exists = False

        if file_exists:
            # HARD FAIL - never overwrite an archived file.  SecGen handles
            # this case manually outside the bot.
            return StepResult(
                success=False,
                message=(
                    f"Πρωτόκολλο {proto} έχει ήδη αρχειοθετηθεί (υπάρχει και "
                    f"εγγραφή και αρχείο στο SharePoint).  Το bot δεν αντικαθιστά "
                    f"υπάρχοντα αρχεία - επικοινωνήστε με τη Γραμματεία για "
                    f"χειροκίνητη επέμβαση."
                ),
            )

        # Row exists, no file - treat as a SecGen pre-reservation.  Decide
        # whether to fill it automatically or defer.
        existing_title = (existing_row.get("title") or "").strip()
        proposed_title = ((ctx.get("llm_result") or {}).get("title") or "").strip()
        match_confidence = _title_match_confidence(existing_title, proposed_title)

        if match_confidence >= _RESERVATION_MATCH_FLOOR:
            ctx["is_filling_reservation"] = True
            ctx["reserved_row"] = existing_row
            return StepResult(
                success=True,
                data={
                    "pre_existence_check": "filling_reservation",
                    "reserved_row": existing_row,
                    "title_match_confidence": match_confidence,
                },
                message=(
                    f"Filling SecGen reservation for {proto}: "
                    f"row title '{existing_title}' ↔ proposed '{proposed_title}' "
                    f"(confidence={match_confidence:.2f})"
                ),
            )

        # Title mismatch - defer to SecGen for confirmation.
        pending = {
            "protocol_number": proto,
            "existing_row": existing_row,
            "existing_title": existing_title,
            "proposed_title": proposed_title,
            "match_confidence": match_confidence,
            "raised_at": datetime.now(timezone.utc).isoformat(),
        }
        ctx["pending_reservation_confirmation"] = pending

        try:
            from src.core.event_bus import bus
            await bus.publish("archive.reservation_confirmation_needed", {
                "workflow_id": self.workflow_id,
                "protocol_number": proto,
                "existing_title": existing_title,
                "proposed_title": proposed_title,
                "match_confidence": match_confidence,
                "raised_at": pending["raised_at"],
            })
        except Exception as exc:  # pragma: no cover - best-effort
            logger.debug("reservation-confirm event publish failed (non-fatal): %s", exc)

        return StepResult(
            success=False,
            data={"pending_reservation_confirmation": pending},
            message=(
                f"RESERVATION_CONFIRMATION_NEEDED: protocol {proto} is reserved "
                f"with title '{existing_title}', but the submitted document looks "
                f"like '{proposed_title}' (match confidence {match_confidence:.2f}). "
                f"SecGen must confirm via "
                f"`ai-assistant archive resolve {self.workflow_id} approve|reject`."
            ),
        )

    async def _step_upload_and_register(self, ctx: dict[str, Any]) -> StepResult:
        """Upload the PDF and append the protocol row."""
        test_mode = bool(ctx.get("test_mode"))
        protocol_number = ctx.get("protocol_number") or ""
        llm_result = ctx.get("llm_result") or {}
        labels = llm_result.get("labels") or []
        key_points = llm_result.get("key_points") or ""
        is_filling_reservation = bool(ctx.get("is_filling_reservation"))
        reserved_row = ctx.get("reserved_row") or {}

        # ── Title precedence ────────────────────────────────────────────────
        # Reservation-filling mode: SecGen's row title is definitive (per user
        # spec 2026-05-27).  Falls back to LLM/filename only if the row's
        # title cell is empty.
        # Normal mode: LLM title → filename → "untitled".
        if is_filling_reservation:
            secgen_title = (reserved_row.get("title") or "").strip()
            title = secgen_title or (llm_result.get("title") or "").strip() or "untitled"
        else:
            title = (llm_result.get("title") or ctx.get("pdf_filename_orig") or "untitled").strip()

        year_match = re.match(r"^(\d{4})", protocol_number)
        if not year_match:
            return StepResult(
                success=False,
                message=f"protocol_number missing/invalid: {protocol_number!r}",
            )
        year = year_match.group(1)

        # Build the filename: [YYYY_NNN] {title}.pdf  (sanitised for filesystem)
        remote_filename = f"[{protocol_number}] {_sanitise_filename(title)}.pdf"
        remote_folder = f"{settings.onedrive.yearly_subfolder}/{year}"

        if test_mode:
            # Skip the real upload + xlsx write, just describe what would happen.
            return StepResult(
                success=True,
                data={
                    "remote_filename": remote_filename,
                    "remote_folder": remote_folder,
                    "upload_file_id": "",
                    "share_link": "",
                    "register_skipped": True,
                },
                message=(
                    f"[TEST] Would upload to {remote_folder}/{remote_filename} "
                    f"and append protocol row {protocol_number}"
                ),
            )

        pdf_path = Path(ctx.get("pdf_path") or "")
        if not pdf_path.exists():
            return StepResult(success=False, message=f"PDF disappeared: {pdf_path}")

        # Upload
        try:
            upload_result = await self.onedrive.upload_file(
                local_path=pdf_path,
                remote_folder=remote_folder,
                filename=remote_filename,
                workflow=self.name,
            )
        except Exception as exc:
            return StepResult(success=False, message=f"Upload failed: {exc}")

        file_id = upload_result.get("id", "")
        share_link = ""
        if file_id:
            try:
                share_link = await self.onedrive.get_share_link(file_id)
            except Exception as exc:
                logger.warning("Could not create share link (non-fatal): %s", exc)

        # ── xlsx write: append a new row OR fill a reservation in-place ─────
        labels_str = ", ".join(labels) if isinstance(labels, list) else str(labels)
        try:
            if is_filling_reservation:
                # FILL-BLANKS-ONLY semantics (user spec 2026-05-27): preserve
                # whatever SecGen already wrote in the row.  Only pass the
                # main_points / tags fields where SecGen left them blank.
                existing_kp = (reserved_row.get("key_points") or "").strip()
                existing_tags = (reserved_row.get("tags") or "").strip()
                update_kwargs: dict[str, str | None] = {}
                if not existing_kp and key_points:
                    update_kwargs["main_points"] = key_points
                if not existing_tags and labels_str:
                    update_kwargs["tags"] = labels_str
                if update_kwargs:
                    await self.onedrive.update_protocol_row(
                        protocol_number, **update_kwargs,
                    )
                    logger.info(
                        "Reservation-fill: updated blank fields %s on row %s",
                        list(update_kwargs.keys()), protocol_number,
                    )
                else:
                    logger.info(
                        "Reservation-fill: row %s already complete - only "
                        "attached the file, no metadata changes.",
                        protocol_number,
                    )
            else:
                await self.onedrive.append_protocol_row(
                    protocol_id=protocol_number,
                    date_str=_date.today().isoformat(),
                    title=title,
                    main_points=key_points,
                    tags=labels_str,
                )
        except Exception as exc:
            # We uploaded the file but couldn't write the row → roll back the upload.
            # Special case: ProtokolloLockedError from a 423 Locked response means
            # someone has the πρωτόκολλο xlsx open in Excel.  Surface that with
            # a clean Greek message instead of the raw HTTP exception.
            from src.integrations.m365.onedrive import ProtokolloLockedError
            logger.warning("πρωτόκολλο write failed; rolling back upload: %s", exc)
            try:
                await self.onedrive.delete_file(
                    f"{remote_folder}/{remote_filename}", workflow=self.name
                )
            except Exception as cleanup_exc:
                logger.warning("Cleanup delete also failed: %s", cleanup_exc)
            if isinstance(exc, ProtokolloLockedError):
                return StepResult(
                    success=False,
                    message=str(exc),   # already a user-facing Greek message
                )
            return StepResult(success=False, message=f"πρωτόκολλο write failed: {exc}")

        # Commit the reservation now that the xlsx row exists
        try:
            commit_protocol_reservation(self.workflow_id)
        except Exception as exc:
            logger.warning("commit_protocol_reservation failed (non-fatal): %s", exc)

        return StepResult(
            success=True,
            data={
                "remote_filename": remote_filename,
                "remote_folder": remote_folder,
                "upload_file_id": file_id,
                "share_link": share_link,
            },
            message=f"Archived to {remote_folder}/{remote_filename}; πρωτόκολλο updated.",
        )

    async def _step_notify(self, ctx: dict[str, Any]) -> StepResult:
        """Print a CLI summary (Phase 3 will replace with email reply)."""
        llm_result = ctx.get("llm_result") or {}
        summary_lines = [
            "",
            "  Archive entry created:",
            f"    Πρωτόκολλο: {ctx.get('protocol_number', '?')}",
            f"    Τίτλος:     {llm_result.get('title', '?')}",
            f"    Ετικέτες:   {', '.join(llm_result.get('labels', [])) or '-'}",
            f"    Confidence: {float(llm_result.get('confidence') or 0):.2f}",
            f"    Folder:     {ctx.get('remote_folder', '-')}",
            f"    File:       {ctx.get('remote_filename', '-')}",
        ]
        if ctx.get("share_link"):
            summary_lines.append(f"    Share link: {ctx['share_link']}")
        if ctx.get("test_mode"):
            summary_lines.append("    (TEST MODE - nothing was actually uploaded)")
        print("\n".join(summary_lines))
        return StepResult(success=True, data={}, message="CLI summary printed.")

    async def _step_revision_window(self, ctx: dict[str, Any]) -> StepResult:
        """Record the revision-window deadline; do NOT block.

        The workflow returns ``completed`` immediately - the revision window is
        enforced by ``ai-assistant archive review``, which checks
        ``revision_open_until`` before allowing amendments.  Storing the
        deadline in workflow context is enough; no background timer needed.
        """
        deadline = datetime.now(timezone.utc) + timedelta(hours=_REVISION_HOURS)
        return StepResult(
            success=True,
            data={
                "revision_open_until": deadline.isoformat(),
                "revision_hours": _REVISION_HOURS,
            },
            message=f"Revision window open until {deadline.isoformat()}",
        )


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


_BAD_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|]')


def _normalize_title(s: str) -> str:
    """Lowercase, accent-strip, and collapse whitespace for title comparison.

    Mirrors the M365 inbox subject matcher's normalisation so behaviour is
    consistent across the codebase.
    """
    import unicodedata as _ud
    if not s:
        return ""
    nfd = _ud.normalize("NFD", s)
    no_marks = "".join(c for c in nfd if _ud.category(c) != "Mn")
    return re.sub(r"\s+", " ", no_marks.casefold()).strip()


def _title_match_confidence(reserved: str, proposed: str) -> float:
    """Score 0.0-1.0 of how likely the proposed title refers to the reserved slot.

    Used by ``_step_collision_check`` to decide whether to auto-fill a
    SecGen-pre-reserved πρωτόκολλο row or defer to SecGen for confirmation.

    Scoring:
      • Either side empty → 0.0 (no signal; defer)
      • Normalised strings equal → 1.0
      • One is a substring of the other (after normalisation) → 0.85
      • Otherwise → 0.0

    The 0.85 substring case catches realistic edits like SecGen writing
    "Πρακτικά ΔΣ04" and the submitter providing "Πρακτικά ΔΣ04-2026" - or
    vice versa.  String-similarity could be added later (Levenshtein /
    token-set ratio); strict substring is the conservative starting point.
    """
    a = _normalize_title(reserved)
    b = _normalize_title(proposed)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if a in b or b in a:
        return 0.85
    return 0.0


def _sanitise_filename(name: str) -> str:
    """Strip filesystem-hostile characters and collapse whitespace."""
    cleaned = _BAD_FILENAME_CHARS.sub("-", name).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    # Limit length to a sensible 200 chars - Windows path limit safety
    return cleaned[:200] or "untitled"


def is_revision_window_open(ctx: dict[str, Any]) -> bool:
    """True if the workflow's revision window has NOT yet expired."""
    raw = (ctx or {}).get("revision_open_until") or ""
    if not raw:
        return False
    try:
        deadline = datetime.fromisoformat(raw)
    except (ValueError, TypeError):
        return False
    return datetime.now(timezone.utc) <= deadline


async def apply_amendments(
    workflow_id: str,
    ctx: dict[str, Any],
    amendments: dict[str, Any],
    *,
    onedrive: Any = None,
) -> dict[str, Any]:
    """Apply user-requested amendments to a completed archive entry.

    Touches three places (in this order, so a failure mid-flight leaves the
    system in a sane recoverable state):

      1. **SharePoint file** - rename if ``title`` or ``protocol_id`` changed
         (the leaf is ``[{protocol_id}] {title}.pdf``, so a change to either
         demands a new filename).
      2. **πρωτόκολλο xlsx row** - update title (col C), key_points (col D),
         and/or tags (col E) via :meth:`OneDriveClient.update_protocol_row`.
      3. **Workflow context** - patched in-memory; caller is responsible for
         persisting via ``save_workflow_state``.

    Test-mode (``ctx['test_mode']``) skips steps 1 and 2 entirely - only the
    in-memory context update happens - mirroring the original archive
    workflow's test-mode behaviour.

    Args:
        workflow_id:  Used for audit logging.
        ctx:          The current archive workflow context (mutated in place).
        amendments:   Dict with optional keys ``title``, ``labels``,
                      ``key_points``, ``protocol_id``.
        onedrive:     Optional OneDriveClient (defaults to a fresh instance).

    Returns:
        Dict ``{applied: [...], remote_filename: ..., row_updated: bool}``
        describing what changed - handy for the CLI summary.
    """
    # Use the module-level OneDriveClient binding (imported at top) so test
    # patches on `src.workflows.archive.OneDriveClient` take effect.
    test_mode = bool(ctx.get("test_mode"))
    client = onedrive if onedrive is not None else OneDriveClient()
    llm_result = dict(ctx.get("llm_result") or {})

    new_title = amendments.get("title")
    new_labels = amendments.get("labels")
    new_key_points = amendments.get("key_points")
    new_protocol_id = amendments.get("protocol_id")

    applied: list[str] = []
    result_summary: dict[str, Any] = {"workflow_id": workflow_id, "test_mode": test_mode}

    # ── Step 1: SharePoint rename (only if title or protocol_id actually changed) ──
    needs_rename = False
    current_title = llm_result.get("title", "")
    current_proto = ctx.get("protocol_number", "")
    final_title = new_title if new_title else current_title
    final_proto = new_protocol_id if new_protocol_id else current_proto

    if (new_title and new_title != current_title) or (
        new_protocol_id and new_protocol_id != current_proto
    ):
        needs_rename = True

    if needs_rename and not test_mode:
        old_remote_path = ctx.get("remote_folder") and ctx.get("remote_filename") and (
            f"{ctx['remote_folder']}/{ctx['remote_filename']}"
        )
        new_filename = f"[{final_proto}] {_sanitise_filename(final_title)}.pdf"
        if old_remote_path:
            try:
                await client.rename_file(old_remote_path, new_filename, workflow="archive")
                ctx["remote_filename"] = new_filename
                result_summary["renamed_to"] = new_filename
                applied.append("file_renamed")
            except Exception as e:
                logger.warning("rename_file failed for %s: %s", old_remote_path, e)
                result_summary["rename_error"] = str(e)
    elif needs_rename and test_mode:
        new_filename = f"[{final_proto}] {_sanitise_filename(final_title)}.pdf"
        ctx["remote_filename"] = new_filename
        result_summary["renamed_to"] = new_filename + "  [TEST - not actually renamed]"

    # ── Step 2: πρωτόκολλο xlsx row update ──────────────────────────────────
    # We update if title/labels/key_points changed.  protocol_id changes are
    # handled separately below - they require finding the row by the OLD id
    # then editing column A as well.
    if (new_title is not None or new_labels is not None or new_key_points is not None) and not test_mode:
        labels_str = ", ".join(new_labels) if new_labels is not None else None
        try:
            updated = await client.update_protocol_row(
                current_proto,
                title=new_title,
                main_points=new_key_points,
                tags=labels_str,
            )
            result_summary["row_updated"] = updated
            if updated:
                if new_title is not None: applied.append("title")
                if new_labels is not None: applied.append("labels")
                if new_key_points is not None: applied.append("key_points")
        except Exception as e:
            logger.warning("update_protocol_row failed for %s: %s", current_proto, e)
            result_summary["row_update_error"] = str(e)
    elif test_mode:
        # Test mode: pretend it worked (we still update the in-memory ctx below)
        if new_title is not None: applied.append("title")
        if new_labels is not None: applied.append("labels")
        if new_key_points is not None: applied.append("key_points")
        result_summary["row_updated"] = "skipped (test mode)"

    # ── protocol_id change support (cross-row rewrite - limited) ────────────
    # Same-year: handled by SharePoint rename (above) + a column-A rewrite.
    # Cross-year: not yet supported (would need delete + re-append across
    # sheets); print a warning and leave the row's column A alone.
    if new_protocol_id and new_protocol_id != current_proto and not test_mode:
        old_year = current_proto[:4] if len(current_proto) >= 4 else ""
        new_year = new_protocol_id[:4] if len(new_protocol_id) >= 4 else ""
        if old_year == new_year:
            # Same year - we already updated the row's other columns via
            # update_protocol_row(current_proto, ...).  Now also rewrite
            # column A by deleting + re-appending so the new id takes effect.
            try:
                # Snapshot the row's current values before delete
                snapshot = {
                    "title": new_title or current_title,
                    "labels_str": ", ".join(new_labels) if new_labels else (
                        ", ".join(llm_result.get("labels", []))
                    ),
                    "key_points": new_key_points if new_key_points is not None else (
                        llm_result.get("key_points", "")
                    ),
                    "date_str": ctx.get("archive_date", ""),
                }
                await client.delete_protocol_row(current_proto)
                await client.append_protocol_row(
                    new_protocol_id,
                    date_str=snapshot["date_str"],
                    title=snapshot["title"],
                    main_points=snapshot["key_points"],
                    tags=snapshot["labels_str"],
                )
                ctx["protocol_number"] = new_protocol_id
                applied.append("protocol_id")
                result_summary["protocol_id_rewrite"] = (
                    f"{current_proto} → {new_protocol_id}"
                )
            except Exception as e:
                logger.warning("protocol_id rewrite failed: %s", e)
                result_summary["protocol_id_error"] = str(e)
        else:
            result_summary["protocol_id_warning"] = (
                f"Cross-year change ({current_proto} → {new_protocol_id}) "
                "not supported - adjust the πρωτόκολλο manually."
            )
    elif new_protocol_id and new_protocol_id != current_proto and test_mode:
        ctx["protocol_number"] = new_protocol_id
        applied.append("protocol_id")
        result_summary["protocol_id_rewrite"] = (
            f"{current_proto} → {new_protocol_id}  [TEST - xlsx unchanged]"
        )

    # ── Step 3: in-memory context patch (always happens; caller persists) ───
    if new_title:
        llm_result["title"] = new_title
    if new_labels is not None:
        llm_result["labels"] = new_labels
    if new_key_points is not None:
        llm_result["key_points"] = new_key_points
    ctx["llm_result"] = llm_result

    result_summary["applied"] = applied
    return result_summary


def copy_to_archive_staging(src: Path) -> Path:
    """Copy *src* into ``data/output/`` so the original is left untouched.

    Returns the path to the copy.  Caller stores it in ctx as
    ``local_copy_path`` so rollback can delete it.
    """
    dest_dir = Path("data") / "output"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / src.name
    shutil.copy2(src, dest)
    return dest
