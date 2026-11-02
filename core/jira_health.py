"""
Jira connection health — is the shared Jira credential usable right now?

The web UI polls GET /api/v1/jira/health and shows a red bar across every
page when the shared credential (JIRA_BASE_URL / JIRA_EMAIL / JIRA_API_TOKEN)
is missing, rejected or past its expiry date, so users find out *before*
they spend ten minutes on a transcript that cannot be turned into tickets.

Statuses
--------
    ok             credential present and accepted by Jira (/rest/api/3/myself = 200)
    missing        one or more of the three env vars is unset / blank / CHANGE_ME
    invalid        Jira answered 401/403, or JIRA_TOKEN_EXPIRES_ON is in the past
    expiring_soon  accepted by Jira, but JIRA_TOKEN_EXPIRES_ON is <= 14 days away
    unreachable    network error, timeout, 5xx or other unexpected answer —
                   we could not tell whether the token is good

Atlassian API tokens have an expiry date chosen at creation, but no API
exposes it, so the optional non-secret JIRA_TOKEN_EXPIRES_ON=YYYY-MM-DD lets
the operator record it and get warned two weeks ahead.

Results are cached for CACHE_TTL_SECONDS so page loads don't hammer Jira.

Security: the token, the email/token pair and the Authorization header are
never returned, logged or included in exception text.
"""

from __future__ import annotations

import datetime as _dt
import os
import threading
import time

import requests
from requests.auth import HTTPBasicAuth

CACHE_TTL_SECONDS = 300          # ~5 min
EXPIRY_WARNING_DAYS = 14
REQUEST_TIMEOUT_SECONDS = 8

STATUS_OK = "ok"
STATUS_MISSING = "missing"
STATUS_INVALID = "invalid"
STATUS_EXPIRING_SOON = "expiring_soon"
STATUS_UNREACHABLE = "unreachable"

# One wording, used by the banner AND by ticket-creation errors, so users see
# the same sentence wherever the failure surfaces.
JIRA_DOWN_MESSAGE = (
    "Jira connection is down — the Jira token has expired or is missing, "
    "so this site cannot create tickets until it is updated. "
    "Please contact the developer (Sage Arbor, scb2) or IT."
)
JIRA_UNREACHABLE_MESSAGE = (
    "Jira is not responding right now, so ticket creation may fail. "
    "This is usually temporary — try again in a few minutes. "
    "If it persists, contact the developer (Sage Arbor, scb2) or IT."
)


def expiring_message(days_left: int) -> str:
    if days_left <= 0:
        when = "today"
    elif days_left == 1:
        when = "in 1 day"
    else:
        when = f"in {days_left} days"
    return f"Jira token expires {when} — developer (Sage Arbor, scb2): renew it."


_REQUIRED_VARS = ("JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN")
_PLACEHOLDERS = {"", "CHANGE_ME"}

_lock = threading.Lock()
_cache: dict | None = None
_cache_at: float = 0.0


class JiraAuthError(RuntimeError):
    """Jira rejected the credential (HTTP 401). str(exc) is user-facing."""

    def __init__(self, message: str = JIRA_DOWN_MESSAGE, *, shared: bool = True):
        super().__init__(message)
        self.shared = shared   # True = shared env credential; False = per-user OAuth


def _now_utc() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _today() -> _dt.date:
    return _now_utc().date()


def _parse_expiry(raw: str | None) -> _dt.date | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return _dt.date.fromisoformat(raw)
    except ValueError:
        print(f"[jira_health] WARNING: JIRA_TOKEN_EXPIRES_ON is not YYYY-MM-DD; ignoring it")
        return None


def _result(status: str, message: str | None, days_left: int | None = None) -> dict:
    out = {
        "status": status,
        "message": message,
        "checked_at": _now_utc().isoformat(timespec="seconds"),
    }
    if days_left is not None:
        out["days_left"] = days_left
    return out


def _check_now() -> dict:
    values = {k: (os.environ.get(k) or "").strip() for k in _REQUIRED_VARS}
    if any(v in _PLACEHOLDERS for v in values.values()):
        return _result(STATUS_MISSING, JIRA_DOWN_MESSAGE)

    expires_on = _parse_expiry(os.environ.get("JIRA_TOKEN_EXPIRES_ON"))
    days_left = (expires_on - _today()).days if expires_on else None

    # Past its recorded expiry: report it as invalid before Jira even rejects it.
    if days_left is not None and days_left < 0:
        return _result(STATUS_INVALID, JIRA_DOWN_MESSAGE, days_left)

    base_url = values["JIRA_BASE_URL"].rstrip("/")
    try:
        resp = requests.get(
            f"{base_url}/rest/api/3/myself",
            auth=HTTPBasicAuth(values["JIRA_EMAIL"], values["JIRA_API_TOKEN"]),
            headers={"Accept": "application/json"},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        # Exception class only — the message can embed the request.
        print(f"[jira_health] Jira unreachable: {type(exc).__name__}")
        return _result(STATUS_UNREACHABLE, JIRA_UNREACHABLE_MESSAGE, days_left)

    code = resp.status_code
    if code in (401, 403):
        print(f"[jira_health] Jira rejected the shared credential (HTTP {code})")
        return _result(STATUS_INVALID, JIRA_DOWN_MESSAGE, days_left)
    if code != 200:
        print(f"[jira_health] Unexpected Jira answer (HTTP {code}); reporting unreachable")
        return _result(STATUS_UNREACHABLE, JIRA_UNREACHABLE_MESSAGE, days_left)

    if days_left is not None and days_left <= EXPIRY_WARNING_DAYS:
        return _result(STATUS_EXPIRING_SOON, expiring_message(days_left), days_left)
    return _result(STATUS_OK, None, days_left)


def get_jira_health(force: bool = False) -> dict:
    """Return the (cached, ~5 min) shared-credential health dict."""
    global _cache, _cache_at
    with _lock:
        if not force and _cache is not None and time.monotonic() - _cache_at < CACHE_TTL_SECONDS:
            return dict(_cache)
    result = _check_now()
    with _lock:
        _cache, _cache_at = result, time.monotonic()
    return dict(result)


def peek_jira_health() -> dict | None:
    """Last cached result without triggering a network call (for /health)."""
    with _lock:
        return dict(_cache) if _cache is not None else None


def mark_shared_credential_rejected() -> None:
    """Called when a real Jira call with the shared credential got a 401, so
    the banner turns red on the next poll instead of after the cache expires."""
    global _cache, _cache_at
    with _lock:
        _cache = _result(STATUS_INVALID, JIRA_DOWN_MESSAGE)
        _cache_at = time.monotonic()


def clear_cache() -> None:
    global _cache, _cache_at
    with _lock:
        _cache, _cache_at = None, 0.0
