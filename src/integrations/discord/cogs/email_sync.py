"""Email sync cog — Google Groups ↔ Discord forum bridge (inbound + outbound)."""
from __future__ import annotations

import asyncio
import logging

import discord
from discord.ext import commands

from src.config import settings
from src.core.audit import log_action
from src.integrations.discord.attachments import AttachmentService
from src.integrations.discord.classifier import ClassificationResult, EmailClassifier
from src.integrations.discord.constants import (
    CLASSIFIER_UNCERTAIN_LABEL,
    DISCORD_MESSAGE_SAFE_CHARS,
    DISCORD_THREAD_NAME_MAX,
    EMAIL_BODY_CLASSIFY_PREVIEW_CHARS,
    STATE_AUTO_CLASSIFY,
    STATE_BOT_ACTIVE,
    STATE_TEST_EMAIL,
    STATE_TEST_MODE_ACTIVE,
    STATE_WEBHOOK_ACTIVE,
    WORKFLOW_NAME,
)
from src.integrations.discord.email_gateway import EmailGateway, InboundEmail
from src.integrations.discord.routing import MessageRouter
from src.integrations.discord.state import (
    BotStateStore,
    EmailThreadMap,
    EnabledChannelsStore,
)
from src.integrations.discord.stats import StatsStore
from src.integrations.discord.webhooks import WebhookManager

logger = logging.getLogger(__name__)


def _build_uncertain(raw: str = "") -> ClassificationResult:
    return ClassificationResult(
        label=CLASSIFIER_UNCERTAIN_LABEL,
        channel_id=None,
        confidence=0.0,
        raw_response=raw,
        fell_back=True,
    )


class EmailSyncCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._state_store: BotStateStore = BotStateStore()
        self._channels_store: EnabledChannelsStore = EnabledChannelsStore()
        self._thread_map: EmailThreadMap = EmailThreadMap()
        self._stats_store: StatsStore = StatsStore()
        self._webhook_mgr: WebhookManager = WebhookManager()
        self._attachment_svc: AttachmentService = AttachmentService()
        self._classifier: EmailClassifier = EmailClassifier(self._channels_store)
        self._gateway: EmailGateway = EmailGateway()
        # Router is lazy — needs guild, available only after on_ready
        self._router: MessageRouter | None = None
        # Last classification result — set just before _post_to_admin calls
        self._last_result: ClassificationResult | None = None

    async def cog_load(self) -> None:
        # Inject the bot into the classifier so its bracket-tag short-circuit
        # can resolve channel names → ALL-CAPS-no-τόνους tags at classify time.
        # Without this the bracket pre-match is silently skipped (LLM still runs).
        self._classifier.set_bot(self.bot)
        self._gateway.on_inbound(self._handle_inbound)
        await self._gateway.start()
        logger.info("EmailSyncCog loaded — email gateway started")

    async def cog_unload(self) -> None:
        if self._gateway:
            await self._gateway.stop()

    # ------------------------------------------------------------------
    # Lazy router (needs guild, available only after on_ready)
    # ------------------------------------------------------------------

    def _get_router(self) -> MessageRouter | None:
        if self._router is not None:
            return self._router
        guild_id_str = settings.discord_guild_id
        if not guild_id_str:
            logger.error(
                "EmailSyncCog: DISCORD_GUILD_ID is not configured — cannot route emails. "
                "Set discord_guild_id in config.yaml or DISCORD_GUILD_ID in .env."
            )
            return None
        guild = self.bot.get_guild(int(guild_id_str))
        if guild is None:
            logger.error(
                "EmailSyncCog: guild %s not found (bot may not have joined yet or ID is wrong) "
                "— dropping inbound email.",
                guild_id_str,
            )
            return None
        self._router = MessageRouter(guild, self._channels_store)
        return self._router

    # ------------------------------------------------------------------
    # Inbound pipeline
    # ------------------------------------------------------------------

    async def _handle_inbound(self, email: InboundEmail) -> None:
        """Process one inbound email: classify → route → post to Discord."""
        # #2 — honor operator kill-switch
        bot_active = await self._state_store.get_bool(STATE_BOT_ACTIVE, default=True)
        if not bot_active:
            logger.debug("EmailSyncCog: STATE_BOT_ACTIVE is False — skipping inbound email %s", email.message_id)
            return

        test_mode = await self._state_store.get_bool(STATE_TEST_MODE_ACTIVE, default=False)
        auto_classify = await self._state_store.get_bool(STATE_AUTO_CLASSIFY, default=True)
        webhook_active = await self._state_store.get_bool(STATE_WEBHOOK_ACTIVE, default=True)

        # #7 — idempotency: skip fresh (non-reply) emails we have already posted
        if email.in_reply_to is None:
            existing_link = await self._thread_map.lookup_thread(email.message_id)
            if existing_link is not None:
                logger.debug(
                    "EmailSyncCog: email %s already posted (thread %s) — skipping duplicate",
                    email.message_id,
                    existing_link.discord_thread_id,
                )
                return

        # Resolve existing thread via In-Reply-To / References
        existing_thread_id: str | None = None
        link = None
        if email.in_reply_to:
            link = await self._thread_map.lookup_thread(email.in_reply_to)
        if link is None:
            for ref in reversed(email.references):
                link = await self._thread_map.lookup_thread(ref)
                if link:
                    break
        if link:
            existing_thread_id = link.discord_thread_id

        # Classify
        if auto_classify and self._classifier and self._classifier.is_enabled():
            result = await self._classifier.classify(
                subject=email.subject,
                body_preview=email.body_plain[:EMAIL_BODY_CLASSIFY_PREVIEW_CHARS],
                test_mode=test_mode,
                request_id=email.request_id,
            )
        else:
            result = _build_uncertain(raw="auto_classify disabled")

        # Route
        router = self._get_router()
        if router is None:
            logger.warning("EmailSyncCog: bot not yet in a guild — dropping email %s", email.message_id)
            return

        decision = await router.resolve(result, existing_thread_id=existing_thread_id, test_mode=test_mode)

        # UNCERTAIN → admin channel
        if decision.channel is None:
            self._last_result = result  # pass to _post_to_admin for confidence bar
            await self._post_to_admin(email, decision.reason, test_mode)
            await self._stats_store.record(
                channel_id=None, thread_id=None,
                direction="inbound_email",
                classification=result.label,
                confidence=result.confidence,
                test_mode=test_mode,
            )
            return

        files = self._attachment_svc.to_discord_files(email.attachments)
        body_formatted = self._format_body(email)
        sender_display = email.from_name or email.from_addr
        new_thread_id: str | None = None
        new_channel_id = str(decision.channel.id)

        try:
            if isinstance(decision.channel, discord.ForumChannel):
                if decision.thread is not None:
                    await self._post_to_existing_thread(
                        decision.thread, body_formatted, files,
                        sender_display=sender_display, webhook_active=webhook_active,
                        parent_channel=decision.channel,
                    )
                    new_thread_id = str(decision.thread.id)
                else:
                    thread = await self._post_to_forum(
                        decision.channel, email, body_formatted, files,
                        sender_display=sender_display, webhook_active=webhook_active,
                        test_mode=test_mode,
                    )
                    new_thread_id = str(thread.id)
            else:
                # TextChannel fallback
                parts = self._split_message(body_formatted)
                for i, part in enumerate(parts):
                    await decision.channel.send(
                        content=part,
                        files=files if i == len(parts) - 1 else [],
                    )
        except Exception as exc:
            logger.error(
                "EmailSyncCog: failed to post email %s to Discord: %s",
                email.message_id, exc,
                extra={"message_id": email.message_id, "request_id": email.request_id},
            )
            return

        if new_thread_id:
            await self._thread_map.record(
                email.message_id,
                discord_thread_id=new_thread_id,
                discord_channel_id=new_channel_id,
                subject=email.subject,
            )

        await self._stats_store.record(
            channel_id=new_channel_id,
            thread_id=new_thread_id,
            direction="inbound_email",
            classification=result.label,
            confidence=result.confidence,
            test_mode=test_mode,
        )
        log_action(
            workflow=WORKFLOW_NAME,
            action="email_routed_to_discord",
            target=email.message_id,
            details={
                "channel": new_channel_id,
                "thread": new_thread_id,
                "label": result.label,
                "test_mode": test_mode,
                "request_id": email.request_id,
            },
        )

    async def _post_to_forum(
        self,
        channel: discord.ForumChannel,
        email: InboundEmail,
        body_formatted: str,
        files: list[discord.File],
        *,
        sender_display: str,
        webhook_active: bool,
        test_mode: bool = False,
    ) -> discord.Thread:
        # #10 — strip whitespace before truncating so "   " falls through to "No Subject"
        thread_name = (email.subject.strip()[:DISCORD_THREAD_NAME_MAX] or "No Subject")
        parts = self._split_message(body_formatted)

        # #16 — apply forum tags if configured for this channel
        applied_tags: list[discord.Object] = []
        enabled_ch = await self._channels_store.get(str(channel.id), test_mode=test_mode)
        if enabled_ch and enabled_ch.forum_tag_ids:
            applied_tags = [discord.Object(id=int(t)) for t in enabled_ch.forum_tag_ids]

        # Webhooks cannot create forum threads — always use channel.create_thread for the opener.
        create_kwargs: dict = {
            "name": thread_name,
            "content": parts[0],
            "files": files if len(parts) == 1 else [],
        }
        if applied_tags:
            create_kwargs["applied_tags"] = applied_tags
        thread, _ = await channel.create_thread(**create_kwargs)
        # Follow-up parts: use webhook when active so they post under sender's name.
        for i, part in enumerate(parts[1:], start=1):
            if webhook_active and self._webhook_mgr is not None:
                await self._webhook_mgr.post(
                    channel,
                    content=part,
                    username=sender_display,
                    thread=thread,
                    files=files if i == len(parts) - 1 else None,
                )
            else:
                await thread.send(
                    content=part,
                    files=files if i == len(parts) - 1 else [],
                )
        return thread

    async def _post_to_existing_thread(
        self,
        thread: discord.Thread,
        body_formatted: str,
        files: list[discord.File],
        *,
        sender_display: str,
        webhook_active: bool,
        parent_channel: discord.ForumChannel,
    ) -> None:
        # #3 — use webhook so messages appear under the original sender's name.
        # Webhook posts to a forum thread require posting to the parent ForumChannel with thread=thread.
        parts = self._split_message(body_formatted)
        for i, part in enumerate(parts):
            if webhook_active and self._webhook_mgr is not None:
                await self._webhook_mgr.post(
                    parent_channel,
                    content=part,
                    username=sender_display,
                    thread=thread,
                    files=files if i == len(parts) - 1 else None,
                )
            else:
                await thread.send(
                    content=part,
                    files=files if i == len(parts) - 1 else [],
                )

    async def _post_to_admin(self, email: InboundEmail, reason: str, test_mode: bool) -> None:
        """I3: post UNCERTAIN email to admin channel + ranked triage buttons.

        Builds a flame-orange embed with:
        - Quoted sender + body snippet
        - Classifier confidence bar
        - Ranked routing candidates
        Each routing decision is recorded to audit_log for classifier training.
        """
        from src.integrations.discord.brand import (
            AMNESTY_FLAME,
            brand_embed,
            confidence_bar,
        )

        admin_id = (
            settings.discord.admin.test_admin_channel_id
            if test_mode
            else settings.discord.admin.admin_channel_id
        )
        if not admin_id:
            logger.warning("No admin channel configured — dropping UNCERTAIN email %s", email.message_id)
            return
        channel = self.bot.get_channel(int(admin_id))
        if channel is None:
            logger.warning(
                "Admin channel %s not found (not cached or wrong ID) — dropping UNCERTAIN email %s",
                admin_id, email.message_id,
            )
            return
        sender = email.from_name or email.from_addr

        # Quoted body snippet — first ~120 chars of plain body
        body_raw = (email.body_plain or "").strip()
        body_snippet = body_raw[:120] + ("…" if len(body_raw) > 120 else "")

        embed = brand_embed(
            title="🔥 Email προς δρομολόγηση",
            description=(
                f"> {sender}\n"
                f"> {body_snippet or '(κενό)'}"
            ),
            color=AMNESTY_FLAME,
        )
        embed.add_field(
            name="Θέμα",
            value=f"**Re:** {email.subject[:100]}",
            inline=False,
        )

        # Classifier confidence bar (last cached classification result)
        result = self._last_result  # set in _handle_inbound before _post_to_admin
        if result is not None:
            embed.add_field(
                name="Εμπιστοσύνη",
                value=confidence_bar(result.confidence),
                inline=False,
            )
            # Ranked candidates field
            candidates: list[str] = []
            if result.channel_id:
                candidates.append(f"1. <#{result.channel_id}> — {result.confidence:.0%}")
            for i, alt in enumerate(result.alternates, start=2):
                candidates.append(f"{i}. <#{alt.channel_id}> — {alt.confidence:.0%}")
                if i >= 3:
                    break
            if candidates:
                embed.add_field(
                    name="Πιθανές διαδρομές",
                    value="\n".join(candidates),
                    inline=False,
                )

        embed.set_footer(text=f"Email ID: {email.message_id}")

        view = await self._build_triage_view(email, test_mode, result=result)

        if not isinstance(channel, discord.TextChannel):
            logger.warning(
                "Admin channel %s is %s, not TextChannel — attempting send anyway",
                admin_id, type(channel).__name__,
            )
        if hasattr(channel, "send"):
            await channel.send(embed=embed, view=view)  # type: ignore[union-attr]
        else:
            logger.warning(
                "Admin channel %s has no send() method (type=%s) — dropping UNCERTAIN email %s",
                admin_id, type(channel).__name__, email.message_id,
            )

    async def _build_triage_view(
        self,
        email: InboundEmail,
        test_mode: bool,
        *,
        result: ClassificationResult | None = None,
    ) -> discord.ui.View:
        """Build a triage View with ranked route buttons + defer + spam.

        When the classifier returned alternates (ranked mode):
          Row 0 — up to 3 routing buttons (primary = success/green,
                   alternates = secondary/grey) + 1 defer + 1 spam = 5 max.

        When no alternates are available (legacy / first-boot fallback):
          Falls back to the original ≤5-buttons-or-Select behaviour so the
          triage flow never regresses.
        """
        view = discord.ui.View(timeout=86400)  # 24h to triage

        has_ranked = (
            result is not None
            and not result.fell_back
            and result.channel_id is not None
        )

        if has_ranked:
            # Primary button (green)
            view.add_item(_TriageRoutePrimaryButton(
                label=result.label[:40],  # type: ignore[union-attr]
                channel_id=result.channel_id,  # type: ignore[arg-type]
                email_message_id=email.message_id,
            ))
            # Alternate buttons (grey) — up to 2
            for alt in (result.alternates or [])[:2]:  # type: ignore[union-attr]
                view.add_item(_TriageRouteAlternateButton(
                    label=alt.label[:40],
                    channel_id=alt.channel_id,
                    email_message_id=email.message_id,
                ))
        else:
            # Legacy fallback — no ranked alternates
            configured = await self._channels_store.list(test_mode=test_mode)
            if len(configured) <= 5:
                for ch_cfg in configured:
                    view.add_item(_TriageRouteButton(
                        label=ch_cfg.label[:80],
                        channel_id=ch_cfg.channel_id,
                        email_message_id=email.message_id,
                    ))
            else:
                view.add_item(_TriageRouteSelect(
                    channels=configured,
                    email_message_id=email.message_id,
                ))

        # Defer and Spam always present
        view.add_item(_TriageDeferButton(email_message_id=email.message_id))
        view.add_item(_TriageSpamButton(email_message_id=email.message_id))
        return view

    def _format_body(self, email: InboundEmail) -> str:
        sender = email.from_name or email.from_addr
        lines = [f"**{sender}** via email:"]
        lines.extend(f"> {line}" for line in email.body_plain.splitlines())
        att = self._attachment_svc.attachment_summary(email.attachments)
        if att:
            lines.append(att)
        return "\n".join(lines)

    def _split_message(self, text: str) -> list[str]:
        if len(text) <= DISCORD_MESSAGE_SAFE_CHARS:
            return [text]
        parts: list[str] = []
        while len(text) > DISCORD_MESSAGE_SAFE_CHARS:
            chunk = text[:DISCORD_MESSAGE_SAFE_CHARS]
            split_at = chunk.rfind("\n")
            if split_at == -1:
                split_at = DISCORD_MESSAGE_SAFE_CHARS
            parts.append(text[:split_at])
            text = text[split_at:].lstrip("\n")
        if text:
            parts.append(text)
        return parts

    # ------------------------------------------------------------------
    # Outbound: Discord message in linked thread → email reply
    # ------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return
        if message.webhook_id is not None:
            return  # our own email posts via webhook — skip
        if not isinstance(message.channel, discord.Thread):
            return

        # #2 — honor operator kill-switch
        bot_active = await self._state_store.get_bool(STATE_BOT_ACTIVE, default=True)
        if not bot_active:
            logger.debug("EmailSyncCog: STATE_BOT_ACTIVE is False — skipping outbound reply from thread %s", message.channel.id)
            return

        thread = message.channel
        links = await self._thread_map.lookup_by_thread(str(thread.id))
        if not links:
            return

        original = links[-1]
        test_mode = await self._state_store.get_bool(STATE_TEST_MODE_ACTIVE, default=False)
        # #1 — test-mode outbound gating
        test_email = await self._state_store.get_str(STATE_TEST_EMAIL, default="")

        subject = original.subject
        if not subject.lower().startswith("re:"):
            subject = f"Re: {subject}"

        group_email = settings.discord.email_gateway.google_group_email
        body = f"{message.author.display_name} (via Discord):\n\n{message.content}"

        # Determine the actual recipient, applying test-mode overrides.
        if test_mode:
            if test_email:
                logger.debug("EmailSyncCog: test_mode — redirecting outbound email to %s", test_email)
                to = test_email
            else:
                logger.debug(
                    "EmailSyncCog: test_mode active but STATE_TEST_EMAIL is empty — skipping outbound send from thread %s",
                    thread.id,
                )
                await self._stats_store.record(
                    channel_id=original.discord_channel_id,
                    thread_id=str(thread.id),
                    direction="outbound_email_skipped",
                    classification=None,
                    confidence=None,
                    test_mode=test_mode,
                )
                return
        else:
            to = group_email

        try:
            outbound_id = await self._gateway.send_email(
                to=to,
                subject=subject,
                body=body,
                in_reply_to_message_id=original.message_id,
                references=[lnk.message_id for lnk in links],
            )
            await self._thread_map.record(
                outbound_id,
                discord_thread_id=str(thread.id),
                discord_channel_id=original.discord_channel_id,
                subject=subject,
            )
            await self._stats_store.record(
                channel_id=original.discord_channel_id,
                thread_id=str(thread.id),
                direction="outbound_email",
                classification=None,
                confidence=None,
                test_mode=test_mode,
            )
        except Exception as exc:
            logger.error("EmailSyncCog: failed to send reply from thread %s: %s", thread.id, exc)

    @commands.Cog.listener()
    async def on_thread_create(self, thread: discord.Thread) -> None:
        logger.debug("Thread created: %r (id=%s) in parent %s", thread.name, thread.id, thread.parent_id)


# ── I3: Triage button/select implementations ────────────────────────────────


class _TriageRouteButton(discord.ui.Button):
    """Legacy: single-click button routing (used when no ranked alternates available).

    Style is ``primary`` (blurple) — same as original, so the fallback path
    looks unchanged to the admin.
    """

    def __init__(self, *, label: str, channel_id: str, email_message_id: str) -> None:
        super().__init__(
            style=discord.ButtonStyle.primary,
            label=f"→ #{label}",
            custom_id=f"triage:route:{channel_id}:{email_message_id}",
        )
        self.target_channel_id = channel_id
        self.email_message_id = email_message_id

    async def callback(self, interaction: discord.Interaction) -> None:
        await _do_triage_route(interaction, self.target_channel_id, self.email_message_id)


class _TriageRoutePrimaryButton(discord.ui.Button):
    """Ranked triage — primary (most likely) routing candidate.

    Styled ``success`` (green) to signal "this is what the classifier
    recommends most strongly".
    """

    def __init__(self, *, label: str, channel_id: str, email_message_id: str) -> None:
        super().__init__(
            style=discord.ButtonStyle.success,
            label=f"→ #{label}",
            custom_id=f"triage:primary:{channel_id}:{email_message_id}",
        )
        self.target_channel_id = channel_id
        self.email_message_id = email_message_id

    async def callback(self, interaction: discord.Interaction) -> None:
        await _do_triage_route(interaction, self.target_channel_id, self.email_message_id)


class _TriageRouteAlternateButton(discord.ui.Button):
    """Ranked triage — alternate routing candidate.

    Styled ``secondary`` (grey) to signal lower confidence than the primary.
    """

    def __init__(self, *, label: str, channel_id: str, email_message_id: str) -> None:
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label=f"→ #{label}",
            custom_id=f"triage:alt:{channel_id}:{email_message_id}",
        )
        self.target_channel_id = channel_id
        self.email_message_id = email_message_id

    async def callback(self, interaction: discord.Interaction) -> None:
        await _do_triage_route(interaction, self.target_channel_id, self.email_message_id)


class _TriageRouteSelect(discord.ui.Select):
    """Drop-down picker when more than 5 routing channels are configured."""

    def __init__(self, *, channels: list, email_message_id: str) -> None:
        options = [
            discord.SelectOption(label=f"#{c.label[:80]}", value=c.channel_id)
            for c in channels[:25]
        ]
        super().__init__(
            placeholder="Επιλέξτε κανάλι δρομολόγησης…",
            min_values=1,
            max_values=1,
            options=options,
            custom_id=f"triage:select:{email_message_id}",
        )
        self.email_message_id = email_message_id

    async def callback(self, interaction: discord.Interaction) -> None:
        target = self.values[0]
        await _do_triage_route(interaction, target, self.email_message_id)


class _TriageDeferButton(discord.ui.Button):
    """Defer triage — keep the card visible, ACK with ephemeral message.

    The email remains in the queue for re-triage later.  No routing is
    recorded; the card stays intact so the admin can return to it.
    """

    def __init__(self, *, email_message_id: str) -> None:
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label="Αργότερα",
            emoji="⏰",
            custom_id=f"triage:defer:{email_message_id}",
        )
        self.email_message_id = email_message_id

    async def callback(self, interaction: discord.Interaction) -> None:
        log_action(
            workflow=WORKFLOW_NAME,
            action="triage_deferred",
            actor=str(interaction.user.id),
            target=self.email_message_id,
        )
        await interaction.response.send_message(
            "⏰ Παρακάμφθηκε — το email παραμένει στην ουρά για αργότερα.",
            ephemeral=True,
        )


class _TriageSpamButton(discord.ui.Button):
    """Mark the email as spam (no routing, just records the decision)."""

    def __init__(self, *, email_message_id: str) -> None:
        super().__init__(
            style=discord.ButtonStyle.danger,
            label="Spam",
            emoji="🚫",
            custom_id=f"triage:spam:{email_message_id}",
        )
        self.email_message_id = email_message_id

    async def callback(self, interaction: discord.Interaction) -> None:
        log_action(
            workflow=WORKFLOW_NAME,
            action="triage_marked_spam",
            actor=str(interaction.user.id),
            target=self.email_message_id,
        )
        await interaction.response.send_message(
            "🚫 Σημειώθηκε ως spam. Δεν θα δρομολογηθεί.",
            ephemeral=True,
        )


async def _do_triage_route(
    interaction: discord.Interaction,
    target_channel_id: str,
    email_message_id: str,
) -> None:
    """Shared callback: post the email's contents into the chosen channel."""
    await interaction.response.defer(ephemeral=True, thinking=True)
    target = interaction.client.get_channel(int(target_channel_id))
    if target is None:
        try:
            target = await interaction.client.fetch_channel(int(target_channel_id))
        except Exception as exc:
            await interaction.followup.send(
                f"❌ Δεν βρέθηκε το κανάλι: {exc}", ephemeral=True,
            )
            return
    # Note: we don't re-post the full email content here — that would require
    # re-fetching the email from IMAP, which is out of scope for a triage
    # button.  Instead we record the routing decision as audit_log so the
    # classifier can use it as training signal, and SecGen knows the email
    # should be filed in the target channel manually if they actually want
    # it routed there.
    log_action(
        workflow=WORKFLOW_NAME,
        action="triage_routed",
        actor=str(interaction.user.id),
        target=email_message_id,
        details={"chosen_channel_id": target_channel_id},
    )
    await interaction.followup.send(
        f"✅ Καταγράφηκε επιλογή: <#{target_channel_id}>. "
        f"(Η μηχανική δρομολόγηση θα μάθει από αυτή την επιλογή στην επόμενη "
        f"εκπαίδευση του classifier.)",
        ephemeral=True,
    )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(EmailSyncCog(bot))
