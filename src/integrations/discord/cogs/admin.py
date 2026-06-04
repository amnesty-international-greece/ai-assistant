"""/admin cog — SecGen operations panel (admin-only Discord slash commands).

Wraps the CLI's data layer so the SecGen can run ops checks from Discord
without SSH-ing to the host.  Every response is ephemeral; every command
body is wrapped in a try/except that sends a terse error message on failure.

Command tree::

    /admin audit       workflow:str  limit:int
    /admin archive list
    /admin archive cancel    workflow-id:str
    /admin minutes    list-drafts
    /admin m365       subscriptions
    /admin m365       renew-now
    /admin m365       poll-now
    /admin onedrive   backup-status
    /admin onedrive   ls           path:str
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

from src.integrations.discord.brand import AMNESTY_YELLOW, brand_embed, fmt_ts

logger = logging.getLogger(__name__)

# Discord embed field value cap
_FIELD_CAP = 1024
# Max onedrive ls entries shown without truncation
_LS_LIMIT = 20
# Default / max audit-log limit
_AUDIT_DEFAULT = 25
_AUDIT_MAX = 100


# ── Subgroups ────────────────────────────────────────────────────────────────


class _ArchiveCommands(app_commands.Group):
    """Subgroup: /admin archive …"""

    def __init__(self) -> None:
        super().__init__(name="archive", description="Archive workflow management")

    @app_commands.command(name="list", description="List in-progress + recent archive workflows")
    async def cmd_list(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
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

            embed = brand_embed(
                title=f"Archive Workflows — {len(rows)} αποτελέσματα",
                color=AMNESTY_YELLOW,
            )

            if not rows:
                embed.add_field(
                    name="(κανένα αποτέλεσμα)",
                    value="Δεν βρέθηκαν archive workflows τις τελευταίες 30 μέρες.",
                    inline=False,
                )
            else:
                for row in rows[:20]:
                    data = json.loads(row["data"] or "{}")
                    ctx = data.get("context") or {}
                    proto = ctx.get("protocol_number", "—")
                    revision = "ανοιχτή" if is_revision_window_open(ctx) else "—"
                    updated = (row["updated_at"] or "")[:19]
                    embed.add_field(
                        name=f"{row['workflow_id'][:8]} • {row['state']}",
                        value=f"started: {updated} • πρωτ: {proto} • revision: {revision}",
                        inline=False,
                    )

            await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception as exc:
            logger.exception("admin archive list failed: %s", exc)
            await interaction.followup.send(f"❌ {exc}", ephemeral=True)

    @app_commands.command(
        name="cancel",
        description="Cancel an archive workflow and roll back side effects",
    )
    @app_commands.describe(workflow_id="Workflow ID to cancel (from /admin archive list)")
    async def cmd_cancel(
        self,
        interaction: discord.Interaction,
        workflow_id: str,
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            from src.core.audit import get_workflow_state, save_workflow_state
            from src.workflows.archive import ArchiveWorkflow

            state = get_workflow_state(workflow_id)
            if not state:
                await interaction.followup.send(
                    f"❌ Workflow `{workflow_id}` δεν βρέθηκε.", ephemeral=True
                )
                return
            if state.get("workflow_name") != "archive":
                await interaction.followup.send(
                    f"❌ Το `{workflow_id}` δεν είναι archive workflow "
                    f"(είναι: {state.get('workflow_name')}).",
                    ephemeral=True,
                )
                return

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

            embed = brand_embed(
                title="Archive Workflow — Ακυρώθηκε",
                description=f"Το workflow `{workflow_id[:8]}` ακυρώθηκε και έγινε rollback.",
                color=AMNESTY_YELLOW,
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception as exc:
            logger.exception("admin archive cancel failed: %s", exc)
            await interaction.followup.send(f"❌ {exc}", ephemeral=True)


class _MinutesCommands(app_commands.Group):
    """Subgroup: /admin minutes …"""

    def __init__(self) -> None:
        super().__init__(name="minutes", description="Board meeting minutes")

    @app_commands.command(
        name="list-drafts",
        description="List draft minutes currently in Google Drive",
    )
    async def cmd_list_drafts(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            from src.config import settings
            from src.integrations.google_drive import GoogleClient

            folder_id = settings.google.minutes_drafts_folder_id
            if not folder_id:
                await interaction.followup.send(
                    "❌ `google.minutes_drafts_folder_id` δεν έχει ρυθμιστεί στο config.yaml.",
                    ephemeral=True,
                )
                return

            google = GoogleClient()
            docs = google.list_docs_in_folder(folder_id)

            embed = brand_embed(
                title=f"Draft Minutes — {len(docs)} έγγραφα",
                color=AMNESTY_YELLOW,
            )
            if not docs:
                embed.add_field(
                    name="(κανένα)",
                    value="Δεν βρέθηκαν draft minutes στον φάκελο.",
                    inline=False,
                )
            else:
                for doc in docs[:20]:
                    mod_date = doc.get("modifiedTime", "")[:10]
                    name = doc.get("name", "(άγνωστο)")
                    embed.add_field(
                        name=name[:100],
                        value=f"Τροποποιήθηκε: {mod_date}",
                        inline=False,
                    )
            await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception as exc:
            logger.exception("admin minutes list-drafts failed: %s", exc)
            await interaction.followup.send(f"❌ {exc}", ephemeral=True)


class _M365Commands(app_commands.Group):
    """Subgroup: /admin m365 …"""

    def __init__(self) -> None:
        super().__init__(name="m365", description="Microsoft 365 / Graph subscriptions")

    @app_commands.command(
        name="subscriptions",
        description="List active Graph webhook subscriptions (local DB + remote)",
    )
    async def cmd_subscriptions(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            from src.core.audit import get_active_graph_subscriptions
            from src.integrations.graph_subscriptions import GraphSubscriptionsClient

            local = get_active_graph_subscriptions()

            try:
                client = GraphSubscriptionsClient()
                remote = await client.list_remote()
            except Exception as e:
                remote = None
                remote_err = str(e)
            else:
                remote_err = None

            embed = brand_embed(
                title="M365 Graph Subscriptions",
                color=AMNESTY_YELLOW,
                description=f"Τοπικές (DB): **{len(local)}**",
            )

            if not local:
                embed.add_field(
                    name="(κανένα local)",
                    value="Δεν υπάρχουν active subscriptions στη βάση.",
                    inline=False,
                )
            else:
                for row in local:
                    exp_str = row.get("expiration_date_time", "")
                    try:
                        exp_dt = datetime.fromisoformat(exp_str.replace("Z", "+00:00"))
                        exp_display = fmt_ts(exp_dt, "R")
                    except (ValueError, AttributeError):
                        exp_display = exp_str[:19] if exp_str else "—"

                    embed.add_field(
                        name=f"`{row['subscription_id'][:20]}…`",
                        value=(
                            f"resource: `{row.get('resource', '—')[:60]}`\n"
                            f"λήγει: {exp_display}"
                        ),
                        inline=False,
                    )

            if remote is not None:
                embed.add_field(
                    name=f"Remote (Graph): {len(remote)} sub(s)",
                    value="\n".join(
                        f"`{s.get('id', '')[:20]}…` expires={s.get('expirationDateTime', '?')[:19]}"
                        for s in remote[:5]
                    ) or "—",
                    inline=False,
                )
            else:
                embed.add_field(
                    name="Remote (Graph)",
                    value=f"❌ Σφάλμα: {remote_err}",
                    inline=False,
                )

            await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception as exc:
            logger.exception("admin m365 subscriptions failed: %s", exc)
            await interaction.followup.send(f"❌ {exc}", ephemeral=True)

    @app_commands.command(
        name="renew-now",
        description="Renew any expiring Graph webhook subscriptions immediately",
    )
    async def cmd_renew_now(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            from src.integrations.graph_subscriptions import GraphSubscriptionsClient

            client = GraphSubscriptionsClient()
            renewed = await client.renew_expiring()

            if renewed:
                desc = f"Ανανεώθηκαν **{len(renewed)}** subscription(s):\n" + "\n".join(
                    f"• `{sid[:20]}…`" for sid in renewed
                )
            else:
                desc = "Δεν χρειάζεται ανανέωση — όλα τα subscriptions είναι ενεργά."

            embed = brand_embed(
                title="M365 — Ανανέωση Subscriptions",
                description=desc,
                color=AMNESTY_YELLOW,
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception as exc:
            logger.exception("admin m365 renew-now failed: %s", exc)
            await interaction.followup.send(f"❌ {exc}", ephemeral=True)

    @app_commands.command(
        name="poll-now",
        description="Run the M365 inbox safety poll once, now",
    )
    async def cmd_poll_now(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            from src.workflows.email_intake import run_safety_poll

            result = await run_safety_poll()
            processed = result.get("processed", 0)
            by_outcome = result.get("by_outcome") or {}

            lines = [f"Επεξεργάστηκαν: **{processed}** μηνύματα"]
            for outcome, count in by_outcome.items():
                lines.append(f"• {outcome}: {count}")
            if result.get("error"):
                lines.append(f"❌ Σφάλμα: {result['error']}")

            embed = brand_embed(
                title="M365 Safety Poll — Αποτέλεσμα",
                description="\n".join(lines),
                color=AMNESTY_YELLOW,
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception as exc:
            logger.exception("admin m365 poll-now failed: %s", exc)
            await interaction.followup.send(f"❌ {exc}", ephemeral=True)


class _OneDriveCommands(app_commands.Group):
    """Subgroup: /admin onedrive …"""

    def __init__(self) -> None:
        super().__init__(name="onedrive", description="OneDrive / SharePoint utilities")

    @app_commands.command(
        name="backup-status",
        description="Show the local πρωτόκολλο safety backup info",
    )
    async def cmd_backup_status(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            from src.integrations.onedrive import OneDriveClient

            backup_path = OneDriveClient.PROTOCOL_BACKUP_PATH

            embed = brand_embed(
                title="Πρωτόκολλο — Local Safety Backup",
                color=AMNESTY_YELLOW,
            )

            if not backup_path.exists():
                embed.add_field(
                    name="Κατάσταση",
                    value="**ΔΕΝ ΥΠΑΡΧΕΙ** — τρέξτε ένα archive workflow για να δημιουργηθεί.",
                    inline=False,
                )
                embed.add_field(
                    name="Διαδρομή",
                    value=f"`{backup_path.resolve()}`",
                    inline=False,
                )
                await interaction.followup.send(embed=embed, ephemeral=True)
                return

            stat = backup_path.stat()
            mtime_dt = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
            size_kb = stat.st_size / 1024

            embed.add_field(
                name="Διαδρομή",
                value=f"`{backup_path.resolve()}`",
                inline=False,
            )
            embed.add_field(
                name="Μέγεθος",
                value=f"{size_kb:.1f} KB",
                inline=True,
            )
            embed.add_field(
                name="Ενημερώθηκε",
                value=fmt_ts(mtime_dt, "R"),
                inline=True,
            )

            # Verify xlsx validity
            try:
                import openpyxl
                wb = openpyxl.load_workbook(backup_path, data_only=True, read_only=True)
                tabs = wb.sheetnames
                wb.close()
                validity = f"✅ VALID — tabs: {', '.join(tabs[:5])}"
            except Exception as xlsx_err:
                validity = f"❌ CORRUPT? — {xlsx_err}"

            embed.add_field(
                name="Κατάσταση",
                value=validity,
                inline=False,
            )

            await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception as exc:
            logger.exception("admin onedrive backup-status failed: %s", exc)
            await interaction.followup.send(f"❌ {exc}", ephemeral=True)

    @app_commands.command(
        name="ls",
        description="List files/folders under the OneDrive/SharePoint archive root (or a sub-path)",
    )
    @app_commands.describe(path="Sub-path relative to archive root (default: root)")
    async def cmd_ls(
        self,
        interaction: discord.Interaction,
        path: str = "",
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            from src.integrations.onedrive import OneDriveClient

            client = OneDriveClient()
            items = await client.list_files(path)

            shown = items[:_LS_LIMIT]
            overflow = len(items) - _LS_LIMIT if len(items) > _LS_LIMIT else 0

            embed = brand_embed(
                title=f"OneDrive ls — /{path.strip('/') or '(root)'}",
                description=f"{len(items)} αντικείμενα{'  (+{} more — refine path:)'.format(overflow) if overflow else ''}",
                color=AMNESTY_YELLOW,
            )

            if not items:
                embed.add_field(
                    name="(κενός φάκελος)",
                    value="—",
                    inline=False,
                )
            else:
                # Build a single compact field; stays under 1024 with 20 lines
                lines = []
                for item in shown:
                    is_folder = "folder" in item
                    icon = "📁" if is_folder else "📄"
                    name = item.get("name", "(άγνωστο)")
                    size = item.get("size", 0)
                    size_str = "" if is_folder else f" ({size:,} B)"
                    lines.append(f"{icon} {name}{size_str}")

                value = "\n".join(lines)
                if len(value) > _FIELD_CAP:
                    value = value[: _FIELD_CAP - 10] + "\n…(+more)"

                if overflow:
                    embed.set_footer(
                        text=f"+{overflow} ακόμα — χρησιμοποιήστε path: για να φιλτράρετε"
                    )

                embed.add_field(name="Αρχεία & Φάκελοι", value=value, inline=False)

            await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception as exc:
            logger.exception("admin onedrive ls failed: %s", exc)
            await interaction.followup.send(f"❌ {exc}", ephemeral=True)


# ── /admin cog ───────────────────────────────────────────────────────────────


class AdminCog(commands.Cog):
    """`/admin` slash command group — SecGen-only ops panel."""

    class _AdminCommands(app_commands.Group):
        def __init__(self, cog: "AdminCog") -> None:
            super().__init__(
                name="admin",
                description="Διαχείριση πλατφόρμας (SecGen μόνο)",
                default_permissions=discord.Permissions(administrator=True),
            )
            self.cog = cog

            # Register subgroups as child commands on the outer group.
            # discord.py 2.x supports nesting app_commands.Group instances.
            self.add_command(_ArchiveCommands())
            self.add_command(_MinutesCommands())
            self.add_command(_M365Commands())
            self.add_command(_OneDriveCommands())

        # ── Top-level /admin audit ────────────────────────────────────────────

        @app_commands.command(
            name="audit",
            description="Show recent audit log entries, optionally filtered by workflow",
        )
        @app_commands.describe(
            workflow="Filter by workflow name (optional)",
            limit=f"Number of entries to show (default {_AUDIT_DEFAULT}, max {_AUDIT_MAX})",
        )
        async def cmd_audit(
            self,
            interaction: discord.Interaction,
            workflow: str = "",
            limit: int = _AUDIT_DEFAULT,
        ) -> None:
            await interaction.response.defer(ephemeral=True, thinking=True)
            try:
                from src.core.audit import get_audit_log

                limit = max(1, min(limit, _AUDIT_MAX))
                entries = get_audit_log(
                    workflow=workflow or None,
                    limit=limit,
                )

                embed = brand_embed(
                    title=f"Audit Log — τελευταίες {len(entries)} εγγραφές"
                    + (f" ({workflow})" if workflow else ""),
                    color=AMNESTY_YELLOW,
                )

                if not entries:
                    embed.add_field(
                        name="(κανένα αποτέλεσμα)",
                        value="Δεν βρέθηκαν εγγραφές για τα επιλεγμένα κριτήρια.",
                        inline=False,
                    )
                else:
                    for entry in entries[:25]:
                        action = entry.get("action", "?")
                        actor = entry.get("actor", "?")
                        timestamp = entry.get("timestamp", "?")[:19]
                        target = entry.get("target") or "—"
                        embed.add_field(
                            name=f"{action} • {actor}",
                            value=f"{timestamp} | target: {target[:80]}",
                            inline=False,
                        )

                await interaction.followup.send(embed=embed, ephemeral=True)
            except Exception as exc:
                logger.exception("admin audit failed: %s", exc)
                await interaction.followup.send(f"❌ {exc}", ephemeral=True)

        # ── Top-level /admin logs ─────────────────────────────────────────────

        @app_commands.command(
            name="logs",
            description="Δείξε τις τελευταίες εγγραφές από το log αρχείο",
        )
        @app_commands.describe(
            scope="Ποιο log να δείξω — main (default) ή errors-only",
            lines="Αριθμός γραμμών (default 25, max 60 — Discord embed cap)",
            pattern="Προαιρετικό regex φίλτρο",
        )
        @app_commands.choices(scope=[
            app_commands.Choice(name="main",   value="main"),
            app_commands.Choice(name="errors", value="errors"),
        ])
        async def cmd_logs(
            self,
            interaction: discord.Interaction,
            scope: str = "main",
            lines: int = 25,
            pattern: str = "",
        ) -> None:
            await interaction.response.defer(ephemeral=True, thinking=True)
            try:
                from src.core.logging_config import error_log_path, main_log_path
                import re as _re

                target = error_log_path() if scope == "errors" else main_log_path()
                lines = max(1, min(lines, 60))

                if not target.exists():
                    await interaction.followup.send(
                        f"⚠️ Το log αρχείο δεν υπάρχει ακόμη ({target.name}).",
                        ephemeral=True,
                    )
                    return

                text = target.read_text(encoding="utf-8", errors="replace").splitlines()
                if pattern:
                    try:
                        rx = _re.compile(pattern, _re.IGNORECASE)
                    except _re.error as exc:
                        await interaction.followup.send(f"❌ Άκυρο regex: {exc}", ephemeral=True)
                        return
                    text = [ln for ln in text if rx.search(ln)]

                tail = text[-lines:]
                if not tail:
                    await interaction.followup.send(
                        f"(Καμία γραμμή στο `{target.name}`{' με ' + pattern if pattern else ''}.)",
                        ephemeral=True,
                    )
                    return

                # Compose as a fenced code block.  Discord caps a message at
                # 2000 chars — back off the tail until it fits.
                while tail:
                    body = "```\n" + "\n".join(tail) + "\n```"
                    if len(body) <= 1950:
                        break
                    tail = tail[1:]  # drop oldest until under the cap

                header = (
                    f"**{target.name}** · last {len(tail)} line(s)"
                    + (f" matching `{pattern}`" if pattern else "")
                )
                await interaction.followup.send(
                    f"{header}\n{body}", ephemeral=True,
                )
            except Exception as exc:
                logger.exception("admin logs failed: %s", exc)
                await interaction.followup.send(f"❌ {exc}", ephemeral=True)

    def __init__(self, bot: commands.Bot) -> None:
        super().__init__()
        self.bot = bot
        self._commands = self._AdminCommands(cog=self)

    async def cog_load(self) -> None:
        self.bot.tree.add_command(self._commands)
        logger.info("AdminCog loaded — /admin group registered")

    async def cog_unload(self) -> None:
        self.bot.tree.remove_command("admin")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AdminCog(bot))
