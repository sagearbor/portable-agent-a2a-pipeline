"""
GET /api/v1/jira/context

Returns project context (epics, sprints, fix versions) for a given Jira
project.  The web UI calls this when the user selects a project so that
draft tickets can be assigned to an existing epic, sprint, or fix version.

Endpoint:
    GET /api/v1/jira/context?base_url=https://org.atlassian.net&project_key=ST

Returns:
    200  {"epics": [...], "sprints": [...], "fix_versions": [...]}
    400  {"detail": "base_url must be an https://....atlassian.net address"}
    401  {"detail": "Invalid Jira credentials"}

Each section returns an empty array on failure so the endpoint never
crashes entirely even when one Jira API is unavailable (e.g. Kanban
boards have no sprints).

SSRF mitigation: same allowlist as jira_projects.py — only
https://*.atlassian.net addresses are accepted.  Fixed API paths are
appended by this code; the caller cannot influence them.
"""

import logging
import requests

from fastapi import APIRouter, HTTPException, Query, Request

from bot.api.routes._jira_helpers import (
    validate_base_url,
    get_jira_auth,
    get_jira_request_config,
)

router = APIRouter()
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal helpers — each returns a list and never raises
# ---------------------------------------------------------------------------

def _fetch_epics(
    base: str,
    project_key: str,
    auth,
    headers: dict,
) -> list[dict]:
    """
    Fetch open epics via POST /rest/api/3/search/jql.

    Returns list of {"key", "summary", "status"} dicts.
    """
    jql = (
        f'project = "{project_key}" '
        f'AND issuetype = Epic '
        f'AND statusCategory != Done '
        f'ORDER BY created DESC'
    )
    payload = {
        "jql": jql,
        "fields": ["key", "summary", "status"],
        "maxResults": 50,
    }

    try:
        resp = requests.post(
            f"{base}/rest/api/3/search/jql",
            json=payload,
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
            logger.warning("Epic query failed %s: %s", resp.status_code, resp.text[:200])
            return []

        epics = []
        for issue in resp.json().get("issues", []):
            fields = issue["fields"]
            epics.append({
                "key":     issue["key"],
                "summary": fields.get("summary", ""),
                "status":  fields.get("status", {}).get("name", ""),
            })
        return epics

    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("Epic query error: %s", exc)
        return []


def _fetch_sprints(
    base: str,
    project_key: str,
    auth,
    headers: dict,
) -> list[dict]:
    """
    Fetch active and future sprints via the Jira Agile REST API.

    1. GET /rest/agile/1.0/board?projectKeyOrId=<key> to find board ID
    2. GET /rest/agile/1.0/board/{id}/sprint?state=active,future

    Returns list of {"id", "name", "state"} dicts.
    Kanban boards may have no sprints — returns [] in that case.
    """
    try:
        # Step 1: find the board
        board_resp = requests.get(
            f"{base}/rest/agile/1.0/board",
            params={"projectKeyOrId": project_key, "maxResults": 1},
            auth=auth,
            headers=headers,
            timeout=15,
        )

        if board_resp.status_code in (401, 403):
            raise HTTPException(
                status_code=401,
                detail="Invalid Jira credentials — check your email and API token.",
            )

        if not board_resp.ok:
            logger.warning(
                "Board lookup failed %s: %s",
                board_resp.status_code, board_resp.text[:200],
            )
            return []

        boards = board_resp.json().get("values", [])
        if not boards:
            return []

        board_id = boards[0]["id"]

        # Step 2: fetch sprints for that board
        sprint_resp = requests.get(
            f"{base}/rest/agile/1.0/board/{board_id}/sprint",
            params={"state": "active,future"},
            auth=auth,
            headers=headers,
            timeout=15,
        )

        if not sprint_resp.ok:
            # Kanban boards return 400 for sprint endpoints — not an error
            logger.info(
                "Sprint query returned %s (board %s may be Kanban)",
                sprint_resp.status_code, board_id,
            )
            return []

        sprints = []
        for s in sprint_resp.json().get("values", []):
            sprints.append({
                "id":    s["id"],
                "name":  s.get("name", ""),
                "state": s.get("state", ""),
            })
        return sprints

    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("Sprint query error: %s", exc)
        return []


def _fetch_fix_versions(
    base: str,
    project_key: str,
    auth,
    headers: dict,
) -> list[dict]:
    """
    Fetch unreleased fix versions via GET /rest/api/3/project/<key>/version.

    Returns list of {"id", "name", "releaseDate"} dicts.
    """
    try:
        resp = requests.get(
            f"{base}/rest/api/3/project/{project_key}/version",
            params={"status": "unreleased", "orderBy": "name"},
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
            logger.warning(
                "Fix version query failed %s: %s",
                resp.status_code, resp.text[:200],
            )
            return []

        versions = []
        for v in resp.json().get("values", []):
            versions.append({
                "id":          str(v["id"]),
                "name":        v.get("name", ""),
                "releaseDate": v.get("releaseDate"),
            })
        return versions

    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("Fix version query error: %s", exc)
        return []


def _fetch_project_members(
    base: str,
    project_key: str,
    auth,
    headers: dict,
) -> list[dict]:
    """
    Fetch actual project members (not the full org user list).

    Queries project role memberships and collects unique human users
    from roles like Administrators, Developers, Scrum Master, etc.
    Sorted alphabetically by display name.
    """
    try:
        # Get all roles for the project
        roles_resp = requests.get(
            f"{base}/rest/api/3/project/{project_key}/role",
            auth=auth, headers=headers, timeout=15,
        )
        if not roles_resp.ok:
            logger.warning("Project roles query failed %s", roles_resp.status_code)
            return []

        seen_ids = set()
        users = []

        # Iterate each role and collect human actors
        # Skip the addons role — it contains app/bot service accounts, not humans
        skip_roles = {"atlassian-addons-project-access"}
        for role_name, role_url in roles_resp.json().items():
            if role_name in skip_roles:
                continue
            # Jira returns absolute URLs pointing at the site host
            # (https://<site>.atlassian.net/...). Under OAuth 3LO we must hit
            # api.atlassian.com/ex/jira/{cloud_id} instead, so rewrite the URL
            # to use the same `base` we used for the roles call.
            idx = role_url.find("/rest/")
            if idx != -1:
                role_url = f"{base}{role_url[idx:]}"
            try:
                r = requests.get(role_url, auth=auth, headers=headers, timeout=10)
                if not r.ok:
                    logger.warning("Role %s query failed %s", role_name, r.status_code)
                    continue
                for actor in r.json().get("actors", []):
                    # Only include human users — skip groups
                    if actor.get("type") != "atlassian-user-role-actor":
                        continue
                    acct = actor.get("actorUser", {}).get("accountId", "")
                    if not acct or acct in seen_ids:
                        continue
                    seen_ids.add(acct)
                    users.append({
                        "accountId":   acct,
                        "displayName": actor.get("displayName", ""),
                        "email":       actor.get("actorUser", {}).get("emailAddress", ""),
                    })
            except Exception:
                continue

        # Sort alphabetically by display name
        users.sort(key=lambda u: u["displayName"].lower())
        return users

    except Exception as exc:
        logger.warning("Project members query error: %s", exc)
        return []


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------

@router.get("/jira/context")
async def get_jira_context(
    request:     Request,
    base_url:    str = Query(..., description="Jira base URL, e.g. https://org.atlassian.net"),
    project_key: str = Query(..., description="Jira project key, e.g. ST"),
):
    """
    Return project context — open epics, active/future sprints, and
    unreleased fix versions — so the web UI can offer smart defaults
    when creating tickets.

    Each section is fetched independently; if one fails (e.g. Kanban
    boards have no sprints) the others still return data.

    Auth preference: OAuth session token when signed in, else service-account.
    (Granular scopes read:jql:jira + read:issue:jira + read:issue-details:jira
    + read:issue-meta:jira must be granted on the Atlassian OAuth app for
    /rest/api/3/search/jql to succeed under OAuth 3LO.)
    """
    # SSRF guard
    base_validated = validate_base_url(base_url)

    cfg = get_jira_request_config(request, base_validated)

    # Derive (auth, headers) from cfg to keep existing helper signatures.
    # Under OAuth: auth=None, headers contain the Bearer token.
    # Under Basic: auth=HTTPBasicAuth, headers contain just Accept.
    auth = cfg.kwargs.get("auth")
    headers = dict(cfg.kwargs.get("headers", {}))
    headers.setdefault("Accept", "application/json")
    headers.setdefault("Content-Type", "application/json")
    base = cfg.base

    try:
        epics        = _fetch_epics(base, project_key, auth, headers)
        sprints      = _fetch_sprints(base, project_key, auth, headers)
        fix_versions = _fetch_fix_versions(base, project_key, auth, headers)
        users        = _fetch_project_members(base, project_key, auth, headers)
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

    return {
        "epics":        epics,
        "sprints":      sprints,
        "fix_versions": fix_versions,
        "users":        users,
    }
