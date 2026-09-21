"""Nightly backup of the database, with rotation.

``data/amnesty.db`` holds every workflow's state, the audit log, protocol
reservations and the captured decisions: losing it means losing the record of
what the Board did and which numbers were issued. A copy is made with SQLite's
own backup API, so it is consistent even while the server and the bot hold the
file open in WAL mode.

Backups stay on this machine. Moving them off it is the section's decision.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime
from pathlib import Path

import src.core.audit as audit
from src.core.audit import log_action

logger = logging.getLogger(__name__)

BACKUP_DIR = Path("data") / "backups"
_PREFIX = "amnesty_"
_SUFFIX = ".db"


def _db_path() -> Path:
    """The live database path, read at call time (init_db sets it)."""
    return Path(audit._DB_PATH or (Path("data") / "amnesty.db"))


def _backup_dir() -> Path:
    return _db_path().parent / "backups"


def create_backup(keep: int = 14, *, actor: str = "system") -> dict:
    """Copy the database, then delete all but the *keep* newest copies.

    Returns ``{"path", "bytes", "removed"}``; ``path`` is None if the database
    does not exist yet.
    """
    source = _db_path()
    if not source.exists():
        logger.warning("No database at %s - nothing to back up", source)
        return {"path": None, "bytes": 0, "removed": []}

    folder = _backup_dir()
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    target = folder / f"{_PREFIX}{stamp}{_SUFFIX}"

    # sqlite3's backup API copies a live database safely; a file copy of a WAL
    # database can catch it mid-transaction.
    with sqlite3.connect(str(source)) as src, sqlite3.connect(str(target)) as dst:
        src.backup(dst)

    removed = rotate(keep)
    size = target.stat().st_size
    log_action(
        workflow="backup",
        action="database_backed_up",
        actor=actor,
        target=str(target),
        details={"bytes": size, "removed": len(removed)},
    )
    logger.info("Database backed up to %s (%d bytes); removed %d old copy(ies)",
                target, size, len(removed))
    return {"path": target, "bytes": size, "removed": removed}


def list_backups() -> list[Path]:
    """Existing backups, newest first."""
    folder = _backup_dir()
    if not folder.exists():
        return []
    return sorted(
        (p for p in folder.glob(f"{_PREFIX}*{_SUFFIX}") if p.is_file()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )


def rotate(keep: int) -> list[Path]:
    """Delete all but the *keep* newest backups; return what was deleted."""
    if keep < 1:
        return []
    removed: list[Path] = []
    for path in list_backups()[keep:]:
        try:
            path.unlink()
            removed.append(path)
        except OSError as exc:
            logger.warning("Could not remove old backup %s: %s", path, exc)
    return removed
