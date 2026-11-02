"""Offline tests for the suggest-updates quality pieces (A-D), LLM mocked.

Each piece has an env switch read at request time; these tests switch the
piece under test ON (and say so), so each deterministic rule is pinned.
The original behaviour (all switches off) is pinned in test_ticket_updates.py.
"""

import json
from types import SimpleNamespace

import pytest

from core import dcri_llm, status_mapping as sm, ticket_updates as tu, update_llm, update_retrieval as ur, update_verifier as uv

ALL_FLAGS = ("UPDATES_RETRIEVAL", "UPDATES_VERIFIER", "UPDATES_STRICT_STATUS")

TRANSCRIPT = (
    "All for Sage. My Sages AI foundry issues are resolved. I have a new AI doc follow up "
    "project i did MVP and need testing and tickets to see if its going anywhere. my IRB "
    "agent is done and in testing, still waiting on manuscript feedback from Henry and "
    "Vincent (chistoph already redlined). Im still working on improvements to VERDICT "
    "LLM-as-a-judge, haveing to fix spiked documents to fit rubric and measure if has rule "
    "content vs placement/format."
)


@pytest.fixture(autouse=True)
def _flags(monkeypatch):
    for f in ALL_FLAGS:
        monkeypatch.setenv(f, "0")


def _cand(key, summary, status="In Progress", itype="Story", desc="", parent=None,
          parent_summary=None, assignee=None, cat="indeterminate"):
    return {"key": key, "type": itype, "summary": summary, "status": status,
            "status_category": cat, "parent_key": parent, "parent_summary": parent_summary,
            "assignee": assignee, "assignee_account_id": ("acct-" + assignee.split()[0].lower())
            if assignee else None, "reporter": None, "updated": "2026-10-30",
            "description_excerpt": desc, "mine": False}


FOUNDRY = "AI Foundry - get up and running, debug permission issues"
CANDS = [
    _cand("ADP-4", "SOP Chatbot", desc="Chatbot answering questions about DCRI standard operating procedures."),
    _cand("ADP-17", FOUNDRY, parent="ADP-21", parent_summary=FOUNDRY, assignee="Sage Arbor"),
    _cand("ADP-21", FOUNDRY, itype="Epic"),
    _cand("ADP-12", "Quarterly budget report template"),
]

TRS = [
    {"id": "11", "name": "To Do", "to_status": "To Do", "to_category": "new"},
    {"id": "21", "name": "In Progress", "to_status": "In Progress", "to_category": "indeterminate"},
    {"id": "41", "name": "On Hold", "to_status": "On Hold", "to_category": "indeterminate"},
    {"id": "51", "name": "Cancel", "to_status": "Cancelled", "to_category": "done"},
    {"id": "61", "name": "Close", "to_status": "Closed", "to_category": "done"},
]


def _trs_fn(key, creds):
    return TRS


def _user_fn(key, name, creds):
    return {"account_id": "acct-sage", "display_name": "Sage Arbor"} if "sage" in name.lower() else None


# ---------------------------------------------------------------------------
# Fake LLM: answers by which prompt it was given
# ---------------------------------------------------------------------------

def _chat(content, finish="stop"):
    return SimpleNamespace(
        model="fake",
        choices=[SimpleNamespace(finish_reason=finish, message=SimpleNamespace(
            content=content, tool_calls=None, function_call=None))],
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5,
                              prompt_tokens_details=SimpleNamespace(cached_tokens=0),
                              completion_tokens_details=SimpleNamespace(reasoning_tokens=1)))


class Router:
    """routes: {"split"|"match"|"verify"|"legacy": dict | callable(kw) -> dict}"""

    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def _which(self, system):
        if system.startswith("You split"):
            return "split"
        if system.startswith("You decide whether pieces"):
            return "match"
        if system.startswith("You are a strict reviewer"):
            return "verify"
        return "legacy"

    def create(self, **kw):
        which = self._which(kw["messages"][0]["content"])
        self.calls.append((which, kw))
        body = self.routes[which]
        body = body(kw) if callable(body) else body
        return _chat(body if isinstance(body, str) else json.dumps(body))


def _install(monkeypatch, routes):
    r = Router(routes)
    fake = dcri_llm.instrument(SimpleNamespace(
        base_url="https://example-foundry.openai.azure.com/openai/",
        chat=SimpleNamespace(completions=r)))
    monkeypatch.setattr(update_llm, "get_client", lambda: (fake, "gpt-5.4-mini"))
    monkeypatch.setattr(tu, "get_client", lambda: (fake, "gpt-5.4-mini"))
    return r


SPLIT = {
    "items": [
        {"id": "w1", "title": "AI Foundry issues", "summary": "Sage's AI Foundry issues are resolved.",
         "quote": "My Sages AI foundry issues are resolved", "status_said": "done", "people": ["Sage"]},
        {"id": "w2", "title": "AI doc follow-up MVP", "summary": "New AI doc follow-up project MVP needs testing.",
         "quote": "I have a new AI doc follow up project i did MVP", "status_said": "in_testing", "people": []},
        {"id": "w3", "title": "IRB agent", "summary": "IRB agent is done and in testing.",
         "quote": "my IRB agent is done and in testing", "status_said": "in_testing", "people": []},
    ],
    "draft_items": [{"index": 0, "item_id": "w2"}, {"index": 1, "item_id": "w3"}, {"index": 2, "item_id": "w1"}],
}
DRAFTS = [{"summary": "Test AI doc follow-up MVP"}, {"summary": "IRB agent testing"},
          {"summary": "Close AI Foundry permission issues"}]


def _suggest(**kw):
    return tu.suggest_ticket_updates(TRANSCRIPT, ["ADP"], DRAFTS, None, candidates=CANDS,
                                     transitions_fn=_trs_fn, user_fn=_user_fn, **kw)


# ---------------------------------------------------------------------------
# Flags
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("val,on", [(None, True), ("", True), ("1", True), ("true", True),
                                    ("0", False), ("false", False), ("OFF", False), ("no", False)])
def test_flag_on_reads_env_at_call_time(monkeypatch, val, on):
    if val is None:
        monkeypatch.delenv("UPDATES_X", raising=False)
    else:
        monkeypatch.setenv("UPDATES_X", val)
    assert update_llm.flag_on("UPDATES_X") is on


@pytest.mark.parametrize("model,ok", [("gpt-5.4-mini", True), ("gpt-4o", True), ("gpt-5.5", False),
                                      ("gpt-5.6-sol", False), ("gpt-5-nano", False), ("o3", False)])
def test_temperature_only_sent_to_models_that_accept_it(model, ok):
    assert update_llm._accepts_temperature(model) is ok


def test_call_json_retries_without_temperature_and_keeps_budget(monkeypatch):
    calls = []

    def create(**kw):
        calls.append(kw)
        if "temperature" in kw:
            raise RuntimeError("Unsupported value: 'temperature' does not support 0.2")
        return _chat('{"ok": true}')
    fake = dcri_llm.instrument(SimpleNamespace(base_url="https://x.openai.azure.com/openai/",
                                               chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    monkeypatch.setattr(update_llm, "get_client", lambda: (fake, "gpt-5.4-mini"))
    assert update_llm.call_json("s", "u", model="gpt-5.4-mini", operation="t") == {"ok": True}
    assert "temperature" in calls[0] and "temperature" not in calls[1]
    assert all(c["max_completion_tokens"] >= 40000 for c in calls)


# ---------------------------------------------------------------------------
# A. Retrieval
# ---------------------------------------------------------------------------

def test_tokens_keep_acronyms_and_drop_stop_words():
    t = ur.tokens("My IRB agent is done and in testing, still waiting")
    assert "irb" in t and "agent" in t and "testi" in t
    assert not {"done", "still", "and", "my"} & set(t)


def test_rank_candidates_needs_shared_word_and_threshold():
    items = SPLIT["items"]
    ranked = ur.rank_candidates(items, CANDS, threshold=0.5, k=5)
    assert {c["key"] for c in ranked["w1"]} == {"ADP-17", "ADP-21"}   # "foundry"
    assert ranked["w3"] == []        # IRB agent shares nothing with SOP Chatbot / budget
    assert all("found" in c["shared"] for c in ranked["w1"])
    # threshold is enforced
    assert ur.rank_candidates(items, CANDS, threshold=99, k=5)["w1"] == []


def test_bm25_prefers_rarer_more_specific_overlap():
    cands = [_cand("X-1", "Testing infrastructure for CI"), _cand("X-2", "IRB agent for protocol review"),
             _cand("X-3", "Budget"), _cand("X-4", "Hiring plan")]
    r = ur.rank_candidates([{"id": "w", "title": "IRB agent", "summary": "", "quote": "IRB agent in testing"}],
                           cands, threshold=0.0, k=5)["w"]
    assert [c["key"] for c in r][:1] == ["X-2"]


def test_retrieval_only_sends_items_with_candidates_and_only_their_candidates(monkeypatch):
    monkeypatch.setenv("UPDATES_RETRIEVAL", "1")
    r = _install(monkeypatch, {"split": SPLIT, "match": {"matches": [], "updates": []}})
    _suggest()
    match_kw = next(kw for w, kw in r.calls if w == "match")
    sent = match_kw["messages"][1]["content"]
    assert "w1" in sent and "ADP-17" in sent and "ADP-21" in sent
    assert "ADP-4" not in sent and "ADP-12" not in sent and '"w3"' not in sent
    assert "NONE" in match_kw["messages"][0]["content"]          # explicit "none of these"


def test_retrieval_skips_match_call_when_nothing_retrieved(monkeypatch):
    monkeypatch.setenv("UPDATES_RETRIEVAL", "1")
    split = {"items": [SPLIT["items"][2]], "draft_items": [{"index": 1, "item_id": "w3"}]}
    r = _install(monkeypatch, {"split": split})
    res = _suggest()
    assert [w for w, _ in r.calls] == ["split"]
    assert res["updates"] == [] and res["covered_drafts"] == {}
    assert res["draft_topics"] == {1: "IRB agent"}


def test_retrieval_drops_proposal_for_ticket_outside_item_candidates(monkeypatch):
    """The original bug: IRB news attached to ADP-4 (SOP Chatbot)."""
    monkeypatch.setenv("UPDATES_RETRIEVAL", "1")
    match = {"matches": [{"item_id": "w1", "issue_keys": ["ADP-17"]}],
             "updates": [
                 {"item_id": "w1", "issue_key": "ADP-17", "action": "comment",
                  "payload": "AI Foundry issues resolved.", "evidence": "My Sages AI foundry issues are resolved",
                  "confidence": 0.9},
                 {"item_id": "w1", "issue_key": "ADP-4", "action": "comment",
                  "payload": "The IRB agent is done and in testing", "evidence": "my IRB agent is done and in testing",
                  "confidence": 0.95},
                 {"item_id": "w1", "issue_key": "ADP-21", "action": "comment",   # matcher said only ADP-17
                  "payload": "x y z", "evidence": "My Sages AI foundry issues are resolved", "confidence": 0.9},
             ]}
    _install(monkeypatch, {"split": SPLIT, "match": match})
    res = _suggest()
    assert [(u["issue_key"], u["action"]) for u in res["updates"]] == [("ADP-17", "comment")]
    reasons = [x["reason"] for x in res["rejected"] if x["issue_key"] in ("ADP-4", "ADP-21")]
    assert len(reasons) == 2 and all("candidates" in r for r in reasons)
    u = res["updates"][0]
    assert u["item_id"] == "w1" and u["topic"] == "AI Foundry issues"
    assert res["covered_drafts"] == {2: "ADP-17"}
    assert res["draft_topics"][0] == "AI doc follow-up MVP"
    assert res["work_items"] == 3


def test_retrieval_off_uses_single_legacy_call(monkeypatch):
    r = _install(monkeypatch, {"legacy": {"updates": []}})
    _suggest()
    assert [w for w, _ in r.calls] == ["legacy"]


# ---------------------------------------------------------------------------
# B. Stronger matcher + verifier
# ---------------------------------------------------------------------------

MATCH_OK = {"matches": [{"item_id": "w1", "issue_keys": ["ADP-17", "ADP-21"]}],
            "updates": [
                {"item_id": "w1", "issue_key": "ADP-17", "action": "comment",
                 "payload": "AI Foundry issues are resolved.",
                 "evidence": "My Sages AI foundry issues are resolved", "confidence": 0.9},
                {"item_id": "w1", "issue_key": "ADP-17", "action": "transition", "payload": "Closed",
                 "evidence": "My Sages AI foundry issues are resolved", "confidence": 0.8},
                {"item_id": "w1", "issue_key": "ADP-21", "action": "comment",
                 "payload": "AI Foundry issues are resolved.",
                 "evidence": "My Sages AI foundry issues are resolved", "confidence": 0.7},
            ]}


def _verify_by_key(table):
    """table: issue_key -> verifier JSON (or Exception to raise)."""
    def fn(kw):
        user = kw["messages"][1]["content"]
        for key, body in table.items():
            if f'"key": "{key}"' in user:
                if isinstance(body, Exception):
                    raise body
                return body
        raise AssertionError("unexpected pair")
    return fn


def test_models_default_to_strongest_and_are_env_switchable(monkeypatch):
    monkeypatch.delenv("UPDATES_MATCH_MODEL", raising=False)
    monkeypatch.delenv("UPDATES_VERIFY_MODEL", raising=False)
    assert uv.match_model() == "gpt-5.5" and uv.verify_model() == "gpt-5.5"
    monkeypatch.setenv("UPDATES_MATCH_MODEL", "gpt-5.4-mini")
    assert uv.match_model() == "gpt-5.4-mini" and ur.match_model() == "gpt-5.4-mini"


def test_verifier_drops_no_pairs_and_unjustified_actions(monkeypatch):
    monkeypatch.setenv("UPDATES_RETRIEVAL", "1")
    monkeypatch.setenv("UPDATES_VERIFIER", "1")
    yes_both = {"same_work": "yes", "reason": "Both are about the AI Foundry permission issues.",
                "actions": [{"index": 0, "justified": "yes", "reason": "accurate"},
                            {"index": 1, "justified": "no", "reason": "epic not said done"}]}
    r = _install(monkeypatch, {"split": SPLIT, "match": MATCH_OK, "verify": _verify_by_key({
        "ADP-17": yes_both,
        "ADP-21": {"same_work": "no", "reason": "epic", "actions": []}})})
    res = _suggest()
    kept = [(u["issue_key"], u["action"]) for u in res["updates"]]
    assert kept == [("ADP-17", "comment")]
    assert res["updates"][0]["verified"] is True
    assert "AI Foundry" in res["updates"][0]["verifier_reason"]
    reasons = {(x["issue_key"], x["action"]): x["reason"] for x in res["rejected"]}
    assert reasons[("ADP-17", "transition")] == "verifier: action not justified"
    assert reasons[("ADP-21", "comment")] == "verifier: not the same work"
    verify_calls = [kw for w, kw in r.calls if w == "verify"]
    assert len(verify_calls) == 2                        # one call per (item, ticket) pair
    assert all(kw["max_completion_tokens"] >= 40000 and "temperature" not in kw for kw in verify_calls)
    assert all(kw["model"] == "gpt-5.5" for kw in verify_calls)


def test_verifier_fails_closed_and_counts_errors(monkeypatch):
    monkeypatch.setenv("UPDATES_RETRIEVAL", "1")
    monkeypatch.setenv("UPDATES_VERIFIER", "1")
    _install(monkeypatch, {"split": SPLIT, "match": MATCH_OK, "verify": _verify_by_key({
        "ADP-17": RuntimeError("boom"), "ADP-21": RuntimeError("boom")})})
    res = _suggest()
    assert res["updates"] == [] and res["verifier_errors"] == 2
    assert {x["reason"] for x in res["rejected"]} >= {"verifier unavailable (fail closed)"}
    assert res["covered_drafts"] == {}       # nothing survived -> no draft is hidden


def test_verifier_off_keeps_matcher_output(monkeypatch):
    monkeypatch.setenv("UPDATES_RETRIEVAL", "1")
    r = _install(monkeypatch, {"split": SPLIT, "match": MATCH_OK})
    res = _suggest()
    assert not [w for w, _ in r.calls if w == "verify"]
    assert not any(u.get("verified") for u in res["updates"])
    assert ("ADP-17", "comment") in [(u["issue_key"], u["action"]) for u in res["updates"]]


def test_verifier_sees_statement_not_whole_transcript(monkeypatch):
    monkeypatch.setenv("UPDATES_RETRIEVAL", "1")
    monkeypatch.setenv("UPDATES_VERIFIER", "1")
    yes = {"same_work": "yes", "reason": "AI Foundry", "actions": [
        {"index": i, "justified": "yes"} for i in range(3)]}
    r = _install(monkeypatch, {"split": SPLIT, "match": MATCH_OK,
                               "verify": _verify_by_key({"ADP-17": yes, "ADP-21": yes})})
    _suggest()
    for w, kw in r.calls:
        if w == "verify":
            assert "VERDICT" not in kw["messages"][1]["content"]
            assert "JIRA_TICKET_DATA (untrusted" in kw["messages"][1]["content"]


# ---------------------------------------------------------------------------
# C. Strict status mapping
# ---------------------------------------------------------------------------

# The real ADP workflow shape: no status literally named "Done".
ADP_TRS = TRS
DONE_ONLY_CANCEL = [t for t in TRS if t["to_status"] != "Closed"]
WITH_TESTING = TRS + [{"id": "71", "name": "Send to testing", "to_status": "In Testing",
                       "to_category": "indeterminate"}]


@pytest.mark.parametrize("evidence,intent", [
    ("My Sages AI foundry issues are resolved", "done"),
    ("my IRB agent is done and in testing", "testing"),       # testing beats done
    ("still waiting on manuscript feedback from Henry", "hold"),
    ("we dropped the dashboard idea", "cancel"),
    ("Im still working on improvements to VERDICT", "progress"),
    ("I have a new AI doc follow up project", None),
    ("did MVP and need testing and tickets", None),           # "needs testing" is not "in testing"
])
def test_intent_from_what_was_said(evidence, intent):
    assert sm.intent_of(evidence) == intent


@pytest.mark.parametrize("requested,evidence,trs,expect", [
    # the ADP-17 bug: "Done" requested, no "Done" status -> Closed, never Cancelled
    ("Done", "My Sages AI foundry issues are resolved", ADP_TRS, "Closed"),
    ("Cancelled", "My Sages AI foundry issues are resolved", ADP_TRS, "Closed"),
    # only Cancelled in the done category -> no transition at all
    ("Done", "My Sages AI foundry issues are resolved", DONE_ONLY_CANCEL, None),
    # ADP-4 bug: On Hold requested from "done and in testing" -> no Testing status -> none
    ("On Hold", "my IRB agent is done and in testing", ADP_TRS, None),
    ("Done", "my IRB agent is done and in testing", ADP_TRS, None),
    ("On Hold", "my IRB agent is done and in testing", WITH_TESTING, "In Testing"),
    # On Hold only when the statement says waiting/blocked/paused
    ("On Hold", "still waiting on manuscript feedback from Henry", ADP_TRS, "On Hold"),
    ("On Hold", "Im still working on improvements to VERDICT", ADP_TRS, "In Progress"),
    # Cancelled only when the statement says dropped/cancelled
    ("Cancelled", "we dropped the dashboard idea", ADP_TRS, "Cancelled"),
    ("Closed", "we dropped the dashboard idea", ADP_TRS, "Cancelled"),
    ("Done", "I have a new AI doc follow up project", ADP_TRS, None),
])
def test_resolve_strict(requested, evidence, trs, expect):
    tr, why = sm.resolve_strict(requested, evidence, trs)
    assert (tr["to_status"] if tr else None) == expect
    if expect is None:
        assert why


@pytest.mark.parametrize("name", ["Cancelled", "Canceled", "Won't Do", "Rejected", "Duplicate",
                                  "Abandoned", "Declined"])
def test_cancel_type_never_chosen_for_done(name):
    trs = [{"id": "1", "name": name, "to_status": name, "to_category": "done"}]
    assert sm.resolve_strict("Done", "the work is finished now", trs)[0] is None


def test_legacy_resolver_still_picks_cancelled_when_flag_off():
    """Documents the old bug that UPDATES_STRICT_STATUS=0 brings back."""
    assert tu.resolve_transition("Done", ADP_TRS)["to_status"] == "Cancelled"


def _validate_transition(payload, evidence, trs=ADP_TRS, status="In Progress"):
    cands = [_cand("ADP-17", FOUNDRY, status=status)]
    body = {"updates": [{"issue_key": "ADP-17", "action": "transition", "payload": payload,
                         "evidence": evidence, "confidence": 0.9}]}
    return tu.validate_proposals(body, cands, 0, TRANSCRIPT, None,
                                 transitions_fn=lambda k, c: trs, user_fn=_user_fn)


def test_validate_strict_on_maps_done_to_closed(monkeypatch):
    monkeypatch.setenv("UPDATES_STRICT_STATUS", "1")
    out = _validate_transition("Done", "My Sages AI foundry issues are resolved")
    u = out["updates"][0]
    assert (u["before"], u["after"], u["transition_id"]) == ("In Progress", "Closed", "61")


def test_validate_strict_on_rejects_on_hold_from_testing(monkeypatch):
    monkeypatch.setenv("UPDATES_STRICT_STATUS", "1")
    out = _validate_transition("On Hold", "my IRB agent is done and in testing")
    assert not out["updates"]
    assert "comment only" in out["rejected"][0]["reason"]


def test_validate_strict_off_reproduces_cancelled(monkeypatch):
    out = _validate_transition("Done", "My Sages AI foundry issues are resolved")
    assert out["updates"][0]["after"] == "Cancelled"


def _apply_with_adp_workflow(monkeypatch, update):
    from core.tools.jira_tool import JiraCredentials
    posts = []
    raw = {"transitions": [{"id": t["id"], "name": t["name"],
                            "to": {"name": t["to_status"], "statusCategory": {"key": t["to_category"]}}}
                           for t in ADP_TRS]}
    ok = SimpleNamespace(ok=True, status_code=200, text="{}", json=lambda: raw)
    monkeypatch.setattr(tu.requests, "get", lambda url, **kw: ok)
    monkeypatch.setattr(tu.requests, "post", lambda url, **kw: (posts.append((url, kw.get("json"))),
                                                               SimpleNamespace(ok=True, status_code=204))[1])
    creds = JiraCredentials(base_url="https://example.atlassian.net", email="", api_token="",
                            project_key="ADP", access_token="t", cloud_id="c")
    return tu.apply_updates([update], creds, ["ADP"]), posts


def test_apply_strict_never_falls_back_to_category(monkeypatch):
    monkeypatch.setenv("UPDATES_STRICT_STATUS", "1")
    stale = {"issue_key": "ADP-17", "action": "transition", "payload": "Done",
             "transition_id": "999", "target_status": "Done"}
    res, posts = _apply_with_adp_workflow(monkeypatch, stale)
    assert res[0]["status"] == "failed" and posts == []
    res, posts = _apply_with_adp_workflow(monkeypatch, dict(stale, target_status="Closed"))
    assert res[0]["status"] == "applied" and posts[0][1] == {"transition": {"id": "61"}}


def test_apply_legacy_category_fallback_when_flag_off(monkeypatch):
    stale = {"issue_key": "ADP-17", "action": "transition", "payload": "Done",
             "transition_id": "999", "target_status": "Done"}
    res, posts = _apply_with_adp_workflow(monkeypatch, stale)
    assert res[0]["message"] == "Moved to Cancelled"      # the old bug, only with the flag off
