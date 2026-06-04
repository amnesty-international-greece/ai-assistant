"""Centralised logging configuration.

What this gives you
-------------------
Three handlers on the root logger, each independently filterable:

1. **Console** — same format as before, so live ``ai-assistant discord run``
   output looks identical to what's been shipping.  Honours ``settings.log_level``.
2. **Rotating main log** at ``data/logs/ai-assistant.log`` — every line at the
   configured level, daily rotation, keep 14 days of history (~2 weeks of full
   context to grep through).
3. **Errors-only log** at ``data/logs/ai-assistant.errors.log`` — WARNING+
   level, daily rotation, keep 30 days.  Smaller file, easier to skim when
   something went wrong.

All three are idempotent — calling :func:`setup_logging` multiple times (e.g.
once in the CLI entry point and again inside :func:`run_bot`) does not stack
handlers.  Library noise (discord.py, msal, urllib3, asyncio) is downgraded
to WARNING by default; turn back up via the ``AI_ASSISTANT_VERBOSE_LIBS``
environment variable when you need it.

Why time-based, not size-based, rotation
-----------------------------------------
A daily file is much easier to navigate than ``log.1``, ``log.2``, etc.
You can ``tail data/logs/ai-assistant.log`` for today, or
``data/logs/ai-assistant.log.2026-05-29`` for a specific past day.  Size
rotation gives you no temporal anchor — useless when you're trying to
correlate "what happened around 18:22 yesterday".
"""
from __future__ import annotations

import logging
import logging.handlers
import os
from pathlib import Path

from src.config import settings


# Module-level marker — when this attribute exists on the root logger we know
# setup has already run and can short-circuit.  The conventional alternative
# (a module-level _CONFIGURED bool) breaks during pytest, where each test
# imports a fresh copy; pinning the marker to the logger itself makes the
# guard survive across modules.
_GUARD_ATTR = "_ai_assistant_logging_configured"

_LOG_DIR = Path("data") / "logs"
_MAIN_LOG = _LOG_DIR / "ai-assistant.log"
_ERROR_LOG = _LOG_DIR / "ai-assistant.errors.log"

_FORMAT = "%(asctime)s | %(name)s | %(levelname)s | %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"

# Libraries that fill the log with INFO-level chatter we rarely need.
# Override by exporting ``AI_ASSISTANT_VERBOSE_LIBS=1`` before launching.
_NOISY_LIBRARIES = (
    "discord.client",
    "discord.gateway",
    "discord.http",
    "msal",
    "msal.application",
    "urllib3.connectionpool",
    "asyncio",
)


def main_log_path() -> Path:
    """Return the absolute path to the main rotating log file."""
    return _MAIN_LOG.resolve()


def error_log_path() -> Path:
    """Return the absolute path to the errors-only rotating log file."""
    return _ERROR_LOG.resolve()


def setup_logging(*, level: str | None = None, log_to_file: bool = True) -> None:
    """Configure root logging.  Idempotent.

    Args:
        level:        Override ``settings.log_level``.  Useful in tests.
        log_to_file:  Skip the file handlers when False — used by ``pytest``
                      where dumping into ``data/logs/`` per-test would be
                      noise.  The console handler is always added.
    """
    root = logging.getLogger()
    if getattr(root, _GUARD_ATTR, False):
        return

    effective_level_name = (level or settings.log_level or "INFO").upper()
    effective_level = getattr(logging, effective_level_name, logging.INFO)
    root.setLevel(effective_level)

    # Wipe any pre-existing handlers added by logging.basicConfig — leaving
    # them in place duplicates every log line.
    for h in list(root.handlers):
        root.removeHandler(h)

    formatter = logging.Formatter(_FORMAT, datefmt=_DATEFMT)

    # ── Console ───────────────────────────────────────────────────────────
    console = logging.StreamHandler()
    console.setLevel(effective_level)
    console.setFormatter(formatter)
    root.addHandler(console)

    # ── File handlers ─────────────────────────────────────────────────────
    if log_to_file:
        try:
            _LOG_DIR.mkdir(parents=True, exist_ok=True)

            main_handler = logging.handlers.TimedRotatingFileHandler(
                filename=str(_MAIN_LOG),
                when="midnight",
                backupCount=14,
                encoding="utf-8",
                # delay=True avoids opening the file until the first log line
                # — keeps the FS clean when ``setup_logging`` runs in CI/tests
                # that produce zero log output.
                delay=True,
            )
            main_handler.setLevel(effective_level)
            main_handler.setFormatter(formatter)
            root.addHandler(main_handler)

            error_handler = logging.handlers.TimedRotatingFileHandler(
                filename=str(_ERROR_LOG),
                when="midnight",
                backupCount=30,
                encoding="utf-8",
                delay=True,
            )
            error_handler.setLevel(logging.WARNING)
            error_handler.setFormatter(formatter)
            root.addHandler(error_handler)
        except OSError as exc:  # pragma: no cover — read-only FS shouldn't break startup
            console.handle(
                logging.LogRecord(
                    name="logging_config",
                    level=logging.WARNING,
                    pathname=__file__,
                    lineno=0,
                    msg="Could not create file log handlers (continuing with console only): %s",
                    args=(exc,),
                    exc_info=None,
                )
            )

    # ── Tamp down library chatter unless explicitly opted in ─────────────
    if not os.environ.get("AI_ASSISTANT_VERBOSE_LIBS"):
        for name in _NOISY_LIBRARIES:
            logging.getLogger(name).setLevel(logging.WARNING)

    setattr(root, _GUARD_ATTR, True)
