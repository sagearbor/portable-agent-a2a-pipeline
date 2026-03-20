# IT Request: SageJiraBot Azure Infrastructure

**Requestor:** scb2@duke.edu
**Project:** SageJiraBot — meeting transcript to Jira tickets via AI
**Subscription:** 2c69c8ba-1dc1-444a-9a18-a483b0be57db
**Existing AI resource:** `ai-foundry-dcri-sage` in `rg-dcri-prod-ai-foundry`
**Date:** 2026-03-20

---

## What This Is

SageJiraBot converts Teams meeting transcripts into Jira tickets using Azure AI Foundry
(the existing `ai-foundry-dcri-sage` endpoint). The bot is a Python FastAPI application
packaged as a Docker container. All AI calls go through Azure — no data leaves the Duke
tenant.

Code is at: https://github.com/sagearbor/portable-agent-a2a-pipeline

---

## Items Requested

### 1. Azure Container Registry (ACR)

**Why:** Need somewhere to push the Docker image before deploying to Container Apps.

**Request:**
- Create an ACR instance (Basic SKU, ~$5/month)
- Suggested name: `acrdcriprodbot` (or follow naming convention)
- Resource group: `rg-dcri-prod-ai-foundry` (so it's near the AI endpoint) or a new `rg-dcri-prod-bot`
- Grant `scb2@duke.edu` the `AcrPush` role so I can push images

---

### 2. Azure Container Apps Environment + Container App

**Why:** This is where the bot runs. Container Apps scales to zero (no cost when idle)
and wakes up in ~2 seconds when a Teams message arrives.

**Request:**
- Create a Container Apps Environment (Consumption plan)
- Create a Container App named `sagejirabot`
  - Image: from the ACR above
  - Min replicas: 0 (scale to zero), Max: 2
  - CPU: 0.5 vCPU, Memory: 1 Gi
  - Port: 9000
  - Ingress: external HTTPS (needs a public URL for Azure Bot Service to call)
- Grant `scb2@duke.edu` `Contributor` on the Container App so I can deploy updates

**Environment variables the Container App needs:**
```
AZURE_OPENAI_ENDPOINT=<existing AI Foundry endpoint>
JIRA_BASE_URL=https://dcri.atlassian.net
JIRA_EMAIL=<service account email>
JIRA_API_TOKEN=<service account token>
JIRA_PROJECT_KEY=ST
PROVIDER=azure
AZURE_AUTH_MODE=managed_identity
SERIES_STORE_BACKEND=blob
AZURE_STORAGE_ACCOUNT_URL=https://<storage account>.blob.core.windows.net
BOT_APP_ID=<from item 4 below>
BOT_APP_PASSWORD=<from item 4 below>
```

---

### 3. Azure Storage Account

**Why:** The bot stores two types of state that must survive container restarts:
- **Blob Storage**: recurring meeting Jira defaults (a small JSON file, ~1KB)
- **Table Storage**: active bot sessions during meetings (one row per meeting in progress)

Both are tiny and cheap (<$1/month).

**Request:**
- Create a Storage Account (Standard LRS, or add to existing if appropriate)
- Suggested name: `stdhpdcriprodsagebot`
- Create a Blob container named `sagejirabot`
- Grant the Container App's **managed identity** the `Storage Blob Data Contributor` role
- Grant the Container App's **managed identity** the `Storage Table Data Contributor` role
- Share the storage account URL: `https://<name>.blob.core.windows.net`

> Note: The Container App uses managed identity to access storage — no connection string
> or storage key needed anywhere in the code.

---

### 4. Azure Bot Service + App Registration

**Why:** Azure Bot Service is Microsoft's relay that routes Teams messages to our endpoint.
Without it, the bot cannot receive messages in Teams.

**Request:**
- Create an **Azure AD App Registration** for the bot:
  - Name: `SageJiraBot`
  - Supported account types: Single tenant (Duke only)
  - Generate a client secret (needed for Bot Framework auth)
  - Share the **Application (client) ID** and **client secret** value
- Create an **Azure Bot Service** resource:
  - Name: `sagejirabot-bot`
  - Messaging endpoint: `https://<container-app-url>/api/messages`
  - Link to the app registration above
  - Enable the **Microsoft Teams** channel

---

### 5. Azure AD SPA App Registration (for web UI sign-in)

**Why:** The web UI has a "Sign in with Microsoft" button that identifies who is submitting
tickets. This is a separate, simpler app registration from the bot (no secret needed).

**Request:**
- Create a separate **Azure AD App Registration**:
  - Name: `SageJiraBotWeb`
  - Supported account types: Single tenant
  - Platform: **Single-page application (SPA)**
  - Redirect URIs: `https://<container-app-url>` and `http://localhost:9000` (for dev)
  - Permissions: `openid`, `profile`, `email` — **no admin consent required** (basic user permissions)
  - Share the **Application (client) ID**

---

### 6. Managed Identity for Container App → AI Foundry

**Why:** Currently running locally with `az login`. In production the Container App needs
a managed identity so it can call the AI Foundry endpoint without storing credentials.

**Request:**
- Enable **system-assigned managed identity** on the `sagejirabot` Container App
- Grant that managed identity `Cognitive Services OpenAI Contributor` on `ai-foundry-dcri-sage`
  (same role that `scb2@duke.edu` has on the resource — just for the managed identity)

---

### 7. Graph API Permissions (Teams Transcript Fetching — Phase 2)

**Why:** Needed for the bot to automatically fetch meeting transcripts from Teams after a
meeting ends. Not needed for Phase 1 (manual paste works without this).

**Request (can defer until Phase 2):**
- On the Bot App Registration (item 4 above), add application permissions:
  - `OnlineMeetings.Read.All`
  - `OnlineMeetingTranscript.Read.All`
- **Admin consent required** for both

---

## Summary Table

| # | Item | Why Needed | Phase | Admin Consent? | Est. Cost |
|---|---|---|---|---|---|
| 1 | Azure Container Registry | Push Docker images | 1 — needed for deploy | No | ~$5/mo |
| 2 | Container App + Environment | Run the bot | 1 — needed for deploy | No | ~$0–10/mo |
| 3 | Storage Account (Blob + Table) | Persist sessions and series defaults | 1 — needed for deploy | No | <$1/mo |
| 4 | Azure Bot Service + App Reg | Route Teams messages to bot | 1 — Teams bot activation | No (client secret) | ~$0 (F0 tier) |
| 5 | SPA App Registration (web UI SSO) | Identify who submits tickets | 1 — SSO sign-in | No | $0 |
| 6 | Managed identity → AI Foundry | Auth for LLM calls in production | 1 — needed for deploy | No | $0 |
| 7 | Graph API permissions | Auto-fetch Teams transcripts | Phase 2 | **Yes** | $0 |

**Total new monthly cost estimate: ~$6–16/month** (vs. always-on VM cost)

---

## What I Can Do Myself (No IT Needed)

- Register the SPA app (item 5) — done, just need someone to review redirect URIs if policy requires
- Push Docker images once ACR exists and I have AcrPush role
- Deploy updated container versions once I have Contributor on the Container App
- `az login` locally for development

---

## Questions for IT

1. Should Container App go in `rg-dcri-prod-ai-foundry` or a new resource group?
2. Is there an existing Storage Account I can add a container to (to avoid creating a new one)?
3. Is there a naming convention I should follow for the Container App and ACR?
4. For the Bot App Registration (item 4), can I create it myself in portal.azure.com
   and just ask you to review/approve, or does IT need to create it?
