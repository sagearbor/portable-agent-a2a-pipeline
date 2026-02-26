"""
Single entry point for all LLM clients.
Import get_client() anywhere in agent code - it reads PROVIDER from settings
and returns the right configured client without the caller needing to know.

Usage:
    from clients.client import get_client
    client, model = get_client()
"""

import os
from dotenv import load_dotenv
from config.settings import PROVIDER, MODELS, AZURE_AUTH_MODE

load_dotenv()


def _build_azure_client():
    """
    Builds an AzureOpenAI client using either managed identity or API key,
    controlled by AZURE_AUTH_MODE in settings.py.

    managed_identity:
        Uses DefaultAzureCredential - no API key in .env needed.
        Locally: picks up your 'az login' session automatically.
        In Azure: picks up the managed identity of the resource (VM, container etc).
        This is the recommended mode - nothing to leak or rotate.

    api_key:
        Uses AZURE_OPENAI_KEY from .env.
        Simpler for initial local testing.
        Do not use for production or with real data if you can avoid it.
    """
    from openai import AzureOpenAI
    endpoint   = os.environ["AZURE_OPENAI_ENDPOINT"]
    api_version = os.environ.get("AZURE_OPENAI_API_VERSION", "2025-01-01-preview")

    if AZURE_AUTH_MODE == "managed_identity":
        from azure.identity import DefaultAzureCredential, get_bearer_token_provider
        token_provider = get_bearer_token_provider(
            DefaultAzureCredential(),
            "https://cognitiveservices.azure.com/.default"
        )
        return AzureOpenAI(
            azure_endpoint=endpoint,
            azure_ad_token_provider=token_provider,
            api_version=api_version,
        )
    elif AZURE_AUTH_MODE == "api_key":
        return AzureOpenAI(
            azure_endpoint=endpoint,
            api_key=os.environ["AZURE_OPENAI_KEY"],
            api_version=api_version,
        )
    else:
        raise ValueError(
            f"Unknown AZURE_AUTH_MODE '{AZURE_AUTH_MODE}'. "
            "Valid options: 'managed_identity', 'api_key'"
        )


def get_client():
    """
    Returns (client, model_name) tuple for the configured PROVIDER.

    azure:            AzureOpenAI client  - Chat Completions - safe for Duke Health data
    azure_responses:  NOT YET VALID       - raises NotImplementedError
    openai_responses: OpenAI client       - Responses API    - NOT for Duke Health data
    openai_chat:      OpenAI client       - Chat Completions - NOT for Duke Health data
    """

    model = MODELS[PROVIDER]

    if PROVIDER == "azure":
        # ------------------------------------------------------------------
        # Azure AI Foundry - Chat Completions
        # Protocol:  client.chat.completions.create(...)
        # Safe for Duke Health / PHI-adjacent data.
        # Auth: see AZURE_AUTH_MODE in settings.py (managed_identity recommended)
        # ------------------------------------------------------------------
        return _build_azure_client(), model

    elif PROVIDER == "azure_responses":
        # ------------------------------------------------------------------
        # Azure AI Foundry - Responses API
        # Protocol:  client.responses.create(...)
        # Auth: same as 'azure' - uses AZURE_AUTH_MODE
        # COMING SOON: Azure Responses API not fully available yet.
        # When ready: same _build_azure_client() call, just needs newer api_version
        # and .responses.create() call pattern in agent code.
        # ------------------------------------------------------------------
        raise NotImplementedError(
            "PROVIDER='azure_responses' is not yet available. "
            "Azure Responses API is still rolling out. "
            "Use 'azure' (Chat Completions) for Duke Health data for now, "
            "or 'openai_responses' for personal/non-PHI work."
        )

    elif PROVIDER == "openai_responses":
        # ------------------------------------------------------------------
        # OpenAI.com - Responses API
        # Protocol:  client.responses.create(...)
        # Stateful: after turn 1, only the new message is sent over the wire.
        # Server holds thread state. Lower latency + cost on long conversations.
        # NOT for Duke Health / PHI data - data leaves Duke tenant.
        # ------------------------------------------------------------------
        from openai import OpenAI
        client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
        return client, model

    elif PROVIDER == "openai_chat":
        # ------------------------------------------------------------------
        # OpenAI.com - Chat Completions
        # Protocol:  client.chat.completions.create(...)
        # Stateless: full message history sent on every call.
        # Broadest compatibility - same protocol as Ollama, Kimi, local models.
        # NOT for Duke Health / PHI data - data leaves Duke tenant.
        # ------------------------------------------------------------------
        from openai import OpenAI
        client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
        return client, model

    else:
        raise ValueError(
            f"Unknown PROVIDER '{PROVIDER}'. "
            "Valid options: 'azure', 'azure_responses', 'openai_responses', 'openai_chat'"
        )
