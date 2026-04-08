"""Configuration loader — merges .env secrets with config.yaml settings."""

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
    prompts_dir: str = "data/prompts"


class OneDriveConfig(BaseModel):
    archive_root: str = "/Amnesty/Archive"


class GoogleConfig(BaseModel):
    templates_folder_id: str = ""
    decisions_sheet_id: str = ""
    agenda_sheet_id: str = ""
    invitation_template_id: str = ""   # Google Doc ID for board meeting invitation template
    minutes_drafts_folder_id: str = ""
    protokollo_sheet_id: str = ""


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


class DiscordChannels(BaseModel):
    announcements: str = ""
    verification: str = ""


class DiscordConfig(BaseModel):
    channels: DiscordChannels = DiscordChannels()


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


class TestingConfig(BaseModel):
    """Settings that apply during dry-run / test executions."""
    # Emails are redirected here instead of skipped — lets you proof the
    # actual email content before sending to real recipients.
    # Set to "" to skip emails entirely during dry-runs.
    dry_run_email: str = ""


class AppConfig(BaseModel):
    name: str = "AI-in-AI Platform"
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
    brevo_api_key: str = ""
    discord_bot_token: str = ""
    discord_guild_id: str = ""
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
    brevo_api_key: str = ""
    discord_bot_token: str = ""
    discord_guild_id: str = ""
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
    discord: DiscordConfig = DiscordConfig()
    workflows: WorkflowsConfig = WorkflowsConfig()
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
