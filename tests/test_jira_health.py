"""Tests for the shared-Jira-credential health check and banner endpoint.

Offline: every Jira HTTP call is mocked; no real network.
"""

import datetime as dt
import pathlib

import pytest
import requests
from fastapi.testclient import TestClient

from core import jira_health
from core.jira_health import (
    JIRA_DOWN_MESSAGE, JIRA_UNREACHABLE_MESSAGE, JiraAuthError,
)

FAKE_TOKEN = "fake-token-SHOULD-NEVER-APPEAR"
FAKE_EMAIL = "bot@example.org"
TODAY = dt.date(2026, 11, 2)


class _Resp:
    def __init__(self, status_code, text=""):
        self.status_code = status_code
        self.text = text
        self.ok = 200 <= status_code < 300

    def json(self):
        return {}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("JIRA_BASE_URL", "https://example.atlassian.net")
    monkeypatch.setenv("JIRA_EMAIL", FAKE_EMAIL)
    monkeypatch.setenv("JIRA_API_TOKEN", FAKE_TOKEN)
    monkeypatch.delenv("JIRA_TOKEN_EXPIRES_ON", raising=False)
    monkeypatch.setattr(jira_health, "_today", lambda: TODAY)
    jira_health.clear_cache()
    yield
    jira_health.clear_cache()


@pytest.fixture
def jira_answers(monkeypatch):
    """Mock GET /myself. Set .result to a status code or an exception."""
    class Ctl:
        result = 200
        calls = []

    def fake_get(url, **kwargs):
        Ctl.calls.append((url, kwargs))
        if isinstance(Ctl.result, Exception):
            raise Ctl.result
        return _Resp(Ctl.result)

    Ctl.calls = []
    monkeypatch.setattr(jira_health.requests, "get", fake_get)
    return Ctl


# --- status: missing --------------------------------------------------------

@pytest.mark.parametrize("var", ["JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN"])
def test_missing_when_any_var_unset(monkeypatch, jira_answers, var):
    monkeypatch.delenv(var)
    out = jira_health.get_jira_health()
    assert out["status"] == "missing"
    assert out["message"] == JIRA_DOWN_MESSAGE
    assert jira_answers.calls == []          # no network call


def test_placeholder_token_counts_as_missing(monkeypatch, jira_answers):
    monkeypatch.setenv("JIRA_API_TOKEN", "CHANGE_ME")
    assert jira_health.get_jira_health()["status"] == "missing"


# --- status: ok / invalid / unreachable --------------------------------------

def test_ok_calls_myself_with_basic_auth(jira_answers):
    out = jira_health.get_jira_health()
    assert out["status"] == "ok"
    assert out["message"] is None
    assert "days_left" not in out
    assert "checked_at" in out
    url, kwargs = jira_answers.calls[0]
    assert url == "https://example.atlassian.net/rest/api/3/myself"
    assert kwargs["auth"].username == FAKE_EMAIL


@pytest.mark.parametrize("code", [401, 403])
def test_invalid_on_401_403(jira_answers, code):
    jira_answers.result = code
    out = jira_health.get_jira_health()
    assert out["status"] == "invalid"
    assert out["message"] == JIRA_DOWN_MESSAGE


@pytest.mark.parametrize("result", [
    500, 503, 404,
    requests.ConnectionError("boom"),
    requests.Timeout("slow"),
])
def test_unreachable_on_network_error_or_5xx(jira_answers, result):
    jira_answers.result = result
    out = jira_health.get_jira_health()
    assert out["status"] == "unreachable"
    assert out["message"] == JIRA_UNREACHABLE_MESSAGE
    assert "expired" not in out["message"]   # softer wording, not "token expired"


# --- JIRA_TOKEN_EXPIRES_ON ---------------------------------------------------

def test_expiring_soon_within_14_days(monkeypatch, jira_answers):
    monkeypatch.setenv("JIRA_TOKEN_EXPIRES_ON", "2026-11-07")
    out = jira_health.get_jira_health()
    assert out["status"] == "expiring_soon"
    assert out["days_left"] == 5
    assert "expires in 5 days" in out["message"]


def test_expiring_today_is_expiring_soon(monkeypatch, jira_answers):
    monkeypatch.setenv("JIRA_TOKEN_EXPIRES_ON", "2026-11-02")
    out = jira_health.get_jira_health()
    assert out["status"] == "expiring_soon"
    assert out["days_left"] == 0
    assert "expires today" in out["message"]


def test_far_expiry_is_ok_with_days_left(monkeypatch, jira_answers):
    monkeypatch.setenv("JIRA_TOKEN_EXPIRES_ON", "2027-06-01")
    out = jira_health.get_jira_health()
    assert out["status"] == "ok"
    assert out["days_left"] == 211


def test_past_expiry_is_invalid_without_asking_jira(monkeypatch, jira_answers):
    monkeypatch.setenv("JIRA_TOKEN_EXPIRES_ON", "2026-10-31")
    out = jira_health.get_jira_health()
    assert out["status"] == "invalid"
    assert out["days_left"] == -2
    assert jira_answers.calls == []


def test_rejected_token_beats_expiring_soon(monkeypatch, jira_answers):
    monkeypatch.setenv("JIRA_TOKEN_EXPIRES_ON", "2026-11-07")
    jira_answers.result = 401
    assert jira_health.get_jira_health()["status"] == "invalid"


def test_malformed_expiry_is_ignored(monkeypatch, jira_answers):
    monkeypatch.setenv("JIRA_TOKEN_EXPIRES_ON", "next tuesday")
    out = jira_health.get_jira_health()
    assert out["status"] == "ok"
    assert "days_left" not in out


# --- caching -------------------------------------------------------------------

def test_result_is_cached(jira_answers):
    jira_health.get_jira_health()
    jira_health.get_jira_health()
    assert len(jira_answers.calls) == 1
    jira_health.get_jira_health(force=True)
    assert len(jira_answers.calls) == 2


def test_cache_expires_after_ttl(monkeypatch, jira_answers):
    clock = [1000.0]
    monkeypatch.setattr(jira_health.time, "monotonic", lambda: clock[0])
    jira_health.get_jira_health()
    clock[0] += jira_health.CACHE_TTL_SECONDS + 1
    jira_health.get_jira_health()
    assert len(jira_answers.calls) == 2


# --- no secrets leak -------------------------------------------------------------

@pytest.mark.parametrize("result", [200, 401, 503, requests.ConnectionError(FAKE_TOKEN)])
def test_never_returns_or_logs_credentials(jira_answers, capsys, result):
    jira_answers.result = result
    out = jira_health.get_jira_health()
    printed = capsys.readouterr()
    blob = repr(out) + printed.out + printed.err
    assert FAKE_TOKEN not in blob
    assert FAKE_EMAIL not in blob
    assert "Authorization" not in blob


# --- HTTP endpoints -----------------------------------------------------------------

@pytest.fixture
def client():
    from bot.api.main import app
    return TestClient(app)


def test_endpoint_returns_status_json(client, jira_answers):
    jira_answers.result = 401
    r = client.get("/api/v1/jira/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "invalid"
    assert body["message"] == JIRA_DOWN_MESSAGE
    assert set(body) <= {"status", "message", "days_left", "checked_at"}
    assert FAKE_TOKEN not in r.text and FAKE_EMAIL not in r.text


def test_endpoint_missing(client, monkeypatch, jira_answers):
    monkeypatch.delenv("JIRA_API_TOKEN")
    assert client.get("/api/v1/jira/health").json()["status"] == "missing"


def test_health_probe_stays_200_and_never_calls_jira(client, monkeypatch, jira_answers):
    monkeypatch.delenv("JIRA_API_TOKEN")
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["jira"] == "unchecked"
    assert jira_answers.calls == []
    client.get("/api/v1/jira/health")          # populate cache
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["jira"] == "missing"


# --- ticket-creation path: clear message on 401 ---------------------------------------

def _capture_posts(monkeypatch, status=401):
    from core.tools import jira_tool
    posts = []

    def fake_post(url, **kwargs):
        posts.append((url, kwargs))
        return _Resp(status, "Client must be authenticated")

    monkeypatch.setattr(jira_tool.requests, "post", fake_post)
    return posts


def _creds(**over):
    from core.tools.jira_tool import JiraCredentials
    base = dict(base_url="https://example.atlassian.net", email=FAKE_EMAIL,
                api_token=FAKE_TOKEN, project_key="ST")
    base.update(over)
    return JiraCredentials(**base)


def test_create_ticket_401_shared_credential_reverified(monkeypatch, jira_answers):
    """401 with the env credential -> re-check /myself; it says 401 too -> red."""
    from core.tools import jira_tool
    monkeypatch.setenv("JIRA_PROJECT_KEY", "ST")
    _capture_posts(monkeypatch)
    jira_answers.result = 401
    with pytest.raises(JiraAuthError) as ei:
        jira_tool.create_ticket(summary="s", description="d")
    assert str(ei.value) == JIRA_DOWN_MESSAGE
    assert ei.value.shared is True
    assert len(jira_answers.calls) == 1            # forced /myself re-check
    assert jira_health.peek_jira_health()["status"] == "invalid"


def test_shared_401_but_myself_ok_does_not_flip_banner(monkeypatch, jira_answers):
    """A failing request alone must not turn the banner red."""
    from core.tools import jira_tool
    monkeypatch.setenv("JIRA_PROJECT_KEY", "ST")
    _capture_posts(monkeypatch)
    jira_answers.result = 200
    with pytest.raises(JiraAuthError) as ei:
        jira_tool.create_ticket(summary="s", description="d")
    assert ei.value.shared is False
    assert str(ei.value) != JIRA_DOWN_MESSAGE
    assert jira_health.peek_jira_health()["status"] == "ok"


def test_exact_env_credentials_object_counts_as_shared(monkeypatch, jira_answers):
    from core.tools import jira_tool
    _capture_posts(monkeypatch)
    jira_answers.result = 401
    with pytest.raises(JiraAuthError) as ei:
        jira_tool.create_ticket(summary="s", description="d", credentials=_creds())
    assert ei.value.shared is True


@pytest.mark.parametrize("over", [
    {"base_url": "https://evil.example.com"},
    {"email": "someone-else@example.org"},
    {"api_token": "callers-own-token"},
])
def test_non_matching_credentials_never_touch_banner(monkeypatch, jira_answers, over):
    from core.tools import jira_tool
    _capture_posts(monkeypatch)
    with pytest.raises(JiraAuthError) as ei:
        jira_tool.create_ticket(summary="s", description="d", credentials=_creds(**over))
    assert ei.value.shared is False
    assert str(ei.value) != JIRA_DOWN_MESSAGE
    assert jira_answers.calls == []                # no re-check, no cache write
    assert jira_health.peek_jira_health() is None


def test_env_token_never_sent_to_caller_base_url(monkeypatch):
    """Caller-supplied base_url + the env token -> the env base_url is used."""
    from core.tools import jira_tool
    posts = _capture_posts(monkeypatch, status=201)
    creds = _creds(base_url="https://evil.example.com", email="attacker@example.org")
    base, auth, _ = jira_tool._client(creds)
    assert base == "https://example.atlassian.net"
    assert auth.username == FAKE_EMAIL
    with pytest.raises(Exception):
        jira_tool.create_ticket(summary="s", description="d", credentials=creds)
    assert posts and all(u.startswith("https://example.atlassian.net/") for u, _ in posts)
    assert not any("evil" in u for u, _ in posts)


def test_callers_own_token_may_use_callers_base_url():
    from core.tools import jira_tool
    base, auth, _ = jira_tool._client(
        _creds(base_url="https://other.atlassian.net", api_token="callers-own-token"))
    assert base == "https://other.atlassian.net"


def test_create_ticket_401_oauth_says_sign_in_again(monkeypatch):
    from core.tools import jira_tool
    monkeypatch.setattr(jira_tool.requests, "post", lambda *a, **k: _Resp(401))
    creds = jira_tool.JiraCredentials(
        base_url="", email="", api_token="", project_key="ST",
        access_token="oauth-access", cloud_id="cid",
    )
    with pytest.raises(JiraAuthError) as ei:
        jira_tool.create_ticket(summary="s", description="d", credentials=creds)
    assert ei.value.shared is False
    assert "sign in to Jira again" in str(ei.value)
    assert jira_health.peek_jira_health() is None   # per-user problem: no banner


def test_create_ticket_other_errors_unchanged(monkeypatch):
    from core.tools import jira_tool
    monkeypatch.setenv("JIRA_PROJECT_KEY", "ST")
    monkeypatch.setattr(jira_tool.requests, "post", lambda *a, **k: _Resp(500, "oops"))
    with pytest.raises(RuntimeError) as ei:
        jira_tool.create_ticket(summary="s", description="d")
    assert not isinstance(ei.value, JiraAuthError)


def test_enrich_drafts_route_returns_clear_401_message(client, monkeypatch):
    import core.jira_context as jc
    monkeypatch.setattr(jc.requests, "post", lambda *a, **k: _Resp(401))
    monkeypatch.setattr(jc.requests, "get", lambda *a, **k: _Resp(401))
    r = client.post("/api/v1/enrich-drafts",
                    json={"tickets": [{"summary": "x"}], "project_key": "ST"})
    assert r.status_code == 503
    assert r.json()["detail"]["error_code"] == "JIRA_AUTH_EXPIRED"
    assert r.json()["detail"]["message"] == JIRA_DOWN_MESSAGE


# --- frontend wiring (no JS test runner in this repo) ------------------------------------

WEB = pathlib.Path(__file__).resolve().parent.parent / "bot" / "web"


@pytest.mark.parametrize("page", ["index.html", "about.html"])
def test_every_page_loads_the_banner_script(page):
    assert '<script src="jira-banner.js"></script>' in (WEB / page).read_text()


def test_banner_script_is_served(client):
    r = client.get("/jira-banner.js")
    assert r.status_code == 200
    assert "api/v1/jira/health" in r.text
    # Collapse must not be persisted — it has to come back on reload.
    assert "localStorage" not in r.text and "sessionStorage" not in r.text
