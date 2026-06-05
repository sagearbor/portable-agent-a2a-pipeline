#!/usr/bin/env bash
# Ensure PATH includes az CLI and docker (needed when run from cron)
export PATH="/dcri/sasusers/home/scb2/.local/bin:/usr/bin:/usr/local/bin:$PATH"

# start-docker.sh — Launch SageJiraBot in Docker with Azure auth
#
# On a dev VM, 'az login' tokens can't be used inside Docker (no az CLI).
# This script fetches a bearer token from az CLI and passes it as
# AZURE_OPENAI_KEY so the container can use api_key auth mode.
#
# The Azure token lasts ~1 hour, so the container must be recreated to inject
# a fresh one. A cron does that every 45 min (see `crontab -l`). Because the
# token is an env var set at container-creation time, "refresh the token" and
# "restart the container" are the same operation.
#
# ONE COMMAND for every situation — just run `./start-docker.sh`:
#   * Cron token refresh (no code changed) -> recreates container, NO rebuild.
#   * After you edit code                  -> auto-detects the change, rebuilds,
#                                             then recreates.
#   * Force a clean rebuild (e.g. new deps in requirements.txt that Docker's
#     layer cache is holding stale) -> `./start-docker.sh rebuild` (no-cache).
#
# You should never need to remember a flag for normal use.

set -euo pipefail

cd "$(dirname "$0")"

# ---------------------------------------------------------------------------
# Pick the compose command available on this host. Newer Docker ships the v2
# plugin ("docker compose"); some machines only have the legacy v1 binary
# ("docker-compose"). Auto-detect so the same script works on both.
# ---------------------------------------------------------------------------
if docker compose version >/dev/null 2>&1; then
  DC=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
  DC=(docker-compose)
else
  echo "ERROR: neither 'docker compose' (v2 plugin) nor 'docker-compose' (v1) is installed." >&2
  exit 1
fi

SERVICE="sagejirabot"

# ---------------------------------------------------------------------------
# Azure bearer token (api_key mode for Docker — see header)
# ---------------------------------------------------------------------------
echo "Fetching Azure bearer token from az CLI..."
AZURE_TOKEN=$(az account get-access-token \
  --resource https://cognitiveservices.azure.com \
  --query accessToken -o tsv)

if [ -z "$AZURE_TOKEN" ]; then
  echo "ERROR: Could not get Azure token. Run 'az login' first." >&2
  exit 1
fi
echo "Token obtained (expires in ~1 hour)"

export AZURE_OPENAI_KEY="$AZURE_TOKEN"
export AZURE_AUTH_MODE="api_key"

# ---------------------------------------------------------------------------
# Decide whether to rebuild the image.
#
#   needs_build == 0 (true)  when there is no image yet, or any build input
#                            (Dockerfile / requirements.txt / bot/ / core/) is
#                            newer than the currently-built image.
#
# This is what makes ONE command correct for both cron and dev: cron runs see
# no source change and skip the rebuild (fast token refresh); a run right after
# you save a file rebuilds automatically.
# ---------------------------------------------------------------------------
needs_build() {
  local image_id img_epoch newest_src
  image_id=$("${DC[@]}" images -q "$SERVICE" 2>/dev/null | head -1)
  [ -z "$image_id" ] && return 0   # no image built yet -> build

  img_epoch=$(date -d "$(docker image inspect -f '{{.Created}}' "$image_id" 2>/dev/null)" +%s 2>/dev/null || echo 0)

  # Newest mtime across everything the Docker build COPYs in.
  newest_src=$(find Dockerfile requirements.txt bot core \
                 -type f -printf '%T@\n' 2>/dev/null \
                 | sort -n | tail -1 | cut -d. -f1)
  [ -z "$newest_src" ] && return 1

  [ "$newest_src" -gt "$img_epoch" ]
}

MODE="${1:-auto}"

if [ "$MODE" = "rebuild" ]; then
  echo "Forcing a clean (no-cache) rebuild..."
  "${DC[@]}" build --no-cache "$SERVICE"
elif needs_build; then
  echo "Source changed since the last image — rebuilding..."
  # If the build fails (e.g. a half-saved edit when cron fires), keep going:
  # we still recreate the container below with the EXISTING image so the token
  # refresh never gets blocked by a transient build error.
  "${DC[@]}" build "$SERVICE" || echo "WARN: build failed — recreating with the existing image."
else
  echo "No source changes — skipping rebuild (token refresh only)."
fi

# --force-recreate guarantees the container is recreated so the freshly-fetched
# token is injected, even when the image itself didn't change.
echo "Starting SageJiraBot on port 3006..."
"${DC[@]}" up -d --force-recreate "$SERVICE"

# ---------------------------------------------------------------------------
# Health check — poll instead of a fixed sleep so a slow start still passes.
# ---------------------------------------------------------------------------
echo ""
echo "Waiting for health check..."
STATUS=000
for _ in $(seq 1 15); do
  STATUS=$(curl -s -o /dev/null -w "%{http_code}" http://localhost:3006/health || true)
  [ "$STATUS" = "200" ] && break
  sleep 2
done

if [ "$STATUS" = "200" ]; then
  echo "SageJiraBot is running at http://localhost:3006"
  echo ""
  curl -s http://localhost:3006/health | python3 -m json.tool
else
  echo "WARNING: Health check returned $STATUS"
  "${DC[@]}" logs --tail 20 "$SERVICE"
fi
