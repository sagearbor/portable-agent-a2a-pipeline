# Azure AI Foundry Setup Commands
> Runbook for connecting to the DCRI AI Foundry resource.
> A colleague can follow this to recreate the setup (swap in their own values where noted).

---

## Prerequisites
- Azure CLI installed: `az --version` (we used 2.77.0)
- Azure Developer CLI installed: `azd version` (we used 1.23.6)

---

## Step 1 — Log in to Azure

```bash
az logout   # clear any stale session (safe to skip if not logged in)
az login --tenant "cb72c54e-4a31-4d9e-b14a-1ea36dfac94c"
```

A browser window will open. Sign in with your Duke (@duke.edu) account.
You will be shown a numbered list of subscriptions — pick **dhp-dcri-prod-sub**.

> **Colleague note:** The tenant ID `cb72c54e-...` is the Duke Health AAD tenant.
> It will be the same for any Duke Health account.

---

## Step 2 — Set the correct subscription

```bash
az account set --subscription "2c69c8ba-1dc1-444a-9a18-a483b0be57db"
```

> The subscription name is `dhp-dcri-prod-sub`.
> Using the ID (not the name) is safer — IDs never change.

---

## Step 3 — Verify you are in the right place

```bash
az account show --query "{subscription:name, id:id, user:user.name}" -o table
```

Expected output:
```
Subscription       User
-----------------  -------------
dhp-dcri-prod-sub  <your-id>@duke.edu
```

---

## Step 4 — Fix broken ml extension (if needed)

The `az ml` extension can get corrupted if interrupted during auto-install.
If any `az` command fails with an error about `cliextensions/ml`, run:

```bash
rm -rf ~/.azure/cliextensions/ml
```

Then reinstall cleanly if needed:
```bash
az extension add --name ml
```

---

## Step 5 — Python environment setup

```bash
cd sageTestAzAgents_01
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

Copy the env template and fill in your values:
```bash
cp .env.example .env
# Edit .env:
#   AZURE_OPENAI_ENDPOINT is required for azure providers
#   AZURE_OPENAI_KEY only needed if AZURE_AUTH_MODE=api_key
#   OPENAI_API_KEY needed for openai_responses or openai_chat providers
```

---

## Step 6 — Run Phase 1 pipeline (pure Python, no Azure compute needed)

```bash
python -m orchestration.pipeline
```

This runs all three agents locally using whichever PROVIDER is set in config/settings.py.
No az commands needed for this step.

---

## Key Resource Info

| Item | Value |
|------|-------|
| Tenant | `cb72c54e-4a31-4d9e-b14a-1ea36dfac94c` |
| Subscription name | `dhp-dcri-prod-sub` |
| Subscription ID | `2c69c8ba-1dc1-444a-9a18-a483b0be57db` |
| Resource group | `rg-dcri-prod-ai-foundry` |
| AI Foundry resource | `ai-foundry-dcri-sage` |
| Endpoint | `https://ai-foundry-dcri-sage.cognitiveservices.azure.com/` |
| Location | `eastus2` |
| Deployed models | `gpt-5.2`, `gpt-5.3-codex` |

---

## Notes
- `scb2@duke.edu` has rights on the AIServices resource directly
  but NOT on the resource group (cannot list/read `rg-dcri-prod-ai-foundry`)
- No AI Foundry Projects exist yet — agents require a Project to be created first
