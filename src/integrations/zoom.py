"""Zoom integration — schedule meetings and retrieve recordings/transcripts."""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from src.config import settings
from src.core.audit import log_action

logger = logging.getLogger(__name__)

_ZOOM_API_BASE = "https://api.zoom.us/v2"
_ZOOM_AUTH_URL = "https://zoom.us/oauth/token"

# Video asset markers (case-insensitive) — matched against a recording file's
# ``file_type`` and ``recording_type``. When ``audio_only=True`` (the default
# for the minutes pipeline) any file matching one of these is skipped: we never
# need the large MP4/screen-share/gallery/active-speaker recordings.
_VIDEO_MARKERS = {
    "mp4",
    "shared_screen_with_speaker_view",
    "shared_screen_with_gallery_view",
    "active_speaker",
    "gallery_view",
    "shared_screen",
}


def _is_video_asset(rec: dict[str, Any]) -> bool:
    """True if a recording file looks like a video asset (skipped by default)."""
    file_type = (rec.get("file_type") or "").strip().lower()
    recording_type = (rec.get("recording_type") or "").strip().lower()
    return file_type in _VIDEO_MARKERS or recording_type in _VIDEO_MARKERS


def _encode_uuid(uuid: str) -> str:
    """URL-encode a Zoom meeting UUID for use in an API path.

    Zoom requires *double* URL-encoding for a UUID that begins with ``/`` or
    contains ``//``; otherwise the raw value is used unchanged.  See
    https://developers.zoom.us/docs/api/ ("Double encode the UUID ...").

    Args:
        uuid: The meeting UUID (or numeric meeting ID).

    Returns:
        The path-safe identifier, double-encoded when required.
    """
    if uuid.startswith("/") or "//" in uuid:
        # Double-encode: encode once, then encode the result again.
        return quote(quote(uuid, safe=""), safe="")
    return uuid


class ZoomClient:
    """Client for Zoom API operations."""

    def __init__(self) -> None:
        self._token: str | None = None
        self._token_expiry: datetime | None = None

    async def _get_token(self) -> str:
        """Acquire access token using Server-to-Server OAuth.

        Returns:
            Bearer access token string.

        Raises:
            httpx.HTTPStatusError: If the token request fails.
        """
        async with httpx.AsyncClient() as client:
            response = await client.post(
                _ZOOM_AUTH_URL,
                params={"grant_type": "account_credentials", "account_id": settings.zoom_account_id},
                auth=(settings.zoom_client_id, settings.zoom_client_secret),
            )
            response.raise_for_status()
            data = response.json()
            self._token = data["access_token"]
            return self._token

    async def _headers(self) -> dict[str, str]:
        token = self._token or await self._get_token()
        return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    async def schedule_meeting(
        self,
        topic: str,
        start_time: str,
        duration: int | None = None,
        agenda: str = "",
        workflow: str = "zoom",
    ) -> dict[str, Any]:
        """Schedule a Zoom meeting with registration enabled.

        Current configuration:
          - type 2: one-time scheduled meeting
          - registration required (approval_type=0 → auto-approve)
          - waiting_room: False  (registration is the gate, not the waiting room)
          - join_before_host: False  (host must start the meeting first)
          - mute_upon_entry: True
          - auto_recording: cloud  (full transcript/recording for minutes)
          - participant_video: True, host_video: True

        Registration model:
          Board members are pre-registered via add_registrants() — they receive
          a personal join link directly from Zoom without filling in a form.
          All other participants (regular members, observers) must register via
          the public registration URL (the join_url returned here).  They are
          auto-approved and receive their personalised link by email from Zoom.

        Args:
            topic: Meeting title.
            start_time: ISO 8601 datetime string (e.g. "2026-04-14T20:30:00").
            duration: Duration in minutes (default from config).
            agenda: Meeting agenda text shown in Zoom.
            workflow: Workflow name for audit logging.

        Returns:
            Zoom API meeting object.  Key fields:
              id          — numeric meeting ID
              join_url    — public registration URL (share with non-board members)
              password    — meeting passcode
        """
        duration = duration or settings.zoom.meeting_defaults.duration
        payload = {
            "topic": topic,
            "type": 2,           # Scheduled one-time meeting
            "start_time": start_time,
            "duration": duration,
            "timezone": settings.zoom.meeting_defaults.timezone,
            "agenda": agenda,
            "settings": {
                "approval_type":     0,      # Registration on, auto-approve
                "registration_type": 1,      # Registrants join once
                "join_before_host":  True,   # Registrants may join before host arrives
                "waiting_room":      False,  # Registration is the gate (not waiting room)
                "mute_upon_entry":   True,
                "participant_video": True,   # Video on by default; participants can turn it off
                "host_video":        True,
                "auto_recording":    "cloud",
            },
        }

        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{_ZOOM_API_BASE}/users/me/meetings",
                headers=await self._headers(),
                json=payload,
            )
            response.raise_for_status()

        result = response.json()
        log_action(
            workflow=workflow,
            action="meeting_scheduled",
            actor="system",
            target=str(result.get("id")),
            details={
                "topic": topic,
                "start_time": start_time,
                "join_url": result.get("join_url"),
            },
        )
        logger.info("Scheduled Zoom meeting: %s at %s", topic, start_time)
        return result

    async def add_registrants(
        self,
        meeting_id: str,
        registrants: list[dict[str, str]],
        workflow: str = "zoom",
    ) -> list[dict[str, str]]:
        """Pre-register participants so they receive a personalised join link.

        Each registrant dict must have at minimum:
          {"email": "...", "first_name": "...", "last_name": "..."}

        Zoom emails each registrant their unique join link automatically.
        Returns the list of registrant records enriched with join_url.

        Args:
            meeting_id: Zoom meeting ID.
            registrants: List of registrant dicts.
            workflow: Workflow name for audit logging.
        """
        results: list[dict[str, str]] = []
        async with httpx.AsyncClient() as client:
            for person in registrants:
                response = await client.post(
                    f"{_ZOOM_API_BASE}/meetings/{meeting_id}/registrants",
                    headers=await self._headers(),
                    json=person,
                )
                response.raise_for_status()
                data = response.json()
                results.append({
                    "email":    person["email"],
                    "join_url": data.get("join_url", ""),
                    "id":       data.get("id", ""),
                })
        log_action(
            workflow=workflow,
            action="registrants_added",
            actor="system",
            target=str(meeting_id),
            details={"count": len(results)},
        )
        logger.info("Pre-registered %d participant(s) for meeting %s", len(results), meeting_id)
        return results

    async def delete_meeting(self, meeting_id: str, workflow: str = "zoom") -> None:
        """Cancel and delete a scheduled Zoom meeting.

        Args:
            meeting_id: Zoom meeting ID.
            workflow: Workflow name for audit logging.
        """
        async with httpx.AsyncClient() as client:
            response = await client.delete(
                f"{_ZOOM_API_BASE}/meetings/{meeting_id}",
                headers=await self._headers(),
            )
            response.raise_for_status()
        log_action(
            workflow=workflow,
            action="meeting_deleted",
            actor="system",
            target=str(meeting_id),
        )
        logger.info("Deleted Zoom meeting %s", meeting_id)

    async def get_recording(self, meeting_id: str) -> dict[str, Any]:
        """Get recording details for a meeting.

        Args:
            meeting_id: Zoom meeting ID or UUID.

        Returns:
            Zoom recording object with recording_files list.
        """
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{_ZOOM_API_BASE}/meetings/{meeting_id}/recordings",
                headers=await self._headers(),
            )
            response.raise_for_status()
            return response.json()

    async def download_recording_assets(
        self,
        meeting_id: str,
        *,
        dest_dir: str | None = None,
        workflow: str = "minutes",
        audio_only: bool = True,
    ) -> dict[str, Any]:
        """Download every recording asset for a meeting to local disk.

        Fetches the recording object via :meth:`get_recording`, then streams
        each entry in ``recording_files`` to ``dest_dir`` using the same bearer
        + ``follow_redirects`` pattern as :meth:`get_transcript`.  This is the
        first stage of the minutes pipeline: it lands all per-participant audio
        on disk so a later stage can transcribe/align it.

        Video assets are SKIPPED by default (``audio_only=True``): the minutes
        pipeline only needs the mixed ``audio_only`` M4A, the ``timeline`` JSON,
        chat, and transcript files — never the (large) MP4/screen-share/gallery
        recordings.  Pass ``audio_only=False`` to download video as well.

        The per-file ``recording_start`` / ``recording_end`` fields are captured
        verbatim in the manifest because they reveal each asset's time origin
        (critical for per-participant alignment downstream).

        Args:
            meeting_id: Zoom meeting ID or UUID.
            dest_dir: Target directory.  Defaults to
                ``data/recordings/{safe_uuid}/``.
            workflow: Workflow name for audit logging.
            audio_only: When True (default), skip video assets and keep only
                audio, the timeline JSON, chat, and transcript files.

        Returns:
            A manifest dict (also written to ``manifest.json`` in ``dest_dir``)::

                {
                  "meeting_uuid", "topic", "start_time", "dest_dir",
                  "files": [{"id", "file_type", "recording_type",
                             "recording_start", "recording_end",
                             "file_extension", "file_size", "local_path"}, ...]
                }
        """
        recording = await self.get_recording(meeting_id)
        meeting_uuid = recording.get("uuid") or meeting_id

        # UUIDs may contain '/', '\\', '+', '=' — make a filesystem-safe folder.
        safe_uuid = re.sub(r"[/\\+=]", "_", str(meeting_uuid))
        if dest_dir is None:
            dest_dir = str(Path("data") / "recordings" / safe_uuid)
        dest_path = Path(dest_dir)
        dest_path.mkdir(parents=True, exist_ok=True)

        files: list[dict[str, Any]] = []
        # Per-participant audio may live under EITHER key depending on Zoom's
        # response shape: the mixed assets are in ``recording_files``, while
        # separate per-participant audio has historically surfaced under
        # ``participant_audio_files``. Iterate both so we never silently miss
        # the per-participant tracks (the whole point of this fetch). The real
        # meeting spike confirms which key Zoom actually populates; capturing
        # both is robust either way and the manifest's ``source`` records it.
        tagged: list[tuple[str, dict[str, Any]]] = [
            ("recording_files", rec)
            for rec in (recording.get("recording_files", []) or [])
        ] + [
            ("participant_audio_files", rec)
            for rec in (recording.get("participant_audio_files", []) or [])
        ]
        headers = await self._headers()

        for index, (source, rec) in enumerate(tagged):
            if audio_only and _is_video_asset(rec):
                logger.debug(
                    "Skipping video asset for meeting %s (audio_only): "
                    "file_type=%r recording_type=%r id=%r",
                    meeting_uuid,
                    rec.get("file_type"),
                    rec.get("recording_type"),
                    rec.get("id") or index,
                )
                continue

            download_url = rec.get("download_url")
            if not download_url:
                logger.warning(
                    "Recording file %s for meeting %s has no download_url; skipping",
                    rec.get("id") or index, meeting_uuid,
                )
                continue

            file_ext = (rec.get("file_extension") or "").lower()
            label = rec.get("recording_type") or rec.get("file_type") or "file"
            ident = rec.get("id") or index
            filename = f"{label}_{ident}.{file_ext}" if file_ext else f"{label}_{ident}"
            local_path = dest_path / filename

            try:
                async with httpx.AsyncClient() as client:
                    response = await client.get(
                        download_url,
                        headers=headers,
                        follow_redirects=True,
                    )
                    response.raise_for_status()
                    local_path.write_bytes(response.content)
            except Exception as e:  # noqa: BLE001 — defensive per-file isolation
                logger.warning(
                    "Failed to download recording file %s for meeting %s: %s",
                    ident, meeting_uuid, e,
                )
                continue

            files.append({
                "id":              rec.get("id", ""),
                "source":          source,  # which Zoom array this came from
                "file_type":       rec.get("file_type", ""),
                "recording_type":  rec.get("recording_type", ""),
                # per-participant audio carries the speaker's name/email here
                "participant":     rec.get("participant_name") or rec.get("user_name") or "",
                "recording_start": rec.get("recording_start", ""),
                "recording_end":   rec.get("recording_end", ""),
                "file_extension":  rec.get("file_extension", ""),
                "file_size":       rec.get("file_size", ""),
                "local_path":      str(local_path),
            })

        manifest: dict[str, Any] = {
            "meeting_uuid": meeting_uuid,
            "topic":        recording.get("topic", ""),
            "start_time":   recording.get("start_time", ""),
            "dest_dir":     str(dest_path),
            "files":        files,
        }

        manifest_path = dest_path / "manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        log_action(
            workflow=workflow,
            action="recording_assets_downloaded",
            actor="system",
            target=str(meeting_uuid),
            details={"file_count": len(files)},
        )
        logger.info(
            "Downloaded %d recording asset(s) for meeting %s to %s",
            len(files), meeting_uuid, dest_path,
        )
        return manifest

    async def get_past_participants(
        self,
        meeting_id: str,
        workflow: str = "minutes",
    ) -> list[dict[str, Any]]:
        """Return the attendance list for a past meeting (best-effort).

        Primary source is the report endpoint
        (``/report/meetings/{id}/participants``), which the account is scoped
        for (``meeting:read:list_past_participants:admin``).  On a 4xx it falls
        back to ``/past_meetings/{id}/participants``.

        UUIDs that begin with ``/`` or contain ``//`` must be double-URL-encoded
        in the path; :func:`_encode_uuid` handles that.

        Args:
            meeting_id: Zoom meeting ID or UUID.
            workflow: Workflow name for audit logging.

        Returns:
            List of participant dicts, or ``[]`` on any failure (logged).
        """
        encoded = _encode_uuid(str(meeting_id))
        headers = await self._headers()
        params = {"page_size": 300}

        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"{_ZOOM_API_BASE}/report/meetings/{encoded}/participants",
                    headers=headers,
                    params=params,
                )
                if response.status_code >= 400 and response.status_code < 500:
                    # Fall back to the non-report past-meetings endpoint.
                    response = await client.get(
                        f"{_ZOOM_API_BASE}/past_meetings/{encoded}/participants",
                        headers=headers,
                        params=params,
                    )
                response.raise_for_status()
                data = response.json()
        except Exception as e:  # noqa: BLE001 — best-effort
            logger.warning(
                "Could not fetch participants for meeting %s: %s", meeting_id, e,
            )
            return []

        participants = data.get("participants", []) or []
        logger.info(
            "Fetched %d participant(s) for meeting %s", len(participants), meeting_id,
        )
        return participants

    async def get_transcript(self, meeting_id: str) -> str | None:
        """Download the transcript for a meeting recording.

        Args:
            meeting_id: Zoom meeting ID or UUID.

        Returns:
            Transcript text, or None if not available.
        """
        recording = await self.get_recording(meeting_id)
        for file in recording.get("recording_files", []):
            if file.get("file_type") == "TRANSCRIPT":
                async with httpx.AsyncClient() as client:
                    response = await client.get(
                        file["download_url"],
                        headers=await self._headers(),
                        follow_redirects=True,
                    )
                    response.raise_for_status()
                    log_action(
                        workflow="zoom",
                        action="transcript_downloaded",
                        actor="system",
                        target=meeting_id,
                    )
                    return response.text
        return None

    async def list_recordings(
        self,
        from_date: str = "",
        to_date: str = "",
    ) -> list[dict[str, Any]]:
        """List cloud recordings for the account.

        Args:
            from_date: Start date (YYYY-MM-DD). Defaults to 30 days ago.
            to_date: End date (YYYY-MM-DD). Defaults to today.

        Returns:
            List of meeting dicts with id, topic, start_time, duration.
        """
        if not from_date:
            from datetime import timedelta
            from_date = (datetime.utcnow() - timedelta(days=30)).strftime("%Y-%m-%d")
        if not to_date:
            to_date = datetime.utcnow().strftime("%Y-%m-%d")

        params = {"from": from_date, "to": to_date, "page_size": 100}
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{_ZOOM_API_BASE}/users/me/recordings",
                headers=await self._headers(),
                params=params,
            )
            response.raise_for_status()
            data = response.json()

        meetings = data.get("meetings", [])
        logger.info("Found %d recordings between %s and %s", len(meetings), from_date, to_date)
        return meetings

    async def delete_recording(self, meeting_id: str) -> None:
        """Delete a meeting recording (for GDPR compliance after processing).

        Args:
            meeting_id: Zoom meeting ID or UUID.
        """
        async with httpx.AsyncClient() as client:
            response = await client.delete(
                f"{_ZOOM_API_BASE}/meetings/{meeting_id}/recordings",
                headers=await self._headers(),
            )
            response.raise_for_status()
        log_action(
            workflow="zoom",
            action="recording_deleted",
            actor="system",
            target=meeting_id,
        )
        logger.info("Deleted recording for meeting %s", meeting_id)
