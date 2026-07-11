#!/bin/bash
# Double-click to start OpenLease. No Docker needed — just python3 (macOS ships it).
cd "$(dirname "$0")" || exit 1
[ -f .env ] || cp .env.example .env
if [ ! -x .venv/bin/uvicorn ]; then
  echo "First run — installing OpenLease. This takes a couple of minutes…"
  python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt || { echo "Install failed."; read -r; exit 1; }
fi
PORT="${OPENLEASE_PORT:-8788}"
echo "Starting OpenLease at http://localhost:$PORT — keep this window open. Close it to stop OpenLease."
.venv/bin/uvicorn app.app:app --host 127.0.0.1 --port "$PORT" &
for _ in $(seq 1 30); do curl -s -o /dev/null "http://localhost:$PORT/login" && break; sleep 1; done
open "http://localhost:$PORT"
wait
