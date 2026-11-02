"""Offline tests for the suggest-updates speed work (LLM and Jira mocked).

* Split fix: the split model defaults to the match model (gpt-5.5) and the
  prompt insists on finished / in-testing / blocked work with no draft; the
  real transcript that exposed the bug is a regression case
  (``split_cases`` in tests/regression/update_fixtures.json).
* Two-phase flow: /suggest-updates/prepare (no drafts, started at Analyze
  time) + /suggest-updates/attach-drafts gives the same topics / covered
  drafts as the one-call path; the Jira fetch and the split run in parallel.
* process-transcript stream: result delivered as soon as the job ends (was
  up to 10 s late) and optional progress lines.
"""

import json
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from core import dcri_llm, ticket_updates as tu, update_llm, update_retrieval as ur
from tests.regression import update_regression as reg

FLAGS = ("UPDATES_RETRIEVAL", "UPDATES_VERIFIER", "UPDATES_STRICT_STATUS", "UPDATES_DROP_WEAK")
DATA = reg.load_fixtures()
CASE = {c["name"]: c for c in DATA["split_cases"]}["sage_status_update"]
FX = {f["name"]: f for f in DATA["fixtures"]}["adp_base"]


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for f in FLAGS:
        monkeypatch.setenv(f, "1")
    monkeypatch.setenv("UPDATES_VERIFIER", "0")      # keep the call list short
    for v in ("UPDATES_SPLIT_MODEL", "UPDATES_MATCH_MODEL", "UPDATES_MAP_MODEL"):
        monkeypatch.delenv(v, raising=False)


def _chat(body):
    return SimpleNamespace(model="fake", choices=[SimpleNamespace(
        finish_reason="stop", message=SimpleNamespace(content=json.dumps(body), tool_calls=None,
                                                      function_call=None))],
        usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1,
                              prompt_tokens_details=SimpleNamespace(cached_tokens=0),
                              completion_tokens_details=SimpleNamespace(reasoning_tokens=0)))


ROUTES = {"You split": "split", "You decide whether": "match", "You map new": "map",
          "You are a strict": "verify"}


def _install(monkeypatch, bodies, delay=0.0):
    calls = []

    def create(**kw):
        which = next(v for k, v in ROUTES.items() if kw["messages"][0]["content"].startswith(k))
        calls.append((which, kw))
        if delay:
            time.sleep(delay)
        b = bodies[which]
        return _chat(b(kw) if callable(b) else b)
    fake = dcri_llm.instrument(SimpleNamespace(
        base_url="https://x.openai.azure.com/openai/",
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    monkeypatch.setattr(update_llm, "get_client", lambda: (fake, "gpt-5.4-mini"))
    return calls


ITEMS_5 = [
    {"id": "w1", "title": "AI Foundry issues", "summary": "Sage's AI Foundry issues are resolved.",
     "quote": "My Sages AI foundry issues are resolved", "status_said": "done", "people": ["Sage"]},
    {"id": "w2", "title": "AI doc follow-up MVP", "summary": "MVP built; needs testing.",
     "quote": "I have a new AI doc follow up project i did MVP", "status_said": "in_testing", "people": []},
    {"id": "w3", "title": "IRB agent", "summary": "IRB agent is done and in testing.",
     "quote": "my IRB agent is done and in testing", "status_said": "in_testing", "people": []},
    {"id": "w4", "title": "Manuscript feedback", "summary": "Waiting on Henry and Vincent.",
     "quote": "still waiting on manuscript feedback from Henry and Vincent", "status_said": "blocked",
     "people": []},
    {"id": "w5", "title": "VERDICT LLM-as-a-judge", "summary": "Improving VERDICT.",
     "quote": "Im still working on improvements to VERDICT LLM-as-a-judge", "status_said": "in_progress",
     "people": []},
]
# What gpt-5.4-mini returned 3/3 times: only the topics that had a draft.
ITEMS_BUGGY = [ITEMS_5[1], ITEMS_5[3], ITEMS_5[4]]

MATCH = {"matches": [{"item_id": "w1", "issue_keys": ["ADP-17"]}],
         "updates": [{"item_id": "w1", "issue_key": "ADP-17", "action": "comment",
                      "payload": "AI Foundry permission issues resolved.",
                      "evidence": "My Sages AI foundry issues are resolved", "confidence": 0.9}]}


# ---------------------------------------------------------------------------
# 1. Split fix
# ---------------------------------------------------------------------------

def test_split_model_defaults_to_match_model(monkeypatch):
    assert ur.split_model() == "gpt-5.5"
    monkeypatch.setenv("UPDATES_MATCH_MODEL", "gpt-5.6")
    assert ur.split_model() == "gpt-5.6"
    monkeypatch.setenv("UPDATES_SPLIT_MODEL", "gpt-5.4-mini")
    assert ur.split_model() == "gpt-5.4-mini"


def test_split_call_uses_gpt55_by_default(monkeypatch):
    calls = _install(monkeypatch, {"split": {"items": ITEMS_5, "draft_items": []}})
    items, _ = ur.split_work_items(CASE["transcript"], CASE["drafts"], 200_000)
    assert len(items) == 5
    assert calls[0][1]["model"] == "gpt-5.5"


def test_split_prompt_demands_finished_work_without_a_draft():
    p = ur.SPLIT_PROMPT
    for w in ("finished", "resolved", "in testing", "blocked", "no draft", "never drop an item"):
        assert w in p, w


def test_split_regression_case_is_the_real_transcript():
    assert CASE["transcript"] == FX["transcript"]
    req = {t["label"] for t in CASE["expect_topics"] if t.get("required")}
    assert req == {"AI Foundry", "IRB agent"}
    # the drafts cover only the OTHER topics -- the condition that triggered the bug
    drafts_text = json.dumps(CASE["drafts"]).lower()
    assert "foundry" not in drafts_text and "irb" not in drafts_text


def test_split_scorer_flags_the_buggy_output_and_passes_the_fixed_one():
    bad = reg.split_topics_found(ITEMS_BUGGY, CASE["expect_topics"])
    assert bad["missing_required"] == ["AI Foundry", "IRB agent"]
    good = reg.split_topics_found(ITEMS_5, CASE["expect_topics"])
    assert good["missing_required"] == [] and good["missing_optional"] == [] and len(good["found"]) == 5


def test_split_harness_exit_code(monkeypatch, capsys):
    _install(monkeypatch, {"split": {"items": ITEMS_BUGGY, "draft_items": []}})
    assert reg.run_split(1) == 1
    _install(monkeypatch, {"split": {"items": ITEMS_5, "draft_items": []}})
    assert reg.run_split(1) == 0
    assert "split model: gpt-5.5" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# 2. Two-phase flow
# ---------------------------------------------------------------------------

def _prepare(**kw):
    return tu.suggest_ticket_updates(FX["transcript"], ["ADP"], [], None, candidates=FX["candidates"],
                                     transitions_fn=lambda k, c: FX["transitions"]["*"],
                                     user_fn=lambda k, n, c: None, **kw)


def test_prepare_then_attach_matches_single_call(monkeypatch):
    drafts = FX["drafts"]           # 0 = AI Foundry close-out, 2 = IRB agent, ...
    mapping = [{"index": 0, "item_id": "w1"}, {"index": 1, "item_id": "w2"},
               {"index": 2, "item_id": "w3"}, {"index": 3, "item_id": "w4"}, {"index": 4, "item_id": "w5"}]
    calls = _install(monkeypatch, {"split": {"items": ITEMS_5, "draft_items": mapping},
                                   "match": MATCH, "map": {"draft_items": mapping}})
    single = tu.suggest_ticket_updates(FX["transcript"], ["ADP"], drafts, None, candidates=FX["candidates"],
                                       transitions_fn=lambda k, c: FX["transitions"]["*"],
                                       user_fn=lambda k, n, c: None)
    assert [w for w, _ in calls] == ["split", "match"]
    calls.clear()

    prep = _prepare()
    assert [w for w, _ in calls] == ["split", "match"]
    split_user = calls[0][1]["messages"][1]["content"]
    assert "Close out AI Foundry" not in split_user             # no drafts sent at prepare time
    assert [it["id"] for it in prep["items"]] == ["w1", "w2", "w3", "w4", "w5"]
    assert prep["updates"][0]["item_id"] == "w1" and prep["covered_drafts"] == {}

    att = tu.attach_drafts(prep["items"], prep["updates"], drafts)
    assert [w for w, _ in calls] == ["split", "match", "map"]
    assert calls[-1][1]["model"] == "gpt-5.4-mini"              # map stays on the fast model
    assert att["covered_drafts"] == single["covered_drafts"] == {0: "ADP-17"}
    assert att["draft_topics"] == single["draft_topics"]
    assert att["topic_groups"] == single["topic_groups"]


def test_attach_never_covers_with_a_weak_or_other_item_update(monkeypatch):
    _install(monkeypatch, {"map": {"draft_items": [{"index": 0, "item_id": "w1"},
                                                   {"index": 1, "item_id": "w3"},
                                                   {"index": 2, "item_id": "zz"}]}})
    ups = [{"id": "u0", "issue_key": "ADP-17", "item_id": "w1", "weak_match": True, "topic": "x"},
           {"id": "u1", "issue_key": "ADP-21", "item_id": "w2", "weak_match": False, "topic": "y"}]
    att = tu.attach_drafts(ITEMS_5, ups, [{"summary": "a"}, {"summary": "b"}, {"summary": "c"}])
    assert att["covered_drafts"] == {}
    assert att["draft_topics"] == {0: "AI Foundry issues", 1: "IRB agent"}   # unknown item id dropped


def test_attach_makes_no_llm_call_without_items_or_drafts(monkeypatch):
    calls = _install(monkeypatch, {})
    assert tu.attach_drafts([], [], [{"summary": "a"}])["draft_topics"] == {}
    assert tu.attach_drafts(ITEMS_5, [], [])["topic_groups"] == []
    assert calls == []


def test_candidate_fetch_and_split_run_in_parallel(monkeypatch):
    _install(monkeypatch, {"split": {"items": ITEMS_5, "draft_items": []}, "match": MATCH}, delay=0.4)

    def slow_fetch(keys, creds):
        time.sleep(0.4)
        return FX["candidates"]
    monkeypatch.setattr(tu, "fetch_candidate_issues", slow_fetch)
    t0 = time.perf_counter()
    res = tu.suggest_ticket_updates(FX["transcript"], ["ADP"], [], None,
                                    transitions_fn=lambda k, c: FX["transitions"]["*"],
                                    user_fn=lambda k, n, c: None)
    took = time.perf_counter() - t0
    # fetch 0.4 || split 0.4, then match 0.4 -> ~0.8 s (sequential would be ~1.2 s)
    assert took < 1.1, took
    assert res["work_items"] == 5 and res["updates"][0]["issue_key"] == "ADP-17"


# --- routes -----------------------------------------------------------------

@pytest.fixture
def client():
    from bot.api.main import app
    return TestClient(app)


def _signed_in(monkeypatch, on=True):
    from bot.api.routes import ticket_updates as route
    sess = {"access_token": "user-oauth-token", "cloud_id": "cid-123",
            "site_url": "https://example.atlassian.net"} if on else None
    monkeypatch.setattr(route, "get_oauth_session", lambda r: sess)


@pytest.fixture
def no_jira(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("no Jira HTTP call expected")
    monkeypatch.setattr(tu.requests, "get", boom)
    monkeypatch.setattr(tu.requests, "post", boom)
    monkeypatch.setattr(tu.requests, "put", boom)


def test_route_prepare_and_attach_require_sign_in(monkeypatch, client, no_jira):
    monkeypatch.delenv("SUGGEST_UPDATES_ALLOW_SHARED_READ", raising=False)
    _signed_in(monkeypatch, on=False)
    r = client.post("/api/v1/suggest-updates/prepare", json={"transcript": "x y z", "project_key": "ADP"})
    assert r.status_code == 401
    r = client.post("/api/v1/suggest-updates/attach-drafts", json={"project_key": "ADP"})
    assert r.status_code == 401


def test_route_prepare_returns_items_and_item_ids(monkeypatch, client):
    _signed_in(monkeypatch, on=True)
    _install(monkeypatch, {"split": {"items": ITEMS_5, "draft_items": []}, "match": MATCH})
    seen = {}

    def fetch(keys, creds):
        seen["auth"] = creds.access_token
        return FX["candidates"]
    monkeypatch.setattr(tu, "fetch_candidate_issues", fetch)
    r = client.post("/api/v1/suggest-updates/prepare", json={
        "transcript": FX["transcript"], "project_key": "ADP", "base_url": "https://example.atlassian.net"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert seen["auth"] == "user-oauth-token" and body["read_as"] == "user"
    assert [i["id"] for i in body["items"]] == ["w1", "w2", "w3", "w4", "w5"]
    assert set(body["items"][0]) == {"id", "title", "summary", "status_said"}     # no quotes/people
    assert body["updates"][0]["item_id"] == "w1"
    assert body["updates"][0]["url"] == "https://example.atlassian.net/browse/ADP-17"


def test_route_attach_drafts_no_jira_call(monkeypatch, client, no_jira):
    _signed_in(monkeypatch, on=True)
    _install(monkeypatch, {"map": {"draft_items": [{"index": 0, "item_id": "w1"}]}})
    r = client.post("/api/v1/suggest-updates/attach-drafts", json={
        "project_key": "ADP",
        "items": [{k: it[k] for k in ("id", "title", "summary", "status_said")} for it in ITEMS_5],
        "updates": [{"id": "u0", "issue_key": "ADP-17", "item_id": "w1", "weak_match": False}],
        "drafts": [{"summary": "Close out AI Foundry permission issues"}, {"summary": "Other"}]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["covered_drafts"] == {"0": "ADP-17"}
    assert body["draft_topics"] == {"0": "AI Foundry issues"}
    assert body["topic_groups"][0]["update_ids"] == ["u0"]


def test_route_attach_drafts_caps_sizes(monkeypatch, client, no_jira):
    _signed_in(monkeypatch, on=True)
    r = client.post("/api/v1/suggest-updates/attach-drafts", json={
        "project_key": "ADP", "items": [{"id": f"w{i}"} for i in range(ur.MAX_ITEMS + 1)]})
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# 3. process-transcript stream: prompt delivery + progress lines
# ---------------------------------------------------------------------------

def _fake_job(monkeypatch):
    from bot.api.routes import transcript as tr

    def job(job_id, req):
        j = tr._jobs[job_id]
        j["stage"] = "agent1"
        time.sleep(0.4)
        j.update(stage="agent2", action_items=["Test AI doc MVP", "Fix VERDICT rubric"])
        time.sleep(0.4)
        j.update(stage="agent3", approved_count=2)
        time.sleep(0.4)
        j.update(status="complete", result={"status": "success", "tickets": []})
    monkeypatch.setattr(tr, "_run_pipeline_job", job)


def test_stream_delivers_result_promptly_without_progress(monkeypatch, client):
    _fake_job(monkeypatch)
    t0 = time.perf_counter()
    r = client.post("/api/v1/process-transcript", json={"transcript": "x", "project_key": "ADP",
                                                         "dry_run": True})
    assert time.perf_counter() - t0 < 3.0          # was >= 10 s (slept 10 s between checks)
    assert json.loads(r.text) == {"status": "success", "tickets": []}   # whitespace-only before JSON
    assert "progress" not in r.text


def test_stream_progress_lines(monkeypatch, client):
    _fake_job(monkeypatch)
    r = client.post("/api/v1/process-transcript", json={"transcript": "x", "project_key": "ADP",
                                                         "dry_run": True, "progress": True})
    lines = [json.loads(ln) for ln in r.text.splitlines() if ln.strip()]
    assert lines[-1] == {"status": "success", "tickets": []}
    prog = lines[:-1]
    assert all(p["progress"] for p in prog)
    assert [p["stage"] for p in prog][-2:] == ["agent2", "agent3"]
    assert prog[-1]["action_items"] == ["Test AI doc MVP", "Fix VERDICT rubric"]
    assert prog[-1]["approved_count"] == 2


def test_ui_uses_two_phase_flow_and_progress():
    import pathlib
    html = (pathlib.Path(__file__).parent.parent / "bot" / "web" / "index.html").read_text()
    assert "/api/v1/suggest-updates/prepare" in html and "/api/v1/suggest-updates/attach-drafts" in html
    assert "progress:         true" in html and "readPipelineStream" in html
    assert "Checking your existing" in html and 'id="updates-pending"' in html
    # prepare starts before the pipeline request
    assert html.index("prepareUpdates(transcript).then") < html.index("/api/v1/process-transcript`, {")
