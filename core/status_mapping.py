"""
Piece C of the suggest-updates quality work: STRICT STATUS MAPPING.

Switch: ``UPDATES_STRICT_STATUS`` (default on; ``0`` restores the old
``resolve_transition`` + evidence-word check).

The old mapping turned "my AI Foundry issues are resolved" into
In Progress -> **Cancelled** on a project with no status literally named
"Done" (it took the first status in the done category).  Here the TARGET IS
DECIDED BY WHAT WAS SAID (the verbatim evidence quote), and the model's
requested status is only a tie-breaker inside the allowed set:

=================  =========================================================
said               allowed target
=================  =========================================================
dropped/cancelled  a Cancelled / Won't Do / Rejected / Abandoned status
blocked/on hold/   a status named like On Hold / Blocked / Paused / Waiting
paused/waiting on
in testing/review  a status named like Testing / QA / Review / UAT / Verify
resolved/done/     a done-category status that is NOT cancel-type
finished/fixed     (prefers Done, Closed, Resolved, Complete)
started/working on an in-progress status that is not hold/testing-type
reopen/not started a to-do status
=================  =========================================================

When the statement matches several rows the first one wins ("done and in
testing" -> testing).  If the allowed set is empty on that issue's workflow,
NO transition is proposed (the comment still is).
"""

from __future__ import annotations

import re

from core.update_llm import flag_on


def strict_enabled() -> bool:
    return flag_on("UPDATES_STRICT_STATUS")


def _n(s: str) -> str:
    return " " + re.sub(r"[^a-z0-9]+", " ", (s or "").lower().replace("'", "")).strip() + " "


# (intent, phrases) in precedence order.
_INTENTS = (
    ("cancel", ("cancelled", "canceled", "cancel", "cancelling", "dropped", "dropping",
                "abandoned", "abandoning", "scrapped", "wont do", "not doing", "shelved",
                "killed", "no longer needed", "not going ahead")),
    ("hold", ("blocked", "on hold", "paused", "waiting on", "waiting for", "stalled",
              "held up", "stuck on")),
    ("testing", ("in testing", "being tested", "under test", "in qa", "in review",
                 "under review", "being reviewed", "in uat", "ready for testing",
                 "ready for review", "testing it now", "now testing")),
    ("done", ("resolved", "done", "finished", "complete", "completed", "closed", "fixed",
              "shipped", "wrapped up", "working now", "sorted", "delivered")),
    ("progress", ("working on", "started", "starting", "in progress", "continuing",
                  "underway", "begun", "picked up")),
    ("new", ("reopen", "re open", "reopened", "not started", "back to the backlog")),
)

_CANCEL_NAME = re.compile(r"cancel|wont ?do|won t do|reject|duplicate|abandon|declin|obsolete|invalid|not do")
_HOLD_NAME = re.compile(r"hold|block|pause|wait|stall|defer|park")
_TEST_NAME = re.compile(r"test|\bqa\b|review|verif|\buat\b|validat|acceptance")
_DONE_PREF = ("done", "closed", "resolved", "complete", "completed", "finished", "fixed")
_NEW_PREF = ("to do", "todo", "open", "backlog", "reopened", "selected for development")


def intent_of(evidence: str) -> str | None:
    t = _n(evidence)
    for intent, phrases in _INTENTS:
        if any(f" {p} " in t for p in phrases):
            return intent
    return None


def _name(tr: dict) -> str:
    return (tr.get("to_status") or "").strip().lower()


def _plain(tr: dict) -> str:
    """Status name for pattern checks: lower-case, apostrophes removed."""
    return re.sub(r"['\u2019]", "", _name(tr))


def allowed_targets(intent: str, transitions: list[dict]) -> list[dict]:
    is_cancel = lambda tr: bool(_CANCEL_NAME.search(_plain(tr)))       # noqa: E731
    is_hold = lambda tr: bool(_HOLD_NAME.search(_plain(tr)))           # noqa: E731
    is_test = lambda tr: bool(_TEST_NAME.search(_plain(tr)))           # noqa: E731
    if intent == "cancel":
        return [tr for tr in transitions if is_cancel(tr)]
    if intent == "hold":
        return [tr for tr in transitions if is_hold(tr) and not is_cancel(tr)]
    if intent == "testing":
        return [tr for tr in transitions if is_test(tr) and not is_cancel(tr) and not is_hold(tr)]
    if intent == "done":
        out = [tr for tr in transitions if tr.get("to_category") == "done"
               and not is_cancel(tr) and not is_hold(tr)]
        return sorted(out, key=lambda tr: _DONE_PREF.index(_name(tr)) if _name(tr) in _DONE_PREF else 99)
    if intent == "progress":
        out = [tr for tr in transitions if tr.get("to_category") == "indeterminate"
               and not (is_cancel(tr) or is_hold(tr) or is_test(tr))]
        return sorted(out, key=lambda tr: 0 if _name(tr) == "in progress" else 1)
    if intent == "new":
        out = [tr for tr in transitions if tr.get("to_category") == "new"
               and not (is_cancel(tr) or is_hold(tr))]
        return sorted(out, key=lambda tr: _NEW_PREF.index(_name(tr)) if _name(tr) in _NEW_PREF else 99)
    return []


def resolve_strict(requested: str, evidence: str, transitions: list[dict]) -> tuple[dict | None, str]:
    """(transition, "") or (None, reason).  The statement decides the allowed
    set; the requested name only picks within it."""
    intent = intent_of(evidence)
    if not intent:
        return None, "evidence does not state a status change"
    allowed = allowed_targets(intent, transitions)
    if not allowed:
        return None, f"no '{intent}' status available on this workflow (comment only)"
    want = (requested or "").strip().lower()
    for tr in allowed:
        if want and (_name(tr) == want or (tr.get("name") or "").strip().lower() == want):
            return tr, ""
    return allowed[0], ""
