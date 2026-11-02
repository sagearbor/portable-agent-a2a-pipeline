"""
GET /api/v1/jira/health — is the shared Jira credential usable?

Polled by bot/web/jira-banner.js on every page (on load + every ~10 min) to
show a red "Jira connection is down" bar.  Logic and caching live in
core/jira_health.py; this route only exposes the result.

Response (always HTTP 200 — the status is in the body):
    {
      "status":     "ok" | "missing" | "invalid" | "expiring_soon" | "unreachable",
      "message":    "<user-facing sentence>" | null,   # null when ok
      "days_left":  <int>,                               # only if JIRA_TOKEN_EXPIRES_ON set
      "checked_at": "2026-11-02T14:05:00+00:00"          # UTC, when Jira was last checked
    }

Never includes the token, the email, or any header.
"""

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from core.jira_health import get_jira_health

router = APIRouter()


@router.get("/jira/health")
def jira_health() -> JSONResponse:
    # Sync def: FastAPI runs it in the threadpool, so the (cached) blocking
    # requests call never stalls the event loop.
    return JSONResponse(
        content=get_jira_health(),
        headers={"Cache-Control": "no-store"},
    )
