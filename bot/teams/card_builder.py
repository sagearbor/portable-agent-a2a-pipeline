"""
Adaptive Card JSON builder for the SageJiraBot ticket review UI.

Builds the Adaptive Card v1.5 JSON that is sent to Teams when a transcript
has been processed and the user needs to review/approve draft tickets.

Reference: https://adaptivecards.io/designer/
Schema: https://adaptivecards.io/schemas/adaptive-card.json

Card types:
  - build_live_item_card: one card per action item, posted in real time during
    a live meeting as the caption stream is processed.
  - build_summary_card: end-of-meeting card summarising all pending drafts;
    also the first (and only) card shown on the transcript paste path.
  - build_review_card: original batch-review card (retained for compatibility).
  - build_processing_card: "processing..." placeholder while the pipeline runs.
  - build_confirmation_card: confirmation shown after tickets are created.

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
    """Map priority label to an Adaptive Card color string.

    Mapping (PRP section 4.4):
        Critical -> "attention"  (red)
        High     -> "warning"   (orange)
        Medium   -> "accent"    (blue)
        Low      -> "default"   (gray)
    """
    return {
        "Critical": "attention",
        "High":     "warning",
        "Medium":   "accent",
        "Low":      "default",
    }.get(priority, "default")


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


def build_live_item_card(ticket: dict, item_number: int) -> dict:
    """
    Return an Adaptive Card v1.5 dict for a single action item detected during
    a live meeting.  One card is posted to the channel each time the caption
    pipeline surfaces a new action item.

    The user can immediately react with Keep / Edit / Discard without waiting
    for the meeting to finish.

    Args:
        ticket:      Draft ticket dict. Required keys:
                         draft_id  (str)  — stable identifier for this draft
                         summary   (str)  — one-line ticket title
                         priority  (str)  — Critical | High | Medium | Low
                     Optional keys used if present:
                         description (str) — longer context text shown as a
                                             subtitle beneath the summary
        item_number: 1-based counter shown in the card title (e.g. "Action
                     Item #3 detected").

    Returns:
        Adaptive Card v1.5 JSON as a Python dict ready to be wrapped in a
        Bot Framework attachment payload.

    Action button data payloads (received by the bot message handler):
        keep_item    {"action": "keep_item",    "draft_id": "<id>"}
        edit_item    {"action": "edit_item",    "draft_id": "<id>"}
        discard_item {"action": "discard_item", "draft_id": "<id>"}
    """
    draft_id = ticket["draft_id"]
    summary  = ticket.get("summary", "(no summary)")
    priority = ticket.get("priority", "Medium")
    description = ticket.get("description", "")

    body: list[dict] = [
        {
            "type":   "TextBlock",
            "text":   f"Action Item #{item_number} detected",
            "weight": "Bolder",
            "size":   "Medium",
        },
        {
            "type": "TextBlock",
            "text": summary,
            "wrap": True,
        },
    ]

    # Optional description subtitle (first 120 chars to keep the card compact)
    if description:
        body.append({
            "type":     "TextBlock",
            "text":     description[:120] + ("..." if len(description) > 120 else ""),
            "wrap":     True,
            "isSubtle": True,
            "size":     "Small",
        })

    # Priority badge via FactSet
    body.append({
        "type": "FactSet",
        "facts": [
            {
                "title": "Priority",
                "value": priority,
            }
        ],
        # FactSet does not support per-fact color in v1.5; the color is
        # surfaced through the companion TextBlock below for emphasis.
    })

    # Coloured priority label so users get the visual signal at a glance
    body.append({
        "type":   "TextBlock",
        "text":   priority,
        "color":  _priority_color(priority),
        "weight": "Bolder",
        "size":   "Small",
    })

    actions = [
        {
            "type":  "Action.Submit",
            "title": "✓ Keep",
            "style": "positive",
            "data":  {"action": "keep_item", "draft_id": draft_id},
        },
        {
            "type":  "Action.Submit",
            "title": "✏ Edit",
            "data":  {"action": "edit_item", "draft_id": draft_id},
        },
        {
            "type":  "Action.Submit",
            "title": "✗ Discard",
            "style": "destructive",
            "data":  {"action": "discard_item", "draft_id": draft_id},
        },
    ]

    return {
        "type":    "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.5",
        "body":    body,
        "actions": actions,
    }


def build_summary_card(draft_tickets: list[dict], project_key: str) -> dict:
    """
    Return an Adaptive Card v1.5 dict summarising all pending (non-discarded)
    draft tickets at the end of a meeting.

    This card is posted in two situations:
      1. After the user sends "@SageJiraBot done" (or the meeting ends).
      2. Immediately when the user pastes a transcript (paste path — no live
         incremental cards were shown, so this is the first card).

    Each row shows a ticket summary, priority (colour-coded), and an [Edit]
    button.  Bottom-level actions let the user bulk-create or discard all.

    Args:
        draft_tickets: List of draft ticket dicts.  Each dict must contain:
                           draft_id    (str)
                           summary     (str)
                           priority    (str) Critical | High | Medium | Low
                       Optional keys used if present:
                           suggested_epic_key (str)
        project_key:   Jira project key shown in the header (e.g. "ST").

    Returns:
        Adaptive Card v1.5 JSON as a Python dict.

    Action button data payloads (received by the bot message handler):
        create_all      {"action": "create_all",      "session_id": "<id>"}
        create_selected {"action": "create_selected", "session_id": "<id>"}
        discard_all     {"action": "discard_all",     "session_id": "<id>"}

    Note: session_id is populated as an empty string here because card_builder
    has no access to the session.  The bot handler MUST overwrite
    data["session_id"] with the real session ID before sending the card.
    """
    n = len(draft_tickets)
    body: list[dict] = []

    # Header
    body.append({
        "type":   "TextBlock",
        "text":   f"Meeting complete \u2014 {n} action item{'s' if n != 1 else ''}",
        "weight": "Bolder",
        "size":   "Medium",
    })
    body.append({
        "type": "TextBlock",
        "text": (
            f"{n} action item{'s' if n != 1 else ''} pending review. "
            f"Create all or review individually in **{project_key}**:"
        ),
        "wrap": True,
    })

    # Column header row
    body.append({
        "type": "ColumnSet",
        "columns": [
            {
                "type":  "Column",
                "width": "auto",
                "items": [{"type": "TextBlock", "text": "#", "weight": "Bolder"}],
            },
            {
                "type":  "Column",
                "width": "stretch",
                "items": [{"type": "TextBlock", "text": "Summary", "weight": "Bolder"}],
            },
            {
                "type":  "Column",
                "width": "auto",
                "items": [{"type": "TextBlock", "text": "Priority", "weight": "Bolder"}],
            },
            {
                "type":  "Column",
                "width": "auto",
                "items": [{"type": "TextBlock", "text": "Epic", "weight": "Bolder"}],
            },
            {
                "type":  "Column",
                "width": "auto",
                "items": [{"type": "TextBlock", "text": "Action", "weight": "Bolder"}],
            },
        ],
    })

    # One row per draft ticket
    for idx, ticket in enumerate(draft_tickets, start=1):
        draft_id  = ticket["draft_id"]
        summary   = ticket.get("summary", "(no summary)")
        priority  = ticket.get("priority", "Medium")
        epic_text = ticket.get("suggested_epic_key") or "(none)"

        body.append({
            "type":      "ColumnSet",
            "separator": True,
            "columns":   [
                {
                    "type":  "Column",
                    "width": "auto",
                    "items": [{
                        "type": "TextBlock",
                        "text": str(idx),
                    }],
                },
                {
                    "type":  "Column",
                    "width": "stretch",
                    "items": [{
                        "type": "TextBlock",
                        "text": summary,
                        "wrap": True,
                    }],
                },
                {
                    "type":  "Column",
                    "width": "auto",
                    "items": [{
                        "type":   "TextBlock",
                        "text":   priority,
                        "color":  _priority_color(priority),
                        "weight": "Bolder",
                    }],
                },
                {
                    "type":  "Column",
                    "width": "auto",
                    "items": [{
                        "type":     "TextBlock",
                        "text":     epic_text,
                        "isSubtle": True,
                        "size":     "Small",
                    }],
                },
                {
                    "type":  "Column",
                    "width": "auto",
                    "items": [{
                        "type": "ActionSet",
                        "actions": [{
                            "type":  "Action.Submit",
                            "title": "Edit",
                            "data":  {"action": "edit_item", "draft_id": draft_id},
                        }],
                    }],
                },
            ],
        })

    # Bottom-level bulk actions.
    # NOTE: bot handler must replace the empty session_id before sending.
    actions = [
        {
            "type":  "Action.Submit",
            "title": f"Create all ({n})",
            "style": "positive",
            "data":  {"action": "create_all", "session_id": ""},
        },
        {
            "type":  "Action.Submit",
            "title": "Create selected",
            "data":  {"action": "create_selected", "session_id": ""},
        },
        {
            "type":  "Action.Submit",
            "title": "Discard all",
            "style": "destructive",
            "data":  {"action": "discard_all", "session_id": ""},
        },
    ]

    return {
        "type":    "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.5",
        "body":    body,
        "actions": actions,
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
