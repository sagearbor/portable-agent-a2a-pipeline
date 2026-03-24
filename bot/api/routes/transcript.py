"""
POST /api/v1/process-transcript

Accepts a meeting transcript and runs the 3-agent pipeline to produce
Jira tickets. Supports dry_run mode to preview tickets without creating them.

Route is mounted at /api/v1 in bot/api/main.py, so the full path is:
    POST /api/v1/process-transcript
"""

import os
import time
from datetime import datetime, timezone
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from config.settings import PROVIDER
from agents import agent1_email, agent2_router, agent3_jira
from bot.adapters.transcript_adapter import transcript_to_pipeline_input

router = APIRouter()


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class TranscriptRequest(BaseModel):
    transcript: str
    project_key: str = "ST"
    meeting_title: str = "Meeting"
    dry_run: bool = False
    # Optional credential overrides — when provided these take precedence
    # over the corresponding JIRA_* environment variables.
    # The web UI no longer sends jira_email / jira_api_token; those fields
    # are kept here for backward compatibility but default to None so the
    # server falls back to env vars (server service account).
    jira_base_url:   str | None = None
    jira_email:      str | None = None
    jira_api_token:  str | None = None
    jira_project_key: str | None = None


class TicketResult(BaseModel):
    ticket_id: str
    url: str
    status: str          # "created" or "draft"
    summary: str
    priority: str
    description: str = ""


class TranscriptResponse(BaseModel):
    status: str          # "success" or "error"
    provider: str
    meeting_title: str
    project_key: str
    tickets_drafted: int
    tickets_created: int
    dry_run: bool
    elapsed_seconds: float
    tickets: list[TicketResult]
    error_message: str | None = None


# ---------------------------------------------------------------------------
# Route handler
# ---------------------------------------------------------------------------

@router.post("/process-transcript", response_model=TranscriptResponse)
async def process_transcript(req: TranscriptRequest) -> TranscriptResponse:
    """
    Convert a meeting transcript into Jira tickets via the 3-agent pipeline.

    1. Validates the transcript is non-empty
    2. Converts transcript text to email-shaped dicts via transcript_adapter
    3. Runs agent1 (structure extraction) -> agent2 (routing) -> agent3 (ticket creation)
    4. Returns draft or created tickets depending on dry_run flag
    """
    # Validate input
    if not req.transcript or not req.transcript.strip():
        raise HTTPException(
            status_code=422,
            detail="transcript must not be empty"
        )

    start = time.time()

    # Temporarily override JIRA_PROJECT_KEY if a non-default project is specified.
    # req.jira_project_key (from the UI) takes precedence over req.project_key.
    effective_project_key = req.jira_project_key or req.project_key
    original_project_key = os.environ.get("JIRA_PROJECT_KEY", "ST")
    key_overridden = effective_project_key != original_project_key
    if key_overridden:
        os.environ["JIRA_PROJECT_KEY"] = effective_project_key

    # Build optional credential overrides dict for agent3
    jira_creds: dict | None = None
    if req.jira_base_url or req.jira_email or req.jira_api_token:
        jira_creds = {
            k: v for k, v in {
                "JIRA_BASE_URL":    req.jira_base_url,
                "JIRA_EMAIL":       req.jira_email,
                "JIRA_API_TOKEN":   req.jira_api_token,
                "JIRA_PROJECT_KEY": req.jira_project_key or effective_project_key,
            }.items() if v is not None
        }

    try:
        # Step 1: Convert transcript to email-shaped items
        items = transcript_to_pipeline_input(
            transcript=req.transcript,
            meeting_title=req.meeting_title,
        )

        if not items:
            return TranscriptResponse(
                status="success",
                provider=PROVIDER,
                meeting_title=req.meeting_title,
                project_key=effective_project_key,
                tickets_drafted=0,
                tickets_created=0,
                dry_run=req.dry_run,
                elapsed_seconds=round(time.time() - start, 2),
                tickets=[],
            )

        # Step 2: Agent 1 — extract structure from transcript segments
        email_extracts = agent1_email.run_on_items(items)

        # Step 3: Agent 2 — route and filter to actionable items only
        approved_items = agent2_router.run(email_extracts=email_extracts)

        # Step 4: Agent 3 — write ticket descriptions and create (or draft)
        raw_tickets = agent3_jira.run(
            approved_items=approved_items,
            dry_run=req.dry_run,
            jira_creds=jira_creds,
        )

        # Step 5: Build response
        tickets = [
            TicketResult(
                ticket_id=t["ticket_id"],
                url=t.get("url", ""),
                status=t["status"],
                summary=t["summary"],
                priority=t.get("priority", "Medium"),
                description=t.get("description", ""),
            )
            for t in raw_tickets
        ]

        drafts = [t for t in tickets if t.status == "draft"]
        created = [t for t in tickets if t.status == "created"]

        return TranscriptResponse(
            status="success",
            provider=PROVIDER,
            meeting_title=req.meeting_title,
            project_key=effective_project_key,
            tickets_drafted=len(drafts),
            tickets_created=len(created),
            dry_run=req.dry_run,
            elapsed_seconds=round(time.time() - start, 2),
            tickets=tickets,
        )

    except RuntimeError as exc:
        # Jira API errors or pipeline errors
        elapsed = round(time.time() - start, 2)
        raise HTTPException(
            status_code=500,
            detail={
                "status": "error",
                "error_code": "PIPELINE_FAILURE",
                "message": str(exc),
                "elapsed_seconds": elapsed,
            }
        )
    except Exception as exc:
        elapsed = round(time.time() - start, 2)
        raise HTTPException(
            status_code=500,
            detail={
                "status": "error",
                "error_code": "PIPELINE_FAILURE",
                "message": f"Unexpected error: {type(exc).__name__}: {exc}",
                "elapsed_seconds": elapsed,
            }
        )
    finally:
        # Restore original project key
        if key_overridden:
            os.environ["JIRA_PROJECT_KEY"] = original_project_key


# ---------------------------------------------------------------------------
# Individual ticket submission (used by the web UI "Create Selected Tickets")
# ---------------------------------------------------------------------------

class SubmitTicketRequest(BaseModel):
    """
    Create a single pre-authored Jira ticket.

    Used by the web UI after the user reviews and edits draft tickets.
    Bypasses the LLM pipeline — the summary/description come directly
    from the user's edits.
    """
    summary:     str
    description: str
    priority:    str = "Medium"
    project_key: str = "ST"
    batch_id:    str | None = None   # shared batch timestamp for all tickets in one submission
    # Optional credential overrides (same semantics as TranscriptRequest).
    # The web UI no longer sends these; server falls back to env vars when None.
    jira_base_url:  str | None = None
    jira_email:     str | None = None
    jira_api_token: str | None = None


@router.post("/submit-ticket", response_model=TicketResult)
async def submit_ticket(req: SubmitTicketRequest) -> TicketResult:
    """
    Create a single Jira ticket from pre-authored content.

    Called by the web UI for each checked ticket row after the user
    finishes editing.  No LLM involved — the summary and description
    are taken verbatim from the request.
    """
    from tools.jira_tool import create_ticket as _create_ticket, JiraCredentials

    # Build credentials object (or None to fall back to env vars)
    credentials: JiraCredentials | None = None
    if req.jira_base_url or req.jira_email or req.jira_api_token:
        credentials = JiraCredentials(
            base_url=req.jira_base_url    or os.environ.get("JIRA_BASE_URL",   ""),
            email=req.jira_email          or os.environ.get("JIRA_EMAIL",      ""),
            api_token=req.jira_api_token  or os.environ.get("JIRA_API_TOKEN",  ""),
            project_key=req.project_key,
        )

    # Temporarily set JIRA_PROJECT_KEY so the fallback path in _client() works
    original_key = os.environ.get("JIRA_PROJECT_KEY", "ST")
    key_overridden = req.project_key != original_key
    if key_overridden:
        os.environ["JIRA_PROJECT_KEY"] = req.project_key

    try:
        # Always label tickets created by the bot for traceability
        # batch_id groups tickets from the same submission for easy bulk delete
        batch_ts = req.batch_id or datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        labels = ["sageJiraBot", batch_ts]

        result = _create_ticket(
            summary=req.summary,
            description=req.description,
            priority=req.priority,
            credentials=credentials,
            labels=labels,
        )
        return TicketResult(
            ticket_id=result["ticket_id"],
            url=result.get("url", ""),
            status=result["status"],
            summary=result["summary"],
            priority=result.get("priority", req.priority),
            description=req.description,
        )
    except RuntimeError as exc:
        raise HTTPException(
            status_code=500,
            detail={"status": "error", "error_code": "JIRA_ERROR", "message": str(exc)},
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={
                "status": "error",
                "error_code": "UNEXPECTED_ERROR",
                "message": f"{type(exc).__name__}: {exc}",
            },
        )
    finally:
        if key_overridden:
            os.environ["JIRA_PROJECT_KEY"] = original_key


# ---------------------------------------------------------------------------
# Enrich drafts with Jira context (epics, sprints, effort, dependencies)
# ---------------------------------------------------------------------------

class EnrichDraftsRequest(BaseModel):
    tickets: list[dict]
    project_key: str = "ST"
    base_url: str = "https://dcri.atlassian.net"


@router.post("/enrich-drafts")
async def enrich_drafts(req: EnrichDraftsRequest):
    """
    Enrich draft tickets with epic assignments, effort estimates, dates,
    sprint/version suggestions, assignees, and dependency identification.

    Called by the frontend after process-transcript returns draft tickets.
    Queries live Jira context (epics, sprints, versions) and passes it
    to the LLM for intelligent assignment.
    """
    import asyncio
    from bot.jira_context import (
        query_epics, query_recent_stories, query_sprints,
        query_fix_versions, enrich_draft_tickets,
    )

    # Temporarily set project key for jira_context queries
    original_key = os.environ.get("JIRA_PROJECT_KEY", "ST")
    os.environ["JIRA_PROJECT_KEY"] = req.project_key

    try:
        loop = asyncio.get_event_loop()

        # Query Jira context in parallel using thread pool
        epics_future = loop.run_in_executor(None, query_epics, req.project_key)
        stories_future = loop.run_in_executor(None, query_recent_stories, req.project_key)
        sprints_future = loop.run_in_executor(None, query_sprints, req.project_key)
        versions_future = loop.run_in_executor(None, query_fix_versions, req.project_key)

        epics = await epics_future
        stories = await stories_future
        sprints = await sprints_future
        fix_versions = await versions_future

        # Enrich with LLM (also in thread pool since it's synchronous)
        enriched = await loop.run_in_executor(
            None, enrich_draft_tickets,
            req.tickets, epics, stories, sprints, fix_versions,
        )

        return {
            "tickets": enriched,
            "context": {
                "epics": epics,
                "sprints": sprints,
                "fix_versions": fix_versions,
            },
        }
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={
                "status": "error",
                "error_code": "ENRICHMENT_FAILURE",
                "message": f"{type(exc).__name__}: {exc}",
            },
        )
    finally:
        os.environ["JIRA_PROJECT_KEY"] = original_key


# ---------------------------------------------------------------------------
# Batch ticket submission with epic creation and issue linking
# ---------------------------------------------------------------------------

class BatchTicket(BaseModel):
    summary: str
    description: str
    priority: str = "Medium"
    epic_key: str | None = None         # "ST-40" or "new:Epic Name" or null
    sprint_id: int | None = None
    fix_version_id: str | None = None
    start_date: str | None = None
    due_date: str | None = None
    effort: str | None = None
    labels: list[str] | None = None
    suggested_assignee: str | None = None
    assignee_account_id: str | None = None  # Jira accountId for assignee
    index: int = 0                      # original array index for dependency mapping


class DependencyPair(BaseModel):
    blocker_index: int
    blocked_index: int


class BatchSubmitRequest(BaseModel):
    project_key: str = "ST"
    base_url: str = "https://dcri.atlassian.net"
    batch_id: str | None = None
    tickets: list[BatchTicket]
    dependencies: list[DependencyPair] = []


class BatchTicketResult(BaseModel):
    index: int
    ticket_id: str
    url: str
    status: str
    summary: str
    error: str | None = None


class BatchSubmitResponse(BaseModel):
    results: list[BatchTicketResult]
    epics_created: list[dict] = []
    links_created: int = 0


@router.post("/submit-tickets-batch", response_model=BatchSubmitResponse)
async def submit_tickets_batch(req: BatchSubmitRequest) -> BatchSubmitResponse:
    """
    Create tickets in batch with epic creation and issue linking.

    Flow:
    1. Group tickets by epic_key
    2. Create any new epics (epic_key starting with "new:")
    3. Create Stories under their respective epics
    4. Create issue links for dependencies
    """
    from tools.jira_tool import (
        create_ticket as _create_ticket,
        create_issue_link as _create_issue_link,
        JiraCredentials,
    )

    batch_ts = req.batch_id or datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    base_labels = ["sageJiraBot", batch_ts]

    # Set project key
    original_key = os.environ.get("JIRA_PROJECT_KEY", "ST")
    os.environ["JIRA_PROJECT_KEY"] = req.project_key

    results: list[BatchTicketResult] = []
    epics_created: list[dict] = []
    # Map from "new:Name" -> created epic key
    new_epic_map: dict[str, str] = {}
    # Map from ticket index -> created ticket key
    index_to_key: dict[int, str] = {}

    try:
        # Step 1: Create new epics first
        new_epic_names = set()
        for t in req.tickets:
            if t.epic_key and t.epic_key.startswith("new:"):
                new_epic_names.add(t.epic_key)

        for epic_ref in new_epic_names:
            epic_name = epic_ref[4:]  # strip "new:" prefix
            try:
                result = _create_ticket(
                    summary=epic_name,
                    description=f"Epic created by SageJiraBot from meeting transcript.",
                    issue_type="Epic",
                    priority="Medium",
                    labels=base_labels,
                )
                new_epic_map[epic_ref] = result["ticket_id"]
                epics_created.append({
                    "ref": epic_ref,
                    "key": result["ticket_id"],
                    "summary": epic_name,
                })
                print(f"[batch] Created Epic {result['ticket_id']}: {epic_name}")
            except Exception as exc:
                print(f"[batch] Failed to create Epic '{epic_name}': {exc}")

        # Step 2: Create Stories
        for ticket in req.tickets:
            # Resolve epic key
            resolved_epic = None
            if ticket.epic_key:
                if ticket.epic_key.startswith("new:"):
                    resolved_epic = new_epic_map.get(ticket.epic_key)
                else:
                    resolved_epic = ticket.epic_key

            # Merge labels
            ticket_labels = list(base_labels)
            if ticket.labels:
                for lbl in ticket.labels:
                    if lbl not in ticket_labels:
                        ticket_labels.append(lbl)

            try:
                result = _create_ticket(
                    summary=ticket.summary,
                    description=ticket.description,
                    issue_type="Story",
                    priority=ticket.priority,
                    epic_key=resolved_epic,
                    labels=ticket_labels,
                    sprint_id=ticket.sprint_id,
                    fix_version_id=ticket.fix_version_id,
                    start_date=ticket.start_date,
                    due_date=ticket.due_date,
                    original_estimate=ticket.effort,
                    assignee_account_id=ticket.assignee_account_id,
                )
                index_to_key[ticket.index] = result["ticket_id"]
                results.append(BatchTicketResult(
                    index=ticket.index,
                    ticket_id=result["ticket_id"],
                    url=result.get("url", ""),
                    status="created",
                    summary=ticket.summary,
                ))
            except Exception as exc:
                results.append(BatchTicketResult(
                    index=ticket.index,
                    ticket_id="",
                    url="",
                    status="error",
                    summary=ticket.summary,
                    error=str(exc),
                ))

        # Step 3: Create dependency links
        links_created = 0
        for dep in req.dependencies:
            blocker_key = index_to_key.get(dep.blocker_index)
            blocked_key = index_to_key.get(dep.blocked_index)
            if blocker_key and blocked_key:
                try:
                    _create_issue_link(blocker_key, blocked_key)
                    links_created += 1
                except Exception as exc:
                    print(f"[batch] Failed to link {blocker_key} -> {blocked_key}: {exc}")

        # Step 4: Set epic dates to span all children (min start, max due)
        import requests as _requests
        for epic_info in epics_created:
            epic_key = epic_info["key"]
            epic_ref = epic_info["ref"]
            child_starts = [t.start_date for t in req.tickets if (t.epic_key in (epic_ref, epic_key)) and t.start_date]
            child_dues = [t.due_date for t in req.tickets if (t.epic_key in (epic_ref, epic_key)) and t.due_date]
            if child_starts or child_dues:
                epic_start = min(child_starts) if child_starts else None
                epic_due = max(child_dues) if child_dues else None
                try:
                    from tools.jira_tool import _client as _jira_client
                    jira_base, jira_auth, jira_hdrs = _jira_client()
                    meta = _requests.get(
                        f"{jira_base}/rest/api/3/issue/{epic_key}/editmeta",
                        auth=jira_auth, headers=jira_hdrs, timeout=10,
                    )
                    if meta.ok:
                        date_update = {}
                        for fid, fmeta in meta.json().get("fields", {}).items():
                            fname = fmeta.get("name", "").lower()
                            if "start" in fname and "date" in fname and epic_start:
                                date_update[fid] = epic_start
                            elif fname == "due date" and epic_due:
                                date_update[fid] = epic_due
                        if date_update:
                            _requests.put(
                                f"{jira_base}/rest/api/3/issue/{epic_key}",
                                json={"fields": date_update},
                                auth=jira_auth, headers=jira_hdrs, timeout=10,
                            )
                            print(f"[batch] Set epic {epic_key} dates: {date_update}")
                except Exception as exc:
                    print(f"[batch] Failed to set epic dates: {exc}")

        return BatchSubmitResponse(
            results=results,
            epics_created=epics_created,
            links_created=links_created,
        )

    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={
                "status": "error",
                "error_code": "BATCH_ERROR",
                "message": f"{type(exc).__name__}: {exc}",
            },
        )
    finally:
        os.environ["JIRA_PROJECT_KEY"] = original_key
