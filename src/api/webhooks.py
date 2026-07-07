"""Webhook endpoints - receive external triggers and launch workflows."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
from datetime import datetime
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from src.config import settings
from src.core.audit import (
    get_graph_subscription,
    log_action,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


class InviteWebhookPayload(BaseModel):
    """Payload sent by Google Apps Script when the three approval checkboxes
    (D16/D17/D18) all become TRUE for the upcoming meeting.

    All fields are optional.  The Apps Script auto-trigger sends just
    ``raw_meeting_id`` + ``start_at_step`` - the workflow's ``read_agenda``
    step pulls everything else from the sheet itself.  Legacy / manual
    callers may still send the full set; unknown fields are ignored.
    """
    raw_meeting_id: str = ""     # verbatim D5, e.g. "ΔΣ04-2026"
    start_at_step: str = ""      # "" → run full workflow; else jump to this step
    test_mode: bool = False
    # ── legacy fields (manual webhook calls only - auto-trigger doesn't send) ──
    meeting_number: str = ""
    meeting_date: str = ""       # YYYY-MM-DD
    meeting_time: str = ""       # HH:MM
    meeting_type: str = ""       # ΤΑΚΤΙΚΗ | ΕΚΤΑΚΤΗ
    location: str = ""           # ΔΙΑΔΙΚΤΥΑΚΑ | physical address
    agenda_items: list[str] = []
    trigger_row: int = 16        # first approval-checkbox row (informational)


def _parse_meeting_number(raw: str) -> str:
    """Extract numeric part from meeting ID like 'ΔΣ04-2026' → '4'."""
    match = re.search(r"\d+", raw)
    return match.group(0).lstrip("0") or "1" if match else raw


def _find_in_progress_invite(raw_meeting_id: str) -> str | None:
    """Return the workflow_id of an in-progress invitation for the given
    raw_meeting_id, or None if no such workflow exists.

    "In progress" means the workflow_state row exists with a state that is
    NOT one of the terminal states (completed / failed / cancelled).
    """
    if not raw_meeting_id:
        return None
    try:
        from src.core.audit import _get_connection
        conn = _get_connection()
        rows = conn.execute(
            "SELECT workflow_id, data FROM workflow_state "
            "WHERE workflow_name = 'board_meeting_invitation' "
            "AND state NOT IN ('completed', 'failed', 'cancelled') "
            "ORDER BY updated_at DESC"
        ).fetchall()
        for row in rows:
            try:
                data = json.loads(row["data"] or "{}")
            except (json.JSONDecodeError, TypeError):
                continue
            ctx = (data.get("context") or {})
            if (ctx.get("raw_meeting_id") or "").strip() == raw_meeting_id.strip():
                return row["workflow_id"]
    except Exception as e:
        logger.warning("Could not query workflow_state for idempotency check: %s", e)
    return None


def _find_scheduling_context(raw_meeting_id: str) -> dict | None:
    """Return the saved context of this meeting's scheduling-email workflow.

    The scheduling email (step 1) stores ``email_thread_anchor`` + ``poll_url``
    on its own workflow, which then pauses at ``await_approval``.  A webhook run
    that jumps to ``read_agenda`` starts a *fresh* workflow, so it must inherit
    that anchor - otherwise ``send_board_email`` has no thread to reply to and
    the final board invitation is silently skipped.

    Matches on ``raw_meeting_id`` or the legacy ``meeting_ref`` key (the
    scheduling workflow predates ``raw_meeting_id``), and requires an anchor to
    be present.  Returns the most recently updated match, or None.
    """
    if not raw_meeting_id:
        return None
    try:
        from src.core.audit import _get_connection
        conn = _get_connection()
        rows = conn.execute(
            "SELECT data FROM workflow_state "
            "WHERE workflow_name = 'board_meeting_invitation' "
            "ORDER BY updated_at DESC"
        ).fetchall()
        target = raw_meeting_id.strip()
        for row in rows:
            try:
                data = json.loads(row["data"] or "{}")
            except (json.JSONDecodeError, TypeError):
                continue
            ctx = data.get("context") or {}
            ref = (ctx.get("raw_meeting_id") or ctx.get("meeting_ref") or "").strip()
            if ref == target and ctx.get("email_thread_anchor"):
                return ctx
    except Exception as e:
        logger.warning("Could not look up scheduling anchor for %s: %s", raw_meeting_id, e)
    return None


async def _auto_resume_gates(wf, result: dict) -> dict:
    """Drive a webhook workflow through its approval gates unattended.

    The engine halts on every ``requires_approval`` step; the interactive CLI
    resumes each one with a human prompt.  A webhook run has no human in the
    loop - the board already approved by ticking D16/D17/D18 - so each gate is
    auto-approved here.

    This is safe: the real side effects (live newsletter send, Discord
    choreography) are governed by ``test_mode`` and the Brevo list config
    *inside* the steps, not by these gates.  Approving a gate never itself
    triggers a send - in test mode the newsletter is flagged ``skipped`` before
    the gate, and in live mode it has already gone out in ``send_newsletter``.
    """
    guard = 0
    while result.get("status") == "awaiting_approval":
        guard += 1
        if guard > 5:
            logger.error(
                "Auto-resume exceeded %d gates for workflow %s - aborting to avoid a loop",
                guard, wf.workflow_id,
            )
            break
        gate = result.get("step", "")
        logger.info("Webhook auto-approving gate %r (workflow %s)", gate, wf.workflow_id)
        log_action(
            workflow="board_meeting_invitation",
            action="approval_given",
            actor="sheets_trigger",
            target=gate,
            details={"workflow_id": wf.workflow_id, "gate": gate, "auto": True},
        )
        result = await wf.approve_and_resume()
    return result


async def _run_invite_workflow(payload: InviteWebhookPayload) -> None:
    """Run the invitation workflow asynchronously."""
    from src.workflows.board_meeting_invitation import BoardMeetingInvitationWorkflow

    meeting_number = _parse_meeting_number(payload.meeting_number)
    log_action(
        workflow="board_meeting_invitation",
        action="webhook_triggered",
        actor="sheets_trigger",
        details={
            "meeting_number": meeting_number,
            "meeting_date": payload.meeting_date,
            "meeting_time": payload.meeting_time,
            "meeting_type": payload.meeting_type,
            "location": payload.location,
            "raw_meeting_id": payload.raw_meeting_id,
            "start_at_step": payload.start_at_step,
            "trigger_row": payload.trigger_row,
            "test_mode": payload.test_mode,
        },
    )

    try:
        wf = BoardMeetingInvitationWorkflow(actor="sheets_trigger")
        initial_data: dict[str, Any] = {
            "meeting_number": meeting_number,
            "meeting_date": payload.meeting_date,
            "meeting_time": payload.meeting_time,
            "meeting_type": payload.meeting_type,
            "location": payload.location,
            "test_mode": payload.test_mode,
        }
        if payload.raw_meeting_id:
            initial_data["raw_meeting_id"] = payload.raw_meeting_id
        if payload.start_at_step:
            initial_data["_start_at_step"] = payload.start_at_step
        if payload.agenda_items:
            initial_data["agenda_items"] = payload.agenda_items
            initial_data["_skip_read_agenda"] = True

        # Inherit the scheduling email's thread anchor (+ poll) so the final
        # board invitation lands as a reply in the existing thread instead of
        # being skipped for lack of an anchor.
        sched_ctx = _find_scheduling_context(payload.raw_meeting_id)
        if sched_ctx:
            initial_data.setdefault("email_thread_anchor", sched_ctx.get("email_thread_anchor", ""))
            if sched_ctx.get("poll_url"):
                initial_data.setdefault("poll_url", sched_ctx["poll_url"])
            logger.info(
                "Webhook: inherited scheduling thread anchor for %s",
                payload.raw_meeting_id,
            )
        else:
            logger.warning(
                "Webhook: no prior scheduling anchor for %s - final board email will be skipped",
                payload.raw_meeting_id or "(none)",
            )

        result = await wf.run(initial_data)
        # No human in the loop - auto-approve the live gates the board already
        # authorised via the sheet checkboxes (PDF review, newsletter confirm).
        result = await _auto_resume_gates(wf, result)
        logger.info("Webhook workflow finished: %s", result.get("status"))

    except Exception as e:
        logger.error("Webhook workflow failed: %s", e)


@router.post("/invite", status_code=202)
async def webhook_invite(
    payload: InviteWebhookPayload,
    background_tasks: BackgroundTasks,
) -> dict:
    """Triggered by Google Apps Script when D16/D17/D18 all become TRUE.

    Launches the board meeting invitation workflow asynchronously.

    Idempotency: if an invitation workflow with the same ``raw_meeting_id``
    is already in progress, returns ``already_in_progress`` without starting
    a duplicate run.
    """
    logger.info(
        "Invite webhook received - meeting %s on %s at %s (raw=%s)",
        payload.meeting_number,
        payload.meeting_date,
        payload.meeting_time,
        payload.raw_meeting_id or "(none)",
    )

    existing_id = _find_in_progress_invite(payload.raw_meeting_id)
    if existing_id:
        logger.info(
            "Duplicate webhook for %s - workflow %s already in progress; skipping",
            payload.raw_meeting_id, existing_id,
        )
        return {
            "status": "already_in_progress",
            "workflow_id": existing_id,
            "message": f"Invitation workflow for {payload.raw_meeting_id} already running",
        }

    background_tasks.add_task(_run_invite_workflow, payload)
    return {
        "status": "accepted",
        "message": f"Workflow started for meeting {payload.meeting_number}",
    }


@router.get("/health")
async def webhook_health() -> dict:
    return {"status": "ok", "timestamp": datetime.utcnow().isoformat()}


# ─────────────────────────────────────────────────────────────────────────────
# Microsoft Graph webhook - members@amnesty.org.gr inbox  (Phase 3)
# ─────────────────────────────────────────────────────────────────────────────
#
# Graph's lifecycle for a webhook subscription:
#   1. On subscription create, Graph POSTs once with ?validationToken=... in
#      the query string.  We MUST echo it back as plain text with a 200
#      within 10s, or the subscription is rejected.
#   2. On each new message, Graph POSTs a JSON payload describing the
#      change.  We compare the embedded clientState against the value we
#      stored when creating the subscription (forgery defence), then
#      fetch the full message and run the intake pipeline.
#
# We answer 202 immediately and process the message in BackgroundTasks -
# Graph will retry with backoff if we don't acknowledge inside 30s.


async def _process_graph_notification(notification: dict[str, Any]) -> None:
    """Fetch the full message and run it through the intake pipeline."""
    from src.integrations.m365_inbox import M365InboxClient
    from src.workflows.email_intake import process_inbox_message

    resource_data = notification.get("resourceData") or {}
    message_id = resource_data.get("id") or ""
    if not message_id:
        logger.warning("Graph notification has no resourceData.id: %r", notification)
        return
    try:
        inbox = M365InboxClient()
        message = await inbox.get_message(message_id)
        await process_inbox_message(message, source="webhook")
    except Exception as e:  # pragma: no cover - defensive
        logger.exception("Failed to process Graph notification: %s", e)


@router.post("/m365/inbox")
async def webhook_m365_inbox(
    request: Request,
    background_tasks: BackgroundTasks,
    validationToken: str | None = Query(default=None),
) -> Response:
    """Receive Graph subscription notifications.

    Two modes (Graph spec):
      * ``?validationToken=...`` - subscription handshake; echo as text/plain.
      * JSON body                - actual change notification.
    """
    # ── Validation handshake ─────────────────────────────────────────────────
    if validationToken is not None:
        logger.info("Graph webhook validation handshake received")
        return Response(content=validationToken, media_type="text/plain", status_code=200)

    # ── Notification ─────────────────────────────────────────────────────────
    payload: dict[str, Any] = {}
    try:
        payload = await request.json()
    except Exception:
        logger.warning("Graph notification body was not valid JSON")
        return Response(status_code=202)

    notifications = payload.get("value") or []
    accepted = 0
    rejected = 0
    for notif in notifications:
        sub_id = notif.get("subscriptionId") or ""
        sent_client_state = notif.get("clientState") or ""
        stored = get_graph_subscription(sub_id)
        if not stored or stored.get("client_state") != sent_client_state:
            rejected += 1
            logger.warning(
                "Rejecting Graph notification with bad clientState (sub_id=%s)",
                sub_id,
            )
            continue
        accepted += 1
        background_tasks.add_task(_process_graph_notification, notif)

    log_action(
        workflow="email_intake",
        action="webhook_notifications_received",
        actor="graph",
        details={"accepted": accepted, "rejected": rejected,
                 "total": len(notifications)},
    )
    return Response(status_code=202)


# ─────────────────────────────────────────────────────────────────────────────
# Zoom webhook - cloud recording completed  (minutes pipeline, stage 0)
# ─────────────────────────────────────────────────────────────────────────────
#
# Zoom's lifecycle for a webhook endpoint:
#   1. On URL save (and re-validation), Zoom POSTs an
#      ``endpoint.url_validation`` event carrying a ``plainToken``.  We must
#      reply 200 with {"plainToken", "encryptedToken"} where encryptedToken is
#      HMAC_SHA256(secret_token, plainToken).hexdigest().  This handshake must
#      work even before signature verification is possible.
#   2. On each subscribed event (here: ``recording.completed``), Zoom signs the
#      request: header ``x-zm-signature: v0={hmac}`` over the message
#      ``f"v0:{x-zm-request-timestamp}:{raw_body}"``.  We verify it, ack 202
#      immediately, and download the assets in a BackgroundTask (Zoom retries
#      with backoff if we don't ack quickly).


def _zoom_crc_response(plain_token: str) -> dict[str, str]:
    """Build the Zoom URL-validation (CRC) response body.

    Args:
        plain_token: The ``plainToken`` Zoom sent in the validation event.

    Returns:
        ``{"plainToken": ..., "encryptedToken": <hmac-sha256 hex>}``.
    """
    secret_token = settings.zoom_webhook_secret_token or ""
    encrypted = hmac.new(
        secret_token.encode(),
        plain_token.encode(),
        hashlib.sha256,
    ).hexdigest()
    return {"plainToken": plain_token, "encryptedToken": encrypted}


def _verify_zoom_signature(headers: Any, raw_body: bytes) -> bool:
    """Verify Zoom's ``x-zm-signature`` header against the raw request body.

    Zoom computes ``v0={HMAC_SHA256(secret, "v0:{timestamp}:{body}")}``.

    SECURITY NOTE: when ``settings.zoom_webhook_secret_token`` is empty (i.e.
    the Secret Token has not been configured yet), this returns ``True`` with a
    logged warning so local/dev setups work before the token exists.  Once the
    token is set in config, signatures are enforced.  Do not deploy to
    production with an empty token.

    Args:
        headers: The request headers (read case-insensitively).
        raw_body: The exact raw request body bytes Zoom signed.

    Returns:
        ``True`` if the signature is valid (or the token is unconfigured).
    """
    secret_token = settings.zoom_webhook_secret_token or ""
    if not secret_token:
        logger.warning(
            "ZOOM_WEBHOOK_SECRET_TOKEN is not configured - accepting Zoom "
            "webhook WITHOUT signature verification (dev mode only)."
        )
        return True

    signature = headers.get("x-zm-signature") or ""
    timestamp = headers.get("x-zm-request-timestamp") or ""
    if not signature or not timestamp:
        logger.warning("Zoom webhook missing signature/timestamp header")
        return False

    message = f"v0:{timestamp}:{raw_body.decode('utf-8', errors='replace')}"
    expected = "v0=" + hmac.new(
        secret_token.encode(),
        message.encode(),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


async def _process_zoom_recording(meeting_uuid: str) -> None:
    """Download all recording assets for a completed Zoom meeting.

    Runs as a BackgroundTask; never raises (mirrors
    :func:`_process_graph_notification`).
    """
    from src.integrations.zoom import ZoomClient

    try:
        manifest = await ZoomClient().download_recording_assets(meeting_uuid)
        logger.info(
            "Zoom recording assets downloaded for %s: %d file(s) -> %s",
            meeting_uuid,
            len(manifest.get("files", [])),
            manifest.get("dest_dir"),
        )
    except Exception as e:  # pragma: no cover - defensive
        logger.exception(
            "Failed to download Zoom recording assets for %s: %s", meeting_uuid, e,
        )


@router.post("/zoom/recording")
async def webhook_zoom_recording(
    request: Request,
    background_tasks: BackgroundTasks,
) -> Response:
    """Receive Zoom webhook events for cloud recordings.

    Handles three cases:
      * ``endpoint.url_validation`` - CRC handshake (always answered 200, even
        if the signature can't be verified - this is what lets the URL save).
      * ``recording.completed``     - verify signature, schedule asset download,
        ack 202.
      * any other event            - verify signature, log, ack 202.
    """
    raw = await request.body()
    try:
        body: dict[str, Any] = json.loads(raw)
    except Exception:
        logger.warning("Zoom webhook body was not valid JSON")
        return Response(status_code=202)

    event = body.get("event")

    # ── URL validation handshake (no signature required) ─────────────────────
    if event == "endpoint.url_validation":
        plain_token = (body.get("payload") or {}).get("plainToken", "")
        logger.info("Zoom webhook URL-validation handshake received")
        return JSONResponse(_zoom_crc_response(plain_token), status_code=200)

    # ── Signature verification for all real events ───────────────────────────
    if not _verify_zoom_signature(request.headers, raw):
        log_action(
            workflow="minutes",
            action="zoom_webhook_rejected",
            actor="zoom",
            details={"reason": "bad_signature", "event": event},
            status="failure",
        )
        logger.warning("Rejecting Zoom webhook with invalid signature (event=%s)", event)
        return Response(status_code=401)

    if event == "recording.completed":
        obj = (body.get("payload") or {}).get("object") or {}
        meeting_uuid = obj.get("uuid") or obj.get("id")
        background_tasks.add_task(_process_zoom_recording, str(meeting_uuid))
        log_action(
            workflow="minutes",
            action="zoom_recording_webhook",
            actor="zoom",
            target=str(meeting_uuid),
        )
        logger.info("Zoom recording.completed accepted for meeting %s", meeting_uuid)
        return Response(status_code=202)

    # ── Any other (subscribed) event - acknowledge and ignore ────────────────
    logger.info("Ignoring unsupported Zoom webhook event: %s", event)
    log_action(
        workflow="minutes",
        action="zoom_webhook_ignored",
        actor="zoom",
        details={"event": event},
    )
    return Response(status_code=202)
