"""
Agent 1 - Email Reader

Responsibility:
  - Reads emails from a specified Outlook folder
  - Uses the LLM to extract structured, actionable information from each email
  - Passes a clean list of candidates to Agent 2 (the router)

A2A output: list of dicts, one per email, with extracted fields.
"""

import json
from core.clients.client import get_client, token_limit_kwarg
from core.config.settings import PROVIDER, TEMPERATURE, MAX_TOKENS
from core.tools.outlook_tool import read_emails

# ------------------------------------------------------------------
# Agent definition - this same dict will be used in Phase 2 to
# register this agent in Azure AI Foundry via create_agents.py
# ------------------------------------------------------------------
AGENT_DEFINITION = {
    "name": "email-reader",
    "instructions": (
        "You are an email triage assistant. "
        "You receive raw emails and extract structured information from them. "
        "For each email return a JSON object with these fields:\n"
        "  email_id:    the original email id\n"
        "  subject:     the email subject\n"
        "  sender:      who sent it\n"
        "  summary:     one sentence summary of the content\n"
        "  is_actionable: true if this looks like it needs a task or ticket, false otherwise\n"
        "  suggested_priority: Critical | High | Medium | Low (only if is_actionable is true)\n"
        "  suggested_jira_summary: a concise Jira ticket title (only if is_actionable is true)\n"
        "\n"
        "Return a JSON array of these objects. No explanation, just the JSON."
    ),
}

# Separate instructions for transcript mode where one input contains an
# entire meeting and we need to extract MANY action items from it.
TRANSCRIPT_INSTRUCTIONS = (
    "You are a meeting transcript analyst. "
    "You receive a meeting transcript and extract ALL distinct actionable items from it. "
    "An actionable item is anything discussed that should become a task, bug fix, feature request, "
    "follow-up, decision to implement, or investigation. Extract every one — do not consolidate "
    "multiple distinct items into a single entry.\n"
    "\n"
    "For each actionable item return a JSON object with these fields:\n"
    "  email_id:    a unique id like 'action_1', 'action_2', etc.\n"
    "  subject:     the topic area this item falls under\n"
    "  sender:      who raised or owns this item (speaker name from transcript)\n"
    "  summary:     one sentence summary of what needs to be done\n"
    "  is_actionable: true\n"
    "  suggested_priority: Critical | High | Medium | Low\n"
    "  suggested_jira_summary: a concise Jira ticket title\n"
    "\n"
    "Also include non-actionable items (informational updates, social chat) with "
    "is_actionable: false so the router agent can see what was filtered at this stage.\n"
    "\n"
    "Return a JSON array of ALL items found. Aim for completeness — it is better to "
    "extract too many items than to miss real action items. No explanation, just the JSON."
)


def _format_emails_text(emails: list[dict]) -> str:
    """Format a list of email dicts into a prompt-friendly text block."""
    email_text = ""
    for i, email in enumerate(emails, 1):
        email_text += (
            f"\n--- Email {i} ---\n"
            f"ID: {email['id']}\n"
            f"From: {email['sender']}\n"
            f"Subject: {email['subject']}\n"
            f"Body: {email['body']}\n"
        )
    return email_text


def _is_transcript_mode(emails: list[dict]) -> bool:
    """Detect if inputs came from the transcript adapter (vs Outlook emails)."""
    return any(e.get("id", "").startswith("transcript_") for e in emails)


def _extract_from_emails(emails: list[dict]) -> list[dict]:
    """
    Core extraction logic: call LLM on a list of email dicts and
    return structured extraction results.

    Detects transcript mode (single full-transcript item) and uses
    a prompt tuned for extracting many action items from one document.
    Falls back to the per-email prompt for multi-item inputs.

    Shared by both run() and run_on_items().
    """
    transcript_mode = _is_transcript_mode(emails)
    instructions = TRANSCRIPT_INSTRUCTIONS if transcript_mode else AGENT_DEFINITION["instructions"]

    if transcript_mode:
        # Transcript input — combine all segments into one block for the LLM
        combined = "\n\n".join(e["body"] for e in emails)
        user_content = f"Here is the meeting transcript to analyze:\n\n{combined}"
        print(f"[agent1] Transcript mode: extracting multiple action items from {len(emails)} segment(s)")
    else:
        email_text = _format_emails_text(emails)
        user_content = f"Here are the emails to process:\n{email_text}"

    client, model = get_client()
    print(f"[agent1] Calling LLM ({model}) to extract structure...")

    if PROVIDER in ("openai_responses", "azure_responses"):
        response = client.responses.create(
            model=model,
            instructions=instructions,
            input=user_content,
            temperature=TEMPERATURE,
        )
        raw = response.output_text

    else:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": instructions},
                {"role": "user",   "content": user_content},
            ],
            temperature=TEMPERATURE,
            **token_limit_kwarg(model, MAX_TOKENS),
        )
        raw = response.choices[0].message.content

    # Guard against empty / None LLM responses
    if not raw:
        raise RuntimeError(
            f"[agent1] LLM returned empty content. Possible content filter or token limit issue."
        )

    # Strip markdown code fences if LLM wraps output in ```json ... ```
    stripped = raw.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[-1]
        stripped = stripped.rsplit("```", 1)[0].strip()

    print(f"[agent1] Raw LLM response (first 300 chars): {stripped[:300]}")
    extracted = json.loads(stripped)
    print(f"[agent1] Extracted {len(extracted)} email records")
    return extracted


def run_on_items(items: list[dict]) -> list[dict]:
    """
    Extract structured data from pre-fetched email-shaped dicts via LLM.
    Skips the Outlook fetch step — used by the transcript pipeline where
    bot/adapters/transcript_adapter.py has already produced the input dicts.

    Args:
        items: List of dicts with keys: id, sender, subject, body
               (same format produced by tools/outlook_tool.read_emails)

    Returns list of extracted email dicts for Agent 2.
    """
    print(f"\n{'='*60}")
    print(f"AGENT 1 - Email Reader (pre-fetched items)  [provider: {PROVIDER}]")
    print(f"{'='*60}")
    print(f"[agent1] Received {len(items)} pre-fetched items (skipping Outlook fetch)")
    return _extract_from_emails(items)


def run(folder: str = "Inbox") -> list[dict]:
    """
    Read emails from folder, extract structured data via LLM.
    Returns list of extracted email dicts for Agent 2.
    """
    print(f"\n{'='*60}")
    print(f"AGENT 1 - Email Reader  [provider: {PROVIDER}]")
    print(f"{'='*60}")

    # Step 1: fetch emails via tool
    emails = read_emails(folder=folder)
    print(f"[agent1] Fetched {len(emails)} emails from '{folder}'")

    return _extract_from_emails(emails)
