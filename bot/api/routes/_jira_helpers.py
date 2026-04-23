"""
Shared Jira helpers — SSRF validation and credential resolution.

Extracted from jira_projects.py so that all Jira-facing routes reuse the
same base_url allowlist check and credential resolution logic.
"""

import os
from urllib.parse import urlparse

from fastapi import HTTPException
from requests.auth import HTTPBasicAuth


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


def get_jira_auth(
    email: str | None = None,
    token: str | None = None,
) -> HTTPBasicAuth:
    """
    Resolve Jira credentials and return an ``HTTPBasicAuth`` instance.

    Caller-supplied *email* / *token* take precedence; when omitted (or
    empty) the function falls back to the ``JIRA_EMAIL`` and
    ``JIRA_API_TOKEN`` environment variables.

    Raises ``HTTPException(500)`` if no credentials are available.
    """
    resolved_email = (email or "").strip() or os.environ.get("JIRA_EMAIL", "")
    resolved_token = (token or "").strip() or os.environ.get("JIRA_API_TOKEN", "")

    if not resolved_email or not resolved_token:
        raise HTTPException(
            status_code=500,
            detail=(
                "No Jira credentials available.  "
                "Set JIRA_EMAIL and JIRA_API_TOKEN in the server environment."
            ),
        )

    return HTTPBasicAuth(resolved_email, resolved_token)


# ---------------------------------------------------------------------------
# Jira request config — unified auth accessor for Basic + OAuth paths
#
# When the user is signed in via Atlassian OAuth 3LO (see auth_jira.py),
# requests must:
#   * use Bearer auth (Authorization header), not Basic
#   * target https://api.atlassian.com/ex/jira/{cloud_id}  (not the site URL)
#
# When no OAuth session exists, fall back to the service-account Basic auth
# against the site URL exactly as before.  This helper returns the full
# kwargs dict to pass to ``requests.*`` plus the base URL to prepend, so
# existing routes can switch over with a one-line change:
#
#     cfg = get_jira_request_config(request, site_base_url)
#     resp = requests.get(f"{cfg.base}/rest/api/3/project/search", **cfg.kwargs)
# ---------------------------------------------------------------------------

from dataclasses import dataclass
from typing import Any


@dataclass
class JiraRequestConfig:
    """Configuration for a Jira REST call: base URL + requests.* kwargs."""
    base: str                    # prefix to concat with /rest/api/3/...
    kwargs: dict[str, Any]       # pass to requests.get/post as **kwargs
    signed_in: bool              # True when OAuth session is active
    principal: str               # email or display name (for logging/UI)


def get_jira_request_config(request, site_base_url: str) -> JiraRequestConfig:
    """
    Return the correct (base, kwargs) to call Jira for this request.

    Preference order:
      1. Atlassian OAuth 3LO session (bearer token, via api.atlassian.com)
      2. JIRA_EMAIL / JIRA_API_TOKEN service-account Basic auth (site URL)

    ``site_base_url`` must already be validated by ``validate_base_url``.

    Not yet used by routes — introduced as scaffolding so the switchover
    to per-user OAuth is a one-line change per route.
    """
    # Lazy import to avoid circular dependency with auth_jira.py
    try:
        from bot.api.routes.auth_jira import get_oauth_session
        sess = get_oauth_session(request)
    except Exception:
        sess = None

    if sess and sess.get("access_token") and sess.get("cloud_id"):
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

    # Fallback: service account Basic auth
    auth = get_jira_auth()
    return JiraRequestConfig(
        base=site_base_url,
        kwargs={
            "auth":    auth,
            "headers": {"Accept": "application/json"},
            "timeout": 15,
        },
        signed_in=False,
        principal=os.environ.get("JIRA_EMAIL", "service-account"),
    )
