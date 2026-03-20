"""
Handles the submit action from the Adaptive Card review interface.
Called when user clicks 'Approve Selected' or 'Cancel'.

The card_data payload from Teams looks like:
    {
        "action": "approve",
        "session_id": "<uuid>",
        "include_<draft_id>": "true" | "false",
        "summary_<draft_id>": "updated summary text",
        ...
    }
"""

from botbuilder.core import TurnContext, MessageFactory
from botbuilder.schema import Activity

from bot.session_store import session_store, SessionState, DraftTicket
from bot.teams.card_builder import build_confirmation_card
from tools.jira_tool import create_ticket


async def handle_card_submit(
    session_id: str,
    card_data: dict,
    turn_context: TurnContext,
) -> None:
    """
    Process the submitted card data.

    Args:
        session_id:   The bot session ID (from card data.session_id)
        card_data:    The full card submit payload dict from Teams
        turn_context: Bot Framework turn context for sending replies

    card_data keys:
        action:              "approve" | "cancel"
        session_id:          matches the session_id arg
        include_<draft_id>:  "true" | "false" per ticket toggle
        summary_<draft_id>:  updated summary text per ticket (if user edited)
    """
    session = session_store.get(session_id)
    if session is None:
        await turn_context.send_activity(
            MessageFactory.text(
                "Session not found or expired. "
                "Please run `process` again to start a new session."
            )
        )
        return

    action = card_data.get("action", "")

    # -----------------------------------------------------------------
    # Cancel
    # -----------------------------------------------------------------
    if action == "cancel":
        session.state = SessionState.CANCELLED
        session_store.update(session)
        await turn_context.send_activity(
            MessageFactory.text(
                f"Cancelled. No tickets were created for '{session.meeting_title}'. "
                "Run `process` to start over."
            )
        )
        return

    # -----------------------------------------------------------------
    # Approve
    # -----------------------------------------------------------------
    if action == "approve":
        # Collect which tickets the user included and their (possibly edited) summaries
        tickets_to_create: list[DraftTicket] = []
        for ticket in session.draft_tickets:
            include_key = f"include_{ticket.draft_id}"
            summary_key = f"summary_{ticket.draft_id}"

            # Default to included (true) if the toggle key is missing
            include_val = card_data.get(include_key, "true")
            is_included = str(include_val).lower() in ("true", "1", "yes")

            if is_included:
                # Apply any summary edits from the card
                updated_summary = card_data.get(summary_key, ticket.summary)
                ticket.summary = updated_summary.strip() if updated_summary else ticket.summary
                tickets_to_create.append(ticket)

        if not tickets_to_create:
            session.state = SessionState.CANCELLED
            session_store.update(session)
            await turn_context.send_activity(
                MessageFactory.text(
                    "No tickets selected. Nothing was created. "
                    "Run `process` again to start over."
                )
            )
            return

        # Send an interim message so the user knows we're working
        await turn_context.send_activity(
            MessageFactory.text(
                f"Creating {len(tickets_to_create)} ticket"
                f"{'s' if len(tickets_to_create) != 1 else ''} in Jira "
                f"project {session.project_key}..."
            )
        )

        # Create tickets in Jira
        created: list[dict] = []
        errors: list[str] = []
        for ticket in tickets_to_create:
            try:
                result = create_ticket(
                    summary=ticket.summary,
                    description=ticket.description,
                    priority=ticket.priority,
                )
                created.append(result)
            except RuntimeError as exc:
                errors.append(f"{ticket.summary}: {exc}")

        # Update session state
        if errors and not created:
            session.state = SessionState.CANCELLED
            session_store.update(session)
            error_text = "\n".join(errors)
            await turn_context.send_activity(
                MessageFactory.text(
                    f"Failed to create tickets:\n{error_text}"
                )
            )
            return

        session.state = SessionState.COMPLETED
        session_store.update(session)

        # Build and send confirmation card
        confirm_card = build_confirmation_card(created)
        card_attachment = {
            "contentType": "application/vnd.microsoft.card.adaptive",
            "content": confirm_card,
        }
        reply = Activity(
            type="message",
            attachments=[card_attachment],
        )
        await turn_context.send_activity(reply)

        # If there were partial errors, note them
        if errors:
            error_text = "\n".join(errors)
            await turn_context.send_activity(
                MessageFactory.text(
                    f"Note: {len(errors)} ticket(s) failed to create:\n{error_text}"
                )
            )
        return

    # Unknown action
    await turn_context.send_activity(
        MessageFactory.text(
            f"Unknown action '{action}'. Please use the card buttons."
        )
    )
