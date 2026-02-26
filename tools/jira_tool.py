"""
Jira tool - stub version for Phase 1.

In Phase 1 this pretends to create tickets and returns a fake ticket ID.
In Phase 2 this will call the real Jira REST API.
The function signature stays the same so agent code does not change.
"""

import random
import string


def create_ticket(summary: str, description: str, priority: str = "Medium") -> dict:
    """
    Create a Jira ticket.

    Phase 1: simulates ticket creation, returns stub response.
    Phase 2: will POST to Jira REST API with real credentials.

    Args:
        summary:     short title for the ticket
        description: full ticket body
        priority:    Low | Medium | High | Critical

    Returns dict with keys:
        ticket_id, url, status
    """

    # ------------------------------------------------------------------
    # STUB - replace this block in Phase 2 with real Jira API call
    # ------------------------------------------------------------------
    fake_id = "DCRI-" + "".join(random.choices(string.digits, k=4))
    result = {
        "ticket_id": fake_id,
        "url": f"https://jira.duke.edu/browse/{fake_id}",
        "status": "created",
        "summary": summary,
        "priority": priority,
    }
    # ------------------------------------------------------------------

    print(f"[jira_tool] Created ticket {fake_id}: '{summary}' (stub)")
    return result
