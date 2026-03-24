"""
Single entry point for all LLM clients.
Import get_client() anywhere in agent code - it reads PROVIDER from settings
and returns the right configured client without the caller needing to know.

Usage:
    from core.clients.client import get_client
    client, model = get_client()
"""

import os
from dotenv import load_dotenv
from core.config.settings import PROVIDER, MODELS, AZURE_AUTH_MODE

load_dotenv()


def _get_azure_credential():
    """Returns the right Azure credential based on AZURE_AUTH_MODE."""
    if AZURE_AUTH_MODE == "az_login":
        from azure.identity import AzureCliCredential
        return AzureCliCredential()
    else:
        from azure.identity import DefaultAzureCredential
        return DefaultAzureCredential()


def _build_azure_responses_client():
    """
    Builds an OpenAI client for the Azure v1 Responses API.

    Uses the /openai/v1/ base URL which:
    - Does NOT require api_version
    - Supports client.responses.create()
    - Works with gpt-5.x and all newer models
    - Token scope is https://ai.azure.com/.default (different from Chat Completions)

    Requires AZURE_OPENAI_V1_BASE_URL in .env.
    """
    from openai import OpenAI
    base_url = os.environ["AZURE_OPENAI_V1_BASE_URL"]
    credential = _get_azure_credential()
    # Get a fresh bearer token. Pipeline completes in <60s so no refresh needed.
    token = credential.get_token("https://ai.azure.com/.default").token
    return OpenAI(base_url=base_url, api_key=token)


def _build_azure_client():
    """
    Builds an AzureOpenAI client using either managed identity or API key,
    controlled by AZURE_AUTH_MODE in settings.py.

    az_login:
        Uses AzureCliCredential - explicitly uses your 'az login' session.
        Best for local dev on an Azure VM where DefaultAzureCredential would
        pick up the VM's managed identity instead of your personal login.
        Requires: az login to have been run.

    managed_identity:
        Uses DefaultAzureCredential - tries managed identity before az login.
        Use this in production containers with a managed identity assigned.
        WARNING: on a dev VM this authenticates as the VM, not you.

    api_key:
        Uses AZURE_OPENAI_KEY from .env.
        Simpler for initial local testing.
        Do not use for production or with real data if you can avoid it.
    """
    from openai import AzureOpenAI
    endpoint   = os.environ["AZURE_OPENAI_ENDPOINT"]
    api_version = os.environ.get("AZURE_OPENAI_API_VERSION", "2025-01-01-preview")

    if AZURE_AUTH_MODE in ("az_login", "managed_identity"):
        from azure.identity import get_bearer_token_provider
        token_provider = get_bearer_token_provider(
            _get_azure_credential(),
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


def token_limit_kwarg(model: str, n: int) -> dict:
    """
    Returns the correct token-limit kwarg for the given model as a dict.

    Newer models (o1, o3, gpt-5.x) require 'max_completion_tokens'.
    Older models (gpt-4, gpt-3.5, gpt-4o) use 'max_tokens'.

    Usage in agent code:
        response = client.chat.completions.create(
            model=model,
            messages=[...],
            **token_limit_kwarg(model, MAX_TOKENS),
        )
    """
    newer = ("o1", "o3", "o4", "gpt-5")
    key = "max_completion_tokens" if any(model.startswith(p) for p in newer) else "max_tokens"
    return {key: n}


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
        return _build_azure_client(), model

    elif PROVIDER == "azure_responses":
        return _build_azure_responses_client(), model

    elif PROVIDER == "openai_responses":
        from openai import OpenAI
        client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
        return client, model

    elif PROVIDER == "openai_chat":
        from openai import OpenAI
        client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
        return client, model

    else:
        raise ValueError(
            f"Unknown PROVIDER '{PROVIDER}'. "
            "Valid options: 'azure', 'azure_responses', 'openai_responses', 'openai_chat'"
        )
