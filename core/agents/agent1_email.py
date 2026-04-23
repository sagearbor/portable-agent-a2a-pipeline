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
from core.config.settings import PROVIDER, TEMPERATURE, MAX_TOKENS, AGENT1_MODEL
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
    "OUTPUT SHAPE — return a single JSON object with two keys:\n"
    "{\n"
    "  \"items\":               [ ... action items, one per object as described below ... ],\n"
    "  \"meeting_directives\": [ ... zero or more global instructions ... ]\n"
    "}\n"
    "(Returning a plain array of items is also accepted for backward compatibility, \n"
    "in which case meeting_directives is treated as empty.)\n"
    "\n"
    "meeting_directives are blanket instructions the speakers made that apply across \n"
    "multiple action items, e.g. 'assign everything unassigned to Joe' or 'mark the \n"
    "migration task as a blocker'. Only include a directive when the speaker clearly \n"
    "intended it as a sweeping rule — do not manufacture directives from a single \n"
    "per-ticket assignment. Each directive is an object:\n"
    "  {\"action\": \"assign_all\"|\"skip_item\"|\"set_priority\",\n"
    "   \"value\":  \"Joe\" | \"Critical\" | \"High\" | \"Medium\" | \"Low\",\n"
    "   \"match\":  null for all, or a short summary-fragment for targeting one item}\n"
    "Phrasings to recognise as directives:\n"
    "  * 'assign everything to Joe' / 'anything unassigned goes to Joe' -> assign_all, value=\"Joe\", match=null\n"
    "  * 'make the <X> task a blocker' / '<X> is critical' -> set_priority, value=\"Critical\", match=\"<X>\"\n"
    "  * 'skip the <Y> one' / 'we don't need to track <Y>' -> skip_item, match=\"<Y>\"\n"
    "If no directives were said, return meeting_directives: [].\n"
    "\n"
    "For each actionable item return a JSON object with these fields:\n"
    "  email_id:    a unique id like 'action_1', 'action_2', etc.\n"
    "  subject:     the topic area this item falls under\n"
    "  sender:      who raised or owns this item (speaker name from transcript)\n"
    "  summary:     one sentence summary of what needs to be done\n"
    "  is_actionable: true\n"
    "  suggested_priority: Critical | High | Medium | Low\n"
    "  suggested_jira_summary: a concise Jira ticket title\n"
    "  suggested_assignee: first or full name of the person who will actually DO the work, or null if unclear.\n"
    "                      IMPORTANT — the assignee is the DOER, not the speaker:\n"
    "                        * 'Jane: Joe will take that'    -> assignee is 'Joe'  (NOT Jane)\n"
    "                        * 'Jane: I'll take that'        -> assignee is 'Jane' (speaker volunteered)\n"
    "                        * 'Jane: Assign Joe to X'       -> assignee is 'Joe'\n"
    "                        * 'Jane: I can do it'           -> assignee is 'Jane'\n"
    "                        * 'Jane: Joe, can you handle X' -> assignee is 'Joe'\n"
    "                      Recognise ALL of these phrasings (case-insensitive) — always pull the doer's name:\n"
    "                        * 'Assign <name> to ...'  /  'Assign this to <name>'\n"
    "                        * 'Action item: <name> will/can/should ...'\n"
    "                        * '<name> will take/own/handle/do/draft/create ...'\n"
    "                        * '<name>, can you handle this?'  /  '<name>, please ...'\n"
    "                        * '<name> is responsible for ...'\n"
    "                        * 'I'll take that' / 'I can do it' / 'I've got it' -> use the speaker's own name\n"
    "                      Return JUST the name (e.g. 'Ethan' or 'Sage Arbor') — not a sentence.\n"
    "                      Do NOT invent names. If no name is stated, return null.\n"
    "  assignee_category: one of these enum values (pick exactly one):\n"
    "      * 'directive_explicit'      — speaker explicitly said 'assign <name> to this'\n"
    "      * 'speaker_volunteered'     — the speaker said 'I'll take it' / 'I can do that'\n"
    "      * 'assigned_by_name'        — another speaker said '<name> will do X' or '<name>, can you handle X'\n"
    "      * 'inferred_from_context'   — no direct assignment, but ownership is clear from context\n"
    "      * 'unassignable'            — no name was stated and none can be inferred (suggested_assignee MUST be null)\n"
    "      (The 'directive_bulk' value is reserved for the meeting_directives applier — do NOT emit it yourself.)\n"
    "  assignee_evidence: a SHORT verbatim substring of the transcript (max ~200 chars) that supports\n"
    "                     the assignment. This MUST be a literal copy-paste of words from the transcript —\n"
    "                     do NOT paraphrase, summarise, or fabricate. Use '' (empty string) when\n"
    "                     assignee_category is 'unassignable'.\n"
    "  assignee_rationale: one short sentence (max ~200 chars) explaining why this category + evidence\n"
    "                      implies this assignee. Paraphrasing is fine here.\n"
    "  assignee_confidence: float 0.0-1.0 — your confidence that this assignment is correct.\n"
    "                       Use 0.0 when category is 'unassignable'.\n"
    "\n"
    "Also include non-actionable items (informational updates, social chat) with "
    "is_actionable: false so the router agent can see what was filtered at this stage.\n"
    "\n"
    "Aim for completeness — it is better to "
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

    client, default_model = get_client()
    model = AGENT1_MODEL or default_model
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
        finish = response.choices[0].finish_reason
        if finish == "length":
            print(f"[agent1] WARNING: response truncated (finish_reason=length). "
                  f"Raw length={len(raw or '')}. Increase MAX_TOKENS or split the transcript.")

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
    parsed = json.loads(stripped)

    # Accept either the new {items, meeting_directives} object form or the
    # legacy plain array. We stash directives as a second element on the
    # returned list via a private attribute — callers that don't know about
    # directives will just see the items list (backward compatible).
    if isinstance(parsed, dict):
        extracted  = parsed.get("items", []) or []
        directives = parsed.get("meeting_directives", []) or []
    else:
        extracted  = parsed
        directives = []

    # Anti-confabulation check: when the LLM claims a verbatim transcript span
    # as evidence for an assignee, verify the span actually appears in the
    # input text. If not, strip it and knock down confidence. This prevents
    # the "made up a quote" failure mode that would poison the audit trail.
    if transcript_mode:
        combined_text = "\n\n".join(e.get("body", "") for e in emails)
        for item in extracted:
            ev = item.get("assignee_evidence")
            if ev and isinstance(ev, str) and ev.strip():
                if ev not in combined_text:
                    print(
                        f"[agent1] WARNING: hallucinated assignee_evidence stripped from "
                        f"email_id={item.get('email_id')!r}: {ev[:120]!r}"
                    )
                    item["assignee_evidence"] = ""
                    try:
                        conf = float(item.get("assignee_confidence") or 0.0)
                    except (TypeError, ValueError):
                        conf = 0.0
                    item["assignee_confidence"] = min(conf, 0.5)

    # Attach directives to the list so run_on_items_with_directives can read
    # them, without changing the public return shape.
    try:
        extracted.__meeting_directives__ = directives  # type: ignore[attr-defined]
    except Exception:
        # list doesn't support attribute assignment directly; wrap in subclass.
        class _ListWithDirectives(list):
            pass
        wrapped = _ListWithDirectives(extracted)
        wrapped.__meeting_directives__ = directives  # type: ignore[attr-defined]
        extracted = wrapped

    print(f"[agent1] Extracted {len(extracted)} email records; {len(directives)} meeting directive(s)")
    for d in directives:
        print(f"[agent1]   directive: {d}")
    return extracted


def get_meeting_directives(extracted: list) -> list[dict]:
    """Return meeting directives attached to an extraction result, or []."""
    return list(getattr(extracted, "__meeting_directives__", []) or [])


def apply_meeting_directives(items: list[dict], directives: list[dict]) -> list[dict]:
    """
    Apply global meeting directives to the extracted items in place.

    Supported actions:
      - assign_all:    value=<name>; only fills blank suggested_assignee so
                       individually-assigned items keep their names.
      - set_priority:  value=Critical|High|Medium|Low; applies to items whose
                       summary contains ``match`` (case-insensitive), or to
                       all items when match is null.
      - skip_item:     drops items whose summary contains ``match``.

    Returns the (possibly filtered) list — callers should use the return
    value rather than the original because skip_item may shorten it.
    """
    if not directives or not items:
        return items

    out = list(items)
    for d in directives:
        action = (d.get("action") or "").lower()
        value  = d.get("value")
        match  = (d.get("match") or "").lower().strip() or None

        if action == "assign_all" and value:
            count = 0
            for t in out:
                if not t.get("suggested_assignee"):
                    t["suggested_assignee"] = value
                    # Populate rationale metadata ONLY when empty — never
                    # overwrite per-item reasoning that Agent 1 already set.
                    if not t.get("assignee_category"):
                        t["assignee_category"] = "directive_bulk"
                    if not t.get("assignee_evidence"):
                        # Bulk directives are not per-ticket quotes, so leave
                        # evidence blank rather than stuff the directive text
                        # in (it would fail the substring check elsewhere).
                        t["assignee_evidence"] = ""
                    if not t.get("assignee_rationale"):
                        t["assignee_rationale"] = (
                            f"filled by bulk meeting directive '{value}'"
                        )
                    if t.get("assignee_confidence") in (None, 0, 0.0):
                        t["assignee_confidence"] = 0.8
                    count += 1
            print(f"[directives] assign_all: set '{value}' on {count} item(s) with blank assignee")

        elif action == "set_priority" and value:
            hit = 0
            for t in out:
                summary = (t.get("summary") or t.get("suggested_jira_summary") or "").lower()
                if match is None or match in summary:
                    t["suggested_priority"] = value
                    hit += 1
            print(f"[directives] set_priority: '{value}' on {hit} item(s) (match={match!r})")

        elif action == "skip_item" and match:
            before = len(out)
            out = [
                t for t in out
                if match not in (t.get("summary") or t.get("suggested_jira_summary") or "").lower()
            ]
            print(f"[directives] skip_item: dropped {before - len(out)} item(s) (match={match!r})")

        else:
            print(f"[directives] ignored unrecognised directive: {d}")

    return out


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
