"""
Shared helpers for the suggest-updates quality pieces.

* ``flag_on(name)`` reads an on/off switch from the environment AT CALL TIME,
  so a piece can be turned off on the running app with
  ``az containerapp update --set-env-vars NAME=0`` (new revision, no rebuild).
* ``call_json(...)`` makes one dcri_llm-instrumented LLM call that must return
  a JSON object: >= 40000 completion tokens, truncated/empty output raises
  ``LLMOutputError`` (via ``completion_text``), and ``temperature`` is omitted
  for models that only accept the default (gpt-5.5 / gpt-5.6 / o-series).

PHI rule: nothing here logs prompt or completion text.
"""

from __future__ import annotations

import os

from core.agents.llm_json import parse_llm_json
from core.clients.client import (
    get_client, token_limit_kwarg, output_token_kwarg, completion_text,
)
from core.config import settings
from core.dcri_llm import llm_action

_OFF = ("0", "false", "off", "no")

# Models that reject any temperature other than the default.
_NO_TEMPERATURE = ("gpt-5.5", "gpt-5.6", "gpt-5-", "o1", "o3", "o4")


def flag_on(name: str, default: bool = True) -> bool:
    """True unless env var *name* is set to 0/false/off/no (unset -> default)."""
    v = (os.environ.get(name) or "").strip().lower()
    if not v:
        return default
    return v not in _OFF


def env_float(name: str, default: float) -> float:
    try:
        return float((os.environ.get(name) or "").strip() or default)
    except ValueError:
        return default


def env_str(name: str, default: str | None) -> str | None:
    return (os.environ.get(name) or "").strip() or default


def _accepts_temperature(model: str) -> bool:
    m = (model or "").lower()
    return not any(m.startswith(p) for p in _NO_TEMPERATURE) and m != "gpt-5"


def llm_timeout_s() -> float:
    """Per-attempt HTTP timeout (the SDK default is 600 s, and a real run saw a
    call hang); the SDK still retries a timed-out attempt."""
    return env_float("UPDATES_LLM_TIMEOUT_S", 120.0)


def reasoning_effort(model: str) -> dict:
    """Thinking level for reasoning models (UPDATES_REASONING_EFFORT, default
    "low"; "default"/"off" = don't send it).  On the user's transcript gpt-5.5
    split took 10-26 s at Azure's default level and 6-9 s at low, same topics."""
    v = (os.environ.get("UPDATES_REASONING_EFFORT") or "low").strip().lower()
    if v in ("default", "off", "") or _accepts_temperature(model):
        return {}
    return {"reasoning_effort": v}


def call_json(system: str, user: str, *, model: str | None, operation: str):
    """One LLM call -> parsed JSON (dict or list).  Raises LLMOutputError on
    truncated/empty output and ValueError on unparseable JSON."""
    client, default_model = get_client()
    model = model or default_model
    temp = {"temperature": settings.TEMPERATURE} if _accepts_temperature(model) else {}
    timeout_kw = {"timeout": llm_timeout_s()}
    effort = reasoning_effort(model)

    def _create(extra):
        if settings.PROVIDER in ("openai_responses", "azure_responses"):
            if "reasoning_effort" in extra:
                extra = {k: v for k, v in extra.items() if k != "reasoning_effort"}
                extra["reasoning"] = {"effort": effort["reasoning_effort"]}
            return client.responses.create(
                model=model, instructions=system, input=user, **timeout_kw,
                **extra, **output_token_kwarg(model, settings.MAX_TOKENS))
        return client.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": user}],
            **timeout_kw, **extra, **token_limit_kwarg(model, settings.MAX_TOKENS))

    with llm_action(operation):
        try:
            response = _create({**temp, **effort})
        except Exception as exc:   # noqa: BLE001 - retry once without temperature/effort
            msg = str(exc).lower()
            if (temp and "temperature" in msg) or (effort and "reasoning" in msg):
                response = _create({})
            else:
                raise
    raw = completion_text(response, where=operation)
    return parse_llm_json(raw, context=operation)
