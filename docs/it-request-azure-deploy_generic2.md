# IT Request: Standing Azure Permissions for AI Agent Development

**Requestor:** scb2@duke.edu — 2026-03-20

Three `az` commands are all that's needed to grant the permissions below (see end of doc).

---

## Why

I am tasked with investigating and developing AI agents in Azure AI Foundry. Each new
project requires connecting LLMs to enterprise systems (Teams, Jira, Confluence,
SharePoint, Outlook, Blob/Table Storage) and deploying the result as a Container App.
Currently each project requires 3–5 IT tickets for routine setup. During rapid prototype
testing with AI agent development, running `az` commands and iterating quickly, these
blockers create significant friction to test things in a timely way. These three
permissions — all narrowly scoped — would eliminate that friction.

| Permission | Scope | What it unblocks |
|---|---|---|
| `Application Developer` | Azure AD — own apps only | Create app registrations for OAuth/SSO integrations |
| `Contributor` | `rg-dcri-ai-dev-scb2` only (new, isolated RG — zero cost) | Deploy Container Apps, Storage Accounts, Container Registries |
| `User Access Administrator` | `ai-foundry-dcri-sage` only | Grant managed identity auth to deployed apps (no stored credentials) |

Microsoft's Cloud Adoption Framework identifies this use case directly: *"A developer
needs a controlled Azure environment to quickly test application design patterns."* A
dedicated resource group within the existing subscription is the lightest-weight way to
provide this — fully isolated, zero cost to create, and visible to IT at all times.

The `rg-dcri-ai-dev-scb2` naming pattern scales cleanly to other developers
(e.g. `rg-dcri-ai-dev-<alias>`) — each person gets their own isolated sandbox with no
ability to affect others' resources. The same three commands apply per person.

**One question:** Is `Application Developer` assigned directly or via PIM?

---

## The Three Commands

```bash
# 1. Application Developer (Azure AD) — allows creating OAuth/SSO app registrations
az rest --method POST \
  --uri "https://graph.microsoft.com/v1.0/roleManagement/directory/roleAssignments" \
  --headers "Content-Type=application/json" \
  --body '{
    "principalId": "1a565cd2-3dd5-44d1-a529-e3ca8ceff46c",
    "roleDefinitionId": "cf1c38e5-3621-4004-a7cb-879624dced7c",
    "directoryScopeId": "/"
  }'

# 2. Create a per-developer resource group and grant Contributor on it only
az group create --name rg-dcri-ai-dev-scb2 --location eastus \
  --subscription 2c69c8ba-1dc1-444a-9a18-a483b0be57db

az role assignment create \
  --assignee 1a565cd2-3dd5-44d1-a529-e3ca8ceff46c \
  --role Contributor \
  --scope /subscriptions/2c69c8ba-1dc1-444a-9a18-a483b0be57db/resourceGroups/rg-dcri-ai-dev-scb2

# 3. User Access Administrator scoped to one AI resource only
az role assignment create \
  --assignee 1a565cd2-3dd5-44d1-a529-e3ca8ceff46c \
  --role "User Access Administrator" \
  --scope /subscriptions/2c69c8ba-1dc1-444a-9a18-a483b0be57db/resourceGroups/rg-dcri-prod-ai-foundry/providers/Microsoft.CognitiveServices/accounts/ai-foundry-dcri-sage
```
