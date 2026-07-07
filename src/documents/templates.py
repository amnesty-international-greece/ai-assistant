"""Template management - fetch and cache document templates from Google Drive."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from src.config import settings
from src.core.audit import log_action

logger = logging.getLogger(__name__)

_TEMPLATE_CACHE_DIR = Path("data/template_cache")


class TemplateManager:
    """Manages document templates stored in Google Drive."""

    def __init__(self, google_client) -> None:
        """Initialize with an authenticated GoogleClient instance."""
        self._google = google_client
        _TEMPLATE_CACHE_DIR.mkdir(parents=True, exist_ok=True)

    def get_template_as_pdf(self, template_name: str, file_id: str) -> Path:
        """Fetch a Google Doc template and export as PDF.

        Args:
            template_name: Human-readable name for logging.
            file_id: Google Drive file ID.

        Returns:
            Path to the cached PDF.
        """
        cache_path = _TEMPLATE_CACHE_DIR / f"{template_name}.pdf"
        self._google.export_doc_as_pdf(file_id, cache_path)
        log_action(
            workflow="templates",
            action="template_fetched",
            actor="system",
            target=template_name,
            details={"file_id": file_id, "format": "pdf"},
        )
        return cache_path

    def list_templates(self) -> list[dict[str, Any]]:
        """List available templates from the configured Google Drive folder."""
        folder_id = settings.google.templates_folder_id
        if not folder_id:
            logger.warning("No templates folder configured")
            return []
        return self._google.list_folder(folder_id)
