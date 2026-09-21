"""Protocol-number allocation: one door for every workflow.

A πρωτόκολλο number is the register's primary key and must be unique, gapless
and never reused. Reading "the next number" from the register and using it is
not enough: two runs that read at the same moment get the same number, and a
run that reads a number and then dies leaves a hole nobody can explain later.

So every workflow reserves through :func:`allocate_protocol_number`, which
records the claim in ``protocol_reservations`` before the number is used, and
then either commits it (the row reached the register) or releases it (the run
was rolled back). :func:`find_register_gaps` reports what went wrong anyway.
"""

from __future__ import annotations

import logging
import re

from src.core.audit import (
    commit_protocol_reservation,
    get_reservations_for_year,
    release_protocol_reservation,
    reserve_next_protocol_number,
)

logger = logging.getLogger(__name__)

_PROTOCOL_RE = re.compile(r"^(\d{4})[_-](\d+)$")

__all__ = [
    "allocate_protocol_number",
    "commit_protocol_reservation",
    "release_protocol_reservation",
    "parse_protocol_number",
    "find_register_gaps",
]


def parse_protocol_number(value: str) -> tuple[int, int] | None:
    """``"2026_034"`` -> ``(2026, 34)``; anything else -> ``None``."""
    match = _PROTOCOL_RE.match(str(value or "").strip())
    return (int(match.group(1)), int(match.group(2))) if match else None


async def allocate_protocol_number(onedrive, year: int, workflow_id: str) -> str:
    """Reserve the next number for *year*, seeded from the live register.

    The reservation is what makes concurrent runs safe, and the register is
    what makes the reservation correct after someone has added rows by hand.
    A register that cannot be read is not fatal here: the reservation table
    still yields a free number, and the mismatch surfaces in `register audit`.

    Args:
        onedrive: client exposing ``get_current_year_max_seq`` (or, failing
            that, ``get_next_protocol_number``).
        year: calendar year the number belongs to.
        workflow_id: the run that owns the reservation, so it can be committed
            or released later.

    Returns:
        ``"YYYY_NNN"``.
    """
    xlsx_max = 0
    try:
        xlsx_max = int(await onedrive.get_current_year_max_seq(year) or 0)
    except AttributeError:
        try:
            parsed = parse_protocol_number(await onedrive.get_next_protocol_number(year))
            xlsx_max = max(parsed[1] - 1, 0) if parsed else 0
        except Exception as exc:
            logger.warning("Register unreadable for %s (%s); reserving from the table alone",
                           year, exc)
    except Exception as exc:
        logger.warning("Register unreadable for %s (%s); reserving from the table alone",
                       year, exc)

    number = reserve_next_protocol_number(year, workflow_id, xlsx_max_seq=xlsx_max)
    logger.info("Reserved protocol number %s for workflow %s (register max %d)",
                number, workflow_id, xlsx_max)
    return number


def find_register_gaps(year: int, register_numbers: list[str] | None = None) -> dict:
    """Compare reservations against the register and report what disagrees.

    Returns a dict with:
      ``reserved``      every reservation for the year, newest first;
      ``uncommitted``   reserved but never confirmed as written - a run that
                        died, or one still in flight;
      ``missing``       numbers the register does not contain although the
                        reservation was committed;
      ``unreserved``    numbers in the register that no reservation explains,
                        which is normal for rows added by hand;
      ``gaps``          sequence numbers absent from both sides, below the
                        highest number in use.
    """
    reservations = get_reservations_for_year(year)
    reserved_seqs = {int(r["seq"]) for r in reservations}
    committed = {int(r["seq"]) for r in reservations if r.get("committed")}
    uncommitted = [r for r in reservations if not r.get("committed")]

    in_register: set[int] = set()
    for value in register_numbers or []:
        parsed = parse_protocol_number(value)
        if parsed and parsed[0] == year:
            in_register.add(parsed[1])

    highest = max(reserved_seqs | in_register, default=0)
    all_seen = reserved_seqs | in_register
    return {
        "year": year,
        "reserved": sorted(reservations, key=lambda r: int(r["seq"]), reverse=True),
        "uncommitted": uncommitted,
        "missing": sorted(committed - in_register) if register_numbers is not None else [],
        "unreserved": sorted(in_register - reserved_seqs),
        "gaps": [n for n in range(1, highest + 1) if n not in all_seen],
        "highest": highest,
    }
