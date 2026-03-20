"""
POST /api/v1/process-transcript

Accepts a meeting transcript and runs the 3-agent pipeline to produce
Jira tickets. Supports dry_run mode to preview tickets without creating them.

Route is mounted at /api/v1 in bot/api/main.py, so the full path is:
    POST /api/v1/process-transcript
"""

import os
import time
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

    # Temporarily override JIRA_PROJECT_KEY if a non-default project is specified
    original_project_key = os.environ.get("JIRA_PROJECT_KEY", "ST")
    key_overridden = req.project_key != original_project_key
    if key_overridden:
        os.environ["JIRA_PROJECT_KEY"] = req.project_key

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
                project_key=req.project_key,
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
            project_key=req.project_key,
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
