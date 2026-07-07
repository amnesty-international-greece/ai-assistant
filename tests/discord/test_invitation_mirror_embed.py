"""Tests for the final-invitation mirror embed.

Covers the three board-reported defects from the live ΔΣ05-2026 run:
  1. meeting time shown in Athens local time (a naive "20:00" must not be
     treated as UTC and pushed to 23:00 for an Athens viewer),
  2. the general Zoom link is shown inline (not a "see email" placeholder),
  3. the action button links to the invitation PDF labelled "Πρόσκληση",
     replacing the old generic-Zoom button.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from src.integrations.discord.embeds import board_meeting as embeds


def test_naive_meeting_time_is_athens_local_not_utc() -> None:
    # "20:00" Athens wall-clock must round-trip as +03:00, not be relabelled UTC.
    starts_at = embeds._parse_dt("2026-06-09T20:00")
    assert starts_at is not None
    assert starts_at.utcoffset() == ZoneInfo("Europe/Athens").utcoffset(
        datetime(2026, 6, 9, 20, 0)
    )
    # The actual instant is 17:00 UTC, never 23:00.
    assert starts_at.astimezone(ZoneInfo("UTC")).hour == 17


def test_zoom_link_shown_inline() -> None:
    embed, _ = embeds.invitation_mirror_embed(
        meeting_ref="ΔΣ05-2026",
        zoom_url="https://us06web.zoom.us/j/123",
        meeting_datetime="2026-06-09T20:00",
    )
    zoom_fields = [f for f in embed.fields if f.name == "Σύνδεσμος Zoom"]
    assert zoom_fields, "expected a Zoom field"
    assert zoom_fields[0].value == "https://us06web.zoom.us/j/123"
    # The old placeholder copy must be gone.
    assert all("βλέπε email" not in (f.value or "") for f in embed.fields)


def test_invitation_pdf_button_replaces_zoom_button() -> None:
    _, view = embeds.invitation_mirror_embed(
        meeting_ref="ΔΣ05-2026",
        zoom_url="https://us06web.zoom.us/j/123",
        agenda_url="https://docs.google.com/spreadsheets/d/abc/",
        invitation_pdf_url="https://amnestygr.sharepoint.com/share/xyz",
        meeting_datetime="2026-06-09T20:00",
    )
    assert view is not None
    labels = [item.label for item in view.children]
    assert "Πρόσκληση" in labels
    assert "Zoom (γενικός σύνδεσμος)" not in labels
    pdf_button = next(i for i in view.children if i.label == "Πρόσκληση")
    assert pdf_button.url == "https://amnestygr.sharepoint.com/share/xyz"


def test_no_pdf_url_means_no_invitation_button() -> None:
    _, view = embeds.invitation_mirror_embed(
        meeting_ref="ΔΣ05-2026",
        zoom_url="https://us06web.zoom.us/j/123",
        meeting_datetime="2026-06-09T20:00",
    )
    # With no agenda URL and no PDF URL there are no link buttons at all.
    assert view is None
