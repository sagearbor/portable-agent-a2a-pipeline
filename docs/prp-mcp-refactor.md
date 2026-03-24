# PRP: Refactor to core/ + MCP Server Architecture

**Date:** 2026-03-24
**Status:** Draft
**Author:** SageJiraBot pipeline team

## Goal

Refactor the portable-agent-a2a-pipeline into a layered architecture where:
1. Core tool logic lives in `core/` (pure Python, no framework deps)
2. MCP servers in `mcp_servers/` wrap core tools for cross-platform use
3. FastAPI web app in `bot/` imports `core/` directly (unchanged UX)
4. Azure AI Foundry agents connect via MCP (enables the comparison eval)

## Why

- Other developers building tools (SharePoint, Confluence, etc.) need a standard pattern
- Azure AI Foundry evaluation requires registering tools — MCP is the native path
- Current code has tools tightly coupled to FastAPI routes and agent files
- Building MCP servers unlocks: Claude Code, OpenAI Agents SDK, Gemini, Copilot Studio

## Current State

```
agents/agent1_email.py      <- imports from clients/, config/
agents/agent2_router.py     <- imports from clients/, config/
agents/agent3_jira.py       <- imports from clients/, config/, tools/
tools/jira_tool.py          <- pure Python (no framework deps) ✓
tools/outlook_tool.py       <- stub, same pattern ✓
bot/jira_context.py         <- pure Python ✓
bot/adapters/transcript_adapter.py  <- pure Python ✓
clients/client.py           <- pure Python ✓
config/settings.py          <- pure Python ✓
bot/api/routes/*.py         <- FastAPI-specific (HTTP handlers)
bot/web/index.html          <- Web UI
```

## Target State

```
core/
├── agents/                  # Moved from agents/
│   ├── agent1_email.py
│   ├── agent2_router.py
│   └── agent3_jira.py
├── tools/                   # Moved from tools/
│   ├── jira_tool.py
│   └── outlook_tool.py
├── adapters/                # Moved from bot/adapters/
│   └── transcript_adapter.py
├── clients/                 # Moved from clients/
│   └── client.py
├── config/                  # Moved from config/
│   └── settings.py
├── jira_context.py          # Moved from bot/jira_context.py
└── __init__.py

mcp_servers/
├── jira_server.py           # NEW: FastMCP wrapper for Jira tools
├── transcript_server.py     # NEW: FastMCP wrapper for transcript adapter
└── requirements.txt         # fastmcp + core deps

bot/                         # UNCHANGED (update imports to core.*)
├── api/
│   ├── main.py
│   └── routes/*.py          # Update: from core.agents import ...
└── web/
    └── index.html

.claude/skills/              # NEW: Claude Code skill
└── transcript-to-jira/
    └── SKILL.md
```

## Implementation Phases

### Phase 1: Extract core/ (2-3 hours)
1. Create core/ directory structure
2. Move files (agents, tools, clients, config, adapters, jira_context)
3. Update all imports in bot/api/routes/*.py to use core.*
4. Add backward-compat re-exports in old locations (import from core, re-export)
5. Test: `python -m uvicorn bot.api.main:app --port 3007` still works
6. Test: `python -m core.agents.agent1_email` imports cleanly

### Phase 2: Build MCP servers (3-4 hours)
1. Install fastmcp: `pip install fastmcp`
2. Create mcp_servers/jira_server.py wrapping core/tools/jira_tool.py
   - Tools: create_ticket, query_epics, query_sprints, query_fix_versions,
     query_project_members, check_duplicates, create_issue_link, enrich_tickets
3. Create mcp_servers/transcript_server.py wrapping core/adapters/
   - Tools: parse_transcript, detect_format, generate_mock_transcript
4. Test: `python mcp_servers/jira_server.py` runs and lists tools
5. Test: Connect from Claude Code via mcpServers config

### Phase 3: Azure AI Foundry agent (4-6 hours)
1. Register MCP servers as tool endpoints in AI Foundry
2. Create Foundry agent using azure-ai-projects SDK
3. Wire agent to use Jira MCP tools instead of direct Python imports
4. Compare: same pipeline, Foundry-managed threads vs portable dicts
5. Document findings in comparison report

### Phase 4: Claude Code skill (1-2 hours)
1. Create .claude/skills/transcript-to-jira/SKILL.md
2. Skill invokes the pipeline via Bash (calls core/ functions)
3. Test: /transcript-to-jira ~/meeting.txt ST

## Dependencies

- fastmcp Python package (pip install)
- AI Foundry Project testsage1 (exists)
- No IT dependencies for Phase 1-2

## Success Criteria

- [ ] `core/` package importable standalone (no bot/ or mcp_servers/ deps)
- [ ] MCP Jira server runs and Claude Code can call `create_ticket` through it
- [ ] Azure AI Foundry agent runs the same pipeline using MCP tools
- [ ] FastAPI web app works identically (no user-visible changes)
- [ ] Other repos can copy mcp_servers/jira_server.py pattern
