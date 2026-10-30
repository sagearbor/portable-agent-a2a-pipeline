# Vendored copy of sagearbor/mvp_landing_page packages/dcri_llm. Do not edit here; update upstream and re-copy.
"""dcri_llm: managed-identity sign-in to Azure OpenAI plus one telemetry event per LLM call.

Single file on purpose: apps vendor it by copying this file. Dependencies:
``azure-identity`` (required for identity auth), ``openai`` (for ``make_client``),
``azure-storage-blob`` (optional, only for the Blob sink). Telemetry itself only
uses the standard library and never raises into, or blocks, the calling app.

Quick use::

    from dcri_llm import make_client, llm_action
    client = make_client()                       # identity auth, instrumented
    with llm_action("summarize"):
        client.chat.completions.create(model="gpt-5.4-nano", messages=[...],
                                       max_completion_tokens=40000)

Every call prints one JSON line ``{"event": "llm_call", "schema": 1, ...}`` to
stdout (schema v1, docs/TELEMETRY-PLAN.md upstream). Events never contain prompt
text, completion text, file names, keys or tokens; only counts and lengths.
"""
from __future__ import annotations

import atexit
import contextvars
import functools
import inspect
import json
import logging
import os
import queue
import re
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from urllib.parse import urlparse

__version__ = "1.2.0"
SCHEMA_VERSION = 1

__all__ = [
    "__version__", "SCHEMA_VERSION", "SCOPE", "DEFAULT_API_VERSION",
    "DEFAULT_MAX_COMPLETION_TOKENS", "REASONING_BUDGET_FLOOR", "PRICE_TABLE_DATE",
    "get_credential", "get_token_provider", "get_bearer_token", "auth_headers", "make_client",
    "instrument", "record_llm_call", "usage_from_responses_json",
    "usage_from_chat_json", "litellm_callbacks", "litellm_metadata",
    "llm_action", "set_user", "current_context", "asgi_middleware",
    "wsgi_middleware", "add_sink", "flush", "telemetry_stats", "estimate_cost_usd",
]

log = logging.getLogger("dcri_llm")

SCOPE = "https://cognitiveservices.azure.com/.default"
DEFAULT_API_VERSION = "2025-04-01-preview"  # supports chat.completions and responses
# HARD RULE: reasoning models (gpt-5.x, o-series) think out of the completion budget.
# Never cap them below 32000; default 40000, or they return empty/truncated output.
DEFAULT_MAX_COMPLETION_TOKENS = 40000
REASONING_BUDGET_FLOOR = 32000

# ---------------------------------------------------------------------------
# Cost estimate. USD per 1M tokens: (input, cached_input, output). Output
# includes reasoning tokens. List prices, NOT Duke's negotiated rate: label as
# an estimate. Unknown model -> est_cost_usd is null. Add rows here upstream.
# ---------------------------------------------------------------------------
PRICE_TABLE_DATE = "2026-10-30"
PRICES = {
    "gpt-5": (1.25, 0.125, 10.00),
    "gpt-5-mini": (0.25, 0.025, 2.00),
    "gpt-5-nano": (0.05, 0.005, 0.40),
    "gpt-4.1": (2.00, 0.50, 8.00),
    "gpt-4.1-mini": (0.40, 0.10, 1.60),
    "gpt-4.1-nano": (0.10, 0.025, 0.40),
    "gpt-4o": (2.50, 1.25, 10.00),
    "gpt-4o-mini": (0.15, 0.075, 0.60),
    "o3": (2.00, 0.50, 8.00),
    "o4-mini": (1.10, 0.275, 4.40),
}
_REASONING_RE = re.compile(r"^(?:azure/|openai/)?(gpt-5|o\d)", re.I)
_DATE_SUFFIX_RE = re.compile(r"-\d{4}-\d{2}-\d{2}$")

FIELDS = (
    "event", "schema", "ts", "solution", "solution_version", "operation",
    "action_id", "user", "provider", "endpoint_host", "api", "model_requested",
    "model_returned", "input_tokens", "cached_input_tokens", "output_tokens",
    "reasoning_tokens", "max_completion_tokens", "reasoning_effort",
    "finish_reason", "status", "empty_output", "finish_length",
    "budget_below_floor", "input_chars", "output_chars", "streamed",
    "latency_ms", "ttft_ms", "attempt", "outcome", "error_type", "http_status",
    "est_cost_usd", "price_table", "lib_version",
)
# Whitelist: record_llm_call drops anything not listed here, so prompt/completion
# text, file names and keys can never reach an event by accident.

# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
_cred_lock = threading.Lock()
_credential = None
_token_provider = None


def get_credential():
    """Return the process-wide Azure credential (cached singleton).

    On Azure Container Apps (``IDENTITY_ENDPOINT`` set) this is the user-assigned
    ``ManagedIdentityCredential(client_id=$AZURE_CLIENT_ID)``; locally it is
    ``AzureCliCredential`` (run ``az login``). DefaultAzureCredential is avoided
    on purpose: its probe chain is slow and can pick a surprising identity.
    """
    global _credential
    if _credential is None:
        with _cred_lock:
            if _credential is None:
                if os.environ.get("IDENTITY_ENDPOINT"):
                    from azure.identity import ManagedIdentityCredential
                    _credential = ManagedIdentityCredential(
                        client_id=os.environ.get("AZURE_CLIENT_ID") or None)
                else:
                    from azure.identity import AzureCliCredential
                    _credential = AzureCliCredential()
    return _credential


def get_token_provider():
    """Return a zero-arg callable that yields a bearer token for Azure OpenAI.

    Built with ``azure.identity.get_bearer_token_provider``; azure-identity caches
    the token and refreshes it before expiry, so long-running apps never see an
    expired token. Pass it as ``azure_ad_token_provider=`` to AzureOpenAI.
    """
    global _token_provider
    if _token_provider is None:
        from azure.identity import get_bearer_token_provider
        _token_provider = get_bearer_token_provider(get_credential(), SCOPE)
    return _token_provider


def get_bearer_token() -> str:
    """Return a current bearer token string (for raw ``requests`` callers).

    Call it per request (cheap: cached and auto-refreshed); never log it.
    Use as ``headers={"Authorization": "Bearer " + get_bearer_token()}``.
    """
    return get_token_provider()()


def _auth_mode() -> str:
    auth = (os.environ.get("LLM_AUTH") or "identity").strip().lower()
    if auth not in ("identity", "key"):
        raise RuntimeError("dcri_llm: LLM_AUTH must be 'identity' or 'key'")
    return auth


def auth_headers() -> dict:
    """Return auth headers for raw ``requests`` callers, honouring ``LLM_AUTH``.

    ``identity`` (default): ``{"Authorization": "Bearer <token>"}``.
    ``key``: ``{"api-key": AZURE_OPENAI_API_KEY}``. Call per request; never log it.
    """
    if _auth_mode() == "key":
        key = os.environ.get("AZURE_OPENAI_API_KEY")
        if not key:
            raise RuntimeError("dcri_llm: LLM_AUTH=key but AZURE_OPENAI_API_KEY is not set")
        return {"api-key": key}
    return {"Authorization": "Bearer " + get_bearer_token()}


def make_client(*, async_: bool = False, **kw):
    """Return an instrumented ``AzureOpenAI`` (or ``AsyncAzureOpenAI``) client.

    Env: ``AZURE_OPENAI_ENDPOINT`` (required), ``AZURE_OPENAI_API_VERSION``
    (default ``DEFAULT_API_VERSION``), ``LLM_AUTH`` = ``identity`` (default) or
    ``key`` (uses ``AZURE_OPENAI_API_KEY``; only for colleagues without an
    identity). Any ``kw`` is passed to the client constructor and wins.
    """
    from openai import AsyncAzureOpenAI, AzureOpenAI
    kw.setdefault("azure_endpoint", os.environ.get("AZURE_OPENAI_ENDPOINT"))
    kw.setdefault("api_version", os.environ.get("AZURE_OPENAI_API_VERSION") or DEFAULT_API_VERSION)
    if not kw["azure_endpoint"]:
        raise RuntimeError("dcri_llm: set AZURE_OPENAI_ENDPOINT")
    if "api_key" not in kw and "azure_ad_token_provider" not in kw and "azure_ad_token" not in kw:
        auth = _auth_mode()
        if auth == "key":
            key = os.environ.get("AZURE_OPENAI_API_KEY")
            if not key:
                raise RuntimeError("dcri_llm: LLM_AUTH=key but AZURE_OPENAI_API_KEY is not set")
            kw["api_key"] = key
        else:
            kw["azure_ad_token_provider"] = get_token_provider()
    cls = AsyncAzureOpenAI if async_ else AzureOpenAI
    return instrument(cls(**kw))

# ---------------------------------------------------------------------------
# Context: action_id, user, operation
# ---------------------------------------------------------------------------
_action_id = contextvars.ContextVar("dcri_llm_action_id", default=None)
_user = contextvars.ContextVar("dcri_llm_user", default=None)
_operation = contextvars.ContextVar("dcri_llm_operation", default=None)


def _normalize_user(value):
    if not value:
        return "anonymous"
    value = str(value).strip()
    if value.lower().endswith("@duke.edu"):
        value = value[: -len("@duke.edu")]
    return value.lower() or "anonymous"


def set_user(netid):
    """Set the user for events in the current context (``x@duke.edu`` -> ``x``)."""
    return _user.set(_normalize_user(netid))


def current_context() -> dict:
    """Return ``{"action_id", "user", "operation"}`` for the current context."""
    return {"action_id": _action_id.get(), "user": _user.get() or "anonymous",
            "operation": _operation.get()}


class llm_action:
    """Group LLM calls of one user action; usable as ``with``/``async with``/decorator.

    Sets ``operation`` (a fixed identifier such as ``"generate_code"``). Reuses
    the enclosing action_id (e.g. the one the middleware set per request) so a
    request's calls stay grouped; creates a fresh uuid if none is set, or always
    when ``new_action=True``.
    """

    def __init__(self, operation=None, *, new_action=False):
        self.operation = operation
        self.new_action = new_action
        self._tokens = []
        self.action_id = None  # the action_id in effect inside the block

    def __enter__(self):
        self.action_id = (uuid.uuid4().hex if self.new_action or not _action_id.get()
                          else _action_id.get())
        a = _action_id.set(self.action_id)
        o = _operation.set(self.operation if self.operation is not None else _operation.get())
        self._tokens.append((a, o))
        return self

    def __exit__(self, *exc):
        a, o = self._tokens.pop()
        try:
            _operation.reset(o)
            _action_id.reset(a)
        except ValueError:  # reset in a different context; harmless
            pass
        return False

    async def __aenter__(self):
        return self.__enter__()

    async def __aexit__(self, *exc):
        return self.__exit__(*exc)

    def __call__(self, fn):
        op, new = self.operation, self.new_action
        if inspect.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def awrapper(*a, **k):
                with llm_action(op, new_action=new):
                    return await fn(*a, **k)
            return awrapper

        @functools.wraps(fn)
        def wrapper(*a, **k):
            with llm_action(op, new_action=new):
                return fn(*a, **k)
        return wrapper


_PRINCIPAL_HEADER = "x-ms-client-principal-name"


def _trust_easy_auth() -> bool:
    # The header is client-controlled unless ACA Easy Auth is enabled (it strips
    # client-sent copies). Only read it when the app opts in explicitly.
    return (os.environ.get("TRUST_EASY_AUTH_HEADER") or "").strip().lower() in ("1", "true", "yes")


def asgi_middleware(app):
    """ASGI middleware (FastAPI/Starlette): fresh action_id and user per request.

    User comes from the Easy Auth header ``X-MS-CLIENT-PRINCIPAL-NAME`` only when
    ``TRUST_EASY_AUTH_HEADER=1`` (set it only on apps with Easy Auth enabled, which
    strips client-sent copies), ``@duke.edu`` stripped; else ``anonymous``. Apps
    with their own verified login should call ``set_user`` instead. Telemetry only.
    Use ``app.add_middleware(asgi_middleware)`` or ``app = asgi_middleware(app)``.
    """
    async def middleware(scope, receive, send):
        if scope.get("type") not in ("http", "websocket"):
            return await app(scope, receive, send)
        name = None
        for k, v in (scope.get("headers") or ()) if _trust_easy_auth() else ():
            if k.lower() == _PRINCIPAL_HEADER.encode():
                name = v.decode("latin-1")
                break
        a = _action_id.set(uuid.uuid4().hex)
        u = _user.set(_normalize_user(name))
        o = _operation.set(None)
        try:
            return await app(scope, receive, send)
        finally:
            for var, tok in ((_operation, o), (_user, u), (_action_id, a)):
                try:
                    var.reset(tok)
                except ValueError:
                    pass
    return middleware


def wsgi_middleware(wsgi_app):
    """WSGI equivalent (Flask: ``app.wsgi_app = wsgi_middleware(app.wsgi_app)``)."""
    def middleware(environ, start_response):
        ctx = contextvars.copy_context()

        def run():
            _action_id.set(uuid.uuid4().hex)
            _user.set(_normalize_user(
                environ.get("HTTP_X_MS_CLIENT_PRINCIPAL_NAME") if _trust_easy_auth() else None))
            _operation.set(None)
            return wsgi_app(environ, start_response)
        return ctx.run(run)
    return middleware

# ---------------------------------------------------------------------------
# Event building helpers (never raise)
# ---------------------------------------------------------------------------


def _g(obj, name, default=None):
    """Get attribute or dict key; works for SDK objects and raw JSON."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _int(v):
    try:
        return None if v is None else int(v)
    except (TypeError, ValueError):
        return None


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _host(url):
    try:
        url = str(url or "")
        return urlparse(url if "://" in url else "https://" + url).hostname
    except Exception:
        return None


def _is_reasoning(model):
    return bool(model and _REASONING_RE.match(str(model)))


def estimate_cost_usd(model, input_tokens, output_tokens, cached_input_tokens=None):
    """List-price estimate in USD, or None for an unknown model / missing counts."""
    try:
        if model is None or input_tokens is None or output_tokens is None:
            return None
        name = str(model).lower().split("/")[-1]
        name = _DATE_SUFFIX_RE.sub("", name)
        price = PRICES.get(name)
        if price is None:
            return None
        cached = cached_input_tokens or 0
        usd = ((input_tokens - cached) * price[0] + cached * price[1]
               + output_tokens * price[2]) / 1_000_000
        return round(usd, 8)
    except Exception:
        return None


def _text_len(value):
    """Total character length of chat messages / responses input. Lengths only."""
    if value is None:
        return 0
    if isinstance(value, str):
        return len(value)
    if isinstance(value, (list, tuple)):
        return sum(_text_len(v) for v in value)
    if isinstance(value, dict) or hasattr(value, "__dict__"):
        total = 0
        for key in ("content", "text", "input", "output"):
            v = _g(value, key)
            if v is not None and v is not value:
                total += _text_len(v)
        return total
    return 0


def usage_from_chat_json(resp) -> dict:
    """Extract schema-v1 usage fields from a chat.completions response (object or dict)."""
    out = {}
    try:
        usage = _g(resp, "usage")
        out["input_tokens"] = _int(_g(usage, "prompt_tokens"))
        out["output_tokens"] = _int(_g(usage, "completion_tokens"))
        out["cached_input_tokens"] = _int(_g(_g(usage, "prompt_tokens_details"), "cached_tokens"))
        out["reasoning_tokens"] = _int(_g(_g(usage, "completion_tokens_details"), "reasoning_tokens"))
        out["model_returned"] = _g(resp, "model")
        choices = _g(resp, "choices") or []
        first = choices[0] if choices else None
        out["finish_reason"] = _g(first, "finish_reason")
        msg = _g(first, "message")
        content = _g(msg, "content")
        text_len = _text_len(content)
        has_tools = bool(_g(msg, "tool_calls") or _g(msg, "function_call"))
        out["output_chars"] = text_len
        out["empty_output"] = (not has_tools) and not (content and str(content).strip())
    except Exception:
        log.debug("dcri_llm: chat usage parse failed", exc_info=True)
    return out


def usage_from_responses_json(resp) -> dict:
    """Extract schema-v1 usage fields from a Responses API result.

    Accepts the raw JSON dict (``requests.post(...).json()``) or an SDK object.
    ``finish_reason`` is normalized: ``stop`` (completed), ``length`` (incomplete
    because of max_output_tokens), ``content_filter``, else the raw status.
    """
    out = {}
    try:
        usage = _g(resp, "usage")
        out["input_tokens"] = _int(_g(usage, "input_tokens"))
        out["output_tokens"] = _int(_g(usage, "output_tokens"))
        out["cached_input_tokens"] = _int(_g(_g(usage, "input_tokens_details"), "cached_tokens"))
        out["reasoning_tokens"] = _int(_g(_g(usage, "output_tokens_details"), "reasoning_tokens"))
        out["model_returned"] = _g(resp, "model")
        status = _g(resp, "status")
        out["status"] = status
        reason = _g(_g(resp, "incomplete_details"), "reason")
        if status == "completed":
            out["finish_reason"] = "stop"
        elif reason == "max_output_tokens":
            out["finish_reason"] = "length"
        elif reason == "content_filter":
            out["finish_reason"] = "content_filter"
        else:
            out["finish_reason"] = status
        text_len, has_tools = 0, False
        for item in _g(resp, "output") or []:
            kind = _g(item, "type")
            if kind == "message":
                for part in _g(item, "content") or []:
                    if _g(part, "type") in ("output_text", "text"):
                        text = _g(part, "text") or ""
                        if text.strip():
                            text_len += len(text)
            elif kind and kind not in ("reasoning",):
                has_tools = True  # function_call, tool calls, etc.
        sdk_text = _g(resp, "output_text")
        if not text_len and not has_tools and isinstance(sdk_text, str) and sdk_text.strip():
            text_len = len(sdk_text)
        out["output_chars"] = text_len
        out["empty_output"] = not text_len and not has_tools
    except Exception:
        log.debug("dcri_llm: responses usage parse failed", exc_info=True)
    return out

# ---------------------------------------------------------------------------
# Sinks
# ---------------------------------------------------------------------------
_stats = {"emitted": 0, "emit_errors": 0, "blob_queued": 0, "blob_dropped": 0,
          "blob_written": 0, "blob_errors": 0}
_extra_sinks = []


def telemetry_stats() -> dict:
    """Counters: emitted, emit_errors, blob_queued/_dropped/_written/_errors."""
    return dict(_stats)


def add_sink(fn):
    """Register an extra sink ``fn(event_dict)``. Must be fast; errors are swallowed."""
    _extra_sinks.append(fn)
    return fn


def _stdout_sink(event):
    sys.stdout.write(json.dumps(event, separators=(",", ":"), default=str) + "\n")
    sys.stdout.flush()


class _BlobSink:
    """Background thread + bounded queue; drops (and counts) rather than blocks."""

    def __init__(self, account_url, container="telemetry", maxsize=1000):
        self.account_url = account_url
        self.container_name = container
        self.q = queue.Queue(maxsize=maxsize)
        self._container = None
        self._idle = threading.Event()
        self._idle.set()
        self.thread = threading.Thread(target=self._run, name="dcri-llm-blob", daemon=True)
        self.thread.start()

    def put(self, event):
        try:
            self._idle.clear()
            self.q.put_nowait(event)
            _stats["blob_queued"] += 1
        except queue.Full:
            _stats["blob_dropped"] += 1

    def _client(self):
        if self._container is None:
            from azure.storage.blob import BlobServiceClient
            svc = BlobServiceClient(self.account_url, credential=get_credential(),
                                    connection_timeout=5, read_timeout=15)
            c = svc.get_container_client(self.container_name)
            try:
                c.create_container()
            except Exception as exc:  # ResourceExistsError is the normal case
                if type(exc).__name__ != "ResourceExistsError":
                    raise
            self._container = c
        return self._container

    def _append(self, blob_name, data: bytes):
        from azure.core.exceptions import ResourceNotFoundError
        blob = self._client().get_blob_client(blob_name)
        try:
            blob.append_block(data)
        except ResourceNotFoundError:
            try:
                blob.create_append_blob()
            except Exception as exc:
                if type(exc).__name__ != "ResourceExistsError":
                    raise
            blob.append_block(data)

    def _run(self):
        backoff = 1.0
        while True:
            batch = [self.q.get()]
            while len(batch) < 200:
                try:
                    batch.append(self.q.get_nowait())
                except queue.Empty:
                    break
            groups = {}
            for ev in batch:
                day = str(ev.get("ts") or _now_iso())[:10]
                sol = re.sub(r"[^A-Za-z0-9_.-]", "_", str(ev.get("solution") or "unknown"))
                groups.setdefault(f"llm/{sol}/{day}.jsonl", []).append(ev)
            for name, events in groups.items():
                data = "".join(json.dumps(e, separators=(",", ":"), default=str) + "\n"
                               for e in events).encode()
                try:
                    self._append(name, data)
                    _stats["blob_written"] += len(events)
                    backoff = 1.0
                except Exception:
                    _stats["blob_errors"] += 1
                    _stats["blob_dropped"] += len(events)
                    log.debug("dcri_llm: blob append failed", exc_info=True)
                    self._container = None
                    time.sleep(backoff)
                    backoff = min(backoff * 2, 60.0)
            if self.q.empty():
                self._idle.set()

    def flush(self, timeout):
        return self._idle.wait(timeout)


_blob_lock = threading.Lock()
_blob_sink = None
_blob_url_seen = None


def _get_blob_sink():
    global _blob_sink, _blob_url_seen
    url = os.environ.get("TELEMETRY_BLOB_ACCOUNT_URL")
    if not url:
        return None
    if _blob_sink is None or _blob_url_seen != url:
        with _blob_lock:
            if _blob_sink is None or _blob_url_seen != url:
                try:
                    import azure.storage.blob  # noqa: F401  optional dependency
                except ImportError:
                    _blob_url_seen = url
                    return None
                _blob_sink = _BlobSink(url, os.environ.get("TELEMETRY_BLOB_CONTAINER", "telemetry"))
                _blob_url_seen = url
    return _blob_sink


def flush(timeout: float = 5.0) -> bool:
    """Wait up to ``timeout`` s for queued Blob events to be written. True if drained."""
    try:
        sink = _blob_sink
        return True if sink is None else sink.flush(timeout)
    except Exception:
        return False


atexit.register(lambda: flush(2.0))


def _emit(event):
    try:
        _stdout_sink(event)
        _stats["emitted"] += 1
    except Exception:
        _stats["emit_errors"] += 1
    try:
        sink = _get_blob_sink()
        if sink is not None:
            sink.put(event)
    except Exception:
        _stats["emit_errors"] += 1
    for fn in list(_extra_sinks):
        try:
            fn(dict(event))
        except Exception:
            _stats["emit_errors"] += 1

# ---------------------------------------------------------------------------
# Public manual emit
# ---------------------------------------------------------------------------


def _solution_version():
    return (os.environ.get("SOLUTION_VERSION") or os.environ.get("IMAGE_TAG")
            or os.environ.get("CONTAINER_APP_REVISION") or None)


def record_llm_call(**fields) -> dict | None:
    """Emit one schema-v1 ``llm_call`` event. Never raises; returns the event (or None).

    Pass any schema fields (e.g. ``model_requested``, ``api``, ``latency_ms``,
    ``outcome``, plus the output of ``usage_from_responses_json``). Missing
    fields default from env/context; unknown or content-like fields are dropped
    (their names listed in ``dropped_fields``). Derived flags are filled in.
    """
    try:
        ctx = current_context()
        ev = dict.fromkeys(FIELDS)
        ev.update(event="llm_call", schema=SCHEMA_VERSION, ts=_now_iso(),
                  solution=os.environ.get("SOLUTION_NAME") or "unknown",
                  solution_version=_solution_version(), operation=ctx["operation"],
                  action_id=ctx["action_id"] or uuid.uuid4().hex, user=ctx["user"],
                  provider="azure_openai", attempt=1, outcome="ok",
                  price_table=PRICE_TABLE_DATE, lib_version=__version__)
        dropped = []
        for k, v in fields.items():
            if k in FIELDS and k not in ("event", "schema", "lib_version"):
                if v is not None or k in ("operation",):
                    ev[k] = v
            else:
                dropped.append(k)
        if ev["endpoint_host"]:
            ev["endpoint_host"] = _host(ev["endpoint_host"])
        if ev["user"]:
            ev["user"] = _normalize_user(ev["user"])
        model = ev["model_returned"] or ev["model_requested"]
        if fields.get("est_cost_usd") is None:
            ev["est_cost_usd"] = estimate_cost_usd(model, ev["input_tokens"], ev["output_tokens"],
                                                   ev["cached_input_tokens"])
        if ev["est_cost_usd"] is None:
            ev["price_table"] = None
        budget = ev["max_completion_tokens"]
        if ev["budget_below_floor"] is None:
            ev["budget_below_floor"] = bool(
                budget is not None and _int(budget) is not None and _int(budget) < REASONING_BUDGET_FLOOR
                and (_is_reasoning(ev["model_requested"]) or _is_reasoning(ev["model_returned"])))
        if ev["finish_length"] is None:
            ev["finish_length"] = ev["finish_reason"] == "length"
        if dropped:
            ev["dropped_fields"] = sorted(dropped)
        _emit(ev)
        return ev
    except Exception:
        _stats["emit_errors"] += 1
        return None

# ---------------------------------------------------------------------------
# Instrumentation of openai clients
# ---------------------------------------------------------------------------


def _error_fields(exc):
    status = _g(exc, "status_code")
    if status is None:
        status = _g(_g(exc, "response"), "status_code")
    name = type(exc).__name__
    return {"outcome": "timeout" if "timeout" in name.lower() else "error",
            "error_type": name, "http_status": _int(status)}


def _call_fields(api, kwargs, host):
    f = {"api": api, "endpoint_host": host, "model_requested": kwargs.get("model"),
         "streamed": bool(kwargs.get("stream"))}
    try:
        if api == "chat":
            f["max_completion_tokens"] = _int(kwargs.get("max_completion_tokens", kwargs.get("max_tokens")))
            f["reasoning_effort"] = kwargs.get("reasoning_effort")
            f["input_chars"] = _text_len(kwargs.get("messages"))
        else:
            f["max_completion_tokens"] = _int(kwargs.get("max_output_tokens"))
            f["reasoning_effort"] = _g(kwargs.get("reasoning"), "effort")
            f["input_chars"] = _text_len(kwargs.get("input")) + _text_len(kwargs.get("instructions"))
    except Exception:
        pass
    return f


class _StreamState:
    def __init__(self, api, base, t0):
        self.api, self.base, self.t0 = api, base, t0
        self.ttft = None
        self.chars = 0
        self.tools = False
        self.result = {}
        self.done = False

    def see(self, chunk):
        try:
            if self.api == "chat":
                if _g(chunk, "model"):
                    self.result["model_returned"] = _g(chunk, "model")
                usage = _g(chunk, "usage")
                if usage is not None:
                    u = usage_from_chat_json({"usage": usage})
                    for k in ("input_tokens", "output_tokens", "cached_input_tokens", "reasoning_tokens"):
                        self.result[k] = u.get(k)
                for ch in _g(chunk, "choices") or []:
                    delta = _g(ch, "delta")
                    text = _g(delta, "content")
                    if text:
                        if self.ttft is None:
                            self.ttft = int((time.perf_counter() - self.t0) * 1000)
                        self.chars += len(text)
                    if _g(delta, "tool_calls") or _g(delta, "function_call"):
                        self.tools = True
                    if _g(ch, "finish_reason"):
                        self.result["finish_reason"] = _g(ch, "finish_reason")
            else:
                kind = _g(chunk, "type") or ""
                if kind == "response.output_text.delta":
                    if self.ttft is None:
                        self.ttft = int((time.perf_counter() - self.t0) * 1000)
                    self.chars += len(_g(chunk, "delta") or "")
                elif kind.startswith("response.function_call"):
                    self.tools = True
                elif kind in ("response.completed", "response.incomplete", "response.failed"):
                    u = usage_from_responses_json(_g(chunk, "response"))
                    u.pop("output_chars", None)
                    u.pop("empty_output", None)
                    self.result.update({k: v for k, v in u.items() if v is not None})
        except Exception:
            pass

    def finish(self, exc=None):
        if self.done:
            return
        self.done = True
        try:
            f = dict(self.base)
            f.update(self.result)
            f["latency_ms"] = int((time.perf_counter() - self.t0) * 1000)
            f["ttft_ms"] = self.ttft
            f["output_chars"] = self.chars
            if exc is not None:
                f.update(_error_fields(exc))
            else:
                f["empty_output"] = not self.chars and not self.tools
            record_llm_call(**f)
        except Exception:
            pass


class _StreamProxy:
    """Wraps a sync or async stream; emits one event when it ends/closes."""

    def __init__(self, inner, state, ctx):
        self._inner = inner
        self._state = state
        self._ctx = ctx

    def _finish(self, exc=None):
        try:
            self._ctx.run(self._state.finish, exc)
        except Exception:
            pass

    def __iter__(self):
        return self

    def __next__(self):
        try:
            chunk = next(self._inner)
        except StopIteration:
            self._finish()
            raise
        except Exception as exc:
            self._finish(exc)
            raise
        self._state.see(chunk)
        return chunk

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            chunk = await self._inner.__anext__()
        except StopAsyncIteration:
            self._finish()
            raise
        except Exception as exc:
            self._finish(exc)
            raise
        self._state.see(chunk)
        return chunk

    def __enter__(self):
        if hasattr(self._inner, "__enter__"):
            self._inner.__enter__()
        return self

    def __exit__(self, *exc):
        try:
            return self._inner.__exit__(*exc) if hasattr(self._inner, "__exit__") else False
        finally:
            self._finish()

    async def __aenter__(self):
        if hasattr(self._inner, "__aenter__"):
            await self._inner.__aenter__()
        return self

    async def __aexit__(self, *exc):
        try:
            return await self._inner.__aexit__(*exc) if hasattr(self._inner, "__aexit__") else False
        finally:
            self._finish()

    def close(self):
        try:
            return self._inner.close()
        finally:
            self._finish()

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._inner, name)

    def __del__(self):
        try:
            self._finish()
        except Exception:
            pass


def _wrap_create(orig, api, host):
    parse = usage_from_chat_json if api == "chat" else usage_from_responses_json

    @functools.wraps(orig)
    def create(*args, **kwargs):
        t0 = time.perf_counter()
        try:
            base = _call_fields(api, kwargs, host)
        except Exception:
            base = {"api": api}

        def ok(resp):
            try:
                if base.get("streamed"):
                    return _StreamProxy(resp, _StreamState(api, base, t0), contextvars.copy_context())
                f = dict(base)
                f.update(parse(resp))
                f["latency_ms"] = int((time.perf_counter() - t0) * 1000)
                record_llm_call(**f)
            except Exception:
                pass
            return resp

        def fail(exc):
            try:
                f = dict(base)
                f.update(_error_fields(exc))
                f["latency_ms"] = int((time.perf_counter() - t0) * 1000)
                record_llm_call(**f)
            except Exception:
                pass

        try:
            result = orig(*args, **kwargs)
        except Exception as exc:
            fail(exc)
            raise
        if inspect.isawaitable(result):
            async def finish_async():
                try:
                    resp = await result
                except Exception as exc:
                    fail(exc)
                    raise
                return ok(resp)
            return finish_async()
        return ok(result)

    create.__dcri_llm_wrapped__ = True
    return create


def instrument(client):
    """Wrap ``chat.completions.create`` and ``responses.create`` (sync or async) in place.

    Idempotent; returns the same client. ``client.with_options(...)`` /
    ``client.copy(...)`` return instrumented copies too. For token counts on
    streamed chat calls, pass ``stream_options={"include_usage": True}``
    (not added automatically, so the chunk sequence your code sees is unchanged).
    """
    try:
        if getattr(client, "__dcri_llm_instrumented__", False):
            return client
        host = _host(_g(client, "base_url"))
        targets = []
        try:
            targets.append((client.chat.completions, "chat"))
        except Exception:
            pass
        try:
            targets.append((client.responses, "responses"))
        except Exception:
            pass
        for resource, api in targets:
            orig = getattr(resource, "create", None)
            if orig is not None and not getattr(orig, "__dcri_llm_wrapped__", False):
                resource.create = _wrap_create(orig, api, host)
        for name in ("copy", "with_options"):
            orig = getattr(client, name, None)
            if callable(orig):
                def make(orig):
                    @functools.wraps(orig)
                    def copier(*a, **k):
                        return instrument(orig(*a, **k))
                    return copier
                try:
                    setattr(client, name, make(orig))
                except Exception:
                    pass
        client.__dcri_llm_instrumented__ = True
    except Exception:
        log.debug("dcri_llm: instrument failed", exc_info=True)
    return client

# ---------------------------------------------------------------------------
# litellm
# ---------------------------------------------------------------------------


def litellm_metadata() -> dict:
    """Context to pass as ``metadata=`` on litellm calls.

    litellm may run callbacks in another thread, where contextvars are lost;
    ``litellm.completion(..., metadata=litellm_metadata())`` carries them across.
    """
    ctx = current_context()
    return {"dcri_action_id": ctx["action_id"], "dcri_user": ctx["user"],
            "dcri_operation": ctx["operation"]}


def _litellm_fields(kwargs, start_time, end_time):
    kwargs = kwargs or {}
    lp = kwargs.get("litellm_params") or {}
    md = lp.get("metadata") or kwargs.get("metadata") or {}
    op = kwargs.get("optional_params") or {}
    f = {"api": "chat", "model_requested": kwargs.get("model"),
         "provider": kwargs.get("custom_llm_provider") or lp.get("custom_llm_provider") or "litellm",
         "endpoint_host": _host(lp.get("api_base")) if lp.get("api_base") else None,
         "max_completion_tokens": _int(op.get("max_completion_tokens", op.get("max_tokens"))),
         "reasoning_effort": op.get("reasoning_effort"),
         "streamed": bool(kwargs.get("stream") or op.get("stream")),
         "input_chars": _text_len(kwargs.get("messages"))}
    if f["provider"] == "azure":
        f["provider"] = "azure_openai"
    if md.get("dcri_action_id"):
        f["action_id"] = md["dcri_action_id"]
    if md.get("dcri_user"):
        f["user"] = md["dcri_user"]
    if md.get("dcri_operation"):
        f["operation"] = md["dcri_operation"]
    try:
        f["latency_ms"] = int((end_time - start_time).total_seconds() * 1000)
    except Exception:
        pass
    return f


def litellm_callbacks():
    """Return ``(success_callback, failure_callback)`` for litellm.

    ``s, f = litellm_callbacks(); litellm.success_callback.append(s);
    litellm.failure_callback.append(f)``. Pair with ``metadata=litellm_metadata()``.
    """
    def success(kwargs, completion_response, start_time, end_time):
        try:
            f = _litellm_fields(kwargs, start_time, end_time)
            f.update(usage_from_chat_json(completion_response))
            record_llm_call(**f)
        except Exception:
            pass

    def failure(kwargs, completion_response, start_time, end_time):
        try:
            f = _litellm_fields(kwargs, start_time, end_time)
            exc = (kwargs or {}).get("exception") or completion_response
            f.update(_error_fields(exc) if isinstance(exc, BaseException)
                     else {"outcome": "error", "error_type": "LiteLLMError"})
            record_llm_call(**f)
        except Exception:
            pass

    return success, failure
