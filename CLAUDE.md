# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

A multi-agent pipeline that reads Outlook emails, routes actionable ones, and creates Jira tickets. Three agents communicate via A2A (agent-to-agent) handoff using a pull-based pattern (no message broker). Currently in Phase 1 (local Python with stub tools); Phase 2 will deploy as Azure Container Apps calling Azure-hosted agents.

## Commands

```bash
# Setup
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .example.env .env             # no edits needed for managed identity

# Azure login (one-time, persists across reboots ~90 days)
az login --tenant "cb72c54e-4a31-4d9e-b14a-1ea36dfac94c"
az account set --subscription "2c69c8ba-1dc1-444a-9a18-a483b0be57db"

# Run the pipeline
python -m orchestration.pipeline
python -m orchestration.pipeline --folder "Jira-Requests"
```

There are no tests, linter, or build step configured yet.

## Authentication

We use **managed identity** (not API keys). See `docs/managed-identity-guide.md` for the full explanation.

- **Locally:** `az login` stores a token in `~/.azure/` that persists across reboots
- **In Azure:** system-assigned managed identity on the container (no secrets at all)
- **Code:** `DefaultAzureCredential` in `clients/client.py` picks up whichever is available
- **The `.env` file** only needs `AZURE_OPENAI_ENDPOINT` — no keys

## Network / VPN

The AI Foundry endpoint (`ai-foundry-dcri-sage`) has a **private endpoint** configured. This means:
- `az login` works **without VPN** (talks to public Microsoft login)
- API calls to AI Foundry **require VPN** (private network only)
- **Primary dev environment: Unix VM** (always on VPN, has Claude Code)
- Windows native / WSL work for coding but cannot reach the endpoint without VPN

## Architecture

### Provider Toggle

`config/settings.py` has a single `PROVIDER` variable that controls the entire backend. All agent code branches on this value to choose between Responses API (`client.responses.create()`) and Chat Completions (`client.chat.completions.create()`).

| PROVIDER | Endpoint | Protocol | Duke Health safe? |
|---|---|---|---|
| `azure` | Azure AI Foundry | Chat Completions | YES |
| `azure_responses` | Azure AI Foundry | Responses API | YES (not yet available) |
| `openai_responses` | OpenAI.com | Responses API | NO |
| `openai_chat` | OpenAI.com | Chat Completions | NO |

**Data governance rule:** Only `azure` or `azure_responses` may be used with Duke Health / PHI-adjacent data. OpenAI providers send data outside the Duke tenant.

### Pipeline Flow

```
orchestration/pipeline.py  (orchestrator, wires agents together)
    -> agents/agent1_email.py   (reads emails via tools/outlook_tool.py, LLM extracts structured data)
    -> agents/agent2_router.py  (LLM filters actionable items, adds routing_reason)
    -> agents/agent3_jira.py    (LLM writes ticket descriptions, calls tools/jira_tool.py)
```

Each agent follows the same pattern:
1. Receives data from the previous agent (plain Python dicts, not a message broker)
2. Formats it into a prompt
3. Calls `get_client()` from `clients/client.py` which returns `(client, model)` for the active PROVIDER
4. Branches on `PROVIDER` to choose Responses API vs Chat Completions call pattern
5. Parses JSON from the LLM response
6. Returns structured data for the next agent

### Client Factory

`clients/client.py` exports `get_client() -> (client, model)`. It reads PROVIDER and AZURE_AUTH_MODE from settings, builds the right client (AzureOpenAI or OpenAI), and returns it. Agent code never imports openai directly — always goes through `get_client()`.

Azure auth uses managed identity (`DefaultAzureCredential` / `az login`). API key mode exists in code as a fallback but is not used. See `docs/managed-identity-guide.md`.

### Tools (Stubs)

`tools/outlook_tool.py` and `tools/jira_tool.py` are currently stubs returning fake data. Their function signatures (`read_emails()`, `create_ticket()`) are the stable contract — Phase 2 replaces the stub bodies with real Microsoft Graph / Jira API calls without changing agent code.

### Agent Definitions

Each agent file has an `AGENT_DEFINITION` dict (name + instructions). In Phase 2 these same dicts will be used to register agents in Azure AI Foundry.

## Project Goals & Next Steps

### Portable vs Azure Agent Comparison

The overarching goal is to compare two approaches to building agents and run both on Azure:

| | Portable Agents (current) | Azure AI Foundry Agents (to build) |
|---|---|---|
| Code | Pure Python + `openai` SDK | `azure-ai-projects` SDK |
| State | You manage it (dicts between functions) | Azure manages threads/conversations |
| Runs where | Anywhere: local, any cloud, containers | Azure AI Foundry only |
| Pros | Full control, portable, no lock-in | Managed state, built-in tools, less plumbing |
| Cons | You build all plumbing | Azure-only, newer/less mature |

### Where Things Stand

- Phase 1 complete: 3 agents wired together locally with stub tools
- Pipeline proven to work end-to-end (requires VPN for AI Foundry endpoint)
- Next: create an AI Foundry Project, then build the Azure-native agent version for comparison
- No AI Foundry Project exists yet — agents require a Project to be created first

### Working With Azure (Safety Rules)

- **Never run az commands that modify shared resources without user approval**
- Give the user az commands with explanations; let them execute
- User (scb2@duke.edu) has rights on the AIServices resource but NOT on the resource group
- Always scope az commands with `--resource-group` and project/workspace name
- The user is learning Azure AI Foundry — teach along the way

## Frontend Debug Logging

`bot/web/index.html` has a leveled debug system controlled by a single variable at the top of `<script>`:

```javascript
const DEBUG_LEVEL = 5;  // 0=off, 1=errors, 5=key events, 8=verbose, 10=everything
function dbg(level, ...args) { if (DEBUG_LEVEL >= level) console.log(...args); }
function dbgErr(level, ...args) { if (DEBUG_LEVEL >= level) console.error(...args); }
```

All `console.log/error` calls in the frontend use `dbg(level, ...)` or `dbgErr(level, ...)`. When adding new debug output, choose a level:
- **1:** Errors that should always be visible
- **5:** Key lifecycle events (request sent, response status, result count)
- **7-8:** Request/response details (headers, body previews)
- **10:** Full payloads, response tails

Set to `0` for production/demo, `5` for general debugging, `10` for tracing issues.

## Tool Architecture — MCP + Core Package

The repo uses a layered architecture for reusable tools:

```
core/                    # Python functions (actual logic)
  tools/jira_tool.py     # Jira REST API v3 — tickets, epics, links, dates
  tools/outlook_tool.py  # Email reader (stub, pending IT Graph API approval)
  adapters/              # Format converters (transcript → email-shaped dicts)
  clients/client.py      # LLM client factory (provider toggle)
  config/settings.py     # Central configuration

mcp_servers/             # MCP wrappers (interop layer)
  jira_server.py         # FastMCP server exposing Jira tools
  transcript_server.py   # FastMCP server exposing transcript parsing

bot/                     # FastAPI web app (imports core/ directly)
.claude/skills/          # Claude Code skill (imports core/ via Bash)
```

**Design principle:** `core/` contains the logic. MCP servers are thin wrappers for cross-platform discovery. The FastAPI web app imports `core/` directly (same process, zero overhead). External consumers (Azure AI Foundry, Claude Code, OpenAI Agents SDK, colleague's agents) connect via MCP.

**MCP context bloat mitigation:** Use `defer_loading: true` in Claude Code MCP config. Azure AI Foundry registers tools server-side (no prompt injection). The web app doesn't use MCP at all.

## Docker / Deployment

- **Port:** 3006 (NGINX proxies from `https://aidemo.dcri.duke.edu/sageapp06/`)
- **Start:** `./start-docker.sh` — fetches Azure bearer token + starts container
- **Auto-start:** Cron `@reboot` runs `start-docker.sh`; token refreshed every 45 min via cron
- **Azure auth in Docker:** Bearer token passed as `AZURE_OPENAI_KEY` (api_key mode) because Docker can't use `az login` directly. This is a dev VM workaround — Azure Container Apps will use managed identity.
- **`restart: unless-stopped`** in docker-compose.yml — container survives Docker daemon restarts
- **Frontend subpath:** All `fetch()` URLs use `BASE` variable (auto-detected from `window.location.pathname`) to support reverse proxy subpaths

## Docs

- `docs/managed-identity-guide.md` — full walkthrough of auth, local dev, deployment, troubleshooting
