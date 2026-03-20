"""
Jira tool — real implementation using Jira REST API v3.

Reads credentials from environment:
    JIRA_BASE_URL     e.g. https://dcri.atlassian.net
    JIRA_EMAIL        your Atlassian account email
    JIRA_API_TOKEN    API token from id.atlassian.com/manage-profile/security/api-tokens
    JIRA_PROJECT_KEY  e.g. ST

For the multi-user Teams bot (Option B — service account), set:
    JIRA_SERVICE_EMAIL  service account email (e.g. sagejirabot@duke.edu)
    JIRA_SERVICE_TOKEN  service account API token

The function signature of create_ticket() is unchanged from the stub so agent
code needs no edits when using default (env-var) credentials.

Pass a JiraCredentials instance to create_ticket(credentials=...) to override
the environment variables — used by the Teams bot and future OAuth flow.
"""

import os
import requests
from dataclasses import dataclass
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


# ---------------------------------------------------------------------------
# Credentials dataclass
# ---------------------------------------------------------------------------

@dataclass
class JiraCredentials:
    """
    Explicit Jira credentials for a single API call.

    Used by the Teams bot to pass service-account or (future) per-user OAuth
    credentials without relying on environment variables.

    When passed to create_ticket(credentials=...) these values override the
    JIRA_BASE_URL / JIRA_EMAIL / JIRA_API_TOKEN / JIRA_PROJECT_KEY env vars.
    """
    base_url:    str
    email:       str
    api_token:   str
    project_key: str


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _client(
    credentials: JiraCredentials | None = None,
) -> tuple[str, HTTPBasicAuth, dict]:
    """
    Returns (base_url, auth, headers) for Jira API calls.

    If credentials is provided, use those values.
    Otherwise fall back to environment variables.
    """
    if credentials is not None:
        base_url = credentials.base_url.rstrip("/")
        auth = HTTPBasicAuth(credentials.email, credentials.api_token)
    else:
        base_url = os.environ["JIRA_BASE_URL"].rstrip("/")
        email    = os.environ["JIRA_EMAIL"]
        token    = os.environ["JIRA_API_TOKEN"]
        auth     = HTTPBasicAuth(email, token)

    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    return base_url, auth, headers


# ---------------------------------------------------------------------------
# Permission check (Option B service account guard)
# ---------------------------------------------------------------------------

def check_project_permission(
    project_key: str,
    base_url: str | None = None,
    auth: HTTPBasicAuth | None = None,
) -> bool:
    """
    Check whether the configured Jira credentials have access to a project.

    Calls GET {base_url}/rest/api/3/project/{project_key}.

    Returns:
        True  — 200: project exists and the authenticated user has Browse access
        False — 403: project exists but user lacks access
        False — 404: project does not exist

    Raises:
        RuntimeError — for unexpected HTTP status codes (network error,
                       misconfigured auth, server error, etc.)

    Args:
        project_key: Jira project key to check (e.g. "ST", "OPS")
        base_url:    Jira base URL; falls back to JIRA_BASE_URL env var
        auth:        HTTPBasicAuth object; falls back to JIRA_EMAIL +
                     JIRA_API_TOKEN env vars

    Usage (Teams bot, service account):
        from tools.jira_tool import check_project_permission

        if not check_project_permission("OPS"):
            raise ValueError(
                "Service account lacks access to Jira project 'OPS'. "
                "Ask IT to grant sagejirabot access."
            )
    """
    if base_url is None:
        base_url = os.environ["JIRA_BASE_URL"].rstrip("/")
    else:
        base_url = base_url.rstrip("/")

    if auth is None:
        email = os.environ["JIRA_EMAIL"]
        token = os.environ["JIRA_API_TOKEN"]
        auth  = HTTPBasicAuth(email, token)

    headers = {"Accept": "application/json"}

    resp = requests.get(
        f"{base_url}/rest/api/3/project/{project_key}",
        auth=auth,
        headers=headers,
        timeout=10,
    )

    if resp.status_code == 200:
        return True
    if resp.status_code in (403, 404):
        return False

    raise RuntimeError(
        f"check_project_permission: unexpected status {resp.status_code} "
        f"for project '{project_key}': {resp.text}"
    )


# ---------------------------------------------------------------------------
# Ticket creation
# ---------------------------------------------------------------------------

def create_ticket(
    summary: str,
    description: str,
    priority: str = "Medium",
    epic_key: str | None = None,
    credentials: JiraCredentials | None = None,
) -> dict:
    """
    Create a Jira ticket via REST API v3.

    Args:
        summary:     short title for the ticket
        description: full ticket body (plain text; converted to Atlassian Doc Format)
        priority:    Critical | High | Medium | Low
        epic_key:    optional parent epic key (e.g. 'ST-40'); sets the parent link
        credentials: optional JiraCredentials; if provided, use these instead of
                     environment variables. Enables the Teams bot and future OAuth
                     flow to pass per-user or service-account credentials.

    Returns dict with keys: ticket_id, url, status, summary, priority
    """
    base_url, auth, headers = _client(credentials)

    if credentials is not None:
        project_key = credentials.project_key
    else:
        project_key = os.environ["JIRA_PROJECT_KEY"]

    jira_priority = _PRIORITY_MAP.get(priority, "Medium")

    # Jira REST API v3 uses Atlassian Document Format for description
    payload = {
        "fields": {
            "project":     {"key": project_key},
            "summary":     summary,
            "issuetype":   {"name": "Task"},
            "priority":    {"name": jira_priority},
            **( {"parent": {"key": epic_key}} if epic_key else {} ),
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
