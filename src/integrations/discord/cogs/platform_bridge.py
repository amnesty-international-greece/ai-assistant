"""Platform-bridge cog - receives events from the platform event bus and
projects them onto Discord (scheduled events, threads, reminders, etc.).

Phase B: scaffolding only - every handler logs and exits. Phase D fills in
board-meeting handlers; later phases add the rest.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import discord
from discord.ext import commands

from src.config import settings
from src.core.audit import log_action
from src.core.event_bus import bus
from src.core.events import (
    EVENT_BOARD_EMAIL_SENT,
    EVENT_BOARD_MEETING_CANCELLED,
    EVENT_BOARD_MEETING_REMINDER_DUE,
    EVENT_BOARD_MEETING_SCHEDULED,
    EVENT_BOARD_MEETING_THREAD_OPENED,
    EVENT_BOARD_MINUTES_SHARED,
    EVENT_EGKYKLIOS_PUBLISHED,
    EVENT_GA_CALLED,
    EVENT_GA_PROXY_WINDOW_OPENING,
    EVENT_MEMBER_APPROVED,
    BoardEmailSentPayload,
    BoardMeetingCancelledPayload,
    BoardMeetingReminderDuePayload,
    BoardMeetingScheduledPayload,
    BoardMeetingThreadOpenedPayload,
    BoardMinutesSharedPayload,
    EgkykliosPublishedPayload,
)
from src.integrations.discord import embeds
from src.integrations.discord.constants import WORKFLOW_NAME
# Module-level import so tests can patch
# ``src.integrations.discord.cogs.platform_bridge.M365MailClient`` directly
# (the Discord→email bridge in this cog instantiates it inside the on_message
# listener; the import target needs to live in the cog's namespace).
from src.integrations.m365.mail import M365MailClient
from src.integrations.discord.scheduler import (
    PendingActionsStore,
    WorkflowResourcesStore,
    register_action_handler,
)

logger = logging.getLogger(__name__)


class PlatformBridgeCog(commands.Cog):
    """Bridges platform events onto Discord. Reactive - never initiates."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._resources_store: WorkflowResourcesStore | None = None
        self._pending_store: PendingActionsStore | None = None

    async def cog_load(self) -> None:
        self._resources_store = WorkflowResourcesStore()
        self._pending_store = PendingActionsStore()

        # Register the reminder action handler - fires when PendingActionsStore
        # dispatches a due "board_meeting_reminder" row. We re-publish onto the
        # event bus so the cog handler below does the real Discord work, keeping
        # the pending-actions layer free of Discord knowledge.
        async def _handle_reminder_action(payload: dict) -> None:
            from src.core.events import (
                EVENT_BOARD_MEETING_REMINDER_DUE,
                BoardMeetingReminderDuePayload,
            )
            await bus.publish(
                EVENT_BOARD_MEETING_REMINDER_DUE,
                BoardMeetingReminderDuePayload(
                    meeting_id=payload["meeting_id"],
                    hours_before=int(payload["hours_before"]),
                ),
            )

        register_action_handler("board_meeting_reminder", _handle_reminder_action)

        # Deferred fallback: CLI writes this action to the DB so the thread is
        # created even when the bot was offline during `cli invite`.
        # Idempotent: skips if thread_board resource already exists (in-process
        # bus event may have already handled it).
        async def _handle_thread_open_action(payload: dict) -> None:
            meeting_id = payload.get("meeting_id", "")
            if not meeting_id or self._resources_store is None:
                return
            existing = await self._resources_store.list_for_workflow(meeting_id)
            if any(r["resource_type"] == "thread_board" for r in existing):
                logger.info(
                    "PlatformBridge: thread_board already exists for %s - skipping deferred open",
                    meeting_id,
                )
                return
            await self._on_board_meeting_thread_opened(
                BoardMeetingThreadOpenedPayload(
                    meeting_id=meeting_id,
                    meeting_ref=payload.get("meeting_ref", ""),
                    email_subject=payload.get("email_subject", ""),
                    email_body_html=payload.get("email_body_html", ""),
                    poll_url=payload.get("poll_url", ""),
                    agenda_sheet_url=payload.get("agenda_sheet_url", ""),
                    test_mode=bool(payload.get("test_mode", False)),
                )
            )
            # Also post the rich scheduling embed into the newly-created thread
            await self._on_board_email_sent(
                BoardEmailSentPayload(
                    meeting_id=meeting_id,
                    meeting_ref=payload.get("meeting_ref", ""),
                    kind="scheduling",
                    subject=payload.get("email_subject", ""),
                    body_html=payload.get("email_body_html", ""),
                    test_mode=bool(payload.get("test_mode", False)),
                    poll_url=payload.get("poll_url", ""),
                    agenda_url=payload.get("agenda_url", "") or payload.get("agenda_sheet_url", ""),
                )
            )

        register_action_handler("board_meeting_thread_open", _handle_thread_open_action)

        # Deferred fallback: CLI/webhook writes this action so the public agenda
        # thread + Discord scheduled event are created even when the bot was
        # offline during the in-proc bus publish.  Idempotent: skips if an
        # "event" resource already exists (same-process run already handled it).
        async def _handle_scheduled_action(payload: dict) -> None:
            meeting_id = payload.get("meeting_id", "")
            if not meeting_id or self._resources_store is None:
                return
            existing = await self._resources_store.list_for_workflow(meeting_id)
            # Idempotent: if the scheduled event was already created in-process, skip.
            if any(r["resource_type"] == "event" for r in existing):
                logger.info(
                    "PlatformBridge: scheduled event already exists for %s - skipping deferred",
                    meeting_id,
                )
                return
            from datetime import datetime as _dt
            from src.core.events import BoardMeetingScheduledPayload
            starts = payload.get("starts_at") or ""
            try:
                starts_at = _dt.fromisoformat(starts)
            except ValueError:
                logger.warning("PlatformBridge: bad starts_at %r in scheduled action", starts)
                return
            await self._on_board_meeting_scheduled(
                BoardMeetingScheduledPayload(
                    meeting_id=meeting_id,
                    starts_at=starts_at,
                    zoom_url=payload.get("zoom_url", ""),
                    agenda_summary=payload.get("agenda_summary", ""),
                    board_member_emails=payload.get("board_member_emails", []) or [],
                    test_mode=bool(payload.get("test_mode", False)),
                )
            )

        register_action_handler("board_meeting_scheduled", _handle_scheduled_action)

        # Deferred fallback: CLI/webhook writes this action so the invitation
        # embed is mirrored into the private board thread even when the bot was
        # offline during the in-proc bus publish.  Idempotent: skips if a
        # "mirror_invitation" marker resource already exists.
        async def _handle_invitation_mirror_action(payload: dict) -> None:
            meeting_id = payload.get("meeting_id", "")
            if not meeting_id or self._resources_store is None:
                return
            existing = await self._resources_store.list_for_workflow(meeting_id)
            if any(r["resource_type"] == "mirror_invitation" for r in existing):
                logger.info(
                    "PlatformBridge: invitation mirror already posted for %s - skipping deferred",
                    meeting_id,
                )
                return
            from src.core.events import BoardEmailSentPayload
            await self._on_board_email_sent(
                BoardEmailSentPayload(
                    meeting_id=meeting_id,
                    meeting_ref=payload.get("meeting_ref", ""),
                    kind="invitation",
                    subject=payload.get("subject", ""),
                    body_html=payload.get("body_html", ""),
                    test_mode=bool(payload.get("test_mode", False)),
                    zoom_url=payload.get("zoom_url", ""),
                    agenda_url=payload.get("agenda_url", ""),
                    invitation_pdf_url=payload.get("invitation_pdf_url", ""),
                    meeting_datetime=payload.get("meeting_datetime", ""),
                    agenda_summary=payload.get("agenda_summary", ""),
                )
            )

        register_action_handler("board_email_invitation_mirror", _handle_invitation_mirror_action)

        bus.subscribe(EVENT_BOARD_MEETING_THREAD_OPENED, self._on_board_meeting_thread_opened)
        bus.subscribe(EVENT_BOARD_EMAIL_SENT, self._on_board_email_sent)
        bus.subscribe(EVENT_BOARD_MEETING_SCHEDULED, self._on_board_meeting_scheduled)
        bus.subscribe(EVENT_BOARD_MEETING_CANCELLED, self._on_board_meeting_cancelled)
        bus.subscribe(EVENT_BOARD_MEETING_REMINDER_DUE, self._on_board_meeting_reminder_due)
        bus.subscribe(EVENT_BOARD_MINUTES_SHARED, self._on_board_minutes_shared)
        bus.subscribe(EVENT_GA_CALLED, self._on_ga_called)
        bus.subscribe(EVENT_GA_PROXY_WINDOW_OPENING, self._on_ga_proxy_window_opening)
        bus.subscribe(EVENT_EGKYKLIOS_PUBLISHED, self._on_egkyklios_published)
        bus.subscribe(EVENT_MEMBER_APPROVED, self._on_member_approved)
        logger.info("PlatformBridgeCog: subscribed to 10 event types")

    async def cog_unload(self) -> None:
        bus.unsubscribe(EVENT_BOARD_MEETING_THREAD_OPENED, self._on_board_meeting_thread_opened)
        bus.unsubscribe(EVENT_BOARD_EMAIL_SENT, self._on_board_email_sent)
        bus.unsubscribe(EVENT_BOARD_MEETING_SCHEDULED, self._on_board_meeting_scheduled)
        bus.unsubscribe(EVENT_BOARD_MEETING_CANCELLED, self._on_board_meeting_cancelled)
        bus.unsubscribe(EVENT_BOARD_MEETING_REMINDER_DUE, self._on_board_meeting_reminder_due)
        bus.unsubscribe(EVENT_BOARD_MINUTES_SHARED, self._on_board_minutes_shared)
        bus.unsubscribe(EVENT_GA_CALLED, self._on_ga_called)
        bus.unsubscribe(EVENT_GA_PROXY_WINDOW_OPENING, self._on_ga_proxy_window_opening)
        bus.unsubscribe(EVENT_EGKYKLIOS_PUBLISHED, self._on_egkyklios_published)
        bus.unsubscribe(EVENT_MEMBER_APPROVED, self._on_member_approved)

    # ── Email-mirror helpers ────────────────────────────────────────────────

    # Greek labels shown in the Discord post header per email kind.
    # Free to edit; keep keys aligned with the workflow's published values.
    # "discord_bridge" is a synthetic kind emitted by _on_message (Discord→email).
    _EMAIL_KIND_LABEL = {
        "scheduling":      "Email προγραμματισμού",
        "invitation":      "Τελική πρόσκληση",
        "minutes_draft":   "Πρακτικά - προσχέδιο",
        "minutes_final":   "Πρακτικά - τελικά",
        "board_reply":     "Απάντηση σε email",
        "discord_bridge":  "Από το Discord",
        # Bot-authored announcement when the Director sends a briefing to
        # members@.  The Director's raw email never reaches the board - this
        # bot-composed reply on the meeting thread is what they see in both
        # email AND Discord.
        "director_briefing_announcement": "📎 Εισηγητικό Διευθυντή",
    }

    # Board email address - duplicated from board_meeting_invitation workflow so
    # this cog stays import-free of that module (avoids circular deps and keeps
    # the bridge self-contained).
    _BOARD_EMAIL = "board@amnesty.org.gr"

    @staticmethod
    def _html_to_plain(html: str) -> str:
        """Strip an email HTML body down to plain text suitable for Discord.

        Email-shell HTML can be 8 KB+; Discord caps a message at 2000 chars.
        This deliberately drops everything that doesn't carry meaning in chat:
        head/style/script blocks, inline images, all tag soup.  Whitespace is
        collapsed.  The caller is expected to truncate at the Discord cap.

        No external HTML library - the parsing is intentionally tiny and
        regex-based.  Output is good-enough-for-board-mirror, not a
        general HTML→Markdown converter.
        """
        import html as _html
        import re as _re

        if not html:
            return ""

        # Drop <head>, <style>, <script> entirely (their content isn't body text).
        for block in ("head", "style", "script"):
            html = _re.sub(
                rf"<{block}\b[^>]*>.*?</{block}>",
                "",
                html,
                flags=_re.IGNORECASE | _re.DOTALL,
            )
        # Common block-level tags → newline.  Lists get a tiny bullet.
        html = _re.sub(r"</?(?:br|p|div|tr|h[1-6]|li|dl|dt|dd)[^>]*>", "\n", html, flags=_re.IGNORECASE)
        html = _re.sub(r"<li[^>]*>", "\n• ", html, flags=_re.IGNORECASE)
        # Strip every remaining tag.
        html = _re.sub(r"<[^>]+>", "", html)
        # Unescape HTML entities (&amp; → &, etc.).
        text = _html.unescape(html)
        # Collapse runs of whitespace; trim per-line.
        lines = [ln.strip() for ln in text.splitlines()]
        # Drop runs of >1 blank line.
        out: list[str] = []
        blank = False
        for ln in lines:
            if not ln:
                if blank:
                    continue
                blank = True
            else:
                blank = False
            out.append(ln)
        return "\n".join(out).strip()

    @staticmethod
    def _render_email_mirror(
        *,
        kind: str,
        kind_label: str,
        meeting_ref: str,
        subject: str,
        body_plain: str,
        cap: int = 1900,
    ) -> str:
        """Apply the editable mirror template at assets/discord_templates/board_email_mirror.md.

        The template uses ``{kind_label}``, ``{meeting_ref}``, ``{subject}``,
        ``{body_plain}``.  Lines starting with ``#`` at the top of the file
        are comments; we skip everything up to (and including) the marker
        line ``# ─── template starts below`` so the file can be self-
        documenting without polluting the rendered output.
        """
        from pathlib import Path as _P
        tmpl_path = (
            _P(__file__).resolve().parent.parent.parent.parent.parent
            / "assets" / "discord_templates" / "board_email_mirror.md"
        )
        try:
            raw = tmpl_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            # Fall back to a sane default if the template ever goes missing.
            raw = "📧 **{kind_label}** - `{meeting_ref}`\n**Θέμα:** {subject}\n\n{body_plain}"
        # Strip the leading comment block (everything up to and including the
        # marker line; tolerate missing marker by using the file as-is).
        marker = "─── template starts below"
        marker_idx = raw.find(marker)
        if marker_idx != -1:
            # Skip past the marker's newline to start with the actual template.
            nl = raw.find("\n", marker_idx)
            raw = raw[nl + 1 :] if nl != -1 else raw[marker_idx + len(marker):]
        rendered = raw.format(
            kind_label=kind_label,
            meeting_ref=meeting_ref or "-",
            subject=subject or "(χωρίς θέμα)",
            body_plain=body_plain or "(κενό σώμα)",
        )
        if len(rendered) > cap:
            rendered = rendered[: cap - 3].rstrip() + "..."
        return rendered

    async def _post_in_board_thread(
        self,
        meeting_id: str,
        *,
        content: str = "",
        embed: discord.Embed | None = None,
        view: discord.ui.View | None = None,
    ) -> bool:
        """Find the private board thread for ``meeting_id`` and post into it.

        Accepts plain ``content``, a rich ``embed``, an interactive ``view``,
        or any combination.  Returns True on success, False otherwise.
        """
        assert self._resources_store is not None
        resources = await self._resources_store.list_for_workflow(meeting_id)
        thread_resource = next(
            (r for r in resources if r["resource_type"] == "thread_board"), None
        )
        if not thread_resource:
            logger.warning(
                "PlatformBridge: no thread_board resource found for meeting %s - "
                "cannot mirror email",
                meeting_id,
            )
            return False
        thread_id = thread_resource["discord_id"]
        try:
            channel = self.bot.get_channel(int(thread_id)) or await self.bot.fetch_channel(int(thread_id))
            kwargs: dict = {}
            if content:
                kwargs["content"] = content
            if embed is not None:
                kwargs["embed"] = embed
            if view is not None:
                kwargs["view"] = view
            await channel.send(**kwargs)
            return True
        except Exception as exc:
            logger.warning(
                "PlatformBridge: could not post mirror in thread %s: %s",
                thread_id, exc,
            )
            return False

    # ── D0: board.meeting.thread_opened ──────────────────────────────────────

    async def _on_board_meeting_thread_opened(
        self, payload: BoardMeetingThreadOpenedPayload
    ) -> None:
        """Open the private board forum thread at scheduling-email send time.

        Discord scheduled-event creation and public-thread opening happen
        later, in :meth:`_on_board_meeting_scheduled`.  This handler ONLY
        opens the board-private thread + records the resource so subsequent
        email mirrors land in the right place.
        """
        assert self._resources_store is not None
        bm_cfg = settings.discord.platform_bridge.board_meeting
        board_channel_id = (
            (bm_cfg.board_channel_id_test or bm_cfg.board_channel_id)
            if payload.test_mode
            else bm_cfg.board_channel_id
        )
        if not board_channel_id:
            logger.warning(
                "PlatformBridge: board_channel_id not configured - "
                "skipping private thread for %s",
                payload.meeting_ref,
            )
            return

        test_prefix = "[TEST] " if payload.test_mode else ""
        thread_name = f"{test_prefix}Συνεδρίαση {payload.meeting_ref}"
        embed = embeds.board_thread_opened_embed(
            meeting_ref=payload.meeting_ref,
            poll_url=payload.poll_url,
            agenda_sheet_url=payload.agenda_sheet_url,
            test_mode=payload.test_mode,
        )

        thread_id, channel_id = await self._open_meeting_thread(
            channel_id_str=board_channel_id,
            thread_name=thread_name,
            thread_content=f"📂 Νέος κύκλος - {payload.meeting_ref}",
            embed=embed,
            label="private board (early)",
        )
        if not thread_id:
            return  # logged inside _open_meeting_thread

        await self._resources_store.record(
            workflow_id=payload.meeting_id,
            resource_type="thread_board",
            discord_id=thread_id,
            channel_id=channel_id,
        )
        log_action(
            workflow=f"{WORKFLOW_NAME}.platform_bridge",
            action="board_thread_opened",
            target=payload.meeting_id,
            details={
                "thread_id": thread_id,
                "meeting_ref": payload.meeting_ref,
                "test_mode": payload.test_mode,
            },
        )
        logger.info(
            "PlatformBridge: opened private board thread %s for %s",
            thread_id, payload.meeting_ref,
        )

    # ── D0.5: board.meeting.email_sent ───────────────────────────────────────

    async def _on_board_email_sent(self, payload: BoardEmailSentPayload) -> None:
        """Mirror an outbound board-thread email into the private Discord thread.

        Platform-sent emails (scheduling / invitation / minutes) get a rich
        embed + action buttons instead of a plain-text dump of the HTML.
        Inbound member-reply mirrors (kind='board_reply', 'discord_bridge',
        'director_briefing_announcement') fall back to the plain-text template
        since their content is conversational rather than structured.
        """
        kind = payload.kind

        if kind == "scheduling":
            embed, view = embeds.scheduling_mirror_embed(
                meeting_ref=payload.meeting_ref,
                poll_url=payload.poll_url,
                agenda_url=payload.agenda_url,
                test_mode=payload.test_mode,
            )
            await self._post_in_board_thread(payload.meeting_id, embed=embed, view=view)

        elif kind == "invitation":
            embed, view = embeds.invitation_mirror_embed(
                meeting_ref=payload.meeting_ref,
                zoom_url=payload.zoom_url,
                agenda_url=payload.agenda_url,
                invitation_pdf_url=payload.invitation_pdf_url,
                meeting_datetime=payload.meeting_datetime,
                agenda_summary=payload.agenda_summary,
                test_mode=payload.test_mode,
            )
            await self._post_in_board_thread(payload.meeting_id, embed=embed, view=view)
            # Record an idempotency marker so the cross-process deferred handler
            # (and any workflow re-run) doesn't double-post the invitation embed.
            if self._resources_store is not None:
                await self._resources_store.record(
                    workflow_id=payload.meeting_id,
                    resource_type="mirror_invitation",
                    discord_id="posted",
                )

        elif kind in ("minutes_draft", "minutes_final"):
            embed, view = embeds.minutes_mirror_embed(
                meeting_ref=payload.meeting_ref,
                doc_url=payload.doc_url,
                is_draft=(kind == "minutes_draft"),
                test_mode=payload.test_mode,
            )
            await self._post_in_board_thread(payload.meeting_id, embed=embed, view=view)

        else:
            # Conversational / inbound mirrors - keep existing plain-text path
            kind_label = self._EMAIL_KIND_LABEL.get(kind, kind)
            if payload.test_mode:
                kind_label = f"[TEST] {kind_label}"
            body_plain = self._html_to_plain(payload.body_html)
            content = self._render_email_mirror(
                kind=kind,
                kind_label=kind_label,
                meeting_ref=payload.meeting_ref,
                subject=payload.subject,
                body_plain=body_plain,
            )
            await self._post_in_board_thread(payload.meeting_id, content=content)

    async def _open_meeting_thread(
        self,
        *,
        channel_id_str: str,
        thread_name: str,
        thread_content: str = "",
        embed: discord.Embed | None = None,
        view: discord.ui.View | None = None,
        label: str,
    ) -> tuple[str | None, str | None]:
        """Create an agenda thread. Returns ``(thread_id, channel_id)`` or ``(None, None)``.

        Accepts either a plain-text ``thread_content`` and/or a rich ``embed``.
        For forum channels both can be passed.  For text channels we send the
        embed-bearing message first, then thread off it.  Errors are logged but
        never raised - thread creation is optional.
        """
        if not channel_id_str:
            return None, None
        try:
            channel = self.bot.get_channel(int(channel_id_str)) or await self.bot.fetch_channel(int(channel_id_str))
        except Exception as exc:
            logger.warning("PlatformBridge: cannot fetch %s channel %s: %s", label, channel_id_str, exc)
            return None, None

        try:
            if isinstance(channel, discord.ForumChannel):
                kwargs: dict = {"name": thread_name, "content": thread_content or " "}
                if embed is not None:
                    kwargs["embed"] = embed
                if view is not None:
                    kwargs["view"] = view
                result = await channel.create_thread(**kwargs)
                return str(result.thread.id), str(channel.id)
            if isinstance(channel, discord.TextChannel):
                send_kwargs: dict = {}
                if thread_content:
                    send_kwargs["content"] = thread_content
                if embed is not None:
                    send_kwargs["embed"] = embed
                if view is not None:
                    send_kwargs["view"] = view
                message = await channel.send(**send_kwargs)
                thread = await message.create_thread(name=thread_name)
                return str(thread.id), str(channel.id)
            logger.warning(
                "PlatformBridge: %s channel %s is %s, not Forum/Text - skipping thread",
                label, channel_id_str, type(channel).__name__,
            )
        except Exception as exc:
            logger.warning("PlatformBridge: failed to create %s thread (non-fatal): %s", label, exc)
        return None, None

    async def _apply_forum_tag(self, thread_id: str, *, tag_name: str) -> None:
        """Apply a forum tag by NAME to an existing forum thread.

        Resolves the tag against the parent ForumChannel's available_tags.
        No-ops gracefully if the channel isn't a forum, the tag doesn't
        exist, or Discord rejects the edit - tagging is cosmetic.
        """
        if not tag_name:
            return
        try:
            thread = self.bot.get_channel(int(thread_id)) or await self.bot.fetch_channel(int(thread_id))
        except Exception as exc:
            logger.warning("PlatformBridge: cannot fetch thread %s for tagging: %s", thread_id, exc)
            return
        parent = getattr(thread, "parent", None)
        if not isinstance(parent, discord.ForumChannel):
            return  # Plain text-channel threads don't support forum tags
        tag = next((t for t in parent.available_tags if t.name == tag_name), None)
        if tag is None:
            logger.warning(
                "PlatformBridge: forum tag %r not found on %s (available: %s)",
                tag_name, parent.name, [t.name for t in parent.available_tags],
            )
            return
        try:
            await thread.edit(applied_tags=[tag])
        except Exception as exc:
            logger.warning("PlatformBridge: could not apply forum tag %r: %s", tag_name, exc)

    # ── D1: board.meeting.scheduled ──────────────────────────────────────────

    async def _on_board_meeting_scheduled(self, payload: BoardMeetingScheduledPayload) -> None:
        """Create Discord scheduled event + agenda thread + enqueue reminder."""
        resources_store = self._resources_store
        pending_store = self._pending_store
        assert resources_store is not None
        assert pending_store is not None

        # 1. Resolve guild
        guild_id = settings.discord_guild_id
        if not guild_id:
            logger.error("PlatformBridge: discord_guild_id not configured - cannot create scheduled event")
            return
        guild = self.bot.get_guild(int(guild_id))
        if guild is None:
            logger.error("PlatformBridge: guild %s not found in bot cache", guild_id)
            return

        # 2. Create Discord scheduled event
        description = (payload.agenda_summary or "")[:1000]
        if payload.zoom_url:
            description += f"\n\nZoom: {payload.zoom_url}"
        location = payload.zoom_url or "Online"

        # Ensure start_time is timezone-aware (UTC)
        starts_at = payload.starts_at
        if starts_at.tzinfo is None:
            starts_at = starts_at.replace(tzinfo=timezone.utc)

        test_prefix = "[TEST] " if payload.test_mode else ""
        try:
            event = await guild.create_scheduled_event(
                name=f"{test_prefix}Συνεδρίαση ΔΣ - {starts_at.strftime('%d/%m/%Y %H:%M')}",
                description=description,
                start_time=starts_at,
                end_time=starts_at + timedelta(hours=2),
                entity_type=discord.EntityType.external,
                location=location,
                privacy_level=discord.PrivacyLevel.guild_only,
            )
        except Exception as exc:
            logger.exception("PlatformBridge: failed to create Discord scheduled event: %s", exc)
            return

        await resources_store.record(
            workflow_id=payload.meeting_id,
            resource_type="event",
            discord_id=str(event.id),
        )

        # 3. Open agenda threads - one public (members-visible) and one private (board-only).
        #    Both are optional; skip gracefully when not configured.
        #    V1: Rich Embed mirroring the Brevo invitation info architecture.
        thread_name = f"{test_prefix}Συνεδρίαση ΔΣ - {starts_at.strftime('%d/%m/%Y')}"

        embed, view = embeds.public_invitation_embed(
            starts_at=starts_at,
            agenda_summary=payload.agenda_summary,
            zoom_url=payload.zoom_url,
        )

        # Plain-text body retained for Google-Group email subscribers who see
        # forum threads as email and won't render embeds.
        thread_content_text = (
            f"**Πρόσκληση Συνεδρίασης ΔΣ - {starts_at.strftime('%d/%m/%Y %H:%M')} (ώρα Ελλάδας)**\n"
            f"{payload.agenda_summary or '(Ημερήσια Διάταξη κατόπιν ανακοίνωσης)'}\n"
            f"{f'Zoom: {payload.zoom_url}' if payload.zoom_url else ''}"
        )

        # PUBLIC thread (members-visible).  Created here because the public
        # invitation isn't known until the Brevo newsletter is confirmed.
        # In test_mode, prefer the sandbox channel if one is configured so
        # the dry-run never leaks into the real members forum.
        bm_cfg = settings.discord.platform_bridge.board_meeting
        agenda_channel_id = (
            (bm_cfg.agenda_channel_id_test or bm_cfg.agenda_channel_id)
            if payload.test_mode
            else bm_cfg.agenda_channel_id
        )
        public_thread_id, public_channel_id = await self._open_meeting_thread(
            channel_id_str=agenda_channel_id,
            thread_name=thread_name,
            thread_content=thread_content_text,
            embed=embed,
            view=view,
            label="public agenda",
        )
        if public_thread_id:
            await resources_store.record(
                workflow_id=payload.meeting_id,
                resource_type="thread",            # public/members thread
                discord_id=public_thread_id,
                channel_id=public_channel_id,
            )
            # Apply the "Συνεδριάσεις" forum tag on the public thread
            # (best-effort; tag absence is logged but doesn't fail the run).
            await self._apply_forum_tag(
                public_thread_id,
                tag_name=getattr(
                    settings.discord.platform_bridge.board_meeting,
                    "agenda_forum_tag_name",
                    "Συνεδριάσεις",
                ),
            )

        # PRIVATE board thread: do NOT re-create.  It was opened at scheduling-
        # email time by :meth:`_on_board_meeting_thread_opened`.  We just look
        # it up and post a milestone update.
        existing_resources = await resources_store.list_for_workflow(payload.meeting_id)
        board_thread_resource = next(
            (r for r in existing_resources if r["resource_type"] == "thread_board"),
            None,
        )
        if board_thread_resource is not None:
            board_thread_id = board_thread_resource["discord_id"]
            try:
                board_channel = (
                    self.bot.get_channel(int(board_thread_id))
                    or await self.bot.fetch_channel(int(board_thread_id))
                )
                milestone = embeds.milestone_published_embed()
                if view is not None:
                    await board_channel.send(embed=milestone, view=view)
                else:
                    await board_channel.send(embed=milestone)
            except Exception as exc:
                logger.warning(
                    "PlatformBridge: could not post milestone in board thread %s: %s",
                    board_thread_id, exc,
                )
        else:
            logger.warning(
                "PlatformBridge: no thread_board resource for %s - the "
                "scheduling-email step did not publish thread_opened",
                payload.meeting_id,
            )

        # Legacy local names retained for the audit log below (public is the headline thread)
        thread_id = public_thread_id

        # 4. Enqueue reminder
        reminder_hours = settings.workflows.board_meeting.reminder_hours_before
        due_at = starts_at - timedelta(hours=reminder_hours)
        action_id: int | None = None
        if due_at > datetime.now(timezone.utc):
            try:
                action_id = await pending_store.enqueue(
                    action_type="board_meeting_reminder",
                    payload={"meeting_id": payload.meeting_id, "hours_before": reminder_hours},
                    due_at=due_at,
                )
                # Track the pending action ID so cancellation can cancel it
                if action_id is not None:
                    await resources_store.record(
                        workflow_id=payload.meeting_id,
                        resource_type="pending_action",
                        discord_id=str(action_id),
                    )
            except Exception as exc:
                logger.warning("PlatformBridge: failed to enqueue reminder (non-fatal): %s", exc)

        # 5. Audit log
        log_action(
            workflow=f"{WORKFLOW_NAME}.platform_bridge",
            action="board_meeting_scheduled_handled",
            target=payload.meeting_id,
            details={
                "event_id": str(event.id),
                "thread_id": thread_id,
                "reminder_due_at": due_at.isoformat() if action_id is not None else None,
            },
        )
        logger.info(
            "PlatformBridge: board meeting %s → Discord event %s, thread %s",
            payload.meeting_id, event.id, thread_id,
        )

    # ── D4: board.meeting.cancelled ──────────────────────────────────────────

    async def _on_board_meeting_cancelled(self, payload: BoardMeetingCancelledPayload) -> None:
        """Delete Discord event, post cancellation notice in thread, cancel reminders."""
        resources_store = self._resources_store
        pending_store = self._pending_store
        assert resources_store is not None
        assert pending_store is not None

        guild_id = settings.discord_guild_id
        guild: discord.Guild | None = None
        if guild_id:
            guild = self.bot.get_guild(int(guild_id))

        resources = await resources_store.list_for_workflow(payload.meeting_id)

        for resource in resources:
            rtype = resource["resource_type"]
            discord_id = resource["discord_id"]

            if rtype == "event":
                # Delete the Discord scheduled event
                try:
                    if guild is not None:
                        scheduled_event = guild.get_scheduled_event(int(discord_id))
                        if scheduled_event is None:
                            try:
                                scheduled_event = await guild.fetch_scheduled_event(int(discord_id))
                            except discord.NotFound:
                                scheduled_event = None
                        if scheduled_event is not None:
                            await scheduled_event.delete()
                            logger.info("PlatformBridge: deleted scheduled event %s", discord_id)
                except Exception as exc:
                    logger.warning("PlatformBridge: could not delete scheduled event %s: %s", discord_id, exc)

            elif rtype in ("thread", "thread_board"):
                # V4: Rich Embed cancellation notice in both public and private threads
                # (preserve history - don't delete the threads themselves).
                try:
                    channel = self.bot.get_channel(int(discord_id))
                    if channel is None:
                        channel = await self.bot.fetch_channel(int(discord_id))
                    if hasattr(channel, "send"):
                        cancel_embed = embeds.cancellation_embed(reason=payload.reason)
                        await channel.send(embed=cancel_embed)
                except Exception as exc:
                    logger.warning("PlatformBridge: could not post cancellation in %s %s: %s", rtype, discord_id, exc)

            elif rtype == "pending_action":
                # Cancel the queued reminder
                try:
                    await pending_store.cancel(int(discord_id))
                except Exception as exc:
                    logger.warning("PlatformBridge: could not cancel pending action %s: %s", discord_id, exc)

        log_action(
            workflow=f"{WORKFLOW_NAME}.platform_bridge",
            action="board_meeting_cancelled_handled",
            target=payload.meeting_id,
            details={"reason": payload.reason},
        )

    # ── D2: board.meeting.reminder_due ───────────────────────────────────────

    async def _on_board_meeting_reminder_due(self, payload: BoardMeetingReminderDuePayload) -> None:
        """V2: Post Rich Embed reminder with live <t:R> countdown in the agenda thread.

        Posts to BOTH the public agenda thread and the private board thread
        when both exist.  Uses ``<t:UNIX:R>`` so every viewer sees the
        countdown auto-update - no need to re-post.
        """
        resources_store = self._resources_store
        assert resources_store is not None

        resources = await resources_store.list_for_workflow(payload.meeting_id)
        thread_resources = [r for r in resources if r["resource_type"] in ("thread", "thread_board")]

        if not thread_resources:
            logger.warning(
                "PlatformBridge: no thread found for meeting %s - skipping reminder post",
                payload.meeting_id,
            )
            return

        # Compute the actual meeting-start datetime from the reminder context
        # so the <t:R> token in the embed auto-counts down per viewer.
        starts_at = datetime.now(timezone.utc) + timedelta(hours=payload.hours_before)
        reminder_embed = embeds.reminder_embed(
            hours_before=payload.hours_before,
            starts_at=starts_at,
        )

        posted: list[str] = []
        for r in thread_resources:
            thread_id = r["discord_id"]
            try:
                channel = self.bot.get_channel(int(thread_id))
                if channel is None:
                    channel = await self.bot.fetch_channel(int(thread_id))
                await channel.send(embed=reminder_embed)
                posted.append(thread_id)
            except Exception as exc:
                logger.exception("PlatformBridge: failed to post reminder in thread %s: %s", thread_id, exc)

        if not posted:
            return

        log_action(
            workflow=f"{WORKFLOW_NAME}.platform_bridge",
            action="board_meeting_reminder_posted",
            target=payload.meeting_id,
            details={"hours_before": payload.hours_before, "thread_ids": posted},
        )

    # ── D3: board.minutes.shared ──────────────────────────────────────────────

    async def _on_board_minutes_shared(self, payload: BoardMinutesSharedPayload) -> None:
        """V3: Post minutes link as Rich Embed with Link button - PRIVATE board thread only.

        Per the user's note: minutes are sensitive pre-finalization material;
        they go only to the board-only thread, not the members-visible public
        thread.  Body mirrors the ``minutes_share.html`` email template.
        """
        resources_store = self._resources_store
        assert resources_store is not None

        resources = await resources_store.list_for_workflow(payload.meeting_id)
        # Private board thread ONLY (per user spec - public thread skipped).
        thread_resources = [r for r in resources if r["resource_type"] == "thread_board"]

        if not thread_resources:
            logger.info(
                "PlatformBridge: no private board thread for meeting %s - skipping minutes post "
                "(meeting may pre-date the platform bridge, or board_channel_id not configured)",
                payload.meeting_id,
            )
            return

        minutes_embed, view = embeds.minutes_shared_embed(drive_url=payload.drive_url)

        posted_thread_ids: list[str] = []
        for r in thread_resources:
            thread_id = r["discord_id"]
            try:
                channel = self.bot.get_channel(int(thread_id))
                if channel is None:
                    channel = await self.bot.fetch_channel(int(thread_id))
                await channel.send(embed=minutes_embed, view=view)
                posted_thread_ids.append(thread_id)
            except Exception as exc:
                logger.exception("PlatformBridge: failed to post minutes in %s %s: %s", r["resource_type"], thread_id, exc)

        if not posted_thread_ids:
            return

        log_action(
            workflow=f"{WORKFLOW_NAME}.platform_bridge",
            action="board_minutes_shared_posted",
            target=payload.meeting_id,
            details={"drive_url": payload.drive_url, "thread_ids": posted_thread_ids},
        )

    # ── Discord → email bridge ────────────────────────────────────────────────

    @staticmethod
    def _find_meeting_id_for_thread(thread_id: str) -> str | None:
        """Return the ``workflow_id`` (e.g. ``board_meeting:ΔΣ05-2026``) for a
        tracked ``thread_board`` resource matching *thread_id*, or ``None``.

        Runs a direct SQLite query via ``_get_connection`` - same pattern used
        by :class:`~src.integrations.discord.scheduler.WorkflowResourcesStore`.
        """
        from src.core.audit import _get_connection
        conn = _get_connection()
        row = conn.execute(
            """SELECT workflow_id FROM discord_workflow_resources
               WHERE discord_id = ? AND resource_type = 'thread_board'
               LIMIT 1""",
            (thread_id,),
        ).fetchone()
        return row["workflow_id"] if row else None

    @staticmethod
    def _find_email_anchor(meeting_id: str) -> str | None:
        """Find the ``email_thread_anchor`` stored in ``workflow_state`` for a
        board-meeting invitation workflow whose raw_meeting_id suffix matches
        *meeting_id*.

        *meeting_id* is ``"board_meeting:ΔΣ05-2026"``; the raw_meeting_id stored
        in the workflow context is the part after the colon (``"ΔΣ05-2026"``).
        Returns the most-recent matching anchor, or ``None``.
        """
        import json as _json
        from src.core.audit import _get_connection
        raw_ref = meeting_id.split(":", 1)[-1]  # "board_meeting:ΔΣ05-2026" → "ΔΣ05-2026"
        conn = _get_connection()
        rows = conn.execute(
            """SELECT data FROM workflow_state
               WHERE workflow_name = 'board_meeting_invitation'
               ORDER BY updated_at DESC""",
        ).fetchall()
        for row in rows:
            if not row["data"]:
                continue
            try:
                data = _json.loads(row["data"])
            except Exception:
                continue
            context = data.get("context", {})
            if context.get("raw_meeting_id") == raw_ref:
                anchor = context.get("email_thread_anchor")
                if anchor:
                    return anchor
        return None

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """Forward messages posted in tracked board threads to the email thread.

        Direction: Discord private board thread → board@amnesty.org.gr reply.
        The reverse direction (email → Discord) is handled by a separate agent;
        loop prevention here relies on skipping bot-authored messages.
        """
        # ── Fast filters (short-circuit in order) ─────────────────────────
        if message.author == self.bot.user:
            return  # own messages - never re-email (loop prevention)
        if message.author.bot:
            return  # other bots
        if message.guild is None:
            return  # DMs
        if not isinstance(message.channel, discord.Thread):
            return  # only care about thread messages

        thread_id = str(message.channel.id)
        meeting_id = self._find_meeting_id_for_thread(thread_id)
        if meeting_id is None:
            return  # not a tracked board thread

        # ── Resolve the email anchor ───────────────────────────────────────
        anchor = self._find_email_anchor(meeting_id)
        if not anchor:
            logger.warning(
                "PlatformBridge(on_message): no email_thread_anchor for %s - "
                "cannot forward Discord message to email",
                meeting_id,
            )
            try:
                await message.channel.send(
                    "⚠️ Δεν βρέθηκε email anchor - το μήνυμα δεν προωθήθηκε"
                )
            except Exception:
                pass
            return

        # ── Build the email body ───────────────────────────────────────────
        display_name = message.author.display_name
        lines: list[str] = [
            f"[{display_name} via Discord]",
            "",
            message.content or "(χωρίς κείμενο)",
        ]
        if message.attachments:
            lines.append("")
            lines.append("Attachments:")
            for att in message.attachments:
                lines.append(f"  - {att.filename}: {att.url}")
        if message.embeds:
            lines.append("")
            lines.append("(Discord embeds not preserved in email.)")
        plain_body = "\n".join(lines)

        # ── Derive meeting_ref from meeting_id ─────────────────────────────
        meeting_ref = meeting_id.split(":", 1)[-1]  # "board_meeting:ΔΣ05-2026" → "ΔΣ05-2026"

        # ── Send the threaded email reply ──────────────────────────────────
        mail_client = M365MailClient()
        try:
            await mail_client.send_reply(
                parent_internet_message_id=anchor,
                body=plain_body,
                html=False,
                to=self._BOARD_EMAIL,
                workflow="board_meeting_discord_bridge",
            )
        except Exception as exc:
            logger.exception(
                "PlatformBridge(on_message): failed to forward Discord message "
                "from %s to email for %s: %s",
                display_name, meeting_id, exc,
            )
            return

        # ── Publish EVENT_BOARD_EMAIL_SENT for loop prevention ─────────────
        # The inbound email→Discord handler must check for kind="discord_bridge"
        # to avoid re-mirroring what we just sent.
        subject = f"Re: Συνεδρίαση {meeting_ref}"
        await bus.publish(
            EVENT_BOARD_EMAIL_SENT,
            BoardEmailSentPayload(
                meeting_id=meeting_id,
                meeting_ref=meeting_ref,
                kind="discord_bridge",
                subject=subject,
                body_html=plain_body,  # plain text; consumer detects via kind
                test_mode=False,
            ),
        )

        # ── Audit log ─────────────────────────────────────────────────────
        log_action(
            workflow=f"{WORKFLOW_NAME}.platform_bridge",
            action="discord_to_email_bridged",
            target=meeting_id,
            details={
                "author": display_name,
                "thread_id": thread_id,
                "meeting_ref": meeting_ref,
            },
        )
        logger.info(
            "PlatformBridge: forwarded Discord message from %s in %s to board email",
            display_name, meeting_id,
        )

        # ── Best-effort reaction to confirm forwarding ─────────────────────
        try:
            await message.add_reaction("📧")
        except Exception:
            pass  # cosmetic - swallow silently

    # ── Stubs for future phases ───────────────────────────────────────────────

    async def _on_ga_called(self, payload) -> None:
        logger.info("PlatformBridge: ga.called received: %r - handler not yet implemented", payload)

    async def _on_ga_proxy_window_opening(self, payload) -> None:
        logger.info("PlatformBridge: ga.proxy_window_opening received: %r - handler not yet implemented", payload)

    async def _on_egkyklios_published(self, payload) -> None:
        """Post a milestone embed to the members announcements channel.

        Reuses the existing public ``agenda_channel_id`` (the #ενημερώσεις forum)
        as the default destination - εγκύκλιοι are member-facing news.  When the
        SecGen later wants a dedicated channel they can override via
        ``settings.discord.platform_bridge.egkyklios.channel_id``.
        """
        # Resolve the destination channel.  Try the egkyklios-specific config
        # first; fall back to the board-meeting public agenda channel.
        channel_id = ""
        try:
            channel_id = (
                getattr(
                    getattr(settings.discord.platform_bridge, "egkyklios", None),
                    "channel_id",
                    "",
                )
                or ""
            )
        except Exception:
            channel_id = ""
        if not channel_id:
            try:
                channel_id = settings.discord.platform_bridge.board_meeting.agenda_channel_id or ""
            except Exception:
                channel_id = ""
        if not channel_id:
            logger.warning(
                "PlatformBridge: no channel configured for εγκύκλιος announcements - skipping post",
            )
            return

        try:
            channel = self.bot.get_channel(int(channel_id)) or await self.bot.fetch_channel(int(channel_id))
        except Exception as exc:
            logger.warning(
                "PlatformBridge: channel %s unreachable for εγκύκλιος announcement: %s",
                channel_id, exc,
            )
            return

        kind_display = embeds.egkyklios.egkyklios_kind_display(getattr(payload, "kind", ""))
        embed, view = embeds.egkyklios_published_embed(
            kind=getattr(payload, "kind", ""),
            title=payload.title,
            protocol_number=payload.protocol_number or "",
            sent_at=payload.sent_at or "",
            sharepoint_url=payload.sharepoint_url or "",
        )

        # Forum channels: open a thread instead of sending into the channel
        # directly.  Regular text channels: just send.
        try:
            if isinstance(channel, discord.ForumChannel):
                thread_name = payload.title[:100] or kind_display
                await channel.create_thread(
                    name=thread_name,
                    content=f"**{kind_display} - {payload.title}**",
                    embed=embed,
                    view=view,
                )
            elif hasattr(channel, "send"):
                await channel.send(embed=embed, view=view)
            else:
                logger.warning(
                    "PlatformBridge: channel %s has no send method (type=%s)",
                    channel_id, type(channel).__name__,
                )
                return
        except Exception as exc:
            logger.warning(
                "PlatformBridge: failed to post εγκύκλιος announcement in %s: %s",
                channel_id, exc,
            )
            return

        log_action(
            workflow=f"{WORKFLOW_NAME}.platform_bridge",
            action="egkyklios_announced",
            target=payload.protocol_number or payload.title,
            details={
                "kind": payload.kind,
                "channel_id": str(channel_id),
                "sharepoint_url": payload.sharepoint_url,
            },
        )
        logger.info(
            "PlatformBridge: εγκύκλιος announced in channel %s (proto=%s)",
            channel_id, payload.protocol_number,
        )

    async def _on_member_approved(self, payload) -> None:
        logger.info("PlatformBridge: member.approved received: %r - handler not yet implemented", payload)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(PlatformBridgeCog(bot))
