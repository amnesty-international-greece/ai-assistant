"""CLI commands for manual workflow triggers and platform management."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import platform
import sys
from pathlib import Path

from src.config import settings
from src.core.audit import init_db, get_audit_log, log_action
from src.core.claude import ClaudeClient

logger = logging.getLogger(__name__)


def setup_logging() -> None:
    """Configure logging based on settings.

    Delegates to :func:`src.core.logging_config.setup_logging`, which adds
    rotating file handlers at ``data/logs/`` on top of the console handler.
    Idempotent — safe to call from both the CLI entry point and from the
    bot's own startup.
    """
    from src.core.logging_config import setup_logging as _setup
    _setup()


def _print_header(title: str) -> None:
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print(f"{'=' * 60}\n")


def _print_step(step_num: int, total: int, message: str) -> None:
    print(f"  [{step_num}/{total}] {message}")


def _strip_json_fences(text: str) -> str:
    """Strip markdown code fences that LLMs sometimes add around JSON."""
    text = text.strip()
    if text.startswith("```"):
        # Remove opening fence (```json or ```)
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
    if text.endswith("```"):
        text = text.rsplit("```", 1)[0]
    return text.strip()


def _confirm(prompt: str = "Approve? [y/n]: ") -> bool:
    """Ask user for confirmation."""
    while True:
        response = input(prompt).strip().lower()
        if response in ("y", "yes"):
            return True
        if response in ("n", "no"):
            return False
        print("  Please enter 'y' or 'n'.")


# --- Workflow Commands ---


def cmd_invite(args: argparse.Namespace) -> None:
    """Dispatch invite subcommands or run the workflow."""
    init_db()
    invite_command = getattr(args, "invite_command", None)
    if invite_command == "share-poll":
        asyncio.run(_run_invite_share_poll(args))
        return
    if invite_command == "reset-sheet":
        _run_invite_reset_sheet(args)
        return
    # Default: run the main workflow
    if getattr(args, "cancel", False) or getattr(args, "rollback", False):
        asyncio.run(_run_invite_cancel(args))
        return
    asyncio.run(_run_invite(args))


def _run_invite_reset_sheet(args: argparse.Namespace) -> None:
    """Manually reset the agenda Google Sheet for the next meeting cycle.

    Bumps D5 to the next meeting_ref, clears D7/D9/D11, unchecks the three
    approval boxes (D16/D17/D18), clears the H7:K agenda block, and removes
    the script-owned protection.

    Normally invoked automatically by the minutes workflow on finalize;
    this command exists for manual recovery.
    """
    from src.integrations.google_drive import GoogleClient

    _print_header("Reset Agenda Sheet")

    sheet_id = settings.google.agenda_sheet_id
    if not sheet_id:
        print("ERROR: google.agenda_sheet_id not configured in config.yaml.")
        sys.exit(1)

    try:
        google = GoogleClient()
        google.authenticate()
        info = google.reset_agenda_sheet(sheet_id)
    except Exception as e:
        print(f"FAILED: {e}")
        sys.exit(1)

    print(f"  Tab:                 {info['tab_title']}")
    print(f"  Old meeting ref:     {info['old_meeting_ref']}")
    print(f"  New meeting ref:     {info['new_meeting_ref']}")
    print(f"  Cleared:             {', '.join(info['cleared_cells'])}")
    print(f"  Protections removed: {info['protections_removed']}")
    print()


async def _run_invite_cancel(args: argparse.Namespace) -> None:
    """Cancel a (possibly completed) invitation workflow and roll back side effects."""
    from src.core.audit import get_workflow_state, save_workflow_state, _get_connection
    from src.workflows.board_meeting_invitation import BoardMeetingInvitationWorkflow

    workflow_id = getattr(args, "workflow_id", None)
    if not workflow_id:
        # Pick the most recently updated invitation workflow
        conn = _get_connection()
        row = conn.execute(
            "SELECT workflow_id FROM workflow_state "
            "WHERE workflow_name = 'board_meeting_invitation' "
            "ORDER BY updated_at DESC LIMIT 1"
        ).fetchone()
        if not row:
            print("No invitation workflow found to cancel.")
            sys.exit(1)
        workflow_id = row["workflow_id"]

    state = get_workflow_state(workflow_id)
    if not state:
        print(f"Workflow {workflow_id} not found.")
        sys.exit(1)

    data = json.loads(state.get("data") or "{}")
    ctx = data.get("context") or {}

    print(f"Cancelling workflow {workflow_id} (status={state.get('state')})...")
    wf = BoardMeetingInvitationWorkflow()
    await wf.rollback(ctx)

    # Mark the workflow_state row as cancelled so the idempotency check
    # (and any other "is this still active?" query) treats it as terminal.
    save_workflow_state(
        workflow_name="board_meeting_invitation",
        workflow_id=workflow_id,
        state="cancelled",
        data=data,
    )
    print("Cancel complete (rollback done; workflow marked as cancelled).")


async def _run_invite_share_poll(args: argparse.Namespace) -> None:
    """Share a scheduling poll URL by replying to the workflow's email thread."""
    from src.core.audit import get_workflow_state, _get_connection
    from src.integrations.m365_mail import M365MailClient

    url = args.url
    workflow_id = getattr(args, "workflow_id", None)

    if not workflow_id:
        conn = _get_connection()
        row = conn.execute(
            "SELECT workflow_id FROM workflow_state "
            "WHERE workflow_name = 'board_meeting_invitation' "
            "ORDER BY updated_at DESC LIMIT 1"
        ).fetchone()
        if not row:
            print("ERROR: No board_meeting_invitation workflow found. Pass --workflow-id.")
            sys.exit(1)
        workflow_id = row["workflow_id"]

    state = get_workflow_state(workflow_id)
    if not state:
        print(f"ERROR: workflow_id {workflow_id} not found.")
        sys.exit(1)

    # Refuse if past await_approval — the date is locked in.
    # The state machine sits AT await_approval when paused; once resumed it
    # moves to read_agenda and beyond, at which point sharing a poll is moot.
    blocked_states = {
        "approved", "executing", "in_progress", "completed",
    }
    # Heuristic: check step_index — anything after the await_approval gate
    # means the date is locked.  await_approval is index 1.
    step_index = (json.loads(state.get("data") or "{}")).get("step_index", 0)
    current_state = state.get("state", "")
    if step_index > 1 and current_state in blocked_states:
        print(f"ERROR: workflow is past await_approval (step_index={step_index}, state={current_state}).")
        print("       Sharing a new poll would be misleading — the date has been locked.")
        sys.exit(1)

    data = json.loads(state.get("data") or "{}")
    ctx = data.get("context") or {}
    anchor = ctx.get("email_thread_anchor")
    if not anchor:
        print("ERROR: workflow has no email_thread_anchor — scheduling email did not run.")
        print("       Cannot send a threaded reply.  Send the poll URL manually.")
        sys.exit(1)

    body = f"Poll διαθεσιμότητας: {url}"
    try:
        client = M365MailClient()
        reply_id = await client.send_reply(
            parent_internet_message_id=anchor,
            body=body,
            html=False,
            to="board@amnesty.gr",
            workflow="board_meeting_invitation",
        )
    except Exception as e:
        print(f"FAILED to send poll reply: {e}")
        sys.exit(1)
    print(f"Poll URL shared in thread (reply id={reply_id}).")


async def _run_invite(args: argparse.Namespace) -> None:
    """Async handler for the invitation workflow."""
    from src.workflows.board_meeting_invitation import BoardMeetingInvitationWorkflow

    # ── Resolve run mode ──────────────────────────────────────────────────────
    # --test : full simulation — Zoom created+rolled back, PDF generated,
    #          emails redirected to testing.test_email, DEBUG logging.
    # (no flag): live run — everything executes for real.
    test_mode = getattr(args, "test", False)

    if test_mode:
        logging.getLogger().setLevel(logging.DEBUG)
        for h in logging.getLogger().handlers:
            h.setLevel(logging.DEBUG)

        test_email = settings.testing.test_email
        _print_header("Board Meeting Invitation Workflow  [TEST MODE]")
        print("  TEST MODE — what will happen:")
        print("  • Reads agenda from Google Sheets (real)")
        print("  • Creates Zoom meeting (real, rolled back at the end)")
        print("  • Generates invitation PDF (real, opened for review)")
        print("  • Newsletter test send →", test_email or "(skipped — set testing.test_email in config.yaml)")
        print("  • Archive: skipped")
        print("  • Reminders: handled by Zoom natively")
        print("  • Logging: DEBUG")
        print()
    else:
        _print_header("Board Meeting Invitation Workflow")

    # Build initial context from CLI args
    initial_data: dict = {
        "test_mode": test_mode,
    }

    if args.sheet_id:
        initial_data["agenda_sheet_id"] = args.sheet_id

    if args.date:
        initial_data["meeting_date"] = args.date

    if args.time:
        initial_data["meeting_time"] = args.time

    if args.brevo_template:
        initial_data["brevo_template_id"] = int(args.brevo_template)

    if args.brevo_lists:
        initial_data["brevo_list_ids"] = [int(x) for x in args.brevo_lists.split(",")]

    # Manual protocol number overrides whatever the workflow reads from Drive
    if getattr(args, "protocol", None):
        initial_data["protocol_number"] = args.protocol

    # Scheduling poll URL — embedded in the board scheduling email (M365)
    if getattr(args, "poll_url", None):
        initial_data["poll_url"] = args.poll_url

    # Response deadline (DD/MM in the scheduling email).  Defaults to today + 4.
    if getattr(args, "response_deadline", None):
        initial_data["response_deadline"] = args.response_deadline

    # Sandbox meeting_ref override — bypasses D5 read so a test workflow runs
    # under a completely separate meeting_id namespace (no Discord-thread /
    # Zoom / pending-reminder collisions with a live invitation cycle).
    # Typical usage: ``--test --meeting-ref ΔΣ99-2099`` + ``--manual``.
    if getattr(args, "meeting_ref", None):
        initial_data["meeting_ref_override"] = args.meeting_ref

    # If manual mode: skip Google Sheets, use provided args directly
    if args.manual:
        if not all([args.meeting_ref, args.date, args.time]):
            print("ERROR: --manual mode requires --meeting-ref, --date, and --time")
            sys.exit(1)
        # Derive meeting_number from the ΔΣXX-YYYY ref so the workflow's
        # downstream steps (which still need the integer XX) don't break.
        import re as _re
        m = _re.match(r"^ΔΣ(\d{1,2})-(\d{4})$", args.meeting_ref.strip())
        if not m:
            print(f"ERROR: --meeting-ref must look like ΔΣ05-2026 (got {args.meeting_ref!r})")
            sys.exit(1)
        initial_data["meeting_number"] = str(int(m.group(1)))

        # Prompt for agenda items interactively
        print("Enter agenda items (one per line, empty line to finish):")
        agenda_items = []
        while True:
            item = input(f"  {len(agenda_items) + 1}. ").strip()
            if not item:
                break
            agenda_items.append(item)

        initial_data["agenda_items"] = agenda_items
        initial_data["_skip_read_agenda"] = True

    wf = BoardMeetingInvitationWorkflow(actor=args.actor if hasattr(args, "actor") else "secgen")

    print(f"Workflow ID: {wf.workflow_id}")
    print(f"Steps: {len(wf.steps)}")
    print()

    # Run workflow (will pause at approval gate)
    result = await wf.run(initial_data)

    while result.get("status") == "awaiting_approval":
        current_step = result.get("step", "")
        ctx = wf.context

        # ── Gate: scheduling email → board responses ─────────────────────────
        if current_step == "await_approval":
            print()
            _print_header("APPROVAL REQUIRED — Board scheduling responses")
            print("  Scheduling email has been sent.  Wait for board availability,")
            print("  pick a date, fill the agenda sheet tab (ΔΣXX-YYYY), then resume.")
            print()
            if _confirm("  Date locked + agenda sheet ready? Proceed? [y/n]: "):
                log_action(
                    workflow="board_meeting_invitation",
                    action="approval_given",
                    actor="secgen",
                    details={"workflow_id": wf.workflow_id, "gate": "await_approval"},
                )
                result = await wf.approve_and_resume()
            else:
                log_action(
                    workflow="board_meeting_invitation",
                    action="approval_rejected",
                    actor="secgen",
                    details={"workflow_id": wf.workflow_id, "gate": "await_approval"},
                )
                print("\n  Cancelling workflow.")
                await wf.rollback(wf.context)
                return
            continue

        # ── Gate: PDF approval (halts only in test_mode) ─────────────────────
        if current_step == "approval" and not test_mode:
            # Live mode: auto-pass through
            log_action(
                workflow="board_meeting_invitation",
                action="approval_auto_live",
                actor="system",
                details={"workflow_id": wf.workflow_id, "gate": "pdf"},
            )
            result = await wf.approve_and_resume()
            continue

        # ── Gate: newsletter confirm (halts only in test_mode) ───────────────
        if current_step == "confirm_newsletter" and not test_mode:
            # Live mode: newsletter already sent (or skipped) during send_newsletter
            log_action(
                workflow="board_meeting_invitation",
                action="approval_auto_live",
                actor="system",
                details={"workflow_id": wf.workflow_id, "gate": "newsletter"},
            )
            result = await wf.approve_and_resume()
            continue

        # ── Gate 1: PDF review ────────────────────────────────────────────────
        if current_step == "approval":
            print()
            _print_header("APPROVAL REQUIRED — Invitation PDF")

            meeting_number = ctx.get("meeting_number", "?")
            meeting_date   = ctx.get("meeting_date", "?")
            meeting_time   = ctx.get("meeting_time", "?")
            meeting_type   = ctx.get("meeting_type") or "ΤΑΚΤΙΚΗ"
            location       = ctx.get("location") or "ΔΙΑΔΙΚΤΥΑΚΑ"
            agenda_items   = ctx.get("agenda_items", [])

            seq = meeting_number.zfill(2) if meeting_number.isdigit() else meeting_number
            print(f"  Συνεδρίαση:  ΔΣ{seq}-{meeting_date[:4]}")
            print(f"  Ημερομηνία:  {meeting_date}  {meeting_time}")
            print(f"  Τύπος:       {meeting_type}")
            print(f"  Τοποθεσία:   {location}")
            print()
            if agenda_items:
                print("  Ημερήσια διάταξη:")
                for i, item in enumerate(agenda_items, 1):
                    print(f"    {i}. {item}")
            print()

            # Auto-open PDF for review
            pdf_path = ctx.get("pdf_path", "")
            if pdf_path and Path(pdf_path).exists():
                print(f"  Opening PDF: {pdf_path}")
                try:
                    if platform.system() == "Windows":
                        os.startfile(pdf_path)
                    elif platform.system() == "Darwin":
                        import subprocess
                        subprocess.Popen(["open", pdf_path])
                    else:
                        import subprocess
                        subprocess.Popen(["xdg-open", pdf_path])
                except Exception as open_err:
                    print(f"  (Could not open PDF automatically: {open_err})")
                print()

            if _confirm("  Approve this PDF and proceed? [y/n]: "):
                log_action(
                    workflow="board_meeting_invitation",
                    action="approval_given",
                    actor="secgen",
                    details={"workflow_id": wf.workflow_id, "gate": "pdf"},
                )
                result = await wf.approve_and_resume()
            else:
                log_action(
                    workflow="board_meeting_invitation",
                    action="approval_rejected",
                    actor="secgen",
                    details={"workflow_id": wf.workflow_id, "gate": "pdf"},
                )
                print("\n  Cancelling workflow and cleaning up...")
                await wf.rollback(wf.context)
                print("  Zoom meeting cancelled. PDF deleted. Done.")
                return

        # ── Gate 2: Newsletter confirm ────────────────────────────────────────
        elif current_step == "confirm_newsletter":
            # Auto-skip if the newsletter step already failed or was skipped
            if ctx.get("newsletter_skipped"):
                print("\n  Newsletter was not created (skipped or failed) — skipping confirm gate.")
                result = await wf.approve_and_resume()
                continue

            print()
            _print_header("APPROVAL REQUIRED — Newsletter Live Send")

            campaign_id = ctx.get("newsletter_campaign_id", "?")
            test_addr   = ctx.get("newsletter_test_addr", "")
            list_ids    = ctx.get("newsletter_list_ids", [])

            if test_addr:
                print(f"  Test email sent to:  {test_addr}")
                print(f"  Check your inbox, then confirm live send.")
            else:
                print("  (No test email address set — review campaign in Brevo dashboard)")
            print(f"  Campaign ID:         {campaign_id}")
            if list_ids:
                print(f"  Will send to lists:  {list_ids}")
            else:
                print("  newsletter_list_ids is empty — campaign will be saved as Brevo draft only")
            print()

            if _confirm("  Send newsletter to members list? [y/n]: "):
                log_action(
                    workflow="board_meeting_invitation",
                    action="approval_given",
                    actor="secgen",
                    details={"workflow_id": wf.workflow_id, "gate": "newsletter"},
                )
                result = await wf.approve_and_resume()
            else:
                log_action(
                    workflow="board_meeting_invitation",
                    action="approval_rejected",
                    actor="secgen",
                    details={"workflow_id": wf.workflow_id, "gate": "newsletter"},
                )
                print("\n  Live send cancelled — campaign saved as draft in Brevo.")
                # Don't rollback — Zoom + PDF are already done; just skip the send
                result = {"status": "completed", "context": ctx}
                break

        else:
            # Unknown gate — generic handler
            print()
            _print_header(f"APPROVAL REQUIRED — {current_step}")
            if _confirm("  Proceed? [y/n]: "):
                result = await wf.approve_and_resume()
            else:
                await wf.rollback(wf.context)
                return

    # Final status
    print()
    if result.get("status") == "completed":
        _print_header("WORKFLOW COMPLETED")
        ctx = wf.context
        raw_id = ctx.get('raw_meeting_id', '')
        mn = ctx.get('meeting_number', '')
        md = ctx.get('meeting_date', '')
        year = md[:4] if len(md) >= 4 else 'ΧΧΧΧ'
        seq = mn.zfill(2) if mn.isdigit() else mn
        meeting_label = raw_id or f"ΔΣ{seq}-{year}"
        print(f"  Meeting:       {meeting_label}")
        print(f"  Date:          {ctx.get('meeting_date', 'N/A')}")
        print(f"  Zoom:          {ctx.get('zoom_join_url', 'N/A')}")
        print(f"  PDF:           {ctx.get('pdf_path', 'N/A')}")

        # If archive was skipped, open the output folder so manual archiving is easy
        archived = bool(ctx.get('archive_file_id'))
        print(f"  Archived:      {'Yes (OneDrive)' if archived else 'No — see folder below'}")
        if not archived:
            output_dir = Path("data") / "output"
            print(f"  PDF folder:    {output_dir.resolve()}")
            try:
                if platform.system() == "Windows":
                    os.startfile(str(output_dir.resolve()))
                elif platform.system() == "Darwin":
                    import subprocess
                    subprocess.Popen(["open", str(output_dir.resolve())])
                else:
                    import subprocess
                    subprocess.Popen(["xdg-open", str(output_dir.resolve())])
            except Exception:
                pass

        print(f"  Newsletter:    {'Sent' if ctx.get('newsletter_sent') else 'Draft/Skipped'}")
        print(f"  Reminder:      Zoom-native")
    elif result.get("status") == "failed":
        _print_header("WORKFLOW FAILED")
        print(f"  Failed at step: {result.get('step', 'unknown')}")
        print(f"  Error: {result.get('error', 'unknown')}")
    print()

    # Test mode cleanup: always roll back (whether completed or failed).
    # Runs AFTER the summary so the user sees the final outcome first,
    # then presses Enter to tear down the Zoom meeting + PDF + Brevo draft.
    if test_mode and result.get("status") in ("completed", "failed"):
        input("  [TEST MODE] Press Enter when done reviewing to clean up (cancel Zoom + delete PDF + draft)...")
        print("  Cleaning up...")
        await wf.rollback(wf.context)
        print("  Cleanup done.")
        print()


# --- Debug (single-step workflow testing) ---


def cmd_debug(args: argparse.Namespace) -> None:
    """Dispatch `debug` subcommands.

    Lets the user exercise ONE workflow step in isolation (always in test_mode)
    against a canonical fake context, instead of running the whole workflow.
    NEVER persists state, NEVER rolls back, NEVER runs steps the user didn't ask
    for.
    """
    # Step messages / fixtures are heavily Greek; on a legacy Windows console
    # (cp1253 etc.) a stray glyph would raise UnicodeEncodeError mid-print.
    # Make this command's output resilient regardless of the active code page.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass

    debug_command = getattr(args, "debug_command", None)
    if debug_command == "list":
        _run_debug_list(args)
        return
    if debug_command == "fixture":
        _run_debug_fixture(args)
        return
    if debug_command == "run":
        init_db()  # steps may touch the audit DB (log_action); ensure schema exists
        asyncio.run(_run_debug_run(args))
        return
    print("Usage: ai-assistant debug list [workflow]")
    print("       ai-assistant debug fixture <workflow> [--json]")
    print("       ai-assistant debug run <workflow> <step[,step2,...]> "
          "[--set k=v ...] [--from-state ID] [--show-ctx] [--json]")


def _debug_unknown_workflow(name: str) -> None:
    from src.workflows.registry import WORKFLOWS
    print(f"❌ Unknown workflow: {name!r}")
    print(f"   Valid workflows: {', '.join(WORKFLOWS)}")


def _run_debug_list(args: argparse.Namespace) -> None:
    """List all workflows, or the steps of one workflow."""
    from src.workflows.registry import WORKFLOWS, get_workflow

    workflow = getattr(args, "workflow", None)
    if not workflow:
        _print_header("Debuggable workflows")
        for name, cls in WORKFLOWS.items():
            wf = cls(actor="debug")
            print(f"  {name}  ({len(wf.steps)} steps)")
        print()
        print("  Inspect one with:  ai-assistant debug list <workflow>")
        print()
        return

    cls = get_workflow(workflow)
    if cls is None:
        _debug_unknown_workflow(workflow)
        return

    wf = cls(actor="debug")
    _print_header(f"{workflow} — {len(wf.steps)} steps")
    for idx, step in enumerate(wf.steps):
        gate = "  [approval gate]" if step.requires_approval else ""
        print(f"  {idx}. {step.name} — {step.description}{gate}")
    fixture_keys = sorted(cls.debug_fixture().keys())
    print()
    print(f"  fixture keys: {', '.join(fixture_keys)}")
    print()


def _run_debug_fixture(args: argparse.Namespace) -> None:
    """Pretty-print a workflow's debug_fixture()."""
    from src.workflows.registry import get_workflow

    workflow = getattr(args, "workflow", None)
    cls = get_workflow(workflow) if workflow else None
    if cls is None:
        _debug_unknown_workflow(workflow or "")
        return

    fixture = cls.debug_fixture()
    if getattr(args, "json", False):
        print(json.dumps(fixture, ensure_ascii=False, indent=2, default=str))
        return

    _print_header(f"{workflow} — debug_fixture()")
    import pprint
    print(pprint.pformat(fixture, indent=2, width=100, sort_dicts=False))
    print()


def _debug_load_from_state(workflow_id: str) -> dict[str, object]:
    """Load the persisted ctx for a workflow_state row.

    The blob in ``workflow_state.data`` is ``{"context": {...}, "step_index": N}``
    (see ``BaseWorkflow._persist``).  We overlay only the stored ``context``.
    Returns an empty dict (with a warning) if the id is unknown or unparseable.
    """
    from src.core.audit import get_workflow_state

    state = get_workflow_state(workflow_id)
    if not state:
        print(f"  ⚠️  No workflow_state row for id {workflow_id!r} — ignoring --from-state.")
        return {}
    raw = state.get("data")
    if not raw:
        print(f"  ⚠️  workflow_state {workflow_id!r} has no data blob — ignoring --from-state.")
        return {}
    try:
        blob = json.loads(raw) if isinstance(raw, str) else raw
    except (json.JSONDecodeError, TypeError) as exc:
        print(f"  ⚠️  Could not parse workflow_state {workflow_id!r} data ({exc}) — ignoring.")
        return {}
    ctx = blob.get("context") if isinstance(blob, dict) else None
    if not isinstance(ctx, dict):
        print(f"  ⚠️  workflow_state {workflow_id!r} has no 'context' dict — ignoring.")
        return {}
    return ctx


def _debug_parse_set(pairs: list[str]) -> dict[str, object]:
    """Parse repeated ``--set key=value`` pairs.

    Splits on the FIRST ``=`` only.  Each value is parsed as JSON (so
    ``--set meeting_number=99`` is an int, ``--set agenda_items='[\"a\"]'`` a
    list); on JSON-parse failure the raw string is used verbatim.
    """
    out: dict[str, object] = {}
    for pair in pairs:
        if "=" not in pair:
            print(f"  ⚠️  Ignoring malformed --set {pair!r} (expected key=value).")
            continue
        key, _, raw_val = pair.partition("=")
        key = key.strip()
        if not key:
            print(f"  ⚠️  Ignoring --set with empty key: {pair!r}.")
            continue
        try:
            out[key] = json.loads(raw_val)
        except json.JSONDecodeError:
            out[key] = raw_val
    return out


async def _run_debug_run(args: argparse.Namespace) -> None:
    """Run one or more workflow steps in isolation against the fake fixture."""
    from src.workflows.registry import get_workflow

    workflow = getattr(args, "workflow", None)
    cls = get_workflow(workflow) if workflow else None
    if cls is None:
        _debug_unknown_workflow(workflow or "")
        return

    # Resolve requested step names (single or comma-separated chain).
    steps_arg = (getattr(args, "steps", None) or "").strip()
    requested = [s.strip() for s in steps_arg.split(",") if s.strip()]
    if not requested:
        print("❌ No step(s) given. Usage: debug run <workflow> <step[,step2,...]>")
        return

    wf = cls(actor="debug")
    step_by_name = {s.name: s for s in wf.steps}
    valid_names = list(step_by_name)
    for name in requested:
        if name not in step_by_name:
            print(f"❌ Unknown step {name!r} for workflow {workflow!r}.")
            print(f"   Valid steps: {', '.join(valid_names)}")
            return

    # ── Build ctx: fixture → --from-state overlay → --set overlay → force test_mode ──
    ctx: dict[str, object] = dict(cls.debug_fixture())
    from_state = getattr(args, "from_state", None)
    if from_state:
        ctx.update(_debug_load_from_state(from_state))
    set_pairs = getattr(args, "set", None) or []
    ctx.update(_debug_parse_set(set_pairs))
    ctx["test_mode"] = True  # forced — no live escape hatch in debug

    as_json = bool(getattr(args, "json", False))
    show_ctx = bool(getattr(args, "show_ctx", False))

    if not as_json:
        _print_header(f"debug run {workflow}  [TEST MODE]")
        print(f"  Steps: {', '.join(requested)}")
        print()

    summary: list[dict[str, object]] = []
    for name in requested:
        step = step_by_name[name]
        try:
            result = await wf.execute_step(step, ctx)
        except Exception as exc:  # surface, never persist/rollback
            if as_json:
                summary.append({"step": name, "success": False,
                                "message": f"EXCEPTION: {exc}", "data": {}})
            else:
                print(f"  -- {name} --")
                print(f"     success: False")
                print(f"     message: EXCEPTION: {exc}")
                print()
            break
        # Thread this step's output into ctx before the next step.
        ctx.update(result.data)

        if as_json:
            summary.append({
                "step": name,
                "success": result.success,
                "message": result.message,
                "data": result.data,
            })
        else:
            print(f"  -- {name} --")
            print(f"     success: {result.success}")
            print(f"     message: {result.message}")
            if result.needs_approval:
                print(f"     needs_approval: True")
            if show_ctx:
                import pprint
                produced = pprint.pformat(result.data, indent=2, width=96, sort_dicts=False)
                indented = "\n".join("       " + ln for ln in produced.splitlines())
                print(f"     data (keys produced):")
                print(indented or "       {}")
            print()

    if as_json:
        print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))


# --- Εγκύκλιοι ---


def cmd_egkyklios(args: argparse.Namespace) -> None:
    """Dispatch εγκύκλιος subcommands.

    Today only the General kind is implemented; future Special kind will live
    under the same top-level command with a different first positional.
    """
    init_db()
    kind = getattr(args, "egkyklios_kind", None) or ""
    if kind == "general":
        egkyklios_command = getattr(args, "egkyklios_command", None)
        if egkyklios_command == "approve":
            asyncio.run(_run_egkyklios_general_approve(args))
            return
        if egkyklios_command == "list":
            _run_egkyklios_list(args)
            return
        # Default: run the main draft workflow
        asyncio.run(_run_egkyklios_general(args))
        return
    print("Usage: ai-assistant egkyklios general [--period-start DATE] [--period-end DATE] [--test]")
    print("       ai-assistant egkyklios general approve <draft-id>")
    print("       ai-assistant egkyklios general list")


async def _run_egkyklios_general(args: argparse.Namespace) -> None:
    """Kick off the Γενική Εγκύκλιος workflow up to the approval gate."""
    from src.workflows.egkyklios_general import EgkykliosGeneralWorkflow

    test_mode = bool(getattr(args, "test", False))
    period_start = getattr(args, "period_start", "") or ""
    period_end = getattr(args, "period_end", "") or ""
    actor = getattr(args, "actor", None) or "secgen"

    if test_mode:
        _print_header("Γενική Εγκύκλιος Ενημέρωσης  [TEST MODE]")
    else:
        _print_header("Γενική Εγκύκλιος Ενημέρωσης")

    initial_data: dict[str, object] = {"test_mode": test_mode}
    if period_start:
        initial_data["period_start"] = period_start
    if period_end:
        initial_data["period_end"] = period_end

    wf = EgkykliosGeneralWorkflow(actor=actor)
    try:
        result = await wf.run(initial_data)
    except Exception as exc:
        print(f"  ❌ Εξαίρεση: {exc}")
        return

    ctx = wf.context or {}
    status = result.get("status", "?")
    print()
    print(f"  Workflow ID:     {wf.workflow_id}")
    print(f"  Κατάσταση:       {status}")
    print(f"  Τίτλος:          {ctx.get('title', '—')}")
    print(f"  Περίοδος:        {ctx.get('period_start', '?')} → {ctx.get('period_end', '?')}")
    draft_id = ctx.get("egkyklios_draft_id")
    if draft_id:
        print(f"  Draft DB id:     {draft_id}")
    if ctx.get("draft_pdf_path"):
        print(f"  PDF προσχέδιο:   {ctx['draft_pdf_path']}")
    if status == "awaiting_approval":
        print()
        print("  Το προσχέδιο στάλθηκε στο ΔΣ + Διευθυντή για έλεγχο.")
        if draft_id:
            print(f"  Όταν είστε έτοιμοι, εκτελέστε:")
            print(f"    ai-assistant egkyklios general approve {draft_id}")
    if status == "failed":
        print(f"  Σφάλμα στο βήμα:  {result.get('step', '?')}")
        print(f"  Μήνυμα:           {result.get('error', '?')}")


async def _run_egkyklios_general_approve(args: argparse.Namespace) -> None:
    """SecGen advances a parked Γενική Εγκύκλιος past its approval gate.

    Looks up the draft row, finds the associated workflow_state, and resumes
    the workflow at ``await_approval`` so the archive + Brevo + bus-event
    steps execute.
    """
    from src.core.audit import get_egkyklios_draft, get_workflow_state
    from src.workflows.egkyklios_general import EgkykliosGeneralWorkflow

    draft_id_str = getattr(args, "draft_id", None) or ""
    if not draft_id_str:
        print("Usage: ai-assistant egkyklios general approve <draft-id>")
        return

    try:
        draft_id = int(draft_id_str)
    except ValueError:
        print(f"❌ Μη έγκυρο draft id: {draft_id_str!r}")
        return

    draft = get_egkyklios_draft(draft_id)
    if not draft:
        print(f"❌ Δεν βρέθηκε draft #{draft_id}.")
        return
    if draft["status"] not in ("awaiting_approval", "drafting"):
        print(
            f"⚠️  Draft #{draft_id} βρίσκεται σε κατάσταση {draft['status']!r}. "
            f"Δεν μπορεί να εγκριθεί."
        )
        return
    workflow_id = draft.get("workflow_id") or ""
    if not workflow_id:
        print(f"❌ Δεν βρέθηκε workflow_id για το draft #{draft_id}.")
        return

    state = get_workflow_state(workflow_id)
    if not state:
        print(f"❌ Δεν βρέθηκε workflow state για {workflow_id}.")
        return

    _print_header(f"Έγκριση Γενικής Εγκυκλίου — draft #{draft_id}")
    print(f"  Workflow ID:  {workflow_id}")
    print(f"  Τίτλος:       {draft.get('title', '—')}")
    print(f"  Περίοδος:     {draft['period_start']} → {draft['period_end']}")
    print()

    wf = EgkykliosGeneralWorkflow(actor=getattr(args, "actor", None) or "secgen")
    try:
        result = await wf.resume(workflow_id, approval_granted=True)
    except Exception as exc:
        print(f"  ❌ Εξαίρεση: {exc}")
        return

    status = result.get("status", "?")
    print(f"  Νέα κατάσταση: {status}")
    if status == "completed":
        ctx = wf.context or {}
        if ctx.get("protocol_number"):
            print(f"  Αρ. Πρωτ.:     {ctx['protocol_number']}")
        if ctx.get("sharepoint_url"):
            print(f"  SharePoint:    {ctx['sharepoint_url']}")
        if ctx.get("brevo_campaign_id"):
            print(f"  Brevo:         campaign #{ctx['brevo_campaign_id']}")
    elif status == "failed":
        print(f"  Σφάλμα στο βήμα: {result.get('step', '?')}")
        print(f"  Μήνυμα:          {result.get('error', '?')}")


def _run_egkyklios_list(args: argparse.Namespace) -> None:
    """List recent εγκύκλιος drafts (newest first)."""
    from src.core.audit import list_egkyklios_drafts

    limit = int(getattr(args, "limit", 10) or 10)
    rows = list_egkyklios_drafts(kind="general", limit=limit)
    _print_header(f"Γενικές Εγκύκλιοι — τελευταίες {len(rows)}")
    if not rows:
        print("  (κανένα draft)")
        return
    for r in rows:
        line = (
            f"  #{r['id']:>4}  [{r['status']:>18}]  {r['period_start']} → {r['period_end']}  "
            f"  {r.get('title', '')}"
        )
        if r.get("protocol_number"):
            line += f"  · πρωτ. {r['protocol_number']}"
        print(line)
    print()


# --- Minutes Commands ---


def cmd_minutes(args: argparse.Namespace) -> None:
    """Dispatch board meeting minutes subcommands."""
    minutes_command = getattr(args, "minutes_command", None)
    if minutes_command == "finalize":
        cmd_minutes_finalize(args)
    elif minutes_command == "list-drafts":
        cmd_minutes_list_drafts(args)
    elif minutes_command == "events":
        cmd_minutes_events(args)
    elif minutes_command == "fetch-recording":
        cmd_minutes_fetch_recording(args)
    elif minutes_command == "propose-decision":
        cmd_minutes_propose_decision(args)
    elif minutes_command == "build":
        cmd_minutes_build(args)
    else:
        # "run" or None → main workflow
        init_db()
        asyncio.run(_run_minutes(args))


async def _run_minutes(args: argparse.Namespace) -> None:
    """Async handler for the minutes workflow."""
    from src.workflows.board_meeting_minutes import BoardMeetingMinutesWorkflow
    from src.integrations.google_drive import GoogleClient
    from src.integrations.zoom import ZoomClient

    test_mode = getattr(args, "test", False)
    meeting_ref = args.meeting

    if test_mode:
        logging.getLogger().setLevel(logging.DEBUG)
        for h in logging.getLogger().handlers:
            h.setLevel(logging.DEBUG)
        _print_header(f"Board Meeting Minutes Workflow  [TEST MODE]  — {meeting_ref}")
    else:
        _print_header(f"Board Meeting Minutes Workflow  — {meeting_ref}")

    # ── Interactive source selection ─────────────────────────────────────────

    # List Google Docs in the minutes drafts folder
    source_doc_id: str = ""
    source_doc_name: str = ""
    docs: list = []
    try:
        google = GoogleClient()
        folder_id = settings.google.minutes_drafts_folder_id
        if folder_id:
            docs = google.list_docs_in_folder(folder_id)
        else:
            print("  (minutes_drafts_folder_id not configured — skipping doc listing)")
    except Exception as e:
        print(f"  (Could not list Google Docs: {e})")

    if docs:
        print("  Available draft documents:")
        for i, doc in enumerate(docs, 1):
            mod_date = doc.get("modifiedTime", "")[:10]
            print(f"    {i}. {doc['name']} ({mod_date})")
        while True:
            raw = input("  Select document [1]: ").strip()
            if not raw:
                selected_idx = 0
                break
            try:
                choice = int(raw)
                if 1 <= choice <= len(docs):
                    selected_idx = choice - 1
                    break
                print(f"  Please enter a number between 1 and {len(docs)}.")
            except ValueError:
                print("  Please enter a valid number.")
        source_doc_id = docs[selected_idx]["id"]
        source_doc_name = docs[selected_idx]["name"]
        print(f"  → Selected: {source_doc_name} ({source_doc_id})")
    print()

    # List Zoom recordings and pick one (or skip)
    recording_index: int = -1  # -1 means skip
    recordings: list = []
    if not getattr(args, "manual", False):
        try:
            zoom = ZoomClient()
            recordings = await zoom.list_recordings()
        except Exception as e:
            print(f"  (Could not list Zoom recordings: {e})")

    if recordings:
        # Auto-match: find recording whose topic contains the meeting_ref
        auto_match_idx = next(
            (i for i, r in enumerate(recordings) if meeting_ref in r.get("topic", "")),
            None,
        )
        default_display = (auto_match_idx + 1) if auto_match_idx is not None else 1

        print("  Available recordings:")
        for i, rec in enumerate(recordings, 1):
            topic = rec.get("topic", "")
            start = rec.get("start_time", "")[:10]
            marker = "  \u2190 auto-matched" if (auto_match_idx is not None and i - 1 == auto_match_idx) else ""
            print(f"    {i}. {topic} ({start}){marker}")
        while True:
            raw = input(f"  Select recording [{default_display}] (or 0 to skip): ").strip()
            if not raw:
                recording_index = auto_match_idx if auto_match_idx is not None else 0
                break
            try:
                choice = int(raw)
                if choice == 0:
                    recording_index = -1
                    break
                if 1 <= choice <= len(recordings):
                    recording_index = choice - 1
                    break
                print(f"  Please enter 0 to skip or a number between 1 and {len(recordings)}.")
            except ValueError:
                print("  Please enter a valid number.")
        print()

    # ── Transcript file (CLI flag or interactive prompt) ────────────────────
    transcript_path: str = getattr(args, "transcript", "") or ""

    # If no Zoom recordings found and no transcript file given, prompt the user
    if not recordings and not transcript_path:
        print("  No Zoom recordings found for this meeting.")
        raw_path = input("  Path to local transcript file (.vtt / .txt / .docx) [skip]: ").strip()
        if raw_path:
            tp = Path(raw_path)
            if tp.exists():
                transcript_path = str(tp)
                print(f"  → Using transcript: {tp.name}")
            else:
                print(f"  File not found: {raw_path} — continuing without transcript")
        print()

    # ── Build initial context and run workflow ────────────────────────────────
    initial_data: dict = {
        "meeting_ref": meeting_ref,
        "source_doc_id": source_doc_id,
        "source_doc_name": source_doc_name,
        "recording_index": recording_index,
        "transcript_path": transcript_path,
        "test_mode": test_mode,
    }

    actor = args.actor if hasattr(args, "actor") else "secgen"
    wf = BoardMeetingMinutesWorkflow(actor=actor)

    print(f"Workflow ID: {wf.workflow_id}")
    print(f"Steps: {len(wf.steps)}")
    print()

    result = await wf.run(initial_data)

    while result.get("status") == "awaiting_approval":
        print()
        _print_header("APPROVAL REQUIRED")

        ctx = wf.context
        print(f"  Meeting ref:  {ctx.get('meeting_ref', meeting_ref)}")
        docx_path = ctx.get("docx_path", "")
        if docx_path and Path(docx_path).exists():
            print(f"  Draft DOCX:   {docx_path}")
            try:
                if platform.system() == "Windows":
                    os.startfile(docx_path)
                elif platform.system() == "Darwin":
                    import subprocess
                    subprocess.Popen(["open", docx_path])
                else:
                    import subprocess
                    subprocess.Popen(["xdg-open", docx_path])
            except Exception as open_err:
                print(f"  (Could not open DOCX automatically: {open_err})")
        print()

        if _confirm("  Approve this draft and share with board? [y/n]: "):
            log_action(
                workflow="board_meeting_minutes",
                action="approval_given",
                actor=actor,
                details={"workflow_id": wf.workflow_id},
            )
            result = await wf.approve_and_resume()
        else:
            log_action(
                workflow="board_meeting_minutes",
                action="approval_rejected",
                actor=actor,
                details={"workflow_id": wf.workflow_id},
            )
            print("\n  Cancelling workflow.")
            if hasattr(wf, "rollback"):
                await wf.rollback(wf.context)
            print("  Done.")
            return

    print()
    if result.get("status") == "completed":
        _print_header("MINUTES WORKFLOW COMPLETED")
        ctx = wf.context
        print(f"  Meeting:      {ctx.get('meeting_ref', meeting_ref)}")
        print(f"  Draft DOCX:   {ctx.get('docx_path', 'N/A')}")
        print(f"  Shared:       {'Yes' if ctx.get('share_url') else 'No'}")
    elif result.get("status") == "failed":
        _print_header("WORKFLOW FAILED")
        print(f"  Failed at step: {result.get('step', 'unknown')}")
        print(f"  Error: {result.get('error', 'unknown')}")
    print()


def cmd_minutes_finalize(args: argparse.Namespace) -> None:
    """Finalize and extract decisions for a completed minutes draft."""
    init_db()
    asyncio.run(_run_minutes_finalize(args))


async def _run_minutes_finalize(args: argparse.Namespace) -> None:
    """Async handler for the minutes finalize subcommand."""
    from src.workflows.board_meeting_minutes import BoardMeetingMinutesWorkflow

    meeting_ref = args.meeting
    test_mode = getattr(args, "test", False)

    label = f"Finalize Minutes  — {meeting_ref}"
    if test_mode:
        label += "  [TEST MODE]"
    _print_header(label)

    actor = getattr(args, "actor", "secgen")
    wf = BoardMeetingMinutesWorkflow(actor=actor)

    initial_data: dict = {
        "meeting_ref": meeting_ref,
        "_start_at_step": "finalize",
        "test_mode": test_mode,
    }

    print(f"Workflow ID: {wf.workflow_id}")
    print()

    result = await wf.run(initial_data)

    print()
    if result.get("status") == "completed":
        _print_header("FINALIZE COMPLETED")
        ctx = wf.context
        print(f"  Protocol number: {ctx.get('protocol_number', 'N/A')}")
        decisions = ctx.get("decisions_written", [])
        print(f"  Decisions written: {len(decisions)}")
        for d in decisions:
            print(f"    • {d}")
    elif result.get("status") == "failed":
        _print_header("FINALIZE FAILED")
        print(f"  Failed at step: {result.get('step', 'unknown')}")
        print(f"  Error: {result.get('error', 'unknown')}")
    print()


def cmd_minutes_list_drafts(args: argparse.Namespace) -> None:
    """List pending draft minutes in Google Drive."""
    from src.integrations.google_drive import GoogleClient

    _print_header("Draft Minutes in Google Drive")

    folder_id = settings.google.minutes_drafts_folder_id
    if not folder_id:
        print("  minutes_drafts_folder_id is not configured in config.yaml.")
        print("  Set google.minutes_drafts_folder_id to list drafts.")
        return

    try:
        google = GoogleClient()
        docs = google.list_docs_in_folder(folder_id)
    except Exception as e:
        print(f"  Error listing documents: {e}")
        sys.exit(1)

    if not docs:
        print("  No draft documents found.")
        return

    print(f"  {'#':<4} {'Name':<50} {'Modified'}")
    print(f"  {'-'*4} {'-'*50} {'-'*10}")
    for i, doc in enumerate(docs, 1):
        mod_date = doc.get("modifiedTime", "")[:10]
        name = doc.get("name", "")
        print(f"  {i:<4} {name:<50} {mod_date}")
    print()


def cmd_minutes_events(args: argparse.Namespace) -> None:
    """Dispatch `minutes events` subcommands (record/list)."""
    # Event payloads are Greek; a legacy Windows console (cp1253) would garble
    # them on print. Make this command's output UTF-8 regardless of code page.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass

    events_command = getattr(args, "events_command", None)
    if events_command == "record":
        cmd_minutes_events_record(args)
    elif events_command == "list":
        cmd_minutes_events_list(args)
    else:
        print("  Usage: minutes events {record|list} ...")


def cmd_minutes_events_record(args: argparse.Namespace) -> None:
    """Record a single meeting event into the meeting_events table."""
    from src.core.meeting_events import MeetingEventsStore, VALID_EVENT_TYPES

    try:
        payload = json.loads(args.payload)
    except json.JSONDecodeError as e:
        print(f"  Error: --payload is not valid JSON: {e}")
        return
    if not isinstance(payload, dict):
        print("  Error: --payload must be a JSON object (dict).")
        return

    init_db()
    try:
        event_id = MeetingEventsStore().record_event(
            meeting_ref=args.meeting_ref,
            event_type=args.type,
            payload=payload,
            confidence=args.confidence,
        )
    except ValueError:
        valid = ", ".join(sorted(VALID_EVENT_TYPES))
        print(f"  Error: invalid event type '{args.type}'. Valid types: {valid}")
        return

    print(f"  Recorded event #{event_id}: {args.type} for {args.meeting_ref} ({args.confidence})")


def cmd_minutes_events_list(args: argparse.Namespace) -> None:
    """List captured meeting events for a meeting_ref (ts-ordered table)."""
    from src.core.meeting_events import MeetingEventsStore

    init_db()
    _print_header(f"Meeting Events — {args.meeting_ref}")
    events = MeetingEventsStore().list_events(
        args.meeting_ref,
        event_type=getattr(args, "type", None),
    )
    if not events:
        print("  (no events)")
        print()
        return

    print(f"  {'ts':<28} {'event_type':<16} {'confidence':<11} payload")
    print(f"  {'-'*28} {'-'*16} {'-'*11} {'-'*30}")
    for ev in events:
        payload_str = json.dumps(ev["payload"], ensure_ascii=False)
        print(f"  {ev['ts']:<28} {ev['event_type']:<16} {ev['confidence']:<11} {payload_str}")
    print()


def cmd_minutes_propose_decision(args: argparse.Namespace) -> None:
    """Draft one canonical Greek board decision from a discussion snippet."""
    # Output is Greek; force UTF-8 regardless of console code page (cp1253).
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass

    from src.core.meeting_events import MeetingEventsStore
    from src.workflows.decision_drafter import (
        LLMDecisionDrafter,
        propose_decision,
        render_decision,
    )

    try:
        # Resolve the decision discussion text from --snippet or --snippet-file.
        snippet = getattr(args, "snippet", None)
        snippet_file = getattr(args, "snippet_file", None)
        if snippet_file:
            snippet = Path(snippet_file).read_text(encoding="utf-8")
        if not snippet or not snippet.strip():
            print("ERROR: provide --snippet TEXT or --snippet-file PATH (non-empty).")
            return

        init_db()

        meeting_ref = args.meeting_ref
        sequence = getattr(args, "sequence", None)
        if sequence is None:
            prior_votes = MeetingEventsStore().list_events(
                meeting_ref, event_type="vote"
            )
            sequence = len(prior_votes) + 1

        proposal = propose_decision(
            meeting_ref=meeting_ref,
            sequence=sequence,
            transcript_snippet=snippet,
            drafter=LLMDecisionDrafter(),
            agenda_item=getattr(args, "agenda_item", "") or "",
        )

        _print_header(f"Πρόταση Απόφασης — {proposal['ref']}")
        print(render_decision(proposal))
        print()
        candidates = proposal.get("candidate_articles") or []
        if candidates:
            print("  Άρθρα που δόθηκαν ως τεκμηρίωση (candidate_articles):")
            for a in candidates:
                print(f"    - άρθρο {a.get('article')} του {a.get('doc')}: {a.get('title', '')}")
        else:
            print("  (δεν εντοπίστηκαν σχετικά άρθρα στο corpus)")
        print()
    except Exception as e:  # noqa: BLE001 — surface a one-liner, no traceback
        print(f"ERROR: {e}")


def cmd_minutes_build(args: argparse.Namespace) -> None:
    """Assemble a minutes skeleton (and optional draft) via the orchestrator."""
    # Output is Greek; force UTF-8 regardless of console code page (cp1253).
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass

    from datetime import datetime
    from src.workflows.minutes_pipeline import assemble_minutes

    init_db()  # MeetingEventsStore + audit logging touch the DB

    try:
        meeting_start = None
        raw_start = getattr(args, "meeting_start", None)
        if raw_start:
            text = raw_start.strip()
            if text.endswith(("Z", "z")):
                text = text[:-1] + "+00:00"
            meeting_start = datetime.fromisoformat(text)

        result = assemble_minutes(
            settings=settings,
            meeting_ref=args.meeting_ref,
            manifest_path=getattr(args, "manifest", None),
            transcript_path=getattr(args, "transcript_file", None),
            reuse_transcript=getattr(args, "reuse_transcript", False),
            meeting_start=meeting_start,
            draft=getattr(args, "draft", False),
        )

        skeleton = result["skeleton"]
        presence = skeleton.get("presence", {})
        items = skeleton.get("items", [])
        vote_count = sum(len(item.get("votes", [])) for item in items)

        _print_header(f"Minutes Build — {result['meeting_ref']}")
        print(f"  Source:        {result['source']}")
        print(f"  Segments:      {result['segment_count']}")
        print(f"  Agenda items:  {len(items)}")
        print(f"  Present:       {len(presence.get('present', []))}")
        print(f"  Absent:        {len(presence.get('absent', []))}")
        print(f"  Votes:         {vote_count}")
        print()
        print(f"  Skeleton:      {result.get('skeleton_path', '')}")
        if getattr(args, "draft", False):
            if result.get("draft") is not None:
                print(f"  Draft:         {result.get('draft_path', '(written)')}")
            else:
                print("  Draft:         (skipped — LLM unavailable or failed)")
        print()
    except Exception as e:  # noqa: BLE001 — surface a one-liner, no traceback
        print(f"ERROR: {e}")


def cmd_minutes_fetch_recording(args: argparse.Namespace) -> None:
    """On-demand pull of a Zoom meeting's recording assets to local disk."""
    # Topics + participant names may be Greek; a legacy Windows console
    # (cp1253) would garble them on print. Force UTF-8 regardless of code page.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass

    asyncio.run(_run_minutes_fetch_recording(args))


async def _run_minutes_fetch_recording(args: argparse.Namespace) -> None:
    """Async handler for `minutes fetch-recording`."""
    from src.integrations.zoom import ZoomClient

    init_db()  # asset download calls log_action

    try:
        client = ZoomClient()
        manifest = await client.download_recording_assets(
            args.meeting_uuid,
            dest_dir=getattr(args, "dest", None),
            audio_only=not getattr(args, "include_video", False),
        )

        _print_header(f"Recording assets — {manifest.get('meeting_uuid', '')}")
        print(f"  Topic:      {manifest.get('topic', '')}")
        print(f"  Start time: {manifest.get('start_time', '')}")
        print(f"  Dest dir:   {manifest.get('dest_dir', '')}")
        print()

        files = manifest.get("files", []) or []
        if not files:
            print("  (no recording files)")
        else:
            print(f"  {'source':<24} {'recording_type':<20} {'participant':<24} {'recording_start':<22} local_path")
            print(f"  {'-'*24} {'-'*20} {'-'*24} {'-'*22} {'-'*30}")
            for f in files:
                print(
                    f"  {f.get('source', ''):<24} {f.get('recording_type', ''):<20} "
                    f"{f.get('participant', ''):<24} {f.get('recording_start', ''):<22} "
                    f"{f.get('local_path', '')}"
                )
        print()

        if getattr(args, "participants", False):
            parts = await client.get_past_participants(args.meeting_uuid)
            print(f"  Participants: {len(parts)}")
            for p in parts:
                name = p.get("name") or p.get("user_name") or ""
                email = p.get("user_email") or p.get("email") or ""
                join = p.get("join_time") or ""
                leave = p.get("leave_time") or ""
                print(f"    - {name}  {email}  {join} -> {leave}")
            print()
    except Exception as e:  # noqa: BLE001 — surface a clean one-line error
        print(f"  ERROR: {e}")
        return


# --- Archive Commands (Phase 1 + 2) ---


def cmd_archive(args: argparse.Namespace) -> None:
    """Dispatch archive subcommands."""
    init_db()
    archive_command = getattr(args, "archive_command", None)
    if archive_command == "submit":
        asyncio.run(_run_archive_submit(args))
        return
    if archive_command == "review":
        asyncio.run(_run_archive_review(args))
        return
    if archive_command == "cancel":
        asyncio.run(_run_archive_cancel(args))
        return
    if archive_command == "resolve":
        asyncio.run(_run_archive_resolve(args))
        return
    if archive_command == "list":
        _run_archive_list(args)
        return
    # No subcommand given
    print("Usage:")
    print("  ai-assistant archive submit <path> [--title ...] [--labels ...] [--proto ...] [--sender ...] [--test]")
    print("  ai-assistant archive review <workflow_id> \"<text>\"")
    print("  ai-assistant archive cancel <workflow_id>")
    print("  ai-assistant archive list")
    sys.exit(1)


async def _run_archive_submit(args: argparse.Namespace) -> None:
    """Submit a PDF to the archive."""
    from src.workflows.archive import ArchiveWorkflow

    test_mode = getattr(args, "test", False)
    if test_mode:
        logging.getLogger().setLevel(logging.DEBUG)
        for h in logging.getLogger().handlers:
            h.setLevel(logging.DEBUG)
        _print_header("Archive Workflow  [TEST MODE]")
        print("  TEST MODE — no SharePoint upload, no πρωτόκολλο write")
    else:
        _print_header("Archive Workflow")

    pdf_path = Path(args.pdf_path)
    if not pdf_path.exists():
        print(f"ERROR: file not found: {pdf_path}")
        sys.exit(1)

    initial_data: dict = {
        "pdf_path": str(pdf_path.resolve()),
        "test_mode": test_mode,
        "sender_email": getattr(args, "sender", None) or "secgen@amnesty.org.gr",
    }
    if getattr(args, "title", None):
        initial_data["override_title"] = args.title
    if getattr(args, "labels", None):
        initial_data["override_labels"] = [s.strip() for s in args.labels.split(",") if s.strip()]
    if getattr(args, "proto", None):
        initial_data["override_protocol"] = args.proto

    wf = ArchiveWorkflow(actor=getattr(args, "actor", "secgen"))
    print(f"Workflow ID: {wf.workflow_id}")
    print()

    result = await wf.run(initial_data)
    print()
    if result.get("status") == "completed":
        _print_header("ARCHIVE COMPLETED")
        ctx = wf.context
        print(f"  Πρωτόκολλο:   {ctx.get('protocol_number', '?')}")
        print(f"  Workflow ID:  {wf.workflow_id}")
        revision_until = ctx.get("revision_open_until", "")
        if revision_until:
            print(f"  Revision until: {revision_until} (UTC)")
            print(f"  To amend:    ai-assistant archive review {wf.workflow_id} \"<text>\"")
            print(f"  To cancel:   ai-assistant archive cancel {wf.workflow_id}")
    elif result.get("status") == "failed":
        _print_header("ARCHIVE FAILED")
        print(f"  Failed at step: {result.get('step', 'unknown')}")
        print(f"  Error: {result.get('error', 'unknown')}")
    print()

    if test_mode and result.get("status") in ("completed", "failed"):
        input("  [TEST MODE] Press Enter to clean up (rollback)...")
        await wf.rollback(wf.context)
        print("  Cleanup done.")
        print()


async def _run_archive_review(args: argparse.Namespace) -> None:
    """Review an archived entry with free-text feedback (Phase 2)."""
    from src.core.audit import get_workflow_state, save_workflow_state
    from src.workflows import archive_llm
    from src.workflows.archive import ArchiveWorkflow, is_revision_window_open

    workflow_id = args.workflow_id
    user_text = args.text or ""

    state = get_workflow_state(workflow_id)
    if not state:
        print(f"ERROR: workflow_id {workflow_id} not found.")
        sys.exit(1)
    if state.get("workflow_name") != "archive":
        print(f"ERROR: workflow_id {workflow_id} is not an archive workflow.")
        sys.exit(1)

    data = json.loads(state.get("data") or "{}")
    ctx = data.get("context") or {}

    if not is_revision_window_open(ctx):
        print(f"ERROR: revision window has closed (was open until {ctx.get('revision_open_until', '?')}).")
        sys.exit(1)

    original = {
        "title": (ctx.get("llm_result") or {}).get("title"),
        "labels": (ctx.get("llm_result") or {}).get("labels"),
        "key_points": (ctx.get("llm_result") or {}).get("key_points"),
        "protocol_id": ctx.get("protocol_number"),
    }

    parsed = await archive_llm.parse_user_feedback(
        workflow_id=workflow_id,
        original=original,
        user_text=user_text,
    )

    _print_header("Archive Review")
    print(f"  Workflow ID: {workflow_id}")
    print(f"  Intent:      {parsed.get('intent')}")
    print(f"  Confidence:  {parsed.get('confidence', 0):.2f}")
    print(f"  Summary:     {parsed.get('summary_for_human', '')}")
    print()

    intent = parsed.get("intent")
    if intent == "acknowledge":
        log_action(
            workflow="archive",
            action="review_acknowledged",
            actor=getattr(args, "actor", "secgen"),
            details={"workflow_id": workflow_id, "user_text": user_text},
        )
        print("  No changes applied.")
        return
    if intent == "cancel":
        wf = ArchiveWorkflow()
        wf.workflow_id = workflow_id
        await wf.rollback(ctx)
        save_workflow_state(
            workflow_name="archive",
            workflow_id=workflow_id,
            state="cancelled",
            data=data,
        )
        print("  Archive entry cancelled and rolled back.")
        return
    if intent == "unrelated":
        print("  Feedback judged unrelated to this archive entry — no action taken.")
        return

    # intent == "amend" — apply to SharePoint + xlsx + context atomically
    from src.workflows.archive import apply_amendments

    amendments = parsed.get("amendments") or {}
    summary = await apply_amendments(workflow_id, ctx, amendments)
    applied = summary.get("applied", [])

    data["context"] = ctx
    save_workflow_state(
        workflow_name="archive",
        workflow_id=workflow_id,
        state=state.get("state", "completed"),
        data=data,
    )
    log_action(
        workflow="archive",
        action="review_amended",
        actor=getattr(args, "actor", "secgen"),
        details={
            "workflow_id": workflow_id,
            "fields": applied,
            "user_text": user_text,
            "summary": summary,
        },
    )

    if applied:
        print(f"  Amended fields:    {', '.join(applied)}")
        if summary.get("renamed_to"):
            print(f"  File renamed to:   {summary['renamed_to']}")
        if summary.get("protocol_id_rewrite"):
            print(f"  Protocol id:       {summary['protocol_id_rewrite']}")
        if summary.get("rename_error"):
            print(f"  ⚠ Rename error:    {summary['rename_error']}")
        if summary.get("row_update_error"):
            print(f"  ⚠ Row update err:  {summary['row_update_error']}")
        if summary.get("protocol_id_warning"):
            print(f"  ⚠ {summary['protocol_id_warning']}")
    else:
        print("  No changes applied (empty amendments).")


async def _run_archive_resolve(args: argparse.Namespace) -> None:
    """SecGen-only: confirm or reject filling a pre-reserved πρωτόκολλο slot.

    Triggers when the bot saw an existing row with no file at the claimed
    αρ.πρωτ. AND the submitted document's title didn't confidently match
    the row's title.  See ``src/workflows/archive.py::_step_collision_check``.

    "Approve" means: yes, this document IS the right file for the reservation.
                     Proceed to upload + fill-blank-fields in the row.

    "Reject" means: the document doesn't belong to that reservation; abort
                    the workflow and release any reservation we held.
                    Sender can re-submit with the correct number (or none).

    Only SecGen should run this command — intentionally CLI-only (no email
    path) because the resolution requires a human judgement call.
    """
    from src.core.audit import get_workflow_state, save_workflow_state
    from src.workflows.archive import ArchiveWorkflow

    workflow_id = args.workflow_id
    decision = args.decision  # "approve" | "reject"

    state = get_workflow_state(workflow_id)
    if not state:
        print(f"ERROR: workflow_id {workflow_id} not found.")
        sys.exit(1)
    if state.get("workflow_name") != "archive":
        print(f"ERROR: workflow_id {workflow_id} is not an archive workflow.")
        sys.exit(1)

    data = json.loads(state.get("data") or "{}")
    ctx = data.get("context") or {}
    pending = ctx.get("pending_reservation_confirmation")
    if not pending:
        print(f"ERROR: workflow {workflow_id} is not awaiting reservation confirmation.")
        print(f"       (state={state.get('state')}, no pending_reservation_confirmation in context)")
        sys.exit(1)

    _print_header(f"Reservation Confirmation — {workflow_id}")
    print(f"  Protocol number:    {pending.get('protocol_number')}")
    print(f"  Row title (yours):  {pending.get('existing_title')}")
    print(f"  Submitted title:    {pending.get('proposed_title')}")
    print(f"  Match confidence:   {float(pending.get('match_confidence') or 0):.2f}")
    print(f"  Raised at:          {pending.get('raised_at', '?')}")
    print()

    if decision == "reject":
        wf = ArchiveWorkflow(actor=getattr(args, "actor", "secgen"))
        wf.workflow_id = workflow_id
        await wf.rollback(ctx)
        save_workflow_state(
            workflow_name="archive",
            workflow_id=workflow_id,
            state="cancelled",
            data=data,
        )
        log_action(
            workflow="archive",
            action="reservation_confirm_rejected",
            actor=getattr(args, "actor", "secgen"),
            details={"workflow_id": workflow_id, "pending": pending},
        )
        print("  Decision: REJECTED — workflow rolled back, sender should re-submit.")
        return

    if decision != "approve":
        print(f"ERROR: unknown decision {decision!r} (use 'approve' or 'reject').")
        sys.exit(1)

    # ── Approve: convert into a reservation-fill and resume from upload step ─
    ctx.pop("pending_reservation_confirmation", None)
    ctx["is_filling_reservation"] = True
    ctx["reserved_row"] = pending.get("existing_row") or ctx.get("reserved_row")
    ctx["_start_at_step"] = "upload_and_register"

    log_action(
        workflow="archive",
        action="reservation_confirm_approved",
        actor=getattr(args, "actor", "secgen"),
        details={"workflow_id": workflow_id, "pending": pending},
    )

    # Re-run the workflow from the upload step with the preserved context.
    wf = ArchiveWorkflow(actor=getattr(args, "actor", "secgen"))
    wf.workflow_id = workflow_id  # preserve id so reservation/audit links carry over
    result = await wf.run(ctx)

    if result.get("status") == "completed":
        ctx_final = wf.context
        print("  Decision: APPROVED — workflow completed.")
        print(f"  Πρωτόκολλο:  {ctx_final.get('protocol_number', '?')}")
        print(f"  File:        {ctx_final.get('remote_filename', '?')}")
    else:
        print(f"  Decision: APPROVED but workflow did not complete cleanly.")
        print(f"  Status: {result.get('status')} — {result.get('error', '')}")


async def _run_archive_cancel(args: argparse.Namespace) -> None:
    """Cancel an archive workflow and roll back side effects."""
    from src.core.audit import get_workflow_state, save_workflow_state
    from src.workflows.archive import ArchiveWorkflow

    workflow_id = getattr(args, "workflow_id", None)
    state = get_workflow_state(workflow_id)
    if not state:
        print(f"ERROR: workflow_id {workflow_id} not found.")
        sys.exit(1)
    if state.get("workflow_name") != "archive":
        print(f"ERROR: workflow_id {workflow_id} is not an archive workflow.")
        sys.exit(1)

    data = json.loads(state.get("data") or "{}")
    ctx = data.get("context") or {}

    wf = ArchiveWorkflow()
    wf.workflow_id = workflow_id
    await wf.rollback(ctx)
    save_workflow_state(
        workflow_name="archive",
        workflow_id=workflow_id,
        state="cancelled",
        data=data,
    )
    print(f"Archive workflow {workflow_id} cancelled and rolled back.")


def _run_archive_list(args: argparse.Namespace) -> None:
    """List in-progress + revision_open + recent (last 30 days) archive workflows."""
    from src.core.audit import _get_connection
    from src.workflows.archive import is_revision_window_open

    conn = _get_connection()
    rows = conn.execute(
        """SELECT workflow_id, state, data, created_at, updated_at
             FROM workflow_state
            WHERE workflow_name = 'archive'
              AND (state IN ('in_progress', 'awaiting_approval', 'executing', 'pending')
                   OR datetime(updated_at) >= datetime('now', '-30 days'))
            ORDER BY updated_at DESC""",
    ).fetchall()

    if not rows:
        print("No archive workflows found.")
        return

    _print_header("Archive Workflows")
    print(f"  {'Workflow ID':<12} {'State':<18} {'Revision':<10} {'Πρωτόκολλο':<12} Updated")
    print(f"  {'-'*12} {'-'*18} {'-'*10} {'-'*12} {'-'*19}")
    for row in rows:
        data = json.loads(row["data"] or "{}")
        ctx = data.get("context") or {}
        proto = ctx.get("protocol_number", "-")
        revision = "open" if is_revision_window_open(ctx) else "-"
        updated = (row["updated_at"] or "")[:19]
        print(f"  {row['workflow_id']:<12} {row['state']:<18} {revision:<10} {proto:<12} {updated}")
    print()


# --- RSS feed management (replaces MonitoRSS) ---


def cmd_rss(args: argparse.Namespace) -> None:
    """Dispatch RSS subcommands."""
    init_db()
    sub = getattr(args, "rss_command", None) or ""
    if sub == "list":
        _rss_list()
    elif sub == "add-feed":
        _rss_add_feed(args)
    elif sub == "remove-feed":
        _rss_remove_feed(args)
    elif sub == "add-route":
        _rss_add_route(args)
    elif sub == "remove-route":
        _rss_remove_route(args)
    elif sub == "poll-now":
        asyncio.run(_rss_poll_now())
    elif sub == "seed-amnesty":
        _rss_seed_amnesty()
    else:
        print("Usage:")
        print("  ai-assistant rss list")
        print("  ai-assistant rss add-feed <url> [--label NAME]")
        print("  ai-assistant rss remove-feed <url>")
        print("  ai-assistant rss add-route <feed_url> <channel_id>")
        print("                            [--url-pattern STR] [--title-pattern REGEX]")
        print("                            [--forum-tag-id ID | --forum-tag-name NAME]")
        print("                            [--label NAME]")
        print("  ai-assistant rss remove-route <route_id>")
        print("  ai-assistant rss poll-now")
        print("  ai-assistant rss seed-amnesty")
        sys.exit(1)


def _rss_list() -> None:
    from src.core.audit import list_rss_feeds, list_rss_routes
    feeds = list_rss_feeds()
    _print_header(f"RSS Feeds — {len(feeds)} configured")
    if not feeds:
        print("  (none — register one with `ai-assistant rss add-feed <url>`)")
        return
    for feed in feeds:
        status = "✓" if feed.get("enabled") else "✗ disabled"
        print(f"  [{status}] {feed['feed_url']}")
        print(f"    label:       {feed.get('label') or '—'}")
        print(f"    last polled: {feed.get('last_polled_at') or 'never'}")
        cursor = feed.get("last_seen_guid") or "—"
        print(f"    cursor:      {cursor[:60]}")
        routes = list_rss_routes(feed["feed_url"])
        if not routes:
            print(f"    routes:      (none — items will not be posted)")
        else:
            print(f"    routes ({len(routes)}):")
            for r in routes:
                tag = r.get("forum_tag_name") or r.get("forum_tag_id") or "—"
                pat = r.get("url_pattern") or r.get("title_pattern") or "*"
                print(f"      [id={r['id']}] → ch={r['channel_id']} tag={tag} pattern={pat} ({r.get('label') or ''})")
        print()


def _rss_add_feed(args: argparse.Namespace) -> None:
    from src.core.audit import upsert_rss_feed
    upsert_rss_feed(args.url, label=args.label or None)
    print(f"Registered feed: {args.url} (label={args.label or '—'})")


def _rss_remove_feed(args: argparse.Namespace) -> None:
    from src.core.audit import delete_rss_feed
    delete_rss_feed(args.url)
    print(f"Deleted feed {args.url} and all its routes.")


def _rss_add_route(args: argparse.Namespace) -> None:
    from src.core.audit import add_rss_route
    route_id = add_rss_route(
        args.feed_url,
        channel_id=args.channel_id,
        forum_tag_id=args.forum_tag_id or None,
        forum_tag_name=args.forum_tag_name or None,
        url_pattern=args.url_pattern or None,
        title_pattern=args.title_pattern or None,
        label=args.label or None,
    )
    print(f"Added route id={route_id} for feed {args.feed_url} → channel {args.channel_id}")


def _rss_remove_route(args: argparse.Namespace) -> None:
    from src.core.audit import delete_rss_route
    delete_rss_route(args.route_id)
    print(f"Deleted route {args.route_id}.")


async def _rss_poll_now() -> None:
    """Manual poll — runs the same logic as the in-bot loop, standalone.

    Note: posts to Discord require the bot process to be running.  This
    command exercises fetch + dedup + route-matching, but the actual
    Discord posting happens via the bot's own loop.  Use this for dry-run
    validation; use the bot's `/ai-assistant rss poll-now` slash command
    for real posting.
    """
    from src.integrations.rss import fetch_feed, filter_new_items, item_matches_route
    from src.core.audit import list_rss_feeds, list_rss_routes

    _print_header("RSS Poll (dry-run, no Discord posting)")
    feeds = list_rss_feeds(enabled_only=True)
    if not feeds:
        print("  No enabled feeds.")
        return
    for feed in feeds:
        feed_url = feed["feed_url"]
        print(f"\n  ▸ {feed_url}")
        items = await fetch_feed(feed_url)
        print(f"      Fetched {len(items)} item(s)")
        new_items = filter_new_items(items, last_seen_guid=feed.get("last_seen_guid"))
        print(f"      New since last poll: {len(new_items)}")
        routes = list_rss_routes(feed_url)
        for item in new_items[:5]:
            matched = [r for r in routes if item_matches_route(
                item,
                url_pattern=r.get("url_pattern"),
                title_pattern=r.get("title_pattern"),
            )]
            print(f"      • {item.title[:80]}")
            print(f"        link={item.link}")
            print(f"        matched_routes={[r['id'] for r in matched]}")


def _rss_seed_amnesty() -> None:
    """Seed the database with amnesty.gr feed + the 4 standard routes.

    Per user spec: route by URL substring against the single rss.xml feed.
    Channel IDs / forum tag names come from config — empty values produce
    a warning so the user knows to fill them in before posts will land.
    """
    from src.core.audit import upsert_rss_feed, add_rss_route, list_rss_routes
    from src.config import settings

    feed_url = "https://www.amnesty.gr/rss.xml"
    upsert_rss_feed(feed_url, label="amnesty.gr — official feed")
    print(f"✓ Registered feed: {feed_url}")

    events_ch = settings.discord.channels.events_channel_id
    info_ch = settings.discord.platform_bridge.board_meeting.agenda_channel_id

    if not events_ch:
        print("⚠ discord.channels.events_channel_id not configured — events route skipped")
    if not info_ch:
        print("⚠ discord.platform_bridge.board_meeting.agenda_channel_id not configured — "
              "articles/press/ektheseis routes skipped")

    # Avoid duplicating routes on re-run
    existing = list_rss_routes(feed_url)
    existing_signatures = {
        (r["channel_id"], r.get("url_pattern"), r.get("forum_tag_name"))
        for r in existing
    }

    seeds: list[dict] = []
    if events_ch:
        seeds.append({
            "channel_id": events_ch, "url_pattern": "/news/events/",
            "forum_tag_name": None, "label": "Εκδηλώσεις",
        })
    if info_ch:
        seeds.extend([
            {"channel_id": info_ch, "url_pattern": "/news/articles/",
             "forum_tag_name": "Άρθρα", "label": "Άρθρα"},
            {"channel_id": info_ch, "url_pattern": "/news/press/",
             "forum_tag_name": "Δελτία Τύπου", "label": "Δελτία Τύπου"},
            {"channel_id": info_ch, "url_pattern": "/news/ektheseis/",
             "forum_tag_name": "Εκθέσεις", "label": "Εκθέσεις"},
        ])

    for s in seeds:
        sig = (s["channel_id"], s["url_pattern"], s.get("forum_tag_name"))
        if sig in existing_signatures:
            print(f"  (skip — already present: {s['label']})")
            continue
        rid = add_rss_route(
            feed_url,
            channel_id=s["channel_id"],
            url_pattern=s["url_pattern"],
            forum_tag_name=s.get("forum_tag_name"),
            label=s["label"],
        )
        print(f"✓ Added route id={rid}: {s['label']} → channel {s['channel_id']}")

    print("\nDone.  Run `ai-assistant rss list` to verify, "
          "or `ai-assistant rss poll-now` for a dry-run.")


# --- M365 inbox watcher (Phase 3) ---


def cmd_m365(args: argparse.Namespace) -> None:
    """Dispatch M365 inbox / Graph subscription subcommands."""
    init_db()
    sub = getattr(args, "m365_command", None) or ""
    if sub == "subscribe":
        asyncio.run(_run_m365_subscribe(args))
    elif sub == "unsubscribe":
        asyncio.run(_run_m365_unsubscribe(args))
    elif sub == "subscriptions":
        asyncio.run(_run_m365_subscriptions(args))
    elif sub == "renew-now":
        asyncio.run(_run_m365_renew_now(args))
    elif sub == "poll-now":
        asyncio.run(_run_m365_poll_now(args))
    else:
        print("Usage:")
        print("  ai-assistant m365 subscribe       Create the Graph webhook subscription")
        print("  ai-assistant m365 unsubscribe <id>  Delete a subscription")
        print("  ai-assistant m365 subscriptions   List active subscriptions (local + Graph)")
        print("  ai-assistant m365 renew-now       Renew any expiring subscriptions immediately")
        print("  ai-assistant m365 poll-now        Run the safety poll once, now")
        sys.exit(1)


async def _run_m365_subscribe(args: argparse.Namespace) -> None:
    from src.integrations.graph_subscriptions import (
        GraphSubscriptionError,
        GraphSubscriptionsClient,
    )

    _print_header("Create Graph Webhook Subscription")
    notification_url = getattr(args, "url", None) or settings.m365_inbox.webhook_url or ""
    if not notification_url:
        print("ERROR: No webhook URL.  Pass --url or set m365_inbox.webhook_url in config.yaml.")
        sys.exit(1)
    try:
        client = GraphSubscriptionsClient()
        body = await client.create(notification_url=notification_url)
    except GraphSubscriptionError as e:
        print(f"FAILED: {e}")
        sys.exit(1)
    print(f"  Subscription ID:    {body['id']}")
    print(f"  Resource:           {body['resource']}")
    print(f"  Expiration (UTC):   {body['expirationDateTime']}")
    print(f"  Notification URL:   {body['notificationUrl']}")


async def _run_m365_unsubscribe(args: argparse.Namespace) -> None:
    from src.integrations.graph_subscriptions import (
        GraphSubscriptionError,
        GraphSubscriptionsClient,
    )

    sub_id = getattr(args, "subscription_id", None)
    if not sub_id:
        print("ERROR: pass a subscription id, e.g. ai-assistant m365 unsubscribe <id>")
        sys.exit(1)
    try:
        client = GraphSubscriptionsClient()
        await client.delete(sub_id)
    except GraphSubscriptionError as e:
        print(f"FAILED: {e}")
        sys.exit(1)
    print(f"Subscription {sub_id} deleted.")


async def _run_m365_subscriptions(args: argparse.Namespace) -> None:
    from src.core.audit import get_active_graph_subscriptions
    from src.integrations.graph_subscriptions import GraphSubscriptionsClient

    _print_header("Graph Webhook Subscriptions")

    print("  Local (DB-tracked):")
    local = get_active_graph_subscriptions()
    if not local:
        print("    (none)")
    for row in local:
        print(f"    {row['subscription_id']}  expires={row['expiration_date_time']}  "
              f"resource={row['resource']}")
    print()

    print("  Remote (per Graph):")
    try:
        client = GraphSubscriptionsClient()
        remote = await client.list_remote()
    except Exception as e:
        print(f"    ERROR querying Graph: {e}")
        return
    if not remote:
        print("    (none)")
    for sub in remote:
        print(f"    {sub.get('id')}  expires={sub.get('expirationDateTime')}  "
              f"resource={sub.get('resource')}")


async def _run_m365_renew_now(args: argparse.Namespace) -> None:
    from src.integrations.graph_subscriptions import GraphSubscriptionsClient

    threshold = getattr(args, "threshold_hours", None)
    client = GraphSubscriptionsClient()
    renewed = await client.renew_expiring(threshold_hours=threshold)
    if renewed:
        print(f"Renewed {len(renewed)} subscription(s):")
        for sid in renewed:
            print(f"  • {sid}")
    else:
        print("Nothing to renew — all subscriptions are healthy.")


async def _run_m365_poll_now(args: argparse.Namespace) -> None:
    from src.workflows.email_intake import run_safety_poll

    _print_header("Safety Poll (manual)")
    result = await run_safety_poll()
    print(f"  Processed: {result.get('processed', 0)} message(s)")
    by_outcome = result.get("by_outcome") or {}
    for outcome, count in by_outcome.items():
        print(f"    {outcome:<20} {count}")


# --- Existing Commands ---


def cmd_status(args: argparse.Namespace) -> None:
    """Show platform status."""
    init_db()
    print(f"AI Assistant Platform v{settings.app.version}")
    print(f"Environment: {settings.app_env}")
    print(f"Claude model: {settings.claude.model}")
    print(f"Database: {settings.storage.database_path}")
    print()

    apis = {
        "Anthropic (Claude)": bool(settings.anthropic_api_key),
        "Microsoft Graph": bool(settings.ms_client_id),
        "Google Cloud": bool(settings.google_client_id),
        "Zoom": bool(settings.zoom_client_id),
        "Brevo": bool(settings.brevo_api_key),
        "Discord": bool(settings.discord_bot_token),
    }
    print("API Keys Configured:")
    for name, configured in apis.items():
        status = "+" if configured else "-"
        print(f"  [{status}] {name}")


def cmd_audit(args: argparse.Namespace) -> None:
    """Show recent audit log entries."""
    init_db()
    entries = get_audit_log(workflow=args.workflow, limit=args.limit)
    if not entries:
        print("No audit log entries found.")
        return

    for entry in entries:
        print(
            f"[{entry['timestamp']}] {entry['workflow']} | {entry['action']} | "
            f"{entry['actor']} | {entry.get('target', '-')} | {entry['status']}"
        )


def cmd_test_claude(args: argparse.Namespace) -> None:
    """Test Claude API connection."""
    init_db()
    client = ClaudeClient()
    print("Testing Claude API connection...")
    try:
        response = client.generate(
            user_prompt="Respond with exactly: 'Connection successful.'",
            workflow="smoke_test",
        )
        print(f"Response: {response}")
        print(f"Usage: {client.usage_summary}")
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)


def cmd_smoke_test(args: argparse.Namespace) -> None:
    """Run end-to-end smoke test (Claude -> PDF -> audit log)."""
    init_db()
    from src.documents.pdf_generator import generate_pdf

    _print_header("Phase 0 Smoke Test")

    # Step 1: Claude generates content
    print("1. Testing Claude API...")
    client = ClaudeClient()
    try:
        content = client.generate(
            user_prompt=(
                "Generate a short test document for Amnesty International Greece. "
                "Return a JSON object with keys: title, subtitle, sections (array of {heading, body}). "
                "Write in Greek. Keep it brief - 2 sections maximum."
            ),
            system_prompt="You are a document generation assistant. Return valid JSON only, no markdown fences.",
            workflow="smoke_test",
        )
        print(f"   Claude responded ({client.usage_summary['total_input_tokens']} input, "
              f"{client.usage_summary['total_output_tokens']} output tokens)")
    except Exception as e:
        print(f"   FAILED: {e}")
        sys.exit(1)

    # Step 2: Generate PDF
    print("2. Generating PDF...")
    try:
        doc_content = json.loads(_strip_json_fences(content))
        output_path = Path("data/smoke_test.pdf")
        generate_pdf(doc_content, output_path, workflow="smoke_test")
        print(f"   PDF saved to {output_path}")
    except Exception as e:
        print(f"   FAILED: {e}")
        sys.exit(1)

    # Step 3: Verify audit log
    print("3. Checking audit log...")
    entries = get_audit_log(workflow="smoke_test")
    print(f"   Found {len(entries)} audit entries for smoke_test")

    print()
    print(f"Smoke Test Complete. Cost: ${client.total_cost:.6f}")


# --- Auth Commands ---


def cmd_auth_google(args: argparse.Namespace) -> None:
    """Run the Google OAuth2 flow and save credentials token.

    Pass ``--fresh`` to bypass the cached token entirely — needed when
    switching accounts (e.g. personal → technical) since otherwise the
    OAuth library tries to refresh the stale token first and fails fast
    with ``invalid_grant`` before reaching the browser flow.
    """
    from src.integrations.google_drive import GoogleClient

    force_interactive = getattr(args, "fresh", False)
    print("Starting Google OAuth2 authentication...")
    if force_interactive:
        print("(--fresh: bypassing any cached token)")
    print("A browser window will open — log in and click Allow.")
    print()
    try:
        client = GoogleClient()
        client.authenticate(force_interactive=force_interactive)
        print("OK Authentication successful! Token saved to data/google_token.json")
    except Exception as e:
        print(f"FAILED Authentication failed: {e}")
        sys.exit(1)


# --- Microsoft Auth and OneDrive Commands ---


def cmd_auth(args: argparse.Namespace) -> None:
    """Dispatch auth subcommands (microsoft, google)."""
    auth_command = getattr(args, "auth_command", None)
    if auth_command == "microsoft":
        cmd_auth_microsoft(args)
    elif auth_command == "google":
        cmd_auth_google(args)
    else:
        print("Usage: ai-in-ai auth microsoft | google")


def cmd_auth_microsoft(args: argparse.Namespace) -> None:
    """Run the Microsoft interactive OAuth2 sign-in flow and cache the token."""
    from src.integrations.onedrive import OneDriveClient, OneDriveAuthRequired

    print("Starting Microsoft OAuth2 sign-in...")
    print("A browser window will open — log in with your Amnesty Microsoft account.")
    print(f"Waiting for redirect to: {settings.ms_redirect_uri}")
    print()
    try:
        client = OneDriveClient()
        client.authenticate_interactive()
        print("Sign-in successful — token cached.")
    except TimeoutError:
        print("FAILED Sign-in timed out. No redirect received within 5 minutes.")
        sys.exit(1)
    except Exception as e:
        print(f"FAILED Sign-in failed: {e}")
        sys.exit(1)


def cmd_onedrive(args: argparse.Namespace) -> None:
    """Dispatch onedrive subcommands."""
    onedrive_command = getattr(args, "onedrive_command", None)
    if onedrive_command == "ls":
        cmd_onedrive_ls(args)
    elif onedrive_command == "backup-status":
        cmd_onedrive_backup_status(args)
    elif onedrive_command == "backup-restore":
        cmd_onedrive_backup_restore(args)
    else:
        print("Usage:")
        print("  ai-assistant onedrive ls [path]              List files under the archive root")
        print("  ai-assistant onedrive backup-status          Show the local πρωτόκολλο backup info")
        print("  ai-assistant onedrive backup-restore <dest>  Copy the backup to <dest> (offline recovery)")


def cmd_onedrive_backup_status(args: argparse.Namespace) -> None:
    """Show info about the local πρωτόκολλο backup (size, mtime, validity)."""
    from datetime import datetime as _dt
    from src.integrations.onedrive import OneDriveClient

    backup_path = OneDriveClient.PROTOCOL_BACKUP_PATH
    _print_header("Πρωτόκολλο — Local Safety Backup")

    if not backup_path.exists():
        print(f"  Path:    {backup_path.resolve()}")
        print(f"  Status:  NOT PRESENT — run any archive workflow to populate it.")
        return

    stat = backup_path.stat()
    mtime = _dt.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
    age_seconds = _dt.now().timestamp() - stat.st_mtime
    age_human = (
        f"{int(age_seconds)} s" if age_seconds < 60
        else f"{int(age_seconds // 60)} min" if age_seconds < 3600
        else f"{age_seconds / 3600:.1f} h" if age_seconds < 86400
        else f"{age_seconds / 86400:.1f} d"
    )

    print(f"  Path:    {backup_path.resolve()}")
    print(f"  Size:    {stat.st_size:,} bytes")
    print(f"  Updated: {mtime}  ({age_human} ago)")

    # Quickly verify the file opens as a valid xlsx
    try:
        import openpyxl
        wb = openpyxl.load_workbook(backup_path, data_only=True, read_only=True)
        tabs = wb.sheetnames
        wb.close()
        print(f"  Sheets:  {', '.join(tabs)}")
        print(f"  Status:  VALID (openpyxl can read it)")
    except Exception as e:
        print(f"  Status:  CORRUPT? — could not parse: {e}")


def cmd_onedrive_backup_restore(args: argparse.Namespace) -> None:
    """Copy the latest backup to a user-specified destination.

    Intentionally does NOT re-upload to OneDrive — restoration of the live
    SharePoint copy is a manual decision (you might want to compare against
    the broken version, restore via Microsoft's own version history, etc.).
    This command just hands you a fresh local copy.
    """
    import shutil
    from src.integrations.onedrive import OneDriveClient

    backup_path = OneDriveClient.PROTOCOL_BACKUP_PATH
    if not backup_path.exists():
        print(f"ERROR: no backup found at {backup_path.resolve()}")
        print("Run any archive workflow first — the backup is refreshed on every download.")
        sys.exit(1)

    dest = Path(args.dest)
    if dest.is_dir():
        dest = dest / backup_path.name
    if dest.exists() and not getattr(args, "force", False):
        print(f"ERROR: destination already exists: {dest}")
        print("Pass --force to overwrite, or pick a different path.")
        sys.exit(1)

    shutil.copy2(backup_path, dest)
    print(f"Restored: {backup_path.resolve()}  →  {dest.resolve()}")


def cmd_onedrive_ls(args: argparse.Namespace) -> None:
    """List files/folders under the SharePoint archive root (or a sub-path)."""
    from src.integrations.onedrive import OneDriveClient, OneDriveAuthRequired

    path: str = getattr(args, "path", "") or ""

    _print_header(f"OneDrive Archive — /{settings.onedrive.archive_root}/{path}".rstrip("/"))

    try:
        client = OneDriveClient()
        items = asyncio.run(client.list_files(path))
    except OneDriveAuthRequired as e:
        print(f"Not authenticated: {e}")
        print("Run: ai-in-ai auth microsoft")
        sys.exit(1)
    except Exception as e:
        print(f"Error listing files: {e}")
        sys.exit(1)

    if not items:
        print("  (empty folder)")
        return

    # Pretty-print: kind | name | size | last modified
    print(f"  {'Kind':<7}  {'Name':<55}  {'Size':>10}  {'Modified'}")
    print(f"  {'-'*7}  {'-'*55}  {'-'*10}  {'-'*10}")
    for item in items:
        kind = "folder" if "folder" in item else "file"
        name = item.get("name", "")
        size = item.get("size", 0)
        size_str = f"{size:,}" if kind == "file" else "-"
        modified = (item.get("lastModifiedDateTime") or "")[:10]
        print(f"  {kind:<7}  {name:<55}  {size_str:>10}  {modified}")
    print(f"\n  {len(items)} item(s)")


def cmd_upload_template(args: argparse.Namespace) -> None:
    """Upload a local HTML file to a Brevo template slot."""
    from src.integrations.brevo import BrevoClient

    html_path = Path(args.file)
    if not html_path.exists():
        print(f"File not found: {html_path}")
        sys.exit(1)

    html = html_path.read_text(encoding="utf-8")
    template_id = args.template_id

    print(f"Uploading '{html_path.name}' to Brevo template #{template_id} …")

    async def _upload() -> None:
        client = BrevoClient()
        await client.update_template(
            template_id=template_id,
            html_content=html,
            subject=args.subject or None,
            template_name=args.name or None,
        )

    asyncio.run(_upload())
    print(f"Done — template #{template_id} updated.")


# --- Logs ---


def cmd_logs(args: argparse.Namespace) -> None:
    """Inspect the rotating log files at ``data/logs/``."""
    from src.core.logging_config import error_log_path, main_log_path

    sub = getattr(args, "logs_command", None) or "path"

    main_p = main_log_path()
    err_p = error_log_path()

    if sub == "path":
        print(f"Main log:   {main_p}")
        print(f"Errors log: {err_p}")
        print()
        if not main_p.exists() and not err_p.exists():
            print("(No log files yet — run the bot at least once.)")
        else:
            for p in (main_p, err_p):
                if p.exists():
                    try:
                        size_kb = p.stat().st_size / 1024
                        print(f"  {p.name}: {size_kb:.1f} KB")
                    except OSError:
                        pass
        return

    target = err_p if sub == "errors" else main_p
    if not target.exists():
        print(f"(No log file at {target} yet — run the bot first.)")
        return

    lines = int(getattr(args, "lines", 50) or 50)
    text = target.read_text(encoding="utf-8", errors="replace").splitlines()
    pattern = getattr(args, "pattern", None)

    if pattern:
        import re as _re
        try:
            rx = _re.compile(pattern, _re.IGNORECASE)
        except _re.error as exc:
            print(f"Invalid regex: {exc}")
            return
        matches = [ln for ln in text if rx.search(ln)]
        for ln in matches[-lines:]:
            print(ln)
        if not matches:
            print(f"(No lines matching {pattern!r} in {target.name})")
        return

    for ln in text[-lines:]:
        print(ln)


# --- Discord ---


def cmd_discord(args: argparse.Namespace) -> None:
    discord_command = getattr(args, "discord_command", None)
    if discord_command == "run":
        from src.integrations.discord.bot import run_bot_sync
        run_bot_sync()
        return
    if discord_command == "clear-stale-commands":
        from src.integrations.discord.bot import clear_stale_commands_sync
        scope_global = not getattr(args, "guild_only", False)
        scope_guild = not getattr(args, "global_only", False)
        print("Connecting to Discord to wipe stale slash commands...")
        result = clear_stale_commands_sync(
            clear_globals=scope_global,
            clear_guild=scope_guild,
        )
        print()
        if scope_global:
            cmds = result.get("global", [])
            print(f"Global registry: cleared {len(cmds)} command(s)" + (f" — {cmds}" if cmds else ""))
        if scope_guild:
            cmds = result.get("guild", [])
            print(f"Guild registry:  cleared {len(cmds)} command(s)" + (f" — {cmds}" if cmds else ""))
        print()
        print("Now restart the bot with `ai-assistant discord run` — it will sync the")
        print("current command set fresh.  Discord clients may take up to an hour to")
        print("drop their local cache of the wiped commands.")
        return
    print("Usage: ai-assistant discord {run|clear-stale-commands}")


# --- Entry Point ---


def main() -> None:
    """Main CLI entry point."""
    setup_logging()

    parser = argparse.ArgumentParser(
        prog="ai-assistant",
        description="AI Assistant Platform -- Amnesty International Greece",
    )
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # Status
    subparsers.add_parser("status", help="Show platform status")

    # Audit
    audit_parser = subparsers.add_parser("audit", help="Show audit log")
    audit_parser.add_argument("--workflow", "-w", help="Filter by workflow")
    audit_parser.add_argument("--limit", "-l", type=int, default=20, help="Number of entries")

    # Test Claude
    subparsers.add_parser("test-claude", help="Test Claude API connection")

    # Smoke test
    subparsers.add_parser("smoke-test", help="Run end-to-end smoke test")

    # Auth Google (legacy command — kept for backward compatibility)
    subparsers.add_parser("auth-google", help="Authenticate with Google APIs (OAuth2)")

    # Auth (new unified subcommand)
    auth_parser = subparsers.add_parser("auth", help="Authenticate with external services")
    auth_sub = auth_parser.add_subparsers(dest="auth_command")
    auth_sub.add_parser("microsoft", help="Interactive Microsoft OAuth2 sign-in (opens browser)")
    auth_google_parser = auth_sub.add_parser("google", help="Interactive Google OAuth2 sign-in (opens browser)")
    auth_google_parser.add_argument("--fresh", action="store_true",
                                    help="Bypass any cached token (use when switching Google accounts)")

    # OneDrive
    onedrive_parser = subparsers.add_parser("onedrive", help="OneDrive / SharePoint utilities")
    onedrive_sub = onedrive_parser.add_subparsers(dest="onedrive_command")
    ls_parser = onedrive_sub.add_parser("ls", help="List files under the SharePoint archive root")
    ls_parser.add_argument("path", nargs="?", default="", help="Sub-path relative to archive root (default: root)")
    onedrive_sub.add_parser(
        "backup-status",
        help="Show local πρωτόκολλο safety backup info (path, size, last refresh, validity)",
    )
    backup_restore_parser = onedrive_sub.add_parser(
        "backup-restore",
        help="Copy the local πρωτόκολλο backup to a destination (recovery from accidental deletion)",
    )
    backup_restore_parser.add_argument("dest", help="Destination path (file or directory)")
    backup_restore_parser.add_argument("--force", action="store_true",
                                       help="Overwrite destination if it already exists")

    # Board meeting invitation
    invite_parser = subparsers.add_parser("invite", help="Run board meeting invitation workflow")
    invite_parser.add_argument("--sheet-id", help="Google Sheets ID for agenda data")
    invite_parser.add_argument("--meeting-ref", dest="meeting_ref",
                               help="Meeting reference (e.g. ΔΣ05-2026). "
                                    "Live mode: bypasses D5 read on the agenda Sheet — useful when "
                                    "running ahead of D5 being seeded for the next cycle, OR for "
                                    "sandbox testing (e.g. --meeting-ref ΔΣ99-2099 --test) so the "
                                    "test workflow never collides with a live cycle's Discord "
                                    "threads / Zoom meeting / pending reminders. "
                                    "Required when --manual is set.")
    invite_parser.add_argument("--date", help="Meeting date (YYYY-MM-DD)")
    invite_parser.add_argument("--time", help="Meeting time (HH:MM)")
    invite_parser.add_argument("--manual", action="store_true",
                               help="Manual mode: skip Google Sheets, enter data via CLI")
    invite_parser.add_argument("--protocol", help="Manual protocol number (e.g. 2026_017), overrides Drive lookup")
    invite_parser.add_argument("--poll-url", help="Scheduling poll URL (When2Meet, Doodle, etc.) embedded in board scheduling email")
    invite_parser.add_argument("--response-deadline", help="Deadline (YYYY-MM-DD) for board responses to scheduling email; defaults to today + 4 days")
    invite_parser.add_argument("--brevo-template", help="Brevo template ID for newsletter")
    invite_parser.add_argument("--brevo-lists", help="Comma-separated Brevo list IDs")
    invite_parser.add_argument("--actor", default="secgen", help="Actor identity for audit log")
    invite_parser.add_argument("--test", action="store_true",
                               help="Test mode: creates Zoom+PDF, emails to test_email, then rollback")
    invite_parser.add_argument("--cancel", action="store_true",
                               help="Cancel an in-progress or completed invitation workflow (rolls back all side effects)")
    invite_parser.add_argument("--rollback", action="store_true",
                               help=argparse.SUPPRESS)  # legacy alias for --cancel
    invite_parser.add_argument("--workflow-id", help="Specific workflow ID to cancel or share-poll on (default: most recent)")

    # invite subcommands
    invite_sub = invite_parser.add_subparsers(dest="invite_command")
    share_poll_parser = invite_sub.add_parser("share-poll", help="Reply to the board scheduling email with a poll URL")
    share_poll_parser.add_argument("--url", required=True, help="Poll URL (When2Meet, Doodle, etc.)")
    share_poll_parser.add_argument("--workflow-id", help="Workflow ID (default: most recent invitation workflow)")

    reset_sheet_parser = invite_sub.add_parser(
        "reset-sheet",
        help="Manually reset the agenda Google Sheet for the next cycle "
             "(normally automatic after minutes finalize)",
    )
    reset_sheet_parser.add_argument("--workflow-id", help=argparse.SUPPRESS)

    # Upload Brevo template
    tmpl_parser = subparsers.add_parser("upload-template", help="Upload HTML file to a Brevo template")
    tmpl_parser.add_argument("--file", required=True, help="Path to the HTML file")
    tmpl_parser.add_argument("--template-id", type=int, required=True, help="Brevo template ID to overwrite")
    tmpl_parser.add_argument("--subject", help="Optional default subject stored on the template")
    tmpl_parser.add_argument("--name", help="Optional template display name in Brevo dashboard")

    # Board meeting minutes
    minutes_parser = subparsers.add_parser("minutes", help="Board meeting minutes workflow")
    minutes_sub = minutes_parser.add_subparsers(dest="minutes_command")

    # minutes run (default)
    run_parser = minutes_sub.add_parser("run", help="Draft minutes from sources")
    run_parser.add_argument("--meeting", required=True, help="Meeting ref (e.g., ΔΣ03-2026)")
    run_parser.add_argument("--transcript", help="Path to local transcript file (.vtt, .txt, .docx)")
    run_parser.add_argument("--manual", action="store_true", help="Skip Zoom transcript")
    run_parser.add_argument("--test", action="store_true", help="Test mode")
    run_parser.add_argument("--actor", default="secgen", help="Actor identity for audit log")

    # minutes build (orchestrator: recording/transcript -> skeleton -> optional draft)
    build_parser = minutes_sub.add_parser(
        "build", help="Assemble a minutes skeleton from a manifest or transcript file"
    )
    build_parser.add_argument("meeting_ref", help="Meeting ref (e.g., ΔΣ05-2026)")
    build_source = build_parser.add_mutually_exclusive_group(required=True)
    build_source.add_argument("--manifest", help="Path to a Zoom recording manifest.json")
    build_source.add_argument(
        "--transcript-file", dest="transcript_file",
        help="Path to a transcript text file (.vtt or Zoom-copy plain text)",
    )
    build_source.add_argument(
        "--reuse-transcript", dest="reuse_transcript", action="store_true",
        help="Reuse the cached transcript.json from a prior --manifest build (no re-transcription)",
    )
    build_parser.add_argument(
        "--meeting-start", dest="meeting_start", default=None,
        help="ISO-8601 meeting start (origin for transcript-file offsets)",
    )
    build_parser.add_argument(
        "--draft", action="store_true",
        help="Also produce an LLM πρακτικά draft (degrades gracefully if unavailable)",
    )

    # minutes finalize
    finalize_parser = minutes_sub.add_parser("finalize", help="Finalize and archive minutes")
    finalize_parser.add_argument("--meeting", required=True, help="Meeting ref (e.g., ΔΣ03-2026)")
    finalize_parser.add_argument("--test", action="store_true",
                                 help="Test mode: generates PDF but skips archive, Πρωτόκολλο, and Βιβλίο Αποφάσεων writes")

    # minutes list-drafts
    minutes_sub.add_parser("list-drafts", help="List draft minutes in Drive")

    # minutes fetch-recording (on-demand pull of Zoom recording assets)
    fetch_rec_parser = minutes_sub.add_parser(
        "fetch-recording", help="Fetch a Zoom meeting's recording assets on demand"
    )
    fetch_rec_parser.add_argument("meeting_uuid", help="Zoom meeting UUID or ID")
    fetch_rec_parser.add_argument(
        "--dest", default=None,
        help="Destination directory (default: data/recordings/{uuid}/)",
    )
    fetch_rec_parser.add_argument(
        "--participants", action="store_true",
        help="Also fetch + print the attendance list",
    )
    fetch_rec_parser.add_argument(
        "--include-video", action="store_true",
        help="Also download video assets (off by default; audio/timeline/chat only)",
    )

    # minutes events (capture backbone for the minutes pipeline)
    events_parser = minutes_sub.add_parser("events", help="Record/list captured meeting events")
    events_sub = events_parser.add_subparsers(dest="events_command")

    events_record_parser = events_sub.add_parser("record", help="Record a meeting event")
    events_record_parser.add_argument("--meeting-ref", required=True, help="Meeting ref (e.g., ΔΣ05-2026)")
    events_record_parser.add_argument(
        "--type", required=True,
        help="Event type: agenda_advance | vote | phase | presence | off_topic | note",
    )
    events_record_parser.add_argument("--payload", required=True, help="Event payload as a JSON object")
    events_record_parser.add_argument(
        "--confidence", default="confirmed", choices=["confirmed", "low"],
        help="Capture confidence (default: confirmed)",
    )

    events_list_parser = events_sub.add_parser("list", help="List captured meeting events")
    events_list_parser.add_argument("--meeting-ref", required=True, help="Meeting ref (e.g., ΔΣ05-2026)")
    events_list_parser.add_argument("--type", help="Optional event-type filter")

    # minutes propose-decision (draft one canonical Greek board decision)
    propose_parser = minutes_sub.add_parser(
        "propose-decision",
        help="Draft a canonical Greek board decision from a discussion snippet",
    )
    propose_parser.add_argument("--meeting-ref", required=True, help="Meeting ref (e.g., ΔΣ05-2026)")
    propose_parser.add_argument("--snippet", help="Decision discussion text")
    propose_parser.add_argument("--snippet-file", help="Read the discussion text from this file")
    propose_parser.add_argument(
        "--sequence", type=int, default=None,
        help="1-based decision number within the meeting (default: count of prior vote events + 1)",
    )
    propose_parser.add_argument("--agenda-item", default="", help="Optional agenda item title/context")

    # Archive (Phase 1 + 2): file a PDF into SharePoint + πρωτόκολλο
    archive_parser = subparsers.add_parser("archive", help="Archive a PDF to SharePoint + πρωτόκολλο")
    archive_sub = archive_parser.add_subparsers(dest="archive_command")

    # archive submit <path>
    archive_submit_parser = archive_sub.add_parser(
        "submit",
        help="Submit a PDF to the archive",
    )
    archive_submit_parser.add_argument("pdf_path", metavar="path", help="Path to the PDF to archive")
    archive_submit_parser.add_argument("--title", help="Override the LLM-picked title")
    archive_submit_parser.add_argument("--labels", help="Comma-separated tag list override")
    archive_submit_parser.add_argument("--proto", help="Manual protocol number (YYYY_NNN)")
    archive_submit_parser.add_argument("--sender", help="Sender email (default: secgen@amnesty.org.gr)")
    archive_submit_parser.add_argument("--actor", default="secgen", help="Actor identity for audit log")
    archive_submit_parser.add_argument("--test", action="store_true",
                                       help="Test mode: skip SharePoint upload + xlsx write")

    # archive review <workflow_id> "<text>"
    archive_review_parser = archive_sub.add_parser(
        "review",
        help="Parse free-text feedback into an amendment/cancel/acknowledge",
    )
    archive_review_parser.add_argument("workflow_id", help="Workflow ID to review")
    archive_review_parser.add_argument("text", help="Free-text feedback (in Greek or English)")
    archive_review_parser.add_argument("--actor", default="secgen", help="Actor identity for audit log")

    # archive cancel <workflow_id>
    archive_cancel_parser = archive_sub.add_parser(
        "cancel",
        help="Cancel an archive workflow and roll back side effects",
    )
    archive_cancel_parser.add_argument("workflow_id", help="Workflow ID to cancel")

    # archive resolve <workflow_id> <decision>  — SecGen reservation confirmation
    archive_resolve_parser = archive_sub.add_parser(
        "resolve",
        help="SecGen-only: confirm/reject filling a pre-reserved πρωτόκολλο slot",
    )
    archive_resolve_parser.add_argument("workflow_id", help="Workflow ID awaiting confirmation")
    archive_resolve_parser.add_argument(
        "decision",
        choices=["approve", "reject"],
        help="approve = yes, this is the file for the reservation; reject = no, abort + roll back",
    )
    archive_resolve_parser.add_argument("--actor", default="secgen",
                                        help="Actor identity for audit log (default: secgen)")

    # archive list
    archive_sub.add_parser("list", help="List in-progress + recent archive workflows")

    # M365 inbox watcher (Phase 3) — Graph webhook subscription lifecycle + safety poll
    m365_parser = subparsers.add_parser(
        "m365",
        help="M365/Graph webhook subscription + inbox safety poll",
    )
    m365_sub = m365_parser.add_subparsers(dest="m365_command")

    m365_sub_subscribe = m365_sub.add_parser("subscribe", help="Create a Graph webhook subscription")
    m365_sub_subscribe.add_argument("--url", help="Override webhook URL (default: m365_inbox.webhook_url)")

    m365_sub_unsubscribe = m365_sub.add_parser("unsubscribe", help="Delete a Graph webhook subscription")
    m365_sub_unsubscribe.add_argument("subscription_id", help="Graph subscription id")

    m365_sub.add_parser("subscriptions", help="List active subscriptions (local + Graph)")

    m365_sub_renew = m365_sub.add_parser("renew-now", help="Renew expiring subscriptions immediately")
    m365_sub_renew.add_argument("--threshold-hours", type=int,
                                help="Renew if remaining lifetime is below this many hours")

    m365_sub.add_parser("poll-now", help="Run the inbox safety poll once, now")

    # RSS feed management (replaces MonitoRSS)
    rss_parser = subparsers.add_parser("rss", help="RSS feed management")
    rss_sub = rss_parser.add_subparsers(dest="rss_command")
    rss_sub.add_parser("list", help="List configured feeds + routes")
    add_feed = rss_sub.add_parser("add-feed", help="Register a new RSS feed source")
    add_feed.add_argument("url", help="Feed URL (e.g. https://www.amnesty.gr/rss.xml)")
    add_feed.add_argument("--label", default="", help="Human-readable label")
    rm_feed = rss_sub.add_parser("remove-feed", help="Delete a feed and ALL its routes")
    rm_feed.add_argument("url", help="Feed URL to delete")
    add_route = rss_sub.add_parser("add-route", help="Add a routing rule for a feed")
    add_route.add_argument("feed_url", help="Feed this rule applies to")
    add_route.add_argument("channel_id", help="Discord channel ID (snowflake)")
    add_route.add_argument("--url-pattern", default="", dest="url_pattern",
                           help="Substring in item.link to match (e.g. /news/events/)")
    add_route.add_argument("--title-pattern", default="", dest="title_pattern",
                           help="Regex against item.title (case-insensitive)")
    add_route.add_argument("--forum-tag-id", default="", dest="forum_tag_id",
                           help="Snowflake of forum tag to apply")
    add_route.add_argument("--forum-tag-name", default="", dest="forum_tag_name",
                           help="Forum tag name (resolved against channel.available_tags)")
    add_route.add_argument("--label", default="", help="Human-readable label")
    rm_route = rss_sub.add_parser("remove-route", help="Delete a route by id")
    rm_route.add_argument("route_id", type=int, help="Route id (from `rss list`)")
    rss_sub.add_parser("poll-now",
                       help="Trigger a poll cycle immediately (note: posts via running bot)")
    rss_sub.add_parser("seed-amnesty",
                       help="Seed amnesty.gr feed + 4 standard routes (events/articles/press/ektheseis)")

    # Debug — run a single workflow step in isolation (test_mode)
    debug_parser = subparsers.add_parser(
        "debug",
        help="Run a single workflow step in isolation against a fake context (test_mode)",
    )
    debug_sub = debug_parser.add_subparsers(dest="debug_command")

    debug_list_parser = debug_sub.add_parser(
        "list", help="List workflows, or the steps of one workflow",
    )
    debug_list_parser.add_argument(
        "workflow", nargs="?", help="Workflow name (omit to list all)",
    )

    debug_fixture_parser = debug_sub.add_parser(
        "fixture", help="Print a workflow's canonical fake debug context",
    )
    debug_fixture_parser.add_argument("workflow", help="Workflow name")
    debug_fixture_parser.add_argument(
        "--json", action="store_true", help="Emit JSON instead of pretty-print",
    )

    debug_run_parser = debug_sub.add_parser(
        "run", help="Execute one step (or a comma-separated chain) in test_mode",
    )
    debug_run_parser.add_argument("workflow", help="Workflow name")
    debug_run_parser.add_argument(
        "steps", help="Step name, or comma-separated chain (e.g. read_agenda,schedule_zoom)",
    )
    debug_run_parser.add_argument(
        "--set", action="append", default=[], metavar="KEY=VALUE",
        help="Override a ctx key (value parsed as JSON, else raw string). Repeatable.",
    )
    debug_run_parser.add_argument(
        "--from-state", dest="from_state", metavar="ID",
        help="Overlay the persisted context of this workflow_state id onto the fixture",
    )
    debug_run_parser.add_argument(
        "--show-ctx", dest="show_ctx", action="store_true",
        help="Also print the ctx keys each step produced",
    )
    debug_run_parser.add_argument(
        "--json", action="store_true", help="Emit a machine-readable JSON summary",
    )

    # Εγκύκλιοι
    egkyklios_parser = subparsers.add_parser(
        "egkyklios",
        help="Γενικές / Ειδικές εγκύκλιοι ενημέρωσης",
    )
    egkyklios_sub = egkyklios_parser.add_subparsers(dest="egkyklios_kind")
    eg_general = egkyklios_sub.add_parser(
        "general", help="Γενική Εγκύκλιος Ενημέρωσης",
    )
    eg_general.add_argument("--period-start", dest="period_start", help="ISO date (YYYY-MM-DD)")
    eg_general.add_argument("--period-end", dest="period_end", help="ISO date (YYYY-MM-DD)")
    eg_general.add_argument("--test", action="store_true", help="Test mode (last week, redirects)")
    eg_general.add_argument("--actor", help="Override the workflow actor (default: secgen)")
    eg_general_sub = eg_general.add_subparsers(dest="egkyklios_command")
    eg_general_approve = eg_general_sub.add_parser(
        "approve", help="SecGen approval: advance a parked draft past its approval gate",
    )
    eg_general_approve.add_argument("draft_id", help="Draft id from egkyklios_drafts (see `list`)")
    eg_general_approve.add_argument("--actor", help="Override the workflow actor")
    eg_general_list = eg_general_sub.add_parser(
        "list", help="List recent Γενικές Εγκύκλιοι drafts",
    )
    eg_general_list.add_argument(
        "-n", "--limit", type=int, default=10, help="Max rows to show (default 10)",
    )

    # Discord bot
    discord_parser = subparsers.add_parser("discord", help="Discord bot commands")
    discord_sub = discord_parser.add_subparsers(dest="discord_command")
    discord_sub.add_parser("run", help="Start the Amnesty Discord bot (blocks until stopped)")
    clear_stale_parser = discord_sub.add_parser(
        "clear-stale-commands",
        help=(
            "Wipe stale slash commands from Discord's registry "
            "(use when ghost commands still appear in the client UI)"
        ),
    )
    clear_stale_parser.add_argument(
        "--global-only",
        action="store_true",
        help="Only clear global commands (skip guild-scoped registry)",
    )
    clear_stale_parser.add_argument(
        "--guild-only",
        action="store_true",
        help="Only clear guild-scoped commands (skip global registry)",
    )

    # Logs
    logs_parser = subparsers.add_parser(
        "logs",
        help="Inspect the rotating log files written under data/logs/",
    )
    logs_sub = logs_parser.add_subparsers(dest="logs_command")
    logs_sub.add_parser("path", help="Print the log file paths + sizes")
    logs_tail = logs_sub.add_parser("tail", help="Show the last N lines of the main log")
    logs_tail.add_argument("-n", "--lines", type=int, default=50, help="Number of lines (default 50)")
    logs_tail.add_argument("-p", "--pattern", help="Optional regex; only lines matching are kept")
    logs_errors = logs_sub.add_parser("errors", help="Show the last N lines of the errors-only log")
    logs_errors.add_argument("-n", "--lines", type=int, default=50, help="Number of lines (default 50)")
    logs_errors.add_argument("-p", "--pattern", help="Optional regex filter")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    commands = {
        "status": cmd_status,
        "audit": cmd_audit,
        "test-claude": cmd_test_claude,
        "smoke-test": cmd_smoke_test,
        "invite": cmd_invite,
        "auth-google": cmd_auth_google,
        "auth": cmd_auth,
        "onedrive": cmd_onedrive,
        "upload-template": cmd_upload_template,
        "minutes": cmd_minutes,
        "discord": cmd_discord,
        "archive": cmd_archive,
        "m365": cmd_m365,
        "rss": cmd_rss,
        "logs": cmd_logs,
        "egkyklios": cmd_egkyklios,
        "debug": cmd_debug,
    }

    cmd_func = commands.get(args.command)
    if cmd_func:
        cmd_func(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
