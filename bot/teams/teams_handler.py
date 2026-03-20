"""
SageJiraBot Teams activity handler.

Handles @mentions and processes transcript requests in Teams channels.
This handler is registered with the Bot Framework CloudAdapter in bot/api/main.py
and receives messages routed from Azure Bot Service.

Commands:
    help      - show command list
    process   - fetch latest meeting transcript via Graph API (IT-blocked, stub)
    paste     - prompt user to paste a transcript in next message
    status    - show current session state
    cancel    - abort current session
    (any long message after 'paste' is treated as the transcript)

Activation requires IT to provision:
  - Azure Bot Service resource (BOT_APP_ID + BOT_APP_PASSWORD in .env)
  - Teams app approval via Teams Admin Center
  - Graph permissions for transcript fetching (Phase 2.x)
"""

import os
import re
import time
import asyncio
from typing import Optional

from botbuilder.core import ActivityHandler, TurnContext, MessageFactory
from botbuilder.schema import ChannelAccount, Activity

from bot.session_store import session_store, SessionState, DraftTicket
from bot.teams.card_builder import build_review_card, build_processing_card
from bot.adapters.transcript_adapter import transcript_to_pipeline_input


# ---------------------------------------------------------------------------
# Help text
# ---------------------------------------------------------------------------
_HELP_TEXT = """\
**SageJiraBot Commands:**

- `process` — Fetch your most recent meeting transcript and create Jira tickets
- `paste` — Paste a transcript for processing (supports Teams VTT, Webex TXT, plain text)
- `status` — Show the current session state (if one is running)
- `cancel` — Cancel the current session and discard drafts
- `help` — Show this message

**Project override:**
Add `project:KEY` to any command to route tickets to a different Jira project.
Example: `paste project:OPS`

**Data governance:**
All LLM calls go to Azure AI Foundry (ai-foundry-dcri-sage).
No data leaves the Duke Health tenant.
"""


class SageJiraBotHandler(ActivityHandler):
    """
    Main Teams activity handler for SageJiraBot.
    Registered with Bot Framework CloudAdapter.
    """

    # ------------------------------------------------------------------
    # on_message_activity — entry point for all user messages
    # ------------------------------------------------------------------

    async def on_message_activity(self, turn_context: TurnContext) -> None:
        text = (turn_context.activity.text or "").strip()

        # Remove the bot @mention tag Teams includes: <at>BotName</at>
        text = re.sub(r'<at>[^<]+</at>', '', text).strip()

        # Parse optional project key override: "project:ST" or "project: OPS"
        project_match = re.search(r'project:\s*([A-Z]{1,10})', text, re.IGNORECASE)
        project_key = (
            project_match.group(1).upper()
            if project_match
            else os.environ.get('BOT_DEFAULT_PROJECT', 'ST')
        )

        if re.search(r'\bhelp\b', text, re.IGNORECASE):
            await self._send_help(turn_context)

        elif re.search(r'\bprocess\b', text, re.IGNORECASE):
            await self._handle_process(turn_context, text, project_key)

        elif re.search(r'\bpaste\b', text, re.IGNORECASE):
            await self._handle_paste_prompt(turn_context, project_key)

        elif re.search(r'\bstatus\b', text, re.IGNORECASE):
            await self._handle_status(turn_context)

        elif re.search(r'\bcancel\b', text, re.IGNORECASE):
            await self._handle_cancel(turn_context)

        else:
            # Could be a transcript paste (user sent a long message after 'paste' prompt)
            user_id = turn_context.activity.from_property.id if turn_context.activity.from_property else "unknown"
            active_session = session_store.get_active_for_user(user_id)

            if active_session and active_session.state == SessionState.PROCESSING:
                # User may have pasted a transcript
                if len(text) > 100:
                    # Looks like a transcript — process it
                    await self._run_pipeline_on_text(turn_context, text, project_key)
                else:
                    await turn_context.send_activity(
                        MessageFactory.text(
                            "I received a short message. "
                            "If you're pasting a transcript, please paste the full text. "
                            "Type `help` for all commands."
                        )
                    )
            else:
                await turn_context.send_activity(
                    MessageFactory.text(
                        "Hi! I'm SageJiraBot. "
                        "Try `process` to analyze your latest meeting transcript, "
                        "`paste` to paste one, or `help` for all commands."
                    )
                )

    # ------------------------------------------------------------------
    # on_members_added_activity — greet new users
    # ------------------------------------------------------------------

    async def on_members_added_activity(
        self, members_added: list[ChannelAccount], turn_context: TurnContext
    ) -> None:
        for member in members_added:
            if member.id != turn_context.activity.recipient.id:
                await turn_context.send_activity(
                    MessageFactory.text(
                        "SageJiraBot is ready. "
                        "@mention me with `process` after a meeting to turn your transcript "
                        "into Jira tickets. Type `help` to see all commands."
                    )
                )

    # ------------------------------------------------------------------
    # Private command handlers
    # ------------------------------------------------------------------

    async def _send_help(self, turn_context: TurnContext) -> None:
        """Send the help message."""
        await turn_context.send_activity(MessageFactory.text(_HELP_TEXT))

    async def _handle_process(
        self,
        turn_context: TurnContext,
        text: str,
        project_key: str,
    ) -> None:
        """
        Handle the 'process' command.

        In Phase 2, this fetches the latest meeting transcript from Graph API.
        Currently a stub — Graph permissions are IT-blocked.
        Informs the user to use `paste` instead.
        """
        await turn_context.send_activity(
            MessageFactory.text(
                "Automatic transcript fetching via Graph API is not yet available — "
                "it requires IT to provision Graph permissions "
                "(OnlineMeetings.Read.All, OnlineMeetingTranscript.Read.All).\n\n"
                "In the meantime, use `paste` to paste your transcript text directly. "
                "You can export a transcript from Teams by going to "
                "**Meeting chat > More options > Download transcript**."
            )
        )

    async def _handle_paste_prompt(
        self,
        turn_context: TurnContext,
        project_key: str,
    ) -> None:
        """
        Prompt the user to paste a transcript in their next message.
        Creates a PROCESSING session to track the awaiting state.
        """
        user_id = (
            turn_context.activity.from_property.id
            if turn_context.activity.from_property else "unknown"
        )
        channel_id = turn_context.activity.channel_id or "unknown"

        # Create a session to track that we're awaiting a paste
        session = session_store.create(
            user_id=user_id,
            channel_id=channel_id,
            project_key=project_key,
            meeting_title="Pasted Transcript",
        )

        await turn_context.send_activity(
            MessageFactory.text(
                f"Paste your transcript in the next message. "
                f"Supported formats: Teams VTT, Webex TXT, plain text.\n"
                f"Tickets will be created in project **{project_key}**.\n\n"
                f"Include the meeting title on the first line for better ticket context, e.g.:\n"
                f"```\nSprint Planning 2026-03-20\nAlice: We need to fix...\n```"
            )
        )

    async def _handle_status(self, turn_context: TurnContext) -> None:
        """Show the current session status for this user."""
        user_id = (
            turn_context.activity.from_property.id
            if turn_context.activity.from_property else "unknown"
        )
        session = session_store.get_active_for_user(user_id)
        if session is None:
            await turn_context.send_activity(
                MessageFactory.text("No active session. Use `process` or `paste` to start one.")
            )
        else:
            await turn_context.send_activity(
                MessageFactory.text(
                    f"Session ID: `{session.session_id}`\n"
                    f"Meeting: {session.meeting_title}\n"
                    f"Project: {session.project_key}\n"
                    f"State: **{session.state.value}**\n"
                    f"Drafts: {len(session.draft_tickets)} ticket(s)"
                )
            )

    async def _handle_cancel(self, turn_context: TurnContext) -> None:
        """Cancel the current active session."""
        user_id = (
            turn_context.activity.from_property.id
            if turn_context.activity.from_property else "unknown"
        )
        session = session_store.get_active_for_user(user_id)
        if session is None:
            await turn_context.send_activity(
                MessageFactory.text("No active session to cancel.")
            )
            return

        session.state = SessionState.CANCELLED
        session_store.update(session)
        await turn_context.send_activity(
            MessageFactory.text(
                f"Session cancelled. No tickets were created. "
                f"Use `process` or `paste` to start a new session."
            )
        )

    async def _run_pipeline_on_text(
        self,
        turn_context: TurnContext,
        transcript_text: str,
        project_key: str,
        meeting_title: str = "Meeting",
    ) -> None:
        """
        Process a pasted transcript through the agent pipeline.

        1. Send a processing card immediately (visual feedback)
        2. Run the pipeline (slow — ~20-25 seconds)
        3. Send a review card with draft tickets

        Args:
            turn_context:    Bot Framework turn context
            transcript_text: Raw transcript text (any supported format)
            project_key:     Jira project key
            meeting_title:   Human-readable meeting name
        """
        user_id = (
            turn_context.activity.from_property.id
            if turn_context.activity.from_property else "unknown"
        )
        channel_id = turn_context.activity.channel_id or "unknown"

        # Try to extract meeting title from the first line if it looks like a title
        lines = transcript_text.strip().splitlines()
        if lines:
            first_line = lines[0].strip()
            # If first line doesn't look like a speaker turn (no ":" separator mid-way through)
            # and is short enough to be a title, use it
            if len(first_line) < 100 and ':' not in first_line:
                meeting_title = first_line
                transcript_text = "\n".join(lines[1:]).strip() or transcript_text

        # Get or create session
        session = session_store.get_active_for_user(user_id)
        if session is None:
            session = session_store.create(
                user_id=user_id,
                channel_id=channel_id,
                project_key=project_key,
                meeting_title=meeting_title,
                transcript=transcript_text,
            )
        else:
            session.meeting_title = meeting_title
            session.transcript = transcript_text
            session.project_key = project_key
            session_store.update(session)

        # Send processing card immediately
        processing_card = build_processing_card(meeting_title)
        card_attachment = {
            "contentType": "application/vnd.microsoft.card.adaptive",
            "content": processing_card,
        }
        processing_reply = Activity(type="message", attachments=[card_attachment])
        await turn_context.send_activity(processing_reply)

        # Run pipeline (synchronous but slow — wraps in run_in_executor for async)
        try:
            loop = asyncio.get_event_loop()
            draft_tickets = await loop.run_in_executor(
                None,
                lambda: self._run_pipeline_sync(transcript_text, meeting_title, project_key),
            )
        except Exception as exc:
            session.state = SessionState.CANCELLED
            session_store.update(session)
            await turn_context.send_activity(
                MessageFactory.text(f"Pipeline failed: {type(exc).__name__}: {exc}")
            )
            return

        if not draft_tickets:
            session.state = SessionState.COMPLETED
            session_store.update(session)
            await turn_context.send_activity(
                MessageFactory.text(
                    f"No actionable items found in the transcript for '{meeting_title}'. "
                    "No tickets were created."
                )
            )
            return

        # Populate session with draft tickets
        session.draft_tickets = [
            DraftTicket(
                draft_id=f"d{i}",
                summary=t.get("summary", ""),
                description=t.get("description", ""),
                priority=t.get("priority", "Medium"),
            )
            for i, t in enumerate(draft_tickets)
        ]
        session.state = SessionState.REVIEW_PENDING
        session_store.update(session)

        # Send review card
        review_card = build_review_card(session)
        card_attachment = {
            "contentType": "application/vnd.microsoft.card.adaptive",
            "content": review_card,
        }
        review_reply = Activity(type="message", attachments=[card_attachment])
        await turn_context.send_activity(review_reply)

    def _run_pipeline_sync(
        self,
        transcript_text: str,
        meeting_title: str,
        project_key: str,
    ) -> list[dict]:
        """
        Synchronous pipeline execution called from run_in_executor.
        Returns list of draft ticket dicts.
        """
        import os as _os
        from agents import agent1_email, agent2_router, agent3_jira
        from bot.adapters.transcript_adapter import transcript_to_pipeline_input

        original_key = _os.environ.get("JIRA_PROJECT_KEY", "ST")
        if project_key != original_key:
            _os.environ["JIRA_PROJECT_KEY"] = project_key

        try:
            items = transcript_to_pipeline_input(transcript_text, meeting_title)
            if not items:
                return []
            extracts = agent1_email.run_on_items(items)
            approved = agent2_router.run(extracts)
            drafts = agent3_jira.run(approved, dry_run=True)
            return drafts
        finally:
            if project_key != original_key:
                _os.environ["JIRA_PROJECT_KEY"] = original_key
