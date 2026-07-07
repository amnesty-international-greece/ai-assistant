"""/board cog - board meeting workflow operations via Discord slash commands.

Admin-only group ``/board`` exposing two commands for the SecGen:

    /board share-poll [url] [workflow-id]   - share a scheduling poll URL in thread
    /board cancel <workflow-id>             - cancel + rollback an invitation workflow

``/board invite`` (full workflow with approval gates) is explicitly deferred
until a workflow-runner Discord infra exists.  The ``_BoardCommands`` class is
structured so adding it later is a 30-line addition.
"""
from __future__ import annotations

import datetime
import json
import logging
from urllib.parse import urlparse

import discord
from discord import app_commands
from discord.ext import commands

from src.integrations.discord.brand import AMNESTY_YELLOW, brand_embed

logger = logging.getLogger(__name__)

# Terminal states - a workflow in one of these is no longer "in-flight".
_TERMINAL_STATES = {"completed", "failed", "cancelled"}

# Role names required to use ``/board invite`` - the holder of BOTH is, in
# practice, the President.  Names not IDs, because IDs differ between the
# production guild and any future test guild.
_PRESIDENT_ROLES = {"Συντονιστής", "Διοικητικό Συμβούλιο"}


def _is_in_flight(state: str) -> bool:
    return state not in _TERMINAL_STATES


def _user_has_president_roles(user: discord.abc.User) -> bool:
    """True if ``user`` may run President-gated board actions.

    Passes for:
      * Guild **administrators** - always (the practical "I run this server"
        check; robust against role renames), AND
      * Members carrying **all** roles in :data:`_PRESIDENT_ROLES`.

    Falls back to ``False`` for DM-context users (who have neither admin
    permissions nor roles).
    """
    perms = getattr(user, "guild_permissions", None)
    if perms is not None and perms.administrator:
        return True
    role_names = {r.name for r in getattr(user, "roles", []) or []}
    return _PRESIDENT_ROLES.issubset(role_names)


# ── Date autocomplete ─────────────────────────────────────────────────────────
# Discord has no native date-picker for slash commands, so we use autocomplete:
# the user focuses start_date/end_date and picks a valid date from a list -
# guaranteeing a correct ISO value with no typing errors.
_DOW = ["Δευ", "Τρι", "Τετ", "Πεμ", "Παρ", "Σαβ", "Κυρ"]
_MON = ["", "Ιαν", "Φεβ", "Μαρ", "Απρ", "Μαΐ", "Ιουν", "Ιουλ", "Αυγ", "Σεπ", "Οκτ", "Νοε", "Δεκ"]


def _weekday_options(
    start_from: datetime.date, current: str, n: int = 25
) -> list[app_commands.Choice[str]]:
    """Up to *n* upcoming weekday (Mon-Fri) choices from *start_from*, filtered
    by the user's partial text *current*. Value is ISO ``YYYY-MM-DD``."""
    out: list[app_commands.Choice[str]] = []
    cur = (current or "").strip().lower()
    today = datetime.date.today()
    d = start_from
    while len(out) < n and (d - today).days < 120:
        if d.weekday() < 5:  # Mon-Fri
            iso = d.isoformat()
            label = f"{_DOW[d.weekday()]} {d.day} {_MON[d.month]} {d.year}"
            if not cur or cur in label.lower() or cur in iso:
                out.append(app_commands.Choice(name=label, value=iso))
        d += datetime.timedelta(days=1)
    return out


async def _ac_start_date(interaction: discord.Interaction, current: str):
    return _weekday_options(datetime.date.today() + datetime.timedelta(days=1), current)


async def _ac_end_date(interaction: discord.Interaction, current: str):
    """Only offer dates on/after the chosen start_date (single-day allowed)."""
    base = datetime.date.today() + datetime.timedelta(days=1)
    raw = getattr(interaction.namespace, "start_date", None)
    try:
        if raw:
            base = datetime.date.fromisoformat(raw)
    except (ValueError, TypeError):
        pass
    return _weekday_options(base, current)


# ── Εγκύκλιος approval helpers ───────────────────────────────────────────────


async def _approve_egkyklios_draft(draft_id: int) -> str:
    """Advance a parked Γενική Εγκύκλιος past its approval gate.

    Returns a short Greek summary suitable for the Discord followup.
    Wraps :class:`EgkykliosGeneralWorkflow.resume` so both the slash
    command and the embedded approve-button reuse the same code.
    """
    from src.core.audit import get_egkyklios_draft, get_workflow_state
    from src.workflows.egkyklios_general import EgkykliosGeneralWorkflow

    draft = get_egkyklios_draft(draft_id)
    if not draft:
        return f"❌ Δεν βρέθηκε draft #{draft_id}."
    if draft["status"] not in ("awaiting_approval", "drafting"):
        return (
            f"⚠️ Draft #{draft_id} βρίσκεται σε κατάσταση `{draft['status']}`. "
            f"Δεν μπορεί να εγκριθεί."
        )
    workflow_id = draft.get("workflow_id") or ""
    if not workflow_id or not get_workflow_state(workflow_id):
        return f"❌ Δεν βρέθηκε workflow για το draft #{draft_id}."

    wf = EgkykliosGeneralWorkflow(actor="discord:approve")
    result = await wf.resume(workflow_id, approval_granted=True)
    status = result.get("status", "?")
    ctx = wf.context or {}
    if status == "completed":
        lines = [f"✅ Draft #{draft_id} εγκρίθηκε & απεστάλη."]
        if ctx.get("protocol_number"):
            lines.append(f"**Αρ. Πρωτ.:** `{ctx['protocol_number']}`")
        if ctx.get("sharepoint_url"):
            lines.append(f"**SharePoint:** {ctx['sharepoint_url']}")
        if ctx.get("brevo_campaign_id"):
            lines.append(f"**Brevo:** campaign #{ctx['brevo_campaign_id']}")
        return "\n".join(lines)
    if status == "failed":
        return (
            f"❌ Η έγκριση απέτυχε στο βήμα `{result.get('step', '?')}`: "
            f"{result.get('error', '?')}"
        )
    return f"⚠️ Νέα κατάσταση: `{status}`."


class _EgkykliosApproveView(discord.ui.View):
    """One-button view attached to the post-draft Discord embed.

    The button advances the workflow exactly the same way as the
    ``/board egkyklios general-approve`` slash command.  Gated to the
    President role-pair so a random admin can't ship a draft to members
    by accident.
    """

    def __init__(self, *, draft_id: int) -> None:
        super().__init__(timeout=86400)  # 24h - plenty of time for SecGen review
        self.draft_id = draft_id
        approve_btn = discord.ui.Button(
            label="Έγκριση & Αποστολή",
            style=discord.ButtonStyle.success,
            emoji="✅",
            custom_id=f"egkyklios:approve:{draft_id}",
        )
        approve_btn.callback = self._on_approve
        self.add_item(approve_btn)

    async def _on_approve(self, interaction: discord.Interaction) -> None:
        if not _user_has_president_roles(interaction.user):
            await interaction.response.send_message(
                "⛔ Μόνο η Πρόεδρος (Συντονιστής + ΔΣ) μπορεί να εγκρίνει.",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            summary = await _approve_egkyklios_draft(self.draft_id)
        except Exception as exc:
            logger.exception("Egkyklios approve-button failed: %s", exc)
            await interaction.followup.send(f"❌ {exc}", ephemeral=True)
            return
        # Disable the button so it can't be re-clicked.
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True
        try:
            await interaction.message.edit(view=self)
        except Exception:
            pass
        await interaction.followup.send(summary, ephemeral=True)


# ── /board command group ─────────────────────────────────────────────────────


class BoardCog(commands.Cog):
    """`/board` slash command group - admin-only board meeting operations."""

    class _BoardCommands(app_commands.Group):
        def __init__(self, cog: "BoardCog") -> None:
            super().__init__(
                name="board",
                description="Λειτουργίες Διοικητικού Συμβουλίου - workflow προσκλήσεων",
                default_permissions=discord.Permissions(administrator=True),
            )
            self.cog = cog

        # ── /board share-poll ────────────────────────────────────────────────

        @app_commands.command(
            name="share-poll",
            description="Αποστολή poll διαθεσιμότητας στο email thread του ΔΣ",
        )
        @app_commands.describe(
            url="URL του poll (When2Meet, Doodle, κ.λπ.)",
            workflow_id="Workflow ID (προαιρετικό - default: πιο πρόσφατο εκκρεμές)",
        )
        async def cmd_share_poll(
            self,
            interaction: discord.Interaction,
            url: str,
            workflow_id: str | None = None,
        ) -> None:
            await interaction.response.defer(ephemeral=True, thinking=True)
            try:
                # ── URL validation ───────────────────────────────────────────
                parsed = urlparse(url)
                if parsed.scheme not in {"http", "https"}:
                    embed = brand_embed(
                        title="Μη έγκυρο URL",
                        description=(
                            f"Το URL `{url}` δεν είναι έγκυρο.\n"
                            "Χρησιμοποιήστε πλήρες URL που αρχίζει με `http://` ή `https://`."
                        ),
                        color=discord.Color.red(),
                    )
                    await interaction.followup.send(embed=embed, ephemeral=True)
                    return

                from src.core.audit import _get_connection, get_workflow_state
                from src.integrations.m365_mail import M365MailClient

                conn = _get_connection()

                # ── Resolve workflow_id ──────────────────────────────────────
                if not workflow_id:
                    rows = conn.execute(
                        "SELECT workflow_id, state FROM workflow_state "
                        "WHERE workflow_name = 'board_meeting_invitation' "
                        "ORDER BY updated_at DESC"
                    ).fetchall()

                    in_flight = [r for r in rows if _is_in_flight(r["state"])]

                    if not in_flight:
                        embed = brand_embed(
                            title="Δεν υπάρχει εκκρεμής πρόσκληση ΔΣ",
                            description=(
                                "Δεν βρέθηκε ενεργό workflow πρόσκλησης ΔΣ.\n"
                                "Εκκινήστε το workflow πρώτα από το CLI."
                            ),
                            color=AMNESTY_YELLOW,
                        )
                        await interaction.followup.send(embed=embed, ephemeral=True)
                        return

                    if len(in_flight) > 1:
                        ids = "\n".join(
                            f"• `{r['workflow_id']}` (state: {r['state']})"
                            for r in in_flight
                        )
                        embed = brand_embed(
                            title="Πολλαπλά εκκρεμή workflows",
                            description=(
                                f"Βρέθηκαν **{len(in_flight)}** εκκρεμή workflows πρόσκλησης ΔΣ.\n"
                                f"Χρησιμοποιήστε την παράμετρο `workflow-id` για να επιλέξετε:\n\n"
                                f"{ids}"
                            ),
                            color=discord.Color.orange(),
                        )
                        await interaction.followup.send(embed=embed, ephemeral=True)
                        return

                    workflow_id = in_flight[0]["workflow_id"]

                # ── Load state ───────────────────────────────────────────────
                state = get_workflow_state(workflow_id)
                if not state:
                    embed = brand_embed(
                        title="Workflow δεν βρέθηκε",
                        description=f"Δεν βρέθηκε workflow με ID `{workflow_id}`.",
                        color=discord.Color.red(),
                    )
                    await interaction.followup.send(embed=embed, ephemeral=True)
                    return

                # ── Refuse if past await_approval gate ───────────────────────
                blocked_states = {"approved", "executing", "in_progress", "completed"}
                data = json.loads(state.get("data") or "{}")
                step_index = data.get("step_index", 0)
                current_state = state.get("state", "")
                if step_index > 1 and current_state in blocked_states:
                    embed = brand_embed(
                        title="Εκπρόθεσμο poll",
                        description=(
                            f"Το workflow `{workflow_id}` έχει ήδη περάσει το await_approval gate "
                            f"(step_index={step_index}, state={current_state}).\n"
                            "Η αποστολή poll θα ήταν παραπλανητική - η ημερομηνία έχει κλειδώσει."
                        ),
                        color=discord.Color.red(),
                    )
                    await interaction.followup.send(embed=embed, ephemeral=True)
                    return

                ctx = data.get("context") or {}
                anchor = ctx.get("email_thread_anchor")
                if not anchor:
                    embed = brand_embed(
                        title="Δεν υπάρχει email thread",
                        description=(
                            f"Το workflow `{workflow_id}` δεν έχει `email_thread_anchor`.\n"
                            "Το scheduling email δεν έχει αποσταλεί ακόμα - αποστείλτε το poll χειροκίνητα."
                        ),
                        color=discord.Color.red(),
                    )
                    await interaction.followup.send(embed=embed, ephemeral=True)
                    return

                # ── Send the poll reply ──────────────────────────────────────
                body = f"Poll διαθεσιμότητας: {url}"
                client = M365MailClient()
                reply_id = await client.send_reply(
                    parent_internet_message_id=anchor,
                    body=body,
                    html=False,
                    to="board@amnesty.gr",
                    workflow="board_meeting_invitation",
                )

                embed = brand_embed(
                    title="Poll απεστάλη",
                    description=(
                        f"Το poll διαθεσιμότητας στάλθηκε στο email thread του ΔΣ.\n\n"
                        f"**URL:** {url}\n"
                        f"**Workflow:** `{workflow_id}`\n"
                        f"**Reply ID:** `{reply_id}`"
                    ),
                    color=AMNESTY_YELLOW,
                )
                await interaction.followup.send(embed=embed, ephemeral=True)

            except Exception as exc:
                logger.exception("board share-poll failed: %s", exc)
                await interaction.followup.send(f"❌ {exc}", ephemeral=True)

        # ── /board invite ────────────────────────────────────────────────────

        @app_commands.command(
            name="invite",
            description="Εκκίνηση κύκλου πρόσκλησης ΔΣ (στέλνει το πρώτο email, ανοίγει το thread)",
        )
        @app_commands.describe(
            poll_url="URL φόρμας διαθεσιμότητας (Doodle, LettuceMeet, κ.λπ.) - προαιρετικό",
            response_deadline="Προθεσμία απαντήσεων (YYYY-MM-DD) - προαιρετικό",
            test="Test mode - emails πάνε στο test inbox αντί στο board@",
            dates="Προχωρημένο: λίστα ημερομηνιών χωρισμένη με κόμμα (YYYY-MM-DD,...) - προαιρετικό",
            start_date="Αρχή εύρους υποψήφιων ημερομηνιών - επιλέξτε από τη λίστα",
            end_date="Τέλος εύρους υποψήφιων ημερομηνιών - επιλέξτε από τη λίστα",
        )
        @app_commands.autocomplete(start_date=_ac_start_date, end_date=_ac_end_date)
        async def cmd_invite(
            self,
            interaction: discord.Interaction,
            poll_url: str | None = None,
            response_deadline: str | None = None,
            test: bool = False,
            dates: str | None = None,
            start_date: str | None = None,
            end_date: str | None = None,
        ) -> None:
            """Discord-side launcher for the board-invitation workflow.

            Role-gated to the President (anyone with BOTH ``Συντονιστής`` AND
            ``Διοικητικό Συμβούλιο`` roles).  Sends the scheduling email,
            opens the private Discord thread, and halts at the
            ``await_approval`` gate.  Approval = ticking D16/D17/D18 on the
            agenda Google Sheet; the sheet's onEdit webhook then auto-resumes
            the workflow from where it parked.  No DM approval-gate infra
            needed - the sheet IS the gate.

            Resuming a manually parked workflow stays on the CLI (``ai-assistant
            invite`` interactive prompts) for now; the slash command only
            launches fresh cycles.
            """
            # Acknowledge within Discord's 3-second window FIRST. Mobile / laggy
            # clients can otherwise invalidate the interaction before we reply,
            # which surfaces as "404 Unknown interaction (10062)". Everything
            # after this point responds via followup.
            try:
                await interaction.response.defer(ephemeral=True, thinking=True)
            except discord.NotFound:
                return  # interaction already expired - nothing we can do

            # ── Role gate ────────────────────────────────────────────────
            if not _user_has_president_roles(interaction.user):
                await interaction.followup.send(
                    f"⛔ Αυτή η εντολή απαιτεί τους ρόλους "
                    f"**{' + '.join(sorted(_PRESIDENT_ROLES))}**.",
                    ephemeral=True,
                )
                return

            try:
                from src.workflows.board_meeting_invitation import (
                    BoardMeetingInvitationWorkflow,
                )

                # ── Collect candidate dates for the Crab.fit poll ─────────
                # start_date/end_date come from autocomplete (always valid ISO),
                # so format errors are effectively impossible. `dates` (a manual
                # comma list) stays as an advanced fallback. If nothing is given
                # the workflow simply proceeds without an auto Crab.fit poll.
                parsed_dates: list[datetime.date] = []

                if dates:
                    for chunk in dates.split(","):
                        chunk = chunk.strip()
                        if not chunk:
                            continue
                        try:
                            parsed_dates.append(datetime.date.fromisoformat(chunk))
                        except ValueError:
                            await interaction.followup.send(
                                f"❌ Μη έγκυρη ημερομηνία `{chunk}`. "
                                "Χρησιμοποιήστε μορφή YYYY-MM-DD.",
                                ephemeral=True,
                            )
                            return

                elif start_date or end_date:
                    # Convenience range - both bounds are required
                    if not (start_date and end_date):
                        await interaction.followup.send(
                            "❌ Επιλέξτε **και** `start_date` **και** `end_date`.",
                            ephemeral=True,
                        )
                        return
                    try:
                        d_start = datetime.date.fromisoformat(start_date)
                        d_end = datetime.date.fromisoformat(end_date)
                    except ValueError:
                        await interaction.followup.send(
                            "❌ Μη έγκυρη ημερομηνία. Επιλέξτε από τη λίστα προτάσεων.",
                            ephemeral=True,
                        )
                        return
                    if d_end < d_start:
                        await interaction.followup.send(
                            "❌ Η `end_date` πρέπει να είναι ίδια ή μεταγενέστερη της `start_date`.",
                            ephemeral=True,
                        )
                        return
                    delta = (d_end - d_start).days
                    parsed_dates = [
                        d_start + datetime.timedelta(days=i) for i in range(delta + 1)
                    ]

                actor = f"discord:{interaction.user.id}"
                wf = BoardMeetingInvitationWorkflow(actor=actor)

                # Build initial data - only set keys the caller actually
                # provided so the workflow's defaults apply otherwise.
                initial_data: dict = {"test_mode": bool(test)}
                if poll_url:
                    initial_data["poll_url"] = poll_url
                if response_deadline:
                    initial_data["response_deadline"] = response_deadline
                # Only pass crabfit_dates when no explicit poll_url was given -
                # poll_url wins (manual override); crabfit_dates would be ignored
                # by the workflow anyway, but we omit it to avoid confusion.
                if parsed_dates and not poll_url:
                    initial_data["crabfit_dates"] = [d.isoformat() for d in parsed_dates]

                result = await wf.run(initial_data)

                status = result.get("status", "unknown")
                step = result.get("step") or "-"

                embed = brand_embed(
                    title="Workflow πρόσκλησης ΔΣ",
                    description=(
                        f"Status: **{status}**\n"
                        f"Workflow ID: `{wf.workflow_id}`\n"
                        f"Σταμάτησε στο βήμα: `{step}`"
                    ),
                    color=AMNESTY_YELLOW,
                )
                if test:
                    embed.add_field(name="Mode", value="🧪 TEST", inline=True)
                if poll_url:
                    embed.add_field(name="Poll URL", value=poll_url, inline=False)
                # Surface the auto-created Crab.fit URL if the workflow created one
                crabfit_url = wf.context.get("crabfit_url") if wf.context else None
                if crabfit_url:
                    embed.add_field(name="Crab.fit poll", value=crabfit_url, inline=False)

                # If parked at the first approval gate, point the operator at
                # the sheet so they know exactly what to do.
                if status == "awaiting_approval" and step == "await_approval":
                    embed.add_field(
                        name="Επόμενο",
                        value=(
                            "1. Το πρώτο email πήγε στο `board@amnesty.org.gr`.\n"
                            "2. Το ΔΣ συμπληρώνει διαθεσιμότητες + ημερήσια διάταξη "
                            "στο agenda sheet.\n"
                            "3. Όταν τσεκαριστούν τα `D16/D17/D18`, ο workflow "
                            "συνεχίζει αυτόματα μέσω webhook."
                        ),
                        inline=False,
                    )

                await interaction.followup.send(embed=embed, ephemeral=True)

            except Exception as exc:
                logger.exception("board invite failed: %s", exc)
                await interaction.followup.send(f"❌ {exc}", ephemeral=True)

        # ── /board cancel ────────────────────────────────────────────────────

        @app_commands.command(
            name="cancel",
            description="Ακύρωση & rollback workflow πρόσκλησης ΔΣ",
        )
        @app_commands.describe(
            workflow_id="Workflow ID προς ακύρωση",
        )
        async def cmd_cancel(
            self,
            interaction: discord.Interaction,
            workflow_id: str,
        ) -> None:
            await interaction.response.defer(ephemeral=True, thinking=True)
            try:
                from src.core.audit import get_workflow_state, save_workflow_state
                from src.workflows.board_meeting_invitation import BoardMeetingInvitationWorkflow

                state = get_workflow_state(workflow_id)
                if not state:
                    embed = brand_embed(
                        title="Workflow δεν βρέθηκε",
                        description=f"Δεν βρέθηκε workflow με ID `{workflow_id}`.",
                        color=discord.Color.red(),
                    )
                    await interaction.followup.send(embed=embed, ephemeral=True)
                    return

                data = json.loads(state.get("data") or "{}")
                ctx = data.get("context") or {}
                current_status = state.get("state", "unknown")

                wf = BoardMeetingInvitationWorkflow()
                await wf.rollback(ctx)

                save_workflow_state(
                    workflow_name="board_meeting_invitation",
                    workflow_id=workflow_id,
                    state="cancelled",
                    data=data,
                )

                embed = brand_embed(
                    title="Workflow ακυρώθηκε",
                    description=(
                        f"Το workflow `{workflow_id}` ακυρώθηκε επιτυχώς.\n\n"
                        f"**Προηγούμενη κατάσταση:** `{current_status}`\n"
                        f"**Νέα κατάσταση:** `cancelled`\n\n"
                        "Τα side effects (Zoom, PDF, Brevo) έχουν αντιστραφεί."
                    ),
                    color=AMNESTY_YELLOW,
                )
                await interaction.followup.send(embed=embed, ephemeral=True)

            except Exception as exc:
                logger.exception("board cancel failed: %s", exc)
                await interaction.followup.send(f"❌ {exc}", ephemeral=True)

        # ── /board egkyklios subgroup ────────────────────────────────────────

        egkyklios = app_commands.Group(
            name="egkyklios",
            description="Διαχείριση εγκυκλίων ενημέρωσης (Συντονιστής + ΔΣ μόνο)",
        )

        @egkyklios.command(
            name="general-draft",
            description="Δημιουργία προσχεδίου Γενικής Εγκυκλίου για περίοδο",
        )
        @app_commands.describe(
            period_start="ISO date YYYY-MM-DD (default: προηγούμενο τρίμηνο)",
            period_end="ISO date YYYY-MM-DD (default: σήμερα)",
        )
        async def cmd_egkyklios_general_draft(
            self,
            interaction: discord.Interaction,
            period_start: str | None = None,
            period_end: str | None = None,
        ) -> None:
            # Only President role-pair may invoke (mirrors /board invite gate).
            if not _user_has_president_roles(interaction.user):
                await interaction.response.send_message(
                    "⛔ Αυτή η εντολή απαιτεί ταυτόχρονα τους ρόλους "
                    "**Συντονιστής** + **Διοικητικό Συμβούλιο** (Πρόεδρος).",
                    ephemeral=True,
                )
                return
            await interaction.response.defer(ephemeral=True, thinking=True)
            try:
                from src.workflows.egkyklios_general import EgkykliosGeneralWorkflow

                initial_data: dict[str, object] = {"test_mode": False}
                if period_start:
                    initial_data["period_start"] = period_start
                if period_end:
                    initial_data["period_end"] = period_end

                wf = EgkykliosGeneralWorkflow(actor=str(interaction.user.id))
                result = await wf.run(initial_data)
                ctx = wf.context or {}
                status = result.get("status", "?")
                draft_id = ctx.get("egkyklios_draft_id")

                embed = brand_embed(
                    title="Γενική Εγκύκλιος - Προσχέδιο",
                    description=(
                        f"**Κατάσταση:** `{status}`\n"
                        f"**Τίτλος:** {ctx.get('title', '-')}\n"
                        f"**Περίοδος:** {ctx.get('period_start', '?')} → {ctx.get('period_end', '?')}\n"
                        + (f"**Draft id:** `{draft_id}`\n" if draft_id else "")
                        + (
                            "\nΤο PDF στάλθηκε σε **ΔΣ + Διευθυντή** για έλεγχο. "
                            "Όταν είστε έτοιμοι, πατήστε **Έγκριση & Αποστολή** "
                            "παρακάτω, ή χρησιμοποιήστε `/board egkyklios general-approve`."
                            if status == "awaiting_approval" else ""
                        )
                    ),
                    color=AMNESTY_YELLOW,
                )
                view: discord.ui.View | None = None
                if status == "awaiting_approval" and draft_id:
                    view = _EgkykliosApproveView(draft_id=int(draft_id))
                await interaction.followup.send(embed=embed, view=view, ephemeral=True)
            except Exception as exc:
                logger.exception("/board egkyklios general-draft failed: %s", exc)
                await interaction.followup.send(f"❌ {exc}", ephemeral=True)

        @egkyklios.command(
            name="general-approve",
            description="Έγκριση & αποστολή ενός προσχεδίου που περιμένει",
        )
        @app_commands.describe(draft_id="Draft id από `/board egkyklios list`")
        async def cmd_egkyklios_general_approve(
            self,
            interaction: discord.Interaction,
            draft_id: int,
        ) -> None:
            if not _user_has_president_roles(interaction.user):
                await interaction.response.send_message(
                    "⛔ Αυτή η εντολή απαιτεί τους ρόλους Συντονιστής + ΔΣ.",
                    ephemeral=True,
                )
                return
            await interaction.response.defer(ephemeral=True, thinking=True)
            try:
                summary = await _approve_egkyklios_draft(draft_id)
                await interaction.followup.send(summary, ephemeral=True)
            except Exception as exc:
                logger.exception("/board egkyklios general-approve failed: %s", exc)
                await interaction.followup.send(f"❌ {exc}", ephemeral=True)

        @egkyklios.command(
            name="list",
            description="Προβολή πρόσφατων προσχεδίων Γενικής Εγκυκλίου",
        )
        async def cmd_egkyklios_list(
            self,
            interaction: discord.Interaction,
        ) -> None:
            await interaction.response.defer(ephemeral=True, thinking=True)
            try:
                from src.core.audit import list_egkyklios_drafts
                rows = list_egkyklios_drafts(kind="general", limit=10)
                if not rows:
                    await interaction.followup.send("(κανένα draft)", ephemeral=True)
                    return
                embed = brand_embed(
                    title="Γενικές Εγκύκλιοι - Πρόσφατα Προσχέδια",
                    color=AMNESTY_YELLOW,
                )
                for r in rows[:10]:
                    proto = f" - πρωτ. {r['protocol_number']}" if r.get("protocol_number") else ""
                    embed.add_field(
                        name=f"#{r['id']} - {r['status']}",
                        value=(
                            f"{r['period_start']} → {r['period_end']}\n"
                            f"{r.get('title', '-')}{proto}"
                        ),
                        inline=False,
                    )
                await interaction.followup.send(embed=embed, ephemeral=True)
            except Exception as exc:
                logger.exception("/board egkyklios list failed: %s", exc)
                await interaction.followup.send(f"❌ {exc}", ephemeral=True)

    # ── Cog lifecycle ────────────────────────────────────────────────────────

    def __init__(self, bot: commands.Bot) -> None:
        super().__init__()
        self.bot = bot
        self._commands = self._BoardCommands(cog=self)

    async def cog_load(self) -> None:
        self.bot.tree.add_command(self._commands)
        logger.info("BoardCog loaded - /board group registered")

    async def cog_unload(self) -> None:
        self.bot.tree.remove_command("board")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(BoardCog(bot))
