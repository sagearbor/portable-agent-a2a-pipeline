#!/usr/bin/env bash
# Ensure PATH includes az CLI and docker (needed when run from cron)
export PATH="/dcri/sasusers/home/scb2/.local/bin:/usr/bin:/usr/local/bin:$PATH"

# start-docker.sh — Launch SageJiraBot in Docker (legacy dev-VM path)
#
# LLM auth is handled by core/dcri_llm.py. The container has no az CLI session
# and no managed identity, so this path needs LLM_AUTH=key and
# AZURE_OPENAI_API_KEY in .env. (It used to fetch the *personal* `az login`
# token of whoever ran it and inject it as the "key"; that is gone: no personal
# identity is ever used as the app's credential.) The preferred deployment is
# Azure Container Apps with the shared managed identity.
#
# ONE COMMAND for every situation — just run `./start-docker.sh`:
#   * After you edit code -> auto-detects the change, rebuilds, then recreates.
#   * Force a clean rebuild (e.g. new deps in requirements.txt that Docker's
#     layer cache is holding stale) -> `./start-docker.sh rebuild` (no-cache).

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
# Decide whether to rebuild the image.
#
#   needs_build == 0 (true)  when there is no image yet, or any build input
#                            (Dockerfile / requirements.txt / bot/ / core/) is
#                            newer than the currently-built image.
#
# A run right after you save a file rebuilds automatically; otherwise the
# existing image is reused.
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
  # If the build fails (e.g. a half-saved edit), keep going with the EXISTING image.
  "${DC[@]}" build "$SERVICE" || echo "WARN: build failed — recreating with the existing image."
else
  echo "No source changes — skipping rebuild."
fi

# --force-recreate picks up .env changes even when the image didn't change.
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
