"""
Suggest / apply updates to EXISTING Jira tickets from a meeting transcript.

POST /api/v1/suggest-updates   read-only: candidates -> LLM -> validated proposals
POST /api/v1/apply-updates     writes ONLY the updates the user ticked, as the
                               signed-in user (Atlassian OAuth).  dry_run=true
                               never writes and needs no sign-in.

Reads use the signed-in user's OAuth session when there is one (so their own
permissions apply and "my issues" can be prioritised); otherwise they fall
back to the shared read credential, the same way /enrich-drafts does.
Writes never use the shared credential.
"""

import asyncio
import contextvars

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

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
    return out


class DraftIn(BaseModel):
    summary: str = ""
    description: str = ""


class SuggestUpdatesRequest(BaseModel):
    transcript: str
    project_key: str
    project_keys: list[str] | None = None
    base_url: str = "https://dcri.atlassian.net"
    drafts: list[DraftIn] = []


@router.post("/suggest-updates")
async def suggest_updates(req: SuggestUpdatesRequest, request: Request):
    """Propose updates to existing open tickets.  Read-only — never writes to Jira."""
    if not req.transcript.strip():
        raise HTTPException(status_code=422, detail="transcript must not be empty")
    site_base = validate_base_url(req.base_url)
    projects = _project_list(req.project_key, req.project_keys)
    creds = _oauth_credentials(request, projects[0])

    loop = asyncio.get_event_loop()
    try:
        result = await loop.run_in_executor(
            None, contextvars.copy_context().run, ticket_updates.suggest_ticket_updates,
            req.transcript, projects, [d.model_dump() for d in req.drafts], creds,
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

    for u in result["updates"]:
        u["url"] = f"{site_base}/browse/{u['issue_key']}"
    # JSON object keys must be strings.
    result["draft_topics"] = {str(k): v for k, v in result["draft_topics"].items()}
    result["covered_drafts"] = {str(k): v for k, v in result["covered_drafts"].items()}
    result["read_as"] = "user" if creds else "shared"
    return result


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
