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
