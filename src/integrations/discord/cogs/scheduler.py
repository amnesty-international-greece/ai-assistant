"""Scheduler cog - owns the pending-actions background worker.

The worker dispatches due rows in `discord_pending_actions` to handlers
registered via `scheduler.register_action_handler`. Other cogs register their
handlers in their own `cog_load`.
"""
from __future__ import annotations

import asyncio
import logging

from discord.ext import commands

from src.integrations.discord.scheduler import (
    PendingActionsStore,
    run_pending_actions_worker,
)

logger = logging.getLogger(__name__)


class SchedulerCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._store = PendingActionsStore()
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task | None = None

    async def cog_load(self) -> None:
        self._stop_event.clear()
        self._task = asyncio.get_event_loop().create_task(
            run_pending_actions_worker(
                store=self._store,
                poll_interval_seconds=30,
                stop_event=self._stop_event,
            ),
            name="discord_pending_actions_worker",
        )
        logger.info("SchedulerCog loaded - pending-actions worker started")

    async def cog_unload(self) -> None:
        self._stop_event.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(SchedulerCog(bot))
