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

    Two auth modes are supported:

    1. **Basic auth** (service account / API token) — set ``email`` +
       ``api_token``.  ``base_url`` points at the site (e.g.
       ``https://dcri.atlassian.net``).  Tickets are attributed to the
       owner of the API token.

    2. **OAuth 3LO bearer token** (per-user sign-in) — set
       ``access_token`` + ``cloud_id``.  ``base_url`` is ignored; calls
       go to ``https://api.atlassian.com/ex/jira/{cloud_id}``.  Tickets
       are attributed to the signed-in user.

    ``project_key`` is required in both modes.  Pass to
    ``create_ticket(credentials=...)`` to override the
    JIRA_BASE_URL / JIRA_EMAIL / JIRA_API_TOKEN / JIRA_PROJECT_KEY env vars.
    """
    base_url:      str
    email:         str
    api_token:     str
    project_key:   str
    access_token:  str | None = None   # set for OAuth mode
    cloud_id:      str | None = None   # set for OAuth mode


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _client(
    credentials: JiraCredentials | None = None,
) -> tuple[str, HTTPBasicAuth | None, dict]:
    """
    Returns (base_url, auth, headers) for Jira API calls.

    Auth mode:
      * OAuth bearer token  — when credentials has access_token + cloud_id.
        Returns (api.atlassian.com/ex/jira/<cid>, None, headers-with-bearer).
      * Basic auth          — when credentials has email + api_token, or
        when falling back to JIRA_EMAIL / JIRA_API_TOKEN env vars.
        Returns (site-base-url, HTTPBasicAuth, plain-headers).

    Callers pass the returned ``auth`` straight through to ``requests.*``.
    ``requests`` treats ``auth=None`` as a no-op, so the caller path is
    identical for both modes.
    """
    headers = {"Accept": "application/json", "Content-Type": "application/json"}

    if credentials is not None and credentials.access_token and credentials.cloud_id:
        # OAuth mode — bearer token, routed through api.atlassian.com
        base_url = f"https://api.atlassian.com/ex/jira/{credentials.cloud_id}"
        headers = {**headers, "Authorization": f"Bearer {credentials.access_token}"}
        return base_url, None, headers

    if credentials is not None:
        base_url = credentials.base_url.rstrip("/")
        auth = HTTPBasicAuth(credentials.email, credentials.api_token)
    else:
        base_url = os.environ["JIRA_BASE_URL"].rstrip("/")
        email    = os.environ["JIRA_EMAIL"]
        token    = os.environ["JIRA_API_TOKEN"]
        auth     = HTTPBasicAuth(email, token)

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
    issue_type: str = "Story",
    epic_key: str | None = None,
    credentials: JiraCredentials | None = None,
    labels: list[str] | None = None,
    sprint_id: int | None = None,
    fix_version_id: str | None = None,
    start_date: str | None = None,
    due_date: str | None = None,
    original_estimate: str | None = None,
    assignee_account_id: str | None = None,
) -> dict:
    """
    Create a Jira ticket via REST API v3.

    Args:
        summary:            short title for the ticket
        description:        full ticket body (plain text; converted to Atlassian Doc Format)
        priority:           Critical | High | Medium | Low
        issue_type:         Jira issue type name — "Epic", "Story", "Sub-task", "Task", "Bug"
        epic_key:           optional parent key (e.g. 'ST-40'); sets the parent link
                            (use for Story→Epic or Sub-task→Story relationships)
        credentials:        optional JiraCredentials; overrides env vars
        labels:             optional list of label strings
        sprint_id:          optional sprint ID; issue is moved into this sprint after creation
        fix_version_id:     optional fixVersion ID string (from /rest/api/3/project/{key}/versions)
        start_date:         optional ISO date string e.g. "2026-03-25"
        due_date:           optional ISO date string e.g. "2026-03-28"
        original_estimate:  optional Jira duration string e.g. "3d", "8h", "1w 2d"

    Returns dict with keys: ticket_id, url, status, summary, priority
    """
    base_url, auth, headers = _client(credentials)

    if credentials is not None:
        project_key = credentials.project_key
    else:
        project_key = os.environ["JIRA_PROJECT_KEY"]

    jira_priority = _PRIORITY_MAP.get(priority, "Medium")

    # Jira REST API v3 uses Atlassian Document Format for description
    fields: dict = {
        "project":     {"key": project_key},
        "summary":     summary,
        "issuetype":   {"name": issue_type},
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

    # Optional fields — only include when provided
    if epic_key:
        fields["parent"] = {"key": epic_key}
    if labels:
        fields["labels"] = labels
    if fix_version_id:
        fields["fixVersions"] = [{"id": fix_version_id}]
    # Dates are set via POST-creation update (see below) because field IDs
    # vary across Jira instances and project types. We discover the correct
    # field IDs from editmeta after the issue exists.
    if original_estimate:
        fields["timetracking"] = {"originalEstimate": original_estimate}
    if assignee_account_id:
        fields["assignee"] = {"accountId": assignee_account_id}

    payload = {"fields": fields}

    resp = requests.post(
        f"{base_url}/rest/api/3/issue",
        json=payload,
        auth=auth,
        headers=headers,
        timeout=15,
    )

    # If create fails due to unsupported fields, retry without them
    if resp.status_code == 400 and "cannot be set" in resp.text:
        # Drop any field that Jira rejects — covers date custom fields, timetracking, etc.
        import re
        err_fields = re.findall(r"\"(\w+)\":\"Field '[^']+' cannot be set", resp.text)
        droppable = err_fields if err_fields else ["timetracking", "fixVersions"]
        dropped = [f for f in droppable if f in fields]
        if dropped:
            for f in dropped:
                del fields[f]
            print(f"[jira_tool] Retrying without unsupported fields: {dropped}")
            resp = requests.post(
                f"{base_url}/rest/api/3/issue",
                json={"fields": fields},
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

    # Set dates via PUT update — discover correct field IDs from editmeta
    if start_date or due_date:
        try:
            meta = requests.get(
                f"{base_url}/rest/api/3/issue/{ticket_id}/editmeta",
                auth=auth, headers=headers, timeout=10,
            )
            if meta.ok:
                date_update = {}
                for fid, fmeta in meta.json().get("fields", {}).items():
                    fname = fmeta.get("name", "").lower()
                    if "start" in fname and "date" in fname and start_date:
                        date_update[fid] = start_date
                    elif fname == "due date" and due_date:
                        date_update[fid] = due_date
                if date_update:
                    dr = requests.put(
                        f"{base_url}/rest/api/3/issue/{ticket_id}",
                        json={"fields": date_update},
                        auth=auth, headers=headers, timeout=10,
                    )
                    if dr.ok:
                        print(f"[jira_tool] Set dates on {ticket_id}: {date_update}")
                    else:
                        print(f"[jira_tool] Date update failed on {ticket_id}: {dr.text[:100]}")
        except Exception as e:
            print(f"[jira_tool] Date update error: {e}")

    # Sprint assignment uses the Agile REST API (custom field IDs vary by
    # instance, so we move the issue into the sprint after creation instead
    # of setting a custom field in the create payload).
    if sprint_id is not None:
        sprint_resp = requests.post(
            f"{base_url}/rest/agile/1.0/sprint/{sprint_id}/issue",
            json={"issues": [ticket_id]},
            auth=auth,
            headers=headers,
            timeout=15,
        )
        if not sprint_resp.ok:
            print(
                f"[jira_tool] WARNING: created {ticket_id} but failed to "
                f"assign to sprint {sprint_id}: {sprint_resp.status_code} "
                f"{sprint_resp.text}"
            )

    print(f"[jira_tool] Created {issue_type} {ticket_id}: '{summary}'")
    return {
        "ticket_id": ticket_id,
        "url":       url,
        "status":    "created",
        "summary":   summary,
        "priority":  priority,
    }


# ---------------------------------------------------------------------------
# Issue linking
# ---------------------------------------------------------------------------

def create_issue_link(
    inward_key: str,
    outward_key: str,
    link_type: str = "Blocks",
    credentials: JiraCredentials | None = None,
) -> bool:
    """
    Create a link between two Jira issues.

    Args:
        inward_key:  the blocker issue key   (e.g. "ST-10")
        outward_key: the blocked issue key   (e.g. "ST-11")
        link_type:   Jira link type name — "Blocks", "Cloners", "Duplicate", etc.
        credentials: optional JiraCredentials; overrides env vars

    Returns True on success.
    Raises RuntimeError on failure.

    Example:
        # ST-10 blocks ST-11
        create_issue_link("ST-10", "ST-11")
    """
    base_url, auth, headers = _client(credentials)

    payload = {
        "type":         {"name": link_type},
        "inwardIssue":  {"key": inward_key},
        "outwardIssue": {"key": outward_key},
    }

    resp = requests.post(
        f"{base_url}/rest/api/3/issueLink",
        json=payload,
        auth=auth,
        headers=headers,
        timeout=15,
    )

    if not resp.ok:
        raise RuntimeError(
            f"Jira issueLink error {resp.status_code}: {resp.text}"
        )

    print(f"[jira_tool] Linked {inward_key} --[{link_type}]--> {outward_key}")
    return True
