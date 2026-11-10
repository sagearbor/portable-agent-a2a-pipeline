"""Regression tests for "0 updates, 8 duplicate drafts" (live run 2026-11-10).

A status review of MVPs that all had open tickets/epics in ADP produced 0
updates: retrieval paired 16/16 work items with candidates, then the matcher
(gpt-5.5, prompt from piece A, 9c223f1) answered NONE for every item.  The
transcript never contains ticket keys, so existing tickets must be found from
the topic alone.  Everything here is offline: fake LLM (routed by system
prompt) + fake Jira (routed by JQL).  No network, nothing written.
"""

import json
import re
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from core import dcri_llm, status_mapping as sm, ticket_updates as tu, update_llm
from core import update_retrieval as ur
from core.tools.jira_tool import JiraCredentials

OAUTH = JiraCredentials(base_url="https://example.atlassian.net", email="", api_token="",
                        project_key="ADP", access_token="user-oauth-token", cloud_id="cid-123")

TRANSCRIPT = """Sage Arbor: Okay, quick status review of the MVPs, one at a time.
Sage Arbor: Redliner first. The Word doc redliner is deployed and Christoph ran it on two protocols last week. So we can close the deploy and verify ticket.
Sage Arbor: Mock data generator. I'm still working on it, adding the lab and adverse event tables, so keep the ticket open.
Sage Arbor: Dates come out as text, let's open a new bug for the date format.
Sage Arbor: Data analyzer. Testing is delayed, big uploads still time out.
Sage Arbor: Last, the portal. Next we need a usage dashboard, new ticket please."""

FLAGS = ("UPDATES_RETRIEVAL", "UPDATES_VERIFIER", "UPDATES_STRICT_STATUS", "UPDATES_DROP_WEAK",
         "UPDATES_TOPIC_SEARCH")


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for f in FLAGS:                       # the deployed configuration: every piece on
        monkeypatch.setenv(f, "1")
    for v in ("UPDATES_SPLIT_MODEL", "UPDATES_MATCH_MODEL", "UPDATES_VERIFY_MODEL",
              "UPDATES_RETRIEVAL_TOP_K", "UPDATES_RETRIEVAL_MIN_SCORE"):
        monkeypatch.delenv(v, raising=False)


# ---------------------------------------------------------------------------
# Fake Jira: answers /search/jql by what the JQL asks for
# ---------------------------------------------------------------------------

def _issue(key, summary, itype="Story", status="In Progress", cat="indeterminate",
           parent=None, parent_summary=None):
    f = {"summary": summary, "status": {"name": status, "statusCategory": {"key": cat}},
         "issuetype": {"name": itype}, "updated": "2026-11-05T10:00:00.000-0500",
         "assignee": {"displayName": "Sage Arbor", "accountId": "acct-sage"}}
    if parent:
        f["parent"] = {"key": parent, "fields": {"summary": parent_summary}}
    return {"key": key, "fields": f}


E_RED, E_MOCK, E_DA = "Word Doc Redliner", "Mock Data Generator v1", "Data Analyzer Upload + EDA"
EPICS = [_issue("ADP-30", E_RED, "Epic"), _issue("ADP-40", E_MOCK, "Epic"),
         _issue("ADP-50", E_DA, "Epic")]
CHILDREN = {
    "ADP-30": [_issue("ADP-31", "Deploy & verify", parent="ADP-30", parent_summary=E_RED)],
    "ADP-40": [_issue("ADP-41", "Add SDTM domains (LB, AE, VS)", parent="ADP-40", parent_summary=E_MOCK),
               _issue("ADP-42", "Deploy & verify", status="To Do", cat="new",
                      parent="ADP-40", parent_summary=E_MOCK)],
    "ADP-50": [_issue("ADP-52", "Testing delayed", status="In Progress",
                      parent="ADP-50", parent_summary=E_DA)],
}
# What "most recently updated" returns: busy unrelated tickets (the MVP
# tickets were not touched lately, so the old fetch never saw them).
# Some share generic words with the work items (deploy, verify, date, testing,
# usage dashboard), so every item still gets "candidates" - the live signature.
RECENT = [_issue("ADP-4", "SOP Chatbot"), _issue("ADP-12", "Quarterly budget report template"),
          _issue("ADP-17", "AI Foundry - get up and running, debug permission issues"),
          _issue("ADP-80", "Deploy SOP chatbot to Teams and verify"),
          _issue("ADP-86", "Date handling in REDCap export"),
          _issue("ADP-81", "Data governance review for PHI tables"),
          _issue("ADP-83", "Testing plan for Foundry agents, uploads"),
          _issue("ADP-84", "Usage dashboard for budget tracking")]
RECENT_KEYS = {i["key"] for i in RECENT}
TRANSITIONS = {"transitions": [
    {"id": "11", "name": "To Do", "to": {"name": "To Do", "statusCategory": {"key": "new"}}},
    {"id": "21", "name": "In Progress", "to": {"name": "In Progress", "statusCategory": {"key": "indeterminate"}}},
    {"id": "41", "name": "On Hold", "to": {"name": "On Hold", "statusCategory": {"key": "indeterminate"}}},
    {"id": "61", "name": "Done", "to": {"name": "Done", "statusCategory": {"key": "done"}}},
]}


class _Resp:
    def __init__(self, status_code=200, data=None):
        self.status_code, self._data = status_code, data if data is not None else {}
        self.text, self.ok = json.dumps(self._data), 200 <= status_code < 300

    def json(self):
        return self._data


class FakeJira:
    def __init__(self):
        self.jqls, self.writes = [], []

    def post(self, url, **kw):
        if not url.endswith("/search/jql"):
            self.writes.append(url)
            return _Resp(204)
        jql = kw["json"]["jql"]
        self.jqls.append(jql)
        if "currentUser()" in jql:
            return _Resp(200, {"issues": []})
        if "issuetype = Epic" in jql:
            return _Resp(200, {"issues": EPICS})
        m = re.search(r"parent in \(([^)]*)\)", jql)
        if m:
            keys = [k.strip() for k in m.group(1).split(",")]
            return _Resp(200, {"issues": [c for k in keys for c in CHILDREN.get(k, [])]})
        if "text ~" in jql:
            terms = re.findall(r'text ~ "([^"]*)"', jql)
            pool = EPICS + [c for cs in CHILDREN.values() for c in cs]
            hits = [i for i in pool if any(
                all(w in (i["fields"]["summary"] + " " + ((i["fields"].get("parent") or {})
                    .get("fields", {}).get("summary") or "")).lower() for w in t.split())
                for t in terms)]
            return _Resp(200, {"issues": hits})
        return _Resp(200, {"issues": RECENT})

    def get(self, url, **kw):
        if url.endswith("/transitions"):
            return _Resp(200, TRANSITIONS)
        return _Resp(404)

    def put(self, url, **kw):
        self.writes.append(url)
        return _Resp(204)


@pytest.fixture
def jira(monkeypatch):
    fake = FakeJira()
    monkeypatch.setattr(tu.requests, "post", fake.post)
    monkeypatch.setattr(tu.requests, "get", fake.get)
    monkeypatch.setattr(tu.requests, "put", fake.put)
    return fake


# ---------------------------------------------------------------------------
# Fake LLM, routed by system prompt
# ---------------------------------------------------------------------------

ITEMS = [
    {"id": "w1", "title": "Deploy and verify", "subject": "Word doc redliner",
     "summary": "Redliner deployed and tried on two protocols; close the deploy ticket.",
     "quote": "So we can close the deploy and verify ticket", "status_said": "done",
     "people": [], "new_work": False},
    {"id": "w2", "title": "Lab and adverse event tables", "subject": "Mock data generator",
     "summary": "Still adding lab and AE tables; keep the ticket open.",
     "quote": "I'm still working on it, adding the lab and adverse event tables, so keep the ticket open",
     "status_said": "in_progress", "people": [], "new_work": False},
    {"id": "w3", "title": "Date format bug", "subject": "Mock data generator",
     "summary": "Dates come out as text; open a new bug.",
     "quote": "Dates come out as text, let's open a new bug for the date format",
     "status_said": "not_started", "people": [], "new_work": True},
    {"id": "w4", "title": "Testing delayed", "subject": "Data analyzer",
     "summary": "Testing delayed; big uploads time out.",
     "quote": "Testing is delayed, big uploads still time out", "status_said": "blocked",
     "people": [], "new_work": False},
    {"id": "w5", "title": "Usage dashboard", "subject": "portal",
     "summary": "Portal needs a usage dashboard.",
     "quote": "Next we need a usage dashboard, new ticket please", "status_said": "not_started",
     "people": [], "new_work": True},
]
DRAFTS = [{"summary": "Fix date format in Mock Data Generator output"},
          {"summary": "Portal usage dashboard"},
          {"summary": "Keep mock data generator ticket open"}]
DRAFT_ITEMS = [{"index": 0, "item_id": "w3"}, {"index": 1, "item_id": "w5"},
               {"index": 2, "item_id": "w2"}]

MATCH = {
    "matches": [{"item_id": "w1", "issue_keys": ["ADP-31"]},
                {"item_id": "w2", "issue_keys": ["ADP-41"]},
                {"item_id": "w3", "issue_keys": ["ADP-41"]},     # same product: possible duplicate
                {"item_id": "w4", "issue_keys": ["ADP-52"]},
                {"item_id": "w5", "issue_keys": []}],
    "updates": [
        {"item_id": "w1", "issue_key": "ADP-31", "action": "comment",
         "payload": "Redliner deployed and verified on two protocols.",
         "evidence": "So we can close the deploy and verify ticket", "confidence": 0.9},
        {"item_id": "w1", "issue_key": "ADP-31", "action": "transition", "payload": "Done",
         "evidence": "So we can close the deploy and verify ticket", "confidence": 0.85},
        {"item_id": "w2", "issue_key": "ADP-41", "action": "comment",
         "payload": "Still adding the lab and adverse event tables.",
         "evidence": "adding the lab and adverse event tables, so keep the ticket open", "confidence": 0.9},
        {"item_id": "w3", "issue_key": "ADP-41", "action": "comment",      # must be dropped: new work
         "payload": "Dates come out as text.", "evidence": "Dates come out as text", "confidence": 0.6},
        {"item_id": "w4", "issue_key": "ADP-52", "action": "comment",
         "payload": "Testing delayed: big uploads still time out.",
         "evidence": "Testing is delayed, big uploads still time out", "confidence": 0.9},
    ],
}
ALL_NONE = {"matches": [{"item_id": it["id"], "issue_keys": []} for it in ITEMS], "updates": []}


def _verify_yes(kw):
    stmt = kw["messages"][1]["content"]
    n = stmt.count('"index"')
    return {"same_work": "yes", "reason": "Both are about the Mock Data / Redliner / Data Analyzer work.",
            "actions": [{"index": i, "justified": "yes", "reason": "said so"} for i in range(n)]}


def _chat(body):
    return SimpleNamespace(model="fake", choices=[SimpleNamespace(
        finish_reason="stop", message=SimpleNamespace(content=json.dumps(body), tool_calls=None,
                                                      function_call=None))],
        usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1,
                              prompt_tokens_details=SimpleNamespace(cached_tokens=0),
                              completion_tokens_details=SimpleNamespace(reasoning_tokens=0)))


ROUTES = {"You split": "split", "You decide whether": "match", "You map new": "map",
          "You are a strict": "verify"}


def _install(monkeypatch, bodies):
    calls = []

    def create(**kw):
        which = next(v for k, v in ROUTES.items() if kw["messages"][0]["content"].startswith(k))
        calls.append((which, kw))
        b = bodies[which]
        return _chat(b(kw) if callable(b) else b)
    fake = dcri_llm.instrument(SimpleNamespace(
        base_url="https://x.openai.azure.com/openai/",
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    monkeypatch.setattr(update_llm, "get_client", lambda: (fake, "gpt-5.4-mini"))
    return calls


def _bodies(match=MATCH, draft_items=()):
    return {"split": {"items": ITEMS, "draft_items": list(draft_items)}, "match": match,
            "verify": _verify_yes, "map": {"draft_items": DRAFT_ITEMS}}


def _match_payload(calls) -> list[dict]:
    kw = next(kw for w, kw in calls if w == "match")
    user = kw["messages"][1]["content"]
    return json.loads(user.split(">>>\n", 1)[1].rsplit("\n<<<END", 1)[0])


# ---------------------------------------------------------------------------
# 1. Status review about existing tickets -> updates proposed
# ---------------------------------------------------------------------------

def test_topic_search_finds_mvp_tickets_the_recent_fetch_missed(monkeypatch, jira):
    calls = _install(monkeypatch, _bodies())
    res = tu.suggest_ticket_updates(TRANSCRIPT, ["ADP"], [], OAUTH)
    keys = {c["issue_key"] for c in res["updates"]}
    # The "recent" fetch only returned ADP-4/12/17; the MVP tickets came from
    # the topic text search, the open epics and those epics' children.
    assert {"ADP-31", "ADP-41", "ADP-52"} <= keys
    by_src = res["match_summary"]["candidates_by_source"]
    assert by_src.get("recent") == len(RECENT) and by_src.get("topic", 0) + by_src.get("epic", 0) \
        + by_src.get("epic_child", 0) >= 6
    assert any("text ~" in j for j in jira.jqls)
    assert any("parent in (" in j for j in jira.jqls)
    assert jira.writes == []                                       # read-only
    # The matcher saw each item's subject and its product's tickets.
    payload = {b["item_id"]: b for b in _match_payload(calls)}
    assert payload["w1"]["subject"] == "Word doc redliner"
    assert "ADP-31" in [c["key"] for c in payload["w1"]["candidates"]]
    assert "ADP-41" in [c["key"] for c in payload["w2"]["candidates"]]
    assert payload["w3"]["new_work"] is True


def _semantic_matcher(kw):
    """A fake matcher that behaves like a sensible model: an item matches the
    candidates whose summary/parent epic names the item's subject, else none."""
    blocks = json.loads(kw["messages"][1]["content"].split(">>>\n", 1)[1].rsplit("\n<<<END", 1)[0])
    matches, updates = [], []
    for b in blocks:
        subj = b.get("subject", "").lower().split()
        keys = [c["key"] for c in b["candidates"]
                if subj and all(w[:4] in (c["summary"] + " " + c.get("parent", "")).lower() for w in subj)
                and c["type"] != "Epic"]
        matches.append({"item_id": b["item_id"], "issue_keys": keys[:1]})
        if keys and not b.get("new_work"):
            updates.append({"item_id": b["item_id"], "issue_key": keys[0], "action": "comment",
                            "payload": f'{b["subject"]}: {b["summary"]}', "evidence": b["quote"],
                            "confidence": 0.9})
    return {"matches": matches, "updates": updates}


def test_live_signature_recent_only_candidates_give_zero_updates(monkeypatch, jira):
    """Reproduces the 2026-11-10 log signature offline: every work item gets
    SOME candidate (generic-word BM25 overlap), yet 0 updates, because the
    MVP tickets are not among the most recently updated open issues.  With
    the topic search on, the same matcher finds them."""
    monkeypatch.setenv("UPDATES_TOPIC_SEARCH", "0")
    calls = _install(monkeypatch, {**_bodies(), "match": _semantic_matcher})
    old = tu.suggest_ticket_updates(TRANSCRIPT, ["ADP"], [], OAUTH)
    assert old["candidates_considered"] == len(RECENT) and old["updates"] == []
    assert old["match_summary"]["items_matched"] == 0
    assert old["work_items_with_candidates"] == old["work_items"] == len(ITEMS)
    sent = {c["key"] for b in _match_payload(calls) for c in b["candidates"]}
    assert sent <= RECENT_KEYS                                       # only unrelated tickets

    monkeypatch.setenv("UPDATES_TOPIC_SEARCH", "1")
    _install(monkeypatch, {**_bodies(), "match": _semantic_matcher})
    new = tu.suggest_ticket_updates(TRANSCRIPT, ["ADP"], [], OAUTH)
    got = {u["issue_key"] for u in new["updates"]}
    # (this simple fake picks the first ticket under the product's epic)
    assert {"ADP-31", "ADP-52"} <= got and got & {"ADP-41", "ADP-42"}
    assert not got & RECENT_KEYS


def test_status_review_yields_comment_and_close_transition(monkeypatch, jira):
    _install(monkeypatch, _bodies())
    res = tu.suggest_ticket_updates(TRANSCRIPT, ["ADP"], [], OAUTH)
    got = {(u["issue_key"], u["action"]): u for u in res["updates"]}
    # "close the deploy and verify ticket" -> Done (strict mapping); it was
    # rejected as "evidence does not state a status change" before.
    assert got[("ADP-31", "transition")]["after"] == "Done"
    assert ("ADP-31", "comment") in got and ("ADP-41", "comment") in got
    assert ("ADP-52", "comment") in got
    # "keep the ticket open" is not a status change.
    assert ("ADP-41", "transition") not in got
    # The new-bug item never updates an existing ticket.
    assert not any(u.get("item_id") == "w3" for u in res["updates"])
    assert res["match_summary"]["kept"] == len(res["updates"]) >= 4


def test_matcher_prompt_no_longer_biased_to_none():
    p = ur.MATCH_PROMPT
    for phrase in ("most candidates are probably NOT", "Choose NONE whenever you are not sure",
                   "A wrong match is much worse than a missed one",
                   "an unmatched item simply becomes a new ticket"):
        assert phrase not in p, phrase
    assert "duplicate" in p and "subject" in p and "keep the ticket" in p
    assert "NONE" in p                                             # still an explicit option
    assert "subject" in ur.SPLIT_PROMPT and "new_work" in ur.SPLIT_PROMPT


@pytest.mark.parametrize("said,intent", [
    ("So we can close the deploy and verify ticket", "done"),
    ("we can close it now", "done"),
    ("so keep the ticket open", None),
    ("Testing is delayed, big uploads still time out", "hold"),
])
def test_status_review_phrases(said, intent):
    assert sm.intent_of(said) == intent


def test_safeguards_still_drop_a_wrong_ticket(monkeypatch, jira):
    """Rebalancing must not let a match to another item's ticket through."""
    bad = {"matches": [{"item_id": "w1", "issue_keys": ["ADP-4"]}],
           "updates": [{"item_id": "w1", "issue_key": "ADP-4", "action": "comment",
                        "payload": "x", "evidence": "So we can close the deploy and verify ticket",
                        "confidence": 0.9}]}
    _install(monkeypatch, _bodies(match=bad))
    res = tu.suggest_ticket_updates(TRANSCRIPT, ["ADP"], [], OAUTH)
    assert res["updates"] == []
    assert res["match_summary"]["rejected"] >= 1


# ---------------------------------------------------------------------------
# 2. Duplicate drafts are flagged, not silently created
# ---------------------------------------------------------------------------

def test_single_call_flags_possible_duplicate_draft(monkeypatch, jira):
    _install(monkeypatch, _bodies(draft_items=DRAFT_ITEMS))
    res = tu.suggest_ticket_updates(TRANSCRIPT, ["ADP"], DRAFTS, OAUTH)
    # Draft 2 ("keep ticket open") is covered by the ADP-41 update; draft 0
    # (date bug) is a possible duplicate of ADP-41; draft 1 (dashboard) is new.
    assert res["covered_drafts"].get(2) == "ADP-41"
    assert [d["key"] for d in res["possible_duplicates"][0]] == ["ADP-41"]
    assert 1 not in res["possible_duplicates"] and 2 not in res["possible_duplicates"]
    assert res["match_summary"]["possible_duplicate_drafts"] == 1


def test_verifier_veto_removes_duplicate_flag(monkeypatch, jira):
    def verify(kw):
        body = _verify_yes(kw)
        if "ADP-41" in kw["messages"][1]["content"]:
            body["same_work"] = "no"
        return body
    b = _bodies(draft_items=DRAFT_ITEMS)
    b["verify"] = verify
    _install(monkeypatch, b)
    res = tu.suggest_ticket_updates(TRANSCRIPT, ["ADP"], DRAFTS, OAUTH)
    assert not any(u["issue_key"] == "ADP-41" for u in res["updates"])
    # w2's link to ADP-41 was vetoed; w3's (no update, so never verified) stays.
    items = {it["id"]: it for it in res["items"]}
    assert items["w2"]["matched"] == [] and [m["key"] for m in items["w3"]["matched"]] == ["ADP-41"]


@pytest.fixture
def client():
    from bot.api.main import app
    return TestClient(app)


def _signed_in(monkeypatch):
    from bot.api.routes import ticket_updates as route
    monkeypatch.setattr(route, "get_oauth_session", lambda r: {
        "access_token": "user-oauth-token", "cloud_id": "cid-123",
        "site_url": "https://example.atlassian.net"})


def test_two_phase_prepare_then_attach_flags_duplicate(monkeypatch, client, jira):
    _signed_in(monkeypatch)
    _install(monkeypatch, _bodies())
    r = client.post("/api/v1/suggest-updates/prepare", json={
        "transcript": TRANSCRIPT, "project_key": "ADP", "base_url": "https://example.atlassian.net"})
    assert r.status_code == 200, r.text
    prep = r.json()
    assert len(prep["updates"]) >= 4
    items = {it["id"]: it for it in prep["items"]}
    assert [m["key"] for m in items["w3"]["matched"]] == ["ADP-41"]
    # A forged key from another project is dropped by attach-drafts.
    items["w5"]["matched"] = [{"key": "XYZ-1", "summary": "forged"}]
    r = client.post("/api/v1/suggest-updates/attach-drafts", json={
        "project_key": "ADP", "items": list(items.values()),
        "updates": [{"id": u["id"], "issue_key": u["issue_key"], "item_id": u["item_id"],
                     "weak_match": bool(u.get("weak_match"))} for u in prep["updates"]],
        "drafts": DRAFTS})
    assert r.status_code == 200, r.text
    att = r.json()
    assert att["covered_drafts"] == {"2": "ADP-41"}
    assert [d["key"] for d in att["possible_duplicates"]["0"]] == ["ADP-41"]
    assert "1" not in att["possible_duplicates"]                     # forged XYZ-1 ignored
    assert jira.writes == []


# ---------------------------------------------------------------------------
# 3. Zero updates is surfaced with numbers
# ---------------------------------------------------------------------------

def test_zero_matches_reports_counts(monkeypatch, client, jira):
    _signed_in(monkeypatch)
    _install(monkeypatch, _bodies(match=ALL_NONE))
    r = client.post("/api/v1/suggest-updates/prepare", json={
        "transcript": TRANSCRIPT, "project_key": "ADP", "base_url": "https://example.atlassian.net"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["updates"] == []
    s = body["match_summary"]
    assert s["candidates_considered"] >= 9 and s["work_items"] == 5
    assert s["items_matched"] == 0 and s["kept"] == 0 and s["proposed"] == 0


def test_ui_shows_zero_update_summary_instead_of_hiding():
    html = open("bot/web/index.html", encoding="utf-8").read()
    assert 'id="updates-summary"' in html
    assert "Checked ${n} existing" in html
    assert "No changes to existing tickets are proposed." in html
    assert "possible_duplicates" in html and "duplicate_warning" in html
    # The old silent path (hide the whole section when there are 0 updates) is gone.
    assert "if (!state.updates.length) { section.style.display = 'none'; list.innerHTML = ''; return; }" \
        not in html


# ---------------------------------------------------------------------------
# Retrieval plumbing
# ---------------------------------------------------------------------------

def test_search_follows_next_page_token(monkeypatch):
    pages = [{"issues": RECENT[:2], "nextPageToken": "p2"}, {"issues": RECENT[2:], "isLast": True}]
    seen = []

    def post(url, **kw):
        seen.append(kw["json"])
        return _Resp(200, pages[len(seen) - 1])
    monkeypatch.setattr(tu.requests, "post", post)
    out = tu._search('project in ("ADP")', 60, OAUTH)
    assert [i["key"] for i in out] == [i["key"] for i in RECENT]
    assert "nextPageToken" not in seen[0] and seen[1]["nextPageToken"] == "p2"


def test_topic_terms_are_quoted_and_capped():
    items = [{"subject": 'Mock "data" generator) OR project = X', "title": "date bug"}] + \
            [{"subject": f"Product{i}", "title": f"thing {i}"} for i in range(60)]
    terms = tu.topic_terms(items)
    assert len(terms) == tu.MAX_TOPIC_TERMS
    jql = " OR ".join(f"text ~ {tu.jql_text_quote(t)}" for t in terms)
    assert re.findall(r'text ~ "([^"]*)"', jql) == terms             # each term one quoted value
    assert all(re.fullmatch(r"[a-z0-9 ]+", t) for t in terms)       # no quotes/operators left
    assert ") OR project = X" not in jql


def test_topic_search_failure_is_non_fatal(monkeypatch, jira):
    _install(monkeypatch, _bodies())
    real = jira.post

    def flaky(url, **kw):
        if "text ~" in kw["json"]["jql"]:
            return _Resp(500)
        return real(url, **kw)
    monkeypatch.setattr(tu.requests, "post", flaky)
    res = tu.suggest_ticket_updates(TRANSCRIPT, ["ADP"], [], OAUTH)
    # Epics + their children still find the tickets.
    assert {"ADP-31", "ADP-41"} <= {u["issue_key"] for u in res["updates"]}
