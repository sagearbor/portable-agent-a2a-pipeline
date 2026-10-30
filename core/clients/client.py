"""
Single entry point for all LLM clients.
Import get_client() anywhere in agent code - it reads PROVIDER from settings
and returns the right configured client without the caller needing to know.

Usage:
    from core.clients.client import get_client, completion_text
    from core.dcri_llm import llm_action
    client, model = get_client()
    with llm_action("my_operation"):
        response = client.chat.completions.create(...)
    text = completion_text(response, where="my_operation")

Auth and telemetry for the Azure providers come from the vendored shared
module ``core/dcri_llm.py``:
  - On Azure Container Apps it signs in with the app's user-assigned managed
    identity (``AZURE_CLIENT_ID``); locally it uses your ``az login`` session.
    No API key is needed. ``LLM_AUTH=key`` (+ ``AZURE_OPENAI_API_KEY``) is the
    only key path, for colleagues without an identity.
  - Every chat.completions / responses call emits one ``llm_call`` JSON line
    (token counts, finish_reason, latency; never prompt or completion text).
"""

import os

from dotenv import load_dotenv

from core import dcri_llm
from core.config.settings import PROVIDER, MODELS

load_dotenv()


class LLMOutputError(RuntimeError):
    """The model returned no usable output (truncated, filtered or empty)."""


def _build_azure_client():
    """AzureOpenAI client with managed-identity auth and per-call telemetry.

    The same client serves Chat Completions (``azure``) and the Responses API
    (``azure_responses``): dcri_llm's default api_version supports both.
    """
    return dcri_llm.make_client()


def token_limit_kwarg(model: str, n: int) -> dict:
    """
    Returns the correct Chat Completions token-limit kwarg for the model.

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
    # Reasoning consumes the completion budget before visible output begins.
    floor = dcri_llm.DEFAULT_MAX_COMPLETION_TOKENS
    return {key: max(n, floor) if key == "max_completion_tokens" else n}


def output_token_kwarg(model: str, n: int) -> dict:
    """Responses API equivalent of token_limit_kwarg (``max_output_tokens``).

    Reasoning models (gpt-5.x, o-series) never get less than 40000.
    """
    limit = token_limit_kwarg(model, n)
    return {"max_output_tokens": next(iter(limit.values()))}


def completion_text(response, *, where: str) -> str:
    """Return the text of a chat.completions or responses result, or raise.

    A truncated (finish_reason=length), content-filtered or empty response is
    an error the caller must surface, never a silent success. Only lengths and
    the finish_reason go into the error message; never the output text.
    """
    if hasattr(response, "choices"):
        info = dcri_llm.usage_from_chat_json(response)
        choices = response.choices or []
        text = (choices[0].message.content if choices else None) or ""
    else:
        info = dcri_llm.usage_from_responses_json(response)
        text = getattr(response, "output_text", None) or ""

    finish = info.get("finish_reason")
    if finish == "length":
        raise LLMOutputError(
            f"[{where}] LLM output truncated (finish_reason=length, "
            f"output_chars={len(text)}, reasoning_tokens={info.get('reasoning_tokens')}). "
            "The completion budget ran out; split the input or raise the budget."
        )
    if finish == "content_filter":
        raise LLMOutputError(f"[{where}] LLM output blocked by content filter.")
    if not text.strip():
        raise LLMOutputError(f"[{where}] LLM returned empty output (finish_reason={finish}).")
    return text


def get_client():
    """
    Returns (client, model_name) tuple for the configured PROVIDER.

    azure:            AzureOpenAI client - Chat Completions - safe for Duke Health data
    azure_responses:  AzureOpenAI client - Responses API    - safe for Duke Health data
    openai_responses: OpenAI client      - Responses API    - NOT for Duke Health data
    openai_chat:      OpenAI client      - Chat Completions - NOT for Duke Health data

    Every client returned is dcri_llm-instrumented (one telemetry event per call).
    """

    model = MODELS[PROVIDER]

    if PROVIDER in ("azure", "azure_responses"):
        return _build_azure_client(), model

    elif PROVIDER in ("openai_responses", "openai_chat"):
        from openai import OpenAI
        client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
        return dcri_llm.instrument(client), model

    else:
        raise ValueError(
            f"Unknown PROVIDER '{PROVIDER}'. "
            "Valid options: 'azure', 'azure_responses', 'openai_responses', 'openai_chat'"
        )
