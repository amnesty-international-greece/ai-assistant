"""Teams cog — /team slash commands for coordinator-managed team membership.

Authority model: a user with the universal Συντονιστής role AND a team role
can manage members of that team. Intersection is enforced server-side; the
Discord UI just shows the slash command.
"""
from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from src.config import settings
from src.core.audit import log_action
from src.integrations.discord.constants import WORKFLOW_NAME
from src.integrations.discord.state import Team, TeamsStore

logger = logging.getLogger(__name__)


class TeamsCog(commands.Cog):
    """`/team` command group — coordinator self-service for team membership."""

    class _TeamCommands(app_commands.Group):
        def __init__(self, cog: "TeamsCog") -> None:
            super().__init__(
                name="team",
                description="Διαχείριση μελών των ομάδων (μόνο για Συντονιστές)",
            )
            self.cog = cog

        # ── helpers ─────────────────────────────────────────────────────

        async def _invoker_teams(
            self,
            interaction: discord.Interaction,
        ) -> list[tuple[discord.Role, Team]]:
            """Return (Discord role, Team) pairs that the invoker can manage.

            Empty list when the invoker doesn't have Συντονιστής or has no team role.
            """
            member = interaction.user
            if not isinstance(member, discord.Member):
                return []

            coordinator_role_id = settings.discord.teams.coordinator_role_id
            if not coordinator_role_id:
                return []
            if not any(str(r.id) == coordinator_role_id for r in member.roles):
                return []

            registered = await self.cog._teams_store.list()
            registered_by_id = {t.team_role_id: t for t in registered}
            result: list[tuple[discord.Role, Team]] = []
            for role in member.roles:
                team = registered_by_id.get(str(role.id))
                if team:
                    result.append((role, team))
            return result

        async def _resolve_team(
            self,
            interaction: discord.Interaction,
            team_param: str | None,
        ) -> tuple[discord.Role, Team] | None:
            """Disambiguate which team to operate on."""
            teams = await self._invoker_teams(interaction)
            if not teams:
                await interaction.response.send_message(
                    "Δεν έχεις τα απαραίτητα δικαιώματα (απαιτείται ο ρόλος "
                    "Συντονιστής + ένας ρόλος ομάδας).",
                    ephemeral=True,
                )
                return None
            if team_param:
                for role, team in teams:
                    if str(role.id) == team_param or team.team_name == team_param:
                        return (role, team)
                await interaction.response.send_message(
                    f"Η ομάδα `{team_param}` δεν βρέθηκε στις ομάδες σου.",
                    ephemeral=True,
                )
                return None
            if len(teams) == 1:
                return teams[0]
            names = ", ".join(t.team_name for _, t in teams)
            await interaction.response.send_message(
                f"Είσαι μέλος πολλών ομάδων ({names}) — προσδιόρισε με `team:`.",
                ephemeral=True,
            )
            return None

        def _hierarchy_ok(self, guild: discord.Guild, target_role: discord.Role) -> bool:
            """The bot's highest role must be above the target role to grant/revoke it."""
            me = guild.me
            if me is None:
                return False
            return me.top_role > target_role

        # ── commands ────────────────────────────────────────────────────

        @app_commands.command(name="add", description="Προσθήκη μέλους σε ομάδα")
        @app_commands.describe(
            user="Μέλος προς προσθήκη",
            team="Όνομα ή ID ομάδας (προαιρετικό αν είσαι σε μία)",
        )
        async def cmd_add(
            self,
            interaction: discord.Interaction,
            user: discord.Member,
            team: str | None = None,
        ) -> None:
            resolved = await self._resolve_team(interaction, team)
            if resolved is None:
                return
            role, team_obj = resolved
            guild = interaction.guild
            assert guild is not None

            if not self._hierarchy_ok(guild, role):
                await interaction.response.send_message(
                    f"Σφάλμα: ο ρόλος μου είναι κάτω από `{role.name}` στην ιεραρχία ρόλων — "
                    "δεν μπορώ να τον αναθέσω. Μετακίνησε τον ρόλο του bot ψηλότερα.",
                    ephemeral=True,
                )
                return

            if role in user.roles:
                await interaction.response.send_message(
                    f"Ο/η {user.display_name} είναι ήδη στην ομάδα {team_obj.team_name}.",
                    ephemeral=True,
                )
                return

            try:
                await user.add_roles(role, reason=f"/team add by {interaction.user}")
            except discord.Forbidden:
                await interaction.response.send_message(
                    "Σφάλμα: το Discord αρνήθηκε την ανάθεση ρόλου (δικαιώματα bot;).",
                    ephemeral=True,
                )
                return

            log_action(
                workflow=f"{WORKFLOW_NAME}.members",
                action="team_member_added",
                actor=str(interaction.user.id),
                target=str(user.id),
                details={"team_role_id": team_obj.team_role_id, "team_name": team_obj.team_name},
            )
            await interaction.response.send_message(
                f"✅ Προστέθηκε ο/η {user.mention} στην ομάδα **{team_obj.team_name}**.",
                ephemeral=True,
            )

        @app_commands.command(name="remove", description="Αφαίρεση μέλους από ομάδα")
        @app_commands.describe(
            user="Μέλος προς αφαίρεση",
            team="Όνομα ή ID ομάδας (προαιρετικό αν είσαι σε μία)",
        )
        async def cmd_remove(
            self,
            interaction: discord.Interaction,
            user: discord.Member,
            team: str | None = None,
        ) -> None:
            resolved = await self._resolve_team(interaction, team)
            if resolved is None:
                return
            role, team_obj = resolved
            guild = interaction.guild
            assert guild is not None

            if not self._hierarchy_ok(guild, role):
                await interaction.response.send_message(
                    f"Σφάλμα: ο ρόλος μου είναι κάτω από `{role.name}` στην ιεραρχία ρόλων.",
                    ephemeral=True,
                )
                return

            if role not in user.roles:
                await interaction.response.send_message(
                    f"Ο/η {user.display_name} δεν είναι στην ομάδα {team_obj.team_name}.",
                    ephemeral=True,
                )
                return

            try:
                await user.remove_roles(role, reason=f"/team remove by {interaction.user}")
            except discord.Forbidden:
                await interaction.response.send_message(
                    "Σφάλμα: το Discord αρνήθηκε την αφαίρεση ρόλου.",
                    ephemeral=True,
                )
                return

            log_action(
                workflow=f"{WORKFLOW_NAME}.members",
                action="team_member_removed",
                actor=str(interaction.user.id),
                target=str(user.id),
                details={"team_role_id": team_obj.team_role_id, "team_name": team_obj.team_name},
            )
            await interaction.response.send_message(
                f"✅ Αφαιρέθηκε ο/η {user.mention} από την ομάδα **{team_obj.team_name}**.",
                ephemeral=True,
            )

        @app_commands.command(name="list", description="Λίστα μελών μιας ομάδας")
        @app_commands.describe(team="Όνομα ή ID ομάδας (προαιρετικό αν είσαι σε μία)")
        async def cmd_list(
            self,
            interaction: discord.Interaction,
            team: str | None = None,
        ) -> None:
            from src.integrations.discord.brand import AMNESTY_YELLOW, brand_embed

            resolved = await self._resolve_team(interaction, team)
            if resolved is None:
                return
            role, team_obj = resolved
            members = list(role.members)

            # Use the role's color for the embed border when it has one,
            # otherwise fall back to Amnesty yellow.
            color = role.color if role.color.value != 0 else AMNESTY_YELLOW
            embed = brand_embed(
                title=f"Ομάδα: {team_obj.team_name}",
                color=color,
            )
            embed.add_field(
                name="Ρόλος", value=role.mention, inline=True,
            )
            embed.add_field(
                name="Μέλη", value=f"**{len(members)}**", inline=True,
            )
            if team_obj.coordinator_role_id:
                embed.add_field(
                    name="Συντονιστής",
                    value=f"<@&{team_obj.coordinator_role_id}>",
                    inline=True,
                )
            if not members:
                embed.add_field(name="Μέλη", value="*(κανένα μέλος)*", inline=False)
            else:
                # Split into pages of ~10 per field to stay readable
                lines = [f"• {m.mention}" for m in members]
                page_size = 10
                for i in range(0, len(lines), page_size):
                    chunk = lines[i:i + page_size]
                    field_name = (
                        f"Μέλη ({i + 1}-{min(i + page_size, len(lines))})"
                        if len(lines) > page_size else "Μέλη"
                    )
                    embed.add_field(name=field_name, value="\n".join(chunk), inline=False)
            await interaction.response.send_message(embed=embed, ephemeral=True)

        # NOTE: /team transfer was removed during the Discord bot modernization
        # (see docs/plans/discord_bot_modernization.md §B.3 user edit).  Admins
        # manage cross-team moves directly via Discord's native role UI
        # (remove old role + add new role).

    # ── cog lifecycle ───────────────────────────────────────────────────

    def __init__(self, bot: commands.Bot) -> None:
        super().__init__()
        self.bot = bot
        self._teams_store: TeamsStore = TeamsStore()
        self._team_commands = self._TeamCommands(cog=self)

    async def cog_load(self) -> None:
        self.bot.tree.add_command(self._team_commands)
        logger.info("TeamsCog loaded — /team group registered")

    async def cog_unload(self) -> None:
        self.bot.tree.remove_command("team")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(TeamsCog(bot))
