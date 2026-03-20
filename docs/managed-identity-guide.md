# Managed Identity Guide

> How authentication works in this project — locally and in Azure.
> Read this when you forget how tokens work or when onboarding someone.

---

## Why Managed Identity (not API Keys)

| | API Key | Managed Identity |
|---|---|---|
| What it is | A static password string | Azure issues short-lived tokens to verified identities |
| Leak risk | High — can leak in .env, logs, chat, git | None — no password exists to leak |
| Rotation | Manual — you have to remember | Automatic — tokens expire in ~1 hour, auto-refresh |
| Works locally | Yes | Yes (via `az login`) |
| Works in Azure | Yes | Yes (via system-assigned identity) |
| Org policy | Being phased out at Duke | Required going forward |

**Bottom line:** Managed identity = no secrets to manage. The code is the same everywhere.

---

## How DefaultAzureCredential Works

The `azure-identity` library's `DefaultAzureCredential` tries a chain of identity sources, in order:

| # | Source | When it's used |
|---|---|---|
| 1 | Environment variables (`AZURE_CLIENT_ID`, etc.) | CI/CD pipelines |
| 2 | Workload Identity | Kubernetes pods |
| 3 | **Managed Identity** | Code running inside Azure (Container App, VM, App Service) |
| 4 | **Azure CLI (`az login`)** | **Your laptop — this is what you use for local dev** |
| 5 | Azure PowerShell | If you use pwsh |
| 6 | Azure Developer CLI (`azd`) | Less common |
| 7 | Interactive browser | Last resort fallback |

It stops at the first one that succeeds. Your code never specifies which — it just calls `DefaultAzureCredential()` and the right thing happens.

---

## Local Development

### One-time setup (or after token expires)

```bash
az login --tenant "cb72c54e-4a31-4d9e-b14a-1ea36dfac94c"
az account set --subscription "2c69c8ba-1dc1-444a-9a18-a483b0be57db"
```

### What this does

- Opens a browser, you sign in as scb2@duke.edu
- Stores a **refresh token** in `~/.azure/` (a set of JSON files)
- `DefaultAzureCredential()` finds this token automatically

### Does it persist after reboot?

**Yes.** The `~/.azure/` tokens survive reboots. They typically expire after:
- ~90 days of inactivity (depends on your org's AAD policy)
- Or when your org forces re-auth

In practice you'll `az login` once every few weeks. When it expires, you get a clear error like `DefaultAzureCredential failed to retrieve a token` — just run `az login` again.

### Verify your session

```bash
az account show --query "{subscription:name, user:user.name}" -o table
```

Expected:
```
Subscription       User
-----------------  ----------------
dhp-dcri-prod-sub  scb2@duke.edu
```

---

## How the Code Uses It

In `clients/client.py`:

```python
from azure.identity import DefaultAzureCredential, get_bearer_token_provider

token_provider = get_bearer_token_provider(
    DefaultAzureCredential(),
    "https://cognitiveservices.azure.com/.default"
)

client = AzureOpenAI(
    azure_endpoint=endpoint,
    azure_ad_token_provider=token_provider,  # <-- no api_key parameter
    api_version=api_version,
)
```

The `get_bearer_token_provider()` wrapper handles auto-refresh. If the 1-hour access token expires mid-session, it silently gets a new one using the refresh token. You never think about it.

### .env file — what you need

```bash
AZURE_OPENAI_ENDPOINT=https://ai-foundry-dcri-sage.cognitiveservices.azure.com/
# That's it. No keys. Token comes from az login / managed identity.
```

---

## Deployed in Azure (Containers)

When your code runs inside Azure (Container Apps, App Service, VM), managed identity works differently but your code stays the same.

### How it works

1. You create a Container App with **system-assigned managed identity** enabled
2. Azure gives that container its own identity (like a service account, but with no password)
3. You grant that identity permission to call your AI Foundry endpoint
4. `DefaultAzureCredential()` detects it's running in Azure and uses the managed identity automatically

### The commands (run at deploy time, not during dev)

```bash
# 1. Create container app with identity enabled
az containerapp create \
  --name sage-pipeline \
  --resource-group rg-dcri-prod-ai-foundry \
  --image your-registry/sage-pipeline:latest \
  --system-assigned   # <-- enables managed identity

# 2. Get the container's identity principal ID
PRINCIPAL_ID=$(az containerapp identity show \
  --name sage-pipeline \
  --resource-group rg-dcri-prod-ai-foundry \
  --query principalId -o tsv)

# 3. Grant "Cognitive Services OpenAI User" role on the AI Foundry resource
az role assignment create \
  --assignee $PRINCIPAL_ID \
  --role "Cognitive Services OpenAI User" \
  --scope "/subscriptions/2c69c8ba-1dc1-444a-9a18-a483b0be57db/resourceGroups/rg-dcri-prod-ai-foundry/providers/Microsoft.CognitiveServices/accounts/ai-foundry-dcri-sage"
```

**What each does:**
- `--system-assigned`: Azure creates a hidden service principal for this container. No password, can't be used from outside Azure.
- `role assignment create`: Tells Azure "this container is allowed to call the OpenAI endpoint". Without this, the container gets 403 Forbidden.

### The flow — same code, different token source

```
LOCAL DEV                           DEPLOYED IN AZURE
─────────                           ─────────────────
You run: az login                   Container starts up
   │                                   │
   ▼                                   ▼
~/.azure/ stores                    Azure injects identity
refresh token                       token into the container
   │                                   │
   ▼                                   ▼
DefaultAzureCredential()  ◄── same code ──►  DefaultAzureCredential()
picks up az login                        picks up managed identity
   │                                   │
   ▼                                   ▼
Gets bearer token                   Gets bearer token
   │                                   │
   ▼                                   ▼
Calls Azure AI Foundry    ◄── same call ──►  Calls Azure AI Foundry
```

---

## WSL vs Windows Native

`az login` tokens are stored per environment:
- **Windows native**: `C:\Users\scb2\.azure\`
- **WSL**: `/home/scb2/.azure/` (separate from Windows)

They do **not** share sessions. You need to `az login` separately in each. This is usually fine — just be aware if you switch between them.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `DefaultAzureCredential failed to retrieve a token` | az login expired | `az login --tenant "cb72c54e-..."` |
| `AADSTS700082: The refresh token has expired` | Idle too long / org policy | `az login` again |
| `403 Forbidden` on API call | Your identity doesn't have the right role | Check role assignment (need "Cognitive Services OpenAI User") |
| Works on Windows, fails in WSL | Separate token stores | `az login` in WSL separately |
| Works locally, fails in container | Managed identity not enabled or no role assigned | Enable `--system-assigned` + create role assignment |

---

## Key Resource Info

| Item | Value |
|------|-------|
| Tenant ID | `cb72c54e-4a31-4d9e-b14a-1ea36dfac94c` (Duke Health AAD) |
| Subscription | `dhp-dcri-prod-sub` / `2c69c8ba-1dc1-444a-9a18-a483b0be57db` |
| Resource group | `rg-dcri-prod-ai-foundry` |
| AI Foundry resource | `ai-foundry-dcri-sage` |
| Endpoint | `https://ai-foundry-dcri-sage.cognitiveservices.azure.com/` |
| Required role | `Cognitive Services OpenAI User` |

> These IDs are not secrets — they are organizational identifiers (like a company address).
> API keys, tokens, and client secrets ARE secrets and belong only in `.env` (gitignored).
