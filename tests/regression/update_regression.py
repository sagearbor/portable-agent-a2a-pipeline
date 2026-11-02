"""
Labelled regression set for "suggest updates to existing tickets" (piece E).

Runs the REAL pipeline (real LLM calls on the dev Foundry; needs VPN and
`az login`) against fixture Jira projects modelled on the real ADP case —
never against live Jira; no Jira HTTP call is made.

    python -m tests.regression.update_regression --runs 3
    python -m tests.regression.update_regression --runs 3 --flags off   # old pipeline
    python -m tests.regression.update_regression --out /path/details.json
    python -m tests.regression.update_regression --path single     # one-call API
    python -m tests.regression.update_regression --split --runs 3   # split recall only

``--path two_phase`` (default) runs what the web UI does: prepare (no
drafts) then attach_drafts; ``single`` runs suggest_ticket_updates with the
drafts in one go (the /suggest-updates endpoint).

``--split`` runs only the work-item split on ``split_cases`` (with and without
drafts) and checks every required topic is found — the regression for
gpt-5.4-mini dropping finished work that no draft covers.

Per run it reports: wrong-ticket updates (must be 0), wrong status changes
(must be 0), wrongly covered drafts, missed expected updates, LLM calls,
empty responses, finish_reason=length, errors, latency and tokens.  Exit code
1 if any run has a wrong ticket, wrong status, empty/length response or error.

stdout carries counts only; ``--out`` writes the proposed updates (fixture
text, not real tickets) for debugging.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import time

FIXTURES = pathlib.Path(__file__).with_name("update_fixtures.json")
FLAGS = ("UPDATES_RETRIEVAL", "UPDATES_VERIFIER", "UPDATES_STRICT_STATUS", "UPDATES_DROP_WEAK")


def load_fixtures() -> dict:
    return json.loads(FIXTURES.read_text())


def score(result: dict, expect: dict) -> dict:
    """Compare one pipeline result with a fixture's expectations."""
    allowed = set(expect["allowed_keys"])
    wrong_ticket, wrong_status, wrong_assign = [], [], []
    for u in result.get("updates") or []:
        key, action = u["issue_key"], u["action"]
        if key not in allowed:
            wrong_ticket.append(f"{key}:{action}")
            continue
        if action == "transition" and u.get("after") not in expect.get("allowed_status", {}).get(key, []):
            wrong_status.append(f"{key}:{u.get('before')}->{u.get('after')}")
        if action == "assign" and u.get("after") not in expect.get("allowed_assign", {}).get(key, []):
            wrong_assign.append(f"{key}:{u.get('after')}")
    missed = []
    for m in expect.get("must") or []:
        hit = any(u["issue_key"] == m["key"] and u["action"] == m["action"]
                  and ("after" not in m or u.get("after") == m["after"])
                  for u in result.get("updates") or [])
        if not hit:
            missed.append(f"{m['key']}:{m['action']}" + (f"->{m['after']}" if "after" in m else ""))
    covered = {int(k) for k in (result.get("covered_drafts") or {})}
    wrong_cover = sorted(covered & set(expect.get("drafts_never_covered") or []))
    return {"wrong_ticket": wrong_ticket, "wrong_status": wrong_status, "wrong_assign": wrong_assign,
            "wrong_cover": wrong_cover, "missed": missed,
            "expected": len(expect.get("must") or []),
            "found": len(expect.get("must") or []) - len(missed)}


def split_topics_found(items: list[dict], expect_topics: list[dict]) -> dict:
    """Which expected topics appear in the split's items (keyword match on
    title + summary + quote).  Returns {found: [...], missing_required: [...],
    missing_optional: [...]}."""
    texts = [" ".join(str(it.get(k) or "") for k in ("title", "summary", "quote")).lower()
             for it in items]
    found, miss_req, miss_opt = [], [], []
    for t in expect_topics:
        hit = any(any(w.lower() in tx for w in t["any"]) for tx in texts)
        if hit:
            found.append(t["label"])
        elif t.get("required"):
            miss_req.append(t["label"])
        else:
            miss_opt.append(t["label"])
    return {"found": found, "missing_required": miss_req, "missing_optional": miss_opt}


def run_split(runs: int) -> int:
    from core import dcri_llm, update_retrieval as ur

    data = load_fixtures()
    print(f"split model: {ur.split_model()}")
    hdr = ("run", "case", "drafts", "items", "found", "missing_required", "missing_optional",
           "empty", "length", "error", "latency_s")
    print("| " + " | ".join(hdr) + " |")
    print("|" + "---|" * len(hdr))
    bad = 0
    for run in range(1, runs + 1):
        for case in data.get("split_cases") or []:
            for drafts in (case["drafts"], []):
                events: list[dict] = []
                sink = dcri_llm.add_sink(lambda e: events.append(e) if e.get("event") == "llm_call" else None)
                t0, err, items = time.perf_counter(), None, []
                try:
                    items, _ = ur.split_work_items(case["transcript"], drafts, 200_000)
                except Exception as exc:   # noqa: BLE001
                    err = type(exc).__name__
                finally:
                    dcri_llm._extra_sinks.remove(sink)
                sc = split_topics_found(items, case["expect_topics"])
                empty = sum(1 for e in events if e.get("empty_output"))
                length = sum(1 for e in events if e.get("finish_reason") == "length")
                bad += len(sc["missing_required"]) + empty + length + (1 if err else 0)
                print("| " + " | ".join(str(x) for x in (
                    run, case["name"], len(drafts), len(items),
                    f"{len(sc['found'])}/{len(case['expect_topics'])}",
                    ", ".join(sc["missing_required"]) or "-", ", ".join(sc["missing_optional"]) or "-",
                    empty, length, err or "-", round(time.perf_counter() - t0, 1))) + " |", flush=True)
    return 1 if bad else 0


def _set_flags(mode: str) -> None:
    for f in FLAGS:
        os.environ[f] = "0" if mode == "off" else "1"
    if mode == "off":
        os.environ.setdefault("UPDATES_MATCH_MODEL", os.environ.get("ENRICH_MODEL") or "gpt-5.4-mini")


def run_once(fx: dict, users: dict, path: str = "two_phase") -> tuple[dict, dict]:
    from core import dcri_llm, ticket_updates as tu

    events: list[dict] = []
    sink = dcri_llm.add_sink(lambda e: events.append(e) if e.get("event") == "llm_call" else None)
    trs = fx["transitions"]

    def transitions_fn(key, creds):
        return trs.get(key) or trs["*"]

    def user_fn(key, name, creds):
        for display, acct in users.items():
            if name.strip().lower() == display.lower():
                return {"account_id": acct, "display_name": display}
        return None

    t0 = time.perf_counter()
    err = None
    result: dict = {}
    try:
        if path == "single":
            result = tu.suggest_ticket_updates(
                fx["transcript"], ["ADP"], fx["drafts"], None, candidates=fx["candidates"],
                transitions_fn=transitions_fn, user_fn=user_fn)
        else:   # what the web UI does: prepare (no drafts), then attach the drafts
            result = tu.suggest_ticket_updates(
                fx["transcript"], ["ADP"], [], None, candidates=fx["candidates"],
                transitions_fn=transitions_fn, user_fn=user_fn)
            result.update(tu.attach_drafts(result.get("items") or [], result["updates"], fx["drafts"]))
    except Exception as exc:   # noqa: BLE001 - counted as an error
        err = f"{type(exc).__name__}: {str(exc)[:200]}"
    finally:
        dcri_llm._extra_sinks.remove(sink)
    wall = time.perf_counter() - t0

    sc = score(result, fx["expect"]) if not err else {
        "wrong_ticket": [], "wrong_status": [], "wrong_assign": [], "wrong_cover": [],
        "missed": [m["key"] for m in fx["expect"]["must"]], "expected": len(fx["expect"]["must"]), "found": 0}
    stats = {
        "llm_calls": len(events),
        "empty": sum(1 for e in events if e.get("empty_output")),
        "length": sum(1 for e in events if e.get("finish_reason") == "length"),
        "error": err,
        "verifier_errors": result.get("verifier_errors", 0),
        "latency_s": round(wall, 1),
        "input_tokens": sum(e.get("input_tokens") or 0 for e in events),
        "output_tokens": sum(e.get("output_tokens") or 0 for e in events),
        "reasoning_tokens": sum(e.get("reasoning_tokens") or 0 for e in events),
        "by_operation": {},
    }
    for e in events:
        op = stats["by_operation"].setdefault(e.get("operation") or "?", {"calls": 0, "latency_ms": 0,
                                                                           "models": set()})
        op["calls"] += 1
        op["latency_ms"] += e.get("latency_ms") or 0
        op["models"].add(e.get("model_requested") or e.get("model") or "?")
    for op in stats["by_operation"].values():
        op["models"] = sorted(op["models"])
    detail = {
        "updates": [{k: u.get(k) for k in ("issue_key", "action", "before", "after", "payload",
                                           "evidence", "verifier_reason")}
                    for u in result.get("updates") or []],
        "rejected": result.get("rejected"),
        "covered_drafts": result.get("covered_drafts"),
        "work_items": result.get("work_items"),
    }
    return {**sc, **stats}, detail


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--flags", choices=("on", "off"), default="on")
    ap.add_argument("--only", help="run only this fixture name")
    ap.add_argument("--out", help="write per-run details (proposed updates) as JSON here")
    ap.add_argument("--path", choices=("two_phase", "single"), default="two_phase")
    ap.add_argument("--split", action="store_true", help="only check the work-item split on split_cases")
    args = ap.parse_args(argv)

    if args.split:
        _set_flags(args.flags)
        return run_split(args.runs)

    _set_flags(args.flags)
    data = load_fixtures()
    rows, details = [], []
    for run in range(1, args.runs + 1):
        for fx in data["fixtures"]:
            if args.only and fx["name"] != args.only:
                continue
            row, detail = run_once(fx, data["users"], args.path)
            row.update(run=run, fixture=fx["name"])
            rows.append(row)
            details.append({"run": run, "fixture": fx["name"], **detail, "score": row})
            print(f"[regression] run {run} {fx['name']}: wrong_ticket={len(row['wrong_ticket'])} "
                  f"wrong_status={len(row['wrong_status'])} missed={len(row['missed'])} "
                  f"empty={row['empty']} length={row['length']} error={bool(row['error'])} "
                  f"{row['latency_s']}s", file=sys.stderr, flush=True)

    hdr = ("run", "fixture", "wrong_tkt", "wrong_status", "wrong_assign", "wrong_cover",
           "found/expected", "missed", "calls", "empty", "length", "errors", "latency_s",
           "in_tok", "out_tok", "reason_tok")
    print("| " + " | ".join(hdr) + " |")
    print("|" + "---|" * len(hdr))
    for r in rows:
        print("| " + " | ".join(str(x) for x in (
            r["run"], r["fixture"], len(r["wrong_ticket"]), len(r["wrong_status"]),
            len(r["wrong_assign"]), len(r["wrong_cover"]), f"{r['found']}/{r['expected']}",
            ", ".join(r["missed"]) or "-", r["llm_calls"], r["empty"], r["length"],
            (1 if r["error"] else 0) + (r["verifier_errors"] or 0), r["latency_s"],
            r["input_tokens"], r["output_tokens"], r["reasoning_tokens"])) + " |")
    tot = lambda k: sum(len(r[k]) for r in rows)                  # noqa: E731
    found, expected = sum(r["found"] for r in rows), sum(r["expected"] for r in rows)
    bad = tot("wrong_ticket") + tot("wrong_status")
    defects = sum(r["empty"] + r["length"] + (1 if r["error"] else 0) for r in rows)
    print(f"\nTOTAL runs={len(rows)} wrong_ticket={tot('wrong_ticket')} wrong_status={tot('wrong_status')} "
          f"wrong_assign={tot('wrong_assign')} wrong_cover={tot('wrong_cover')} "
          f"recall={found}/{expected} empty={sum(r['empty'] for r in rows)} "
          f"length={sum(r['length'] for r in rows)} errors={sum(1 for r in rows if r['error'])} "
          f"verifier_errors={sum(r['verifier_errors'] or 0 for r in rows)}")
    if args.out:
        pathlib.Path(args.out).write_text(json.dumps(details, indent=1, default=str))
    return 1 if (bad or defects) else 0


if __name__ == "__main__":
    sys.exit(main())
