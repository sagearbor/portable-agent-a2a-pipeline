"""
Suggest (and, on explicit request, apply) UPDATES to existing Jira tickets
from a meeting transcript.

The new-ticket pipeline (agents 1-3) only ever drafts NEW tickets.  When a
speaker says "my AI Foundry issues are resolved", the right action is to
comment on / transition the ticket that already tracks that work.  This
module:

1. ``fetch_candidate_issues``  — reads open issues in the selected project(s)
   (the signed-in user's own issues first, then the most recently updated),
   capped in count and in the fields sent to the LLM.
2. ``propose_updates``         — one LLM call (dcri_llm-instrumented,
   ``llm_action("suggest_updates")``, >= 40000 completion tokens) that returns
   proposed updates, a topic per new draft, and which drafts an update covers.
3. ``validate_proposals``      — keeps only proposals whose issue_key was in
   the candidate list, whose action is allowed, whose transition target maps
   to a real transition on that issue (GET /issue/{key}/transitions), and
   whose assignee resolves to an assignable Jira account.
4. ``group_by_topic``          — groups drafts + updates under topic headings
   for the UI's "Group by: Topic" view.
5. ``apply_updates``           — performs ONLY the updates the user ticked,
   with the signed-in user's OAuth credentials.  Refuses any non-OAuth
   credential; ``dry_run=True`` makes no HTTP call at all.

PHI rule: nothing here prints prompt text, completion text, transcript text or
ticket content — counts and lengths only.
"""

from __future__ import annotations

import json
import re

import requests

from core.agents.llm_json import parse_llm_json
from core.clients.client import (
    get_client, token_limit_kwarg, output_token_kwarg, completion_text,
)
from core.config.settings import PROVIDER, TEMPERATURE, MAX_TOKENS, ENRICH_MODEL
from core.dcri_llm import llm_action
from core.jql import jql_quote, is_issue_key, validate_project_key
from core.tools.jira_tool import JiraCredentials, _client, _raise_if_unauthorized

ALLOWED_ACTIONS = ("comment", "transition", "append_description", "assign")

MAX_CANDIDATES = 60          # issues sent to the LLM
MAX_MINE = 30                # of which "mine" (assignee/reporter = me) first
DESC_EXCERPT_CHARS = 300     # description excerpt per candidate
MAX_TRANSCRIPT_CHARS = 200_000
LOW_CONFIDENCE = 0.7         # below this, the UI leaves the checkbox unticked

_FIELDS = ["summary", "status", "issuetype", "parent", "assignee", "reporter",
           "updated", "description"]

# Free-text status words -> Jira status category key.
_CATEGORY_SYNONYMS = {
    "done": "done", "closed": "done", "resolved": "done", "complete": "done",
    "completed": "done", "finished": "done", "fixed": "done",
    "in progress": "indeterminate", "started": "indeterminate",
    "doing": "indeterminate", "in development": "indeterminate",
    "to do": "new", "todo": "new", "open": "new", "backlog": "new",
}

# OAuth scope each action needs (shown to the user if Atlassian refuses it).
_SCOPE_FOR_ACTION = {
    "comment":            "write:comment:jira (classic: write:jira-work)",
    "transition":         "write:issue:jira + read:issue.transition:jira (classic: write:jira-work)",
    "append_description": "write:issue:jira (classic: write:jira-work)",
    "assign":             "write:issue:jira (classic: write:jira-work)",
}

ATTRIBUTION = "(added by SageJiraBot from meeting notes)"


# ---------------------------------------------------------------------------
# Atlassian Document Format helpers
# ---------------------------------------------------------------------------

def adf_to_text(node) -> str:
    """Flatten an ADF document (or fragment) to plain text."""
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return " ".join(t for t in (adf_to_text(n) for n in node) if t)
    if isinstance(node, dict):
        if node.get("type") == "text":
            return node.get("text", "")
        return adf_to_text(node.get("content"))
    return ""


def text_to_adf_paragraphs(text: str) -> list[dict]:
    """Plain text -> list of ADF paragraphs (one per non-empty line)."""
    lines = [ln.strip() for ln in (text or "").splitlines()]
    return [
        {"type": "paragraph", "content": [{"type": "text", "text": ln}]}
        for ln in lines if ln
    ]


# ---------------------------------------------------------------------------
# Jira reads
# ---------------------------------------------------------------------------

def _is_oauth(credentials: JiraCredentials | None) -> bool:
    return bool(credentials and credentials.access_token and credentials.cloud_id)


def _search(jql: str, max_results: int, credentials) -> list[dict]:
    base, auth, headers = _client(credentials)
    resp = requests.post(
        f"{base}/rest/api/3/search/jql",
        json={"jql": jql, "fields": _FIELDS, "maxResults": max_results},
        auth=auth, headers=headers, timeout=20,
    )
    _raise_if_unauthorized(resp, credentials)
    if not resp.ok:
        raise RuntimeError(f"Jira search failed (HTTP {resp.status_code})")
    return resp.json().get("issues", [])


def _normalise_issue(issue: dict, mine: bool) -> dict:
    f = issue.get("fields") or {}
    parent = f.get("parent") or {}
    assignee = f.get("assignee") or {}
    reporter = f.get("reporter") or {}
    status = f.get("status") or {}
    return {
        "key":                 issue.get("key", ""),
        "type":                (f.get("issuetype") or {}).get("name", ""),
        "summary":             f.get("summary", "") or "",
        "status":              status.get("name", ""),
        "status_category":     (status.get("statusCategory") or {}).get("key", ""),
        "parent_key":          parent.get("key"),
        "parent_summary":      ((parent.get("fields") or {}).get("summary")) if parent else None,
        "assignee":            assignee.get("displayName") or None,
        "assignee_account_id": assignee.get("accountId") or None,
        "reporter":            reporter.get("displayName") or None,
        "updated":             (f.get("updated") or "")[:10],
        "description_excerpt": adf_to_text(f.get("description"))[:DESC_EXCERPT_CHARS],
        "mine":                mine,
    }


def fetch_candidate_issues(
    project_keys: list[str],
    credentials: JiraCredentials | None = None,
    limit: int = MAX_CANDIDATES,
) -> list[dict]:
    """
    Open issues in *project_keys* that a transcript might be talking about.

    With a per-user OAuth credential, the user's own issues (assignee or
    reporter = currentUser()) come first; the rest is filled with the most
    recently updated open issues.  With the shared credential, currentUser()
    would be the bot account, so only the "recently updated" query runs.
    """
    keys = [validate_project_key(k) for k in project_keys if k]
    if not keys:
        raise ValueError("At least one project key is required")
    projects = ", ".join(jql_quote(k) for k in keys)
    base_clause = f"project in ({projects}) AND statusCategory != Done"

    out: dict[str, dict] = {}
    if _is_oauth(credentials):
        jql = (f"{base_clause} AND (assignee = currentUser() OR reporter = currentUser()) "
               f"ORDER BY updated DESC")
        for issue in _search(jql, min(MAX_MINE, limit), credentials):
            out.setdefault(issue["key"], _normalise_issue(issue, mine=True))

    remaining = limit - len(out)
    if remaining > 0:
        jql = f"{base_clause} ORDER BY updated DESC"
        for issue in _search(jql, remaining + len(out), credentials):
            if len(out) >= limit:
                break
            out.setdefault(issue["key"], _normalise_issue(issue, mine=False))

    print(f"[ticket_updates] {len(out)} candidate issues from {len(keys)} project(s)")
    return list(out.values())


def fetch_transitions(issue_key: str, credentials) -> list[dict]:
    """Transitions currently available on *issue_key* for the given credential."""
    base, auth, headers = _client(credentials)
    resp = requests.get(
        f"{base}/rest/api/3/issue/{issue_key}/transitions",
        auth=auth, headers=headers, timeout=15,
    )
    _raise_if_unauthorized(resp, credentials)
    if not resp.ok:
        raise RuntimeError(f"Jira transitions lookup failed for {issue_key} (HTTP {resp.status_code})")
    out = []
    for t in resp.json().get("transitions", []):
        to = t.get("to") or {}
        out.append({
            "id":          str(t.get("id", "")),
            "name":        t.get("name", ""),
            "to_status":   to.get("name", ""),
            "to_category": (to.get("statusCategory") or {}).get("key", ""),
        })
    return out


def resolve_transition(target: str, transitions: list[dict]) -> dict | None:
    """
    Map a free-text target status ("Done", "resolved", "In Testing") to one of
    the issue's real transitions.  Order: exact status/transition name, then
    status-category synonym (preferring a status literally named like the
    target, then "Done"), then a unique substring match.
    """
    t = (target or "").strip().lower()
    if not t or not transitions:
        return None
    for tr in transitions:
        if tr["to_status"].lower() == t or tr["name"].lower() == t:
            return tr
    cat = _CATEGORY_SYNONYMS.get(t)
    if cat:
        in_cat = [tr for tr in transitions if tr["to_category"] == cat]
        if in_cat:
            for pref in (t, "done"):
                for tr in in_cat:
                    if tr["to_status"].lower() == pref:
                        return tr
            return in_cat[0]
    subs = [tr for tr in transitions
            if t in tr["to_status"].lower() or tr["to_status"].lower() in t]
    return subs[0] if len(subs) == 1 else None


def find_assignable_user(issue_key: str, name: str, credentials) -> dict | None:
    """Resolve a person's name to an account assignable on *issue_key*."""
    if not (name or "").strip():
        return None
    base, auth, headers = _client(credentials)
    resp = requests.get(
        f"{base}/rest/api/3/user/assignable/search",
        params={"issueKey": issue_key, "query": name.strip(), "maxResults": 10},
        auth=auth, headers=headers, timeout=15,
    )
    _raise_if_unauthorized(resp, credentials)
    if not resp.ok:
        return None
    users = [u for u in resp.json() if u.get("accountId") and u.get("active", True)]
    if not users:
        return None
    want = name.strip().lower()
    exact = [u for u in users if (u.get("displayName") or "").lower() == want]
    pick = exact[0] if exact else (users[0] if len(users) == 1 else None)
    if not pick:
        return None
    return {"account_id": pick["accountId"], "display_name": pick.get("displayName", "")}


# ---------------------------------------------------------------------------
# LLM step
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = (
    "You map a meeting transcript onto a Jira project. You receive (1) the "
    "transcript, (2) OPEN existing Jira issues (candidates), and (3) NEW ticket "
    "drafts another agent already wrote from the same transcript.\n"
    "\n"
    "For every statement about work status or progress, decide whether it is about "
    "an existing candidate issue (match on meaning, not just shared words; a story "
    "and its parent epic can both match). If it is, propose UPDATES to that issue. "
    "If no candidate matches, leave it to the new drafts.\n"
    "\n"
    "Update actions:\n"
    "- comment: a short factual status note in the speaker's terms (1-3 sentences). "
    "Use whenever the transcript gives news about the issue.\n"
    "- transition: payload is the target status NAME. Only when the transcript clearly "
    "states a status change: done/resolved/finished -> \"Done\"; started/working on -> "
    "\"In Progress\"; in testing/in review -> that status name. For an epic whose work "
    "is described as finished, propose it too, with lower confidence.\n"
    "- append_description: new scope or requirement details that belong in the issue body.\n"
    "- assign: payload is the person's name (use the exact name from the candidate list "
    "when the same person appears there). Only when the transcript says who owns it "
    "and the issue is unassigned or assigned to someone else.\n"
    "\n"
    "Rules:\n"
    "- issue_key MUST be one of the candidate keys. Never invent keys.\n"
    "- evidence: the transcript words that justify the update, copied verbatim (<= 200 chars).\n"
    "- confidence: 0.0-1.0 that the update is correct and wanted.\n"
    "- topic: a 2-5 word label for the piece of work. Use the SAME topic string for "
    "related updates and drafts so they can be grouped.\n"
    "- draft_topics: one entry per draft index with its topic.\n"
    "- drafts_covered_by_updates: draft indices whose work is already tracked by an "
    "issue you are updating (they should NOT become new tickets), with that issue_key.\n"
    "\n"
    "Return ONLY this JSON object:\n"
    "{\"updates\": [{\"issue_key\": \"ABC-1\", \"action\": \"comment|transition|append_description|assign\", "
    "\"payload\": \"...\", \"topic\": \"...\", \"evidence\": \"...\", \"confidence\": 0.0, "
    "\"rationale\": \"one short sentence\"}], "
    "\"draft_topics\": [{\"index\": 0, \"topic\": \"...\"}], "
    "\"drafts_covered_by_updates\": [{\"index\": 0, \"issue_key\": \"ABC-1\"}]}"
)


def _llm_candidate_view(c: dict) -> dict:
    """The subset of a candidate the LLM sees (capped)."""
    v = {
        "key": c["key"], "type": c["type"], "summary": c["summary"],
        "status": c["status"], "assignee": c["assignee"] or "Unassigned",
        "updated": c["updated"],
    }
    if c.get("parent_key"):
        v["parent"] = f'{c["parent_key"]}: {c.get("parent_summary") or ""}'.strip()
    if c.get("description_excerpt"):
        v["description_excerpt"] = c["description_excerpt"]
    if c.get("mine"):
        v["mine"] = True
    return v


def propose_updates(transcript: str, candidates: list[dict], drafts: list[dict]) -> dict:
    """One LLM call -> raw (unvalidated) proposal dict.  Raises LLMOutputError on
    truncated/empty output and ValueError on unparseable JSON."""
    client, default_model = get_client()
    model = ENRICH_MODEL or default_model

    draft_view = [
        {"index": i, "summary": (d.get("summary") or "")[:200],
         "description_excerpt": (d.get("description") or "")[:300]}
        for i, d in enumerate(drafts)
    ]
    user_content = (
        "CANDIDATE OPEN ISSUES:\n"
        f"{json.dumps([_llm_candidate_view(c) for c in candidates], indent=1)}\n\n"
        "NEW TICKET DRAFTS:\n"
        f"{json.dumps(draft_view, indent=1)}\n\n"
        "TRANSCRIPT:\n"
        f"{transcript[:MAX_TRANSCRIPT_CHARS]}"
    )
    print(f"[ticket_updates] LLM ({model}): {len(candidates)} candidates, "
          f"{len(drafts)} drafts, transcript {len(transcript)} chars")

    with llm_action("suggest_updates"):
        if PROVIDER in ("openai_responses", "azure_responses"):
            response = client.responses.create(
                model=model, instructions=SYSTEM_PROMPT, input=user_content,
                temperature=TEMPERATURE, **output_token_kwarg(model, MAX_TOKENS),
            )
        else:
            response = client.chat.completions.create(
                model=model,
                messages=[{"role": "system", "content": SYSTEM_PROMPT},
                          {"role": "user", "content": user_content}],
                temperature=TEMPERATURE, **token_limit_kwarg(model, MAX_TOKENS),
            )
    raw = completion_text(response, where="suggest_updates")
    parsed = parse_llm_json(raw, context="suggest_updates")
    if isinstance(parsed, list):          # tolerate a bare updates array
        parsed = {"updates": parsed}
    if not isinstance(parsed, dict):
        raise ValueError("[suggest_updates] LLM JSON was not an object")
    return parsed


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _norm_ws(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def _clamp_conf(v) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0.5
    return max(0.0, min(1.0, f))


def validate_proposals(
    raw: dict,
    candidates: list[dict],
    drafts_count: int,
    transcript: str,
    credentials=None,
    transitions_fn=fetch_transitions,
    user_fn=find_assignable_user,
) -> dict:
    """
    Keep only proposals that can actually be applied.  Returns
    {updates, rejected, draft_topics, covered_drafts}.  ``rejected`` holds
    {issue_key, action, reason} (no content) for debugging/UI counts.
    """
    by_key = {c["key"]: c for c in candidates}
    transcript_norm = _norm_ws(transcript)
    updates: list[dict] = []
    rejected: list[dict] = []
    seen: set[tuple] = set()
    transitions_cache: dict[str, list[dict]] = {}

    def reject(key, action, reason):
        rejected.append({"issue_key": key, "action": action, "reason": reason})

    for item in raw.get("updates") or []:
        if not isinstance(item, dict):
            continue
        key = str(item.get("issue_key") or "").strip().upper()
        action = str(item.get("action") or "").strip().lower()
        payload = item.get("payload")
        payload = "" if payload is None else str(payload).strip()

        if action not in ALLOWED_ACTIONS:
            reject(key, action, "unknown action"); continue
        if not is_issue_key(key) or key not in by_key:
            reject(key, action, "issue not in the candidate list"); continue
        if not payload:
            reject(key, action, "empty payload"); continue

        cand = by_key[key]
        evidence = str(item.get("evidence") or "").strip()[:300]
        conf = _clamp_conf(item.get("confidence"))
        ev_ok = bool(evidence) and _norm_ws(evidence) in transcript_norm
        if not ev_ok:
            conf = min(conf, 0.5)   # quote not found verbatim -> never default-ticked

        upd = {
            "issue_key":  key,
            "issue_summary": cand["summary"],
            "issue_type": cand["type"],
            "issue_status": cand["status"],
            "action":     action,
            "payload":    payload[:4000],
            "topic":      (str(item.get("topic") or "").strip() or cand["summary"])[:80],
            "evidence":   evidence,
            "evidence_verified": ev_ok,
            "confidence": round(conf, 2),
            "rationale":  str(item.get("rationale") or "").strip()[:300],
        }

        if action == "transition":
            if key not in transitions_cache:
                try:
                    transitions_cache[key] = transitions_fn(key, credentials)
                except Exception as exc:   # noqa: BLE001 - reported, not fatal
                    transitions_cache[key] = []
                    print(f"[ticket_updates] transitions lookup failed for {key}: {type(exc).__name__}")
            tr = resolve_transition(payload, transitions_cache[key])
            if not tr:
                reject(key, action, f"no available transition matches '{payload[:40]}'"); continue
            if tr["to_status"].lower() == (cand["status"] or "").lower():
                reject(key, action, "already in that status"); continue
            upd.update(transition_id=tr["id"], target_status=tr["to_status"],
                       before=cand["status"], after=tr["to_status"])
        elif action == "assign":
            try:
                user = user_fn(key, payload, credentials)
            except Exception as exc:   # noqa: BLE001
                user = None
                print(f"[ticket_updates] assignee lookup failed for {key}: {type(exc).__name__}")
            if not user:
                reject(key, action, "assignee not found among assignable users"); continue
            if user["account_id"] == cand.get("assignee_account_id"):
                reject(key, action, "already assigned to that person"); continue
            upd.update(account_id=user["account_id"], payload=user["display_name"] or payload,
                       before=cand.get("assignee") or "Unassigned",
                       after=user["display_name"] or payload)
        elif action == "comment":
            upd.update(before=None, after=payload)
        else:  # append_description
            upd.update(before=cand.get("description_excerpt") or "", after=payload)

        sig = (key, action, upd.get("transition_id") or upd.get("account_id") or _norm_ws(payload))
        if sig in seen:
            continue
        seen.add(sig)
        upd["default_checked"] = conf >= LOW_CONFIDENCE and ev_ok
        upd["id"] = f"u{len(updates)}"
        updates.append(upd)

    draft_topics: dict[int, str] = {}
    for dt in raw.get("draft_topics") or []:
        if not isinstance(dt, dict):
            continue
        try:
            i = int(dt.get("index"))
        except (TypeError, ValueError):
            continue
        topic = str(dt.get("topic") or "").strip()[:80]
        if 0 <= i < drafts_count and topic:
            draft_topics[i] = topic

    updated_keys = {u["issue_key"] for u in updates}
    covered: dict[int, str] = {}
    for cv in raw.get("drafts_covered_by_updates") or []:
        if not isinstance(cv, dict):
            continue
        try:
            i = int(cv.get("index"))
        except (TypeError, ValueError):
            continue
        k = str(cv.get("issue_key") or "").strip().upper()
        if 0 <= i < drafts_count and k in updated_keys:
            covered[i] = k

    return {"updates": updates, "rejected": rejected,
            "draft_topics": draft_topics, "covered_drafts": covered}


# ---------------------------------------------------------------------------
# Topic grouping (used by the UI's "Group by: Topic" view)
# ---------------------------------------------------------------------------

OTHER_TOPIC = "Other"


def _topic_key(t: str) -> str:
    return _norm_ws(t)


def group_by_topic(drafts_count: int, draft_topics: dict, updates: list[dict]) -> list[dict]:
    """
    Group new drafts and updates under topic headings.  Topics that differ
    only in case/punctuation merge.  Order: first appearance (updates first,
    then drafts by index); drafts without a topic go under "Other", last.
    Returns [{topic, draft_indices: [...], update_ids: [...]}].
    """
    groups: dict[str, dict] = {}
    order: list[str] = []

    def bucket(topic: str) -> dict:
        k = _topic_key(topic) or _topic_key(OTHER_TOPIC)
        if k not in groups:
            groups[k] = {"topic": topic or OTHER_TOPIC, "draft_indices": [], "update_ids": []}
            order.append(k)
        return groups[k]

    for u in updates:
        bucket(u.get("topic") or OTHER_TOPIC)["update_ids"].append(u["id"])
    for i in range(drafts_count):
        t = (draft_topics.get(i) or draft_topics.get(str(i)) or "").strip()
        bucket(t or OTHER_TOPIC)["draft_indices"].append(i)

    other = _topic_key(OTHER_TOPIC)
    ordered = [groups[k] for k in order if k != other]
    if other in groups:
        ordered.append(groups[other])
    return ordered


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def suggest_ticket_updates(
    transcript: str,
    project_keys: list[str],
    drafts: list[dict] | None = None,
    credentials: JiraCredentials | None = None,
) -> dict:
    """Read-only: candidates -> LLM -> validated proposals.  Never writes to Jira."""
    drafts = drafts or []
    candidates = fetch_candidate_issues(project_keys, credentials)
    if not candidates:
        groups = group_by_topic(len(drafts), {}, [])
        return {"updates": [], "rejected": [], "draft_topics": {}, "covered_drafts": {},
                "topic_groups": groups, "candidates_considered": 0}
    raw = propose_updates(transcript, candidates, drafts)
    result = validate_proposals(raw, candidates, len(drafts), transcript, credentials)
    result["topic_groups"] = group_by_topic(len(drafts), result["draft_topics"], result["updates"])
    result["candidates_considered"] = len(candidates)
    print(f"[ticket_updates] proposed={len(raw.get('updates') or [])} "
          f"valid={len(result['updates'])} rejected={len(result['rejected'])} "
          f"covered_drafts={len(result['covered_drafts'])}")
    return result


# ---------------------------------------------------------------------------
# Writes (OAuth only)
# ---------------------------------------------------------------------------

class UpdateApplyError(RuntimeError):
    """A single update failed; message is safe to show the user."""


def _check_write(resp, action: str, credentials) -> None:
    if resp.ok:
        return
    body = (resp.text or "")[:500].lower()
    if resp.status_code in (401, 403) and "scope" in body:
        raise UpdateApplyError(
            f"JiraBot's Atlassian app is missing the OAuth scope needed to {action.replace('_', ' ')} "
            f"({_SCOPE_FOR_ACTION.get(action, 'write:jira-work')}). The app owner must add it in the "
            "Atlassian developer console; then sign in to Jira again."
        )
    if resp.status_code == 401:
        _raise_if_unauthorized(resp, credentials)   # OAuth -> "sign in again"
    if resp.status_code == 403:
        raise UpdateApplyError("Jira refused this change (HTTP 403): your Jira account lacks permission.")
    if resp.status_code == 404:
        raise UpdateApplyError("Issue not found, or you cannot see it (HTTP 404).")
    detail = ""
    try:
        j = resp.json()
        msgs = list(j.get("errorMessages") or []) + [f"{k}: {v}" for k, v in (j.get("errors") or {}).items()]
        detail = "; ".join(msgs)[:200]
    except Exception:   # noqa: BLE001
        pass
    raise UpdateApplyError(f"Jira error HTTP {resp.status_code}" + (f": {detail}" if detail else ""))


def _apply_one(u: dict, credentials) -> str:
    key, action = u["issue_key"], u["action"]
    payload = (u.get("payload") or "").strip()
    base, auth, headers = _client(credentials)

    if action == "comment":
        body = {"type": "doc", "version": 1,
                "content": text_to_adf_paragraphs(payload) + text_to_adf_paragraphs(ATTRIBUTION)}
        r = requests.post(f"{base}/rest/api/3/issue/{key}/comment", json={"body": body},
                          auth=auth, headers=headers, timeout=15)
        _check_write(r, action, credentials)
        return "Comment added"

    if action == "transition":
        # Re-check with THE USER's credential: the shared read token may see
        # different transitions than the signed-in user is allowed to make.
        r = requests.get(f"{base}/rest/api/3/issue/{key}/transitions",
                         auth=auth, headers=headers, timeout=15)
        _check_write(r, action, credentials)
        trs = []
        for t in r.json().get("transitions", []):
            to = t.get("to") or {}
            trs.append({"id": str(t.get("id", "")), "name": t.get("name", ""),
                        "to_status": to.get("name", ""),
                        "to_category": (to.get("statusCategory") or {}).get("key", "")})
        want_id = str(u.get("transition_id") or "")
        tr = next((t for t in trs if want_id and t["id"] == want_id), None) \
            or resolve_transition(u.get("target_status") or payload, trs)
        if not tr:
            raise UpdateApplyError(
                f"'{u.get('target_status') or payload}' is not an available transition for you on {key}.")
        r = requests.post(f"{base}/rest/api/3/issue/{key}/transitions",
                          json={"transition": {"id": tr["id"]}},
                          auth=auth, headers=headers, timeout=15)
        _check_write(r, action, credentials)
        return f"Moved to {tr['to_status']}"

    if action == "assign":
        acct = (u.get("account_id") or "").strip()
        if not acct:
            raise UpdateApplyError("No Jira account resolved for this assignee.")
        r = requests.put(f"{base}/rest/api/3/issue/{key}/assignee", json={"accountId": acct},
                         auth=auth, headers=headers, timeout=15)
        _check_write(r, action, credentials)
        return f"Assigned to {u.get('after') or payload}"

    if action == "append_description":
        r = requests.get(f"{base}/rest/api/3/issue/{key}", params={"fields": "description"},
                         auth=auth, headers=headers, timeout=15)
        _check_write(r, action, credentials)
        desc = (r.json().get("fields") or {}).get("description") or \
            {"type": "doc", "version": 1, "content": []}
        content = list(desc.get("content") or [])
        content += text_to_adf_paragraphs(payload) + text_to_adf_paragraphs(ATTRIBUTION)
        new_desc = {"type": "doc", "version": desc.get("version", 1), "content": content}
        r = requests.put(f"{base}/rest/api/3/issue/{key}", json={"fields": {"description": new_desc}},
                         auth=auth, headers=headers, timeout=15)
        _check_write(r, action, credentials)
        return "Description updated"

    raise UpdateApplyError(f"Unknown action '{action}'")


def apply_updates(
    updates: list[dict],
    credentials: JiraCredentials | None,
    allowed_projects: list[str],
    dry_run: bool = False,
) -> list[dict]:
    """
    Apply the given (user-ticked) updates.  Per-update result:
    {id, issue_key, action, status: applied|failed|dry_run, message}.

    * dry_run=True: validates shape only, makes NO HTTP request.
    * Otherwise requires a per-user OAuth credential; the shared Basic-auth
      token is never used for writes.
    """
    allowed = {validate_project_key(p) for p in allowed_projects if p}
    if not dry_run and not _is_oauth(credentials):
        raise PermissionError("Updates can only be applied with the signed-in user's Jira session.")

    results = []
    for u in updates:
        key = str(u.get("issue_key") or "").strip().upper()
        action = str(u.get("action") or "").strip().lower()
        res = {"id": u.get("id"), "issue_key": key, "action": action}
        if not is_issue_key(key) or key.rsplit("-", 1)[0] not in allowed:
            results.append({**res, "status": "failed",
                            "message": "Issue key is not in the selected project."})
            continue
        if action not in ALLOWED_ACTIONS:
            results.append({**res, "status": "failed", "message": f"Unknown action '{action}'."})
            continue
        if dry_run:
            results.append({**res, "status": "dry_run", "message": "Dry run - nothing written."})
            continue
        try:
            msg = _apply_one({**u, "issue_key": key, "action": action}, credentials)
            results.append({**res, "status": "applied", "message": msg})
        except Exception as exc:   # noqa: BLE001 - reported per update
            results.append({**res, "status": "failed", "message": str(exc)[:300]})
    ok = sum(1 for r in results if r["status"] == "applied")
    print(f"[ticket_updates] apply: {ok}/{len(results)} applied (dry_run={dry_run})")
    return results
