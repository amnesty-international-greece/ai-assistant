"""Typed event payloads for the platform event bus.

Constants at top — use these strings everywhere instead of bare literals so
typos surface as ImportError, not as silently-ignored events.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

# ── Event type names (use these, never bare strings) ────────────────────────
EVENT_BOARD_MEETING_THREAD_OPENED = "board.meeting.thread_opened"
EVENT_BOARD_EMAIL_SENT = "board.meeting.email_sent"
EVENT_BOARD_MEETING_SCHEDULED = "board.meeting.scheduled"
EVENT_BOARD_MEETING_CANCELLED = "board.meeting.cancelled"
EVENT_BOARD_MEETING_REMINDER_DUE = "board.meeting.reminder_due"
EVENT_BOARD_MINUTES_SHARED = "board.minutes.shared"
EVENT_GA_CALLED = "ga.called"
EVENT_GA_PROXY_WINDOW_OPENING = "ga.proxy_window_opening"
EVENT_EGKYKLIOS_PUBLISHED = "egkyklios.published"
EVENT_MEMBER_APPROVED = "member.approved"


@dataclass(slots=True)
class BoardMeetingThreadOpenedPayload:
    """Published when the first scheduling email goes out.

    Triggers ``platform_bridge`` to open the **private** board forum thread
    named ``Συνεδρίαση {meeting_ref}`` and post the first email's body as the
    opening message.  Distinct from ``BoardMeetingScheduled`` which only
    fires later, once the invitation is actually published to the wider
    membership (Brevo campaign sent).
    """
    meeting_id: str           # e.g. "board_meeting:ΔΣ05-2026"
    meeting_ref: str          # e.g. "ΔΣ05-2026"  (also appears in thread title)
    email_subject: str        # The subject line of the scheduling email
    email_body_html: str      # Raw HTML body; handler strips to plain text
    poll_url: str = ""        # Doodle / LettuceMeet / When2Meet URL if any
    agenda_sheet_url: str = ""# Google Sheet URL (link for board agenda input)
    test_mode: bool = False


@dataclass(slots=True)
class BoardEmailSentPayload:
    """Published after every successful board-thread email send.

    Lets ``platform_bridge`` mirror the email body into the private Discord
    thread as a follow-up message.  ``kind`` selects which mirror template
    to apply (scheduling / invitation / minutes).
    """
    meeting_id: str           # Routes the post to the right thread
    meeting_ref: str          # Used in template substitution
    kind: str                 # 'scheduling' | 'invitation' | 'minutes_draft' | 'minutes_final'
    subject: str
    body_html: str            # Kept for non-platform emails (member replies) mirrored verbatim
    test_mode: bool = False
    # ── Optional metadata for rich Discord embeds ─────────────────────────────
    # Filled by the workflow when a platform-sent email fires this event.
    # Left empty for inbound member-reply mirrors (where body_html is used).
    poll_url: str = ""        # scheduling: availability-poll link
    agenda_url: str = ""      # scheduling + invitation: Google Sheet URL
    zoom_url: str = ""        # invitation: general Zoom meeting URL
    meeting_datetime: str = ""  # invitation: ISO "YYYY-MM-DDTHH:MM" (UTC)
    agenda_summary: str = ""  # invitation: newline-separated agenda items
    doc_url: str = ""         # minutes_draft / minutes_final: Google Doc link


@dataclass(slots=True)
class BoardMeetingScheduledPayload:
    meeting_id: str           # e.g. "board_meeting:2026-05-21"
    starts_at: datetime       # UTC
    zoom_url: str
    agenda_summary: str       # short text or markdown
    board_member_emails: list[str] = field(default_factory=list)
    test_mode: bool = False   # True → platform_bridge uses sandbox channels


@dataclass(slots=True)
class BoardMeetingCancelledPayload:
    meeting_id: str
    reason: str = ""


@dataclass(slots=True)
class BoardMeetingReminderDuePayload:
    meeting_id: str
    hours_before: int


@dataclass(slots=True)
class BoardMinutesSharedPayload:
    meeting_id: str
    drive_url: str
    doc_id: str


@dataclass(slots=True)
class GACalledPayload:
    ga_id: str
    starts_at: datetime
    notice_days: int
    agenda_url: str = ""


@dataclass(slots=True)
class GAProxyWindowOpeningPayload:
    ga_id: str
    closes_at: datetime


@dataclass(slots=True)
class EgkykliosPublishedPayload:
    kind: str                   # 'general' | 'special'
    title: str                  # e.g. "ΙΑΝΟΥΑΡΙΟΣ - ΜΑΡΤΙΟΣ 2026"
    protocol_number: str        # e.g. "2026_042"
    sharepoint_url: str         # public share link
    sent_at: str                # ISO-8601 UTC timestamp


@dataclass(slots=True)
class MemberApprovedPayload:
    member_id: str           # Discord user snowflake
    name: str
    joined_at: datetime
