# portable-agent-a2a-pipeline

Multi-agent pipeline: Outlook emails -> LLM triage -> Jira tickets.
Comparing portable (pure Python) agents vs Azure AI Foundry agents.

## Quickstart

```bash
git clone git@github.com:sagearbor/portable-agent-a2a-pipeline.git
cd portable-agent-a2a-pipeline
python -m venv .venv
source .venv/bin/activate           # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .example.env .env                # no edits needed for managed identity

# Azure login (one-time, persists ~90 days)
az login --tenant "cb72c54e-4a31-4d9e-b14a-1ea36dfac94c"
az account set --subscription "2c69c8ba-1dc1-444a-9a18-a483b0be57db"

# Run the pipeline (requires VPN — AI Foundry has a private endpoint)
python -m orchestration.pipeline
```

> **VPN note:** `az login` works without VPN, but API calls to AI Foundry require VPN.
> Primary dev environment is the Unix VM (always on VPN).

---

## Goal: Email-to-Jira Agent Pipeline

Three agents that communicate via A2A:

```
[Outlook Inbox / Folder]
        |
        v
  Agent 1 - Email Reader
  - Reads a user's Outlook email folder (via MCP / Microsoft Graph tool)
  - Extracts relevant content and structures it
  - Passes structured data to Agent 2
        |
        v (A2A)
  Agent 2 - Router
  - Receives structured email data from Agent 1
  - Decides: is this actionable enough to become a Jira ticket?
  - Routes to Agent 3 if yes, discards / logs if no
        |
        v (A2A)
  Agent 3 - Jira Ticket Creator
  - Receives approved items from Agent 2
  - Creates Jira tickets via MCP / Jira API tool
  - Returns ticket ID and confirmation
```

---

## Provider Toggle

All agent code is provider-agnostic. One setting in `config/settings.py` controls
which backend is used. Change `PROVIDER` to switch everything:

| PROVIDER | Endpoint | Protocol | Duke Health safe? | Status |
|---|---|---|---|---|
| `azure` | Azure AI Foundry | Chat Completions | YES | Working |
| `azure_responses` | Azure AI Foundry | Responses API | YES | Not yet - coming soon |
| `openai_responses` | OpenAI.com | Responses API | NO | Working |
| `openai_chat` | OpenAI.com | Chat Completions | NO | Working |

**Rule:** Only use `azure` or `azure_responses` with real Duke Health / PHI-adjacent data.
OpenAI providers send data outside the Duke tenant.

### Why four toggles?

- **Protocol matters:** Responses API is stateful (server holds thread history, only
  new message sent each turn = lower latency and cost). Chat Completions is stateless
  (full history resent every call = more compatible with all models).
- **Provider matters for data governance:** Azure keeps data in Duke tenant.
  OpenAI.com does not.
- **Learning value:** keeping both lets you compare the patterns side by side.

---

## Azure Authentication

For Azure providers, authentication is controlled by `AZURE_AUTH_MODE` in `config/settings.py`:

### `managed_identity` (recommended)
No API key needed. Uses `DefaultAzureCredential` which automatically checks:
1. Your `az login` session (when running locally on your machine)
2. Managed Identity assigned to the Azure resource (when deployed to Azure)
3. Environment variables (CI/CD pipelines)

Same code runs locally and in production without any changes.

### `api_key`
Uses `AZURE_OPENAI_KEY` from `.env`. Simpler for initial testing but keys
can leak and must be rotated manually. Avoid for anything touching real data.

---

## Azure Resource Hierarchy

```
AAD Tenant: cb72c54e-...  (Duke Health - identity/auth boundary for whole org)
  |
  └── Subscription: dhp-dcri-prod-sub  (billing + access boundary, shared with other DCRI resources)
        |
        └── Resource Group: rg-dcri-prod-ai-foundry  (logical folder)
              |
              └── AIServices Resource: ai-foundry-dcri-sage  (the AI service endpoint)
                    endpoint: https://ai-foundry-dcri-sage.cognitiveservices.azure.com/
                    deployed models: gpt-5.2, gpt-5.3-codex
                    |
                    └── AI Foundry Project: (to be created)
                          agents live here, each project gets its own endpoint
```

---

## Project Structure

```
portable-agent-a2a-pipeline/
├── config/settings.py          # PROVIDER toggle, model names, auth mode, inference settings
├── clients/client.py           # get_client() factory - returns (client, model) for active PROVIDER
├── agents/
│   ├── agent1_email.py         # Email Reader: reads emails, LLM extracts structured data
│   ├── agent2_router.py        # Router: filters actionable items, adds routing_reason
│   └── agent3_jira.py          # Jira Creator: writes descriptions, creates tickets
├── tools/
│   ├── outlook_tool.py         # STUB: read_emails() — Phase 2: Microsoft Graph API
│   └── jira_tool.py            # STUB: create_ticket() — Phase 2: Jira REST API
├── orchestration/pipeline.py   # Wires agents together, CLI entry point
├── docs/
│   └── managed-identity-guide.md  # Full auth walkthrough
├── .example.env                # Copy to .env — no edits needed for managed identity
├── setup-commands.md           # Azure CLI runbook
└── CLAUDE.md                   # Context for Claude Code sessions
```

---

## A2A Communication Pattern

Agent-to-agent communication is handled via shared threads.
Agent 1 creates a thread and posts a message. Agent 2 reads the thread,
processes it, and posts its decision. Agent 3 picks up from Agent 2's output.

This is a pull-based handoff pattern — no message broker needed for the simple case.
For production, a queue (Azure Service Bus) would sit between agents for reliability.

---

## MCP Tools (planned)

| Tool | Protocol | What it does |
|---|---|---|
| `outlook_tool` | Microsoft Graph API | Read emails from a specified folder |
| `jira_tool` | Jira REST API | Create tickets in a specified project |

MCP (Model Context Protocol) is how agents call external tools in a standardized way.
The agent declares what tools it has, the LLM decides when to call them,
the framework executes the call and feeds the result back to the agent.
