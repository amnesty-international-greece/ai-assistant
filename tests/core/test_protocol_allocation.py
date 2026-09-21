"""Protocol numbers are reserved before use, and the audit explains the rest.

A number is a document's identity for the rest of its life: it must be unique,
and a number that a failed run burned should be visible rather than becoming a
hole nobody can account for years later.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from src.core.audit import get_reservations_for_year
from src.core.protocol import (
    allocate_protocol_number,
    commit_protocol_reservation,
    find_register_gaps,
    parse_protocol_number,
    release_protocol_reservation,
)


def _register(max_seq: int) -> AsyncMock:
    client = AsyncMock()
    client.get_current_year_max_seq.return_value = max_seq
    return client


def test_parse_protocol_number_accepts_both_separators():
    assert parse_protocol_number("2026_034") == (2026, 34)
    assert parse_protocol_number("2026-7") == (2026, 7)
    assert parse_protocol_number("ΔΣ07-2026") is None
    assert parse_protocol_number("") is None


@pytest.mark.asyncio
async def test_allocation_continues_after_the_registers_last_row():
    number = await allocate_protocol_number(_register(35), 2026, "wf-1")
    assert number == "2026_036"


@pytest.mark.asyncio
async def test_two_runs_never_get_the_same_number():
    register = _register(35)
    first = await allocate_protocol_number(register, 2026, "wf-1")
    second = await allocate_protocol_number(register, 2026, "wf-2")
    assert first == "2026_036" and second == "2026_037"


@pytest.mark.asyncio
async def test_an_unreadable_register_still_yields_a_free_number():
    register = AsyncMock()
    register.get_current_year_max_seq.side_effect = RuntimeError("SharePoint down")
    assert await allocate_protocol_number(register, 2026, "wf-1") == "2026_001"


@pytest.mark.asyncio
async def test_a_released_reservation_stops_holding_its_number():
    await allocate_protocol_number(_register(0), 2026, "wf-dead")
    assert len(get_reservations_for_year(2026)) == 1

    assert release_protocol_reservation("wf-dead") == 1
    assert get_reservations_for_year(2026) == []


@pytest.mark.asyncio
async def test_audit_names_the_run_that_burned_a_number():
    """The real case: a May run reserved 2026_028 and never wrote the row."""
    await allocate_protocol_number(_register(27), 2026, "a178da24")
    report = find_register_gaps(2026, [f"2026_{n:03d}" for n in range(1, 28)])

    assert [int(r["seq"]) for r in report["uncommitted"]] == [28]
    assert report["uncommitted"][0]["workflow_id"] == "a178da24"
    assert report["missing"] == []          # never committed, so nothing is owed
    assert report["gaps"] == []             # 28 is still held by the reservation


@pytest.mark.asyncio
async def test_audit_reports_a_committed_number_the_register_never_got():
    await allocate_protocol_number(_register(10), 2026, "wf-1")
    commit_protocol_reservation("wf-1")
    report = find_register_gaps(2026, [f"2026_{n:03d}" for n in range(1, 11)])

    assert report["missing"] == [11]
    assert report["uncommitted"] == []


@pytest.mark.asyncio
async def test_audit_treats_hand_written_rows_as_normal_and_finds_holes():
    report = find_register_gaps(2026, ["2026_001", "2026_002", "2026_004"])
    assert report["unreserved"] == [1, 2, 4]
    assert report["gaps"] == [3]
    assert report["highest"] == 4
