"""
GET /api/v1/jira/projects

Proxies a credential check against Jira and returns the list of projects
the caller has access to.  Credentials are passed as optional query parameters;
when omitted the server falls back to the JIRA_EMAIL / JIRA_API_TOKEN
environment variables (server service account).

Endpoint:
    GET /api/v1/jira/projects?base_url=https://org.atlassian.net
    GET /api/v1/jira/projects?base_url=https://org.atlassian.net&email=you@org&token=...

Returns:
    200  [{"key": "ST", "name": "Sage Tools"}, ...]
    400  {"detail": "base_url must be an https://....atlassian.net address"}
    401  {"detail": "Invalid Jira credentials"}
    502  {"detail": "Could not reach Jira: <reason>"}

SSRF mitigation
---------------
base_url is validated against an allowlist before any outbound request is
made.  Only HTTPS URLs whose hostname ends with ".atlassian.net" are
accepted.  The fixed path "/rest/api/3/project/search" is appended by this
code — the caller cannot influence it.  The response body is never forwarded
verbatim; only extracted project keys and names are returned.
"""

import requests

from fastapi import APIRouter, HTTPException, Query

from bot.api.routes._jira_helpers import validate_base_url, get_jira_auth

router = APIRouter()

# Backward compatibility — importers that referenced the private name still work
_validate_base_url = validate_base_url


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------

@router.get("/jira/projects")
async def get_jira_projects(
    base_url: str       = Query(...,  description="Jira base URL, e.g. https://org.atlassian.net"),
    email:    str | None = Query(None, description="Atlassian account email (falls back to JIRA_EMAIL env var)"),
    token:    str | None = Query(None, description="Jira API token (falls back to JIRA_API_TOKEN env var)"),
):
    """
    Return Jira projects the caller has BROWSE_PROJECTS permission on.

    When email / token are not provided (or empty), the server falls back to
    the JIRA_EMAIL and JIRA_API_TOKEN environment variables so the frontend
    does not need to collect user credentials.

    Uses the Jira Cloud REST API v3 project/search endpoint.  Paginates
    through all results (max 50 per page) so large instances are fully
    covered.
    """
    # -- SSRF guard: validate before any outbound call ---------------------
    base = validate_base_url(base_url)

    # Resolve credentials: prefer caller-supplied values, fall back to env vars
    auth = get_jira_auth(email, token)

    # The full URL is assembled here from a trusted base and a fixed path.
    # No caller-supplied data appears in the path or query string.
    jira_search_url = f"{base}/rest/api/3/project/search"
    headers = {"Accept": "application/json"}

    projects: list[dict] = []
    start_at  = 0
    page_size = 50

    try:
        while True:
            resp = requests.get(
                jira_search_url,
                params={
                    "startAt":    start_at,
                    "maxResults": page_size,
                    "orderBy":    "name",
                },
                auth=auth,
                headers=headers,
                timeout=15,
            )

            if resp.status_code in (401, 403):
                raise HTTPException(
                    status_code=401,
                    detail="Invalid Jira credentials — check your email and API token.",
                )

            if not resp.ok:
                raise HTTPException(
                    status_code=502,
                    detail=f"Jira returned {resp.status_code}: {resp.text[:300]}",
                )

            data   = resp.json()
            values = data.get("values", [])

            # Extract only the fields we need — do not forward raw Jira data
            for p in values:
                projects.append({"key": p["key"], "name": p["name"]})

            # Pagination
            total     = data.get("total", 0)
            start_at += len(values)
            if start_at >= total or not values:
                break

    except HTTPException:
        raise
    except requests.exceptions.ConnectionError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Could not reach Jira at {base}: {exc}",
        )
    except requests.exceptions.Timeout:
        raise HTTPException(
            status_code=504,
            detail=f"Timed out connecting to Jira at {base}",
        )
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Unexpected error fetching Jira projects: {exc}",
        )

    return projects
