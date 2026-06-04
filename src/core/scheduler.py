"""Background scheduler — APScheduler in async mode.

Owns two recurring jobs:

  * ``email_intake.safety_poll``  — daily at 12:00 Europe/Athens; runs
                                    :func:`src.workflows.email_intake.run_safety_poll`
  * ``m365_inbox.renew_subs``     — hourly; renews any Graph webhook
                                    subscription whose remaining lifetime
                                    drops below the threshold

The scheduler starts in the FastAPI ``lifespan`` context.  CLI commands
that need to invoke the same logic on-demand call the underlying coro
directly (see ``ai-assistant m365 poll-now`` / ``renew-now``) — they
don't go through the scheduler.
"""

from __future__ import annotations

import logging
from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from src.config import settings

logger = logging.getLogger(__name__)

_scheduler: Optional[AsyncIOScheduler] = None


async def _safety_poll_job() -> None:
    """APScheduler job wrapper for run_safety_poll."""
    from src.workflows.email_intake import run_safety_poll

    try:
        result = await run_safety_poll()
        logger.info("Safety poll job done: %s", result)
    except Exception as e:
        logger.exception("Safety poll job crashed: %s", e)


async def _egkyklios_general_quarterly_job() -> None:
    """Quarterly trigger for the Γενική Εγκύκλιος Ενημέρωσης workflow.

    Fires at 00:00 Europe/Athens on the 1st of January, April, July, and
    October.  Runs the workflow with default-quarter window (the previous
    3 months) and `test_mode=False`, which carries the run all the way to
    the approval gate.  The workflow then parks (`awaiting_approval`); the
    SecGen advances it manually via ``/board egkyklios general-approve``
    or the embedded approve button on the draft Discord embed.

    Idempotency is enforced inside the workflow's first step (it aborts
    if a non-cancelled draft already overlaps the period), so a duplicate
    fire (e.g. machine clock skew) is harmless.
    """
    from src.workflows.egkyklios_general import EgkykliosGeneralWorkflow

    try:
        wf = EgkykliosGeneralWorkflow(actor="scheduler")
        result = await wf.run({"test_mode": False})
        logger.info(
            "Quarterly Γενική Εγκύκλιος job done: status=%s draft_id=%s",
            result.get("status"),
            (wf.context or {}).get("egkyklios_draft_id"),
        )
    except Exception as e:
        logger.exception("Quarterly Γενική Εγκύκλιος job crashed: %s", e)


async def _renew_subscriptions_job() -> None:
    """APScheduler job wrapper for GraphSubscriptionsClient.renew_expiring."""
    from src.integrations.graph_subscriptions import GraphSubscriptionsClient

    try:
        client = GraphSubscriptionsClient()
        renewed = await client.renew_expiring()
        if renewed:
            logger.info("Renewed %d Graph subscription(s): %s",
                        len(renewed), renewed)
    except Exception as e:
        logger.exception("Subscription renewal job crashed: %s", e)


# How long an unresolved SecGen-reservation-confirmation workflow can sit
# before we auto-fail it.  Tuned conservatively: SecGen typically resolves
# within a day; 48h leaves room for weekends.  Bump via this constant if
# needed.  (Renamed from COLLISION_STUCK_HOURS on 2026-05-27 when the old
# collision-gate flow was replaced by reservation-confirmation.)
RESERVATION_CONFIRM_STUCK_HOURS = 48
COLLISION_STUCK_HOURS = RESERVATION_CONFIRM_STUCK_HOURS   # legacy alias


async def _reservation_confirm_timeout_job() -> None:
    """Auto-fail archive workflows stuck awaiting SecGen reservation confirmation.

    Scans ``workflow_state`` for archive rows whose context contains
    ``pending_reservation_confirmation`` AND whose ``updated_at`` is older
    than ``RESERVATION_CONFIRM_STUCK_HOURS``.  Each is rolled back and
    marked ``failed_reservation_timeout`` so SecGen sees them in
    `archive list`.

    Runs hourly alongside the subscription-renewal job.
    """
    import json as _json
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    from src.core.audit import _get_connection, save_workflow_state, log_action
    from src.workflows.archive import ArchiveWorkflow

    cutoff = _dt.now(_tz.utc) - _td(hours=RESERVATION_CONFIRM_STUCK_HOURS)
    try:
        conn = _get_connection()
        rows = conn.execute(
            "SELECT workflow_id, data, updated_at FROM workflow_state "
            "WHERE workflow_name = 'archive' "
            "  AND state IN ('failed', 'in_progress', 'executing') "
            "  AND data LIKE '%pending_reservation_confirmation%' "
            "  AND datetime(updated_at) < datetime(?)",
            (cutoff.isoformat(),),
        ).fetchall()
    except Exception as e:
        logger.exception("Reservation-confirm timeout scan failed: %s", e)
        return

    auto_failed = 0
    for row in rows:
        wf_id = row["workflow_id"]
        try:
            data = _json.loads(row["data"] or "{}")
        except (TypeError, _json.JSONDecodeError):
            continue
        ctx = data.get("context") or {}
        if not ctx.get("pending_reservation_confirmation"):
            continue   # belt-and-braces: LIKE matched but the actual key is gone
        try:
            wf = ArchiveWorkflow()
            wf.workflow_id = wf_id
            await wf.rollback(ctx)
        except Exception as e:
            logger.warning("Reservation-timeout rollback failed for %s: %s", wf_id, e)
        save_workflow_state(
            workflow_name="archive",
            workflow_id=wf_id,
            state="failed_reservation_timeout",
            data=data,
        )
        log_action(
            workflow="archive",
            action="reservation_timeout_auto_failed",
            actor="scheduler",
            details={"workflow_id": wf_id, "hours_stuck": RESERVATION_CONFIRM_STUCK_HOURS},
            status="failure",
        )
        auto_failed += 1

    if auto_failed:
        logger.info(
            "Auto-failed %d archive workflow(s) stuck on reservation-confirm > %dh",
            auto_failed, RESERVATION_CONFIRM_STUCK_HOURS,
        )


# Legacy alias: kept so the scheduler's add_job registration below doesn't
# break if anything else imports the old name.
_collision_timeout_job = _reservation_confirm_timeout_job


# How old a data/inbox/m365_intake_* tempdir must be before the cleanup job
# removes it.  The active intake creates + deletes its own tempdir inside
# the same call, so anything older than this is leftover from a crash or
# an old bug.  7 days is conservative.
INBOX_TMPDIR_STALE_DAYS = 7


async def _inbox_tmpdir_cleanup_job() -> None:
    """Remove leftover ``data/inbox/m365_intake_*`` tempdirs older than 7d.

    Belt-and-braces against future tempdir leaks.  The email-intake flow
    cleans up its own tempdir via try/finally now (see
    ``email_intake.process_inbox_message``), so this job should normally
    find nothing.  Runs daily.
    """
    import shutil as _shutil
    import time as _time
    from pathlib import Path

    inbox_dir = Path("data") / "inbox"
    if not inbox_dir.exists():
        return
    cutoff = _time.time() - (INBOX_TMPDIR_STALE_DAYS * 86400)
    removed = 0
    for child in inbox_dir.iterdir():
        if not child.is_dir() or not child.name.startswith("m365_intake_"):
            continue
        try:
            if child.stat().st_mtime < cutoff:
                _shutil.rmtree(child, ignore_errors=True)
                removed += 1
        except Exception as exc:  # pragma: no cover — best-effort
            logger.debug("inbox tmpdir cleanup: skip %s (%s)", child, exc)
    if removed:
        logger.info(
            "inbox tmpdir cleanup: removed %d stale tempdir(s) older than %dd",
            removed, INBOX_TMPDIR_STALE_DAYS,
        )


def start_scheduler() -> AsyncIOScheduler:
    """Start the background scheduler (idempotent)."""
    global _scheduler
    if _scheduler is not None and _scheduler.running:
        return _scheduler

    cfg = settings.m365_inbox
    _scheduler = AsyncIOScheduler(timezone="Europe/Athens")
    _scheduler.add_job(
        _safety_poll_job,
        CronTrigger(hour=cfg.safety_poll_hour, minute=cfg.safety_poll_minute),
        id="email_intake.safety_poll",
        replace_existing=True,
    )
    _scheduler.add_job(
        _renew_subscriptions_job,
        IntervalTrigger(hours=1),
        id="m365_inbox.renew_subs",
        replace_existing=True,
    )
    _scheduler.add_job(
        _collision_timeout_job,
        IntervalTrigger(hours=1),
        id="archive.collision_timeout",
        replace_existing=True,
    )
    _scheduler.add_job(
        _inbox_tmpdir_cleanup_job,
        IntervalTrigger(days=1),
        id="email_intake.tmpdir_cleanup",
        replace_existing=True,
    )
    # Γενική Εγκύκλιος Ενημέρωσης — quarterly: first day of each calendar
    # quarter at 00:00 Europe/Athens.  Parks at SecGen approval gate; the
    # SecGen advances via `/board egkyklios general-approve`.
    _scheduler.add_job(
        _egkyklios_general_quarterly_job,
        CronTrigger(month="1,4,7,10", day=1, hour=0, minute=0),
        id="egkyklios.general_quarterly",
        replace_existing=True,
    )
    _scheduler.start()
    logger.info(
        "Scheduler started: safety_poll @ %02d:%02d Europe/Athens, renew_subs hourly",
        cfg.safety_poll_hour, cfg.safety_poll_minute,
    )
    return _scheduler


def stop_scheduler() -> None:
    """Stop the scheduler if running."""
    global _scheduler
    if _scheduler is not None and _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("Scheduler stopped")
    _scheduler = None
