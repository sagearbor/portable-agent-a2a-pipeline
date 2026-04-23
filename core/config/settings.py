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
# Per-agent model overrides (optional, env-driven)
# ---------------------------------------------------------------------------
# Each stage of the pipeline can be pointed at a different model. Cheaper /
# faster models (e.g. gpt-5-nano) are fine for simple classification and
# short-form writing but hurt quality for transcript comprehension and
# dependency reasoning. Leave unset to use the provider default from MODELS.
#
# Recommended baseline:
#   AGENT1_MODEL  = unset           # transcript comprehension — needs full capability
#   AGENT2_MODEL  = gpt-5-nano      # approve/reject classification — simple
#   AGENT3_MODEL  = gpt-5-nano      # description writing — mostly boilerplate
#   ENRICH_MODEL  = unset           # epic matching + dependency ordering — needs reasoning
#
# We keep the model names as env vars (not settings constants) so the matrix
# can be tuned per-deployment without a code change, and we can A/B test
# cheaper models without touching the image.
AGENT1_MODEL = (os.environ.get("AGENT1_MODEL") or "").strip() or None
AGENT2_MODEL = (os.environ.get("AGENT2_MODEL") or "").strip() or None
AGENT3_MODEL = (os.environ.get("AGENT3_MODEL") or "").strip() or None
ENRICH_MODEL = (os.environ.get("ENRICH_MODEL") or "").strip() or None

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
# Model context window sizes (input tokens)
# Used by transcript_adapter to decide whether to chunk or send whole.
# Conservative estimates — leave headroom for system prompt + output.
# ---------------------------------------------------------------------------
MODEL_CONTEXT_WINDOWS = {
    "gpt-5.2":       1_000_000,
    "gpt-5.3-codex": 1_000_000,
    "gpt-4o":          128_000,
    "gpt-4-turbo":     128_000,
    "o3":              200_000,
    "o4-mini":         200_000,
}

# Max input tokens to use for transcript (fraction of context window).
# Reserves the rest for system prompt + output tokens.
MAX_INPUT_FRACTION = 0.6

# ---------------------------------------------------------------------------
# Temperature / shared inference settings
# ---------------------------------------------------------------------------
TEMPERATURE = 0.2   # lower = more deterministic, good for routing agents
MAX_TOKENS  = 16384  # generous default — truncation causes silent failures that waste days debugging
