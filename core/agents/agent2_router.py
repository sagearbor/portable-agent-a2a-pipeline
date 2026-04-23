"""
Agent 2 - Router

Responsibility:
  - Receives structured email data from Agent 1
  - Decides which emails are genuinely worth creating a Jira ticket for
  - Enriches the approved items with routing metadata
  - Passes approved items to Agent 3

This is the A2A handoff point: Agent 1 -> Agent 2 -> Agent 3.
In Phase 2 this becomes a real Azure agent that receives a thread message.
"""

import json
from core.clients.client import get_client, token_limit_kwarg
from core.config.settings import PROVIDER, TEMPERATURE, MAX_TOKENS

AGENT_DEFINITION = {
    "name": "router",
    "instructions": (
        "You are a routing agent that decides which items should become Jira tickets. "
        "You receive a list of pre-extracted summaries. "
        "Your job is to classify every item as approved or rejected.\n"
        "\n"
        "Rules:\n"
        "  - Approve items that represent concrete, actionable work (bugs, features, tasks, decisions needing follow-up)\n"
        "  - Reject anything that is social, administrative, purely informational, or unclear\n"
        "  - For approved items, confirm or adjust the suggested_priority\n"
        "  - For approved items, confirm or improve the suggested_jira_summary\n"
        "  - For each item add 'routing_reason' explaining in one sentence why it was approved or rejected\n"
        "  - PRESERVE the 'suggested_assignee' field on each item exactly as received — do not drop it, rename it, or blank it.\n"
        "\n"
        "Return a JSON object with two arrays:\n"
        "  { \"approved\": [...items to become tickets...], \"rejected\": [...items filtered out...] }\n"
        "Each rejected item needs only: email_id, suggested_jira_summary (or summary), routing_reason.\n"
        "No explanation outside the JSON."
    ),
}


def run(email_extracts: list[dict]) -> dict:
    """
    Filter and route email extracts from Agent 1.

    Returns a dict with:
        approved: list of items to become Jira tickets
        rejected: list of items that were filtered out (with reasons)
    """
    print(f"\n{'='*60}")
    print(f"AGENT 2 - Router  [provider: {PROVIDER}]")
    print(f"{'='*60}")
    print(f"[agent2] Received {len(email_extracts)} items from Agent 1")

    client, model = get_client()
    print(f"[agent2] Calling LLM ({model}) to route...")

    input_text = f"Here are the extracts to evaluate:\n{json.dumps(email_extracts, indent=2)}"

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

    # Guard against empty / None LLM responses
    if not raw:
        raise RuntimeError(
            f"[agent2] LLM returned empty content. Possible content filter or token limit issue."
        )

    # Strip markdown code fences if LLM wraps output in ```json ... ```
    stripped = raw.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[-1]
        stripped = stripped.rsplit("```", 1)[0].strip()

    print(f"[agent2] Raw LLM response (first 300 chars): {stripped[:300]}")
    parsed = json.loads(stripped)

    # Handle both old format (plain array) and new format (object with approved/rejected)
    if isinstance(parsed, list):
        # Old format: LLM returned just the approved array
        approved = parsed
        rejected = []
    else:
        approved = parsed.get("approved", [])
        rejected = parsed.get("rejected", [])

    # Deterministic carry-forward: if the router LLM dropped suggested_assignee,
    # restore it from the original extract keyed by email_id. This is cheaper
    # and more reliable than asking the LLM to preserve extra fields.
    extract_by_id = {e.get("email_id"): e for e in email_extracts if e.get("email_id")}
    for item in approved:
        eid = item.get("email_id")
        if eid and not item.get("suggested_assignee"):
            src = extract_by_id.get(eid)
            if src and src.get("suggested_assignee"):
                item["suggested_assignee"] = src["suggested_assignee"]

    print(f"[agent2] Approved {len(approved)} items for ticket creation")
    for item in approved:
        print(f"  -> {item.get('suggested_jira_summary', '?')}  [{item.get('suggested_priority', '?')}]")
    if rejected:
        print(f"[agent2] Rejected {len(rejected)} items:")
        for item in rejected:
            reason = item.get('routing_reason', 'no reason given')
            summary = item.get('suggested_jira_summary', item.get('summary', '?'))
            print(f"  x  {summary} — {reason}")

    return {"approved": approved, "rejected": rejected}
