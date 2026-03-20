# Jira Authentication Design: Multi-User Teams Bot

**Document version:** 1.0
**Date:** 2026-03-20
**Status:** Draft — implementation guidance for SageJiraBot

---

## Overview

`tools/jira_tool.py` currently uses a single personal API token from `.env`
(`JIRA_EMAIL` + `JIRA_API_TOKEN`). This is sufficient for a single developer
running the pipeline locally, but it breaks for a multi-user Teams bot for two
reasons:

1. **Authorization scope:** The personal token only grants access to the
   developer's own Jira projects. A Teams user from a different team cannot
   create tickets in their project with a credential they don't own.
2. **Audit trail:** All tickets are attributed to one person's account, making
   it impossible to trace which Teams user initiated the action.

This document describes three authentication options in order of increasing
complexity, with a recommended path for Phase 1 and Phase 2.

---

## Option A: Personal API Tokens (current approach)

### How it works now

`tools/jira_tool.py` reads three environment variables at startup:

```
JIRA_BASE_URL=https://dcri.atlassian.net
JIRA_EMAIL=scb2@duke.edu
JIRA_API_TOKEN=<personal token from id.atlassian.com>
```

The `_client()` helper constructs an `HTTPBasicAuth(email, token)` object and
every `create_ticket()` call uses it. There is one credential for the entire
process lifetime.

### Limitations for a multi-user bot

| Limitation | Impact |
|---|---|
| Single Jira identity | All tickets are attributed to the developer's account, not the actual Teams user |
| One project scope | The personal token only authorizes the developer's project access; other users' projects are 403 |
| Secret in `.env` | The API token is a personal credential — sharing it or storing it in a shared service violates Atlassian's terms and Duke security policy |
| No revocation isolation | Rotating or revoking the developer's personal token disables the entire bot |

**Verdict:** Acceptable for local single-user development. Not suitable for a
shared Teams bot serving multiple users.

---

## Option B: Jira Service Account (Recommended for Phase 1)

### Overview

Create a single dedicated Atlassian account for the bot (e.g.,
`sagejirabot@duke.edu`). That account is granted access to every Jira project
the bot may write to. The bot uses this account's API token for all API calls.

Before creating a ticket, the bot calls `check_project_permission()` to verify
that the requested project exists and the service account can access it. This
prevents silent failures where a user requests a project the service account
hasn't been granted.

### Architecture

```
Teams user: "paste project:OPS"
        |
        v
teams_handler.py
    -> check_project_permission("OPS")
           |-- 403/404: "Service account lacks access to OPS — contact IT"
           |-- 200: proceed
        |
        v
_run_pipeline_sync(...)
    -> agent3_jira.py -> create_ticket(...)
           uses JIRA_SERVICE_EMAIL + JIRA_SERVICE_TOKEN from env
```

### Code changes

1. **`tools/jira_tool.py`** — add `JiraCredentials` dataclass and
   `check_project_permission()` function; update `create_ticket()` to accept
   optional `credentials` parameter (see Task 2 below for full implementation).

2. **`.example.env`** — add `JIRA_SERVICE_EMAIL` and `JIRA_SERVICE_TOKEN`
   variables (see Task 3 below).

3. **`bot/teams/teams_handler.py`** — call `check_project_permission()` before
   running the pipeline. Example usage in `_run_pipeline_sync()`:

   ```python
   from tools.jira_tool import check_project_permission

   if not check_project_permission(project_key):
       raise ValueError(
           f"Service account lacks access to Jira project '{project_key}'. "
           "Ask IT to grant the sagejirabot service account access."
       )
   ```

4. **`config/settings.py`** (if it exists) — optionally expose
   `JIRA_SERVICE_EMAIL` / `JIRA_SERVICE_TOKEN` as typed settings rather than
   reading `os.environ` directly in the tool.

### `check_project_permission()` design

The function calls `GET {base_url}/rest/api/3/project/{project_key}` using the
service account credentials. This endpoint returns:

- **200 OK** — project exists and the authenticated user has Browse permission
- **404 Not Found** — project does not exist
- **403 Forbidden** — project exists but the authenticated user lacks access

The function returns `True` on 200 and `False` on 403/404. Any other status
code raises an exception (network error, auth misconfiguration, etc.).

Using the project detail endpoint (rather than the permission endpoint
`/rest/api/3/user/permission?projectKey=...`) is preferred because it is
available in all Jira Cloud plans and requires no special admin scope.

### Pros and cons

| | |
|---|---|
| **Pro** | Simple to activate — one IT request, one `.env` change |
| **Pro** | No OAuth app registration or callback URL required |
| **Pro** | Works immediately with the existing bot framework |
| **Pro** | Service account revocation is isolated from developer accounts |
| **Con** | All ticket writes are attributed to the service account, not the individual user |
| **Con** | The service account must be manually granted access each time a new project is onboarded |
| **Con** | Single shared credential — if the token leaks, all bot-accessible projects are exposed |

### IT requirements

- Create a dedicated Atlassian account: `sagejirabot@duke.edu` (or equivalent)
- Generate an API token for that account at `id.atlassian.com`
- Grant the service account "Browse Projects" + "Create Issues" permissions on
  each Jira project the bot will serve
- Store the token in Azure Key Vault (Phase 2) or `.env` (Phase 1 local dev)

---

## Option C: OAuth 2.0 (3-legged) — Best for Production

### Overview

Each Teams user authenticates to Atlassian once via the OAuth 2.0
Authorization Code flow. The bot receives and stores a per-user access token.
Subsequent Jira API calls use the token for that specific user, so every ticket
is attributed to the actual author.

### Flow

```
1. Teams user: "@SageJiraBot process"
2. Bot detects no Jira token for this user in session store
3. Bot sends a sign-in card with a link to the Atlassian OAuth authorization URL
4. User clicks link -> Atlassian login -> grants bot permission
5. Atlassian redirects to bot callback URL with authorization code
6. Bot exchanges code for access_token + refresh_token
7. Bot stores tokens in BotSession (keyed by user_id)
8. All subsequent API calls for this user use their personal access_token
9. Bot refreshes token automatically when it expires
```

### Architecture

```
Teams user -> sign-in card -> Atlassian OAuth app -> callback URL
                                                          |
                                               token stored in BotSession
                                                          |
                               create_ticket(credentials=JiraCredentials(...))
```

### Code changes required

1. **`bot/session_store.py`** — add `jira_access_token` and
   `jira_refresh_token` fields to `BotSession`:

   ```python
   @dataclass
   class BotSession:
       # ... existing fields ...
       jira_access_token:  Optional[str] = None
       jira_refresh_token: Optional[str] = None
       jira_token_expiry:  Optional[datetime] = None
   ```

2. **`tools/jira_tool.py`** — extend `JiraCredentials` to support OAuth bearer
   tokens in addition to Basic Auth:

   ```python
   @dataclass
   class JiraCredentials:
       base_url:    str
       project_key: str
       # Basic Auth (Option B service account)
       email:       Optional[str] = None
       api_token:   Optional[str] = None
       # OAuth (Option C)
       access_token: Optional[str] = None
   ```

   Update `create_ticket()` to use `Authorization: Bearer {access_token}` when
   `access_token` is provided.

3. **`bot/teams/teams_handler.py`** — add OAuth initiation and callback
   handling:

   ```python
   ATLASSIAN_AUTH_URL = "https://auth.atlassian.com/authorize"
   ATLASSIAN_TOKEN_URL = "https://auth.atlassian.com/oauth/token"
   ATLASSIAN_SCOPE = "read:jira-user write:jira-work offline_access"

   async def _initiate_jira_oauth(self, turn_context: TurnContext) -> None:
       """Send sign-in card to the user to start OAuth flow."""
       auth_url = (
           f"{ATLASSIAN_AUTH_URL}"
           f"?audience=api.atlassian.com"
           f"&client_id={os.environ['ATLASSIAN_CLIENT_ID']}"
           f"&scope={ATLASSIAN_SCOPE}"
           f"&redirect_uri={os.environ['ATLASSIAN_CALLBACK_URL']}"
           f"&state={user_id}"  # used to match callback to user
           f"&response_type=code"
           f"&prompt=consent"
       )
       # Send an OAuthCard or a simple text link
       await turn_context.send_activity(
           MessageFactory.text(f"Please [sign in to Jira]({auth_url}) to continue.")
       )
   ```

4. **`bot/api/main.py`** — add a `/oauth/callback` route that receives the
   code, exchanges it for tokens, and stores them in the session for the
   matching `user_id`.

5. **`.example.env`** — add OAuth app credentials:

   ```
   ATLASSIAN_CLIENT_ID=CHANGE_ME
   ATLASSIAN_CLIENT_SECRET=CHANGE_ME
   ATLASSIAN_CALLBACK_URL=https://<bot-host>/oauth/callback
   ```

### Pros and cons

| | |
|---|---|
| **Pro** | Tickets are attributed to the actual user — correct audit trail |
| **Pro** | Per-user permission enforcement — users can only create tickets where they personally have access |
| **Pro** | Granular revocation — one user's token can be revoked without affecting others |
| **Pro** | Industry standard — Atlassian recommends OAuth 2.0 for production integrations |
| **Con** | Requires IT to register an OAuth 2.0 app at `developer.atlassian.com` |
| **Con** | Requires a publicly reachable callback URL (Azure Container App or App Service) |
| **Con** | Significantly more code: token exchange, refresh logic, sign-in card UI |
| **Con** | Users must complete a one-time sign-in flow before the bot works for them |
| **Con** | Token storage in `BotSession` (in-memory) is lost on restart — requires persistent store (Azure Table Storage / Redis) for production |

### IT requirements

- Register an OAuth 2.0 (3LO) app at `developer.atlassian.com`
  - Callback URL: `https://<bot-host>/oauth/callback`
  - Scopes: `read:jira-user`, `write:jira-work`, `offline_access`
- Store `ATLASSIAN_CLIENT_ID` and `ATLASSIAN_CLIENT_SECRET` in Azure Key Vault
- The bot must have a public HTTPS endpoint (Azure Container App already
  satisfies this for Phase 2)

---

## Recommended Path

### Phase 1 Teams Bot: Option B (Service Account)

Option B is the right choice for the first Teams bot deployment. It requires
one IT request (create the service account and grant project access) and two
new environment variables. No OAuth app registration, no callback URL, no token
refresh logic. The bot is functional end-to-end in a single sprint.

The `check_project_permission()` guard ensures that if a user requests a
project the service account cannot access, they get a clear error message
rather than a silent 403 from the Jira API.

Limitation to communicate to users: tickets will show the service account
(`sagejirabot@duke.edu`) as the reporter, not the individual Teams user. This
is acceptable for an internal tool where the meeting itself is the audit trail.

### Phase 2: Option C (OAuth 2.0)

Once the bot is in production and users want proper ticket attribution, migrate
to Option C. The `JiraCredentials` dataclass introduced in Option B makes this
migration straightforward — the OAuth `access_token` becomes a new field on
`JiraCredentials`, and `create_ticket()` branches on whether `api_token` or
`access_token` is set.

The persistent session store (Azure Table Storage) required for token storage
is the same infrastructure upgrade needed for multi-instance bot deployment
anyway, so the two requirements land together naturally.

---

## Summary Table

| | Option A (current) | Option B (Phase 1) | Option C (Phase 2) |
|---|---|---|---|
| Credential type | Personal API token | Service account token | Per-user OAuth 2.0 |
| Ticket attribution | Developer | Service account | Individual user |
| Permission enforcement | Developer's access | Service account's access + `check_project_permission()` guard | User's own Jira access |
| IT requirements | None (already done) | Create service account | Register OAuth 2.0 app |
| Code complexity | Low (done) | Low (add `JiraCredentials` + permission check) | High (OAuth flow, token refresh, persistent store) |
| Suitable for multi-user | No | Yes (with limitations) | Yes (best) |
| Recommended for | Local dev only | Phase 1 Teams bot | Phase 2 production |
