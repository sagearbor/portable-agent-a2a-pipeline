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
    # Legacy states (keep for backward compat with existing code)
    PROCESSING        = "processing"
    REVIEW_PENDING    = "review_pending"
    APPROVED          = "approved"
    CANCELLED         = "cancelled"
    EXPIRED           = "expired"
    COMPLETED         = "completed"
    # PRP §4.5 state machine states
    IDLE                    = "idle"
    AWAITING_PROJECT        = "awaiting_project"
    AWAITING_SERIES_CONFIRM = "awaiting_series_confirm"
    LIVE_MEETING            = "live_meeting"
    MEETING_ENDED           = "meeting_ended"
    CREATING                = "creating"
    COMPLETE                = "complete"


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
    """
    State for one transcript processing run.

    Required fields (must be supplied at construction):
        session_id    — UUID string
        user_id       — Teams user who triggered the session
        channel_id    — Teams channel to post updates to
        meeting_title — human-readable meeting name

    Optional fields with defaults:
        state            — starts as IDLE; set to PROCESSING by legacy create()
        jira_base_url    — set after user runs "@sageJiraBot use jira <url>"
        project_key      — set after jira_base_url is verified
        series_master_id — Graph API seriesMasterId for recurring meetings

    jira_base_url, project_key, and series_master_id all default to None so
    that existing call sites (card_builder.py, teams_handler.py) that do not
    pass them continue to work without modification.
    """
    session_id:       str
    user_id:          str
    channel_id:       str
    meeting_title:    str

    # State defaults to IDLE; legacy create() sets it to PROCESSING explicitly
    state:            SessionState = SessionState.IDLE

    # Jira targeting — optional at construction; set during session setup flow
    jira_base_url:    Optional[str] = None   # None until 'use jira' command verified
    project_key:      Optional[str] = None   # None until project specified

    # Recurring meeting series key (PRP §4.8)
    series_master_id: Optional[str] = None   # None if not a recurring meeting

    draft_tickets:    list = field(default_factory=list)   # list[DraftTicket]
    created_at:       datetime = field(default_factory=datetime.utcnow)
    card_activity_id: Optional[str] = None  # Teams activity ID for updating the card
    transcript:       str = ""
    awaiting_paste:   bool = False           # True after 'paste' prompt, before transcript received


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
        user_id:          str,
        channel_id:       str,
        project_key:      Optional[str] = None,
        meeting_title:    str = "Meeting",
        transcript:       str = "",
        jira_base_url:    Optional[str] = None,
        series_master_id: Optional[str] = None,
        state:            Optional["SessionState"] = None,
    ) -> BotSession:
        """
        Create and store a new session.

        Default state is PROCESSING (legacy behaviour, keeps teams_handler.py
        working without modification — it checks
        ``session.state == SessionState.PROCESSING`` after a paste prompt).

        Pass ``state=SessionState.IDLE`` explicitly when using the new PRP §4.5
        flow (or just call create_session() which always uses IDLE).

        Returns the created BotSession.
        """
        session_id = str(uuid.uuid4())
        initial_state = state if state is not None else SessionState.PROCESSING
        session = BotSession(
            session_id=session_id,
            user_id=user_id,
            channel_id=channel_id,
            project_key=project_key,
            meeting_title=meeting_title,
            state=initial_state,
            transcript=transcript,
            jira_base_url=jira_base_url,
            series_master_id=series_master_id,
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

        Includes both legacy states (PROCESSING, REVIEW_PENDING) and PRP §4.5
        states (IDLE through MEETING_ENDED) so both code paths work correctly.
        """
        active_states = {
            # Legacy
            SessionState.PROCESSING,
            SessionState.REVIEW_PENDING,
            # PRP §4.5
            SessionState.IDLE,
            SessionState.AWAITING_PROJECT,
            SessionState.AWAITING_SERIES_CONFIRM,
            SessionState.LIVE_MEETING,
            SessionState.MEETING_ENDED,
            SessionState.CREATING,
        }
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
        """Legacy alias for expire_old_sessions()."""
        return self.expire_old_sessions(timeout_minutes)

    def expire_old_sessions(self, timeout_minutes: int = 30) -> list[str]:
        """
        Mark sessions that have been in a pending/waiting state longer than
        timeout_minutes as EXPIRED.

        Sessions eligible for expiry: REVIEW_PENDING, MEETING_ENDED.
        Mid-flight states (LIVE_MEETING, CREATING, PROCESSING) are excluded
        because they represent active operations, not idle waiting.

        Returns list of expired session IDs.

        Call this periodically (e.g., via a FastAPI background task every 5 min).
        """
        expirable = {SessionState.REVIEW_PENDING, SessionState.MEETING_ENDED}
        cutoff = datetime.utcnow() - timedelta(minutes=timeout_minutes)
        expired_ids: list[str] = []
        for session in self._sessions.values():
            if session.state in expirable and session.created_at < cutoff:
                session.state = SessionState.EXPIRED
                expired_ids.append(session.session_id)
        return expired_ids

    # PRP spec aliases for create / get / update / get_active_for_user

    def create_session(
        self,
        user_id:          str,
        channel_id:       str,
        meeting_title:    str,
        project_key:      Optional[str] = None,
        jira_base_url:    Optional[str] = None,
        series_master_id: Optional[str] = None,
        transcript:       str = "",
    ) -> BotSession:
        """
        PRP spec name for create().  Creates session in IDLE state.

        New code (Phase 2 handler) should call this instead of create().
        Returns the created BotSession.
        """
        return self.create(
            user_id=user_id,
            channel_id=channel_id,
            meeting_title=meeting_title,
            project_key=project_key,
            jira_base_url=jira_base_url,
            series_master_id=series_master_id,
            transcript=transcript,
            state=SessionState.IDLE,
        )

    def get_session(self, session_id: str) -> Optional[BotSession]:
        """PRP spec alias for get()."""
        return self.get(session_id)

    def get_active_session_for_user(self, user_id: str) -> Optional[BotSession]:
        """PRP spec alias for get_active_for_user()."""
        return self.get_active_for_user(user_id)

    def update_session(self, session: BotSession) -> None:
        """PRP spec alias for update()."""
        self.update(session)

    def all_sessions(self) -> list[BotSession]:
        """Return all sessions (for debugging/admin)."""
        return list(self._sessions.values())


# ---------------------------------------------------------------------------
# Module-level singleton — import and use directly:
#   from bot.session_store import session_store
# ---------------------------------------------------------------------------
session_store = SessionStore()
