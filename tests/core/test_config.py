"""Tests for configuration loading."""

import pytest
from pathlib import Path


def test_settings_load():
    """Settings should load without error even without .env file."""
    from src.config import settings
    assert settings.app.name == "AI Assistant Platform"
    assert settings.app.version == "0.1.0"


def test_claude_defaults():
    """Claude configuration defaults should be set."""
    from src.config import settings
    assert settings.claude.model == "claude-sonnet-4-20250514"
    assert settings.claude.max_tokens == 4096
    assert settings.claude.temperature == 0.3


def test_workflow_defaults():
    """Workflow configuration defaults should be sensible."""
    from src.config import settings
    assert settings.workflows.board_meeting.reminder_hours_before == 3
    assert settings.workflows.board_meeting.min_notice_days == 7
    assert settings.workflows.general_assembly.min_notice_days == 30
    assert settings.workflows.general_assembly.min_electronic_notice_days == 15


def test_storage_paths():
    """The database is the platform's; the prompts are the section's."""
    from src.config import settings
    from src.profile import section

    assert settings.storage.database_path == "data/amnesty.db"
    # Empty here means "ask the section profile", which is where prompts live.
    assert settings.storage.prompts_dir == ""
    assert section.asset_path("prompts").is_dir()
