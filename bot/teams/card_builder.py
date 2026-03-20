"""
Adaptive Card JSON builder for the SageJiraBot ticket review UI.

Builds the Adaptive Card v1.5 JSON that is sent to Teams when a transcript
has been processed and the user needs to review/approve draft tickets.

Reference: https://adaptivecards.io/designer/
Schema: https://adaptivecards.io/schemas/adaptive-card.json

To preview a card visually:
    python -c "
    from bot.teams.card_builder import build_review_card
    from bot.session_store import BotSession, DraftTicket, SessionState
    from datetime import datetime
    import json
    s = BotSession(session_id='test', user_id='u1', channel_id='c1',
                   project_key='ST', meeting_title='Test', state=SessionState.REVIEW_PENDING)
    s.draft_tickets = [DraftTicket(draft_id='d0', summary='Fix login', description='...', priority='High')]
    print(json.dumps(build_review_card(s), indent=2))
    " | pbcopy  # paste into https://adaptivecards.io/designer/
"""

from bot.session_store import BotSession, DraftTicket


def _priority_color(priority: str) -> str:
    """Map priority to Adaptive Card color string."""
    return {
        "Critical": "Attention",
        "High":     "Warning",
        "Medium":   "Default",
        "Low":      "Good",
    }.get(priority, "Default")


def build_processing_card(meeting_title: str) -> dict:
    """
    Returns a minimal 'processing...' card shown immediately after the
    user triggers the pipeline, while the LLM pipeline runs in the background.

    Send this card first (~1 second), then update it with the review card
    once the pipeline completes (~20-25 seconds later).
    """
    return {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.5",
        "body": [
            {
                "type": "TextBlock",
                "text": f"SageJiraBot — {meeting_title}",
                "weight": "Bolder",
                "size": "Medium",
            },
            {
                "type": "TextBlock",
                "text": "Processing transcript... this takes about 20-25 seconds.",
                "wrap": True,
                "isSubtle": True,
            },
            {
                "type": "Image",
                "url": "https://c.tenor.com/I6kN-6X7nhAAAAAj/loading-buffering.gif",
                "size": "Small",
                "horizontalAlignment": "Left",
                "altText": "Loading...",
            },
        ],
    }


def build_review_card(session: BotSession) -> dict:
    """
    Returns Adaptive Card JSON dict for the ticket review interface.

    Shows a table of draft tickets with:
      - Summary (editable Input.Text)
      - Priority badge
      - Suggested epic (if matched)
      - Per-row Toggle to include/exclude from submission
    Plus global "Approve Selected" and "Cancel" buttons.

    Args:
        session: BotSession in REVIEW_PENDING state with draft_tickets populated.

    Returns:
        Adaptive Card v1.5 JSON as a Python dict.
    """
    ticket_count = len(session.draft_tickets)
    body: list[dict] = []

    # Header
    body.append({
        "type": "TextBlock",
        "text": f"SageJiraBot — {session.meeting_title}",
        "weight": "Bolder",
        "size": "Medium",
    })
    body.append({
        "type": "TextBlock",
        "text": (
            f"Found {ticket_count} action item{'s' if ticket_count != 1 else ''}. "
            f"Review before creating tickets in **{session.project_key}**:"
        ),
        "wrap": True,
    })

    # Column header row
    body.append({
        "type": "ColumnSet",
        "columns": [
            {"type": "Column", "width": "auto",    "items": [{"type": "TextBlock", "text": "Include", "weight": "Bolder"}]},
            {"type": "Column", "width": "stretch", "items": [{"type": "TextBlock", "text": "Summary", "weight": "Bolder"}]},
            {"type": "Column", "width": "auto",    "items": [{"type": "TextBlock", "text": "Priority", "weight": "Bolder"}]},
            {"type": "Column", "width": "auto",    "items": [{"type": "TextBlock", "text": "Epic", "weight": "Bolder"}]},
        ]
    })

    # Separator
    body.append({"type": "Separator"})

    # One row per draft ticket
    for ticket in session.draft_tickets:
        epic_text = ticket.suggested_epic_key or "(none)"

        body.append({
            "type": "ColumnSet",
            "separator": True,
            "columns": [
                {
                    "type": "Column",
                    "width": "auto",
                    "items": [{
                        "type": "Input.Toggle",
                        "id": f"include_{ticket.draft_id}",
                        "value": "true",
                        "valueOn": "true",
                        "valueOff": "false",
                        "label": "",
                    }]
                },
                {
                    "type": "Column",
                    "width": "stretch",
                    "items": [
                        {
                            "type": "Input.Text",
                            "id": f"summary_{ticket.draft_id}",
                            "value": ticket.summary,
                            "placeholder": "Ticket summary",
                        },
                        {
                            "type": "TextBlock",
                            "text": ticket.description[:120] + ("..." if len(ticket.description) > 120 else ""),
                            "wrap": True,
                            "isSubtle": True,
                            "size": "Small",
                        },
                    ]
                },
                {
                    "type": "Column",
                    "width": "auto",
                    "items": [{
                        "type": "TextBlock",
                        "text": ticket.priority,
                        "color": _priority_color(ticket.priority),
                        "weight": "Bolder",
                    }]
                },
                {
                    "type": "Column",
                    "width": "auto",
                    "items": [{
                        "type": "TextBlock",
                        "text": epic_text,
                        "isSubtle": True,
                        "size": "Small",
                    }]
                },
            ]
        })

    # Action buttons
    actions = [
        {
            "type": "Action.Submit",
            "title": f"Approve Selected ({ticket_count})",
            "style": "positive",
            "data": {
                "action": "approve",
                "session_id": session.session_id,
            }
        },
        {
            "type": "Action.Submit",
            "title": "Cancel",
            "style": "destructive",
            "data": {
                "action": "cancel",
                "session_id": session.session_id,
            }
        }
    ]

    return {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.5",
        "body": body,
        "actions": actions,
    }


def build_confirmation_card(tickets: list[dict]) -> dict:
    """
    Returns confirmation card shown after tickets are created.

    Args:
        tickets: List of dicts with keys: ticket_id, url, summary, priority
    """
    body: list[dict] = [
        {
            "type": "TextBlock",
            "text": "SageJiraBot — Tickets Created",
            "weight": "Bolder",
            "size": "Medium",
            "color": "Good",
        },
        {
            "type": "TextBlock",
            "text": f"Successfully created {len(tickets)} ticket{'s' if len(tickets) != 1 else ''} in Jira:",
            "wrap": True,
        },
    ]

    for t in tickets:
        body.append({
            "type": "ColumnSet",
            "columns": [
                {
                    "type": "Column",
                    "width": "auto",
                    "items": [{
                        "type": "TextBlock",
                        "text": t.get("ticket_id", "?"),
                        "weight": "Bolder",
                        "color": "Accent",
                    }]
                },
                {
                    "type": "Column",
                    "width": "stretch",
                    "items": [{
                        "type": "TextBlock",
                        "text": t.get("summary", ""),
                        "wrap": True,
                    }]
                },
                {
                    "type": "Column",
                    "width": "auto",
                    "items": [{
                        "type": "TextBlock",
                        "text": t.get("priority", ""),
                        "isSubtle": True,
                    }]
                },
            ]
        })

    # Add link to Jira board
    if tickets and tickets[0].get("url"):
        base_url = "/".join(tickets[0]["url"].split("/")[:3])
        body.append({
            "type": "ActionSet",
            "actions": [{
                "type": "Action.OpenUrl",
                "title": "Open Jira Board",
                "url": f"{base_url}/jira/software/projects/ST/boards",
            }]
        })

    return {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.5",
        "body": body,
    }
