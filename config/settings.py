"""
Central configuration.
Change PROVIDER here to switch all agent code to a different backend.

Options:
  "azure"             - Azure AI Foundry endpoint, Chat Completions protocol
                        Safe for Duke Health / PHI-adjacent data.
                        Data stays inside Duke Health tenant.
                        Works with your foundry today.

  "azure_responses"   - Azure AI Foundry endpoint, Responses API (v1, GA 2026)
                        Safe for Duke Health data + stateful threads.
                        Uses /openai/v1/ base URL — no api_version needed.
                        Requires AZURE_OPENAI_V1_BASE_URL in .env.
                        Supports gpt-5.x and all modern models.

  "openai_responses"  - OpenAI.com endpoint, Responses API protocol
                        Stateful threads: only new message sent after turn 1.
                        Lower latency + lower cost per turn than chat completions.
                        Your existing code uses this pattern.
                        NOT for Duke Health / PHI data - data leaves Duke tenant.

  "openai_chat"       - OpenAI.com endpoint, Chat Completions protocol
                        Stateless: full history sent on every call.
                        Broadest model compatibility (local models, Kimi, etc).
                        Good for learning and comparing protocols.
                        NOT for Duke Health / PHI data - data leaves Duke tenant.
"""

import os

#PROVIDER = "openai_responses"  # <-- change this one value to switch everything
PROVIDER = "azure"  # <-- change this one value to switch everything

# ---------------------------------------------------------------------------
# Model names per provider
# ---------------------------------------------------------------------------
MODELS = {
    "azure":             "gpt-5.2",   # deployment name in your AI Foundry
    "azure_responses":   "gpt-5.2",   # same deployment, different protocol (future)
    "openai_responses":  "gpt-4o",    # or o3, gpt-4-turbo, etc
    "openai_chat":       "gpt-4o",
}

# ---------------------------------------------------------------------------
# Azure authentication mode (only applies to azure / azure_responses providers)
# ---------------------------------------------------------------------------
# "az_login"         - uses your 'az login' session explicitly (AzureCliCredential).
#     Best for local dev on an Azure VM, where DefaultAzureCredential would
#     otherwise pick up the VM's managed identity instead of your personal login.
#     Requires: az login to have been run in this terminal session.
#
# "managed_identity" - uses DefaultAzureCredential which checks (in order):
#     1. Environment variables AZURE_CLIENT_ID / AZURE_CLIENT_SECRET / AZURE_TENANT_ID
#     2. Workload Identity
#     3. Managed Identity (VM, App Service, Container) ← picks this up first on a VM!
#     4. Your 'az login' session
#   Use this in production containers where a managed identity is assigned.
#   WARNING: on a dev VM this will authenticate as the VM's identity, not you.
#
# "api_key"          - uses AZURE_OPENAI_KEY from .env
#     Simpler for quick local testing. Never commit the key.
#
AZURE_AUTH_MODE = os.environ.get("AZURE_AUTH_MODE", "az_login")  # "az_login" | "managed_identity" | "api_key"

# ---------------------------------------------------------------------------
# Temperature / shared inference settings
# ---------------------------------------------------------------------------
TEMPERATURE = 0.2   # lower = more deterministic, good for routing agents
MAX_TOKENS  = 4096  # bumped from 2048 — agent3 needs room for multi-ticket descriptions
