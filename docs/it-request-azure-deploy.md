# IT Request: SageJiraBot Azure Infrastructure

**Requestor:** scb2@duke.edu
**Subscription:** `2c69c8ba-1dc1-444a-9a18-a483b0be57db`
**Tenant:** `cb72c54e-4a31-4d9e-b14a-1ea36dfac94c`
**My object ID:** `1a565cd2-3dd5-44d1-a529-e3ca8ceff46c`
**Date:** 2026-03-20

SageJiraBot converts Teams meeting transcripts into Jira tickets using the existing
`ai-foundry-dcri-sage` Azure AI endpoint. It is a Docker container deployed to
Container Apps. All AI calls stay in the Duke Azure tenant.

Code: https://github.com/sagearbor/portable-agent-a2a-pipeline

---

## Part A: One-Time SageJiraBot Deployment

| # | What to Create/Grant | Details |
|---|---|---|
| 1 | Azure Container Registry (Basic) | Name: `acrdcriprodbot`; grant me `AcrPush` |
| 2 | Container Apps Environment + App `sagejirabot` | Consumption plan, 0–2 replicas, port 9000, external HTTPS; grant me `Contributor` on the app |
| 3 | Storage Account + Blob container `sagejirabot` | Standard LRS; grant Container App managed identity `Storage Blob Data Contributor` + `Storage Table Data Contributor` |
| 4 | Azure Bot Service + App Registration `SageJiraBot` | Single-tenant, client secret; share App ID + secret; enable Teams channel; set messaging endpoint to Container App URL |
| 5 | SPA App Registration `SageJiraBotWeb` | Single-tenant, SPA platform, redirect URIs: `https://<app-url>` + `http://localhost:9000`, permissions: `openid profile email`; share Client ID |
| 6 | Managed identity → AI Foundry | Enable system-assigned identity on Container App; grant it `Cognitive Services OpenAI Contributor` on `ai-foundry-dcri-sage` |
| 7 *(Phase 2)* | Graph API permissions on item 4 | `OnlineMeetings.Read.All` + `OnlineMeetingTranscript.Read.All`, admin consent required |

**Estimated cost: ~$6–16/month** (Container App scales to zero when idle)

---

## Part B: Standing Permissions to Unblock Future AI Work

I am building AI agents on Azure AI Foundry. Currently each new project requires an IT
ticket for basic setup. Three standing permissions — all tightly scoped — would let me
work autonomously within the Duke tenant.

### B1. Azure AD `Application Developer` role

**What it allows:** Create and manage my own app registrations (OAuth apps, SSO). Cannot
grant admin consent. Cannot modify other users' apps.

**Why blocked now:** I received Error 401 / "You do not have access" when opening
App Registrations in portal.azure.com.

**Commands for IT to run:**
```bash
# Assign Application Developer built-in role to scb2@duke.edu
az rest --method POST \
  --uri "https://graph.microsoft.com/v1.0/roleManagement/directory/roleAssignments" \
  --headers "Content-Type=application/json" \
  --body '{
    "principalId": "1a565cd2-3dd5-44d1-a529-e3ca8ceff46c",
    "roleDefinitionId": "cf1c38e5-3621-4004-a7cb-879624dced7c",
    "directoryScopeId": "/"
  }'
```

---

### B2. `Contributor` on a new dedicated resource group `rg-dcri-prod-sage`

**What it allows:** Create Container Apps, Storage Accounts, and Container Registries
within only this resource group. No access to any other resource group.

**Commands for IT to run:**
```bash
# Create the isolated resource group
az group create \
  --name rg-dcri-prod-sage \
  --location eastus \
  --subscription 2c69c8ba-1dc1-444a-9a18-a483b0be57db

# Grant Contributor to scb2@duke.edu on this RG only
az role assignment create \
  --assignee 1a565cd2-3dd5-44d1-a529-e3ca8ceff46c \
  --role Contributor \
  --scope /subscriptions/2c69c8ba-1dc1-444a-9a18-a483b0be57db/resourceGroups/rg-dcri-prod-sage
```

---

### B3. `User Access Administrator` scoped to `ai-foundry-dcri-sage` only

**What it allows:** Grant managed identities (on Container Apps I deploy) access to the
AI Foundry endpoint, so deployed apps can authenticate without storing credentials.
Scoped to one resource only — cannot affect anything else.

**Commands for IT to run:**
```bash
az role assignment create \
  --assignee 1a565cd2-3dd5-44d1-a529-e3ca8ceff46c \
  --role "User Access Administrator" \
  --scope /subscriptions/2c69c8ba-1dc1-444a-9a18-a483b0be57db/resourceGroups/rg-dcri-prod-ai-foundry/providers/Microsoft.CognitiveServices/accounts/ai-foundry-dcri-sage
```

---

### Part B Summary

| Permission | Scope | What It Unlocks |
|---|---|---|
| `Application Developer` (Azure AD role) | Tenant — own apps only | Create app registrations without a ticket |
| `Contributor` on `rg-dcri-prod-sage` | One new resource group | Deploy Container Apps, Storage, ACR without a ticket |
| `User Access Administrator` on `ai-foundry-dcri-sage` | One AI resource | Wire managed identities to AI Foundry without a ticket |

---

## Questions for IT

1. Should Part A resources go in `rg-dcri-prod-sage` (preferred, pairs with Part B) or elsewhere?
2. Is there an existing Storage Account I can reuse for item 3?
3. Is `Application Developer` (B1) granted via PIM or directly?
