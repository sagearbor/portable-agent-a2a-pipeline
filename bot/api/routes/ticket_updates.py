"""
Suggest / apply updates to EXISTING Jira tickets from a meeting transcript.

POST /api/v1/suggest-updates   read-only: candidates -> LLM -> validated proposals
POST /api/v1/suggest-updates/prepare
                               the same, started at Analyze time WITHOUT drafts
                               (runs in parallel with Agents 1-3); also returns
                               the work items so drafts can be attached later
POST /api/v1/suggest-updates/attach-drafts
                               maps the drafts (once written) to those work
                               items: topics + covered drafts.  No Jira call.
POST /api/v1/apply-updates     writes ONLY the updates the user ticked, as the
                               signed-in user (Atlassian OAuth).  dry_run=true
                               never writes and needs no sign-in.

Both endpoints are OAuth-only: reads use the signed-in user's session so the
suggestions only ever contain issues THAT user can see.  The shared read
credential is used only when SUGGEST_UPDATES_ALLOW_SHARED_READ=1 is set
(local testing only; never set it on a deployed app - it would let anyone who
can reach the site read issues visible to the shared account).  Writes never
use the shared credential, flag or not.
"""

import asyncio
import contextvars
import os

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from bot.api.routes.auth_jira import get_oauth_session
from bot.api.routes._jira_helpers import validate_base_url
from core.clients.client import LLMOutputError
from core.jira_health import JiraAuthError
from core.jql import validate_project_key
from core.tools.jira_tool import JiraCredentials
from core import ticket_updates

router = APIRouter()


def _oauth_credentials(request: Request, project_key: str) -> JiraCredentials | None:
    sess = get_oauth_session(request)
    if not (sess and sess.get("access_token") and sess.get("cloud_id")):
        return None
    return JiraCredentials(
        base_url=sess.get("site_url", ""), email="", api_token="",
        project_key=project_key,
        access_token=sess["access_token"], cloud_id=sess["cloud_id"],
    )


def _project_list(project_key: str, project_keys: list[str] | None) -> list[str]:
    try:
        keys = [validate_project_key(k) for k in ([project_key] + list(project_keys or [])) if k]
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    out = list(dict.fromkeys(keys))
    if not out:
        raise HTTPException(status_code=400, detail="project_key is required")
    if len(out) > ticket_updates.MAX_PROJECTS:
        raise HTTPException(status_code=400,
                            detail=f"At most {ticket_updates.MAX_PROJECTS} projects per request")
    return out


def _shared_read_allowed() -> bool:
    return os.environ.get("SUGGEST_UPDATES_ALLOW_SHARED_READ", "").strip() == "1"


# Fields of each proposed update the UI needs; nothing else is returned.
_UPDATE_FIELDS = (
    "id", "issue_key", "issue_summary", "issue_type", "issue_status", "action",
    "payload", "topic", "evidence", "evidence_verified", "evidence_source",
    "confidence", "rationale", "default_checked", "before", "after",
    "transition_id", "target_status", "account_id", "weak_match",
    "verified", "verifier_reason", "verifier_action_reason", "item_id",
)
_ITEM_FIELDS = ("id", "title", "subject", "summary", "status_said", "new_work", "matched")


class DraftIn(BaseModel):
    summary: str = ""
    description: str = ""


class SuggestUpdatesRequest(BaseModel):
    transcript: str
    project_key: str
    project_keys: list[str] | None = None
    base_url: str = "https://dcri.atlassian.net"
    drafts: list[DraftIn] = []


def _require_read_access(request: Request, project_key: str) -> JiraCredentials | None:
    creds = _oauth_credentials(request, project_key)
    if creds is None and not _shared_read_allowed():
        raise HTTPException(status_code=401, detail="Sign in to Jira to get update suggestions.")
    return creds


@router.post("/suggest-updates")
async def suggest_updates(req: SuggestUpdatesRequest, request: Request):
    """Propose updates to existing open tickets.  Read-only — never writes to Jira."""
    return await _suggest(req, request, [d.model_dump() for d in req.drafts])


class PrepareUpdatesRequest(BaseModel):
    transcript: str
    project_key: str
    project_keys: list[str] | None = None
    base_url: str = "https://dcri.atlassian.net"


@router.post("/suggest-updates/prepare")
async def prepare_updates(req: PrepareUpdatesRequest, request: Request):
    """
    Phase 1 of the two-phase flow, called by the UI the moment the transcript
    is submitted, in parallel with Agents 1-3: candidate fetch + split (in
    parallel), match, validate, verify — everything except the drafts, which
    do not exist yet.  Same read-only / OAuth-only rules and validators as
    /suggest-updates.  Response = the /suggest-updates shape plus ``items``
    (the work items, for /suggest-updates/attach-drafts).
    """
    return await _suggest(req, request, [])


async def _suggest(req, request: Request, drafts: list[dict]):
    if not req.transcript.strip():
        raise HTTPException(status_code=422, detail="transcript must not be empty")
    site_base = validate_base_url(req.base_url)
    projects = _project_list(req.project_key, req.project_keys)
    creds = _require_read_access(request, projects[0])

    loop = asyncio.get_event_loop()
    try:
        result = await loop.run_in_executor(
            None, contextvars.copy_context().run, ticket_updates.suggest_ticket_updates,
            req.transcript, projects, drafts, creds,
        )
    except JiraAuthError as exc:
        raise HTTPException(
            status_code=503 if exc.shared else 401,
            detail={"status": "error", "error_code": "JIRA_AUTH_EXPIRED", "message": str(exc)},
        )
    except LLMOutputError as exc:
        raise HTTPException(status_code=502, detail={
            "status": "error", "error_code": "LLM_OUTPUT_ERROR", "message": str(exc)})
    except Exception as exc:   # noqa: BLE001
        raise HTTPException(status_code=500, detail={
            "status": "error", "error_code": "SUGGEST_UPDATES_FAILURE",
            "message": f"{type(exc).__name__}: {exc}"})

    updates = []
    for u in result["updates"]:
        out = {k: u.get(k) for k in _UPDATE_FIELDS if k in u}
        out["url"] = f"{site_base}/browse/{u['issue_key']}"
        updates.append(out)
    return {
        "updates": updates,
        "rejected_count": len(result["rejected"]),
        # JSON object keys must be strings.
        "draft_topics": {str(k): v for k, v in result["draft_topics"].items()},
        "covered_drafts": {str(k): v for k, v in result["covered_drafts"].items()},
        "possible_duplicates": {str(k): [{**d, "url": f"{site_base}/browse/{d['key']}"} for d in v]
                                for k, v in (result.get("possible_duplicates") or {}).items()},
        "topic_groups": result["topic_groups"],
        "candidates_considered": result["candidates_considered"],
        # Counts only: lets the UI say "checked N existing tickets, 0 matched".
        "match_summary": result.get("match_summary") or {},
        "work_items": result.get("work_items", 0),
        "verifier_errors": result.get("verifier_errors", 0),
        "work_items_with_candidates": result.get("work_items_with_candidates"),
        "items": [{k: it.get(k) for k in _ITEM_FIELDS} for it in result.get("items") or []],
        "read_as": "user" if creds else "shared",
    }


class MatchedIn(BaseModel):
    key: str = Field(max_length=40)
    summary: str = Field("", max_length=300)


class ItemIn(BaseModel):
    id: str = Field(max_length=20)
    title: str = Field("", max_length=200)
    subject: str | None = Field(None, max_length=200)
    summary: str = Field("", max_length=600)
    status_said: str | None = Field(None, max_length=20)
    new_work: bool | None = None
    # Tickets /prepare's matcher linked to this item (possible duplicates).
    matched: list[MatchedIn] = Field(default_factory=list, max_length=5)


class AttachUpdateIn(BaseModel):
    id: str = Field(max_length=20)
    issue_key: str = Field(max_length=40)
    item_id: str | None = Field(None, max_length=20)
    topic: str | None = Field(None, max_length=200)
    weak_match: bool = False


class AttachDraftsRequest(BaseModel):
    project_key: str
    items: list[ItemIn] = Field(default_factory=list, max_length=ticket_updates.update_retrieval.MAX_ITEMS)
    updates: list[AttachUpdateIn] = Field(default_factory=list, max_length=200)
    drafts: list[DraftIn] = Field(default_factory=list, max_length=100)


@router.post("/suggest-updates/attach-drafts")
async def attach_drafts(req: AttachDraftsRequest, request: Request):
    """
    Phase 2: map the new-ticket drafts onto the work items /prepare returned.
    Returns ``draft_topics``, ``covered_drafts``, ``possible_duplicates`` and
    ``topic_groups`` (same meaning as in /suggest-updates).  Makes no Jira call; only affects how the
    caller's own results are grouped/ticked.  Same sign-in rule as /prepare
    (it spends an LLM call).
    """
    projects = _project_list(req.project_key, None)
    _require_read_access(request, projects[0])
    items = []
    for it in req.items:
        d = it.model_dump()
        # Only well-formed keys of the selected project survive (they become
        # "possible duplicate" links in the UI).
        d["matched"] = [m for m in d["matched"]
                        if ticket_updates.is_issue_key(m["key"].upper())
                        and m["key"].upper().rsplit("-", 1)[0] == projects[0]]
        items.append(d)
    loop = asyncio.get_event_loop()
    try:
        out = await loop.run_in_executor(
            None, contextvars.copy_context().run, ticket_updates.attach_drafts,
            items, [u.model_dump() for u in req.updates],
            [d.model_dump() for d in req.drafts],
        )
    except LLMOutputError as exc:
        raise HTTPException(status_code=502, detail={
            "status": "error", "error_code": "LLM_OUTPUT_ERROR", "message": str(exc)})
    except Exception as exc:   # noqa: BLE001
        raise HTTPException(status_code=500, detail={
            "status": "error", "error_code": "ATTACH_DRAFTS_FAILURE",
            "message": f"{type(exc).__name__}: {exc}"})
    return {
        "draft_topics": {str(k): v for k, v in out["draft_topics"].items()},
        "covered_drafts": {str(k): v for k, v in out["covered_drafts"].items()},
        "possible_duplicates": {str(k): v for k, v in (out.get("possible_duplicates") or {}).items()},
        "topic_groups": out["topic_groups"],
    }


class UpdateIn(BaseModel):
    id: str | None = None
    issue_key: str
    action: str
    payload: str = ""
    transition_id: str | None = None
    target_status: str | None = None
    account_id: str | None = None
    after: str | None = None


class ApplyUpdatesRequest(BaseModel):
    project_key: str
    project_keys: list[str] | None = None
    dry_run: bool = False
    updates: list[UpdateIn]


@router.post("/apply-updates")
async def apply_updates(req: ApplyUpdatesRequest, request: Request):
    """
    Apply the user-ticked updates as the signed-in user.  Per-update results.

    dry_run=true: validates and echoes, makes no Jira call, needs no sign-in.
    Otherwise: 401 if not signed in to Jira (no shared-token write fallback).
    """
    projects = _project_list(req.project_key, req.project_keys)
    creds = None
    if not req.dry_run:
        creds = _oauth_credentials(request, projects[0])
        if creds is None:
            raise HTTPException(status_code=401, detail="Sign in to Jira to apply updates.")

    loop = asyncio.get_event_loop()
    results = await loop.run_in_executor(
        None, contextvars.copy_context().run, ticket_updates.apply_updates,
        [u.model_dump() for u in req.updates], creds, projects, req.dry_run,
    )
    return {
        "dry_run": req.dry_run,
        "results": results,
        "applied": sum(1 for r in results if r["status"] == "applied"),
        "failed": sum(1 for r in results if r["status"] == "failed"),
    }
