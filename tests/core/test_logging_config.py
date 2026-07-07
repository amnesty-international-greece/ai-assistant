"""Tests for the centralised logging config.

Focus: handlers wired correctly, file outputs land where expected,
idempotency works, file handlers can be opted out of for clean test runs.
"""
from __future__ import annotations

import logging
import logging.handlers
import shutil
from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def _reset_root_logger():
    """Clean the root logger before AND after each test so the idempotency
    marker, handler list, and noisy-library levels start fresh."""
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    saved_marker = getattr(root, "_ai_assistant_logging_configured", False)

    for h in list(root.handlers):
        root.removeHandler(h)
    if hasattr(root, "_ai_assistant_logging_configured"):
        delattr(root, "_ai_assistant_logging_configured")

    yield

    for h in list(root.handlers):
        try:
            h.close()
        except Exception:
            pass
        root.removeHandler(h)
    for h in saved_handlers:
        root.addHandler(h)
    root.setLevel(saved_level)
    if saved_marker:
        setattr(root, "_ai_assistant_logging_configured", True)


def test_setup_adds_console_handler():
    from src.core.logging_config import setup_logging

    setup_logging(log_to_file=False)
    handlers = logging.getLogger().handlers
    assert any(isinstance(h, logging.StreamHandler) for h in handlers)


def test_setup_is_idempotent():
    """Calling setup_logging twice must not stack duplicate handlers."""
    from src.core.logging_config import setup_logging

    setup_logging(log_to_file=False)
    n_first = len(logging.getLogger().handlers)
    setup_logging(log_to_file=False)
    n_second = len(logging.getLogger().handlers)
    assert n_first == n_second


def test_setup_creates_file_handlers_in_data_logs(tmp_path):
    """The two TimedRotatingFileHandlers should write into data/logs/."""
    from src.core import logging_config

    # Redirect the module's path constants into tmp_path so we don't write
    # into the real repo during the test.
    fake_dir = tmp_path / "logs"
    fake_main = fake_dir / "main.log"
    fake_err = fake_dir / "err.log"
    with patch.object(logging_config, "_LOG_DIR", fake_dir), \
         patch.object(logging_config, "_MAIN_LOG", fake_main), \
         patch.object(logging_config, "_ERROR_LOG", fake_err):
        logging_config.setup_logging(log_to_file=True)

    handlers = logging.getLogger().handlers
    file_handlers = [h for h in handlers if isinstance(h, logging.handlers.TimedRotatingFileHandler)]
    assert len(file_handlers) == 2
    targets = {Path(h.baseFilename).resolve() for h in file_handlers}
    assert fake_main.resolve() in targets
    assert fake_err.resolve() in targets


def test_error_handler_only_catches_warning_and_above(tmp_path):
    """The errors-only handler must NOT receive INFO/DEBUG lines."""
    from src.core import logging_config

    fake_dir = tmp_path / "logs"
    fake_main = fake_dir / "main.log"
    fake_err = fake_dir / "err.log"
    with patch.object(logging_config, "_LOG_DIR", fake_dir), \
         patch.object(logging_config, "_MAIN_LOG", fake_main), \
         patch.object(logging_config, "_ERROR_LOG", fake_err):
        logging_config.setup_logging(log_to_file=True)

        log = logging.getLogger("test_error_filter")
        log.info("an info-level line")
        log.warning("a warning-level line")
        log.error("an error-level line")

        # Flush so we can read the files
        for h in logging.getLogger().handlers:
            try:
                h.flush()
            except Exception:
                pass

    err_content = fake_err.read_text(encoding="utf-8") if fake_err.exists() else ""
    assert "an info-level line" not in err_content
    assert "a warning-level line" in err_content
    assert "an error-level line" in err_content


def test_main_handler_catches_info_too(tmp_path):
    """The main log writes INFO+ at the configured level."""
    from src.core import logging_config

    fake_dir = tmp_path / "logs"
    fake_main = fake_dir / "main.log"
    fake_err = fake_dir / "err.log"
    with patch.object(logging_config, "_LOG_DIR", fake_dir), \
         patch.object(logging_config, "_MAIN_LOG", fake_main), \
         patch.object(logging_config, "_ERROR_LOG", fake_err):
        logging_config.setup_logging(log_to_file=True, level="INFO")

        log = logging.getLogger("test_main_filter")
        log.info("expected line in main")

        for h in logging.getLogger().handlers:
            try:
                h.flush()
            except Exception:
                pass

    main_content = fake_main.read_text(encoding="utf-8") if fake_main.exists() else ""
    assert "expected line in main" in main_content


def test_noisy_libraries_clipped_to_warning():
    """discord.client / msal / asyncio etc. shouldn't dump INFO chatter
    unless the operator sets AI_ASSISTANT_VERBOSE_LIBS."""
    from src.core.logging_config import setup_logging

    with patch.dict("os.environ", {}, clear=False):
        # Make sure the verbose-libs flag is unset
        import os
        os.environ.pop("AI_ASSISTANT_VERBOSE_LIBS", None)
        setup_logging(log_to_file=False)

    assert logging.getLogger("discord.client").level == logging.WARNING
    assert logging.getLogger("msal").level == logging.WARNING


def test_verbose_libs_env_var_opts_back_in():
    """Setting AI_ASSISTANT_VERBOSE_LIBS=1 leaves library levels untouched."""
    from src.core.logging_config import setup_logging

    # Reset to whatever the libraries' default is (NOTSET = inherit from root)
    for name in ("discord.client", "msal"):
        logging.getLogger(name).setLevel(logging.NOTSET)

    with patch.dict("os.environ", {"AI_ASSISTANT_VERBOSE_LIBS": "1"}, clear=False):
        setup_logging(log_to_file=False)

    # NOTSET means "inherit from root" - we did NOT clip them to WARNING
    assert logging.getLogger("discord.client").level == logging.NOTSET
