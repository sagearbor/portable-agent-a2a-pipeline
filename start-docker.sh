#!/usr/bin/env bash
# Ensure PATH includes az CLI and docker (needed when run from cron)
export PATH="/dcri/sasusers/home/scb2/.local/bin:/usr/bin:/usr/local/bin:$PATH"

# start-docker.sh — Launch SageJiraBot in Docker with Azure auth
#
# On a dev VM, 'az login' tokens can't be used inside Docker (no az CLI).
# This script fetches a bearer token from az CLI and passes it as
# AZURE_OPENAI_KEY so the container can use api_key auth mode.
#
# The token lasts ~1 hour. For a long demo, re-run this script.
#
# Usage:
#   ./start-docker.sh          # build + start
#   ./start-docker.sh rebuild  # force rebuild + start

set -euo pipefail

echo "Fetching Azure bearer token from az CLI..."
AZURE_TOKEN=$(az account get-access-token \
  --resource https://cognitiveservices.azure.com \
  --query accessToken -o tsv)

if [ -z "$AZURE_TOKEN" ]; then
  echo "ERROR: Could not get Azure token. Run 'az login' first."
  exit 1
fi

echo "Token obtained (expires in ~1 hour)"

# Export so docker-compose picks it up alongside .env
export AZURE_OPENAI_KEY="$AZURE_TOKEN"
export AZURE_AUTH_MODE="api_key"

if [ "${1:-}" = "rebuild" ]; then
  echo "Rebuilding Docker image..."
  docker compose build --quiet
fi

echo "Starting SageJiraBot on port 3006..."
docker compose down 2>/dev/null || true
docker compose up -d

echo ""
echo "Waiting for health check..."
sleep 5

STATUS=$(curl -s -o /dev/null -w "%{http_code}" http://localhost:3006/health)
if [ "$STATUS" = "200" ]; then
  echo "SageJiraBot is running at http://localhost:3006"
  echo ""
  curl -s http://localhost:3006/health | python3 -m json.tool
else
  echo "WARNING: Health check returned $STATUS"
  docker compose logs --tail 20
fi
