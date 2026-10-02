#!/usr/bin/env bash
# Fire a fake alert through the whole flow. Requires the app to be running:
#   uvicorn src.app:app --reload
set -euo pipefail
BASE="${1:-http://localhost:8000}"

echo "1) Breaking the demo service..."
curl -s -X POST "$BASE/demo/break" | python3 -m json.tool

echo -e "\n2) Sending an alert..."
RESP=$(curl -s -X POST "$BASE/webhook/alert" \
  -H 'Content-Type: application/json' \
  -d '{"event_id":"evt-demo-1","name":"High 5xx error rate","description":"500 errors after deploy","service":"web"}')
echo "$RESP" | python3 -m json.tool

ACTION_ID=$(echo "$RESP" | python3 -c "import sys,json;print(json.load(sys.stdin)['action_id'])")
echo -e "\n   -> Open the approval page: $BASE/approve/$ACTION_ID"
echo "   -> Or approve from the CLI:  python -m scripts.cli approve $ACTION_ID"
