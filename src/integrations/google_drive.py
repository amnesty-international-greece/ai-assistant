"""Google Drive, Sheets, and Docs integration."""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from googleapiclient.errors import HttpError as _HttpError


def _batch_update_with_retry(service, doc_id: str, body: dict, max_retries: int = 3) -> dict:
    """Execute a Docs batchUpdate with exponential backoff on transient errors (5xx)."""
    delay = 2.0
    for attempt in range(max_retries):
        try:
            return service.documents().batchUpdate(documentId=doc_id, body=body).execute()
        except _HttpError as e:
            if e.resp.status in (429, 500, 502, 503, 504) and attempt < max_retries - 1:
                logger.warning(
                    "batchUpdate transient error %s (attempt %d/%d) - retrying in %.0fs",
                    e.resp.status, attempt + 1, max_retries, delay,
                )
                time.sleep(delay)
                delay *= 2
                continue
            raise

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

from src.core.audit import log_action

logger = logging.getLogger(__name__)

_SCOPES = [
    "https://www.googleapis.com/auth/drive",          # copy + delete temp docs
    "https://www.googleapis.com/auth/documents",       # fill placeholders
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/gmail.send",
]

_TOKEN_PATH = Path("data/google_token.json")
_CREDENTIALS_PATH = Path("data/google_credentials.json")


class GoogleClient:
    """Client for Google Drive, Sheets, Docs, and Gmail APIs."""

    def __init__(self) -> None:
        self._creds: Credentials | None = None
        self._drive_service = None
        self._sheets_service = None
        self._docs_service = None

    def authenticate(self, *, force_interactive: bool = False) -> None:
        """Authenticate with Google APIs using OAuth2 flow.

        Loads cached credentials from disk if available and still valid.
        Falls back to running the local OAuth server flow for first-time auth.
        If the cached refresh token is revoked (``invalid_grant``), the stale
        token file is wiped and the interactive flow runs.

        Args:
            force_interactive: Skip the cache entirely and run the browser
                flow.  Use this when switching Google accounts.
        """
        from google.auth.exceptions import RefreshError

        creds = None
        if not force_interactive and _TOKEN_PATH.exists():
            creds = Credentials.from_authorized_user_file(str(_TOKEN_PATH), _SCOPES)

        if force_interactive or not creds or not creds.valid:
            refreshed = False
            if not force_interactive and creds and creds.expired and creds.refresh_token:
                try:
                    creds.refresh(Request())
                    refreshed = True
                except RefreshError as exc:
                    # Token was revoked or the Google account changed -
                    # wipe the stale file and fall through to interactive.
                    logger.warning("Google token refresh failed (%s) - re-authenticating", exc)
                    try:
                        _TOKEN_PATH.unlink()
                    except OSError:
                        pass
                    creds = None
            if not refreshed:
                flow = InstalledAppFlow.from_client_secrets_file(str(_CREDENTIALS_PATH), _SCOPES)
                creds = flow.run_local_server(port=0)
            _TOKEN_PATH.write_text(creds.to_json())

        self._creds = creds
        self._drive_service = build("drive", "v3", credentials=creds)
        self._sheets_service = build("sheets", "v4", credentials=creds)
        self._docs_service = build("docs", "v1", credentials=creds)
        logger.info("Google APIs authenticated")

    def _ensure_authenticated(self) -> None:
        if not self._creds:
            self.authenticate()

    # ── Google Docs ──────────────────────────────────────────────────────────

    def copy_document(self, template_id: str, title: str) -> str:
        """Copy a Google Doc and return the new file's ID.

        Args:
            template_id: The Drive file ID of the source document.
            title: Title for the new copy.

        Returns:
            Drive file ID of the newly created copy.
        """
        self._ensure_authenticated()
        body = {"name": title}
        result = self._drive_service.files().copy(
            fileId=template_id, body=body
        ).execute()
        new_id = result["id"]
        log_action(
            workflow="google_drive",
            action="doc_copied",
            actor="system",
            target=template_id,
            details={"new_id": new_id, "title": title},
        )
        logger.info("Copied template %s → %s (%s)", template_id, new_id, title)
        return new_id

    def fill_document_template(
        self,
        doc_id: str,
        replacements: dict[str, str],
        zoom_url: str = "",
    ) -> None:
        """Replace placeholder text in a Google Doc using batchUpdate.

        Each key in `replacements` is searched (case-sensitive) and replaced
        with its corresponding value throughout the document.

        Special handling:
        - Agenda items: if replacements contain numbered "1. [ΘΕΜΑ]" patterns,
          those are replaced first, then any remaining "[ΘΕΜΑ]" are cleared.
          A final catch-all replaces any remaining unnumbered "[ΘΕΜΑ]".
        - Zoom link: if replacements include "[ZOOM_PLACEHOLDER]" in any value
          AND zoom_url is provided, "Zoom" is inserted at that position as a
          hyperlink with black text (no blue default colour).

        Args:
            doc_id: The Drive file ID of the document to edit.
            replacements: {search_text: replacement_text} mapping.
            zoom_url: Optional Zoom join URL.  When provided the literal text
                "[ZOOM_PLACEHOLDER]" in the replacements is swapped for "Zoom"
                with an active hyperlink coloured black.
        """
        self._ensure_authenticated()

        # Build ordered request list: agenda numbered patterns last so the
        # catch-all "[ΘΕΜΑ]" fires only after numbered ones are consumed.
        # _agenda_items_ is a special list key handled separately via character indices
        agenda_items: list[str] = replacements.get("_agenda_items_", [])
        core_replacements = {k: v for k, v in replacements.items() if not k.startswith("_")}

        requests: list[dict] = [
            {
                "replaceAllText": {
                    "containsText": {"text": search, "matchCase": True},
                    "replaceText": replace,
                }
            }
            for search, replace in core_replacements.items()
        ]

        result = _batch_update_with_retry(self._docs_service, doc_id, {"requests": requests})
        total = sum(
            r.get("replaceAllText", {}).get("occurrencesChanged", 0)
            for r in result.get("replies", [])
        )
        log_action(
            workflow="google_docs",
            action="template_filled",
            actor="system",
            target=doc_id,
            details={"replacements": len(core_replacements), "occurrences_changed": total},
        )
        logger.info("Filled template %s: %d replacements, %d occurrences changed", doc_id, len(core_replacements), total)

        # ── Invitation dates (character-index approach) ───────────────────────
        # The template reuses [ΗΜΕΡΟΜΗΝΙΑ] for both the letterhead issue date and
        # the body meeting date; each occurrence is filled separately so they can
        # carry different values.
        date_slots = replacements.get("_invitation_dates_")
        if isinstance(date_slots, dict):
            self._fill_invitation_dates(
                doc_id,
                date_slots.get("issue", ""),
                date_slots.get("meeting", ""),
            )

        # ── Agenda items (character-index approach) ───────────────────────────
        # replaceAllText cannot target individual occurrences of the same text,
        # so each [ΘΕΜΑ] slot is filled separately using exact character positions.
        if agenda_items:
            self._fill_agenda_items(doc_id, agenda_items)

        # ── Paragraph deletions ───────────────────────────────────────────────
        # Some replacements need the entire containing paragraph removed rather
        # than just the text zeroed out (which leaves a blank line).
        paragraphs_to_delete: list[str] = replacements.get("_delete_paragraphs_", [])
        if paragraphs_to_delete:
            self._delete_paragraphs_containing(doc_id, paragraphs_to_delete)

        # ── Zoom hyperlink (black text) ───────────────────────────────────────
        # [ZOOM_PLACEHOLDER] was injected into the location replacement string;
        # we now find its character index, swap it for "Zoom", and apply the link.
        if zoom_url:
            doc = self._docs_service.documents().get(documentId=doc_id).execute()
            zoom_start = _find_text_start_index(doc["body"], "[ZOOM_PLACEHOLDER]")
            if zoom_start is not None:
                # Replace the placeholder AND apply link style in one batch.
                # replaceAllText fires first; "Zoom" then occupies zoom_start → zoom_start+4.
                _batch_update_with_retry(self._docs_service, doc_id, {"requests": [
                    {
                        "replaceAllText": {
                            "containsText": {"text": "[ZOOM_PLACEHOLDER]", "matchCase": True},
                            "replaceText": "Zoom",
                        }
                    },
                    {
                        "updateTextStyle": {
                            "range": {
                                "startIndex": zoom_start,
                                "endIndex": zoom_start + 4,  # len("Zoom")
                            },
                            "textStyle": {
                                "link": {"url": zoom_url},
                                "foregroundColor": {
                                    "color": {"rgbColor": {"red": 0.0, "green": 0.0, "blue": 0.0}}
                                },
                                "underline": False,
                            },
                            "fields": "link,foregroundColor,underline",
                        }
                    },
                ]})
                logger.info("Applied black Zoom hyperlink (%s) at index %d", zoom_url, zoom_start)

    def _fill_agenda_items(self, doc_id: str, items: list[str]) -> None:
        """Replace each [ΘΕΜΑ] placeholder with its corresponding agenda item.

        Uses character-index manipulation so each occurrence of the identical
        placeholder text can be targeted individually - something replaceAllText
        cannot do.

        Handles auto-numbered Google Docs lists (no manual number prefix added)
        and plain paragraphs (number prefix added).

        Rules:
        - Extra template slots (more [ΘΕΜΑ] than items) → paragraph deleted.
        - Extra items (more items than [ΘΕΜΑ] slots) → last slot absorbs the
          remainder, each on its own line (inherits list formatting via \\n).
        """
        if not items:
            items = ["(κατόπιν ανακοίνωσης)"]

        doc = self._docs_service.documents().get(documentId=doc_id).execute()
        search = "[ΘΕΜΑ]"
        search_len = len(search)

        # ── Collect all [ΘΕΜΑ] occurrences in document order ─────────────────
        occurrences: list[dict] = []
        for elem in doc["body"]["content"]:
            para = elem.get("paragraph")
            if not para:
                continue
            for pe in para.get("elements", []):
                tr = pe.get("textRun")
                if not tr:
                    continue
                content = tr.get("content", "")
                idx = content.find(search)
                if idx != -1:
                    occurrences.append({
                        "char_start": pe["startIndex"] + idx,
                        "char_end":   pe["startIndex"] + idx + search_len,
                        "elem_start": elem["startIndex"],
                        "elem_end":   elem["endIndex"],
                        "is_list":    bool(para.get("bullet")),
                    })

        if not occurrences:
            logger.warning("No [ΘΕΜΑ] placeholders found in doc %s", doc_id)
            return

        n_slots = len(occurrences)
        n_items = len(items)
        requests: list[dict] = []

        # Process in REVERSE order so that deletions/insertions at higher indices
        # do not shift the positions of elements at lower indices.
        for i in range(n_slots - 1, -1, -1):
            occ = occurrences[i]

            if i >= n_items:
                # No item for this slot - delete the entire paragraph
                requests.append({
                    "deleteContentRange": {
                        "range": {
                            "startIndex": occ["elem_start"],
                            "endIndex":   occ["elem_end"],
                        }
                    }
                })
            else:
                # Determine the text to insert
                if i == n_slots - 1 and n_items > n_slots:
                    # Last slot absorbs all overflow; \n creates new list paragraphs
                    remaining = items[i:]
                    if occ["is_list"]:
                        text = "\n".join(remaining)
                    else:
                        text = "\n".join(f"{j + 1}. {t}" for j, t in enumerate(remaining, start=i))
                else:
                    text = items[i] if occ["is_list"] else f"{i + 1}. {items[i]}"

                # Delete [ΘΕΜΑ] then insert replacement at the same position
                requests.append({
                    "deleteContentRange": {
                        "range": {
                            "startIndex": occ["char_start"],
                            "endIndex":   occ["char_end"],
                        }
                    }
                })
                requests.append({
                    "insertText": {
                        "location": {"index": occ["char_start"]},
                        "text": text,
                    }
                })

        if requests:
            _batch_update_with_retry(self._docs_service, doc_id, {"requests": requests})
            logger.info(
                "Filled agenda: %d items into %d [ΘΕΜΑ] slots in doc %s",
                n_items, n_slots, doc_id,
            )

    def _fill_invitation_dates(self, doc_id: str, issue_date: str, meeting_date: str) -> None:
        """Fill the two [ΗΜΕΡΟΜΗΝΙΑ] slots with distinct values.

        The invitation template reuses [ΗΜΕΡΟΜΗΝΙΑ] in two places: the top-right
        letterhead (the *issue* date, always today) and the body sentence (the
        *meeting* date).  ``replaceAllText`` cannot distinguish them, so each
        occurrence is targeted by character index.

        The first occurrence in document order is the letterhead (it sits in a
        table above the body), so it receives ``issue_date``; every remaining
        occurrence receives ``meeting_date``.
        """
        self._ensure_authenticated()
        doc = self._docs_service.documents().get(documentId=doc_id).execute()
        search = "[ΗΜΕΡΟΜΗΝΙΑ]"
        search_len = len(search)

        occurrences: list[tuple[int, int]] = []  # (char_start, char_end)

        def _scan(content: list) -> None:
            for elem in content:
                para = elem.get("paragraph")
                if para:
                    for pe in para.get("elements", []):
                        tr = pe.get("textRun")
                        if not tr:
                            continue
                        idx = tr.get("content", "").find(search)
                        if idx != -1:
                            start = pe["startIndex"] + idx
                            occurrences.append((start, start + search_len))
                elif "table" in elem:
                    for row in elem["table"].get("tableRows", []):
                        for cell in row.get("tableCells", []):
                            _scan(cell.get("content", []))

        _scan(doc["body"]["content"])

        if not occurrences:
            logger.warning("No [ΗΜΕΡΟΜΗΝΙΑ] placeholders found in doc %s", doc_id)
            return

        # First occurrence (letterhead) → issue date; the rest → meeting date.
        occurrences.sort(key=lambda o: o[0])
        values = [issue_date] + [meeting_date] * (len(occurrences) - 1)

        # Apply in reverse index order so edits don't shift earlier positions.
        requests: list[dict] = []
        for (start, end), value in sorted(
            zip(occurrences, values), key=lambda pair: pair[0][0], reverse=True
        ):
            requests.append({"deleteContentRange": {"range": {"startIndex": start, "endIndex": end}}})
            requests.append({"insertText": {"location": {"index": start}, "text": value}})

        _batch_update_with_retry(self._docs_service, doc_id, {"requests": requests})
        logger.info(
            "Filled %d [ΗΜΕΡΟΜΗΝΙΑ] slot(s) in doc %s (issue=%s, meeting=%s)",
            len(occurrences), doc_id, issue_date, meeting_date,
        )

    def _delete_paragraphs_containing(self, doc_id: str, search_strings: list[str]) -> None:
        """Delete entire paragraphs whose text contains any of the given strings.

        Used instead of replaceAllText(..., "") so that no blank line is left
        behind in the document.  Walks the full document tree including table
        cells.  Processes deletions in reverse index order to maintain
        positional integrity.
        """
        doc = self._docs_service.documents().get(documentId=doc_id).execute()
        to_delete: list[tuple[int, int]] = []  # (start, end) pairs

        def _scan(content: list) -> None:
            for elem in content:
                if "paragraph" in elem:
                    full_text = "".join(
                        pe.get("textRun", {}).get("content", "")
                        for pe in elem["paragraph"].get("elements", [])
                    )
                    if any(s in full_text for s in search_strings):
                        to_delete.append((elem["startIndex"], elem["endIndex"]))
                elif "table" in elem:
                    for row in elem["table"].get("tableRows", []):
                        for cell in row.get("tableCells", []):
                            _scan(cell.get("content", []))

        _scan(doc["body"]["content"])

        if not to_delete:
            return

        # Delete from last to first so indices stay valid
        requests = [
            {"deleteContentRange": {"range": {"startIndex": s, "endIndex": e}}}
            for s, e in sorted(to_delete, reverse=True)
        ]
        _batch_update_with_retry(self._docs_service, doc_id, {"requests": requests})
        logger.info("Deleted %d paragraph(s) from doc %s", len(to_delete), doc_id)

    def delete_file(self, file_id: str, workflow: str = "google_drive") -> None:
        """Move a Drive file to trash (does not permanently delete).

        Args:
            file_id: Drive file ID to trash.
        """
        self._ensure_authenticated()
        self._drive_service.files().update(
            fileId=file_id, body={"trashed": True}
        ).execute()
        log_action(
            workflow=workflow,
            action="file_trashed",
            actor="system",
            target=file_id,
        )
        logger.info("Trashed Drive file %s", file_id)

    def export_doc_as_pdf(self, file_id: str, output_path: Path) -> Path:
        """Export a Google Doc as PDF.

        Args:
            file_id: Google Drive file ID.
            output_path: Local path to save the PDF.

        Returns:
            Path to the saved PDF.
        """
        self._ensure_authenticated()
        content = self._drive_service.files().export(
            fileId=file_id, mimeType="application/pdf"
        ).execute()
        output_path.write_bytes(content)
        log_action(
            workflow="google_drive",
            action="doc_exported_pdf",
            actor="system",
            target=file_id,
        )
        logger.info("Exported Google Doc %s as PDF to %s", file_id, output_path)
        return output_path

    # ── Google Sheets ────────────────────────────────────────────────────────

    def read_sheet(
        self,
        spreadsheet_id: str,
        range_name: str,
        value_render_option: str = "FORMATTED_VALUE",
    ) -> list[list[str]]:
        """Read data from a Google Sheet.

        Args:
            spreadsheet_id: The Sheets document ID.
            range_name: A1 notation range (e.g., 'Sheet1!A1:D10').
            value_render_option: "FORMATTED_VALUE" (default, display strings),
                "UNFORMATTED_VALUE" (raw numbers/dates as serial floats),
                or "FORMULA".

        Returns:
            List of rows, each a list of cell values.
        """
        self._ensure_authenticated()
        result = self._sheets_service.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id,
            range=range_name,
            valueRenderOption=value_render_option,
        ).execute()
        values = result.get("values", [])
        log_action(
            workflow="google_sheets",
            action="sheet_read",
            actor="system",
            target=spreadsheet_id,
            details={"range": range_name, "rows": len(values)},
        )
        return values

    def write_sheet(
        self,
        spreadsheet_id: str,
        range_name: str,
        values: list[list[str]],
    ) -> dict[str, Any]:
        """Write data to a Google Sheet.

        Args:
            spreadsheet_id: The Sheets document ID.
            range_name: A1 notation range.
            values: List of rows to write.

        Returns:
            API response dict.
        """
        self._ensure_authenticated()
        body = {"values": values}
        result = self._sheets_service.spreadsheets().values().append(
            spreadsheetId=spreadsheet_id,
            range=range_name,
            valueInputOption="USER_ENTERED",
            body=body,
        ).execute()
        log_action(
            workflow="google_sheets",
            action="sheet_written",
            actor="system",
            target=spreadsheet_id,
            details={"range": range_name, "rows": len(values)},
        )
        return result

    # Pattern enforced for ``meeting_ref`` - uppercase "ΔΣ" followed by 1-2
    # digits, hyphen, 4-digit year.  Tightening this single regex changes the
    # entire workflow's notion of what a valid reference looks like.
    _MEETING_REF_PATTERN = r"^ΔΣ\d{1,2}-\d{4}$"

    def read_meeting_ref(
        self,
        spreadsheet_id: str,
        tab_title: str | None = None,
        *,
        use_cache: bool = True,
    ) -> str:
        """Read the current ``meeting_ref`` from cell D5 of the agenda sheet.

        D5 is the **universal source of truth** for which meeting cycle the
        agenda sheet currently represents (e.g. ``ΔΣ05-2026``).  This method
        also maintains a thin local SQLite cache of the most recent valid
        value - so a transient Sheets API outage doesn't block the workflow.

        Read policy:
          1. Read D5 from the Sheet.  If valid → cache it, return it.
          2. If D5 is empty / malformed / Sheets API failed AND ``use_cache``
             is ``True``, return the most recently cached value (with a
             ``logger.warning`` so the operator sees what happened).
          3. If no cache exists either, raise - failing loudly is deliberate:
             a wrong meeting_ref would corrupt every downstream artefact
             (subject line, PDF filename, archive folder, protocol number).

        Args:
            spreadsheet_id: Spreadsheet ID.
            tab_title:      Tab the D5 lives in.  ``None`` (default) picks
                ``tabs[0]`` - there is only ever one tab on the agenda sheet
                by convention.
            use_cache:      Set to ``False`` to disable both the on-success
                cache write AND the on-failure cache fallback.  Useful in
                tests that want pure I/O.

        Returns:
            The validated meeting_ref string (e.g. ``"ΔΣ05-2026"``).

        Raises:
            RuntimeError if the Sheet is unreadable AND no cached value
            exists, or if a fresh D5 value is malformed.
        """
        import re as _re

        from src.core.audit import get_meeting_ref_cache, set_meeting_ref_cache

        def _fallback(reason: str) -> str:
            """Return the cached value or raise with the original reason."""
            if not use_cache:
                raise RuntimeError(reason)
            cached = get_meeting_ref_cache()
            if cached:
                logger.warning(
                    "read_meeting_ref: %s - falling back to cached value %r",
                    reason, cached,
                )
                return cached
            raise RuntimeError(
                f"{reason} (no cached value available either - run "
                f"`ai-assistant invite reset-sheet` to seed D5)."
            )

        try:
            self._ensure_authenticated()
            if tab_title is None:
                tabs = self.list_sheet_tabs(spreadsheet_id)
                if not tabs:
                    return _fallback(
                        f"Agenda sheet {spreadsheet_id} has no tabs"
                    )
                tab_title = tabs[0]["title"]

            rows = self.read_sheet(
                spreadsheet_id,
                f"'{tab_title}'!D5",
                value_render_option="FORMATTED_VALUE",
            )
        except Exception as exc:
            return _fallback(f"Sheets API read failed: {exc!r}")

        value = (rows[0][0] if rows and rows[0] else "").strip()
        if not value:
            return _fallback(f"D5 is empty in tab '{tab_title}'")
        if not _re.match(self._MEETING_REF_PATTERN, value):
            return _fallback(
                f"D5 value {value!r} in tab '{tab_title}' is not a valid "
                f"meeting_ref (expected 'ΔΣXX-YYYY')"
            )

        # Live D5 read succeeded - refresh the local mirror (best-effort).
        if use_cache:
            try:
                set_meeting_ref_cache(value)
            except Exception as cache_err:  # pragma: no cover - DB hiccup
                logger.warning(
                    "read_meeting_ref: live read OK but cache refresh failed: %s",
                    cache_err,
                )
        return value

    def list_sheet_tabs(self, spreadsheet_id: str) -> list[dict[str, Any]]:
        """Return all tab names and their sheet IDs for a spreadsheet.

        Returns:
            List of dicts with keys: title, sheetId, index.
        """
        self._ensure_authenticated()
        meta = self._sheets_service.spreadsheets().get(
            spreadsheetId=spreadsheet_id,
            fields="sheets.properties",
        ).execute()
        return [
            {
                "title": s["properties"]["title"],
                "sheetId": s["properties"]["sheetId"],
                "index": s["properties"]["index"],
            }
            for s in meta.get("sheets", [])
        ]

    def list_folder(self, folder_id: str) -> list[dict[str, Any]]:
        """List files in a Google Drive folder.

        Args:
            folder_id: Google Drive folder ID.

        Returns:
            List of file metadata dicts (id, name, mimeType, modifiedTime).
        """
        self._ensure_authenticated()
        results = self._drive_service.files().list(
            q=f"'{folder_id}' in parents and trashed=false",
            fields="files(id, name, mimeType, modifiedTime)",
        ).execute()
        return results.get("files", [])

    def list_docs_in_folder(self, folder_id: str) -> list[dict[str, Any]]:
        """List only Google Docs in a Drive folder (no Sheets, Slides, etc.)."""
        self._ensure_authenticated()
        results = self._drive_service.files().list(
            q=(
                f"'{folder_id}' in parents "
                "and mimeType='application/vnd.google-apps.document' "
                "and trashed=false"
            ),
            fields="files(id, name, mimeType, modifiedTime)",
            orderBy="modifiedTime desc",
        ).execute()
        return results.get("files", [])

    def read_doc_content(self, doc_id: str) -> str:
        """Read the full plain-text content of a Google Doc."""
        self._ensure_authenticated()
        doc = self._docs_service.documents().get(documentId=doc_id).execute()
        parts: list[str] = []
        for elem in doc.get("body", {}).get("content", []):
            para = elem.get("paragraph")
            if not para:
                continue
            for pe in para.get("elements", []):
                tr = pe.get("textRun")
                if tr:
                    parts.append(tr.get("content", ""))
        return "".join(parts)

    def clear_and_write_doc(self, doc_id: str, content: str) -> None:
        """Replace all content in a Google Doc with new text."""
        self._ensure_authenticated()
        doc = self._docs_service.documents().get(documentId=doc_id).execute()
        body = doc.get("body", {})
        end_index = max(
            (elem.get("endIndex", 1) for elem in body.get("content", [])),
            default=1,
        )

        requests: list[dict] = []
        if end_index > 1:
            requests.append({
                "deleteContentRange": {
                    "range": {"startIndex": 1, "endIndex": end_index - 1}
                }
            })
        requests.append({
            "insertText": {
                "location": {"index": 1},
                "text": content,
            }
        })

        _batch_update_with_retry(self._docs_service, doc_id, {"requests": requests})
        log_action(
            workflow="google_docs",
            action="doc_content_replaced",
            actor="system",
            target=doc_id,
            details={"content_length": len(content)},
        )
        logger.info("Replaced content of doc %s (%d chars)", doc_id, len(content))

    def write_structured_doc(self, doc_id: str, sections: list[dict[str, str]]) -> None:
        """Write formatted content to a Google Doc, preserving heading styles."""
        self._ensure_authenticated()

        # Step 1: Clear existing content
        doc = self._docs_service.documents().get(documentId=doc_id).execute()
        body = doc.get("body", {})
        end_index = max(
            (elem.get("endIndex", 1) for elem in body.get("content", [])),
            default=1,
        )

        requests: list[dict] = []
        if end_index > 1:
            requests.append({
                "deleteContentRange": {
                    "range": {"startIndex": 1, "endIndex": end_index - 1}
                }
            })

        # Step 2: Build the full text and track section ranges
        # Each section's text ends with \n
        full_text = ""
        section_ranges: list[tuple[int, int, str]] = []  # (start, end, style_type)

        for sec in sections:
            text = sec.get("text", "").rstrip("\n") + "\n"
            start = len(full_text) + 1  # +1 because doc content starts at index 1
            full_text += text
            end = len(full_text) + 1
            section_ranges.append((start, end, sec.get("type", "body")))

        if not full_text:
            if requests:
                _batch_update_with_retry(self._docs_service, doc_id, {"requests": requests})
            return

        # Step 3: Insert all text at once
        requests.append({
            "insertText": {
                "location": {"index": 1},
                "text": full_text,
            }
        })

        _batch_update_with_retry(self._docs_service, doc_id, {"requests": requests})

        # Step 4: Apply paragraph styles
        style_map = {
            "title": "TITLE",
            "heading": "HEADING_2",
            "body": "NORMAL_TEXT",
        }

        style_requests: list[dict] = []
        for start, end, sec_type in section_ranges:
            named_style = style_map.get(sec_type, "NORMAL_TEXT")
            style_requests.append({
                "updateParagraphStyle": {
                    "range": {"startIndex": start, "endIndex": end},
                    "paragraphStyle": {"namedStyleType": named_style},
                    "fields": "namedStyleType",
                }
            })

        if style_requests:
            _batch_update_with_retry(self._docs_service, doc_id, {"requests": style_requests})

        log_action(
            workflow="google_docs",
            action="doc_structured_write",
            actor="system",
            target=doc_id,
            details={"sections": len(sections), "content_length": len(full_text)},
        )
        logger.info("Wrote %d structured sections to doc %s (%d chars)", len(sections), doc_id, len(full_text))

    def rename_file(self, file_id: str, new_name: str) -> None:
        """Rename a file in Google Drive."""
        self._ensure_authenticated()
        self._drive_service.files().update(
            fileId=file_id, body={"name": new_name}
        ).execute()
        log_action(
            workflow="google_drive",
            action="file_renamed",
            actor="system",
            target=file_id,
            details={"new_name": new_name},
        )
        logger.info("Renamed Drive file %s → %s", file_id, new_name)

    def upload_pdf_and_share(self, local_path: Path, folder_id: str) -> str:
        """Upload a local PDF to a Google Drive folder and make it publicly viewable.

        Returns the direct download link (webContentLink) for use in emails.
        """
        self._ensure_authenticated()
        from googleapiclient.http import MediaFileUpload  # type: ignore

        file_metadata = {
            "name": local_path.name,
            "parents": [folder_id],
        }
        media = MediaFileUpload(str(local_path), mimetype="application/pdf", resumable=False)
        file = self._drive_service.files().create(
            body=file_metadata,
            media_body=media,
            fields="id,webContentLink,webViewLink",
        ).execute()

        file_id = file["id"]
        self._drive_service.permissions().create(
            fileId=file_id,
            body={"role": "reader", "type": "anyone"},
        ).execute()

        # webContentLink forces a download; webViewLink opens in Drive viewer
        link = file.get("webContentLink") or file.get("webViewLink", "")
        log_action(
            workflow="google_drive",
            action="file_shared",
            actor="system",
            target=file_id,
            details={"name": local_path.name, "folder": folder_id},
        )
        logger.info("Uploaded & shared %s → %s", local_path.name, link)
        return link

    # ── Agenda sheet reset (post-cycle housekeeping) ─────────────────────────

    def reset_agenda_sheet(self, sheet_id: str) -> dict[str, Any]:
        """Reset the agenda sheet for the next meeting cycle.

        Performs in one logical pass:
          - D5 incremented to next meeting_ref (year-aware: ΔΣ12-YYYY rolls
            over to ΔΣ01-(YYYY+1); otherwise just ΔΣXX → ΔΣ(XX+1))
          - D7, D9, D11 cleared (type / date / time)
          - D16, D17, D18 unchecked (boolean FALSE)
          - H7:K10000 cleared (agenda items)
          - Script-owned protection (description == "ai-assistant:cycle-locked")
            removed if present; the user's own protections are left alone.

        Idempotency is enforced on the Python side via workflow_state
        (see ``_find_in_progress_invite`` in webhooks.py).  No sheet cell
        is used as an idempotency marker.

        Args:
            sheet_id: Spreadsheet ID to reset.

        Returns:
            Dict summarising what changed:
              {
                "tab_title": str,
                "old_meeting_ref": str,
                "new_meeting_ref": str,
                "cleared_cells": [str, ...],
                "protections_removed": int,
              }

        Raises:
            RuntimeError: if the sheet has no tabs or D5 is unrecognisable.
        """
        import re

        self._ensure_authenticated()

        tabs = self.list_sheet_tabs(sheet_id)
        if not tabs:
            raise RuntimeError(f"Agenda sheet {sheet_id} has no tabs")
        tab = tabs[0]
        tab_title = tab["title"]
        sheet_internal_id = tab["sheetId"]

        # ── Read current meeting_ref from D5 ─────────────────────────────────
        current = self._sheets_service.spreadsheets().values().get(
            spreadsheetId=sheet_id,
            range=f"'{tab_title}'!D5",
        ).execute()
        rows = current.get("values", [])
        old_meeting_ref = (rows[0][0] if rows and rows[0] else "").strip()

        match = re.match(r"^(ΔΣ)(\d{1,2})-(\d{4})$", old_meeting_ref)
        if not match:
            raise RuntimeError(
                f"D5 value {old_meeting_ref!r} is not a recognised meeting ref "
                "(expected 'ΔΣXX-YYYY')"
            )
        prefix, seq_str, year_str = match.group(1), match.group(2), match.group(3)
        seq = int(seq_str)
        year = int(year_str)
        if seq >= 12:
            new_seq = 1
            new_year = year + 1
        else:
            new_seq = seq + 1
            new_year = year
        new_meeting_ref = f"{prefix}{new_seq:02d}-{new_year}"

        # ── Batch value updates (D5 + clears for D7/D9/D11/D16/D17/D18) ──
        # checkbox cells must be written as booleans so the checkbox stays
        # functional (clearing the value via clear() would also work but
        # batchUpdate keeps everything in one round-trip).
        value_data = [
            {"range": f"'{tab_title}'!D5",  "values": [[new_meeting_ref]]},
            {"range": f"'{tab_title}'!D7",  "values": [[""]]},
            {"range": f"'{tab_title}'!D9",  "values": [[""]]},
            {"range": f"'{tab_title}'!D11", "values": [[""]]},
            {"range": f"'{tab_title}'!D16", "values": [[False]]},
            {"range": f"'{tab_title}'!D17", "values": [[False]]},
            {"range": f"'{tab_title}'!D18", "values": [[False]]},
        ]
        self._sheets_service.spreadsheets().values().batchUpdate(
            spreadsheetId=sheet_id,
            body={"valueInputOption": "USER_ENTERED", "data": value_data},
        ).execute()

        # D5 was just written; refresh the local mirror so the next read
        # falls back to this exact value if Sheets is briefly unreachable.
        try:
            from src.core.audit import set_meeting_ref_cache
            set_meeting_ref_cache(new_meeting_ref)
        except Exception as cache_err:  # pragma: no cover - DB hiccup
            logger.warning(
                "reset_agenda_sheet: D5 written but cache refresh failed: %s",
                cache_err,
            )

        # ── Clear the agenda items block H7:K10000 ───────────────────────────
        self._sheets_service.spreadsheets().values().clear(
            spreadsheetId=sheet_id,
            range=f"'{tab_title}'!H7:K10000",
            body={},
        ).execute()

        # ── Remove only OUR script-owned protection ──────────────────────────
        meta = self._sheets_service.spreadsheets().get(
            spreadsheetId=sheet_id,
            fields="sheets(properties.sheetId,protectedRanges(protectedRangeId,description))",
        ).execute()
        protection_ids: list[int] = []
        for s in meta.get("sheets", []):
            if s.get("properties", {}).get("sheetId") != sheet_internal_id:
                continue
            for pr in s.get("protectedRanges", []) or []:
                if pr.get("description") == "ai-assistant:cycle-locked":
                    protection_ids.append(pr["protectedRangeId"])

        if protection_ids:
            requests = [
                {"deleteProtectedRange": {"protectedRangeId": pid}}
                for pid in protection_ids
            ]
            self._sheets_service.spreadsheets().batchUpdate(
                spreadsheetId=sheet_id,
                body={"requests": requests},
            ).execute()

        log_action(
            workflow="google_sheets",
            action="agenda_sheet_reset",
            actor="system",
            target=sheet_id,
            details={
                "tab": tab_title,
                "old_meeting_ref": old_meeting_ref,
                "new_meeting_ref": new_meeting_ref,
                "protections_removed": len(protection_ids),
            },
        )
        logger.info(
            "Reset agenda sheet %s: %s → %s (removed %d protection(s))",
            sheet_id, old_meeting_ref, new_meeting_ref, len(protection_ids),
        )

        return {
            "tab_title": tab_title,
            "old_meeting_ref": old_meeting_ref,
            "new_meeting_ref": new_meeting_ref,
            "cleared_cells": ["D7", "D9", "D11", "D16", "D17", "D18", "H7:K10000"],
            "protections_removed": len(protection_ids),
        }

    def get_last_row_value(
        self,
        spreadsheet_id: str,
        range_name: str,
    ) -> str | None:
        """Read the last non-empty value in the first column of a range.

        Useful for auto-incrementing protocol/decision numbers.
        """
        rows = self.read_sheet(spreadsheet_id, range_name)
        for row in reversed(rows[1:]):
            if row and row[0]:
                return row[0]
        return None


# ── Module-level helpers ──────────────────────────────────────────────────────

def _find_text_start_index(doc_body: dict, search_text: str) -> int | None:
    """Walk a Google Docs API body and return the absolute start character index
    of the first occurrence of search_text, or None if not found.

    Used to locate a placeholder after replaceAllText has run so that a
    subsequent updateTextStyle request can target the exact character range.
    """
    for elem in doc_body.get("content", []):
        paragraph = elem.get("paragraph")
        if not paragraph:
            continue
        for pe in paragraph.get("elements", []):
            text_run = pe.get("textRun")
            if not text_run:
                continue
            content = text_run.get("content", "")
            idx = content.find(search_text)
            if idx != -1:
                return pe.get("startIndex", 0) + idx
    return None
