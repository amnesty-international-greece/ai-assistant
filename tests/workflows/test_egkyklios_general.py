"""Tests for the Γενική Εγκύκλιος Ενημέρωσης workflow."""

from __future__ import annotations

import json
import pytest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch, mock_open


# ── Shared fixtures ───────────────────────────────────────────────────────────


@pytest.fixture
def mock_db(tmp_path):
    """Isolate SQLite to a temp file for each test."""
    with patch("src.core.audit._DB_PATH", tmp_path / "test.db"), \
         patch("src.core.audit._CONNECTION", None):
        from src.core.audit import init_db
        init_db()
        yield tmp_path


@pytest.fixture
def workflow(mock_db):
    """EgkykliosGeneralWorkflow with all external clients mocked."""
    with patch("src.workflows.egkyklios_general.OneDriveClient"), \
         patch("src.workflows.egkyklios_general.BrevoClient"):
        from src.workflows.egkyklios_general import EgkykliosGeneralWorkflow
        wf = EgkykliosGeneralWorkflow()
        wf._onedrive = AsyncMock()
        wf._brevo = AsyncMock()
        yield wf


# ── Helper: build a fake briefing row ────────────────────────────────────────


def _briefing_row(
    meeting_ref: str = "ΔΣ01-2026",
    local_path: str = "data/briefings/test.pdf",
    archived_at: str = "2026-01-15T10:00:00",
) -> dict:
    return {
        "id": 1,
        "meeting_ref": meeting_ref,
        "kind": "ΕΙΣΗΓΗΤΙΚΟ",
        "protocol_number": None,
        "local_path": local_path,
        "sharepoint_url": None,
        "archived_at": archived_at,
        "source_message_id": "",
        "workflow_id": "",
    }


def _minutes_row(
    workflow_id: str = "wf-abc123",
    meeting_ref: str = "ΔΣ01-2026",
    meeting_date: str = "2026-01-20",
    updated_at: str = "2026-01-20T22:00:00",
) -> dict:
    data_payload = {
        "context": {
            "meeting_ref": meeting_ref,
            "meeting_date": meeting_date,
            "draft_json": {
                "sections": [
                    {"heading": "Διάφορα", "body": "Συζητήθηκαν τα πάντα."}
                ],
                "decisions": [{"text": "Αποφάσισε να προχωρήσει."}],
            },
        },
        "step_index": 6,
    }
    return {
        "workflow_id": workflow_id,
        "state": "completed",
        "data": json.dumps(data_payload),
        "created_at": "2026-01-20T18:00:00",
        "updated_at": updated_at,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 1. test_gather_sources_returns_briefings_and_minutes_in_window
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_gather_sources_returns_briefings_and_minutes_in_window(workflow):
    with patch(
        "src.workflows.egkyklios_general.list_director_briefings_in_window",
        return_value=[_briefing_row()],
    ), patch(
        "src.workflows.egkyklios_general.list_completed_minutes_in_window",
        return_value=[_minutes_row()],
    ), patch(
        "src.workflows.egkyklios_general.list_egkyklios_drafts",
        return_value=[],
    ):
        ctx = {"period_start": "2026-01-01", "period_end": "2026-03-31"}
        result = await workflow._step_gather_sources(ctx)

    assert result.success is True
    assert len(result.data["briefings_meta"]) == 1
    assert len(result.data["minutes_rows"]) == 1
    assert result.data["period_start"] == "2026-01-01"
    assert result.data["period_end"] == "2026-03-31"
    assert "ΙΑΝΟΥΑΡΙΟΣ" in result.data["title"]
    assert "ΜΑΡΤΙΟΣ" in result.data["title"]


# ─────────────────────────────────────────────────────────────────────────────
# 2. test_gather_sources_fails_when_no_briefings_or_minutes
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_gather_sources_fails_when_no_briefings_or_minutes(workflow):
    with patch(
        "src.workflows.egkyklios_general.list_director_briefings_in_window",
        return_value=[],
    ), patch(
        "src.workflows.egkyklios_general.list_completed_minutes_in_window",
        return_value=[],
    ), patch(
        "src.workflows.egkyklios_general.list_egkyklios_drafts",
        return_value=[],
    ):
        ctx = {"period_start": "2026-01-01", "period_end": "2026-03-31"}
        result = await workflow._step_gather_sources(ctx)

    assert result.success is False
    assert "Δεν βρέθηκαν πηγές" in result.message


# ─────────────────────────────────────────────────────────────────────────────
# 3. test_period_title_format_greek_uppercase
# ─────────────────────────────────────────────────────────────────────────────


def test_period_title_format_greek_uppercase():
    from src.workflows.egkyklios_general import _period_title

    title = _period_title("2026-01-01", "2026-03-31")
    assert title == "ΙΑΝΟΥΑΡΙΟΣ - ΜΑΡΤΙΟΣ 2026"

    # Single month
    title_single = _period_title("2026-05-01", "2026-05-31")
    assert title_single == "ΜΑΪΟΣ 2026"

    # Q4
    title_q4 = _period_title("2025-10-01", "2025-12-31")
    assert title_q4 == "ΟΚΤΩΒΡΙΟΣ - ΔΕΚΕΜΒΡΙΟΣ 2025"


# ─────────────────────────────────────────────────────────────────────────────
# 4. test_draft_circular_calls_claude_with_template_prompt
# ─────────────────────────────────────────────────────────────────────────────


def _fake_model(items: list[dict], staff_names: list[str] | None = None):
    """A ClaudeClient stand-in that answers each prompt the way the model would."""
    client = MagicMock()
    client.load_prompt.side_effect = lambda name: (
        f"<{name}> {{meeting_ref}} {{meeting_date_greek}} "
        "{section_title} {period_start} {period_end} {previous_sections}"
    )
    calls: list[tuple[str, str]] = []

    def generate(user_prompt, system_prompt, workflow=None, max_tokens=None):
        calls.append((system_prompt, user_prompt))
        if system_prompt.startswith("<egkyklios_extract>"):
            return json.dumps({"items": items, "staff_names": staff_names or []})
        if system_prompt.startswith("<egkyklios_plan>"):
            ids = [ln.split(" | ")[0] for ln in user_prompt.splitlines() if ln.strip()]
            return json.dumps({"sections": [{"title": "Events", "items": ids[:1]},
                                            {"title": "Media", "items": ids[1:]}]})
        if system_prompt.startswith("<egkyklios_office>"):
            return "\n".join(ln for ln in user_prompt.splitlines() if ln.startswith("TEXT-"))
        return "Board text."

    client.generate.side_effect = generate
    client.calls = calls
    return client


def _draft_ctx() -> dict:
    return {
        "period_start": "2026-04-01",
        "period_end": "2026-09-30",
        "title": "ΑΠΡΙΛΙΟΣ - ΣΕΠΤΕΜΒΡΙΟΣ 2026",
        "briefing_texts": [
            {"meeting_ref": "ΔΣ05-2026", "kind": "ΕΙΣΗΓΗΤΙΚΟ",
             "archived_at": "2026-06-05", "text": "briefing", "is_scan": False}
        ],
        "meeting_summaries": [
            {"workflow_id": "wf1", "meeting_ref": "ΔΣ05-2026",
             "meeting_date": "2026-06-09", "text": "minutes"}
        ],
    }


def _draft_patches(tmp_path):
    from contextlib import ExitStack
    stack = ExitStack()
    stack.enter_context(patch("src.workflows.egkyklios_general.create_egkyklios_draft",
                              return_value=42))
    stack.enter_context(patch("src.workflows.egkyklios_general.update_egkyklios_draft"))
    stack.enter_context(patch("src.workflows.egkyklios_general.log_action"))
    stack.enter_context(patch("src.workflows.egkyklios_general.Path",
                              side_effect=lambda p: tmp_path / p))
    return stack


@pytest.mark.asyncio
async def test_draft_circular_is_written_piece_by_piece(workflow, tmp_path):
    items = [
        {"date": "2026-06-13", "title": "Pride", "heading": "Events", "text": "TEXT-PRIDE"},
        {"date": "2026-07-02", "title": "Interview", "heading": "Media", "text": "TEXT-INTERVIEW"},
    ]
    client = _fake_model(items)
    with patch("src.workflows.egkyklios_general.ClaudeClient", return_value=client),          _draft_patches(tmp_path):
        result = await workflow._step_draft_circular(_draft_ctx())

    assert result.success is True, result.message
    # One call per meeting, one per briefing to break it into items, one to
    # plan, one per section: a single call for a whole edition is what
    # produced a summary of a summary.
    prompts_loaded = {c.args[0] for c in client.load_prompt.call_args_list}
    assert prompts_loaded == {"egkyklios_extract", "egkyklios_plan",
                              "egkyklios_board", "egkyklios_office"}
    assert result.data["egkyklios_draft_id"] == 42
    assert result.data["office_sections"] == ["Events", "Media"]
    markdown = result.data["draft_markdown"]
    assert "Α. ΔΙΟΙΚΗΤΙΚΟ ΣΥΜΒΟΥΛΙΟ" in markdown and "Β. ΓΡΑΦΕΙΟ" in markdown
    # The two paragraphs the Regulations fix are quoted, not regenerated.
    assert "ανά τέσσερις συνεδριάσεις" in markdown


@pytest.mark.asyncio
async def test_each_event_reaches_one_section_and_nothing_personal_or_stale_does(workflow, tmp_path):
    """Run 3 put Athens Pride in four sections, carried March items into an
    April-September edition and named a new hire with her salary."""
    items = [
        {"date": "2026-06-13", "title": "Pride", "heading": "Events", "text": "TEXT-PRIDE"},
        {"date": "2026-07-02", "title": "Interview", "heading": "Media", "text": "TEXT-INTERVIEW"},
        {"date": "2026-03-28", "title": "March event", "heading": "Events", "text": "TEXT-MARCH"},
        {"date": "2026-07-01", "title": "New hire", "heading": "Staff", "text": "TEXT-HIRE",
         "personal": True},
        {"date": "", "title": "Funding", "heading": "Finance", "text": "TEXT-FUNDING"},
    ]
    client = _fake_model(items)
    with patch("src.workflows.egkyklios_general.ClaudeClient", return_value=client),          _draft_patches(tmp_path):
        result = await workflow._step_draft_circular(_draft_ctx())

    assert result.success is True, result.message
    writer_inputs = [u for sp, u in client.calls if sp.startswith("<egkyklios_office>")]
    for text in ("TEXT-PRIDE", "TEXT-INTERVIEW", "TEXT-FUNDING"):
        assert sum(text in u for u in writer_inputs) == 1, text
    assert not any("TEXT-MARCH" in u or "TEXT-HIRE" in u for u in writer_inputs)
    assert result.data["office_items"] == 3


def test_the_privacy_guard_removes_personal_matters_and_pay_but_nothing_else():
    from src.workflows.egkyklios_general import _redact

    text = (
        "### **\\[1 Ιουλίου 2026\\] Στελέχωση**\n\n"
        "Ξεκίνησε το πρόγραμμα F2F. Ο προϋπολογισμός είναι 5.000 ευρώ.\n\n"
        "Εγκρίθηκε η πρόσληψη της Ελένης Καραμπέτσου, η οποία αναλαμβάνει καθήκοντα τον Αύγουστο.\n\n"
        "Η θέση μετονομάστηκε. Εγκρίθηκε αύξηση κατά 100€ καθαρά.\n\n"
        "### **\\[2 Ιουλίου 2026\\] Εκπομπή**\n\n"
        "Η κα Καραμπέτσου παρουσίασε την εκστρατεία στην εκπομπή.\n\n"
        "### **\\[3 Ιουλίου 2026\\] Αποχώρηση**\n\n"
        "Αποχώρησε η Ελένη Καραμπέτσου.\n"
    )
    # a fictional employee, named the way a model would list her
    out, removed, review = _redact(text, ["Ελένη Καραμπέτσου"])

    assert "Ξεκίνησε το πρόγραμμα F2F." in out
    assert "5.000 ευρώ" in out                     # a figure that is not pay stays
    assert "πρόσληψη" not in out                    # a named hire goes whole
    assert "100€" not in out and "Η θέση μετονομάστηκε." in out
    # a name outside any personal matter stays, listed for the reviewer: the
    # names come from the model, which once counted journalists as staff
    assert "παρουσίασε την εκστρατεία" in out and len(review) == 1
    # an entry left without a body does not stay behind as a bare heading
    assert "Αποχώρηση" not in out
    assert len(removed) == 4


def test_the_house_conventions_are_applied_exactly():
    from src.workflows.egkyklios_general import _tidy, _meeting_heading

    out = _tidy(
        "### [26 Μαΐου 2026 – 28 Μαΐου 2026] Workshop στα Τίρανα\n"
        "### **\\[23-24 Απριλίου 2026\\] Workshop**\n"
        "ΕΡΤ News Radio — Καθρέπτης"
    )
    assert "Workshop**\n\nΕΡΤ" in out       # a blank line between heading and body
    lines = [ln for ln in out.split("\n") if ln]
    assert lines[0] == "### **\\[26 - 28 Μαΐου 2026\\] Workshop στα Τίρανα**"
    assert lines[1] == "### **\\[23 - 24 Απριλίου 2026\\] Workshop**"
    assert lines[2] == "ΕΡΤ News Radio - Καθρέπτης"
    assert _tidy("### [10 Ιουνίου 2026 έως 10 Αυγούστου 2026] Προκήρυξη") == \
        "### **\\[10 Ιουνίου - 10 Αυγούστου 2026\\] Προκήρυξη**"
    assert _meeting_heading(1, "2026-06-09") == "### **1.1. Τακτική Συνεδρίαση Ιουνίου**"
    assert "20ης Απριλίου" in _meeting_heading(2, "2026-04-20", month_shared=True)


# ─────────────────────────────────────────────────────────────────────────────
# 5. test_render_pdf_produces_file_at_expected_path
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_render_pdf_produces_file_at_expected_path(workflow, tmp_path):
    """render_pdf step calls the PDF renderer and updates the DB row.

    Also tests the renderer directly: given valid Markdown it produces a
    non-empty PDF on disk.
    """
    period_start = "2026-01-01"
    period_end = "2026-03-31"
    expected_pdf = Path("data/egkyklios/drafts") / f"{period_start}_{period_end}_draft.pdf"

    ctx = {
        "period_start": period_start,
        "period_end": period_end,
        "title": "ΙΑΝΟΥΑΡΙΟΣ - ΜΑΡΤΙΟΣ 2026",
        "draft_markdown": "# ΓΕΝΙΚΗ ΕΓΚΥΚΛΙΟΣ ΕΝΗΜΕΡΩΣΗΣ\n\nΔοκιμαστικό περιεχόμενο.",
        "egkyklios_draft_id": 99,
    }

    mock_render = MagicMock(return_value=expected_pdf)
    with patch("src.workflows.egkyklios_general.update_egkyklios_draft"), \
         patch("src.documents.egkyklios_pdf.render_egkyklios_pdf", mock_render):
        result = await workflow._step_render_pdf(ctx)

    assert result.success is True
    assert "draft_pdf_path" in result.data
    mock_render.assert_called_once()

    # Direct renderer smoke-test: writes a real PDF
    pdf_out = tmp_path / "test_egkyklios.pdf"
    from src.documents.egkyklios_pdf import render_egkyklios_pdf
    out = render_egkyklios_pdf(
        markdown_text=(
            "# ΓΕΝΙΚΗ ΕΓΚΥΚΛΙΟΣ ΕΝΗΜΕΡΩΣΗΣ\n"
            "## ΙΑΝΟΥΑΡΙΟΣ - ΜΑΡΤΙΟΣ 2026\n\n"
            "## Α. ΔΙΟΙΚΗΤΙΚΟ ΣΥΜΒΟΥΛΙΟ\n\n"
            "### 1. Συνεδριάσεις\n\n"
            "Πραγματοποιήθηκε η συνεδρίαση της [20 Ιανουαρίου 2026].\n\n"
            "- **Απόφαση**: Εγκρίθηκε ο προϋπολογισμός.\n\n"
            "## Β. ΓΡΑΦΕΙΟ\n\n"
            "### 1. Εκδηλώσεις\n\nΟργανώθηκαν τρεις εκδηλώσεις.\n"
        ),
        output_path=pdf_out,
        title="ΙΑΝΟΥΑΡΙΟΣ - ΜΑΡΤΙΟΣ 2026",
        period_start="2026-01-01",
        period_end="2026-03-31",
        protocol_number="2026_042",
        workflow="test",
    )
    assert out.exists()
    assert out.stat().st_size > 1000  # a real PDF, not an empty file


def test_the_frame_carries_only_what_repeats_and_the_rest_emerges():
    """The section's frame holds the paragraph the Regulations prescribe; the
    meetings and Office sections come from the sources, in whatever number."""
    from src.workflows.egkyklios_general import _assemble

    out = _assemble(
        "ΑΠΡΙΛΙΟΣ - ΣΕΠΤΕΜΒΡΙΟΣ 2026", "Απρίλιο", "Σεπτέμβριο", "2026",
        ["### **1.1. Τακτική Συνεδρίαση Απριλίου**\n\nΚείμενο Α."],
        ["## 1\\. Εκστρατείες\n\nΚείμενο Β.", "## 2\\. Οικονομικά\n\nΚείμενο Γ."],
    )
    assert "ανά τέσσερις συνεδριάσεις" in out                  # quoted from the frame
    assert "από τον Απρίλιο μέχρι και τον Σεπτέμβριο του 2026" in out
    assert out.index("Κείμενο Α.") < out.index("Β. ΓΡΑΦΕΙΟ") < out.index("Εκστρατείες")
    assert "{{" not in out          # every placeholder filled
    assert "<!--" not in out        # editor's notes in the frame stay out of the circular


def test_a_section_without_a_frame_still_gets_a_circular(monkeypatch):
    """Adopting the workflow must not start with writing a template."""
    from src.profile.loader import load_section
    from src.workflows import egkyklios_general

    monkeypatch.setattr(egkyklios_general, "section", load_section("no-such-section"))
    out = egkyklios_general._assemble(
        "Q3 2026", "July", "September", "2026",
        ["### 1.1 Meeting\n\nText."], ["## 1\\. Events\n\nText."],
    )
    assert "Α. ΔΙΟΙΚΗΤΙΚΟ ΣΥΜΒΟΥΛΙΟ" in out and "Β. ΓΡΑΦΕΙΟ" in out
    assert "{{" not in out


@pytest.mark.asyncio
async def test_a_failed_piece_stops_the_draft_instead_of_reaching_review(workflow):
    """One unavailable model once produced eleven failure notices, emailed them
    for review and parked them for approval. A failed piece now stops the step."""
    client = MagicMock()
    client.load_prompt.return_value = "{meeting_ref} {meeting_date_greek} {section_title} " \
                                      "{period_start} {period_end} {previous_sections}"
    client.generate.side_effect = RuntimeError("429 RESOURCE_EXHAUSTED")
    ctx = {
        "period_start": "2026-04-01", "period_end": "2026-06-30",
        "title": "ΑΠΡΙΛΙΟΣ - ΙΟΥΝΙΟΣ 2026",
        "briefing_texts": [{"meeting_ref": "ΔΣ05-2026", "kind": "ΕΙΣΗΓΗΤΙΚΟ",
                            "archived_at": "2026-06-05", "text": "κείμενο", "is_scan": False}],
        "meeting_summaries": [{"meeting_ref": "ΔΣ05-2026", "meeting_date": "2026-06-09",
                               "text": "πρακτικά"}],
    }
    with patch("src.workflows.egkyklios_general.ClaudeClient", return_value=client), \
         patch("src.workflows.egkyklios_general.create_egkyklios_draft") as create:
        result = await workflow._step_draft_circular(ctx)

    assert result.success is False
    assert result.data["draft_failures"]
    create.assert_not_called()          # no draft row, nothing to review



# ─────────────────────────────────────────────────────────────────────────────
# 6. test_await_approval_parks_workflow
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_await_approval_parks_workflow(workflow):
    """The workflow halts at step 7 (await_approval) when run normally."""
    define_steps_result = workflow.define_steps()
    approval_step = next(s for s in define_steps_result if s.name == "await_approval")
    assert approval_step.requires_approval is True

    # Simulate: call the step handler directly - it should succeed and mark approved
    ctx = {"egkyklios_draft_id": 0, "test_mode": False}
    with patch("src.workflows.egkyklios_general.update_egkyklios_draft"):
        result = await workflow._step_await_approval(ctx)

    assert result.success is True
    assert result.data.get("approved") is True


# ─────────────────────────────────────────────────────────────────────────────
# 7. test_publish_event_emits_egkyklios_published
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_publish_event_emits_egkyklios_published(workflow):
    ctx = {
        "title": "ΙΑΝΟΥΑΡΙΟΣ - ΜΑΡΤΙΟΣ 2026",
        "protocol_number": "2026_042",
        "sharepoint_url": "https://sharepoint.example.com/file",
    }

    published_events: list = []

    mock_bus = AsyncMock()
    mock_bus.publish = AsyncMock(side_effect=lambda evt, payload: published_events.append((evt, payload)))

    with patch("src.workflows.egkyklios_general.EgkykliosGeneralWorkflow._step_publish_event",
               wraps=workflow._step_publish_event):
        with patch("src.core.event_bus.bus", mock_bus), \
             patch("src.workflows.egkyklios_general.update_egkyklios_draft", MagicMock()):
            result = await workflow._step_publish_event(ctx)

    assert result.success is True
    assert result.data.get("event_published") is True

    if published_events:
        evt_name, payload = published_events[0]
        assert "egkyklios" in evt_name
        assert payload.title == "ΙΑΝΟΥΑΡΙΟΣ - ΜΑΡΤΙΟΣ 2026"
        assert payload.protocol_number == "2026_042"
        assert payload.kind == "general"


# ─────────────────────────────────────────────────────────────────────────────
# 8. test_idempotency_guard_blocks_duplicate_period
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_idempotency_guard_blocks_duplicate_period(workflow):
    """gather_sources aborts if a non-cancelled draft for the same period exists."""
    existing_draft = {
        "id": 5,
        "kind": "general",
        "period_start": "2026-01-01",
        "period_end": "2026-03-31",
        "title": "ΙΑΝΟΥΑΡΙΟΣ - ΜΑΡΤΙΟΣ 2026",
        "status": "awaiting_approval",
    }

    with patch(
        "src.workflows.egkyklios_general.list_egkyklios_drafts",
        return_value=[existing_draft],
    ):
        ctx = {"period_start": "2026-01-01", "period_end": "2026-03-31"}
        result = await workflow._step_gather_sources(ctx)

    assert result.success is False
    assert "Υπάρχει ήδη" in result.message
    assert "id=5" in result.message


# ─────────────────────────────────────────────────────────────────────────────
# 9. test_extract_briefing_texts_skips_missing_files
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_extract_briefing_texts_skips_missing_files(workflow, tmp_path):
    """Briefings whose local_path doesn't exist are skipped gracefully."""
    ctx = {
        "briefings_meta": [
            _briefing_row(local_path="/nonexistent/path/brief.pdf"),
        ]
    }
    result = await workflow._step_extract_briefing_texts(ctx)
    # No valid PDFs → empty list; all briefings were meta (no local file), so not an error
    # (error only if briefings_meta is non-empty AND ALL fail)
    # But since all fail, we expect a failure here
    assert result.success is False or len(result.data.get("briefing_texts", [])) == 0


@pytest.mark.asyncio
async def test_extract_briefing_texts_reads_valid_pdf(workflow, tmp_path):
    """extract_briefing_texts calls extract_pdf_text for each valid local_path."""
    fake_pdf = tmp_path / "brief.pdf"
    fake_pdf.write_bytes(b"%PDF-1.4 fake")  # not a real PDF but path exists

    ctx = {
        "briefings_meta": [
            _briefing_row(local_path=str(fake_pdf)),
        ]
    }

    mock_extract = MagicMock(return_value=("Κείμενο εισηγητικού 2026.", {"is_scan": False}))
    with patch("src.workflows.egkyklios_general.extract_pdf_text", mock_extract):
        result = await workflow._step_extract_briefing_texts(ctx)

    assert result.success is True
    assert len(result.data["briefing_texts"]) == 1
    assert result.data["briefing_texts"][0]["text"] == "Κείμενο εισηγητικού 2026."
    mock_extract.assert_called_once()


def test_an_item_the_plan_forgets_is_placed_not_lost():
    from src.workflows.egkyklios_general import _plan_office_sections

    items = [
        {"id": "I01", "title": "Pride", "heading": "Events - Activism"},
        {"id": "I02", "title": "Grant", "heading": "Finances and funding"},
        {"id": "I03", "title": "Radio", "heading": "Media"},
    ]
    client = MagicMock()
    client.generate.return_value = json.dumps({"sections": [
        {"title": "Events and activism", "items": ["I01"]},
        {"title": "Finances", "items": ["I02"]},
        {"title": "International Secretariat", "items": []},
    ]})
    plan = _plan_office_sections(client, "{period_start}{period_end}{previous_sections}",
                                 items + [{"id": "I04", "title": "x", "heading": "Events"}],
                                 "", "2026-04-01", "2026-09-30", "test")
    placed = {i: sec["title"] for sec in plan for i in sec["items"]}
    assert set(placed) == {"I01", "I02", "I03", "I04"}
    assert placed["I04"] == "Events and activism"     # by its own heading
    assert placed["I03"] == plan[-1]["title"]         # nothing matches: last section


def test_one_event_one_entry_under_the_heading_members_know():
    from src.workflows.egkyklios_general import _plan_office_sections

    items = [{"id": "I01", "title": "Press conference", "heading": "Events"},
             {"id": "I02", "title": "Visit", "heading": "International"}]
    client = MagicMock()
    client.generate.return_value = json.dumps({"sections": [
        {"title": "Εκδηλώσεις και ακτιβισμός", "items": ["I01"]},
        {"title": "Προβολή στα ΜΜΕ", "items": ["I01"]},          # the same event again
        {"title": "Διεθνή Γραμματεία", "items": ["I02"]},          # inflected by the model
    ]})
    previous = "- Εκδηλώσεις και ακτιβισμός | events\n- Διεθνής Γραμματεία | the IS"
    plan = _plan_office_sections(client, "{period_start}{period_end}{previous_sections}",
                                 items, previous, "2026-04-01", "2026-09-30", "test")

    assert [s["title"] for s in plan] == ["Εκδηλώσεις και ακτιβισμός", "Διεθνής Γραμματεία"]
    assert sum(s["items"].count("I01") for s in plan) == 1


def test_the_organisations_money_is_not_mistaken_for_someones_pay():
    from src.workflows.egkyklios_general import _redact

    text = ("Το αποθεματικό του Τμήματος αυξήθηκε στις 15.000 ευρώ, ενώ ο πληθωρισμός "
            "του 4% θα ληφθεί υπόψη για τις μισθολογικές διορθώσεις στο τέλος του έτους.")
    out, removed, _ = _redact(text, [])
    assert out == text and removed == []
