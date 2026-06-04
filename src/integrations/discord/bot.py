"""Discord bot skeleton — AmnestyDiscordBot + run_bot / run_bot_sync entry points."""
from __future__ import annotations

import asyncio
import logging

import discord
from discord.ext import commands

from src.config import settings
from src.core.audit import init_db, log_action

logger = logging.getLogger(__name__)

_COGS = [
    "src.integrations.discord.cogs.email_sync",
    "src.integrations.discord.cogs.events",
    "src.integrations.discord.cogs.stats",
    "src.integrations.discord.cogs.ai_assistant",
    "src.integrations.discord.cogs.archive",
    "src.integrations.discord.cogs.forum",
    "src.integrations.discord.cogs.teams",
    "src.integrations.discord.cogs.scheduler",
    "src.integrations.discord.cogs.platform_bridge",
    "src.integrations.discord.cogs.context_menus",
    "src.integrations.discord.cogs.welcome",
    "src.integrations.discord.cogs.rss_feeds",
    "src.integrations.discord.cogs.admin",
    "src.integrations.discord.cogs.board",
]


class AmnestyDiscordBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.messages = True
        intents.message_content = True
        intents.guild_scheduled_events = True
        intents.members = True   # needed for on_member_join (M1 welcome DM)
        # Legacy "!" prefix removed — the bot responds only to slash commands
        # and @mentions.  ``commands.when_mentioned`` keeps prefix-commands
        # working for any future debug use, but doesn't claim a textual prefix.
        super().__init__(command_prefix=commands.when_mentioned, intents=intents)

    async def setup_hook(self) -> None:
        init_db()  # Schema must exist before any cog touches the DB
        for ext in _COGS:
            try:
                await self.load_extension(ext)
                logger.info("Loaded cog: %s", ext)
            except Exception as exc:
                logger.warning("Failed to load cog %s: %s", ext, exc)

        # ── Global app-command audit hook ────────────────────────────────────
        # Every slash command (and context menu) routes through tree.interaction_check
        # before its handler runs.  Returning True lets it proceed; we use this
        # purely as an audit trail — any interaction the user invokes is
        # captured to the audit_log table whether or not the handler then
        # decides to call log_action() itself.
        self.tree.interaction_check = self._audit_interaction
        # Global error handler — keeps stale-command (CommandNotFound) and
        # uncaught handler exceptions from spamming a traceback to the log
        # while leaving the user staring at a frozen Discord client.
        self.tree.on_error = self._on_tree_error

    async def _on_tree_error(
        self,
        interaction: "discord.Interaction",
        error: "discord.app_commands.AppCommandError",
    ) -> None:
        """Friendly fallback for unhandled app-command errors.

        Discord clients aggressively cache the guild-command list.  After
        a sync that *removes* a command, the user's client may still
        autocomplete and invoke it for up to an hour — Discord then routes
        the interaction to us but our tree has no handler, raising
        :class:`discord.app_commands.CommandNotFound`.  Without this hook
        the user sees "This interaction failed" with no context, and the
        log fills with traceback noise.

        We swallow CommandNotFound with a soft Greek message and log a
        single concise line.  Genuine handler bugs still get logged with
        the full exception via ``logger.exception``.
        """
        from discord.app_commands import CommandNotFound

        if isinstance(error, CommandNotFound):
            cmd = (interaction.data or {}).get("name", "?") if interaction.data else "?"
            logger.info(
                "Stale slash command invoked (CommandNotFound): /%s by %s — "
                "client cache likely lagging behind tree sync.",
                cmd, getattr(interaction.user, "id", "?"),
            )
            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message(
                        f"⚠️ Η εντολή `/{cmd}` δεν υπάρχει πλέον. "
                        "Δοκιμάστε `/ai-assistant` ή `/forum`.",
                        ephemeral=True,
                    )
            except Exception:  # pragma: no cover — interaction expired
                pass
            return

        # Any other AppCommandError → log with full traceback so we can
        # diagnose, then tell the user something went wrong.
        logger.exception("Unhandled app-command error: %s", error)
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "❌ Κάτι πήγε στραβά. Δοκιμάστε ξανά σε λίγο.",
                    ephemeral=True,
                )
            else:
                await interaction.followup.send(
                    "❌ Κάτι πήγε στραβά. Δοκιμάστε ξανά σε λίγο.",
                    ephemeral=True,
                )
        except Exception:  # pragma: no cover
            pass

    async def _audit_interaction(self, interaction: discord.Interaction) -> bool:
        """Log every app-command invocation. Always returns True (don't gate)."""
        try:
            data = interaction.data or {}
            cmd_name = data.get("name", "?")
            # Build the full path including any group nesting
            options = data.get("options") or []
            path = [cmd_name]
            while options and isinstance(options, list) and options[0].get("type") in (1, 2):
                # 1 = SUB_COMMAND, 2 = SUB_COMMAND_GROUP
                path.append(options[0]["name"])
                options = options[0].get("options") or []
            log_action(
                workflow="discord.interaction",
                action=" ".join(path),
                actor=str(interaction.user.id),
                target=str(interaction.channel_id or "—"),
                details={
                    "user_name": getattr(interaction.user, "display_name", str(interaction.user)),
                    "guild_id": str(interaction.guild_id or ""),
                    "command_type": interaction.command.qualified_name if interaction.command else cmd_name,
                },
            )
        except Exception as exc:  # pragma: no cover — best-effort audit
            logger.debug("Audit hook failed (non-fatal): %s", exc)
        return True

    async def on_ready(self) -> None:
        logger.info(
            "Bot ready as %s (ID: %s)",
            self.user,
            self.user.id if self.user else "?",
        )
        try:
            guild_id = settings.discord_guild_id
            if guild_id:
                guild_obj = discord.Object(id=int(guild_id))
                self.tree.copy_global_to(guild=guild_obj)
                synced = await self.tree.sync(guild=guild_obj)
            else:
                synced = await self.tree.sync()  # fallback to global
            logger.info("Synced %d slash command(s)", len(synced))
        except Exception as exc:
            logger.warning("Slash command sync failed: %s", exc)

        # NOTE: no Activity / presence text is set (user-removed 2026-05-26).
        # The bot appears online with no "Watching..." / "Playing..." line
        # under its name in the member list — cleaner look for a productivity
        # bot.  Re-enable here with ``self.change_presence(activity=...)`` if
        # ever needed.

        # Upload any app-owned brand emojis declared in brand.APP_EMOJI_DEFINITIONS.
        # Idempotent — skips emojis Discord already knows about.
        try:
            from src.integrations.discord.brand import ensure_app_emojis
            self.app_emojis = await ensure_app_emojis(self)
            logger.info("App emojis resolved: %s", list(self.app_emojis.keys()))
        except Exception as exc:  # pragma: no cover — emojis are cosmetic
            logger.warning("Could not ensure app emojis (non-fatal): %s", exc)
            self.app_emojis = {}

        # Phase C — verify role hierarchy for team management
        try:
            from src.integrations.discord.state import TeamsStore
            guild_id = settings.discord_guild_id
            if guild_id:
                guild = self.get_guild(int(guild_id))
                if guild is not None and guild.me is not None:
                    teams = await TeamsStore().list()
                    bot_top = guild.me.top_role
                    for team in teams:
                        role = guild.get_role(int(team.team_role_id))
                        if role is not None and role >= bot_top:
                            logger.error(
                                "Role hierarchy issue: team role '%s' (%s) is at or above bot's top role '%s'. "
                                "/team add/remove will fail for this team. Move the bot's role higher.",
                                team.team_name, team.team_role_id, bot_top.name,
                            )
        except Exception as exc:
            logger.warning("Could not run team hierarchy check: %s", exc)


async def run_bot() -> None:
    """Start the bot (async entry point)."""
    token = settings.discord_bot_token
    if not token:
        raise RuntimeError("DISCORD_BOT_TOKEN is not configured in .env")
    bot = AmnestyDiscordBot()
    try:
        await bot.start(token)
    finally:
        if not bot.is_closed():
            await bot.close()


def run_bot_sync() -> None:
    """Blocking entry point — called by the CLI."""
    try:
        asyncio.run(run_bot())
    except KeyboardInterrupt:
        logger.info("Bot shutdown requested")


# ── Stale-command cleanup ────────────────────────────────────────────────────


async def clear_stale_commands(
    *,
    clear_globals: bool = True,
    clear_guild: bool = True,
) -> dict[str, list[str]]:
    """Connect briefly to Discord, wipe stale slash commands, disconnect.

    Why this exists
    ---------------
    The bot's normal ``on_ready`` syncs **guild-scoped** commands.  If at any
    point in the past a command was registered **globally** (no ``guild=``
    argument), Discord keeps it in its global command registry forever — even
    after we remove it from the bot's tree.  Discord clients then continue to
    show the stale command in their slash-command autocomplete UI for up to
    an hour (sometimes longer due to local caching).

    This helper does two passes:

    1. ``clear_globals``: ``tree.clear_commands(guild=None)`` + ``sync(guild=None)``
       pushes an EMPTY global set, killing every globally-registered command.
    2. ``clear_guild``: same dance scoped to ``settings.discord_guild_id`` —
       useful when the guild registry has its own accretion.

    After this returns, restart the bot normally; ``on_ready`` will push the
    current command set fresh.

    Returns a small dict reporting what was deleted in each pass (command
    names).  Empty lists mean there was nothing stale.
    """
    token = settings.discord_bot_token
    if not token:
        raise RuntimeError("DISCORD_BOT_TOKEN is not configured in .env")

    deleted: dict[str, list[str]] = {"global": [], "guild": []}

    # Use a barebones client — we don't need cogs / event handlers, just an
    # authenticated session with API access.
    intents = discord.Intents.default()
    client = discord.Client(intents=intents)
    tree = discord.app_commands.CommandTree(client)

    ready_evt = asyncio.Event()

    @client.event
    async def on_ready() -> None:  # noqa: E306
        ready_evt.set()

    async def _do_work() -> None:
        await ready_evt.wait()
        if clear_globals:
            try:
                existing = await tree.fetch_commands()
                deleted["global"] = [c.name for c in existing]
                tree.clear_commands(guild=None)
                await tree.sync()
                logger.info(
                    "Cleared %d global slash command(s): %s",
                    len(deleted["global"]),
                    deleted["global"] or "(none)",
                )
            except Exception as exc:
                logger.warning("Could not clear global commands: %s", exc)
        if clear_guild:
            guild_id = settings.discord_guild_id
            if guild_id:
                guild_obj = discord.Object(id=int(guild_id))
                try:
                    existing = await tree.fetch_commands(guild=guild_obj)
                    deleted["guild"] = [c.name for c in existing]
                    tree.clear_commands(guild=guild_obj)
                    await tree.sync(guild=guild_obj)
                    logger.info(
                        "Cleared %d guild slash command(s) on %s: %s",
                        len(deleted["guild"]),
                        guild_id,
                        deleted["guild"] or "(none)",
                    )
                except Exception as exc:
                    logger.warning("Could not clear guild commands: %s", exc)
            else:
                logger.info("No discord_guild_id configured — skipping guild-scoped clear.")

    # Run the gateway client and the work task concurrently; once the work
    # task finishes, close the client so the start() coroutine returns.
    work_task = asyncio.create_task(_do_work())
    start_task = asyncio.create_task(client.start(token))
    try:
        await work_task
    finally:
        await client.close()
        try:
            await start_task
        except Exception:  # pragma: no cover — closing the session may raise
            pass

    return deleted


def clear_stale_commands_sync(
    *, clear_globals: bool = True, clear_guild: bool = True
) -> dict[str, list[str]]:
    """Blocking CLI wrapper for :func:`clear_stale_commands`."""
    return asyncio.run(
        clear_stale_commands(clear_globals=clear_globals, clear_guild=clear_guild)
    )
