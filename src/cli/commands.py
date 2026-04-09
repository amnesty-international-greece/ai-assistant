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
    """Configure logging based on settings."""
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s | %(name)s | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


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
    """Run the board meeting invitation workflow."""
    init_db()
    asyncio.run(_run_invite(args))


async def _run_invite(args: argparse.Namespace) -> None:
    """Async handler for the invitation workflow."""
    from src.workflows.board_meeting_invitation import BoardMeetingInvitationWorkflow

    # ── Resolve run mode ──────────────────────────────────────────────────────
    # --test : full simulation — Zoom created+rolled back, PDF generated,
    #          emails redirected to testing.dry_run_email, DEBUG logging.
    # (no flag): live run — everything executes for real.
    test_mode = getattr(args, "test", False)

    if test_mode:
        logging.getLogger().setLevel(logging.DEBUG)
        for h in logging.getLogger().handlers:
            h.setLevel(logging.DEBUG)

        test_email = settings.testing.dry_run_email
        _print_header("Board Meeting Invitation Workflow  [TEST MODE]")
        print("  TEST MODE — what will happen:")
        print("  • Reads agenda from Google Sheets (real)")
        print("  • Creates Zoom meeting (real, rolled back at the end)")
        print("  • Generates invitation PDF (real, opened for review)")
        print("  • Newsletter test send →", test_email or "(skipped — set testing.dry_run_email in config.yaml)")
        print("  • Archive: skipped")
        print("  • Reminders: handled by Zoom natively")
        print("  • Logging: DEBUG")
        print()
    else:
        _print_header("Board Meeting Invitation Workflow")

    # Build initial context from CLI args
    initial_data: dict = {
        "dry_run":   test_mode,   # test_mode implies dry_run behaviour internally
        "test_mode": test_mode,
    }

    if args.sheet_id:
        initial_data["agenda_sheet_id"] = args.sheet_id

    if args.meeting_number:
        initial_data["meeting_number"] = args.meeting_number

    if args.date:
        initial_data["meeting_date"] = args.date

    if args.time:
        initial_data["meeting_time"] = args.time

    if args.brevo_template:
        initial_data["brevo_template_id"] = int(args.brevo_template)

    if args.brevo_lists:
        initial_data["brevo_list_ids"] = [int(x) for x in args.brevo_lists.split(",")]

    # If manual mode: skip Google Sheets, use provided args directly
    if args.manual:
        if not all([args.meeting_number, args.date, args.time]):
            print("ERROR: --manual mode requires --meeting-number, --date, and --time")
            sys.exit(1)

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
        print()
        _print_header("APPROVAL REQUIRED")

        ctx = wf.context

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

        # Open PDF automatically so the user can review the actual document
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

        if _confirm("  Approve this draft and proceed? [y/n]: "):
            log_action(
                workflow="board_meeting_invitation",
                action="approval_given",
                actor="secgen",
                details={"workflow_id": wf.workflow_id},
            )
            result = await wf.approve_and_resume()
        else:
            log_action(
                workflow="board_meeting_invitation",
                action="approval_rejected",
                actor="secgen",
                details={"workflow_id": wf.workflow_id},
            )
            print("\n  Cancelling workflow and cleaning up...")
            await wf.rollback(wf.context)
            print("  Zoom meeting cancelled. PDF deleted. Done.")
            return

    # Test mode cleanup: always roll back (whether completed or failed)
    if test_mode and result.get("status") in ("completed", "failed"):
        print("\n  [TEST MODE] Cleaning up: cancelling Zoom meeting and deleting PDF...")
        await wf.rollback(wf.context)
        print("  Cleanup done.")

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
        print(f"  Archived:      {'Yes' if ctx.get('archive_file_id') else 'No'}")
        print(f"  Newsletter:    {'Sent' if ctx.get('newsletter_sent') else 'Skipped'}")
        print(f"  Reminder:      {ctx.get('reminder_at', 'N/A')}")
    elif result.get("status") == "failed":
        _print_header("WORKFLOW FAILED")
        print(f"  Failed at step: {result.get('step', 'unknown')}")
        print(f"  Error: {result.get('error', 'unknown')}")
    print()


# --- Minutes Commands ---


def cmd_minutes(args: argparse.Namespace) -> None:
    """Dispatch board meeting minutes subcommands."""
    minutes_command = getattr(args, "minutes_command", None)
    if minutes_command == "finalize":
        cmd_minutes_finalize(args)
    elif minutes_command == "list-drafts":
        cmd_minutes_list_drafts(args)
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
        "dry_run": test_mode,
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
        "dry_run": test_mode,
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


# --- Existing Commands ---


def cmd_status(args: argparse.Namespace) -> None:
    """Show platform status."""
    init_db()
    print(f"AI-in-AI Platform v{settings.app.version}")
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
    """Run the Google OAuth2 flow and save credentials token."""
    from src.integrations.google_drive import GoogleClient

    print("Starting Google OAuth2 authentication...")
    print("A browser window will open — log in and click Allow.")
    print()
    try:
        client = GoogleClient()
        client.authenticate()
        print("✓ Authentication successful! Token saved to data/google_token.json")
    except Exception as e:
        print(f"✗ Authentication failed: {e}")
        sys.exit(1)


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


# --- Entry Point ---


def main() -> None:
    """Main CLI entry point."""
    setup_logging()

    parser = argparse.ArgumentParser(
        prog="ai-in-ai",
        description="AI-in-AI Platform -- Amnesty International Greece",
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

    # Auth Google
    subparsers.add_parser("auth-google", help="Authenticate with Google APIs (OAuth2)")

    # Board meeting invitation
    invite_parser = subparsers.add_parser("invite", help="Run board meeting invitation workflow")
    invite_parser.add_argument("--sheet-id", help="Google Sheets ID for agenda data")
    invite_parser.add_argument("--meeting-number", help="Meeting number (e.g., 42)")
    invite_parser.add_argument("--date", help="Meeting date (YYYY-MM-DD)")
    invite_parser.add_argument("--time", help="Meeting time (HH:MM)")
    invite_parser.add_argument("--manual", action="store_true",
                               help="Manual mode: skip Google Sheets, enter data via CLI")
    invite_parser.add_argument("--brevo-template", help="Brevo template ID for newsletter")
    invite_parser.add_argument("--brevo-lists", help="Comma-separated Brevo list IDs")
    invite_parser.add_argument("--actor", default="secgen", help="Actor identity for audit log")
    invite_parser.add_argument("--test", action="store_true",
                               help="Test mode: creates Zoom+PDF, emails to dry_run_email, then rollback")

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

    # minutes finalize
    finalize_parser = minutes_sub.add_parser("finalize", help="Finalize and archive minutes")
    finalize_parser.add_argument("--meeting", required=True, help="Meeting ref (e.g., ΔΣ03-2026)")
    finalize_parser.add_argument("--test", action="store_true",
                                 help="Test mode: generates PDF but skips archive, Πρωτόκολλο, and Βιβλίο Αποφάσεων writes")

    # minutes list-drafts
    minutes_sub.add_parser("list-drafts", help="List draft minutes in Drive")

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
        "upload-template": cmd_upload_template,
        "minutes": cmd_minutes,
    }

    cmd_func = commands.get(args.command)
    if cmd_func:
        cmd_func(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
