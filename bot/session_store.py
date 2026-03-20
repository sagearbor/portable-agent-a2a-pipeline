"""
In-memory session store for bot review sessions.
Each session tracks the state of one transcript processing run.

In production (Phase 2), replace the in-memory dict backend with
Azure Table Storage while keeping the same SessionStore interface.
"""

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional


class SessionState(Enum):
    PROCESSING        = "processing"
    REVIEW_PENDING    = "review_pending"
    APPROVED          = "approved"
    CANCELLED         = "cancelled"
    EXPIRED           = "expired"
    COMPLETED         = "completed"


@dataclass
class DraftTicket:
    """One draft Jira ticket within a review session."""
    draft_id:               str
    summary:                str
    description:            str
    priority:               str
    suggested_epic_key:     Optional[str] = None
    suggested_epic_summary: Optional[str] = None
    approved:               bool = True  # default approved; user can uncheck


@dataclass
class BotSession:
    """State for one transcript processing run."""
    session_id:       str
    user_id:          str
    channel_id:       str
    project_key:      str
    meeting_title:    str
    state:            SessionState
    draft_tickets:    list[DraftTicket] = field(default_factory=list)
    created_at:       datetime = field(default_factory=datetime.utcnow)
    card_activity_id: Optional[str] = None  # Teams activity ID for updating the card
    transcript:       str = ""


class SessionStore:
    """
    In-memory session store.

    Thread safety: good enough for single-process uvicorn.
    For multi-worker deployments, replace with Azure Table Storage or Redis.
    """

    def __init__(self) -> None:
        self._sessions: dict[str, BotSession] = {}

    def create(
        self,
        user_id:       str,
        channel_id:    str,
        project_key:   str,
        meeting_title: str,
        transcript:    str = "",
    ) -> BotSession:
        """
        Create a new session in PROCESSING state.
        Returns the created BotSession.
        """
        session_id = str(uuid.uuid4())
        session = BotSession(
            session_id=session_id,
            user_id=user_id,
            channel_id=channel_id,
            project_key=project_key,
            meeting_title=meeting_title,
            state=SessionState.PROCESSING,
            transcript=transcript,
        )
        self._sessions[session_id] = session
        return session

    def get(self, session_id: str) -> Optional[BotSession]:
        """Return session by ID, or None if not found."""
        return self._sessions.get(session_id)

    def get_active_for_user(self, user_id: str) -> Optional[BotSession]:
        """
        Return the most recent active (non-completed/cancelled/expired) session
        for a given user, or None.
        """
        active_states = {SessionState.PROCESSING, SessionState.REVIEW_PENDING}
        candidates = [
            s for s in self._sessions.values()
            if s.user_id == user_id and s.state in active_states
        ]
        if not candidates:
            return None
        # Return the most recently created
        return max(candidates, key=lambda s: s.created_at)

    def update(self, session: BotSession) -> None:
        """Persist an updated session object back to the store."""
        self._sessions[session.session_id] = session

    def delete(self, session_id: str) -> None:
        """Remove a session from the store."""
        self._sessions.pop(session_id, None)

    def expire_old(self, timeout_minutes: int = 30) -> list[str]:
        """
        Mark sessions that have been in REVIEW_PENDING longer than
        timeout_minutes as EXPIRED.

        Returns list of expired session IDs.

        Call this periodically (e.g., via a FastAPI background task every 5 min).
        """
        cutoff = datetime.utcnow() - timedelta(minutes=timeout_minutes)
        expired_ids = []
        for session in self._sessions.values():
            if (
                session.state == SessionState.REVIEW_PENDING and
                session.created_at < cutoff
            ):
                session.state = SessionState.EXPIRED
                expired_ids.append(session.session_id)
        return expired_ids

    def all_sessions(self) -> list[BotSession]:
        """Return all sessions (for debugging/admin)."""
        return list(self._sessions.values())


# ---------------------------------------------------------------------------
# Module-level singleton — import and use directly:
#   from bot.session_store import session_store
# ---------------------------------------------------------------------------
session_store = SessionStore()
