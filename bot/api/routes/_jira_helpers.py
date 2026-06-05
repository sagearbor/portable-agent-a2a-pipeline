"""
Shared Jira helpers — SSRF validation and credential resolution.

Extracted from jira_projects.py so that all Jira-facing routes reuse the
same base_url allowlist check and credential resolution logic.
"""

import os
from urllib.parse import urlparse

from fastapi import HTTPException


# ---------------------------------------------------------------------------
# SSRF allowlist
# ---------------------------------------------------------------------------
_ALLOWED_SCHEME = "https"
_ALLOWED_HOST_SUFFIX = ".atlassian.net"


def validate_base_url(raw: str) -> str:
    """
    Validate that *raw* is an https://*.atlassian.net address.

    Returns the normalised base (scheme + host only, no trailing slash)
    or raises ``HTTPException(400)`` if the URL does not match the allowlist.
    """
    parsed = urlparse(raw.strip())

    if parsed.scheme != _ALLOWED_SCHEME:
        raise HTTPException(
            status_code=400,
            detail=(
                f"base_url must use https://.  "
                f"Received scheme: '{parsed.scheme or '(none)'}'"
            ),
        )

    hostname = (parsed.hostname or "").lower()
    if not hostname.endswith(_ALLOWED_HOST_SUFFIX):
        raise HTTPException(
            status_code=400,
            detail=(
                f"base_url hostname must end with '{_ALLOWED_HOST_SUFFIX}'.  "
                f"Received hostname: '{hostname or '(none)'}'"
            ),
        )

    # Reconstruct a clean base — scheme + host only, no path/query/fragment
    clean = f"{_ALLOWED_SCHEME}://{hostname}"
    if parsed.port:
        clean += f":{parsed.port}"

    return clean


# ---------------------------------------------------------------------------
# Jira request config — per-user OAuth accessor
#
# The app is OAuth-only.  When the user is signed in via Atlassian OAuth 3LO
# (see auth_jira.py), Jira requests must:
#   * use Bearer auth (Authorization header)
#   * target https://api.atlassian.com/ex/jira/{cloud_id}  (not the site URL)
#
# There is intentionally NO service-account fallback: every Jira call is
# attributed to the signed-in user so their own Jira permissions apply.  A
# request with no valid session raises HTTPException(401) and the UI prompts
# the user to sign in.
#
#     cfg = get_jira_request_config(request, site_base_url)   # 401 if not signed in
#     resp = requests.get(f"{cfg.base}/rest/api/3/project/search", **cfg.kwargs)
#
# The previous JIRA_EMAIL/JIRA_API_TOKEN Basic-auth fallback (and the
# get_jira_auth helper) were removed: they let any visitor read or write Jira
# as the bot account regardless of their own permissions.  The CLI/pipeline
# path (core/tools/jira_tool.py) still supports env-var credentials for
# headless/batch use; see git history if the request-time fallback is ever
# needed again.
# ---------------------------------------------------------------------------

from dataclasses import dataclass
from typing import Any


@dataclass
class JiraRequestConfig:
    """Configuration for a Jira REST call: base URL + requests.* kwargs."""
    base: str                    # prefix to concat with /rest/api/3/...
    kwargs: dict[str, Any]       # pass to requests.get/post as **kwargs
    signed_in: bool              # always True (we raise 401 otherwise)
    principal: str               # email or display name (for logging/UI)


def get_oauth_request_config(request, site_base_url: str) -> JiraRequestConfig:
    """
    Return the Jira call config for the signed-in user's OAuth session, or
    raise ``HTTPException(401)`` when there is no valid session.

    ``site_base_url`` must already be validated by ``validate_base_url``.
    It is accepted for signature symmetry (and possible future use) even
    though OAuth 3LO calls target api.atlassian.com rather than the site URL.
    """
    # Lazy import to avoid circular dependency with auth_jira.py
    try:
        from bot.api.routes.auth_jira import get_oauth_session
        sess = get_oauth_session(request)
    except Exception:
        sess = None

    if not (sess and sess.get("access_token") and sess.get("cloud_id")):
        raise HTTPException(
            status_code=401,
            detail="Sign in to Jira to continue.",
        )

    return JiraRequestConfig(
        base=f"https://api.atlassian.com/ex/jira/{sess['cloud_id']}",
        kwargs={
            "headers": {
                "Authorization": f"Bearer {sess['access_token']}",
                "Accept":        "application/json",
            },
            "timeout": 15,
        },
        signed_in=True,
        principal=sess.get("email") or sess.get("display_name") or "oauth-user",
    )


# Backwards-compatible alias — existing routes import this name.
get_jira_request_config = get_oauth_request_config
