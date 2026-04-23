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

from core.clients.client import get_client, token_limit_kwarg
from core.config.settings import PROVIDER, TEMPERATURE, MAX_TOKENS, ENRICH_MODEL

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


def query_sprints(project_key: str) -> list[dict]:
    """
    Get active and future sprints for the project's board.
    Returns empty list if project uses Kanban (no sprints).
    """
    base_url, auth, headers = _client()

    # Find the board for this project
    try:
        board_resp = requests.get(
            f"{base_url}/rest/agile/1.0/board",
            params={"projectKeyOrId": project_key},
            auth=auth, headers=headers, timeout=15,
        )
        if not board_resp.ok:
            return []

        boards = board_resp.json().get("values", [])
        if not boards:
            return []

        board_id = boards[0]["id"]

        sprint_resp = requests.get(
            f"{base_url}/rest/agile/1.0/board/{board_id}/sprint",
            params={"state": "active,future"},
            auth=auth, headers=headers, timeout=15,
        )
        if not sprint_resp.ok:
            return []

        return [
            {
                "id": s["id"],
                "name": s.get("name", ""),
                "state": s.get("state", ""),
                "startDate": s.get("startDate"),
                "endDate": s.get("endDate"),
            }
            for s in sprint_resp.json().get("values", [])
        ]
    except Exception:
        return []


def query_fix_versions(project_key: str) -> list[dict]:
    """Get unreleased fix versions for the project."""
    base_url, auth, headers = _client()

    try:
        resp = requests.get(
            f"{base_url}/rest/api/3/project/{project_key}/version",
            params={"status": "unreleased", "orderBy": "-sequence"},
            auth=auth, headers=headers, timeout=15,
        )
        if not resp.ok:
            return []

        return [
            {
                "id": str(v["id"]),
                "name": v.get("name", ""),
                "releaseDate": v.get("releaseDate"),
            }
            for v in resp.json().get("values", resp.json()) if not v.get("released", False)
        ]
    except Exception:
        return []


def enrich_draft_tickets(
    draft_tickets: list[dict],
    epics: list[dict],
    stories: list[dict],
    sprints: list[dict] | None = None,
    fix_versions: list[dict] | None = None,
) -> list[dict]:
    """
    Use the LLM to enrich draft tickets with epic assignment, effort estimates,
    scheduling, sprint/version suggestions, and dependency identification.

    Args:
        draft_tickets: List of draft ticket dicts (from agent3 dry_run output)
        epics:         List of open epics from query_epics()
        stories:       List of recent stories from query_recent_stories()
        sprints:       List of active/future sprints (optional)
        fix_versions:  List of unreleased fix versions (optional)

    Returns:
        Same draft_tickets list with added fields:
          - suggested_epic_key:       "ST-12" or "new:Epic Name" or null
          - suggested_epic_summary:   "Epic title" or null
          - effort:                   "3d", "8h", etc.
          - start_date:              ISO date string or null
          - due_date:                ISO date string or null
          - suggested_sprint_id:     sprint ID or null
          - suggested_sprint_name:   sprint name or null
          - suggested_fix_version_id:   version ID or null
          - suggested_fix_version_name: version name or null
          - suggested_assignee:        name from transcript or null
          - dependency_indices:      list of ticket indices this ticket blocks
    """
    if not draft_tickets:
        return draft_tickets

    client, default_model = get_client()
    model = ENRICH_MODEL or default_model

    epic_context = ""
    if epics:
        epic_context = (
            f"\nExisting epics in this project:\n{json.dumps(epics, indent=2)}\n"
            "Assign tickets to existing epics when they clearly fit. "
            "If multiple tickets belong to a new feature area not covered by existing epics, "
            "suggest a new epic with suggested_epic_key = \"new:Epic Name\".\n"
        )
    else:
        epic_context = (
            "\nNo existing epics. Group related tickets under a new epic "
            "using suggested_epic_key = \"new:Epic Name\".\n"
        )

    sprint_context = ""
    if sprints:
        sprint_context = (
            f"\nAvailable sprints:\n{json.dumps(sprints, indent=2)}\n"
            "Assign tickets to the most appropriate sprint based on timing.\n"
        )

    version_context = ""
    if fix_versions:
        version_context = (
            f"\nUnreleased fix versions:\n{json.dumps(fix_versions, indent=2)}\n"
            "Suggest a fix version if appropriate.\n"
        )

    system_prompt = (
        "You are a Jira project planning assistant. You receive draft tickets from a "
        "meeting transcript and existing project context.\n"
        "\n"
        "For each draft ticket, add these fields:\n"
        "- suggested_epic_key: existing epic key (e.g. \"ST-40\") or \"new:Epic Name\" for a new epic, or null\n"
        "- suggested_epic_summary: epic summary text\n"
        "- effort: estimated effort as a Jira duration (e.g. \"3d\", \"8h\", \"1w 2d\")\n"
        "- start_date: suggested start date as ISO string (YYYY-MM-DD) — stagger based on dependencies\n"
        "- due_date: suggested due date based on effort and start_date\n"
        + ("- suggested_sprint_id: sprint ID number or null\n"
           "- suggested_sprint_name: sprint name or null\n" if sprints else "")
        + ("- suggested_fix_version_id: version ID string or null\n"
           "- suggested_fix_version_name: version name or null\n" if fix_versions else "")
        + "- suggested_assignee: first or full name of the person who will actually DO the work, or null if unclear.\n"
        "    IMPORTANT — the assignee is the DOER, not the speaker:\n"
        "      * 'Jane: Joe will take that' -> assignee 'Joe' (NOT Jane)\n"
        "      * 'Jane: I'll take that'     -> assignee 'Jane' (speaker volunteered)\n"
        "    Recognise ALL of these phrasings (case-insensitive), always pulling the doer's name:\n"
        "    * 'Assign <name> to ...'  /  'Assign this to <name>'\n"
        "    * 'Action item: <name> will/can/should ...'\n"
        "    * '<name> will take/own/handle/do/draft/create ...'\n"
        "    * '<name>, can you handle this?'  /  '<name>, please ...'\n"
        "    * '<name> is responsible for ...'\n"
        "    * 'I'll take that' / 'I can do it' / 'I've got it' -> use the speaker's own name\n"
        "    Return just the name (e.g. 'Ethan' or 'Sage Arbor') — not a sentence. Do NOT invent names; if no name is stated, return null.\n"
        "- dependency_indices: array of ticket indices (0-based) that THIS ticket blocks (empty if none)\n"
        "\n"
        "Rules for dependencies:\n"
        "- Only add dependencies when there is a real technical blocker relationship\n"
        "- Parallel work should NOT be chained — give them the same start_date\n"
        "- Use realistic effort estimates — not all tickets take the same time\n"
        "\n"
        "Keep all existing fields (summary, description, priority, email_id, ticket_id, suggested_assignee) unchanged.\n"
        "If suggested_assignee is already present and non-null on an input ticket, NEVER overwrite or drop it — "
        "only populate it yourself when it is null/missing.\n"
        "Return the complete array as JSON. No explanation, just the JSON array."
    )

    user_content = (
        f"Draft tickets (use array index for dependency_indices):\n"
        f"{json.dumps(draft_tickets, indent=2)}\n"
        + epic_context
        + (f"\nRecent stories (for context):\n{json.dumps(stories[:10], indent=2)}\n" if stories else "")
        + sprint_context
        + version_context
    )

    # Use generous token limit — enrichment output can be large.
    # With ~30 tickets × ~10 fields each, output easily reaches 15-20k tokens.
    # 8192 was causing silent truncation → JSON parse failure → falling back
    # to un-enriched tickets with no effort, dates, or dependencies.
    enrichment_max_tokens = MAX_TOKENS  # uses the global generous default (16384+)

    print(f"[jira_context] Enriching {len(draft_tickets)} tickets (max_tokens={enrichment_max_tokens})...")

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
            **token_limit_kwarg(model, enrichment_max_tokens),
        )
        raw = response.choices[0].message.content
        finish = response.choices[0].finish_reason
        if finish == "length":
            print(f"[jira_context] WARNING: enrichment response truncated (finish_reason=length)!")

    if not raw:
        print("[jira_context] WARNING: LLM returned empty enrichment response")
        return draft_tickets

    # Strip markdown code fences if present
    stripped = raw.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[-1]
        stripped = stripped.rsplit("```", 1)[0].strip()

    try:
        enriched = json.loads(stripped)
    except json.JSONDecodeError as e:
        print(f"[jira_context] ERROR: Failed to parse enrichment JSON: {e}")
        print(f"[jira_context] Raw response (last 200 chars): ...{stripped[-200:]}")
        return draft_tickets

    # Deterministic carry-forward: re-assert suggested_assignee and the
    # assignee rationale fields from the input whenever the enrichment LLM
    # forgot to echo them. Match by ticket_id first, then fall back to array
    # position.
    _CARRY_FIELDS = (
        "suggested_assignee",
        "assignee_category",
        "assignee_evidence",
        "assignee_rationale",
        "assignee_confidence",
    )
    input_by_id = {t.get("ticket_id"): t for t in draft_tickets if t.get("ticket_id")}
    for idx, t in enumerate(enriched):
        src = input_by_id.get(t.get("ticket_id")) or (draft_tickets[idx] if idx < len(draft_tickets) else {})
        if not src:
            continue
        for field in _CARRY_FIELDS:
            current = t.get(field)
            if current in (None, ""):
                if field in src and src[field] not in (None, ""):
                    t[field] = src[field]

    print(f"[jira_context] Enriched {len(enriched)} tickets successfully")
    # Log a sample to verify fields are populated
    if enriched:
        sample = enriched[0]
        print(f"[jira_context] Sample ticket[0]: effort={sample.get('effort')}, "
              f"start_date={sample.get('start_date')}, due_date={sample.get('due_date')}, "
              f"deps={sample.get('dependency_indices')}")

    # Assignee extraction visibility — helps debug when the LLM fails to
    # pick up "Assign X" / "Action item: X" phrasings from the transcript.
    assignee_counts = sum(1 for t in enriched if t.get("suggested_assignee"))
    print(f"[jira_context] suggested_assignee populated on {assignee_counts}/{len(enriched)} tickets")
    for i, t in enumerate(enriched):
        sa = t.get("suggested_assignee")
        if sa:
            summary = (t.get("summary") or "")[:60]
            print(f"[jira_context]   ticket[{i}] assignee={sa!r}  summary={summary!r}")

    return enriched


# Keep backward compat alias
def match_tickets_to_epics(
    draft_tickets: list[dict],
    epics: list[dict],
    stories: list[dict],
) -> list[dict]:
    """Legacy wrapper — calls enrich_draft_tickets with epics only."""
    return enrich_draft_tickets(draft_tickets, epics, stories)
