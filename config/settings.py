"""
Central configuration.
Change PROVIDER here to switch all agent code to a different backend.

Options:
  "azure"             - Azure AI Foundry endpoint, Chat Completions protocol
                        Safe for Duke Health / PHI-adjacent data.
                        Data stays inside Duke Health tenant.
                        Works with your foundry today.

  "azure_responses"   - Azure AI Foundry endpoint, Responses API protocol
                        Safe for Duke Health data + stateful threads.
                        NOT YET VALID - Azure Responses API not fully available.
                        Will raise NotImplementedError until Azure catches up.

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
# "managed_identity" - no API key needed. Uses DefaultAzureCredential which checks:
#     1. Your 'az login' session (when running locally)
#     2. Managed Identity (when running inside Azure: VM, App Service, Container)
#     3. Environment variables AZURE_CLIENT_ID / AZURE_CLIENT_SECRET / AZURE_TENANT_ID
#   Recommended for production and for any code that may touch real data.
#   Requires: azure-identity package (already in requirements.txt)
#
# "api_key"          - uses AZURE_OPENAI_KEY from .env
#     Simpler for quick local testing. Never commit the key.
#     Not recommended once managed identity is working.
#
AZURE_AUTH_MODE = "managed_identity"  # "managed_identity" | "api_key"

# ---------------------------------------------------------------------------
# Temperature / shared inference settings
# ---------------------------------------------------------------------------
TEMPERATURE = 0.2   # lower = more deterministic, good for routing agents
MAX_TOKENS  = 2048
