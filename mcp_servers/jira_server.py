"""
MCP server wrapping Jira tools for DCRI.

Thin wrapper around core/ functions — no business logic here.
Exposes tools for ticket creation, epic/sprint/version queries,
duplicate checking, issue linking, and LLM-based ticket enrichment.

Run standalone:
    python mcp_servers/jira_server.py

Configure in Claude Code (.claude.json or settings):
    See .claude/mcp-config-example.json
"""

from fastmcp import FastMCP

mcp = FastMCP(
    "dcri-jira-tools",
    instructions=(
        "Jira ticket management for DCRI — create tickets, epics, stories "
        "with dates, dependencies, and smart enrichment"
    ),
)


# ---------------------------------------------------------------------------
# Tool: create_ticket
# ---------------------------------------------------------------------------

@mcp.tool()
def create_ticket(
    summary: str,
    description: str,
    project_key: str = "ST",
    priority: str = "Medium",
    issue_type: str = "Story",
    epic_key: str | None = None,
    labels: list[str] | None = None,
    start_date: str | None = None,
    due_date: str | None = None,
    original_estimate: str | None = None,
) -> dict:
    """Create a Jira ticket (Story, Task, Sub-task, Epic, or Bug) in the specified project.

    Args:
        summary: Short title for the ticket.
        description: Full ticket body (plain text).
        project_key: Jira project key (e.g. "ST").
        priority: Critical, High, Medium, or Low.
        issue_type: Jira issue type — Epic, Story, Task, Sub-task, or Bug.
        epic_key: Parent key for Story→Epic or Sub-task→Story links (e.g. "ST-40").
        labels: Optional list of label strings.
        start_date: ISO date string (YYYY-MM-DD) for start date.
        due_date: ISO date string (YYYY-MM-DD) for due date.
        original_estimate: Jira duration string (e.g. "3d", "8h", "1w 2d").

    Returns:
        Dict with keys: ticket_id, url, status, summary, priority.
    """
    from core.tools.jira_tool import create_ticket as _create

    return _create(
        summary=summary,
        description=description,
        priority=priority,
        issue_type=issue_type,
        epic_key=epic_key,
        labels=labels,
        start_date=start_date,
        due_date=due_date,
        original_estimate=original_estimate,
    )


# ---------------------------------------------------------------------------
# Tool: query_epics
# ---------------------------------------------------------------------------

@mcp.tool()
def query_epics(project_key: str = "ST") -> list[dict]:
    """Get all open epics in a Jira project.

    Args:
        project_key: Jira project key (e.g. "ST").

    Returns:
        List of dicts with keys: key, summary, status, description_excerpt.
    """
    from core.jira_context import query_epics as _query

    return _query(project_key)


# ---------------------------------------------------------------------------
# Tool: query_sprints
# ---------------------------------------------------------------------------

@mcp.tool()
def query_sprints(project_key: str = "ST") -> list[dict]:
    """Get active and future sprints for a project.

    Returns empty list if the project uses Kanban (no sprints).

    Args:
        project_key: Jira project key (e.g. "ST").

    Returns:
        List of dicts with keys: id, name, state, startDate, endDate.
    """
    from core.jira_context import query_sprints as _query

    return _query(project_key)


# ---------------------------------------------------------------------------
# Tool: query_fix_versions
# ---------------------------------------------------------------------------

@mcp.tool()
def query_fix_versions(project_key: str = "ST") -> list[dict]:
    """Get unreleased fix versions for a project.

    Args:
        project_key: Jira project key (e.g. "ST").

    Returns:
        List of dicts with keys: id, name, releaseDate.
    """
    from core.jira_context import query_fix_versions as _query

    return _query(project_key)


# ---------------------------------------------------------------------------
# Tool: check_duplicates
# ---------------------------------------------------------------------------

@mcp.tool()
def check_duplicates(
    summaries: list[str],
    project_key: str = "ST",
) -> list[dict]:
    """Check if similar tickets already exist in Jira for a list of summaries.

    For each summary, extracts keywords and searches via JQL text matching.
    Returns up to 3 matching issues per summary.

    Args:
        summaries: List of ticket summary strings to check.
        project_key: Jira project key (e.g. "ST").

    Returns:
        List of dicts, one per summary, each with keys:
          - summary: the input summary
          - duplicates: list of {key, summary, url} for matching issues
    """
    import os
    import re
    import requests
    from requests.auth import HTTPBasicAuth

    # Stop words for keyword extraction (same set as jira_search.py)
    stop_words = frozenset({
        "a", "an", "the", "and", "or", "but", "is", "are", "was", "were",
        "be", "been", "being", "in", "on", "at", "to", "for", "of", "with",
        "by", "from", "as", "into", "through", "during", "before", "after",
        "it", "its", "this", "that", "these", "those", "do", "does", "did",
        "will", "would", "could", "should", "may", "might", "can", "shall",
        "not", "no", "so", "if", "then", "than", "too", "very", "just",
        "about", "up", "out", "all", "each", "every", "both", "few", "more",
        "most", "other", "some", "such", "only", "own", "same", "also",
        "how", "what", "which", "who", "whom", "when", "where", "why",
        "new", "need", "create", "add", "update", "implement", "set",
    })

    base_url = os.environ["JIRA_BASE_URL"].rstrip("/")
    auth = HTTPBasicAuth(os.environ["JIRA_EMAIL"], os.environ["JIRA_API_TOKEN"])
    headers = {"Accept": "application/json", "Content-Type": "application/json"}

    results = []

    for summary in summaries:
        # Extract 2-3 meaningful keywords
        words = re.findall(r"[a-zA-Z0-9]+", summary)
        keywords = [w for w in words if w.lower() not in stop_words and len(w) > 2][:3]

        if not keywords:
            results.append({"summary": summary, "duplicates": []})
            continue

        keyword_text = " ".join(keywords)
        jql = (
            f'project = "{project_key}" '
            f'AND summary ~ "{keyword_text}" '
            f'AND statusCategory != Done'
        )

        try:
            resp = requests.post(
                f"{base_url}/rest/api/3/search/jql",
                json={"jql": jql, "fields": ["key", "summary"], "maxResults": 3},
                auth=auth,
                headers=headers,
                timeout=15,
            )

            if not resp.ok:
                results.append({"summary": summary, "duplicates": []})
                continue

            duplicates = []
            for issue in resp.json().get("issues", []):
                duplicates.append({
                    "key": issue["key"],
                    "summary": issue["fields"].get("summary", ""),
                    "url": f"{base_url}/browse/{issue['key']}",
                })

            results.append({"summary": summary, "duplicates": duplicates})

        except Exception:
            results.append({"summary": summary, "duplicates": []})

    return results


# ---------------------------------------------------------------------------
# Tool: create_issue_link
# ---------------------------------------------------------------------------

@mcp.tool()
def create_issue_link(
    inward_key: str,
    outward_key: str,
    link_type: str = "Blocks",
) -> bool:
    """Create a dependency link between two Jira issues.

    Example: create_issue_link("ST-10", "ST-11") means ST-10 blocks ST-11.

    Args:
        inward_key: The blocker issue key (e.g. "ST-10").
        outward_key: The blocked issue key (e.g. "ST-11").
        link_type: Jira link type — "Blocks", "Cloners", "Duplicate", etc.

    Returns:
        True on success.
    """
    from core.tools.jira_tool import create_issue_link as _link

    return _link(inward_key, outward_key, link_type)


# ---------------------------------------------------------------------------
# Tool: enrich_tickets
# ---------------------------------------------------------------------------

@mcp.tool()
def enrich_tickets(
    draft_tickets: list[dict],
    project_key: str = "ST",
) -> list[dict]:
    """Enrich draft tickets with epic assignments, effort estimates, dates,
    and dependencies using LLM analysis and Jira project context.

    Queries the project for epics, recent stories, sprints, and fix versions,
    then uses the LLM to assign each draft ticket to an appropriate epic,
    estimate effort, suggest dates, and identify dependencies.

    Args:
        draft_tickets: List of draft ticket dicts (from transcript parsing).
            Each dict should have at minimum: summary, description.
        project_key: Jira project key (e.g. "ST").

    Returns:
        Same ticket list with added fields: suggested_epic_key,
        suggested_epic_summary, effort, start_date, due_date,
        dependency_indices, and optionally sprint/version suggestions.
    """
    from core.jira_context import (
        query_epics as _query_epics,
        query_recent_stories,
        query_sprints as _query_sprints,
        query_fix_versions as _query_fix_versions,
        enrich_draft_tickets,
    )

    epics = _query_epics(project_key)
    stories = query_recent_stories(project_key)
    sprints = _query_sprints(project_key)
    versions = _query_fix_versions(project_key)

    return enrich_draft_tickets(draft_tickets, epics, stories, sprints, versions)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run()
