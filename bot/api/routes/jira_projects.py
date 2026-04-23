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

from fastapi import APIRouter, HTTPException, Query, Request

from bot.api.routes._jira_helpers import (
    validate_base_url,
    get_jira_auth,
    get_jira_request_config,
)

router = APIRouter()

# Backward compatibility — importers that referenced the private name still work
_validate_base_url = validate_base_url


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------

@router.get("/jira/projects")
async def get_jira_projects(
    request:       Request,
    base_url:      str        = Query(...,  description="Jira base URL, e.g. https://org.atlassian.net"),
    email:         str | None = Query(None, description="Atlassian account email (Basic-auth fallback only; ignored when OAuth-signed-in)"),
    token:         str | None = Query(None, description="Jira API token (Basic-auth fallback only; ignored when OAuth-signed-in)"),
    writable_only: bool       = Query(True, description="If true (default), returns only projects where the caller has CREATE_ISSUES permission."),
    recent_only:   bool       = Query(True, description="If true (default), narrows further to projects the user has recently viewed or worked in. Set to false to see every project they can write to."),
):
    """
    Return Jira projects the caller can see (and, by default, write to).

    Auth preference order:
      1. Atlassian OAuth 3LO session (bearer token via api.atlassian.com)
      2. Caller-supplied email/token query params (Basic auth to the site)
      3. JIRA_EMAIL / JIRA_API_TOKEN service-account env vars

    Filtering (stacked, cheapest → most restrictive):
      - ``writable_only`` (default True)  intersects with projects where
        the caller has CREATE_ISSUES permission.
      - ``recent_only`` (default True)    further narrows to projects the
        user has recently viewed (proxy for "projects I actually work in");
        useful at Duke where Jira permission schemes grant CREATE_ISSUES
        broadly and writable_only alone returns ~500 projects.

    If ``recent_only`` is true but /project/recent returns no hits (new
    user, or user with no recent activity), we automatically fall back to
    the full writable list rather than returning an empty dropdown.
    """
    # -- SSRF guard: validate before any outbound call ---------------------
    base_validated = validate_base_url(base_url)

    # Pick the auth path: OAuth bearer token when signed in, Basic auth otherwise.
    cfg = get_jira_request_config(request, base_validated)
    # If the caller supplied explicit email/token, override the Basic-auth path
    # (but never override an active OAuth session — the signed-in user is authoritative).
    if not cfg.signed_in and (email or token):
        cfg.kwargs["auth"] = get_jira_auth(email, token)

    # The full URL is assembled here from a trusted base and a fixed path.
    # No caller-supplied data appears in the path or query string.
    projects_url    = f"{cfg.base}/rest/api/3/project/search"
    permissions_url = f"{cfg.base}/rest/api/3/permissions/project"
    recent_url      = f"{cfg.base}/rest/api/3/project/recent"

    projects: list[dict] = []
    start_at  = 0
    page_size = 50

    try:
        while True:
            # nosemgrep: python.flask.security.injection.ssrf-requests.ssrf-requests
            resp = requests.get(
                projects_url,
                params={
                    "startAt":    start_at,
                    "maxResults": page_size,
                    "orderBy":    "name",
                },
                **cfg.kwargs,
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

        # Filter to writable projects if requested.  This is the second API
        # call: POST /rest/api/3/permissions/project returns only projects
        # where the authenticated user has the requested permission(s).
        #
        # SSRF-safe: ``cfg.base`` is either the allowlisted site URL (Basic)
        # or the literal api.atlassian.com (OAuth).  The path is a fixed
        # literal and only project keys from the response are consumed.
        if writable_only and projects:
            # nosemgrep: python.flask.security.injection.ssrf-requests.ssrf-requests
            perm_resp = requests.post(
                permissions_url,
                json={"permissions": ["CREATE_ISSUES"]},
                **cfg.kwargs,
            )

            if perm_resp.ok:
                writable_keys = {
                    p.get("key")
                    for p in perm_resp.json().get("projects", [])
                    if p.get("key")
                }
                projects = [p for p in projects if p["key"] in writable_keys]
            # If the permissions call fails we fall back to the unfiltered
            # list rather than failing the whole request — the user will
            # simply see "no permission" if they pick a read-only project.

        # Further narrow to "projects I work in" using /project/recent.
        # This endpoint returns up to 20 projects the authenticated user
        # has recently viewed.  At Duke where CREATE_ISSUES is granted on
        # ~500 projects, this cuts the dropdown to the handful the user
        # actually uses.  Falls back to the full writable list if empty.
        if recent_only and projects:
            # nosemgrep: python.flask.security.injection.ssrf-requests.ssrf-requests
            recent_resp = requests.get(
                recent_url,
                params={"maxResults": 20},
                **cfg.kwargs,
            )
            if recent_resp.ok:
                recent_keys = {
                    p.get("key")
                    for p in recent_resp.json()
                    if isinstance(p, dict) and p.get("key")
                }
                narrowed = [p for p in projects if p["key"] in recent_keys]
                # Only apply the narrowing if it leaves at least one
                # project — otherwise the dropdown would be empty for
                # users with no recent Jira activity.
                if narrowed:
                    projects = narrowed

    except HTTPException:
        raise
    except requests.exceptions.ConnectionError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Could not reach Jira: {exc}",
        )
    except requests.exceptions.Timeout:
        raise HTTPException(
            status_code=504,
            detail=f"Timed out connecting to Jira",
        )
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Unexpected error fetching Jira projects: {exc}",
        )

    return projects
