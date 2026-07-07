"""Board-meeting (ΔΣ) lifecycle posts.

Every Discord message the board-meeting workflow emits is defined here as a
pure builder.  Edit copy / fields / buttons here to restyle the live posts.

Lifecycle order (matches the meeting flow):
    1. board_thread_opened_embed   - private thread opens (scheduling email out)
    2. scheduling_mirror_embed     - "fill availability + agenda" mirror
    3. public_invitation_embed     - members-visible agenda thread (on schedule)
    4. milestone_published_embed   - "invitation published" note in board thread
    5. invitation_mirror_embed     - final invitation mirror in board thread
    6. reminder_embed              - N-hours-before countdown
    7. minutes_mirror_embed        - minutes draft/final mirror (board email)
    8. minutes_shared_embed        - minutes Drive link (board.minutes.shared)
    9. cancellation_embed          - meeting cancelled notice

Convention: builders that may carry link buttons return
``(embed, view | None)``; plain notices return just ``embed``.
"""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import discord

from src.integrations.discord.brand import AMNESTY_YELLOW, brand_embed, fmt_ts

# Meeting times in the agenda sheet are Athens-local wall-clock times, so a naive
# datetime means "Europe/Athens", not UTC.
_ATHENS_TZ = ZoneInfo("Europe/Athens")


def _prefix(test_mode: bool) -> str:
    """Visual ``[TEST] `` marker prepended to titles during sandbox runs."""
    return "[TEST] " if test_mode else ""


def _parse_dt(value: str | datetime | None) -> datetime | None:
    """Coerce an ISO string / datetime into a tz-aware UTC datetime, or None."""
    if value is None:
        return None
    dt = value
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value)
        except ValueError:
            return None
    if isinstance(dt, datetime):
        return dt.replace(tzinfo=_ATHENS_TZ) if dt.tzinfo is None else dt
    return None


# ── 1. Private thread opens ───────────────────────────────────────────────────


def board_thread_opened_embed(
    *,
    meeting_ref: str,
    poll_url: str = "",
    agenda_sheet_url: str = "",
    test_mode: bool = False,
) -> discord.Embed:
    """Opening post of the private board thread (fires with the scheduling email)."""
    embed = brand_embed(
        title=f"{_prefix(test_mode)}Νέος κύκλος συνεδρίασης - {meeting_ref}",
        description=(
            "Ο κύκλος αυτής της συνεδρίασης ξεκίνησε.  Όλα τα emails του Δ.Σ. "
            "(προγραμματισμός, τελική πρόσκληση, πρακτικά) θα δημοσιεύονται "
            "εδώ ως απαντήσεις στο thread."
        ),
        color=AMNESTY_YELLOW,
    )
    if poll_url:
        embed.add_field(name="Διαθεσιμότητες", value=poll_url, inline=False)
    if agenda_sheet_url:
        embed.add_field(name="Ημερήσια Διάταξη", value=agenda_sheet_url, inline=False)
    return embed


# ── 2. Scheduling-email mirror ────────────────────────────────────────────────


def scheduling_mirror_embed(
    *,
    meeting_ref: str,
    poll_url: str = "",
    agenda_url: str = "",
    test_mode: bool = False,
) -> tuple[discord.Embed, discord.ui.View | None]:
    """📅 Scheduling email mirror - availability poll + agenda sheet links."""
    embed = brand_embed(
        title=f"{_prefix(test_mode)}📅 Προγραμματισμός Συνεδρίασης",
        description=(
            f"Εστάλη email προγραμματισμού για τη συνεδρίαση **{meeting_ref}**.\n"
            "Παρακαλώ δηλώστε τη διαθεσιμότητά σας και συμπληρώστε την ημερήσια διάταξη."
        ),
        color=AMNESTY_YELLOW,
    )
    embed.add_field(name="Σύσκεψη", value=meeting_ref, inline=True)

    view = discord.ui.View(timeout=None)
    has_buttons = False
    if poll_url:
        embed.add_field(name="Διαθεσιμότητες", value=poll_url, inline=False)
        view.add_item(discord.ui.Button(
            label="Δήλωση Διαθεσιμότητας",
            style=discord.ButtonStyle.link,
            url=poll_url,
            emoji="📆",
        ))
        has_buttons = True
    if agenda_url:
        embed.add_field(name="Ημερήσια Διάταξη", value=agenda_url, inline=False)
        view.add_item(discord.ui.Button(
            label="Ημερήσια Διάταξη",
            style=discord.ButtonStyle.link,
            url=agenda_url,
            emoji="📋",
        ))
        has_buttons = True
    return embed, (view if has_buttons else None)


# ── 3. Public agenda thread (members-visible) ─────────────────────────────────


def public_invitation_embed(
    *,
    starts_at: datetime,
    agenda_summary: str = "",
    zoom_url: str = "",
) -> tuple[discord.Embed, discord.ui.View | None]:
    """Members-visible invitation embed for the public agenda forum thread."""
    if starts_at.tzinfo is None:
        starts_at = starts_at.replace(tzinfo=_ATHENS_TZ)
    embed = brand_embed(
        title="Πρόσκληση Συνεδρίασης ΔΣ",
        color=AMNESTY_YELLOW,
        timestamp=starts_at,
    )
    embed.add_field(name="Ημερομηνία", value=fmt_ts(starts_at, "F"), inline=False)
    embed.add_field(name="Έναρξη", value=fmt_ts(starts_at, "R"), inline=True)
    embed.add_field(name="Διάρκεια", value="≈ 2 ώρες", inline=True)
    if agenda_summary:
        embed.add_field(name="Ημερήσια Διάταξη", value=agenda_summary[:1024], inline=False)
    else:
        embed.add_field(name="Ημερήσια Διάταξη", value="*κατόπιν ανακοίνωσης*", inline=False)
    if zoom_url:
        embed.add_field(name="Τοποθεσία", value="🎥 Zoom (online)", inline=True)

    view = None
    if zoom_url:
        view = discord.ui.View(timeout=None)
        view.add_item(discord.ui.Button(
            label="Συμμετοχή στη Συνεδρίαση",
            style=discord.ButtonStyle.link,
            url=zoom_url,
            emoji="🎥",
        ))
    return embed, view


# ── 4. "Invitation published" milestone ───────────────────────────────────────


def milestone_published_embed() -> discord.Embed:
    """Posted in the board thread when the public invitation goes out."""
    return brand_embed(
        title="✓ Πρόσκληση δημοσιεύτηκε",
        description=(
            "Η τελική πρόσκληση εστάλη στο ΔΣ και η ενημέρωση "
            "δημοσιεύτηκε στο public channel."
        ),
        color=AMNESTY_YELLOW,
    )


# ── 5. Final-invitation email mirror ──────────────────────────────────────────


def invitation_mirror_embed(
    *,
    meeting_ref: str,
    zoom_url: str = "",
    agenda_url: str = "",
    invitation_pdf_url: str = "",
    meeting_datetime: str | datetime | None = None,
    agenda_summary: str = "",
    test_mode: bool = False,
) -> tuple[discord.Embed, discord.ui.View | None]:
    """📩 Final invitation mirror - date/time, agenda, Zoom link, invitation PDF."""
    starts_at = _parse_dt(meeting_datetime)
    embed = brand_embed(
        title=f"{_prefix(test_mode)}📩 Πρόσκληση Συνεδρίασης ΔΣ",
        description=f"Απεστάλη η τελική πρόσκληση για τη συνεδρίαση **{meeting_ref}**.",
        color=AMNESTY_YELLOW,
        timestamp=starts_at,
    )
    if starts_at:
        embed.add_field(name="Ημερομηνία & Ώρα", value=fmt_ts(starts_at, "F"), inline=False)
        embed.add_field(name="", value=fmt_ts(starts_at, "R"), inline=False)
    if agenda_summary:
        embed.add_field(name="Ημερήσια Διάταξη", value=agenda_summary[:1024], inline=False)
    if zoom_url:
        embed.add_field(name="Σύνδεσμος Zoom", value=zoom_url, inline=False)

    view = discord.ui.View(timeout=None)
    has_buttons = False
    if invitation_pdf_url:
        view.add_item(discord.ui.Button(
            label="Πρόσκληση",
            style=discord.ButtonStyle.link,
            url=invitation_pdf_url,
            emoji="📄",
        ))
        has_buttons = True
    if agenda_url:
        view.add_item(discord.ui.Button(
            label="Ημερήσια Διάταξη",
            style=discord.ButtonStyle.link,
            url=agenda_url,
            emoji="📋",
        ))
        has_buttons = True
    return embed, (view if has_buttons else None)


# ── 6. Reminder ───────────────────────────────────────────────────────────────


def reminder_embed(
    *,
    hours_before: int,
    starts_at: datetime,
) -> discord.Embed:
    """⏰ N-hours-before reminder with a live ``<t:R>`` countdown."""
    if starts_at.tzinfo is None:
        starts_at = starts_at.replace(tzinfo=_ATHENS_TZ)
    embed = brand_embed(
        title="Υπενθύμιση Συνεδρίασης",
        description=(
            f"Η συνεδρίαση του Διοικητικού Συμβουλίου ξεκινά σε "
            f"**{hours_before} ώρες** ({fmt_ts(starts_at, 'R')})."
        ),
        color=AMNESTY_YELLOW,
    )
    embed.add_field(name="Ώρα Έναρξης", value=fmt_ts(starts_at, "F"), inline=False)
    return embed


# ── 7. Minutes email mirror (draft/final) ─────────────────────────────────────


def minutes_mirror_embed(
    *,
    meeting_ref: str,
    doc_url: str = "",
    is_draft: bool = True,
    test_mode: bool = False,
) -> tuple[discord.Embed, discord.ui.View | None]:
    """📄 Minutes email mirror - draft (for comment) or finalised."""
    prefix = _prefix(test_mode)
    if is_draft:
        title = f"{prefix}📄 Πρόχειρα Πρακτικά - προς σχολιασμό"
        description = (
            f"Τα **πρόχειρα πρακτικά** της συνεδρίασης **{meeting_ref}** "
            "είναι διαθέσιμα για σχολιασμό.  Παρακαλώ αφήστε τα σχόλιά σας απευθείας στο έγγραφο."
        )
        button_label = "Προβολή & Σχολιασμός"
        button_emoji = "📝"
    else:
        title = f"{prefix}✅ Τελικά Πρακτικά"
        description = (
            f"Τα **τελικά πρακτικά** της συνεδρίασης **{meeting_ref}** έχουν οριστικοποιηθεί."
        )
        button_label = "Προβολή Πρακτικών"
        button_emoji = "📄"

    embed = brand_embed(title=title, description=description, color=AMNESTY_YELLOW)
    view = discord.ui.View(timeout=None)
    has_buttons = False
    if doc_url:
        embed.add_field(name="Έγγραφο", value=doc_url, inline=False)
        view.add_item(discord.ui.Button(
            label=button_label,
            style=discord.ButtonStyle.link,
            url=doc_url,
            emoji=button_emoji,
        ))
        has_buttons = True
    return embed, (view if has_buttons else None)


# ── 8. Minutes shared (board.minutes.shared event) ────────────────────────────


def minutes_shared_embed(
    *,
    drive_url: str,
) -> tuple[discord.Embed, discord.ui.View | None]:
    """📄 Minutes Drive link posted to the private board thread for comment."""
    embed = brand_embed(
        title="Πρακτικά Συνεδρίασης",
        description=(
            "Σας κοινοποιείται το προσχέδιο των πρακτικών της προηγούμενης "
            "συνεδρίασης. Μπορείτε να σχολιάσετε και να προτείνετε διορθώσεις "
            "απευθείας επί του εγγράφου."
        ),
        color=AMNESTY_YELLOW,
    )
    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button(
        label="Άνοιγμα Πρακτικών",
        style=discord.ButtonStyle.link,
        url=drive_url,
        emoji="📄",
    ))
    return embed, view


# ── 9. Cancellation ───────────────────────────────────────────────────────────


def cancellation_embed(*, reason: str = "") -> discord.Embed:
    """Posted in every meeting thread when the meeting is cancelled."""
    return brand_embed(
        title="Η Συνεδρίαση Ακυρώθηκε",
        description=f"**Λόγος:** {reason or 'δεν δόθηκε'}",
        color=AMNESTY_YELLOW,
    )
