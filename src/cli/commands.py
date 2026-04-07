"""CLI commands for manual workflow triggers and platform management."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
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

    _print_header("Board Meeting Invitation Workflow")

    # Build initial context from CLI args
    initial_data: dict = {}

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

        # Display the draft for review
        ctx = wf.context
        invitation = ctx.get("invitation_content", {})

        print(f"  Title:    {invitation.get('title', 'N/A')}")
        print(f"  Subtitle: {invitation.get('subtitle', 'N/A')}")
        print()
        for section in invitation.get("sections", []):
            if section.get("heading"):
                print(f"  ## {section['heading']}")
            if section.get("body"):
                # Word-wrap body text
                body = section["body"]
                for line in body.split("\n"):
                    print(f"     {line}")
            print()
        if invitation.get("footer"):
            print(f"  ---")
            print(f"  {invitation['footer']}")
        print()

        pdf_path = ctx.get("pdf_path", "")
        if pdf_path:
            print(f"  PDF saved at: {pdf_path}")
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
            print("\n  Workflow cancelled by user.")
            return

    # Final status
    print()
    if result.get("status") == "completed":
        _print_header("WORKFLOW COMPLETED")
        ctx = wf.context
        print(f"  Meeting #:     {ctx.get('meeting_number', 'N/A')}")
        print(f"  Date:          {ctx.get('meeting_date', 'N/A')}")
        print(f"  Zoom:          {ctx.get('zoom_join_url', 'N/A')}")
        print(f"  PDF:           {ctx.get('pdf_path', 'N/A')}")
        print(f"  Archived:      {'Yes' if ctx.get('archive_file_id') else 'No'}")
        print(f"  Newsletter:    {'Sent' if ctx.get('newsletter_sent') else 'Skipped'}")
        print(f"  Board email:   {'Sent' if ctx.get('board_email_sent') else 'N/A'}")
        print(f"  Reminder:      {ctx.get('reminder_at', 'N/A')}")
    elif result.get("status") == "failed":
        _print_header("WORKFLOW FAILED")
        print(f"  Failed at step: {result.get('step', 'unknown')}")
        print(f"  Error: {result.get('error', 'unknown')}")
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
    }

    cmd_func = commands.get(args.command)
    if cmd_func:
        cmd_func(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
