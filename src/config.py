"""Configuration loader - merges .env secrets with config.yaml settings."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel
from pydantic_settings import BaseSettings

_PROJECT_ROOT = Path(__file__).resolve().parent.parent


# --- YAML-only config models (not loaded from env) ---

class ClaudeConfig(BaseModel):
    model: str = "claude-sonnet-4-20250514"
    max_tokens: int = 4096
    temperature: float = 0.3


class LLMConfig(BaseModel):
    """Active LLM provider config. Switch provider here to change the model used."""
    provider: str = "gemini"                        # "gemini" | "claude"
    model: str = "gemini-2.0-flash"                 # model name for the chosen provider
    max_tokens: int = 4096
    temperature: float = 0.3


class StorageConfig(BaseModel):
    database_path: str = "data/amnesty.db"
    prompts_dir: str = "src/prompts"   # LLM system-prompt .md files (versioned with code)


class OneDriveConfig(BaseModel):
    sharepoint_host: str = ""                        # e.g. "amnestygr.sharepoint.com"
    sharepoint_site_path: str = ""                   # e.g. "/sites/Board"
    archive_root: str = "Αρχείο"                     # folder name relative to the site's default drive root
    yearly_subfolder: str = "Αρχείο ανά έτος"        # sub-folder pattern under archive_root
    protocol_excel: str = "[Πρωτόκολλο] Αρχείο ΔΣ.xlsx"   # master protocol registry filename
    archiving_guide: str = "Σύστημα Αρχειοθέτησης.docx"    # filing convention document filename


class GoogleConfig(BaseModel):
    templates_folder_id: str = ""
    decisions_sheet_id: str = ""
    agenda_sheet_id: str = ""
    invitation_template_id: str = ""   # Google Doc ID for board meeting invitation template
    minutes_drafts_folder_id: str = ""
    # NOTE: protokollo_sheet_id was removed 2026-05-23 - the πρωτόκολλο now
    # lives in SharePoint as [Πρωτόκολλο] Αρχείο ΔΣ.xlsx (see settings.onedrive.protocol_excel).


class ZoomMeetingDefaults(BaseModel):
    duration: int = 120
    timezone: str = "Europe/Athens"


class ZoomConfig(BaseModel):
    meeting_defaults: ZoomMeetingDefaults = ZoomMeetingDefaults()


class BrevoConfig(BaseModel):
    sender_email: str = "info@amnesty.org.gr"
    sender_name: str = "Διεθνής Αμνηστία - Ελληνικό Τμήμα"
    # Default newsletter template & lists (can be overridden via CLI --brevo-template / --brevo-lists)
    newsletter_template_id: int | None = None
    newsletter_list_ids: list[int] = []
    # Master membership list - used as fallback when newsletter_list_ids is empty
    # so Brevo campaign creation doesn't fail with an invalid list ID
    master_list_id: int = 0


class CrabFitConfig(BaseModel):
    """Crab Fit availability-poll integration.

    Base URLs default to the public hosted instance; point them at a
    self-hosted Crab Fit (open source, GPLv3) when ready.
    """
    api_base: str = "https://api.crab.fit"
    web_base: str = "https://crab.fit"
    default_start_hour: int = 9    # local-time start of the daily availability window
    default_end_hour: int = 23     # exclusive end (last slot is end_hour-1:45)


class DiscordChannels(BaseModel):
    """Discord channel IDs by purpose."""

    announcements: str = ""
    verification: str = ""
    events_channel_id: str = ""


class DiscordEmailGatewayConfig(BaseModel):
    """IMAP/SMTP settings for the email ↔ Discord bridge."""

    gmail_user: str = ""                  # e.g. "membersforum.amnesty.gr@gmail.com"
    google_group_email: str = ""          # e.g. "forum-ai@googlegroups.com"
    imap_host: str = "imap.gmail.com"
    imap_port: int = 993
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    poll_interval_seconds: int = 60
    max_attachment_mb: int = 5


class DiscordClassifierConfig(BaseModel):
    """Gemini-based email classifier configuration."""

    enabled: bool = True
    confidence_threshold: float = 0.70   # 70% - below this returns UNCERTAIN
    temperature: float = 0.1
    uncertain_label: str = "UNCERTAIN"


class DiscordAdminConfig(BaseModel):
    """Admin channel IDs for production and test modes."""

    admin_channel_id: str = ""
    test_admin_channel_id: str = ""


class DiscordStatsConfig(BaseModel):
    """Weekly digest scheduling (Greece local time)."""

    weekly_post_day: int = 6   # Sunday (0=Monday … 6=Sunday)
    weekly_post_hour: int = 0  # midnight Europe/Athens


class DiscordTeamsConfig(BaseModel):
    """Team management settings."""

    coordinator_role_id: str = ""    # Universal Συντονιστής role


class DiscordPlatformBridgeBoardMeetingConfig(BaseModel):
    """Platform-bridge settings for board meeting events."""

    agenda_channel_id: str = ""       # Public channel (forum or text) - members-visible agenda thread
    agenda_channel_id_test: str = ""  # Sandbox channel used when payload.test_mode=True (falls back to agenda_channel_id if blank)
    agenda_forum_tag_name: str = "Συνεδριάσεις"  # Tag applied to public forum threads (forum channels only)
    board_channel_id: str = ""        # Private board channel - board-only thread for preliminary discussion + minutes
    board_channel_id_test: str = ""   # Sandbox channel used when payload.test_mode=True (falls back to board_channel_id if blank)
    board_role_id: str = ""           # Διοικητικό Συμβούλιο role - gates /archive submit + right-click archive
    reminder_hours_before: int = 3    # mirrors workflows.board_meeting.reminder_hours_before


class DiscordPlatformBridgeConfig(BaseModel):
    """Platform-bridge sub-config aggregated into DiscordConfig."""

    board_meeting: DiscordPlatformBridgeBoardMeetingConfig = DiscordPlatformBridgeBoardMeetingConfig()


class DiscordConfig(BaseModel):
    """Top-level Discord integration config - aggregates all sub-configs."""

    channels: DiscordChannels = DiscordChannels()
    email_gateway: DiscordEmailGatewayConfig = DiscordEmailGatewayConfig()
    classifier: DiscordClassifierConfig = DiscordClassifierConfig()
    admin: DiscordAdminConfig = DiscordAdminConfig()
    stats: DiscordStatsConfig = DiscordStatsConfig()
    teams: DiscordTeamsConfig = DiscordTeamsConfig()
    platform_bridge: DiscordPlatformBridgeConfig = DiscordPlatformBridgeConfig()


class BoardMemberConfig(BaseModel):
    """A single board member for Zoom pre-registration."""
    email: str
    first_name: str
    last_name: str


class BoardMeetingConfig(BaseModel):
    reminder_hours_before: int = 3
    min_notice_days: int = 7
    max_advance_days: int = 14
    minutes_share_message: str = "Σας κοινοποιούνται τα πρόχειρα πρακτικά προς σχολιασμό."
    # Pre-registered on Zoom so each member gets a personal join link
    board_members: list[BoardMemberConfig] = []


class GeneralAssemblyConfig(BaseModel):
    min_notice_days: int = 30
    min_electronic_notice_days: int = 15


class WorkflowsConfig(BaseModel):
    board_meeting: BoardMeetingConfig = BoardMeetingConfig()
    general_assembly: GeneralAssemblyConfig = GeneralAssemblyConfig()


class M365InboxConfig(BaseModel):
    """Phase 3 - board members' mailbox watcher (Graph webhook + safety poll).

    ``members@amnesty.org.gr`` is the M365 account signed in via
    ``ai-assistant auth microsoft``.  The webhook subscription watches
    its Inbox; the safety poll runs daily at 12:00 Europe/Athens and
    catches anything the webhook may have missed (e.g. during the
    sub-minute window between subscription expiry and renewal).
    """

    # Substring patterns (case-insensitive, accent-stripped) that mark an
    # email as an archive request.  Default catches "αρχειο", "αρχείο",
    # "ΑΡΧΕΙΟ", "Archive", etc.  Customize via config.yaml if needed.
    subject_patterns: list[str] = ["αρχειο", "archive"]

    # Email addresses authorized to submit archive requests.  Empty list
    # = ALL board_members + secgen.  Add additional addresses (e.g. the
    # Director's @amnesty.org.gr address) here.
    sender_allow_list: list[str] = []

    # Public HTTPS URL Graph posts notifications to.  Override per
    # environment via this YAML value (set after the Cloudflare Tunnel
    # is up).  REQUIRED for `ai-assistant m365 subscribe` to work.
    webhook_url: str = ""

    # Subscription lifetime - Outlook resources max out at 4230 minutes
    # (~70.5h).  We renew when remaining lifetime < renew_threshold_hours.
    subscription_lifetime_minutes: int = 4230
    renew_threshold_hours: int = 24

    # Safety poll cadence (Europe/Athens local time, 24h).
    safety_poll_hour: int = 12
    safety_poll_minute: int = 0


class MinutesPipelineConfig(BaseModel):
    """Minutes pipeline orchestrator config (recording -> transcript -> skeleton).

    ``transcriber`` selects the ASR backend:
      * ``"faster_whisper"`` - real ASR via :class:`FasterWhisperTranscriber`
        (heavy dep, imported lazily only when used).
      * ``"fake"`` - :class:`FakeTranscriber`, a no-ASR stub for testing/wiring.
    The ``whisper_*`` knobs are forwarded to faster-whisper.
    """

    transcriber: str = "faster_whisper"     # "faster_whisper" | "fake"
    whisper_model: str = "large-v3"
    whisper_device: str = "cpu"
    whisper_compute_type: str = "int8"
    language: str = "el"
    recordings_dir: str = "data/recordings"
    transcripts_dir: str = "data/transcripts"
    articles_path: str = "assets/governance/articles.json"
    # Map Zoom display names (as they appear in the recording timeline) to the
    # canonical Greek roster names, so attributed segments + presence use the
    # board's real names. e.g. {"Giorgos Athanasias": "Γεώργιος Αθανασιάς"}.
    speaker_aliases: dict[str, str] = {}
    # Extra terms to prime ASR (Whisper initial_prompt): English words spoken in
    # Greek speech, acronyms, and Amnesty jargon the model would otherwise
    # mis-hear. Appended to the auto-built name/org glossary. e.g.
    # ["NEC", "campaign", "fundraising", "newsletter", "Urgent Action"].
    glossary_extra: list[str] = []
    # Coalesce consecutive same-speaker transcript segments (Whisper's VAD splits
    # one utterance into many) into single turns within each agenda item, so the
    # first-degree drafting LLM sees clean speaker turns instead of fragments.
    # Two adjacent same-speaker segments merge only when the gap between them is
    # <= this many seconds. 0 merges only zero/negative-gap (pure VAD splits); a
    # large value merges a speaker's whole held-floor run; a negative value
    # disables coalescing entirely.
    coalesce_max_gap_seconds: float = 30.0


class UrlsConfig(BaseModel):
    """Public-facing URLs referenced by user-facing copy (welcome DM, embeds, etc.).

    Empty defaults - fill these in ``config.yaml`` when ready.  Code referencing
    these falls back to omitting the link when the URL is empty.
    """
    katastatiko: str = ""              # Καταστατικό - used in M1 welcome DM
    esoterikoi_kanonismoi: str = ""    # Εσωτερικοί Κανονισμοί - used in M1 welcome DM
    website: str = "https://www.amnesty.gr"


class TestingConfig(BaseModel):
    """Settings that apply during test (--test) executions."""
    # Emails are redirected here instead of skipped - lets you proof the
    # actual email content before sending to real recipients.
    # Set to "" to skip emails entirely in test mode.
    test_email: str = ""


class AppConfig(BaseModel):
    name: str = "AI Assistant Platform"
    version: str = "0.1.0"


# --- Env-only secrets ---

class EnvSecrets(BaseSettings):
    """Secrets loaded from .env file."""

    gemini_api_key: str = ""
    anthropic_api_key: str = ""
    ms_client_id: str = ""
    ms_client_secret: str = ""
    ms_tenant_id: str = ""
    ms_redirect_uri: str = "http://localhost:8000/auth/microsoft/callback"
    google_client_id: str = ""
    google_client_secret: str = ""
    google_project_id: str = ""
    zoom_account_id: str = ""
    zoom_client_id: str = ""
    zoom_client_secret: str = ""
    zoom_webhook_secret_token: str = ""  # Zoom webhook CRC validation, e.g. recording.completed (env: ZOOM_WEBHOOK_SECRET_TOKEN)
    brevo_api_key: str = ""
    discord_bot_token: str = ""
    discord_guild_id: str = ""
    gmail_app_password: str = ""
    app_env: str = "development"
    log_level: str = "INFO"

    model_config = {
        "env_file": str(_PROJECT_ROOT / ".env"),
        "env_file_encoding": "utf-8",
        "extra": "ignore",
    }


# --- Combined settings ---

class Settings(BaseModel):
    """Top-level settings combining .env secrets and config.yaml values."""

    # Secrets from .env
    gemini_api_key: str = ""
    anthropic_api_key: str = ""
    ms_client_id: str = ""
    ms_client_secret: str = ""
    ms_tenant_id: str = ""
    ms_redirect_uri: str = "http://localhost:8000/auth/microsoft/callback"
    google_client_id: str = ""
    google_client_secret: str = ""
    google_project_id: str = ""
    zoom_account_id: str = ""
    zoom_client_id: str = ""
    zoom_client_secret: str = ""
    zoom_webhook_secret_token: str = ""  # Zoom webhook CRC validation, e.g. recording.completed (env: ZOOM_WEBHOOK_SECRET_TOKEN)
    brevo_api_key: str = ""
    discord_bot_token: str = ""
    discord_guild_id: str = ""
    gmail_app_password: str = ""
    app_env: str = "development"
    log_level: str = "INFO"

    # Structured config from config.yaml
    app: AppConfig = AppConfig()
    llm: LLMConfig = LLMConfig()
    claude: ClaudeConfig = ClaudeConfig()  # kept for reference / future direct use
    storage: StorageConfig = StorageConfig()
    onedrive: OneDriveConfig = OneDriveConfig()
    google: GoogleConfig = GoogleConfig()
    zoom: ZoomConfig = ZoomConfig()
    brevo: BrevoConfig = BrevoConfig()
    crabfit: CrabFitConfig = CrabFitConfig()
    discord: DiscordConfig = DiscordConfig()
    workflows: WorkflowsConfig = WorkflowsConfig()
    m365_inbox: M365InboxConfig = M365InboxConfig()
    minutes_pipeline: MinutesPipelineConfig = MinutesPipelineConfig()
    urls: UrlsConfig = UrlsConfig()
    testing: TestingConfig = TestingConfig()


def _load_yaml_config() -> dict[str, Any]:
    """Load config.yaml and return as dict."""
    config_path = _PROJECT_ROOT / "config.yaml"
    if config_path.exists():
        with open(config_path, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    return {}


def load_settings() -> Settings:
    """Create Settings instance, merging .env secrets and config.yaml."""
    yaml_config = _load_yaml_config()
    env_secrets = EnvSecrets()
    # Merge: env secrets (flat) + yaml config (nested)
    merged = {**env_secrets.model_dump(), **yaml_config}
    return Settings(**merged)


settings = load_settings()
