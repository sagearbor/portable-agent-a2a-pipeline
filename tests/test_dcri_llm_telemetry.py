"""Offline tests for the dcri_llm wiring: auth, telemetry events, finish_reason.

No network: the LLM client is a fake whose ``create`` returns canned objects,
wrapped by the real ``dcri_llm.instrument``.
"""
import json
from types import SimpleNamespace

import pytest

from core import dcri_llm
from core.agents import agent2_router
from core.clients import client as client_mod
from core.clients.client import LLMOutputError, completion_text

PROMPT_MARKER = "zz-prompt-marker-zz"


def _chat_response(content, finish_reason="stop"):
    return SimpleNamespace(
        model="gpt-5.4-mini-2026-01-01",
        choices=[SimpleNamespace(
            finish_reason=finish_reason,
            message=SimpleNamespace(content=content, tool_calls=None, function_call=None),
        )],
        usage=SimpleNamespace(
            prompt_tokens=120, completion_tokens=80,
            prompt_tokens_details=SimpleNamespace(cached_tokens=0),
            completion_tokens_details=SimpleNamespace(reasoning_tokens=30),
        ),
    )


class _FakeCompletions:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


def _fake_client(response):
    fake = SimpleNamespace(
        base_url="https://example-foundry.openai.azure.com/openai/",
        chat=SimpleNamespace(completions=_FakeCompletions(response)),
    )
    return dcri_llm.instrument(fake)


def _events(capsys):
    out = capsys.readouterr().out
    return out, [json.loads(line) for line in out.splitlines()
                 if line.startswith('{"event":"llm_call"')]


@pytest.fixture(autouse=True)
def _solution(monkeypatch):
    monkeypatch.setenv("SOLUTION_NAME", "sagejirabot")


def test_agent2_call_emits_schema_v1_event_without_prompt_text(monkeypatch, capsys):
    body = json.dumps({"approved": [], "rejected": []})
    fake = _fake_client(_chat_response(body))
    monkeypatch.setattr(agent2_router, "get_client", lambda: (fake, "gpt-5.4-mini"))

    dcri_llm.set_user("testuser@duke.edu")
    with dcri_llm.llm_action("request"):
        agent2_router.run([{"email_id": "e1", "summary": PROMPT_MARKER}])

    out, events = _events(capsys)
    assert len(events) == 1
    ev = events[0]
    assert ev["schema"] == 1
    assert ev["solution"] == "sagejirabot"
    assert ev["operation"] == "agent2_route"
    assert ev["user"] == "testuser"
    assert ev["api"] == "chat"
    assert ev["input_tokens"] == 120
    assert ev["output_tokens"] == 80
    assert ev["reasoning_tokens"] == 30
    assert ev["max_completion_tokens"] == 40000
    assert ev["budget_below_floor"] is False
    assert ev["finish_reason"] == "stop"
    assert ev["endpoint_host"] == "example-foundry.openai.azure.com"
    # No prompt text in the event, and none printed anywhere to stdout.
    assert PROMPT_MARKER not in json.dumps(ev)
    assert PROMPT_MARKER not in out
    # The request reached the model with a >= 40k reasoning budget.
    assert fake.chat.completions.calls[0]["max_completion_tokens"] == 40000


def test_agent2_raises_on_truncated_output(monkeypatch, capsys):
    fake = _fake_client(_chat_response('{"approved": [', finish_reason="length"))
    monkeypatch.setattr(agent2_router, "get_client", lambda: (fake, "gpt-5.4-mini"))
    with pytest.raises(LLMOutputError, match="finish_reason=length"):
        agent2_router.run([{"email_id": "e1"}])
    _, events = _events(capsys)
    assert events[0]["finish_length"] is True


def test_agent2_raises_on_empty_output(monkeypatch):
    fake = _fake_client(_chat_response(None))
    monkeypatch.setattr(agent2_router, "get_client", lambda: (fake, "gpt-5.4-mini"))
    with pytest.raises(LLMOutputError, match="empty output"):
        agent2_router.run([{"email_id": "e1"}])


def test_completion_text_responses_api():
    resp = SimpleNamespace(
        status="incomplete", incomplete_details=SimpleNamespace(reason="max_output_tokens"),
        output=[], output_text="", usage=None, model="gpt-5.4-mini")
    with pytest.raises(LLMOutputError, match="length"):
        completion_text(resp, where="t")
    ok = SimpleNamespace(status="completed", incomplete_details=None, output=[],
                         output_text="hello", usage=None, model="gpt-5.4-mini")
    assert completion_text(ok, where="t") == "hello"


def test_output_token_kwarg_floor():
    assert client_mod.output_token_kwarg("gpt-5.4-mini", 4096) == {"max_output_tokens": 40000}
    assert client_mod.output_token_kwarg("gpt-4o", 4096) == {"max_output_tokens": 4096}


def test_get_client_is_dcri_llm_instrumented(monkeypatch):
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://example.openai.azure.com/")
    monkeypatch.setenv("LLM_AUTH", "key")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "fake-key-for-test")
    client, model = client_mod.get_client()
    assert getattr(client, "__dcri_llm_instrumented__", False)
    assert getattr(client.chat.completions.create, "__dcri_llm_wrapped__", False)
    assert getattr(client.responses.create, "__dcri_llm_wrapped__", False)


def test_azure_auth_mode_switch_is_gone():
    from core.config import settings
    assert not hasattr(settings, "AZURE_AUTH_MODE")
    assert not hasattr(client_mod, "_get_azure_credential")


def test_sso_session_user_reaches_telemetry_context():
    from fastapi import Request
    from fastapi.testclient import TestClient
    from bot.api.main import app
    from bot.api.routes.auth import SSO_USER_SESSION_KEY

    seen = {}

    async def ctx(request: Request):
        seen.clear()
        seen.update(dcri_llm.current_context())
        return {}

    async def login(request: Request):
        request.session[SSO_USER_SESSION_KEY] = "jdoe@duke.edu"
        return {}

    app.add_api_route("/_test_ctx", ctx)
    app.add_api_route("/_test_login", login)
    # Put the test routes ahead of the SPA catch-all.
    app.router.routes.insert(0, app.router.routes.pop())
    app.router.routes.insert(0, app.router.routes.pop())

    c = TestClient(app)
    c.get("/_test_ctx")
    assert seen["user"] == "anonymous"
    c.get("/_test_login")
    c.get("/_test_ctx")
    assert seen["user"] == "jdoe"
    # A client-sent Easy Auth header is ignored; the verified session wins.
    c.get("/_test_ctx", headers={"X-MS-CLIENT-PRINCIPAL-NAME": "spoof@duke.edu"})
    assert seen["user"] == "jdoe"


@pytest.mark.parametrize("content,finish", [('{"x": [', "length"), (None, "stop")])
def test_enrichment_falls_back_to_drafts_on_length_or_empty(monkeypatch, capsys, content, finish):
    from core import jira_context
    fake = _fake_client(_chat_response(content, finish_reason=finish))
    monkeypatch.setattr(jira_context, "get_client", lambda: (fake, "gpt-5.4-mini"))
    drafts = [{"ticket_id": "t1", "summary": PROMPT_MARKER}]
    assert jira_context.enrich_draft_tickets(drafts, [], []) is drafts
    out, events = _events(capsys)
    assert events[0]["operation"] == "enrich_drafts"
    assert events[0]["finish_reason"] == finish
    assert PROMPT_MARKER not in out
