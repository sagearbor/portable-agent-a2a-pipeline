"""
Atlassian OAuth 2.0 (3LO) for Jira Cloud — per-user sign-in.

Flow:
    GET  /api/v1/auth/jira/login     -> redirect to auth.atlassian.com
    GET  /api/v1/auth/jira/callback  -> exchange code, store token in session
    GET  /api/v1/auth/jira/status    -> is the caller signed in?
    POST /api/v1/auth/jira/logout    -> clear the session

When a user is signed in, other Jira routes can call ``get_oauth_session()``
to retrieve a Bearer token + Atlassian cloud id and write to Jira *as that
user*.  Writable-project filtering then matches the signed-in user's own
permissions automatically.

Environment variables required:
    ATLASSIAN_OAUTH_CLIENT_ID       (from developer.atlassian.com)
    ATLASSIAN_OAUTH_CLIENT_SECRET   (from developer.atlassian.com)
    ATLASSIAN_OAUTH_REDIRECT_URI    (must match the Callback URL registered
                                     in the OAuth app; e.g. during local
                                     dev: http://localhost:3006/api/v1/auth/jira/callback)
    SESSION_SECRET                  (used by SessionMiddleware to sign
                                     session cookies; any long random string)

Scopes requested (granular — all must also be enabled on the Atlassian OAuth
app under Permissions → Jira API → Configure → Granular scopes):

  Issue reads (for `/rest/api/3/search/jql` — the new enhanced JQL endpoint):
    read:jql:jira               run JQL
    read:issue:jira             read issue
    read:issue-details:jira     issue fields/body
    read:issue-meta:jira        issue metadata
    read:issue-type:jira        issue type info
    read:status:jira            status info
    read:field:jira             field definitions

  Project reads (projects, roles, versions):
    read:project:jira
    read:project-role:jira
    read:project-version:jira
    read:project-category:jira

  People reads (assignee picker + role members):
    read:user:jira
    read:avatar:jira
    read:group:jira
    read:application-role:jira

  Agile (sprints/boards):
    read:board-scope:jira-software
    read:sprint:jira-software

  Writes (ticket creation + linking):
    write:issue:jira
    write:comment:jira
    write:issue-link:jira

  Identity + refresh:
    read:me                     signed-in user's own profile
    offline_access              refresh tokens

Confluence scopes are intentionally NOT requested here — enable them on the
Atlassian OAuth app so they are available, but only add to this string when
Confluence features are implemented (keeps the consent screen minimal).

Security notes:
    * OAuth ``state`` is a 32-byte URL-safe random value stored in the
      session before redirect; verified on callback to prevent CSRF.
    * Tokens are stored server-side in the signed session cookie only —
      never exposed to JavaScript.  Cookie is HttpOnly + Secure + SameSite=Lax
      via SessionMiddleware configuration in main.py.
    * The callback URL is pulled from env (not user input), so the
      attacker cannot pivot the redirect target.
"""

import os
import secrets
import time
from urllib.parse import urlencode

import requests
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

router = APIRouter()


# ---------------------------------------------------------------------------
# Atlassian endpoints (fixed, public)
# ---------------------------------------------------------------------------

_AUTHORIZE_URL  = "https://auth.atlassian.com/authorize"
_TOKEN_URL      = "https://auth.atlassian.com/oauth/token"  # nosec — public Atlassian endpoint, not a secret
_ACCESSIBLE_URL = "https://api.atlassian.com/oauth/token/accessible-resources"

_SCOPES = (
    # Classic scopes — still required for endpoints like /project/search
    # that Atlassian kept on the classic scope model even under granular-enabled
    # 3LO. Both classic + granular must be enabled on the app registration.
    "read:jira-work write:jira-work read:jira-user "
    # Granular issue reads (required for the enhanced /rest/api/3/search/jql)
    "read:jql:jira read:issue:jira read:issue-details:jira read:issue-meta:jira "
    "read:issue-type:jira read:status:jira read:field:jira "
    # Granular project reads
    "read:project:jira read:project-role:jira read:project-version:jira read:project-category:jira "
    # Granular people reads
    "read:user:jira read:avatar:jira read:group:jira read:application-role:jira "
    # Granular agile
    "read:board-scope:jira-software read:sprint:jira-software "
    # Granular writes
    "write:issue:jira write:comment:jira write:issue-link:jira "
    # Identity + refresh
    "read:me offline_access"
)

_SID_KEY   = "jira_sid"          # small session-id pointer; value lives in _TOKEN_STORE
_STATE_KEY = "jira_oauth_state"  # session dict entry for CSRF state
_EXPIRY_SKEW_SECONDS = 60        # refresh tokens 60s before real expiry
# Hard cap on how long we keep any session.  Atlassian's OAuth "does your app
# store personal data?" form treats caching beyond 24 hours as storing personal
# data and requires the Personal Data Reporting API.  Expiring at 23h keeps us
# cleanly under the threshold so we can continue to answer "No" to that
# question.  Users re-authenticate once a day; acceptable for an internal tool.
_MAX_SESSION_AGE_SECONDS = 23 * 3600

# ---------------------------------------------------------------------------
# Server-side token store
#
# Why not in the session cookie?  Atlassian OAuth access tokens are JWTs
# (~2-4KB each) — combined with refresh token and user profile, the
# signed+base64'd session cookie blows past the browser's 4KB per-cookie
# limit and the browser *silently drops the Set-Cookie header*.  We only
# keep a short session id (~44 chars) in the cookie and park the big
# token blob in-process, keyed by the id.
#
# Trade-off: the store is in-process and in-memory.  On uvicorn restart
# all users are logged out.  For single-worker dev + the current Docker
# POC this is fine; production Azure Container Apps will want Redis or
# similar.  See docs/deployment-migration.md.
# ---------------------------------------------------------------------------
_TOKEN_STORE: dict[str, dict] = {}


# ---------------------------------------------------------------------------
# Helpers (used by other routes via import)
# ---------------------------------------------------------------------------

def _client_creds() -> tuple[str, str]:
    cid = (os.environ.get("ATLASSIAN_OAUTH_CLIENT_ID") or "").strip()
    sec = (os.environ.get("ATLASSIAN_OAUTH_CLIENT_SECRET") or "").strip()
    if not cid or cid == "CHANGE_ME" or not sec or sec == "CHANGE_ME":
        raise HTTPException(
            status_code=503,
            detail=(
                "Atlassian OAuth is not configured on this server. "
                "Set ATLASSIAN_OAUTH_CLIENT_ID and ATLASSIAN_OAUTH_CLIENT_SECRET "
                "in the server environment. See docs/design-jira-auth.md."
            ),
        )
    return cid, sec


def _redirect_uri() -> str:
    uri = (os.environ.get("ATLASSIAN_OAUTH_REDIRECT_URI") or "").strip()
    if not uri or uri == "CHANGE_ME":
        raise HTTPException(
            status_code=503,
            detail=(
                "Atlassian OAuth redirect URI is not configured. "
                "Set ATLASSIAN_OAUTH_REDIRECT_URI in the server environment."
            ),
        )
    return uri


def get_oauth_session(request: Request) -> dict | None:
    """
    Return the current user's OAuth session dict, refreshing the access token
    if needed.  ``None`` means no user is signed in — callers should fall back
    to service-account (Basic auth) credentials.

    Returned dict (when signed in):
        {
            "access_token":  "...",
            "refresh_token": "..." | None,
            "expires_at":    <unix-seconds>,
            "cloud_id":      "<Atlassian cloud id>",
            "site_url":      "https://<your-site>.atlassian.net",
            "account_id":    "<Atlassian account id>",
            "email":         "<Atlassian account email>",
            "display_name":  "...",
        }
    """
    sid = request.session.get(_SID_KEY)
    if not sid:
        return None
    sess = _TOKEN_STORE.get(sid)
    if not sess:
        # Server restarted or entry was evicted — drop the stale pointer
        request.session.pop(_SID_KEY, None)
        return None

    # Hard cap on session lifetime (see _MAX_SESSION_AGE_SECONDS note): once we
    # cross the threshold the entry is evicted even if Atlassian would still
    # honour the refresh token.  User gets bounced to re-login.
    if time.time() - sess.get("created_at", 0) > _MAX_SESSION_AGE_SECONDS:
        _TOKEN_STORE.pop(sid, None)
        request.session.pop(_SID_KEY, None)
        return None

    # Still valid?
    if time.time() < sess.get("expires_at", 0):
        return sess

    # Expired — try refresh
    refresh = sess.get("refresh_token")
    if not refresh:
        # No refresh token, session is stale
        _TOKEN_STORE.pop(sid, None)
        request.session.pop(_SID_KEY, None)
        return None

    try:
        client_id, client_secret = _client_creds()
    except HTTPException:
        return None  # OAuth disabled — treat session as stale

    resp = requests.post(
        _TOKEN_URL,
        json={
            "grant_type":    "refresh_token",
            "client_id":     client_id,
            "client_secret": client_secret,
            "refresh_token": refresh,
        },
        timeout=15,
    )
    if not resp.ok:
        # Refresh failed — session is dead
        _TOKEN_STORE.pop(sid, None)
        request.session.pop(_SID_KEY, None)
        return None

    tokens = resp.json()
    sess["access_token"] = tokens["access_token"]
    sess["expires_at"]   = int(time.time()) + int(tokens.get("expires_in", 3600)) - _EXPIRY_SKEW_SECONDS
    # Atlassian may rotate the refresh token
    if tokens.get("refresh_token"):
        sess["refresh_token"] = tokens["refresh_token"]
    _TOKEN_STORE[sid] = sess
    return sess


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/auth/jira/login")
async def jira_login(request: Request):
    """
    Start the Atlassian OAuth 3LO flow by redirecting the browser to
    auth.atlassian.com.  Stores a CSRF ``state`` value in the session so the
    callback can verify the response came from the same flow.
    """
    client_id, _ = _client_creds()
    redirect_uri = _redirect_uri()

    state = secrets.token_urlsafe(32)
    request.session[_STATE_KEY] = state

    params = {
        "audience":      "api.atlassian.com",
        "client_id":     client_id,
        "scope":         _SCOPES,
        "redirect_uri":  redirect_uri,
        "state":         state,
        "response_type": "code",
        "prompt":        "consent",
    }
    return RedirectResponse(f"{_AUTHORIZE_URL}?{urlencode(params)}")


@router.get("/auth/jira/callback")
async def jira_callback(
    request: Request,
    code:  str | None = None,
    state: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
):
    """
    Exchange the authorization ``code`` for access + refresh tokens, fetch
    the user's accessible Atlassian cloud resources, and store the result
    in the session.  Finally redirect back to the web UI.
    """
    if error:
        detail = f"Atlassian OAuth error: {error}"
        if error_description:
            detail += f" — {error_description}"
        raise HTTPException(status_code=400, detail=detail)

    if not code:
        raise HTTPException(status_code=400, detail="Missing authorization code.")

    expected = request.session.pop(_STATE_KEY, None)
    if not expected or state != expected:
        raise HTTPException(
            status_code=400,
            detail="Invalid OAuth state — possible CSRF attempt or expired flow. Try signing in again.",
        )

    client_id, client_secret = _client_creds()
    redirect_uri = _redirect_uri()

    # Step 1: exchange code for tokens
    try:
        token_resp = requests.post(
            _TOKEN_URL,
            json={
                "grant_type":    "authorization_code",
                "client_id":     client_id,
                "client_secret": client_secret,
                "code":          code,
                "redirect_uri":  redirect_uri,
            },
            timeout=15,
        )
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"Could not reach Atlassian token endpoint: {exc}")

    if not token_resp.ok:
        raise HTTPException(
            status_code=502,
            detail=f"Atlassian token exchange failed: {token_resp.status_code} {token_resp.text[:300]}",
        )
    tokens = token_resp.json()
    access_token = tokens.get("access_token")
    if not access_token:
        raise HTTPException(status_code=502, detail="Atlassian did not return an access token.")

    # Step 2: find the Jira site(s) this user authorized
    try:
        ar_resp = requests.get(
            _ACCESSIBLE_URL,
            headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
            timeout=15,
        )
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"Could not fetch accessible Atlassian resources: {exc}")

    if not ar_resp.ok:
        raise HTTPException(
            status_code=502,
            detail=f"Could not fetch accessible Atlassian resources: {ar_resp.status_code} {ar_resp.text[:300]}",
        )

    resources = ar_resp.json() or []
    if not resources:
        raise HTTPException(
            status_code=403,
            detail="This Atlassian account has no accessible Jira sites — ask a Jira admin to add you to the site.",
        )

    # Prefer dcri.atlassian.net if present; otherwise take the first site
    preferred_host = (os.environ.get("JIRA_PREFERRED_HOST") or "dcri.atlassian.net").lower()
    site = next(
        (r for r in resources if (r.get("url") or "").lower().endswith(preferred_host)),
        resources[0],
    )

    # Step 3: fetch the signed-in user's profile (for "Signed in as …" display)
    display_name = ""
    email = ""
    account_id = ""
    try:
        me_resp = requests.get(
            f"https://api.atlassian.com/ex/jira/{site['id']}/rest/api/3/myself",
            headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
            timeout=10,
        )
        if me_resp.ok:
            me = me_resp.json()
            display_name = me.get("displayName", "")
            email        = me.get("emailAddress", "")
            account_id   = me.get("accountId", "")
    except requests.RequestException:
        pass  # non-fatal — we can still use the token

    # Persist in the server-side token store; cookie only gets a small id
    sid = secrets.token_urlsafe(32)
    _TOKEN_STORE[sid] = {
        "access_token":  access_token,
        "refresh_token": tokens.get("refresh_token"),
        "expires_at":    int(time.time()) + int(tokens.get("expires_in", 3600)) - _EXPIRY_SKEW_SECONDS,
        "created_at":    int(time.time()),
        "cloud_id":      site["id"],
        "site_url":      site.get("url", ""),
        "account_id":    account_id,
        "email":         email,
        "display_name":  display_name,
    }
    request.session[_SID_KEY] = sid

    # Redirect back to the UI.  Prefer an explicit env override; otherwise
    # derive the app root from the registered callback URL by stripping the
    # known ``/api/v1/auth/jira/callback`` suffix.  This auto-adapts across
    # environments (localhost, /sageapp06/ behind NGINX, Azure Container
    # Apps) without requiring a separate env var per deployment.  We never
    # use a user-supplied value here, so this is not an open-redirect risk.
    post_login = (os.environ.get("ATLASSIAN_OAUTH_POST_LOGIN_REDIRECT") or "").strip()
    if not post_login:
        cb_path = "/api/v1/auth/jira/callback"
        if redirect_uri.endswith(cb_path):
            post_login = redirect_uri[: -len(cb_path)] + "/"
        else:
            post_login = "/"
    return RedirectResponse(post_login)


@router.get("/auth/jira/status")
async def jira_status(request: Request):
    """
    Lightweight status endpoint for the frontend to display "Signed in as …"
    without exposing the access token itself.
    """
    sess = get_oauth_session(request)
    if not sess:
        # Also report whether OAuth is configured server-side so the frontend
        # can show "Not configured" vs. "Sign in" appropriately.
        configured = bool(
            (os.environ.get("ATLASSIAN_OAUTH_CLIENT_ID") or "").strip() not in ("", "CHANGE_ME")
            and (os.environ.get("ATLASSIAN_OAUTH_CLIENT_SECRET") or "").strip() not in ("", "CHANGE_ME")
        )
        return JSONResponse({"signed_in": False, "configured": configured})

    return JSONResponse({
        "signed_in":    True,
        "configured":   True,
        "display_name": sess.get("display_name", ""),
        "email":        sess.get("email", ""),
        "site_url":     sess.get("site_url", ""),
        "expires_in":   max(0, int(sess["expires_at"] - time.time())),
    })


@router.post("/auth/jira/logout")
async def jira_logout(request: Request):
    """Clear the OAuth session (no Atlassian call — we just drop the tokens)."""
    sid = request.session.pop(_SID_KEY, None)
    if sid:
        _TOKEN_STORE.pop(sid, None)
    request.session.pop(_STATE_KEY, None)
    return {"ok": True}
