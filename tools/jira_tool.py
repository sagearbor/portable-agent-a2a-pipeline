"""
Jira tool — real implementation using Jira REST API v3.

Reads credentials from environment:
    JIRA_BASE_URL     e.g. https://dcri.atlassian.net
    JIRA_EMAIL        your Atlassian account email
    JIRA_API_TOKEN    API token from id.atlassian.com/manage-profile/security/api-tokens
    JIRA_PROJECT_KEY  e.g. ST

The function signature is unchanged from the stub so agent code needs no edits.
"""

import os
import requests
from requests.auth import HTTPBasicAuth
from dotenv import load_dotenv

load_dotenv()

# Priority names must match exactly what your Jira project supports.
# Jira Cloud defaults: Highest, High, Medium, Low, Lowest
_PRIORITY_MAP = {
    "Critical": "Highest",
    "High":     "High",
    "Medium":   "Medium",
    "Low":      "Low",
}


def _client() -> tuple[str, HTTPBasicAuth, dict]:
    """Returns (base_url, auth, headers) for Jira API calls."""
    base_url = os.environ["JIRA_BASE_URL"].rstrip("/")
    email    = os.environ["JIRA_EMAIL"]
    token    = os.environ["JIRA_API_TOKEN"]
    auth     = HTTPBasicAuth(email, token)
    headers  = {"Accept": "application/json", "Content-Type": "application/json"}
    return base_url, auth, headers


def create_ticket(summary: str, description: str, priority: str = "Medium") -> dict:
    """
    Create a Jira ticket via REST API v3.

    Args:
        summary:     short title for the ticket
        description: full ticket body (plain text; converted to Atlassian Doc Format)
        priority:    Critical | High | Medium | Low

    Returns dict with keys: ticket_id, url, status, summary, priority
    """
    base_url, auth, headers = _client()
    project_key = os.environ["JIRA_PROJECT_KEY"]
    jira_priority = _PRIORITY_MAP.get(priority, "Medium")

    # Jira REST API v3 uses Atlassian Document Format for description
    payload = {
        "fields": {
            "project":     {"key": project_key},
            "summary":     summary,
            "issuetype":   {"name": "Task"},
            "priority":    {"name": jira_priority},
            "description": {
                "type":    "doc",
                "version": 1,
                "content": [
                    {
                        "type":    "paragraph",
                        "content": [{"type": "text", "text": description}],
                    }
                ],
            },
        }
    }

    resp = requests.post(
        f"{base_url}/rest/api/3/issue",
        json=payload,
        auth=auth,
        headers=headers,
        timeout=15,
    )

    if not resp.ok:
        raise RuntimeError(
            f"Jira API error {resp.status_code}: {resp.text}"
        )

    data = resp.json()
    ticket_id = data["key"]
    url = f"{base_url}/browse/{ticket_id}"

    print(f"[jira_tool] Created ticket {ticket_id}: '{summary}'")
    return {
        "ticket_id": ticket_id,
        "url":       url,
        "status":    "created",
        "summary":   summary,
        "priority":  priority,
    }
