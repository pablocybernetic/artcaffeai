#!/usr/bin/env bash
# schedule_snapshot.sh
# OS-level daily snapshot trigger — fallback when pg_cron/pg_net is unavailable.
#
# Installation (on the VM at 136.115.140.77):
#   chmod +x /opt/artcaffe/scripts/schedule_snapshot.sh
#   sudo crontab -e
#   # Add this line (runs daily at 06:00 EAT = 03:00 UTC):
#   0 3 * * * FASTAPI_API_KEY=your_key /opt/artcaffe/scripts/schedule_snapshot.sh >> /var/log/artcaffe-snapshot.log 2>&1

set -euo pipefail

API_KEY="${FASTAPI_API_KEY:-}"
API_URL="${ARTCAFFE_API_URL:-http://127.0.0.1:8000}"

echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] Starting daily snapshot..."

HTTP_STATUS=$(curl -s -o /tmp/snapshot_response.json -w "%{http_code}" \
  -X POST \
  -H "Content-Type: application/json" \
  ${API_KEY:+-H "X-Api-Key: ${API_KEY}"} \
  "${API_URL}/data/snapshot" \
  -d '{}')

if [ "${HTTP_STATUS}" = "200" ]; then
  echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] Snapshot succeeded (HTTP 200)."
  python3 -m json.tool /tmp/snapshot_response.json 2>/dev/null || cat /tmp/snapshot_response.json
else
  echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] Snapshot FAILED (HTTP ${HTTP_STATUS})."
  cat /tmp/snapshot_response.json
  exit 1
fi
