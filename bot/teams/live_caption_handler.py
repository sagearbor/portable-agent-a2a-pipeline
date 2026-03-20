"""
Real-time caption buffering for live Teams meeting sessions.

STUB: Requires the bot to be added as a participant to the meeting AND
Microsoft Graph streaming API permissions. Scaffold only — not functional
until IT provisions the required permissions and the bot is deployed.

Background:
  Teams provides two transcript paths:
  A) Post-meeting VTT file via Graph API (RECOMMENDED — Phase 2 MVP)
     Requires: OnlineMeetings.Read.All + OnlineMeetingTranscript.Read.All
  B) Real-time captions via Teams Real-Time Media SDK (C# only as of 2026-03)
     Requires: a C# media bot sidecar (no Python SDK available)

This module scaffolds path A's meeting session management:
  - Bot receives a change notification webhook when transcript is ready
  - Bot fetches the VTT transcript via Graph API
  - Bot posts to /api/v1/process-transcript for pipeline processing

When IT provisions the bot:
  1. Bot Framework SDK calls on_teams_meeting_participant_join_activity()
  2. Bot stores the meeting session in LiveCaptionSession
  3. At meeting end (change notification), fetch transcript from Graph
  4. Process full transcript through the pipeline

See also:
  - bot/graph/transcript_fetcher.py  for the Graph API fetch stub
  - bot/api/routes/transcript.py     for the pipeline entry point
  - docs/it-request-teams-bot.md     for the IT request
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class LiveCaptionSession:
    """
    Tracks the state of a live meeting session.

    Created when the bot joins a meeting or receives a meeting start webhook.
    Used to accumulate captions and trigger processing at meeting end.
    """
    meeting_id:     str
    project_key:    str
    caption_buffer: list[str] = field(default_factory=list)
    started_at:     datetime = field(default_factory=datetime.utcnow)
    meeting_title:  str = "Live Meeting"
    organizer_id:   str = ""       # Azure AD user ID of meeting organizer
    channel_id:     str = ""       # Teams channel to post results to
    user_id:        str = ""       # User who triggered the bot

    def append_caption(self, speaker: str, text: str) -> None:
        """
        Add a caption line to the buffer.

        Args:
            speaker: Display name of the speaker
            text:    Caption text for this speaker turn
        """
        self.caption_buffer.append(f"{speaker}: {text}")

    def get_full_transcript(self) -> str:
        """Return full buffered transcript as plain text."""
        return "\n".join(self.caption_buffer)

    def caption_count(self) -> int:
        """Return number of caption lines buffered."""
        return len(self.caption_buffer)


class LiveCaptionSessionManager:
    """
    In-memory manager for active meeting caption sessions.
    One session per meeting.
    """

    def __init__(self) -> None:
        self._sessions: dict[str, LiveCaptionSession] = {}

    def start_session(
        self,
        meeting_id:    str,
        project_key:   str,
        meeting_title: str = "Live Meeting",
        organizer_id:  str = "",
        channel_id:    str = "",
        user_id:       str = "",
    ) -> LiveCaptionSession:
        """
        Start a new live caption session for a meeting.
        Overwrites any existing session with the same meeting_id.
        """
        session = LiveCaptionSession(
            meeting_id=meeting_id,
            project_key=project_key,
            meeting_title=meeting_title,
            organizer_id=organizer_id,
            channel_id=channel_id,
            user_id=user_id,
        )
        self._sessions[meeting_id] = session
        return session

    def get_session(self, meeting_id: str) -> Optional[LiveCaptionSession]:
        """Return session for meeting_id, or None."""
        return self._sessions.get(meeting_id)

    def end_session(self, meeting_id: str) -> Optional[LiveCaptionSession]:
        """
        End and remove a session.
        Returns the final session object (with full caption buffer).
        """
        return self._sessions.pop(meeting_id, None)

    def all_sessions(self) -> list[LiveCaptionSession]:
        """Return all active sessions."""
        return list(self._sessions.values())


# Module-level singleton
live_caption_manager = LiveCaptionSessionManager()


# ---------------------------------------------------------------------------
# Bot Framework activity handlers (stubs — called by teams_handler.py)
# ---------------------------------------------------------------------------

async def on_meeting_start(meeting_id: str, organizer_id: str, project_key: str, channel_id: str, user_id: str) -> LiveCaptionSession:
    """
    Called when the bot is notified of a meeting start.

    STUB: In production, triggered by Bot Framework meeting lifecycle events
    or Microsoft Graph change notification subscriptions.

    Args:
        meeting_id:    Teams online meeting ID
        organizer_id:  Azure AD user ID of the meeting organizer
        project_key:   Jira project key for this meeting's channel
        channel_id:    Teams channel to post results to
        user_id:       Teams user who triggered the bot

    Returns:
        The new LiveCaptionSession.
    """
    session = live_caption_manager.start_session(
        meeting_id=meeting_id,
        project_key=project_key,
        organizer_id=organizer_id,
        channel_id=channel_id,
        user_id=user_id,
    )
    print(f"[live_caption] Meeting started: {meeting_id} (project: {project_key})")
    return session


async def on_meeting_end(meeting_id: str) -> Optional[str]:
    """
    Called when a meeting ends (via Bot Framework or change notification).

    STUB: In production, fetches the VTT transcript from Graph API and
    posts it to the pipeline.

    Returns:
        The full transcript text, or None if no session was found.
    """
    session = live_caption_manager.end_session(meeting_id)
    if session is None:
        print(f"[live_caption] No active session for meeting {meeting_id}")
        return None

    transcript = session.get_full_transcript()
    print(
        f"[live_caption] Meeting ended: {meeting_id}. "
        f"Buffered {session.caption_count()} caption lines."
    )

    if not transcript.strip():
        # No captions buffered — try fetching from Graph API
        print("[live_caption] No buffered captions. Attempting Graph API fetch (STUB)...")
        raise NotImplementedError(
            "Graph API transcript fetch not yet implemented. "
            "Requires IT to provision OnlineMeetings.Read.All + OnlineMeetingTranscript.Read.All. "
            "See docs/it-request-teams-bot.md."
        )

    return transcript
