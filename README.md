# sageTestAzAgents

Learning project: multi-agent system using Azure AI Foundry with A2A (agent-to-agent)
communication and MCP (Model Context Protocol) tool calls.

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
sageTestAzAgents_01/
│
├── config/
│   ├── __init__.py
│   └── settings.py          # PROVIDER toggle, model names, auth mode, inference settings
│
├── clients/
│   ├── __init__.py
│   └── client.py            # get_client() - returns correct client for active PROVIDER
│                            # _build_azure_client() - handles managed identity vs api key
│
├── agents/
│   ├── agent1_email.py      # (TODO) reads Outlook folder, structures email data
│   ├── agent2_router.py     # (TODO) decides if email content warrants a Jira ticket
│   └── agent3_jira.py       # (TODO) creates Jira tickets via tool/MCP call
│
├── tools/
│   ├── outlook_tool.py      # (TODO) Microsoft Graph MCP / tool for reading email
│   └── jira_tool.py         # (TODO) Jira API MCP / tool for creating tickets
│
├── .env.example             # copy to .env, fill in keys - never commit .env
├── .gitignore
├── requirements.txt
├── setup-commands.md        # az/azd commands runbook - reproducible setup
└── README.md                # this file
```

---

## Setup

### 1. Clone and install dependencies

```bash
git clone <repo-url>
cd sageTestAzAgents_01
python -m venv .venv
source .venv/bin/activate       # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp .env.example .env
# Edit .env - fill in AZURE_OPENAI_ENDPOINT at minimum
# If using api_key auth mode, also fill in AZURE_OPENAI_KEY
# If using openai_* providers, fill in OPENAI_API_KEY
```

### 3. Log in to Azure (for managed identity auth)

See `setup-commands.md` for the full az login runbook.

```bash
az login --tenant "cb72c54e-4a31-4d9e-b14a-1ea36dfac94c"
az account set --subscription "2c69c8ba-1dc1-444a-9a18-a483b0be57db"
az account show  # verify
```

### 4. Set your provider

Edit `config/settings.py`:
```python
PROVIDER = "azure"              # for Duke Health work
PROVIDER = "openai_responses"   # for personal learning / non-PHI
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
