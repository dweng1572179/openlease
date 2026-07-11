#!/bin/bash
# Start OpenLease locally. Default port 8788 (OpenProp holds 8787); override with
# OPENLEASE_PORT=9000 ./run.sh
set -e
cd "$(dirname "$0")"
PORT="${OPENLEASE_PORT:-8788}"
[ -x .venv/bin/uvicorn ] && UV=.venv/bin/uvicorn || UV=uvicorn
echo "OpenLease -> http://localhost:$PORT   (Ctrl-C to stop)"
exec "$UV" app.app:app --host 127.0.0.1 --port "$PORT"
