"""Tests for TeamsStore — data layer for Phase C team management."""

from __future__ import annotations

import pytest

from src.integrations.discord.state import TeamsStore


# ---------------------------------------------------------------------------
# add + list round-trip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_teams_store_add_and_list(in_memory_db):
    store = TeamsStore()
    await store.add(
        "111111111111111111",
        team_name="Επιτροπή Πολιτικής",
        category_id="222222222222222222",
        coordinator_role_id="333333333333333333",
    )
    teams = await store.list()
    assert len(teams) == 1
    t = teams[0]
    assert t.team_role_id == "111111111111111111"
    assert t.team_name == "Επιτροπή Πολιτικής"
    assert t.category_id == "222222222222222222"
    assert t.coordinator_role_id == "333333333333333333"


@pytest.mark.asyncio
async def test_teams_store_add_multiple_and_list(in_memory_db):
    store = TeamsStore()
    await store.add("100000000000000001", team_name="Ομάδα Α")
    await store.add("100000000000000002", team_name="Ομάδα Β")
    teams = await store.list()
    assert len(teams) == 2
    names = {t.team_name for t in teams}
    assert names == {"Ομάδα Α", "Ομάδα Β"}


@pytest.mark.asyncio
async def test_teams_store_add_optional_fields_default_none(in_memory_db):
    store = TeamsStore()
    await store.add("200000000000000001", team_name="Ομάδα Γ")
    teams = await store.list()
    assert len(teams) == 1
    t = teams[0]
    assert t.category_id is None
    assert t.coordinator_role_id is None


# ---------------------------------------------------------------------------
# upsert behaviour (same team_role_id updates, not duplicates)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_teams_store_add_upserts_on_conflict(in_memory_db):
    store = TeamsStore()
    await store.add("300000000000000001", team_name="Αρχικό Όνομα")
    await store.add(
        "300000000000000001",
        team_name="Νέο Όνομα",
        category_id="999999999999999999",
    )
    teams = await store.list()
    # Should still be one row, not two
    assert len(teams) == 1
    assert teams[0].team_name == "Νέο Όνομα"
    assert teams[0].category_id == "999999999999999999"


# ---------------------------------------------------------------------------
# remove
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_teams_store_remove_existing_returns_true(in_memory_db):
    store = TeamsStore()
    await store.add("400000000000000001", team_name="Ομάδα Δ")
    deleted = await store.remove("400000000000000001")
    assert deleted is True
    teams = await store.list()
    assert teams == []


@pytest.mark.asyncio
async def test_teams_store_remove_nonexistent_returns_false(in_memory_db):
    store = TeamsStore()
    deleted = await store.remove("999999999999999999")
    assert deleted is False


# ---------------------------------------------------------------------------
# get
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_teams_store_get_existing(in_memory_db):
    store = TeamsStore()
    await store.add(
        "500000000000000001",
        team_name="Ομάδα Ε",
        coordinator_role_id="500000000000000099",
    )
    team = await store.get("500000000000000001")
    assert team is not None
    assert team.team_name == "Ομάδα Ε"
    assert team.coordinator_role_id == "500000000000000099"


@pytest.mark.asyncio
async def test_teams_store_get_missing_returns_none(in_memory_db):
    store = TeamsStore()
    team = await store.get("000000000000000000")
    assert team is None
