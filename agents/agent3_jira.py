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
from clients.client import get_client
from config.settings import PROVIDER, TEMPERATURE, MAX_TOKENS
from tools.jira_tool import create_ticket

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


def run(approved_items: list[dict]) -> list[dict]:
    """
    Write and create Jira tickets for all approved items from Agent 2.
    Returns list of created ticket results.
    """
    print(f"\n{'='*60}")
    print(f"AGENT 3 - Jira Creator  [provider: {PROVIDER}]")
    print(f"{'='*60}")
    print(f"[agent3] Received {len(approved_items)} approved items from Agent 2")

    if not approved_items:
        print("[agent3] Nothing to create.")
        return []

    client, model = get_client()
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
            max_tokens=MAX_TOKENS,
        )
        raw = response.choices[0].message.content

    tickets_to_create = json.loads(raw)

    # Call the Jira tool for each ticket
    results = []
    for ticket in tickets_to_create:
        result = create_ticket(
            summary=ticket["summary"],
            description=ticket["description"],
            priority=ticket.get("priority", "Medium"),
        )
        result["email_id"] = ticket["email_id"]
        results.append(result)
        print(f"[agent3] Created {result['ticket_id']}: {result['url']}")

    return results
