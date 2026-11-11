"""One row per ticket: the 2026-11-10 ADP run showed ADP-17 and ADP-21 twice
each (a real status comment plus "Update the ... ticket with the current
status").  Different tickets (epic + its story/subtasks) keep their own rows."""
from core import ticket_updates as tu


def _u(key, action, payload, conf=0.8, item="i1", checked=True, **kw):
    return {"issue_key": key, "action": action, "payload": payload, "confidence": conf,
            "item_id": item, "default_checked": checked, "weak_match": False, **kw}


STATUS = "AI Foundry access and permissions are resolved; apps now use keyless managed identity."
META = "Update the AI Foundry permissions ticket with the current status."


def test_adp_run_collapses_to_one_row_per_ticket():
    ups = [_u("ADP-17", "comment", STATUS, .88), _u("ADP-21", "comment", STATUS, .82),
           _u("ADP-17", "comment", META, .70, "i2"), _u("ADP-21", "comment", META, .66, "i2", False)]
    rows, rej = tu.merge_per_issue(ups)
    assert [r["issue_key"] for r in rows] == ["ADP-17", "ADP-21"]   # epic + story both kept
    assert all(r["payload"] == STATUS for r in rows)
    assert {r["reason"] for r in rej} == {"comment only restates an instruction"}


def test_meta_instruction_detection():
    assert tu.is_meta_instruction(META)
    assert tu.is_meta_instruction("Please update the Jira epic with this status.")
    assert not tu.is_meta_instruction(STATUS)
    assert not tu.is_meta_instruction("Updated the sandbox retry logic; the ticket's tests now pass "
                                      "and the deploy went out on Tuesday afternoon after review.")


def test_two_real_comments_on_one_ticket_merge_into_one():
    rows, _ = tu.merge_per_issue([_u("ADP-17", "comment", "Keys removed.", .9),
                                  _u("ADP-17", "comment", "Budget raised to 40000.", .7, "i2"),
                                  _u("ADP-17", "comment", "Keys removed.", .6, "i3")])
    assert len(rows) == 1
    r = rows[0]
    assert r["payload"] == "Keys removed.\n\nBudget raised to 40000."
    assert r["confidence"] == .9 and r["item_ids"] == ["i1", "i2", "i3"] and r["default_checked"]


def test_status_change_plus_comment_is_one_unticked_row():
    rows, _ = tu.merge_per_issue([
        _u("ADP-9", "comment", "Shipped.", .9),
        _u("ADP-9", "transition", "Done", .8, checked=False, transition_id="31",
           target_status="Done", before="In Progress", after="Done"),
        _u("ADP-9", "transition", "Cancelled", .5, checked=False, transition_id="41")])
    assert len(rows) == 1
    r = rows[0]
    assert r["action"] == "transition" and r["transition_id"] == "31"
    assert [a["action"] for a in r["also"]] == ["comment"] and r["also"][0]["payload"] == "Shipped."
    assert r["default_checked"] is False     # state changes are never pre-ticked


def test_apply_runs_the_merged_parts_on_the_same_ticket(monkeypatch):
    calls = []
    monkeypatch.setattr(tu, "_is_oauth", lambda c: True)
    monkeypatch.setattr(tu, "_apply_one", lambda u, c: calls.append((u["issue_key"], u["action"])) or u["action"])
    res = tu.apply_updates([{"id": "u0", "issue_key": "ADP-9", "action": "transition", "payload": "Done",
                             "also": [{"action": "comment", "payload": "Shipped."}]}], object(), ["ADP"])
    assert calls == [("ADP-9", "transition"), ("ADP-9", "comment")]
    assert res[0]["status"] == "applied"


def test_apply_reports_partial_success(monkeypatch):
    def one(u, c):
        if u["action"] == "comment":
            raise tu.UpdateApplyError("boom")
        return "Moved to Done"
    monkeypatch.setattr(tu, "_is_oauth", lambda c: True)
    monkeypatch.setattr(tu, "_apply_one", one)
    res = tu.apply_updates([{"id": "u0", "issue_key": "ADP-9", "action": "transition", "payload": "Done",
                             "also": [{"action": "comment", "payload": "x"}]}], object(), ["ADP"])
    assert res[0]["status"] == "failed" and "already done: Moved to Done" in res[0]["message"]
