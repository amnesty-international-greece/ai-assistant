"""Zoom integration — schedule meetings and retrieve recordings/transcripts."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import httpx

from src.config import settings
from src.core.audit import log_action

logger = logging.getLogger(__name__)

_ZOOM_API_BASE = "https://api.zoom.us/v2"
_ZOOM_AUTH_URL = "https://zoom.us/oauth/token"


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
