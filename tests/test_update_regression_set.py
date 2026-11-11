"""Piece E, offline: the labelled regression fixtures and their scorer.

The real-model run is tests/regression/update_regression.py (needs VPN);
these tests pin the fixtures and the scorer, and replay the ORIGINAL bad
suggestions through the full pipeline (all pieces on, LLM mocked) to show
every one of them is now removed.
"""

import json
from types import SimpleNamespace

import pytest

from core import dcri_llm, ticket_updates as tu, update_llm
from tests.regression import update_regression as reg

DATA = reg.load_fixtures()
FX = {f["name"]: f for f in DATA["fixtures"]}


@pytest.fixture(autouse=True)
def _all_on(monkeypatch):
    for f in reg.FLAGS:
        monkeypatch.setenv(f, "1")


def test_fixtures_cover_the_real_case():
    assert set(FX) == {"adp_base", "adp_irb_present", "adp_irb_testing_workflow", "adp_irb_decoy"}
    base = FX["adp_base"]
    keys = {c["key"] for c in base["candidates"]}
    assert {"ADP-4", "ADP-17", "ADP-21"} <= keys
    # ADP workflow: no status named Done; done category = Closed + Cancelled
    names = {t["to_status"]: t["to_category"] for t in base["transitions"]["*"]}
    assert "Done" not in names and names["Closed"] == names["Cancelled"] == "done" and "On Hold" in names
    for f in DATA["fixtures"]:
        assert "ADP-4" not in f["expect"]["allowed_keys"]          # nothing on SOP Chatbot, ever
        assert all("Cancelled" not in v and "On Hold" not in v for v in f["expect"]["allowed_status"].values())


ORIGINAL_BAD = [   # what the deployed build proposed for the real transcript
    {"issue_key": "ADP-17", "action": "transition", "before": "In Progress", "after": "Cancelled"},
    {"issue_key": "ADP-17", "action": "comment"},
    {"issue_key": "ADP-4", "action": "comment"},
    {"issue_key": "ADP-4", "action": "transition", "before": "In Progress", "after": "On Hold"},
    {"issue_key": "ADP-4", "action": "comment"},
    {"issue_key": "ADP-4", "action": "comment"},
]


def test_scorer_flags_the_original_bad_output():
    s = reg.score({"updates": ORIGINAL_BAD, "covered_drafts": {"2": "ADP-4"}}, FX["adp_base"]["expect"])
    assert len(s["wrong_ticket"]) == 4 and s["wrong_status"] == ["ADP-17:In Progress->Cancelled"]
    assert s["missed"] == ["ADP-17:transition->Closed"] and s["wrong_cover"] == [2]


def test_scorer_perfect_output():
    good = [{"issue_key": "ADP-17", "action": "comment"},
            {"issue_key": "ADP-17", "action": "transition", "before": "In Progress", "after": "Closed"},
            {"issue_key": "ADP-21", "action": "assign", "after": "Sage Arbor"}]
    s = reg.score({"updates": good, "covered_drafts": {"0": "ADP-17"}}, FX["adp_base"]["expect"])
    assert not (s["wrong_ticket"] or s["wrong_status"] or s["wrong_assign"] or s["wrong_cover"] or s["missed"])
    assert s["found"] == s["expected"] == 2


# --- replay the original bad proposals through the full pipeline (mocked LLM) ---

def _chat(body):
    return SimpleNamespace(model="fake", choices=[SimpleNamespace(
        finish_reason="stop", message=SimpleNamespace(content=json.dumps(body), tool_calls=None,
                                                      function_call=None))],
        usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1,
                              prompt_tokens_details=SimpleNamespace(cached_tokens=0),
                              completion_tokens_details=SimpleNamespace(reasoning_tokens=0)))


SPLIT = {"items": [
    {"id": "w1", "title": "AI Foundry issues", "summary": "Sage's AI Foundry issues are resolved.",
     "quote": "My Sages AI foundry issues are resolved", "status_said": "done", "people": ["Sage"]},
    {"id": "w3", "title": "IRB agent", "summary": "IRB agent done and in testing; waiting on manuscript feedback.",
     "quote": "my IRB agent is done and in testing, still waiting on manuscript feedback from Henry and Vincent",
     "status_said": "in_testing", "people": []},
    {"id": "w4", "title": "VERDICT LLM-as-a-judge",
     "summary": "Improving VERDICT; fixing spiked documents to fit rubric.",
     "quote": "Im still working on improvements to VERDICT LLM-as-a-judge, haveing to fix spiked documents to fit rubric",
     "status_said": "in_progress", "people": []}],
    "draft_items": [{"index": 0, "item_id": "w1"}, {"index": 2, "item_id": "w3"}, {"index": 4, "item_id": "w4"}]}


def _match(kw):
    """A careless matcher: reproduces the deployed build's mistakes wherever
    the retrieved candidates allow it."""
    sent = kw["messages"][1]["content"]
    ups = [{"item_id": "w1", "issue_key": "ADP-17", "action": "comment",
            "payload": "AI Foundry issues resolved.", "evidence": "My Sages AI foundry issues are resolved",
            "confidence": 0.9},
           {"item_id": "w1", "issue_key": "ADP-17", "action": "transition", "payload": "Cancelled",
            "evidence": "My Sages AI foundry issues are resolved", "confidence": 0.9}]
    for iid, ev in (("w3", "my IRB agent is done and in testing"),
                    ("w4", "Im still working on improvements to VERDICT")):
        ups.append({"item_id": iid, "issue_key": "ADP-4", "action": "comment", "payload": ev,
                    "evidence": ev, "confidence": 0.95})
        ups.append({"item_id": iid, "issue_key": "ADP-4", "action": "transition", "payload": "On Hold",
                    "evidence": ev, "confidence": 0.72})
    if "ADP-23" in sent:     # rubric decoy retrieved for VERDICT
        ups.append({"item_id": "w4", "issue_key": "ADP-23", "action": "comment", "payload": "rubric work",
                    "evidence": "haveing to fix spiked documents to fit rubric", "confidence": 0.8})
    return {"matches": [], "updates": ups}


def _verify(kw):
    user = kw["messages"][1]["content"]
    if '"key": "ADP-17"' in user:
        return {"same_work": "yes", "reason": "Both are about the AI Foundry permission issues.",
                "actions": [{"index": i, "justified": "yes"} for i in range(5)]}
    return {"same_work": "no", "reason": "different work", "actions": []}


def test_original_bad_suggestions_are_all_removed(monkeypatch):
    routes = {"You split": SPLIT, "You decide": _match, "You are a strict": _verify}

    def create(**kw):
        sysmsg = kw["messages"][0]["content"]
        body = next(v for k, v in routes.items() if sysmsg.startswith(k))
        return _chat(body(kw) if callable(body) else body)
    fake = dcri_llm.instrument(SimpleNamespace(base_url="https://x.openai.azure.com/openai/",
                                               chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    monkeypatch.setattr(update_llm, "get_client", lambda: (fake, "gpt-5.4-mini"))
    fx = FX["adp_base"]
    res = tu.suggest_ticket_updates(fx["transcript"], ["ADP"], fx["drafts"], None,
                                    candidates=fx["candidates"],
                                    transitions_fn=lambda k, c: fx["transitions"]["*"],
                                    user_fn=lambda k, n, c: None)
    s = reg.score(res, fx["expect"])
    assert s["wrong_ticket"] == [] and s["wrong_status"] == [] and s["wrong_cover"] == []
    assert s["duplicate_rows"] == []                                # one row per ticket
    # "Cancelled" requested for resolved work -> strict mapping gives Closed
    assert s["missed"] == []
    t = next(u for u in reg.changes(res) if u["action"] == "transition")
    assert (t["issue_key"], t["after"]) == ("ADP-17", "Closed")
