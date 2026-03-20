"""
SageJiraBot Teams activity handler.

Handles @mentions and processes transcript requests in Teams channels.
This handler is registered with the Bot Framework CloudAdapter in bot/api/main.py
and receives messages routed from Azure Bot Service.

Commands:
    use jira <url>              - set Jira instance (required before other commands)
    use jira <url> project <KEY> - set Jira instance and project in one step
    process                     - fetch latest meeting transcript via Graph API (IT-blocked, stub)
    paste                       - prompt user to paste a transcript in next message
    status                      - show current session state
    done                        - end meeting; post summary card of pending drafts
    cancel                      - abort current session and discard drafts
    forget series               - clear saved Jira defaults for this recurring series
    show series                 - display saved Jira settings for this recurring series
    help                        - show command list

State machine (PRP §4.5):
    IDLE -> AWAITING_PROJECT -> LIVE_MEETING -> MEETING_ENDED -> CREATING -> COMPLETE

Activation requires IT to provision:
  - Azure Bot Service resource (BOT_APP_ID + BOT_APP_PASSWORD in .env)
  - Teams app approval via Teams Admin Center
  - Graph permissions for transcript fetching (Phase 2.x)
"""

import os
import re
import asyncio
import logging
from typing import Optional

from botbuilder.core import ActivityHandler, TurnContext, MessageFactory
from botbuilder.schema import ChannelAccount, Activity

from bot.session_store import session_store, SessionState, DraftTicket, BotSession
from bot.teams.card_builder import build_review_card, build_processing_card
from bot.adapters.transcript_adapter import transcript_to_pipeline_input
from bot.data.series_defaults_store import series_defaults_store

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Help text
# ---------------------------------------------------------------------------

_HELP_TEXT = """\
**SageJiraBot Commands:**

**Session setup (required first):**
- `use jira <url>` — set the Jira instance for this session
- `use jira <url> project <KEY>` — set Jira instance and project in one step

**Transcript processing:**
- `process` — fetch your most recent meeting transcript (requires IT Graph permissions)
- `paste` — paste a transcript for processing (Teams VTT, Webex TXT, plain text)

**Session management:**
- `status` — show current session state
- `done` — signal end of meeting; post summary card of pending draft tickets
- `cancel` — cancel the current session and discard drafts

**Recurring meeting settings:**
- `show series` — display saved Jira settings for this recurring series
- `forget series` — clear saved Jira defaults for this recurring series

**Data governance:**
All LLM calls go to Azure AI Foundry (ai-foundry-dcri-sage).
No data leaves the Duke Health tenant.
"""

# Reply sent when Jira is not yet configured
_NO_JIRA_MSG = (
    "Please specify a Jira instance first: "
    "`@sageJiraBot use jira <url>`\n\n"
    "Example: `use jira https://your-org.atlassian.net project ST`"
)


# ---------------------------------------------------------------------------
# Helper: extract user/channel identity from turn_context
# ---------------------------------------------------------------------------

def _user_id(turn_context: TurnContext) -> str:
    prop = turn_context.activity.from_property
    return prop.id if prop else "unknown"


def _channel_id(turn_context: TurnContext) -> str:
    return turn_context.activity.channel_id or "unknown"


def _meeting_title(turn_context: TurnContext) -> str:
    """
    Best-effort meeting title from Teams activity channel_data.
    Falls back to 'Meeting' if not available.
    """
    channel_data = getattr(turn_context.activity, "channel_data", None) or {}
    meeting = channel_data.get("meeting") or {}
    return meeting.get("title") or "Meeting"


def _series_master_id(turn_context: TurnContext) -> Optional[str]:
    """
    Return the seriesMasterId from Teams activity channel_data if this is
    a recurring meeting, or None otherwise.

    The field appears in channel_data.meeting.seriesMasterId when the bot
    is invoked inside a recurring meeting via Graph API scheduling.
    """
    channel_data = getattr(turn_context.activity, "channel_data", None) or {}
    meeting = channel_data.get("meeting") or {}
    series_id = meeting.get("seriesMasterId")
    return series_id if series_id else None


# ---------------------------------------------------------------------------
# SageJiraBotHandler
# ---------------------------------------------------------------------------

class SageJiraBotHandler(ActivityHandler):
    """
    Main Teams activity handler for SageJiraBot.
    Registered with Bot Framework CloudAdapter.

    Command dispatch is handled in on_message_activity.  Each command has
    a dedicated private method prefixed with _handle_.

    State machine (PRP §4.5):
        [IDLE]
            user provides "use jira <url>" command
        [AWAITING_PROJECT]
            bot verifies Jira access; prompts for project key if not given
            (or AWAITING_SERIES_CONFIRM if recurring meeting with no saved defaults)
        [LIVE_MEETING]
            Jira URL + project key are confirmed; bot accepts process/paste commands
        [MEETING_ENDED]
            user runs "done"; bot posts summary card of pending drafts
        [CREATING]
            jira_tool.create_ticket() called for each approved ticket
        [COMPLETE]
            confirmation card sent; session winds down
    """

    # ------------------------------------------------------------------
    # on_message_activity — entry point for all user messages
    # ------------------------------------------------------------------

    async def on_message_activity(self, turn_context: TurnContext) -> None:
        raw_text = (turn_context.activity.text or "").strip()

        # Remove the bot @mention tag Teams includes: <at>BotName</at>
        text = re.sub(r'<at>[^<]+</at>', '', raw_text).strip()

        user_id = _user_id(turn_context)

        # ------------------------------------------------------------------
        # Priority 1: "use jira <url>" — session start command
        # Matched before everything else so it works from any state.
        # ------------------------------------------------------------------
        use_jira_match = re.search(
            r'\buse\s+jira\s+(https?://\S+)',
            text,
            re.IGNORECASE,
        )
        if use_jira_match:
            jira_url = use_jira_match.group(1).rstrip('/')
            proj_match = re.search(r'\bproject\s+([A-Z]{1,10})\b', text, re.IGNORECASE)
            project_key = proj_match.group(1).upper() if proj_match else None
            await self._handle_use_jira(turn_context, jira_url, project_key)
            return

        # ------------------------------------------------------------------
        # Priority 2: series confirmation responses ("yes" / "no")
        # Must be checked before generic command dispatch.
        # ------------------------------------------------------------------
        active_session = session_store.get_active_for_user(user_id)
        if (
            active_session
            and active_session.state == SessionState.AWAITING_SERIES_CONFIRM
        ):
            cleaned = text.strip().lower()
            if cleaned in ("yes", "y", "yeah", "yep", "save", "confirm"):
                await self._handle_series_confirm_yes(turn_context, active_session)
                return
            elif cleaned in ("no", "n", "nope", "skip", "cancel"):
                await self._handle_series_confirm_no(turn_context, active_session)
                return
            # If neither yes nor no, fall through to normal dispatch below.
            # This lets the user send commands while in AWAITING_SERIES_CONFIRM
            # without being stuck.

        # ------------------------------------------------------------------
        # Priority 3: paste path — user just sent the transcript text
        # ------------------------------------------------------------------
        if active_session and active_session.awaiting_paste:
            if len(text) > 100:
                # Looks like a real transcript — process it
                active_session.awaiting_paste = False
                session_store.update(active_session)
                await self._run_pipeline_on_text(
                    turn_context=turn_context,
                    transcript_text=text,
                    session=active_session,
                )
                return
            else:
                await turn_context.send_activity(
                    MessageFactory.text(
                        "That message looks too short to be a transcript. "
                        "Please paste the full transcript text (at least 100 characters). "
                        "Type `cancel` to abort."
                    )
                )
                return

        # ------------------------------------------------------------------
        # Priority 4: named commands
        # ------------------------------------------------------------------

        if re.search(r'\bhelp\b', text, re.IGNORECASE):
            await self._send_help(turn_context)

        elif re.search(r'\bforget\s+series\b', text, re.IGNORECASE):
            await self._handle_forget_series(turn_context)

        elif re.search(r'\bshow\s+series\b', text, re.IGNORECASE):
            await self._handle_show_series(turn_context)

        elif re.search(r'\bprocess\b', text, re.IGNORECASE):
            await self._handle_process(turn_context)

        elif re.search(r'\bpaste\b', text, re.IGNORECASE):
            await self._handle_paste_prompt(turn_context)

        elif re.search(r'\bstatus\b', text, re.IGNORECASE):
            await self._handle_status(turn_context)

        elif re.search(r'\bdone\b', text, re.IGNORECASE):
            await self._handle_done(turn_context)

        elif re.search(r'\bcancel\b', text, re.IGNORECASE):
            await self._handle_cancel(turn_context)

        else:
            # Unknown message — suggest help
            await turn_context.send_activity(
                MessageFactory.text(
                    "Hi! I'm SageJiraBot. "
                    "Start with `use jira <url>` to set your Jira instance, "
                    "then use `process` or `paste` to create tickets. "
                    "Type `help` for all commands."
                )
            )

    # ------------------------------------------------------------------
    # on_members_added_activity — greet new users
    # ------------------------------------------------------------------

    async def on_members_added_activity(
        self,
        members_added: list[ChannelAccount],
        turn_context: TurnContext,
    ) -> None:
        for member in members_added:
            if member.id != turn_context.activity.recipient.id:
                await turn_context.send_activity(
                    MessageFactory.text(
                        "SageJiraBot is ready. "
                        "Start with `use jira <url>` to set your Jira instance, "
                        "then `paste` to process a meeting transcript. "
                        "Type `help` to see all commands."
                    )
                )

    # ------------------------------------------------------------------
    # Private command handlers
    # ------------------------------------------------------------------

    async def _send_help(self, turn_context: TurnContext) -> None:
        """Send the help message."""
        await turn_context.send_activity(MessageFactory.text(_HELP_TEXT))

    # ------------------------------------------------------------------ use jira

    async def _handle_use_jira(
        self,
        turn_context: TurnContext,
        jira_url: str,
        project_key: Optional[str],
    ) -> None:
        """
        Handle the 'use jira <url> [project <KEY>]' command.

        Flow:
          1. Detect recurring series (channel_data.meeting.seriesMasterId).
          2. If series and saved defaults exist, auto-apply and skip to LIVE_MEETING.
          3. Check Jira reachability via check_project_permission (if project given)
             or a simple instance-level check.
          4. Create/update session with jira_base_url.
          5. If project key was not provided, enter AWAITING_PROJECT and prompt.
          6. If project key provided and verified, enter LIVE_MEETING.
          7. If new recurring series (no saved defaults), enter AWAITING_SERIES_CONFIRM.
        """
        user_id    = _user_id(turn_context)
        channel_id = _channel_id(turn_context)
        title      = _meeting_title(turn_context)
        series_id  = _series_master_id(turn_context)

        # Check for saved series defaults
        if series_id:
            saved = series_defaults_store.get(series_id)
            if saved:
                # Auto-apply saved settings
                eff_url = saved["jira_base_url"]
                eff_key = saved["project_key"]

                session = self._get_or_create_session(
                    user_id=user_id,
                    channel_id=channel_id,
                    title=title,
                    series_id=series_id,
                )
                session.jira_base_url    = eff_url
                session.project_key      = eff_key
                session.series_master_id = series_id
                session.state            = SessionState.LIVE_MEETING
                session_store.update(session)

                host = re.sub(r'^https?://', '', eff_url).rstrip('/')
                await turn_context.send_activity(
                    MessageFactory.text(
                        f"Using saved Jira settings for this series: "
                        f"**{host}** / **{eff_key}**. "
                        f"[Use `forget series` to clear, or `use jira <url>` to change.]"
                    )
                )
                return

        # No saved series defaults (or not a recurring series).
        # Verify the Jira instance / project access.
        verified_key: Optional[str] = None
        if project_key:
            ok, err_msg = await self._check_jira_access(jira_url, project_key)
            if not ok:
                await turn_context.send_activity(MessageFactory.text(err_msg))
                return
            verified_key = project_key
        else:
            # No project key yet — just check instance reachability.
            # check_project_permission needs a project key so we use a placeholder
            # that will deliberately return False on 404, but any non-network-error
            # response tells us the instance is reachable.
            reachable = await self._check_jira_reachable(jira_url)
            if not reachable:
                await turn_context.send_activity(
                    MessageFactory.text(
                        f"Cannot reach that Jira instance. "
                        f"Check the URL and try again: `{jira_url}`"
                    )
                )
                return

        # Create or update session
        session = self._get_or_create_session(
            user_id=user_id,
            channel_id=channel_id,
            title=title,
            series_id=series_id,
        )
        session.jira_base_url    = jira_url
        session.series_master_id = series_id

        if verified_key:
            session.project_key = verified_key

            if series_id:
                # Recurring series — ask to save settings
                session.state = SessionState.AWAITING_SERIES_CONFIRM
                session_store.update(session)
                await turn_context.send_activity(
                    MessageFactory.text(
                        f"Got it. Jira set to **{jira_url}**, project **{verified_key}**.\n\n"
                        f"This looks like a recurring meeting. "
                        f"Save these Jira settings for future sessions in this series? "
                        f"[**Yes** / **No**]"
                    )
                )
            else:
                session.state = SessionState.LIVE_MEETING
                session_store.update(session)
                await turn_context.send_activity(
                    MessageFactory.text(
                        f"Got it. Jira instance set to **{jira_url}**, "
                        f"project **{verified_key}**. "
                        f"You can now use `process` or `paste` to create tickets."
                    )
                )
        else:
            # No project key yet — ask for it
            session.state = SessionState.AWAITING_PROJECT
            session_store.update(session)
            await turn_context.send_activity(
                MessageFactory.text(
                    f"Got it. Jira instance set to **{jira_url}**.\n\n"
                    f"Which Jira project? (e.g. `ST`, `OPS`)"
                )
            )

    # ------------------------------------------------------------------ project key (AWAITING_PROJECT)

    async def _handle_project_key_response(
        self,
        turn_context: TurnContext,
        session: BotSession,
        project_key: str,
    ) -> None:
        """
        Handle the user's project key reply when session is in AWAITING_PROJECT.
        Validates project access, then transitions to LIVE_MEETING or AWAITING_SERIES_CONFIRM.
        """
        ok, err_msg = await self._check_jira_access(
            session.jira_base_url or "", project_key
        )
        if not ok:
            await turn_context.send_activity(MessageFactory.text(err_msg))
            return

        session.project_key = project_key

        if session.series_master_id:
            # Recurring series — ask to save
            session.state = SessionState.AWAITING_SERIES_CONFIRM
            session_store.update(session)
            await turn_context.send_activity(
                MessageFactory.text(
                    f"Project set to **{project_key}**.\n\n"
                    f"This looks like a recurring meeting. "
                    f"Save these Jira settings for future sessions in this series? "
                    f"[**Yes** / **No**]"
                )
            )
        else:
            session.state = SessionState.LIVE_MEETING
            session_store.update(session)
            await turn_context.send_activity(
                MessageFactory.text(
                    f"Project set to **{project_key}**. "
                    f"You can now use `process` or `paste` to create tickets."
                )
            )

    # ------------------------------------------------------------------ series confirm yes/no

    async def _handle_series_confirm_yes(
        self,
        turn_context: TurnContext,
        session: BotSession,
    ) -> None:
        """User confirmed saving series defaults."""
        series_id = session.series_master_id
        if series_id and session.jira_base_url and session.project_key:
            series_defaults_store.set(series_id, {
                "jira_base_url": session.jira_base_url,
                "project_key":   session.project_key,
            })

        session.state = SessionState.LIVE_MEETING
        session_store.update(session)

        await turn_context.send_activity(
            MessageFactory.text(
                f"Saved. Future sessions in this series will auto-apply: "
                f"**{session.jira_base_url}** / **{session.project_key}**.\n\n"
                f"You can now use `process` or `paste` to create tickets."
            )
        )

    async def _handle_series_confirm_no(
        self,
        turn_context: TurnContext,
        session: BotSession,
    ) -> None:
        """User declined saving series defaults."""
        session.state = SessionState.LIVE_MEETING
        session_store.update(session)

        await turn_context.send_activity(
            MessageFactory.text(
                f"Understood. Settings not saved. "
                f"You can now use `process` or `paste` to create tickets."
            )
        )

    # ------------------------------------------------------------------ process

    async def _handle_process(self, turn_context: TurnContext) -> None:
        """
        Handle the 'process' command.

        Requires jira_base_url to be set first.
        In Phase 2, fetches the latest meeting transcript from Graph API.
        Currently a stub — Graph permissions are IT-blocked.
        """
        user_id = _user_id(turn_context)
        session = session_store.get_active_for_user(user_id)

        # State gate: jira_base_url must be set
        if not session or not session.jira_base_url:
            await turn_context.send_activity(MessageFactory.text(_NO_JIRA_MSG))
            return

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

    # ------------------------------------------------------------------ paste

    async def _handle_paste_prompt(self, turn_context: TurnContext) -> None:
        """
        Prompt the user to paste a transcript in their next message.

        Requires jira_base_url to be set first (state gate).
        Sets session.awaiting_paste = True so the next message is treated as a transcript.
        """
        user_id    = _user_id(turn_context)
        channel_id = _channel_id(turn_context)
        title      = _meeting_title(turn_context)
        series_id  = _series_master_id(turn_context)

        session = session_store.get_active_for_user(user_id)

        # State gate: jira_base_url must be set
        if not session or not session.jira_base_url:
            await turn_context.send_activity(MessageFactory.text(_NO_JIRA_MSG))
            return

        # If still awaiting project key, re-prompt instead
        if session.state == SessionState.AWAITING_PROJECT:
            await turn_context.send_activity(
                MessageFactory.text(
                    "Please provide the Jira project key first. "
                    "Example: `ST` or `OPS`"
                )
            )
            return

        project_key = session.project_key or os.environ.get("BOT_DEFAULT_PROJECT", "ST")
        session.awaiting_paste = True
        session_store.update(session)

        await turn_context.send_activity(
            MessageFactory.text(
                f"Paste your transcript in the next message. "
                f"Supported formats: Teams VTT, Webex TXT, plain text.\n"
                f"Tickets will be created in project **{project_key}** "
                f"on **{session.jira_base_url}**.\n\n"
                f"Include the meeting title on the first line for better context, e.g.:\n"
                f"```\nSprint Planning 2026-03-20\nAlice: We need to fix...\n```"
            )
        )

    # ------------------------------------------------------------------ status

    async def _handle_status(self, turn_context: TurnContext) -> None:
        """Show the current session status for this user."""
        user_id = _user_id(turn_context)
        session = session_store.get_active_for_user(user_id)

        if session is None:
            await turn_context.send_activity(
                MessageFactory.text(
                    "No active session. "
                    "Start with `use jira <url>` to set your Jira instance."
                )
            )
            return

        jira_info = session.jira_base_url or "(not set)"
        proj_info = session.project_key   or "(not set)"

        await turn_context.send_activity(
            MessageFactory.text(
                f"**Session:** `{session.session_id}`\n"
                f"**Meeting:** {session.meeting_title}\n"
                f"**Jira:** {jira_info}\n"
                f"**Project:** {proj_info}\n"
                f"**State:** {session.state.value}\n"
                f"**Drafts:** {len(session.draft_tickets)} ticket(s)\n"
                f"**Awaiting paste:** {session.awaiting_paste}"
            )
        )

    # ------------------------------------------------------------------ done

    async def _handle_done(self, turn_context: TurnContext) -> None:
        """
        Handle the 'done' command.

        Requires jira_base_url to be set (state gate).
        Transitions to MEETING_ENDED and posts a summary card (or stub text).
        """
        user_id = _user_id(turn_context)
        session = session_store.get_active_for_user(user_id)

        # State gate: jira_base_url must be set
        if not session or not session.jira_base_url:
            await turn_context.send_activity(MessageFactory.text(_NO_JIRA_MSG))
            return

        session.state = SessionState.MEETING_ENDED
        session_store.update(session)

        n = len(session.draft_tickets)
        if n == 0:
            await turn_context.send_activity(
                MessageFactory.text(
                    "Meeting ended. No draft tickets pending review.\n"
                    "Use `paste` or `process` to analyze a transcript first."
                )
            )
            return

        # Try to send an Adaptive Card summary; fall back to plain text
        try:
            review_card = build_review_card(session)
            card_attachment = {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": review_card,
            }
            summary_reply = Activity(type="message", attachments=[card_attachment])
            await turn_context.send_activity(summary_reply)
        except Exception as exc:
            logger.warning("card_builder failed in _handle_done: %s", exc)
            await turn_context.send_activity(
                MessageFactory.text(
                    f"Meeting ended. **{n}** draft ticket{'s' if n != 1 else ''} pending review.\n"
                    f"(Card display unavailable — use the web UI to review tickets.)"
                )
            )

    # ------------------------------------------------------------------ cancel

    async def _handle_cancel(self, turn_context: TurnContext) -> None:
        """
        Cancel the current active session.
        Clears session state; requires jira_base_url to be set (state gate).
        """
        user_id = _user_id(turn_context)
        session = session_store.get_active_for_user(user_id)

        if not session or not session.jira_base_url:
            await turn_context.send_activity(MessageFactory.text(_NO_JIRA_MSG))
            return

        session.state         = SessionState.CANCELLED
        session.awaiting_paste = False
        session.draft_tickets  = []
        session_store.update(session)

        await turn_context.send_activity(
            MessageFactory.text(
                "Session cancelled. No tickets were created. "
                "Use `use jira <url>` to start a new session."
            )
        )

    # ------------------------------------------------------------------ forget series

    async def _handle_forget_series(self, turn_context: TurnContext) -> None:
        """
        Handle 'forget series' command.
        Clears saved Jira defaults for the current recurring meeting series.
        """
        series_id = _series_master_id(turn_context)
        if not series_id:
            await turn_context.send_activity(
                MessageFactory.text(
                    "This command is only available in a recurring meeting. "
                    "No series ID detected in this channel."
                )
            )
            return

        deleted = series_defaults_store.delete(series_id)
        if deleted:
            await turn_context.send_activity(
                MessageFactory.text(
                    "Saved Jira settings for this series have been cleared. "
                    "The next session will ask for Jira details again."
                )
            )
        else:
            await turn_context.send_activity(
                MessageFactory.text(
                    "No saved settings found for this series."
                )
            )

    # ------------------------------------------------------------------ show series

    async def _handle_show_series(self, turn_context: TurnContext) -> None:
        """
        Handle 'show series' command.
        Displays saved Jira settings for the current recurring meeting series.
        """
        series_id = _series_master_id(turn_context)
        if not series_id:
            await turn_context.send_activity(
                MessageFactory.text(
                    "This command is only available in a recurring meeting. "
                    "No series ID detected in this channel."
                )
            )
            return

        defaults = series_defaults_store.get(series_id)
        if defaults:
            host = re.sub(r'^https?://', '', defaults["jira_base_url"]).rstrip('/')
            await turn_context.send_activity(
                MessageFactory.text(
                    f"**Saved settings for this series:**\n"
                    f"- Jira: `{defaults['jira_base_url']}`\n"
                    f"- Project: `{defaults['project_key']}`\n\n"
                    f"Use `forget series` to clear these."
                )
            )
        else:
            await turn_context.send_activity(
                MessageFactory.text("No saved settings for this series.")
            )

    # ------------------------------------------------------------------
    # Pipeline execution helpers
    # ------------------------------------------------------------------

    async def _run_pipeline_on_text(
        self,
        turn_context: TurnContext,
        transcript_text: str,
        session: BotSession,
    ) -> None:
        """
        Process a pasted transcript through the agent pipeline.

        1. Extract meeting title from the first line if applicable.
        2. Send a processing card immediately for visual feedback.
        3. Run the pipeline asynchronously (~20-25 seconds).
        4. Send a review card with draft tickets (transitions to MEETING_ENDED).

        Args:
            turn_context:    Bot Framework turn context
            transcript_text: Raw transcript text (any supported format)
            session:         Active BotSession (jira_base_url must be set)
        """
        project_key  = session.project_key or os.environ.get("BOT_DEFAULT_PROJECT", "ST")
        meeting_title = session.meeting_title

        # Try to extract meeting title from the first line
        lines = transcript_text.strip().splitlines()
        if lines:
            first_line = lines[0].strip()
            if len(first_line) < 100 and ':' not in first_line:
                meeting_title = first_line
                transcript_text = "\n".join(lines[1:]).strip() or transcript_text

        # Update session with transcript text and resolved title
        session.meeting_title = meeting_title
        session.transcript    = transcript_text
        session_store.update(session)

        # Send processing card immediately
        processing_card = build_processing_card(meeting_title)
        card_attachment = {
            "contentType": "application/vnd.microsoft.card.adaptive",
            "content": processing_card,
        }
        processing_reply = Activity(type="message", attachments=[card_attachment])
        await turn_context.send_activity(processing_reply)

        # Run pipeline (synchronous but slow — wrap in run_in_executor)
        try:
            loop = asyncio.get_event_loop()
            draft_tickets = await loop.run_in_executor(
                None,
                lambda: self._run_pipeline_sync(
                    transcript_text=transcript_text,
                    meeting_title=meeting_title,
                    project_key=project_key,
                ),
            )
        except Exception as exc:
            logger.exception("Pipeline failed for session %s", session.session_id)
            session.state = SessionState.CANCELLED
            session_store.update(session)
            await turn_context.send_activity(
                MessageFactory.text(
                    f"Pipeline failed: `{type(exc).__name__}: {exc}`\n\n"
                    f"Use `cancel` to reset and try again."
                )
            )
            return

        if not draft_tickets:
            session.state = SessionState.MEETING_ENDED
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
        session.state = SessionState.MEETING_ENDED
        session_store.update(session)

        # Send review card
        try:
            review_card = build_review_card(session)
            card_attachment = {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": review_card,
            }
            review_reply = Activity(type="message", attachments=[card_attachment])
            await turn_context.send_activity(review_reply)
        except Exception as exc:
            logger.warning("build_review_card failed: %s", exc)
            n = len(session.draft_tickets)
            await turn_context.send_activity(
                MessageFactory.text(
                    f"Meeting ended. **{n}** draft ticket{'s' if n != 1 else ''} pending review.\n"
                    f"(Card display unavailable — use the web UI to review tickets.)"
                )
            )

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
            drafts   = agent3_jira.run(approved, dry_run=True)
            return drafts
        finally:
            if project_key != original_key:
                _os.environ["JIRA_PROJECT_KEY"] = original_key

    # ------------------------------------------------------------------
    # Jira access checks (async wrappers around blocking requests calls)
    # ------------------------------------------------------------------

    async def _check_jira_access(
        self,
        jira_url: str,
        project_key: str,
    ) -> tuple[bool, str]:
        """
        Non-blocking wrapper around jira_tool.check_project_permission().

        Returns (True, "") on success, or (False, error_message) on failure.
        """
        loop = asyncio.get_event_loop()
        try:
            ok = await loop.run_in_executor(
                None,
                lambda: self._check_permission_sync(jira_url, project_key),
            )
            if ok:
                return True, ""
            else:
                return False, (
                    f"You don't have Jira CREATE_ISSUES permission for project "
                    f"`{project_key}` on `{jira_url}`. "
                    f"Contact your Jira admin."
                )
        except RuntimeError as exc:
            logger.warning("check_project_permission error: %s", exc)
            return False, (
                f"Cannot reach that Jira instance. "
                f"Check the URL and try again: `{jira_url}`"
            )
        except Exception as exc:
            logger.exception("Unexpected error in _check_jira_access")
            return False, (
                f"Jira check failed: `{type(exc).__name__}: {exc}`"
            )

    async def _check_jira_reachable(self, jira_url: str) -> bool:
        """
        Lightweight reachability check: does the Jira instance respond at all?

        Uses check_project_permission with a dummy key; any HTTP response
        (including 404) means the server is reachable.  Only network errors
        and RuntimeError from the tool indicate unreachability.
        """
        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(
                None,
                lambda: self._check_permission_sync(jira_url, "__REACHABILITY_CHECK__"),
            )
            return True
        except RuntimeError:
            # RuntimeError from check_project_permission means unexpected HTTP
            # status — instance is reachable but misbehaving; treat as reachable.
            return True
        except Exception:
            return False

    @staticmethod
    def _check_permission_sync(jira_url: str, project_key: str) -> bool:
        """
        Synchronous call to jira_tool.check_project_permission.
        Called from run_in_executor so it does not block the event loop.
        """
        from tools.jira_tool import check_project_permission
        return check_project_permission(
            project_key=project_key,
            base_url=jira_url,
        )

    # ------------------------------------------------------------------
    # Session helpers
    # ------------------------------------------------------------------

    def _get_or_create_session(
        self,
        user_id:    str,
        channel_id: str,
        title:      str,
        series_id:  Optional[str],
    ) -> BotSession:
        """
        Return the user's active session, or create a new IDLE one.

        If an existing session is in a terminal state (CANCELLED, EXPIRED,
        COMPLETED, COMPLETE) it is treated as absent so a fresh session starts.
        """
        terminal = {
            SessionState.CANCELLED,
            SessionState.EXPIRED,
            SessionState.COMPLETED,
            SessionState.COMPLETE,
        }
        session = session_store.get_active_for_user(user_id)
        if session is not None and session.state in terminal:
            session = None

        if session is None:
            session = session_store.create(
                user_id=user_id,
                channel_id=channel_id,
                meeting_title=title,
                series_master_id=series_id,
                state=SessionState.IDLE,
            )
        return session
