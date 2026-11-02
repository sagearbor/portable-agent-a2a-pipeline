"""Offline tests for suggesting / applying updates to existing Jira tickets.

Every Jira HTTP call and the LLM are mocked; nothing touches the network.
"""

import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from core import dcri_llm, ticket_updates as tu
from core.clients.client import LLMOutputError
from core.jql import jql_quote, jql_text_quote, validate_project_key
from core.tools.jira_tool import JiraCredentials

SHARED_TOKEN = "shared-token-NEVER-FOR-WRITES"
OAUTH = JiraCredentials(base_url="https://example.atlassian.net", email="", api_token="",
                        project_key="ADP", access_token="user-oauth-token", cloud_id="cid-123")

TRANSCRIPT = (
    "All for Sage. My Sages AI foundry issues are resolved. I have a new AI doc follow up "
    "project i did MVP and need testing and tickets to see if its going anywhere."
)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("JIRA_BASE_URL", "https://example.atlassian.net")
    monkeypatch.setenv("JIRA_EMAIL", "bot@example.org")
    monkeypatch.setenv("JIRA_API_TOKEN", SHARED_TOKEN)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class _Resp:
    def __init__(self, status_code=200, data=None, text=None):
        self.status_code = status_code
        self._data = data if data is not None else {}
        self.text = text if text is not None else json.dumps(self._data)
        self.ok = 200 <= status_code < 300

    def json(self):
        return self._data


def _issue(key, summary, status="In Progress", itype="Story", parent=None,
           assignee=None, cat="indeterminate"):
    f = {"summary": summary, "status": {"name": status, "statusCategory": {"key": cat}},
         "issuetype": {"name": itype}, "updated": "2026-10-30T10:00:00.000-0400",
         "description": {"type": "doc", "version": 1, "content": [
             {"type": "paragraph", "content": [{"type": "text", "text": "Get Foundry working"}]}]}}
    if parent:
        f["parent"] = {"key": parent, "fields": {"summary": "parent summary"}}
    if assignee:
        f["assignee"] = {"displayName": assignee, "accountId": "acct-" + assignee.split()[0].lower()}
    return {"key": key, "fields": f}


ADP17 = _issue("ADP-17", "AI Foundry - get up and running, debug permission issues",
               parent="ADP-21", assignee="Sage Arbor")
ADP21 = _issue("ADP-21", "AI Foundry - get up and running, debug permission issues",
               status="To Do", itype="Epic", cat="new")

TRANSITIONS = {"transitions": [
    {"id": "11", "name": "To Do", "to": {"name": "To Do", "statusCategory": {"key": "new"}}},
    {"id": "21", "name": "In Progress", "to": {"name": "In Progress", "statusCategory": {"key": "indeterminate"}}},
    {"id": "31", "name": "Done", "to": {"name": "Done", "statusCategory": {"key": "done"}}},
]}


class FakeJira:
    """Records every call; answers search / transitions / user search / writes."""

    def __init__(self, issues=(ADP17, ADP21)):
        self.issues = list(issues)
        self.calls = []

    def _rec(self, method, url, kw):
        self.calls.append(SimpleNamespace(method=method, url=url, **{
            "json": kw.get("json"), "params": kw.get("params"),
            "auth": kw.get("auth"), "headers": kw.get("headers") or {}}))

    def get(self, url, **kw):
        self._rec("GET", url, kw)
        if url.endswith("/transitions"):
            return _Resp(200, TRANSITIONS)
        if "/user/assignable/search" in url:
            return _Resp(200, [{"accountId": "acct-sage", "displayName": "Sage Arbor", "active": True}])
        if "/rest/api/3/issue/" in url:
            return _Resp(200, {"fields": {"description": {"type": "doc", "version": 1, "content": []}}})
        return _Resp(404)

    def post(self, url, **kw):
        self._rec("POST", url, kw)
        if url.endswith("/search/jql"):
            return _Resp(200, {"issues": self.issues})
        return _Resp(201 if url.endswith("/comment") else 204, {})

    def put(self, url, **kw):
        self._rec("PUT", url, kw)
        return _Resp(204, {})

    @property
    def writes(self):
        return [c for c in self.calls
                if c.method in ("PUT",) or (c.method == "POST" and not c.url.endswith("/search/jql"))]


@pytest.fixture
def jira(monkeypatch):
    fake = FakeJira()
    monkeypatch.setattr(tu.requests, "get", fake.get)
    monkeypatch.setattr(tu.requests, "post", fake.post)
    monkeypatch.setattr(tu.requests, "put", fake.put)
    return fake


@pytest.fixture
def no_http(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("HTTP call made where none was allowed")
    for m in ("get", "post", "put", "delete"):
        monkeypatch.setattr(tu.requests, m, boom)


def _chat_response(content, finish_reason="stop"):
    return SimpleNamespace(
        model="gpt-5.4-mini",
        choices=[SimpleNamespace(finish_reason=finish_reason, message=SimpleNamespace(
            content=content, tool_calls=None, function_call=None))],
        usage=SimpleNamespace(prompt_tokens=100, completion_tokens=50,
                              prompt_tokens_details=SimpleNamespace(cached_tokens=0),
                              completion_tokens_details=SimpleNamespace(reasoning_tokens=20)),
    )


class _FakeCompletions:
    def __init__(self, response):
        self.response, self.calls = response, []

    def create(self, **kw):
        self.calls.append(kw)
        return self.response


def _install_llm(monkeypatch, body, finish_reason="stop"):
    comp = _FakeCompletions(_chat_response(body if isinstance(body, str) else json.dumps(body),
                                           finish_reason))
    fake = dcri_llm.instrument(SimpleNamespace(
        base_url="https://example-foundry.openai.azure.com/openai/",
        chat=SimpleNamespace(completions=comp)))
    monkeypatch.setattr(tu, "get_client", lambda: (fake, "gpt-5.4-mini"))
    return comp


LLM_BODY = {
    "updates": [
        {"issue_key": "ADP-17", "action": "comment", "payload": "Sage reports the AI Foundry issues are resolved.",
         "topic": "AI Foundry setup", "evidence": "My Sages AI foundry issues are resolved", "confidence": 0.9},
        {"issue_key": "ADP-17", "action": "transition", "payload": "Done", "topic": "AI Foundry setup",
         "evidence": "My Sages AI foundry issues are resolved", "confidence": 0.85},
        {"issue_key": "ADP-21", "action": "transition", "payload": "resolved", "topic": "AI foundry setup",
         "evidence": "My Sages AI foundry issues are resolved", "confidence": 0.6},
        {"issue_key": "ADP-21", "action": "assign", "payload": "Sage Arbor", "topic": "AI Foundry setup",
         "evidence": "All for Sage", "confidence": 0.75},
        {"issue_key": "ADP-999", "action": "comment", "payload": "invented", "topic": "x",
         "evidence": "All for Sage", "confidence": 0.9},
        {"issue_key": "ADP-17", "action": "transition", "payload": "Shipped to Mars", "topic": "x",
         "evidence": "All for Sage", "confidence": 0.9},
        {"issue_key": "ADP-17", "action": "delete", "payload": "x", "topic": "x",
         "evidence": "All for Sage", "confidence": 0.9},
    ],
    "draft_topics": [{"index": 0, "topic": "AI doc follow-up MVP"}, {"index": 1, "topic": "AI Foundry setup"}],
    "drafts_covered_by_updates": [{"index": 1, "issue_key": "ADP-17"}, {"index": 0, "issue_key": "ADP-999"}],
}
DRAFTS = [{"summary": "Test AI doc follow-up MVP"}, {"summary": "Resolve AI Foundry issues"}]


# ---------------------------------------------------------------------------
# JQL escaping
# ---------------------------------------------------------------------------

def test_jql_quote_escapes_quotes_and_backslashes():
    assert jql_quote('ST" OR project = "SECRET') == '"ST\\" OR project = \\"SECRET"'
    assert jql_quote("a\\b") == '"a\\\\b"'
    assert "\n" not in jql_quote("a\nb")


def test_jql_text_quote_strips_lucene_operators():
    q = jql_text_quote('foo" OR summary ~ "bar* (x)')
    assert q.count('"') == 2 and "*" not in q and "(" not in q


@pytest.mark.parametrize("bad", ['ST" OR project = "X', "ST OR 1=1", "", "1ST", "S T"])
def test_validate_project_key_rejects_injection(bad):
    with pytest.raises(ValueError):
        validate_project_key(bad)


def test_validate_project_key_normalises():
    assert validate_project_key(" adp ") == "ADP"


def test_check_duplicates_rejects_injected_project_key_and_quotes_jql(monkeypatch):
    from bot.api.main import app
    from bot.api.routes import jira_search
    from bot.api.routes import auth_jira

    sent = []
    monkeypatch.setattr(auth_jira, "get_oauth_session",
                        lambda r: {"access_token": "t", "cloud_id": "c"})
    monkeypatch.setattr(jira_search.requests, "post",
                        lambda url, **kw: (sent.append(kw["json"]["jql"]), _Resp(200, {"issues": []}))[1])
    c = TestClient(app)
    r = c.post("/api/v1/jira/check-duplicates", json={
        "summaries": ["Fix login"], "project_key": 'ST" OR project = "SECRET',
        "base_url": "https://example.atlassian.net"})
    assert r.status_code == 400 and not sent

    r = c.post("/api/v1/jira/check-duplicates", json={
        "summaries": ["Fix login page timeout"], "project_key": "st",
        "base_url": "https://example.atlassian.net"})
    assert r.status_code == 200
    assert sent == ['project = "ST" AND summary ~ "Fix login page" AND statusCategory != Done']


def test_jira_context_queries_quote_project_key(monkeypatch):
    from core import jira_context
    sent = []
    monkeypatch.setattr(jira_context.requests, "post",
                        lambda url, **kw: (sent.append(kw["json"]["jql"]), _Resp(200, {"issues": []}))[1])
    jira_context.query_epics('ST" OR project = "SECRET')
    assert sent[0].startswith('project = "ST\\" OR project = \\"SECRET" AND')


# ---------------------------------------------------------------------------
# Candidate fetch
# ---------------------------------------------------------------------------

def test_candidates_oauth_prioritises_current_user_and_caps_fields(jira):
    cands = tu.fetch_candidate_issues(["ADP"], OAUTH)
    jqls = [c.json["jql"] for c in jira.calls if c.url.endswith("/search/jql")]
    assert "currentUser()" in jqls[0] and 'project in ("ADP")' in jqls[0]
    assert "statusCategory != Done" in jqls[1]
    assert {c["key"] for c in cands} == {"ADP-17", "ADP-21"}
    adp17 = next(c for c in cands if c["key"] == "ADP-17")
    assert adp17["mine"] and adp17["parent_key"] == "ADP-21" and adp17["assignee"] == "Sage Arbor"
    assert all(c.json["fields"] == tu._FIELDS for c in jira.calls)
    # OAuth reads go to api.atlassian.com with the bearer token, no basic auth.
    assert all(c.auth is None and c.headers["Authorization"] == "Bearer user-oauth-token"
               for c in jira.calls)


def test_candidates_shared_credential_skips_current_user(jira):
    tu.fetch_candidate_issues(["ADP", "ST"])
    jqls = [c.json["jql"] for c in jira.calls]
    assert len(jqls) == 1 and "currentUser()" not in jqls[0]
    assert 'project in ("ADP", "ST")' in jqls[0]


def test_candidates_reject_bad_project_key(jira):
    with pytest.raises(ValueError):
        tu.fetch_candidate_issues(['ADP") OR ("1'])
    assert not jira.calls


# ---------------------------------------------------------------------------
# Transition mapping + validation
# ---------------------------------------------------------------------------

def _trs():
    return [{"id": t["id"], "name": t["name"], "to_status": t["to"]["name"],
             "to_category": t["to"]["statusCategory"]["key"]} for t in TRANSITIONS["transitions"]]


@pytest.mark.parametrize("target,tid", [("Done", "31"), ("done", "31"), ("resolved", "31"),
                                        ("closed", "31"), ("in progress", "21"), ("Started", "21")])
def test_resolve_transition_maps_names_to_real_ids(target, tid):
    assert tu.resolve_transition(target, _trs())["id"] == tid


def test_resolve_transition_unknown_is_none():
    assert tu.resolve_transition("Shipped to Mars", _trs()) is None
    assert tu.resolve_transition("Done", []) is None


def test_validate_keeps_only_valid_keys_actions_and_transitions(jira):
    cands = tu.fetch_candidate_issues(["ADP"])
    out = tu.validate_proposals(LLM_BODY, cands, len(DRAFTS), TRANSCRIPT)
    kept = [(u["issue_key"], u["action"]) for u in out["updates"]]
    assert kept == [("ADP-17", "comment"), ("ADP-17", "transition"),
                    ("ADP-21", "transition"), ("ADP-21", "assign")]
    reasons = {(r["issue_key"], r["action"]): r["reason"] for r in out["rejected"]}
    assert "candidate" in reasons[("ADP-999", "comment")]
    assert "no available transition" in reasons[("ADP-17", "transition")]
    assert reasons[("ADP-17", "delete")] == "unknown action"

    t17 = out["updates"][1]
    assert t17["transition_id"] == "31" and t17["before"] == "In Progress" and t17["after"] == "Done"
    t21 = out["updates"][2]
    assert t21["transition_id"] == "31" and t21["default_checked"] is False   # 0.6 < 0.7
    assign = out["updates"][3]
    assert assign["account_id"] == "acct-sage" and assign["before"] == "Unassigned"
    assert out["updates"][0]["default_checked"] is True
    # covered drafts only reference issues that actually got an update
    assert out["covered_drafts"] == {1: "ADP-17"}
    assert out["draft_topics"] == {0: "AI doc follow-up MVP", 1: "AI Foundry setup"}


def test_validate_unverified_evidence_is_never_default_checked(jira):
    cands = tu.fetch_candidate_issues(["ADP"])
    body = {"updates": [{"issue_key": "ADP-17", "action": "comment", "payload": "x",
                         "evidence": "a sentence nobody said", "confidence": 0.99}]}
    u = tu.validate_proposals(body, cands, 0, TRANSCRIPT)["updates"][0]
    assert u["evidence_verified"] is False and u["confidence"] <= 0.5 and not u["default_checked"]


def test_validate_skips_transition_to_current_status(jira):
    cands = tu.fetch_candidate_issues(["ADP"])
    body = {"updates": [{"issue_key": "ADP-17", "action": "transition", "payload": "In Progress",
                         "evidence": "All for Sage", "confidence": 0.9}]}
    out = tu.validate_proposals(body, cands, 0, TRANSCRIPT)
    assert not out["updates"] and out["rejected"][0]["reason"] == "already in that status"


def test_validate_dedupes(jira):
    cands = tu.fetch_candidate_issues(["ADP"])
    one = {"issue_key": "ADP-17", "action": "transition", "payload": "Done",
           "evidence": "All for Sage", "confidence": 0.9}
    out = tu.validate_proposals({"updates": [one, dict(one, payload="done")]}, cands, 0, TRANSCRIPT)
    assert len(out["updates"]) == 1


# ---------------------------------------------------------------------------
# LLM step
# ---------------------------------------------------------------------------

def test_propose_uses_reasoning_budget_and_emits_telemetry(monkeypatch, capsys, jira):
    comp = _install_llm(monkeypatch, LLM_BODY)
    cands = tu.fetch_candidate_issues(["ADP"])
    raw = tu.propose_updates(TRANSCRIPT, cands, DRAFTS)
    assert len(raw["updates"]) == len(LLM_BODY["updates"])
    assert comp.calls[0]["max_completion_tokens"] >= 40000
    out = capsys.readouterr().out
    events = [json.loads(l) for l in out.splitlines() if l.startswith('{"event":"llm_call"')]
    assert events and events[0]["operation"] == "suggest_updates"
    assert TRANSCRIPT not in out          # PHI: never log the transcript


def test_propose_truncated_output_raises(monkeypatch, jira):
    _install_llm(monkeypatch, '{"updates": [', finish_reason="length")
    with pytest.raises(LLMOutputError):
        tu.propose_updates(TRANSCRIPT, tu.fetch_candidate_issues(["ADP"]), [])


def test_suggest_end_to_end_never_writes(monkeypatch, jira):
    _install_llm(monkeypatch, LLM_BODY)
    res = tu.suggest_ticket_updates(TRANSCRIPT, ["ADP"], DRAFTS, OAUTH)
    assert {u["issue_key"] for u in res["updates"]} == {"ADP-17", "ADP-21"}
    assert jira.writes == []
    assert [g["topic"] for g in res["topic_groups"]] == ["AI Foundry setup", "AI doc follow-up MVP"]


# ---------------------------------------------------------------------------
# Grouping
# ---------------------------------------------------------------------------

def test_group_by_topic_merges_case_and_puts_other_last():
    updates = [{"id": "u0", "topic": "AI Foundry setup"}, {"id": "u1", "topic": "ai foundry SETUP"},
               {"id": "u2", "topic": "VERDICT judge"}]
    groups = tu.group_by_topic(3, {0: "VERDICT judge", 2: "IRB agent"}, updates)
    assert groups == [
        {"topic": "AI Foundry setup", "draft_indices": [], "update_ids": ["u0", "u1"]},
        {"topic": "VERDICT judge", "draft_indices": [0], "update_ids": ["u2"]},
        {"topic": "IRB agent", "draft_indices": [2], "update_ids": []},
        {"topic": "Other", "draft_indices": [1], "update_ids": []},
    ]


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------

APPLY = [
    {"id": "u0", "issue_key": "ADP-17", "action": "comment", "payload": "Resolved."},
    {"id": "u1", "issue_key": "ADP-17", "action": "transition", "payload": "Done", "transition_id": "31"},
    {"id": "u2", "issue_key": "ADP-21", "action": "assign", "payload": "Sage Arbor", "account_id": "acct-sage"},
    {"id": "u3", "issue_key": "ADP-21", "action": "append_description", "payload": "More scope."},
]


def test_apply_uses_oauth_bearer_never_shared_token(jira):
    res = tu.apply_updates(APPLY, OAUTH, ["ADP"])
    assert [r["status"] for r in res] == ["applied"] * 4
    assert jira.writes, "expected writes"
    for c in jira.calls:
        assert c.auth is None
        assert c.url.startswith("https://api.atlassian.com/ex/jira/cid-123/")
        assert c.headers["Authorization"] == "Bearer user-oauth-token"
        assert SHARED_TOKEN not in json.dumps(c.headers)
    tr = next(c for c in jira.calls if c.method == "POST" and c.url.endswith("/transitions"))
    assert tr.json == {"transition": {"id": "31"}}
    assign = next(c for c in jira.calls if c.url.endswith("/assignee"))
    assert assign.json == {"accountId": "acct-sage"}


@pytest.mark.parametrize("creds", [None, JiraCredentials(
    base_url="https://example.atlassian.net", email="bot@example.org",
    api_token=SHARED_TOKEN, project_key="ADP")])
def test_apply_refuses_shared_or_missing_credentials(creds, no_http):
    with pytest.raises(PermissionError):
        tu.apply_updates(APPLY, creds, ["ADP"])


def test_apply_dry_run_makes_no_http_calls(no_http):
    res = tu.apply_updates(APPLY, None, ["ADP"], dry_run=True)
    assert [r["status"] for r in res] == ["dry_run"] * 4


def test_apply_rejects_issue_outside_selected_project(jira):
    res = tu.apply_updates([{"issue_key": "HR-1", "action": "comment", "payload": "x"}], OAUTH, ["ADP"])
    assert res[0]["status"] == "failed" and not jira.calls


def test_apply_reports_missing_scope_per_update(monkeypatch, jira):
    def post(url, **kw):
        jira._rec("POST", url, kw)
        if url.endswith("/transitions"):
            return _Resp(401, text='{"code":401,"message":"Unauthorized; scope does not match"}')
        return _Resp(201, {})
    monkeypatch.setattr(tu.requests, "post", post)
    res = tu.apply_updates(APPLY[:2], OAUTH, ["ADP"])
    assert res[0]["status"] == "applied"
    assert res[1]["status"] == "failed" and "scope" in res[1]["message"]
    assert "write:issue:jira" in res[1]["message"]


def test_apply_transition_no_longer_available(monkeypatch, jira):
    res = tu.apply_updates([{"issue_key": "ADP-17", "action": "transition",
                             "payload": "Shipped to Mars", "transition_id": "999"}], OAUTH, ["ADP"])
    assert res[0]["status"] == "failed" and "not an available transition" in res[0]["message"]
    assert not [c for c in jira.calls if c.method == "POST"]


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@pytest.fixture
def client():
    from bot.api.main import app
    return TestClient(app)


def _signed_in(monkeypatch, on=True):
    from bot.api.routes import ticket_updates as route
    sess = {"access_token": "user-oauth-token", "cloud_id": "cid-123",
            "site_url": "https://example.atlassian.net"} if on else None
    monkeypatch.setattr(route, "get_oauth_session", lambda r: sess)


def test_route_apply_requires_sign_in(monkeypatch, client, no_http):
    _signed_in(monkeypatch, on=False)
    r = client.post("/api/v1/apply-updates", json={"project_key": "ADP", "updates": APPLY})
    assert r.status_code == 401


def test_route_apply_dry_run_never_writes(monkeypatch, client, no_http):
    _signed_in(monkeypatch, on=True)
    r = client.post("/api/v1/apply-updates",
                    json={"project_key": "ADP", "dry_run": True, "updates": APPLY})
    assert r.status_code == 200
    body = r.json()
    assert body["dry_run"] and body["applied"] == 0
    assert {x["status"] for x in body["results"]} == {"dry_run"}


def test_route_apply_uses_session(monkeypatch, client, jira):
    _signed_in(monkeypatch, on=True)
    r = client.post("/api/v1/apply-updates", json={"project_key": "ADP", "updates": APPLY[:1]})
    assert r.status_code == 200 and r.json()["applied"] == 1
    assert jira.calls[0].headers["Authorization"] == "Bearer user-oauth-token"


def test_route_suggest_read_only_with_links(monkeypatch, client, jira):
    _signed_in(monkeypatch, on=False)      # falls back to the shared READ credential
    _install_llm(monkeypatch, LLM_BODY)
    r = client.post("/api/v1/suggest-updates", json={
        "transcript": TRANSCRIPT, "project_key": "ADP",
        "base_url": "https://example.atlassian.net", "drafts": DRAFTS})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["read_as"] == "shared"
    assert body["updates"][0]["url"] == "https://example.atlassian.net/browse/ADP-17"
    assert body["covered_drafts"] == {"1": "ADP-17"}
    assert jira.writes == []


def test_route_suggest_rejects_bad_project(monkeypatch, client, no_http):
    _signed_in(monkeypatch, on=False)
    r = client.post("/api/v1/suggest-updates", json={
        "transcript": TRANSCRIPT, "project_key": 'ADP" OR project = "X'})
    assert r.status_code == 400


def test_ui_has_updates_section_and_group_toggle():
    import pathlib
    html = (pathlib.Path(__file__).parent.parent / "bot" / "web" / "index.html").read_text()
    assert "Updates to existing tickets" in html
    assert 'id="group-by-toggle"' in html and "Write vs update" in html and "Topic" in html
    assert "/api/v1/suggest-updates" in html and "/api/v1/apply-updates" in html
