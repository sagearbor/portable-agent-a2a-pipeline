"""
SageJiraBot FastAPI application.

Run with:
    uvicorn bot.api.main:app --reload --port 8000

Endpoints:
    GET  /health                          - health check (liveness probe)
    POST /api/v1/process-transcript       - process transcript -> Jira tickets
    POST /api/v1/submit-ticket            - create a single pre-authored ticket
    GET  /api/v1/jira/projects            - list accessible Jira projects
    GET  /                                - web UI (bot/web/index.html)

Phase 2 will add:
    POST /api/messages                    - Bot Framework Teams message handler
"""

import os
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

# Load .env before importing anything that reads env vars
load_dotenv()

from config.settings import PROVIDER
from bot.api.routes.transcript import router as transcript_router
from bot.api.routes.jira_projects import router as jira_projects_router


# ---------------------------------------------------------------------------
# Lifespan: startup / shutdown events
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown logic."""
    # Startup
    provider = os.environ.get("PROVIDER_OVERRIDE", PROVIDER)
    project_key = os.environ.get("JIRA_PROJECT_KEY", "ST")
    jira_base = os.environ.get("JIRA_BASE_URL", "(not set)")
    print(f"\n{'='*60}")
    print(f"  SageJiraBot API starting up")
    print(f"  Provider:        {provider}")
    print(f"  Jira project:    {project_key}")
    print(f"  Jira base URL:   {jira_base}")
    print(f"{'='*60}\n")
    yield
    # Shutdown (nothing to clean up)


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(
    title="SageJiraBot API",
    description=(
        "Transcript-to-Jira pipeline endpoint. "
        "POST a meeting transcript and receive structured Jira tickets. "
        "Part of the portable-agent-a2a-pipeline project."
    ),
    version="1.0.0",
    lifespan=lifespan,
)

# Mount API routes at /api/v1
app.include_router(transcript_router,  prefix="/api/v1")
app.include_router(jira_projects_router, prefix="/api/v1")


# ---------------------------------------------------------------------------
# Health endpoint
# ---------------------------------------------------------------------------

@app.get("/health")
async def health() -> JSONResponse:
    """
    Liveness probe endpoint.

    Returns basic connectivity status. In production this is called by
    Azure Container Apps to determine if the container is healthy.

    Full connectivity check (Jira + LLM reachability) is expensive (~2s),
    so we return a lightweight 'ok' response here and rely on the pipeline
    itself to surface errors at request time.
    """
    return JSONResponse(
        status_code=200,
        content={
            "status": "ok",
            "version": "1.0.0",
            "provider": PROVIDER,
            "jira_project": os.environ.get("JIRA_PROJECT_KEY", "ST"),
        }
    )


# ---------------------------------------------------------------------------
# Static file serving — web UI
# Must be mounted LAST so API routes take precedence.
# ---------------------------------------------------------------------------

import pathlib as _pathlib

_web_dir = _pathlib.Path(__file__).parent.parent / "web"
if _web_dir.is_dir():
    app.mount("/", StaticFiles(directory=str(_web_dir), html=True), name="web")
