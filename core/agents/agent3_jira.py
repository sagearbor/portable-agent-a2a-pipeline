"""
Agent 3 - Jira Ticket Creator

Responsibility:
  - Receives approved, routed items from Agent 2
  - Uses the LLM to write a well-formed Jira ticket description for each
  - Calls the Jira tool to create the ticket
  - Returns a summary of what was created

In Phase 2 this becomes a real Azure agent with the jira_tool registered
as an actual tool/function call rather than a direct Python import.
"""

import json
import os
from core.clients.client import get_client, token_limit_kwarg
from core.config.settings import PROVIDER, TEMPERATURE, MAX_TOKENS, AGENT3_MODEL
from core.tools.jira_tool import create_ticket, JiraCredentials

AGENT_DEFINITION = {
    "name": "jira-creator",
    "instructions": (
        "You are a Jira ticket writing assistant. "
        "You receive approved, pre-routed items that need to become Jira tickets. "
        "For each item, write a clear Jira ticket description in this format:\n"
        "\n"
        "**Problem:** what is broken or needed\n"
        "**Impact:** who is affected and how\n"
        "**Steps to investigate:** numbered list of first actions\n"
        "\n"
        "Return a JSON array where each object has:\n"
        "  email_id:    original email id\n"
        "  summary:     the Jira ticket title (from suggested_jira_summary)\n"
        "  description: the formatted ticket body you wrote\n"
        "  priority:    the confirmed priority\n"
        "\n"
        "No explanation, just the JSON."
    ),
}


def run(
    approved_items: list[dict],
    dry_run: bool = False,
    jira_creds: dict | None = None,
) -> list[dict]:
    """
    Write and create Jira tickets for all approved items from Agent 2.
    Returns list of created (or drafted, if dry_run=True) ticket results.

    Args:
        approved_items: List of approved items from Agent 2 (agent2_router).
        dry_run:        If True, skip the actual Jira API call. Returns draft
                        results with status="draft" and a placeholder ticket_id.
                        Use this to preview tickets without creating them.
        jira_creds:     Optional dict of Jira credential overrides.  Keys are
                        the same env-var names used by jira_tool:
                        JIRA_BASE_URL, JIRA_EMAIL, JIRA_API_TOKEN,
                        JIRA_PROJECT_KEY.  When provided these take precedence
                        over environment variables for this call only.
    """
    dry_label = " [DRY RUN]" if dry_run else ""
    print(f"\n{'='*60}")
    print(f"AGENT 3 - Jira Creator  [provider: {PROVIDER}]{dry_label}")
    print(f"{'='*60}")
    print(f"[agent3] Received {len(approved_items)} approved items from Agent 2")

    if not approved_items:
        print("[agent3] Nothing to create.")
        return []

    client, default_model = get_client()
    model = AGENT3_MODEL or default_model
    print(f"[agent3] Calling LLM ({model}) to write ticket descriptions...")

    input_text = f"Write Jira tickets for these approved items:\n{json.dumps(approved_items, indent=2)}"

    if PROVIDER in ("openai_responses", "azure_responses"):
        response = client.responses.create(
            model=model,
            instructions=AGENT_DEFINITION["instructions"],
            input=input_text,
            temperature=TEMPERATURE,
        )
        raw = response.output_text

    else:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": AGENT_DEFINITION["instructions"]},
                {"role": "user",   "content": input_text},
            ],
            temperature=TEMPERATURE,
            **token_limit_kwarg(model, MAX_TOKENS),
        )
        raw = response.choices[0].message.content
        finish = response.choices[0].finish_reason
        if finish == "length":
            print(f"[agent3] WARNING: response truncated (finish_reason=length). Try fewer items.")
        if not raw:
            raise RuntimeError(
                f"LLM returned empty content (finish_reason={finish}). "
                "Token limit may be too low or content filter triggered."
            )

    # Strip markdown code fences if present
    stripped = raw.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[-1]
        stripped = stripped.rsplit("```", 1)[0].strip()

    tickets_to_create = json.loads(stripped)

    # Deterministic carry-forward from approved_items (Agent 2 output) —
    # the description-writing LLM only returns summary/description/priority,
    # so the assignee name + rationale fields would otherwise be lost.
    _CARRY_FIELDS = (
        "suggested_assignee",
        "assignee_category",
        "assignee_evidence",
        "assignee_rationale",
        "assignee_confidence",
    )
    approved_by_id = {a.get("email_id"): a for a in approved_items if a.get("email_id")}
    for ticket in tickets_to_create:
        eid = ticket.get("email_id")
        if not eid:
            continue
        src = approved_by_id.get(eid)
        if not src:
            continue
        for field in _CARRY_FIELDS:
            current = ticket.get(field)
            if current in (None, ""):
                if field in src and src[field] not in (None, ""):
                    ticket[field] = src[field]

    # Call the Jira tool for each ticket (or skip if dry_run)
    results = []
    for i, ticket in enumerate(tickets_to_create):
        if dry_run:
            # Return a draft result without hitting the Jira API
            result = {
                "ticket_id": f"DRAFT-{i}",
                "url":       "",
                "status":    "draft",
                "summary":   ticket["summary"],
                "priority":  ticket.get("priority", "Medium"),
                "description": ticket.get("description", ""),
                "suggested_assignee":  ticket.get("suggested_assignee"),
                "assignee_category":   ticket.get("assignee_category"),
                "assignee_evidence":   ticket.get("assignee_evidence"),
                "assignee_rationale":  ticket.get("assignee_rationale"),
                "assignee_confidence": ticket.get("assignee_confidence"),
            }
            result["email_id"] = ticket.get("email_id", "")
            results.append(result)
            print(f"[agent3] [DRY RUN] Drafted ticket {i}: {ticket['summary'][:60]}")
        else:
            # Convert the optional dict of env-var-keyed overrides into the
            # JiraCredentials dataclass that jira_tool expects.
            credentials: JiraCredentials | None = None
            if jira_creds:
                credentials = JiraCredentials(
                    base_url=jira_creds.get("JIRA_BASE_URL",    os.environ.get("JIRA_BASE_URL", "")),
                    email=jira_creds.get("JIRA_EMAIL",          os.environ.get("JIRA_EMAIL", "")),
                    api_token=jira_creds.get("JIRA_API_TOKEN",  os.environ.get("JIRA_API_TOKEN", "")),
                    project_key=jira_creds.get("JIRA_PROJECT_KEY", os.environ.get("JIRA_PROJECT_KEY", "ST")),
                )
            result = create_ticket(
                summary=ticket["summary"],
                description=ticket["description"],
                priority=ticket.get("priority", "Medium"),
                credentials=credentials,
            )
            result["email_id"] = ticket.get("email_id", "")
            result["description"] = ticket.get("description", "")
            result["suggested_assignee"]  = ticket.get("suggested_assignee")
            result["assignee_category"]   = ticket.get("assignee_category")
            result["assignee_evidence"]   = ticket.get("assignee_evidence")
            result["assignee_rationale"]  = ticket.get("assignee_rationale")
            result["assignee_confidence"] = ticket.get("assignee_confidence")
            results.append(result)
            print(f"[agent3] Created {result['ticket_id']}: {result['url']}")

    return results
