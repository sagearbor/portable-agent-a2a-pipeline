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
from clients.client import get_client
from config.settings import PROVIDER, TEMPERATURE, MAX_TOKENS

AGENT_DEFINITION = {
    "name": "router",
    "instructions": (
        "You are a routing agent that decides which emails should become Jira tickets. "
        "You receive a list of pre-extracted email summaries. "
        "Your job is to filter and enrich them.\n"
        "\n"
        "Rules:\n"
        "  - Only approve items where is_actionable is true\n"
        "  - Reject anything that is social, administrative, or unclear\n"
        "  - For approved items, confirm or adjust the suggested_priority\n"
        "  - For approved items, confirm or improve the suggested_jira_summary\n"
        "  - Add a field 'routing_reason' explaining in one sentence why this warrants a ticket\n"
        "\n"
        "Return a JSON array of approved items only. No explanation, just the JSON."
    ),
}


def run(email_extracts: list[dict]) -> list[dict]:
    """
    Filter and route email extracts from Agent 1.
    Returns only the items approved for Jira ticket creation.
    """
    print(f"\n{'='*60}")
    print(f"AGENT 2 - Router  [provider: {PROVIDER}]")
    print(f"{'='*60}")
    print(f"[agent2] Received {len(email_extracts)} items from Agent 1")

    client, model = get_client()
    print(f"[agent2] Calling LLM ({model}) to route...")

    input_text = f"Here are the email extracts to evaluate:\n{json.dumps(email_extracts, indent=2)}"

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

    approved = json.loads(raw)
    print(f"[agent2] Approved {len(approved)} items for ticket creation")
    for item in approved:
        print(f"  -> {item.get('suggested_jira_summary', '?')}  [{item.get('suggested_priority', '?')}]")

    return approved
