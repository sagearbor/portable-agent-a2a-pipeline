# Deployment migration checklist — moving off the VM

This doc tracks everything that needs to change when SageJiraBot moves
from the current dev VM Docker POC (`https://aidemo.dcri.duke.edu/sageapp06/`)
to a dedicated **Azure Container Apps** deployment.

Kept in sync with the code so future sessions (and the future you) can tell
exactly which config needs to change without diffing three environments.


## IT-provided ACA environment (offered 2026-06)

DCRI IT now runs a **shared Azure Container Apps environment** with the full
infra stack already provisioned, offered as an easier alternative to the
Foundry-project SDK (use Foundry only for inference). Per IT it already has:

- **User-assigned managed identity (UAMI)** with all roles pre-wired
- **Postgres** (managed) — our durable store for sessions + per-user prefs
- **ADLS Gen2** — blob/file storage (transcripts, exports)
- **Key Vault** — secrets
- **Log Analytics** — centralized logs/metrics

This changes the migration from "we create the infra" to **"we obtain resource
IDs from IT and they grant the UAMI the role(s) our app needs."** What we still
own: the container image, the app config (env vars), and confirming the UAMI
has the specific roles below.

**Ask IT for, at the meeting:**
1. The **UAMI client ID** (→ our `AZURE_CLIENT_ID` env var — see §2; without it
   `DefaultAzureCredential` cannot pick the right user-assigned identity).
2. Confirmation the UAMI has **`Cognitive Services OpenAI User`** on
   `ai-foundry-dcri-sage` (our token scope is `cognitiveservices.azure.com`).
3. Confirmation the UAMI has **`Key Vault Secrets User`** on their Key Vault.
4. The **ACR name** + confirmation the UAMI has **`AcrPull`** (or how they want
   the image pushed/built — `az acr build` vs. push).
5. **Postgres** connection details (host, db, the secret in Key Vault) so we can
   move session/prefs storage off the ephemeral container filesystem (see §9).
6. The **Container Apps environment name** + resource group to deploy into.
7. Whether ingress should be **external** (public FQDN) or internal-only behind
   their gateway, and the **target port** they expect (we default 8080).


## Environments we support

| Env | Base URL | Purpose |
|-----|----------|---------|
| **local dev** | `http://localhost:3011` (or any 3011+) | Fast iteration, no Docker, SSH-tunnel from workstation |
| **VM POC** | `https://aidemo.dcri.duke.edu/sageapp06/` | Current demo env, Docker-on-VM, NGINX proxy |
| **Azure Container Apps (future)** | `https://<app>.<region>.azurecontainerapps.io` | Production, managed identity, auto-scale |

### VM port convention

Ports **3001–3010** on the dev VM are reserved for running POCs (e.g. 3006 =
sagejirabot Docker, 3007 = another active POC). Use **3011+** for ad-hoc
local testing so you don't collide with a teammate's POC. Whatever port you
pick for local OAuth testing must be registered as a callback URL in the
Atlassian app (`Authorization` tab at developer.atlassian.com).


## What changes between environments

### 1. Atlassian OAuth app — callback URLs

The app at `developer.atlassian.com/console/myapps/...` must list **every**
callback URL that will be used. Add them cumulatively — keep old ones so
previous envs can still roll back.

- `http://localhost:3011/api/v1/auth/jira/callback` *(local dev — use 3011+, see port convention below)*
- `https://aidemo.dcri.duke.edu/sageapp06/api/v1/auth/jira/callback` *(VM POC)*
- `https://<app-name>.<region>.azurecontainerapps.io/api/v1/auth/jira/callback` *(Azure)*
- `https://sagejirabot.dcri.duke.edu/api/v1/auth/jira/callback` *(future custom domain, if we map one)*

**Rule:** the URL the code sends in the authorize request must EXACTLY match
one of these (scheme, host, port, path). Trailing slashes matter. `http` vs
`https` matters.


### 2. `.env` — values that MUST change per environment

| Variable | local dev | VM POC | Azure Container Apps |
|---|---|---|---|
| `ATLASSIAN_OAUTH_REDIRECT_URI` | `http://localhost:3011/api/v1/auth/jira/callback` | `https://aidemo.dcri.duke.edu/sageapp06/api/v1/auth/jira/callback` | `https://<app>.<region>.azurecontainerapps.io/api/v1/auth/jira/callback` |
| `ATLASSIAN_OAUTH_POST_LOGIN_REDIRECT` | `/` | `/sageapp06/` | `/` |
| `SESSION_COOKIE_SECURE` | `false` (no HTTPS on localhost) | `true` | `true` |
| `SESSION_SECRET` | any 48-byte urlsafe | any 48-byte urlsafe | **fresh value**, store in Key Vault |
| `BOT_PORT` | `3011+` (any free VM port outside 3001–3010 POC range) | `3006` | `8080` (Container Apps default) |
| `AZURE_AUTH_MODE` | `az_login` | `api_key` (bearer-token hack) | `managed_identity` |
| `AZURE_OPENAI_KEY` | not needed | needed (hack) | not needed (managed identity) |
| `AZURE_CLIENT_ID` | not set | not set | **UAMI client ID** (required for user-assigned identity — get from IT) |
| `SESSION_COOKIE_DOMAIN` | not set | not set | may need to set when behind custom domain / multi-subdomain |


### 3. `.env` — values that stay the same across envs

- `ATLASSIAN_OAUTH_CLIENT_ID` — same app reg everywhere
- `ATLASSIAN_OAUTH_CLIENT_SECRET` — same; rotate separately, not per-env
- `JIRA_BASE_URL`, `JIRA_PROJECT_KEY`, `JIRA_PREFERRED_HOST`
- `JIRA_AI_HOUR_ESTIMATE_FIELD` — defaults to `customfield_16496` (the "AI Hour
  Estimate" field in dcri.atlassian.net); only set if pointing at another Jira
- `GRAPH_TENANT_ID`, `GRAPH_USER_EMAIL`
- `AZURE_OPENAI_ENDPOINT`


### 4. Infrastructure changes

| Concern | VM | Azure Container Apps |
|---|---|---|
| **Image source** | built locally, loaded into Docker | pushed to **Azure Container Registry**; Container App pulls from ACR |
| **TLS / HTTPS** | NGINX on VM terminates TLS | Container Apps auto-provisions TLS cert for `*.azurecontainerapps.io`; custom domain needs managed-cert config |
| **Reverse proxy path prefix** | `/sageapp06/` (NGINX) | None by default — app runs at root; can be added but no reason to |
| **Frontend `BASE` variable** (auto-detected in `bot/web/index.html`) | resolves to `/sageapp06` | resolves to `/` — same code, no change needed |
| **Auth to Azure OpenAI** | api_key with bearer token refreshed via cron | **system-assigned managed identity** on the Container App; `DefaultAzureCredential` picks it up |
| **Secrets** | plaintext `.env` on VM | **Azure Key Vault** + Container App secret references |
| **Logs** | `docker logs sagejirabot` | Container App log stream + Application Insights |
| **Restart** | `./start-docker.sh` (cron `@reboot`) | Container App auto-restart policy |


### 5. Secrets management checklist for Azure Container Apps

None of these should be plaintext in the manifest or in an `.env` committed to the image:

- `ATLASSIAN_OAUTH_CLIENT_SECRET` → Key Vault → ref as Container App secret
- `SESSION_SECRET` → Key Vault → ref as Container App secret
- `JIRA_API_TOKEN` (service-account fallback) → Key Vault
- `GRAPH_CLIENT_SECRET` (once IT provides Graph app reg) → Key Vault
- Everything else (client IDs, endpoints, URLs) can be plaintext env vars

Create the Container App with `--secrets` and `--env-vars` referencing them:

```bash
az containerapp create \
  --name sagejirabot \
  --resource-group rg-dcri-prod-sagejirabot \
  --environment <env-name> \
  --image <acr>.azurecr.io/sagejirabot:<tag> \
  --target-port 8080 \
  --ingress external \
  --user-assigned <managed-identity-resource-id> \
  --secrets \
    atlassian-oauth-secret=keyvaultref:https://kv/secrets/atlassian-secret \
    session-secret=keyvaultref:https://kv/secrets/session-secret \
    jira-api-token=keyvaultref:https://kv/secrets/jira-token \
  --env-vars \
    ATLASSIAN_OAUTH_CLIENT_SECRET=secretref:atlassian-oauth-secret \
    SESSION_SECRET=secretref:session-secret \
    JIRA_API_TOKEN=secretref:jira-api-token \
    ATLASSIAN_OAUTH_CLIENT_ID=<public-value> \
    ATLASSIAN_OAUTH_REDIRECT_URI=https://<app>.<region>.azurecontainerapps.io/api/v1/auth/jira/callback \
    SESSION_COOKIE_SECURE=true \
    JIRA_BASE_URL=https://dcri.atlassian.net \
    AZURE_AUTH_MODE=managed_identity \
    AZURE_CLIENT_ID=<uami-client-id-from-IT> \
    BOT_PORT=8080
```

> **The single most common managed-identity failure:** omitting `AZURE_CLIENT_ID`
> with a user-assigned identity. `DefaultAzureCredential` then can't tell which
> attached identity to use and either fails or grabs the wrong one. Our
> `client.py` binds to `ManagedIdentityCredential(client_id=AZURE_CLIENT_ID)`
> when the var is set — so set it. (Use `--user-assigned <uami-resource-id>` on
> the container app AND pass the UAMI's *client ID* as this env var; the
> resource ID and client ID are different values.)


### 5b. Managed-identity primer (what the object IDs in our notes mean)

Object IDs are the canonical way Azure RBAC grants permissions to non-human
principals. In our notes and IT tickets you'll see:

- `2e3747a7-935f-457a-9d39-968beea39d7a` — **system-assigned managed identity
  attached to the dev VM `alp-dsvm-003`**. This identity represents "whatever
  code runs on the VM as that machine." It is *not* the same as the user scb2
  or any Container App. It only becomes useful if the app is running directly
  on the VM (not in Docker), because Docker containers on the VM can't reach
  the VM's IMDS endpoint without extra plumbing. **Status: no OpenAI role yet.**
  An IT request to grant `Cognitive Services OpenAI User` on `ai-foundry-dcri-sage`
  to this object ID would eliminate the bearer-token refresh hack in
  `start-docker.sh` *for the VM POC only*. Not strictly required — the bearer
  token approach works — so this ticket is low-priority and optional.

- `<TBD — will be created when we deploy the Container App>` — **user-assigned
  managed identity for the production Container App**. This is the one that
  actually matters for the migration. Its object ID doesn't exist yet; we'll
  create it as part of cutover step 3 below. It gets granted
  `Cognitive Services OpenAI User` on `ai-foundry-dcri-sage`,
  `Key Vault Secrets User` on the Key Vault, and `AcrPull` on the Container
  Registry. Once created, the container app's `DefaultAzureCredential` picks
  it up automatically — no keys, no tokens, no cron.

So: two different IDs, two different scopes, only the second one is
required for production. The first is just tech-debt cleanup on the current
dev POC.


### 6. Migration cutover steps (the day we flip)

Run in order:

1. **Atlassian app** → add the Azure Container Apps callback URL. Keep VM URL too (rollback).
2. **Key Vault** → create secrets for the 4 sensitive env vars above.
3. **Managed identity** → create user-assigned identity, grant:
   - `Cognitive Services OpenAI User` on `ai-foundry-dcri-sage`
   - `Key Vault Secrets User` on the Key Vault
   - `AcrPull` on the Container Registry
4. **Container Registry** → push the `sagejirabot:<tag>` image
5. **Container Apps env** → create once (network, log workspace)
6. **Container App** → create with the command above
7. **DNS** (if custom domain) → CNAME `sagejirabot.dcri.duke.edu` → container app FQDN; add managed cert
8. **Smoke test** → hit `/health`, check version; hit `/api/v1/auth/jira/status` (should show `configured: true, signed_in: false`)
9. **User test** → sign in via the new URL, create a draft, create a ticket — confirm ticket reporter is the signed-in user
10. **Update VPN-only docs** — Container Apps is public so VPN constraint goes away for end-users (AI Foundry calls still go through private endpoint from the container's VNet)
11. **Retire the VM POC** → only after new env is proven; keep VM container stopped but config intact for ~30 days


### 7. Things that get simpler once we're on Container Apps

- No more cron-refreshed bearer token hack for Azure OpenAI — managed identity handles auth continuously.
- No more NGINX path-prefix gymnastics — root-mounted app, cleaner URLs.
- No more "rebuild Docker to pick up code change" — `az containerapp update --image <new-tag>` is the deploy unit.
- Secrets rotation becomes a one-line `az keyvault secret set` + `az containerapp update --revision-suffix` to trigger a reload.


### 8. What stays painful (known)

- **Atlassian OAuth redirect URIs are env-specific and must be pre-registered.** Every new env (staging, prod, preview) = another URI added to the app registration. Not automatable via API in a clean way; plan for it.
- **First-time consent screen** — if the Duke Jira instance requires admin approval for third-party OAuth apps, a site-admin will be prompted the first time a Duke user signs in through the Container Apps URL. One-time per user per site.
- **Cookie domain** — if we ever split the app across subdomains (e.g., `api.sagejirabot.dcri.duke.edu` and `ui.sagejirabot.dcri.duke.edu`), we'll need `SESSION_COOKIE_DOMAIN=dcri.duke.edu`. Not needed while everything is one FQDN.


### 9. State persistence — the real ACA gotcha (do this before scaling out)

On the VM, two things persist to the **local filesystem** (the `./data` bind
mount in `docker-compose.yml`):

- **Per-user prefs** — `bot/data/series_defaults_store.py` writes
  `bot/data/series_defaults.json` (`SERIES_STORE_BACKEND=file`).
- **Sessions** — `bot/session_store.py` (in-memory / process-local).

**ACA's local filesystem is ephemeral** and a revision can run **multiple
replicas**. Consequences if we change nothing:

- Any prefs written to the JSON file vanish on restart/redeploy and are **not
  shared** across replicas.
- In-memory sessions don't survive a restart and **break under multi-replica**
  (a user's second request can land on a replica that never saw their login →
  random sign-outs).

**Two options:**

| | Quick (demo-safe) | Proper (production) |
|---|---|---|
| What | Pin `minReplicas = maxReplicas = 1`; accept that a redeploy resets sessions/prefs | Move sessions + prefs to **IT's Postgres**; keep any large artifacts in **ADLS Gen2** |
| Effort | one ACA setting | implement a Postgres-backed `session_store` + `series_defaults_store` backend |
| Good for | tomorrow's demo / first deploy | once real users rely on it |

For the first cutover, **pin to a single replica** and move on. Schedule the
Postgres backend as the immediate follow-up — IT already provisioned Postgres
specifically so we don't have to run our own. (Note: `SERIES_STORE_BACKEND`
already exists as a seam — add a `postgres` backend alongside `file`.)


### 10. Expose the pipeline over MCP *and* REST

The REST API (FastAPI, `bot/api/main.py`) is already container-ready and is the
primary external interface. To also serve the **MCP** tools
(`mcp_servers/jira_server.py`, `transcript_server.py`) remotely:

- Today they call `mcp.run()` with **stdio** transport — fine for a local
  Claude Code subprocess, but **not reachable over the network**.
- For ACA, run FastMCP with **streamable-HTTP** transport
  (`mcp.run(transport="http", host="0.0.0.0", port=...)`) and expose it through
  ingress (either a second container/port or mounted under the same app).
- The internal agent→agent handoff stays as-is (plain dicts / "A2A"); MCP and
  REST are the *external* surfaces. This is a tool/service, not an agent, from
  the consumer's point of view.


### 11. Pre-flight checklist (quick scan before deploy)

- [ ] `AZURE_AUTH_MODE=managed_identity` **and** `AZURE_CLIENT_ID=<uami client id>` set
- [ ] UAMI has `Cognitive Services OpenAI User` on `ai-foundry-dcri-sage`
- [ ] Secrets in Key Vault, referenced as ACA secrets (not plaintext): `ATLASSIAN_OAUTH_CLIENT_SECRET`, `SESSION_SECRET`, `JIRA_API_TOKEN`, (later) `GRAPH_CLIENT_SECRET`
- [ ] `SESSION_SECRET` set to a fresh value (not the ephemeral per-process fallback)
- [ ] `SESSION_COOKIE_SECURE=true` (ACA serves HTTPS)
- [ ] Atlassian OAuth app: new ACA callback URL added; `ATLASSIAN_OAUTH_REDIRECT_URI` matches exactly
- [ ] Azure AD SSO app (if used): ACA redirect URI added
- [ ] `minReplicas = maxReplicas = 1` until Postgres-backed state lands (see §9)
- [ ] Image built and pushed to IT's ACR; UAMI has `AcrPull`
- [ ] `start-docker.sh` bearer-token hack is **not** on the prod path (VM-only)
- [ ] Smoke test `/health`; then sign in and create a ticket end-to-end
