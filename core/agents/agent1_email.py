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


def _extract_from_emails(emails: list[dict]) -> list[dict]:
    """
    Core extraction logic: call LLM on a list of email dicts and
    return structured extraction results.

    Shared by both run() and run_on_items().
    """
    email_text = _format_emails_text(emails)

    client, model = get_client()
    print(f"[agent1] Calling LLM ({model}) to extract structure...")

    if PROVIDER in ("openai_responses", "azure_responses"):
        # ------------------------------------------------------------------
        # Responses API path - stateful, server holds thread
        # client.responses.create() is the call pattern
        # ------------------------------------------------------------------
        response = client.responses.create(
            model=model,
            instructions=AGENT_DEFINITION["instructions"],
            input=f"Here are the emails to process:\n{email_text}",
            temperature=TEMPERATURE,
        )
        raw = response.output_text

    else:
        # ------------------------------------------------------------------
        # Chat Completions path - stateless, full history each call
        # client.chat.completions.create() is the call pattern
        # ------------------------------------------------------------------
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": AGENT_DEFINITION["instructions"]},
                {"role": "user",   "content": f"Here are the emails to process:\n{email_text}"},
            ],
            temperature=TEMPERATURE,
            **token_limit_kwarg(model, MAX_TOKENS),
        )
        raw = response.choices[0].message.content

    # Strip markdown code fences if LLM wraps output in ```json ... ```
    stripped = raw.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[-1]
        stripped = stripped.rsplit("```", 1)[0].strip()

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
