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

## Part A: SageJiraBot Deployment (Immediate Need)

### 1. Azure Container Registry (ACR)

**Why:** Need somewhere to push the Docker image before deploying to Container Apps.

**Request:**
- Create an ACR instance (Basic SKU, ~$5/month)
- Suggested name: `acrdcriprodbot` (or follow naming convention)
- Resource group: `rg-dcri-prod-ai-foundry` or a new dedicated `rg-dcri-prod-sage`
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
tickets. I tried to create this myself in portal.azure.com but received "You do not have
access" (Error 401) — the tenant policy blocks self-service app registration.

**Request:**
- Create a **Azure AD App Registration**:
  - Name: `SageJiraBotWeb`
  - Supported account types: Single tenant
  - Platform: **Single-page application (SPA)**
  - Redirect URIs: `https://<container-app-url>` and `http://localhost:9000` (for dev)
  - Permissions: `openid`, `profile`, `email` — no admin consent required
  - Share the **Application (client) ID**

---

### 6. Managed Identity for Container App → AI Foundry

**Why:** In production the Container App needs a managed identity to call the AI Foundry
endpoint without storing credentials anywhere.

**Request:**
- Enable **system-assigned managed identity** on the `sagejirabot` Container App
- Grant that managed identity `Cognitive Services OpenAI Contributor` on `ai-foundry-dcri-sage`

---

### 7. Graph API Permissions (Teams Transcript Fetching — Phase 2, can defer)

**Why:** Needed for the bot to automatically fetch meeting transcripts. Not needed for
Phase 1 (manual paste works without this).

**Request (defer to Phase 2):**
- On the Bot App Registration (item 4), add application permissions:
  - `OnlineMeetings.Read.All`
  - `OnlineMeetingTranscript.Read.All`
- **Admin consent required** for both

---

## Part A Summary: What IT Creates vs. What I Do

| # | Item | Who Creates | Role IT Grants Me | Phase | Admin Consent | Est. Cost |
|---|---|---|---|---|---|---|
| 1 | Azure Container Registry | IT creates | `AcrPush` on the ACR | 1 | No | ~$5/mo |
| 2 | Container App + Environment | IT creates | `Contributor` on the Container App | 1 | No | ~$0–10/mo |
| 3 | Storage Account (Blob + Table) | IT creates | Managed identity gets data roles | 1 | No | <$1/mo |
| 4 | Bot Service + App Reg | IT creates | Shares App ID + secret | 1 | No | $0 |
| 5 | SPA App Reg (web UI SSO) | IT creates | Shares Client ID | 1 | No | $0 |
| 6 | Managed identity → AI Foundry | IT assigns role | — | 1 | No | $0 |
| 7 | Graph API permissions | IT approves consent | — | Phase 2 | **Yes** | $0 |

**Total new monthly cost estimate: ~$6–16/month**

---

## Part B: Permanent Permissions to Unblock Future AI Development

**Context:** I am building AI agents and tooling on Azure AI Foundry. Currently every
new project requires an IT ticket for basic infrastructure setup, which creates weeks of
delay and blocks my agents from being able to provision their own dependencies. I would
like to be granted a small set of standing permissions that would let me work
autonomously within a dedicated scope.

### What I Am Asking For

#### B1. Azure AD: `Application Developer` role (tenant-level)

**What it does:** Allows me to create App Registrations (OAuth apps, SPA sign-in apps)
in Azure AD without admin rights. I currently get "You do not have access" when trying
to open App Registrations in the portal (Error 401). This is the **most impactful
single permission** for AI development.

**Scope:** Tenant-wide, but the role is non-admin — I cannot consent to Graph API
permissions that require admin consent, cannot modify other users' apps, cannot grant
roles. I can only create and manage my own app registrations.

**Why I need it:** Every AI project that has a web UI or needs identity verification
requires an app registration. Without this, each one is an IT ticket.

---

#### B2. `Contributor` on a dedicated resource group `rg-dcri-prod-sage`

**What it does:** Allows me to create and manage Azure resources (Container Apps,
Storage Accounts, Container Registries) within only that resource group. Has no effect
on any other resource group.

**Why I need it:** Currently I can use the AI Foundry endpoint but cannot deploy the
applications that consume it. Every deployment is an IT ticket. With Contributor on a
dedicated RG, I can:
- Create Container Apps (deploy the bot)
- Create Storage Accounts (state persistence)
- Create Container Registries (push Docker images)
- All scoped only to `rg-dcri-prod-sage`

**I would NOT have:**
- Access to any other resource group
- Ability to modify `rg-dcri-prod-ai-foundry` (the AI Foundry resources)
- Subscription-level access

---

#### B3. `Cognitive Services OpenAI Contributor` on `ai-foundry-dcri-sage` for any managed identity I create

**What it does:** Allows me to grant new managed identities (on Container Apps I deploy)
access to the AI Foundry endpoint, so my applications can authenticate as themselves
rather than as me personally.

**Currently:** I have this role for my personal account (`scb2@duke.edu`). When I
deploy a Container App, the app's managed identity also needs this role — and I
cannot grant it myself because I don't have role assignment permissions.

**Alternative ask:** Grant me `User Access Administrator` scoped only to
`ai-foundry-dcri-sage` (not the whole subscription), which lets me assign roles on that
one resource only.

---

### Part B Summary: Permissions to Request

| Permission | Scope | What It Unlocks | Risk to IT |
|---|---|---|---|
| `Application Developer` (Azure AD role) | Tenant | Create app registrations for OAuth/SSO | Low — can't grant admin consent or modify others' apps |
| `Contributor` on `rg-dcri-prod-sage` | One resource group only | Deploy Container Apps, Storage, ACR | Low — fully isolated RG |
| `User Access Administrator` on `ai-foundry-dcri-sage` | One resource only | Grant managed identity access to AI Foundry | Low — scoped to one resource |

With these three, I can develop and deploy new AI agents end-to-end without an IT ticket
for each project. All work stays in the Duke tenant and Duke Azure subscription.

---

## Questions for IT

1. Should Container App go in `rg-dcri-prod-ai-foundry` or a new `rg-dcri-prod-sage`?
   (I am requesting Contributor on the new RG as Part B above — creating it as a new
   isolated RG makes the permission easier to scope.)
2. Is there an existing Storage Account I can add a container to (to avoid creating a new one)?
3. Is there a naming convention I should follow for the Container App and ACR?
4. For Part B — is `Application Developer` a role IT can grant, or does it require a
   different process (e.g., PIM request)?
