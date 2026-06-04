"""/archive cog — Discord-side archive workflow surface (B3).

Provides:
  • ``/archive submit file [title] [proto] [tags] [sender]``
        Runs the ArchiveWorkflow with the attached file.  Returns an embed
        with the πρωτόκολλο number plus two persistent buttons (Διόρθωση /
        Ακύρωση) that stay live for the 72h revision window.

  • Persistent View infrastructure
        Buttons stay clickable after a bot restart.  On ``on_ready`` we
        re-register each in-flight workflow's View by looking up
        ``workflow_state`` rows whose context still has ``revision_open_until``
        in the future.

Test-mode awareness:
  • Sender == ``settings.testing.test_email`` → forced test mode (skips
    SharePoint write).  Same behaviour as the email-route intake.
  • Admin's STATE_TEST_MODE_ACTIVE toggle also forces test mode for ALL
    submissions while ON.
"""
from __future__ import annotations

import asyncio
import json
import logging
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

from src.config import settings
from src.core.audit import get_workflow_state, log_action, save_workflow_state
from src.integrations.discord.brand import AMNESTY_YELLOW, brand_embed, fmt_ts
from src.integrations.discord.constants import STATE_TEST_MODE_ACTIVE
from src.integrations.discord.state import BotStateStore

logger = logging.getLogger(__name__)


# Custom_id prefixes — Discord uses these to route button clicks back to our
# View class even after a bot restart.  Format: ``archive:<action>:<wf_id>``.
_CUSTOM_ID_AMEND = "archive:amend"
_CUSTOM_ID_CANCEL = "archive:cancel"


def _is_board_member(member: discord.Member | discord.User | None) -> bool:
    """True if the member has the configured board role."""
    role_id = (settings.discord.platform_bridge.board_meeting.board_role_id or "").strip()
    if not role_id:
        # Not configured — fail OPEN during development, but log so SecGen
        # knows to set it.  In production this should be set.
        logger.warning(
            "_is_board_member: board_role_id not configured; allowing — "
            "set discord.platform_bridge.board_meeting.board_role_id in config.yaml",
        )
        return True
    if not isinstance(member, discord.Member):
        return False
    return any(str(r.id) == role_id for r in member.roles)


# ── Persistent View for Amend/Cancel buttons ──────────────────────────────────


class ArchiveActionView(discord.ui.View):
    """Persistent view with Διόρθωση + Ακύρωση buttons.

    Stays alive across bot restarts thanks to ``custom_id`` strings that
    encode the workflow_id.  On_ready re-registers these via ``bot.add_view``.
    """

    def __init__(self, workflow_id: str | None = None) -> None:
        super().__init__(timeout=None)
        # When workflow_id is provided we customize the custom_ids;
        # when None, we add generic ones and route via the custom_id
        # parser in the callbacks (used for persistent re-registration).
        wf = workflow_id or "{wf}"
        self.workflow_id = workflow_id
        # Buttons added dynamically so the custom_id carries the wf id
        amend_btn = discord.ui.Button(
            style=discord.ButtonStyle.primary,
            label="Διόρθωση",
            emoji="✏️",
            custom_id=f"{_CUSTOM_ID_AMEND}:{wf}",
        )
        amend_btn.callback = self._on_amend
        cancel_btn = discord.ui.Button(
            style=discord.ButtonStyle.danger,
            label="Ακύρωση",
            emoji="🗑️",
            custom_id=f"{_CUSTOM_ID_CANCEL}:{wf}",
        )
        cancel_btn.callback = self._on_cancel
        self.add_item(amend_btn)
        self.add_item(cancel_btn)

    @staticmethod
    def _extract_workflow_id(interaction: discord.Interaction) -> str:
        """Pull the workflow_id back out of the clicked button's custom_id."""
        data = interaction.data or {}
        custom_id = data.get("custom_id", "")
        parts = custom_id.split(":", 2)
        return parts[2] if len(parts) >= 3 else ""

    async def _on_amend(self, interaction: discord.Interaction) -> None:
        workflow_id = self._extract_workflow_id(interaction)
        if not _is_board_member(interaction.user):
            await interaction.response.send_message(
                "Δεν επιτρέπεται. Μόνο μέλη ΔΣ μπορούν να αναθεωρήσουν αρχειοθετήσεις.",
                ephemeral=True,
            )
            return

        # Load the current state from workflow_state so the modal can pre-fill
        state = get_workflow_state(workflow_id)
        if not state:
            await interaction.response.send_message(
                f"Workflow `{workflow_id}` δεν βρέθηκε.", ephemeral=True,
            )
            return
        data = json.loads(state.get("data") or "{}")
        ctx = data.get("context") or {}
        llm = ctx.get("llm_result") or {}

        modal = AmendArchiveModal(
            workflow_id=workflow_id,
            current_title=llm.get("title", ""),
            current_labels=", ".join(llm.get("labels") or []),
            current_key_points=llm.get("key_points", ""),
        )
        await interaction.response.send_modal(modal)

    async def _on_cancel(self, interaction: discord.Interaction) -> None:
        workflow_id = self._extract_workflow_id(interaction)
        if not _is_board_member(interaction.user):
            await interaction.response.send_message(
                "Δεν επιτρέπεται. Μόνο μέλη ΔΣ μπορούν να ακυρώσουν αρχειοθετήσεις.",
                ephemeral=True,
            )
            return
        # Confirmation step via a second view ("Are you sure?")
        confirm_view = _ConfirmCancelView(workflow_id=workflow_id)
        await interaction.response.send_message(
            f"⚠️ Σίγουρα θέλεις να ακυρώσεις την αρχειοθέτηση `{workflow_id}`; "
            "Θα διαγραφεί από το πρωτόκολλο και το SharePoint.",
            view=confirm_view,
            ephemeral=True,
        )


class _ConfirmCancelView(discord.ui.View):
    """Ephemeral confirmation buttons for the destructive cancel action."""

    def __init__(self, workflow_id: str) -> None:
        super().__init__(timeout=60)
        self.workflow_id = workflow_id

    @discord.ui.button(label="Ναι, ακύρωση", style=discord.ButtonStyle.danger, emoji="🗑️")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        """Confirm cancellation.

        Discord interactions time out at 3 seconds.  The rollback below can
        take 60s+ when it talks to SharePoint, so we MUST defer the response
        before doing any I/O — otherwise the user sees "This interaction
        failed" AND Discord may re-fire the button (causing duplicate
        rollbacks, which was a real production incident on 2026-05-27).
        """
        from src.workflows.archive import ArchiveWorkflow

        # Defer immediately so the interaction stays alive.  ephemeral=True
        # because the original confirm prompt was already ephemeral.
        try:
            await interaction.response.defer(ephemeral=True)
        except discord.errors.InteractionResponded:
            # Already deferred (re-fire from a stale view) — bail to avoid
            # duplicate side-effects.  See production incident 2026-05-27.
            logger.warning(
                "Cancel confirm fired twice for workflow %s — ignoring re-fire",
                self.workflow_id,
            )
            return

        # Disable the buttons IMMEDIATELY so a click-spammer can't re-fire
        # while rollback is running.
        for child in self.children:
            child.disabled = True
        try:
            await interaction.edit_original_response(view=self)
        except Exception:  # pragma: no cover — cosmetic
            pass

        state = get_workflow_state(self.workflow_id)
        if not state:
            await interaction.followup.send(
                f"Workflow `{self.workflow_id}` δεν βρέθηκε.", ephemeral=True,
            )
            return
        data = json.loads(state.get("data") or "{}")
        ctx = data.get("context") or {}
        wf = ArchiveWorkflow()
        wf.workflow_id = self.workflow_id
        try:
            await wf.rollback(ctx)
            save_workflow_state(
                workflow_name="archive",
                workflow_id=self.workflow_id,
                state="cancelled",
                data=data,
            )
            log_action(
                workflow="archive",
                action="discord_cancel",
                actor=str(interaction.user.id),
                target=self.workflow_id,
            )
            test_banner = " (TEST MODE — no SharePoint changes)" if ctx.get("test_mode") else ""
            await interaction.followup.send(
                f"✅ Η αρχειοθέτηση `{self.workflow_id}` ακυρώθηκε{test_banner}.",
                ephemeral=True,
            )
        except Exception as exc:
            logger.exception("Cancel rollback failed: %s", exc)
            await interaction.followup.send(
                f"❌ Αποτυχία ακύρωσης: {exc}", ephemeral=True,
            )

    @discord.ui.button(label="Όχι, πίσω", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.edit_message(content="Ακυρώθηκε η ακύρωση.", view=None)


class AmendArchiveModal(discord.ui.Modal):
    """Modal that captures title / labels / key_points overrides."""

    def __init__(
        self,
        *,
        workflow_id: str,
        current_title: str = "",
        current_labels: str = "",
        current_key_points: str = "",
    ) -> None:
        super().__init__(title="Διόρθωση Αρχειοθέτησης", timeout=600)
        self.workflow_id = workflow_id

        self.new_title = discord.ui.TextInput(
            label="Τίτλος",
            default=current_title[:200],
            max_length=200,
            required=False,
            placeholder="Αφήστε κενό για να μη διορθωθεί",
        )
        self.new_labels = discord.ui.TextInput(
            label="Ετικέτες (comma-separated)",
            default=current_labels[:200],
            max_length=200,
            required=False,
            placeholder="π.χ. Διοικητικά, Πρακτικά",
        )
        self.new_key_points = discord.ui.TextInput(
            label="Κύρια Σημεία",
            default=current_key_points[:500],
            max_length=500,
            required=False,
            style=discord.TextStyle.paragraph,
            placeholder="Σύντομη περίληψη/σημεία",
        )
        self.add_item(self.new_title)
        self.add_item(self.new_labels)
        self.add_item(self.new_key_points)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        from src.workflows.archive import apply_amendments

        state = get_workflow_state(self.workflow_id)
        if not state:
            await interaction.response.send_message(
                f"Workflow `{self.workflow_id}` δεν βρέθηκε.", ephemeral=True,
            )
            return
        data = json.loads(state.get("data") or "{}")
        ctx = data.get("context") or {}

        # Build amendments dict — only include fields that actually changed
        amendments: dict = {}
        new_t = self.new_title.value.strip()
        if new_t and new_t != (ctx.get("llm_result") or {}).get("title", ""):
            amendments["title"] = new_t
        labels_raw = self.new_labels.value.strip()
        if labels_raw:
            new_labels = [t.strip() for t in labels_raw.split(",") if t.strip()]
            if new_labels != (ctx.get("llm_result") or {}).get("labels", []):
                amendments["labels"] = new_labels
        new_kp = self.new_key_points.value.strip()
        if new_kp != (ctx.get("llm_result") or {}).get("key_points", ""):
            amendments["key_points"] = new_kp

        if not amendments:
            await interaction.response.send_message(
                "Καμία αλλαγή ανιχνεύθηκε — άφησες ό,τι ήταν.", ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        summary = await apply_amendments(self.workflow_id, ctx, amendments)
        data["context"] = ctx
        save_workflow_state(
            workflow_name="archive",
            workflow_id=self.workflow_id,
            state=state.get("state", "completed"),
            data=data,
        )
        log_action(
            workflow="archive",
            action="discord_amend",
            actor=str(interaction.user.id),
            target=self.workflow_id,
            details={"applied": summary.get("applied", [])},
        )
        applied_fields = ", ".join(summary.get("applied", []))
        msg = f"✅ Εφαρμόστηκαν οι διορθώσεις: **{applied_fields or '(καμία)'}**"
        if summary.get("renamed_to"):
            msg += f"\nFile: `{summary['renamed_to']}`"
        await interaction.followup.send(msg, ephemeral=True)


# ── /archive cog ──────────────────────────────────────────────────────────────


class ArchiveCog(commands.Cog):
    """`/archive` slash command group — board-only PDF submissions."""

    class _ArchiveCommands(app_commands.Group):
        def __init__(self, cog: "ArchiveCog") -> None:
            super().__init__(
                name="archive",
                description="Αρχειοθέτηση εγγράφων στο πρωτόκολλο ΔΣ",
            )
            self.cog = cog

        @app_commands.command(name="submit", description="Υποβολή PDF στο πρωτόκολλο")
        @app_commands.describe(
            file="Το έγγραφο προς αρχειοθέτηση (PDF, DOCX, ODT, RTF, JPG/PNG)",
            title="Παρακάμπτει τον τίτλο που θα προτείνει το LLM",
            proto="Χειροκίνητος αρ. πρωτ. (YYYY_NNN) — αλλιώς θα δεσμευτεί ο επόμενος",
            tags="Ετικέτες χωρισμένες με κόμμα",
            sender="Επιπλέον info για τον αποστολέα (default: ο χρήστης Discord)",
        )
        async def cmd_submit(
            self,
            interaction: discord.Interaction,
            file: discord.Attachment,
            title: str | None = None,
            proto: str | None = None,
            tags: str | None = None,
            sender: str | None = None,
        ) -> None:
            # Board-role gate
            if not _is_board_member(interaction.user):
                await interaction.response.send_message(
                    "Μόνο μέλη ΔΣ μπορούν να αρχειοθετήσουν έγγραφα.",
                    ephemeral=True,
                )
                return

            await interaction.response.defer(ephemeral=True, thinking=True)

            # Download the attachment to a temp dir
            tmpdir = Path(tempfile.mkdtemp(prefix="discord_archive_"))
            dest = tmpdir / file.filename
            try:
                await file.save(dest)
            except Exception as exc:
                await interaction.followup.send(
                    f"❌ Αποτυχία λήψης συνημμένου: {exc}", ephemeral=True,
                )
                return

            # Determine test_mode (admin toggle OR test-email sender match)
            state_store = BotStateStore()
            admin_test_mode = await state_store.get_bool(STATE_TEST_MODE_ACTIVE, default=False)
            sender_email_str = sender or f"discord:{interaction.user.id}"

            initial_data: dict = {
                "pdf_path": str(dest.resolve()),
                "sender_email": sender_email_str,
                "sender_name": getattr(interaction.user, "display_name", ""),
                "test_mode": admin_test_mode,
                "_source": "discord",
                "_discord_user_id": str(interaction.user.id),
                "_discord_channel_id": str(interaction.channel_id or ""),
            }
            if title:
                initial_data["override_title"] = title
            if proto:
                initial_data["override_protocol"] = proto
            if tags:
                initial_data["override_labels"] = [
                    t.strip() for t in tags.split(",") if t.strip()
                ]

            from src.workflows.archive import ArchiveWorkflow
            wf = ArchiveWorkflow(actor=f"discord:{interaction.user.id}")

            try:
                result = await wf.run(initial_data)
            except Exception as exc:
                logger.exception("Discord /archive submit failed: %s", exc)
                await interaction.followup.send(
                    f"❌ Σφάλμα κατά την αρχειοθέτηση: {exc}", ephemeral=True,
                )
                return

            status = result.get("status")
            ctx = wf.context

            if status != "completed":
                pending = ctx.get("pending_reservation_confirmation")
                if pending:
                    await interaction.followup.send(
                        f"📥 Ο αρ.πρωτ. `{pending.get('protocol_number')}` είναι "
                        f"δεσμευμένος από τον/τη Γραμματέα και ο τίτλος του "
                        f"υποβληθέντος αρχείου δεν ταιριάζει σίγουρα. "
                        f"Το αίτημα έχει σταλεί στον/στη ΓΓ για επιβεβαίωση. "
                        f"Θα ενημερωθείς όταν αποφασιστεί.",
                        ephemeral=True,
                    )
                else:
                    await interaction.followup.send(
                        f"❌ Η αρχειοθέτηση δεν ολοκληρώθηκε. Status: `{status}`. "
                        f"Σφάλμα: {result.get('error', 'unknown')}",
                        ephemeral=True,
                    )
                return

            # Success — build the confirmation embed + persistent buttons
            llm = ctx.get("llm_result") or {}
            test_banner = ""
            if admin_test_mode:
                test_banner = "**[TEST MODE — δεν έγινε πραγματική αρχειοθέτηση]**\n\n"

            embed = brand_embed(
                title="Αρχειοθέτηση Ολοκληρώθηκε",
                description=(
                    f"{test_banner}"
                    f"Το έγγραφό σας αρχειοθετήθηκε στο πρωτόκολλο ΔΣ."
                ),
                color=AMNESTY_YELLOW,
            )
            embed.add_field(name="Αρ. Πρωτ.", value=f"`{ctx.get('protocol_number', '?')}`", inline=True)
            embed.add_field(name="Τίτλος", value=llm.get("title", "?"), inline=True)
            embed.add_field(
                name="Ετικέτες",
                value=", ".join(llm.get("labels", [])) or "—",
                inline=False,
            )
            kp = llm.get("key_points", "")
            if kp:
                embed.add_field(name="Κύρια Σημεία", value=kp[:1024], inline=False)
            revision_until = ctx.get("revision_open_until", "")
            if revision_until:
                try:
                    deadline = datetime.fromisoformat(revision_until)
                    embed.add_field(
                        name="Παράθυρο Αναθεώρησης",
                        value=f"έως {fmt_ts(deadline, 'R')}",
                        inline=False,
                    )
                except Exception:
                    pass
            embed.set_footer(text=f"Workflow ID: {wf.workflow_id}")

            view = ArchiveActionView(workflow_id=wf.workflow_id)

            # Track for persistent view re-registration on reboot
            try:
                from src.integrations.discord.scheduler import WorkflowResourcesStore
                resources = WorkflowResourcesStore()
                await resources.record(
                    workflow_id=wf.workflow_id,
                    resource_type="archive_view",
                    discord_id=wf.workflow_id,  # we re-register by workflow_id
                )
            except Exception as exc:  # pragma: no cover
                logger.debug("Could not record archive view for re-registration: %s", exc)

            await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    # ── Lifecycle ──────────────────────────────────────────────────────────

    def __init__(self, bot: commands.Bot) -> None:
        super().__init__()
        self.bot = bot
        self._commands = self._ArchiveCommands(cog=self)

    async def cog_load(self) -> None:
        self.bot.tree.add_command(self._commands)
        # Re-register persistent views for in-flight archive workflows so
        # buttons attached to historical messages keep working after reboot.
        try:
            from src.core.audit import _get_connection
            conn = _get_connection()
            rows = conn.execute(
                """SELECT workflow_id, data FROM workflow_state
                   WHERE workflow_name = 'archive'
                     AND state NOT IN ('cancelled', 'failed', 'failed_collision_timeout')
                     AND datetime('now') < datetime(json_extract(data, '$.context.revision_open_until'))""",
            ).fetchall()
            for row in rows:
                self.bot.add_view(ArchiveActionView(workflow_id=row["workflow_id"]))
            logger.info(
                "ArchiveCog: re-registered %d persistent archive view(s)", len(rows),
            )
        except Exception as exc:  # pragma: no cover — best-effort
            logger.warning("ArchiveCog: persistent view re-registration failed: %s", exc)

        # Reservation-confirmation events (renamed from old "collision" flow on
        # 2026-05-27).  Fires when the bot needs SecGen to confirm whether a
        # submitted file matches a SecGen-pre-reserved πρωτόκολλο slot.
        from src.core.event_bus import bus
        bus.subscribe(
            "archive.reservation_confirmation_needed",
            self._on_reservation_confirmation_needed,
        )

        logger.info("ArchiveCog loaded — /archive group registered")

    async def cog_unload(self) -> None:
        self.bot.tree.remove_command("archive")
        try:
            from src.core.event_bus import bus
            bus.unsubscribe(
                "archive.reservation_confirmation_needed",
                self._on_reservation_confirmation_needed,
            )
        except Exception:
            pass

    async def _on_reservation_confirmation_needed(self, payload: dict) -> None:
        """DM SecGen with side-by-side embed + Approve/Reject buttons.

        Triggered when the bot detects a SecGen pre-reservation (row exists,
        no file yet) but the submitted document's title doesn't confidently
        match the reserved row's title — defer to SecGen to confirm.
        """
        secgen_user_id = _resolve_secgen_user_id(self.bot)
        if not secgen_user_id:
            logger.warning(
                "Reservation-confirm DM skipped: no SecGen user resolved. "
                "Configure workflows.board_meeting.board_members with a secgen entry."
            )
            return
        try:
            secgen = await self.bot.fetch_user(secgen_user_id)
        except Exception as exc:
            logger.warning("Could not fetch SecGen user %s: %s", secgen_user_id, exc)
            return

        workflow_id = payload["workflow_id"]
        match_conf = float(payload.get("match_confidence") or 0.0)
        embed = brand_embed(
            title="📥 Επιβεβαίωση Δεσμευμένου Αρ. Πρωτοκόλλου",
            description=(
                f"Έχει υποβληθεί αρχείο για τον αρ.πρωτ. "
                f"`{payload.get('protocol_number')}` που έχεις δεσμεύσει — "
                f"όμως ο τίτλος του αρχείου δεν ταιριάζει σίγουρα με τον "
                f"τίτλο της εγγραφής (match confidence {match_conf:.2f}). "
                f"Παρακαλώ επιβεβαίωσε αν είναι το σωστό έγγραφο."
            ),
            color=AMNESTY_YELLOW,
        )
        embed.add_field(
            name="Τίτλος της εγγραφής (δικός σου)",
            value=payload.get("existing_title", "—")[:1024],
            inline=False,
        )
        embed.add_field(
            name="Τίτλος του υποβληθέντος αρχείου",
            value=payload.get("proposed_title", "—")[:1024],
            inline=False,
        )
        embed.set_footer(text=f"Workflow ID: {workflow_id}")

        view = ReservationConfirmView(workflow_id=workflow_id)
        try:
            dm = await secgen.create_dm()
            await dm.send(embed=embed, view=view)
            logger.info(
                "Reservation-confirm DM sent to SecGen %s for workflow %s",
                secgen.id, workflow_id,
            )
        except discord.Forbidden:
            logger.warning(
                "Could not DM SecGen %s (DMs disabled).  Falling back to CLI: "
                "ai-assistant archive resolve %s approve|reject",
                secgen.id, workflow_id,
            )
        except Exception as exc:
            logger.warning("Reservation-confirm DM failed (non-fatal): %s", exc)


def _resolve_secgen_user_id(bot: commands.Bot) -> int | None:
    """Resolve the SecGen's Discord user id.

    Strategy: look for a guild member whose Discord username or display_name
    contains 'secgen' OR who carries the board role and whose email matches
    settings.workflows.board_meeting.board_members entry with email containing
    'secgen@amnesty.org.gr'.

    Discord doesn't expose member emails so we fall back to display_name/global_name
    contains 'γραμματ' (Greek for secretary).  Override by setting a hard-coded
    DISCORD_SECGEN_USER_ID in .env if needed (not yet wired).
    """
    # Phase B4 stub — for now just probe by display_name matching.  Hardening
    # against the role + a config key lands in a follow-up.
    guild_id = settings.discord_guild_id
    if not guild_id:
        return None
    guild = bot.get_guild(int(guild_id))
    if guild is None:
        return None
    for m in guild.members:
        name = (getattr(m, "display_name", "") or "").lower()
        if "γραμματ" in name or "secgen" in name or "γγ" in name:
            return m.id
    return None


class ReservationConfirmView(discord.ui.View):
    """SecGen DM buttons: confirm/reject filling a reserved πρωτόκολλο slot.

    Renamed from CollisionResolveView on 2026-05-27 — the old collision-
    approval flow was removed (the bot never overwrites archived files).
    This view is now used only when a SecGen pre-reservation has a title
    that doesn't confidently match the submitted document.
    """

    def __init__(self, workflow_id: str) -> None:
        super().__init__(timeout=86400)  # 24h before falling back to CLI
        self.workflow_id = workflow_id

    @discord.ui.button(
        label="Ναι, είναι το σωστό έγγραφο",
        style=discord.ButtonStyle.success, emoji="✅",
    )
    async def approve(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        from src.workflows.archive import ArchiveWorkflow

        await interaction.response.defer(ephemeral=True, thinking=True)
        state = get_workflow_state(self.workflow_id)
        if not state:
            await interaction.followup.send(f"Workflow `{self.workflow_id}` δεν βρέθηκε.", ephemeral=True)
            return
        data = json.loads(state.get("data") or "{}")
        ctx = data.get("context") or {}

        # Convert the pending confirmation into an active reservation-fill.
        pending = ctx.pop("pending_reservation_confirmation", None) or {}
        ctx["is_filling_reservation"] = True
        ctx["reserved_row"] = pending.get("existing_row") or ctx.get("reserved_row")
        ctx["_start_at_step"] = "upload_and_register"

        wf = ArchiveWorkflow(actor=f"discord:{interaction.user.id}")
        wf.workflow_id = self.workflow_id
        try:
            result = await wf.run(ctx)
        except Exception as exc:
            await interaction.followup.send(f"❌ Αποτυχία: {exc}", ephemeral=True)
            return

        log_action(
            workflow="archive",
            action="reservation_confirm_approved",
            actor=str(interaction.user.id),
            target=self.workflow_id,
            details={"via": "discord_dm"},
        )
        if result.get("status") == "completed":
            await interaction.followup.send(
                f"✅ Επιβεβαιώθηκε — αρχειοθέτηση `{self.workflow_id}` ολοκληρώθηκε.",
                ephemeral=True,
            )
            for child in self.children:
                child.disabled = True
            try:
                await interaction.message.edit(view=self)  # type: ignore[union-attr]
            except Exception:
                pass
        else:
            await interaction.followup.send(
                f"⚠️ Επιβεβαίωση δόθηκε αλλά η αρχειοθέτηση δεν ολοκληρώθηκε καθαρά: "
                f"{result.get('status')} — {result.get('error', '')}",
                ephemeral=True,
            )

    @discord.ui.button(
        label="Όχι, δεν ταιριάζει — ακύρωση",
        style=discord.ButtonStyle.danger, emoji="❌",
    )
    async def reject(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        from src.workflows.archive import ArchiveWorkflow

        await interaction.response.defer(ephemeral=True, thinking=True)
        state = get_workflow_state(self.workflow_id)
        if not state:
            await interaction.followup.send(f"Workflow `{self.workflow_id}` δεν βρέθηκε.", ephemeral=True)
            return
        data = json.loads(state.get("data") or "{}")
        ctx = data.get("context") or {}
        wf = ArchiveWorkflow(actor=f"discord:{interaction.user.id}")
        wf.workflow_id = self.workflow_id
        try:
            await wf.rollback(ctx)
            save_workflow_state(
                workflow_name="archive",
                workflow_id=self.workflow_id,
                state="cancelled",
                data=data,
            )
        except Exception as exc:
            await interaction.followup.send(f"❌ Αποτυχία rollback: {exc}", ephemeral=True)
            return

        log_action(
            workflow="archive",
            action="reservation_confirm_rejected",
            actor=str(interaction.user.id),
            target=self.workflow_id,
            details={"via": "discord_dm"},
        )
        await interaction.followup.send(
            f"❌ Απορρίφθηκε — η αρχειοθέτηση `{self.workflow_id}` ακυρώθηκε.",
            ephemeral=True,
        )
        for child in self.children:
            child.disabled = True
        try:
            await interaction.message.edit(view=self)  # type: ignore[union-attr]
        except Exception:
            pass


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ArchiveCog(bot))
