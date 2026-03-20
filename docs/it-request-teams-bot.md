# IT Request: SageJiraBot — Azure Bot Service & Graph Permissions

**Requestor:** scb2@duke.edu (DCRI SAGE Team)
**Date:** 2026-03-20
**Priority:** Medium — development complete, deployment blocked on provisioning
**Project:** SageJiraBot (portable-agent-a2a-pipeline)
**Jira epic:** ST-41 (Teams Bot — Core Bot & Review UX)

---

## What This Is

SageJiraBot is a Microsoft Teams bot that converts meeting transcripts into Jira tickets. All LLM calls go to Azure AI Foundry (`ai-foundry-dcri-sage`), which is already provisioned in the DCRI subscription. Data stays inside the Duke Health Azure tenant.

The bot code is complete and tested locally (see `docs/prp-teams-bot.md`). The following items are blocked on IT provisioning.

---

## Azure Resource Details (Already Known)

| Resource | Value |
|---|---|
| Tenant ID | `cb72c54e-4a31-4d9e-b14a-1ea36dfac94c` |
| Subscription ID | `2c69c8ba-1dc1-444a-9a18-a483b0be57db` |
| Resource group | DCRI resource group (scb2 does not have RG-level rights) |
| AI Foundry resource | `ai-foundry-dcri-sage` (already provisioned) |
| Jira project | `ST` at `https://dcri.atlassian.net` |
| Requesting user | `scb2@duke.edu` |

---

## Item 1: Azure Bot Service Resource

**What to create:**
An Azure Bot Service resource in the DCRI subscription.

**Suggested name:** `sagejirabot`
**Subscription:** `2c69c8ba-1dc1-444a-9a18-a483b0be57db`
**Resource group:** DCRI resource group (IT to determine appropriate RG)
**Bot type:** Azure Bot (multi-tenant or single-tenant, single-tenant preferred)
**Messaging endpoint:** Will be provided by developer after container deployment.
  Placeholder: `https://sagejirabot.{container-app-domain}/api/messages`

**What the developer needs after provisioning:**
- `BOT_APP_ID` (the Azure AD Application ID / client ID)
- `BOT_APP_PASSWORD` (a client secret OR configure federated identity — see note below)

**Security note:** For production, we prefer to use **managed identity + federated credential** instead of a client secret (BOT_APP_PASSWORD). This avoids secrets entirely. Please configure the bot to use the container's managed identity as the bot credential if possible. If a client secret is required, it should be stored in Azure Key Vault, not in .env.

---

## Item 2: Teams App Approval

**What to submit:**
Upload the Teams app manifest ZIP to Teams Admin Center for approval.

The manifest template is at:
```
bot/teams/manifest/manifest.json
```

**Steps:**
1. IT provisions Azure Bot Service and provides the Bot App ID (GUID)
2. Developer fills `botId` and `id` fields in `manifest.json` with the Bot App ID
3. Developer adds two PNG icon files (192x192 color, 32x32 outline) — DCRI branding
4. Developer ZIPs the manifest directory: `zip sagejirabot.zip manifest.json color.png outline.png`
5. IT uploads the ZIP to **Teams Admin Center > Manage apps > Upload an app**
6. IT approves the app for installation in DCRI tenant channels

**Scopes requested:**
- `team` (install in Teams channels)
- `groupChat` (install in group chats and meeting chats)
- `personal` (install for individual users)

---

## Item 3: Microsoft Graph API Permissions

**App registration to receive permissions:**
The bot's managed identity or the Azure Bot Service app registration.

**Required application permissions (admin consent required for all):**

| Permission | Why Needed | Phase |
|---|---|---|
| `OnlineMeetings.Read.All` | Read meeting metadata and transcript IDs from Graph API | Phase 2 |
| `OnlineMeetingTranscript.Read.All` | Download meeting transcript content (VTT format) | Phase 2 |
| `ChannelMessage.Send` | Post Adaptive Card messages to Teams channels | Phase 2 |
| `TeamsActivity.Send` | Send activity notifications to Teams users | Phase 2 |
| `Mail.Read` | Read mailbox for the existing email pipeline (separate request, low priority) | Future |

**Note:** `Mail.Read` is for the pre-existing Outlook email pipeline, not the bot. It can be deferred.

**Exact strings for admin consent screen:**
```
Microsoft Graph — Application Permissions:
  - OnlineMeetings.Read.All
  - OnlineMeetingTranscript.Read.All
  - ChannelMessage.Send
  - TeamsActivity.Send
```

---

## Item 4: Azure Container Apps Environment

**What to create:**
An Azure Container Apps environment for hosting the bot service.

**Suggested name:** `sagejirabot-env`
**Subscription:** `2c69c8ba-1dc1-444a-9a18-a483b0be57db`
**Region:** East US (or same region as AI Foundry resource)

**Container App within the environment:**
- **Name:** `sagejirabot`
- **Image:** Developer will push to Azure Container Registry (or provide a Docker image)
- **Port:** 9000
- **Min replicas:** 1 (bot must always be running to receive messages)
- **Max replicas:** 3
- **Ingress:** External, HTTPS, port 9000

**After the environment is created, the developer needs:**
- The Container App FQDN (e.g., `sagejirabot.{hash}.eastus.azurecontainerapps.io`)
  to update the Bot Service messaging endpoint.

---

## Item 5: Managed Identity for Bot Container

**What to configure:**
Assign a **system-assigned managed identity** to the `sagejirabot` Container App.

**Role assignment needed:**
After the managed identity is created (it gets a principal ID), grant it:

| Role | Resource | Why |
|---|---|---|
| `Cognitive Services OpenAI User` | `ai-foundry-dcri-sage` AIServices resource | Allows LLM calls from the container without API keys |

**Az CLI command (developer to run after IT creates the managed identity):**
```bash
# Replace {principal-id} with the managed identity's object/principal ID
az role assignment create \
  --role "Cognitive Services OpenAI User" \
  --assignee {principal-id} \
  --scope /subscriptions/2c69c8ba-1dc1-444a-9a18-a483b0be57db/resourceGroups/{rg-name}/providers/Microsoft.CognitiveServices/accounts/ai-foundry-dcri-sage
```

**Note:** The developer (scb2) does NOT have rights to create role assignments on the resource group. IT needs to execute this command or grant the developer the necessary permissions.

---

## Summary Checklist for IT

- [ ] Create Azure Bot Service resource `sagejirabot` in DCRI subscription
- [ ] Provide developer with `BOT_APP_ID` and `BOT_APP_PASSWORD` (or configure federated identity)
- [ ] Grant Microsoft Graph application permissions:
  - [ ] `OnlineMeetings.Read.All`
  - [ ] `OnlineMeetingTranscript.Read.All`
  - [ ] `ChannelMessage.Send`
  - [ ] `TeamsActivity.Send`
- [ ] Provide admin consent for all Graph permissions
- [ ] Create Azure Container Apps environment `sagejirabot-env`
- [ ] Provide Container App FQDN after creation
- [ ] Assign system-assigned managed identity to the container
- [ ] Grant managed identity `Cognitive Services OpenAI User` on `ai-foundry-dcri-sage`
- [ ] Upload Teams app manifest ZIP to Teams Admin Center (developer provides ZIP)
- [ ] Approve Teams app for DCRI tenant

---

## Timeline

| Milestone | Dependency |
|---|---|
| Phase 1 (FastAPI endpoint) | None — already complete, runs locally |
| Phase 2 (Bot receives Teams messages) | Azure Bot Service + Teams App approval |
| Phase 2.x (Transcript fetch from real meetings) | Graph API permissions |
| Production deployment | Container Apps environment + managed identity |

Phase 1 is fully functional today. The bot code is code-complete for Phase 2.
All of Phase 2 activation is blocked on this IT request.

---

## Contact

Developer: scb2@duke.edu
Repository: `portable-agent-a2a-pipeline` on the DCRI GitHub organization
Questions: please reply to this ticket or ping the SAGE team channel.
