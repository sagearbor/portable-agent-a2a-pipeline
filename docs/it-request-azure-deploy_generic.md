# IT Request: Standing Azure Permissions for AI Agent Development

**Requestor:** scb2@duke.edu
**Subscription:** `2c69c8ba-1dc1-444a-9a18-a483b0be57db`
**Tenant:** `cb72c54e-4a31-4d9e-b14a-1ea36dfac94c`
**My object ID:** `1a565cd2-3dd5-44d1-a529-e3ca8ceff46c`
**Date:** 2026-03-20

---

## Context

I am tasked with evaluating iand building AI agents at DCRI using Azure AI
Foundry (`ai-foundry-dcri-sage`). This involves connecting LLMs to enterprise systems —
Teams, Jira, Confluence, SharePoint, Outlook, and Azure services (Blob, Table, Container
Apps) — and deploying those integrations as containerized applications.

Each new project could currently require multiple IT tickets for infrastructure that is
routine in AI development. This creates delays and friction which stop work. 
The three permissions below, all tightly scoped, would resolve this.

---

## Requested Permissions

### 1. Azure AD `Application Developer` role

**What it allows:** Create and manage my own OAuth app registrations — required for
any integration that uses Azure AD sign-in (Teams bots, web UIs, API clients for
Confluence/SharePoint/Outlook). Cannot grant admin consent. Cannot access or modify
other users' apps.

**Current blocker:** Attempting to open App Registrations in portal.azure.com returns
Error 401 / "You do not have access." Every integration that requires identity
verification is blocked until IT creates the registration manually.

**Command for IT:**
```bash
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

### 2. `Contributor` on a new dedicated resource group `rg-dcri-prod-sage`

**What it allows:** Create and manage Azure resources (Container Apps, Storage Accounts,
Container Registries) within this one resource group only. No access to any other
resource group or subscription resources.

**Why needed:** AI agents need to be deployed somewhere. A typical project requires:
a Container App to run the agent, a Storage Account for state persistence, and a
Container Registry to hold the Docker image. Each of these is currently a separate IT
ticket. A dedicated isolated resource group removes that friction for all future projects.

**Commands for IT:**
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

### 3. `User Access Administrator` scoped to `ai-foundry-dcri-sage` only

**What it allows:** Grant managed identities (on Container Apps I deploy) access to the
AI Foundry endpoint, so deployed applications authenticate as themselves via managed
identity — no credentials stored anywhere. Scoped to one resource; cannot affect
anything else in the subscription.

**Why needed:** I have `Cognitive Services OpenAI Contributor` on `ai-foundry-dcri-sage`
for my personal account. When I deploy a Container App, its managed identity also needs
this role. Currently I cannot grant it myself, so every deployment requires IT to run
one role-assignment command.

**Command for IT:**
```bash
az role assignment create \
  --assignee 1a565cd2-3dd5-44d1-a529-e3ca8ceff46c \
  --role "User Access Administrator" \
  --scope /subscriptions/2c69c8ba-1dc1-444a-9a18-a483b0be57db/resourceGroups/rg-dcri-prod-ai-foundry/providers/Microsoft.CognitiveServices/accounts/ai-foundry-dcri-sage
```

---

## Summary

| Permission | Scope | Unblocks |
|---|---|---|
| `Application Developer` (Azure AD) | Tenant — own apps only | OAuth/SSO for any Teams, Confluence, SharePoint, Outlook integration |
| `Contributor` on `rg-dcri-prod-sage` | One new resource group | Deploy Container Apps, Storage, ACR for any AI agent project |
| `User Access Administrator` on `ai-foundry-dcri-sage` | One AI resource | Managed identity auth for deployed agents — no stored credentials |

All work remains in the Duke Azure tenant and subscription.

---

## Question for IT

Is `Application Developer` (item 1) assigned directly or via PIM?
