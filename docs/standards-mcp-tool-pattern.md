# MCP Tool Pattern — Standard for DCRI AI Agent Tools

**Version:** 1.0 — 2026-03-24
**Purpose:** Standardize how we build reusable AI tools across repos so they compose cleanly.

## Why MCP?

MCP (Model Context Protocol) is the universal standard for AI tool integration, adopted by:
- Azure AI Foundry (native MCP server support for agents)
- OpenAI Agents SDK (built-in MCP client)
- Claude Code (native MCP with deferred loading)
- Google Gemini, Copilot Studio, and others

Building tools as MCP servers means build once, use from any agent platform.

## The Pattern

Every repo that provides reusable tools should follow this structure:

```
my-repo/
├── core/                    # Pure Python logic (no framework deps)
│   ├── tools/
│   │   ├── my_tool.py       # Functions with clear signatures
│   │   └── __init__.py
│   ├── config/
│   │   └── settings.py      # Environment-driven config
│   └── __init__.py
│
├── mcp_servers/             # MCP wrappers (thin layer)
│   ├── my_tool_server.py    # FastMCP server
│   └── requirements.txt     # fastmcp + core deps
│
├── bot/ or app/             # Optional: web UI, API, bot (imports core/)
└── .claude/skills/          # Optional: Claude Code skills
```

### Rules

1. **core/ has zero framework dependencies** — no FastAPI, no MCP, no Azure SDK imports. Just `requests`, `json`, `os`. This makes it importable anywhere.

2. **MCP servers are thin wrappers** — they import from `core/` and expose functions with `@mcp.tool()`. No business logic in the MCP layer.

3. **Configuration via environment variables** — tools read credentials from env vars. MCP servers pass through whatever the host environment provides. No hardcoded secrets.

4. **Each tool function is self-documenting** — clear docstrings, type hints, and sensible defaults. MCP auto-generates tool descriptions from these.

5. **Tools are stateless** — no global state between calls. Each invocation gets what it needs from parameters + env vars.

### Example: Wrapping a tool as MCP

```python
# core/tools/jira_tool.py (existing pure Python)
def create_ticket(summary: str, description: str, project_key: str = "ST") -> dict:
    """Create a Jira ticket. Returns {ticket_id, url, status, summary}."""
    ...

# mcp_servers/jira_server.py (thin MCP wrapper)
from fastmcp import FastMCP
from core.tools import jira_tool

mcp = FastMCP("dcri-jira-tools",
    description="Jira ticket management — create, query, link issues")

@mcp.tool()
def create_ticket(summary: str, description: str, project_key: str = "ST") -> dict:
    """Create a Jira ticket in the specified project."""
    return jira_tool.create_ticket(summary, description, project_key=project_key)

@mcp.tool()
def query_epics(project_key: str) -> list[dict]:
    """Get open epics in a Jira project."""
    from core.tools.jira_context import query_epics
    return query_epics(project_key)
```

### Running an MCP Server

```bash
# Standalone (for testing)
python mcp_servers/jira_server.py

# In Claude Code settings.json
{
  "mcpServers": {
    "dcri-jira": {
      "command": "python",
      "args": ["mcp_servers/jira_server.py"],
      "env": {"JIRA_BASE_URL": "...", "JIRA_EMAIL": "...", "JIRA_API_TOKEN": "..."}
    }
  }
}

# In Azure AI Foundry agent
# Register as MCP tool endpoint in the Foundry portal
```

### Context Bloat Mitigation

For Claude Code, enable deferred loading so tools only load when needed:

```json
{
  "mcpServers": {
    "dcri-jira": {
      "command": "python",
      "args": ["mcp_servers/jira_server.py"],
      "defer_loading": true
    }
  }
}
```

Azure AI Foundry doesn't have this problem — tools are registered server-side.

### Credential Pattern

Tools should read credentials from environment variables:
- `JIRA_BASE_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN` — for Jira tools
- `AZURE_OPENAI_ENDPOINT`, `AZURE_AUTH_MODE` — for LLM tools
- MCP servers inherit env vars from the host process

Never hardcode credentials. Never pass them as tool parameters.

### Testing

```bash
# Test the core Python function directly
python -c "from core.tools.jira_tool import create_ticket; print(create_ticket('Test', 'Desc', project_key='ST'))"

# Test the MCP server
echo '{"method": "tools/list"}' | python mcp_servers/jira_server.py

# Test from Claude Code
# Enable the MCP server, then ask: "list epics in project ST"
```

### Repos Using This Pattern

| Repo | MCP Server | Tools Provided |
|------|-----------|----------------|
| portable-agent-a2a-pipeline | dcri-jira-tools | Jira CRUD, transcript parsing, LLM enrichment |
| (your SharePoint repo) | dcri-sharepoint-tools | File sync, document read/write |
| (future) | dcri-outlook-tools | Email read, send, folder management |

### References

- [Azure AI Foundry MCP](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/tools/model-context-protocol)
- [FastMCP Python](https://github.com/jlowin/fastmcp)
- [OpenAI Agents SDK MCP](https://openai.github.io/openai-agents-python/mcp/)
- [MCP Specification](https://modelcontextprotocol.io)
