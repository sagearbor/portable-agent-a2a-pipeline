# Product Requirements & Planning: SageJiraBot for Microsoft Teams

**Document version:** 1.0
**Date:** 2026-03-20
**Status:** Draft — ready for implementation
**Author:** Derived from codebase analysis of `portable-agent-a2a-pipeline`

---

## Table of Contents

1. [Overview & Goals](#1-overview--goals)
2. [Architecture Diagram](#2-architecture-diagram)
3. [Phase 1: FastAPI Transcript Endpoint](#3-phase-1-fastapi-transcript-endpoint)
4. [Phase 2: Full Real-Time Teams Bot](#4-phase-2-full-real-time-teams-bot)
5. [Cross-Platform Support](#5-cross-platform-support)
6. [IT Dependencies](#6-it-dependencies)
7. [Implementation Order](#7-implementation-order)
8. [Environment Variables](#8-environment-variables)
9. [Testing Plan](#9-testing-plan)

---

## 1. Overview & Goals

### What We Are Building

SageJiraBot is a Microsoft Teams bot that bridges meeting discussions to Jira tickets. It extends the existing three-agent pipeline (defined in `orchestration/pipeline.py`) — which was built to process Outlook emails — to instead process meeting transcripts and real-time caption streams.

The project is built in two phases:

- **Phase 1 (MVP):** A FastAPI HTTP endpoint that accepts a transcript as plain text and runs the existing agent pipeline to produce Jira tickets. No Teams integration required. Can be called from curl, a test harness, or the bot. Proves the pipeline works with transcript-shaped input before adding Teams complexity.

- **Phase 2 (Full Bot):** `@SageJiraBot` is invited to a Teams meeting. As the meeting runs, the bot receives live captions. At meeting end (or on demand), it drafts tickets, presents an Adaptive Card table for human review, then creates approved tickets in Jira with smart context linking (epics, assignees).

### Why This Matters

The existing pipeline (agents 1-3) already demonstrates that LLMs can convert unstructured natural language into well-formed Jira tickets. Meetings are the richest source of undocumented action items inside a team. Manual ticket creation from meeting notes is slow, inconsistent, and often skipped entirely.

SageJiraBot closes that loop: the meeting IS the ticket creation workflow.

### Goals

| Goal | Success Metric |
|---|---|
| Convert a meeting transcript to Jira tickets with one command | Tickets appear in ST board within 60 seconds of approval |
| Human stays in control | No ticket created without explicit approval via Adaptive Card |
| Works with recorded meetings AND live meetings | Both transcript file upload and real-time caption paths produce identical output |
| Data stays inside Duke tenant | All LLM calls go to `ai-foundry-dcri-sage` (Azure, PROVIDER = "azure") |
| Extensible to Zoom/Webex | Same FastAPI endpoint accepts transcript text from any source |

### Out of Scope (Phase 1 and 2)

- Automatic meeting recording or transcription (bot uses Teams Transcription API; recording is the user's responsibility)
- PHI detection / redaction (separate concern, handled at the Jira project policy level)
- Multi-language transcripts (English only)
- Mobile Teams client (desktop and web only)

---

## 2. Architecture Diagram

### Phase 1: FastAPI Endpoint

```
User / Teams Bot / curl
        |
        | POST /api/v1/process-transcript
        | { "transcript": "...", "project_key": "ST" }
        |
        v
+---------------------------+
|  FastAPI App              |
|  bot/api/main.py          |
|                           |
|  TranscriptRequest        |
|  -> adapter layer         |
|     (maps transcript to   |
|      email-shaped dicts)  |
+---------------------------+
        |
        | calls orchestration/pipeline.py
        | (existing code, unchanged)
        v
+-------+--------+----------+
|       |        |          |
| Agent1 |Agent2 | Agent3   |
| email  |router | jira     |
| .py    |.py    | .py      |
+-------+--------+----------+
        |
        | get_client() -> AzureOpenAI
        | PROVIDER = "azure"
        v
+----------------------------+
|  Azure AI Foundry          |
|  ai-foundry-dcri-sage      |
|  model: gpt-5.2            |
|  auth: az_login / MI       |
+----------------------------+
        |
        v
+----------------------------+
|  Jira REST API v3          |
|  dcri.atlassian.net        |
|  project: ST               |
|  tools/jira_tool.py        |
+----------------------------+
        |
        | HTTP 200 response
        v
{ "tickets_created": [...],
  "draft_tickets": [...],   <- Phase 1 returns drafts for UI
  "elapsed_seconds": 22 }
```

### Phase 2: Full Teams Bot

```
Teams Meeting
     |  (live captions stream via Graph API)
     |  OR (transcript file available at meeting end)
     v
+-----------------------------+
|  Teams Bot Service          |
|  Azure Bot Service          |
|  bot/teams_handler.py       |
|                             |
|  - onMembersAdded           |
|  - onMessage (commands)     |
|  - onMeetingEnd (webhook)   |
|  - onTranscriptReady        |
+-----------------------------+
     |
     | 1. Receive transcript / captions
     | 2. POST to FastAPI /process-transcript
     v
+-----------------------------+
|  FastAPI App (Phase 1)      |
|  bot/api/main.py            |
+-----------------------------+
     |
     | runs 3-agent pipeline
     v
+-------+--------+----------+
| Agent1 |Agent2 | Agent3   |
| (transcript    | (jira)   |
|  parser)       |          |
+-------+--------+----------+
     |
     | returns draft_tickets[]
     v
+-----------------------------+
|  Jira Context Enricher      |
|  bot/jira_context.py        |
|                             |
|  - query_epics()            |
|  - query_open_stories()     |
|  - match_tickets_to_epics() |
|    (LLM call)               |
+-----------------------------+
     |
     | enriched drafts
     v
+-----------------------------+
|  Adaptive Card Builder      |
|  bot/card_builder.py        |
|                             |
|  Builds review table card   |
|  with edit/delete/approve   |
+-----------------------------+
     |
     | sends card to Teams channel/chat
     v
+-----------------------------+
|  Teams Adaptive Card UI     |
|  (user sees ticket table)   |
|                             |
|  [Edit] [Delete] [Approve]  |
|  [Reassign] [Change Project]|
+-----------------------------+
     |
     | user clicks Approve
     v
+-----------------------------+
|  Ticket Submitter           |
|  bot/ticket_submitter.py    |
|                             |
|  calls jira_tool.create_    |
|  ticket() for each approved |
+-----------------------------+
     |
     v
+-----------------------------+
|  Jira REST API v3           |
|  dcri.atlassian.net/ST      |
+-----------------------------+
     |
     | returns ticket URLs
     v
+-----------------------------+
|  Confirmation Card          |
|  (links to created tickets) |
+-----------------------------+
```

---

## 3. Phase 1: FastAPI Transcript Endpoint

### 3.1 Goals

Build the minimal backend that can be tested end-to-end with curl before touching Teams at all. This endpoint is also the stable API contract that the Phase 2 bot will call — the bot is just a thin Teams adapter on top of this HTTP service.

### 3.2 File Structure to Create

All new files live under a `bot/` directory at the project root. Existing files under `agents/`, `clients/`, `config/`, `orchestration/`, and `tools/` are NOT modified.

```
portable-agent-a2a-pipeline/
  bot/                            <- new directory, all Teams/FastAPI code
    __init__.py
    api/
      __init__.py
      main.py                     <- FastAPI app, mounts all routes
      routes/
        __init__.py
        transcript.py             <- POST /api/v1/process-transcript
        health.py                 <- GET /health (liveness probe)
    adapters/
      __init__.py
      transcript_adapter.py       <- converts transcript text -> email-shaped dicts
    teams/
      __init__.py
      teams_handler.py            <- Bot Framework activity handler
      card_builder.py             <- Adaptive Card JSON builder
      ticket_submitter.py         <- calls jira_tool after user approves
    jira_context.py               <- queries Jira for epics/stories, LLM matching
    session_store.py              <- in-memory session state for Phase 2
  requirements-bot.txt            <- additional dependencies for bot
  Dockerfile.bot                  <- container image for Azure Container Apps
```

### 3.3 Transcript Adapter: The Key Integration Point

The existing pipeline expects input shaped like email dicts. The adapter at `bot/adapters/transcript_adapter.py` converts a transcript string into that format WITHOUT modifying any agent code.

The adapter splits the transcript into logical segments (speaker turns or time-boxed chunks) and produces a list of dicts that match what `agents/agent1_email.py` receives from `tools/outlook_tool.py`.

**File:** `bot/adapters/transcript_adapter.py`

```python
"""
Converts a meeting transcript (plain text) into the same dict format
that agents/agent1_email.py produces after LLM extraction.

By producing agent1-shaped output we can feed this directly into
agent2_router.run() and agent3_jira.run(), bypassing agent1 entirely
for the transcript use case.

Alternatively, we can feed the raw transcript into a modified agent1
prompt — see transcript_pipeline.py for that approach.
"""

import re
import textwrap
from typing import Optional


def parse_transcript_segments(transcript: str, max_segment_chars: int = 3000) -> list[dict]:
    """
    Splits a raw transcript into segments suitable for LLM processing.

    Strategy:
      1. Try to split on speaker labels ("Speaker:", "Name:", timestamps like "00:05:")
      2. If no speaker markers, split on paragraph breaks
      3. If still too large, hard-split at max_segment_chars

    Returns list of dicts: [{"id": "seg_0", "body": "...", "speaker": "..."}]
    """
    ...

def transcript_to_pipeline_input(
    transcript: str,
    meeting_title: str = "Teams Meeting",
    project_key: str = "ST",
) -> list[dict]:
    """
    Converts a full transcript into a list of pseudo-email dicts that
    the existing pipeline (agent2_router, agent3_jira) can process.

    Each segment becomes one "email" dict with:
      id:      "transcript_seg_N"
      sender:  speaker name if detectable, else "Meeting Participant"
      subject: f"[{meeting_title}] Action Item Segment {N}"
      body:    the transcript segment text

    The pipeline is then called starting at agent2_router (not agent1)
    because agent1's job (read + extract structure) is replaced by the
    LLM prompt in transcript.py route.
    """
    ...
```

### 3.4 New Pipeline Entry Point for Transcripts

Rather than calling `orchestration/pipeline.py` directly (which starts with email reading), a transcript-specific pipeline function is created at `bot/api/routes/transcript.py`.

**File:** `bot/api/routes/transcript.py` — pipeline invocation logic:

```python
async def run_transcript_pipeline(
    transcript: str,
    project_key: str,
    meeting_title: str,
    dry_run: bool,
) -> TranscriptResponse:
    """
    1. Call agent1 with a transcript-specific prompt (not outlook_tool)
    2. Call agent2_router.run(email_extracts)
    3. Call agent3_jira.run(approved_items) — or skip if dry_run=True
    4. Return draft tickets + created ticket URLs
    """
```

The agent1 call is replaced by a direct LLM call using the same `get_client()` factory from `clients/client.py`, with a modified system prompt tuned for meeting transcripts rather than email bodies.

### 3.5 API Contract

#### Endpoint: POST /api/v1/process-transcript

**Request body (JSON):**

```json
{
  "transcript": "string (required) — full meeting transcript, plain text",
  "project_key": "string (optional, default: 'ST') — Jira project key",
  "meeting_title": "string (optional, default: 'Teams Meeting') — for ticket context",
  "dry_run": "boolean (optional, default: false) — if true, return draft tickets without creating in Jira",
  "max_tickets": "integer (optional, default: 10) — cap on tickets generated per run"
}
```

**Request example:**

```json
{
  "transcript": "Alice: We need to fix the login timeout issue before next sprint.\nBob: Agreed. Also, the data export button is broken on Firefox.\nAlice: I'll file both. Bob, can you handle the Firefox one?\nBob: Sure.",
  "project_key": "ST",
  "meeting_title": "Sprint Planning 2026-03-20",
  "dry_run": true
}
```

**Response body (JSON):**

```json
{
  "status": "success | partial_failure | error",
  "meeting_title": "Sprint Planning 2026-03-20",
  "project_key": "ST",
  "provider": "azure",
  "elapsed_seconds": 22.4,
  "segments_processed": 3,
  "tickets_drafted": 2,
  "tickets_created": 0,
  "dry_run": true,
  "draft_tickets": [
    {
      "draft_id": "draft_0",
      "summary": "Fix login timeout issue",
      "description": "**Problem:** ...\n**Impact:** ...\n**Steps to investigate:** ...",
      "priority": "High",
      "routing_reason": "Explicitly mentioned as needing a fix before next sprint.",
      "suggested_assignee": null,
      "suggested_epic_key": null
    },
    {
      "draft_id": "draft_1",
      "summary": "Fix broken data export button on Firefox",
      "description": "**Problem:** ...",
      "priority": "Medium",
      "routing_reason": "Bug report with a named owner (Bob).",
      "suggested_assignee": "bob@duke.edu",
      "suggested_epic_key": null
    }
  ],
  "created_tickets": []
}
```

**When dry_run is false**, `created_tickets` is populated:

```json
"created_tickets": [
  {
    "ticket_id": "ST-142",
    "url": "https://dcri.atlassian.net/browse/ST-142",
    "summary": "Fix login timeout issue",
    "priority": "High",
    "status": "created"
  }
]
```

**Error response (HTTP 422 / 500):**

```json
{
  "status": "error",
  "error_code": "PIPELINE_FAILURE | JIRA_API_ERROR | LLM_TIMEOUT | INVALID_PROJECT_KEY",
  "message": "Human-readable error description",
  "elapsed_seconds": 5.1
}
```

#### Endpoint: GET /health

```json
{
  "status": "ok",
  "provider": "azure",
  "jira_reachable": true,
  "llm_reachable": true,
  "version": "1.0.0"
}
```

The health check makes a lightweight call to both the Jira API (`GET /rest/api/3/myself`) and the Azure OpenAI endpoint (a single-token completion) to confirm connectivity. Returns HTTP 200 if both reachable, HTTP 503 otherwise.

### 3.6 FastAPI App Setup

**File:** `bot/api/main.py`

```python
from fastapi import FastAPI
from bot.api.routes.transcript import router as transcript_router
from bot.api.routes.health import router as health_router

app = FastAPI(
    title="SageJiraBot API",
    description="Transcript-to-Jira pipeline endpoint",
    version="1.0.0",
)

app.include_router(transcript_router, prefix="/api/v1")
app.include_router(health_router)
```

Run locally:

```bash
uvicorn bot.api.main:app --reload --port 8000
```

### 3.7 How to Run and Test

**Prerequisites:** The main repo `.venv` must be active and `.env` must contain Jira credentials and `AZURE_OPENAI_ENDPOINT`. See Section 8 for the full `.env` additions.

**Install additional dependencies:**

```bash
pip install -r requirements-bot.txt
```

**Start the server:**

```bash
source .venv/bin/activate
uvicorn bot.api.main:app --reload --port 8000
```

**Test with curl (dry run, no ticket created):**

```bash
curl -X POST http://localhost:8000/api/v1/process-transcript \
  -H "Content-Type: application/json" \
  -d '{
    "transcript": "Alice: We need to migrate the database before the go-live date.\nBob: Can you open a ticket? It needs at least 3 days.\nAlice: Yes, I will mark it High priority.",
    "project_key": "ST",
    "meeting_title": "Go-Live Planning",
    "dry_run": true
  }'
```

**Test with curl (actually create tickets):**

```bash
curl -X POST http://localhost:8000/api/v1/process-transcript \
  -H "Content-Type: application/json" \
  -d '{
    "transcript": "...",
    "project_key": "ST",
    "dry_run": false
  }'
```

**Health check:**

```bash
curl http://localhost:8000/health
```

**Expected round-trip time:** ~20-25 seconds (consistent with existing pipeline timing: gpt-5.2 at ai-foundry-dcri-sage takes ~7 seconds per LLM call x 3 agents).

---

## 4. Phase 2: Full Real-Time Teams Bot

### 4.1 Components Needed

| Component | Purpose | Where it lives |
|---|---|---|
| Azure Bot Service | Registers the bot identity (App ID + password), routes Teams messages | Azure Portal (IT to provision) |
| Bot Framework SDK (Python) | Handles activity routing, sends/receives messages | `requirements-bot.txt` |
| Teams App Manifest | Declares bot capabilities, permissions, installs into Teams | `bot/teams/manifest/` |
| FastAPI (Phase 1) | The pipeline backend — bot calls it rather than running pipeline inline | Already built in Phase 1 |
| Azure Container Apps | Hosts both FastAPI and the bot in production | Separate from main pipeline |
| Session Store | Holds draft tickets between pipeline run and user approval | `bot/session_store.py` (in-memory for dev, Azure Table Storage for prod) |

### 4.2 Bot Framework SDK Python Packages

Add to `requirements-bot.txt`:

```
fastapi>=0.110.0
uvicorn>=0.29.0
botbuilder-core>=4.15.0
botbuilder-integration-aiohttp>=4.15.0
botbuilder-schema>=4.15.0
aiohttp>=3.9.0
```

`botframework-integration-aiohttp` is the correct package name for the aiohttp adapter used with FastAPI/asyncio. `botbuilder-core` handles activity parsing and the ActivityHandler base class.

### 4.3 Real-Time Meeting Captions Flow

Teams provides two ways to get transcript content. Use whichever is available:

#### Option A: Post-Meeting Transcript (Recommended for Phase 2 MVP)

1. Meeting ends. Teams auto-generates a transcript if transcription was enabled.
2. Graph API: `GET /v1.0/users/{user-id}/onlineMeetings/{meetingId}/transcripts` returns the transcript ID.
3. Graph API: `GET /v1.0/users/{user-id}/onlineMeetings/{meetingId}/transcripts/{transcriptId}/content?$format=text/vtt` returns the VTT file.
4. Bot parses VTT, strips timestamps, posts text to `/api/v1/process-transcript`.

Required Graph permission: `OnlineMeetings.Read.All` (application permission, admin consent required).

#### Option B: Live Captions via Bot in Meeting (Advanced, Phase 2.x)

1. Bot joins the meeting as a participant.
2. Uses the Teams Real-Time Media SDK (C# only as of 2026-03; Python wrapper does not exist).
3. This path requires a C# sidecar service or a different approach (recording bot).

**Recommendation:** Use Option A (post-meeting transcript via Graph API) for Phase 2. Option B requires a C# media bot which is a significant additional engineering effort and has no Python SDK.

#### Transcript Fetch Flow (Option A)

```
Meeting ends
    |
    | Teams fires a Change Notification webhook
    | (subscription registered on /communications/onlineMeetings)
    v
bot/teams/teams_handler.py: onTranscriptReady()
    |
    | GET https://graph.microsoft.com/v1.0/...onlineMeetings/{id}/transcripts
    | Authorization: Bearer {managed_identity_token for https://graph.microsoft.com}
    v
Parse VTT -> plain text transcript
    |
    | POST http://localhost:8000/api/v1/process-transcript
    | { "transcript": "...", "project_key": "ST", "dry_run": true }
    v
draft_tickets[] returned
    |
    | run Jira context enrichment (bot/jira_context.py)
    v
Send Adaptive Card to Teams channel
```

### 4.4 Adaptive Card Design

The review card is sent to the Teams channel where the bot was invoked (or to the meeting chat). It shows a table of draft tickets with action buttons per row, plus a global "Approve All" button.

#### ASCII Mockup

```
+-----------------------------------------------------------------------+
|  SageJiraBot  |  Sprint Planning 2026-03-20  |  Project: ST          |
|  Found 3 action items. Review before creating tickets:               |
+-----------------------------------------------------------------------+
|  #  | Summary                          | Priority | Epic    | Assign  |
|-----|----------------------------------|----------|---------|---------|
|  1  | Fix login timeout issue          | High     | AUTH-12 | (none)  |
|     | [Edit] [Delete] [Change Epic]    |          |         |         |
|-----|----------------------------------|----------|---------|---------|
|  2  | Fix export button on Firefox     | Medium   | (none)  | bob@... |
|     | [Edit] [Delete] [Change Epic]    |          |         |         |
|-----|----------------------------------|----------|---------|---------|
|  3  | Update DB migration runbook      | Low      | OPS-7   | (none)  |
|     | [Edit] [Delete] [Change Epic]    |          |         |         |
+-----------------------------------------------------------------------+
|  [ Approve All (3) ]                           [ Cancel ]            |
+-----------------------------------------------------------------------+
|  Routing reasons available: hover or expand each row                 |
+-----------------------------------------------------------------------+
```

#### Adaptive Card JSON Structure

The card is built in `bot/card_builder.py`. Key elements:

```json
{
  "type": "AdaptiveCard",
  "version": "1.5",
  "body": [
    {
      "type": "TextBlock",
      "text": "SageJiraBot — Sprint Planning 2026-03-20",
      "weight": "Bolder",
      "size": "Medium"
    },
    {
      "type": "TextBlock",
      "text": "Found 3 action items. Review before creating tickets in **ST**:",
      "wrap": true
    },
    {
      "type": "ColumnSet",
      "columns": [
        {"type": "Column", "width": "auto", "items": [{"type": "TextBlock", "text": "#"}]},
        {"type": "Column", "width": "stretch", "items": [{"type": "TextBlock", "text": "Summary"}]},
        {"type": "Column", "width": "auto", "items": [{"type": "TextBlock", "text": "Priority"}]},
        {"type": "Column", "width": "auto", "items": [{"type": "TextBlock", "text": "Epic"}]}
      ]
    },
    // ... one ColumnSet per draft ticket, with Input.Text for inline editing
  ],
  "actions": [
    {
      "type": "Action.Submit",
      "title": "Approve All (3)",
      "data": {"action": "approve_all", "session_id": "uuid-here"}
    },
    {
      "type": "Action.Submit",
      "title": "Cancel",
      "data": {"action": "cancel", "session_id": "uuid-here"}
    }
  ]
}
```

Per-ticket actions use `Action.Submit` with `data.action` set to `"edit"`, `"delete"`, or `"change_epic"` plus a `data.draft_id` field. The bot receives the submit payload and acts on it.

Teams Adaptive Cards 1.5 supports `Input.Text`, `Input.ChoiceSet` (dropdown), and `Input.Toggle` natively in cards without requiring a dialog. Use `Input.Text` for inline summary editing within the card itself.

### 4.5 Meeting Session State Machine

Each meeting interaction is a session. State transitions:

```
[IDLE]
    | bot added to channel or meeting
    v
[AWAITING_TRANSCRIPT]
    | transcript received (webhook or user pastes text)
    v
[PROCESSING]
    | pipeline + context enrichment running (~25 seconds)
    v
[REVIEW_PENDING]
    | Adaptive Card sent to channel
    | user can edit/delete rows
    v
[PARTIAL_EDIT] (optional)
    | user edits a row -> card refreshes with updated data
    v
[APPROVED]
    | user clicks "Approve All" or selects rows to approve
    v
[CREATING]
    | jira_tool.create_ticket() called for each approved ticket
    v
[COMPLETE]
    | confirmation card sent with ticket URLs
    v
[IDLE]
```

**Timeout:** Sessions expire after 30 minutes in `REVIEW_PENDING` state. Bot sends a message: "Session timed out. Run `/process` again to re-analyze the transcript."

**Session data** stored in `bot/session_store.py`:

```python
@dataclass
class BotSession:
    session_id: str          # uuid
    meeting_title: str
    project_key: str
    transcript: str
    draft_tickets: list[dict]
    state: str               # IDLE | PROCESSING | REVIEW_PENDING | APPROVED | COMPLETE
    created_at: datetime
    channel_id: str          # Teams channel to post updates to
    user_id: str             # Teams user who triggered the session
```

In-memory dict keyed by `session_id` for local dev. Phase 2 production replaces with Azure Table Storage (same interface, just change the backend in `session_store.py`).

### 4.6 Smart Jira Context

Before the Adaptive Card is sent, `bot/jira_context.py` queries the Jira project for existing epics and open stories, then uses the LLM to match each draft ticket to the most relevant epic.

**File:** `bot/jira_context.py`

```python
def query_epics(project_key: str) -> list[dict]:
    """
    GET /rest/api/3/search?jql=project={project_key} AND issuetype=Epic AND statusCategory != Done
    Returns list of {key, summary, description_excerpt}
    """

def query_recent_stories(project_key: str, max_results: int = 50) -> list[dict]:
    """
    GET /rest/api/3/search?jql=project={project_key} AND issuetype in (Story, Task)
        AND created >= -30d ORDER BY created DESC
    Returns list of {key, summary, status}
    """

def match_tickets_to_epics(
    draft_tickets: list[dict],
    epics: list[dict],
    recent_stories: list[dict],
) -> list[dict]:
    """
    LLM call (using get_client() from clients/client.py) that:
    1. Receives the draft ticket list + epic list + recent stories
    2. Returns the same draft_tickets list with two new fields added:
       - suggested_epic_key: "EPIC-12" or null
       - suggested_assignee: email or null (inferred from recent story assignees)
       - is_duplicate_risk: true if a very similar story exists in recent_stories
       - similar_existing_key: "ST-99" if is_duplicate_risk is true
    """
```

LLM prompt for epic matching (system):

```
You are a Jira context assistant. You receive a list of draft tickets and
a list of existing Jira epics and recent stories from the same project.

For each draft ticket:
- If there is a clearly relevant epic (same feature area or system), set suggested_epic_key
- If a recent story is nearly identical in scope (potential duplicate), set is_duplicate_risk=true
- If recent stories show a clear assignee pattern for this type of work, set suggested_assignee

Return the draft_tickets list as JSON with the new fields added.
No explanation, just JSON.
```

### 4.7 Permission Model: Jira Write Access Verification

Before creating tickets, the bot verifies the requesting Teams user has permission to create issues in the target Jira project.

**Approach:**

1. The bot maintains a config map: `CHANNEL_JIRA_PERMISSIONS` — a dict from Teams `channel_id` to allowed Jira usernames/email list. This is stored in `bot/teams/config.py` (or loaded from a config file, not hardcoded with secrets).

2. When a user triggers ticket creation, the bot checks:
   - Is the `teams_user_email` (from the Teams activity) in the allowed list for this channel's project?
   - OR: call Jira API `GET /rest/api/3/user/permission/search?projectKey=ST&permissions=CREATE_ISSUES&accountId={jira_account_id}` with the bot's service account credentials to verify dynamically.

3. If not authorized: bot replies "You don't have Jira CREATE_ISSUES permission for project ST. Contact your Jira admin."

**Recommended approach for Phase 2 MVP:** Use the dynamic Jira API check (option 2b), which does not require maintaining a separate permission list. The bot's Jira API token must have Browse permission on the project.

### 4.8 Project Routing: How Users Specify Jira Project

#### Default per channel

A channel-to-project mapping is configured at bot install time in `bot/teams/config.py`:

```python
CHANNEL_PROJECT_DEFAULTS = {
    "19:abc123@thread.tacv2": "ST",   # #sage-dev channel
    "19:def456@thread.tacv2": "OPS",  # #operations channel
    # add new channels here when bot is installed to a new channel
}

DEFAULT_PROJECT = "ST"  # fallback if channel not in map
```

#### Override with inline command

Users can override with `project:KEY` anywhere in their message:

```
@SageJiraBot process transcript project:OPS
```

Or when pasting a transcript:

```
@SageJiraBot [paste transcript here] project:OPS
```

The bot parses `project:([A-Z]{1,10})` from the message body using regex before passing the project key to the pipeline.

#### Bot Commands

| Command | Action |
|---|---|
| `@SageJiraBot help` | Show command list |
| `@SageJiraBot process` | Process the most recent meeting transcript (fetched from Graph API) |
| `@SageJiraBot process project:OPS` | Same, but route to OPS project |
| `@SageJiraBot paste` | Bot prompts user to paste a transcript in the next message |
| `@SageJiraBot status` | Show current session state if one is running |
| `@SageJiraBot cancel` | Abort current session, discard drafts |

### 4.9 Teams App Manifest

**File:** `bot/teams/manifest/manifest.json`

Key sections (fill in Bot App ID from Azure Bot Service after IT provisions it):

```json
{
  "$schema": "https://developer.microsoft.com/en-us/json-schemas/teams/v1.16/MicrosoftTeams.schema.json",
  "manifestVersion": "1.16",
  "version": "1.0.0",
  "id": "{{BOT_APP_ID}}",
  "name": {
    "short": "SageJiraBot",
    "full": "SageJiraBot - Meeting to Jira Tickets"
  },
  "developer": {
    "name": "DCRI SAGE Team",
    "websiteUrl": "https://dcri.duke.edu",
    "privacyUrl": "https://dcri.duke.edu/privacy",
    "termsOfUseUrl": "https://dcri.duke.edu/terms"
  },
  "bots": [
    {
      "botId": "{{BOT_APP_ID}}",
      "scopes": ["team", "groupChat", "personal"],
      "supportsFiles": false,
      "isNotificationOnly": false,
      "commandLists": [
        {
          "scopes": ["team", "groupChat"],
          "commands": [
            {"title": "process", "description": "Process meeting transcript into Jira tickets"},
            {"title": "paste", "description": "Paste a transcript for processing"},
            {"title": "status", "description": "Show current session status"},
            {"title": "cancel", "description": "Cancel the current session"},
            {"title": "help", "description": "Show available commands"}
          ]
        }
      ]
    }
  ],
  "permissions": ["identity", "messageTeamMembers"],
  "validDomains": ["{{BOT_DOMAIN}}"]
}
```

The manifest is zipped with icon files and uploaded to Teams Admin Center by IT.

### 4.10 Activity Handler

**File:** `bot/teams/teams_handler.py`

```python
from botbuilder.core import ActivityHandler, TurnContext, MessageFactory
from botbuilder.schema import ChannelAccount
import httpx
import re

class SageJiraBotHandler(ActivityHandler):

    async def on_message_activity(self, turn_context: TurnContext):
        text = turn_context.activity.text.strip()

        # Parse project key override
        project_match = re.search(r'project:([A-Z]{1,10})', text, re.IGNORECASE)
        project_key = project_match.group(1).upper() if project_match else self._get_default_project(turn_context)

        if 'process' in text.lower():
            await self._handle_process_command(turn_context, project_key)
        elif 'paste' in text.lower():
            await self._handle_paste_prompt(turn_context)
        elif 'status' in text.lower():
            await self._handle_status(turn_context)
        elif 'cancel' in text.lower():
            await self._handle_cancel(turn_context)
        else:
            # Check if this is a transcript paste (long message after a 'paste' prompt)
            session = session_store.get_active_paste_session(turn_context.activity.from_property.id)
            if session and session.state == 'AWAITING_PASTE':
                await self._run_pipeline_on_text(turn_context, text, project_key)
            else:
                await turn_context.send_activity(MessageFactory.text(
                    "Hi! I'm SageJiraBot. Use `process` to analyze your latest meeting transcript, "
                    "or `paste` to paste a transcript. Type `help` for all commands."
                ))

    async def on_reactions_added(self, message_reactions, turn_context: TurnContext):
        # Future: thumbs up on a draft ticket row auto-approves that ticket
        pass
```

---

## 5. Cross-Platform Support

### Problem

Teams Transcription API only works for Teams meetings. Zoom and Webex meetings produce their own transcript formats but cannot be fetched via Microsoft Graph.

### Solution: Universal "Paste" Flow

Since Phase 1 delivers a plain-text HTTP API, any transcript format from any platform can be fed through it. The Teams bot exposes this as a "paste" command.

#### Workflow for Zoom/Webex Users

1. After a Zoom/Webex meeting, export the transcript from the meeting platform:
   - **Zoom:** Host console -> Recording -> Transcript -> Download `.vtt`
   - **Webex:** Post-meeting email contains a transcript link; export as `.txt`
2. Open the Teams channel where SageJiraBot is installed.
3. Type `@SageJiraBot paste`
4. Bot replies: "Paste your transcript in the next message. Include the meeting title on the first line."
5. User pastes the transcript text (Teams supports very long messages).
6. Bot processes it identically to a Teams transcript.

#### VTT Parsing

Zoom and Teams both produce WebVTT (`.vtt`) format. A shared parser in `bot/adapters/transcript_adapter.py` handles both:

```python
def parse_vtt(vtt_text: str) -> str:
    """
    Strip WebVTT header, timestamps, and WEBVTT markers.
    Returns clean plain text with speaker labels preserved.

    Input:
        WEBVTT

        00:00:05.000 --> 00:00:10.000
        Alice: We need to fix the login issue.

    Output:
        Alice: We need to fix the login issue.
    """
```

#### Webex `.txt` Format

Webex transcript exports use a different format:
```
0:00  Alice Smith
      We need to fix the login issue.

0:05  Bob Jones
      Agreed.
```

A separate `parse_webex_txt()` function handles this in `transcript_adapter.py`.

The bot auto-detects format by checking for `WEBVTT` header vs the Webex timestamp pattern.

### Direct API Usage (No Teams Required)

Teams is not required to use the pipeline. The FastAPI endpoint works with any HTTP client:

```bash
# From a Zoom meeting export
cat transcript.vtt | python bot/scripts/strip_vtt.py | \
  curl -X POST http://localhost:8000/api/v1/process-transcript \
    -H "Content-Type: application/json" \
    -d @-
```

This makes the system accessible to anyone who can run a curl command, regardless of platform.

---

## 6. IT Dependencies

The following items require IT involvement before Phase 2 can be deployed. Phase 1 (FastAPI endpoint) requires none of these — it runs entirely on the existing Unix VM with existing credentials.

### 6.1 Items to Request from IT

| Item | What to Request | Why Needed | Blocking Phase |
|---|---|---|---|
| Azure Bot Service resource | Create a Bot Service resource in the DCRI subscription (2c69c8ba-1dc1-444a-9a18-a483b0be57db) | Provides the bot's App ID and messaging endpoint registration | Phase 2 |
| Teams App Approval | Submit SageJiraBot manifest to Teams Admin Center for approval | Required before bot can be installed in any Teams channel | Phase 2 |
| Graph API: Mail.Read | Application permission consent for the bot's managed identity | Already needed for Outlook email reading (existing pipeline Phase 2) | Phase 2 |
| Graph API: OnlineMeetings.Read.All | Application permission consent | Required to fetch meeting metadata and transcript IDs | Phase 2 |
| Graph API: OnlineMeetingTranscript.Read.All | Application permission consent | Required to download the actual transcript content | Phase 2 |
| Graph API: ChannelMessage.Send | Application permission consent | Required for bot to post Adaptive Cards to channels | Phase 2 |
| Azure Container Apps environment | Create a Container Apps Environment in the DCRI resource group | Hosts the FastAPI + bot service in production | Phase 2 |
| Managed Identity for bot container | Assign system-assigned managed identity to the bot Container App | Allows bot to call AI Foundry and Graph without secrets | Phase 2 |
| AI Foundry role assignment | Grant the bot's managed identity "Cognitive Services OpenAI User" role on ai-foundry-dcri-sage | Allows LLM calls from production container | Phase 2 |

### 6.2 Exact Graph Permissions List for IT Request

Submit this list when requesting admin consent:

```
Microsoft Graph — Application Permissions (require admin consent):
  - Mail.Read                          (read mailbox for email pipeline)
  - OnlineMeetings.Read.All            (read meeting metadata + transcript IDs)
  - OnlineMeetingTranscript.Read.All   (read transcript content)
  - ChannelMessage.Send                (post bot messages to channels)
  - TeamsActivity.Send                 (send activity notifications)

Microsoft Graph — Delegated Permissions (require user consent):
  - (none required — bot uses application permissions only)
```

### 6.3 Timeline Dependencies

```
IT provisions Azure Bot Service
    -> developer gets Bot App ID + Password
    -> developer completes bot/teams/teams_handler.py
    -> developer submits manifest to IT

IT approves Teams App
    -> bot can be installed in channels
    -> end-to-end Phase 2 testing begins

IT grants Graph permissions
    -> transcript fetch from real meetings works
    -> live caption flow (if pursued) becomes possible
```

---

## 7. Implementation Order

A fresh agent (or developer) should follow these steps in order. Each step is independently testable before the next begins.

### Step 1: Create bot/ directory structure

Create all `__init__.py` files and empty module stubs. Run `python -c "import bot.api.main"` to confirm imports work.

Files to create (empty stubs):
- `bot/__init__.py`
- `bot/api/__init__.py`
- `bot/api/main.py`
- `bot/api/routes/__init__.py`
- `bot/api/routes/transcript.py`
- `bot/api/routes/health.py`
- `bot/adapters/__init__.py`
- `bot/adapters/transcript_adapter.py`
- `bot/session_store.py`
- `bot/jira_context.py`
- `bot/teams/__init__.py`
- `bot/teams/teams_handler.py`
- `bot/teams/card_builder.py`
- `bot/teams/ticket_submitter.py`
- `bot/teams/config.py`
- `bot/teams/manifest/manifest.json`

### Step 2: Implement transcript_adapter.py

Implement `parse_vtt()`, `parse_webex_txt()`, and `transcript_to_pipeline_input()`. These are pure Python string functions with no external dependencies. Test with sample transcript text before touching the pipeline.

Test:
```python
from bot.adapters.transcript_adapter import transcript_to_pipeline_input
result = transcript_to_pipeline_input("Alice: Fix login.\nBob: I'll do it.", "Test Meeting")
assert result[0]["body"] is not None
```

### Step 3: Implement the transcript pipeline function

In `bot/api/routes/transcript.py`, implement `run_transcript_pipeline()`. This function:
1. Calls `get_client()` from `clients/client.py` (existing code)
2. Runs a modified agent1 prompt on the transcript text
3. Calls `agent2_router.run()` (existing code, unchanged)
4. Calls `agent3_jira.run()` with `dry_run` protection (skip `create_ticket()` if `dry_run=True`)

Test by calling the function directly in a Python shell with `dry_run=True`.

**Note on agent3 dry_run:** Agent 3 currently calls `create_ticket()` unconditionally. Add a `dry_run` parameter to `agent3_jira.run()` OR handle this at the route level by inspecting the draft output before calling agent3. The cleaner approach is to add `dry_run: bool = False` to `agents/agent3_jira.run()` and skip the `create_ticket()` call when True. This is the one modification to existing agent code.

### Step 4: Implement FastAPI routes and app

Implement `bot/api/routes/transcript.py` (request parsing, calling pipeline function, returning response) and `bot/api/routes/health.py`. Wire them into `bot/api/main.py`.

Add `requirements-bot.txt` with `fastapi` and `uvicorn`.

Test:
```bash
uvicorn bot.api.main:app --reload --port 8000
curl http://localhost:8000/health
curl -X POST http://localhost:8000/api/v1/process-transcript \
  -H "Content-Type: application/json" \
  -d '{"transcript": "Alice: Fix the login bug.", "dry_run": true}'
```

At this point Phase 1 is complete and fully testable.

### Step 5: Implement jira_context.py

Implement `query_epics()`, `query_recent_stories()`, and `match_tickets_to_epics()`. The Jira queries use the same `HTTPBasicAuth` pattern already in `tools/jira_tool.py`. The LLM matching uses `get_client()` exactly like the existing agents.

Test by calling `query_epics("ST")` and checking the response shape, then passing mock draft tickets to `match_tickets_to_epics()`.

### Step 6: Implement session_store.py

Implement the in-memory session store with `create_session()`, `get_session()`, `update_session()`, `expire_old_sessions()`. Keep it as a plain dict — no database yet. Add a background task in `main.py` that calls `expire_old_sessions()` every 5 minutes.

### Step 7: Implement card_builder.py

Implement `build_review_card(session: BotSession) -> dict` that returns the Adaptive Card JSON dict. Start with a simplified card (just a list of ticket summaries with Approve All / Cancel buttons). Add inline editing in a second iteration.

Test by rendering the JSON and pasting it into https://adaptivecards.io/designer/ to preview it visually.

### Step 8: Set up Bot Framework integration

Install `botbuilder-core` and `botbuilder-integration-aiohttp`. Implement `bot/teams/teams_handler.py` with the `SageJiraBotHandler` class. Add the Bot Framework adapter to `bot/api/main.py`:

```python
from botbuilder.integration.aiohttp import CloudAdapter, ConfigurationBotFrameworkAuthentication
from bot.teams.teams_handler import SageJiraBotHandler

BOT_CONFIG = {
    "MicrosoftAppId": os.environ["BOT_APP_ID"],
    "MicrosoftAppPassword": os.environ["BOT_APP_PASSWORD"],
}
adapter = CloudAdapter(ConfigurationBotFrameworkAuthentication(BOT_CONFIG))
bot = SageJiraBotHandler()

@app.post("/api/messages")
async def messages(request: Request):
    return await adapter.process(request, bot)
```

This adds the `/api/messages` endpoint that Azure Bot Service will route Teams messages to.

### Step 9: Request IT dependencies (parallel to Steps 7-8)

While implementing the bot handler, submit the IT request for Azure Bot Service and Graph permissions (Section 6). These can take days to weeks to provision.

### Step 10: Create Teams App Manifest

Fill in `manifest.json` with the Bot App ID once IT provides it. Zip the manifest with two icon files (192x192 and 32x32 PNG, DCRI branding). Submit to IT for Teams Admin Center upload.

### Step 11: End-to-end testing

Once IT has provisioned everything:
1. Deploy the bot to Azure Container Apps (using existing `az containerapp` patterns from the managed identity guide)
2. Install the bot to a test Teams channel
3. Run a test meeting with transcription enabled
4. After the meeting, type `@SageJiraBot process` in the channel
5. Verify the Adaptive Card appears with correct ticket drafts
6. Click Approve All, verify tickets appear in `https://dcri.atlassian.net/browse/ST`

### Step 12: Implement ticket_submitter.py

Implement `bot/teams/ticket_submitter.py` which handles the Adaptive Card submit payload (`action = "approve_all"` or `action = "approve_selected"`), retrieves the session, and calls `tools/jira_tool.create_ticket()` for each approved ticket. Send a confirmation card with ticket URLs.

---

## 8. Environment Variables

### Existing .env variables (already present)

```bash
AZURE_OPENAI_ENDPOINT=https://ai-foundry-dcri-sage.openai.azure.com/
AZURE_OPENAI_API_VERSION=2025-01-01-preview
JIRA_BASE_URL=https://dcri.atlassian.net
JIRA_EMAIL=scb2@duke.edu
JIRA_API_TOKEN=<your Atlassian API token>
JIRA_PROJECT_KEY=ST
```

### New additions required for the bot

Add these to `.env` (and to `.example.env` without the secret values):

```bash
# ---- Bot Framework (Phase 2, provided by IT after Azure Bot Service is provisioned) ----
BOT_APP_ID=                        # Azure Bot Service App ID (GUID)
BOT_APP_PASSWORD=                  # Azure Bot Service App Password (client secret)
                                   # NOTE: in production, use managed identity + federated
                                   # credential instead of a password — ask IT to configure

# ---- Microsoft Graph API (Phase 2, for transcript fetching) ----
GRAPH_TENANT_ID=cb72c54e-4a31-4d9e-b14a-1ea36dfac94c
# For local dev with az login, Graph token is fetched via AzureCliCredential.
# In production, the bot container's managed identity fetches Graph tokens.
# No Graph client secret needed if managed identity has the permissions granted.

# ---- FastAPI Bot Server ----
BOT_HOST=0.0.0.0                   # bind address
BOT_PORT=8000                      # port
BOT_LOG_LEVEL=info                 # uvicorn log level

# ---- Session store (Phase 2 production) ----
SESSION_BACKEND=memory             # "memory" for dev, "azure_table" for production
AZURE_STORAGE_ACCOUNT_NAME=        # only needed when SESSION_BACKEND=azure_table
AZURE_STORAGE_TABLE_NAME=sagebotSessions

# ---- Bot behavior tuning ----
BOT_DEFAULT_PROJECT=ST             # fallback Jira project if channel not configured
BOT_MAX_TICKETS_PER_RUN=10        # safety cap
BOT_SESSION_TIMEOUT_MINUTES=30    # how long REVIEW_PENDING sessions live
BOT_DRY_RUN_DEFAULT=false         # set true during testing to avoid creating real tickets
```

### .example.env additions (no secret values)

```bash
BOT_APP_ID=                        # from IT after Azure Bot Service provisioned
BOT_APP_PASSWORD=                  # from IT (or use managed identity — see docs)
GRAPH_TENANT_ID=cb72c54e-4a31-4d9e-b14a-1ea36dfac94c
SESSION_BACKEND=memory
BOT_DEFAULT_PROJECT=ST
BOT_MAX_TICKETS_PER_RUN=10
BOT_SESSION_TIMEOUT_MINUTES=30
BOT_DRY_RUN_DEFAULT=false
```

---

## 9. Testing Plan

### 9.1 Phase 1 Tests (No Teams, No Real Meeting Required)

#### Unit Test: Transcript Adapter

```python
# tests/test_transcript_adapter.py
from bot.adapters.transcript_adapter import parse_vtt, parse_webex_txt, transcript_to_pipeline_input

SAMPLE_VTT = """WEBVTT

00:00:05.000 --> 00:00:10.000
Alice: We need to fix the login timeout before next sprint.

00:00:11.000 --> 00:00:15.000
Bob: I can take that. Also the Firefox export bug.
"""

def test_parse_vtt_strips_timestamps():
    result = parse_vtt(SAMPLE_VTT)
    assert "00:00:05" not in result
    assert "Alice: We need to fix" in result

def test_transcript_to_pipeline_input():
    result = transcript_to_pipeline_input(SAMPLE_VTT, "Sprint Planning")
    assert len(result) >= 1
    assert "body" in result[0]
    assert "id" in result[0]
```

#### Integration Test: FastAPI Endpoint (dry_run=True)

```bash
# Start server
uvicorn bot.api.main:app --port 8000 &

# Test 1: health check
curl -f http://localhost:8000/health

# Test 2: minimal transcript, dry run
curl -X POST http://localhost:8000/api/v1/process-transcript \
  -H "Content-Type: application/json" \
  -d '{
    "transcript": "Alice: We need to migrate the database schema before go-live. High priority.",
    "project_key": "ST",
    "meeting_title": "Go-Live Planning",
    "dry_run": true
  }' | python -m json.tool

# Expected: status=success, tickets_drafted >= 1, tickets_created=0, dry_run=true

# Test 3: multi-speaker transcript
curl -X POST http://localhost:8000/api/v1/process-transcript \
  -H "Content-Type: application/json" \
  -d '{
    "transcript": "Alice: Reminder that the data export button is broken on Firefox.\nBob: Ill fix it this week.\nAlice: Also we need to update the runbook for on-call.",
    "dry_run": true
  }' | python -m json.tool

# Expected: 2 tickets drafted (Firefox bug + runbook update), no social items
```

#### Integration Test: Actual Ticket Creation

**WARNING:** Only run against the ST project during testing. Creates real tickets.

```bash
curl -X POST http://localhost:8000/api/v1/process-transcript \
  -H "Content-Type: application/json" \
  -d '{
    "transcript": "TEST BOT: Please create a test ticket from the SageJiraBot transcript endpoint.",
    "project_key": "ST",
    "meeting_title": "Bot Integration Test",
    "dry_run": false
  }'

# Verify: ticket appears at https://dcri.atlassian.net/browse/ST
# Clean up: manually close/delete the test ticket in Jira
```

#### Test: Error Handling

```bash
# Test invalid project key
curl -X POST http://localhost:8000/api/v1/process-transcript \
  -H "Content-Type: application/json" \
  -d '{"transcript": "test", "project_key": "DOESNOTEXIST", "dry_run": false}'
# Expected: HTTP 422 or 500 with JIRA_API_ERROR error_code

# Test empty transcript
curl -X POST http://localhost:8000/api/v1/process-transcript \
  -H "Content-Type: application/json" \
  -d '{"transcript": "", "dry_run": true}'
# Expected: HTTP 422 with message about empty transcript

# Test transcript with no action items
curl -X POST http://localhost:8000/api/v1/process-transcript \
  -H "Content-Type: application/json" \
  -d '{"transcript": "Alice: Great meeting everyone! Same time next week. Bob: Sounds good!", "dry_run": true}'
# Expected: status=success, tickets_drafted=0
```

### 9.2 Jira Context Tests

```bash
# Test epic query (requires VPN + Jira credentials)
python -c "
from bot.jira_context import query_epics
epics = query_epics('ST')
print(f'Found {len(epics)} epics')
for e in epics:
    print(f'  {e[\"key\"]}: {e[\"summary\"]}')
"

# Test epic matching with mock drafts
python -c "
from bot.jira_context import query_epics, match_tickets_to_epics
epics = query_epics('ST')
drafts = [{'draft_id': 'test_0', 'summary': 'Fix login timeout', 'description': 'Login issues'}]
enriched = match_tickets_to_epics(drafts, epics, [])
print(enriched)
"
```

### 9.3 Adaptive Card Visual Test

Without a real Teams environment, test the card JSON visually:

1. Implement `card_builder.build_review_card()` with mock session data.
2. Print the resulting JSON: `python -c "from bot.teams.card_builder import build_review_card; import json; print(json.dumps(build_review_card(mock_session), indent=2))"`
3. Copy the JSON and paste into https://adaptivecards.io/designer/
4. Verify the card renders correctly with all buttons and columns.

### 9.4 Bot Framework Integration Test (Without Teams)

The Bot Framework SDK provides a test client that simulates Teams message activities without requiring a real Teams connection:

```python
# tests/test_bot_handler.py
import asyncio
from botbuilder.core import TurnContext
from botbuilder.core.adapters import SimpleAdapter
from botbuilder.schema import Activity, ActivityTypes
from bot.teams.teams_handler import SageJiraBotHandler

async def test_help_command():
    adapter = SimpleAdapter()
    handler = SageJiraBotHandler()

    activity = Activity(
        type=ActivityTypes.message,
        text="help",
        from_property=ChannelAccount(id="test_user", name="Test User"),
        channel_id="test_channel",
    )
    turn_context = TurnContext(adapter, activity)
    await handler.on_turn(turn_context)

    responses = adapter.activity_buffer
    assert any("SageJiraBot" in str(r.text) for r in responses)

asyncio.run(test_help_command())
```

### 9.5 Phase 2 End-to-End Test (Requires IT Provisioning)

Once Azure Bot Service and Teams App are provisioned:

1. Install bot to a private test Teams channel.
2. In the channel, type `@SageJiraBot help` — verify the command list appears.
3. Type `@SageJiraBot paste` — verify bot prompts for transcript.
4. Paste a sample transcript — verify the Adaptive Card appears with draft tickets.
5. Click "Delete" on one row — verify the card refreshes without that row.
6. Click "Approve All" — verify tickets appear in Jira.
7. Verify the confirmation card shows correct ticket URLs.
8. Test timeout: leave the card for 31 minutes — verify the bot sends a timeout message.

### 9.6 Performance Baseline

The existing pipeline takes ~20 seconds end-to-end with `gpt-5.2`. Track these metrics for the bot:

| Stage | Expected Time | Measured |
|---|---|---|
| Transcript parsing | < 0.1s | TBD |
| Agent 1 (transcript -> structured items) | ~7s | TBD |
| Agent 2 (router) | ~7s | TBD |
| Agent 3 (ticket writer, dry_run=True) | ~7s | TBD |
| Jira epic query | ~1s | TBD |
| LLM epic matching | ~5s | TBD |
| Card build + send | ~1s | TBD |
| **Total (dry_run=True)** | **~28s** | TBD |
| Jira ticket creation (per ticket) | ~1s | TBD |

If latency exceeds 30 seconds, Teams may show a timeout warning. Mitigation: send an immediate acknowledgment message ("Processing your transcript... this takes about 25 seconds") before starting the pipeline, so the user knows the bot is working.

---

## Appendix A: Key File References

All paths are relative to `/dcri/sasusers/home/scb2/gitRepos/portable-agent-a2a-pipeline/`.

| File | Role |
|---|---|
| `config/settings.py` | PROVIDER, AZURE_AUTH_MODE, model names — do not modify |
| `clients/client.py` | `get_client()` factory — all LLM calls go through this |
| `orchestration/pipeline.py` | `run_pipeline()` — existing email pipeline, reference for pattern |
| `agents/agent1_email.py` | Email reader agent — AGENT_DEFINITION pattern to follow |
| `agents/agent2_router.py` | Router agent — called unchanged by transcript pipeline |
| `agents/agent3_jira.py` | Jira creator — needs `dry_run` parameter added |
| `tools/jira_tool.py` | `create_ticket()`, `_client()` pattern — reuse in `jira_context.py` |
| `tools/outlook_tool.py` | Stub tool — do not modify |
| `docs/managed-identity-guide.md` | Auth reference for production deployment |
| `bot/api/main.py` | FastAPI app — to be created |
| `bot/adapters/transcript_adapter.py` | Transcript parser — to be created |
| `bot/teams/teams_handler.py` | Bot Framework handler — to be created |
| `bot/teams/card_builder.py` | Adaptive Card builder — to be created |
| `bot/jira_context.py` | Jira epic/story query + LLM matching — to be created |
| `bot/session_store.py` | Session state management — to be created |

## Appendix B: Azure Resource Reference

| Resource | Value |
|---|---|
| Tenant ID | `cb72c54e-4a31-4d9e-b14a-1ea36dfac94c` |
| Subscription ID | `2c69c8ba-1dc1-444a-9a18-a483b0be57db` |
| AI Foundry resource | `ai-foundry-dcri-sage` |
| Deployed models | `gpt-5.2` (primary), `gpt-5.3-codex` (available) |
| Jira instance | `https://dcri.atlassian.net` |
| Jira project | `ST` (KanbanBoardAgents board) |
| Auth mode (local) | `az_login` → `AzureCliCredential` |
| Auth mode (prod) | `managed_identity` → `DefaultAzureCredential` |
| VPN requirement | Required for AI Foundry API calls; not required for `az login` |
