"""
Jira context module — queries existing epics and stories to provide
context for smart ticket assignment during the review flow.

Used by the Teams bot before presenting the Adaptive Card so each
draft ticket shows a suggested epic. Uses the same _client() pattern
from tools/jira_tool.py and get_client() from clients/client.py.
"""

import json
import os
import requests
from requests.auth import HTTPBasicAuth
from dotenv import load_dotenv

from clients.client import get_client, token_limit_kwarg
from config.settings import PROVIDER, TEMPERATURE, MAX_TOKENS

load_dotenv()


def _client() -> tuple[str, HTTPBasicAuth, dict]:
    """Returns (base_url, auth, headers) for Jira API calls.
    Same pattern as tools/jira_tool.py."""
    base_url = os.environ["JIRA_BASE_URL"].rstrip("/")
    email    = os.environ["JIRA_EMAIL"]
    token    = os.environ["JIRA_API_TOKEN"]
    auth     = HTTPBasicAuth(email, token)
    headers  = {"Accept": "application/json", "Content-Type": "application/json"}
    return base_url, auth, headers


def query_epics(project_key: str) -> list[dict]:
    """
    Query Jira for all open epics in the given project.

    Args:
        project_key: Jira project key (e.g. "ST")

    Returns:
        List of dicts with keys: key, summary, description_excerpt
        Only returns epics that are not Done/Cancelled.
    """
    base_url, auth, headers = _client()

    # JQL: all epics in project that aren't Done
    jql = (
        f'project = "{project_key}" '
        f'AND issuetype = Epic '
        f'AND statusCategory != Done '
        f'ORDER BY created DESC'
    )
    payload = {
        "jql": jql,
        "fields": ["key", "summary", "description", "status"],
        "maxResults": 50,
    }

    resp = requests.post(
        f"{base_url}/rest/api/3/search/jql",
        json=payload,
        auth=auth,
        headers=headers,
        timeout=15,
    )

    if not resp.ok:
        raise RuntimeError(
            f"Jira epic query failed {resp.status_code}: {resp.text[:200]}"
        )

    epics = []
    for issue in resp.json().get("issues", []):
        fields = issue["fields"]

        # Extract plain text description excerpt from ADF format
        description_excerpt = ""
        desc = fields.get("description")
        if desc and isinstance(desc, dict):
            try:
                content = desc.get("content", [])
                texts = []
                for block in content:
                    for inline in block.get("content", []):
                        if inline.get("type") == "text":
                            texts.append(inline.get("text", ""))
                description_excerpt = " ".join(texts)[:200]
            except Exception:
                pass

        epics.append({
            "key":                 issue["key"],
            "summary":             fields.get("summary", ""),
            "description_excerpt": description_excerpt,
            "status":              fields.get("status", {}).get("name", ""),
        })

    return epics


def query_recent_stories(project_key: str, limit: int = 20) -> list[dict]:
    """
    Query Jira for recent tasks/stories in the given project.

    Args:
        project_key: Jira project key (e.g. "ST")
        limit:       Maximum number of stories to return

    Returns:
        List of dicts with keys: key, summary, status, assignee
    """
    base_url, auth, headers = _client()

    jql = (
        f'project = "{project_key}" '
        f'AND issuetype in (Story, Task) '
        f'AND created >= -30d '
        f'ORDER BY created DESC'
    )
    payload = {
        "jql": jql,
        "fields": ["key", "summary", "status", "assignee"],
        "maxResults": limit,
    }

    resp = requests.post(
        f"{base_url}/rest/api/3/search/jql",
        json=payload,
        auth=auth,
        headers=headers,
        timeout=15,
    )

    if not resp.ok:
        raise RuntimeError(
            f"Jira story query failed {resp.status_code}: {resp.text[:200]}"
        )

    stories = []
    for issue in resp.json().get("issues", []):
        fields = issue["fields"]
        assignee = fields.get("assignee")
        assignee_email = (
            assignee.get("emailAddress", "") if assignee else ""
        )

        stories.append({
            "key":     issue["key"],
            "summary": fields.get("summary", ""),
            "status":  fields.get("status", {}).get("name", ""),
            "assignee": assignee_email,
        })

    return stories


def match_tickets_to_epics(
    draft_tickets: list[dict],
    epics: list[dict],
    stories: list[dict],
) -> list[dict]:
    """
    Use the LLM to suggest the best epic for each draft ticket.
    Adds suggested_epic_key and suggested_epic_summary fields to each ticket.

    Args:
        draft_tickets: List of draft ticket dicts (from agent3 dry_run output)
        epics:         List of open epics from query_epics()
        stories:       List of recent stories from query_recent_stories()

    Returns:
        Same draft_tickets list with added fields:
          - suggested_epic_key:     "ST-12" or null
          - suggested_epic_summary: "Epic title" or null
    """
    if not draft_tickets:
        return draft_tickets

    if not epics:
        # No epics to match against — return drafts unchanged
        for t in draft_tickets:
            t.setdefault("suggested_epic_key", None)
            t.setdefault("suggested_epic_summary", None)
        return draft_tickets

    client, model = get_client()

    system_prompt = (
        "You are a Jira context assistant. You receive a list of draft tickets and "
        "a list of existing Jira epics and recent stories from the same project.\n"
        "\n"
        "For each draft ticket:\n"
        "- If there is a clearly relevant epic (same feature area or system), "
        "set suggested_epic_key to that epic's key and suggested_epic_summary to its summary.\n"
        "- If no epic clearly fits, set both to null.\n"
        "\n"
        "Return the draft_tickets list as JSON with suggested_epic_key and "
        "suggested_epic_summary fields added to each. "
        "No explanation, just the JSON array."
    )

    user_content = (
        f"Draft tickets:\n{json.dumps(draft_tickets, indent=2)}\n\n"
        f"Existing epics:\n{json.dumps(epics, indent=2)}\n\n"
        f"Recent stories (for context):\n{json.dumps(stories[:10], indent=2)}"
    )

    if PROVIDER in ("openai_responses", "azure_responses"):
        response = client.responses.create(
            model=model,
            instructions=system_prompt,
            input=user_content,
            temperature=TEMPERATURE,
        )
        raw = response.output_text
    else:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": user_content},
            ],
            temperature=TEMPERATURE,
            **token_limit_kwarg(model, MAX_TOKENS),
        )
        raw = response.choices[0].message.content

    # Strip markdown code fences if present
    stripped = raw.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[-1]
        stripped = stripped.rsplit("```", 1)[0].strip()

    enriched = json.loads(stripped)
    return enriched
